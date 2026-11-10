# Omni Stage2 生产运行手册（公开版）

当前锁：`omni-stage2-strict/v0.8.3` / `omni-stage2-contract/v4`。开始前根据当前安装包生成
`strict_release`，并使它与 job、结果、evidence 和远端事件保持一致。

字幕必须使用由 libass 实际解析的 `Noto Sans CJK SC Bold`；字幕外上方画面门禁固定为
SSIM ≥ 0.995、PSNR ≥ 45 dB。

## 开始回执

写入并读回与本行绑定的 `stage2_started`，至少包含：

```json
{"v":4,"phase":"stage2_started","record_id":"<record_id>","video_number":"<video_number>","dispatch_key":"<dispatch_key>","input_fingerprint":"<sha256>","strict_release":{"release_id":"omni-stage2-strict/v0.8.3","contract":"omni-stage2-contract/v4","package_version":"0.8.3","manifest_sha256":"<sha256>"},"attempt":1,"readback_verified":true}
```

## 运行与复核

只运行 `omni-stage2 run-row --job <absolute-job.json>`。初次运行的默认速度上限为 1.25x；
仅在失败唯一原因是局部速度且不超过授权上限时才允许一次例外。例外结果保持
`review_required=true`，并须有完成的 `review_receipt`：`manual_full_playback`，或与
record、dispatch、attempt 绑定的 `authorized_automated_audit`。自动审计必须保存 SHA、完整解码、
音频一致、字幕质量和字幕帧检查。

## 发布

外部发布器原生追加四个交付物；不要整格覆盖。幂等键固定为：

`omni2|sha256(dispatch_key)[0:16]|<artifact>|sha256`

该格式保留完整交付 SHA，长度不超过连接器 128 字符。每件上传后读回名称、大小与 SHA-256；
两个 MP4 还须以完全相同的字节经本地完整解码验证。四件均成功后追加并读回
`stage2_complete`，其中 `remote_readback_verified=true`。
