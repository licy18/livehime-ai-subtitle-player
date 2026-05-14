from __future__ import annotations

import json
import os
import re
import time
from abc import ABC, abstractmethod
from typing import Any

import httpx

from app.core.config import AppConfig, httpx_proxy
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
        api_key = os.getenv("DEEPSEEK_API_KEY")
        if not api_key:
            raise RuntimeError("DEEPSEEK_API_KEY is not set")

        started = time.perf_counter()
        payload = self._build_payload(source_text, normalized_text, source_language, candidate_terms, recent_context or [])
        url = self.deepseek.base_url.rstrip("/") + "/chat/completions"
        last_error: Exception | None = None

        for _ in range(self.deepseek.retries + 1):
            try:
                async with httpx.AsyncClient(
                    timeout=self.deepseek.timeout_seconds,
                    proxy=httpx_proxy(self.config),
                ) as client:
                    response = await client.post(
                        url,
                        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                        json=payload,
                    )
                response.raise_for_status()
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                parsed = parse_translation_json(content)
                return TranslationResult(
                    zh=parsed.get("zh", content),
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
        return {
            "model": self.deepseek.model,
            "temperature": self.deepseek.temperature,
            "max_tokens": self.deepseek.max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You translate live game subtitles into Simplified Chinese. "
                        "Return compact JSON only. Always preserve official zh_cn terms."
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
    try:
        data = json.loads(content)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if match:
        try:
            data = json.loads(match.group(0))
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            pass
    return {"zh": content.strip(), "terms_used": [], "uncertain_terms": []}


def _case_insensitive_replace(text: str, old: str, new: str) -> str:
    return re.sub(re.escape(old), new, text, flags=re.IGNORECASE)
