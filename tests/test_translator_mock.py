import pytest

from app.core.config import AppConfig, TranslationConfig
from app.models.term import Term
from app.services.translator import DeepSeekV4Translator, MockTranslator, parse_translation_json


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


@pytest.mark.asyncio
async def test_deepseek_missing_api_key_is_clear(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    translator = DeepSeekV4Translator(AppConfig(translation=TranslationConfig(provider="deepseek_v4")))

    with pytest.raises(RuntimeError, match="DEEPSEEK_API_KEY is not set"):
        await translator.translate("hello", "hello", "en", [])
