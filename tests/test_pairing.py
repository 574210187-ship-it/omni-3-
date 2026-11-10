import tempfile
import unittest
from pathlib import Path

from benchmark_av_sync.pairing import build_pair_manifest, extract_pair_id
from benchmark_av_sync.two_stage import _generated_pair_id


class PairingTests(unittest.TestCase):
    def test_preserves_leading_zero(self):
        self.assertEqual(extract_pair_id(Path("对标_010203.mp4")), "010203")

    def test_multiple_numbers_rejected(self):
        with self.assertRaises(ValueError):
            extract_pair_id(Path("v2_123.mp4"))

    def test_stage2_accepts_prefixed_pair_when_hash_suffix_has_digits(self):
        # Attachment names carry a pair prefix plus a hexadecimal-ish suffix.
        # The generic parser correctly remains strict, while the Stage2
        # expected-ID fallback must select the one numeric token that is the
        # declared pair rather than rejecting a legal filename.
        self.assertEqual(
            _generated_pair_id(Path("04_4fb960b5_1080p.mp4"), "004"), "04"
        )

    def test_exact_pairing_and_orphans(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = root / "ref"
            gen = root / "gen"
            ref.mkdir()
            gen.mkdir()
            (ref / "参考_010203.mp4").write_bytes(b"r")
            (gen / "新片_010203.mp4").write_bytes(b"g")
            (ref / "参考_123.mp4").write_bytes(b"orphan")
            manifest = build_pair_manifest(ref, gen, hash_files=False)
            self.assertEqual([x["pair_id"] for x in manifest["pairs"]], ["010203"])
            self.assertEqual(manifest["errors"][0]["code"], "missing_generated")

    def test_duplicate_id_blocks_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ref = root / "ref"
            gen = root / "gen"
            ref.mkdir()
            gen.mkdir()
            (ref / "a_123.mp4").write_bytes(b"a")
            (ref / "b_123.mov").write_bytes(b"b")
            (gen / "x_123.mp4").write_bytes(b"x")
            manifest = build_pair_manifest(ref, gen, hash_files=False)
            self.assertEqual(manifest["pairs"], [])
            self.assertTrue(any(x["code"] == "duplicate_id" for x in manifest["errors"]))

    def test_three_duplicates_still_block_pair(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ref, gen = root / "ref", root / "gen"
            ref.mkdir()
            gen.mkdir()
            (ref / "a_123.mp4").write_bytes(b"a")
            (ref / "b_123.mov").write_bytes(b"b")
            (ref / "c_123.mkv").write_bytes(b"c")
            (gen / "new_123.mp4").write_bytes(b"g")
            manifest = build_pair_manifest(ref, gen, hash_files=False)
            self.assertEqual(manifest["pairs"], [])
            duplicate = next(x for x in manifest["errors"] if x["code"] == "duplicate_id")
            self.assertEqual(len(duplicate["paths"]), 3)


if __name__ == "__main__":
    unittest.main()
