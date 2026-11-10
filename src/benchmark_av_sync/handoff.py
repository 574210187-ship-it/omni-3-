from __future__ import annotations

import os
import shutil
import tempfile
import wave
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterator

from .contracts import BenchmarkScript
from .subtitle_contract import (
    STAGE1_V32_EVIDENCE_VERSION,
    STAGE1_V32_SCHEMA_VERSION,
    validate_subtitle_contract,
)
from .workspace import atomic_write_json, read_json, sha256_file


PACKAGE_SCHEMA_VERSION = 2
PACKAGE_TYPE = "benchmark-reference"
REQUIRED_FILES = ("manifest.json", "benchmark.json", "subtitle.json", "reference.wav")


class HandoffError(RuntimeError):
    """Raised when a reference handoff package is invalid."""


@dataclass(slots=True)
class OpenReferencePackage:
    root: Path
    manifest: dict[str, Any]
    benchmark: BenchmarkScript
    subtitle: dict[str, Any]
    reference_audio: Path


def wav_metadata(path: Path) -> dict[str, Any]:
    try:
        with wave.open(str(path), "rb") as handle:
            frame_count = handle.getnframes()
            sample_rate = handle.getframerate()
            channels = handle.getnchannels()
            sample_width = handle.getsampwidth()
    except (OSError, wave.Error) as exc:
        raise HandoffError(f"invalid reference WAV: {path}: {exc}") from exc
    if frame_count <= 0 or sample_rate <= 0 or channels <= 0:
        raise HandoffError("reference WAV must contain positive audio data")
    return {
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width_bytes": sample_width,
        "frame_count": frame_count,
        "duration": frame_count / sample_rate,
    }


def _file_record(path: Path) -> dict[str, Any]:
    return {"size": path.stat().st_size, "sha256": sha256_file(path)}


def _manifest(
    pair_id: str,
    benchmark: BenchmarkScript,
    benchmark_path: Path,
    subtitle: dict[str, Any],
    subtitle_path: Path,
    reference_audio: Path,
) -> dict[str, Any]:
    audio = wav_metadata(reference_audio)
    return {
        "version": PACKAGE_SCHEMA_VERSION,
        "package_type": PACKAGE_TYPE,
        "pair_id": pair_id,
        "files": {
            "benchmark.json": _file_record(benchmark_path),
            "subtitle.json": _file_record(subtitle_path),
            "reference.wav": _file_record(reference_audio),
        },
        "reference_audio": audio,
        "benchmark": {
            "status": benchmark.status,
            "reference_duration": benchmark.reference_duration,
            "speech_start": benchmark.speech_start,
            "speech_end": benchmark.speech_end,
            "token_count": len(benchmark.tokens),
            "safe_audio_cut_count": sum(item.cut_allowed for item in benchmark.boundaries),
        },
        "subtitle": {
            "status": subtitle["quality_status"],
            "source_mode": subtitle["source_mode"],
            "visual_subtitle_present": subtitle["visual_subtitle_present"],
            "caption_event_count": len(subtitle["caption_events"]),
            "approved_split_point_count": len(subtitle["approved_split_points"]),
        },
        "policy": {
            "auto_pause_ms": 120,
            "review_pause_ms": 80,
            "default_auto_speed_min": 0.90,
            "default_auto_speed_max": 1.20,
            "default_hard_speed_min": 0.85,
            "default_hard_speed_max": 1.25,
            "generated_audio_policy": "ignore",
            "middle_video_policy": "continuous-no-delete",
        },
    }


def write_reference_package(
    output: Path,
    *,
    pair_id: str,
    benchmark: BenchmarkScript,
    subtitle: dict[str, Any],
    reference_audio: Path,
) -> dict[str, Any]:
    if not pair_id.isdigit():
        raise HandoffError("pair ID must contain digits only")
    if benchmark.pair_id != pair_id:
        raise HandoffError("benchmark pair ID does not match requested package ID")
    if benchmark.status == "fail":
        raise HandoffError("failed benchmark evidence cannot be packaged")
    try:
        validate_subtitle_contract(subtitle, benchmark)
    except ValueError as exc:
        raise HandoffError(f"invalid subtitle contract: {exc}") from exc
    if subtitle["quality_status"] == "fail":
        raise HandoffError("failed subtitle evidence cannot be packaged")
    reference_audio = reference_audio.resolve()
    if not reference_audio.is_file():
        raise HandoffError(f"reference WAV does not exist: {reference_audio}")
    if subtitle["reference_audio_sha256"] != sha256_file(reference_audio):
        raise HandoffError("subtitle reference audio SHA-256 does not match reference WAV")
    output = output.resolve()
    if output.exists():
        raise HandoffError(f"output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(prefix=f"reference-{pair_id}-", dir=output.parent) as tmp:
        staging = Path(tmp)
        benchmark_path = staging / "benchmark.json"
        subtitle_path = staging / "subtitle.json"
        staged_audio = staging / "reference.wav"
        atomic_write_json(benchmark_path, benchmark.to_dict())
        atomic_write_json(subtitle_path, subtitle)
        shutil.copy2(reference_audio, staged_audio)
        manifest = _manifest(
            pair_id,
            benchmark,
            benchmark_path,
            subtitle,
            subtitle_path,
            staged_audio,
        )
        atomic_write_json(staging / "manifest.json", manifest)

        if output.suffix.lower() == ".zip":
            temporary_zip = output.with_name(f".{output.name}.tmp")
            try:
                with zipfile.ZipFile(
                    temporary_zip, "w", compression=zipfile.ZIP_DEFLATED
                ) as archive:
                    for name in REQUIRED_FILES:
                        info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
                        info.compress_type = zipfile.ZIP_DEFLATED
                        info.external_attr = 0o644 << 16
                        archive.writestr(info, (staging / name).read_bytes())
                os.replace(temporary_zip, output)
            finally:
                temporary_zip.unlink(missing_ok=True)
        else:
            shutil.copytree(staging, output)
    return manifest


def _safe_extract(archive_path: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive_path, "r") as archive:
        names = [item.filename for item in archive.infolist() if not item.is_dir()]
        for name in names:
            parsed = PurePosixPath(name)
            if parsed.is_absolute() or ".." in parsed.parts or len(parsed.parts) != 1:
                raise HandoffError(f"unsafe or nested ZIP member: {name!r}")
        missing = sorted(set(REQUIRED_FILES) - set(names))
        if missing:
            raise HandoffError("reference package is missing: " + ", ".join(missing))
        archive.extractall(destination)


def _validate_root(root: Path) -> OpenReferencePackage:
    for name in REQUIRED_FILES:
        if not (root / name).is_file():
            raise HandoffError(f"reference package is missing {name}")
    manifest = read_json(root / "manifest.json")
    if not isinstance(manifest, dict):
        raise HandoffError("manifest.json must contain an object")
    if manifest.get("version") != PACKAGE_SCHEMA_VERSION:
        raise HandoffError(f"unsupported reference package version: {manifest.get('version')!r}")
    if manifest.get("package_type") != PACKAGE_TYPE:
        raise HandoffError("manifest package_type is invalid")
    pair_id = str(manifest.get("pair_id", ""))
    if not pair_id.isdigit():
        raise HandoffError("manifest pair_id must contain digits only")

    records = manifest.get("files")
    if not isinstance(records, dict):
        raise HandoffError("manifest files section is invalid")
    for name in ("benchmark.json", "subtitle.json", "reference.wav"):
        record = records.get(name)
        if not isinstance(record, dict):
            raise HandoffError(f"manifest has no file record for {name}")
        path = root / name
        if record.get("size") != path.stat().st_size:
            raise HandoffError(f"file size mismatch for {name}")
        if record.get("sha256") != sha256_file(path):
            raise HandoffError(f"SHA-256 mismatch for {name}")

    benchmark = BenchmarkScript.from_dict(read_json(root / "benchmark.json"))
    if benchmark.pair_id != pair_id:
        raise HandoffError("benchmark pair ID does not match manifest")
    if benchmark.status == "fail":
        raise HandoffError("failed benchmark evidence cannot be consumed")
    try:
        subtitle = validate_subtitle_contract(
            read_json(root / "subtitle.json"), benchmark
        )
    except ValueError as exc:
        raise HandoffError(f"invalid subtitle contract: {exc}") from exc
    if subtitle["quality_status"] == "fail":
        raise HandoffError("failed subtitle evidence cannot be consumed")
    if subtitle.get("schema_version") == STAGE1_V32_SCHEMA_VERSION:
        if manifest.get("evidence_version") != STAGE1_V32_EVIDENCE_VERSION:
            raise HandoffError("v3 subtitle evidence_version does not match manifest")
    audio = wav_metadata(root / "reference.wav")
    if subtitle["reference_audio_sha256"] != sha256_file(root / "reference.wav"):
        raise HandoffError("subtitle reference audio SHA-256 does not match reference WAV")
    manifest_audio = manifest.get("reference_audio")
    if not isinstance(manifest_audio, dict):
        raise HandoffError("manifest reference_audio section is invalid")
    for key in ("sample_rate", "channels", "sample_width_bytes", "frame_count"):
        if manifest_audio.get(key) != audio[key]:
            raise HandoffError(f"reference WAV metadata mismatch: {key}")
    if abs(float(manifest_audio.get("duration", -1)) - audio["duration"]) > 1e-6:
        raise HandoffError("reference WAV duration does not match manifest")
    if abs(benchmark.reference_duration - audio["duration"]) > 0.06:
        raise HandoffError("reference WAV duration does not match benchmark duration")
    return OpenReferencePackage(
        root=root,
        manifest=manifest,
        benchmark=benchmark,
        subtitle=subtitle,
        reference_audio=root / "reference.wav",
    )


@contextmanager
def open_reference_package(path: Path) -> Iterator[OpenReferencePackage]:
    path = path.resolve()
    if path.is_dir():
        yield _validate_root(path)
        return
    if not path.is_file():
        raise HandoffError(f"reference package does not exist: {path}")
    if path.suffix.lower() != ".zip":
        raise HandoffError("reference package must be a directory or .zip file")
    with tempfile.TemporaryDirectory(prefix="reference-package-") as tmp:
        root = Path(tmp)
        _safe_extract(path, root)
        yield _validate_root(root)


def validate_reference_package(path: Path) -> dict[str, Any]:
    with open_reference_package(path) as package:
        return {
            "ok": True,
            "pair_id": package.benchmark.pair_id,
            "benchmark_status": package.benchmark.status,
            "subtitle_status": package.subtitle["quality_status"],
            "subtitle_source_mode": package.subtitle["source_mode"],
            "reference_duration": package.benchmark.reference_duration,
            "speech_start": package.benchmark.speech_start,
            "speech_end": package.benchmark.speech_end,
            "manifest": package.manifest,
        }

