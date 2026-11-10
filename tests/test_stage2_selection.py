import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from benchmark_av_sync.stage2_contract import SOURCE_BINDING, build_stage2_complete_event
from benchmark_av_sync.stage2_release import validate_installed_release
from benchmark_av_sync.stage2_selection import (
    DELIVERY_SPECS,
    evaluate_row,
    select_row_file,
)


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _attachment(token: str, name: str, label: str) -> dict:
    return {"file_token": token, "name": name, "size_bytes": 100 + len(label), "sha256": _sha(label)}


def _snapshot() -> dict:
    record_id = "rec-selection-001"
    return {
        "schema_version": 1,
        "record_id": record_id,
        "video_number": "mx-selection-001",
        "source_binding": dict(SOURCE_BINDING),
        "fields": {
            "素材1": [
                _attachment("video001", "001_main.mp4", "main001"),
                _attachment("video002", "002_main.mp4", "main002"),
            ],
            "素材2": [
                _attachment("zip001", f"omni_stage1__v3.2__{record_id}__001.zip", "zip001"),
                _attachment("zip002", f"omni_stage1__v3.2__{record_id}__002.zip", "zip002"),
            ],
            "素材3": [],
            "执行记录": "",
            "ZED备注": "不影响选择",
        },
        "execution_events": [
            {
                "phase": "stage1_complete",
                "remote_readback_verified": True,
                "updated_at": "2026-11-02T03:00:00+08:00",
            }
        ],
    }


def _delivery_attachment(snapshot: dict, artifact_key: str) -> dict:
    field_name, suffix = DELIVERY_SPECS[artifact_key]
    return _attachment(
        f"out-{artifact_key}",
        f"omni_stage2__{snapshot['record_id']}__{suffix}",
        f"out-{artifact_key}",
    )


def _complete_event(snapshot: dict, initial: dict, deliveries: list[dict]) -> dict:
    pair_results = []
    for index, (main, package) in enumerate(
        zip(snapshot["fields"]["素材1"], snapshot["fields"]["素材2"]), start=1
    ):
        pair_results.append(
            {
                "pair_id": f"{index:03d}",
                "input": {
                    "reference_package_sha256": package["sha256"],
                    "generated_video_sha256": main["sha256"],
                },
                "output": {"sha256": _sha(f"output-{index}")},
                "subtitle": {"source_mode": "stage1", "quality_status": "pass"},
                "status": "pass",
                "review_required": False,
            }
        )
    return build_stage2_complete_event(
        {
            "ok": True,
            "status": "pass",
            "record_id": snapshot["record_id"],
            "video_number": snapshot["video_number"],
            "dispatch_key": initial["dispatch_key"],
            "input_fingerprint": initial["input_fingerprint"],
            "strict_release": initial["strict_release"],
            "attempt": 1,
            "runtime_receipt_sha256": _sha("runtime"),
            "pair_results": pair_results,
            "deliveries": deliveries,
            "written_at": "2026-11-07T08:00:00+08:00",
        },
        remote_readback_verified=True,
    )


class Stage2SelectionTests(unittest.TestCase):
    def test_selects_verified_stage1_row_and_ignores_zed_note(self):
        snapshot = _snapshot()
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "selected_initial")
        self.assertEqual(result["pair_ids"], ["001", "002"])
        self.assertEqual(result["delivery_policy"]["mode"], "append")
        self.assertFalse(result["zed_note_used"])
        self.assertTrue(result["dispatch_key"].startswith("omni-stage2-v32-resource07|rec-selection-001|"))
        self.assertEqual(result["strict_release"], validate_installed_release())

    def test_refuses_missing_or_nonmatching_stage1_pairs(self):
        snapshot = _snapshot()
        snapshot["fields"]["素材2"] = snapshot["fields"]["素材2"][:1]
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "ineligible_stage1")
        self.assertTrue(any("two-way exact match" in reason for reason in result["reasons"]))

    def test_ineligible_stage1_emits_zed_note_update_and_continues(self):
        snapshot = _snapshot()
        snapshot["fields"]["素材2"] = snapshot["fields"]["素材2"][:1]
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "ineligible_stage1")
        self.assertTrue(result["continue_selection"])
        self.assertEqual(
            result["zed_note_update"]["value"],
            "不影响选择；现有规则不适合",
        )
        self.assertTrue(result["zed_note_update"]["changed"])

    def test_zed_note_marker_is_idempotent(self):
        snapshot = _snapshot()
        snapshot["fields"]["ZED备注"] = "omni；现有规则不适合"
        snapshot["fields"]["素材2"] = snapshot["fields"]["素材2"][:1]
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["zed_note_update"]["value"], "omni；现有规则不适合")
        self.assertFalse(result["zed_note_update"]["changed"])

    def test_rule_incompatible_failure_is_marked_and_not_retried(self):
        snapshot = _snapshot()
        initial = evaluate_row(snapshot, batch_date="2026-11-02")
        snapshot["execution_events"].append(
            {
                "phase": "stage2_failed",
                "dispatch_key": initial["dispatch_key"],
                "input_fingerprint": initial["input_fingerprint"],
                "attempt": 1,
                "failed_at": "2026-11-02T11:00:00+08:00",
                "error_class": "rule_incompatible",
                "rule_incompatible": True,
                "rule_incompatible_reason": "SYNC_UNSOLVABLE: no compatible visual pause",
            }
        )
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "rule_incompatible")
        self.assertTrue(result["continue_selection"])
        self.assertIn("现有规则不适合", result["zed_note_update"]["value"])

    def test_runtime_failure_remains_retryable_and_unmarked(self):
        snapshot = _snapshot()
        initial = evaluate_row(snapshot, batch_date="2026-11-02")
        snapshot["execution_events"].append(
            {
                "phase": "stage2_failed",
                "dispatch_key": initial["dispatch_key"],
                "input_fingerprint": initial["input_fingerprint"],
                "attempt": 1,
                "failed_at": "2026-11-02T11:00:00+08:00",
                "error_class": "runtime_error",
                "reason": "ffmpeg preflight unavailable",
            }
        )
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "retry_eligible_after_first_pass")
        self.assertFalse(result["zed_note_used"])

    def test_skips_only_when_matching_complete_and_all_four_outputs_read_back(self):
        snapshot = _snapshot()
        initial = evaluate_row(snapshot, batch_date="2026-11-02")
        deliveries = []
        for artifact_key, (field_name, _) in DELIVERY_SPECS.items():
            item = _delivery_attachment(snapshot, artifact_key)
            snapshot["fields"].setdefault(field_name, []).append(item)
            deliveries.append(
                {
                    "artifact_key": artifact_key,
                    "field_name": field_name,
                    "name": item["name"],
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                }
            )
        snapshot["execution_events"].append(
            {
                "phase": "stage2_started",
                "record_id": snapshot["record_id"],
                "dispatch_key": initial["dispatch_key"],
                "input_fingerprint": initial["input_fingerprint"],
                "strict_release": initial["strict_release"],
                "attempt": 1,
                "readback_verified": True,
                "updated_at": "2026-11-07T08:00:00+08:00",
            }
        )
        snapshot["execution_events"].append(_complete_event(snapshot, initial, deliveries))
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "skip_complete")

    def test_completion_from_other_release_is_hold(self):
        snapshot = _snapshot()
        initial = evaluate_row(snapshot, batch_date="2026-11-02")
        deliveries = []
        for artifact_key, (field_name, _) in DELIVERY_SPECS.items():
            item = _delivery_attachment(snapshot, artifact_key)
            snapshot["fields"].setdefault(field_name, []).append(item)
            deliveries.append(
                {
                    "artifact_key": artifact_key,
                    "field_name": field_name,
                    "name": item["name"],
                    "size_bytes": item["size_bytes"],
                    "sha256": item["sha256"],
                }
            )
        stale_release = dict(initial["strict_release"])
        stale_release["manifest_sha256"] = "0" * 64
        snapshot["execution_events"].append(
            {
                "phase": "stage2_started",
                "record_id": snapshot["record_id"],
                "dispatch_key": initial["dispatch_key"],
                "input_fingerprint": initial["input_fingerprint"],
                "strict_release": stale_release,
                "attempt": 1,
                "readback_verified": True,
                "updated_at": "2026-11-07T08:00:00+08:00",
            }
        )
        event = _complete_event(snapshot, initial, deliveries)
        event["strict_release"] = stale_release
        snapshot["execution_events"].append(event)
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "hold_inconsistent_completion")
        self.assertIn("strict_release", result["reason"])

    def test_completion_with_missing_remote_delivery_is_hold_not_repeat(self):
        snapshot = _snapshot()
        initial = evaluate_row(snapshot, batch_date="2026-11-02")
        snapshot["execution_events"].append(
            {
                "phase": "stage2_complete",
                "dispatch_key": initial["dispatch_key"],
                "input_fingerprint": initial["input_fingerprint"],
                "remote_readback_verified": True,
                "deliveries": [],
            }
        )
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "hold_inconsistent_completion")

    def test_partial_old_delivery_becomes_exact_rework_target_after_new_readback(self):
        snapshot = _snapshot()
        old_captioned = _attachment(
            "oldCaption001",
            f"omni_stage2__{snapshot['record_id']}__complete_captioned_d_v073.mp4",
            "old-caption",
        )
        snapshot["fields"]["素材3"].append(old_captioned)
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "selected_initial")
        self.assertEqual(result["delivery_policy"]["mode"], "replace_rework")
        self.assertEqual(result["delivery_policy"]["replacements"][0]["file_token"], "oldCaption001")
        self.assertEqual(result["delivery_policy"]["replacements"][0]["artifact_key"], "complete_captioned")

    def test_only_one_retry_is_eligible_on_the_same_batch_day(self):
        snapshot = _snapshot()
        initial = evaluate_row(snapshot, batch_date="2026-11-02")
        snapshot["execution_events"].append(
            {
                "phase": "stage2_failed",
                "dispatch_key": initial["dispatch_key"],
                "input_fingerprint": initial["input_fingerprint"],
                "attempt": 1,
                "failed_at": "2026-11-02T11:00:00+08:00",
            }
        )
        retry = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(retry["status"], "retry_eligible_after_first_pass")
        self.assertEqual(retry["attempt"], 2)
        snapshot["execution_events"].append(
            {
                "phase": "stage2_failed_final",
                "dispatch_key": initial["dispatch_key"],
                "input_fingerprint": initial["input_fingerprint"],
                "attempt": 2,
                "failed_at": "2026-11-02T12:00:00+08:00",
            }
        )
        final = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(final["status"], "skip_failed_final_today")

    def test_same_input_inflight_dispatch_is_hold_not_parallel_task(self):
        snapshot = _snapshot()
        initial = evaluate_row(snapshot, batch_date="2026-11-02")
        snapshot["execution_events"].append(
            {
                "phase": "stage2_dispatched",
                "dispatch_key": initial["dispatch_key"],
                "input_fingerprint": initial["input_fingerprint"],
                "attempt": 1,
            }
        )
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "hold_active_same_input_task")

    def test_cli_file_writer_preserves_machine_readable_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            snapshot_path = root / "row.json"
            output_path = root / "decision.json"
            snapshot_path.write_text(json.dumps(_snapshot()), encoding="utf-8")
            result = select_row_file(snapshot_path, batch_date="2026-11-02", output_path=output_path)
            self.assertEqual(result, json.loads(output_path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
