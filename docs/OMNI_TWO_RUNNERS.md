# Omni 双阶段执行合同（公开版）

第二阶段唯一生产发布为 `omni-stage2-strict/v0.8.3`，合同为
`omni-stage2-contract/v4`。Stage1 生成不可变参考包；Stage2 读取匹配视频、
`input_fingerprint` 和 `strict_release` 后渲染四件交付物。

Stage1 负责 `reference.wav`、`benchmark.json`、`subtitle.json` 和证据；Stage2 不得重做
OCR/ASR、不得改写字幕语义、不得混入主视频原声。发布由外部控制器依据 `result.json` 完成，
并在目标系统中保存输入输出 SHA-256 与完成事件。
