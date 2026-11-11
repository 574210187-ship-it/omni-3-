# Omni 第三阶段 批处理飞书桥接合同

`omni-stage2-batch` 把媒体处理、选择和远端交付顺序做成了一个可审计控制器；它从不保存
飞书 OAuth 凭据。已连接飞书 MCP 的 WorkBuddy 窗口必须实现本合同中的小型桥接层，才能让
控制器实际运行。桥接可由宿主原生调用、受控子进程或同一窗口的工具循环实现，但不能用网页
点击、临时下载 URL、Cookie、浏览器开发者工具或用户手工搬运代替。

## 命令形状

控制器以如下形式调用桥接：

```text
<bridge> <operation> --request <absolute-request.json> --response <absolute-response.json>
```

请求总是含有 `context`；响应必须是 JSON 对象，成功时不写 `ok:false`。真实生产绑定只可放在
当前 WorkBuddy 的受保护上下文中，绝不可写入公开仓库、聊天输出、日志摘要或交付包。

| operation | WorkBuddy 必须实际使用的飞书能力 | 成功响应的最低形状 |
| --- | --- | --- |
| `connection_status` | `bitable_connection_status` | `{ "connected": true }` |
| `list_records` | 先 `bitable_list_views` 核对真实 view，再分页 `bitable_list_records` | `{ "records": [ ... ] }` |
| `get_record` | `bitable_get_record` | `{ "record": { "record_id": "…", "fields": { … } } }` |
| `read_attachment_sha256` | `bitable_read_attachment_sha256` | `{ "sha256": "<64 lower hex>" }` |
| `download_attachment` | 优先 `bitable_materialize_attachment`；需要落盘时使用受控原生资源/分片读取 | `{ "path": "<exact requested destination>" }` |
| `append_execution` | 先精确读该行，保留原 `执行记录`，追加一行 JSON 后 `bitable_update_record`，再由控制器读回 | `{ "ok": true }` |
| `upload_attachment` | MP4 用 `bitable_upload_video_append`，ZIP 用 `bitable_upload_zip_append`（或受支持的 `bitable_upload_file_append`） | `{ "attachment": { "file_token": "…", "name": "…", "size_bytes": 1 } }` |
| `remove_attachment_exact` | `bitable_remove_attachment_exact` | `{ "ok": true }` |
| `update_fields` | `bitable_update_record`，只传该次允许改的精确字段 | `{ "ok": true }` |

## 不可省略的桥接规则

1. `list_records` 只能读取经私有 `source_binding` 核验后的正式 view，必须翻完所有分页；不能按
   视频编号、ZED备注、附件顺序或 UI 可见行猜选。
2. 每次 `get_record`、SHA、下载、上传和删除都必须携带同一 `app_token/table_id/record_id` 与
   精确字段名；附件操作还必须携带该附件的 `file_token`。
3. `append_execution` 必须保留整个旧文本，添加一个换行与控制器给出的完整事件 JSON。写后若
   无法读回该 `event_id`，桥接应让控制器失败，不能报告成功。
4. 上传只准原生**追加**，必须原样使用控制器给出的 `idempotency_key`、绝对本地路径、文件名和
   真实大小；`continue_required` 只可用原参数继续，`uncertain` 必须停下来让控制器重新读回。
5. 删除只准 `remove_attachment_exact` 指定的单个 token。不得重写整个附件单元格、不得删除
   Stage1 ZIP、不得按文件名模糊删除。
6. 响应不得编造 SHA、大小、file token、读回或本地路径。无法做到任一项时返回失败，控制器会
   写入可读回的 `stage2_failed`/`stage2_failed_final`，而不是写完成。

该合同的设计目的是让 WorkBuddy 能自动执行整批表格任务，同时令凭据继续留在已连接的飞书
桥接服务中。
