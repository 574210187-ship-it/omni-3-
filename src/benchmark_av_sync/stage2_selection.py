"""Fail-closed Stage2 Bitable row selection.

The batch controller owns Bitable reads and browser scheduling.  This module
does not have Feishu credentials; it turns one fully read row snapshot into a
deterministic decision that can be audited and tested before a normal ChatGPT
task page is created.

Only a verified Stage1 v3.2 input set may enter Stage2.  A matching completed
input is skipped only when all four matching delivery records are still present
on their prescribed fields.  Ambiguous or damaged remote state is a HOLD,
never an invitation to overwrite or delete attachments.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable

from .workspace import atomic_write_json, read_json
from .stage2_contract import (
    DELIVERY_SPECS as CONTRACT_DELIVERY_SPECS,
    validate_source_binding,
    validate_stage2_complete_event,
)
from .stage2_release import validate_installed_release


ROW_SELECTION_SCHEMA_VERSION = 1
STAGE1_EVIDENCE_VERSION = "subtitle-v3.2"
STAGE1_PREFIX = "omni_stage1__v3.2__"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_RECORD_ID = re.compile(r"^[A-Za-z0-9_-]+$")
MAIN_VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"})
RULE_INCOMPATIBLE_ZED_NOTE = "现有规则不适合"
RULE_INCOMPATIBLE_PHASES = frozenset({"stage2_failed", "stage2_failed_final"})

# This is deliberately shared with the runner.  A controller cannot select an
# output to replace in a different field from the one the renderer declares.
DELIVERY_SPECS = CONTRACT_DELIVERY_SPECS


class Stage2SelectionError(ValueError):
    """A controller snapshot is malformed and cannot be safely assessed."""


@dataclass(frozen=True, slots=True)
class Attachment:
    field_name: str
    file_token: str
    name: str
    size_bytes: int
    sha256: str


def append_zed_note(current: Any, marker: str = RULE_INCOMPATIBLE_ZED_NOTE) -> str:
    """Append a deterministic row-note marker without erasing an existing note."""

    if not isinstance(marker, str) or not marker:
        raise Stage2SelectionError("zed note marker must be a non-empty string")
    if current in (None, ""):
        return marker
    if not isinstance(current, str):
        raise Stage2SelectionError("zed备注 must be text before it can be updated")
    if marker in current:
        return current
    separator = "" if current.endswith((" ", "；", ";", "\n")) else "；"
    return f"{current}{separator}{marker}"


def _zed_note_update(snapshot: dict[str, Any]) -> dict[str, Any]:
    fields = snapshot.get("fields")
    if not isinstance(fields, dict):
        raise Stage2SelectionError("snapshot.fields must be an object")
    field_name = "zed备注" if "zed备注" in fields else "ZED备注" if "ZED备注" in fields else "zed备注"
    current = fields.get(field_name, "")
    value = append_zed_note(current)
    return {
        "field_name": field_name,
        "marker": RULE_INCOMPATIBLE_ZED_NOTE,
        "value": value,
        "changed": value != current,
    }


def _normalized_pair(value: str) -> str:
    if not value.isdigit():
        raise Stage2SelectionError(f"pair id is not numeric: {value!r}")
    return f"{int(value):03d}"


def _leading_pair_id(name: str) -> str | None:
    match = re.match(r"^(\d+)(?:$|[\s_.-])", name)
    return _normalized_pair(match.group(1)) if match else None


def _stage1_pair_id(name: str, *, record_id: str) -> str | None:
    if not name.lower().endswith(".zip"):
        return None
    prefix = f"{STAGE1_PREFIX}{record_id}__"
    if not name.startswith(prefix):
        return None
    match = re.match(r"(\d+)(?:$|[\s_.-])", name[len(prefix) : -4])
    return _normalized_pair(match.group(1)) if match else None


def _attachment(value: Any, *, field_name: str, index: int) -> Attachment:
    if not isinstance(value, dict):
        raise Stage2SelectionError(f"{field_name}[{index}] must be an object")
    token = value.get("file_token", value.get("token"))
    name = value.get("name")
    size = value.get("size_bytes", value.get("size"))
    digest = value.get("sha256")
    if not isinstance(token, str) or not token:
        raise Stage2SelectionError(f"{field_name}[{index}] has no file_token")
    if not isinstance(name, str) or not name:
        raise Stage2SelectionError(f"{field_name}[{index}] has no name")
    if not isinstance(size, int) or size <= 0:
        raise Stage2SelectionError(f"{field_name}[{index}] has invalid size_bytes")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest.lower()):
        raise Stage2SelectionError(
            f"{field_name}[{index}] requires a verified 64-character sha256"
        )
    return Attachment(field_name, token, name, size, digest.lower())


def _attachments(snapshot: dict[str, Any], field_name: str) -> tuple[Attachment, ...]:
    fields = snapshot.get("fields")
    if not isinstance(fields, dict):
        raise Stage2SelectionError("snapshot.fields must be an object")
    values = fields.get(field_name, [])
    if values in (None, ""):
        values = []
    if not isinstance(values, list):
        raise Stage2SelectionError(f"fields.{field_name} must be an attachment list")
    return tuple(_attachment(item, field_name=field_name, index=index) for index, item in enumerate(values))


def parse_execution_events(value: Any) -> tuple[dict[str, Any], ...]:
    """Read only complete JSON lines; free text never becomes a completion."""

    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, dict))
    if not isinstance(value, str):
        raise Stage2SelectionError("execution_record must be a JSON event list or text")
    events: list[dict[str, Any]] = []
    for line in value.splitlines():
        line = line.strip()
        if not line.startswith("{") or not line.endswith("}"):
            continue
        try:
            decoded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(decoded, dict):
            events.append(decoded)
    return tuple(events)


def _events(snapshot: dict[str, Any]) -> tuple[dict[str, Any], ...]:
    if "execution_events" in snapshot:
        return parse_execution_events(snapshot["execution_events"])
    fields = snapshot.get("fields")
    if not isinstance(fields, dict):
        raise Stage2SelectionError("snapshot.fields must be an object")
    return parse_execution_events(fields.get("执行记录", ""))


def _event_is_verified_stage1(event: dict[str, Any]) -> bool:
    return (
        event.get("phase") == "stage1_complete"
        and event.get("remote_readback_verified") is True
    )


def _event_matches_input(event: dict[str, Any], *, dispatch_key: str, fingerprint: str) -> bool:
    return event.get("input_fingerprint") == fingerprint or event.get("dispatch_key") == dispatch_key


def _rule_incompatible_reason(event: dict[str, Any]) -> str | None:
    """Return a deterministic rule rejection, never a transient/runtime error."""

    if not isinstance(event, dict):
        return None
    explicit = (
        event.get("rule_incompatible") is True
        or event.get("failure_class") == "rule_incompatible"
        or event.get("error_class") == "rule_incompatible"
    )
    values: list[str] = []
    for container in (event, event.get("renderer_result")):
        if not isinstance(container, dict):
            continue
        for key in ("rule_incompatible_reason", "reason", "error"):
            value = container.get(key)
            if isinstance(value, str) and value:
                values.append(value)
        reasons = container.get("reasons")
        if isinstance(reasons, str):
            values.append(reasons)
        elif isinstance(reasons, list):
            values.extend(str(item) for item in reasons if isinstance(item, (str, int, float)))
    if explicit:
        return values[0] if values else "current Stage2 rules reject this input"
    markers = (
        "sync_unsolvable:",
        "outside 0.85-1.25x",
        "outside 0.85-1.90x",
        "no compatible visual pause",
        "no positive visual span",
        "continuous reference audio extends beyond visual output",
        "continuous benchmark audio extends beyond available video tail",
    )
    for value in values:
        lowered = value.lower()
        if any(marker in lowered for marker in markers):
            return value
    return None


def _event_day(event: dict[str, Any]) -> str | None:
    for key in ("completed_at", "updated_at", "failed_at", "written_at"):
        value = event.get(key)
        if not isinstance(value, str) or not value:
            continue
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).date().isoformat()
        except ValueError:
            continue
    return None


def _input_fingerprint(record_id: str, pairs: Iterable[tuple[str, Attachment, Attachment]]) -> str:
    body = {
        "record_id": record_id,
        "evidence_version": STAGE1_EVIDENCE_VERSION,
        "pairs": [
            {
                "pair_id": pair_id,
                "main_video_sha256": video.sha256,
                "stage1_zip_sha256": package.sha256,
            }
            for pair_id, video, package in pairs
        ],
    }
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _artifact_key_from_name(name: str, *, record_id: str) -> str | None:
    prefix = f"omni_stage2__{record_id}__"
    if not name.startswith(prefix):
        return None
    suffix = name[len(prefix) :]
    for artifact_key, (_, expected) in DELIVERY_SPECS.items():
        if suffix == expected:
            return artifact_key
    # Caption style revisions have a suffix (for example `_d_v073`) but are
    # still the same logical captioned delivery.
    if suffix.startswith("complete_captioned") and suffix.lower().endswith(".mp4"):
        return "complete_captioned"
    return None


def _remote_outputs(record_id: str, attachments: Iterable[Attachment]) -> dict[str, Attachment]:
    grouped: dict[str, list[Attachment]] = {}
    for item in attachments:
        artifact_key = _artifact_key_from_name(item.name, record_id=record_id)
        if artifact_key is None:
            continue
        expected_field, _ = DELIVERY_SPECS[artifact_key]
        if item.field_name == expected_field:
            grouped.setdefault(artifact_key, []).append(item)
    duplicates = sorted(key for key, values in grouped.items() if len(values) > 1)
    if duplicates:
        raise Stage2SelectionError(
            "ambiguous old Stage2 deliveries require manual resolution: " + ", ".join(duplicates)
        )
    return {key: values[0] for key, values in grouped.items()}


def _verified_complete_outputs(
    event: dict[str, Any],
    outputs: dict[str, Attachment],
    *,
    strict_release: dict[str, str],
) -> tuple[bool, str | None]:
    valid, contract_reason = validate_stage2_complete_event(
        event, require_remote_readback=True, require_review_receipt=True
    )
    if not valid:
        return False, contract_reason or "stage2_complete contract validation failed"
    if event.get("strict_release") != strict_release:
        return False, "stage2_complete strict_release does not match the installed release"
    deliveries = event.get("deliveries", event.get("delivery_files"))
    if not isinstance(deliveries, list):
        return False, "stage2_complete has no delivery inventory"
    expected = set(DELIVERY_SPECS)
    listed: dict[str, dict[str, Any]] = {}
    for item in deliveries:
        if not isinstance(item, dict) or item.get("artifact_key") not in expected:
            continue
        listed[str(item["artifact_key"])] = item
    if set(listed) != expected:
        return False, "stage2_complete does not list all four delivery artifacts"
    for artifact_key, (field_name, _) in DELIVERY_SPECS.items():
        output = outputs.get(artifact_key)
        item = listed[artifact_key]
        if output is None:
            return False, f"remote {artifact_key} attachment is missing"
        if item.get("field_name", field_name) != field_name:
            return False, f"stage2_complete has wrong field for {artifact_key}"
        size = item.get("size_bytes", item.get("size"))
        digest = item.get("sha256")
        name = item.get("name", item.get("file_name"))
        if name != output.name or size != output.size_bytes or digest != output.sha256:
            return False, f"remote {artifact_key} does not match stage2_complete inventory"
    return True, None


def _replacement_targets(outputs: dict[str, Attachment]) -> list[dict[str, Any]]:
    values: list[dict[str, Any]] = []
    for artifact_key in DELIVERY_SPECS:
        item = outputs.get(artifact_key)
        if item is None:
            continue
        values.append(
            {
                "artifact_key": artifact_key,
                "field_name": item.field_name,
                "file_token": item.file_token,
                "name": item.name,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
        )
    return values


def _input_manifest(pairs: Iterable[tuple[str, Attachment, Attachment]]) -> list[dict[str, Any]]:
    """Return the immutable byte-level inputs needed for either attempt.

    Retry decisions used to omit this inventory.  That left a controller able
    to identify a legitimate one-time retry but unable to construct the same
    strictly bound job without re-deriving attachment pairing on its own.
    Keep selection as the single source of that pairing contract.
    """

    return [
        {
            "pair_id": pair_id,
            "main_video": {
                "field_name": video.field_name,
                "file_token": video.file_token,
                "name": video.name,
                "size_bytes": video.size_bytes,
                "sha256": video.sha256,
            },
            "stage1_zip": {
                "field_name": package.field_name,
                "file_token": package.file_token,
                "name": package.name,
                "size_bytes": package.size_bytes,
                "sha256": package.sha256,
            },
        }
        for pair_id, video, package in pairs
    ]


def evaluate_row(snapshot: dict[str, Any], *, batch_date: str | None = None) -> dict[str, Any]:
    """Return one of selected_initial/retry_eligible/skip_complete/HOLD states.

    ``batch_date`` controls the once-per-day retry ledger.  It must be an ISO
    calendar date so controllers cannot accidentally reuse yesterday's failure.
    """

    if not isinstance(snapshot, dict):
        raise Stage2SelectionError("row snapshot must be an object")
    if snapshot.get("schema_version", ROW_SELECTION_SCHEMA_VERSION) != ROW_SELECTION_SCHEMA_VERSION:
        raise Stage2SelectionError("unsupported row selection schema_version")
    record_id = snapshot.get("record_id")
    video_number = snapshot.get("video_number")
    if not isinstance(record_id, str) or not SAFE_RECORD_ID.fullmatch(record_id):
        raise Stage2SelectionError("record_id is missing or unsafe")
    if not isinstance(video_number, str) or not video_number.strip():
        raise Stage2SelectionError("video_number is required only to address the chosen row")
    video_number = video_number.strip()
    selected_date = batch_date or date.today().isoformat()
    try:
        date.fromisoformat(selected_date)
    except ValueError as exc:
        raise Stage2SelectionError("batch_date must be YYYY-MM-DD") from exc

    source_ok, source_reason = validate_source_binding(snapshot.get("source_binding"))
    if not source_ok:
        return {
            "status": "hold_source_binding_mismatch",
            "record_id": record_id,
            "video_number": video_number,
            "reason": source_reason,
            "zed_note_used": False,
        }
    try:
        strict_release = validate_installed_release()
    except Exception as exc:
        return {
            "status": "hold_release_lock_invalid",
            "record_id": record_id,
            "video_number": video_number,
            "reason": str(exc),
            "zed_note_used": False,
        }

    try:
        main = _attachments(snapshot, "素材1")
        packages = _attachments(snapshot, "素材2")
        captions = _attachments(snapshot, "素材3")
        events = _events(snapshot)
    except Stage2SelectionError as exc:
        return {"status": "hold_invalid_snapshot", "record_id": record_id, "reason": str(exc)}

    main_by_pair: dict[str, Attachment] = {}
    package_by_pair: dict[str, Attachment] = {}
    errors: list[str] = []
    for item in main:
        if Path(item.name).suffix.lower() not in MAIN_VIDEO_EXTENSIONS:
            continue
        pair_id = _leading_pair_id(item.name)
        if pair_id is None:
            errors.append(f"素材1 main video lacks a leading numeric pair id: {item.name}")
        elif pair_id in main_by_pair:
            errors.append(f"素材1 has duplicate numeric pair id {pair_id}")
        else:
            main_by_pair[pair_id] = item
    for item in packages:
        pair_id = _stage1_pair_id(item.name, record_id=record_id)
        if pair_id is None:
            continue
        if pair_id in package_by_pair:
            errors.append(f"素材2 has duplicate Stage1 v3.2 ZIP pair id {pair_id}")
        else:
            package_by_pair[pair_id] = item
    if not main_by_pair:
        errors.append("素材1 has no numbered main video")
    if not package_by_pair:
        errors.append("素材2 has no matching omni_stage1__v3.2__ Stage1 ZIP")
    if set(main_by_pair) != set(package_by_pair):
        errors.append(
            "素材1 and Stage1 ZIP pair ids are not a two-way exact match: "
            f"main={sorted(main_by_pair)}, stage1={sorted(package_by_pair)}"
        )
    if not any(_event_is_verified_stage1(event) for event in events):
        errors.append("执行记录 lacks verified phase=stage1_complete")
    if errors:
        return {
            "status": "ineligible_stage1",
            "record_id": record_id,
            "video_number": video_number,
            "reasons": errors,
            "zed_note_update": _zed_note_update(snapshot),
            "continue_selection": True,
            "zed_note_used": True,
        }

    pairs = [(pair_id, main_by_pair[pair_id], package_by_pair[pair_id]) for pair_id in sorted(main_by_pair, key=lambda value: int(value))]
    fingerprint = _input_fingerprint(record_id, pairs)
    dispatch_key = f"omni-stage2-v32-resource07|{record_id}|{video_number}|{fingerprint}"
    try:
        outputs = _remote_outputs(record_id, [*packages, *captions])
    except Stage2SelectionError as exc:
        return {
            "status": "hold_ambiguous_existing_output",
            "record_id": record_id,
            "video_number": video_number,
            "dispatch_key": dispatch_key,
            "input_fingerprint": fingerprint,
            "reason": str(exc),
        }

    matching_complete = [
        event for event in events
        if event.get("phase") == "stage2_complete"
        and _event_matches_input(event, dispatch_key=dispatch_key, fingerprint=fingerprint)
    ]
    if matching_complete:
        if len(matching_complete) > 1:
            return {
                "status": "hold_ambiguous_completion_record",
                "record_id": record_id,
                "video_number": video_number,
                "dispatch_key": dispatch_key,
                "input_fingerprint": fingerprint,
                "reason": "more than one stage2_complete matches the current input",
            }
        matching_started = [
            event
            for event in events
            if event.get("phase") == "stage2_started"
            and event.get("readback_verified") is True
            and _event_matches_input(event, dispatch_key=dispatch_key, fingerprint=fingerprint)
            and event.get("strict_release") == matching_complete[0].get("strict_release")
            and event.get("attempt", matching_complete[0].get("attempt")) == matching_complete[0].get("attempt")
        ]
        if not matching_started:
            return {
                "status": "hold_inconsistent_completion",
                "record_id": record_id,
                "video_number": video_number,
                "dispatch_key": dispatch_key,
                "input_fingerprint": fingerprint,
                "reason": "stage2_complete has no matching read-back stage2_started event",
            }
        complete, reason = _verified_complete_outputs(
            matching_complete[0], outputs, strict_release=strict_release
        )
        if complete:
            return {
                "status": "skip_complete",
                "record_id": record_id,
                "video_number": video_number,
                "dispatch_key": dispatch_key,
                "input_fingerprint": fingerprint,
                "strict_release": strict_release,
                "pair_ids": [pair_id for pair_id, _, _ in pairs],
                "zed_note_used": False,
            }
        return {
            "status": "hold_inconsistent_completion",
            "record_id": record_id,
            "video_number": video_number,
            "dispatch_key": dispatch_key,
            "input_fingerprint": fingerprint,
            "reason": reason,
            "replacement_targets": _replacement_targets(outputs),
        }

    # A previous task page may still be rendering or awaiting a remote readback.
    # Never create a second page for the same input merely because its first
    # dispatch event exists.  A failure or completion for the same attempt is a
    # terminal event and releases this HOLD into the normal retry/skip logic.
    started_attempts = {
        event.get("attempt", 1)
        for event in events
        if event.get("phase") in {"stage2_dispatched", "stage2_started"}
        and _event_matches_input(event, dispatch_key=dispatch_key, fingerprint=fingerprint)
        and isinstance(event.get("attempt", 1), int)
    }
    terminal_attempts = {
        event.get("attempt", 1)
        for event in events
        if event.get("phase") in {"stage2_complete", "stage2_failed", "stage2_failed_final"}
        and _event_matches_input(event, dispatch_key=dispatch_key, fingerprint=fingerprint)
        and isinstance(event.get("attempt", 1), int)
    }
    active_attempts = sorted(started_attempts - terminal_attempts)
    if active_attempts:
        return {
            "status": "hold_active_same_input_task",
            "record_id": record_id,
            "video_number": video_number,
            "dispatch_key": dispatch_key,
            "input_fingerprint": fingerprint,
            "reason": f"same-input Stage2 attempt is still non-terminal: {active_attempts}",
            "zed_note_used": False,
        }

    incompatible_events: list[tuple[dict[str, Any], str]] = []
    for event in events:
        if event.get("phase") not in RULE_INCOMPATIBLE_PHASES:
            continue
        if not _event_matches_input(event, dispatch_key=dispatch_key, fingerprint=fingerprint):
            continue
        reason = _rule_incompatible_reason(event)
        if reason:
            incompatible_events.append((event, reason))
    if incompatible_events:
        event, reason = incompatible_events[0]
        return {
            "status": "rule_incompatible",
            "record_id": record_id,
            "video_number": video_number,
            "dispatch_key": dispatch_key,
            "input_fingerprint": fingerprint,
            "reason": reason,
            "failed_phase": event.get("phase"),
            "zed_note_update": _zed_note_update(snapshot),
            "continue_selection": True,
            "zed_note_used": True,
        }

    same_input_failures = [
        event for event in events
        if event.get("phase") in {"stage2_failed", "stage2_failed_final"}
        and _event_matches_input(event, dispatch_key=dispatch_key, fingerprint=fingerprint)
        and _event_day(event) == selected_date
    ]
    if any(event.get("phase") == "stage2_failed_final" or event.get("attempt") == 2 for event in same_input_failures):
        return {
            "status": "skip_failed_final_today",
            "record_id": record_id,
            "video_number": video_number,
            "dispatch_key": dispatch_key,
            "input_fingerprint": fingerprint,
            "zed_note_used": False,
        }
    if same_input_failures:
        return {
            "status": "retry_eligible_after_first_pass",
            "record_id": record_id,
            "video_number": video_number,
            "dispatch_key": dispatch_key,
            "input_fingerprint": fingerprint,
            "strict_release": strict_release,
            "attempt": 2,
            "pair_ids": [pair_id for pair_id, _, _ in pairs],
            "input_manifest": _input_manifest(pairs),
            "delivery_policy": {
                "mode": "replace_rework" if outputs else "append",
                "replacements": _replacement_targets(outputs),
            },
            "zed_note_used": False,
        }

    return {
        "status": "selected_initial",
        "record_id": record_id,
        "video_number": video_number,
        "dispatch_key": dispatch_key,
        "input_fingerprint": fingerprint,
        "strict_release": strict_release,
        "attempt": 1,
        "pair_ids": [pair_id for pair_id, _, _ in pairs],
        "input_manifest": _input_manifest(pairs),
        "delivery_policy": {
            "mode": "replace_rework" if outputs else "append",
            "replacements": _replacement_targets(outputs),
        },
        "zed_note_used": False,
    }


def select_row_file(snapshot_path: Path, *, batch_date: str | None, output_path: Path | None) -> dict[str, Any]:
    value = read_json(snapshot_path)
    result = evaluate_row(value, batch_date=batch_date)
    if output_path is not None:
        atomic_write_json(output_path, result)
    return result

