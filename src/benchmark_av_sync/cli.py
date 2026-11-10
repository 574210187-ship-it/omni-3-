from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .alignment import clean_alignment
from .contracts import AlignmentPlan, AlignmentToken
from .pairing import build_pair_manifest
from .pipeline import environment_report, run_sample, run_visual_sample
from .probe import probe_media
from .render import build_ffmpeg_command, render_plan
from .text_fusion import fuse_texts
from .timeline import solve_timeline
from .two_stage import (
    build_reference_package_from_video,
    online_preflight,
    render_from_reference_package,
)
from .verify import verify_plan, verify_rendered_media
from .workspace import BatchWorkspace, atomic_write_json, read_json


def _tokens(path: Path) -> list[AlignmentToken]:
    value = read_json(path)
    items = value.get("tokens", value.get("items", value)) if isinstance(value, dict) else value
    if not isinstance(items, list):
        raise ValueError(f"alignment file must contain a token list: {path}")
    return [AlignmentToken.from_dict(item) for item in items]


def _print(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2))


def _cmd_scan(args: argparse.Namespace) -> int:
    workspace = BatchWorkspace(args.batch_dir)
    workspace.ensure()
    probe = None
    if args.ffprobe:
        probe = lambda path: probe_media(path, args.ffprobe)
    manifest = build_pair_manifest(
        args.reference_dir,
        args.generated_dir,
        id_regex=args.id_regex,
        probe=probe,
        hash_files=not args.no_hash,
    )
    output = workspace.edit / "pair_manifest.json"
    atomic_write_json(output, manifest)
    _print({"output": str(output), "pair_count": len(manifest["pairs"]), "error_count": len(manifest["errors"])})
    return 0 if not manifest["errors"] else 2


def _cmd_fuse(args: argparse.Namespace) -> int:
    asr = args.asr_text or args.asr_text_file.read_text(encoding="utf-8")
    ocr = args.ocr_text or args.ocr_text_file.read_text(encoding="utf-8")
    result = fuse_texts(asr, ocr, asr_confidence=args.asr_confidence, ocr_confidence=args.ocr_confidence)
    atomic_write_json(args.output, result)
    _print({"output": str(args.output), "characters": len(result["tokens"]), "conflicts": len(result["conflicts"])})
    return 0


def _cmd_clean_alignment(args: argparse.Namespace) -> int:
    value = read_json(args.input)
    items = value.get("items", value) if isinstance(value, dict) else value
    qc = clean_alignment(items, audio_duration=args.audio_duration)
    atomic_write_json(args.output, qc.to_dict())
    _print({"output": str(args.output), "status": qc.status, "issues": len(qc.issues)})
    return 0 if qc.status != "fail" else 2


def _cmd_solve(args: argparse.Namespace) -> int:
    plan = solve_timeline(
        args.pair_id,
        _tokens(args.reference_alignment),
        _tokens(args.generated_alignment),
        reference_duration=args.reference_duration,
        generated_duration=args.generated_duration,
    )
    atomic_write_json(args.output, plan.to_dict())
    report = verify_plan(plan)
    _print({"output": str(args.output), "status": plan.status, "valid": report.passed, "errors": report.errors})
    return 0 if report.passed else 2


def _cmd_render(args: argparse.Namespace) -> int:
    plan = AlignmentPlan.from_dict(read_json(args.plan))
    if args.dry_run:
        command = build_ffmpeg_command(
            plan,
            generated_video=args.generated_video,
            reference_video=args.reference_video,
            output_path=args.output,
            ffmpeg_path=args.ffmpeg,
            preview=args.preview,
        )
        _print({"command": command})
        return 0
    render_plan(
        plan,
        generated_video=args.generated_video,
        reference_video=args.reference_video,
        output_path=args.output,
        ffmpeg_path=args.ffmpeg,
        preview=args.preview,
    )
    _print({"output": str(args.output), "duration": plan.output_duration})
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    plan = AlignmentPlan.from_dict(read_json(args.plan))
    report = verify_plan(plan)
    if args.output:
        atomic_write_json(args.output, report.to_dict())
    _print(report.to_dict())
    return 0 if report.passed else 2


def _cmd_verify_media(args: argparse.Namespace) -> int:
    report = verify_rendered_media(
        args.media,
        pair_id=args.pair_id,
        expected_duration=args.expected_duration,
        ffprobe_path=args.ffprobe,
        ffmpeg_path=args.ffmpeg,
    )
    if args.output:
        atomic_write_json(args.output, report.to_dict())
    _print(report.to_dict())
    return 0 if report.passed else 2


def _cmd_environment(args: argparse.Namespace) -> int:
    report = environment_report(
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        qwen_python=args.qwen_python,
        aligner_model=args.aligner_model,
        asr_model=args.asr_model,
        ocr_python=args.ocr_python,
        mouth_python=args.mouth_python,
    )
    _print(report)
    return 0 if report["ok"] else 2


def _cmd_run_sample(args: argparse.Namespace) -> int:
    result = run_sample(
        pair_id=args.pair_id,
        reference_dir=args.reference_dir,
        generated_dir=args.generated_dir,
        batch_dir=args.batch_dir,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        qwen_python=args.qwen_python,
        aligner_model=args.aligner_model,
        asr_model=args.asr_model,
        ocr_python=args.ocr_python,
        subtitle_box=args.subtitle_box,
        preview=not args.final,
    )
    _print(result)
    return 0


def _cmd_run_visual_sample(args: argparse.Namespace) -> int:
    result = run_visual_sample(
        pair_id=args.pair_id,
        reference_dir=args.reference_dir,
        generated_dir=args.generated_dir,
        batch_dir=args.batch_dir,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        qwen_python=args.qwen_python,
        mouth_python=args.mouth_python,
        aligner_model=args.aligner_model,
        asr_model=args.asr_model,
        ocr_python=args.ocr_python,
        subtitle_box=args.subtitle_box,
        preview=not args.final,
        approved_hard_speed_max=args.approved_hard_speed_max,
    )
    _print(result)
    return 0 if result.get("ok") else 2


def _cmd_build_reference_package(args: argparse.Namespace) -> int:
    result = build_reference_package_from_video(
        pair_id=args.pair_id,
        reference_video=args.reference_video,
        output=args.output,
        work_dir=args.work_dir,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        qwen_python=args.qwen_python,
        aligner_model=args.aligner_model,
        asr_model=args.asr_model,
        ocr_python=args.ocr_python,
        subtitle_box=args.subtitle_box,
    )
    _print(result)
    return 0


def _cmd_online_preflight(args: argparse.Namespace) -> int:
    result = online_preflight(
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        python_executable=args.python,
    )
    _print(result)
    return 0 if result["ok"] else 2


def _cmd_render_from_package(args: argparse.Namespace) -> int:
    result = render_from_reference_package(
        package_path=args.package,
        generated_video=args.generated_video,
        output_dir=args.output_dir,
        ffmpeg=args.ffmpeg,
        ffprobe=args.ffprobe,
        mouth_python=args.mouth_python,
        preview=args.preview,
        approved_hard_speed_max=args.approved_hard_speed_max,
    )
    _print(result)
    return 0 if result.get("ok") else 2


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmark-av-sync")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="pair reference and generated videos by numeric id")
    scan.add_argument("--reference-dir", type=Path, required=True)
    scan.add_argument("--generated-dir", type=Path, required=True)
    scan.add_argument("--batch-dir", type=Path, required=True)
    scan.add_argument("--id-regex")
    scan.add_argument("--ffprobe", type=Path)
    scan.add_argument("--no-hash", action="store_true")
    scan.set_defaults(func=_cmd_scan)

    fuse = sub.add_parser("fuse-text", help="merge ASR and OCR evidence")
    asr_group = fuse.add_mutually_exclusive_group(required=True)
    asr_group.add_argument("--asr-text")
    asr_group.add_argument("--asr-text-file", type=Path)
    ocr_group = fuse.add_mutually_exclusive_group(required=True)
    ocr_group.add_argument("--ocr-text")
    ocr_group.add_argument("--ocr-text-file", type=Path)
    fuse.add_argument("--asr-confidence", type=float, default=0.8)
    fuse.add_argument("--ocr-confidence", type=float, default=0.8)
    fuse.add_argument("--output", type=Path, required=True)
    fuse.set_defaults(func=_cmd_fuse)

    clean = sub.add_parser("clean-alignment", help="validate forced-alignment output")
    clean.add_argument("--input", type=Path, required=True)
    clean.add_argument("--audio-duration", type=float, required=True)
    clean.add_argument("--output", type=Path, required=True)
    clean.set_defaults(func=_cmd_clean_alignment)

    solve = sub.add_parser("solve", help="build a constrained alignment plan")
    solve.add_argument("--pair-id", required=True)
    solve.add_argument("--reference-alignment", type=Path, required=True)
    solve.add_argument("--generated-alignment", type=Path, required=True)
    solve.add_argument("--reference-duration", type=float, required=True)
    solve.add_argument("--generated-duration", type=float, required=True)
    solve.add_argument("--output", type=Path, required=True)
    solve.set_defaults(func=_cmd_solve)

    render = sub.add_parser("render", help="render a plan with FFmpeg")
    render.add_argument("--plan", type=Path, required=True)
    render.add_argument("--generated-video", type=Path, required=True)
    render.add_argument("--reference-video", type=Path, required=True)
    render.add_argument("--ffmpeg", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    render.add_argument("--preview", action="store_true")
    render.add_argument("--dry-run", action="store_true")
    render.set_defaults(func=_cmd_render)

    verify = sub.add_parser("verify-plan", help="verify no-middle-delete and timing invariants")
    verify.add_argument("--plan", type=Path, required=True)
    verify.add_argument("--output", type=Path)
    verify.set_defaults(func=_cmd_verify)

    verify_media = sub.add_parser("verify-media", help="probe and fully decode a rendered file")
    verify_media.add_argument("--media", type=Path, required=True)
    verify_media.add_argument("--pair-id", required=True)
    verify_media.add_argument("--expected-duration", type=float, required=True)
    verify_media.add_argument("--ffprobe", type=Path, required=True)
    verify_media.add_argument("--ffmpeg", type=Path, required=True)
    verify_media.add_argument("--output", type=Path)
    verify_media.set_defaults(func=_cmd_verify_media)

    environment = sub.add_parser("environment-report", help="check local binary, Python and model paths")
    environment.add_argument("--ffmpeg", type=Path, required=True)
    environment.add_argument("--ffprobe", type=Path, required=True)
    environment.add_argument("--qwen-python", type=Path, required=True)
    environment.add_argument("--aligner-model", type=Path, required=True)
    environment.add_argument("--asr-model", type=Path, required=True)
    environment.add_argument("--ocr-python", type=Path)
    environment.add_argument("--mouth-python", type=Path)
    environment.set_defaults(func=_cmd_environment)

    sample = sub.add_parser("run-sample", help="run the complete pipeline for one exact numeric id")
    sample.add_argument("--pair-id", required=True)
    sample.add_argument("--reference-dir", type=Path, required=True)
    sample.add_argument("--generated-dir", type=Path, required=True)
    sample.add_argument("--batch-dir", type=Path, required=True)
    sample.add_argument("--ffmpeg", type=Path, required=True)
    sample.add_argument("--ffprobe", type=Path, required=True)
    sample.add_argument("--qwen-python", type=Path, required=True)
    sample.add_argument("--aligner-model", type=Path, required=True)
    sample.add_argument("--asr-model", type=Path, required=True)
    sample.add_argument("--ocr-python", type=Path)
    sample.add_argument("--subtitle-box", type=int, nargs=4, metavar=("X", "Y", "W", "H"))
    sample.add_argument("--final", action="store_true", help="render final quality instead of preview")
    sample.set_defaults(func=_cmd_run_sample)

    visual = sub.add_parser(
        "run-visual-sample",
        help="run reference-JSON plus MediaPipe mouth-activity sync for one numeric id",
    )
    visual.add_argument("--pair-id", required=True)
    visual.add_argument("--reference-dir", type=Path, required=True)
    visual.add_argument("--generated-dir", type=Path, required=True)
    visual.add_argument("--batch-dir", type=Path, required=True)
    visual.add_argument("--ffmpeg", type=Path, required=True)
    visual.add_argument("--ffprobe", type=Path, required=True)
    visual.add_argument("--qwen-python", type=Path, required=True)
    visual.add_argument("--mouth-python", type=Path, required=True)
    visual.add_argument("--aligner-model", type=Path, required=True)
    visual.add_argument("--asr-model", type=Path, required=True)
    visual.add_argument("--ocr-python", type=Path)
    visual.add_argument("--subtitle-box", type=int, nargs=4, metavar=("X", "Y", "W", "H"))
    visual.add_argument("--final", action="store_true")
    visual.add_argument(
        "--approved-hard-speed-max",
        type=float,
        help="explicit per-run speed exception; default policy remains 1.25x",
    )
    visual.set_defaults(func=_cmd_run_visual_sample)

    build_package = sub.add_parser(
        "build-reference-package",
        help="analyze one reference video locally and emit a handoff ZIP",
    )
    build_package.add_argument("--pair-id", required=True)
    build_package.add_argument("--reference-video", type=Path, required=True)
    build_package.add_argument("--output", type=Path, required=True)
    build_package.add_argument("--work-dir", type=Path, required=True)
    build_package.add_argument("--ffmpeg", type=Path, required=True)
    build_package.add_argument("--ffprobe", type=Path, required=True)
    build_package.add_argument("--qwen-python", type=Path, required=True)
    build_package.add_argument("--aligner-model", type=Path, required=True)
    build_package.add_argument("--asr-model", type=Path, required=True)
    build_package.add_argument("--ocr-python", type=Path)
    build_package.add_argument(
        "--subtitle-box", type=int, nargs=4, metavar=("X", "Y", "W", "H")
    )
    build_package.set_defaults(func=_cmd_build_reference_package)

    preflight = sub.add_parser(
        "online-preflight",
        help="check the lightweight MediaPipe and FFmpeg online runtime",
    )
    preflight.add_argument("--ffmpeg", type=Path)
    preflight.add_argument("--ffprobe", type=Path)
    preflight.add_argument("--python", type=Path)
    preflight.set_defaults(func=_cmd_online_preflight)

    online = sub.add_parser(
        "render-from-package",
        help="run MediaPipe solve, render and verification from a reference package",
    )
    online.add_argument("--package", type=Path, required=True)
    online.add_argument("--generated-video", type=Path, required=True)
    online.add_argument("--output-dir", type=Path, required=True)
    online.add_argument("--ffmpeg", type=Path)
    online.add_argument("--ffprobe", type=Path)
    online.add_argument("--mouth-python", type=Path)
    online.add_argument("--preview", action="store_true")
    online.add_argument(
        "--approved-hard-speed-max",
        type=float,
        help="explicit per-run speed exception; default policy remains 1.25x",
    )
    online.set_defaults(func=_cmd_render_from_package)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
