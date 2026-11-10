import unittest

from benchmark_av_sync.alignment import clean_alignment, pair_stable_tokens


class AlignmentTests(unittest.TestCase):
    def test_zero_duration_is_not_stable(self):
        qc = clean_alignment(
            [
                {"text": "你", "start": 0.1, "end": 0.3},
                {"text": "好", "start": 0.4, "end": 0.4},
            ],
            audio_duration=1.0,
            zero_ratio_fail=0.6,
        )
        self.assertEqual(qc.status, "review")
        self.assertTrue(qc.tokens[0].is_stable)
        self.assertFalse(qc.tokens[1].is_stable)

    def test_many_zero_duration_items_fail(self):
        qc = clean_alignment(
            [
                {"text": "你", "start": 0.1, "end": 0.1},
                {"text": "好", "start": 0.2, "end": 0.2},
            ],
            audio_duration=1.0,
        )
        self.assertEqual(qc.status, "fail")

    def test_pairing_uses_index_and_text(self):
        ref = clean_alignment([{"index": 0, "text": "你", "start": 0.1, "end": 0.3}], audio_duration=1).tokens
        gen = clean_alignment([{"index": 0, "text": "你", "start": 0.2, "end": 0.5}], audio_duration=1).tokens
        self.assertEqual(len(pair_stable_tokens(ref, gen)), 1)


if __name__ == "__main__":
    unittest.main()
