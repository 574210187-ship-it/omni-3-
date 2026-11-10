from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
from typing import Any

from .contracts import AlignmentToken
from .text_fusion import normalize_text


@dataclass(slots=True)
class AlignmentQC:
    tokens: list[AlignmentToken]
    issues: list[dict[str, Any]]
    status: str
    zero_duration_ratio: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "status": self.status,
            "zero_duration_ratio": self.zero_duration_ratio,
            "tokens": [token.to_dict() for token in self.tokens],
            "issues": self.issues,
        }


def clean_alignment(
    items: list[dict[str, Any]],
    *,
    audio_duration: float,
    zero_ratio_fail: float = 0.25,
) -> AlignmentQC:
    if not isfinite(audio_duration) or audio_duration <= 0:
        raise ValueError("audio_duration must be finite and positive")
    tokens: list[AlignmentToken] = []
    issues: list[dict[str, Any]] = []
    zero_count = 0
    previous_start = -1.0
    for index, item in enumerate(items):
        text = str(item.get("text", "")).strip()
        try:
            start = float(item.get("start", item.get("start_time")))
            end = float(item.get("end", item.get("end_time")))
            confidence = float(item.get("confidence", 1.0))
        except (TypeError, ValueError):
            issues.append({"index": index, "code": "invalid_number", "item": item})
            continue
        status = "valid"
        if not text:
            issues.append({"index": index, "code": "empty_text"})
            continue
        if not all(isfinite(value) for value in (start, end, confidence)):
            issues.append({"index": index, "code": "non_finite"})
            continue
        if start < 0 or end < 0 or start > audio_duration or end > audio_duration:
            status = "invalid"
            issues.append({"index": index, "code": "out_of_bounds", "start": start, "end": end})
            start = min(max(start, 0.0), audio_duration)
            end = min(max(end, 0.0), audio_duration)
        if end < start:
            status = "invalid"
            issues.append({"index": index, "code": "reversed", "start": start, "end": end})
            end = start
        if start + 1e-9 < previous_start:
            status = "invalid"
            issues.append({"index": index, "code": "non_monotonic", "start": start})
        if end <= start + 1e-9:
            zero_count += 1
            if status == "valid":
                status = "zero_duration"
            issues.append({"index": index, "code": "zero_duration", "start": start})
        previous_start = max(previous_start, start)
        tokens.append(
            AlignmentToken(
                index=int(item.get("index", index)),
                text=text,
                start=start,
                end=end,
                confidence=min(1.0, max(0.0, confidence)),
                status=status,
                source=str(item.get("source", "forced_alignment")),
            )
        )
    ratio = zero_count / len(tokens) if tokens else 1.0
    stable_count = sum(token.is_stable for token in tokens)
    status = "pass"
    if not tokens or stable_count == 0 or ratio > zero_ratio_fail:
        status = "fail"
    elif issues:
        status = "review"
    return AlignmentQC(tokens=tokens, issues=issues, status=status, zero_duration_ratio=ratio)


def pair_stable_tokens(
    reference: list[AlignmentToken], generated: list[AlignmentToken]
) -> list[tuple[AlignmentToken, AlignmentToken]]:
    generated_by_index = {token.index: token for token in generated}
    pairs: list[tuple[AlignmentToken, AlignmentToken]] = []
    for ref in reference:
        gen = generated_by_index.get(ref.index)
        if gen is None or not ref.is_stable or not gen.is_stable:
            continue
        if normalize_text(ref.text) != normalize_text(gen.text):
            continue
        pairs.append((ref, gen))
    return pairs
