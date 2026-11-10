from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import uuid
from dataclasses import dataclass
from datetime import date as date_type
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from .handoff import validate_reference_package
from .two_stage import build_reference_package_from_video
from .workspace import atomic_write_json, read_json, sha256_file


SCHEMA_VERSION = 1
STAGE1_EVIDENCE_VERSION = "subtitle-v2"
LEADING_PAIR_ID = re.compile(r"^\s*(\d+)(?=[_.\-\s])")


class Stage1RunnerError(RuntimeError):
    """Raised for a batch-level Stage 1 failure."""


class PairingIneligible(ValueError):
    """Raised when one row cannot be paired by leading numeric IDs."""


class Stage1BitableAdapter(Protocol):
    def check_connection(self) -> dict[str, Any]: ...

    def list_records(self) -> list[dict[str, Any]]: ...

    def get_record(self, record_id: str) -> dict[str, Any]: ...

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


@dataclass(frozen=True, slots=True)
class Stage1Fields:
    video_number: str = "视频编号"
    note: str = "zed备注"
    reference: str = "处理后音、视频"
    generated: str = "素材1"
    output: str = "素材2"
    execution: str = "执行记录"


@dataclass(frozen=True, slots=True)
class AttachmentPair:
    pair_id: str
    numeric_id: int
    reference: dict[str, Any]
    generated: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Stage1Config:
    path: Path
    state_root: Path
    work_root: Path
    fields: Stage1Fields
    adapter: dict[str, Any]
    media: dict[str, Any]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _safe_component(value: str, *, name: str) -> str:
    if (
        not value
        or value in {".", ".."}
        or not re.fullmatch(r"[A-Za-z0-9_.-]+", value)
    ):
        raise Stage1RunnerError(f"unsafe {name}: {value!r}")
    return value


def _config_path(value: Any, *, name: str, config_root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise Stage1RunnerError(f"{name} must be a non-empty path")
    path = Path(value)
    if not path.is_absolute():
        path = config_root / path
    return path.resolve()


def load_config(path: Path) -> Stage1Config:
    path = path.resolve()
    data = read_json(path)
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise Stage1RunnerError("Stage 1 config schema_version must be 1")
    fields_data = data.get("fields", {})
    if not isinstance(fields_data, dict):
        raise Stage1RunnerError("fields must be an object")
    defaults = Stage1Fields()
    fields = Stage1Fields(
        video_number=str(fields_data.get("video_number", defaults.video_number)),
        note=str(fields_data.get("note", defaults.note)),
        reference=str(fields_data.get("reference", defaults.reference)),
        generated=str(fields_data.get("generated", defaults.generated)),
        output=str(fields_data.get("output", defaults.output)),
        execution=str(fields_data.get("execution", defaults.execution)),
    )
    adapter = data.get("adapter")
    if not isinstance(adapter, dict):
        raise Stage1RunnerError("adapter must be an object")
    media = data.get("media", {})
    if not isinstance(media, dict):
        raise Stage1RunnerError("media must be an object")
    return Stage1Config(
        path=path,
        state_root=_config_path(data.get("state_root"), name="state_root", config_root=path.parent),
        work_root=_config_path(data.get("work_root"), name="work_root", config_root=path.parent),
        fields=fields,
        adapter=dict(adapter),
        media=dict(media),
    )


class CommandBitableAdapter:
    """Invoke a credential-owning bridge through a small JSON request contract.

    The configured command receives:
      <operation> --request <absolute-json> --response <absolute-json>

    It must write one JSON object to the response path.  This keeps credentials
    outside this project and makes every external call independently auditable.
    """

    def __init__(self, config: dict[str, Any], *, audit_root: Path):
        command = config.get("command")
        if not isinstance(command, list) or not command or not all(
            isinstance(item, str) and item for item in command
        ):
            raise Stage1RunnerError("adapter.command must be a non-empty string list")
        self.command = list(command)
        cwd = config.get("cwd")
        self.cwd = Path(cwd).resolve() if isinstance(cwd, str) and cwd else None
        if self.cwd is not None and not self.cwd.is_dir():
            raise Stage1RunnerError(f"adapter.cwd does not exist: {self.cwd}")
        self.audit_root = audit_root.resolve()
        self.audit_root.mkdir(parents=True, exist_ok=True)
        context = config.get("context", {})
        if not isinstance(context, dict):
            raise Stage1RunnerError("adapter.context must be an object")
        self.context = dict(context)

    def _call(self, operation: str, payload: dict[str, Any]) -> dict[str, Any]:
        call_id = f"{datetime.now().strftime('%Y%m%dT%H%M%S%f')}-{uuid.uuid4().hex[:8]}"
        request = self.audit_root / f"{call_id}.{operation}.request.json"
        response = self.audit_root / f"{call_id}.{operation}.response.json"
        atomic_write_json(request, {"context": self.context, **payload})
        completed = subprocess.run(
            self.command
            + [operation, "--request", str(request), "--response", str(response)],
            cwd=self.cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if completed.returncode != 0:
            raise Stage1RunnerError(
                f"adapter {operation} failed with exit code {completed.returncode}: "
                + (completed.stderr or completed.stdout or "unknown error")[-1500:]
            )
        if not response.is_file():
            raise Stage1RunnerError(f"adapter {operation} did not write {response}")
        value = read_json(response)
        if not isinstance(value, dict) or value.get("ok") is False:
            raise Stage1RunnerError(
                f"adapter {operation} returned failure: {json.dumps(value, ensure_ascii=False)}"
            )
        return value

    def check_connection(self) -> dict[str, Any]:
        return self._call("connection_status", {})

    def list_records(self) -> list[dict[str, Any]]:
        value = self._call("list_records", {})
        records = value.get("records")
        if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
            raise Stage1RunnerError("adapter list_records returned an invalid records list")
        return records

    def get_record(self, record_id: str) -> dict[str, Any]:
        value = self._call("get_record", {"record_id": record_id})
        record = value.get("record", value)
        if not isinstance(record, dict):
            raise Stage1RunnerError("adapter get_record returned an invalid record")
        return record

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
            raise Stage1RunnerError("adapter downloaded attachment to an unexpected path")
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
            raise Stage1RunnerError("adapter upload_attachment returned invalid metadata")
        return attachment


def _field(record: dict[str, Any], name: str, default: Any = None) -> Any:
    fields = record.get("fields")
    if isinstance(fields, dict):
        return fields.get(name, default)
    return record.get(name, default)


def _record_id(record: dict[str, Any]) -> str:
    value = record.get("record_id")
    if not isinstance(value, str) or not value:
        raise Stage1RunnerError("record has no record_id")
    return value


def _video_number(record: dict[str, Any], fields: Stage1Fields) -> str:
    value = _field(record, fields.video_number, "")
    if not isinstance(value, str) or not value.strip():
        raise PairingIneligible(f"field {fields.video_number!r} is empty")
    return value.strip()


def _attachments(record: dict[str, Any], field_name: str) -> list[dict[str, Any]]:
    value = _field(record, field_name, [])
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise PairingIneligible(f"field {field_name!r} is not an attachment list")
    return list(value)


def _attachment_id(attachment: dict[str, Any]) -> tuple[str, int]:
    name = attachment.get("name")
    if not isinstance(name, str):
        raise PairingIneligible("attachment has no filename")
    match = LEADING_PAIR_ID.search(name)
    if not match:
        raise PairingIneligible(f"attachment filename has no leading numeric ID: {name}")
    return match.group(1), int(match.group(1))


def pair_record(record: dict[str, Any], fields: Stage1Fields) -> list[AttachmentPair]:
    references = _attachments(record, fields.reference)
    generated = _attachments(record, fields.generated)
    if not references or not generated:
        raise PairingIneligible("reference and generated attachment fields must both be non-empty")

    def index(items: Sequence[dict[str, Any]], label: str) -> dict[int, tuple[str, dict[str, Any]]]:
        result: dict[int, tuple[str, dict[str, Any]]] = {}
        for attachment in items:
            raw, numeric = _attachment_id(attachment)
            if numeric in result:
                raise PairingIneligible(f"duplicate numeric ID {numeric} in {label}")
            result[numeric] = (raw, attachment)
        return result

    reference_index = index(references, fields.reference)
    generated_index = index(generated, fields.generated)
    if set(reference_index) != set(generated_index):
        missing_generated = sorted(set(reference_index) - set(generated_index))
        missing_reference = sorted(set(generated_index) - set(reference_index))
        raise PairingIneligible(
            "numeric ID sets differ: "
            f"missing generated={missing_generated}, missing reference={missing_reference}"
        )
    return [
        AttachmentPair(
            pair_id=reference_index[numeric][0],
            numeric_id=numeric,
            reference=reference_index[numeric][1],
            generated=generated_index[numeric][1],
        )
        for numeric in sorted(reference_index)
    ]


def _event(*, phase: str, key: str, run_id: str, record_id: str, **extra: Any) -> dict[str, Any]:
    return {
        "v": 1,
        "flow": "omni_video_merge",
        "stage": "stage1",
        "phase": phase,
        "key": key,
        "run_id": run_id,
        "record_id": record_id,
        **extra,
        "updated_at": _now(),
    }


def _execution_text(record: dict[str, Any], fields: Stage1Fields) -> str:
    value = _field(record, fields.execution, "")
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _execution_events(record: dict[str, Any], fields: Stage1Fields) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in _execution_text(record, fields).splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


def _completed_pair(
    record: dict[str, Any], fields: Stage1Fields, input_key: str
) -> dict[str, Any] | None:
    for event in reversed(_execution_events(record, fields)):
        if event.get("phase") != "stage1_complete":
            continue
        pairs = event.get("pairs")
        if not isinstance(pairs, list):
            continue
        for item in pairs:
            if isinstance(item, dict) and item.get("input_key") == input_key:
                return item
    return None


def _verify_execution_readback(
    adapter: Stage1BitableAdapter,
    *,
    record_id: str,
    fields: Stage1Fields,
    phase: str,
    key: str,
    run_id: str,
) -> None:
    text = _execution_text(adapter.get_record(record_id), fields)
    matched = False
    for value in _execution_events({"fields": {fields.execution: text}}, fields):
        if (
            value.get("phase") == phase
            and value.get("key") == key
            and value.get("run_id") == run_id
        ):
            matched = True
            break
    if not matched:
        raise Stage1RunnerError(
            f"execution readback missing phase={phase!r} key={key!r} run_id={run_id!r}"
        )


def _verify_uploaded_readback(
    adapter: Stage1BitableAdapter,
    *,
    record_id: str,
    fields: Stage1Fields,
    path: Path,
) -> dict[str, Any]:
    attachments = _attachments(adapter.get_record(record_id), fields.output)
    matches = [
        item
        for item in attachments
        if item.get("name") == path.name and int(item.get("size", -1)) == path.stat().st_size
    ]
    if len(matches) != 1:
        raise Stage1RunnerError(
            f"remote readback expected one {path.name!r} ({path.stat().st_size} bytes), found {len(matches)}"
        )
    return matches[0]


Processor = Callable[[AttachmentPair, Path, Path, Stage1Config], dict[str, Any]]


def _default_processor(
    pair: AttachmentPair, reference_video: Path, output: Path, config: Stage1Config
) -> dict[str, Any]:
    required = ("ffmpeg", "ffprobe", "qwen_python", "aligner_model", "asr_model")
    missing = [name for name in required if not config.media.get(name)]
    if missing:
        raise Stage1RunnerError("media config is missing: " + ", ".join(missing))
    subtitle_box = config.media.get("subtitle_box")
    if subtitle_box is not None:
        if not isinstance(subtitle_box, list) or len(subtitle_box) != 4:
            raise Stage1RunnerError("media.subtitle_box must contain four integers")
        subtitle_box = [int(item) for item in subtitle_box]
    return build_reference_package_from_video(
        pair_id=pair.pair_id,
        reference_video=reference_video,
        output=output,
        work_dir=output.parent / f"work_{pair.pair_id}",
        ffmpeg=Path(str(config.media["ffmpeg"])),
        ffprobe=Path(str(config.media["ffprobe"])),
        qwen_python=Path(str(config.media["qwen_python"])),
        aligner_model=Path(str(config.media["aligner_model"])),
        asr_model=Path(str(config.media["asr_model"])),
        ocr_python=(Path(str(config.media["ocr_python"])) if config.media.get("ocr_python") else None),
        subtitle_box=subtitle_box,
    )


def _attachment_expected_size(attachment: dict[str, Any]) -> int | None:
    value = attachment.get("size")
    return int(value) if isinstance(value, (int, float)) else None


def process_record(
    record: dict[str, Any],
    *,
    adapter: Stage1BitableAdapter,
    config: Stage1Config,
    run_id: str,
    run_root: Path,
    processor: Processor = _default_processor,
) -> dict[str, Any]:
    record_id = _record_id(record)
    record_root = run_root / _safe_component(record_id, name="record_id")
    record_root.mkdir(parents=True, exist_ok=True)
    video_number = ""
    try:
        video_number = _video_number(record, config.fields)
        pairs = pair_record(record, config.fields)
    except PairingIneligible as exc:
        key = f"omni-stage1|{record_id}|pairing"
        event = _event(
            phase="pairing_ineligible",
            key=key,
            run_id=run_id,
            record_id=record_id,
            video_number=video_number,
            reason=str(exc),
        )
        adapter.append_execution(record_id=record_id, event=event)
        _verify_execution_readback(
            adapter,
            record_id=record_id,
            fields=config.fields,
            phase="pairing_ineligible",
            key=key,
            run_id=run_id,
        )
        return {
            "record_id": record_id,
            "video_number": video_number,
            "result": "pairing_ineligible",
            "reason": str(exc),
            "packages": [],
        }

    packages: list[dict[str, Any]] = []
    try:
        for pair in pairs:
            source_name = str(pair.reference.get("name", f"{pair.pair_id}.mp4"))
            extension = Path(source_name).suffix or ".mp4"
            reference_video = record_root / f"reference_{pair.pair_id}{extension}"
            adapter.download_attachment(
                record_id=record_id,
                field_name=config.fields.reference,
                attachment=pair.reference,
                destination=reference_video,
            )
            if not reference_video.is_file() or reference_video.stat().st_size == 0:
                raise Stage1RunnerError(f"downloaded reference is missing or empty: {reference_video}")
            expected_size = _attachment_expected_size(pair.reference)
            if expected_size is not None and reference_video.stat().st_size != expected_size:
                raise Stage1RunnerError(
                    f"downloaded reference size mismatch for pair {pair.pair_id}: "
                    f"expected {expected_size}, got {reference_video.stat().st_size}"
                )
            reference_sha = sha256_file(reference_video)
            input_key = (
                f"omni-stage1|{STAGE1_EVIDENCE_VERSION}|{record_id}|"
                f"{pair.pair_id}|{reference_sha}"
            )
            fresh_record = adapter.get_record(record_id)
            completed_pair = _completed_pair(fresh_record, config.fields, input_key)
            if completed_pair is not None:
                zip_name = completed_pair.get("zip_name")
                zip_sha = completed_pair.get("zip_sha256")
                if not isinstance(zip_name, str) or not isinstance(zip_sha, str):
                    raise Stage1RunnerError("completed pair record lacks ZIP identity")
                remote_matches = [
                    item
                    for item in _attachments(fresh_record, config.fields.output)
                    if item.get("name") == zip_name
                ]
                if len(remote_matches) != 1:
                    raise Stage1RunnerError(
                        f"completed pair readback expected one {zip_name!r}, found {len(remote_matches)}"
                    )
                remote_item = remote_matches[0]
                packages.append(
                    {
                        "pair_id": pair.pair_id,
                        "numeric_id": pair.numeric_id,
                        "input_key": input_key,
                        "reference_name": source_name,
                        "reference_size": reference_video.stat().st_size,
                        "reference_sha256": reference_sha,
                        "generated_name": pair.generated.get("name"),
                        "zip_name": zip_name,
                        "zip_path": None,
                        "zip_size": remote_item.get("size"),
                        "zip_sha256": zip_sha,
                        "benchmark_status": completed_pair.get("benchmark_status"),
                        "subtitle_status": completed_pair.get("subtitle_status"),
                        "subtitle_source_mode": completed_pair.get(
                            "subtitle_source_mode"
                        ),
                        "remote": remote_item,
                        "remote_readback": remote_item,
                        "processor": {"already_complete": True},
                    }
                )
                continue
            started = _event(
                phase="stage1_started",
                key=input_key,
                run_id=run_id,
                record_id=record_id,
                video_number=video_number,
                pair_id=pair.pair_id,
                reference_sha256=reference_sha,
            )
            adapter.append_execution(record_id=record_id, event=started)
            _verify_execution_readback(
                adapter,
                record_id=record_id,
                fields=config.fields,
                phase="stage1_started",
                key=input_key,
                run_id=run_id,
            )

            output = record_root / (
                f"omni_stage1__v2__{record_id}__{pair.pair_id}__{reference_sha[:12]}.zip"
            )
            if output.exists():
                summary = validate_reference_package(output)
                if int(summary["pair_id"]) != pair.numeric_id:
                    raise Stage1RunnerError("existing package pair ID is inconsistent")
                process_result: dict[str, Any] = {"resumed_local_package": True}
            else:
                process_result = processor(pair, reference_video, output, config)
                summary = validate_reference_package(output)
            if int(summary["pair_id"]) != pair.numeric_id:
                raise Stage1RunnerError("output package pair ID is inconsistent")
            package_sha = sha256_file(output)
            upload_key = f"{input_key}|package|{package_sha}"
            remote = adapter.upload_attachment(
                record_id=record_id,
                field_name=config.fields.output,
                path=output,
                idempotency_key=upload_key,
            )
            readback = _verify_uploaded_readback(
                adapter,
                record_id=record_id,
                fields=config.fields,
                path=output,
            )
            packages.append(
                {
                    "pair_id": pair.pair_id,
                    "numeric_id": pair.numeric_id,
                    "input_key": input_key,
                    "reference_name": source_name,
                    "reference_size": reference_video.stat().st_size,
                    "reference_sha256": reference_sha,
                    "generated_name": pair.generated.get("name"),
                    "zip_name": output.name,
                    "zip_path": str(output.resolve()),
                    "zip_size": output.stat().st_size,
                    "zip_sha256": package_sha,
                    "benchmark_status": summary["benchmark_status"],
                    "subtitle_status": summary["subtitle_status"],
                    "subtitle_source_mode": summary["subtitle_source_mode"],
                    "remote": remote,
                    "remote_readback": readback,
                    "processor": process_result,
                }
            )
        completion_key = f"omni-stage1|{record_id}"
        complete = _event(
            phase="stage1_complete",
            key=completion_key,
            run_id=run_id,
            record_id=record_id,
            video_number=video_number,
            pairs=[
                {
                    "pair_id": item["pair_id"],
                    "input_key": item["input_key"],
                    "zip_name": item["zip_name"],
                    "zip_size": item["zip_size"],
                    "zip_sha256": item["zip_sha256"],
                    "benchmark_status": item["benchmark_status"],
                    "subtitle_status": item["subtitle_status"],
                    "subtitle_source_mode": item["subtitle_source_mode"],
                }
                for item in packages
            ],
            remote_readback_verified=True,
        )
        adapter.append_execution(record_id=record_id, event=complete)
        _verify_execution_readback(
            adapter,
            record_id=record_id,
            fields=config.fields,
            phase="stage1_complete",
            key=completion_key,
            run_id=run_id,
        )
        return {
            "record_id": record_id,
            "video_number": video_number,
            "result": "success",
            "reason": None,
            "packages": packages,
        }
    except Exception as exc:
        failure_key = f"omni-stage1|{record_id}"
        failed = _event(
            phase="stage1_failed",
            key=failure_key,
            run_id=run_id,
            record_id=record_id,
            video_number=video_number,
            reason=str(exc),
            completed_pair_count=len(packages),
        )
        try:
            adapter.append_execution(record_id=record_id, event=failed)
            _verify_execution_readback(
                adapter,
                record_id=record_id,
                fields=config.fields,
                phase="stage1_failed",
                key=failure_key,
                run_id=run_id,
            )
        except Exception as record_exc:
            return {
                "record_id": record_id,
                "video_number": video_number,
                "result": "failed",
                "reason": str(exc),
                "recording_error": str(record_exc),
                "packages": packages,
            }
        return {
            "record_id": record_id,
            "video_number": video_number,
            "result": "failed",
            "reason": str(exc),
            "packages": packages,
        }


def _receipt(
    *, run_id: str, run_date: str, candidates: Sequence[str], rows: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    row_ids = [_record_id(item) if "result" not in item else str(item["record_id"]) for item in rows]
    if sorted(row_ids) != sorted(candidates):
        raise Stage1RunnerError("receipt row set does not equal the live candidate set")
    success = sum(item.get("result") == "success" for item in rows)
    pairing_ineligible = sum(item.get("result") == "pairing_ineligible" for item in rows)
    failed = len(rows) - success
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "run_id": run_id,
        "run_date": run_date,
        "written_at": _now(),
        "status": "complete" if failed == 0 else "complete_with_failures",
        "candidate_record_ids": sorted(candidates),
        "counts": {
            "candidate": len(rows),
            "eligible": len(rows) - pairing_ineligible,
            "success": success,
            "failed": failed,
            "pairing_ineligible": pairing_ineligible,
        },
        "rows": list(rows),
    }


def _write_receipts(config: Stage1Config, receipt: dict[str, Any]) -> None:
    root = config.state_root / "stage1"
    atomic_write_json(root / "runs" / f"{receipt['run_id']}.json", receipt)
    atomic_write_json(root / f"{receipt['run_date']}.json", receipt)
    atomic_write_json(root / "latest.json", receipt)


def run_batch(
    config: Stage1Config,
    *,
    adapter: Stage1BitableAdapter,
    run_date: str,
    record_id: str | None = None,
    run_id: str | None = None,
    processor: Processor = _default_processor,
) -> dict[str, Any]:
    try:
        date_type.fromisoformat(run_date)
    except ValueError as exc:
        raise Stage1RunnerError("run date must use YYYY-MM-DD") from exc
    connection = adapter.check_connection()
    if not bool(connection.get("connected", connection.get("ok", False))):
        raise Stage1RunnerError("Bitable adapter is not connected")
    records = adapter.list_records()
    candidates = [
        item
        for item in records
        if "omni" in str(_field(item, config.fields.note, "")).lower()
    ]
    if record_id is not None:
        candidates = [item for item in candidates if _record_id(item) == record_id]
        if not candidates:
            raise Stage1RunnerError(f"record {record_id!r} is not an Omni candidate")
    candidate_ids = sorted(_record_id(item) for item in candidates)
    run_id = run_id or f"omni-stage1-{run_date.replace('-', '')}-{datetime.now().strftime('%H%M%S')}"
    _safe_component(run_id, name="run_id")
    run_root = config.work_root / "stage1" / run_date / run_id
    run_root.mkdir(parents=True, exist_ok=True)
    rows = [
        process_record(
            record,
            adapter=adapter,
            config=config,
            run_id=run_id,
            run_root=run_root,
            processor=processor,
        )
        for record in sorted(candidates, key=_record_id)
    ]
    receipt = _receipt(
        run_id=run_id, run_date=run_date, candidates=candidate_ids, rows=rows
    )
    _write_receipts(config, receipt)
    return receipt


def _adapter_from_config(config: Stage1Config, *, run_id: str) -> Stage1BitableAdapter:
    kind = config.adapter.get("kind", "command")
    if kind != "command":
        raise Stage1RunnerError(f"unsupported adapter kind: {kind!r}")
    return CommandBitableAdapter(
        config.adapter,
        audit_root=config.work_root / "adapter-calls" / run_id,
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omni-stage1")
    subparsers = parser.add_subparsers(dest="command", required=True)
    batch = subparsers.add_parser("run-batch", help="run the complete daily Stage 1 batch")
    batch.add_argument("--config", type=Path, required=True)
    batch.add_argument("--date", required=True)
    batch.add_argument("--run-id")
    record = subparsers.add_parser("run-record", help="run or resume one exact Bitable record")
    record.add_argument("--config", type=Path, required=True)
    record.add_argument("--record-id", required=True)
    record.add_argument("--run-id", required=True)
    record.add_argument("--date", default=date_type.today().isoformat())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        effective_run_id = args.run_id or (
            f"omni-stage1-{args.date.replace('-', '')}-{datetime.now().strftime('%H%M%S')}"
        )
        adapter = _adapter_from_config(config, run_id=effective_run_id)
        receipt = run_batch(
            config,
            adapter=adapter,
            run_date=args.date,
            record_id=getattr(args, "record_id", None),
            run_id=effective_run_id,
        )
        print(json.dumps(receipt, ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False, indent=2), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
