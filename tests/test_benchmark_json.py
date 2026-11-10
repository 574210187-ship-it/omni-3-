import unittest

from benchmark_av_sync.benchmark_json import build_benchmark_script
from benchmark_av_sync.contracts import AlignmentToken


class BenchmarkJsonTests(unittest.TestCase):
    def test_edge_zero_duration_token_is_recovered_into_caption_text(self):
        tokens = [
            AlignmentToken(0, "我", 0.08, 0.08, status="zero_duration"),
            AlignmentToken(1, "的", 0.08, 0.24),
            AlignmentToken(2, "当", 0.24, 0.40),
            AlignmentToken(3, "事", 0.40, 0.56),
            AlignmentToken(4, "人", 0.56, 0.72),
        ]
        script = build_benchmark_script(
            "01", "我的当事人", tokens, reference_duration=0.9
        )
        self.assertEqual("".join(item.text for item in script.tokens), "我的当事人")
        self.assertEqual("".join(item.text for item in script.groups), "我的当事人")
        self.assertTrue(any("recovered leading caption text" in item for item in script.reasons))

    def test_internal_caption_text_mismatch_fails_closed(self):
        tokens = [
            AlignmentToken(0, "甲", 0.0, 0.2),
            AlignmentToken(1, "丙", 0.2, 0.4),
        ]
        script = build_benchmark_script(
            "01", "甲乙丙", tokens, reference_duration=0.6
        )
        self.assertEqual(script.status, "fail")
        self.assertEqual(script.groups, [])

    def test_internal_zero_duration_token_is_recovered_by_aligner_index(self):
        tokens = [
            AlignmentToken(0, "甲", 0.0, 0.2),
            AlignmentToken(1, "乙", 0.2, 0.2, status="zero_duration"),
            AlignmentToken(2, "丙", 0.2, 0.4),
        ]
        script = build_benchmark_script(
            "01", "甲乙丙", tokens, reference_duration=0.6
        )
        self.assertNotEqual(script.status, "fail")
        self.assertEqual("".join(item.text for item in script.tokens), "甲乙丙")
        self.assertEqual("".join(item.text for item in script.groups), "甲乙丙")
        self.assertTrue(
            any("forced-aligner token indexes" in item for item in script.reasons)
        )

    def test_zero_gap_boundaries_are_locked(self):
        tokens = [
            AlignmentToken(0, "还", 0.08, 0.24),
            AlignmentToken(1, "给", 0.24, 0.40),
            AlignmentToken(2, "我", 0.40, 0.48),
        ]
        script = build_benchmark_script(
            "04", "还给我", tokens, reference_duration=0.6
        )
        self.assertTrue(all(not item.cut_allowed for item in script.boundaries))
        self.assertTrue(all(item.pause_ms == 0 for item in script.boundaries))
        self.assertEqual({item.continuity_group_id for item in script.groups}, {"c00"})

    def test_120ms_boundary_is_automatic_and_80ms_is_review(self):
        tokens = [
            AlignmentToken(0, "甲", 0.0, 0.2),
            AlignmentToken(1, "乙", 0.32, 0.5),
            AlignmentToken(2, "丙", 0.58, 0.8),
        ]
        script = build_benchmark_script(
            "01", "甲乙丙", tokens, reference_duration=1.0
        )
        self.assertTrue(script.boundaries[0].cut_allowed)
        self.assertFalse(script.boundaries[0].review)
        self.assertFalse(script.boundaries[1].cut_allowed)
        self.assertTrue(script.boundaries[1].review)


if __name__ == "__main__":
    unittest.main()
