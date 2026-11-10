import unittest

from benchmark_av_sync.contracts import AlignmentToken, BenchmarkGroup, BenchmarkScript
from benchmark_av_sync.subtitle_contract import (
    SubtitleContractError,
    build_subtitle_contract,
    select_caption_source,
    validate_subtitle_contract,
)


def _tokens(text: str) -> list[AlignmentToken]:
    return [
        AlignmentToken(index, char, index * 0.2, (index + 1) * 0.2)
        for index, char in enumerate(text)
    ]


def _benchmark(text: str) -> BenchmarkScript:
    tokens = _tokens(text)
    return BenchmarkScript(
        pair_id="01",
        text=text,
        reference_duration=max(1.0, len(text) * 0.2),
        speech_start=0.0,
        speech_end=len(text) * 0.2,
        tokens=tokens,
        boundaries=[],
        groups=[
            BenchmarkGroup(
                "g00",
                text,
                0,
                len(tokens) - 1,
                0.0,
                len(text) * 0.2,
                "c00",
            )
        ],
    )


class SubtitleContractTests(unittest.TestCase):
    def test_visual_subtitle_text_wins_over_conflicting_asr(self):
        ocr = {
            "entries": [
                {"text": "依法维护你的权益", "confidence": 0.99, "start": 0.2, "end": 2.0}
            ],
            "text": "依法维护你的权益",
            "scan": {"completed": True, "sampled_frames": 8, "stable_event_count": 1},
        }
        asr = {"text": "依法维护您的权益"}
        selected = select_caption_source(ocr, asr)
        self.assertEqual(selected["source_mode"], "visual_subtitle_fused")
        self.assertEqual(selected["canonical_caption_text"], "依法维护你的权益")
        self.assertTrue(selected["conflicts"])

    def test_no_visual_subtitle_requires_completed_scan(self):
        with self.assertRaisesRegex(SubtitleContractError, "completed visual scan"):
            select_caption_source({"entries": [], "text": ""}, {"text": "只有声音"})

    def test_no_visual_subtitle_uses_audio_semantic_mode(self):
        selected = select_caption_source(
            {
                "entries": [],
                "text": "",
                "scan": {"completed": True, "sampled_frames": 10, "stable_event_count": 0},
            },
            {"text": "第一句话。第二句话。"},
        )
        self.assertEqual(selected["source_mode"], "audio_semantic")
        self.assertEqual(selected["canonical_caption_text"], "第一句话第二句话")

    def test_low_confidence_ocr_false_positive_does_not_override_asr(self):
        selected = select_caption_source(
            {
                "entries": [
                    {"text": "は士", "confidence": 0.32, "start": 4.0, "end": 4.5}
                ],
                "text": "は士",
                "scan": {"completed": True, "sampled_frames": 15, "stable_event_count": 1},
            },
            {"text": "今天我要为普通人讨回公道。"},
        )
        self.assertEqual(selected["source_mode"], "audio_semantic")
        self.assertEqual(selected["canonical_caption_text"], "今天我要为普通人讨回公道")
        self.assertTrue(any("ignored 1 OCR event" in item for item in selected["reasons"]))

    def test_conditional_sentence_approves_semantic_split_before_response(self):
        text = "如果法律都不能给他一个公道"
        ocr = {
            "entries": [
                {"text": text, "confidence": 0.99, "start": 0.0, "end": 3.0}
            ],
            "text": text,
            "scan": {"completed": True, "sampled_frames": 8, "stable_event_count": 1},
        }
        asr = {"text": text + "。"}
        selected = select_caption_source(ocr, asr)
        benchmark = _benchmark(text)
        contract = build_subtitle_contract(
            pair_id="01",
            reference_video_sha256="1" * 64,
            reference_audio_sha256="2" * 64,
            reference_duration=benchmark.reference_duration,
            selected=selected,
            aligned_tokens=benchmark.tokens,
        )
        points = {item["token_end"]: item for item in contract["approved_split_points"]}
        self.assertIn(3, points)
        self.assertEqual(points[3]["left_text"], "如果法律")
        self.assertEqual(points[3]["right_text"], "都不能给他一个公道")
        validate_subtitle_contract(contract, benchmark)

    def test_contract_rejects_caption_text_drift(self):
        text = "测试字幕"
        selected = select_caption_source(
            {
                "entries": [
                    {"text": text, "confidence": 0.99, "start": 0.0, "end": 1.0}
                ],
                "text": text,
                "scan": {"completed": True, "sampled_frames": 4, "stable_event_count": 1},
            },
            {"text": text},
        )
        benchmark = _benchmark(text)
        contract = build_subtitle_contract(
            pair_id="01",
            reference_video_sha256="1" * 64,
            reference_audio_sha256="2" * 64,
            reference_duration=benchmark.reference_duration,
            selected=selected,
            aligned_tokens=benchmark.tokens,
        )
        contract["caption_events"][0]["text"] = "错误字幕"
        with self.assertRaisesRegex(SubtitleContractError, "caption events"):
            validate_subtitle_contract(contract, benchmark)


if __name__ == "__main__":
    unittest.main()
