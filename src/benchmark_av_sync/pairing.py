from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Callable

from .workspace import sha256_file


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v"}
DEFAULT_ID_PATTERN = r"(?<!\d)(\d+)(?!\d)"


def extract_pair_id(path: Path, id_regex: str | None = None) -> str:
    pattern = re.compile(id_regex or DEFAULT_ID_PATTERN)
    matches = list(pattern.finditer(path.stem))
    if len(matches) != 1:
        raise ValueError(
            f"expected exactly one numeric id in {path.name!r}, found {len(matches)}"
        )
    match = matches[0]
    if match.lastindex:
        value = match.group(1)
    else:
        value = match.group(0)
    if not value or not value.isdigit():
        raise ValueError(f"pair id must be digits in {path.name!r}, got {value!r}")
    return value


def _scan_side(directory: Path, side: str, id_regex: str | None) -> tuple[dict[str, Path], list[dict[str, Any]]]:
    buckets: dict[str, list[Path]] = {}
    errors: list[dict[str, Any]] = []
    if not directory.is_dir():
        return {}, [{"code": "directory_missing", "side": side, "path": str(directory.resolve())}]
    files = sorted(
        (p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in VIDEO_EXTENSIONS),
        key=lambda p: p.name.casefold(),
    )
    for path in files:
        try:
            pair_id = extract_pair_id(path, id_regex=id_regex)
        except ValueError as exc:
            errors.append({"code": "invalid_id", "side": side, "path": str(path.resolve()), "message": str(exc)})
            continue
        buckets.setdefault(pair_id, []).append(path)
    found: dict[str, Path] = {}
    for pair_id, paths in buckets.items():
        if len(paths) > 1:
            errors.append(
                {
                    "code": "duplicate_id",
                    "side": side,
                    "pair_id": pair_id,
                    "paths": [str(path.resolve()) for path in paths],
                }
            )
            continue
        found[pair_id] = paths[0]
    return found, errors


def build_pair_manifest(
    reference_dir: Path,
    generated_dir: Path,
    *,
    id_regex: str | None = None,
    probe: Callable[[Path], dict[str, Any]] | None = None,
    hash_files: bool = True,
) -> dict[str, Any]:
    reference, errors = _scan_side(reference_dir, "reference", id_regex)
    generated, generated_errors = _scan_side(generated_dir, "generated", id_regex)
    errors.extend(generated_errors)

    reference_ids = set(reference)
    generated_ids = set(generated)
    for pair_id in sorted(reference_ids - generated_ids):
        errors.append({"code": "missing_generated", "pair_id": pair_id, "path": str(reference[pair_id].resolve())})
    for pair_id in sorted(generated_ids - reference_ids):
        errors.append({"code": "missing_reference", "pair_id": pair_id, "path": str(generated[pair_id].resolve())})

    pairs: list[dict[str, Any]] = []
    for pair_id in sorted(reference_ids & generated_ids):
        ref = reference[pair_id].resolve()
        gen = generated[pair_id].resolve()
        record: dict[str, Any] = {
            "pair_id": pair_id,
            "reference": {"path": str(ref), "size": ref.stat().st_size},
            "generated": {"path": str(gen), "size": gen.stat().st_size},
        }
        if hash_files:
            record["reference"]["sha256"] = sha256_file(ref)
            record["generated"]["sha256"] = sha256_file(gen)
        if probe is not None:
            record["reference"]["media"] = probe(ref)
            record["generated"]["media"] = probe(gen)
        pairs.append(record)

    return {
        "version": 1,
        "id_regex": id_regex or DEFAULT_ID_PATTERN,
        "reference_dir": str(reference_dir.resolve()),
        "generated_dir": str(generated_dir.resolve()),
        "pairs": pairs,
        "errors": errors,
    }
