import unittest

from benchmark_av_sync.workers.ocr_worker import _stabilize_entries


class OcrWorkerTests(unittest.TestCase):
    def test_drops_short_transition_glitch_and_clamps_duration(self):
        entries = [
            {"text": "测试字幕", "confidence": 0.99, "start": 0.0, "end": 1.21},
            {"text": "第试句话", "confidence": 0.91, "start": 1.20, "end": 1.45},
            {"text": "第二句话", "confidence": 0.99, "start": 1.44, "end": 2.65},
        ]
        result = _stabilize_entries(entries, min_duration=0.30, video_duration=2.48)
        self.assertEqual([item["text"] for item in result], ["测试字幕", "第二句话"])
        self.assertEqual(result[-1]["end"], 2.48)


if __name__ == "__main__":
    unittest.main()
