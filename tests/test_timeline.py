import unittest

from benchmark_av_sync.contracts import AlignmentToken
from benchmark_av_sync.timeline import solve_timeline
from benchmark_av_sync.verify import verify_plan


def tokens(spec):
    return [AlignmentToken(index, text, start, end) for index, text, start, end in spec]


class TimelineTests(unittest.TestCase):
    def setUp(self):
        self.reference = tokens(
            [
                (0, "你", 0.20, 0.60),
                (1, "好", 0.65, 1.05),
                (2, "啊", 1.30, 1.70),
            ]
        )
        self.generated = tokens(
            [
                (0, "你", 0.30, 0.74),
                (1, "好", 0.80, 1.24),
                (2, "啊", 1.50, 1.94),
            ]
        )

    def test_pass_plan_has_continuous_middle_coverage(self):
        plan = solve_timeline(
            "010203",
            self.reference,
            self.generated,
            reference_duration=1.9,
            generated_duration=2.1,
        )
        report = verify_plan(plan)
        self.assertTrue(report.passed, report.errors)
        self.assertEqual(plan.status, "pass")
        self.assertAlmostEqual(
            sum(x.source_duration for x in plan.video_segments),
            plan.trim_end - plan.trim_start,
        )
        self.assertLessEqual(max(x.speed for x in plan.video_segments), 1.15)

    def test_review_speed_is_preserved_and_flagged(self):
        generated = tokens([(0, "你", 0.2, 0.92)])
        reference = tokens([(0, "你", 0.2, 0.8)])
        plan = solve_timeline(
            "123", reference, generated, reference_duration=1.0, generated_duration=1.1
        )
        self.assertEqual(plan.status, "review")
        self.assertGreater(max(x.speed for x in plan.video_segments), 1.15)

    def test_single_boundary_quantization_jitter_does_not_trigger_speed(self):
        generated = tokens([(0, "你", 0.2, 0.68)])
        reference = tokens([(0, "你", 0.2, 0.6)])
        plan = solve_timeline(
            "123", reference, generated, reference_duration=0.9, generated_duration=1.0
        )
        self.assertEqual(max(x.speed for x in plan.video_segments), 1.0)

    def test_speed_over_hard_limit_fails(self):
        generated = tokens([(0, "你", 0.2, 0.8)])
        reference = tokens([(0, "你", 0.2, 0.6)])
        plan = solve_timeline(
            "123", reference, generated, reference_duration=0.9, generated_duration=1.0
        )
        self.assertEqual(plan.status, "fail")
        self.assertFalse(verify_plan(plan).passed)

    def test_large_multiword_pace_difference_uses_audio_reflow(self):
        reference = tokens(
            [
                (0, "甲", 0.2, 0.4),
                (1, "乙", 0.4, 0.6),
                (2, "丙", 0.6, 0.8),
                (3, "丁", 0.8, 1.0),
            ]
        )
        generated = tokens(
            [
                (0, "甲", 0.2, 0.5),
                (1, "乙", 0.5, 0.8),
                (2, "丙", 0.8, 1.1),
                (3, "丁", 1.1, 1.4),
            ]
        )
        plan = solve_timeline(
            "123", reference, generated, reference_duration=1.2, generated_duration=1.6
        )
        self.assertNotEqual(plan.status, "fail")
        self.assertLessEqual(max(x.speed for x in plan.video_segments), 1.25)
        self.assertGreater(len(plan.audio_chunks), 1)
        self.assertTrue(verify_plan(plan).passed)


if __name__ == "__main__":
    unittest.main()
