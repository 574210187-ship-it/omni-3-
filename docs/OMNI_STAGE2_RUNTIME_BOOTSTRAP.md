# Omni 第三阶段 运行时 Bootstrap（公开版）

目标发布：`omni-stage2-strict/v0.8.4` / `omni-stage2-contract/v4`。使用 Python 3.11.14，
安装 MediaPipe 0.10.21、OpenCV 4.11.0、NumPy 1.26.4，并确认 FFmpeg 带 `minterpolate` 与
`ass`。每个 job 均携带 `input_fingerprint` 和由当前包生成的 `strict_release`。

先创建隔离虚拟环境并安装：

```bash
python3.11 -m venv .venv
. .venv/bin/activate
python -m pip install -e ".[online]"
PYTHONPATH=src python scripts/verify_strict_release.py
```

运行时回执必须记录实际版本、FaceMesh 黑帧检查、FFmpeg/ffprobe/minterpolate/libass 与
`Noto Sans CJK SC Bold` 字体检查。恢复或重建必须可追溯，不能伪装为其他来源。
