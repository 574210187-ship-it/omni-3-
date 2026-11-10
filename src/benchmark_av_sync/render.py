from __future__ import annotations

import subprocess
from pathlib import Path

from .contracts import AlignmentPlan


class RenderError(RuntimeError):
    pass


def _f(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def build_ffmpeg_command(
    plan: AlignmentPlan,
    *,
    generated_video: Path,
    reference_video: Path,
    output_path: Path,
    ffmpeg_path: Path,
    preview: bool = False,
) -> list[str]:
    if plan.status == "fail":
        raise RenderError("refusing to render a failed alignment plan")
    if not plan.video_segments or not plan.audio_chunks:
        raise RenderError("plan must contain video segments and audio chunks")

    filters: list[str] = []
    video_labels: list[str] = []
    for index, segment in enumerate(plan.video_segments):
        label = f"v{index}"
        if segment.reason.startswith("stage1 approved visual pause hold"):
            # The planner has reserved exactly one low-open source frame for a
            # measured reference-audio pause.  Loop only that frame for the
            # requested output duration; do not cut source audio or invent a
            # mouth shape.
            filters.append(
                f"[0:v]trim=start={_f(segment.source_start)}:end={_f(segment.source_end)},"
                "setpts=PTS-STARTPTS,loop=loop=-1:size=1:start=0,"
                f"trim=duration={_f(segment.output_duration)},setpts=PTS-STARTPTS,"
                f"fps={_f(plan.output_fps)},format=yuv420p[{label}]"
            )
            video_labels.append(f"[{label}]")
            continue
        interpolation = (
            f",minterpolate=fps={_f(plan.output_fps)}:mi_mode=mci:mc_mode=aobmc:vsbmc=1"
            if segment.speed < 1.0 - 1e-9
            else f",fps={_f(plan.output_fps)}"
        )
        filters.append(
            f"[0:v]trim=start={_f(segment.source_start)}:end={_f(segment.source_end)},"
            f"setpts=(PTS-STARTPTS)/{_f(segment.speed)}{interpolation},"
            f"format=yuv420p[{label}]"
        )
        video_labels.append(f"[{label}]")
    if len(video_labels) == 1:
        filters.append(f"{video_labels[0]}null[vout]")
    else:
        filters.append(f"{''.join(video_labels)}concat=n={len(video_labels)}:v=1:a=0[vout]")

    filters.append(
        f"[2:a]atrim=duration={_f(plan.output_duration)},asetpts=PTS-STARTPTS,"
        "aformat=sample_rates=48000:channel_layouts=stereo[abase]"
    )
    audio_labels: list[str] = ["[abase]"]
    for index, chunk in enumerate(plan.audio_chunks):
        duration = chunk.duration
        fade = min(chunk.fade_seconds, duration / 2)
        fade_out = max(0.0, duration - fade)
        delay_ms = round(chunk.output_start * 1000)
        label = f"a{index}"
        fades = ""
        if fade > 0:
            fades = (
                f"afade=t=in:st=0:d={_f(fade)},"
                f"afade=t=out:st={_f(fade_out)}:d={_f(fade)},"
            )
        filters.append(
            f"[1:a]atrim=start={_f(chunk.source_start)}:end={_f(chunk.source_end)},"
            "asetpts=PTS-STARTPTS,aformat=sample_rates=48000:channel_layouts=stereo,"
            f"{fades}"
            f"adelay={delay_ms}:all=1[{label}]"
        )
        audio_labels.append(f"[{label}]")
    filters.append(
        f"{''.join(audio_labels)}amix=inputs={len(audio_labels)}:duration=first:normalize=0[aout]"
    )

    # Formal segments must be encoded after time-warping, so make that single
    # necessary encode visually conservative. Preview remains intentionally
    # fast and lightweight.
    preset = "ultrafast" if preview else "slow"
    crf = "28" if preview else "15"
    return [
        str(ffmpeg_path),
        "-y",
        "-i",
        str(generated_video),
        "-i",
        str(reference_video),
        "-f",
        "lavfi",
        "-t",
        _f(plan.output_duration),
        "-i",
        "anullsrc=r=48000:cl=stereo",
        "-filter_complex",
        ";".join(filters),
        "-map",
        "[vout]",
        "-map",
        "[aout]",
        "-t",
        _f(plan.output_duration),
        "-c:v",
        "libx264",
        "-preset",
        preset,
        "-crf",
        crf,
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        "-ar",
        "48000",
        "-movflags",
        "+faststart",
        str(output_path),
    ]


def render_plan(
    plan: AlignmentPlan,
    *,
    generated_video: Path,
    reference_video: Path,
    output_path: Path,
    ffmpeg_path: Path,
    preview: bool = False,
) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_ffmpeg_command(
        plan,
        generated_video=generated_video,
        reference_video=reference_video,
        output_path=output_path,
        ffmpeg_path=ffmpeg_path,
        preview=preview,
    )
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise RenderError(completed.stderr[-4000:] or "ffmpeg render failed")
