# Omni Stage2 调度提示（公开版）

当前发布为 `omni-stage2-strict/v0.8.3` / `omni-stage2-contract/v4`。

调度器只派发一个已选定行：`record_id`、`video_number`、`dispatch_key`、`attempt`、
`input_fingerprint`、输入清单和当前安装包生成的 `strict_release`。业务系统地址、令牌、
附件 ID 与真实媒体必须通过受控配置传递，不能硬编码在本仓库。

执行器需先读回 `stage2_started`，再运行严格 Runner；若速度例外触发，则附上与
record/dispatch/attempt 绑定的 `review_authorization`。只有四件交付物 SHA-256 读回、
`review_receipt` 完成且 `stage2_complete` 读回成功后，调度器才可将任务标为完成。
