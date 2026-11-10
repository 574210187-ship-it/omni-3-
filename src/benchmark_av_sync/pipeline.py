from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

from .alignment import clean_alignment
from .benchmark_json import build_benchmark_script
from .contracts import BenchmarkScript, MouthTrack
from .external import WorkerError, run_worker
from .mouth_track import analyze_mouth_frames, write_activity_plot
from .pairing import build_pair_manifest
from .probe import probe_media
from .render import render_plan
from .subtitle_contract import build_subtitle_contract, select_caption_source
from .text_fusion import fuse_texts
from .timeline import solve_timeline
from .visual_timeline import VisualSolverConfig, solve_visual_timeline
from .verify import verify_plan, verify_rendered_media
from .workspace import BatchWorkspace, atomic_write_json, read_json, sha256_file


class PipelineError(RuntimeError):
    pass


def _run(command: list[str], error: str) -> None:
    completed = subprocess.run(
        command, capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        raise PipelineError(completed.stderr[-2000:] or error)


def extract_alignment_audio(video: Path, output: Path, ffmpeg: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    _run(
        [
            str(ffmpeg),
            "-y",
            "-v",
            "error",
            "-i",
            str(video),
            "-map",
            "0:a:0",
            "-vn",
            "-ac",
            "1",
            "-ar",
            "16000",
            "-c:a",
            "pcm_s16le",
            str(output),
        ],
        f"failed to extract audio from {video}",
    )


def environment_report(
    *,
    ffmpeg: Path,
    ffprobe: Path,
    qwen_python: Path,
    aligner_model: Path,
    asr_model: Path,
    ocr_python: Path | None = None,
    mouth_python: Path | None = None,
) -> dict[str, Any]:
    ocr_python = ocr_python or Path(sys.executable)
    checks = {
        "ffmpeg": ffmpeg.is_file(),
        "ffprobe": ffprobe.is_file(),
        "ocr_python": ocr_python.is_file(),
        "qwen_python": qwen_python.is_file(),
        "aligner_model": aligner_model.is_dir(),
        "asr_model": asr_model.is_dir(),
    }
    if mouth_python is not None:
        checks["mouth_python"] = mouth_python.is_file()
    return {
        "ok": all(checks.values()),
        "checks": checks,
        "paths": {
            "ffmpeg": str(ffmpeg.resolve()),
            "ffprobe": str(ffprobe.resolve()),
            "ocr_python": str(ocr_python.resolve()),
            "qwen_python": str(qwen_python.resolve()),
            "aligner_model": str(aligner_model.resolve()),
            "asr_model": str(asr_model.resolve()),
            **({"mouth_python": str(mouth_python.resolve())} if mouth_python else {}),
        },
    }


def build_reference_evidence(
    *,
    pair_id: str,
    pair: dict[str, Any],
    workspace: BatchWorkspace,
    ffmpeg: Path,
    qwen_python: Path,
    aligner_model: Path,
    asr_model: Path,
    ocr_python: Path,
    subtitle_box: list[int] | None,
) -> tuple[dict[str, Any], BenchmarkScript, dict[str, Any]]:
    reference_video = Path(pair["reference"]["path"])
    pair_root = workspace.edit / "cache" / pair_id
    pair_root.mkdir(parents=True, exist_ok=True)
    reference_wav = pair_root / "reference.wav"
    if not reference_wav.is_file():
        extract_alignment_audio(reference_video, reference_wav, ffmpeg)
    transcript_path = workspace.edit / "canonical_transcripts" / f"{pair_id}.json"
    alignment_path = workspace.edit / "alignments" / f"{pair_id}.reference.json"
    benchmark_path = workspace.edit / "benchmark_json" / f"{pair_id}.json"
    subtitle_path = workspace.edit / "subtitle_json" / f"{pair_id}.json"
    if benchmark_path.is_file() and transcript_path.is_file() and subtitle_path.is_file():
        return (
            read_json(transcript_path),
            BenchmarkScript.from_dict(read_json(benchmark_path)),
            read_json(subtitle_path),
        )

    evidence_errors: list[str] = []
    ocr_result: dict[str, Any] = {}
    asr_result: dict[str, Any] = {}
    try:
        ocr_request: dict[str, Any] = {"video_path": str(reference_video), "language": "ch"}
        if subtitle_box is not None:
            ocr_request["subtitle_box"] = subtitle_box
        ocr_result = run_isolated_worker(
            python=ocr_python,
            module="benchmark_av_sync.workers.ocr_worker",
            request=ocr_request,
            root=pair_root,
            stem="ocr",
        )
    except WorkerError as exc:
        raise PipelineError(f"reference subtitle visual scan failed: {exc}") from exc
    try:
        asr_result = run_isolated_worker(
            python=qwen_python,
            module="benchmark_av_sync.workers.asr_worker",
            request={
                "model_path": str(asr_model),
                "audio_path": str(reference_wav),
                "language": "Chinese",
                "device": "cuda:0",
            },
            root=pair_root,
            stem="asr",
        )
    except WorkerError as exc:
        raise PipelineError(f"reference audio recognition failed: {exc}") from exc
    selected = select_caption_source(ocr_result, asr_result)
    canonical = {
        "version": 2,
        "text": selected["canonical_caption_text"],
        "source_mode": selected["source_mode"],
        "visual_subtitle_present": selected["visual_subtitle_present"],
        "visual_events": selected["visual_events"],
        "asr_text": selected["asr_text"],
        "conflicts": selected["conflicts"],
        "quality_status": selected["quality_status"],
        "reasons": selected["reasons"],
        "evidence_errors": evidence_errors,
    }
    atomic_write_json(transcript_path, canonical)

    raw = run_isolated_worker(
        python=qwen_python,
        module="benchmark_av_sync.workers.qwen_align_worker",
        request={
            "model_path": str(aligner_model),
            "audio_path": str(reference_wav),
            "text": canonical["text"],
            "language": "Chinese",
            "device": "cuda:0",
        },
        root=pair_root,
        stem="align.reference",
    )
    reference_duration = float(pair["reference"]["media"]["duration"])
    qc = clean_alignment(raw["items"], audio_duration=reference_duration)
    atomic_write_json(alignment_path, qc.to_dict())
    if qc.status == "fail":
        raise PipelineError("reference forced alignment failed quality checks")
    benchmark = build_benchmark_script(
        pair_id,
        canonical["text"],
        qc.tokens,
        reference_duration=reference_duration,
        wav_path=reference_wav,
    )
    atomic_write_json(benchmark_path, benchmark.to_dict())
    if benchmark.status == "fail":
        raise PipelineError("reference benchmark failed caption coverage checks")
    subtitle = build_subtitle_contract(
        pair_id=pair_id,
        reference_video_sha256=str(pair["reference"]["sha256"]),
        reference_audio_sha256=sha256_file(reference_wav),
        reference_duration=reference_duration,
        selected=selected,
        aligned_tokens=benchmark.tokens,
    )
    atomic_write_json(subtitle_path, subtitle)
    return canonical, benchmark, subtitle


def run_visual_sample(
    *,
    pair_id: str,
    reference_dir: Path,
    generated_dir: Path,
    batch_dir: Path,
    ffmpeg: Path,
    ffprobe: Path,
    qwen_python: Path,
    mouth_python: Path,
    aligner_model: Path,
    asr_model: Path,
    ocr_python: Path | None = None,
    subtitle_box: list[int] | None = None,
    preview: bool = True,
    approved_hard_speed_max: float | None = None,
) -> dict[str, Any]:
    report = environment_report(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        qwen_python=qwen_python,
        aligner_model=aligner_model,
        asr_model=asr_model,
        ocr_python=ocr_python,
        mouth_python=mouth_python,
    )
    if not report["ok"]:
        missing = [name for name, ok in report["checks"].items() if not ok]
        raise PipelineError("missing dependencies: " + ", ".join(missing))
    ocr_python = ocr_python or Path(sys.executable)
    workspace = BatchWorkspace(batch_dir)
    workspace.ensure()
    manifest = build_pair_manifest(
        reference_dir,
        generated_dir,
        probe=lambda path: probe_media(path, ffprobe),
        hash_files=True,
    )
    atomic_write_json(workspace.edit / "pair_manifest.json", manifest)
    pair = next((item for item in manifest["pairs"] if item["pair_id"] == pair_id), None)
    if pair is None:
        raise PipelineError(f"pair id {pair_id!r} is not uniquely available")

    canonical, benchmark, _subtitle = build_reference_evidence(
        pair_id=pair_id,
        pair=pair,
        workspace=workspace,
        ffmpeg=ffmpeg,
        qwen_python=qwen_python,
        aligner_model=aligner_model,
        asr_model=asr_model,
        ocr_python=ocr_python,
        subtitle_box=subtitle_box,
    )
    pair_root = workspace.edit / "cache" / pair_id
    generated_video = Path(pair["generated"]["path"])
    mouth_raw = run_isolated_worker(
        python=mouth_python,
        module="benchmark_av_sync.workers.mouth_track_worker",
        request={"video_path": str(generated_video)},
        root=pair_root,
        stem="mouth",
    )
    mouth = analyze_mouth_frames(
        pair_id,
        mouth_raw["frames"],
        duration=float(mouth_raw["duration"]),
        fps=float(mouth_raw["fps"]),
    )
    mouth_path = workspace.edit / "mouth_tracks" / f"{pair_id}.json"
    atomic_write_json(mouth_path, mouth.to_dict())
    write_activity_plot(mouth, workspace.edit / "activity_plots" / f"{pair_id}.png")

    visual_config = VisualSolverConfig(
        hard_speed_max=approved_hard_speed_max or 1.25
    )
    plan = solve_visual_timeline(benchmark, mouth, config=visual_config)
    plan_path = workspace.edit / "plans" / f"{pair_id}.json"
    atomic_write_json(plan_path, plan.to_dict())
    plan_report = verify_plan(plan)
    atomic_write_json(workspace.edit / "verify" / f"{pair_id}.plan.json", plan_report.to_dict())
    if not plan_report.passed:
        result = {
            "ok": False,
            "pair_id": pair_id,
            "status": plan.status,
            "output": None,
            "plan": str(plan_path.resolve()),
            "benchmark_json": str((workspace.edit / "benchmark_json" / f"{pair_id}.json").resolve()),
            "mouth_track": str(mouth_path.resolve()),
            "reasons": plan.reasons,
        }
        atomic_write_json(workspace.edit / f"visual-{pair_id}.result.json", result)
        return result

    reference_video = Path(pair["reference"]["path"])
    output = (workspace.edit / "previews" if preview else workspace.edit / "finals") / f"{pair_id}.mp4"
    render_plan(
        plan,
        generated_video=generated_video,
        reference_video=reference_video,
        output_path=output,
        ffmpeg_path=ffmpeg,
        preview=preview,
    )
    media_report = verify_rendered_media(
        output,
        pair_id=pair_id,
        expected_duration=plan.output_duration,
        ffprobe_path=ffprobe,
        ffmpeg_path=ffmpeg,
    )
    atomic_write_json(workspace.edit / "verify" / f"{pair_id}.media.json", media_report.to_dict())
    if not media_report.passed:
        raise PipelineError("rendered media failed verification")
    result = {
        "ok": True,
        "pair_id": pair_id,
        "status": plan.status,
        "output": str(output.resolve()),
        "plan": str(plan_path.resolve()),
        "benchmark_json": str((workspace.edit / "benchmark_json" / f"{pair_id}.json").resolve()),
        "mouth_track": str(mouth_path.resolve()),
        "transcript": canonical["text"],
        "review_required": plan.status == "review",
    }
    atomic_write_json(workspace.edit / f"visual-{pair_id}.result.json", result)
    return result


def run_isolated_worker(
    *,
    python: Path,
    module: str,
    request: dict[str, Any],
    root: Path,
    stem: str,
) -> dict[str, Any]:
    package_src = Path(__file__).resolve().parents[1]
    return run_worker(
        python_executable=python,
        module=module,
        request=request,
        request_path=root / f"{stem}.request.json",
        response_path=root / f"{stem}.response.json",
        module_root=package_src,
    )


def run_sample(
    *,
    pair_id: str,
    reference_dir: Path,
    generated_dir: Path,
    batch_dir: Path,
    ffmpeg: Path,
    ffprobe: Path,
    qwen_python: Path,
    aligner_model: Path,
    asr_model: Path,
    ocr_python: Path | None = None,
    subtitle_box: list[int] | None = None,
    preview: bool = True,
) -> dict[str, Any]:
    report = environment_report(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        qwen_python=qwen_python,
        aligner_model=aligner_model,
        asr_model=asr_model,
        ocr_python=ocr_python,
    )
    if not report["ok"]:
        missing = [name for name, ok in report["checks"].items() if not ok]
        raise PipelineError("missing dependencies: " + ", ".join(missing))
    ocr_python = ocr_python or Path(sys.executable)
    workspace = BatchWorkspace(batch_dir)
    workspace.ensure()
    manifest = build_pair_manifest(
        reference_dir,
        generated_dir,
        probe=lambda path: probe_media(path, ffprobe),
        hash_files=True,
    )
    atomic_write_json(workspace.edit / "pair_manifest.json", manifest)
    pair = next((x for x in manifest["pairs"] if x["pair_id"] == pair_id), None)
    if pair is None:
        available = ", ".join(x["pair_id"] for x in manifest["pairs"]) or "none"
        raise PipelineError(f"pair id {pair_id!r} is not uniquely available; available: {available}")

    reference_video = Path(pair["reference"]["path"])
    generated_video = Path(pair["generated"]["path"])
    pair_root = workspace.edit / "cache" / pair_id
    pair_root.mkdir(parents=True, exist_ok=True)
    reference_wav = pair_root / "reference.wav"
    generated_wav = pair_root / "generated.wav"
    extract_alignment_audio(reference_video, reference_wav, ffmpeg)
    extract_alignment_audio(generated_video, generated_wav, ffmpeg)

    evidence_errors: list[str] = []
    ocr_result: dict[str, Any] = {}
    asr_result: dict[str, Any] = {}
    try:
        ocr_request: dict[str, Any] = {"video_path": str(reference_video), "language": "ch"}
        if subtitle_box is not None:
            ocr_request["subtitle_box"] = subtitle_box
        ocr_result = run_isolated_worker(
            python=ocr_python,
            module="benchmark_av_sync.workers.ocr_worker",
            request=ocr_request,
            root=pair_root,
            stem="ocr",
        )
    except WorkerError as exc:
        evidence_errors.append(f"ocr: {exc}")
    try:
        asr_result = run_isolated_worker(
            python=qwen_python,
            module="benchmark_av_sync.workers.asr_worker",
            request={
                "model_path": str(asr_model),
                "audio_path": str(reference_wav),
                "language": "Chinese",
                "device": "cuda:0",
            },
            root=pair_root,
            stem="asr",
        )
    except WorkerError as exc:
        evidence_errors.append(f"asr: {exc}")
    ocr_text = str(ocr_result.get("text", ""))
    asr_text = str(asr_result.get("text", ""))
    if not ocr_text and not asr_text:
        raise PipelineError("OCR and ASR produced no usable transcript; " + "; ".join(evidence_errors))
    canonical = fuse_texts(asr_text, ocr_text)
    canonical["evidence_errors"] = evidence_errors
    transcript_path = workspace.edit / "canonical_transcripts" / f"{pair_id}.json"
    atomic_write_json(transcript_path, canonical)

    alignment_results: dict[str, Any] = {}
    for side, wav in (("reference", reference_wav), ("generated", generated_wav)):
        raw = run_isolated_worker(
            python=qwen_python,
            module="benchmark_av_sync.workers.qwen_align_worker",
            request={
                "model_path": str(aligner_model),
                "audio_path": str(wav),
                "text": canonical["text"],
                "language": "Chinese",
                "device": "cuda:0",
            },
            root=pair_root,
            stem=f"align.{side}",
        )
        duration = float(pair[side]["media"]["duration"])
        qc = clean_alignment(raw["items"], audio_duration=duration)
        alignment_results[side] = qc
        atomic_write_json(
            workspace.edit / "alignments" / f"{pair_id}.{side}.json", qc.to_dict()
        )
        if qc.status == "fail":
            raise PipelineError(f"{side} forced alignment failed quality checks")

    plan = solve_timeline(
        pair_id,
        alignment_results["reference"].tokens,
        alignment_results["generated"].tokens,
        reference_duration=float(pair["reference"]["media"]["duration"]),
        generated_duration=float(pair["generated"]["media"]["duration"]),
    )
    plan_path = workspace.edit / "plans" / f"{pair_id}.json"
    atomic_write_json(plan_path, plan.to_dict())
    plan_report = verify_plan(plan)
    atomic_write_json(workspace.edit / "verify" / f"{pair_id}.plan.json", plan_report.to_dict())
    if not plan_report.passed:
        raise PipelineError("alignment plan failed mechanical verification")

    output = (workspace.edit / "previews" if preview else workspace.edit / "finals") / f"{pair_id}.mp4"
    render_plan(
        plan,
        generated_video=generated_video,
        reference_video=reference_video,
        output_path=output,
        ffmpeg_path=ffmpeg,
        preview=preview,
    )
    media_report = verify_rendered_media(
        output,
        pair_id=pair_id,
        expected_duration=plan.output_duration,
        ffprobe_path=ffprobe,
        ffmpeg_path=ffmpeg,
    )
    atomic_write_json(workspace.edit / "verify" / f"{pair_id}.media.json", media_report.to_dict())
    if not media_report.passed:
        raise PipelineError("rendered media failed verification")
    result = {
        "ok": True,
        "pair_id": pair_id,
        "status": plan.status,
        "output": str(output.resolve()),
        "plan": str(plan_path.resolve()),
        "transcript": str(transcript_path.resolve()),
        "review_required": plan.status == "review",
        "manifest_error_count": len(manifest["errors"]),
    }
    atomic_write_json(workspace.edit / f"sample-{pair_id}.result.json", result)
    return result
