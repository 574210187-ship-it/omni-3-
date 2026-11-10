from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class BatchWorkspace:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.edit = self.root / "edit"

    def ensure(self) -> None:
        for relative in (
            "canonical_transcripts",
            "alignments",
            "benchmark_json",
            "mouth_tracks",
            "activity_plots",
            "plans",
            "previews",
            "verify",
            "finals",
            "cache",
        ):
            (self.edit / relative).mkdir(parents=True, exist_ok=True)

    def cache_key(self, *parts: str) -> str:
        digest = hashlib.sha256()
        for part in parts:
            digest.update(part.encode("utf-8"))
            digest.update(b"\0")
        return digest.hexdigest()
