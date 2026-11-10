# Omni Stage2 执行器说明（公开版）

当前发布为 `omni-stage2-strict/v0.8.3` / `omni-stage2-contract/v4`。

执行器一次只处理一个明确的 `record_id`、`video_number`、`input_fingerprint` 与
`strict_release` 组合。先以当前安装包生成 `strict_release`，写入并读回
`stage2_started`，再运行 `omni-stage2 run-row --job <absolute-job.json>`。

输入必须由你自己的受控存储直接物化并逐字节校验；不要把真实访问令牌、临时链接或媒体
提交到本仓库。完成后，外部发布器根据 `result.json` 的字段归属上传四个交付物，逐件 SHA-256
读回，再写入 `stage2_complete`。

公开版不附带任何具体业务系统或自动上传凭据。
