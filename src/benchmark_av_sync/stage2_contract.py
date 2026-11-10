"""Shared, fail-closed contracts for Omni Stage2 production.

The renderer, row selector, and remote publisher used to carry parts of this
contract independently.  That made it possible for a locally successful
render to be reported as a production completion even when the lifecycle,
runtime receipt, review, or remote readback evidence was missing.  This module
is intentionally dependency-light so every layer can validate the same data.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
CONTRACT_VERSION = "omni-stage2-contract/v4"
RUNTIME_RECEIPT_SCHEMA = "omni-stage2-runtime-receipt/v2"
STRICT_RELEASE_SCHEMA = "omni-stage2-release/v1"
STRICT_RELEASE_ID = "omni-stage2-strict/v0.8.3"
STRICT_PACKAGE_VERSION = "0.8.3"

# These are the values stated by the production runbook and caption style
# contract.  They are deliberately single-sourced here so the runner cannot
# silently use a wider threshold than the written acceptance rule.
CAPTION_QUALITY_MIN_SSIM = 0.995
CAPTION_QUALITY_MIN_PSNR = 45.0
CAPTION_FONT_FAMILY = "Noto Sans CJK SC"
CAPTION_FONT_FULL_NAME = "Noto Sans CJK SC Bold"

RUNTIME_PROVENANCE_BY_MODE = {
    "reused": "verified_existing_runtime",
    "restored": "verified_offline_restore",
    "reconstructed": "user_authorized_reconstruction",
}
AUTOMATED_REVIEW_METHOD = "authorized_automated_audit"
MANUAL_REVIEW_METHOD = "manual_full_playback"
NO_REVIEW_METHOD = "not_required"
PENDING_REVIEW_METHOD = "pending"

DELIVERY_SPECS: dict[str, tuple[str, str]] = {
    "segments": ("素材2", "segments.zip"),
    "complete": ("素材2", "complete.mp4"),
    "complete_captioned": ("素材3", "complete_captioned.mp4"),
    "evidence": ("素材2", "evidence.zip"),
}

SOURCE_BINDING_ENV = "OMNI_STAGE2_SOURCE_BINDING_JSON"
SOURCE_BINDING_KEYS = ("provider", "app_token", "table_id", "view_name", "view_id")
_PUBLIC_SOURCE_BINDING = {
    "provider": "feishu_bitable",
    "app_token": "<configure-app-token>",
    "table_id": "<configure-table-id>",
    "view_name": "<configure-view-name>",
    "view_id": "<configure-view-id>",
}


def _configured_source_binding() -> dict[str, str]:
    """Load a deployment-specific source binding without shipping one publicly.

    The private production deployment binds selection to one exact Bitable
    source. The public edition deliberately contains no customer identifiers:
    set ``OMNI_STAGE2_SOURCE_BINDING_JSON`` before invoking selection to supply
    an object with the five ``SOURCE_BINDING_KEYS`` fields. Invalid or absent
    configuration remains a non-runnable placeholder and fails closed later in
    ``validate_source_binding``.
    """

    raw = os.environ.get(SOURCE_BINDING_ENV)
    if not raw:
        return dict(_PUBLIC_SOURCE_BINDING)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return dict(_PUBLIC_SOURCE_BINDING)
    if not isinstance(value, dict):
        return dict(_PUBLIC_SOURCE_BINDING)
    binding = {key: value.get(key) for key in SOURCE_BINDING_KEYS}
    if all(isinstance(item, str) and item.strip() for item in binding.values()):
        return {key: str(binding[key]) for key in SOURCE_BINDING_KEYS}
    return dict(_PUBLIC_SOURCE_BINDING)


SOURCE_BINDING = _configured_source_binding()


class Stage2ContractError(ValueError):
    """Raised when a production contract object is incomplete or inconsistent."""


def _require_sha(value: Any, *, name: str) -> str:
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value.lower()):
        raise Stage2ContractError(f"{name} must be a 64-character SHA-256")
    return value.lower()


def validate_strict_release(value: Any) -> dict[str, str]:
    """Validate the release identity carried by every executable job and result.

    A label such as "v0.7.8" was previously allowed to drift across documents,
    source files, and runtime packages.  This compact value is intentionally
    required in the job, result, evidence manifest, and completion event.
    """

    if not isinstance(value, dict):
        raise Stage2ContractError("strict_release must be an object")
    if value.get("release_id") != STRICT_RELEASE_ID:
        raise Stage2ContractError("strict_release.release_id is missing or stale")
    if value.get("contract") != CONTRACT_VERSION:
        raise Stage2ContractError("strict_release.contract is missing or stale")
    if value.get("package_version") != STRICT_PACKAGE_VERSION:
        raise Stage2ContractError("strict_release.package_version is missing or stale")
    manifest_sha256 = _require_sha(
        value.get("manifest_sha256"), name="strict_release.manifest_sha256"
    )
    return {
        "release_id": STRICT_RELEASE_ID,
        "contract": CONTRACT_VERSION,
        "package_version": STRICT_PACKAGE_VERSION,
        "manifest_sha256": manifest_sha256,
    }


def stage2_key(dispatch_key: str, attempt: int) -> str:
    if not isinstance(dispatch_key, str) or not dispatch_key.strip():
        raise Stage2ContractError("dispatch_key must be non-empty")
    if not isinstance(attempt, int) or attempt not in {1, 2, 3}:
        raise Stage2ContractError("attempt must be 1, 2, or 3")
    return f"{dispatch_key}|attempt-{attempt}"


def upload_idempotency_key(dispatch_key: str, artifact_key: str, sha256: str) -> str:
    """Return the connector-safe idempotency key for one immutable artifact.

    The external append endpoint permits at most 128 characters.  The prior
    literal ``dispatch_key|artifact|sha256`` encoding exceeded that bound for
    valid production dispatch keys.  Keep the full artifact SHA and derive a
    stable 16-character scope from the full dispatch key; the completion event
    carries the original dispatch key separately for audit and verification.
    Retries must reuse this exact value; timestamps or retry suffixes are not
    permitted.
    """

    if artifact_key not in DELIVERY_SPECS:
        raise Stage2ContractError(f"unsupported delivery artifact: {artifact_key}")
    digest = _require_sha(sha256, name=f"{artifact_key}.sha256")
    if not isinstance(dispatch_key, str) or not dispatch_key.strip():
        raise Stage2ContractError("dispatch_key must be non-empty")
    scope = hashlib.sha256(dispatch_key.encode("utf-8")).hexdigest()[:16]
    key = f"omni2|{scope}|{artifact_key}|{digest}"
    if len(key) > 128:
        raise Stage2ContractError("canonical upload idempotency key exceeds connector limit")
    return key


def validate_source_binding(value: Any) -> tuple[bool, str | None]:
    if not isinstance(value, dict):
        return False, "source_binding is missing"
    for key, expected in SOURCE_BINDING.items():
        if value.get(key) != expected:
            return False, f"source_binding.{key} must equal {expected!r}"
    return True, None


def validate_stage2_started_receipt(
    value: Any,
    *,
    record_id: str,
    dispatch_key: str,
    input_fingerprint: str,
    strict_release: dict[str, str],
    attempt: int,
) -> dict[str, Any]:
    """Validate the remote start receipt as the exact precursor of one job.

    A generic “started” line was previously enough to make a later completion
    look plausible.  It must instead bind the row, immutable inputs, release
    lock and attempt before the runner reads any media.
    """

    if not isinstance(value, dict):
        raise Stage2ContractError("stage2_started_receipt must be an object")
    if value.get("phase") != "stage2_started":
        raise Stage2ContractError("stage2_started_receipt.phase must be stage2_started")
    if value.get("record_id") != record_id:
        raise Stage2ContractError("stage2_started_receipt.record_id does not match job")
    if value.get("dispatch_key") != dispatch_key:
        raise Stage2ContractError("stage2_started_receipt.dispatch_key does not match job")
    expected_fingerprint = _require_sha(
        input_fingerprint, name="expected stage2_started input_fingerprint"
    )
    if value.get("input_fingerprint") != expected_fingerprint:
        raise Stage2ContractError(
            "stage2_started_receipt.input_fingerprint does not match job"
        )
    expected_release = validate_strict_release(strict_release)
    receipt_release = validate_strict_release(value.get("strict_release"))
    if receipt_release != expected_release:
        raise Stage2ContractError("stage2_started_receipt.strict_release does not match job")
    if value.get("attempt") != attempt:
        raise Stage2ContractError("stage2_started_receipt.attempt does not match job")
    if value.get("readback_verified") is not True:
        raise Stage2ContractError(
            "stage2_started_receipt requires readback_verified=true before rendering"
        )
    if not isinstance(value.get("updated_at"), str) or not value["updated_at"].strip():
        raise Stage2ContractError("stage2_started_receipt.updated_at is required")
    return dict(value)


def validate_runtime_receipt(value: Any, *, record_id: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise Stage2ContractError("runtime receipt must be an object")
    if value.get("schema") != RUNTIME_RECEIPT_SCHEMA:
        raise Stage2ContractError(
            "runtime receipt schema is not omni-stage2-runtime-receipt/v2"
        )
    if value.get("record_id") != record_id:
        raise Stage2ContractError("runtime receipt record_id does not match job")
    mode = value.get("mode")
    if mode not in RUNTIME_PROVENANCE_BY_MODE:
        raise Stage2ContractError(
            "runtime receipt mode must be reused, restored, or reconstructed"
        )
    provenance = value.get("provenance")
    if not isinstance(provenance, dict):
        raise Stage2ContractError("runtime receipt provenance must be an object")
    expected_provenance = RUNTIME_PROVENANCE_BY_MODE[mode]
    if provenance.get("kind") != expected_provenance:
        raise Stage2ContractError(
            f"runtime receipt provenance.kind must equal {expected_provenance!r}"
        )
    if mode == "reconstructed":
        if not isinstance(provenance.get("authorization"), str) or not provenance["authorization"].strip():
            raise Stage2ContractError(
                "reconstructed runtime requires explicit authorization text"
            )
        source = provenance.get("source")
        if not isinstance(source, dict):
            raise Stage2ContractError(
                "reconstructed runtime requires a pinned source object"
            )
        commit = source.get("commit")
        if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
            raise Stage2ContractError(
                "reconstructed runtime source.commit must be a 40-character Git SHA"
            )
        _require_sha(
            source.get("release_manifest_sha256"),
            name="reconstructed runtime source.release_manifest_sha256",
        )
    required_flags = (
        "face_mesh_smoke",
        "online_preflight_ok",
        "ffmpeg_ok",
        "ffprobe_ok",
        "minterpolate_ok",
        "libass_ok",
    )
    for key in required_flags:
        if value.get(key) is not True:
            raise Stage2ContractError(f"runtime receipt requires {key}=true")
    versions = value.get("versions")
    if not isinstance(versions, dict):
        raise Stage2ContractError("runtime receipt versions must be an object")
    expected_versions = {
        "python": "3.11.14",
        "mediapipe": "0.10.21",
        "opencv": "4.11.0",
        "numpy": "1.26.4",
    }
    for key, expected in expected_versions.items():
        if str(versions.get(key)) != expected:
            raise Stage2ContractError(
                f"runtime receipt versions.{key} must equal {expected}, got {versions.get(key)!r}"
            )
    if value.get("cjk_bold_font") != CAPTION_FONT_FULL_NAME:
        raise Stage2ContractError(
            f"runtime receipt cjk_bold_font must equal {CAPTION_FONT_FULL_NAME!r}"
        )
    _require_sha(value.get("cjk_bold_font_sha256"), name="runtime receipt cjk_bold_font_sha256")
    if not isinstance(value.get("checked_at"), str) or not value["checked_at"].strip():
        raise Stage2ContractError("runtime receipt checked_at is required")
    return dict(value)


def _delivery_from_result(item: Any, *, artifact_key: str) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise Stage2ContractError(f"delivery {artifact_key} must be an object")
    field_name, _ = DELIVERY_SPECS[artifact_key]
    if item.get("artifact_key") != artifact_key:
        raise Stage2ContractError(f"delivery artifact_key mismatch for {artifact_key}")
    if item.get("field_name") != field_name:
        raise Stage2ContractError(f"delivery field_name mismatch for {artifact_key}")
    name = item.get("name")
    if not isinstance(name, str) or not name:
        raise Stage2ContractError(f"delivery {artifact_key}.name is required")
    size = item.get("size_bytes", item.get("size"))
    if not isinstance(size, int) or size <= 0:
        raise Stage2ContractError(f"delivery {artifact_key}.size_bytes is invalid")
    digest = _require_sha(item.get("sha256"), name=f"delivery {artifact_key}.sha256")
    expected_key = upload_idempotency_key(
        str(item.get("dispatch_key", "")), artifact_key, digest
    ) if item.get("dispatch_key") else None
    normalized = {
        "artifact_key": artifact_key,
        "field_name": field_name,
        "name": name,
        "size_bytes": size,
        "sha256": digest,
    }
    if item.get("idempotency_key") is not None:
        if expected_key is None or item.get("idempotency_key") != expected_key:
            raise Stage2ContractError(
                f"delivery {artifact_key}.idempotency_key is not the canonical key"
            )
        normalized["idempotency_key"] = expected_key
    return normalized


def _normalize_review_receipt(
    value: Any,
    *,
    review_required: bool,
    record_id: str | None = None,
    dispatch_key: str | None = None,
    attempt: int | None = None,
) -> dict[str, Any]:
    """Validate the completion-side review evidence without assuming a human-only path.

    Speed exceptions remain visible in ``review_required``.  They can be closed
    either by a real playback receipt or by a row-bound, explicitly authorized
    automated audit that records each deterministic verification it relied on.
    """

    if not review_required:
        receipt = value or {
            "required": False,
            "completed": True,
            "method": NO_REVIEW_METHOD,
        }
        if (
            not isinstance(receipt, dict)
            or receipt.get("required") is not False
            or receipt.get("completed") is not True
            or receipt.get("method") != NO_REVIEW_METHOD
        ):
            raise Stage2ContractError(
                "non-review completion requires review_receipt=not_required"
            )
        return dict(receipt)

    if value is None:
        return {
            "required": True,
            "completed": False,
            "method": PENDING_REVIEW_METHOD,
        }
    if not isinstance(value, dict):
        raise Stage2ContractError("review_receipt must be an object")
    if value.get("required") is not True or not isinstance(value.get("completed"), bool):
        raise Stage2ContractError(
            "review_receipt requires required=true and boolean completed"
        )
    method = value.get("method")
    if method == PENDING_REVIEW_METHOD:
        if value.get("completed") is not False:
            raise Stage2ContractError("pending review_receipt cannot be completed")
        return dict(value)
    if method == MANUAL_REVIEW_METHOD:
        if not isinstance(value.get("receipt_id"), str) or not value["receipt_id"].strip():
            raise Stage2ContractError("manual review_receipt requires receipt_id")
        return dict(value)
    if method != AUTOMATED_REVIEW_METHOD:
        raise Stage2ContractError("review_receipt method is unsupported")
    authorization = value.get("authorization")
    if not isinstance(authorization, dict):
        raise Stage2ContractError(
            "authorized automated review_receipt requires authorization"
        )
    for key in ("record_id", "dispatch_key", "statement", "authorized_at"):
        if not isinstance(authorization.get(key), str) or not authorization[key].strip():
            raise Stage2ContractError(
                f"authorized automated review_receipt requires authorization.{key}"
            )
    if not isinstance(authorization.get("attempt"), int):
        raise Stage2ContractError(
            "authorized automated review_receipt requires authorization.attempt"
        )
    if record_id is not None and authorization["record_id"] != record_id:
        raise Stage2ContractError(
            "authorized automated review_receipt record_id does not match completion"
        )
    if dispatch_key is not None and authorization["dispatch_key"] != dispatch_key:
        raise Stage2ContractError(
            "authorized automated review_receipt dispatch_key does not match completion"
        )
    if attempt is not None and authorization["attempt"] != attempt:
        raise Stage2ContractError(
            "authorized automated review_receipt attempt does not match completion"
        )
    audit = value.get("audit")
    required_checks = (
        "delivery_sha256",
        "full_decode",
        "audio_payload_match",
        "caption_visual_quality",
        "caption_frame_checks",
    )
    if not isinstance(audit, dict) or any(audit.get(key) is not True for key in required_checks):
        raise Stage2ContractError(
            "authorized automated review_receipt lacks required passing audit checks"
        )
    return dict(value)


def build_stage2_complete_event(
    result: dict[str, Any],
    *,
    remote_readback_verified: bool,
    review_receipt: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the only allowed shape for the publisher's completion event."""

    if not isinstance(result, dict) or not result.get("ok"):
        raise Stage2ContractError("only a successful runner result can produce completion")
    record_id = result.get("record_id")
    dispatch_key = result.get("dispatch_key")
    attempt = result.get("attempt")
    fingerprint = _require_sha(result.get("input_fingerprint"), name="input_fingerprint")
    strict_release = validate_strict_release(result.get("strict_release"))
    if not isinstance(record_id, str) or not record_id:
        raise Stage2ContractError("result.record_id is required")
    if not isinstance(dispatch_key, str) or not dispatch_key:
        raise Stage2ContractError("result.dispatch_key is required")
    if not isinstance(attempt, int) or attempt not in {1, 2, 3}:
        raise Stage2ContractError("result.attempt is invalid")
    pair_results = result.get("pair_results")
    if not isinstance(pair_results, list) or not pair_results:
        raise Stage2ContractError("result.pair_results is required")
    normalized_pairs: list[dict[str, Any]] = []
    review_required = False
    for pair in pair_results:
        if not isinstance(pair, dict) or not isinstance(pair.get("pair_id"), str):
            raise Stage2ContractError("each pair result requires pair_id")
        input_data = pair.get("input")
        output_data = pair.get("output")
        subtitle = pair.get("subtitle")
        if not isinstance(input_data, dict) or not isinstance(output_data, dict):
            raise Stage2ContractError(f"pair {pair['pair_id']} input/output is incomplete")
        _require_sha(input_data.get("reference_package_sha256"), name="pair input reference_package_sha256")
        _require_sha(input_data.get("generated_video_sha256"), name="pair input generated_video_sha256")
        _require_sha(output_data.get("sha256"), name="pair output sha256")
        if not isinstance(subtitle, dict) or not isinstance(subtitle.get("source_mode"), str) or not isinstance(subtitle.get("quality_status"), str):
            raise Stage2ContractError(f"pair {pair['pair_id']} subtitle source/status is incomplete")
        if not isinstance(pair.get("status"), str):
            raise Stage2ContractError(f"pair {pair['pair_id']} status is required")
        required = bool(pair.get("review_required"))
        review_required = review_required or required
        normalized_pairs.append(
            {
                "pair_id": pair["pair_id"],
                "input": {
                    "reference_package_sha256": input_data["reference_package_sha256"].lower(),
                    "generated_video_sha256": input_data["generated_video_sha256"].lower(),
                },
                "output": {"sha256": output_data["sha256"].lower()},
                "max_speed": pair.get("max_speed"),
                "min_speed": pair.get("min_speed"),
                "speed_exception_used": bool(pair.get("speed_exception_used")),
                "pause_hold_used": bool(pair.get("pause_hold_used")),
                "subtitle": {
                    "source_mode": subtitle["source_mode"],
                    "quality_status": subtitle["quality_status"],
                },
                "status": pair["status"],
                "review_required": required,
            }
        )
    deliveries = result.get("deliveries")
    if not isinstance(deliveries, list):
        raise Stage2ContractError("result.deliveries is required")
    by_key = {item.get("artifact_key"): item for item in deliveries if isinstance(item, dict)}
    if set(by_key) != set(DELIVERY_SPECS):
        raise Stage2ContractError("result.deliveries must contain the four canonical artifacts")
    normalized_deliveries = []
    for artifact_key in DELIVERY_SPECS:
        item = dict(by_key[artifact_key])
        item["dispatch_key"] = dispatch_key
        normalized = _delivery_from_result(item, artifact_key=artifact_key)
        normalized["idempotency_key"] = upload_idempotency_key(
            dispatch_key, artifact_key, normalized["sha256"]
        )
        normalized_deliveries.append(normalized)
    review = _normalize_review_receipt(
        review_receipt,
        review_required=review_required or result.get("status") == "review",
        record_id=record_id,
        dispatch_key=dispatch_key,
        attempt=attempt,
    )
    return {
        "contract": CONTRACT_VERSION,
        "strict_release": strict_release,
        "phase": "stage2_complete",
        "schema_version": 1,
        "video_number": result.get("video_number"),
        "record_id": record_id,
        "input_fingerprint": fingerprint,
        "dispatch_key": dispatch_key,
        "stage2_key": stage2_key(dispatch_key, attempt),
        "attempt": attempt,
        "pair_results": normalized_pairs,
        "status": result.get("status"),
        "review_required": review_required,
        "review_receipt": review,
        "deliveries": normalized_deliveries,
        "remote_readback_verified": bool(remote_readback_verified),
        "runtime_receipt_sha256": result.get("runtime_receipt_sha256"),
        "written_at": result.get("written_at"),
    }


def validate_stage2_complete_event(
    event: Any,
    *,
    expected_record_id: str | None = None,
    expected_dispatch_key: str | None = None,
    expected_fingerprint: str | None = None,
    require_remote_readback: bool = True,
    require_review_receipt: bool = True,
) -> tuple[bool, str | None]:
    try:
        if not isinstance(event, dict):
            raise Stage2ContractError("completion event must be an object")
        if event.get("contract") != CONTRACT_VERSION:
            raise Stage2ContractError("completion event contract version is missing or stale")
        validate_strict_release(event.get("strict_release"))
        if event.get("phase") != "stage2_complete":
            raise Stage2ContractError("completion event phase must be stage2_complete")
        if expected_record_id is not None and event.get("record_id") != expected_record_id:
            raise Stage2ContractError("completion event record_id does not match row")
        if expected_dispatch_key is not None and event.get("dispatch_key") != expected_dispatch_key:
            raise Stage2ContractError("completion event dispatch_key does not match input")
        if expected_fingerprint is not None and event.get("input_fingerprint") != expected_fingerprint:
            raise Stage2ContractError("completion event input_fingerprint does not match input")
        _require_sha(event.get("input_fingerprint"), name="completion input_fingerprint")
        stage2_key(event.get("dispatch_key"), event.get("attempt"))
        if event.get("stage2_key") != stage2_key(event["dispatch_key"], event["attempt"]):
            raise Stage2ContractError("completion event stage2_key is not canonical")
        if require_remote_readback and event.get("remote_readback_verified") is not True:
            raise Stage2ContractError("completion event lacks remote_readback_verified=true")
        pairs = event.get("pair_results")
        if not isinstance(pairs, list) or not pairs:
            raise Stage2ContractError("completion event pair_results is missing")
        event_review_required = bool(event.get("review_required"))
        for pair in pairs:
            if not isinstance(pair, dict):
                raise Stage2ContractError("completion event pair result is invalid")
            input_data = pair.get("input")
            output_data = pair.get("output")
            subtitle = pair.get("subtitle")
            _require_sha(input_data.get("reference_package_sha256"), name="pair reference input SHA")
            _require_sha(input_data.get("generated_video_sha256"), name="pair generated input SHA")
            _require_sha(output_data.get("sha256"), name="pair output SHA")
            if not isinstance(subtitle, dict) or not isinstance(subtitle.get("source_mode"), str) or not isinstance(subtitle.get("quality_status"), str):
                raise Stage2ContractError("pair subtitle source/status is missing")
            if "status" not in pair or "review_required" not in pair:
                raise Stage2ContractError("pair status/review_required is missing")
            event_review_required = event_review_required or bool(pair.get("review_required"))
        deliveries = event.get("deliveries")
        if not isinstance(deliveries, list):
            raise Stage2ContractError("completion event deliveries is missing")
        by_key = {item.get("artifact_key"): item for item in deliveries if isinstance(item, dict)}
        if set(by_key) != set(DELIVERY_SPECS):
            raise Stage2ContractError("completion event must list all four delivery artifacts")
        for artifact_key in DELIVERY_SPECS:
            item = dict(by_key[artifact_key])
            item["dispatch_key"] = event["dispatch_key"]
            normalized = _delivery_from_result(item, artifact_key=artifact_key)
            expected_key = upload_idempotency_key(event["dispatch_key"], artifact_key, normalized["sha256"])
            if item.get("idempotency_key") != expected_key:
                raise Stage2ContractError(f"delivery {artifact_key} idempotency key is not canonical")
        review = _normalize_review_receipt(
            event.get("review_receipt"),
            review_required=event_review_required,
            record_id=event.get("record_id"),
            dispatch_key=event.get("dispatch_key"),
            attempt=event.get("attempt"),
        )
        if require_review_receipt and event_review_required and review.get("completed") is not True:
            raise Stage2ContractError(
                "review is required but no completed receipt was supplied"
            )
        return True, None
    except (Stage2ContractError, AttributeError, TypeError, KeyError) as exc:
        return False, str(exc)


def runtime_receipt_sha256(receipt: dict[str, Any]) -> str:
    import json

    encoded = json.dumps(receipt, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


