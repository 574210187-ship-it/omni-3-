# Omni 第三阶段

这是 Omni 第三阶段视频合并与字幕渲染引擎的公开、可复现版本。它基于当前生产引擎
`omni-stage2-strict/v0.8.4` / `omni-stage2-contract/v4`，但不含任何客户素材、记录、
附件、访问凭据或业务系统标识。

用户可见名称统一为“Omni 第三阶段”。为兼容既有自动化和历史回执，内部技术标识仍使用
`stage2`、`omni-stage2-batch` 与 `omni-stage2-contract`。

## 适用范围

- 将 Stage1 v2 参考包与对应生成视频按序号配对；
- 保留参考音频，受约束地重定时视频并生成单行中文字幕；
- 输出 segments ZIP、完整无字幕 MP4、完整字幕 MP4 和 evidence ZIP；
- 对输入、运行时、字幕质量、交付哈希和远端完成事件执行 fail-closed 校验。

它包含完整但通用的 `omni-stage2-batch` 批处理控制器：在接入你自己的受保护来源绑定和
桥接后，它会扫描全部目标行、选择未完成行、执行、逐件追加上传与 SHA-256 读回、逐行写
完成事件，并对首轮失败行只重试一次。公开仓库绝不包含任何实际飞书绑定、OAuth 凭据或
业务媒体。

## 快速安装与验证

需要 Python 3.11、FFmpeg（含 `minterpolate` 与 `ass` 过滤器）以及可被 libass 解析的
`Noto Sans CJK SC Bold` 字体。

```bash
git clone https://github.com/574210187-ship-it/omni-3-.git
cd omni-3-
python3.11 -m venv .venv
. .venv/bin/activate              # Windows: .venv\\Scripts\\activate
python -m pip install --upgrade pip
python -m pip install -e ".[online]"
PYTHONPATH=src python scripts/verify_strict_release.py
PYTHONPATH=src python -m unittest discover -s tests -v
```

通过上述两项命令表示源码发布锁和单元测试均正常；真正的 `run-row` 还需要符合
`schema_version=2` 的 job、真实 Stage1 参考包、生成视频、字体目录及运行时回执。

## 配置自己的来源绑定

选择器使用 `OMNI_STAGE2_SOURCE_BINDING_JSON` 绑定自己的来源，避免把任何业务 ID 写入
源码或提交到仓库。示例见 [config/source_binding.example.json](config/source_binding.example.json)：

```bash
export OMNI_STAGE2_SOURCE_BINDING_JSON="$(cat config/source_binding.example.json)"
```

请只在本机或受控密钥系统中设置真实值，切勿提交 `.env`、媒体、job、运行时回执或交付物。

## 可配置批处理

从 `config/omni_stage2.batch.example.json` 创建只保存在受保护环境的配置，并接入自己的
来源桥接后运行：

```bash
omni-stage2-batch --config private-stage2-batch.json --date 2026-11-11 --execute
```

桥接必须实现 `docs/OMNI_STAGE2_BATCH_BRIDGE_CONTRACT.md` 中的读取、SHA、附件物化、执行
记录追加、附件原生追加/精确删除与字段回写接口。完整 WorkBuddy 指令见
`docs/WORKBUDDY_STAGE2_BATCH_PROMPT.md`。

## 当前规则

- 运行时可复用、按校验清单恢复，或在用户明确授权下受控重建；不要求只能使用某个离线包。
- 速度例外必须保留 `review_receipt`，可用完整人工回放或已绑定授权的
  `authorized_automated_audit` 关闭。
- 交付上传幂等键为 `omni2|sha256(dispatch_key)[0:16]|<artifact>|sha256`，长度不超过
  128 字符，重试必须复用同一键。

详见 [docs](docs/) 中的公开运行合同与 [PUBLIC_EXPORT.md](PUBLIC_EXPORT.md)。
