from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any


def _extract_text(result: Any) -> tuple[str, float]:
    if not result:
        return "", 0.0
    first = result[0] if isinstance(result, list) else result
    if hasattr(first, "json"):
        raw = first.json
        raw = raw() if callable(raw) else raw
        if isinstance(raw, str):
            raw = json.loads(raw)
        if isinstance(raw, dict):
            raw = raw.get("res", raw)
            texts = raw.get("rec_texts", [])
            scores = raw.get("rec_scores", [])
            return "".join(str(x) for x in texts), min(scores) if scores else 0.0
    if isinstance(first, dict):
        raw = first.get("res", first)
        texts = raw.get("rec_texts", [])
        scores = raw.get("rec_scores", [])
        return "".join(str(x) for x in texts), min(scores) if scores else 0.0
    lines = first if isinstance(first, list) else result
    texts: list[str] = []
    scores: list[float] = []
    for line in lines or []:
        try:
            texts.append(str(line[1][0]))
            scores.append(float(line[1][1]))
        except (IndexError, TypeError, ValueError):
            continue
    return "".join(texts), min(scores) if scores else 0.0


def _stabilize_entries(
    entries: list[dict[str, Any]], *, min_duration: float, video_duration: float
) -> list[dict[str, Any]]:
    stable: list[dict[str, Any]] = []
    for raw in entries:
        item = dict(raw)
        item["start"] = max(0.0, min(float(item["start"]), video_duration))
        item["end"] = max(item["start"], min(float(item["end"]), video_duration))
        if item["end"] - item["start"] + 1e-9 < min_duration:
            continue
        if stable and stable[-1]["text"] == item["text"]:
            stable[-1]["end"] = max(stable[-1]["end"], item["end"])
            stable[-1]["confidence"] = max(stable[-1]["confidence"], item["confidence"])
        else:
            stable.append(item)
    if not stable and entries:
        longest = max(entries, key=lambda x: float(x["end"]) - float(x["start"]))
        item = dict(longest)
        item["start"] = max(0.0, min(float(item["start"]), video_duration))
        item["end"] = max(item["start"], min(float(item["end"]), video_duration))
        stable.append(item)
    return stable


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        video_path = Path(request["video_path"])
        if not video_path.exists():
            raise FileNotFoundError(f"video missing: {video_path}")
        # Paddle 3.x can select an incompatible oneDNN PIR path on some Windows CPUs.
        # Disable it before importing Paddle so OCR remains deterministic and portable.
        os.environ.setdefault("FLAGS_use_mkldnn", "0")
        import cv2
        import numpy as np
        from paddleocr import PaddleOCR

        capture = cv2.VideoCapture(str(video_path))
        if not capture.isOpened():
            raise RuntimeError(f"cannot open video: {video_path}")
        fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
        if fps <= 0:
            raise RuntimeError("video reports invalid frame rate")
        frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        video_duration = frame_count / fps if frame_count > 0 else float("inf")
        sample_interval = float(request.get("sample_interval_seconds", 0.5))
        step = max(1, round(fps * sample_interval))
        difference_threshold = float(request.get("difference_threshold", 6.0))
        box = request.get("subtitle_box")
        ocr = PaddleOCR(
            lang=request.get("language", "ch"),
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
            enable_mkldnn=False,
            device=request.get("device", "cpu"),
        )
        entries: list[dict[str, Any]] = []
        previous_gray = None
        current: dict[str, Any] | None = None
        frame_index = 0
        sampled_frames = 0
        ocr_invocations = 0
        while True:
            capture.set(cv2.CAP_PROP_POS_FRAMES, frame_index)
            ok, frame = capture.read()
            if not ok:
                break
            sampled_frames += 1
            height, width = frame.shape[:2]
            if box:
                x, y, w, h = map(int, box)
                x1, y1 = max(0, x), max(0, y)
                x2, y2 = min(width, x + w), min(height, y + h)
            else:
                x1, y1, x2, y2 = 0, int(height * 0.6), width, height
            roi = frame[y1:y2, x1:x2]
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
            changed = previous_gray is None or float(np.mean(cv2.absdiff(gray, previous_gray))) >= difference_threshold
            timestamp = frame_index / fps
            if changed:
                ocr_invocations += 1
                result = ocr.predict(roi)
                text, confidence = _extract_text(result)
                text = "".join(text.split())
                if current and text == current["text"]:
                    current["end"] = timestamp + sample_interval
                else:
                    if current:
                        entries.append(current)
                    current = (
                        {
                            "text": text,
                            "confidence": confidence,
                            "start": timestamp,
                            "end": timestamp + sample_interval,
                            "representative_frame_index": frame_index,
                            "representative_time": timestamp,
                            "subtitle_box": [x1, y1, x2 - x1, y2 - y1],
                        }
                        if text
                        else None
                    )
            elif current:
                current["end"] = timestamp + sample_interval
            previous_gray = gray
            frame_index += step
        capture.release()
        if current:
            entries.append(current)
        entries = _stabilize_entries(
            entries,
            min_duration=float(request.get("min_stable_seconds", max(0.18, sample_interval * 1.2))),
            video_duration=video_duration,
        )
        payload = {
            "ok": True,
            "entries": entries,
            "text": "".join(x["text"] for x in entries),
            "scan": {
                "completed": True,
                "sample_interval_seconds": sample_interval,
                "difference_threshold": difference_threshold,
                "sampled_frames": sampled_frames,
                "ocr_invocations": ocr_invocations,
                "stable_event_count": len(entries),
                "video_duration": video_duration,
                "default_region": box is None,
            },
        }
    except Exception as exc:
        code = "dependency_missing" if isinstance(exc, (FileNotFoundError, ImportError)) else "worker_failed"
        payload = {"ok": False, "error": code, "exception": type(exc).__name__, "message": str(exc)}
    args.response.parent.mkdir(parents=True, exist_ok=True)
    args.response.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
