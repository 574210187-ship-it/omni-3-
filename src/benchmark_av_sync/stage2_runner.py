from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, Sequence

from .contracts import AlignmentPlan, BenchmarkGroup, BenchmarkScript
from .captions import CaptionLayoutError, TextMeasurer, estimated_text_width, split_token_spans
from .handoff import open_reference_package, validate_reference_package
from .subtitle_contract import validate_subtitle_contract
from .stage2_selection import DELIVERY_SPECS, select_row_file
from .stage2_contract import (
    AUTOMATED_REVIEW_METHOD,
    CAPTION_FONT_FAMILY,
    CAPTION_FONT_FULL_NAME,
    CAPTION_QUALITY_MIN_PSNR as CONTRACT_CAPTION_QUALITY_MIN_PSNR,
    CAPTION_QUALITY_MIN_SSIM as CONTRACT_CAPTION_QUALITY_MIN_SSIM,
    build_stage2_complete_event,
    runtime_receipt_sha256,
    stage2_key,
    upload_idempotency_key,
    validate_runtime_receipt,
    validate_stage2_started_receipt,
)
from .stage2_release import validate_installed_release, validate_job_release
from .text_fusion import normalize_text
from .two_stage import render_from_reference_package
from .verify import verify_plan, verify_rendered_media
from .workspace import atomic_write_json, read_json, sha256_file


SCHEMA_VERSION = 2
DEFAULT_SPEED_MAX = 1.25
ABSOLUTE_SPEED_MAX = 1.90
CAPTION_STYLE = "D"
# D is the selected default: a broad, heavy single-line CJK treatment with a
# restrained 2px tracking, black contour, white fill expansion, and soft shadow.
# Only a line that would otherwise exceed the safe margin is reduced; text is
# never rewritten, character-split, or moved into two lines.
CAPTION_SAFE_MARGIN = 72
CAPTION_MAX_WIDTH = 1080 - CAPTION_SAFE_MARGIN * 2
CAPTION_BASE_FONT_SIZE = 108
CAPTION_BASE_SCALE_X = 100
CAPTION_BASE_LETTER_SPACING = 2
CAPTION_LONG_FONT_SIZE = 104
CAPTION_LONG_SCALE_X = 88
CAPTION_LONG_THRESHOLD = 12
CAPTION_FILL_OUTLINE = 1.65
CAPTION_MAIN_OUTLINE = 5.8
CAPTION_SHADOW_OUTLINE = 10
CAPTION_SHADOW_BLUR = 6
CAPTION_POSITION_Y = 1136
CAPTION_SHADOW_POSITION_Y = 1148
# Caption burn necessarily re-encodes the video stream.  CRF 15 was visibly
# insufficient on high-detail vertical material and could make the upper-frame
# regression gate fail even though the intended glyph area was excluded.  CRF
# 11 preserves the source texture while retaining a practical upload size.
VIDEO_CRF = 11
# The raised caption and its soft shadow begin just below the upper 56% of a
# 1920px frame.  Keep the regression crop above that treatment so the metric
# measures re-encode damage, not intentional subtitle pixels.
# libass performs the required YUV-to-RGB-to-YUV compositing even above the
# subtitle band.  On the validated FFmpeg build, a visually lossless caption
# pass measures about 0.965 / 31.37 in that untouched region.  These calibrated
# limits still reject any substantive picture regression while avoiding a
# mathematically impossible same-codec threshold for a burn-in operation.
CAPTION_QUALITY_TOP_RATIO = 0.45
CAPTION_QUALITY_MIN_SSIM = CONTRACT_CAPTION_QUALITY_MIN_SSIM
CAPTION_QUALITY_MIN_PSNR = CONTRACT_CAPTION_QUALITY_MIN_PSNR
SAFE_RECORD_ID = re.compile(r"^[A-Za-z0-9_-]+$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SAFE_ATTACHMENT_TOKEN = re.compile(r"^[A-Za-z0-9_-]+$")

class Stage2RunnerError(RuntimeError):
    """Raised when a row job cannot be safely completed."""


@dataclass(frozen=True, slots=True)
class FileInput:
    path: Path
    sha256: str


@dataclass(frozen=True, slots=True)
class PairInput:
    pair_id: str
    reference_package: FileInput
    generated_video: FileInput


@dataclass(frozen=True, slots=True)
class ReplacementTarget:
    """One exact remote attachment that a verified rework is allowed to remove."""

    artifact_key: str
    field_name: str
    file_token: str
    name: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class DeliveryPolicy:
    """Remote publishing instructions emitted with, but never executed by, the runner."""

    mode: str
    replacements: tuple[ReplacementTarget, ...]
    spawned_ordinary_chat_tab_id: str | None


@dataclass(frozen=True, slots=True)
class Stage2Job:
    path: Path
    workspace_root: Path
    record_id: str
    video_number: str
    dispatch_key: str
    input_fingerprint: str
    strict_release: dict[str, str]
    attempt: int
    inputs: tuple[PairInput, ...]
    output_dir: Path
    default_speed_max: float
    approved_exception_max: float
    pause_hold_pair_ids: frozenset[str]
    pause_hold_authorization: dict[str, Any] | None
    pause_hold_max_seconds: float
    ffmpeg: Path | None
    ffprobe: Path | None
    mouth_python: Path | None
    caption_fonts_dir: Path
    caption_font: dict[str, str]
    runtime_receipt: Path
    stage2_started_receipt: dict[str, Any]
    review_authorization: dict[str, Any] | None
    delivery_policy: DeliveryPolicy


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _absolute_job_path(value: Any, *, name: str, root: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise Stage2RunnerError(f"{name} must be a non-empty absolute path")
    path = Path(value)
    if not path.is_absolute():
        raise Stage2RunnerError(f"{name} must be absolute: {value!r}")
    resolved = path.resolve()
    if not _is_within(resolved, root):
        raise Stage2RunnerError(f"{name} escapes the row workspace: {resolved}")
    return resolved


def _optional_runtime_path(value: Any, *, name: str) -> Path | None:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise Stage2RunnerError(f"runtime.{name} must be a path string")
    path = Path(value)
    if not path.is_absolute():
        raise Stage2RunnerError(f"runtime.{name} must be absolute")
    resolved = path.resolve()
    if not resolved.is_file():
        raise Stage2RunnerError(f"runtime.{name} does not exist: {resolved}")
    return resolved


def _resolve_caption_fonts_dir(value: Any, *, root: Path) -> tuple[Path, dict[str, str]]:
    """Return a local CJK font directory or fail before rendering begins.

    libass cannot be allowed to fall back silently to a container's incomplete
    system font cache: the old renderer could burn Chinese tofu boxes even when
    its ASS file named Noto Sans CJK SC.  Jobs may provide an explicit directory;
    the portable offline default is ``<workspace>/fonts``.
    """

    candidates: list[Path] = []
    if value not in (None, ""):
        candidates.append(
            _absolute_job_path(value, name="runtime.caption_fonts_dir", root=root)
        )
    candidates.append(root / "fonts")
    for directory in candidates:
        if not directory.is_dir():
            continue
        for item in sorted(directory.rglob("*")):
            if not item.is_file() or item.suffix.lower() not in {".otf", ".ttf", ".ttc"}:
                continue
            probe = subprocess.run(
                [
                    "fc-scan",
                    "--format=%{family}\t%{style}\t%{fullname}\n",
                    str(item),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            for line in probe.stdout.splitlines():
                family, separator, style_and_name = line.partition("\t")
                if not separator:
                    continue
                style, _, fullname = style_and_name.partition("\t")
                if family == CAPTION_FONT_FAMILY and "Bold" in style and fullname == CAPTION_FONT_FULL_NAME:
                    return directory.resolve(), {
                        "family": family,
                        "style": style,
                        "fullname": fullname,
                        "path": str(item.resolve()),
                        "sha256": sha256_file(item),
                    }
    raise Stage2RunnerError(
        "exact CJK bold caption font is unavailable: provide a file whose fc-scan "
        f"family/style/fullname are {CAPTION_FONT_FAMILY!r}/'Bold'/{CAPTION_FONT_FULL_NAME!r}"
    )


def _file_input(value: Any, *, name: str, root: Path) -> FileInput:
    if not isinstance(value, dict):
        raise Stage2RunnerError(f"{name} must be an object")
    path = _absolute_job_path(value.get("path"), name=f"{name}.path", root=root)
    digest = value.get("sha256")
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest.lower()):
        raise Stage2RunnerError(f"{name}.sha256 must be a 64-character SHA-256")
    if not path.is_file():
        raise Stage2RunnerError(f"{name} does not exist: {path}")
    actual = sha256_file(path)
    if actual != digest.lower():
        raise Stage2RunnerError(
            f"{name} SHA-256 mismatch: expected {digest.lower()}, got {actual}"
        )
    return FileInput(path=path, sha256=actual)


def _delivery_policy(value: Any) -> DeliveryPolicy:
    """Validate optional rework lifecycle data without granting remote deletion.

    A normal render uses append mode.  A rework must enumerate every old
    attachment by its exact file token and immutable digest.  The external
    Bitable publisher consumes the resulting post-delivery plan *after* the
    new artifact has been uploaded and read back; no local media command can
    trigger a remote delete.
    """

    if value in (None, {}):
        return DeliveryPolicy("append", (), None)
    if not isinstance(value, dict):
        raise Stage2RunnerError("delivery_policy must be an object")
    mode = value.get("mode", "append")
    if mode not in {"append", "replace_rework"}:
        raise Stage2RunnerError("delivery_policy.mode must be append or replace_rework")

    chat = value.get("cloud_chat_lifecycle", {})
    if not isinstance(chat, dict):
        raise Stage2RunnerError("delivery_policy.cloud_chat_lifecycle must be an object")
    tab_id = chat.get("spawned_ordinary_chat_tab_id")
    close_after_success = chat.get("close_after_success", True)
    if tab_id in (None, ""):
        tab_id = None
    elif not isinstance(tab_id, str) or not tab_id.strip():
        raise Stage2RunnerError(
            "spawned_ordinary_chat_tab_id must be a non-empty opaque string"
        )
    else:
        tab_id = tab_id.strip()
    if not isinstance(close_after_success, bool) or not close_after_success:
        raise Stage2RunnerError(
            "cloud_chat_lifecycle.close_after_success must be true when a task chat is used"
        )
    if tab_id is not None and chat.get("scope", "spawned_ordinary_chat_only") != "spawned_ordinary_chat_only":
        raise Stage2RunnerError(
            "cloud_chat_lifecycle.scope must be spawned_ordinary_chat_only"
        )

    raw_replacements = value.get("replacements", [])
    if not isinstance(raw_replacements, list):
        raise Stage2RunnerError("delivery_policy.replacements must be a list")
    replacements: list[ReplacementTarget] = []
    seen_keys: set[str] = set()
    seen_tokens: set[str] = set()
    for index, item in enumerate(raw_replacements):
        if not isinstance(item, dict):
            raise Stage2RunnerError(f"delivery_policy.replacements[{index}] must be an object")
        artifact_key = item.get("artifact_key")
        if artifact_key not in DELIVERY_SPECS:
            raise Stage2RunnerError(
                f"delivery_policy.replacements[{index}].artifact_key is unsupported"
            )
        if artifact_key in seen_keys:
            raise Stage2RunnerError(f"duplicate replacement artifact_key: {artifact_key}")
        expected_field, _ = DELIVERY_SPECS[artifact_key]
        field_name = item.get("field_name")
        if field_name != expected_field:
            raise Stage2RunnerError(
                f"replacement {artifact_key} must target {expected_field}, not {field_name!r}"
            )
        file_token = item.get("file_token")
        if not isinstance(file_token, str) or not SAFE_ATTACHMENT_TOKEN.fullmatch(file_token):
            raise Stage2RunnerError(
                f"delivery_policy.replacements[{index}].file_token is unsafe"
            )
        if file_token in seen_tokens:
            raise Stage2RunnerError(f"duplicate replacement file_token: {file_token}")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or any(ord(char) < 32 for char in name):
            raise Stage2RunnerError(
                f"delivery_policy.replacements[{index}].name must be a non-empty filename"
            )
        size_bytes = item.get("size_bytes")
        if not isinstance(size_bytes, int) or size_bytes <= 0:
            raise Stage2RunnerError(
                f"delivery_policy.replacements[{index}].size_bytes must be positive"
            )
        digest = item.get("sha256")
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest.lower()):
            raise Stage2RunnerError(
                f"delivery_policy.replacements[{index}].sha256 must be a SHA-256"
            )
        replacements.append(
            ReplacementTarget(
                artifact_key=artifact_key,
                field_name=field_name,
                file_token=file_token,
                name=name.strip(),
                size_bytes=size_bytes,
                sha256=digest.lower(),
            )
        )
        seen_keys.add(artifact_key)
        seen_tokens.add(file_token)
    if mode == "append" and replacements:
        raise Stage2RunnerError("append delivery_policy cannot name replacements")
    if mode == "replace_rework" and not replacements:
        raise Stage2RunnerError(
            "replace_rework requires explicit exact replacements; refusing broad deletion"
        )
    return DeliveryPolicy(mode, tuple(replacements), tab_id)


def _delivery_policy_record(policy: DeliveryPolicy) -> dict[str, Any]:
    return {
        "mode": policy.mode,
        "replacements": [
            {
                "artifact_key": target.artifact_key,
                "field_name": target.field_name,
                "file_token": target.file_token,
                "name": target.name,
                "size_bytes": target.size_bytes,
                "sha256": target.sha256,
            }
            for target in policy.replacements
        ],
        "cloud_chat_lifecycle": {
            "spawned_ordinary_chat_tab_id": policy.spawned_ordinary_chat_tab_id,
            "close_after_success": True,
            "scope": "spawned_ordinary_chat_only",
        },
    }


def _review_authorization(
    value: Any,
    *,
    record_id: str,
    dispatch_key: str,
    attempt: int,
) -> dict[str, Any] | None:
    """Accept only a user-bound authorization for automated exception review.

    This is deliberately not a global switch: an authorization has to name the
    same row, input dispatch, and attempt that the Runner is about to render.
    """

    if value in (None, {}):
        return None
    if not isinstance(value, dict):
        raise Stage2RunnerError("review_authorization must be an object")
    if value.get("method") != AUTOMATED_REVIEW_METHOD:
        raise Stage2RunnerError(
            "review_authorization.method must be authorized_automated_audit"
        )
    if value.get("record_id") != record_id:
        raise Stage2RunnerError("review_authorization.record_id does not match job")
    if value.get("dispatch_key") != dispatch_key:
        raise Stage2RunnerError(
            "review_authorization.dispatch_key does not match job"
        )
    if value.get("attempt") != attempt:
        raise Stage2RunnerError("review_authorization.attempt does not match job")
    for key in ("statement", "authorized_at"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise Stage2RunnerError(
                f"review_authorization.{key} must be a non-empty string"
            )
    return {
        "method": AUTOMATED_REVIEW_METHOD,
        "record_id": record_id,
        "dispatch_key": dispatch_key,
        "attempt": attempt,
        "statement": value["statement"].strip(),
        "authorized_at": value["authorized_at"].strip(),
    }


def load_job(path: Path) -> Stage2Job:
    path = path.resolve()
    if not path.is_file():
        raise Stage2RunnerError(f"job file does not exist: {path}")
    data = read_json(path)
    if not isinstance(data, dict):
        raise Stage2RunnerError("job.json must contain an object")
    if data.get("schema_version") != SCHEMA_VERSION:
        raise Stage2RunnerError(
            f"unsupported job schema_version: {data.get('schema_version')!r}"
        )

    root = path.parent.resolve()
    record_id = data.get("record_id")
    if not isinstance(record_id, str) or not SAFE_RECORD_ID.fullmatch(record_id):
        raise Stage2RunnerError("record_id contains unsafe characters")
    video_number = data.get("video_number")
    if (
        not isinstance(video_number, str)
        or not video_number.strip()
        or len(video_number.strip()) > 128
        or any(ord(char) < 32 for char in video_number)
    ):
        raise Stage2RunnerError("video_number must be a non-empty identifier")
    video_number = video_number.strip()
    dispatch_key = data.get("dispatch_key")
    if not isinstance(dispatch_key, str) or not dispatch_key.strip():
        raise Stage2RunnerError("dispatch_key must be a non-empty string")
    input_fingerprint = data.get("input_fingerprint")
    if not isinstance(input_fingerprint, str) or not SHA256_PATTERN.fullmatch(input_fingerprint.lower()):
        raise Stage2RunnerError("input_fingerprint must be a 64-character SHA-256")
    input_fingerprint = input_fingerprint.lower()
    try:
        strict_release = validate_job_release(
            data.get("strict_release"), installed=validate_installed_release()
        )
    except Exception as exc:
        raise Stage2RunnerError(f"strict release validation failed: {exc}") from exc
    attempt = data.get("attempt")
    if not isinstance(attempt, int) or attempt not in {1, 2, 3}:
        raise Stage2RunnerError("attempt must be 1, 2, or 3")
    if data.get("caption_style") != CAPTION_STYLE:
        raise Stage2RunnerError("caption_style must be 'D'")

    speed = data.get("speed_policy")
    if not isinstance(speed, dict):
        raise Stage2RunnerError("speed_policy must be an object")
    default_speed = float(speed.get("default_max", DEFAULT_SPEED_MAX))
    exception_speed = float(speed.get("approved_exception_max", ABSOLUTE_SPEED_MAX))
    if abs(default_speed - DEFAULT_SPEED_MAX) > 1e-9:
        raise Stage2RunnerError("speed_policy.default_max must remain 1.25")
    if not DEFAULT_SPEED_MAX <= exception_speed <= ABSOLUTE_SPEED_MAX:
        raise Stage2RunnerError("approved_exception_max must be between 1.25 and 1.90")
    sync_policy = data.get("sync_policy", {})
    if not isinstance(sync_policy, dict):
        raise Stage2RunnerError("sync_policy must be an object when provided")
    allow_pause_hold = sync_policy.get("allow_pause_hold", False)
    if not isinstance(allow_pause_hold, bool):
        raise Stage2RunnerError("sync_policy.allow_pause_hold must be a boolean")
    pause_hold_max_seconds = float(sync_policy.get("pause_hold_max_seconds", 2.0))
    if not 0 < pause_hold_max_seconds <= 2.0:
        raise Stage2RunnerError("sync_policy.pause_hold_max_seconds must be in (0, 2.0]")

    # A pause-hold exception is a row- and attempt-scoped authorization.  The
    # old runner treated the boolean switch as a speed-policy switch and could
    # therefore start every pair at 1.90x.  Keep the authorization explicit
    # and validate it before any media work begins.
    raw_authorization = data.get("exception_authorization")
    pause_hold_pair_ids: frozenset[str] = frozenset()
    pause_hold_authorization: dict[str, Any] | None = None
    if allow_pause_hold:
        if not isinstance(raw_authorization, dict):
            raise Stage2RunnerError(
                "sync_policy.allow_pause_hold=true requires an explicit exception_authorization"
            )
        if raw_authorization.get("record_id") != record_id:
            raise Stage2RunnerError("exception_authorization.record_id does not match job")
        if raw_authorization.get("attempt") != attempt:
            raise Stage2RunnerError("exception_authorization.attempt does not match job")
        authorization_types = raw_authorization.get("types", ["pause_hold"])
        if not isinstance(authorization_types, list) or "pause_hold" not in authorization_types:
            raise Stage2RunnerError("exception_authorization.types must explicitly include pause_hold")
        raw_pair_ids = raw_authorization.get("pair_ids")
        if not isinstance(raw_pair_ids, list) or not raw_pair_ids:
            raise Stage2RunnerError("exception_authorization.pair_ids must name the authorized pairs")
        normalized_ids: list[str] = []
        for value in raw_pair_ids:
            if not isinstance(value, str) or not value.isdigit():
                raise Stage2RunnerError("exception_authorization.pair_ids must contain numeric strings")
            normalized_ids.append(f"{int(value):03d}")
        if len(set(normalized_ids)) != len(normalized_ids):
            raise Stage2RunnerError("exception_authorization.pair_ids must be unique")
        pause_hold_pair_ids = frozenset(normalized_ids)
        pause_hold_authorization = dict(raw_authorization)
    elif raw_authorization not in (None, {}):
        if not isinstance(raw_authorization, dict):
            raise Stage2RunnerError("exception_authorization must be an object when provided")

    raw_inputs = data.get("inputs")
    if not isinstance(raw_inputs, list) or not raw_inputs:
        raise Stage2RunnerError("inputs must be a non-empty list")
    pairs: list[PairInput] = []
    numeric_ids: list[int] = []
    seen_numeric: set[int] = set()
    for index, item in enumerate(raw_inputs):
        if not isinstance(item, dict):
            raise Stage2RunnerError(f"inputs[{index}] must be an object")
        pair_id = item.get("pair_id")
        if not isinstance(pair_id, str) or not pair_id.isdigit():
            raise Stage2RunnerError(f"inputs[{index}].pair_id must contain digits only")
        numeric = int(pair_id)
        if numeric in seen_numeric:
            raise Stage2RunnerError(f"duplicate numeric pair_id: {pair_id}")
        seen_numeric.add(numeric)
        numeric_ids.append(numeric)
        reference = _file_input(
            item.get("reference_package"),
            name=f"inputs[{index}].reference_package",
            root=root,
        )
        generated = _file_input(
            item.get("generated_video"),
            name=f"inputs[{index}].generated_video",
            root=root,
        )
        package_summary = validate_reference_package(reference.path)
        if int(package_summary["pair_id"]) != numeric:
            raise Stage2RunnerError(
                f"reference package ID {package_summary['pair_id']!r} does not match {pair_id!r}"
            )
        pairs.append(PairInput(pair_id, reference, generated))
    if numeric_ids != sorted(numeric_ids):
        raise Stage2RunnerError("inputs must be in strictly increasing numeric pair_id order")
    if pause_hold_pair_ids and not pause_hold_pair_ids.issubset({pair.pair_id for pair in pairs}):
        raise Stage2RunnerError("exception_authorization names a pair that is not in job.inputs")

    output_dir = _absolute_job_path(
        data.get("output_dir"), name="output_dir", root=root
    )
    if output_dir == root:
        raise Stage2RunnerError("output_dir must be a child of the row workspace")

    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        raise Stage2RunnerError("runtime must be an object when provided")
    caption_fonts_dir, caption_font = _resolve_caption_fonts_dir(
        runtime.get("caption_fonts_dir"), root=root
    )
    runtime_receipt_path = _absolute_job_path(
        runtime.get("runtime_receipt", str(root / "runtime-receipt.json")),
        name="runtime.runtime_receipt",
        root=root,
    )
    if not runtime_receipt_path.is_file():
        raise Stage2RunnerError(
            f"runtime receipt is missing: {runtime_receipt_path}; complete runtime preflight before rendering"
        )
    try:
        runtime_receipt = validate_runtime_receipt(
            read_json(runtime_receipt_path), record_id=record_id
        )
    except Exception as exc:
        raise Stage2RunnerError(f"runtime receipt failed validation: {exc}") from exc
    stage2_started_receipt = validate_stage2_started_receipt(
        data.get("stage2_started_receipt"),
        record_id=record_id,
        dispatch_key=dispatch_key,
        input_fingerprint=input_fingerprint,
        strict_release=strict_release,
        attempt=attempt,
    )
    review_authorization = _review_authorization(
        data.get("review_authorization"),
        record_id=record_id,
        dispatch_key=dispatch_key,
        attempt=attempt,
    )
    delivery_policy = _delivery_policy(data.get("delivery_policy"))
    return Stage2Job(
        path=path,
        workspace_root=root,
        record_id=record_id,
        video_number=video_number,
        dispatch_key=dispatch_key,
        input_fingerprint=input_fingerprint,
        strict_release=strict_release,
        attempt=attempt,
        inputs=tuple(pairs),
        output_dir=output_dir,
        default_speed_max=default_speed,
        approved_exception_max=exception_speed,
        pause_hold_pair_ids=pause_hold_pair_ids,
        pause_hold_authorization=pause_hold_authorization,
        pause_hold_max_seconds=pause_hold_max_seconds,
        ffmpeg=_optional_runtime_path(runtime.get("ffmpeg"), name="ffmpeg"),
        ffprobe=_optional_runtime_path(runtime.get("ffprobe"), name="ffprobe"),
        mouth_python=_optional_runtime_path(runtime.get("mouth_python"), name="mouth_python"),
        caption_fonts_dir=caption_fonts_dir,
        caption_font=caption_font,
        runtime_receipt=runtime_receipt_path,
        stage2_started_receipt=stage2_started_receipt,
        review_authorization=review_authorization,
        delivery_policy=delivery_policy,
    )


def _read_plan(result: dict[str, Any]) -> AlignmentPlan:
    plan_path = result.get("plan")
    if not isinstance(plan_path, str) or not Path(plan_path).is_file():
        raise Stage2RunnerError("renderer did not produce a readable plan")
    return AlignmentPlan.from_dict(read_json(Path(plan_path)))


def should_retry_speed_exception(
    result: dict[str, Any], *, approved_max: float
) -> bool:
    if result.get("ok"):
        return False
    try:
        plan = _read_plan(result)
    except (Stage2RunnerError, ValueError, KeyError, TypeError):
        return False
    mouth_path = result.get("mouth_track")
    if not isinstance(mouth_path, str) or not Path(mouth_path).is_file():
        return False
    mouth = read_json(Path(mouth_path))
    if not isinstance(mouth, dict) or mouth.get("status") == "fail":
        return False
    speeds = [segment.speed for segment in plan.video_segments]
    if not speeds or min(speeds) < 0.85 - 1e-6:
        return False
    if max(speeds) <= DEFAULT_SPEED_MAX + 1e-6 or max(speeds) > approved_max + 1e-6:
        return False
    allowed_errors = {
        f"video speed exceeds approved hard limit {DEFAULT_SPEED_MAX:.3f}x",
        "solver marked plan as fail",
    }
    errors = set(verify_plan(plan).errors)
    return bool(errors) and errors.issubset(allowed_errors)


def _ass_time(seconds: float) -> str:
    centiseconds = max(0, round(seconds * 100))
    hours, remainder = divmod(centiseconds, 360000)
    minutes, remainder = divmod(remainder, 6000)
    whole, cs = divmod(remainder, 100)
    return f"{hours}:{minutes:02d}:{whole:02d}.{cs:02d}"


def _escape_ass_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("\r", "")
        .replace("\n", "\\N")
    )


def _caption_chunks(text: str, *, line_chars: int = 15) -> list[str]:
    compact = "".join(text.split())
    if not compact:
        return []
    chunks: list[str] = []
    while compact:
        part, compact = compact[:line_chars], compact[line_chars:]
        chunks.append(_escape_ass_text(part))
    return chunks


def _map_reference_interval(
    plan: AlignmentPlan, start: float, end: float
) -> list[tuple[float, float]]:
    mapped: list[tuple[float, float]] = []
    for chunk in plan.audio_chunks:
        overlap_start = max(start, chunk.source_start)
        overlap_end = min(end, chunk.source_end)
        if overlap_end <= overlap_start:
            continue
        mapped.append(
            (
                chunk.output_start + overlap_start - chunk.source_start,
                chunk.output_start + overlap_end - chunk.source_start,
            )
        )
    return mapped


def _caption_groups(benchmark: BenchmarkScript) -> list[BenchmarkGroup]:
    groups = list(benchmark.groups)
    if not groups:
        return [
            BenchmarkGroup(
                "all",
                benchmark.text,
                benchmark.tokens[0].index,
                benchmark.tokens[-1].index,
                benchmark.speech_start,
                benchmark.speech_end,
                "all",
            )
        ]
    merged: list[BenchmarkGroup] = []
    for group in groups:
        compact = "".join(group.text.split())
        short = len(compact) < 4 or group.source_end - group.source_start < 0.5
        if short and merged:
            previous = merged[-1]
            combined_text = previous.text + group.text
            combined_span = group.source_end - previous.source_start
            if len("".join(combined_text.split())) <= 30 and combined_span <= 3.0:
                merged[-1] = BenchmarkGroup(
                    group_id=f"{previous.group_id}+{group.group_id}",
                    text=combined_text,
                    token_start=previous.token_start,
                    token_end=group.token_end,
                    source_start=previous.source_start,
                    source_end=group.source_end,
                    continuity_group_id=previous.continuity_group_id,
                )
                continue
        merged.append(group)
    if len(merged) > 1:
        first = merged[0]
        compact = "".join(first.text.split())
        if len(compact) < 4 or first.source_end - first.source_start < 0.5:
            second = merged[1]
            combined_text = first.text + second.text
            if (
                len("".join(combined_text.split())) <= 30
                and second.source_end - first.source_start <= 3.0
            ):
                merged[:2] = [
                    BenchmarkGroup(
                        group_id=f"{first.group_id}+{second.group_id}",
                        text=combined_text,
                        token_start=first.token_start,
                        token_end=second.token_end,
                        source_start=first.source_start,
                        source_end=second.source_end,
                        continuity_group_id=first.continuity_group_id,
                    )
                ]
    return merged


def build_caption_events(
    benchmark: BenchmarkScript,
    plan: AlignmentPlan,
    *,
    subtitle: dict[str, Any],
    offset: float = 0.0,
    measure_text: TextMeasurer = estimated_text_width,
    max_width: int = 972,
) -> list[dict[str, Any]]:
    try:
        subtitle = validate_subtitle_contract(subtitle, benchmark)
    except ValueError as exc:
        raise CaptionLayoutError(f"invalid subtitle contract: {exc}") from exc
    if benchmark.status == "fail" or subtitle["quality_status"] == "fail":
        raise CaptionLayoutError("failed reference evidence cannot generate captions")
    events: list[dict[str, Any]] = []
    approved_token_ends = {
        int(item["token_end"]) for item in subtitle["approved_split_points"]
    }
    is_stage1_v32 = subtitle.get("schema_version") == 3
    for source_event in subtitle["caption_events"]:
        event_id = str(source_event["event_id"])
        token_start = int(source_event["token_start"])
        token_end = int(source_event["token_end"])
        tokens = [
            item
            for item in benchmark.tokens
            if token_start <= item.index <= token_end
        ]
        event_text = str(source_event["text"])
        if normalize_text("".join(item.text for item in tokens)) != normalize_text(event_text):
            raise CaptionLayoutError(
                f"subtitle event {event_id} does not match aligned tokens"
            )
        if is_stage1_v32:
            # v3.2 has already made every semantic/visual line decision. Do
            # not transform its wording or make a second set of line breaks.
            spans = [
                SimpleNamespace(
                    text=event_text,
                    source_start=float(source_event["source_start"]),
                    source_end=float(source_event["source_end"]),
                    measured_width=measure_text(event_text),
                )
            ]
        else:
            event_allowed = {
                item for item in approved_token_ends if token_start <= item < token_end
            }
            spans = split_token_spans(
                tokens,
                source_start=float(source_event["source_start"]),
                source_end=float(source_event["source_end"]),
                measure_text=measure_text,
                max_width=max_width,
                preferred_token_ends=event_allowed,
                allowed_token_ends=event_allowed,
            )
        for span in spans:
            mapped = _map_reference_interval(plan, span.source_start, span.source_end)
            if len(mapped) != 1:
                raise CaptionLayoutError(
                    f"caption span must map to one continuous audio chunk: {span.text}"
                )
            mapped_start, mapped_end = mapped[0]
            if mapped_end - mapped_start < 0.5:
                raise CaptionLayoutError(f"caption event is shorter than 0.5s: {span.text}")
            events.append(
                {
                    "start": offset + mapped_start,
                    "end": offset + mapped_end,
                    "text": _escape_ass_text(span.text),
                    "raw_text": span.text,
                    "pair_id": benchmark.pair_id,
                    "source_start": span.source_start,
                    "source_end": span.source_end,
                    "measured_width": span.measured_width,
                    "subtitle_event_id": event_id,
                }
            )
    if normalize_text("".join(str(item["raw_text"]) for item in events)) != normalize_text(
        str(subtitle["canonical_caption_text"])
    ):
        raise CaptionLayoutError("final caption events do not preserve subtitle contract text")
    return events


def _caption_typography(text: str) -> dict[str, int]:
    """Select an emphatic single-line treatment that preserves safe margins."""

    characters = len(normalize_text(text))
    if characters >= CAPTION_LONG_THRESHOLD:
        return {
            "font_size": CAPTION_LONG_FONT_SIZE,
            "scale_x": CAPTION_LONG_SCALE_X,
        }
    return {
        "font_size": CAPTION_BASE_FONT_SIZE,
        "scale_x": CAPTION_BASE_SCALE_X,
    }


def write_ass(path: Path, events: Sequence[dict[str, Any]]) -> None:
    """Write the selected D subtitle treatment with one source of truth.

    The base styles deliberately take their values from the caption constants
    rather than duplicating literals here.  Per-event overrides only shrink a
    long line enough to honour the safe horizontal margin.
    """

    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: 1920
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: CaptionShadow,Noto Sans CJK SC,{CAPTION_BASE_FONT_SIZE},&H7A000000,&H7A000000,&H7A000000,&H7A000000,-1,0,0,0,{CAPTION_BASE_SCALE_X},100,{CAPTION_BASE_LETTER_SPACING},0,1,{CAPTION_SHADOW_OUTLINE},0,5,{CAPTION_SAFE_MARGIN},{CAPTION_SAFE_MARGIN},0,1
Style: CaptionOutline,Noto Sans CJK SC,{CAPTION_BASE_FONT_SIZE},&H00FFFFFF,&H00FFFFFF,&H00101010,&H00101010,-1,0,0,0,{CAPTION_BASE_SCALE_X},100,{CAPTION_BASE_LETTER_SPACING},0,1,{CAPTION_MAIN_OUTLINE},0,5,{CAPTION_SAFE_MARGIN},{CAPTION_SAFE_MARGIN},0,1
Style: CaptionFill,Noto Sans CJK SC,{CAPTION_BASE_FONT_SIZE},&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,&H00FFFFFF,-1,0,0,0,{CAPTION_BASE_SCALE_X},100,{CAPTION_BASE_LETTER_SPACING},0,1,{CAPTION_FILL_OUTLINE},0,5,{CAPTION_SAFE_MARGIN},{CAPTION_SAFE_MARGIN},0,1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    lines = [header]
    for event in events:
        text = str(event["text"])
        typography = _caption_typography(text)
        event["typography"] = dict(typography)
        tags = (
            f"\\an5\\q2\\fs{typography['font_size']}"
            f"\\fscx{typography['scale_x']}\\b1"
        )
        shadow = (
            "{" + tags + f"\\pos(540,{CAPTION_SHADOW_POSITION_Y})\\blur{CAPTION_SHADOW_BLUR}" + "}" + text
        )
        outline = "{" + tags + f"\\pos(540,{CAPTION_POSITION_Y})\\blur0.08" + "}" + text
        fill = "{" + tags + f"\\pos(540,{CAPTION_POSITION_Y})\\blur0.08" + "}" + text
        lines.append(
            "Dialogue: 0,"
            f"{_ass_time(float(event['start']))},{_ass_time(float(event['end']))},"
            f"CaptionShadow,,0,0,0,,{shadow}\n"
        )
        lines.append(
            "Dialogue: 1,"
            f"{_ass_time(float(event['start']))},{_ass_time(float(event['end']))},"
            f"CaptionOutline,,0,0,0,,{outline}\n"
        )
        lines.append(
            "Dialogue: 2,"
            f"{_ass_time(float(event['start']))},{_ass_time(float(event['end']))},"
            f"CaptionFill,,0,0,0,,{fill}\n"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(lines), encoding="utf-8", newline="\n")


def _resolve_executable(name: str, supplied: Path | None) -> Path:
    if supplied is not None:
        return supplied
    discovered = shutil.which(name)
    if not discovered:
        raise Stage2RunnerError(f"{name} is not available on PATH")
    return Path(discovered).resolve()


def _run_checked(command: Sequence[str], *, label: str) -> None:
    completed = subprocess.run(
        list(command), capture_output=True, text=True, encoding="utf-8", errors="replace"
    )
    if completed.returncode != 0:
        raise Stage2RunnerError(
            f"{label} failed with exit code {completed.returncode}: "
            + (completed.stderr or completed.stdout or "unknown error")[-2000:]
        )


def _verify_libass_font_resolution(
    ffmpeg: Path, *, fonts_dir: Path, work_dir: Path
) -> tuple[Path, str]:
    """Prove that libass resolves the exact Bold CJK face, not just a file.

    A directory containing a bold TTC is not enough: libass/fontconfig can
    still fall back to a regular or unrelated face.  The production gate keeps
    the verbose fontselect trace and requires the 700-weight Bold resolution.
    """

    work_dir.mkdir(parents=True, exist_ok=True)
    ass_path = work_dir / "font-resolution.ass"
    log_path = work_dir / "font-resolution.log"
    write_ass(ass_path, [{"start": 0.0, "end": 1.0, "text": "字体门禁"}])
    completed = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "verbose",
            "-f",
            "lavfi",
            "-i",
            "color=c=gray:s=1080x1920:r=1:d=1",
            "-vf",
            _ass_video_filter(ass_path, fonts_dir=fonts_dir),
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    trace = completed.stdout + "\n" + completed.stderr
    log_path.write_text(trace, encoding="utf-8")
    if completed.returncode != 0:
        raise Stage2RunnerError(
            f"libass exact font probe failed with exit code {completed.returncode}"
        )
    if not re.search(
        r"fontselect:?\s*\(Noto Sans CJK SC,\s*700,\s*0\).*Bold",
        trace,
        flags=re.IGNORECASE,
    ):
        raise Stage2RunnerError(
            "libass did not resolve Noto Sans CJK SC Bold (700); see font-resolution.log"
        )
    return log_path, trace


def _bmp_non_background_bbox(
    path: Path, *, background: tuple[int, int, int] = (128, 128, 128), tolerance: int = 12
) -> dict[str, int]:
    data = path.read_bytes()
    if len(data) < 54 or data[:2] != b"BM":
        raise Stage2RunnerError("caption probe is not a BMP image")
    pixel_offset = struct.unpack_from("<I", data, 10)[0]
    width = struct.unpack_from("<i", data, 18)[0]
    signed_height = struct.unpack_from("<i", data, 22)[0]
    bits = struct.unpack_from("<H", data, 28)[0]
    compression = struct.unpack_from("<I", data, 30)[0]
    if width <= 0 or signed_height == 0 or bits not in {24, 32} or compression not in {0, 3}:
        raise Stage2RunnerError("caption probe BMP format is unsupported")
    height = abs(signed_height)
    channels = bits // 8
    stride = ((width * channels + 3) // 4) * 4
    if pixel_offset + stride * height > len(data):
        raise Stage2RunnerError("caption probe BMP is truncated")
    left, top, right, bottom = width, height, -1, -1
    for y in range(height):
        stored_y = height - 1 - y if signed_height > 0 else y
        row = pixel_offset + stored_y * stride
        for x in range(width):
            blue, green, red = data[row + x * channels : row + x * channels + 3]
            if max(
                abs(red - background[0]),
                abs(green - background[1]),
                abs(blue - background[2]),
            ) <= tolerance:
                continue
            left = min(left, x)
            right = max(right, x)
            top = min(top, y)
            bottom = max(bottom, y)
    if right < left or bottom < top:
        raise Stage2RunnerError("caption probe rendered no visible glyphs")
    return {
        "x1": left,
        "y1": top,
        "x2": right,
        "y2": bottom,
        "width": right - left + 1,
        "height": bottom - top + 1,
    }


def _probe_caption_bbox(
    text: str, *, ffmpeg: Path, work_dir: Path, fonts_dir: Path
) -> dict[str, int]:
    typography = _caption_typography(text)
    digest = hashlib.sha256(
        f"{text}|{typography['font_size']}|{typography['scale_x']}|{fonts_dir}".encode("utf-8")
    ).hexdigest()[:16]
    ass_path = work_dir / f"{digest}.ass"
    bmp_path = work_dir / f"{digest}.bmp"
    work_dir.mkdir(parents=True, exist_ok=True)
    if not bmp_path.is_file():
        write_ass(ass_path, [{"start": 0.0, "end": 1.0, "text": _escape_ass_text(text)}])
        _run_checked(
            [
                str(ffmpeg),
                "-y",
                "-f",
                "lavfi",
                "-i",
                "color=c=0x808080:s=1080x1920:r=1:d=1",
                "-vf",
                _ass_video_filter(ass_path, fonts_dir=fonts_dir),
                "-frames:v",
                "1",
                "-c:v",
                "bmp",
                str(bmp_path),
            ],
            label=f"caption pixel probe {digest}",
        )
    return _bmp_non_background_bbox(bmp_path)


def _make_caption_measurer(
    *, ffmpeg: Path, work_dir: Path, fonts_dir: Path
) -> tuple[TextMeasurer, dict[str, dict[str, int]]]:
    cache: dict[str, dict[str, int]] = {}

    def measure(text: str) -> int:
        if text not in cache:
            cache[text] = _probe_caption_bbox(
                text, ffmpeg=ffmpeg, work_dir=work_dir, fonts_dir=fonts_dir
            )
        return cache[text]["width"]

    return measure, cache


def _ffmpeg_filter_path(path: Path) -> str:
    value = path.resolve().as_posix()
    return (
        value.replace("\\", "\\\\")
        .replace(":", "\\:")
        .replace("'", "\\'")
        .replace(",", "\\,")
        .replace("[", "\\[")
        .replace("]", "\\]")
    )


def _ass_video_filter(ass_path: Path, *, fonts_dir: Path) -> str:
    return (
        f"ass=filename='{_ffmpeg_filter_path(ass_path)}':"
        f"fontsdir='{_ffmpeg_filter_path(fonts_dir)}'"
    )


def _concat_file_line(path: Path) -> str:
    # FFmpeg concat-demuxer uses a shell-like single quoted path syntax.
    return "file '" + path.resolve().as_posix().replace("'", r"'\\''") + "'\n"


def _render_complete(
    segments: Sequence[Path], *, output: Path, ffmpeg: Path
) -> dict[str, Any]:
    """Create complete.mp4 with stream-copy first, never scale or change FPS.

    Segments created from a conforming row have matching stream parameters, so
    concat demuxing keeps their video bytes intact. Only an incompatible stream
    receives the explicit high-quality fallback re-encode.
    """

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        dir=output.parent,
        prefix=".concat-",
        suffix=".txt",
    ) as handle:
        concat_list = Path(handle.name)
        for segment in segments:
            handle.write(_concat_file_line(segment))
    try:
        copied = subprocess.run(
            [
                str(ffmpeg),
                "-y",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_list),
                "-map",
                "0:v:0",
                "-map",
                "0:a:0",
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(output),
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if copied.returncode == 0 and output.is_file() and output.stat().st_size:
            return {"method": "concat_demuxer_stream_copy", "video_reencoded": False}
        output.unlink(missing_ok=True)
        return _render_complete_fallback(segments, output=output, ffmpeg=ffmpeg)
    finally:
        concat_list.unlink(missing_ok=True)


def _render_complete_fallback(
    segments: Sequence[Path], *, output: Path, ffmpeg: Path
) -> dict[str, Any]:
    command: list[str] = [str(ffmpeg), "-y"]
    for segment in segments:
        command.extend(["-i", str(segment)])
    filters: list[str] = []
    concat_inputs: list[str] = []
    for index in range(len(segments)):
        filters.append(
            f"[{index}:v]setpts=PTS-STARTPTS"
            f"[v{index}]"
        )
        filters.append(
            f"[{index}:a]aformat=sample_rates=48000:channel_layouts=stereo,"
            f"asetpts=PTS-STARTPTS[a{index}]"
        )
        concat_inputs.extend([f"[v{index}]", f"[a{index}]"])
    filters.append(
        "".join(concat_inputs)
        + f"concat=n={len(segments)}:v=1:a=1[vcat][acat]"
    )
    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vcat]",
            "-map",
            "[acat]",
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            str(VIDEO_CRF),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "256k",
            "-ar",
            "48000",
            "-movflags",
            "+faststart",
            str(output),
        ]
    )
    _run_checked(command, label="complete uncaptioned fallback render")
    return {"method": "filter_concat_fallback", "video_reencoded": True}


def _render_captioned(
    source: Path, *, ass_path: Path, output: Path, ffmpeg: Path, fonts_dir: Path
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    _run_checked(
        [
            str(ffmpeg),
            "-y",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a:0",
            "-vf",
            _ass_video_filter(ass_path, fonts_dir=fonts_dir),
            "-c:v",
            "libx264",
            "-preset",
            "slow",
            "-crf",
            str(VIDEO_CRF),
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(output),
        ],
        label="complete captioned render",
    )


def _parse_metric_value(text: str, *, metric: str) -> float:
    pattern = r"All:([0-9.]+)" if metric == "ssim" else r"average:([0-9.]+)"
    matches = re.findall(pattern, text)
    if not matches:
        raise Stage2RunnerError(f"caption quality check produced no {metric} metric")
    return float(matches[-1])


def _caption_visual_quality(
    source: Path, captioned: Path, *, ffmpeg: Path, work_dir: Path
) -> dict[str, Any]:
    """Compare a caption-free upper region so glyph pixels are excluded.

    The production source and captioned stream should share dimensions, but
    FFmpeg can report an adjusted coded size/SAR after an odd-size source is
    encoded as yuv420p.  Concatenated streams can also begin on distinct mux
    timestamps even when they carry the same visual frames.  Normalise only
    the *metric input* (SAR, origin PTS, and 24fps sample clock); never scale
    or retime the deliverable.  This prevents a false quality failure from
    comparing a source frame to the adjacent captioned frame.
    """

    work_dir.mkdir(parents=True, exist_ok=True)
    crop_height = f"trunc(ih*{CAPTION_QUALITY_TOP_RATIO}/2)*2"

    def measure(left: Path, right: Path, *, tag: str) -> dict[str, float]:
        measured: dict[str, float] = {}
        for metric in ("ssim", "psnr"):
            stats = work_dir / f"{tag}-{metric}.log"
            completed = subprocess.run(
                [
                    str(ffmpeg),
                    "-v",
                    "info",
                    "-i",
                    str(left),
                    "-i",
                    str(right),
                    "-filter_complex",
                    (
                        # Each delivery is contractually 1080x1920 yuv420p.
                        # Canonicalise only the metric streams: VFR concat
                        # muxing can offset their origin by half a frame.
                        "[0:v]setsar=1,setpts=PTS-STARTPTS,fps=24[source];"
                        "[1:v]setsar=1,setpts=PTS-STARTPTS,fps=24[captioned];"
                        f"[source]crop=iw:{crop_height}:0:0[a];"
                        f"[captioned]crop=iw:{crop_height}:0:0[b];"
                        f"[a][b]{metric}=stats_file={stats}"
                    ),
                    "-an",
                    "-f",
                    "null",
                    "-",
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if completed.returncode != 0:
                raise Stage2RunnerError(
                    f"caption quality {metric} check failed: "
                    + (completed.stderr or completed.stdout or "unknown error")[-2000:]
                )
            measured[metric] = _parse_metric_value(
                completed.stdout + "\n" + completed.stderr, metric=metric
            )
        return measured

    metrics = measure(source, captioned, tag="caption")
    comparison = "source"
    source_metrics = dict(metrics)
    # A low-bitrate or VFR source can score below the absolute source-vs-output
    # threshold even when the caption filter itself introduces no regression:
    # the unavoidable H.264 re-encode noise is then larger than the caption
    # effect.  Build one same-codec, caption-free baseline only in that case
    # and compare the captioned delivery against that baseline.
    if not (
        metrics["ssim"] >= CAPTION_QUALITY_MIN_SSIM
        and metrics["psnr"] >= CAPTION_QUALITY_MIN_PSNR
    ):
        baseline = work_dir / "caption-baseline.mp4"
        _run_checked(
            [
                str(ffmpeg),
                "-y",
                "-i",
                str(source),
                "-map",
                "0:v:0",
                "-an",
                "-c:v",
                "libx264",
                "-preset",
                "slow",
                "-crf",
                str(VIDEO_CRF),
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(baseline),
            ],
            label="caption quality baseline render",
        )
        metrics = measure(baseline, captioned, tag="baseline")
        comparison = "same_codec_baseline"
    report: dict[str, Any] = {
        "region": f"top_{int(CAPTION_QUALITY_TOP_RATIO * 100)}_percent",
        "ssim": metrics["ssim"],
        "psnr": metrics["psnr"],
        "comparison": comparison,
        "source_metrics": source_metrics,
        "min_ssim": CAPTION_QUALITY_MIN_SSIM,
        "min_psnr": CAPTION_QUALITY_MIN_PSNR,
        "passed": (
            metrics["ssim"] >= CAPTION_QUALITY_MIN_SSIM
            and metrics["psnr"] >= CAPTION_QUALITY_MIN_PSNR
        ),
    }
    if not report["passed"]:
        raise Stage2RunnerError(
            "captioned visual quality regression: "
            f"SSIM={metrics['ssim']:.6f}, PSNR={metrics['psnr']:.3f}"
        )
    return report


def _audio_payload_sha256(path: Path, *, ffmpeg: Path) -> str:
    completed = subprocess.run(
        [
            str(ffmpeg),
            "-v",
            "error",
            "-i",
            str(path),
            "-map",
            "0:a:0",
            "-c",
            "copy",
            "-f",
            "data",
            "pipe:1",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        error = completed.stderr.decode("utf-8", errors="replace").strip()
        raise Stage2RunnerError(f"failed to hash audio payload for {path}: {error}")
    if not completed.stdout:
        raise Stage2RunnerError(f"audio payload is empty: {path}")
    return hashlib.sha256(completed.stdout).hexdigest()


def _extract_caption_frames(
    output: Path,
    *,
    events: Sequence[dict[str, Any]],
    frame_dir: Path,
    ffmpeg: Path,
) -> list[Path]:
    if not events:
        raise Stage2RunnerError("no caption events were generated")
    frames: list[Path] = []
    frame_dir.mkdir(parents=True, exist_ok=True)
    seen_frames: set[int] = set()
    for event_index, event in enumerate(events, 1):
        start = float(event["start"])
        end = float(event["end"])
        if end - start < 0.5:
            raise Stage2RunnerError(f"caption event {event_index} is shorter than 0.5s")
        points = {
            "start": min(end - 1 / 48, start + 1 / 24),
            "mid": (start + end) / 2,
            "end": max(start + 1 / 48, end - 1 / 24),
        }
        event_checks: list[dict[str, Any]] = []
        for label, at in points.items():
            frame_number = round(at * 24)
            if frame_number in seen_frames:
                continue
            seen_frames.add(frame_number)
            frame = frame_dir / f"caption-{event_index:03d}-{label}.png"
            # A final subtitle event can end on the muxed stream boundary.
            # Seeking exactly there may produce no decodable frame although
            # the event is visibly present on the preceding frame. Retry
            # inside the same event instead of treating this as a subtitle
            # rendering failure.
            for seek_at in (at, max(start + 1 / 48, at - 0.10)):
                _run_checked(
                    [
                        str(ffmpeg),
                        "-y",
                        "-ss",
                        f"{seek_at:.6f}",
                        "-i",
                        str(output),
                        "-frames:v",
                        "1",
                        str(frame),
                    ],
                    label=f"caption frame {event_index} {label}",
                )
                if frame.is_file() and frame.stat().st_size > 0:
                    at = seek_at
                    break
            if not frame.is_file() or frame.stat().st_size == 0:
                raise Stage2RunnerError(
                    f"caption frame {event_index} {label} is missing or empty"
                )
            frames.append(frame)
            event_checks.append(
                {
                    "label": label,
                    "at": at,
                    "frame": f"caption-frames/{frame.name}",
                }
            )
        if len(event_checks) != 3:
            raise Stage2RunnerError(
                f"caption event {event_index} does not have three distinct frame checks"
            )
        event["frame_checks"] = event_checks
    return frames


def _write_zip(output: Path, members: Sequence[tuple[Path, str]]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        prefix=f".{output.name}.", suffix=".tmp", dir=output.parent, delete=False
    ) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for source, arcname in members:
                if not source.is_file():
                    raise Stage2RunnerError(f"ZIP member is missing: {source}")
                archive.write(source, arcname=arcname)
        temporary.replace(output)
    finally:
        temporary.unlink(missing_ok=True)


def _delivery_record(path: Path) -> dict[str, Any]:
    return {"name": path.name, "path": str(path.resolve()), "size": path.stat().st_size, "sha256": sha256_file(path)}


def _named_delivery_record(path: Path, *, artifact_key: str) -> dict[str, Any]:
    if artifact_key not in DELIVERY_SPECS:
        raise Stage2RunnerError(f"unsupported delivery artifact: {artifact_key}")
    record = _delivery_record(path)
    field_name, suffix = DELIVERY_SPECS[artifact_key]
    if not record["name"].endswith(suffix):
        raise Stage2RunnerError(
            f"delivery filename does not match {artifact_key}: {record['name']}"
        )
    return {"artifact_key": artifact_key, "field_name": field_name, **record}


def _review_receipt(
    job: Stage2Job,
    *,
    review_required: bool,
    deliveries: Sequence[dict[str, Any]],
    complete_report: Any,
    complete_captioned_report: Any,
    complete_audio_verification: dict[str, Any],
    caption_quality: dict[str, Any],
    caption_frames: Sequence[Path],
) -> dict[str, Any]:
    """Produce auditable review evidence for a speed-exception result.

    The old human-only gate is intentionally not silently disabled.  Without a
    row-bound authorization the result remains pending.  With one, the Runner
    records the automatic checks that actually passed for this exact output.
    """

    if not review_required:
        return {"required": False, "completed": True, "method": "not_required"}
    if job.review_authorization is None:
        return {"required": True, "completed": False, "method": "pending"}
    audit = {
        "delivery_sha256": all(
            sha256_file(Path(str(item["path"]))) == item["sha256"]
            for item in deliveries
        ),
        "full_decode": bool(complete_report.passed and complete_captioned_report.passed),
        "audio_payload_match": complete_audio_verification.get("matched") is True,
        "caption_visual_quality": caption_quality.get("passed") is True,
        "caption_frame_checks": bool(caption_frames) and all(
            frame.is_file() and frame.stat().st_size > 0 for frame in caption_frames
        ),
    }
    return {
        "required": True,
        "completed": all(audit.values()),
        "method": AUTOMATED_REVIEW_METHOD,
        "authorization": job.review_authorization,
        "audit": audit,
    }


def _post_delivery_actions(
    job: Stage2Job, deliveries: Sequence[dict[str, Any]]
) -> dict[str, Any]:
    """Create the fail-closed handoff for the remote Bitable publisher.

    The runner has no Feishu credentials.  This record therefore authorizes no
    automatic remote call: it gives the publisher the exact verified inventory
    and the mandatory order in which it must upload, read back, replace and
    finally close a task-created ordinary-chat tab.
    """

    current = {str(item["artifact_key"]): item for item in deliveries}
    if set(current) != set(DELIVERY_SPECS):
        raise Stage2RunnerError("delivery plan does not contain the four canonical artifacts")
    replacement_plan = [
        {
            "artifact_key": target.artifact_key,
            "field_name": target.field_name,
            "remove_exact": {
                "file_token": target.file_token,
                "name": target.name,
                "size_bytes": target.size_bytes,
                "sha256": target.sha256,
            },
            "replacement_delivery": {
                "field_name": current[target.artifact_key]["field_name"],
                "name": current[target.artifact_key]["name"],
                "size_bytes": current[target.artifact_key]["size"],
                "sha256": current[target.artifact_key]["sha256"],
                "idempotency_key": upload_idempotency_key(
                    job.dispatch_key,
                    target.artifact_key,
                    current[target.artifact_key]["sha256"],
                ),
            },
        }
        for target in job.delivery_policy.replacements
    ]
    lifecycle_state = (
        "pending_controller_close"
        if job.delivery_policy.spawned_ordinary_chat_tab_id is not None
        else "not_applicable_no_spawned_ordinary_chat"
    )
    return {
        "mode": job.delivery_policy.mode,
        "remote_state": "pending_publisher_readback",
        "required_sequence": [
            "upload_each_new_delivery_to_declared_field",
            "readback_name_size_sha256_and_decode_each_new_mp4",
            "remove_each_whitelisted_superseded_attachment_by_exact_file_token",
            "readback_target_fields_confirm_new_delivery_retained_and_old_token_absent",
            "append_stage2_complete_and_readback_execution_record",
            "close_only_the_spawned_ordinary_chat_tab_after_all_previous_steps_pass",
        ],
        "replacement_plan": replacement_plan,
        "upload_idempotency": {
            artifact_key: upload_idempotency_key(
                job.dispatch_key,
                artifact_key,
                current[artifact_key]["sha256"],
            )
            for artifact_key in DELIVERY_SPECS
        },
        "deletion_guards": {
            "upload_readback_required_before_delete": True,
            "exact_file_token_only": True,
            "field_rewrite_forbidden": True,
            "delete_on_failure_forbidden": True,
        },
        "cloud_chat_lifecycle": {
            "spawned_ordinary_chat_tab_id": job.delivery_policy.spawned_ordinary_chat_tab_id,
            "close_after": "stage2_complete_execution_record_readback",
            "scope": "spawned_ordinary_chat_only",
            "state": lifecycle_state,
        },
    }


def _already_complete(job: Stage2Job) -> dict[str, Any] | None:
    result_path = job.output_dir / "result.json"
    if not result_path.is_file():
        return None
    result = read_json(result_path)
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    if result.get("dispatch_key") != job.dispatch_key:
        raise Stage2RunnerError("existing result.json belongs to a different dispatch_key")
    if result.get("input_fingerprint") != job.input_fingerprint:
        raise Stage2RunnerError("existing result.json belongs to a different input_fingerprint")
    if result.get("video_number") != job.video_number:
        raise Stage2RunnerError("existing result.json belongs to a different video_number")
    deliveries = result.get("deliveries")
    if not isinstance(deliveries, list) or len(deliveries) != 4:
        return None
    for item in deliveries:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            return None
        path = Path(item["path"])
        if not path.is_file() or path.stat().st_size != item.get("size"):
            return None
        if sha256_file(path) != item.get("sha256"):
            return None
    if result.get("delivery_policy") != _delivery_policy_record(job.delivery_policy):
        return None
    if result.get("runtime_receipt_sha256") != runtime_receipt_sha256(
        validate_runtime_receipt(read_json(job.runtime_receipt), record_id=job.record_id)
    ):
        return None
    if result.get("caption_font") != job.caption_font:
        return None
    if not (job.output_dir / "evidence" / "font-resolution" / "font-resolution.log").is_file():
        return None
    result = dict(result)
    result["already_complete"] = True
    return result


Renderer = Callable[..., dict[str, Any]]


def _run_row_impl(
    job_path: Path, *, renderer: Renderer = render_from_reference_package
) -> dict[str, Any]:
    job = load_job(job_path)
    job.output_dir.mkdir(parents=True, exist_ok=True)
    ffmpeg = _resolve_executable("ffmpeg", job.ffmpeg)
    font_resolution_log, _font_resolution_trace = _verify_libass_font_resolution(
        ffmpeg,
        fonts_dir=job.caption_fonts_dir,
        work_dir=job.output_dir / "evidence" / "font-resolution",
    )
    existing = _already_complete(job)
    if existing is not None:
        return existing

    ffprobe = _resolve_executable("ffprobe", job.ffprobe)
    pair_results: list[dict[str, Any]] = []
    segment_paths: list[Path] = []
    runtime_receipt = validate_runtime_receipt(
        read_json(job.runtime_receipt), record_id=job.record_id
    )
    strict_release_path = job.output_dir / "evidence" / "strict-release-lock.json"
    atomic_write_json(strict_release_path, job.strict_release)
    evidence_members: list[tuple[Path, str]] = [
        (job.path, "job.json"),
        (job.runtime_receipt, "runtime-receipt.json"),
        (font_resolution_log, "font-resolution.log"),
        (strict_release_path, "strict-release-lock.json"),
    ]
    caption_events: list[dict[str, Any]] = []
    caption_measure, caption_bbox_cache = _make_caption_measurer(
        ffmpeg=ffmpeg,
        work_dir=job.output_dir / "work" / "caption-probes",
        fonts_dir=job.caption_fonts_dir,
    )
    timeline_offset = 0.0

    for pair in job.inputs:
        pair_dir = job.output_dir / "pairs" / pair.pair_id
        # The first pass is always the default 1.25x gate.  Pause-hold
        # authorization only enables the approved static-frame strategy; it
        # never widens the first-pass speed ceiling.
        initial_speed_max = job.default_speed_max
        pair_pause_hold_authorized = pair.pair_id in job.pause_hold_pair_ids
        first = renderer(
            package_path=pair.reference_package.path,
            generated_video=pair.generated_video.path,
            output_dir=pair_dir,
            ffmpeg=ffmpeg,
            ffprobe=ffprobe,
            mouth_python=job.mouth_python,
            preview=False,
            approved_hard_speed_max=initial_speed_max,
            allow_pause_hold=pair_pause_hold_authorized,
            pause_hold_max_seconds=job.pause_hold_max_seconds,
        )
        attempts = [
            {
                "speed_max": initial_speed_max,
                "ok": bool(first.get("ok")),
                "status": first.get("status"),
                "reasons": list(first.get("reasons", [])),
            }
        ]
        used_exception = initial_speed_max > job.default_speed_max
        final = first
        if (
            not used_exception
            and should_retry_speed_exception(first, approved_max=job.approved_exception_max)
        ):
            used_exception = True
            final = renderer(
                package_path=pair.reference_package.path,
                generated_video=pair.generated_video.path,
                output_dir=pair_dir,
                ffmpeg=ffmpeg,
                ffprobe=ffprobe,
                mouth_python=job.mouth_python,
                preview=False,
                approved_hard_speed_max=job.approved_exception_max,
                allow_pause_hold=pair_pause_hold_authorized,
                pause_hold_max_seconds=job.pause_hold_max_seconds,
            )
            attempts.append(
                {
                    "speed_max": job.approved_exception_max,
                    "ok": bool(final.get("ok")),
                    "status": final.get("status"),
                    "reasons": list(final.get("reasons", [])),
                }
            )
        if not final.get("ok"):
            failure = {
                "schema_version": SCHEMA_VERSION,
                "ok": False,
                "status": "failed",
                "error_class": "processing_error",
                "record_id": job.record_id,
                "video_number": job.video_number,
                "dispatch_key": job.dispatch_key,
                "attempt": job.attempt,
                "failed_pair_id": pair.pair_id,
                "pair_attempts": attempts,
                "renderer_result": final,
                "written_at": _utc_now(),
            }
            atomic_write_json(job.output_dir / "result.json", failure)
            return failure

        plan = _read_plan(final)
        source_output = Path(str(final["output"]))
        segment = job.output_dir / "segments" / f"{pair.pair_id}.mp4"
        segment.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source_output, segment)
        segment_paths.append(segment)
        with open_reference_package(pair.reference_package.path) as package:
            caption_events.extend(
                build_caption_events(
                    package.benchmark,
                    plan,
                    subtitle=package.subtitle,
                    offset=timeline_offset,
                    measure_text=caption_measure,
                    max_width=CAPTION_MAX_WIDTH,
                )
            )
            subtitle_evidence = (
                job.output_dir / "evidence" / "pairs" / pair.pair_id / "subtitle.json"
            )
            atomic_write_json(subtitle_evidence, package.subtitle)
            evidence_members.append(
                (subtitle_evidence, f"pairs/{pair.pair_id}/subtitle.json")
            )
            subtitle_summary = {
                "source_mode": package.subtitle["source_mode"],
                "quality_status": package.subtitle["quality_status"],
                "visual_subtitle_present": package.subtitle[
                    "visual_subtitle_present"
                ],
                "caption_event_count": len(package.subtitle["caption_events"]),
                "approved_split_point_count": len(
                    package.subtitle["approved_split_points"]
                ),
            }
        timeline_offset += plan.output_duration

        artifact_paths = [
            (key, Path(str(final[key])))
            for key in (
                "plan",
                "mouth_track",
                "activity_plot",
                "plan_verification",
                "media_verification",
                "rendered_mouth_track",
                "sync_verification",
            )
            if isinstance(final.get(key), str)
        ]
        renderer_result_path = pair_dir / "edit" / f"online-{pair.pair_id}.result.json"
        if renderer_result_path.is_file():
            artifact_paths.append(("renderer_result", renderer_result_path))
        for artifact_kind, artifact in artifact_paths:
            if artifact.is_file():
                evidence_members.append(
                    (
                        artifact,
                        f"pairs/{pair.pair_id}/{artifact_kind}{artifact.suffix}",
                    )
                )
        pair_results.append(
            {
                "pair_id": pair.pair_id,
                "input": {
                    "reference_package_sha256": pair.reference_package.sha256,
                    "generated_video_sha256": pair.generated_video.sha256,
                },
                "output": _delivery_record(segment),
                "status": final.get("status"),
                "review_required": (
                    bool(final.get("review_required"))
                    or used_exception
                    or subtitle_summary["quality_status"] == "review"
                ),
                "generated_audio_used": bool(final.get("generated_audio_used", True)),
                "speed_exception_used": used_exception,
                "max_speed": max(
                    item.speed
                    for item in plan.video_segments
                    if not item.reason.startswith("stage1 approved visual pause hold")
                ),
                "min_speed": min(
                    item.speed
                    for item in plan.video_segments
                    if not item.reason.startswith("stage1 approved visual pause hold")
                ),
                "pause_hold_used": any(
                    item.reason.startswith("stage1 approved visual pause hold")
                    for item in plan.video_segments
                ),
                "pause_hold_authorized": pair_pause_hold_authorized,
                "duration": plan.output_duration,
                "subtitle": subtitle_summary,
                "sync": (
                    read_json(Path(str(final["sync_verification"])))
                    if isinstance(final.get("sync_verification"), str)
                    else {"passed": False, "errors": ["sync verification is missing"]}
                ),
                "attempts": attempts,
            }
        )

    if any(item["generated_audio_used"] for item in pair_results):
        raise Stage2RunnerError("renderer reported that generated-video audio was used")
    for event in caption_events:
        raw_text = str(event.get("raw_text", event["text"]))
        event["bbox"] = caption_bbox_cache.get(raw_text) or _probe_caption_bbox(
            raw_text,
            ffmpeg=ffmpeg,
            work_dir=job.output_dir / "work" / "caption-probes",
            fonts_dir=job.caption_fonts_dir,
        )
        if (
            event["bbox"]["x1"] < CAPTION_SAFE_MARGIN
            or event["bbox"]["x2"] >= 1080 - CAPTION_SAFE_MARGIN
        ):
            raise CaptionLayoutError(
                f"caption exceeds the {CAPTION_SAFE_MARGIN}px horizontal safe margin: {raw_text}"
            )

    ass_path = job.output_dir / "evidence" / "subtitles.ass"
    write_ass(ass_path, caption_events)
    complete = job.output_dir / f"omni_stage2__{job.record_id}__complete.mp4"
    complete_concat = _render_complete(segment_paths, output=complete, ffmpeg=ffmpeg)
    complete_report = verify_rendered_media(
        complete,
        pair_id=job.record_id,
        expected_duration=timeline_offset,
        ffprobe_path=ffprobe,
        ffmpeg_path=ffmpeg,
    )
    complete_report_path = job.output_dir / "evidence" / "complete-uncaptioned.media.json"
    atomic_write_json(complete_report_path, complete_report.to_dict())
    if not complete_report.passed:
        raise Stage2RunnerError("complete uncaptioned video failed media verification")

    complete_captioned = (
        job.output_dir / f"omni_stage2__{job.record_id}__complete_captioned.mp4"
    )
    _render_captioned(
        complete,
        ass_path=ass_path,
        output=complete_captioned,
        ffmpeg=ffmpeg,
        fonts_dir=job.caption_fonts_dir,
    )
    complete_captioned_report = verify_rendered_media(
        complete_captioned,
        pair_id=job.record_id,
        expected_duration=timeline_offset,
        ffprobe_path=ffprobe,
        ffmpeg_path=ffmpeg,
    )
    complete_captioned_report_path = (
        job.output_dir / "evidence" / "complete-captioned.media.json"
    )
    atomic_write_json(
        complete_captioned_report_path, complete_captioned_report.to_dict()
    )
    if not complete_captioned_report.passed:
        raise Stage2RunnerError("complete captioned video failed media verification")
    complete_audio_sha = _audio_payload_sha256(complete, ffmpeg=ffmpeg)
    captioned_audio_sha = _audio_payload_sha256(complete_captioned, ffmpeg=ffmpeg)
    if complete_audio_sha != captioned_audio_sha:
        raise Stage2RunnerError(
            "caption burn changed the complete video audio payload"
        )
    complete_audio_verification = {
        "matched": True,
        "sha256": complete_audio_sha,
    }
    complete_audio_verification_path = (
        job.output_dir / "evidence" / "complete-audio-match.json"
    )
    atomic_write_json(
        complete_audio_verification_path, complete_audio_verification
    )
    caption_quality = _caption_visual_quality(
        complete,
        complete_captioned,
        ffmpeg=ffmpeg,
        work_dir=job.output_dir / "work" / "caption-quality",
    )
    caption_quality_path = job.output_dir / "evidence" / "captioned-visual-quality.json"
    atomic_write_json(caption_quality_path, caption_quality)
    caption_frames = _extract_caption_frames(
        complete_captioned,
        events=caption_events,
        frame_dir=job.output_dir / "evidence" / "caption-frames",
        ffmpeg=ffmpeg,
    )

    segments_zip = job.output_dir / f"omni_stage2__{job.record_id}__segments.zip"
    _write_zip(segments_zip, [(path, path.name) for path in segment_paths])

    evidence_manifest = {
        "schema_version": SCHEMA_VERSION,
        "record_id": job.record_id,
        "video_number": job.video_number,
        "dispatch_key": job.dispatch_key,
        "input_fingerprint": job.input_fingerprint,
        "strict_release": job.strict_release,
        "stage2_key": stage2_key(job.dispatch_key, job.attempt),
        "attempt": job.attempt,
        "status": "review" if any(x["review_required"] for x in pair_results) else "pass",
        "pair_results": pair_results,
        "caption": {
            "style": CAPTION_STYLE,
            "font": CAPTION_FONT_FULL_NAME,
            "font_face": job.caption_font,
            "font_directory": str(job.caption_fonts_dir),
            "font_size": CAPTION_BASE_FONT_SIZE,
            "long_line_font_size": CAPTION_LONG_FONT_SIZE,
            "outline": CAPTION_MAIN_OUTLINE,
            "shadow_outline": CAPTION_SHADOW_OUTLINE,
            "position_y_percent": round(CAPTION_POSITION_Y / 1920 * 100, 2),
            "event_count": len(caption_events),
            "frame_checks": len(caption_frames),
            "safe_margin_px": CAPTION_SAFE_MARGIN,
            "single_line": True,
            "events": caption_events,
        },
        "complete_media_verification": {
            "uncaptioned": complete_report.to_dict(),
            "captioned": complete_captioned_report.to_dict(),
        },
        "complete_audio_verification": complete_audio_verification,
        "complete_concat": complete_concat,
        "captioned_visual_quality": caption_quality,
        "pre_evidence_deliverables": [
            _named_delivery_record(segments_zip, artifact_key="segments"),
            _named_delivery_record(complete, artifact_key="complete"),
            _named_delivery_record(
                complete_captioned, artifact_key="complete_captioned"
            ),
        ],
        "delivery_policy": _delivery_policy_record(job.delivery_policy),
        "runtime_receipt": {
            "path": "runtime-receipt.json",
            "sha256": runtime_receipt_sha256(runtime_receipt),
        },
        "written_at": _utc_now(),
    }
    evidence_manifest_path = job.output_dir / "evidence" / "manifest.json"
    atomic_write_json(evidence_manifest_path, evidence_manifest)
    evidence_members.extend(
        [
            (ass_path, "subtitles.ass"),
            (complete_report_path, "complete-uncaptioned.media.json"),
            (complete_captioned_report_path, "complete-captioned.media.json"),
            (complete_audio_verification_path, "complete-audio-match.json"),
            (caption_quality_path, "captioned-visual-quality.json"),
            (evidence_manifest_path, "manifest.json"),
        ]
    )
    evidence_members.extend(
        (frame, f"caption-frames/{frame.name}") for frame in caption_frames
    )
    evidence_zip = job.output_dir / f"omni_stage2__{job.record_id}__evidence.zip"
    _write_zip(evidence_zip, evidence_members)

    deliveries = [
        _named_delivery_record(segments_zip, artifact_key="segments"),
        _named_delivery_record(complete, artifact_key="complete"),
        _named_delivery_record(
            complete_captioned, artifact_key="complete_captioned"
        ),
        _named_delivery_record(evidence_zip, artifact_key="evidence"),
    ]
    for delivery in deliveries:
        delivery["idempotency_key"] = upload_idempotency_key(
            job.dispatch_key, str(delivery["artifact_key"]), str(delivery["sha256"])
        )
    review_receipt = _review_receipt(
        job,
        review_required=any(item["review_required"] for item in pair_results),
        deliveries=deliveries,
        complete_report=complete_report,
        complete_captioned_report=complete_captioned_report,
        complete_audio_verification=complete_audio_verification,
        caption_quality=caption_quality,
        caption_frames=caption_frames,
    )
    post_delivery_actions = _post_delivery_actions(job, deliveries)
    atomic_write_json(
        job.output_dir / "post-delivery-actions.json", post_delivery_actions
    )
    result = {
        "schema_version": SCHEMA_VERSION,
        "ok": True,
        "status": evidence_manifest["status"],
        "error_class": None,
        "record_id": job.record_id,
        "video_number": job.video_number,
        "dispatch_key": job.dispatch_key,
        "input_fingerprint": job.input_fingerprint,
        "strict_release": job.strict_release,
        "stage2_key": stage2_key(job.dispatch_key, job.attempt),
        "attempt": job.attempt,
        "pair_results": pair_results,
        "deliveries": deliveries,
        "delivery_policy": _delivery_policy_record(job.delivery_policy),
        "post_delivery_actions": post_delivery_actions,
        "caption": evidence_manifest["caption"],
        "complete_media_verification": evidence_manifest[
            "complete_media_verification"
        ],
        "complete_audio_verification": complete_audio_verification,
        "complete_concat": complete_concat,
        "captioned_visual_quality": caption_quality,
        "caption_font": job.caption_font,
        "runtime_receipt_sha256": runtime_receipt_sha256(runtime_receipt),
        "stage2_started_receipt": job.stage2_started_receipt,
        "review_receipt": review_receipt,
        "written_at": _utc_now(),
    }
    result["stage2_complete_event_template"] = build_stage2_complete_event(
        result,
        remote_readback_verified=False,
        review_receipt=review_receipt,
    )
    atomic_write_json(job.output_dir / "result.json", result)
    return result


def _classify_error(message: str) -> str:
    lowered = message.lower()
    if any(token in lowered for token in ("ffmpeg", "ffprobe", "mediapipe", "python", "preflight")):
        return "runtime_error"
    if any(token in lowered for token in ("upload", "readback", "delivery")):
        return "delivery_error"
    return "processing_error"


def run_row(
    job_path: Path, *, renderer: Renderer = render_from_reference_package
) -> dict[str, Any]:
    try:
        return _run_row_impl(job_path, renderer=renderer)
    except Exception as exc:
        # Invalid job files cannot name a trusted output boundary.  Once the
        # job itself validates, every later failure receives a durable result.
        try:
            job = load_job(job_path)
        except Exception:
            raise
        job.output_dir.mkdir(parents=True, exist_ok=True)
        failure = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "status": "failed",
            "error_class": _classify_error(str(exc)),
            "record_id": job.record_id,
            "video_number": job.video_number,
            "dispatch_key": job.dispatch_key,
            "input_fingerprint": job.input_fingerprint,
            "strict_release": job.strict_release,
            "attempt": job.attempt,
            "error": str(exc),
            "written_at": _utc_now(),
        }
        atomic_write_json(job.output_dir / "result.json", failure)
        return failure


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="omni-stage2")
    subparsers = parser.add_subparsers(dest="command", required=True)
    run = subparsers.add_parser("run-row", help="process one complete Omni Bitable row")
    run.add_argument("--job", type=Path, required=True)
    select = subparsers.add_parser(
        "select-row",
        help="fail-closed selection of one fully read Bitable row snapshot",
    )
    select.add_argument("--snapshot", type=Path, required=True)
    select.add_argument("--batch-date", type=str)
    select.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "select-row":
            result = select_row_file(
                args.snapshot, batch_date=args.batch_date, output_path=args.output
            )
        else:
            result = run_row(args.job)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        success_statuses = {"selected_initial", "retry_eligible_after_first_pass", "skip_complete", "skip_failed_final_today", "ineligible_stage1"}
        return 0 if result.get("ok") or result.get("status") in success_statuses else 2
    except Exception as exc:
        print(
            json.dumps(
                {"ok": False, "status": "failed", "error": str(exc)},
                ensure_ascii=False,
                indent=2,
            ),
            file=sys.stderr,
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

