from __future__ import annotations

from dataclasses import dataclass

from .alignment import pair_stable_tokens
from .contracts import AlignmentPlan, AlignmentToken, AudioChunk, VideoSegment


@dataclass(slots=True)
class SolverConfig:
    head_padding: float = 0.05
    tail_padding: float = 0.08
    split_gap: float = 0.18
    audio_padding: float = 0.03
    auto_speed_max: float = 1.15
    review_speed_max: float = 1.25
    residual_review_ms: float = 120.0
    residual_fail_ms: float = 240.0
    audio_reflow_split_ms: float = 140.0
    speed_window_tokens: int = 5
    min_speed_gain_seconds: float = 0.10


def _merge_speed_windows(
    pairs: list[tuple[AlignmentToken, AlignmentToken]],
    trim_start: float,
    trim_end: float,
    config: SolverConfig,
) -> list[tuple[float, float, float, str]]:
    # Forced aligners quantize boundaries, so a single Chinese character can appear
    # 80 ms longer on one encode and 80 ms shorter on the next. Estimate speed over
    # a short word group and require a real accumulated gain before changing video.
    raw: list[tuple[float, float, float, float, str]] = []
    width = max(3, config.speed_window_tokens)
    radius = width // 2
    for index in range(len(pairs)):
        first = max(0, index - radius)
        last = min(len(pairs), index + radius + 1)
        window = pairs[first:last]
        if len(window) < min(3, len(pairs)):
            continue
        ref_start, ref_end = window[0][0].start, window[-1][0].end
        gen_start, gen_end = window[0][1].start, window[-1][1].end
        ref_span = ref_end - ref_start
        gen_span = gen_end - gen_start
        if ref_span <= 0 or gen_span - ref_span < config.min_speed_gain_seconds:
            continue
        if gen_span / ref_span > 1.02:
            raw.append((gen_start, gen_end, ref_start, ref_end, "".join(x[1].text for x in window)))
    merged_raw: list[tuple[float, float, float, float, str]] = []
    for gen_start, gen_end, ref_start, ref_end, text in raw:
        if merged_raw and gen_start <= merged_raw[-1][1] + 0.08:
            old_gs, old_ge, old_rs, old_re, old_text = merged_raw[-1]
            merged_raw[-1] = (
                min(old_gs, gen_start),
                max(old_ge, gen_end),
                min(old_rs, ref_start),
                max(old_re, ref_end),
                old_text + text,
            )
        else:
            merged_raw.append((gen_start, gen_end, ref_start, ref_end, text))

    merged: list[tuple[float, float, float, str]] = []
    for gen_start, gen_end, ref_start, ref_end, text in merged_raw:
        start = max(trim_start, gen_start)
        end = min(trim_end, gen_end)
        if end <= start:
            continue
        ref_span = ref_end - ref_start
        gen_span = gen_end - gen_start
        speed = gen_span / ref_span
        merged.append((start, end, speed, text))
    return merged


def _build_video_segments(
    trim_start: float,
    trim_end: float,
    speed_windows: list[tuple[float, float, float, str]],
    config: SolverConfig,
) -> tuple[list[VideoSegment], list[str], str]:
    segments: list[VideoSegment] = []
    reasons: list[str] = []
    status = "pass"
    source_cursor = trim_start
    output_cursor = 0.0

    def append_segment(start: float, end: float, speed: float, reason: str = "") -> None:
        nonlocal output_cursor, status
        if end <= start + 1e-9:
            return
        review = speed > config.auto_speed_max
        if speed > config.review_speed_max:
            status = "fail"
            reasons.append(f"required speed {speed:.3f}x exceeds {config.review_speed_max:.2f}x near {reason!r}")
        elif review and status == "pass":
            status = "review"
            reasons.append(f"speed {speed:.3f}x requires review near {reason!r}")
        segment = VideoSegment(start, end, speed, output_cursor, review=review, reason=reason)
        segments.append(segment)
        output_cursor = segment.output_end

    for start, end, speed, text in speed_windows:
        start = max(start, source_cursor)
        end = min(end, trim_end)
        append_segment(source_cursor, start, 1.0)
        append_segment(start, end, speed, f"match token group {text}")
        source_cursor = end
    append_segment(source_cursor, trim_end, 1.0)
    return segments, reasons, status


def _map_video_time(segments: list[VideoSegment], source_time: float) -> float:
    for segment in segments:
        if segment.source_start - 1e-9 <= source_time <= segment.source_end + 1e-9:
            local = min(max(source_time, segment.source_start), segment.source_end) - segment.source_start
            return segment.output_start + local / segment.speed
    if source_time < segments[0].source_start:
        return 0.0
    return segments[-1].output_end


def _group_pairs(
    pairs: list[tuple[AlignmentToken, AlignmentToken]], split_gap: float
) -> list[list[tuple[AlignmentToken, AlignmentToken]]]:
    groups: list[list[tuple[AlignmentToken, AlignmentToken]]] = []
    current: list[tuple[AlignmentToken, AlignmentToken]] = []
    previous: tuple[AlignmentToken, AlignmentToken] | None = None
    for pair in pairs:
        if previous is not None:
            ref_gap = pair[0].start - previous[0].end
            gen_gap = pair[1].start - previous[1].end
            index_gap = pair[0].index - previous[0].index
            if ref_gap >= split_gap or gen_gap >= split_gap or index_gap > 1:
                groups.append(current)
                current = []
        current.append(pair)
        previous = pair
    if current:
        groups.append(current)
    return groups


def _split_groups_for_audio_reflow(
    pairs: list[tuple[AlignmentToken, AlignmentToken]],
    segments: list[VideoSegment],
    config: SolverConfig,
) -> list[list[tuple[AlignmentToken, AlignmentToken]]]:
    result: list[list[tuple[AlignmentToken, AlignmentToken]]] = []
    for natural_group in _group_pairs(pairs, config.split_gap):
        current = [natural_group[0]]
        base_ref = natural_group[0][0].start
        base_visual = _map_video_time(segments, natural_group[0][1].start)
        for pair in natural_group[1:]:
            reference_offset = pair[0].start - base_ref
            visual_offset = _map_video_time(segments, pair[1].start) - base_visual
            drift_ms = (reference_offset - visual_offset) * 1000
            if abs(drift_ms) >= config.audio_reflow_split_ms:
                result.append(current)
                current = [pair]
                base_ref = pair[0].start
                base_visual = _map_video_time(segments, pair[1].start)
            else:
                current.append(pair)
        result.append(current)
    return result


def solve_timeline(
    pair_id: str,
    reference_tokens: list[AlignmentToken],
    generated_tokens: list[AlignmentToken],
    *,
    reference_duration: float,
    generated_duration: float,
    config: SolverConfig | None = None,
) -> AlignmentPlan:
    config = config or SolverConfig()
    pairs = pair_stable_tokens(reference_tokens, generated_tokens)
    if not pairs:
        raise ValueError("no stable matching token anchors")
    trim_start = max(0.0, pairs[0][1].start - config.head_padding)
    trim_end = min(generated_duration, pairs[-1][1].end + config.tail_padding)
    speed_windows = _merge_speed_windows(pairs, trim_start, trim_end, config)
    applicable_speed_windows = [x for x in speed_windows if x[2] <= config.review_speed_max]
    deferred_speed_windows = [x for x in speed_windows if x[2] > config.review_speed_max]
    video_segments, reasons, status = _build_video_segments(
        trim_start, trim_end, applicable_speed_windows, config
    )
    if deferred_speed_windows:
        if len(pairs) < 2:
            status = "fail"
            reasons.append(
                f"required speed {deferred_speed_windows[0][2]:.3f}x exceeds {config.review_speed_max:.2f}x and has no safe word boundary for audio reflow"
            )
        else:
            if status == "pass":
                status = "review"
            reasons.append(
                "large pace difference handled by moving reference audio at word-group boundaries instead of over-speeding video"
            )
    output_duration = video_segments[-1].output_end

    audio_groups = _split_groups_for_audio_reflow(pairs, video_segments, config)
    audio_chunks: list[AudioChunk] = []
    residuals_ms: list[float] = []
    previous_audio_end = 0.0
    for group_index, group in enumerate(audio_groups):
        ref_first, gen_first = group[0]
        ref_last, _gen_last = group[-1]
        source_start = max(0.0, ref_first.start - config.audio_padding)
        source_end = min(reference_duration, ref_last.end + config.audio_padding)
        if group_index > 0:
            previous_ref_end = audio_groups[group_index - 1][-1][0].end
            source_start = max(source_start, (previous_ref_end + ref_first.start) / 2)
        if group_index + 1 < len(audio_groups):
            next_ref_start = audio_groups[group_index + 1][0][0].start
            source_end = min(source_end, (ref_last.end + next_ref_start) / 2)
        desired_start = max(0.0, _map_video_time(video_segments, gen_first.start) - (ref_first.start - source_start))
        output_start = max(desired_start, previous_audio_end)
        push_ms = (output_start - desired_start) * 1000
        if push_ms > config.residual_review_ms:
            if status == "pass":
                status = "review"
            reasons.append(f"audio group {ref_first.index}-{ref_last.index} pushed {push_ms:.0f}ms to avoid overlap")
        chunk = AudioChunk(source_start, source_end, output_start, config.audio_padding, "natural speech group")
        audio_chunks.append(chunk)
        previous_audio_end = chunk.output_end
        for ref, gen in group:
            audio_word_start = output_start + (ref.start - source_start)
            visual_word_start = _map_video_time(video_segments, gen.start)
            residuals_ms.append((audio_word_start - visual_word_start) * 1000)

    max_residual = max((abs(value) for value in residuals_ms), default=0.0)
    if max_residual > config.residual_fail_ms:
        status = "fail"
        reasons.append(
            f"anchor residual {max_residual:.0f}ms exceeds {config.residual_fail_ms:.0f}ms after audio reflow"
        )
    elif max_residual > config.residual_review_ms and status == "pass":
        status = "review"
        reasons.append(
            f"anchor residual {max_residual:.0f}ms requires review"
        )

    audio_end = audio_chunks[-1].output_end
    if audio_end > output_duration and trim_end < generated_duration:
        # Prefer retaining a little more of the generated tail over truncating the
        # final reference word. This changes only the allowed tail trim boundary;
        # the generated middle remains continuously covered.
        extension = min(generated_duration - trim_end, audio_end - output_duration + 0.02)
        if extension > 1e-9:
            old_trim_end = trim_end
            if video_segments[-1].speed == 1.0:
                video_segments[-1].source_end += extension
            else:
                video_segments.append(
                    VideoSegment(old_trim_end, old_trim_end + extension, 1.0, output_duration, reason="retain tail for full reference audio")
                )
            trim_end += extension
            output_duration = video_segments[-1].output_end
            reasons.append(f"retained {extension:.3f}s of generated tail to preserve final reference audio")

    if audio_end > output_duration + 0.05:
        status = "fail"
        reasons.append(
            f"reference audio ends at {audio_end:.3f}s after video {output_duration:.3f}s"
        )
    if any(gen.duration + 1e-9 < ref.duration for ref, gen in pairs):
        if status == "pass":
            status = "review"
        reasons.append("one or more generated mouth-motion tokens are shorter than reference audio tokens")

    return AlignmentPlan(
        pair_id=pair_id,
        generated_duration=generated_duration,
        reference_duration=reference_duration,
        trim_start=trim_start,
        trim_end=trim_end,
        output_duration=output_duration,
        video_segments=video_segments,
        audio_chunks=audio_chunks,
        status=status,
        reasons=reasons,
        residuals_ms=residuals_ms,
    )
