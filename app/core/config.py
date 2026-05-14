from __future__ import annotations

from pathlib import Path
from typing import Literal

import os
import yaml
from pydantic import BaseModel, Field


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8765


class InputConfig(BaseModel):
    url: str = "tests/assets/sample.mp4"
    resolver: Literal["direct", "streamlink"] = "direct"
    quality: str = "best"


class NetworkConfig(BaseModel):
    proxy_enabled: bool = False
    proxy_url: str = "http://127.0.0.1:7897"


class VideoConfig(BaseModel):
    delay_seconds: float = 15.0
    hls_time: int = 1
    hls_list_size: int = 120
    prefer_copy: bool = True


class SubtitleConfig(BaseModel):
    extra_offset_seconds: float = 0.0
    hold_seconds_after_speech: float = 1.2
    max_chars_per_line: int = 24
    max_lines: int = 2
    bottom_percent: float = 18.0
    font_size_px: int = 42
    min_display_seconds: float = 1.5
    max_display_seconds: float = 8.0


class ASRConfig(BaseModel):
    enabled: bool = True
    provider: Literal["faster_whisper", "mock"] = "mock"
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    beam_size: int = 1
    language: str | None = None
    initial_prompt_terms: int = 80


class DeepSeekConfig(BaseModel):
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    timeout_seconds: float = 8.0
    retries: int = 1
    temperature: float = 0.1
    max_tokens: int = 512


class TranslationConfig(BaseModel):
    provider: Literal["mock", "deepseek_v4"] = "mock"
    target_language: str = "zh-CN"
    repair_strategy: Literal["rule_only", "rule_then_llm"] = "rule_only"
    deepseek_v4: DeepSeekConfig = Field(default_factory=DeepSeekConfig)


class GlossaryConfig(BaseModel):
    game_id: str = "example_game"
    paths: list[str] = Field(default_factory=lambda: ["glossaries/*.yaml"])
    fuzzy_threshold: int = 86
    max_candidate_terms: int = 20


class RuntimeConfig(BaseModel):
    runtime_dir: str = "app/runtime"
    hls_dir: str = "app/runtime/hls"
    live_terms_db: str = "app/runtime/live_terms.sqlite3"
    unknown_terms_log: str = "app/runtime/unknown_terms.log"


class AppConfig(BaseModel):
    server: ServerConfig = Field(default_factory=ServerConfig)
    input: InputConfig = Field(default_factory=InputConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    video: VideoConfig = Field(default_factory=VideoConfig)
    subtitle: SubtitleConfig = Field(default_factory=SubtitleConfig)
    asr: ASRConfig = Field(default_factory=ASRConfig)
    translation: TranslationConfig = Field(default_factory=TranslationConfig)
    glossary: GlossaryConfig = Field(default_factory=GlossaryConfig)
    runtime: RuntimeConfig = Field(default_factory=RuntimeConfig)


def load_config(path: str | Path = "config.yaml") -> AppConfig:
    config_path = Path(path)
    if not config_path.exists():
        fallback = Path("config.example.yaml")
        config_path = fallback if fallback.exists() else config_path

    raw: dict = {}
    if config_path.exists():
        with config_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}

    config = AppConfig.model_validate(raw)
    Path(config.runtime.runtime_dir).mkdir(parents=True, exist_ok=True)
    Path(config.runtime.hls_dir).mkdir(parents=True, exist_ok=True)
    return config


def save_config(config: AppConfig, path: str | Path = "config.yaml") -> None:
    config_path = Path(path)
    payload = config.model_dump(mode="json")
    with config_path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)


def proxy_env(config: AppConfig) -> dict[str, str]:
    env = os.environ.copy()
    if config.network.proxy_enabled and config.network.proxy_url.strip():
        proxy = config.network.proxy_url.strip()
        env["HTTP_PROXY"] = proxy
        env["HTTPS_PROXY"] = proxy
        env["ALL_PROXY"] = proxy
        env["http_proxy"] = proxy
        env["https_proxy"] = proxy
        env["all_proxy"] = proxy
    return env


def httpx_proxy(config: AppConfig) -> str | None:
    if config.network.proxy_enabled and config.network.proxy_url.strip():
        return config.network.proxy_url.strip()
    return None


def public_config(config: AppConfig) -> dict:
    return {
        "input": {
            "url": config.input.url,
            "resolver": config.input.resolver,
            "quality": config.input.quality,
        },
        "network": {
            "proxy_enabled": config.network.proxy_enabled,
            "proxy_url": config.network.proxy_url,
        },
        "video": {"delay_seconds": config.video.delay_seconds},
        "subtitle": {
            "extra_offset_seconds": config.subtitle.extra_offset_seconds,
            "max_chars_per_line": config.subtitle.max_chars_per_line,
            "max_lines": config.subtitle.max_lines,
            "bottom_percent": config.subtitle.bottom_percent,
            "font_size_px": config.subtitle.font_size_px,
            "min_display_seconds": config.subtitle.min_display_seconds,
            "max_display_seconds": config.subtitle.max_display_seconds,
        },
        "asr": {
            "enabled": config.asr.enabled,
            "provider": config.asr.provider,
            "model": config.asr.model,
            "device": config.asr.device,
        },
        "translation": {
            "provider": config.translation.provider,
            "target_language": config.translation.target_language,
        },
        "glossary": {"game_id": config.glossary.game_id},
    }
