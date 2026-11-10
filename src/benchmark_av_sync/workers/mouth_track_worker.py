from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path


LIP_INDICES = [
    0, 13, 14, 17, 37, 39, 40, 61, 78, 80, 81, 82, 84, 87, 88, 91,
    95, 146, 178, 181, 185, 191, 267, 269, 270, 291, 308, 310, 311,
    312, 314, 317, 318, 321, 324, 375, 402, 405, 409, 415,
]


def _distance(left, right) -> float:
    return math.hypot(left.x - right.x, left.y - right.y)


def run(request: dict) -> dict:
    import cv2
    import mediapipe as mp

    video_path = Path(request["video_path"])
    capture = cv2.VideoCapture(str(video_path))
    if not capture.isOpened():
        raise RuntimeError(f"cannot open video: {video_path}")
    fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
    frame_count = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    if fps <= 0:
        raise RuntimeError("video FPS is unavailable")
    duration = frame_count / fps if frame_count > 0 else 0.0
    frames: list[dict] = []
    previous: list[tuple[float, float]] | None = None
    face_mesh = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=False,
        max_num_faces=1,
        refine_landmarks=True,
        min_detection_confidence=0.5,
        min_tracking_confidence=0.5,
    )
    index = 0
    try:
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = face_mesh.process(rgb)
            time = index / fps
            openness = 0.0
            motion = 0.0
            confidence = 0.0
            if result.multi_face_landmarks:
                landmarks = result.multi_face_landmarks[0].landmark
                width = max(_distance(landmarks[61], landmarks[291]), 1e-6)
                openness = _distance(landmarks[13], landmarks[14]) / width
                center_x = (landmarks[61].x + landmarks[291].x) / 2
                center_y = (landmarks[61].y + landmarks[291].y) / 2
                normalized = [
                    ((landmarks[item].x - center_x) / width, (landmarks[item].y - center_y) / width)
                    for item in LIP_INDICES
                ]
                if previous is not None:
                    motion = sum(
                        math.hypot(point[0] - old[0], point[1] - old[1])
                        for point, old in zip(normalized, previous)
                    ) / len(normalized)
                previous = normalized
                confidence = 1.0
            else:
                previous = None
            frames.append(
                {
                    "time": time,
                    "openness": openness,
                    "motion": motion,
                    "face_confidence": confidence,
                }
            )
            index += 1
    finally:
        face_mesh.close()
        capture.release()
    return {
        "ok": True,
        "fps": fps,
        "duration": duration or (len(frames) / fps),
        "frames": frames,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        response = run(request)
        code = 0
    except Exception as exc:
        response = {"ok": False, "message": str(exc)}
        code = 2
    args.response.parent.mkdir(parents=True, exist_ok=True)
    args.response.write_text(json.dumps(response, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return code


if __name__ == "__main__":
    sys.exit(main())
