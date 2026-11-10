import unittest

from benchmark_av_sync.cli import _parser


class CliSmokeTests(unittest.TestCase):
    def test_subcommands_exist(self):
        parser = _parser()
        args = parser.parse_args(
            [
                "verify-plan",
                "--plan",
                "plan.json",
            ]
        )
        self.assertEqual(args.command, "verify-plan")

    def test_run_sample_subcommand_exists(self):
        parser = _parser()
        args = parser.parse_args(
            [
                "run-sample",
                "--pair-id", "010203",
                "--reference-dir", "reference",
                "--generated-dir", "generated",
                "--batch-dir", "batch",
                "--ffmpeg", "ffmpeg.exe",
                "--ffprobe", "ffprobe.exe",
                "--qwen-python", "python.exe",
                "--aligner-model", "aligner",
                "--asr-model", "asr",
            ]
        )
        self.assertEqual(args.pair_id, "010203")

    def test_run_visual_sample_subcommand_requires_mouth_python(self):
        parser = _parser()
        args = parser.parse_args(
            [
                "run-visual-sample",
                "--pair-id", "010203",
                "--reference-dir", "reference",
                "--generated-dir", "generated",
                "--batch-dir", "batch",
                "--ffmpeg", "ffmpeg.exe",
                "--ffprobe", "ffprobe.exe",
                "--qwen-python", "qwen-python.exe",
                "--mouth-python", "mouth-python.exe",
                "--aligner-model", "aligner",
                "--asr-model", "asr",
                "--approved-hard-speed-max", "1.9",
            ]
        )
        self.assertEqual(args.command, "run-visual-sample")
        self.assertEqual(str(args.mouth_python), "mouth-python.exe")
        self.assertEqual(args.approved_hard_speed_max, 1.9)

    def test_two_stage_subcommands_exist(self):
        parser = _parser()
        package_args = parser.parse_args(
            [
                "build-reference-package",
                "--pair-id", "01",
                "--reference-video", "001.mp4",
                "--output", "01_reference_bundle.zip",
                "--work-dir", "work",
                "--ffmpeg", "ffmpeg.exe",
                "--ffprobe", "ffprobe.exe",
                "--qwen-python", "qwen-python.exe",
                "--aligner-model", "aligner",
                "--asr-model", "asr",
            ]
        )
        self.assertEqual(package_args.command, "build-reference-package")
        online_args = parser.parse_args(
            [
                "render-from-package",
                "--package", "01_reference_bundle.zip",
                "--generated-video", "01.mp4",
                "--output-dir", "result",
            ]
        )
        self.assertEqual(online_args.command, "render-from-package")
        self.assertEqual(parser.parse_args(["online-preflight"]).command, "online-preflight")


if __name__ == "__main__":
    unittest.main()
