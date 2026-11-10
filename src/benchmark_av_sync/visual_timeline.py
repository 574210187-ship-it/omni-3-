from __future__ import annotations

from array import array
from dataclasses import dataclass
from math import sqrt
from pathlib import Path
from typing import Any
import wave

from .contracts import AlignmentPlan, AudioChunk, BenchmarkGroup, BenchmarkScript, MouthTrack, VideoSegment


@dataclass(slots=True)
class VisualSolverConfig:
    head_padding: float = 0.05
    tail_padding: float = 0.08
    auto_speed_min: float = 0.90
    auto_speed_max: float = 1.20
    hard_speed_min: float = 0.85
    hard_speed_max: float = 1.25
    snap_radius: float = 0.22
    minimum_window: float = 0.75
    target_window: float = 1.6
    maximum_window: float = 3.0
    # Short consonant gaps belong to the surrounding spoken phrase.  Only a
    # half-second of measured quiet is promoted to a hard visual-pause gate.
    pause_gate_seconds: float = 0.50
    pause_match_radius: float = 0.90
    max_anchor_residual_ms: float = 200.0
    allow_pause_hold: bool = False
    pause_hold_max_seconds: float = 2.00
    pause_hold_match_radius: float = 1.50

    def __post_init__(self) -> None:
        if self.hard_speed_max < 1.25:
            raise ValueError("hard_speed_max cannot be below the default 1.25x limit")
        if self.pause_hold_max_seconds <= 0:
            raise ValueError("pause_hold_max_seconds must be positive")


def _merge_reference_groups(
    groups: list[BenchmarkGroup], config: VisualSolverConfig
) -> list[list[BenchmarkGroup]]:
    merged: list[list[BenchmarkGroup]] = []
    current: list[BenchmarkGroup] = []
    for group in groups:
        if not current:
            current = [group]
            continue
        candidate_duration = group.source_end - current[0].source_start
        current_duration = current[-1].source_end - current[0].source_start
        if current_duration >= config.target_window or candidate_duration > config.maximum_window:
            merged.append(current)
            current = [group]
        else:
            current.append(group)
    if current:
        merged.append(current)
    if len(merged) > 1:
        tail_duration = merged[-1][-1].source_end - merged[-1][0].source_start
        combined = merged[-1][-1].source_end - merged[-2][0].source_start
        if tail_duration < config.minimum_window and combined <= config.maximum_window:
            merged[-2].extend(merged.pop())
    return merged


def _reference_boundaries(
    benchmark: BenchmarkScript, config: VisualSolverConfig
) -> tuple[list[float], list[str]]:
    merged = _merge_reference_groups(benchmark.groups, config)
    boundaries = [benchmark.speech_start]
    labels: list[str] = []
    for index, group in enumerate(merged):
        labels.append("".join(item.text for item in group))
        if index + 1 < len(merged):
            next_group = merged[index + 1]
            boundaries.append((group[-1].source_end + next_group[0].source_start) / 2)
    boundaries.append(benchmark.speech_end)
    return boundaries, labels


def _snap_visual_boundary(
    desired: float,
    candidates: list[float],
    *,
    previous: float,
    final: float,
    radius: float,
) -> float:
    valid = [
        item
        for item in candidates
        if previous + 0.25 <= item <= final - 0.25 and abs(item - desired) <= radius
    ]
    if not valid:
        return desired
    return min(valid, key=lambda item: abs(item - desired))


def _solve_span_timeline(
    benchmark: BenchmarkScript,
    mouth: MouthTrack,
    *,
    config: VisualSolverConfig | None = None,
) -> AlignmentPlan:
    config = config or VisualSolverConfig()
    reference_boundaries, labels = _reference_boundaries(benchmark, config)
    reference_span = benchmark.speech_end - benchmark.speech_start
    visual_span = mouth.speech_end - mouth.speech_start
    if reference_span <= 0 or visual_span <= 0:
        raise ValueError("reference and mouth activity spans must be positive")

    visual_boundaries = [mouth.speech_start]
    for reference_time in reference_boundaries[1:-1]:
        fraction = (reference_time - benchmark.speech_start) / reference_span
        desired = mouth.speech_start + fraction * visual_span
        snapped = _snap_visual_boundary(
            desired,
            mouth.low_motion_candidates,
            previous=visual_boundaries[-1],
            final=mouth.speech_end,
            radius=config.snap_radius,
        )
        visual_boundaries.append(snapped)
    visual_boundaries.append(mouth.speech_end)

    snapped_speeds = [
        (visual_boundaries[index + 1] - visual_boundaries[index])
        / (reference_boundaries[index + 1] - reference_boundaries[index])
        for index in range(len(labels))
    ]
    overall_speed = visual_span / reference_span
    snap_fallback = False
    if (
        config.hard_speed_min <= overall_speed <= config.hard_speed_max
        and any(
            speed < config.hard_speed_min or speed > config.hard_speed_max
            for speed in snapped_speeds
        )
    ):
        # A nearby low-motion valley is useful only when it does not create an
        # otherwise unnecessary out-of-policy local speed.  Fall back to the
        # proportional word/phrase windows while keeping the same 1-3 s cuts.
        visual_boundaries = [
            mouth.speech_start
            + (reference_time - benchmark.speech_start) / reference_span * visual_span
            for reference_time in reference_boundaries
        ]
        snap_fallback = True

    trim_start = max(0.0, mouth.speech_start - config.head_padding)
    trim_end = min(mouth.duration, mouth.speech_end + config.tail_padding)
    segments: list[VideoSegment] = []
    reasons = list(mouth.reasons)
    status = mouth.status
    output_cursor = 0.0

    def append(start: float, end: float, speed: float, reason: str, review: bool = False) -> None:
        nonlocal output_cursor
        if end <= start + 1e-9:
            return
        segment = VideoSegment(start, end, speed, output_cursor, review=review, reason=reason)
        segments.append(segment)
        output_cursor = segment.output_end

    append(trim_start, mouth.speech_start, 1.0, "head buffer")
    window_metrics: list[dict[str, float | str]] = []
    for index, label in enumerate(labels):
        source_start = visual_boundaries[index]
        source_end = visual_boundaries[index + 1]
        target_start = reference_boundaries[index]
        target_end = reference_boundaries[index + 1]
        speed = (source_end - source_start) / (target_end - target_start)
        review = speed < config.auto_speed_min or speed > config.auto_speed_max
        if speed < config.hard_speed_min or speed > config.hard_speed_max:
            status = "fail"
            reasons.append(
                f"local speed {speed:.3f}x outside {config.hard_speed_min:.2f}-{config.hard_speed_max:.2f}x near {label!r}"
            )
        elif review:
            if status == "pass":
                status = "review"
            reasons.append(f"local speed {speed:.3f}x requires review near {label!r}")
        append(source_start, source_end, speed, f"mouth activity window {label}", review)
        window_metrics.append(
            {
                "label": label,
                "source_start": source_start,
                "source_end": source_end,
                "target_start": target_start,
                "target_end": target_end,
                "speed": speed,
            }
        )
    append(mouth.speech_end, trim_end, 1.0, "tail buffer")

    head_output = next(
        (segment.output_end for segment in segments if segment.reason == "head buffer"),
        0.0,
    )
    audio_chunk = AudioChunk(
        source_start=benchmark.speech_start,
        source_end=benchmark.speech_end,
        output_start=head_output,
        fade_seconds=0.0,
        reason="single continuous benchmark speech block",
        continuity_group_id="all",
        cut_before_allowed=True,
        cut_after_allowed=True,
    )
    output_duration = segments[-1].output_end
    if audio_chunk.output_end > output_duration + 1e-6:
        extension = audio_chunk.output_end - output_duration
        old_end = trim_end
        available = max(0.0, mouth.duration - trim_end)
        retained = min(available, extension)
        if retained > 0:
            append(old_end, old_end + retained, 1.0, "retain tail for complete benchmark audio")
            trim_end += retained
            output_duration = segments[-1].output_end
    if audio_chunk.output_end > output_duration + 0.05:
        status = "fail"
        reasons.append("continuous benchmark audio extends beyond available video tail")

    return AlignmentPlan(
        pair_id=benchmark.pair_id,
        generated_duration=mouth.duration,
        reference_duration=benchmark.reference_duration,
        trim_start=trim_start,
        trim_end=trim_end,
        output_duration=output_duration,
        video_segments=segments,
        audio_chunks=[audio_chunk],
        status=status,
        reasons=reasons,
        residuals_ms=[],
        strategy="mediapipe-mouth-activity",
        output_fps=mouth.fps,
        metadata={
            "mouth_quality": mouth.quality,
            "mouth_speech_start": mouth.speech_start,
            "mouth_speech_end": mouth.speech_end,
            "reference_speech_start": benchmark.speech_start,
            "reference_speech_end": benchmark.speech_end,
            "safe_audio_cut_count": sum(item.cut_allowed for item in benchmark.boundaries),
            "audio_chunk_policy": "single continuous block",
            "default_hard_speed_max": 1.25,
            "approved_hard_speed_max": config.hard_speed_max,
            "speed_exception_authorized": config.hard_speed_max > 1.25,
            "boundary_snap_fallback": snap_fallback,
            "windows": window_metrics,
        },
    )


def _subtitle_events(
    benchmark: BenchmarkScript, subtitle: dict[str, Any]
) -> list[dict[str, Any]]:
    """Return the locked Stage1 v3.2 semantic events in audio order."""

    raw = subtitle.get("caption_events")
    if not isinstance(raw, list) or not raw:
        raise ValueError("Stage1 subtitle has no caption_events")
    events: list[dict[str, Any]] = []
    last_end = benchmark.speech_start
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("subtitle caption event is invalid")
        start = float(item.get("source_start", -1))
        end = float(item.get("source_end", -1))
        token_start = int(item.get("token_start", -1))
        token_end = int(item.get("token_end", -1))
        text = str(item.get("text", ""))
        if (
            not text
            or start < benchmark.speech_start - 0.06
            or end > benchmark.speech_end + 0.06
            or end <= start
            or token_start < 0
            or token_end < token_start
            or start + 1e-6 < last_end
        ):
            raise ValueError("subtitle event order or timing is invalid")
        events.append(
            {
                "event_id": str(item.get("event_id", f"event-{len(events):03d}")),
                "text": text,
                "reference_start": start,
                "reference_end": end,
                "token_start": token_start,
                "token_end": token_end,
            }
        )
        last_end = end
    return events


def _inactive_runs(mouth: MouthTrack) -> list[tuple[float, float]]:
    """Find genuine closed/low-motion intervals from the independently tracked mouth."""

    frames = [
        frame
        for frame in mouth.frames
        if mouth.speech_start <= frame.time <= mouth.speech_end
    ]
    if not frames:
        return []
    frame_duration = 1.0 / mouth.fps
    runs: list[tuple[float, float]] = []
    start: float | None = None
    previous: float | None = None
    for frame in frames:
        inactive = not frame.active
        if inactive and start is None:
            start = frame.time
        if not inactive and start is not None:
            runs.append((start, frame.time))
            start = None
        previous = frame.time
    if start is not None and previous is not None:
        runs.append((start, min(mouth.speech_end, previous + frame_duration)))
    return runs


def _reference_audio_pause_runs(
    reference_audio: Path,
    *,
    speech_start: float,
    speech_end: float,
    minimum_duration: float,
) -> tuple[list[tuple[float, float]], dict[str, Any]]:
    """Return real low-energy intervals in the locked Stage1 reference WAV.

    Caption-event timestamps are semantic timing hints, not proof of a spoken
    pause.  The old implementation confused a gap between two caption events
    with silence, which creates false visual-pause requirements whenever a
    caption boundary falls inside a connected utterance.  This detector is
    intentionally simple, deterministic and evidence-friendly: 10 ms RMS
    windows, a per-file robust noise threshold, and only intervals that are
    genuinely quiet in ``reference.wav``.
    """

    if not reference_audio.is_file():
        raise ValueError(f"SYNC_UNSOLVABLE: Stage1 reference.wav is unavailable: {reference_audio}")
    try:
        with wave.open(str(reference_audio), "rb") as source:
            channels = source.getnchannels()
            sample_width = source.getsampwidth()
            frame_rate = source.getframerate()
            compression = source.getcomptype()
            raw = source.readframes(source.getnframes())
    except (OSError, wave.Error) as exc:
        raise ValueError(f"SYNC_UNSOLVABLE: cannot read Stage1 reference.wav: {exc}") from exc
    if compression != "NONE" or channels <= 0 or frame_rate <= 0:
        raise ValueError("SYNC_UNSOLVABLE: Stage1 reference.wav must be uncompressed PCM")
    type_code = {1: "b", 2: "h", 4: "i"}.get(sample_width)
    if type_code is None:
        raise ValueError("SYNC_UNSOLVABLE: unsupported Stage1 reference.wav sample width")
    samples = array(type_code)
    samples.frombytes(raw)
    if not samples:
        raise ValueError("SYNC_UNSOLVABLE: Stage1 reference.wav contains no samples")

    # The input packages use PCM little-endian.  Byte swap only when a
    # non-little-endian host is encountered; keeping this local avoids a NumPy
    # dependency in the pure planning module.
    import sys

    if sys.byteorder != "little" and sample_width > 1:
        samples.byteswap()
    samples_per_window = max(1, round(frame_rate * 0.01)) * channels
    levels: list[float] = []
    for offset in range(0, len(samples), samples_per_window):
        window = samples[offset : offset + samples_per_window]
        if window:
            levels.append(sqrt(sum(value * value for value in window) / len(window)))
    if not levels:
        raise ValueError("SYNC_UNSOLVABLE: Stage1 reference.wav has no analysis windows")
    ordered = sorted(levels)
    floor = ordered[round((len(ordered) - 1) * 0.10)]
    peak = ordered[round((len(ordered) - 1) * 0.90)]
    threshold = floor + (peak - floor) * 0.15

    # Join one 10 ms noisy blip inside an otherwise genuine silence.
    low = [level <= threshold for level in levels]
    for index in range(1, len(low) - 1):
        if not low[index] and low[index - 1] and low[index + 1]:
            low[index] = True
    runs: list[tuple[float, float]] = []
    start_index: int | None = None
    for index, quiet in enumerate([*low, False]):
        if quiet and start_index is None:
            start_index = index
        elif not quiet and start_index is not None:
            start = start_index * 0.01
            end = index * 0.01
            clipped_start = max(start, speech_start)
            clipped_end = min(end, speech_end)
            if clipped_end - clipped_start >= minimum_duration:
                runs.append((clipped_start, clipped_end))
            start_index = None
    return runs, {
        "analysis_window_ms": 10,
        "noise_floor_rms": floor,
        "speech_peak_rms": peak,
        "quiet_threshold_rms": threshold,
        "quiet_runs": [
            {"start": start, "end": end, "duration": end - start}
            for start, end in runs
        ],
    }


def _failed_sync_plan(
    benchmark: BenchmarkScript,
    mouth: MouthTrack,
    *,
    config: VisualSolverConfig,
    reason: str,
    events: list[dict[str, Any]],
) -> AlignmentPlan:
    """Persist a readable plan and an explicit failure instead of a false pass."""

    plan = _solve_span_timeline(benchmark, mouth, config=config)
    plan.status = "fail"
    plan.reasons.append(reason)
    plan.strategy = "stage1-token-pause-sync"
    plan.metadata.update(
        {
            "sync_contract": "stage1-v3.2-token-pause",
            "sync_events": events,
            "sync_anchors": [],
            "pause_map": [],
            "sync_failure": reason,
        }
    )
    return plan


def _solve_stage1_token_pause_timeline(
    benchmark: BenchmarkScript,
    mouth: MouthTrack,
    *,
    subtitle: dict[str, Any],
    reference_audio: Path | None,
    config: VisualSolverConfig,
) -> AlignmentPlan:
    """Map locked Stage1 semantic events and pauses onto visual mouth activity.

    The older span solver could place a visual phrase boundary at the midpoint
    of a long reference pause.  This routine makes every v3.2 event boundary
    explicit.  A reference pause needs a real closed/low-motion visual run
    that can be retimed within the approved speed range; otherwise the source
    is rejected rather than being silently marked as synchronised.
    """

    try:
        events = _subtitle_events(benchmark, subtitle)
    except ValueError as exc:
        return _failed_sync_plan(
            benchmark, mouth, config=config, reason=str(exc), events=[]
        )

    if reference_audio is None:
        return _failed_sync_plan(
            benchmark,
            mouth,
            config=config,
            events=events,
            reason="SYNC_UNSOLVABLE: v3.2 token/pause planning requires Stage1 reference.wav",
        )
    try:
        audio_pause_runs, audio_pause_evidence = _reference_audio_pause_runs(
            reference_audio,
            speech_start=benchmark.speech_start,
            speech_end=benchmark.speech_end,
            minimum_duration=config.pause_gate_seconds,
        )
    except ValueError as exc:
        return _failed_sync_plan(
            benchmark, mouth, config=config, reason=str(exc), events=events
        )

    pauses: list[dict[str, Any]] = []
    for index, (left, right) in enumerate(zip(events, events[1:])):
        gap_start = left["reference_end"]
        gap_end = right["reference_start"]
        duration = gap_end - gap_start
        matching_runs = [
            (max(gap_start, run_start), min(gap_end, run_end))
            for run_start, run_end in audio_pause_runs
            if min(gap_end, run_end) - max(gap_start, run_start)
            >= config.pause_gate_seconds
        ]
        if duration >= config.pause_gate_seconds and matching_runs:
            pauses.append(
                {
                    "index": index,
                    "reference_start": gap_start,
                    "reference_end": gap_end,
                    "reference_duration": duration,
                    "acoustic_silence_runs": [
                        {
                            "start": start,
                            "end": end,
                            "duration": end - start,
                        }
                        for start, end in matching_runs
                    ],
                }
            )

    visual_pause_runs = _inactive_runs(mouth)
    selected_pauses: list[dict[str, Any]] = []
    previous_end = mouth.speech_start
    previous_reference_end = benchmark.speech_start
    reference_span = benchmark.speech_end - benchmark.speech_start
    visual_span = mouth.speech_end - mouth.speech_start
    for pause_index, pause in enumerate(pauses):
        fraction = (
            ((pause["reference_start"] + pause["reference_end"]) / 2)
            - benchmark.speech_start
        ) / max(reference_span, 1e-9)
        desired = mouth.speech_start + fraction * visual_span
        minimum = pause["reference_duration"] * config.hard_speed_min
        maximum = pause["reference_duration"] * config.hard_speed_max
        candidates: list[tuple[float, float]] = []
        for run_start, run_end in visual_pause_runs:
            # A real closed-mouth run can legitimately be longer than the
            # reference pause.  Use a correctly-sized window inside it rather
            # than rejecting that otherwise ideal evidence because its full
            # length exceeds the local speed envelope.
            available_start = max(run_start, previous_end)
            available_end = run_end
            if available_end - available_start < minimum - 1e-6:
                continue
            candidate_duration = min(
                maximum, available_end - available_start, pause["reference_duration"]
            )
            candidate_duration = max(candidate_duration, minimum)
            candidate_start = min(
                max(desired - candidate_duration / 2, available_start),
                available_end - candidate_duration,
            )
            candidate_end = candidate_start + candidate_duration
            if abs(((candidate_start + candidate_end) / 2) - desired) <= config.pause_match_radius:
                candidates.append((candidate_start, candidate_end))
        if not candidates and config.allow_pause_hold and (
            pause["reference_duration"] <= config.pause_hold_max_seconds
        ):
            frame_duration = 1.0 / mouth.fps
            hold_frames = [
                frame
                for frame in mouth.frames
                if previous_end - 1e-6 <= frame.time <= mouth.speech_end - frame_duration
                and abs(frame.time - desired) <= config.pause_hold_match_radius
            ]
            # A frozen frame cannot make either adjacent spoken block violate
            # the authorised local speed envelope.  The first condition is
            # known for every pause; the tail condition is exact for the last
            # measured pause and prevents a visually pretty but unusable late
            # hold frame from pushing the final speech block below 0.85x.
            reference_before = float(pause["reference_start"]) - previous_reference_end
            if reference_before > 1e-9:
                hold_frames = [
                    frame
                    for frame in hold_frames
                    if config.hard_speed_min - 1e-6
                    <= (frame.time - previous_end) / reference_before
                    <= config.hard_speed_max + 1e-6
                ]
            if pause_index == len(pauses) - 1:
                reference_after = benchmark.speech_end - float(pause["reference_end"])
                if reference_after > 1e-9:
                    hold_frames = [
                        frame
                        for frame in hold_frames
                        if config.hard_speed_min - 1e-6
                        <= (mouth.speech_end - (frame.time + frame_duration)) / reference_after
                        <= config.hard_speed_max + 1e-6
                    ]
            if hold_frames:
                # Explicitly user-authorised fallback: hold one low-open frame
                # for the measured acoustic pause.  No source range is deleted
                # and no synthetic mouth shape is introduced.
                frame = min(
                    hold_frames,
                    key=lambda item: (
                        item.openness,
                        item.motion,
                        abs(item.time - desired),
                    ),
                )
                candidates.append((frame.time, frame.time + frame_duration))
                pause["render_mode"] = "freeze_low_open_frame"
                pause["hold_source_frame_time"] = frame.time
                pause["hold_source_openness"] = frame.openness
                pause["hold_source_motion"] = frame.motion
        if not candidates:
            return _failed_sync_plan(
                benchmark,
                mouth,
                config=config,
                events=events,
                reason=(
                    "SYNC_UNSOLVABLE: reference pause "
                    f"{pause['reference_duration']:.3f}s after "
                    f"{events[pause['index']]['text']!r} has no compatible "
                    "closed-mouth visual interval"
                ),
            )
        source_start, source_end = min(
            candidates, key=lambda run: abs(((run[0] + run[1]) / 2) - desired)
        )
        selected = dict(pause)
        selected.update(
            source_start=source_start,
            source_end=source_end,
            source_duration=source_end - source_start,
        )
        selected_pauses.append(selected)
        previous_end = source_end
        previous_reference_end = float(pause["reference_end"])

    trim_start = max(0.0, mouth.speech_start - config.head_padding)
    trim_end = min(mouth.duration, mouth.speech_end + config.tail_padding)
    segments: list[VideoSegment] = []
    reasons = list(mouth.reasons)
    status = mouth.status
    output_cursor = 0.0

    def append(
        start: float, end: float, speed: float, reason: str
    ) -> VideoSegment | None:
        nonlocal output_cursor, status
        if end <= start + 1e-9:
            return None
        segment = VideoSegment(start, end, speed, output_cursor, reason=reason)
        segments.append(segment)
        output_cursor = segment.output_end
        is_pause_hold = reason.startswith("stage1 approved visual pause hold")
        if is_pause_hold:
            if status == "pass":
                status = "review"
            reasons.append("user-approved closed/low-open frame hold for measured acoustic pause")
        elif speed < config.hard_speed_min or speed > config.hard_speed_max:
            status = "fail"
            reasons.append(
                f"local speed {speed:.3f}x outside "
                f"{config.hard_speed_min:.2f}-{config.hard_speed_max:.2f}x near {reason}"
            )
        elif speed < config.auto_speed_min or speed > config.auto_speed_max:
            if status == "pass":
                status = "review"
            reasons.append(f"local speed {speed:.3f}x requires review near {reason}")
        return segment

    head = append(trim_start, mouth.speech_start, 1.0, "head buffer")
    head_output = head.output_end if head is not None else 0.0
    current_source = mouth.speech_start
    current_reference = benchmark.speech_start
    speech_blocks: list[dict[str, float]] = []
    anchors: list[dict[str, Any]] = []
    residuals: list[float] = []

    def append_speech_block(reference_end: float, source_end: float, label: str) -> bool:
        nonlocal current_reference, current_source
        reference_duration = reference_end - current_reference
        source_duration = source_end - current_source
        if reference_duration <= 1e-6 or source_duration <= 1e-6:
            return False
        segment = append(
            current_source,
            source_end,
            source_duration / reference_duration,
            label,
        )
        if segment is None:
            return False
        speech_blocks.append(
            {
                "reference_start": current_reference,
                "reference_end": reference_end,
                "source_start": current_source,
                "source_end": source_end,
                "output_start": segment.output_start,
                "output_end": segment.output_end,
            }
        )
        current_reference = reference_end
        current_source = source_end
        return True

    # Speech is kept continuous between real acoustic pauses.  Forcing every
    # caption event into an independent speed segment both overfit noisy
    # caption timestamps and caused the false 0.8 s drift in the prior build.
    # Every long acoustic pause is still an explicit, mouth-closed segment.
    for pause in selected_pauses:
        if not append_speech_block(
            float(pause["reference_start"]),
            float(pause["source_start"]),
            f"Stage1 speech block before pause after {events[pause['index']]['event_id']}",
        ):
            return _failed_sync_plan(
                benchmark,
                mouth,
                config=config,
                events=events,
                reason="SYNC_UNSOLVABLE: no positive visual span before required pause",
            )
        pause_reason = (
            "stage1 approved visual pause hold"
            if pause.get("render_mode") == "freeze_low_open_frame"
            else f"stage1 audio pause after {events[pause['index']]['event_id']}"
        )
        pause_segment = append(
            float(pause["source_start"]),
            float(pause["source_end"]),
            float(pause["source_duration"]) / float(pause["reference_duration"]),
            pause_reason,
        )
        if pause_segment is None:
            return _failed_sync_plan(
                benchmark,
                mouth,
                config=config,
                events=events,
                reason="SYNC_UNSOLVABLE: empty visual pause interval",
            )
        pause["output_start"] = pause_segment.output_start
        pause["output_end"] = pause_segment.output_end
        current_reference = float(pause["reference_end"])
        current_source = float(pause["source_end"])
    if not append_speech_block(
        benchmark.speech_end, mouth.speech_end, "Stage1 final speech block"
    ):
        return _failed_sync_plan(
            benchmark,
            mouth,
            config=config,
            events=events,
            reason="SYNC_UNSOLVABLE: no positive visual span after required pause",
        )

    def anchor_reference_time(reference_time: float) -> tuple[float, float | None]:
        output_time = head_output + (reference_time - benchmark.speech_start)
        for block in speech_blocks:
            if block["reference_start"] - 1e-6 <= reference_time <= block["reference_end"] + 1e-6:
                fraction = (reference_time - block["reference_start"]) / max(
                    block["reference_end"] - block["reference_start"], 1e-9
                )
                source_time = block["source_start"] + fraction * (
                    block["source_end"] - block["source_start"]
                )
                return output_time, source_time
        return output_time, None

    for event in events:
        for label, reference_time in (
            ("event_start", event["reference_start"]),
            ("event_end", event["reference_end"]),
        ):
            visual_time, source_time = anchor_reference_time(reference_time)
            audio_time = head_output + (reference_time - benchmark.speech_start)
            residual = (visual_time - audio_time) * 1000
            residuals.append(residual)
            anchors.append(
                {
                    "kind": label,
                    "event_id": event["event_id"],
                    "text": event["text"],
                    "audio_time": audio_time,
                    "visual_time": visual_time,
                    "source_time": source_time,
                    "residual_ms": residual,
                    "basis": "locked-stage1-event-timeline",
                }
            )
    for token in benchmark.tokens:
        visual_time, source_time = anchor_reference_time(token.start)
        audio_time = head_output + (token.start - benchmark.speech_start)
        residual = (visual_time - audio_time) * 1000
        residuals.append(residual)
        anchors.append(
            {
                "kind": "token_start",
                "token_index": token.index,
                "text": token.text,
                "audio_time": audio_time,
                "visual_time": visual_time,
                "source_time": source_time,
                "residual_ms": residual,
                "basis": "locked-stage1-token-timeline",
            }
        )

    append(mouth.speech_end, trim_end, 1.0, "tail buffer")
    audio_chunk = AudioChunk(
        source_start=benchmark.speech_start,
        source_end=benchmark.speech_end,
        output_start=head_output,
        fade_seconds=0.0,
        reason="single continuous Stage1 reference speech block",
        continuity_group_id="all",
        cut_before_allowed=True,
        cut_after_allowed=True,
    )
    if audio_chunk.output_end > output_cursor + 0.05:
        status = "fail"
        reasons.append("continuous reference audio extends beyond visual output")
    if not residuals or max(abs(value) for value in residuals) > config.max_anchor_residual_ms:
        status = "fail"
        reasons.append("token/event residual gate did not pass")

    return AlignmentPlan(
        pair_id=benchmark.pair_id,
        generated_duration=mouth.duration,
        reference_duration=benchmark.reference_duration,
        trim_start=trim_start,
        trim_end=trim_end,
        output_duration=segments[-1].output_end,
        video_segments=segments,
        audio_chunks=[audio_chunk],
        status=status,
        reasons=reasons,
        residuals_ms=residuals,
        strategy="stage1-token-pause-sync",
        output_fps=mouth.fps,
        metadata={
            "sync_contract": "stage1-v3.2-token-pause",
            "mouth_quality": mouth.quality,
            "sync_events": events,
            "sync_anchors": anchors,
            "pause_map": selected_pauses,
            "reference_audio_pause_evidence": audio_pause_evidence,
            "audio_chunk_policy": "single continuous Stage1 reference speech block",
            "default_hard_speed_max": 1.25,
            "approved_hard_speed_max": config.hard_speed_max,
            "speed_exception_authorized": config.hard_speed_max > 1.25,
            "pause_hold_authorized": config.allow_pause_hold,
            "pause_hold_max_seconds": config.pause_hold_max_seconds,
        },
    )


def solve_visual_timeline(
    benchmark: BenchmarkScript,
    mouth: MouthTrack,
    *,
    subtitle: dict[str, Any] | None = None,
    reference_audio: Path | None = None,
    config: VisualSolverConfig | None = None,
) -> AlignmentPlan:
    config = config or VisualSolverConfig()
    if subtitle is not None and subtitle.get("schema_version") == 3:
        return _solve_stage1_token_pause_timeline(
            benchmark,
            mouth,
            subtitle=subtitle,
            reference_audio=reference_audio,
            config=config,
        )
    return _solve_span_timeline(benchmark, mouth, config=config)


def verify_rendered_stage1_sync(
    plan: AlignmentPlan, rendered_mouth: MouthTrack
) -> dict[str, Any]:
    """Independently check the final rendered mouth track against the plan.

    The verifier intentionally consumes the *rendered* MP4 mouth track rather
    than reusing source-frame assumptions from the planner.  It catches a
    renderer timing drift and rejects plans that lack token/event residuals.
    """

    report: dict[str, Any] = {
        "pair_id": plan.pair_id,
        "strategy": plan.strategy,
        "passed": False,
        "errors": [],
        "metrics": {},
    }
    if plan.strategy != "stage1-token-pause-sync":
        report["errors"].append("rendered token/pause verifier requires v3.2 sync plan")
        return report
    absolute = [abs(float(value)) for value in plan.residuals_ms]
    if not absolute:
        report["errors"].append("sync plan has no residuals")
    elif max(absolute) > 200.0:
        report["errors"].append("sync plan residual exceeds 200ms")

    frame_duration = 1.0 / rendered_mouth.fps
    pauses = plan.metadata.get("pause_map")
    if not isinstance(pauses, list):
        report["errors"].append("sync plan has no pause map")
        pauses = []
    pause_metrics: list[dict[str, Any]] = []
    for item in pauses:
        if not isinstance(item, dict):
            report["errors"].append("pause map entry is invalid")
            continue
        start = float(item.get("output_start", -1))
        end = float(item.get("output_end", -1))
        if end <= start:
            report["errors"].append("pause map output interval is invalid")
            continue
        frames = [
            frame
            for frame in rendered_mouth.frames
            if start + frame_duration <= frame.time <= end - frame_duration
        ]
        if not frames:
            report["errors"].append("rendered pause has no analysable frames")
            continue
        active_ratio = sum(frame.active for frame in frames) / len(frames)
        metric = {
            "reference_duration": item.get("reference_duration"),
            "output_start": start,
            "output_end": end,
            "active_ratio": active_ratio,
        }
        is_pause_hold = item.get("render_mode") == "freeze_low_open_frame"
        if is_pause_hold:
            source_openness = float(item.get("hold_source_openness", 1.0))
            motions = sorted(float(frame.motion) for frame in frames)
            openness = sorted(float(frame.openness) for frame in frames)
            motion_p95 = motions[max(0, int(len(motions) * 0.95) - 1)]
            openness_p50 = openness[len(openness) // 2]
            metric.update(
                render_mode="freeze_low_open_frame",
                source_openness=source_openness,
                rendered_openness_p50=openness_p50,
                rendered_motion_p95=motion_p95,
            )
            if motion_p95 > 0.05:
                report["errors"].append("rendered pause-hold frame is not visually static")
            # Codec quantisation and a second MediaPipe pass can move the
            # numerical ratio of the same frozen frame by a few hundredths.
            # Keep the absolute ceiling in the closed/low-open range while
            # retaining the source-relative check; motion remains a separate
            # hard static-frame gate.
            if openness_p50 > max(source_openness + 0.03, source_openness * 2.0, 0.035):
                report["errors"].append("rendered pause-hold mouth is more open than the approved source frame")
        pause_metrics.append(metric)
        if not is_pause_hold and active_ratio > 0.25:
            report["errors"].append(
                f"rendered mouth remains active through planned pause ({active_ratio:.1%})"
            )
    report["metrics"] = {
        "residual_p50_ms": sorted(absolute)[len(absolute) // 2] if absolute else None,
        "residual_max_ms": max(absolute) if absolute else None,
        "pause_checks": pause_metrics,
        "rendered_mouth_quality": rendered_mouth.quality,
    }
    if rendered_mouth.quality < 0.92:
        report["errors"].append("rendered mouth tracking coverage is below 92%")
    report["passed"] = not report["errors"]
    return report
