import hashlib
import unittest

from benchmark_av_sync.stage2_contract import SOURCE_BINDING
from benchmark_av_sync.stage2_selection import evaluate_row


def _sha(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _attachment(token: str, name: str, label: str) -> dict:
    return {
        "file_token": token,
        "name": name,
        "size_bytes": 100,
        "sha256": _sha(label),
    }


class StrictSelectionTests(unittest.TestCase):
    def _snapshot(self):
        record_id = "rec-selection-001"
        return {
            "schema_version": 1,
            "record_id": record_id,
            "video_number": "mx-selection-001",
            "source_binding": dict(SOURCE_BINDING),
            "fields": {
                "素材1": [_attachment("v1", "001_main.mp4", "v1")],
                "素材2": [_attachment("z1", f"omni_stage1__v3.2__{record_id}__001.zip", "z1")],
                "素材3": [],
                "执行记录": "",
                "zed备注": "保留",
            },
            "execution_events": [
                {"phase": "stage1_complete", "remote_readback_verified": True}
            ],
        }

    def test_chat_view_binding_is_not_accepted(self):
        snapshot = self._snapshot()
        snapshot["source_binding"]["view_name"] = "聊天"
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "hold_source_binding_mismatch")

    def test_ineligible_row_is_marked_and_can_continue(self):
        snapshot = self._snapshot()
        snapshot["fields"]["素材2"] = []
        result = evaluate_row(snapshot, batch_date="2026-11-02")
        self.assertEqual(result["status"], "ineligible_stage1")
        self.assertTrue(result["continue_selection"])
        self.assertEqual(result["zed_note_update"]["value"], "保留；现有规则不适合")


if __name__ == "__main__":
    unittest.main()

