# Omni 第三阶段 字幕样式 D（公开版）

样式 D 属于 `omni-stage2-strict/v0.8.5` / `omni-stage2-contract/v4`。每个 job 必须携带
由当前包计算的 `strict_release` 与不可变的 `input_fingerprint`。

- 字体：`Noto Sans CJK SC Bold`；必须由 libass 从工作区真实字体文件解析。
- 画布：1080×1920；单行字幕，左右安全边距 72px。
- 质量：字幕外上方画面 SSIM ≥ 0.995，PSNR ≥ 45 dB。
- 字幕文本、语义切分和时间必须来自 Stage1 `subtitle.json`，不得重听主视频或改写文本。

未通过字体、边界、逐帧或画质门禁时必须失败，不能静默降级。
