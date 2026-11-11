# 公开发布说明

本仓库是 `chat-video-pipeline` 中 Omni 第三阶段 v0.8.3 引擎的公开发布副本。

- 已移除客户级 Bitable/资料库绑定、业务记录、媒体、运行时回执及交付物。
- 来源绑定改为启动时从 `OMNI_STAGE2_SOURCE_BINDING_JSON` 读取；未配置时保持 fail-closed。
- 公开代码保留严格发布锁、运行时回执、自动审计复核、字幕质量门禁和连接器安全幂等键。
- 本仓库不等同于任何客户生产环境；生产接入需自行配置来源、发布器、权限与保存策略。

当前公开提交应先运行 `scripts/verify_strict_release.py` 和完整单元测试后再用于试运行。
