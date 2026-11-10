#!/usr/bin/env python3
"""Verify the Omni Stage2 strict release is internally coherent.

This is intentionally a release gate, not a rendering test.  It refuses a
package in which the documented values, Python version fields, release manifest,
and runner's fail-closed entry points have drifted apart.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

import sys
sys.path.insert(0, str(SRC))

from benchmark_av_sync import __version__
from benchmark_av_sync.stage2_contract import (
    CAPTION_FONT_FULL_NAME,
    CAPTION_QUALITY_MIN_PSNR,
    CAPTION_QUALITY_MIN_SSIM,
    CONTRACT_VERSION,
    STRICT_PACKAGE_VERSION,
    STRICT_RELEASE_ID,
)
from benchmark_av_sync.stage2_release import validate_installed_release


def require(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(f"STRICT_RELEASE_VERIFY_FAILED: {message}")


def main() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project_version = project["project"]["version"]
    require(project_version == __version__ == STRICT_PACKAGE_VERSION, "package versions differ")

    installed = validate_installed_release()
    require(installed["release_id"] == STRICT_RELEASE_ID, "release id differs")
    require(installed["contract"] == CONTRACT_VERSION, "contract version differs")

    runner = (SRC / "benchmark_av_sync" / "stage2_runner.py").read_text(encoding="utf-8")
    require("validate_installed_release" in runner, "runner does not validate installed release")
    require("validate_job_release" in runner, "runner does not validate job release")
    require("initial_speed_max = job.default_speed_max" in runner, "first pass speed gate widened")
    require(
        "CAPTION_QUALITY_MIN_SSIM = CONTRACT_CAPTION_QUALITY_MIN_SSIM" in runner,
        "runner SSIM is not contract sourced",
    )
    require(
        "CAPTION_QUALITY_MIN_PSNR = CONTRACT_CAPTION_QUALITY_MIN_PSNR" in runner,
        "runner PSNR is not contract sourced",
    )
    require(
        "input_fingerprint=input_fingerprint" in runner
        and "strict_release=strict_release" in runner,
        "runner start receipt is not bound to input and release",
    )
    selection = (SRC / "benchmark_av_sync" / "stage2_selection.py").read_text(
        encoding="utf-8"
    )
    require(
        "validate_installed_release" in selection
        and "strict_release=strict_release" in selection,
        "selector is not bound to the installed release",
    )

    for name in (
        "OMNI_STAGE2_PRODUCTION_RUNBOOK.md",
        "OMNI_STAGE2_CAPTION_STYLE_D.md",
    ):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        require(STRICT_RELEASE_ID in text, f"{name} omits strict release id")
        require(CAPTION_FONT_FULL_NAME in text, f"{name} omits exact caption font")
        require(str(CAPTION_QUALITY_MIN_SSIM) in text, f"{name} omits SSIM threshold")
        require(str(int(CAPTION_QUALITY_MIN_PSNR)) in text, f"{name} omits PSNR threshold")
    runbook = (ROOT / "docs" / "OMNI_STAGE2_PRODUCTION_RUNBOOK.md").read_text(
        encoding="utf-8"
    )
    require(
        "input_fingerprint" in runbook and "stage2_started" in runbook,
        "runbook omits input-bound start receipt",
    )
    require(
        "review_receipt" in runbook,
        "runbook omits the authorized review contract",
    )

    runtime_contract = (ROOT / "docs" / "OMNI_STAGE2_RUNTIME_CONTINUE_PROMPT.md").read_text(
        encoding="utf-8"
    )
    require(
        "mode=reconstructed" in runtime_contract
        and "user_authorized_reconstruction" in runtime_contract,
        "runtime contract omits controlled reconstruction",
    )
    obsolete_network_rule = "不得" + "联网安装"
    require(
        obsolete_network_rule not in runtime_contract,
        "runtime contract still imposes the obsolete offline-only rule",
    )
    contract = (SRC / "benchmark_av_sync" / "stage2_contract.py").read_text(
        encoding="utf-8"
    )
    obsolete_review_field = "manual" + "_review"
    require(
        "authorized_automated_audit" in contract
        and "review_receipt" in contract
        and obsolete_review_field not in contract,
        "completion contract still requires a human-only review field",
    )
    require(
        "review_authorization" in runner and "_review_receipt" in runner,
        "runner does not bind authorized automated review to the job",
    )
    require(
        "omni2|" in contract
        and "hashlib.sha256(dispatch_key.encode(\"utf-8\"))" in contract
        and "exceeds connector limit" in contract,
        "upload idempotency key is not connector-safe",
    )
    require(
        "sha256(dispatch_key)[0:16]" in runbook and "128" in runbook,
        "runbook omits the connector-safe upload idempotency contract",
    )

    for name in (
        "BROWSER_CHATGPT_STAGE2_PROMPT.md",
        "OMNI_STAGE2_RUNTIME_CONTINUE_PROMPT.md",
        "OMNI_TWO_RUNNERS.md",
        "WORKBUDDY_STAGE2_PRODUCTION_PROMPT.md",
    ):
        text = (ROOT / "docs" / name).read_text(encoding="utf-8")
        require(STRICT_RELEASE_ID in text, f"{name} omits strict release id")
        require(CONTRACT_VERSION in text, f"{name} omits strict release contract")
        require(
            "input_fingerprint" in text and "strict_release" in text,
            f"{name} omits release-bound lifecycle fields",
        )
        require("版本：0.6.0" not in text, f"{name} retains legacy production version")

    builder = (ROOT / "scripts" / "build_stage2_release.py").read_text(encoding="utf-8")
    require("STRICT_RELEASE_ID" in builder, "release builder omits strict release id")
    require("STRICT_DOCS" in builder, "release builder has no strict document allowlist")
    require(
        "omni_stage2_strict_release.json" in builder,
        "release builder omits immutable release lock",
    )
    require(
        "OMNI_STAGE2_SYNC_V077.md" not in builder
        and "OMNI_STAGE2_SYNC_V078_RELAXED_HOLD.md" not in builder,
        "release builder still includes historical docs",
    )

    print(
        "STRICT_RELEASE_VERIFY_OK "
        f"release={STRICT_RELEASE_ID} contract={CONTRACT_VERSION} version={__version__} "
        f"manifest={installed['manifest_sha256']}"
    )


if __name__ == "__main__":
    main()
