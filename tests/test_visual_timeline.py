from array import array
from pathlib import Path
import tempfile
import unittest
import wave

from benchmark_av_sync.benchmark_json import build_benchmark_script
from benchmark_av_sync.contracts import AlignmentToken, MouthFrame, MouthTrack
from benchmark_av_sync.verify import verify_plan
from benchmark_av_sync.visual_timeline import VisualSolverConfig, solve_visual_timeline


def benchmark(duration=2.0):
    tokens = [
        AlignmentToken(0, "甲", 0.1, 0.6),
        AlignmentToken(1, "乙", 0.6, 1.1),
        AlignmentToken(2, "丙", 1.1, duration - 0.1),
    ]
    return build_benchmark_script("01", "甲乙丙", tokens, reference_duration=duration)


def mouth(start, end, duration=3.0, fps=20.0):
    frames = [
        MouthFrame(index / fps, 0.1, 0.02, 0.02, 1.0, start <= index / fps <= end)
        for index in range(round(duration * fps))
    ]
    return MouthTrack(
        pair_id="01",
        duration=duration,
        fps=fps,
        frames=frames,
        speech_start=start,
        speech_end=end,
        active_segments=[[start, end]],
        low_motion_candidates=[],
        quality=1.0,
    )


class VisualTimelineTests(unittest.TestCase):
    def test_allows_local_slow_motion(self):
        plan = solve_visual_timeline(benchmark(2.0), mouth(0.5, 2.25))
        speeds = [item.speed for item in plan.video_segments if "mouth activity" in item.reason]
        self.assertTrue(any(value < 1.0 for value in speeds))
        self.assertTrue(verify_plan(plan).passed)

    def test_keeps_reference_audio_as_one_continuous_chunk(self):
        plan = solve_visual_timeline(benchmark(2.0), mouth(0.3, 2.2))
        self.assertEqual(len(plan.audio_chunks), 1)
        self.assertEqual(plan.audio_chunks[0].fade_seconds, 0.0)
        self.assertEqual(plan.metadata["audio_chunk_policy"], "single continuous block")

    def test_speed_outside_hard_limit_fails(self):
        plan = solve_visual_timeline(benchmark(2.0), mouth(0.2, 2.9))
        self.assertEqual(plan.status, "fail")
        self.assertFalse(verify_plan(plan).passed)

    def test_explicit_speed_exception_is_reviewable_and_verifiable(self):
        plan = solve_visual_timeline(
            benchmark(2.0),
            mouth(0.2, 2.9),
            config=VisualSolverConfig(hard_speed_max=1.9),
        )
        self.assertEqual(plan.status, "review")
        self.assertTrue(plan.metadata["speed_exception_authorized"])
        self.assertTrue(verify_plan(plan).passed)

    def test_v32_long_audio_pause_without_closed_mouth_fails(self):
        script = build_benchmark_script(
            "001",
            "甲乙",
            [
                AlignmentToken(0, "甲", 0.40, 0.90),
                AlignmentToken(1, "乙", 2.40, 2.90),
            ],
            reference_duration=3.0,
        )
        continuous = mouth(0.35, 2.95, duration=3.2)
        subtitle = {
            "schema_version": 3,
            "caption_events": [
                {
                    "event_id": "c000",
                    "text": "甲",
                    "token_start": 0,
                    "token_end": 0,
                    "source_start": 0.40,
                    "source_end": 0.90,
                },
                {
                    "event_id": "c001",
                    "text": "乙",
                    "token_start": 1,
                    "token_end": 1,
                    "source_start": 2.40,
                    "source_end": 2.90,
                },
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            reference_audio = Path(directory) / "reference.wav"
            samples = array("h")
            for index in range(3 * 16_000):
                second = index / 16_000
                # The 0.95--2.35s region is actual PCM silence, rather than
                # merely a semantic gap between the two caption events.
                samples.append(0 if 0.95 <= second <= 2.35 else 8_000)
            with wave.open(str(reference_audio), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(samples.tobytes())
            plan = solve_visual_timeline(
                script,
                continuous,
                subtitle=subtitle,
                reference_audio=reference_audio,
            )
        self.assertEqual(plan.status, "fail")
        self.assertIn("SYNC_UNSOLVABLE", " ".join(plan.reasons))
        self.assertFalse(verify_plan(plan).passed)

    def test_v32_authorized_pause_hold_uses_low_open_frame(self):
        script = build_benchmark_script(
            "001",
            "甲乙",
            [
                AlignmentToken(0, "甲", 0.40, 0.90),
                AlignmentToken(1, "乙", 2.40, 2.90),
            ],
            reference_duration=3.0,
        )
        continuous = mouth(0.35, 2.30, duration=2.8)
        subtitle = {
            "schema_version": 3,
            "caption_events": [
                {"event_id": "c000", "text": "甲", "token_start": 0, "token_end": 0,
                 "source_start": 0.40, "source_end": 0.90},
                {"event_id": "c001", "text": "乙", "token_start": 1, "token_end": 1,
                 "source_start": 2.40, "source_end": 2.90},
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            reference_audio = Path(directory) / "reference.wav"
            samples = array("h")
            for index in range(3 * 16_000):
                second = index / 16_000
                samples.append(0 if 0.95 <= second <= 2.35 else 8_000)
            with wave.open(str(reference_audio), "wb") as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16_000)
                output.writeframes(samples.tobytes())
            plan = solve_visual_timeline(
                script,
                continuous,
                subtitle=subtitle,
                reference_audio=reference_audio,
                config=VisualSolverConfig(
                    hard_speed_max=1.9,
                    allow_pause_hold=True,
                    pause_hold_max_seconds=2.0,
                ),
            )
        self.assertEqual(plan.status, "review")
        self.assertTrue(plan.metadata["pause_hold_authorized"])
        self.assertEqual(plan.metadata["pause_map"][0]["render_mode"], "freeze_low_open_frame")
        self.assertTrue(verify_plan(plan).passed)


if __name__ == "__main__":
    unittest.main()
