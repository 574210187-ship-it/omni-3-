import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_stage2_release.py"


def _load_builder():
    spec = importlib.util.spec_from_file_location("stage2_release_builder", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StrictReleaseBuilderTests(unittest.TestCase):
    def test_builder_emits_only_current_strict_entry_material(self):
        builder = _load_builder()
        with tempfile.TemporaryDirectory() as temp:
            result = builder.build_release(
                project_root=ROOT,
                output_dir=Path(temp),
                production_commit="a" * 40,
            )
            archive = result["archive"]
            manifest = json.loads(result["manifest"].read_text(encoding="utf-8"))
            self.assertEqual(manifest["release_id"], "omni-stage2-strict/v0.8.3")
            self.assertEqual(manifest["contract"], "omni-stage2-contract/v4")
            self.assertEqual(manifest["source_archive"]["sha256"], builder._sha256(archive))
            self.assertEqual(manifest["runner"]["sha256"], manifest["source_archive"]["sha256"])
            self.assertEqual(manifest["entrypoint"], "omni-stage2 run-row --job <absolute-job.json>")
            self.assertEqual(manifest["strict_runtime"]["caption_quality_min_ssim"], 0.995)
            with zipfile.ZipFile(archive) as package:
                names = set(package.namelist())
                self.assertIn("docs/OMNI_TWO_RUNNERS.md", names)
                self.assertIn("scripts/build_stage2_release.py", names)
                self.assertIn("src/benchmark_av_sync/omni_stage2_strict_release.json", names)
                self.assertNotIn("docs/OMNI_STAGE2_SYNC_V077.md", names)
                self.assertNotIn("docs/OMNI_STAGE2_SYNC_V078_RELAXED_HOLD.md", names)
                two_runners = package.read("docs/OMNI_TWO_RUNNERS.md").decode("utf-8")
                self.assertIn("omni-stage2-strict/v0.8.3", two_runners)
                self.assertNotIn("版本：0.6.0", two_runners)


if __name__ == "__main__":
    unittest.main()
