from __future__ import annotations

import re
from typing import Any, Sequence

from .contracts import AlignmentToken, BenchmarkScript
from .text_fusion import fuse_texts, normalize_text


# v1 is retained only so that previously completed rows remain reproducible.
# New Stage1 v3.2 evidence is schema v3 and is consumed without rewriting it.
SCHEMA_VERSION = 1
STAGE1_V32_SCHEMA_VERSION = 3
STAGE1_V32_EVIDENCE_VERSION = "subtitle-v3.2"
STAGE1_V32_SPLIT_POLICY = "semantic-word-v1"
STAGE1_V32_STYLE_SIGNATURE = "Noto Sans CJK SC|88|8|1080x1920|an5|q2|pos(540,1229)"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PUNCTUATION = set("，。！？；：、,.!?;:")
MIN_AUTHORITATIVE_VISUAL_CONFIDENCE = 0.55


class SubtitleContractError(ValueError):
    """Raised when subtitle evidence is incomplete or internally inconsistent."""


def _float(value: Any, *, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise SubtitleContractError(f"{name} must be numeric") from exc
    if result < 0:
        raise SubtitleContractError(f"{name} must be non-negative")
    return result


def _validate_stage1_v32_metadata(value: dict[str, Any]) -> None:
    """Validate immutable fields introduced by Stage1 subtitle-v3.2.

    These fields deliberately live in the contract rather than an adapter: a
    Stage2 run must either consume the ZIP's original evidence or fail. It
    must never downgrade schema_version or re-split wording character by
    character just to fit an older runner.
    """

    if value.get("evidence_version") != STAGE1_V32_EVIDENCE_VERSION:
        raise SubtitleContractError("v3 subtitle evidence_version must be subtitle-v3.2")
    if value.get("split_policy") != STAGE1_V32_SPLIT_POLICY:
        raise SubtitleContractError("v3 subtitle split_policy must be semantic-word-v1")
    if value.get("caption_style_signature") != STAGE1_V32_STYLE_SIGNATURE:
        raise SubtitleContractError("v3 subtitle caption_style_signature is invalid")
    max_chars = value.get("max_line_chars")
    min_chars = value.get("min_line_chars")
    if (
        not isinstance(max_chars, int)
        or not isinstance(min_chars, int)
        or min_chars < 2
        or max_chars < min_chars
    ):
        raise SubtitleContractError("v3 subtitle line character limits are invalid")
    if _float(value.get("max_line_width_px"), name="max_line_width_px") <= 0:
        raise SubtitleContractError("v3 subtitle max_line_width_px is invalid")
    if _float(value.get("min_line_duration"), name="min_line_duration") < 0.5:
        raise SubtitleContractError("v3 subtitle min_line_duration must be at least 0.5")


def _visual_entries(ocr_result: dict[str, Any]) -> list[dict[str, Any]]:
    raw = ocr_result.get("entries", [])
    if not isinstance(raw, list):
        raise SubtitleContractError("OCR entries must be a list")
    entries: list[dict[str, Any]] = []
    for index, value in enumerate(raw):
        if not isinstance(value, dict):
            raise SubtitleContractError(f"OCR entry {index} must be an object")
        text = normalize_text(str(value.get("text", "")))
        if not text:
            continue
        start = _float(value.get("start", 0), name=f"OCR entry {index} start")
        end = _float(value.get("end", 0), name=f"OCR entry {index} end")
        if end <= start:
            raise SubtitleContractError(f"OCR entry {index} must have positive duration")
        confidence = _float(
            value.get("confidence", 0), name=f"OCR entry {index} confidence"
        )
        if confidence > 1:
            raise SubtitleContractError(f"OCR entry {index} confidence exceeds 1")
        entries.append(
            {
                "event_id": f"v{len(entries):03d}",
                "text": text,
                "confidence": confidence,
                "display_start": start,
                "display_end": end,
                "representative_frame_index": value.get("representative_frame_index"),
                "representative_time": value.get("representative_time", start),
                "subtitle_box": value.get("subtitle_box"),
            }
        )
    return entries


def select_caption_source(
    ocr_result: dict[str, Any], asr_result: dict[str, Any]
) -> dict[str, Any]:
    """Select immutable caption wording before forced alignment.

    Stable visual subtitle text is authoritative. ASR is retained as timing and
    conflict evidence, but never silently replaces different wording on screen.
    """

    scanned_entries = _visual_entries(ocr_result)
    entries = [
        item
        for item in scanned_entries
        if item["confidence"] >= MIN_AUTHORITATIVE_VISUAL_CONFIDENCE
    ]
    ignored_low_confidence = len(scanned_entries) - len(entries)
    scan = ocr_result.get("scan")
    if not isinstance(scan, dict) or scan.get("completed") is not True:
        raise SubtitleContractError("OCR result has no completed visual scan evidence")
    asr_raw = str(asr_result.get("text", ""))
    asr_text = normalize_text(asr_raw)
    visual_text = "".join(item["text"] for item in entries)
    if entries:
        fusion = fuse_texts(asr_text, visual_text) if asr_text else {
            "tokens": [],
            "conflicts": [],
            "sources": {"asr": "", "ocr": visual_text},
        }
        conflicts: list[dict[str, Any]] = []
        if asr_text and asr_text != visual_text:
            conflicts.append(
                {
                    "type": "spoken_text_conflict",
                    "visual_text": visual_text,
                    "asr_text": asr_text,
                    "resolution": "visual_subtitle_preserved",
                    "character_conflicts": list(fusion.get("conflicts", [])),
                }
            )
        low_confidence = any(item["confidence"] < 0.80 for item in entries)
        reasons: list[str] = []
        if conflicts:
            reasons.append("ASR wording differs from the displayed subtitle; visual text preserved")
        if low_confidence:
            reasons.append("one or more visual subtitle events have OCR confidence below 0.80")
        return {
            "source_mode": "visual_subtitle_fused",
            "canonical_caption_text": visual_text,
            "visual_subtitle_present": True,
            "visual_events": entries,
            "asr_text": asr_text,
            "asr_raw_text": asr_raw,
            "scan": dict(scan),
            "conflicts": conflicts,
            "quality_status": "review" if reasons else "pass",
            "reasons": reasons,
        }

    if not asr_text:
        raise SubtitleContractError("completed visual scan found no subtitle and ASR text is empty")
    reasons = ["completed visual scan found no stable subtitle; ASR semantic mode used"]
    if ignored_low_confidence:
        reasons.append(
            f"ignored {ignored_low_confidence} OCR event(s) below authoritative confidence "
            f"{MIN_AUTHORITATIVE_VISUAL_CONFIDENCE:.2f}"
        )
    return {
        "source_mode": "audio_semantic",
        "canonical_caption_text": asr_text,
        "visual_subtitle_present": False,
        "visual_events": [],
        "asr_text": asr_text,
        "asr_raw_text": asr_raw,
        "scan": dict(scan),
        "conflicts": [],
        "quality_status": "pass",
        "reasons": reasons,
    }


def _punctuation_offsets(raw_text: str, canonical: str) -> set[int]:
    if normalize_text(raw_text) != canonical:
        return set()
    offsets: set[int] = set()
    compact_count = 0
    for char in raw_text:
        normalized = normalize_text(char)
        compact_count += len(normalized)
        if char in PUNCTUATION and 0 < compact_count < len(canonical):
            offsets.add(compact_count)
    return offsets


def _semantic_offsets(text: str) -> dict[int, str]:
    offsets: dict[int, str] = {}
    conditional_prefixes = ("如果", "假如", "倘若", "即使", "虽然", "只要", "除非")
    response_markers = ("那么", "那就", "就", "都", "也", "还", "仍", "便", "则")
    if text.startswith(conditional_prefixes):
        for marker in response_markers:
            position = text.find(marker, 2)
            if position >= 4 and len(text) - position >= 4:
                offsets[position] = "conditional_response_boundary"
                break
    clause_markers = (
        "但是",
        "然而",
        "可是",
        "却",
        "所以",
        "因此",
        "否则",
        "而且",
        "并且",
        "同时",
        "后来",
        "最后",
    )
    for marker in clause_markers:
        start = 0
        while True:
            position = text.find(marker, start)
            if position < 0:
                break
            if position >= 4 and len(text) - position >= 4:
                offsets.setdefault(position, f"clause_marker:{marker}")
            start = position + len(marker)
    return offsets


def _audio_semantic_event_texts(raw_text: str, canonical: str) -> list[str]:
    chunks = [normalize_text(item) for item in re.split(r"[，。！？；：、,.!?;:]+", raw_text)]
    chunks = [item for item in chunks if item]
    if chunks and "".join(chunks) == canonical:
        return chunks
    return [canonical]


def _token_boundaries(tokens: Sequence[AlignmentToken]) -> tuple[dict[int, int], str]:
    offset_to_token_end: dict[int, int] = {}
    pieces: list[str] = []
    offset = 0
    for token in tokens:
        compact = normalize_text(token.text)
        if not compact:
            raise SubtitleContractError("aligned token text is empty after normalization")
        pieces.append(compact)
        offset += len(compact)
        offset_to_token_end[offset] = token.index
    return offset_to_token_end, "".join(pieces)


def _event_from_offsets(
    *,
    event_id: str,
    text: str,
    char_start: int,
    char_end: int,
    tokens: Sequence[AlignmentToken],
    offset_to_token_end: dict[int, int],
    source: str,
    display: dict[str, Any] | None,
) -> dict[str, Any]:
    if char_start not in ({0} | set(offset_to_token_end)):
        raise SubtitleContractError(f"caption event {event_id} starts inside an aligned token")
    if char_end not in offset_to_token_end:
        raise SubtitleContractError(f"caption event {event_id} ends inside an aligned token")
    token_start = tokens[0].index if char_start == 0 else offset_to_token_end[char_start] + 1
    token_end = offset_to_token_end[char_end]
    selected = [item for item in tokens if token_start <= item.index <= token_end]
    if not selected or normalize_text("".join(item.text for item in selected)) != text:
        raise SubtitleContractError(f"caption event {event_id} does not match aligned tokens")
    event: dict[str, Any] = {
        "event_id": event_id,
        "text": text,
        "token_start": token_start,
        "token_end": token_end,
        "source_start": selected[0].start,
        "source_end": selected[-1].end,
        "source": source,
    }
    if display is not None:
        event.update(
            {
                "display_start": display["display_start"],
                "display_end": display["display_end"],
                "ocr_confidence": display["confidence"],
                "representative_frame_index": display.get("representative_frame_index"),
                "representative_time": display.get("representative_time"),
                "subtitle_box": display.get("subtitle_box"),
            }
        )
    return event


def build_subtitle_contract(
    *,
    pair_id: str,
    reference_video_sha256: str,
    reference_audio_sha256: str,
    reference_duration: float,
    selected: dict[str, Any],
    aligned_tokens: Sequence[AlignmentToken],
) -> dict[str, Any]:
    if not pair_id.isdigit():
        raise SubtitleContractError("pair_id must contain digits only")
    for name, digest in (
        ("reference_video_sha256", reference_video_sha256),
        ("reference_audio_sha256", reference_audio_sha256),
    ):
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest.lower()):
            raise SubtitleContractError(f"{name} must be a SHA-256")
    canonical = normalize_text(str(selected.get("canonical_caption_text", "")))
    if not canonical:
        raise SubtitleContractError("canonical caption text is empty")
    tokens = list(aligned_tokens)
    if not tokens:
        raise SubtitleContractError("subtitle contract requires aligned tokens")
    offset_to_token_end, aligned_text = _token_boundaries(tokens)
    if aligned_text != canonical:
        raise SubtitleContractError("aligned tokens do not preserve canonical caption text")

    source_mode = selected.get("source_mode")
    raw_events: list[dict[str, Any] | None]
    event_texts: list[str]
    if source_mode == "visual_subtitle_fused":
        visual_events = selected.get("visual_events")
        if not isinstance(visual_events, list) or not visual_events:
            raise SubtitleContractError("visual subtitle mode requires visual events")
        raw_events = list(visual_events)
        event_texts = [normalize_text(str(item.get("text", ""))) for item in visual_events]
    elif source_mode == "audio_semantic":
        event_texts = _audio_semantic_event_texts(
            str(selected.get("asr_raw_text", "")), canonical
        )
        raw_events = [None] * len(event_texts)
    else:
        raise SubtitleContractError(f"unsupported subtitle source mode: {source_mode!r}")
    if "".join(event_texts) != canonical:
        raise SubtitleContractError("source caption events do not preserve canonical text")

    caption_events: list[dict[str, Any]] = []
    approved: dict[int, dict[str, Any]] = {}
    char_start = 0
    for index, (text, display) in enumerate(zip(event_texts, raw_events)):
        char_end = char_start + len(text)
        caption_events.append(
            _event_from_offsets(
                event_id=f"c{index:03d}",
                text=text,
                char_start=char_start,
                char_end=char_end,
                tokens=tokens,
                offset_to_token_end=offset_to_token_end,
                source=str(source_mode),
                display=display,
            )
        )
        if char_end < len(canonical):
            approved[char_end] = {
                "char_offset": char_end,
                "reason": "visual_subtitle_change"
                if source_mode == "visual_subtitle_fused"
                else "asr_punctuation_boundary",
                "source_event_id": f"c{index:03d}",
            }
        for local, reason in _semantic_offsets(text).items():
            absolute = char_start + local
            approved.setdefault(
                absolute,
                {
                    "char_offset": absolute,
                    "reason": reason,
                    "source_event_id": f"c{index:03d}",
                },
            )
        char_start = char_end

    for offset in _punctuation_offsets(str(selected.get("asr_raw_text", "")), canonical):
        approved.setdefault(
            offset,
            {
                "char_offset": offset,
                "reason": "asr_punctuation_boundary",
                "source_event_id": None,
            },
        )

    approved_points: list[dict[str, Any]] = []
    for offset, value in sorted(approved.items()):
        token_end = offset_to_token_end.get(offset)
        if token_end is None or offset <= 0 or offset >= len(canonical):
            continue
        point = dict(value)
        point.update(
            {
                "token_end": token_end,
                "left_text": canonical[:offset],
                "right_text": canonical[offset:],
            }
        )
        approved_points.append(point)

    contract = {
        "schema_version": SCHEMA_VERSION,
        "pair_id": pair_id,
        "source_mode": source_mode,
        "reference_video_sha256": reference_video_sha256.lower(),
        "reference_audio_sha256": reference_audio_sha256.lower(),
        "reference_duration": float(reference_duration),
        "visual_subtitle_present": bool(selected.get("visual_subtitle_present")),
        "visual_scan": dict(selected.get("scan", {})),
        "visual_events": list(selected.get("visual_events", [])),
        "asr_text": str(selected.get("asr_text", "")),
        "canonical_caption_text": canonical,
        "aligned_tokens": [item.to_dict() for item in tokens],
        "caption_events": caption_events,
        "approved_split_points": approved_points,
        "conflicts": list(selected.get("conflicts", [])),
        "quality_status": str(selected.get("quality_status", "fail")),
        "reasons": list(selected.get("reasons", [])),
    }
    validate_subtitle_contract(contract)
    return contract


def validate_subtitle_contract(
    value: dict[str, Any], benchmark: BenchmarkScript | None = None
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SubtitleContractError("subtitle.json must contain an object")
    schema_version = value.get("schema_version")
    if schema_version not in {SCHEMA_VERSION, STAGE1_V32_SCHEMA_VERSION}:
        raise SubtitleContractError(
            f"unsupported subtitle schema_version: {schema_version!r}"
        )
    is_stage1_v32 = schema_version == STAGE1_V32_SCHEMA_VERSION
    if is_stage1_v32:
        _validate_stage1_v32_metadata(value)
    pair_id = value.get("pair_id")
    if not isinstance(pair_id, str) or not pair_id.isdigit():
        raise SubtitleContractError("subtitle pair_id must contain digits only")
    canonical = normalize_text(str(value.get("canonical_caption_text", "")))
    if not canonical:
        raise SubtitleContractError("canonical caption text is empty")
    raw_tokens = value.get("aligned_tokens")
    if not isinstance(raw_tokens, list) or not raw_tokens:
        raise SubtitleContractError("subtitle aligned_tokens must be non-empty")
    tokens = [AlignmentToken.from_dict(dict(item)) for item in raw_tokens]
    _, aligned_text = _token_boundaries(tokens)
    if aligned_text != canonical:
        raise SubtitleContractError("subtitle aligned tokens do not preserve canonical text")
    events = value.get("caption_events")
    if not isinstance(events, list) or not events:
        raise SubtitleContractError("subtitle caption_events must be non-empty")
    if normalize_text("".join(str(item.get("text", "")) for item in events)) != canonical:
        raise SubtitleContractError("caption events do not preserve canonical caption text")
    indexes = {item.index for item in tokens}
    previous_source_end = 0.0
    for event in events:
        start = event.get("token_start")
        end = event.get("token_end")
        if start not in indexes or end not in indexes or int(end) < int(start):
            raise SubtitleContractError("caption event has an invalid token range")
        selected = [item for item in tokens if int(start) <= item.index <= int(end)]
        if normalize_text("".join(item.text for item in selected)) != normalize_text(
            str(event.get("text", ""))
        ):
            raise SubtitleContractError("caption event text does not match aligned tokens")
        if is_stage1_v32:
            event_text = normalize_text(str(event.get("text", "")))
            if len(event_text) < int(value["min_line_chars"]):
                raise SubtitleContractError("v3 subtitle contains a too-short caption event")
            if len(event_text) > int(value["max_line_chars"]):
                raise SubtitleContractError("v3 subtitle contains an overlong caption event")
            source_start = _float(event.get("source_start"), name="caption event source_start")
            source_end = _float(event.get("source_end"), name="caption event source_end")
            if source_end <= source_start:
                raise SubtitleContractError("v3 caption event has an invalid source interval")
            if source_start + 1e-6 < previous_source_end:
                raise SubtitleContractError("v3 caption events are not in source-time order")
            previous_source_end = source_end
            if not isinstance(event.get("event_id"), str) or not event["event_id"]:
                raise SubtitleContractError("v3 caption event has no event_id")
    points = value.get("approved_split_points")
    if not isinstance(points, list):
        raise SubtitleContractError("approved_split_points must be a list")
    last_index = tokens[-1].index
    for point in points:
        token_end = point.get("token_end") if isinstance(point, dict) else None
        if token_end not in indexes or int(token_end) >= last_index:
            raise SubtitleContractError("approved split point has an invalid token boundary")
        if is_stage1_v32:
            offset = point.get("char_offset")
            if not isinstance(offset, int) or not 0 < offset < len(canonical):
                raise SubtitleContractError("v3 approved split point has an invalid char_offset")
            if point.get("left_text") != canonical[:offset] or point.get("right_text") != canonical[offset:]:
                raise SubtitleContractError("v3 approved split point text does not match canonical text")
    if value.get("quality_status") not in {"pass", "review", "fail"}:
        raise SubtitleContractError("subtitle quality_status is invalid")
    if benchmark is not None:
        if benchmark.pair_id != pair_id:
            raise SubtitleContractError("subtitle pair ID does not match benchmark")
        if normalize_text(benchmark.text) != canonical:
            raise SubtitleContractError("subtitle text does not match benchmark text")
        benchmark_tokens = [item.to_dict() for item in benchmark.tokens]
        if benchmark_tokens != [item.to_dict() for item in tokens]:
            raise SubtitleContractError("subtitle aligned tokens do not match benchmark tokens")
    return value
