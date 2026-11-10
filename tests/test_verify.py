import unittest

from benchmark_av_sync.contracts import AlignmentPlan, AudioChunk, VideoSegment
from benchmark_av_sync.verify import verify_plan


class VerifyTests(unittest.TestCase):
    def test_gap_in_generated_middle_fails(self):
        plan = AlignmentPlan(
            pair_id="123",
            generated_duration=2.0,
            reference_duration=2.0,
            trim_start=0.1,
            trim_end=1.9,
            output_duration=1.7,
            video_segments=[
                VideoSegment(0.1, 0.9, 1.0, 0.0),
                VideoSegment(1.0, 1.9, 1.0, 0.8),
            ],
            audio_chunks=[AudioChunk(0.2, 1.0, 0.1)],
        )
        report = verify_plan(plan)
        self.assertFalse(report.passed)
        self.assertTrue(any("coverage" in error for error in report.errors))

    def test_speed_below_085_fails(self):
        plan = AlignmentPlan(
            pair_id="123",
            generated_duration=1.0,
            reference_duration=1.5,
            trim_start=0.0,
            trim_end=1.0,
            output_duration=1.25,
            video_segments=[VideoSegment(0.0, 1.0, 0.8, 0.0)],
            audio_chunks=[AudioChunk(0.0, 1.2, 0.0, fade_seconds=0.0)],
            status="fail",
        )
        report = verify_plan(plan)
        self.assertFalse(report.passed)
        self.assertTrue(any("below hard limit" in error for error in report.errors))


if __name__ == "__main__":
    unittest.main()
