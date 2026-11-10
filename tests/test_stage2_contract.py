import json
import unittest

from benchmark_av_sync.stage2_release import validate_installed_release

from benchmark_av_sync.stage2_contract import (
    CONTRACT_VERSION,
    SOURCE_BINDING,
    build_stage2_complete_event,
    upload_idempotency_key,
    validate_source_binding,
    validate_stage2_complete_event,
    validate_runtime_receipt,
    validate_stage2_started_receipt,
)


def _sha(label: str) -> str:
    import hashlib

    return hashlib.sha256(label.encode()).hexdigest()


class Stage2ContractTests(unittest.TestCase):
    def test_source_binding_is_exact(self):
        self.assertEqual(validate_source_binding(dict(SOURCE_BINDING)), (True, None))
        wrong = dict(SOURCE_BINDING)
        wrong["view_name"] = "聊天"
        ok, reason = validate_source_binding(wrong)
        self.assertFalse(ok)
        self.assertIn("view_name", reason)

    def test_idempotency_key_has_no_retry_suffix(self):
        digest = _sha("artifact")
        key = upload_idempotency_key("dispatch", "complete", digest)
        self.assertEqual(key, f"omni2|{_sha('dispatch')[:16]}|complete|{digest}")
        self.assertLessEqual(len(key), 128)

    def test_idempotency_key_compacts_a_long_dispatch_key(self):
        dispatch = "dispatch|" + "x" * 512
        digest = _sha("artifact")
        key = upload_idempotency_key(dispatch, "complete_captioned", digest)
        self.assertEqual(
            key,
            f"omni2|{_sha(dispatch)[:16]}|complete_captioned|{digest}",
        )
        self.assertLessEqual(len(key), 128)

    def test_started_receipt_binds_input_and_release(self):
        release = validate_installed_release()
        receipt = {
            "phase": "stage2_started",
            "record_id": "rec1",
            "dispatch_key": "dispatch",
            "input_fingerprint": _sha("input"),
            "strict_release": release,
            "attempt": 1,
            "readback_verified": True,
            "updated_at": "2026-11-07T08:00:00+08:00",
        }
        self.assertEqual(
            validate_stage2_started_receipt(
                receipt,
                record_id="rec1",
                dispatch_key="dispatch",
                input_fingerprint=_sha("input"),
                strict_release=release,
                attempt=1,
            ),
            receipt,
        )
        stale = dict(receipt)
        stale["input_fingerprint"] = _sha("other")
        with self.assertRaisesRegex(ValueError, "input_fingerprint"):
            validate_stage2_started_receipt(
                stale,
                record_id="rec1",
                dispatch_key="dispatch",
                input_fingerprint=_sha("input"),
                strict_release=release,
                attempt=1,
            )

    def test_completion_event_requires_remote_readback_and_review_receipt(self):
        digest = _sha("x")
        result = {
            "ok": True,
            "status": "review",
            "record_id": "rec1",
            "video_number": "mx1",
            "input_fingerprint": _sha("input"),
            "strict_release": validate_installed_release(),
            "dispatch_key": "dispatch",
            "attempt": 1,
            "runtime_receipt_sha256": _sha("runtime"),
            "pair_results": [
                {
                    "pair_id": "001",
                    "input": {
                        "reference_package_sha256": _sha("ref"),
                        "generated_video_sha256": _sha("video"),
                    },
                    "output": {"sha256": _sha("output")},
                    "subtitle": {"source_mode": "stage1", "quality_status": "pass"},
                    "status": "pass",
                    "review_required": True,
                }
            ],
            "deliveries": [
                {
                    "artifact_key": key,
                    "field_name": field,
                    "name": f"omni_stage2__rec1__{suffix}",
                    "size": 10,
                    "sha256": digest,
                }
                for key, (field, suffix) in {
                    "segments": ("素材2", "segments.zip"),
                    "complete": ("素材2", "complete.mp4"),
                    "complete_captioned": ("素材3", "complete_captioned.mp4"),
                    "evidence": ("素材2", "evidence.zip"),
                }.items()
            ],
        }
        event = build_stage2_complete_event(result, remote_readback_verified=False)
        self.assertEqual(event["contract"], CONTRACT_VERSION)
        ok, reason = validate_stage2_complete_event(event)
        self.assertFalse(ok)
        self.assertIn("remote_readback_verified", reason)

        event["remote_readback_verified"] = True
        event["review_receipt"] = {
            "required": True,
            "completed": True,
            "method": "authorized_automated_audit",
            "authorization": {
                "record_id": "rec1",
                "dispatch_key": "dispatch",
                "attempt": 1,
                "statement": "owner explicitly authorized automated audit",
                "authorized_at": "2026-11-10T12:00:00+08:00",
            },
            "audit": {
                "delivery_sha256": True,
                "full_decode": True,
                "audio_payload_match": True,
                "caption_visual_quality": True,
                "caption_frame_checks": True,
            },
        }
        ok, reason = validate_stage2_complete_event(event)
        self.assertTrue(ok, reason)
        event["review_receipt"]["authorization"]["record_id"] = "other"
        ok, reason = validate_stage2_complete_event(event)
        self.assertFalse(ok)
        self.assertIn("record_id", reason)

    def test_reconstructed_runtime_requires_pinned_user_authorization(self):
        receipt = {
            "schema": "omni-stage2-runtime-receipt/v2",
            "record_id": "rec1",
            "mode": "reconstructed",
            "provenance": {
                "kind": "user_authorized_reconstruction",
                "authorization": "owner explicitly authorized controlled reconstruction",
                "source": {
                    "commit": "a" * 40,
                    "release_manifest_sha256": _sha("release"),
                },
            },
            "versions": {
                "python": "3.11.14",
                "mediapipe": "0.10.21",
                "opencv": "4.11.0",
                "numpy": "1.26.4",
            },
            "face_mesh_smoke": True,
            "online_preflight_ok": True,
            "ffmpeg_ok": True,
            "ffprobe_ok": True,
            "minterpolate_ok": True,
            "libass_ok": True,
            "cjk_bold_font": "Noto Sans CJK SC Bold",
            "cjk_bold_font_sha256": _sha("font"),
            "checked_at": "2026-11-10T12:00:00+08:00",
        }
        self.assertEqual(validate_runtime_receipt(receipt, record_id="rec1"), receipt)
        unauthorized = dict(receipt)
        unauthorized["provenance"] = dict(receipt["provenance"])
        unauthorized["provenance"].pop("authorization")
        with self.assertRaisesRegex(ValueError, "authorization"):
            validate_runtime_receipt(unauthorized, record_id="rec1")


if __name__ == "__main__":
    unittest.main()

