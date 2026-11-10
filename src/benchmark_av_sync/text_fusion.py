from __future__ import annotations

import re
from typing import Any


_IGNORED = re.compile(r"[\s，。！？；：、,.!?;:'\"“”‘’（）()【】\[\]《》<>…—-]+")


def normalize_text(text: str) -> str:
    return _IGNORED.sub("", text or "")


def _align(a: str, b: str) -> list[tuple[str | None, str | None]]:
    rows, cols = len(a) + 1, len(b) + 1
    score = [[0] * cols for _ in range(rows)]
    for i in range(rows):
        score[i][0] = i
    for j in range(cols):
        score[0][j] = j
    for i in range(1, rows):
        for j in range(1, cols):
            score[i][j] = min(
                score[i - 1][j] + 1,
                score[i][j - 1] + 1,
                score[i - 1][j - 1] + (a[i - 1] != b[j - 1]),
            )
    aligned: list[tuple[str | None, str | None]] = []
    i, j = len(a), len(b)
    while i or j:
        if i and j and score[i][j] == score[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            aligned.append((a[i - 1], b[j - 1]))
            i -= 1
            j -= 1
        elif i and score[i][j] == score[i - 1][j] + 1:
            aligned.append((a[i - 1], None))
            i -= 1
        else:
            aligned.append((None, b[j - 1]))
            j -= 1
    aligned.reverse()
    return aligned


def fuse_texts(
    asr_text: str,
    ocr_text: str,
    *,
    asr_confidence: float = 0.8,
    ocr_confidence: float = 0.8,
) -> dict[str, Any]:
    asr = normalize_text(asr_text)
    ocr = normalize_text(ocr_text)
    if not asr and not ocr:
        raise ValueError("ASR and OCR text are both empty")
    tokens: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for position, (a_char, o_char) in enumerate(_align(asr, ocr)):
        if a_char == o_char and a_char is not None:
            token = {
                "text": a_char,
                "source": "asr+ocr",
                "confidence": min(1.0, (asr_confidence + ocr_confidence) / 2 + 0.1),
                "anchor_eligible": True,
                "conflict": False,
            }
        elif a_char is None:
            token = {
                "text": o_char,
                "source": "ocr",
                "confidence": ocr_confidence * 0.8,
                "anchor_eligible": False,
                "conflict": False,
            }
        elif o_char is None:
            token = {
                "text": a_char,
                "source": "asr",
                "confidence": asr_confidence * 0.8,
                "anchor_eligible": False,
                "conflict": False,
            }
        else:
            choose_asr = asr_confidence >= ocr_confidence
            token = {
                "text": a_char if choose_asr else o_char,
                "source": "asr" if choose_asr else "ocr",
                "confidence": max(asr_confidence, ocr_confidence) * 0.5,
                "anchor_eligible": False,
                "conflict": True,
            }
            conflicts.append(
                {
                    "position": position,
                    "asr": a_char,
                    "ocr": o_char,
                    "chosen": token["text"],
                }
            )
        token["index"] = len(tokens)
        tokens.append(token)
    return {
        "version": 1,
        "text": "".join(str(token["text"]) for token in tokens),
        "tokens": tokens,
        "conflicts": conflicts,
        "sources": {"asr": asr, "ocr": ocr},
    }
