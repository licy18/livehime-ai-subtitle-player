# LiveHime AI Subtitle Player

本工具用于把用户已获授权、且符合平台规则的外语游戏直播源，在本地延迟播放，并叠加 AI 生成的简体中文字幕。最终由哔哩哔哩直播姬通过窗口捕获本地播放器开播。

## 合规声明

本工具仅用于用户拥有授权或符合平台规则的直播内容处理。使用者需要自行确认转播、翻译、字幕、二创、同传是否获得授权或符合平台规则。本工具不提供绕过 DRM、登录限制、付费墙、推流权限或平台风控的功能。

本项目不实现 B 站推流码提取、不接入 B 站 RTMP 推流、不绕过直播姬权限、不破解 DRM、不下载或转播无授权内容，也不在直播实时翻译链路中逐句联网搜索术语。

## 这是什么

核心链路：

1. 从本地文件或授权直播 URL 读取视频。
2. 用 FFmpeg 生成本地 HLS 延迟播放流。
3. 用 faster-whisper 或 mock ASR 生成原文片段。
4. 用 mock 或 DeepSeek V4 翻译为简中字幕。
5. 用本地游戏术语表强制修正官方译名。
6. 在 `/player` 页面叠加字幕，供直播姬窗口捕获。

## 系统要求

- Windows 10 / Windows 11
- Python 3.11+
- FFmpeg
- 可选：Streamlink
- 可选：NVIDIA GPU，用于 faster-whisper 加速

## 快速启动

1. 双击 `start.bat`。
2. 首次启动会创建 `config.yaml`。
3. 打开 `http://127.0.0.1:8765/player`。
4. 使用 `Mock Subtitle` 验证字幕链路。
5. 如需播放真实视频，把 `config.yaml` 的 `input.url` 改成授权直播源或本地视频路径，再点击 `Start HLS`。

## DeepSeek API Key

不要把 API Key 写入 `config.yaml`。

```bat
setx DEEPSEEK_API_KEY "你的 API Key"
```

重新打开命令行或重新双击 `start.bat` 后，把配置改为：

```yaml
translation:
  provider: "deepseek_v4"
```

## Real ASR mode

The default configuration uses `asr.provider: "mock"` so the player can be tested without downloading a Whisper model. To enable real speech recognition, edit `config.yaml`:

```yaml
asr:
  provider: "faster_whisper"
  model: "small"
  device: "cpu"
  compute_type: "int8"
  beam_size: 1
```

For an NVIDIA GPU, use:

```yaml
asr:
  provider: "faster_whisper"
  model: "large-v3"
  device: "cuda"
  compute_type: "float16"
  beam_size: 1
```

The first run may download the selected model and take longer than usual. The bundled `tests/assets/sample.mp4` is only a short HLS playback sample; real ASR testing needs a video or authorized stream with recognizable speech.

In `/player`, use `Start HLS` to begin local playback and `Start ASR` to start subtitle generation. If DeepSeek is not configured or translation fails, the app reports the error and falls back to normalized source text instead of stopping the WebSocket subtitle loop.

## 术语表

示例文件在 `glossaries/example_game.yaml`。直播中可打开：

```text
http://127.0.0.1:8765/admin/live-terms
```

新增或修改术语后，下一条字幕开始生效。

## 开播前术语生成器

打开：

```text
http://127.0.0.1:8765/admin/glossary-builder
```

可以粘贴官方资料、CSV、Markdown、JSON 或用户提供的官方 URL，生成候选术语草稿。第一版提供本地启发式抽取，保留 DeepSeek 生成链路的接口位置。

## 字幕不同步怎么办

- 字幕偏早：增大 `subtitle.extra_offset_seconds`，例如 `0.8`。
- 字幕偏晚：减小 `subtitle.extra_offset_seconds`，例如 `-0.8`。
- 字幕经常来不及：增大 `video.delay_seconds`，换更小 ASR 模型，使用 GPU，或降低 `asr.beam_size`。

## 已知限制

- ASR 不能保证 100% 听对游戏术语。
- Whisper prompt 只能辅助拼写，不是强制热词。
- DeepSeek 偶尔可能输出非 JSON，程序会尽量修复或降级。
- 直播中新术语只影响后续字幕，不回改已显示字幕。
- 需要登录、DRM 或地区限制的平台不保证可拉流。
