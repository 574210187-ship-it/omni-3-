from __future__ import annotations

import os
import subprocess
from math import ceil
from pathlib import Path
from statistics import median

from .contracts import AlignmentPlan, VerificationReport
from .probe import ProbeError, probe_media


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, ceil(percentile * len(ordered)) - 1))
    return ordered[index]


def verify_plan(plan: AlignmentPlan, *, tolerance: float = 1e-6) -> VerificationReport:
    errors: list[str] = []
    warnings: list[str] = list(plan.reasons)
    speed_exception_authorized = bool(plan.metadata.get("speed_exception_authorized", False))
    pause_hold_authorized = bool(plan.metadata.get("pause_hold_authorized", False))
    approved_hard_speed_max = 1.25
    if speed_exception_authorized:
        approved_hard_speed_max = max(
            1.25, float(plan.metadata.get("approved_hard_speed_max", 1.25))
        )
        warnings.append(
            f"user-approved local speed exception up to {approved_hard_speed_max:.3f}x"
        )
    segments = plan.video_segments
    if not segments:
        errors.append("plan has no video segments")
    else:
        if abs(segments[0].source_start - plan.trim_start) > tolerance:
            errors.append("video coverage does not start at trim_start")
        if abs(segments[0].output_start) > tolerance:
            errors.append("first output segment does not start at zero")
        for previous, current in zip(segments, segments[1:]):
            if abs(previous.source_end - current.source_start) > tolerance:
                errors.append("generated middle coverage contains a gap or overlap")
            if abs(previous.output_end - current.output_start) > tolerance:
                errors.append("output video timeline contains a gap or overlap")
        if abs(segments[-1].source_end - plan.trim_end) > tolerance:
            errors.append("video coverage does not end at trim_end")
        if abs(segments[-1].output_end - plan.output_duration) > tolerance:
            errors.append("output_duration does not match segment timeline")
        pause_holds = [
            segment
            for segment in segments
            if segment.reason.startswith("stage1 approved visual pause hold")
        ]
        if pause_holds and not pause_hold_authorized:
            errors.append("visual pause hold is present without user authorization")
        timed_segments = [segment for segment in segments if segment not in pause_holds]
        if any(segment.speed < 0.85 - tolerance for segment in timed_segments):
            errors.append("video speed is below hard limit 0.85x")
        if any(segment.speed > approved_hard_speed_max + tolerance for segment in timed_segments):
            errors.append(
                f"video speed exceeds approved hard limit {approved_hard_speed_max:.3f}x"
            )

    previous_end = 0.0
    for chunk in plan.audio_chunks:
        if chunk.source_end > plan.reference_duration + tolerance:
            errors.append("audio chunk exceeds reference duration")
        if chunk.output_start + tolerance < previous_end:
            errors.append("audio chunks overlap or move backwards")
        if chunk.output_end > plan.output_duration + 0.05:
            errors.append("audio chunk ends after output video")
        previous_end = chunk.output_end

    absolute = [abs(value) for value in plan.residuals_ms]
    metrics = {
        "source_coverage_seconds": sum(x.source_duration for x in segments),
        "expected_source_coverage_seconds": plan.trim_end - plan.trim_start,
        "max_speed": max((x.speed for x in segments), default=None),
        "min_speed": min((x.speed for x in segments), default=None),
        "approved_hard_speed_max": approved_hard_speed_max,
        "speed_exception_authorized": speed_exception_authorized,
        "pause_hold_authorized": pause_hold_authorized,
        "pause_hold_count": len(pause_holds) if segments else 0,
        "audio_chunk_count": len(plan.audio_chunks),
        "residual_p50_ms": median(absolute) if absolute else None,
        "residual_p95_ms": _percentile(absolute, 0.95),
        "residual_max_ms": max(absolute) if absolute else None,
        "plan_status": plan.status,
    }
    if plan.strategy == "stage1-token-pause-sync":
        anchors = plan.metadata.get("sync_anchors")
        if not isinstance(anchors, list) or not anchors:
            errors.append("token/pause sync plan has no anchors")
        if not plan.residuals_ms:
            errors.append("token/pause sync plan has no residuals")
        elif max(absolute) > 200.0 + tolerance:
            errors.append("token/pause sync residual exceeds 200ms")
        if plan.metadata.get("sync_contract") != "stage1-v3.2-token-pause":
            errors.append("token/pause sync contract marker is missing")
    if plan.status == "fail":
        errors.append("solver marked plan as fail")
    return VerificationReport(plan.pair_id, not errors, errors, warnings, metrics)


def verify_rendered_media(
    path: Path,
    *,
    pair_id: str,
    expected_duration: float,
    ffprobe_path: Path,
    ffmpeg_path: Path,
    duration_tolerance: float = 0.12,
) -> VerificationReport:
    errors: list[str] = []
    warnings: list[str] = []
    metrics: dict[str, object] = {
        "path": str(path.resolve()),
        "expected_duration": expected_duration,
    }
    if not path.is_file() or path.stat().st_size == 0:
        return VerificationReport(pair_id, False, ["rendered file is missing or empty"], [], metrics)
    try:
        media = probe_media(path, ffprobe_path)
    except (ProbeError, OSError, ValueError) as exc:
        return VerificationReport(pair_id, False, [f"ffprobe failed: {exc}"], [], metrics)
    video_streams = [x for x in media["streams"] if x.get("codec_type") == "video"]
    audio_streams = [x for x in media["streams"] if x.get("codec_type") == "audio"]
    if len(video_streams) != 1:
        errors.append(f"expected one video stream, found {len(video_streams)}")
    if len(audio_streams) != 1:
        errors.append(f"expected one audio stream, found {len(audio_streams)}")
    duration = media.get("duration")
    metrics.update(
        actual_duration=duration,
        duration_delta=(abs(duration - expected_duration) if duration is not None else None),
        video_streams=len(video_streams),
        audio_streams=len(audio_streams),
        size=media.get("size"),
    )
    if duration is None:
        errors.append("rendered duration is unavailable")
    elif abs(duration - expected_duration) > duration_tolerance:
        errors.append(
            f"rendered duration differs by {abs(duration - expected_duration):.3f}s"
        )
    if audio_streams and audio_streams[0].get("sample_rate") != 48000:
        errors.append("rendered audio sample rate is not 48000 Hz")

    decode = subprocess.run(
        [
            str(ffmpeg_path),
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-f",
            "null",
            os.devnull,
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    metrics["decode_returncode"] = decode.returncode
    if decode.returncode != 0:
        errors.append(f"full decode failed: {(decode.stderr or '').strip()[-500:]}")
    return VerificationReport(pair_id, not errors, errors, warnings, metrics)
