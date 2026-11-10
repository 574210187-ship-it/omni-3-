from __future__ import annotations

import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from .contracts import AlignmentToken
from .text_fusion import normalize_text


class CaptionLayoutError(ValueError):
    """Raised when caption text cannot satisfy the locked one-line policy."""


TextMeasurer = Callable[[str], int]


@dataclass(frozen=True, slots=True)
class CaptionSpan:
    text: str
    token_start: int
    token_end: int
    source_start: float
    source_end: float
    measured_width: int


def estimated_text_width(text: str, *, font_size: int = 88, outline: int = 8) -> int:
    """Return a conservative width for planning before exact libass probing."""

    width = outline * 2
    for char in text:
        category = unicodedata.east_asian_width(char)
        width += font_size if category in {"W", "F", "A"} else round(font_size * 0.58)
    return width


def _boundary_time(tokens: Sequence[AlignmentToken], index: int) -> float:
    left = tokens[index - 1]
    right = tokens[index]
    return (left.end + right.start) / 2


def split_token_spans(
    tokens: Sequence[AlignmentToken],
    *,
    source_start: float,
    source_end: float,
    measure_text: TextMeasurer,
    max_width: int,
    preferred_token_ends: set[int] | None = None,
    allowed_token_ends: set[int] | None = None,
    min_chars: int = 4,
    min_duration: float = 0.5,
) -> list[CaptionSpan]:
    if not tokens:
        raise CaptionLayoutError("caption span has no aligned tokens")
    if source_end <= source_start:
        raise CaptionLayoutError("caption span has non-positive duration")
    preferred = preferred_token_ends or set()
    total_chars = len(normalize_text("".join(item.text for item in tokens)))

    def solve(start: int, end: int, span_start: float, span_end: float) -> list[CaptionSpan]:
        text = "".join(item.text for item in tokens[start:end])
        compact = normalize_text(text)
        width = measure_text(text)
        char_floor = 1 if total_chars < min_chars else min_chars
        if len(compact) >= char_floor and span_end - span_start >= min_duration and width <= max_width:
            return [
                CaptionSpan(
                    text=text,
                    token_start=tokens[start].index,
                    token_end=tokens[end - 1].index,
                    source_start=span_start,
                    source_end=span_end,
                    measured_width=width,
                )
            ]

        target_width = min(max_width, max(1, width // 2))
        candidates: list[tuple[tuple[int, int, int], int, float]] = []
        for split in range(start + 1, end):
            boundary_token_end = tokens[split - 1].index
            if allowed_token_ends is not None and boundary_token_end not in allowed_token_ends:
                continue
            boundary = _boundary_time(tokens, split)
            left_text = "".join(item.text for item in tokens[start:split])
            right_text = "".join(item.text for item in tokens[split:end])
            left_chars = len(normalize_text(left_text))
            right_chars = len(normalize_text(right_text))
            if left_chars < char_floor or right_chars < char_floor:
                continue
            if boundary - span_start < min_duration or span_end - boundary < min_duration:
                continue
            left_width = measure_text(left_text)
            if left_width > max_width:
                continue
            preference_penalty = 0 if boundary_token_end in preferred else 1
            score = (preference_penalty, abs(left_width - target_width), -split)
            candidates.append((score, split, boundary))

        for _, split, boundary in sorted(candidates, key=lambda item: item[0]):
            try:
                left = solve(start, split, span_start, boundary)
                right = solve(split, end, boundary, span_end)
            except CaptionLayoutError:
                continue
            return left + right
        raise CaptionLayoutError(
            f"caption text cannot fit one line within {max_width}px without an orphan: {text}"
        )

    spans = solve(0, len(tokens), source_start, source_end)
    if normalize_text("".join(item.text for item in spans)) != normalize_text(
        "".join(item.text for item in tokens)
    ):
        raise CaptionLayoutError("caption splitting did not preserve aligned text")
    return spans
