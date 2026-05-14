# 故障排查

## FFmpeg 不存在

安装 FFmpeg，并确认 `ffmpeg` 已加入 PATH。

## Streamlink 不存在

只有 `input.resolver=streamlink` 时需要 Streamlink。若使用本地文件或直连 URL，可保持 `resolver=direct`。

## DeepSeek API Key 未设置

运行：

```bat
setx DEEPSEEK_API_KEY "你的 API Key"
```

然后重新打开命令行或重新双击 `start.bat`。

## HLS 10 秒内未生成

检查 `input.url` 是否可访问，确认 FFmpeg 能读取该地址。若视频编码无法 copy，尝试把 `video.prefer_copy` 改为 `false`。

## 术语 YAML 错误

检查每个术语是否有 `id`、`category`、`source`、`zh_cn`。`id` 不要重复，`zh_cn` 不能为空。

## 字幕没有出现

先点击播放器页的 `Mock Subtitle`。如果 mock 字幕能出现，说明前端和 WebSocket 正常；再检查 ASR、翻译和 HLS 链路。

