# Omni 第三阶段

这是 Omni Stage2 视频合并与字幕渲染引擎的公开、可复现版本。它基于当前生产引擎
`omni-stage2-strict/v0.8.3` / `omni-stage2-contract/v4`，但不含任何客户素材、记录、
附件、访问凭据或业务系统标识。

## 适用范围

- 将 Stage1 v2 参考包与对应生成视频按序号配对；
- 保留参考音频，受约束地重定时视频并生成单行中文字幕；
- 输出 segments ZIP、完整无字幕 MP4、完整字幕 MP4 和 evidence ZIP；
- 对输入、运行时、字幕质量、交付哈希和远端完成事件执行 fail-closed 校验。

它不包含任何上传控制器。接入自己的表格、对象存储或媒体系统时，应由外部发布器按
`result.json` 的 `deliveries` 执行追加上传和 SHA-256 读回。

## 快速安装与验证

需要 Python 3.11、FFmpeg（含 `minterpolate` 与 `ass` 过滤器）以及可被 libass 解析的
`Noto Sans CJK SC Bold` 字体。

```bash
git clone https://github.com/574210187-ship-it/omni第三阶段.git
cd omni第三阶段
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

## 当前规则

- 运行时可复用、按校验清单恢复，或在用户明确授权下受控重建；不要求只能使用某个离线包。
- 速度例外必须保留 `review_receipt`，可用完整人工回放或已绑定授权的
  `authorized_automated_audit` 关闭。
- 交付上传幂等键为 `omni2|sha256(dispatch_key)[0:16]|<artifact>|sha256`，长度不超过
  128 字符，重试必须复用同一键。

详见 [docs](docs/) 中的公开运行合同与 [PUBLIC_EXPORT.md](PUBLIC_EXPORT.md)。
