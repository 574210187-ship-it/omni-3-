from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .handoff import open_reference_package, write_reference_package
from .mouth_track import analyze_mouth_frames, write_activity_plot
from .pairing import DEFAULT_ID_PATTERN, extract_pair_id
from .pipeline import build_reference_evidence, run_isolated_worker
from .probe import probe_media
from .render import render_plan
from .verify import verify_plan, verify_rendered_media
from .visual_timeline import (
    VisualSolverConfig,
    solve_visual_timeline,
    verify_rendered_stage1_sync,
)
from .workspace import BatchWorkspace, atomic_write_json, sha256_file


class TwoStageError(RuntimeError):
    pass


def _resolved_executable(name: str, supplied: Path | None) -> Path:
    if supplied is not None:
        path = supplied.resolve()
        if not path.is_file():
            raise TwoStageError(f"{name} executable does not exist: {path}")
        return path
    discovered = shutil.which(name)
    if not discovered:
        raise TwoStageError(f"{name} is not available on PATH")
    return Path(discovered).resolve()


def _same_numeric_id(left: str, right: str) -> bool:
    return left.isdigit() and right.isdigit() and int(left) == int(right)


def _generated_pair_id(path: Path, expected: str) -> str:
    # Bitable's main-video contract uses a leading pair prefix (for example
    # ``004_<asset-hash>_1080p.mp4``).  The opaque asset hash and resolution
    # are allowed to contain further digits, including a bare ``4`` that is
    # numerically equal to pair ``004``.  Prefer the contractual leading
    # prefix before applying the legacy generic-token fallback.
    # Only recognise the Bitable attachment contract here; a loose ``01_``
    # prefix would hide a second pair token in names such as ``01_copy_001``.
    leading = re.match(r"^(\d+)_[0-9a-fA-F]{8}_\d{3,4}p$", path.stem)
    if leading and _same_numeric_id(leading.group(1), expected):
        return leading.group(1)
    try:
        return extract_pair_id(path)
    except ValueError:
        matches = [item.group(1) for item in re.finditer(DEFAULT_ID_PATTERN, path.stem)]
        matching = [item for item in matches if _same_numeric_id(item, expected)]
        if len(matching) != 1:
            raise TwoStageError(
                f"expected exactly one numeric token matching package ID {expected!r} "
                f"in {path.name!r}, found {len(matching)}"
            )
        return matching[0]


def build_reference_package_from_video(
    *,
    pair_id: str,
    reference_video: Path,
    output: Path,
    work_dir: Path,
    ffmpeg: Path,
    ffprobe: Path,
    qwen_python: Path,
    aligner_model: Path,
    asr_model: Path,
    ocr_python: Path | None = None,
    subtitle_box: list[int] | None = None,
) -> dict[str, Any]:
    if not pair_id.isdigit():
        raise TwoStageError("pair ID must contain digits only")
    reference_video = reference_video.resolve()
    if not reference_video.is_file():
        raise TwoStageError(f"reference video does not exist: {reference_video}")
    workspace = BatchWorkspace(work_dir)
    workspace.ensure()
    media = probe_media(reference_video, ffprobe)
    pair = {
        "pair_id": pair_id,
        "reference": {
            "path": str(reference_video),
            "size": reference_video.stat().st_size,
            "sha256": sha256_file(reference_video),
            "media": media,
        },
    }
    canonical, benchmark, subtitle = build_reference_evidence(
        pair_id=pair_id,
        pair=pair,
        workspace=workspace,
        ffmpeg=ffmpeg,
        qwen_python=qwen_python,
        aligner_model=aligner_model,
        asr_model=asr_model,
        ocr_python=ocr_python or Path(sys.executable),
        subtitle_box=subtitle_box,
    )
    reference_audio = workspace.edit / "cache" / pair_id / "reference.wav"
    manifest = write_reference_package(
        output,
        pair_id=pair_id,
        benchmark=benchmark,
        subtitle=subtitle,
        reference_audio=reference_audio,
    )
    return {
        "ok": True,
        "pair_id": pair_id,
        "output": str(output.resolve()),
        "output_sha256": sha256_file(output) if output.suffix.lower() == ".zip" else None,
        "transcript": canonical["text"],
        "benchmark_status": benchmark.status,
        "subtitle_status": subtitle["quality_status"],
        "subtitle_source_mode": subtitle["source_mode"],
        "manifest": manifest,
    }


def online_preflight(
    *,
    ffmpeg: Path | None = None,
    ffprobe: Path | None = None,
    python_executable: Path | None = None,
) -> dict[str, Any]:
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    errors: list[str] = []
    try:
        ffmpeg_path = _resolved_executable("ffmpeg", ffmpeg)
        checks["ffmpeg"] = True
        details["ffmpeg"] = str(ffmpeg_path)
    except TwoStageError as exc:
        checks["ffmpeg"] = False
        errors.append(str(exc))
        ffmpeg_path = None
    try:
        ffprobe_path = _resolved_executable("ffprobe", ffprobe)
        checks["ffprobe"] = True
        details["ffprobe"] = str(ffprobe_path)
    except TwoStageError as exc:
        checks["ffprobe"] = False
        errors.append(str(exc))
        ffprobe_path = None

    python_path = (python_executable or Path(sys.executable)).resolve()
    checks["python"] = python_path.is_file()
    details["python"] = str(python_path)
    if not checks["python"]:
        errors.append(f"Python executable does not exist: {python_path}")
    else:
        script = (
            "import json, cv2, mediapipe as mp, numpy as np; "
            "assert hasattr(mp, 'solutions') and hasattr(mp.solutions, 'face_mesh'); "
            "print(json.dumps({'opencv': cv2.__version__, "
            "'mediapipe': mp.__version__, 'numpy': np.__version__}))"
        )
        completed = subprocess.run(
            [str(python_path), "-c", script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        checks["mediapipe_import"] = completed.returncode == 0
        if checks["mediapipe_import"]:
            try:
                details["python_packages"] = json.loads(completed.stdout.strip())
            except json.JSONDecodeError:
                details["python_packages"] = completed.stdout.strip()
        else:
            errors.append(completed.stderr[-1000:] or "MediaPipe import check failed")

    if ffmpeg_path is not None:
        completed = subprocess.run(
            [str(ffmpeg_path), "-hide_banner", "-filters"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        checks["ffmpeg_minterpolate"] = (
            completed.returncode == 0 and "minterpolate" in completed.stdout
        )
        if not checks["ffmpeg_minterpolate"]:
            errors.append("FFmpeg does not expose the minterpolate filter")
        checks["ffmpeg_ass"] = completed.returncode == 0 and bool(
            re.search(r"\b(?:ass|subtitles)\b", completed.stdout)
        )
        if not checks["ffmpeg_ass"]:
            errors.append("FFmpeg does not expose an ASS/libass subtitle filter")
    else:
        checks["ffmpeg_minterpolate"] = False
        checks["ffmpeg_ass"] = False

    return {
        "ok": all(checks.values()),
        "checks": checks,
        "details": details,
        "errors": errors,
        "resolved": {
            "ffmpeg": str(ffmpeg_path) if ffmpeg_path else None,
            "ffprobe": str(ffprobe_path) if ffprobe_path else None,
            "python": str(python_path),
        },
    }


def render_from_reference_package(
    *,
    package_path: Path,
    generated_video: Path,
    output_dir: Path,
    ffmpeg: Path | None = None,
    ffprobe: Path | None = None,
    mouth_python: Path | None = None,
    preview: bool = False,
    approved_hard_speed_max: float | None = None,
    allow_pause_hold: bool = False,
    pause_hold_max_seconds: float = 2.0,
) -> dict[str, Any]:
    preflight = online_preflight(
        ffmpeg=ffmpeg,
        ffprobe=ffprobe,
        python_executable=mouth_python,
    )
    if not preflight["ok"]:
        raise TwoStageError("online preflight failed: " + "; ".join(preflight["errors"]))
    ffmpeg_path = Path(preflight["resolved"]["ffmpeg"])
    ffprobe_path = Path(preflight["resolved"]["ffprobe"])
    python_path = Path(preflight["resolved"]["python"])
    generated_video = generated_video.resolve()
    if not generated_video.is_file():
        raise TwoStageError(f"generated video does not exist: {generated_video}")

    workspace = BatchWorkspace(output_dir)
    workspace.ensure()
    with open_reference_package(package_path) as package:
        pair_id = package.benchmark.pair_id
        generated_id = _generated_pair_id(generated_video, pair_id)
        if not _same_numeric_id(pair_id, generated_id):
            raise TwoStageError(
                f"generated video ID {generated_id!r} does not match package ID {pair_id!r}"
            )
        pair_root = workspace.edit / "cache" / pair_id
        pair_root.mkdir(parents=True, exist_ok=True)
        mouth_raw = run_isolated_worker(
            python=python_path,
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
        activity_plot = workspace.edit / "activity_plots" / f"{pair_id}.png"
        write_activity_plot(mouth, activity_plot)

        config = VisualSolverConfig(
            hard_speed_max=approved_hard_speed_max or 1.25,
            allow_pause_hold=allow_pause_hold,
            pause_hold_max_seconds=pause_hold_max_seconds,
        )
        plan = solve_visual_timeline(
            package.benchmark,
            mouth,
            subtitle=package.subtitle,
            reference_audio=package.reference_audio,
            config=config,
        )
        plan_path = workspace.edit / "plans" / f"{pair_id}.json"
        atomic_write_json(plan_path, plan.to_dict())
        plan_report = verify_plan(plan)
        plan_report_path = workspace.edit / "verify" / f"{pair_id}.plan.json"
        atomic_write_json(plan_report_path, plan_report.to_dict())
        if not plan_report.passed:
            result = {
                "ok": False,
                "pair_id": pair_id,
                "status": plan.status,
                "output": None,
                "plan": str(plan_path.resolve()),
                "mouth_track": str(mouth_path.resolve()),
                "activity_plot": str(activity_plot.resolve()),
                "plan_verification": str(plan_report_path.resolve()),
                "reasons": plan.reasons,
                "preflight": preflight,
            }
            atomic_write_json(workspace.edit / f"online-{pair_id}.result.json", result)
            return result

        output = (
            workspace.edit / "previews" if preview else workspace.edit / "finals"
        ) / f"{pair_id}.mp4"
        render_plan(
            plan,
            generated_video=generated_video,
            reference_video=package.reference_audio,
            output_path=output,
            ffmpeg_path=ffmpeg_path,
            preview=preview,
        )
        media_report = verify_rendered_media(
            output,
            pair_id=pair_id,
            expected_duration=plan.output_duration,
            ffprobe_path=ffprobe_path,
            ffmpeg_path=ffmpeg_path,
        )
        media_report_path = workspace.edit / "verify" / f"{pair_id}.media.json"
        atomic_write_json(media_report_path, media_report.to_dict())
        if not media_report.passed:
            raise TwoStageError("rendered media failed verification")
        rendered_mouth_raw = run_isolated_worker(
            python=python_path,
            module="benchmark_av_sync.workers.mouth_track_worker",
            request={"video_path": str(output)},
            root=pair_root,
            stem="rendered-mouth",
        )
        rendered_mouth = analyze_mouth_frames(
            pair_id,
            rendered_mouth_raw["frames"],
            duration=float(rendered_mouth_raw["duration"]),
            fps=float(rendered_mouth_raw["fps"]),
        )
        rendered_mouth_path = workspace.edit / "mouth_tracks" / f"{pair_id}.rendered.json"
        atomic_write_json(rendered_mouth_path, rendered_mouth.to_dict())
        sync_report = verify_rendered_stage1_sync(plan, rendered_mouth)
        sync_report_path = workspace.edit / "verify" / f"{pair_id}.sync.json"
        atomic_write_json(sync_report_path, sync_report)
        if not sync_report["passed"]:
            result = {
                "ok": False,
                "pair_id": pair_id,
                "status": "fail",
                "output": str(output.resolve()),
                "plan": str(plan_path.resolve()),
                "mouth_track": str(mouth_path.resolve()),
                "rendered_mouth_track": str(rendered_mouth_path.resolve()),
                "activity_plot": str(activity_plot.resolve()),
                "plan_verification": str(plan_report_path.resolve()),
                "media_verification": str(media_report_path.resolve()),
                "sync_verification": str(sync_report_path.resolve()),
                "reasons": list(sync_report["errors"]),
                "preflight": preflight,
            }
            atomic_write_json(workspace.edit / f"online-{pair_id}.result.json", result)
            return result
        result = {
            "ok": True,
            "pair_id": pair_id,
            "status": plan.status,
            "output": str(output.resolve()),
            "output_sha256": sha256_file(output),
            "plan": str(plan_path.resolve()),
            "mouth_track": str(mouth_path.resolve()),
            "activity_plot": str(activity_plot.resolve()),
            "plan_verification": str(plan_report_path.resolve()),
            "media_verification": str(media_report_path.resolve()),
            "rendered_mouth_track": str(rendered_mouth_path.resolve()),
            "sync_verification": str(sync_report_path.resolve()),
            "review_required": plan.status == "review",
            "generated_audio_used": False,
            "preflight": preflight,
        }
        atomic_write_json(workspace.edit / f"online-{pair_id}.result.json", result)
        return result

