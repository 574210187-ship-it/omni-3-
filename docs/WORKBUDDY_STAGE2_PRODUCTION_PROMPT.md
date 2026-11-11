# 已废止：旧 WorkBuddy 单行派发提示

本文件曾要求控制页逐行创建普通聊天页，再把单行任务交给新标签。这种方式没有一个真正的
“扫描未完成行 → 执行 → 上传 → 逐行回写 → 回读 → 重试”控制器，容易留下已渲染但未写回、
已上传但未完成、或旧页面重复处理的断层。

从 `omni-stage2-strict/v0.8.5` 起，唯一当前的 WorkBuddy 生产入口是：

- `docs/WORKBUDDY_STAGE2_BATCH_PROMPT.md`：粘贴到全新 WorkBuddy 窗口的完整批处理指令；
- `docs/OMNI_STAGE2_BATCH_BRIDGE_CONTRACT.md`：飞书 MCP 与批处理控制器的精确桥接合同；
- `omni-stage2-batch --config <private-config.json> --date <Asia/Shanghai YYYY-MM-DD> --execute`：
  程序化控制入口。

不得把本旧文件当作生产调度规则，也不得再创建“每行一个普通聊天页”来替代批处理控制器。
