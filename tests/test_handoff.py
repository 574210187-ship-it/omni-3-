import json
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path

from benchmark_av_sync.contracts import (
    AlignmentToken,
    BenchmarkGroup,
    BenchmarkScript,
    SafeBoundary,
)
from benchmark_av_sync.handoff import (
    HandoffError,
    open_reference_package,
    validate_reference_package,
    write_reference_package,
)
from benchmark_av_sync.two_stage import (
    _generated_pair_id,
    _same_numeric_id,
    TwoStageError,
    online_preflight,
)
from benchmark_av_sync.subtitle_contract import build_subtitle_contract, select_caption_source
from benchmark_av_sync.workspace import sha256_file


def _write_wav(path: Path, duration: float = 1.0, rate: int = 16000) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(b"\0\0" * round(duration * rate))


def _benchmark() -> BenchmarkScript:
    tokens = [
        AlignmentToken(0, "测", 0.1, 0.4),
        AlignmentToken(1, "试", 0.6, 0.9),
    ]
    return BenchmarkScript(
        pair_id="01",
        text="测试",
        reference_duration=1.0,
        speech_start=0.07,
        speech_end=0.93,
        tokens=tokens,
        boundaries=[SafeBoundary(0, 0.5, 200.0, True)],
        groups=[BenchmarkGroup("g00", "测试", 0, 1, 0.1, 0.9, "c00")],
    )


def _subtitle(benchmark: BenchmarkScript, wav: Path) -> dict:
    selected = select_caption_source(
        {
            "entries": [
                {"text": benchmark.text, "confidence": 0.99, "start": 0.1, "end": 0.9}
            ],
            "scan": {"completed": True, "sampled_frames": 4, "stable_event_count": 1},
        },
        {"text": benchmark.text},
    )
    return build_subtitle_contract(
        pair_id=benchmark.pair_id,
        reference_video_sha256="1" * 64,
        reference_audio_sha256=sha256_file(wav),
        reference_duration=benchmark.reference_duration,
        selected=selected,
        aligned_tokens=benchmark.tokens,
    )


def _subtitle_v32(benchmark: BenchmarkScript, wav: Path) -> dict:
    value = _subtitle(benchmark, wav)
    value.update(
        {
            "schema_version": 3,
            "evidence_version": "subtitle-v3.2",
            "split_policy": "semantic-word-v1",
            "max_line_chars": 12,
            "min_line_chars": 2,
            "max_line_width_px": 864,
            "min_line_duration": 0.5,
            "caption_style_signature": "Noto Sans CJK SC|88|8|1080x1920|an5|q2|pos(540,1229)",
        }
    )
    for event in value["caption_events"]:
        event["parent_event_id"] = event["event_id"]
    return value


class HandoffTests(unittest.TestCase):
    def test_zip_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "source.wav"
            package = root / "01_reference_bundle.zip"
            _write_wav(wav)
            benchmark = _benchmark()
            write_reference_package(
                package,
                pair_id="01",
                benchmark=benchmark,
                subtitle=_subtitle(benchmark, wav),
                reference_audio=wav,
            )
            summary = validate_reference_package(package)
            self.assertTrue(summary["ok"])
            self.assertEqual(summary["pair_id"], "01")
            with open_reference_package(package) as opened:
                self.assertTrue(opened.reference_audio.is_file())
                self.assertEqual(opened.benchmark.text, "测试")

    def test_zip_bytes_are_reproducible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "source.wav"
            first = root / "first.zip"
            second = root / "second.zip"
            _write_wav(wav)
            benchmark = _benchmark()
            subtitle = _subtitle(benchmark, wav)
            write_reference_package(
                first,
                pair_id="01",
                benchmark=benchmark,
                subtitle=subtitle,
                reference_audio=wav,
            )
            write_reference_package(
                second,
                pair_id="01",
                benchmark=benchmark,
                subtitle=subtitle,
                reference_audio=wav,
            )
            self.assertEqual(sha256_file(first), sha256_file(second))

    def test_stage1_v32_package_is_consumed_without_schema_downgrade(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "source.wav"
            package = root / "package"
            _write_wav(wav)
            benchmark = _benchmark()
            write_reference_package(
                package,
                pair_id="01",
                benchmark=benchmark,
                subtitle=_subtitle(benchmark, wav),
                reference_audio=wav,
            )
            subtitle_path = package / "subtitle.json"
            subtitle = _subtitle_v32(benchmark, wav)
            subtitle_path.write_text(
                json.dumps(subtitle, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["evidence_version"] = "subtitle-v3.2"
            manifest["files"]["subtitle.json"] = {
                "size": subtitle_path.stat().st_size,
                "sha256": sha256_file(subtitle_path),
            }
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            summary = validate_reference_package(package)
            self.assertEqual(summary["manifest"]["evidence_version"], "subtitle-v3.2")
            with open_reference_package(package) as opened:
                self.assertEqual(opened.subtitle["schema_version"], 3)
                self.assertEqual(opened.subtitle["caption_events"][0]["text"], "测试")

    def test_directory_tamper_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "source.wav"
            package = root / "package"
            _write_wav(wav)
            benchmark = _benchmark()
            write_reference_package(
                package,
                pair_id="01",
                benchmark=benchmark,
                subtitle=_subtitle(benchmark, wav),
                reference_audio=wav,
            )
            with (package / "reference.wav").open("ab") as handle:
                handle.write(b"tamper")
            with self.assertRaisesRegex(HandoffError, "size mismatch"):
                validate_reference_package(package)

    def test_nested_zip_member_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / "bad.zip"
            with zipfile.ZipFile(package, "w") as archive:
                archive.writestr("nested/manifest.json", json.dumps({}))
                archive.writestr("benchmark.json", "{}")
                archive.writestr("subtitle.json", "{}")
                archive.writestr("reference.wav", b"bad")
            with self.assertRaisesRegex(HandoffError, "unsafe or nested"):
                validate_reference_package(package)

    def test_v1_package_is_rejected_even_when_all_v2_members_exist(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            wav = root / "source.wav"
            package = root / "package"
            _write_wav(wav)
            benchmark = _benchmark()
            write_reference_package(
                package,
                pair_id="01",
                benchmark=benchmark,
                subtitle=_subtitle(benchmark, wav),
                reference_audio=wav,
            )
            manifest_path = package / "manifest.json"
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["version"] = 1
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(HandoffError, "unsupported reference package version"):
                validate_reference_package(package)

    def test_numeric_ids_allow_leading_zero_width_difference(self):
        self.assertTrue(_same_numeric_id("001", "01"))
        self.assertFalse(_same_numeric_id("01", "02"))
        self.assertEqual(
            _generated_pair_id(Path("01_1080p_20261027185449.mp4"), "001"),
            "01",
        )
        with self.assertRaisesRegex(TwoStageError, "matching package ID"):
            _generated_pair_id(Path("01_copy_001.mp4"), "01")

    def test_preflight_reports_missing_explicit_binaries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = online_preflight(
                ffmpeg=root / "ffmpeg-missing",
                ffprobe=root / "ffprobe-missing",
            )
            self.assertFalse(result["ok"])
            self.assertFalse(result["checks"]["ffmpeg"])
            self.assertFalse(result["checks"]["ffprobe"])


if __name__ == "__main__":
    unittest.main()

