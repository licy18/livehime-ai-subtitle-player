from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.core.config import AppConfig, httpx_proxy
from app.core.env import refresh_env_var_from_windows
from app.models.subtitle import TranslationResult
from app.models.term import Term


class Translator(ABC):
    @abstractmethod
    async def translate(
        self,
        source_text: str,
        normalized_text: str,
        source_language: str | None,
        candidate_terms: list[Term],
        recent_context: list[str] | None = None,
    ) -> TranslationResult:
        ...


class MockTranslator(Translator):
    async def translate(
        self,
        source_text: str,
        normalized_text: str,
        source_language: str | None,
        candidate_terms: list[Term],
        recent_context: list[str] | None = None,
    ) -> TranslationResult:
        started = time.perf_counter()
        text = normalized_text or source_text
        for term in candidate_terms:
            for phrase in term.match_phrases:
                if phrase.lower() in text.lower():
                    text = _case_insensitive_replace(text, phrase, term.zh_cn)
        if not any("\u4e00" <= char <= "\u9fff" for char in text):
            text = f"模拟字幕：{text}"
        return TranslationResult(
            zh=text,
            terms_used=[term.id for term in candidate_terms if term.zh_cn in text],
            latency=time.perf_counter() - started,
        )


class DeepSeekV4Translator(Translator):
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.deepseek = config.translation.deepseek_v4

    async def translate(
        self,
        source_text: str,
        normalized_text: str,
        source_language: str | None,
        candidate_terms: list[Term],
        recent_context: list[str] | None = None,
    ) -> TranslationResult:
        api_key = refresh_env_var_from_windows("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")

        started = time.perf_counter()
        payload = self._build_payload(source_text, normalized_text, source_language, candidate_terms, recent_context or [])
        url = self.deepseek.base_url.rstrip("/") + "/chat/completions"
        last_error: Exception | None = None

        for attempt in range(self.deepseek.retries + 1):
            try:
                request_payload = payload
                if attempt > 0:
                    request_payload = self._build_payload(
                        source_text,
                        normalized_text,
                        source_language,
                        candidate_terms,
                        recent_context or [],
                        strict_retry=True,
                    )
                async with httpx.AsyncClient(
                    timeout=self.deepseek.timeout_seconds,
                    proxy=httpx_proxy(self.config),
                ) as client:
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=request_payload,
                    )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = parse_translation_json(content)
                zh = normalize_translation_text(parsed.get("zh", ""))
                if not is_simplified_chinese_translation(zh):
                    raise ValueError(f"DeepSeek returned non-Chinese zh field: {zh[:120]}")
                return TranslationResult(
                    zh=zh,
                    terms_used=parsed.get("terms_used", []),
                    uncertain_terms=parsed.get("uncertain_terms", []),
                    latency=time.perf_counter() - started,
                    raw=data,
                )
            except Exception as exc:
                last_error = exc

        raise RuntimeError(f"DeepSeek translation failed: {last_error}")

    def _build_payload(
        self,
        source_text: str,
        normalized_text: str,
        source_language: str | None,
        candidate_terms: list[Term],
        recent_context: list[str],
        strict_retry: bool = False,
    ) -> dict[str, Any]:
        user_payload = {
            "task": "translate_live_game_subtitle",
            "source_text": source_text,
            "normalized_text": normalized_text,
            "source_language": source_language or "auto",
            "target_language": self.config.translation.target_language,
            "style": {
                "subtitle": True,
                "max_chars_per_line": self.config.subtitle.max_chars_per_line,
                "max_lines": self.config.subtitle.max_lines,
                "tone": "natural concise live subtitle",
            },
            "candidate_terms": [
                {
                    "id": term.id,
                    "category": term.category,
                    "source": term.source,
                    "aliases": term.aliases,
                    "zh_cn": term.zh_cn,
                    "notes": term.notes,
                }
                for term in candidate_terms
            ],
            "recent_context": recent_context[-3:],
            "required_output_schema": {"zh": "string", "terms_used": ["string"], "uncertain_terms": ["string"]},
        }
        strict_instruction = (
            "\n\nSTRICT RETRY: Your previous response was rejected because zh was not Simplified Chinese. "
            "Translate the meaning into Simplified Chinese now. Do not copy Korean/Japanese/Thai/English source text."
            if strict_retry
            else ""
        )
        return {
            "model": self.deepseek.model,
            "temperature": self.deepseek.temperature,
            "max_tokens": self.deepseek.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "你是直播游戏字幕翻译器。你的唯一任务：把 source_text / normalized_text 的含义翻译成"
                        "自然、简洁、适合字幕显示的简体中文。\n"
                        "输入语言可能是韩语、日语、英语、泰语或自动识别文本。无论输入是什么语言，输出 zh "
                        "都必须是简体中文。\n"
                        "必须遵守：\n"
                        "1. 只返回 JSON 对象，不要 Markdown，不要解释。\n"
                        "2. JSON 必须包含 zh、terms_used、uncertain_terms 三个字段。\n"
                        "3. zh 必须是简体中文译文，禁止原样复制韩语、日语、泰语或英语。\n"
                        "4. source_text 如果包含乱码或 ASR 轻微错误，也要根据上下文尽量翻译成中文，不要输出原文。\n"
                        "5. candidate_terms 命中时必须使用对应 zh_cn 官方简中术语。\n"
                        '正确格式示例：{"zh":"这个角色很强。","terms_used":[],"uncertain_terms":[]}'
                        + strict_instruction
                    ),
                },
                {"role": "user", "content": json.dumps(user_payload, ensure_ascii=False)},
            ],
        }


def create_translator(config: AppConfig) -> Translator:
    if config.translation.provider == "deepseek_v4":
        return DeepSeekV4Translator(config)
    return MockTranslator()


def parse_translation_json(content: str) -> dict:
    content = strip_json_fence(content.strip())
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return normalize_translation_payload(data, content)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return normalize_translation_payload(data, content)
        except json.JSONDecodeError:
            pass
    if looks_like_broken_translation_json(content):
        return {"zh": "", "terms_used": [], "uncertain_terms": ["invalid_json"]}
    return {"zh": content.strip(), "terms_used": [], "uncertain_terms": []}


def strip_json_fence(content: str) -> str:
    match = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", content, flags=re.DOTALL | re.IGNORECASE)
    return match.group(1).strip() if match else content


def normalize_translation_payload(data: dict, raw_content: str) -> dict:
    zh = (
        data.get("zh")
        or data.get("zh_cn")
        or data.get("translation")
        or data.get("translated_text")
        or data.get("text")
        or raw_content
    )
    terms_used = data.get("terms_used", [])
    uncertain_terms = data.get("uncertain_terms", [])
    return {
        "zh": normalize_translation_text(str(zh)),
        "terms_used": terms_used if isinstance(terms_used, list) else [],
        "uncertain_terms": uncertain_terms if isinstance(uncertain_terms, list) else [],
    }


def looks_like_broken_translation_json(content: str) -> bool:
    stripped = content.lstrip()
    return stripped.startswith("{") or '"zh"' in content or "'zh'" in content


def normalize_translation_text(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def is_simplified_chinese_translation(text: str) -> bool:
    if not text:
        return False
    han_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
    foreign_count = 0
    for char in text:
        code = ord(char)
        if (
            0x3040 <= code <= 0x30FF
            or 0xAC00 <= code <= 0xD7AF
            or 0x1100 <= code <= 0x11FF
            or 0x0E00 <= code <= 0x0E7F
        ):
            foreign_count += 1
    return han_count >= 1 and foreign_count < 2


def _case_insensitive_replace(text: str, old: str, new: str) -> str:
    return re.sub(re.escape(old), new, text, flags=re.IGNORECASE)
