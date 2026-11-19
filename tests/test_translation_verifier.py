import json

from app.core.config import AppConfig
from app.models.term import Term
from app.services.translation_verifier import TranslationVerifier, contains_untranslated_foreign_script
from app.services.translator import DeepSeekV4Translator


def test_verifier_replaces_forbidden_translation():
    verifier = TranslationVerifier()
    term = Term(
        id="game.example.title",
        category="game_title",
        source=["Azur Promilia"],
        zh_cn="蓝色星原：旅谣",
        forbidden_translations=["阿祖尔普罗米利亚"],
    )

    result = verifier.verify("Azur Promilia", "Azur Promilia", "阿祖尔普罗米利亚很好玩", [term])

    assert result.text == "蓝色星原：旅谣很好玩"
    assert "forbidden_replaced:game.example.title" in result.actions


def test_verifier_inserts_required_term_when_missing():
    verifier = TranslationVerifier()
    term = Term(id="game.example.title", category="game_title", source=["Azur Promilia"], zh_cn="蓝色星原：旅谣")

    result = verifier.verify("Azur Promilia looks good", "Azur Promilia looks good", "看起来很好", [term])

    assert result.text.startswith("蓝色星原：旅谣")
    assert result.missing_terms[0].id == "game.example.title"


def test_verifier_replaces_non_chinese_translation():
    verifier = TranslationVerifier()

    result = verifier.verify("근데 얘가 뭐라고 했나?", "근데 얘가 뭐라고 했나?", "근데 얘가 뭐라고 했나?", [])

    assert result.text == "（翻译未返回中文）"
    assert "non_chinese_translation_replaced" in result.actions


def test_verifier_replaces_english_fallback_translation():
    verifier = TranslationVerifier()

    result = verifier.verify("hello world", "hello world", "hello world", [])

    assert result.text == "（翻译未返回中文）"
    assert "non_chinese_translation_replaced" in result.actions


def test_foreign_script_detection_allows_chinese():
    assert contains_untranslated_foreign_script("我感觉这个角色很可爱") is False
    assert contains_untranslated_foreign_script("근데 얘가 뭐라고 했나?") is True
    assert contains_untranslated_foreign_script("お下げる儀式です") is True


def test_fuzzy_candidate_is_a_hint_without_forced_replacement():
    term = Term(
        id="hospital", category="location", source=["カルノン療養院"],
        zh_cn="卡尔侬疗养院", forbidden_translations=["病院"],
    )
    transcript = "カルノン療養員がいます"
    payload = DeepSeekV4Translator(AppConfig())._build_payload(transcript, transcript, "ja", [term], [])
    message = json.loads(payload["messages"][1]["content"])
    result = TranslationVerifier().verify(transcript, transcript, "这里有个病院。", [term])

    assert message["candidate_terms"][0]["match_type"] == "fuzzy"
    assert result.text == "这里有个病院。"
    assert result.missing_terms == []
