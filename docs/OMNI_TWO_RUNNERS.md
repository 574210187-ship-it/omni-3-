# Omni 批处理与行级执行合同（公开版）

唯一严格发布为 `omni-stage2-strict/v0.8.5`，合同为 `omni-stage2-contract/v4`。
公开版不含任何客户数据、飞书绑定或凭据，但包含完整的可配置批处理控制器。

## 入口

```bash
omni-stage2-batch --config <private-config.json> --date <Asia/Shanghai-YYYY-MM-DD> --execute
```

该入口通过受保护的来源绑定和桥接层，读取全部目标行、为每行读取附件 SHA-256、调用严格
选择器、写并读回 `stage2_started`、调用行级 Runner、逐件上传/读回四个交付、精确替换旧件、
写完成事件和备注，并在首轮终态后只重试一次。

`omni-stage2 run-row --job <absolute-job.json>` 仍是唯一行级媒体入口；它不得绕过批处理
控制器自行挑选远端附件。

## 非可选门禁

- `input_fingerprint` 与 `strict_release` 必须进入开始事件、job、result、evidence 与完成事件；
- 严格锁验证六个核心文件（含批处理控制器）；
- 只有四件交付均原生追加、名称/大小/SHA-256 远端读回且 MP4 完整解码后，才可写
  `stage2_complete`；
- 返工只能在新件读回后按精确 `file_token` 删除旧件；
- `hold_*` 不上传、不删除、不写完成，attempt=2 失败后当天不再试。

将自己的来源绑定和桥接实现保留在受保护环境中。桥接接口见
`OMNI_STAGE2_BATCH_BRIDGE_CONTRACT.md`，新 WorkBuddy 窗口的完整指令见
`WORKBUDDY_STAGE2_BATCH_PROMPT.md`。
