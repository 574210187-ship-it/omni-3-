from __future__ import annotations

import json
import subprocess
from fractions import Fraction
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    pass


def _rate(value: str | None) -> float | None:
    if not value or value in {"0/0", "N/A"}:
        return None
    try:
        return float(Fraction(value))
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(path: Path, ffprobe_path: Path) -> dict[str, Any]:
    command = [
        str(ffprobe_path),
        "-v",
        "error",
        "-show_streams",
        "-show_format",
        "-of",
        "json",
        str(path),
    ]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if completed.returncode != 0:
        raise ProbeError(completed.stderr.strip() or f"ffprobe failed for {path}")
    raw = json.loads(completed.stdout)
    streams: list[dict[str, Any]] = []
    for stream in raw.get("streams", []):
        item: dict[str, Any] = {
            "index": stream.get("index"),
            "codec_type": stream.get("codec_type"),
            "codec_name": stream.get("codec_name"),
            "duration": float(stream["duration"]) if stream.get("duration") not in {None, "N/A"} else None,
        }
        if stream.get("codec_type") == "video":
            item.update(
                width=stream.get("width"),
                height=stream.get("height"),
                avg_frame_rate=_rate(stream.get("avg_frame_rate")),
                pix_fmt=stream.get("pix_fmt"),
            )
        elif stream.get("codec_type") == "audio":
            item.update(
                sample_rate=int(stream["sample_rate"]) if stream.get("sample_rate") else None,
                channels=stream.get("channels"),
                channel_layout=stream.get("channel_layout"),
            )
        streams.append(item)
    format_info = raw.get("format", {})
    duration_value = format_info.get("duration")
    return {
        "duration": float(duration_value) if duration_value not in {None, "N/A"} else None,
        "format_name": format_info.get("format_name"),
        "size": int(format_info["size"]) if format_info.get("size") else path.stat().st_size,
        "streams": streams,
    }
