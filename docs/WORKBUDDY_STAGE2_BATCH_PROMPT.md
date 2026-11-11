# 新 WorkBuddy 窗口：Omni 第三阶段全表批处理指令

将下面整个代码块原样粘贴到**一个全新的 WorkBuddy 窗口**。它替代旧的“只派发单行普通
聊天页”的做法：当前窗口自己完成选行、执行、上传、逐行写回和远端读回，不要求用户逐行
选单元格、上传附件、复制 token 或确认每一个文件。

```text
你现在是 Omni 第三阶段的唯一批处理控制器。我要你在本窗口自动完成：从已连接飞书多维表的正式视图读取全部行 → 找出尚未完成且符合第三阶段严格条件的行 → 串行执行每一行 → 原生追加四件交付 → 逐件远端读回 → 写入该行的完成事件和备注 → 首轮结束后只对首轮失败行再试一次。不要把任务拆回给我，不要让我点选单元格、上传文件、提供 Cookie 或复制 token。

我在此对本次批处理作出明确授权：对于本窗口从正式绑定和严格选择器生成的批次计划中的每一行，只要该行输入、严格发布锁、运行时、媒体校验和远端读回都通过，你可以：追加执行记录、原生追加对应交付文件、在合规返工中按精确 file_token 删除被新件替代的旧交付、以及更新该行 ZED备注。此授权不允许改表结构、创建/删除记录、覆盖整格附件、改动其他表/视图、删除 Stage1 输入、发布 GitHub 或暴露凭据。

一、先恢复正确的程序和绑定
1. 到本账号资料空间中读取最新私有 Omni 第三阶段生产源码、`docs/WORKBUDDY_STAGE2_BATCH_PROMPT.md`、`docs/OMNI_STAGE2_BATCH_BRIDGE_CONTRACT.md`、`config/omni_stage2.batch.example.json` 和当前严格发布文件；不要把公开 GitHub 仓库当作生产绑定来源。公开仓库只用于代码审阅/测试。
2. 使用当前源码创建隔离工作目录和 Python 3.11.14 环境，安装当前包；运行 `PYTHONPATH=src python scripts/verify_strict_release.py`。唯一可执行发布是 `omni-stage2-strict/v0.8.5` / `omni-stage2-contract/v4`；只有通过后才能读取业务附件。不得使用旧 v0.8.3 或更早包，也不得将历史提示词、历史离线包规则或旧普通聊天页流程带回生产。
3. 由当前私有安装包读取 `benchmark_av_sync.stage2_contract.SOURCE_BINDING`，在受保护的本窗口配置中作为 `source_binding` 使用。不得在聊天、公开文件、GitHub、批次摘要或日志中打印 app_token、table_id、view_id 或 token。
4. 调用已连接飞书 MCP 的 `bitable_connection_status`，必须实际得到 `connected=true`。再通过 `bitable_list_views` 核对私有绑定中的真实视图，随后用该真实 view_id 分页读取全部记录；不能只处理当前屏幕可见的几行。
5. 核对字段存在且是本流程字段：`视频编号`、`素材1`、`素材2`、`素材3`、`执行记录`、`zed备注`（若实际大小写不同，以正式绑定和字段读回为准）。任一绑定、字段或权限不符时，停止为 HOLD，报告唯一真实原因；不要猜测另一张表或另一视图。

二、必须使用完整控制器，不得只跑单行程序
1. 当前生产总入口是：
   `omni-stage2-batch --config <私有批配置.json> --date <Asia/Shanghai YYYY-MM-DD> --execute`
   行级渲染器 `omni-stage2 run-row --job ...` 只能由这个控制器为已选中的一行调用，绝不能手工挑附件后单独调用。
2. 在受保护工作目录按 `config/omni_stage2.batch.example.json` 建立实际配置：填写私有 source_binding、当前运行时的绝对路径、上述本次批处理授权文字，以及已授权的自动审计文字/时间。不得把该配置上传到 GitHub、资料空间公开目录或交付 ZIP。
3. 为 `omni-stage2-batch` 实现/接入同窗口的飞书桥接，严格遵循 `OMNI_STAGE2_BATCH_BRIDGE_CONTRACT.md`：控制器请求的 `connection_status`、`list_records`、`get_record`、`read_attachment_sha256`、`download_attachment`、`append_execution`、`upload_attachment`、`remove_attachment_exact`、`update_fields` 必须一一映射到已连接飞书 MCP 的真实调用和 JSON 回应。不能使用浏览器 UI 点击来冒充桥接。若当前 WorkBuddy 不能把 MCP 调用接入此桥接，则不要假装批处理已执行；明确报告这个唯一技术门禁。
4. 先无写入运行一次不带 `--execute` 的计划，保存 `batch-plan.json`。它必须列出正式 view 的全部 record_id 和每行选择结论。检查计划中没有 source-binding HOLD 后，使用上方的已给授权运行同一日期的 `--execute`；无需逐行向我再确认。

三、控制器对每一行必须执行的顺序（不可跳步）
1. 每一行先 `get_record` 全量读回，读取素材1、素材2、素材3的所有附件 token、文件名、真实大小和完整 SHA-256，再生成快照并调用严格选择器。不得按 ZED备注、视频编号、附件显示顺序或历史聊天判断是否完成。
2. 只按选择器状态行动：
   - `selected_initial`：进入首轮 attempt=1；
   - `skip_complete`：标记 already_complete，不下载、不上传；
   - `ineligible_stage1` 或 `rule_incompatible`：不处理媒体；仅在选择器给出时保留旧备注并去重追加“现有规则不适合”，随后远端读回；
   - `hold_*`：不写完成、不上传、不删除，保留唯一原因；
   - `retry_eligible_after_first_pass`：只能等首轮所有选中行已有终态后，再作为 attempt=2；
   - `skip_failed_final_today`：当天绝不再试。
3. 对真正要执行的行，先追加完整 `stage2_started` JSON 事件，并重新读取同一行，确认 event_id、record_id、dispatch_key、input_fingerprint、attempt 和 strict_release 都存在且 `readback_verified=true`。未读回前不得下载媒体或创建 job。
4. 仅将该行 `input_manifest` 指定的素材1主视频和素材2 Stage1 v3.2 ZIP 原生物化到本行工作目录；逐文件核对大小与 SHA-256。不得使用临时下载 URL、聊天附件、同名缓存或主视频原声替代真实字节。
5. 运行时必须实测通过：Python 3.11.14、MediaPipe 0.10.21、OpenCV 4.11.0、NumPy 1.26.4、FaceMesh 黑帧、ffmpeg、ffprobe、minterpolate、libass，以及可被 libass 解析的 Noto Sans CJK SC Bold。可以复用、受控恢复，或在本授权下按固定版本受控重建；无法通过就写真实失败事件，不降级。
6. 由控制器写入 schema_version=2 的 job 后只调用正式 Runner。Runner 成功并不等于行完成：必须读取 `result.json`，确认四件本地交付、所有本地 SHA、完整 MP4 解码、音频一致、字幕质量，以及需要复核时已完成的合法 review_receipt。
7. 原生追加交付，严格使用结果中的固定 idempotency_key：`segments.zip`、`complete.mp4`、`evidence.zip` 到素材2；`complete_captioned.mp4` 到素材3。每件上传后都重新读行并读取远端 SHA-256，确认文件名、大小、SHA 和 file_token；任何 uncertain/continue_required 只能以原 key 续传或先读回，不能换 key 盲传。
8. 若选择器要求返工替换，必须在四件新文件全部远端读回之后，才对 decision 中逐件列出的旧 file_token 做精确删除；每删一件重新读回，确认新件仍在、旧 token 不在、Stage1 ZIP 和其他附件都还在。不得覆盖整格，也不得按名字批量删除。
9. 四件交付全部远端读回后，才可用 `build_stage2_complete_event(... remote_readback_verified=True)` 生成并追加 `stage2_complete`；再读回该 event。然后保留原 `zed备注`，去重追加 `omni视频处理已完成` 并读回。只有这两次读回都成功，该行才是 success。
10. 任何一步失败：绝不写完成标记，绝不删旧件，追加并读回 `stage2_failed`（attempt=2 用 `stage2_failed_final`）及最短真实原因。不能用聊天自述、mock、单测、局部文件或“看起来已上传”代替远端验收。

四、批次完成和一次返工
1. 首轮所有 `selected_initial` 行都有 success/failed/hold 终态后，重新完整读取首轮失败行并重新跑选择器。只对返回 `retry_eligible_after_first_pass` 的同输入行执行一次 attempt=2。
2. attempt=2 失败必须写 `stage2_failed_final` 并停止；当天不再第三次尝试。输入变化产生的新 input_fingerprint 时，按新输入重新选择，不能被旧失败错误阻塞。
3. 最终写入并读回 `batch-receipt.json`。它必须覆盖正式 view 的每一行，至少列 record_id、视频编号、选择结论、dispatch_key/input_fingerprint（可在受保护产物保存）、attempt、结果、四件远端读回摘要和唯一原因。
4. 最终只给我一份简洁汇总：总行数、success、already_complete、ineligible、hold、failed_final，各失败/hold 的 record_id 和唯一原因，以及受保护批次回执的本地位置。绝不输出凭据、Cookie、完整绑定、附件临时链接或大段日志。

严格禁止：扫描/修改其他飞书表或 view；要求我逐行操作；跳过 SHA/远端读回；把旧离线包限制恢复；启动多个并行媒体行；以浏览器 UI、模拟文件或旧聊天替代桥接；把私有绑定、业务素材或运行配置推送到 GitHub；在没有 `stage2_complete` 远端读回时报告完成。
```

这个提示词与 `omni-stage2-batch` 的状态机对应；新窗口需要的唯一人工输入是这一次粘贴，
不是每行的再次确认。
