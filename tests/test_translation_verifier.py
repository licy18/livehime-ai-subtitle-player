from app.models.term import Term
from app.services.translation_verifier import TranslationVerifier


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

