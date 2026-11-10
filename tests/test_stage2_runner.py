import hashlib
import json
import struct
import tempfile
import unittest
import wave
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from benchmark_av_sync.contracts import (
    AlignmentPlan,
    AlignmentToken,
    AudioChunk,
    BenchmarkGroup,
    BenchmarkScript,
    MouthFrame,
    MouthTrack,
    SafeBoundary,
    VideoSegment,
)
from benchmark_av_sync.handoff import write_reference_package
from benchmark_av_sync.subtitle_contract import (
    SubtitleContractError,
    build_subtitle_contract,
    select_caption_source,
)
from benchmark_av_sync.stage2_runner import (
    DeliveryPolicy,
    ReplacementTarget,
    Stage2RunnerError,
    _bmp_non_background_bbox,
    _caption_chunks,
    _caption_groups,
    _caption_visual_quality,
    _post_delivery_actions,
    _resolve_caption_fonts_dir,
    _parse_metric_value,
    build_caption_events,
    load_job,
    run_row,
    should_retry_speed_exception,
    write_ass,
)
from benchmark_av_sync.workspace import atomic_write_json, sha256_file
from benchmark_av_sync.contracts import VerificationReport
from benchmark_av_sync.stage2_release import validate_installed_release


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(b"\0\0" * 16000)


def _benchmark() -> BenchmarkScript:
    tokens = [AlignmentToken(0, "测", 0.1, 0.4), AlignmentToken(1, "试", 0.5, 0.9)]
    return BenchmarkScript(
        pair_id="001",
        text="测试",
        reference_duration=1.0,
        speech_start=0.08,
        speech_end=0.92,
        tokens=tokens,
        boundaries=[SafeBoundary(0, 0.45, 100, False, True)],
        groups=[BenchmarkGroup("g00", "测试", 0, 1, 0.1, 0.9, "c00")],
    )


def _subtitle(benchmark: BenchmarkScript, *, audio_sha256: str = "2" * 64) -> dict:
    selected = select_caption_source(
        {
            "entries": [
                {
                    "text": benchmark.text,
                    "confidence": 0.99,
                    "start": benchmark.speech_start,
                    "end": benchmark.speech_end,
                }
            ],
            "scan": {"completed": True, "sampled_frames": 4, "stable_event_count": 1},
        },
        {"text": benchmark.text},
    )
    return build_subtitle_contract(
        pair_id=benchmark.pair_id,
        reference_video_sha256="1" * 64,
        reference_audio_sha256=audio_sha256,
        reference_duration=benchmark.reference_duration,
        selected=selected,
        aligned_tokens=benchmark.tokens,
    )


def _subtitle_v32(benchmark: BenchmarkScript, *, audio_sha256: str = "2" * 64) -> dict:
    value = _subtitle(benchmark, audio_sha256=audio_sha256)
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


def _font_gate(root: Path) -> tuple[Path, dict[str, str]]:
    font_path = root / "fonts" / "NotoSansCJKsc-Bold.ttc"
    return root / "fonts", {
        "family": "Noto Sans CJK SC",
        "style": "Bold",
        "fullname": "Noto Sans CJK SC Bold",
        "path": str(font_path),
        "sha256": _sha(font_path),
    }


def _load_job(path: Path):
    with patch(
        "benchmark_av_sync.stage2_runner._resolve_caption_fonts_dir",
        side_effect=lambda _value, *, root: _font_gate(root),
    ):
        return load_job(path)


def _make_job(root: Path) -> Path:
    wav = root / "reference.wav"
    package = root / "001_reference.zip"
    generated = root / "001_generated.mp4"
    fonts = root / "fonts"
    fonts.mkdir(exist_ok=True)
    # Font parsing is tested separately; load-job tests use this deterministic
    # resolver so they stay focused on job contracts.
    (fonts / "NotoSansCJKsc-Bold.ttc").write_bytes(b"font-fixture")
    _write_wav(wav)
    benchmark = _benchmark()
    write_reference_package(
        package,
        pair_id="001",
        benchmark=benchmark,
        subtitle=_subtitle(benchmark, audio_sha256=sha256_file(wav)),
        reference_audio=wav,
    )
    generated.write_bytes(b"video-fixture")
    runtime_receipt = root / "runtime-receipt.json"
    atomic_write_json(
        runtime_receipt,
        {
            "schema": "omni-stage2-runtime-receipt/v2",
            "record_id": "rec123",
            "mode": "reused",
            "provenance": {"kind": "verified_existing_runtime"},
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
            "cjk_bold_font_sha256": "f" * 64,
            "checked_at": "2026-11-07T08:00:00+08:00",
        },
    )
    job = root / "job.json"
    input_fingerprint = hashlib.sha256(b"fixture-input").hexdigest()
    strict_release = validate_installed_release()
    atomic_write_json(
        job,
        {
            "schema_version": 2,
            "record_id": "rec123",
            "video_number": "V-001",
            "dispatch_key": "dispatch|rec123",
            "input_fingerprint": input_fingerprint,
            "strict_release": strict_release,
            "attempt": 1,
            "inputs": [
                {
                    "pair_id": "001",
                    "reference_package": {"path": str(package), "sha256": _sha(package)},
                    "generated_video": {"path": str(generated), "sha256": _sha(generated)},
                }
            ],
            "output_dir": str(root / "results"),
            "caption_style": "D",
            "speed_policy": {"default_max": 1.25, "approved_exception_max": 1.9},
            "runtime": {
                "caption_fonts_dir": str(fonts),
                "runtime_receipt": str(runtime_receipt),
            },
            "stage2_started_receipt": {
                "phase": "stage2_started",
                "record_id": "rec123",
                "dispatch_key": "dispatch|rec123",
                "input_fingerprint": input_fingerprint,
                "strict_release": strict_release,
                "attempt": 1,
                "readback_verified": True,
                "updated_at": "2026-11-07T08:00:00+08:00",
            },
        },
    )
    return job

def _plan(speed: float, root: Path) -> dict:
    plan = AlignmentPlan(
        pair_id="001",
        generated_duration=speed,
        reference_duration=1.0,
        trim_start=0.0,
        trim_end=speed,
        output_duration=1.0,
        video_segments=[VideoSegment(0.0, speed, speed, 0.0)],
        audio_chunks=[AudioChunk(0.0, 1.0, 0.0, fade_seconds=0.0)],
        status="fail" if speed > 1.25 else "pass",
        reasons=[f"local speed {speed:.3f}x outside 0.85-1.25x near '测试'"] if speed > 1.25 else [],
        metadata={"approved_hard_speed_max": 1.25, "speed_exception_authorized": False},
    )
    plan_path = root / "plan.json"
    atomic_write_json(plan_path, plan.to_dict())
    mouth = MouthTrack(
        pair_id="001",
        duration=speed,
        fps=24,
        frames=[MouthFrame(0, 0, 0, 0, 1, False)],
        speech_start=0,
        speech_end=speed,
        active_segments=[[0, speed]],
        low_motion_candidates=[],
        quality=1,
    )
    mouth_path = root / "mouth.json"
    atomic_write_json(mouth_path, mouth.to_dict())
    return {"ok": False, "plan": str(plan_path), "mouth_track": str(mouth_path)}


class Stage2RunnerTests(unittest.TestCase):
    def test_caption_font_gate_rejects_workspace_without_font_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(Stage2RunnerError, "exact CJK bold caption font is unavailable"):
                _resolve_caption_fonts_dir(None, root=Path(tmp))

    def test_load_job_validates_hashes_and_package_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            job = _load_job(_make_job(Path(tmp)))
            self.assertEqual(job.record_id, "rec123")
            self.assertEqual(job.video_number, "V-001")
            self.assertEqual(job.inputs[0].pair_id, "001")
            self.assertEqual(job.delivery_policy.mode, "append")
            self.assertEqual(job.delivery_policy.replacements, ())

    def test_load_job_accepts_row_bound_automated_review_authorization(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_path = _make_job(Path(tmp))
            data = json.loads(job_path.read_text(encoding="utf-8"))
            data["review_authorization"] = {
                "method": "authorized_automated_audit",
                "record_id": "rec123",
                "dispatch_key": "dispatch|rec123",
                "attempt": 1,
                "statement": "owner explicitly authorized automated audit",
                "authorized_at": "2026-11-10T12:00:00+08:00",
            }
            atomic_write_json(job_path, data)
            job = _load_job(job_path)
            self.assertEqual(
                job.review_authorization["method"], "authorized_automated_audit"
            )
            data["review_authorization"]["record_id"] = "wrong"
            atomic_write_json(job_path, data)
            with self.assertRaisesRegex(Stage2RunnerError, "review_authorization.record_id"):
                _load_job(job_path)

    def test_load_job_accepts_exact_rework_replacement_and_task_chat_close(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_path = _make_job(Path(tmp))
            data = json.loads(job_path.read_text(encoding="utf-8"))
            data["delivery_policy"] = {
                "mode": "replace_rework",
                "replacements": [
                    {
                        "artifact_key": "complete_captioned",
                        "field_name": "素材3",
                        "file_token": "oldCaptionToken001",
                        "name": "previous-captioned.mp4",
                        "size_bytes": 1234,
                        "sha256": "a" * 64,
                    }
                ],
                "cloud_chat_lifecycle": {
                    "spawned_ordinary_chat_tab_id": "opaque-task-tab",
                    "close_after_success": True,
                    "scope": "spawned_ordinary_chat_only",
                },
            }
            atomic_write_json(job_path, data)
            job = _load_job(job_path)
            self.assertEqual(job.delivery_policy.mode, "replace_rework")
            self.assertEqual(job.delivery_policy.replacements[0].field_name, "素材3")
            self.assertEqual(
                job.delivery_policy.spawned_ordinary_chat_tab_id, "opaque-task-tab"
            )

    def test_load_job_rejects_rework_without_exact_safe_field_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_path = _make_job(Path(tmp))
            data = json.loads(job_path.read_text(encoding="utf-8"))
            data["delivery_policy"] = {
                "mode": "replace_rework",
                "replacements": [
                    {
                        "artifact_key": "complete_captioned",
                        "field_name": "素材2",
                        "file_token": "oldCaptionToken001",
                        "name": "previous-captioned.mp4",
                        "size_bytes": 1234,
                        "sha256": "a" * 64,
                    }
                ],
            }
            atomic_write_json(job_path, data)
            with self.assertRaisesRegex(Stage2RunnerError, "must target 素材3"):
                _load_job(job_path)

    def test_rework_post_delivery_plan_requires_verified_upload_before_exact_delete(self):
        policy = DeliveryPolicy(
            "replace_rework",
            (
                ReplacementTarget(
                    artifact_key="complete_captioned",
                    field_name="素材3",
                    file_token="oldCaptionToken001",
                    name="previous-captioned.mp4",
                    size_bytes=1234,
                    sha256="a" * 64,
                ),
            ),
            "opaque-task-tab",
        )
        job = SimpleNamespace(delivery_policy=policy, dispatch_key="dispatch|rec123")
        deliveries = [
            {"artifact_key": "segments", "field_name": "素材2", "name": "segments.zip", "size": 1, "sha256": "1" * 64},
            {"artifact_key": "complete", "field_name": "素材2", "name": "complete.mp4", "size": 2, "sha256": "2" * 64},
            {"artifact_key": "complete_captioned", "field_name": "素材3", "name": "captioned.mp4", "size": 3, "sha256": "3" * 64},
            {"artifact_key": "evidence", "field_name": "素材2", "name": "evidence.zip", "size": 4, "sha256": "4" * 64},
        ]
        actions = _post_delivery_actions(job, deliveries)
        self.assertEqual(actions["replacement_plan"][0]["remove_exact"]["file_token"], "oldCaptionToken001")
        self.assertTrue(actions["deletion_guards"]["upload_readback_required_before_delete"])
        self.assertEqual(actions["cloud_chat_lifecycle"]["state"], "pending_controller_close")
        self.assertEqual(actions["required_sequence"][-1], "close_only_the_spawned_ordinary_chat_tab_after_all_previous_steps_pass")

    def test_load_job_rejects_path_outside_row_workspace(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            job_path = _make_job(Path(tmp))
            data = json.loads(job_path.read_text(encoding="utf-8"))
            external = Path(outside) / "external.mp4"
            external.write_bytes(b"outside")
            data["inputs"][0]["generated_video"] = {"path": str(external), "sha256": _sha(external)}
            atomic_write_json(job_path, data)
            with self.assertRaisesRegex(Stage2RunnerError, "escapes the row workspace"):
                _load_job(job_path)

    def test_load_job_rejects_bad_hash(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_path = _make_job(Path(tmp))
            data = json.loads(job_path.read_text(encoding="utf-8"))
            data["inputs"][0]["generated_video"]["sha256"] = "0" * 64
            atomic_write_json(job_path, data)
            with self.assertRaisesRegex(Stage2RunnerError, "SHA-256 mismatch"):
                _load_job(job_path)

    def test_load_job_requires_video_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_path = _make_job(Path(tmp))
            data = json.loads(job_path.read_text(encoding="utf-8"))
            data.pop("video_number")
            atomic_write_json(job_path, data)
            with self.assertRaisesRegex(Stage2RunnerError, "video_number"):
                _load_job(job_path)

    def test_load_job_rejects_stale_release_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            job_path = _make_job(Path(tmp))
            data = json.loads(job_path.read_text(encoding="utf-8"))
            data["strict_release"]["manifest_sha256"] = "0" * 64
            atomic_write_json(job_path, data)
            with self.assertRaisesRegex(Stage2RunnerError, "strict_release"):
                _load_job(job_path)

    def test_speed_exception_only_retries_pure_upper_bound_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertTrue(should_retry_speed_exception(_plan(1.71, root), approved_max=1.9))
            self.assertFalse(should_retry_speed_exception(_plan(1.95, root), approved_max=1.9))

    def test_caption_timing_maps_reference_audio_to_output(self):
        benchmark = _benchmark()
        plan = AlignmentPlan(
            pair_id="001",
            generated_duration=1.0,
            reference_duration=1.0,
            trim_start=0,
            trim_end=1,
            output_duration=1.2,
            video_segments=[VideoSegment(0, 1, 1, 0)],
            audio_chunks=[AudioChunk(0.08, 0.92, 0.2, fade_seconds=0)],
        )
        events = build_caption_events(
            benchmark, plan, subtitle=_subtitle(benchmark), offset=2.0
        )
        self.assertEqual(len(events), 1)
        self.assertAlmostEqual(events[0]["start"], 2.22)
        self.assertAlmostEqual(events[0]["end"], 3.02)

    def test_v32_caption_events_are_consumed_without_re_splitting(self):
        benchmark = _benchmark()
        plan = AlignmentPlan(
            pair_id="001",
            generated_duration=1.0,
            reference_duration=1.0,
            trim_start=0,
            trim_end=1,
            output_duration=1.0,
            video_segments=[VideoSegment(0, 1, 1, 0)],
            audio_chunks=[AudioChunk(0.1, 0.9, 0.1, fade_seconds=0)],
        )
        events = build_caption_events(
            benchmark,
            plan,
            subtitle=_subtitle_v32(benchmark),
            measure_text=lambda value: len(value) * 10_000,
            max_width=1,
        )
        self.assertEqual([item["raw_text"] for item in events], ["测试"])
        self.assertEqual(events[0]["subtitle_event_id"], "c000")

    def test_caption_metric_parser_reads_ffmpeg_summary(self):
        self.assertAlmostEqual(
            _parse_metric_value("SSIM Y:0.999 U:0.999 V:0.999 All:0.996069", metric="ssim"),
            0.996069,
        )
        self.assertAlmostEqual(
            _parse_metric_value("PSNR y:51.2 u:51.2 v:51.2 average:51.286", metric="psnr"),
            51.286,
        )

    def test_caption_quality_normalizes_metric_timestamps_only(self):
        """A mux-origin offset must not compare adjacent visual frames."""
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            filters: list[str] = []

            def fake_run(command, **_kwargs):
                if "-filter_complex" not in command:
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                expression = command[command.index("-filter_complex") + 1]
                filters.append(expression)
                baseline = "caption-baseline.mp4" in " ".join(command)
                if "ssim=" in expression:
                    value = "0.998917" if baseline else "0.986393"
                    return SimpleNamespace(
                        returncode=0,
                        stdout=f"SSIM Y:0.999 U:0.999 V:0.999 All:{value}",
                        stderr="",
                    )
                value = "57.926" if baseline else "37.524"
                return SimpleNamespace(
                    returncode=0,
                    stdout=f"PSNR y:51.2 u:51.2 v:51.2 average:{value}",
                    stderr="",
                )

            with patch("benchmark_av_sync.stage2_runner.subprocess.run", side_effect=fake_run):
                report = _caption_visual_quality(
                    root / "complete.mp4",
                    root / "captioned.mp4",
                    ffmpeg=root / "ffmpeg",
                    work_dir=root / "quality",
                )
            self.assertTrue(report["passed"])
            self.assertEqual(report["comparison"], "same_codec_baseline")
            self.assertEqual(len(filters), 4)
            self.assertTrue(
                all("setpts=PTS-STARTPTS,fps=24" in expression for expression in filters)
            )

    def test_subtitle_contract_rejects_legacy_missing_leading_character(self):
        benchmark = BenchmarkScript(
            pair_id="001",
            text="我的测试",
            reference_duration=1.0,
            speech_start=0.08,
            speech_end=0.92,
            tokens=[
                AlignmentToken(1, "的", 0.1, 0.3),
                AlignmentToken(2, "测", 0.3, 0.6),
                AlignmentToken(3, "试", 0.6, 0.9),
            ],
            boundaries=[
                SafeBoundary(0, 0.3, 0, False),
                SafeBoundary(1, 0.6, 0, False),
            ],
            groups=[BenchmarkGroup("g00", "的测试", 1, 3, 0.1, 0.9, "c00")],
        )
        with self.assertRaisesRegex(SubtitleContractError, "aligned tokens"):
            _subtitle(benchmark)

    def test_caption_chunks_preserve_text_and_never_emit_line_breaks(self):
        text = "这是一个很长的中文字幕用于验证最多两行并且不会丢失任何文字"
        chunks = _caption_chunks(text, line_chars=8)
        rebuilt = "".join(chunks)
        self.assertEqual(rebuilt, text)
        self.assertTrue(all("\\N" not in chunk for chunk in chunks))

    def test_long_caption_splits_at_token_time_into_consecutive_single_lines(self):
        text = "如果法律都不能给他一个公道"
        tokens = [
            AlignmentToken(index, char, index / 5, (index + 1) / 5)
            for index, char in enumerate(text)
        ]
        benchmark = BenchmarkScript(
            pair_id="001",
            text=text,
            reference_duration=2.8,
            speech_start=0.0,
            speech_end=2.6,
            tokens=tokens,
            boundaries=[
                SafeBoundary(index, (index + 1) / 5, 0, False)
                for index in range(len(tokens) - 1)
            ],
            groups=[
                BenchmarkGroup("g00", text[:-1], 0, 11, 0.0, 2.4, "c00"),
                BenchmarkGroup("g01", text[-1], 12, 12, 2.4, 2.6, "c00"),
            ],
        )
        plan = AlignmentPlan(
            pair_id="001",
            generated_duration=2.6,
            reference_duration=2.8,
            trim_start=0,
            trim_end=2.6,
            output_duration=2.8,
            video_segments=[VideoSegment(0, 2.6, 1, 0)],
            audio_chunks=[AudioChunk(0.0, 2.6, 0.2, fade_seconds=0)],
        )
        events = build_caption_events(
            benchmark,
            plan,
            subtitle=_subtitle(benchmark),
            measure_text=lambda value: len(value) * 70,
            max_width=700,
        )
        self.assertEqual([item["text"] for item in events], ["如果法律", "都不能给他一个公道"])
        self.assertAlmostEqual(events[0]["end"], 1.0)
        self.assertAlmostEqual(events[1]["start"], 1.0)
        self.assertTrue(all(item["end"] - item["start"] >= 0.5 for item in events))
        self.assertTrue(all("\\N" not in item["text"] for item in events))

    def test_short_caption_tail_merges_into_previous_phrase(self):
        benchmark = _benchmark()
        benchmark.groups = [
            BenchmarkGroup("g00", "如果法律都不能给他一个公", 0, 0, 0.1, 0.8, "c00"),
            BenchmarkGroup("g01", "道", 1, 1, 0.8, 0.9, "c00"),
        ]
        groups = _caption_groups(benchmark)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].text, "如果法律都不能给他一个公道")

    def test_ass_uses_selected_style_d(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "captions.ass"
            write_ass(path, [{"start": 0.1, "end": 0.9, "text": "测试"}])
            content = path.read_text(encoding="utf-8")
            self.assertIn("Noto Sans CJK SC,108", content)
            self.assertIn("CaptionFill", content)
            self.assertIn(",100,100,2,0,1,5.8,0,5,", content)
            self.assertIn(",100,100,2,0,1,1.65,0,5,", content)
            self.assertIn(",100,100,2,0,1,10,0,5,", content)
            self.assertIn("WrapStyle: 2", content)
            self.assertIn(r"\q2", content)
            self.assertIn(r"\pos(540,1136)", content)
            self.assertIn(r"\pos(540,1148)", content)
            self.assertIn(r"\fscx100", content)
            self.assertIn(r"\blur6", content)

    def test_ass_narrows_only_long_approved_caption_lines(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "captions.ass"
            write_ass(
                path,
                [
                    {"start": 0.1, "end": 0.9, "text": "短句"},
                    {"start": 1.0, "end": 1.9, "text": "为了那些无处声张的正义感"},
                ],
            )
            content = path.read_text(encoding="utf-8")
            self.assertIn(r"\fs108\fscx100", content)
            self.assertIn(r"\fs104\fscx88", content)

    def test_bmp_bbox_parser_finds_non_background_pixels(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "probe.bmp"
            width, height, channels = 4, 3, 3
            stride = ((width * channels + 3) // 4) * 4
            pixels = bytearray([128] * (stride * height))
            for x, y in ((1, 1), (2, 1)):
                stored_y = height - 1 - y
                offset = stored_y * stride + x * channels
                pixels[offset : offset + 3] = b"\xff\xff\xff"
            header_size = 54
            header = bytearray(header_size)
            header[:2] = b"BM"
            struct.pack_into("<I", header, 2, header_size + len(pixels))
            struct.pack_into("<I", header, 10, header_size)
            struct.pack_into("<I", header, 14, 40)
            struct.pack_into("<i", header, 18, width)
            struct.pack_into("<i", header, 22, height)
            struct.pack_into("<H", header, 26, 1)
            struct.pack_into("<H", header, 28, 24)
            path.write_bytes(header + pixels)
            self.assertEqual(
                _bmp_non_background_bbox(path),
                {"x1": 1, "y1": 1, "x2": 2, "y2": 1, "width": 2, "height": 1},
            )

    def test_post_validation_failure_writes_result_json(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = _make_job(root)
            with (
                patch("benchmark_av_sync.stage2_runner.shutil.which", return_value=None),
                patch(
                    "benchmark_av_sync.stage2_runner._resolve_caption_fonts_dir",
                    side_effect=lambda _value, *, root: _font_gate(root),
                ),
            ):
                result = run_row(job_path)
            self.assertFalse(result["ok"])
            self.assertEqual(result["error_class"], "runtime_error")
            persisted = json.loads((Path(tmp) / "results" / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["dispatch_key"], "dispatch|rec123")

    def test_run_row_packages_exact_four_deliveries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            job_path = _make_job(root)

            def renderer(**kwargs):
                pair_root = Path(kwargs["output_dir"])
                edit = pair_root / "edit"
                (edit / "finals").mkdir(parents=True, exist_ok=True)
                (edit / "plans").mkdir(parents=True, exist_ok=True)
                (edit / "mouth_tracks").mkdir(parents=True, exist_ok=True)
                (edit / "activity_plots").mkdir(parents=True, exist_ok=True)
                (edit / "verify").mkdir(parents=True, exist_ok=True)
                output = edit / "finals" / "001.mp4"
                output.write_bytes(b"segment")
                plan = AlignmentPlan(
                    pair_id="001",
                    generated_duration=1,
                    reference_duration=1,
                    trim_start=0,
                    trim_end=1,
                    output_duration=1,
                    video_segments=[VideoSegment(0, 1, 1, 0)],
                    audio_chunks=[AudioChunk(0, 1, 0, fade_seconds=0)],
                )
                plan_path = edit / "plans" / "001.json"
                mouth_path = edit / "mouth_tracks" / "001.json"
                plot_path = edit / "activity_plots" / "001.png"
                plan_verify = edit / "verify" / "001.plan.json"
                media_verify = edit / "verify" / "001.media.json"
                atomic_write_json(plan_path, plan.to_dict())
                atomic_write_json(mouth_path, {"status": "pass"})
                plot_path.write_bytes(b"png")
                atomic_write_json(plan_verify, {"passed": True})
                atomic_write_json(media_verify, {"passed": True})
                result = {
                    "ok": True,
                    "status": "pass",
                    "output": str(output),
                    "plan": str(plan_path),
                    "mouth_track": str(mouth_path),
                    "activity_plot": str(plot_path),
                    "plan_verification": str(plan_verify),
                    "media_verification": str(media_verify),
                    "review_required": False,
                    "generated_audio_used": False,
                }
                atomic_write_json(edit / "online-001.result.json", result)
                return result

            def fake_complete(segments, *, output, ffmpeg):
                output.write_bytes(b"complete-video")
                return {"method": "concat_demuxer_stream_copy", "video_reencoded": False}

            def fake_captioned(source, *, ass_path, output, ffmpeg, fonts_dir):
                self.assertEqual(source.name, "omni_stage2__rec123__complete.mp4")
                self.assertEqual(fonts_dir, root / "fonts")
                output.write_bytes(b"complete-captioned-video")

            def fake_frames(output, *, events, frame_dir, ffmpeg):
                frame_dir.mkdir(parents=True, exist_ok=True)
                paths = []
                for index in range(3):
                    path = frame_dir / f"frame-{index}.png"
                    path.write_bytes(b"png")
                    paths.append(path)
                for event in events:
                    event["frame_checks"] = [
                        {"label": label, "at": index / 10, "frame": f"caption-frames/frame-{index}.png"}
                        for index, label in enumerate(("start", "mid", "end"))
                    ]
                return paths

            report = VerificationReport("rec123", True, metrics={"decode_returncode": 0})

            def fake_font_probe(_ffmpeg, *, fonts_dir, work_dir):
                self.assertEqual(fonts_dir, root / "fonts")
                work_dir.mkdir(parents=True, exist_ok=True)
                log = work_dir / "font-resolution.log"
                trace = "fontselect: (Noto Sans CJK SC, 700, 0) -> NotoSansCJKsc-Bold\n"
                log.write_text(trace, encoding="utf-8")
                return log, trace

            def fake_caption_measurer(*, ffmpeg, work_dir, fonts_dir):
                self.assertEqual(fonts_dir, root / "fonts")
                cache = {}

                def measure(text):
                    cache[text] = {"x1": 100, "y1": 100, "x2": 200, "y2": 200, "width": 101, "height": 101}
                    return 101

                return measure, cache

            with (
                patch("benchmark_av_sync.stage2_runner._resolve_executable", return_value=root / "tool"),
                patch(
                    "benchmark_av_sync.stage2_runner._resolve_caption_fonts_dir",
                    side_effect=lambda _value, *, root: _font_gate(root),
                ),
                patch(
                    "benchmark_av_sync.stage2_runner._verify_libass_font_resolution",
                    side_effect=fake_font_probe,
                ),
                patch("benchmark_av_sync.stage2_runner._make_caption_measurer", side_effect=fake_caption_measurer),
                patch("benchmark_av_sync.stage2_runner._render_complete", side_effect=fake_complete),
                patch("benchmark_av_sync.stage2_runner._render_captioned", side_effect=fake_captioned),
                patch("benchmark_av_sync.stage2_runner._audio_payload_sha256", return_value="a" * 64),
                patch(
                    "benchmark_av_sync.stage2_runner._caption_visual_quality",
                    return_value={"passed": True, "ssim": 1.0, "psnr": 99.0},
                ),
                patch("benchmark_av_sync.stage2_runner._extract_caption_frames", side_effect=fake_frames),
                patch("benchmark_av_sync.stage2_runner.verify_rendered_media", return_value=report),
            ):
                result = run_row(job_path, renderer=renderer)
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["deliveries"]), 4)
            self.assertEqual(
                {item["name"].split("__")[-1] for item in result["deliveries"]},
                {"segments.zip", "complete.mp4", "complete_captioned.mp4", "evidence.zip"},
            )
            self.assertEqual(
                result["complete_audio_verification"],
                {"matched": True, "sha256": "a" * 64},
            )
            self.assertEqual(result["delivery_policy"]["mode"], "append")
            self.assertEqual(result["strict_release"], validate_installed_release())
            self.assertEqual(
                result["post_delivery_actions"]["cloud_chat_lifecycle"]["state"],
                "not_applicable_no_spawned_ordinary_chat",
            )
            self.assertEqual(
                result["post_delivery_actions"]["deletion_guards"]["delete_on_failure_forbidden"],
                True,
            )
            evidence = next(Path(item["path"]) for item in result["deliveries"] if item["name"].endswith("evidence.zip"))
            with zipfile.ZipFile(evidence) as archive:
                self.assertEqual(len(archive.namelist()), len(set(archive.namelist())))
                self.assertIn("strict-release-lock.json", archive.namelist())
                self.assertIn("font-resolution.log", archive.namelist())
                self.assertEqual(
                    json.loads(archive.read("strict-release-lock.json")),
                    validate_installed_release(),
                )
                manifest = json.loads(archive.read("manifest.json"))
                self.assertTrue(
                    all(len(event["frame_checks"]) == 3 for event in manifest["caption"]["events"])
                )


if __name__ == "__main__":
    unittest.main()
