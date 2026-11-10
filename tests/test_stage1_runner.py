import json
import tempfile
import unittest
import wave
from pathlib import Path

from benchmark_av_sync.contracts import AlignmentToken, BenchmarkGroup, BenchmarkScript
from benchmark_av_sync.handoff import write_reference_package
from benchmark_av_sync.subtitle_contract import build_subtitle_contract, select_caption_source
from benchmark_av_sync.stage1_runner import (
    PairingIneligible,
    Stage1Config,
    Stage1Fields,
    load_config,
    pair_record,
    run_batch,
)
from benchmark_av_sync.workspace import sha256_file


def _attachment(name: str, content: bytes) -> dict:
    return {"name": name, "size": len(content), "file_token": f"token-{name}", "content": content}


def _record(record_id: str, *, good: bool = True) -> dict:
    references = [_attachment("001_reference.mp4", b"reference-1")]
    generated = [_attachment("01_generated.mp4" if good else "Character_generated.mp4", b"generated-1")]
    return {
        "record_id": record_id,
        "fields": {
            "视频编号": f"video-{record_id}",
            "zed备注": "其他文字\nomni\n拆分",
            "处理后音、视频": references,
            "素材1": generated,
            "素材2": [],
            "执行记录": "older event",
        },
    }


class FakeAdapter:
    def __init__(self, records, *, break_upload_readback: bool = False):
        self.records = {item["record_id"]: item for item in records}
        self.break_upload_readback = break_upload_readback
        self.upload_count = 0

    def check_connection(self):
        return {"ok": True, "connected": True}

    def list_records(self):
        return list(self.records.values())

    def get_record(self, record_id):
        return self.records[record_id]

    def download_attachment(self, *, record_id, field_name, attachment, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(attachment["content"])
        return destination

    def append_execution(self, *, record_id, event):
        fields = self.records[record_id]["fields"]
        fields["执行记录"] += "\n" + json.dumps(event, ensure_ascii=False)

    def upload_attachment(self, *, record_id, field_name, path, idempotency_key):
        self.upload_count += 1
        item = {"name": path.name, "size": path.stat().st_size, "file_token": "uploaded"}
        if not self.break_upload_readback:
            self.records[record_id]["fields"][field_name].append(item)
        return item


def _processor(pair, reference_video, output, config):
    wav = output.parent / f"{pair.pair_id}.wav"
    with wave.open(str(wav), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 16000)
    benchmark = BenchmarkScript(
        pair_id=pair.pair_id,
        text="测试",
        reference_duration=1.0,
        speech_start=0.05,
        speech_end=0.95,
        tokens=[AlignmentToken(0, "测", 0.1, 0.4), AlignmentToken(1, "试", 0.5, 0.9)],
        boundaries=[],
        groups=[BenchmarkGroup("g00", "测试", 0, 1, 0.1, 0.9, "c00")],
    )
    selected = select_caption_source(
        {
            "entries": [
                {"text": "测试", "confidence": 0.99, "start": 0.05, "end": 0.95}
            ],
            "scan": {"completed": True, "sampled_frames": 4, "stable_event_count": 1},
        },
        {"text": "测试"},
    )
    subtitle = build_subtitle_contract(
        pair_id=pair.pair_id,
        reference_video_sha256="1" * 64,
        reference_audio_sha256=sha256_file(wav),
        reference_duration=benchmark.reference_duration,
        selected=selected,
        aligned_tokens=benchmark.tokens,
    )
    write_reference_package(
        output,
        pair_id=pair.pair_id,
        benchmark=benchmark,
        subtitle=subtitle,
        reference_audio=wav,
    )
    return {"ok": True}


def _config(root: Path) -> Stage1Config:
    return Stage1Config(
        path=root / "config.json",
        state_root=root / "state",
        work_root=root / "work",
        fields=Stage1Fields(),
        adapter={},
        media={},
    )


class Stage1RunnerTests(unittest.TestCase):
    def test_pair_record_matches_numeric_ids_with_different_width(self):
        pairs = pair_record(_record("rec1"), Stage1Fields())
        self.assertEqual(len(pairs), 1)
        self.assertEqual(pairs[0].pair_id, "001")
        self.assertEqual(pairs[0].numeric_id, 1)

    def test_pair_record_rejects_missing_leading_id(self):
        with self.assertRaisesRegex(PairingIneligible, "leading numeric ID"):
            pair_record(_record("rec1", good=False), Stage1Fields())

    def test_batch_writes_complete_receipt_for_success_and_pairing_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            records = [_record("rec-good"), _record("rec-bad", good=False)]
            receipt = run_batch(
                _config(root),
                adapter=FakeAdapter(records),
                run_date="2026-10-29",
                run_id="omni-stage1-test",
                processor=_processor,
            )
            self.assertEqual(receipt["status"], "complete_with_failures")
            self.assertEqual(
                receipt["counts"],
                {"candidate": 2, "eligible": 1, "success": 1, "failed": 1, "pairing_ineligible": 1},
            )
            self.assertEqual(
                {item["result"] for item in receipt["rows"]},
                {"success", "pairing_ineligible"},
            )
            self.assertEqual(
                {item["video_number"] for item in receipt["rows"]},
                {"video-rec-good", "video-rec-bad"},
            )
            latest = root / "state" / "stage1" / "latest.json"
            self.assertTrue(latest.is_file())
            self.assertEqual(json.loads(latest.read_text(encoding="utf-8"))["run_id"], "omni-stage1-test")

    def test_upload_readback_failure_is_recorded_as_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter([_record("rec-fail")], break_upload_readback=True)
            receipt = run_batch(
                _config(root),
                adapter=adapter,
                run_date="2026-10-29",
                run_id="omni-stage1-test",
                processor=_processor,
            )
            self.assertEqual(receipt["rows"][0]["result"], "failed")
            self.assertIn("remote readback expected one", receipt["rows"][0]["reason"])
            self.assertIn("stage1_failed", adapter.records["rec-fail"]["fields"]["执行记录"])

    def test_completed_input_is_not_uploaded_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter = FakeAdapter([_record("rec-repeat")])
            config = _config(root)
            first = run_batch(
                config,
                adapter=adapter,
                run_date="2026-10-29",
                run_id="omni-stage1-first",
                processor=_processor,
            )
            second = run_batch(
                config,
                adapter=adapter,
                run_date="2026-10-29",
                run_id="omni-stage1-second",
                processor=_processor,
            )
            self.assertEqual(first["rows"][0]["result"], "success")
            self.assertEqual(second["rows"][0]["result"], "success")
            self.assertEqual(adapter.upload_count, 1)
            self.assertTrue(second["rows"][0]["packages"][0]["processor"]["already_complete"])

    def test_load_config_uses_real_field_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            config_path.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "state_root": "state",
                        "work_root": "work",
                        "adapter": {"command": ["bridge"]},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            config = load_config(config_path)
            self.assertEqual(config.fields.video_number, "视频编号")
            self.assertEqual(config.fields.reference, "处理后音、视频")
            self.assertEqual(config.fields.output, "素材2")

    def test_missing_video_number_is_pairing_ineligible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            record = _record("rec-no-number")
            record["fields"]["视频编号"] = ""
            receipt = run_batch(
                _config(root),
                adapter=FakeAdapter([record]),
                run_date="2026-10-29",
                run_id="omni-stage1-test",
                processor=_processor,
            )
            self.assertEqual(receipt["rows"][0]["result"], "pairing_ineligible")
            self.assertIn("视频编号", receipt["rows"][0]["reason"])


if __name__ == "__main__":
    unittest.main()
