# DeepSeek 配置

默认配置使用 mock provider，便于无网络、无 API Key 时验证本地链路。

切换 DeepSeek：

```yaml
translation:
  provider: "deepseek_v4"
  deepseek_v4:
    base_url: "https://api.deepseek.com"
    model: "deepseek-v4-flash"
    timeout_seconds: 8
    retries: 1
    temperature: 0.1
    max_tokens: 512
```

API Key 只从环境变量读取：

```bat
setx DEEPSEEK_API_KEY "你的 API Key"
```

