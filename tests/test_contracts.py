import unittest

from benchmark_av_sync.contracts import AlignmentPlan, AlignmentToken, AudioChunk, ContractError, VideoSegment


class ContractTests(unittest.TestCase):
    def test_round_trip_plan(self):
        plan = AlignmentPlan(
            pair_id="010203",
            generated_duration=2.0,
            reference_duration=1.8,
            trim_start=0.1,
            trim_end=1.9,
            output_duration=1.7,
            video_segments=[VideoSegment(0.1, 1.9, 1.8 / 1.7, 0.0)],
            audio_chunks=[AudioChunk(0.1, 1.7, 0.05)],
        )
        restored = AlignmentPlan.from_dict(plan.to_dict())
        self.assertEqual(restored.pair_id, "010203")
        self.assertAlmostEqual(restored.video_segments[0].output_end, 1.7)

    def test_negative_time_rejected(self):
        with self.assertRaises(ContractError):
            AlignmentToken(0, "字", -0.1, 0.1)

    def test_slow_motion_segment_is_supported(self):
        segment = VideoSegment(0.0, 1.0, 0.9, 0.0)
        self.assertAlmostEqual(segment.output_duration, 1 / 0.9)


if __name__ == "__main__":
    unittest.main()
