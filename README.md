# LiveHime AI Subtitle Player

本工具用于把用户已获授权、且符合平台规则的外语游戏直播源，在本地延迟播放，并叠加识别原文和 AI 生成的简体中文字幕。哔哩哔哩直播姬可以通过浏览器素材或窗口捕获接入本地播放器。

## 合规声明

本工具仅用于用户拥有授权或符合平台规则的直播内容处理。使用者需要自行确认转播、翻译、字幕、二创、同传是否获得授权或符合平台规则。本工具不提供绕过 DRM、登录限制、付费墙、推流权限或平台风控的功能。

本项目不实现 B 站推流码提取、不接入 B 站 RTMP 推流、不绕过直播姬权限、不破解 DRM、不下载或转播无授权内容，也不在直播实时翻译链路中逐句联网搜索术语。

## 这是什么

核心链路：

1. 从本地文件或授权直播 URL 读取视频。
2. 用 FFmpeg 生成本地 HLS 延迟播放流，语音识别读取同一条流的音频。
3. 用 faster-whisper 或 mock ASR 生成原文片段，再用 mock 或 DeepSeek 翻译为简体中文。
4. 从本地术语表筛选当前台词相关的术语，并校对明确命中的官方译名。
5. 按播放器的实际媒体时间显示字幕，可同时保存源音画及两份 SRT。

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
4. 点击「测试单条字幕」验证字幕链路。
5. 在控制台设置授权直播源或本地视频路径，保存并重启后点击「启动抓取」。直播网页地址可使用 Streamlink 解析，直接媒体地址及本地文件可选择直接读取。

## 接入直播姬

- 浏览器素材地址为 `http://127.0.0.1:8765/player?capture=1`，素材宽高按直播画布设置，例如 1920×1080。
- 使用窗口捕获时，点击控制台的「打开捕获窗口」，在捕获页用「全屏（含字幕）」让画面和字幕一起全屏。播放器显示「打开节目声音」时，点击该按钮开启原声。
- 开播前可用直播姬本地录制确认画面、字幕和原声。控制台的「启动抓取」用于本地播放。

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

## 语音识别与翻译

默认配置 `asr.provider: "mock"` 可以直接测试字幕。要启用真实识别，可在控制台选择 Faster-Whisper，或编辑 `config.yaml`：

```yaml
asr:
  provider: "faster_whisper"
  model: "small"
  device: "cpu"
  compute_type: "int8"
  beam_size: 1
```

使用 NVIDIA GPU 时，可以这样设置：

```yaml
asr:
  provider: "faster_whisper"
  model: "large-v3"
  device: "cuda"
  compute_type: "float16"
  beam_size: 1
```

首次运行可能需要下载模型。附带的 `tests/assets/sample.mp4` 用于测试 HLS 播放，测试真实识别时请选择含人声的视频或授权直播。

先点「启动抓取」，再点「启动识别与翻译」。已启用源视频录制和真实 ASR 时，「启动抓取」会自动开始识别及翻译。

可在「节目语境」填写背景和专名释义。翻译会参考同一节目近期最多三条识别原文。所用 DeepSeek 接口支持推理开关时，可在控制台关闭推理以降低耗时。

## 术语表

示例文件在 `glossaries/example_game.yaml`。可在 `glossary.paths` 配置自备术语表。系统优先使用精确匹配，模糊命中的候选会作为翻译参考。直播中可打开：

```text
http://127.0.0.1:8765/admin/live-terms
```

新增或修改术语后，下一条字幕开始生效。

## 源视频与双语 SRT

在控制台将「同步录制源视频」设为「随抓取自动录制」，保存并重启。默认在 `app/runtime/recordings/` 生成同名的一组文件：

- `.ts` 保存从源流抓到的音画。
- `.ja.srt` 保存识别原文，后缀沿用日语转播约定。
- `.zh-CN.srt` 保存中文译文，翻译请求失败的条目标为「待翻译」。

两份 SRT 以源录像开头为零点。点击「停止」后，等末尾台词处理完再取文件。需要更换录制盘时，可修改 `runtime.source_recordings_dir`，录制目录应位于 HLS 缓存目录之外。

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
