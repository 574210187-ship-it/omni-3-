import tempfile
import unittest
from pathlib import Path

from benchmark_av_sync.pipeline import environment_report


class PipelineTests(unittest.TestCase):
    def test_environment_report_names_missing_dependency(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            existing = root / "tool.exe"
            existing.write_bytes(b"x")
            missing = root / "missing"
            report = environment_report(
                ffmpeg=existing,
                ffprobe=existing,
                qwen_python=existing,
                aligner_model=missing,
                asr_model=missing,
                ocr_python=existing,
                mouth_python=missing,
            )
        self.assertFalse(report["ok"])
        self.assertFalse(report["checks"]["aligner_model"])
        self.assertFalse(report["checks"]["asr_model"])
        self.assertFalse(report["checks"]["mouth_python"])


if __name__ == "__main__":
    unittest.main()
