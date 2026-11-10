import unittest
from pathlib import Path

from benchmark_av_sync.contracts import AlignmentPlan, AudioChunk, VideoSegment
from benchmark_av_sync.render import build_ffmpeg_command


class RenderPlanTests(unittest.TestCase):
    def test_command_uses_generated_video_and_reference_audio_only(self):
        plan = AlignmentPlan(
            pair_id="123",
            generated_duration=2.0,
            reference_duration=2.0,
            trim_start=0.1,
            trim_end=1.9,
            output_duration=1.7,
            video_segments=[VideoSegment(0.1, 1.9, 1.8 / 1.7, 0.0)],
            audio_chunks=[AudioChunk(0.2, 1.7, 0.1)],
        )
        command = build_ffmpeg_command(
            plan,
            generated_video=Path("generated.mp4"),
            reference_video=Path("reference.mp4"),
            output_path=Path("out.mp4"),
            ffmpeg_path=Path("ffmpeg.exe"),
        )
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("[0:v]", filters)
        self.assertIn("[1:a]", filters)
        self.assertNotIn("[0:a]", filters)
        self.assertEqual(command[-1], "out.mp4")
        self.assertEqual(command[command.index("-preset") + 1], "slow")
        self.assertEqual(command[command.index("-crf") + 1], "15")

    def test_slow_motion_uses_interpolation_and_continuous_audio_has_no_fade(self):
        plan = AlignmentPlan(
            pair_id="123",
            generated_duration=2.0,
            reference_duration=2.0,
            trim_start=0.0,
            trim_end=1.8,
            output_duration=2.0,
            video_segments=[VideoSegment(0.0, 1.8, 0.9, 0.0)],
            audio_chunks=[AudioChunk(0.0, 2.0, 0.0, fade_seconds=0.0)],
            strategy="mediapipe-mouth-activity",
            output_fps=24.0,
        )
        command = build_ffmpeg_command(
            plan,
            generated_video=Path("generated.mp4"),
            reference_video=Path("reference.mp4"),
            output_path=Path("out.mp4"),
            ffmpeg_path=Path("ffmpeg.exe"),
        )
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("minterpolate=fps=24", filters)
        self.assertNotIn("afade", filters)

    def test_authorized_pause_hold_uses_single_frame_loop(self):
        plan = AlignmentPlan(
            pair_id="123",
            generated_duration=2.0,
            reference_duration=2.0,
            trim_start=0.0,
            trim_end=1.8,
            output_duration=2.0,
            video_segments=[
                VideoSegment(0.0, 0.8, 1.0, 0.0),
                VideoSegment(
                    0.8,
                    0.84,
                    0.04 / 0.6,
                    0.8,
                    reason="stage1 approved visual pause hold",
                ),
                VideoSegment(0.84, 1.8, 0.96 / 0.6, 1.4),
            ],
            audio_chunks=[AudioChunk(0.0, 2.0, 0.0, fade_seconds=0.0)],
            output_fps=24.0,
            metadata={"pause_hold_authorized": True},
        )
        command = build_ffmpeg_command(
            plan,
            generated_video=Path("generated.mp4"),
            reference_video=Path("reference.wav"),
            output_path=Path("out.mp4"),
            ffmpeg_path=Path("ffmpeg"),
        )
        filters = command[command.index("-filter_complex") + 1]
        self.assertIn("loop=loop=-1:size=1:start=0", filters)
        self.assertIn("trim=duration=0.6", filters)


if __name__ == "__main__":
    unittest.main()
