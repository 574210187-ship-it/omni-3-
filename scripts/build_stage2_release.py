#!/usr/bin/env python3
"""Build the only shippable Omni Stage2 strict release.

The release archive deliberately contains only current strict entry documents.
Historical v0.6/v0.7 documents must never be reintroduced into a production
archive by a convenience build script.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import zipfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from benchmark_av_sync.stage2_contract import (
    CONTRACT_VERSION,
    STRICT_PACKAGE_VERSION,
    STRICT_RELEASE_ID,
)


FIXED_ZIP_TIME = (2026, 11, 7, 0, 0, 0)
STRICT_DOCS = (
    "BROWSER_CHATGPT_STAGE2_PROMPT.md",
    "OMNI_STAGE2_CAPTION_STYLE_D.md",
    "OMNI_STAGE2_RUNTIME_CONTINUE_PROMPT.md",
    "OMNI_STAGE2_PRODUCTION_RUNBOOK.md",
    "OMNI_STAGE2_RUNTIME_BOOTSTRAP.md",
    "OMNI_TWO_RUNNERS.md",
    "WORKBUDDY_STAGE2_PRODUCTION_PROMPT.md",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_name() -> str:
    return f"omni_stage2_strict_v{STRICT_PACKAGE_VERSION.replace('.', '')}_source_final.zip"


def _strict_release_lock(project_root: Path) -> tuple[Path, dict[str, object]]:
    path = project_root / "src" / "benchmark_av_sync" / "omni_stage2_strict_release.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("release_id") != STRICT_RELEASE_ID:
        raise RuntimeError("strict release id differs from source contract")
    if payload.get("contract") != CONTRACT_VERSION:
        raise RuntimeError("strict release contract differs from source contract")
    if payload.get("package_version") != STRICT_PACKAGE_VERSION:
        raise RuntimeError("strict release package version differs from source contract")
    return path, payload


def _members(project_root: Path) -> list[Path]:
    fixed = [
        project_root / "pyproject.toml",
        project_root / "scripts" / "build_stage2_release.py",
        project_root / "scripts" / "verify_strict_release.py",
        project_root / "src" / "benchmark_av_sync" / "omni_stage2_strict_release.json",
    ]
    fixed.extend(project_root / "docs" / name for name in STRICT_DOCS)
    discovered = list((project_root / "src").rglob("*.py")) + list(
        (project_root / "tests").rglob("*.py")
    )
    members = sorted({path.resolve() for path in fixed + discovered})
    missing = [path for path in members if not path.is_file()]
    if missing:
        raise RuntimeError("release member is missing: " + ", ".join(map(str, missing)))
    return members


def _write_member(archive: zipfile.ZipFile, *, project_root: Path, path: Path) -> None:
    relative = path.relative_to(project_root).as_posix()
    info = zipfile.ZipInfo(relative, date_time=FIXED_ZIP_TIME)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    archive.writestr(info, path.read_bytes())


def build_release(
    *,
    project_root: Path,
    output_dir: Path,
    production_commit: str,
) -> dict[str, object]:
    project_root = project_root.resolve()
    output_dir = output_dir.resolve()
    if len(production_commit) != 40 or any(char not in "0123456789abcdef" for char in production_commit):
        raise RuntimeError("production_commit must be a 40-character lowercase Git SHA")
    output_dir.mkdir(parents=True, exist_ok=True)
    release_lock_path, release_lock = _strict_release_lock(project_root)
    archive_path = output_dir / _archive_name()
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for member in _members(project_root):
            _write_member(archive, project_root=project_root, path=member)
    archive = {
        "name": archive_path.name,
        "sha256": _sha256(archive_path),
        "size_bytes": archive_path.stat().st_size,
    }
    manifest = {
        "schema": "omni-stage2-production-release/v2",
        "release_id": STRICT_RELEASE_ID,
        "contract": CONTRACT_VERSION,
        "package_version": STRICT_PACKAGE_VERSION,
        "production_commit": production_commit,
        "source_archive": {"filename": archive["name"], **{k: v for k, v in archive.items() if k != "name"}},
        "runner": dict(archive),
        "entrypoint": "omni-stage2 run-row --job <absolute-job.json>",
        "runtime": {
            "python": "3.11.x",
            "mediapipe": "0.10.21",
            "opencv": "4.11.0",
            "numpy": "1.26.4",
            "ffmpeg_filters": ["minterpolate", "ass"],
            "caption_font_requirement": "Noto Sans CJK SC Bold resolved by libass at weight 700",
        },
        "strict_runtime": {
            "first_pass_speed_max": 1.25,
            "caption_quality_min_ssim": 0.995,
            "caption_quality_min_psnr_db": 45,
        },
        "inner_release_lock": {
            "path": release_lock_path.relative_to(project_root).as_posix(),
            "sha256": _sha256(release_lock_path),
            "files": release_lock["files"],
        },
        "strict_documents": list(STRICT_DOCS),
    }
    manifest_path = output_dir / "OMNI_STAGE2_RELEASE.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"archive": archive_path, "manifest": manifest_path, "payload": manifest}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--production-commit", required=True)
    args = parser.parse_args()
    result = build_release(
        project_root=ROOT,
        output_dir=args.output_dir,
        production_commit=args.production_commit,
    )
    print(json.dumps(result["payload"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
