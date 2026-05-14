from pathlib import Path

import yaml

from app.models.term import Term


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_example_glossary_schema_has_unique_ids():
    payload = yaml.safe_load((PROJECT_ROOT / "glossaries" / "example_game.yaml").read_text(encoding="utf-8"))
    terms = [Term.model_validate(item) for item in payload["terms"]]
    ids = [term.id for term in terms]

    assert len(ids) == len(set(ids))
    assert all(term.zh_cn for term in terms)
    assert {term.category for term in terms} >= {"game_title", "character", "item"}
