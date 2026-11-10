import math
import unittest

from benchmark_av_sync.mouth_track import analyze_mouth_frames


class MouthTrackTests(unittest.TestCase):
    def test_detects_main_activity_without_using_audio(self):
        fps = 20.0
        frames = []
        for index in range(80):
            time = index / fps
            active = 1.0 <= time <= 3.0
            frames.append(
                {
                    "time": time,
                    "openness": 0.08 + (0.04 * math.sin(index) if active else 0.0),
                    "motion": 0.02 if active else 0.0002,
                    "face_confidence": 1.0,
                }
            )
        track = analyze_mouth_frames("01", frames, duration=4.0, fps=fps)
        self.assertLessEqual(track.speech_start, 1.2)
        self.assertGreaterEqual(track.speech_end, 2.8)
        self.assertEqual(track.status, "pass")

    def test_low_face_coverage_fails(self):
        frames = [
            {
                "time": index / 10,
                "openness": 0.1,
                "motion": 0.02 if 5 <= index <= 12 else 0.0,
                "face_confidence": 1.0 if index < 10 else 0.0,
            }
            for index in range(20)
        ]
        track = analyze_mouth_frames("01", frames, duration=2.0, fps=10.0)
        self.assertEqual(track.status, "fail")

    def test_quiet_tail_of_long_utterance_remains_inside_speech_span(self):
        fps = 20.0
        frames = []
        for index in range(200):
            if index < 10 or index >= 195:
                motion = 0.0002
            elif index >= 165:
                motion = 0.008
            else:
                motion = 0.02
            frames.append(
                {
                    "time": index / fps,
                    "openness": 0.08 + 0.02 * math.sin(index / 2),
                    "motion": motion,
                    "face_confidence": 1.0,
                }
            )
        track = analyze_mouth_frames("03", frames, duration=10.0, fps=fps)
        self.assertGreaterEqual(track.speech_end, 9.5)


if __name__ == "__main__":
    unittest.main()
