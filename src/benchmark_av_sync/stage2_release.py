"""Fail-closed installed-release lock for Omni Stage2.

The release manifest binds the source files that can render, select, and attest
a Stage2 row.  A job must carry the exact manifest digest, so a copied document,
stale package, or hand-edited runner cannot silently run as the current release.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .stage2_contract import (
    STRICT_PACKAGE_VERSION,
    STRICT_RELEASE_ID,
    STRICT_RELEASE_SCHEMA,
    Stage2ContractError,
    validate_strict_release,
)

MANIFEST_FILENAME = "omni_stage2_strict_release.json"
CORE_RELEASE_FILES = (
    "__init__.py",
    "stage2_contract.py",
    "stage2_runner.py",
    "stage2_selection.py",
    "stage2_batch.py",
    "stage2_release.py",
)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _manifest_path() -> Path:
    return Path(__file__).resolve().with_name(MANIFEST_FILENAME)


def validate_installed_release() -> dict[str, str]:
    """Validate the release manifest against the currently imported source."""

    manifest_path = _manifest_path()
    if not manifest_path.is_file():
        raise Stage2ContractError(
            f"strict release manifest is missing beside the runtime: {manifest_path}"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise Stage2ContractError(f"strict release manifest is unreadable: {exc}") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != STRICT_RELEASE_SCHEMA:
        raise Stage2ContractError("strict release manifest schema is missing or stale")
    if manifest.get("release_id") != STRICT_RELEASE_ID:
        raise Stage2ContractError("strict release manifest release_id is missing or stale")
    if manifest.get("contract") is None:
        raise Stage2ContractError("strict release manifest contract is missing")
    if manifest.get("package_version") != STRICT_PACKAGE_VERSION:
        raise Stage2ContractError("strict release manifest package_version is missing or stale")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != set(CORE_RELEASE_FILES):
        raise Stage2ContractError("strict release manifest core file set is invalid")
    package_dir = manifest_path.parent
    for relative in CORE_RELEASE_FILES:
        expected = files.get(relative)
        if not isinstance(expected, str) or len(expected) != 64:
            raise Stage2ContractError(f"strict release manifest hash is invalid: {relative}")
        actual_path = package_dir / relative
        if not actual_path.is_file():
            raise Stage2ContractError(f"strict release core file is missing: {relative}")
        actual = _sha256_file(actual_path)
        if actual != expected.lower():
            raise Stage2ContractError(
                f"strict release core hash mismatch for {relative}: expected {expected}, got {actual}"
            )
    return validate_strict_release(
        {
            "release_id": manifest["release_id"],
            "contract": manifest["contract"],
            "package_version": manifest["package_version"],
            "manifest_sha256": _sha256_file(manifest_path),
        }
    )


def validate_job_release(value: Any, *, installed: dict[str, str]) -> dict[str, str]:
    """Require a job to name exactly the release that is currently installed."""

    declared = validate_strict_release(value)
    if declared != installed:
        raise Stage2ContractError(
            "job strict_release does not match the installed immutable release"
        )
    return declared
