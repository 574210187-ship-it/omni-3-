import unittest

from benchmark_av_sync.text_fusion import fuse_texts, normalize_text


class TextFusionTests(unittest.TestCase):
    def test_normalizes_spacing_and_punctuation(self):
        self.assertEqual(normalize_text("你 好，世界！"), "你好世界")

    def test_agreement_is_anchor_eligible(self):
        result = fuse_texts("你好世界", "你好，世界")
        self.assertEqual(result["text"], "你好世界")
        self.assertTrue(all(x["anchor_eligible"] for x in result["tokens"]))
        self.assertEqual(result["conflicts"], [])

    def test_conflict_is_not_anchor(self):
        result = fuse_texts("你是好人", "你是号人", asr_confidence=0.9, ocr_confidence=0.7)
        self.assertEqual(result["text"], "你是好人")
        self.assertEqual(len(result["conflicts"]), 1)
        self.assertFalse(result["tokens"][2]["anchor_eligible"])


if __name__ == "__main__":
    unittest.main()
