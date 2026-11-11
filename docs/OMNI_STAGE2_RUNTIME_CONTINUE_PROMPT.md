# Omni 第三阶段 运行时接续（公开版）

唯一生产合同为 `omni-stage2-strict/v0.8.4` / `omni-stage2-contract/v4`。所有 job 必须包含
`input_fingerprint` 与当前 `strict_release`。

可接受的运行时来源为：已验证复用、经清单验证的恢复，或 `mode=reconstructed` 的受控重建。
重建回执必须标注 `user_authorized_reconstruction`、固定源码提交和发布清单 SHA-256。

在接触媒体前，运行 `PYTHONPATH=src python scripts/verify_strict_release.py`；然后记录 Python
3.11.14、MediaPipe 0.10.21、OpenCV 4.11.0、NumPy 1.26.4、FaceMesh、FFmpeg、minterpolate、
libass 与字体门禁。不要把运行时回执或业务配置提交到公开仓库。
