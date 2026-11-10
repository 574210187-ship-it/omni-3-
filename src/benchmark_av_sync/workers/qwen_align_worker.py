from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--response", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(args.request.read_text(encoding="utf-8"))
        model_path = Path(request["model_path"])
        audio_path = Path(request["audio_path"])
        if not model_path.exists():
            raise FileNotFoundError(f"forced aligner model missing: {model_path}")
        if not audio_path.exists():
            raise FileNotFoundError(f"audio missing: {audio_path}")
        import torch
        from qwen_asr import Qwen3ForcedAligner

        model = Qwen3ForcedAligner.from_pretrained(
            str(model_path), dtype=torch.bfloat16, device_map=request.get("device", "cuda:0")
        )
        result = model.align(
            audio=str(audio_path),
            text=str(request["text"]),
            language=str(request.get("language", "Chinese")),
        )[0]
        payload = {
            "ok": True,
            "items": [
                {
                    "index": index,
                    "text": item.text,
                    "start": item.start_time,
                    "end": item.end_time,
                    "confidence": 1.0,
                    "source": "qwen3_forced_aligner",
                }
                for index, item in enumerate(result.items)
            ],
        }
    except Exception as exc:
        code = "dependency_missing" if isinstance(exc, (FileNotFoundError, ImportError)) else "worker_failed"
        payload = {"ok": False, "error": code, "exception": type(exc).__name__, "message": str(exc)}
    args.response.parent.mkdir(parents=True, exist_ok=True)
    args.response.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0 if payload["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
