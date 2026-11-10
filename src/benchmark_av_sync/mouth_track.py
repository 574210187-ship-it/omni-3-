from __future__ import annotations

from pathlib import Path
from statistics import median
from typing import Any

from .contracts import MouthFrame, MouthTrack


def _percentile(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * fraction)))
    return ordered[index]


def _moving_average(values: list[float], width: int) -> list[float]:
    if not values:
        return []
    width = max(1, width)
    radius = width // 2
    prefix = [0.0]
    for value in values:
        prefix.append(prefix[-1] + value)
    result: list[float] = []
    for index in range(len(values)):
        left = max(0, index - radius)
        right = min(len(values), index + radius + 1)
        result.append((prefix[right] - prefix[left]) / (right - left))
    return result


def _close_short_gaps(flags: list[bool], maximum: int) -> list[bool]:
    result = list(flags)
    index = 0
    while index < len(result):
        if result[index]:
            index += 1
            continue
        start = index
        while index < len(result) and not result[index]:
            index += 1
        if start > 0 and index < len(result) and index - start <= maximum:
            result[start:index] = [True] * (index - start)
    return result


def _remove_short_runs(flags: list[bool], minimum: int) -> list[bool]:
    result = list(flags)
    index = 0
    while index < len(result):
        if not result[index]:
            index += 1
            continue
        start = index
        while index < len(result) and result[index]:
            index += 1
        if index - start < minimum:
            result[start:index] = [False] * (index - start)
    return result


def _runs(flags: list[bool], times: list[float], frame_duration: float) -> list[list[float]]:
    result: list[list[float]] = []
    index = 0
    while index < len(flags):
        if not flags[index]:
            index += 1
            continue
        start = index
        while index < len(flags) and flags[index]:
            index += 1
        result.append([times[start], times[index - 1] + frame_duration])
    return result


def analyze_mouth_frames(
    pair_id: str,
    raw_frames: list[dict[str, Any]],
    *,
    duration: float,
    fps: float,
) -> MouthTrack:
    if not raw_frames:
        raise ValueError("mouth tracker produced no frames")
    times = [float(item["time"]) for item in raw_frames]
    openness = [max(0.0, float(item.get("openness", 0.0))) for item in raw_frames]
    motion = [max(0.0, float(item.get("motion", 0.0))) for item in raw_frames]
    confidence = [max(0.0, float(item.get("face_confidence", 0.0))) for item in raw_frames]
    smooth_width = max(3, round(fps * 0.15))
    smooth_motion = _moving_average(motion, smooth_width)
    openness_delta = [0.0]
    openness_delta.extend(abs(right - left) for left, right in zip(openness, openness[1:]))
    smooth_delta = _moving_average(openness_delta, smooth_width)
    activity = [left + 0.75 * right for left, right in zip(smooth_motion, smooth_delta)]
    detected_activity = [value for value, conf in zip(activity, confidence) if conf > 0]
    center = median(detected_activity) if detected_activity else 0.0
    baseline = _percentile(detected_activity, 0.10)
    high = _percentile(detected_activity, 0.90)
    # A long utterance can occupy almost the whole clip, which makes the
    # median itself a speech value and hides quieter final words.  Anchor the
    # onset/offset threshold to the low-activity baseline instead.
    threshold = max(0.0015, baseline + 0.20 * max(0.0, high - baseline))
    flags = [conf > 0 and value >= threshold for conf, value in zip(confidence, activity)]
    flags = _close_short_gaps(flags, max(1, round(fps * 0.30)))
    flags = _remove_short_runs(flags, max(2, round(fps * 0.12)))
    frame_duration = 1.0 / fps
    active_segments = _runs(flags, times, frame_duration)
    face_ratio = sum(conf > 0 for conf in confidence) / len(confidence)
    reasons: list[str] = []
    status = "pass"
    if not active_segments:
        raise ValueError("no stable mouth activity detected")
    speech_start = active_segments[0][0]
    speech_end = active_segments[-1][1]
    if face_ratio < 0.75:
        status = "fail"
        reasons.append(f"face tracked on only {face_ratio:.1%} of frames")
    elif face_ratio < 0.92:
        status = "review"
        reasons.append(f"face tracking coverage {face_ratio:.1%} requires review")

    low_motion_candidates: list[float] = []
    minimum_spacing = max(1, round(fps * 0.35))
    last_index = -minimum_spacing
    for index in range(1, len(activity) - 1):
        if times[index] <= speech_start or times[index] >= speech_end:
            continue
        if index - last_index < minimum_spacing:
            continue
        if activity[index] <= activity[index - 1] and activity[index] <= activity[index + 1]:
            if activity[index] <= center:
                low_motion_candidates.append(times[index])
                last_index = index

    frames = [
        MouthFrame(
            time=times[index],
            openness=openness[index],
            motion=smooth_motion[index],
            activity=activity[index],
            face_confidence=confidence[index],
            active=flags[index],
        )
        for index in range(len(times))
    ]
    return MouthTrack(
        pair_id=pair_id,
        duration=duration,
        fps=fps,
        frames=frames,
        speech_start=speech_start,
        speech_end=min(duration, speech_end),
        active_segments=active_segments,
        low_motion_candidates=low_motion_candidates,
        quality=face_ratio,
        status=status,
        reasons=reasons,
    )


def write_activity_plot(track: MouthTrack, path: Path) -> None:
    import cv2
    import numpy as np

    width, height = 1200, 360
    canvas = np.full((height, width, 3), 248, dtype=np.uint8)
    values = [item.activity for item in track.frames]
    maximum = max(values, default=1.0) or 1.0
    points = []
    for frame in track.frames:
        x = round(frame.time / max(track.duration, 1e-9) * (width - 1))
        y = height - 35 - round(frame.activity / maximum * (height - 70))
        points.append((x, y))
    if len(points) >= 2:
        cv2.polylines(canvas, [np.asarray(points, dtype=np.int32)], False, (35, 92, 190), 2)
    for value, color in ((track.speech_start, (20, 150, 20)), (track.speech_end, (20, 20, 190))):
        x = round(value / max(track.duration, 1e-9) * (width - 1))
        cv2.line(canvas, (x, 20), (x, height - 20), color, 2)
    cv2.putText(
        canvas,
        f"{track.pair_id} mouth activity | speech {track.speech_start:.3f}-{track.speech_end:.3f}s | quality {track.quality:.1%}",
        (20, 28),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.65,
        (20, 20, 20),
        1,
        cv2.LINE_AA,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), canvas):
        raise OSError(f"failed to write activity plot: {path}")
