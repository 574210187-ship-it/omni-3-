from __future__ import annotations

import math
import wave
from array import array
from dataclasses import replace
from pathlib import Path

from .contracts import AlignmentToken, BenchmarkGroup, BenchmarkScript, SafeBoundary
from .text_fusion import normalize_text


def _percentile(values: list[float], fraction: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _energy_pause_ms(wav_path: Path, boundary_time: float) -> float:
    """Return the low-energy run around a token boundary, conservatively."""
    with wave.open(str(wav_path), "rb") as handle:
        rate = handle.getframerate()
        channels = handle.getnchannels()
        width = handle.getsampwidth()
        raw = handle.readframes(handle.getnframes())
    if width != 2 or rate <= 0:
        return 0.0
    samples = array("h")
    samples.frombytes(raw)
    if channels > 1:
        samples = array("h", samples[::channels])
    frame = max(1, round(rate * 0.01))
    rms: list[float] = []
    for start in range(0, len(samples), frame):
        block = samples[start : start + frame]
        if not block:
            break
        rms.append(math.sqrt(sum(float(value) * value for value in block) / len(block)))
    if not rms:
        return 0.0
    threshold = max(80.0, _percentile(rms, 0.75) * 0.16)
    center = min(len(rms) - 1, max(0, round(boundary_time / 0.01)))
    radius = 25
    start = max(0, center - radius)
    end = min(len(rms), center + radius + 1)
    low = [value <= threshold for value in rms[start:end]]
    local_center = center - start
    candidates = [index for index, value in enumerate(low) if value]
    if not candidates:
        return 0.0
    nearest = min(candidates, key=lambda index: abs(index - local_center))
    if abs(nearest - local_center) > 3:
        return 0.0
    left = nearest
    right = nearest
    while left > 0 and low[left - 1]:
        left -= 1
    while right + 1 < len(low) and low[right + 1]:
        right += 1
    return (right - left + 1) * 10.0


def _build_boundaries(
    tokens: list[AlignmentToken], wav_path: Path | None
) -> list[SafeBoundary]:
    boundaries: list[SafeBoundary] = []
    for index, (left, right) in enumerate(zip(tokens, tokens[1:])):
        gap_ms = max(0.0, (right.start - left.end) * 1000.0)
        pause_ms = gap_ms
        if wav_path is not None:
            acoustic_ms = _energy_pause_ms(wav_path, (left.end + right.start) / 2)
            pause_ms = min(gap_ms, acoustic_ms)
        # Alignment timestamps are binary floats, so an exact 80 ms source gap
        # can arrive here as 79.99999999999996 ms.  Keep the documented
        # millisecond thresholds stable without widening them materially.
        threshold_epsilon_ms = 1e-6
        allowed = pause_ms + threshold_epsilon_ms >= 120.0
        review = (
            pause_ms + threshold_epsilon_ms >= 80.0
            and pause_ms + threshold_epsilon_ms < 120.0
        )
        if allowed:
            reason = "word/phrase boundary with at least 120ms verified pause"
        elif review:
            reason = "80-120ms pause requires review and is locked by default"
        else:
            reason = "continuous speech boundary; audio cut forbidden"
        boundaries.append(
            SafeBoundary(
                index=index,
                time=(left.end + right.start) / 2,
                pause_ms=pause_ms,
                cut_allowed=allowed,
                review=review,
                reason=reason,
            )
        )
    return boundaries


def _build_groups(
    tokens: list[AlignmentToken], boundaries: list[SafeBoundary]
) -> list[BenchmarkGroup]:
    groups: list[BenchmarkGroup] = []
    start = 0
    continuity_index = 0
    index = 0
    while index < len(tokens):
        elapsed = tokens[index].end - tokens[start].start
        at_end = index == len(tokens) - 1
        boundary = boundaries[index] if not at_end else None
        safe_break = bool(boundary and boundary.cut_allowed)
        target_break = elapsed >= 1.5
        max_break = elapsed >= 3.0
        if at_end or safe_break or target_break or max_break:
            group_tokens = tokens[start : index + 1]
            groups.append(
                BenchmarkGroup(
                    group_id=f"g{len(groups):02d}",
                    text="".join(item.text for item in group_tokens),
                    token_start=group_tokens[0].index,
                    token_end=group_tokens[-1].index,
                    source_start=group_tokens[0].start,
                    source_end=group_tokens[-1].end,
                    continuity_group_id=f"c{continuity_index:02d}",
                )
            )
            if safe_break:
                continuity_index += 1
            start = index + 1
        index += 1
    return groups


def _recover_caption_coverage(
    text: str, stable: list[AlignmentToken]
) -> tuple[list[AlignmentToken], list[str], bool]:
    """Recover provable edge omissions without inventing interior timing."""

    canonical = normalize_text(text)
    aligned = normalize_text("".join(item.text for item in stable))
    if canonical == aligned:
        return stable, [], True
    if not canonical or not aligned:
        return stable, ["caption text coverage is empty or unaligned"], False

    first = canonical.find(aligned)
    if first < 0 or canonical.find(aligned, first + 1) >= 0:
        # Qwen occasionally assigns a real character a zero-length interval.  The
        # cleaner intentionally excludes that interval from timing decisions, but
        # the remaining tokens still carry their original character indexes.  We
        # may restore caption coverage only when those indexes prove an exact,
        # monotonic mapping back to the canonical text.  This changes caption text
        # attached to existing safe intervals; it does not invent an audio cut or
        # a new timing interval.
        ordered = sorted(stable, key=lambda item: item.index)
        indexes = [item.index for item in ordered]
        index_mapping_is_proven = bool(ordered) and all(
            left < right for left, right in zip(indexes, indexes[1:])
        )
        if index_mapping_is_proven:
            for item in ordered:
                token_text = normalize_text(item.text)
                if (
                    not token_text
                    or item.index < 0
                    or item.index + len(token_text) > len(canonical)
                    or canonical[item.index : item.index + len(token_text)] != token_text
                ):
                    index_mapping_is_proven = False
                    break
        if index_mapping_is_proven:
            repaired: list[AlignmentToken] = []
            for position, item in enumerate(ordered):
                start = 0 if position == 0 else item.index
                end = indexes[position + 1] if position + 1 < len(ordered) else len(canonical)
                recovered_text = canonical[start:end]
                if not recovered_text:
                    index_mapping_is_proven = False
                    break
                repaired.append(
                    replace(
                        item,
                        text=recovered_text,
                        source=item.source + "+caption_index_recovery"
                        if recovered_text != normalize_text(item.text)
                        else item.source,
                    )
                )
        if index_mapping_is_proven and normalize_text(
            "".join(item.text for item in repaired)
        ) == canonical:
            missing_count = len(canonical) - len(aligned)
            return (
                repaired,
                [
                    "recovered caption text from forced-aligner token indexes: "
                    f"{missing_count} character(s)"
                ],
                True,
            )
        return stable, ["stable alignment text does not uniquely cover canonical text"], False

    leading = canonical[:first]
    trailing = canonical[first + len(aligned) :]
    repaired = list(stable)
    reasons: list[str] = []
    if leading:
        repaired[0] = replace(
            repaired[0],
            text=leading + repaired[0].text,
            source=repaired[0].source + "+caption_edge_recovery",
        )
        reasons.append(f"recovered leading caption text: {leading}")
    if trailing:
        repaired[-1] = replace(
            repaired[-1],
            text=repaired[-1].text + trailing,
            source=repaired[-1].source + "+caption_edge_recovery",
        )
        reasons.append(f"recovered trailing caption text: {trailing}")
    covered = normalize_text("".join(item.text for item in repaired)) == canonical
    if not covered:
        reasons.append("caption edge recovery did not preserve canonical text")
    return repaired, reasons, covered


def repair_benchmark_caption_coverage(benchmark: BenchmarkScript) -> BenchmarkScript:
    """Upgrade an older benchmark whose stable tokens omitted only edge text."""

    if benchmark.status == "fail":
        return benchmark
    repaired, reasons, covered = _recover_caption_coverage(
        benchmark.text, list(benchmark.tokens)
    )
    if not covered:
        return replace(
            benchmark,
            groups=[],
            status="fail",
            reasons=list(benchmark.reasons) + reasons,
        )
    groups = _build_groups(repaired, list(benchmark.boundaries))
    if normalize_text("".join(item.text for item in groups)) != normalize_text(
        benchmark.text
    ):
        return replace(
            benchmark,
            tokens=repaired,
            groups=[],
            status="fail",
            reasons=list(benchmark.reasons)
            + reasons
            + ["repaired benchmark groups do not preserve canonical caption text"],
        )
    return replace(
        benchmark,
        tokens=repaired,
        groups=groups,
        reasons=list(benchmark.reasons) + reasons,
    )


def build_benchmark_script(
    pair_id: str,
    text: str,
    tokens: list[AlignmentToken],
    *,
    reference_duration: float,
    wav_path: Path | None = None,
    padding: float = 0.03,
) -> BenchmarkScript:
    stable = [item for item in tokens if item.is_stable]
    if not stable:
        raise ValueError("reference alignment has no stable tokens")
    stable, coverage_reasons, coverage_ok = _recover_caption_coverage(text, stable)
    speech_start = max(0.0, stable[0].start - padding)
    speech_end = min(reference_duration, stable[-1].end + padding)
    if not coverage_ok:
        return BenchmarkScript(
            pair_id=pair_id,
            text=text,
            reference_duration=reference_duration,
            speech_start=speech_start,
            speech_end=speech_end,
            tokens=stable,
            boundaries=[],
            groups=[],
            status="fail",
            reasons=coverage_reasons,
        )
    boundaries = _build_boundaries(stable, wav_path)
    groups = _build_groups(stable, boundaries)
    reasons: list[str] = list(coverage_reasons)
    review_count = sum(item.review for item in boundaries)
    if review_count:
        reasons.append(f"{review_count} boundary candidates are locked pending review")
    reasons.append(
        f"{sum(item.cut_allowed for item in boundaries)} of {len(boundaries)} boundaries are safe for audio cuts"
    )
    if normalize_text("".join(item.text for item in groups)) != normalize_text(text):
        return BenchmarkScript(
            pair_id=pair_id,
            text=text,
            reference_duration=reference_duration,
            speech_start=speech_start,
            speech_end=speech_end,
            tokens=stable,
            boundaries=boundaries,
            groups=[],
            status="fail",
            reasons=reasons + ["benchmark groups do not preserve canonical caption text"],
        )
    return BenchmarkScript(
        pair_id=pair_id,
        text=text,
        reference_duration=reference_duration,
        speech_start=speech_start,
        speech_end=speech_end,
        tokens=stable,
        boundaries=boundaries,
        groups=groups,
        status="pass",
        reasons=reasons,
    )
