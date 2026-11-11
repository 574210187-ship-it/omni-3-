import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any

from benchmark_av_sync.stage2_batch import (
    Stage2BatchConfig,
    Stage2BatchFields,
    run_batch,
)
from benchmark_av_sync.stage2_contract import (
    DELIVERY_SPECS,
    SOURCE_BINDING,
    upload_idempotency_key,
)


def _sha(value: bytes | str) -> str:
    if isinstance(value, str):
        value = value.encode("utf-8")
    return hashlib.sha256(value).hexdigest()


def _attachment(token: str, name: str, content: bytes) -> dict[str, Any]:
    return {
        "file_token": token,
        "name": name,
        "size_bytes": len(content),
        "sha256": _sha(content),
        "content": content,
    }


def _record(record_id: str = "rec-batch-001") -> dict[str, Any]:
    main = _attachment("main-001", "001_main.mp4", b"main-video")
    package = _attachment(
        "stage1-001", f"omni_stage1__v3.2__{record_id}__001.zip", b"stage1-zip"
    )
    stage1 = {
        "phase": "stage1_complete",
        "remote_readback_verified": True,
        "updated_at": "2026-11-11T08:00:00+08:00",
    }
    return {
        "record_id": record_id,
        "fields": {
            "视频编号": "batch-video-001",
            "素材1": [main],
            "素材2": [package],
            "素材3": [],
            "执行记录": json.dumps(stage1, ensure_ascii=False),
            "zed备注": "保留内容",
        },
    }


class FakeAdapter:
    def __init__(self, records: list[dict[str, Any]]):
        self.records = {record["record_id"]: record for record in records}
        self.contents = {
            item["file_token"]: item["content"]
            for record in records
            for field in ("素材1", "素材2", "素材3")
            for item in record["fields"][field]
        }
        self.upload_calls: list[tuple[str, str, str]] = []
        self.remove_calls: list[tuple[str, str, str]] = []
        self.update_calls: list[tuple[str, dict[str, Any]]] = []

    def check_connection(self):
        return {"connected": True}

    def list_records(self):
        return [self._visible(record) for record in self.records.values()]

    def get_record(self, record_id):
        return self._visible(self.records[record_id])

    @staticmethod
    def _visible(record):
        copied = json.loads(json.dumps(record, ensure_ascii=False, default=str))
        for field in ("素材1", "素材2", "素材3"):
            for item in copied["fields"][field]:
                item.pop("content", None)
        return copied

    def read_attachment_sha256(self, *, record_id, field_name, attachment):
        return attachment["sha256"]

    def download_attachment(self, *, record_id, field_name, attachment, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(self.contents[attachment["file_token"]])
        return destination

    def append_execution(self, *, record_id, event):
        fields = self.records[record_id]["fields"]
        fields["执行记录"] += "\n" + json.dumps(event, ensure_ascii=False)

    def upload_attachment(self, *, record_id, field_name, path, idempotency_key):
        self.upload_calls.append((record_id, field_name, idempotency_key))
        fields = self.records[record_id]["fields"]
        for item in fields[field_name]:
            if item.get("idempotency_key") == idempotency_key:
                return item
        content = path.read_bytes()
        token = f"upload-{len(self.upload_calls)}"
        item = _attachment(token, path.name, content)
        item["idempotency_key"] = idempotency_key
        fields[field_name].append(item)
        self.contents[token] = content
        visible = dict(item)
        visible.pop("content", None)
        return visible

    def remove_attachment(self, *, record_id, field_name, attachment):
        token = attachment["file_token"]
        self.remove_calls.append((record_id, field_name, token))
        values = self.records[record_id]["fields"][field_name]
        self.records[record_id]["fields"][field_name] = [
            item for item in values if item["file_token"] != token
        ]

    def update_fields(self, *, record_id, fields):
        self.update_calls.append((record_id, dict(fields)))
        self.records[record_id]["fields"].update(fields)


def _config(root: Path) -> Stage2BatchConfig:
    return Stage2BatchConfig(
        path=root / "stage2-batch.json",
        state_root=root / "state",
        work_root=root / "work",
        fields=Stage2BatchFields(),
        source_binding=dict(SOURCE_BINDING),
        adapter={},
        runtime={},
        batch_authorization="用户明确授权：按本批次计划自动执行并逐行回写。",
        review_authorization=None,
    )


def _fake_writer(decisions: dict[Path, dict[str, Any]]):
    def write_job(_adapter, *, decision, run_root, **_kwargs):
        root = run_root / decision["record_id"] / f"attempt-{decision['attempt']}"
        root.mkdir(parents=True, exist_ok=True)
        path = root / "job.json"
        path.write_text("{}", encoding="utf-8")
        decisions[path] = decision
        return path

    return write_job


def _success_runner(decisions: dict[Path, dict[str, Any]]):
    def run(job_path: Path):
        decision = decisions[job_path]
        root = job_path.parent / "results"
        root.mkdir(parents=True, exist_ok=True)
        deliveries = []
        for artifact_key, (field_name, suffix) in DELIVERY_SPECS.items():
            content = f"{artifact_key}|{decision['record_id']}|{decision['attempt']}".encode()
            path = root / f"omni_stage2__{decision['record_id']}__{suffix}"
            path.write_bytes(content)
            digest = _sha(content)
            deliveries.append(
                {
                    "artifact_key": artifact_key,
                    "field_name": field_name,
                    "name": path.name,
                    "path": str(path),
                    "size_bytes": len(content),
                    "sha256": digest,
                    "idempotency_key": upload_idempotency_key(
                        decision["dispatch_key"], artifact_key, digest
                    ),
                }
            )
        manifest = decision["input_manifest"][0]
        return {
            "ok": True,
            "status": "pass",
            "record_id": decision["record_id"],
            "video_number": decision["video_number"],
            "dispatch_key": decision["dispatch_key"],
            "input_fingerprint": decision["input_fingerprint"],
            "strict_release": decision["strict_release"],
            "attempt": decision["attempt"],
            "runtime_receipt_sha256": _sha("runtime"),
            "pair_results": [
                {
                    "pair_id": "001",
                    "input": {
                        "reference_package_sha256": manifest["stage1_zip"]["sha256"],
                        "generated_video_sha256": manifest["main_video"]["sha256"],
                    },
                    "output": {"sha256": _sha("segment-output")},
                    "subtitle": {"source_mode": "stage1", "quality_status": "pass"},
                    "status": "pass",
                    "review_required": False,
                    "max_speed": 1.0,
                    "min_speed": 1.0,
                    "speed_exception_used": False,
                    "pause_hold_used": False,
                }
            ],
            "deliveries": deliveries,
            "review_receipt": {
                "required": False,
                "completed": True,
                "method": "not_required",
            },
            "written_at": "2026-11-11T08:10:00+08:00",
        }

    return run


class Stage2BatchTests(unittest.TestCase):
    def test_execute_reads_selects_uploads_readbacks_and_completes_row(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = FakeAdapter([_record()])
            decisions: dict[Path, dict[str, Any]] = {}
            with patch(
                "benchmark_av_sync.stage2_batch._write_job", side_effect=_fake_writer(decisions)
            ):
                receipt = run_batch(
                    _config(root),
                    adapter=adapter,
                    batch_date="2026-11-11",
                    run_id="batch-success",
                    execute=True,
                    runner=_success_runner(decisions),
                )
            self.assertEqual(receipt["status"], "complete")
            self.assertEqual(receipt["rows"][0]["result"], "success")
            self.assertEqual(len(adapter.upload_calls), 4)
            self.assertEqual(len(adapter.remove_calls), 0)
            fields = adapter.records["rec-batch-001"]["fields"]
            self.assertEqual(len(fields["素材2"]), 4)  # Stage1 ZIP + 3 new deliveries
            self.assertEqual(len(fields["素材3"]), 1)
            events = [json.loads(line) for line in fields["执行记录"].splitlines()]
            complete = [event for event in events if event.get("phase") == "stage2_complete"]
            self.assertEqual(len(complete), 1)
            self.assertTrue(complete[0]["remote_readback_verified"])
            self.assertIn("omni视频处理已完成", fields["zed备注"])

    def test_first_failure_gets_one_retry_then_final_failure(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            adapter = FakeAdapter([_record()])
            decisions: dict[Path, dict[str, Any]] = {}

            def failing_runner(_job: Path):
                return {"ok": False, "status": "failed", "error": "render preflight failed"}

            with patch(
                "benchmark_av_sync.stage2_batch._write_job", side_effect=_fake_writer(decisions)
            ):
                receipt = run_batch(
                    _config(root),
                    adapter=adapter,
                    batch_date="2026-11-11",
                    run_id="batch-retry",
                    execute=True,
                    runner=failing_runner,
                )
            self.assertEqual(receipt["rows"][0]["result"], "failed_final")
            text = adapter.records["rec-batch-001"]["fields"]["执行记录"]
            events = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(sum(event.get("phase") == "stage2_started" for event in events), 2)
            self.assertEqual(sum(event.get("phase") == "stage2_failed" for event in events), 1)
            self.assertEqual(sum(event.get("phase") == "stage2_failed_final" for event in events), 1)
            self.assertEqual(len(adapter.upload_calls), 0)

    def test_retry_rework_deletes_only_the_exact_superseded_tokens(self):
        from unittest.mock import patch
        from benchmark_av_sync.stage2_selection import evaluate_row

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record()
            # Obtain the canonical same-input dispatch key/fingerprint first.
            snapshot = {
                "schema_version": 1,
                "record_id": record["record_id"],
                "video_number": record["fields"]["视频编号"],
                "source_binding": dict(SOURCE_BINDING),
                "fields": {
                    key: value
                    for key, value in record["fields"].items()
                },
            }
            initial = evaluate_row(snapshot, batch_date="2026-11-11")
            old_tokens = []
            for artifact_key, (field_name, suffix) in DELIVERY_SPECS.items():
                old = _attachment(
                    f"old-{artifact_key}",
                    f"omni_stage2__{record['record_id']}__{suffix}",
                    f"old-{artifact_key}".encode(),
                )
                record["fields"][field_name].append(old)
                old_tokens.append(old["file_token"])
            failed = {
                "phase": "stage2_failed",
                "dispatch_key": initial["dispatch_key"],
                "input_fingerprint": initial["input_fingerprint"],
                "attempt": 1,
                "updated_at": "2026-11-11T09:00:00+08:00",
            }
            record["fields"]["执行记录"] += "\n" + json.dumps(failed, ensure_ascii=False)
            adapter = FakeAdapter([record])
            decisions: dict[Path, dict[str, Any]] = {}
            with patch(
                "benchmark_av_sync.stage2_batch._write_job", side_effect=_fake_writer(decisions)
            ):
                receipt = run_batch(
                    _config(root),
                    adapter=adapter,
                    batch_date="2026-11-11",
                    run_id="batch-rework",
                    execute=True,
                    runner=_success_runner(decisions),
                )
            self.assertEqual(receipt["rows"][0]["result"], "success")
            self.assertEqual(set(token for _, _, token in adapter.remove_calls), set(old_tokens))
            tokens = {
                item["file_token"]
                for field in ("素材2", "素材3")
                for item in adapter.records["rec-batch-001"]["fields"][field]
            }
            self.assertTrue(set(old_tokens).isdisjoint(tokens))
            self.assertEqual(len(adapter.upload_calls), 4)

    def test_ineligible_row_gets_the_rule_marker_without_media_work(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            record = _record()
            record["fields"]["素材2"] = []
            adapter = FakeAdapter([record])
            receipt = run_batch(
                _config(root),
                adapter=adapter,
                batch_date="2026-11-11",
                run_id="batch-ineligible",
                execute=True,
            )
            self.assertEqual(receipt["rows"][0]["result"], "ineligible_stage1")
            self.assertIn("现有规则不适合", record["fields"]["zed备注"])
            self.assertEqual(adapter.upload_calls, [])

    def test_unreadable_row_is_held_without_blocking_another_valid_row(self):
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            good = _record("rec-good")
            bad = _record("rec-bad")
            # Simulate a bridge that cannot obtain the checksum for one exact
            # attachment.  The batch must preserve the row as HOLD and still
            # finish the independent valid row.
            bad["fields"]["素材1"][0].pop("sha256")
            adapter = FakeAdapter([good, bad])
            decisions: dict[Path, dict[str, Any]] = {}
            with patch(
                "benchmark_av_sync.stage2_batch._write_job", side_effect=_fake_writer(decisions)
            ):
                receipt = run_batch(
                    _config(root),
                    adapter=adapter,
                    batch_date="2026-11-11",
                    run_id="batch-local-hold",
                    execute=True,
                    runner=_success_runner(decisions),
                )
            by_id = {item["record_id"]: item for item in receipt["rows"]}
            self.assertEqual(by_id["rec-good"]["result"], "success")
            self.assertEqual(by_id["rec-bad"]["result"], "hold")
            self.assertEqual(len(adapter.upload_calls), 4)


if __name__ == "__main__":
    unittest.main()
