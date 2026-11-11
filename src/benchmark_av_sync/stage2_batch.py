"""Fail-closed end-to-end Stage2 Bitable batch controller.

``stage2_runner`` deliberately has no Feishu credentials: it can only turn a
fully specified row job into local verified delivery files.  This module is
the missing control plane between that runner and a credential-owning Bitable
bridge.  It does all of the following in one durable, resumable batch:

* reads every row in the configured Bitable view and enriches attachment SHA-256s;
* applies the existing fail-closed selector to every fully-read row;
* creates and reads back a row-bound ``stage2_started`` event before media work;
* materialises the exact selected attachments, builds a strict job, and runs it;
* appends each delivery, verifies name/size/SHA-256 remotely, then performs
  whitelisted replacement deletion only after all new files have read back;
* appends a strict ``stage2_complete`` event and updates the row note only
  after remote verification; and
* makes exactly one retry pass for same-day first-pass failures.

The controller intentionally speaks a small JSON command-adapter protocol.
The adapter owns credentials and is normally implemented by a WorkBuddy window
using its connected Feishu Bitable bridge.  No token, cookie, or table binding
is embedded in this module or its public configuration example.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .stage2_contract import (
    AUTOMATED_REVIEW_METHOD,
    CAPTION_FONT_FAMILY,
    CAPTION_FONT_FULL_NAME,
    DELIVERY_SPECS,
    RUNTIME_PROVENANCE_BY_MODE,
    build_stage2_complete_event,
    validate_runtime_receipt,
    validate_stage2_complete_event,
)
from .stage2_release import validate_installed_release
from .stage2_runner import run_row
from .stage2_selection import (
    Stage2SelectionError,
    append_zed_note,
    evaluate_row,
    parse_execution_events,
)
from .two_stage import online_preflight
from .workspace import atomic_write_json, read_json, sha256_file


SCHEMA_VERSION = 1
SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9_.-]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class Stage2BatchError(RuntimeError):
    """Raised when a batch cannot safely continue."""


class Stage2BitableAdapter(Protocol):
    """Credential-owning operations required by the Stage2 controller.

    Every mutating operation must be implemented by the bridge as an append or
    an exact-token mutation.  Broad field replacement is intentionally absent.
    """

    def check_connection(self) -> dict[str, Any]: ...

    def list_records(self) -> list[dict[str, Any]]: ...

    def get_record(self, record_id: str) -> dict[str, Any]: ...

    def read_attachment_sha256(
        self, *, record_id: str, field_name: str, attachment: dict[str, Any]
    ) -> str: ...

    def download_attachment(
        self,
        *,
        record_id: str,
        field_name: str,
        attachment: dict[str, Any],
        destination: Path,
    ) -> Path: ...

    def append_execution(self, *, record_id: str, event: dict[str, Any]) -> None: ...

    def upload_attachment(
        self,
        *,
        record_id: str,
        field_name: str,
        path: Path,
        idempotency_key: str,
    ) -> dict[str, Any]: ...

    def remove_attachment(
        self,
        *,
        record_id: str,
        field_name: str,
        attachment: dict[str, Any],
    ) -> None: ...

    def update_fields(self, *, record_id: str, fields: dict[str, Any]) -> None: ...


@dataclass(frozen=True, slots=True)
class Stage2BatchFields:
    video_number: str = "视频编号"
    main_video: str = "素材1"
    stage1_output: str = "素材2"
    captioned_output: str = "素材3"
    execution: str = "执行记录"
    note: str = "zed备注"


@dataclass(frozen=True, slots=True)
class Stage2BatchConfig:
    path: Path
    state_root: Path
    work_root: Path
    fields: Stage2BatchFields
    source_binding: dict[str, Any]
    adapter: dict[str, Any]
    runtime: dict[str, Any]
    batch_authorization: str | None
    review_authorization: dict[str, Any] | None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_component(value: str, *, name: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."} or not SAFE_COMPONENT.fullmatch(value):
        raise Stage2BatchError(f"unsafe {name}: {value!r}")
    return value


def _safe_record_component(record_id: str) -> str:
    # Record IDs are selector-validated but use a dedicated error at the file
    # boundary so an adapter cannot turn an opaque remote identifier into a path.
    return _safe_component(record_id, name="record_id")


def _config_path(value: Any, *, name: str, config_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise Stage2BatchError(f"{name} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        path = config_root / path
    return path.resolve()


def load_config(path: Path) -> Stage2BatchConfig:
    """Load only non-secret controller settings.

    The adapter itself receives whatever private connection context its host
    needs.  The config records a source binding so the selector can reject a
    row from an accidental table/view, but callers must keep real bindings out
    of public repositories and logs.
    """

    path = path.resolve()
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise Stage2BatchError("Stage2 batch config schema_version must be 1")
    raw_fields = data.get("fields", {})
    if not isinstance(raw_fields, dict):
        raise Stage2BatchError("fields must be an object")
    defaults = Stage2BatchFields()
    fields = Stage2BatchFields(
        video_number=str(raw_fields.get("video_number", defaults.video_number)),
        main_video=str(raw_fields.get("main_video", defaults.main_video)),
        stage1_output=str(raw_fields.get("stage1_output", defaults.stage1_output)),
        captioned_output=str(raw_fields.get("captioned_output", defaults.captioned_output)),
        execution=str(raw_fields.get("execution", defaults.execution)),
        note=str(raw_fields.get("note", defaults.note)),
    )
    if not all(getattr(fields, name).strip() for name in fields.__dataclass_fields__):
        raise Stage2BatchError("field names must be non-empty")
    source_binding = data.get("source_binding")
    if not isinstance(source_binding, dict) or not source_binding:
        raise Stage2BatchError("source_binding must be a non-empty object")
    adapter = data.get("adapter")
    if not isinstance(adapter, dict):
        raise Stage2BatchError("adapter must be an object")
    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        raise Stage2BatchError("runtime must be an object")
    authorization = data.get("batch_authorization")
    if authorization is not None and (not isinstance(authorization, str) or not authorization.strip()):
        raise Stage2BatchError("batch_authorization must be a non-empty string when present")
    review_authorization = data.get("review_authorization")
    if review_authorization is not None and not isinstance(review_authorization, dict):
        raise Stage2BatchError("review_authorization must be an object when present")
    return Stage2BatchConfig(
        path=path,
        state_root=_config_path(data.get("state_root"), name="state_root", config_root=path.parent),
        work_root=_config_path(data.get("work_root"), name="work_root", config_root=path.parent),
        fields=fields,
        source_binding=dict(source_binding),
        adapter=dict(adapter),
        runtime=dict(runtime),
        batch_authorization=authorization.strip() if isinstance(authorization, str) else None,
        review_authorization=dict(review_authorization) if isinstance(review_authorization, dict) else None,
    )


class CommandBitableAdapter:
    """JSON subprocess bridge for WorkBuddy-owned Feishu calls.

    The configured command receives ``<operation> --request PATH --response
    PATH``.  It must write one JSON object to the response path.  Request and
    response files are retained under the run workspace as audit evidence.
    """

    def __init__(self, config: dict[str, Any], *, audit_root: Path):
        command = config.get("command")
        if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
            raise Stage2BatchError("adapter.command must be a non-empty string list")
        self.command = list(command)
        raw_cwd = config.get("cwd")
        self.cwd = Path(raw_cwd).resolve() if isinstance(raw_cwd, str) and raw_cwd else None
        if self.cwd is not None and not self.cwd.is_dir():
            raise Stage2BatchError(f"adapter.cwd does not exist: {self.cwd}")
        raw_context = config.get("context", {})
        if not isinstance(raw_context, dict):
            raise Stage2BatchError("adapter.context must be an object")
        self.context = dict(raw_context)
        self.audit_root = audit_root.resolve()
        self.audit_root.mkdir(parents=True, exist_ok=True)

    def _call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        call_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:10]}"
        request = self.audit_root / f"{call_id}.{operation}.request.json"
        response = self.audit_root / f"{call_id}.{operation}.response.json"
        atomic_write_json(request, {"context": self.context, **payload})
        completed = subprocess.run(
            self.command + [operation, "--request", str(request), "--response", str(response)],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout or "unknown error")[-1600:]
            raise Stage2BatchError(f"adapter {operation} failed with exit code {completed.returncode}: {detail}")
        if not response.is_file():
            raise Stage2BatchError(f"adapter {operation} did not write {response}")
        value = read_json(response)
        if not isinstance(value, dict) or value.get("ok") is False:
            raise Stage2BatchError(
                f"adapter {operation} returned failure: {json.dumps(value, ensure_ascii=False)}"
            )
        return value

    def check_connection(self) -> dict[str, Any]:
        return self._call("connection_status", {})

    def list_records(self) -> list[dict[str, Any]]:
        value = self._call("list_records", {})
        records = value.get("records")
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise Stage2BatchError("adapter list_records returned an invalid records list")
        return records

    def get_record(self, record_id: str) -> dict[str, Any]:
        value = self._call("get_record", {"record_id": record_id})
        record = value.get("record", value)
        if not isinstance(record, dict):
            raise Stage2BatchError("adapter get_record returned an invalid record")
        return record

    def read_attachment_sha256(
        self, *, record_id: str, field_name: str, attachment: dict[str, Any]
    ) -> str:
        value = self._call(
            "read_attachment_sha256",
            {"record_id": record_id, "field_name": field_name, "attachment": attachment},
        )
        digest = value.get("sha256")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest.lower()):
            raise Stage2BatchError("adapter read_attachment_sha256 returned an invalid SHA-256")
        return digest.lower()

    def download_attachment(
        self,
        *,
        record_id: str,
        field_name: str,
        attachment: dict[str, Any],
        destination: Path,
    ) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        value = self._call(
            "download_attachment",
            {
                "record_id": record_id,
                "field_name": field_name,
                "attachment": attachment,
                "destination": str(destination.resolve()),
            },
        )
        returned = Path(str(value.get("path", destination))).resolve()
        if returned != destination.resolve():
            raise Stage2BatchError("adapter downloaded attachment to an unexpected path")
        return returned

    def append_execution(self, *, record_id: str, event: dict[str, Any]) -> None:
        self._call("append_execution", {"record_id": record_id, "event": event})

    def upload_attachment(
        self,
        *,
        record_id: str,
        field_name: str,
        path: Path,
        idempotency_key: str,
    ) -> dict[str, Any]:
        value = self._call(
            "upload_attachment",
            {
                "record_id": record_id,
                "field_name": field_name,
                "path": str(path.resolve()),
                "idempotency_key": idempotency_key,
            },
        )
        attachment = value.get("attachment", value)
        if not isinstance(attachment, dict):
            raise Stage2BatchError("adapter upload_attachment returned invalid attachment metadata")
        return attachment

    def remove_attachment(
        self,
        *,
        record_id: str,
        field_name: str,
        attachment: dict[str, Any],
    ) -> None:
        self._call(
            "remove_attachment_exact",
            {"record_id": record_id, "field_name": field_name, "attachment": attachment},
        )

    def update_fields(self, *, record_id: str, fields: dict[str, Any]) -> None:
        self._call("update_fields", {"record_id": record_id, "fields": fields})


def _adapter_from_config(config: Stage2BatchConfig, *, run_id: str) -> Stage2BitableAdapter:
    kind = config.adapter.get("kind", "command")
    if kind != "command":
        raise Stage2BatchError(f"unsupported adapter kind: {kind!r}")
    return CommandBitableAdapter(
        config.adapter,
        audit_root=config.work_root / "stage2-batch" / "adapter-calls" / run_id,
    )


def _record_id(record: dict[str, Any]) -> str:
    value = record.get("record_id")
    if not isinstance(value, str) or not value:
        raise Stage2BatchError("record has no record_id")
    return _safe_record_component(value)


def _fields(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("fields")
    if not isinstance(value, dict):
        raise Stage2BatchError("record has no fields object")
    return value


def _field(record: dict[str, Any], field_name: str, default: Any = None) -> Any:
    return _fields(record).get(field_name, default)


def _attachment_list(record: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    value = _field(record, field_name, [])
    if value in (None, ""):
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise Stage2BatchError(f"field {field_name!r} is not an attachment list")
    return [dict(item) for item in value]


def _attachment_token(attachment: dict[str, Any]) -> str:
    value = attachment.get("file_token", attachment.get("token"))
    if not isinstance(value, str) or not value:
        raise Stage2BatchError("attachment has no file_token")
    return value


def _attachment_name(attachment: dict[str, Any]) -> str:
    value = attachment.get("name")
    if not isinstance(value, str) or not value:
        raise Stage2BatchError("attachment has no name")
    return value


def _attachment_size(attachment: dict[str, Any]) -> int:
    value = attachment.get("size_bytes", attachment.get("size"))
    if not isinstance(value, int) or value <= 0:
        raise Stage2BatchError("attachment has invalid size")
    return value


def _attachment_sha(attachment: dict[str, Any]) -> str:
    value = attachment.get("sha256")
    if not isinstance(value, str) or not SHA256_PATTERN.fullmatch(value.lower()):
        raise Stage2BatchError("attachment has invalid SHA-256")
    return value.lower()


def _enrich_attachment(
    adapter: Stage2BitableAdapter,
    *,
    record_id: str,
    field_name: str,
    attachment: dict[str, Any],
) -> dict[str, Any]:
    item = dict(attachment)
    _attachment_token(item)
    _attachment_name(item)
    _attachment_size(item)
    item["sha256"] = adapter.read_attachment_sha256(
        record_id=record_id, field_name=field_name, attachment=item
    )
    return item


def _snapshot_record(
    adapter: Stage2BitableAdapter,
    *,
    config: Stage2BatchConfig,
    record_id: str,
) -> dict[str, Any]:
    """Fetch one row freshly and enrich every selector-visible attachment."""

    record = adapter.get_record(record_id)
    if _record_id(record) != record_id:
        raise Stage2BatchError("adapter get_record returned a different record_id")
    fields = dict(_fields(record))
    for field_name in (
        config.fields.main_video,
        config.fields.stage1_output,
        config.fields.captioned_output,
    ):
        fields[field_name] = [
            _enrich_attachment(
                adapter, record_id=record_id, field_name=field_name, attachment=item
            )
            for item in _attachment_list(record, field_name)
        ]
    video_number = fields.get(config.fields.video_number)
    if not isinstance(video_number, str):
        video_number = ""
    return {
        "schema_version": 1,
        "record_id": record_id,
        "video_number": video_number,
        "source_binding": dict(config.source_binding),
        "fields": fields,
    }


def _snapshot_for_disk(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return audit evidence without persisting a protected binding."""

    redacted = dict(snapshot)
    redacted["source_binding"] = {"redacted": True}
    return redacted


def _event_matches(event: dict[str, Any], expected: dict[str, Any]) -> bool:
    return event.get("event_id") == expected["event_id"]


def _append_event_and_verify(
    adapter: Stage2BitableAdapter,
    *,
    config: Stage2BatchConfig,
    record_id: str,
    event: dict[str, Any],
) -> dict[str, Any]:
    adapter.append_execution(record_id=record_id, event=event)
    record = adapter.get_record(record_id)
    text = _field(record, config.fields.execution, "")
    try:
        events = parse_execution_events(text)
    except Stage2SelectionError as exc:
        raise Stage2BatchError(f"execution readback is invalid: {exc}") from exc
    for item in reversed(events):
        if _event_matches(item, event):
            return dict(item)
    raise Stage2BatchError(
        f"execution readback did not contain event_id={event['event_id']!r}"
    )


def _event(
    *,
    phase: str,
    record_id: str,
    video_number: str,
    dispatch_key: str,
    input_fingerprint: str,
    strict_release: dict[str, str],
    attempt: int,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "v": 4,
        "flow": "omni_video_merge",
        "stage": "stage2",
        "phase": phase,
        "event_id": f"omni-stage2-{uuid.uuid4().hex}",
        "record_id": record_id,
        "video_number": video_number,
        "dispatch_key": dispatch_key,
        "input_fingerprint": input_fingerprint,
        "strict_release": strict_release,
        "attempt": attempt,
        "updated_at": _utc_now(),
        **extra,
    }


def _remote_attachment_by_token(
    adapter: Stage2BitableAdapter,
    *,
    record_id: str,
    field_name: str,
    token: str,
) -> dict[str, Any]:
    record = adapter.get_record(record_id)
    matches = [
        item
        for item in _attachment_list(record, field_name)
        if _attachment_token(item) == token
    ]
    if len(matches) != 1:
        raise Stage2BatchError(
            f"remote readback expected exactly one attachment token={token!r} in {field_name!r}, found {len(matches)}"
        )
    return matches[0]


def _verify_uploaded_attachment(
    adapter: Stage2BitableAdapter,
    *,
    record_id: str,
    field_name: str,
    returned: dict[str, Any],
    path: Path,
) -> dict[str, Any]:
    token = _attachment_token(returned)
    remote = _remote_attachment_by_token(
        adapter, record_id=record_id, field_name=field_name, token=token
    )
    if _attachment_name(remote) != path.name:
        raise Stage2BatchError(f"remote upload name mismatch for {path.name!r}")
    if _attachment_size(remote) != path.stat().st_size:
        raise Stage2BatchError(f"remote upload size mismatch for {path.name!r}")
    remote_sha = adapter.read_attachment_sha256(
        record_id=record_id, field_name=field_name, attachment=remote
    )
    local_sha = sha256_file(path)
    if remote_sha != local_sha:
        raise Stage2BatchError(f"remote upload SHA-256 mismatch for {path.name!r}")
    verified = dict(remote)
    verified["sha256"] = remote_sha
    return verified


def _verify_exact_removal(
    adapter: Stage2BitableAdapter,
    *,
    record_id: str,
    field_name: str,
    token: str,
) -> None:
    record = adapter.get_record(record_id)
    if any(_attachment_token(item) == token for item in _attachment_list(record, field_name)):
        raise Stage2BatchError(f"remote readback still contains removed token={token!r}")


def _read_local_attachment(
    adapter: Stage2BitableAdapter,
    *,
    record_id: str,
    field_name: str,
    attachment: dict[str, Any],
    destination: Path,
) -> Path:
    downloaded = adapter.download_attachment(
        record_id=record_id,
        field_name=field_name,
        attachment=attachment,
        destination=destination,
    )
    if downloaded.resolve() != destination.resolve() or not destination.is_file():
        raise Stage2BatchError("adapter did not materialise the requested attachment")
    if destination.stat().st_size != _attachment_size(attachment):
        raise Stage2BatchError(f"downloaded attachment size mismatch: {destination.name}")
    if sha256_file(destination) != _attachment_sha(attachment):
        raise Stage2BatchError(f"downloaded attachment SHA-256 mismatch: {destination.name}")
    return destination


def _path_suffix(attachment: dict[str, Any], *, fallback: str) -> str:
    suffix = Path(_attachment_name(attachment)).suffix
    if not suffix or len(suffix) > 12 or not re.fullmatch(r"\.[A-Za-z0-9]+", suffix):
        return fallback
    return suffix.lower()


def _runtime_path(value: Any, *, name: str, config_root: Path) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise Stage2BatchError(f"runtime.{name} must be a path string")
    path = Path(value)
    if not path.is_absolute():
        path = config_root / path
    path = path.resolve()
    if not path.is_file():
        raise Stage2BatchError(f"runtime.{name} does not exist: {path}")
    return path


def _find_executable(value: Any, *, name: str, config_root: Path) -> Path:
    supplied = _runtime_path(value, name=name, config_root=config_root)
    if supplied is not None:
        return supplied
    found = shutil.which(name)
    if not found:
        raise Stage2BatchError(f"runtime executable {name!r} is unavailable")
    return Path(found).resolve()


def _font_identity(path: Path) -> dict[str, str]:
    completed = subprocess.run(
        ["fc-scan", "--format=%{family}\t%{style}\t%{fullname}\n", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if completed.returncode != 0:
        raise Stage2BatchError(f"fc-scan failed for runtime.font_file: {completed.stderr[-1000:]}")
    for line in completed.stdout.splitlines():
        family, separator, tail = line.partition("\t")
        if not separator:
            continue
        style, separator, fullname = tail.partition("\t")
        if (
            family == CAPTION_FONT_FAMILY
            and "Bold" in style
            and fullname == CAPTION_FONT_FULL_NAME
        ):
            return {
                "family": family,
                "style": style,
                "fullname": fullname,
                "path": str(path.resolve()),
                "sha256": sha256_file(path),
            }
    raise Stage2BatchError(
        "runtime.font_file is not the required Noto Sans CJK SC Bold font"
    )


def _runtime_receipt(
    *,
    config: Stage2BatchConfig,
    record_id: str,
    row_root: Path,
    strict_release: dict[str, str],
) -> tuple[Path, dict[str, Any], dict[str, str]]:
    """Run the actual strict runtime checks and write a row-bound receipt."""

    runtime = config.runtime
    mode = runtime.get("mode", "reused")
    if mode not in RUNTIME_PROVENANCE_BY_MODE:
        raise Stage2BatchError("runtime.mode must be reused, restored, or reconstructed")
    python_path = _find_executable(runtime.get("python"), name="python", config_root=config.path.parent)
    ffmpeg = _find_executable(runtime.get("ffmpeg"), name="ffmpeg", config_root=config.path.parent)
    ffprobe = _find_executable(runtime.get("ffprobe"), name="ffprobe", config_root=config.path.parent)
    font_source = _runtime_path(runtime.get("font_file"), name="font_file", config_root=config.path.parent)
    if font_source is None:
        raise Stage2BatchError("runtime.font_file is required; system font fallback is forbidden")
    font = _font_identity(font_source)
    fonts_dir = row_root / "fonts"
    fonts_dir.mkdir(parents=True, exist_ok=True)
    row_font = fonts_dir / font_source.name
    if not row_font.exists():
        shutil.copy2(font_source, row_font)
    if sha256_file(row_font) != font["sha256"]:
        raise Stage2BatchError("copied runtime font SHA-256 mismatch")

    preflight = online_preflight(
        ffmpeg=ffmpeg, ffprobe=ffprobe, python_executable=python_path
    )
    package_versions = preflight.get("details", {}).get("python_packages", {})
    if not isinstance(package_versions, dict):
        package_versions = {}
    python_version = subprocess.run(
        [str(python_path), "-c", "import sys; print('.'.join(map(str, sys.version_info[:3])))"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if python_version.returncode != 0:
        raise Stage2BatchError("unable to read runtime Python version")
    face_mesh = subprocess.run(
        [
            str(python_path),
            "-c",
            (
                "import mediapipe as mp, numpy as np; "
                "mesh=mp.solutions.face_mesh.FaceMesh(static_image_mode=True,max_num_faces=1); "
                "mesh.process(np.zeros((64,64,3),dtype=np.uint8)); mesh.close()"
            ),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    versions = {
        "python": python_version.stdout.strip(),
        "mediapipe": str(package_versions.get("mediapipe", "")),
        "opencv": str(package_versions.get("opencv", "")),
        "numpy": str(package_versions.get("numpy", "")),
    }
    expected = {"python": "3.11.14", "mediapipe": "0.10.21", "opencv": "4.11.0", "numpy": "1.26.4"}
    mismatches = [key for key, value in expected.items() if versions.get(key) != value]
    if mismatches:
        raise Stage2BatchError(
            "runtime versions do not meet the strict lock: "
            + ", ".join(f"{key}={versions.get(key)!r}" for key in mismatches)
        )
    if not preflight.get("ok"):
        raise Stage2BatchError(
            "online preflight failed: " + "; ".join(str(item) for item in preflight.get("errors", []))
        )
    if face_mesh.returncode != 0:
        raise Stage2BatchError("FaceMesh smoke test failed: " + face_mesh.stderr[-1000:])
    provenance: dict[str, Any] = {"kind": RUNTIME_PROVENANCE_BY_MODE[mode]}
    if mode == "reconstructed":
        authorization = runtime.get("reconstruction_authorization")
        source = runtime.get("source")
        if not isinstance(authorization, str) or not authorization.strip():
            raise Stage2BatchError("reconstructed runtime requires reconstruction_authorization")
        if not isinstance(source, dict):
            raise Stage2BatchError("reconstructed runtime requires pinned runtime.source")
        provenance["authorization"] = authorization.strip()
        provenance["source"] = dict(source)
    receipt = {
        "schema": "omni-stage2-runtime-receipt/v2",
        "record_id": record_id,
        "mode": mode,
        "provenance": provenance,
        "versions": versions,
        "face_mesh_smoke": True,
        "online_preflight_ok": True,
        "ffmpeg_ok": True,
        "ffprobe_ok": True,
        "minterpolate_ok": True,
        "libass_ok": True,
        "cjk_bold_font": CAPTION_FONT_FULL_NAME,
        "cjk_bold_font_sha256": font["sha256"],
        "checked_at": _utc_now(),
    }
    try:
        receipt = validate_runtime_receipt(receipt, record_id=record_id)
    except Exception as exc:
        raise Stage2BatchError(f"runtime receipt validation failed: {exc}") from exc
    receipt_path = row_root / "runtime-receipt.json"
    atomic_write_json(receipt_path, receipt)
    return receipt_path, receipt, {
        "ffmpeg": str(ffmpeg),
        "ffprobe": str(ffprobe),
        "mouth_python": str(python_path),
        "caption_fonts_dir": str(fonts_dir),
    }


def _review_authorization(
    config: Stage2BatchConfig,
    *,
    record_id: str,
    dispatch_key: str,
    attempt: int,
) -> dict[str, Any] | None:
    value = config.review_authorization
    if value is None:
        return None
    statement = value.get("statement")
    authorized_at = value.get("authorized_at")
    if not isinstance(statement, str) or not statement.strip():
        raise Stage2BatchError("review_authorization.statement is required")
    if not isinstance(authorized_at, str) or not authorized_at.strip():
        raise Stage2BatchError("review_authorization.authorized_at is required")
    return {
        "method": AUTOMATED_REVIEW_METHOD,
        "record_id": record_id,
        "dispatch_key": dispatch_key,
        "attempt": attempt,
        "statement": statement.strip(),
        "authorized_at": authorized_at.strip(),
    }


def _write_job(
    adapter: Stage2BitableAdapter,
    *,
    config: Stage2BatchConfig,
    decision: dict[str, Any],
    stage2_started_receipt: dict[str, Any],
    run_root: Path,
) -> Path:
    record_id = str(decision["record_id"])
    attempt = int(decision["attempt"])
    row_root = run_root / _safe_record_component(record_id) / f"attempt-{attempt}"
    inputs_root = row_root / "inputs"
    manifest = decision.get("input_manifest")
    if not isinstance(manifest, list) or not manifest:
        raise Stage2BatchError("selection decision has no input_manifest")
    inputs: list[dict[str, Any]] = []
    seen_pairs: set[int] = set()
    for item in manifest:
        if not isinstance(item, dict):
            raise Stage2BatchError("input_manifest item is invalid")
        pair_id = item.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id.isdigit():
            raise Stage2BatchError("input_manifest pair_id is invalid")
        numeric = int(pair_id)
        if numeric in seen_pairs:
            raise Stage2BatchError("input_manifest has duplicate numeric pair IDs")
        seen_pairs.add(numeric)
        main = item.get("main_video")
        package = item.get("stage1_zip")
        if not isinstance(main, dict) or not isinstance(package, dict):
            raise Stage2BatchError("input_manifest needs main_video and stage1_zip")
        pair_root = inputs_root / f"{numeric:03d}"
        main_path = pair_root / f"main{_path_suffix(main, fallback='.mp4')}"
        package_path = pair_root / f"stage1{_path_suffix(package, fallback='.zip')}"
        _read_local_attachment(
            adapter,
            record_id=record_id,
            field_name=str(main.get("field_name", config.fields.main_video)),
            attachment=main,
            destination=main_path,
        )
        _read_local_attachment(
            adapter,
            record_id=record_id,
            field_name=str(package.get("field_name", config.fields.stage1_output)),
            attachment=package,
            destination=package_path,
        )
        inputs.append(
            {
                "pair_id": f"{numeric:03d}",
                "reference_package": {"path": str(package_path), "sha256": sha256_file(package_path)},
                "generated_video": {"path": str(main_path), "sha256": sha256_file(main_path)},
            }
        )
    inputs.sort(key=lambda item: int(str(item["pair_id"])))
    receipt_path, _receipt, runtime_paths = _runtime_receipt(
        config=config,
        record_id=record_id,
        row_root=row_root,
        strict_release=dict(decision["strict_release"]),
    )
    job = {
        "schema_version": 2,
        "record_id": record_id,
        "video_number": decision["video_number"],
        "dispatch_key": decision["dispatch_key"],
        "input_fingerprint": decision["input_fingerprint"],
        "strict_release": decision["strict_release"],
        "attempt": attempt,
        "inputs": inputs,
        "output_dir": str(row_root / "results"),
        "caption_style": "D",
        "speed_policy": {"default_max": 1.25, "approved_exception_max": 1.90},
        "sync_policy": {"allow_pause_hold": False, "pause_hold_max_seconds": 2.0},
        "runtime": {**runtime_paths, "runtime_receipt": str(receipt_path)},
        "stage2_started_receipt": stage2_started_receipt,
        "delivery_policy": decision["delivery_policy"],
    }
    review = _review_authorization(
        config,
        record_id=record_id,
        dispatch_key=str(decision["dispatch_key"]),
        attempt=attempt,
    )
    if review is not None:
        job["review_authorization"] = review
    job_path = row_root / "job.json"
    atomic_write_json(job_path, job)
    return job_path


def _result_deliveries(result: dict[str, Any]) -> list[dict[str, Any]]:
    deliveries = result.get("deliveries")
    if not isinstance(deliveries, list):
        raise Stage2BatchError("runner success result has no deliveries")
    by_key = {
        item.get("artifact_key"): item for item in deliveries if isinstance(item, dict)
    }
    if set(by_key) != set(DELIVERY_SPECS):
        raise Stage2BatchError("runner result does not list all four canonical deliveries")
    return [dict(by_key[key]) for key in DELIVERY_SPECS]


def _publish_result(
    adapter: Stage2BitableAdapter,
    *,
    config: Stage2BatchConfig,
    decision: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    """Append all deliveries, read each back, then close the row lifecycle."""

    record_id = str(decision["record_id"])
    remote_deliveries: list[dict[str, Any]] = []
    new_tokens: dict[str, str] = {}
    for delivery in _result_deliveries(result):
        artifact_key = str(delivery["artifact_key"])
        field_name, _suffix = DELIVERY_SPECS[artifact_key]
        if delivery.get("field_name") != field_name:
            raise Stage2BatchError(f"runner delivery field mismatch for {artifact_key}")
        path = Path(str(delivery.get("path", ""))).resolve()
        if not path.is_file() or sha256_file(path) != str(delivery.get("sha256", "")).lower():
            raise Stage2BatchError(f"runner delivery file failed local SHA validation: {artifact_key}")
        key = delivery.get("idempotency_key")
        if not isinstance(key, str) or not key:
            raise Stage2BatchError(f"runner delivery has no idempotency key: {artifact_key}")
        returned = adapter.upload_attachment(
            record_id=record_id, field_name=field_name, path=path, idempotency_key=key
        )
        verified = _verify_uploaded_attachment(
            adapter,
            record_id=record_id,
            field_name=field_name,
            returned=returned,
            path=path,
        )
        new_tokens[artifact_key] = _attachment_token(verified)
        remote_deliveries.append(
            {
                "artifact_key": artifact_key,
                "field_name": field_name,
                "name": _attachment_name(verified),
                "size_bytes": _attachment_size(verified),
                "sha256": _attachment_sha(verified),
                "idempotency_key": key,
                "file_token": new_tokens[artifact_key],
            }
        )

    policy = decision.get("delivery_policy", {})
    if not isinstance(policy, dict):
        raise Stage2BatchError("selection delivery_policy is invalid")
    if policy.get("mode") == "replace_rework":
        replacements = policy.get("replacements")
        if not isinstance(replacements, list) or not replacements:
            raise Stage2BatchError("rework has no exact replacement list")
        for target in replacements:
            if not isinstance(target, dict):
                raise Stage2BatchError("replacement target is invalid")
            field_name = target.get("field_name")
            token = target.get("file_token")
            if not isinstance(field_name, str) or not isinstance(token, str):
                raise Stage2BatchError("replacement target lacks exact field/token")
            # Re-read the exact old attachment before deletion so a stale plan
            # cannot remove a replacement somebody has changed in the meantime.
            old = _remote_attachment_by_token(
                adapter, record_id=record_id, field_name=field_name, token=token
            )
            if (
                _attachment_name(old) != target.get("name")
                or _attachment_size(old) != target.get("size_bytes")
                or adapter.read_attachment_sha256(
                    record_id=record_id, field_name=field_name, attachment=old
                ) != target.get("sha256")
            ):
                raise Stage2BatchError("replacement target changed after selection; refusing deletion")
            adapter.remove_attachment(
                record_id=record_id, field_name=field_name, attachment=old
            )
            _verify_exact_removal(
                adapter, record_id=record_id, field_name=field_name, token=token
            )
    elif policy.get("mode") != "append":
        raise Stage2BatchError("delivery_policy mode is unsupported")

    # One final readback proves all new attachments coexist after any rework
    # deletion; it is the boundary before a remote completion can be written.
    for delivery in remote_deliveries:
        _remote_attachment_by_token(
            adapter,
            record_id=record_id,
            field_name=str(delivery["field_name"]),
            token=str(delivery["file_token"]),
        )
    completion_input = dict(result)
    completion_input["deliveries"] = remote_deliveries
    completion = build_stage2_complete_event(
        completion_input,
        remote_readback_verified=True,
        review_receipt=result.get("review_receipt"),
    )
    valid, reason = validate_stage2_complete_event(
        completion,
        expected_record_id=record_id,
        expected_dispatch_key=str(decision["dispatch_key"]),
        expected_fingerprint=str(decision["input_fingerprint"]),
        require_remote_readback=True,
        require_review_receipt=True,
    )
    if not valid:
        raise Stage2BatchError("completion event validation failed: " + str(reason))
    completion["event_id"] = f"omni-stage2-{uuid.uuid4().hex}"
    readback = _append_event_and_verify(
        adapter, config=config, record_id=record_id, event=completion
    )
    _append_completion_note(adapter, config=config, record_id=record_id)
    return {"completion_event": readback, "deliveries": remote_deliveries}


def _append_completion_note(
    adapter: Stage2BitableAdapter,
    *,
    config: Stage2BatchConfig,
    record_id: str,
) -> str:
    record = adapter.get_record(record_id)
    current = _field(record, config.fields.note, "")
    value = append_zed_note(current, "omni视频处理已完成")
    if value != current:
        adapter.update_fields(record_id=record_id, fields={config.fields.note: value})
    readback = _field(adapter.get_record(record_id), config.fields.note, "")
    if readback != value:
        raise Stage2BatchError("completion note remote readback mismatch")
    return value


def _apply_rule_incompatible_note(
    adapter: Stage2BitableAdapter,
    *,
    config: Stage2BatchConfig,
    record_id: str,
) -> str:
    record = adapter.get_record(record_id)
    current = _field(record, config.fields.note, "")
    value = append_zed_note(current, "现有规则不适合")
    if value != current:
        adapter.update_fields(record_id=record_id, fields={config.fields.note: value})
    readback = _field(adapter.get_record(record_id), config.fields.note, "")
    if readback != value:
        raise Stage2BatchError("rule-incompatible note remote readback mismatch")
    return value


Runner = Callable[[Path], dict[str, Any]]


def _run_one(
    adapter: Stage2BitableAdapter,
    *,
    config: Stage2BatchConfig,
    decision: dict[str, Any],
    run_root: Path,
    runner: Runner,
) -> dict[str, Any]:
    """Execute one selected decision and always durably expose its terminal state."""

    record_id = str(decision["record_id"])
    attempt = int(decision["attempt"])
    started = _event(
        phase="stage2_started",
        record_id=record_id,
        video_number=str(decision["video_number"]),
        dispatch_key=str(decision["dispatch_key"]),
        input_fingerprint=str(decision["input_fingerprint"]),
        strict_release=dict(decision["strict_release"]),
        attempt=attempt,
        readback_verified=True,
    )
    started_receipt = _append_event_and_verify(
        adapter, config=config, record_id=record_id, event=started
    )
    try:
        job_path = _write_job(
            adapter,
            config=config,
            decision=decision,
            stage2_started_receipt=started_receipt,
            run_root=run_root,
        )
        result = runner(job_path)
        if not isinstance(result, dict):
            raise Stage2BatchError("runner returned a non-object result")
        if not result.get("ok"):
            raise Stage2BatchError(str(result.get("error", result.get("status", "runner failed"))))
        published = _publish_result(
            adapter, config=config, decision=decision, result=result
        )
        return {
            "record_id": record_id,
            "video_number": decision["video_number"],
            "dispatch_key": decision["dispatch_key"],
            "input_fingerprint": decision["input_fingerprint"],
            "attempt": attempt,
            "result": "success",
            "remote_readback": published,
        }
    except Exception as exc:
        phase = "stage2_failed_final" if attempt == 2 else "stage2_failed"
        failure = _event(
            phase=phase,
            record_id=record_id,
            video_number=str(decision["video_number"]),
            dispatch_key=str(decision["dispatch_key"]),
            input_fingerprint=str(decision["input_fingerprint"]),
            strict_release=dict(decision["strict_release"]),
            attempt=attempt,
            failure_class="controller_or_runner_failure",
            reason=str(exc)[:2000],
            readback_verified=True,
        )
        # If a failure cannot be appended/read back, do not conceal that as a
        # normal row failure: caller needs the batch itself to stop visibly.
        failure_readback = _append_event_and_verify(
            adapter, config=config, record_id=record_id, event=failure
        )
        return {
            "record_id": record_id,
            "video_number": decision["video_number"],
            "dispatch_key": decision["dispatch_key"],
            "input_fingerprint": decision["input_fingerprint"],
            "attempt": attempt,
            "result": "failed_final" if attempt == 2 else "failed",
            "reason": str(exc),
            "remote_readback": {"failure_event": failure_readback},
        }


def _plan_row(
    adapter: Stage2BitableAdapter,
    *,
    config: Stage2BatchConfig,
    record_id: str,
    batch_date: str,
    state_root: Path,
) -> tuple[dict[str, Any], dict[str, Any]]:
    snapshot = _snapshot_record(adapter, config=config, record_id=record_id)
    decision = evaluate_row(snapshot, batch_date=batch_date)
    row_root = state_root / "snapshots" / batch_date / _safe_record_component(record_id)
    atomic_write_json(row_root / "snapshot.json", _snapshot_for_disk(snapshot))
    atomic_write_json(row_root / "decision.json", decision)
    return snapshot, decision


def _decision_summary(decision: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "status",
        "record_id",
        "video_number",
        "dispatch_key",
        "input_fingerprint",
        "attempt",
        "reason",
    )
    return {key: decision[key] for key in keys if key in decision}


def _write_receipt(config: Stage2BatchConfig, receipt: dict[str, Any]) -> None:
    root = config.state_root / "stage2-batch"
    atomic_write_json(root / "runs" / f"{receipt['run_id']}.json", receipt)
    atomic_write_json(root / f"{receipt['batch_date']}.json", receipt)
    atomic_write_json(root / "latest.json", receipt)


def _receipt_status(rows: Sequence[dict[str, Any]]) -> str:
    failures = {"failed", "failed_final", "hold"}
    return "complete" if not any(item.get("result") in failures for item in rows) else "complete_with_exceptions"


def _require_batch_authorization(config: Stage2BatchConfig) -> None:
    if not config.batch_authorization:
        raise Stage2BatchError(
            "--execute requires config.batch_authorization; generate a dry-run plan first and bind the user's batch authorization"
        )


def run_batch(
    config: Stage2BatchConfig,
    *,
    adapter: Stage2BitableAdapter,
    batch_date: str,
    run_id: str | None = None,
    execute: bool = False,
    runner: Runner = run_row,
) -> dict[str, Any]:
    """Plan or execute every selector-visible row, with one retry pass."""

    try:
        date_type.fromisoformat(batch_date)
    except ValueError as exc:
        raise Stage2BatchError("batch_date must use YYYY-MM-DD") from exc
    if execute:
        _require_batch_authorization(config)
    connection = adapter.check_connection()
    if not bool(connection.get("connected", connection.get("ok", False))):
        raise Stage2BatchError("Bitable adapter is not connected")
    strict_release = validate_installed_release()
    effective_run_id = run_id or f"omni-stage2-batch-{batch_date.replace('-', '')}-{datetime.now().strftime('%H%M%S')}"
    _safe_component(effective_run_id, name="run_id")
    run_root = config.work_root / "stage2-batch" / batch_date / effective_run_id
    run_root.mkdir(parents=True, exist_ok=True)
    records = adapter.list_records()
    record_ids = sorted({_record_id(item) for item in records})
    if len(record_ids) != len(records):
        raise Stage2BatchError("list_records returned duplicate record_id values")

    rows: list[dict[str, Any]] = []
    initial: list[dict[str, Any]] = []
    retry_after_first_pass: set[str] = set()
    for record_id in record_ids:
        try:
            _snapshot, decision = _plan_row(
                adapter,
                config=config,
                record_id=record_id,
                batch_date=batch_date,
                state_root=config.state_root / "stage2-batch",
            )
        except Exception as exc:
            # A malformed or unreadable row is a row-local HOLD.  Continuing
            # lets the controller finish other independent rows without
            # turning an attachment inconsistency into a silent skip.
            rows.append({"record_id": record_id, "result": "hold", "reason": str(exc)})
            continue
        status = decision.get("status")
        if status == "rule_incompatible":
            if execute:
                note = _apply_rule_incompatible_note(
                    adapter, config=config, record_id=record_id
                )
                rows.append({**_decision_summary(decision), "result": "rule_incompatible", "note_readback": note})
            else:
                rows.append({**_decision_summary(decision), "result": "rule_incompatible_planned"})
        elif status == "selected_initial":
            initial.append(decision)
            rows.append({**_decision_summary(decision), "result": "selected_initial_planned" if not execute else "queued_initial"})
        elif status == "retry_eligible_after_first_pass":
            retry_after_first_pass.add(record_id)
            rows.append({**_decision_summary(decision), "result": "retry_queued_after_first_pass" if execute else "retry_planned"})
        elif status == "skip_complete":
            rows.append({**_decision_summary(decision), "result": "already_complete"})
        elif status == "ineligible_stage1":
            if execute and isinstance(decision.get("zed_note_update"), dict):
                note = _apply_rule_incompatible_note(
                    adapter, config=config, record_id=record_id
                )
                rows.append({**_decision_summary(decision), "result": "ineligible_stage1", "note_readback": note})
            else:
                rows.append({**_decision_summary(decision), "result": "ineligible_stage1"})
        elif isinstance(status, str) and status.startswith("hold_"):
            rows.append({**_decision_summary(decision), "result": "hold"})
        elif status == "skip_failed_final_today":
            rows.append({**_decision_summary(decision), "result": "failed_final_today"})
        else:
            rows.append({**_decision_summary(decision), "result": "hold", "reason": f"unknown selector status: {status!r}"})

    plan = {
        "schema_version": SCHEMA_VERSION,
        "run_id": effective_run_id,
        "batch_date": batch_date,
        "execute": execute,
        "strict_release": strict_release,
        "candidate_record_ids": record_ids,
        "authorization_bound": bool(config.batch_authorization),
        "rows": rows,
    }
    atomic_write_json(run_root / "batch-plan.json", plan)
    if not execute:
        receipt = {**plan, "status": "planned", "counts": {"candidate": len(record_ids), "planned_initial": len(initial), "planned_retry": len(retry_after_first_pass)}}
        _write_receipt(config, receipt)
        return receipt

    # Replace provisional initial entries with real results, preserving one
    # final row result per record in the receipt.
    row_by_id = {str(item["record_id"]): item for item in rows}
    for planned in initial:
        # Re-plan immediately before mutation.  A concurrent edit or a prior
        # crashed controller must never be silently resumed from an old snapshot.
        try:
            _snapshot, fresh = _plan_row(
                adapter,
                config=config,
                record_id=str(planned["record_id"]),
                batch_date=batch_date,
                state_root=config.state_root / "stage2-batch",
            )
        except Exception as exc:
            row_by_id[str(planned["record_id"])] = {
                "record_id": planned["record_id"],
                "result": "hold",
                "reason": str(exc),
            }
            continue
        if fresh.get("status") != "selected_initial":
            row_by_id[str(planned["record_id"])] = {
                **_decision_summary(fresh),
                "result": "hold",
                "reason": "selection changed before dispatch",
            }
            continue
        outcome = _run_one(
            adapter,
            config=config,
            decision=fresh,
            run_root=run_root,
            runner=runner,
        )
        row_by_id[str(planned["record_id"])] = outcome
        if outcome["result"] == "failed":
            retry_after_first_pass.add(str(planned["record_id"]))

    # After every first-pass selected row has a terminal state, give all
    # selector-eligible first failures exactly one fresh attempt.  This also
    # resumes an interruption from an earlier controller run without a blind
    # duplicate upload.
    for record_id in sorted(retry_after_first_pass):
        try:
            _snapshot, fresh = _plan_row(
                adapter,
                config=config,
                record_id=record_id,
                batch_date=batch_date,
                state_root=config.state_root / "stage2-batch",
            )
        except Exception as exc:
            row_by_id[record_id] = {
                "record_id": record_id,
                "result": "hold",
                "reason": str(exc),
            }
            continue
        if fresh.get("status") != "retry_eligible_after_first_pass":
            if record_id not in row_by_id or row_by_id[record_id].get("result") == "queued_initial":
                row_by_id[record_id] = {
                    **_decision_summary(fresh),
                    "result": "hold" if str(fresh.get("status", "")).startswith("hold_") else str(fresh.get("status")),
                    "reason": fresh.get("reason", "not retry eligible after first pass"),
                }
            continue
        row_by_id[record_id] = _run_one(
            adapter,
            config=config,
            decision=fresh,
            run_root=run_root,
            runner=runner,
        )

    final_rows = [row_by_id[record_id] for record_id in record_ids]
    receipt = {
        "schema_version": SCHEMA_VERSION,
        "run_id": effective_run_id,
        "batch_date": batch_date,
        "status": _receipt_status(final_rows),
        "strict_release": strict_release,
        "candidate_record_ids": record_ids,
        "authorization_bound": True,
        "counts": {
            "candidate": len(record_ids),
            "success": sum(item.get("result") == "success" for item in final_rows),
            "already_complete": sum(item.get("result") == "already_complete" for item in final_rows),
            "ineligible_stage1": sum(item.get("result") == "ineligible_stage1" for item in final_rows),
            "hold": sum(item.get("result") == "hold" for item in final_rows),
            "failed_final": sum(item.get("result") in {"failed", "failed_final", "failed_final_today"} for item in final_rows),
        },
        "rows": final_rows,
    }
    _write_receipt(config, receipt)
    atomic_write_json(run_root / "batch-receipt.json", receipt)
    return receipt


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omni-stage2-batch")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--date", required=True, help="Asia/Shanghai calendar date, YYYY-MM-DD")
    parser.add_argument("--run-id")
    parser.add_argument(
        "--execute",
        action="store_true",
        help="perform remote writes; default is a read-only batch plan",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        effective_run_id = args.run_id or f"omni-stage2-batch-{args.date.replace('-', '')}-{datetime.now().strftime('%H%M%S')}"
        adapter = _adapter_from_config(config, run_id=effective_run_id)
        receipt = run_batch(
            config,
            adapter=adapter,
            batch_date=args.date,
            run_id=effective_run_id,
            execute=bool(args.execute),
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0 if receipt.get("status") in {"planned", "complete"} else 2
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
