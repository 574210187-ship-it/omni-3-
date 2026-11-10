from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

from .workspace import atomic_write_json, read_json


class WorkerError(RuntimeError):
    pass


def run_worker(
    *,
    python_executable: Path,
    module: str,
    request: dict[str, Any],
    request_path: Path,
    response_path: Path,
    module_root: Path | None = None,
) -> dict[str, Any]:
    atomic_write_json(request_path, request)
    command = [
        str(python_executable),
        "-m",
        module,
        "--request",
        str(request_path),
        "--response",
        str(response_path),
    ]
    environment = os.environ.copy()
    if module_root is not None:
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            str(module_root) if not existing else str(module_root) + os.pathsep + existing
        )
    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )
    if not response_path.exists():
        raise WorkerError(completed.stderr[-2000:] or f"worker produced no response: {module}")
    response = read_json(response_path)
    if completed.returncode != 0 or not response.get("ok"):
        message = response.get("message") or completed.stderr[-2000:] or "worker failed"
        raise WorkerError(f"{module}: {message}")
    return response
