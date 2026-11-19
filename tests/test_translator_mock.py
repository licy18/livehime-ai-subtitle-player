import json

import pytest

from app.core.config import AppConfig, TranslationConfig
from app.models.term import Term
from app.services.translator import (
    DeepSeekV4Translator,
    MockTranslator,
    is_simplified_chinese_translation,
    parse_translation_json,
)


@pytest.mark.asyncio
async def test_mock_translator_replaces_terms():
    translator = MockTranslator()
    result = await translator.translate(
        "Azure Familiar looks fun",
        "Azur Promilia looks fun",
        "en",
        [
            Term(
                id="game.example.title",
                category="game_title",
                source=["Azur Promilia"],
                aliases=["Azure Familiar"],
                zh_cn="蓝色星原：旅谣",
            )
        ],
    )

    assert "蓝色星原：旅谣" in result.zh
    assert result.terms_used == ["game.example.title"]


def test_parse_translation_json_repairs_wrapped_json():
    parsed = parse_translation_json('Here is {"zh":"你好","terms_used":[],"uncertain_terms":[]} done')

    assert parsed["zh"] == "你好"


def test_parse_translation_json_accepts_fenced_and_alias_keys():
    parsed = parse_translation_json('```json\n{"translation":"这个角色很强","terms_used":"bad"}\n```')

    assert parsed == {"zh": "这个角色很强", "terms_used": [], "uncertain_terms": []}


def test_parse_translation_json_rejects_broken_json():
    parsed = parse_translation_json('{"zh":"黄色, 巨, 陪, 亲, 爱,')

    assert parsed["zh"] == ""
    assert parsed["uncertain_terms"] == ["invalid_json"]


def test_simplified_chinese_translation_validation():
    assert is_simplified_chinese_translation("这个角色很强") is True
    assert is_simplified_chinese_translation("근데 뭐라고 했어") is False
    assert is_simplified_chinese_translation("Thank you for watching") is False


def test_deepseek_payload_adds_context_and_opt_in_thinking_setting():
    config = AppConfig()
    translator = DeepSeekV4Translator(config)
    assert "thinking" not in translator._build_payload("now", "now", "ja", [], [])

    config.translation.program_context = "奇波是游戏中的宠物伙伴。"
    config.translation.deepseek_v4.thinking_enabled = False
    payload = translator._build_payload("now", "now", "ja", [], ["older", "first", "second", "third"])
    assert payload["thinking"] == {"type": "disabled"}
    context = json.loads(payload["messages"][-1]["content"])
    assert context["program_context"] == config.translation.program_context
    assert context["recent_context"] == ["first", "second", "third"]


@pytest.mark.asyncio
async def test_deepseek_missing_api_key_is_clear(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("app.core.env._read_windows_env_var", lambda name, scope: None)
    translator = DeepSeekV4Translator(AppConfig(translation=TranslationConfig(provider="deepseek_v4")))

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY is not set"):
        await translator.translate("hello", "hello", "en", [])
