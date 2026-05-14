from pathlib import Path

import yaml

from app.core.config import AppConfig, GlossaryConfig, RuntimeConfig
from app.models.term import Term
from app.services.terminology_manager import TerminologyManager


def make_config(tmp_path: Path, glossary_path: Path) -> AppConfig:
    return AppConfig(
        glossary=GlossaryConfig(paths=[str(glossary_path)], fuzzy_threshold=80),
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )


def write_glossary(path: Path) -> None:
    payload = {
        "terms": [
            {
                "id": "game.example.title",
                "category": "game_title",
                "source": ["Azur Promilia"],
                "aliases": ["Azure Familiar"],
                "zh_cn": "蓝色星原：旅谣",
                "priority": 100,
                "asr_mishearings": ["Azure Familiar"],
                "forbidden_translations": ["阿祖尔普罗米利亚"],
            }
        ]
    }
    path.write_text(yaml.safe_dump(payload, allow_unicode=True), encoding="utf-8")


def test_loads_yaml_and_retrieves_terms(tmp_path):
    glossary_path = tmp_path / "terms.yaml"
    write_glossary(glossary_path)
    manager = TerminologyManager(make_config(tmp_path, glossary_path))

    terms = manager.retrieve_candidate_terms("I like Azur Promilia")

    assert terms[0].id == "game.example.title"


def test_normalizes_asr_mishearing(tmp_path):
    glossary_path = tmp_path / "terms.yaml"
    write_glossary(glossary_path)
    manager = TerminologyManager(make_config(tmp_path, glossary_path))

    result = manager.normalize_transcript("Azure Familiar is great")

    assert result.text == "Azur Promilia is great"
    assert result.replacements[0]["term_id"] == "game.example.title"


def test_live_term_overrides_static_term(tmp_path):
    glossary_path = tmp_path / "terms.yaml"
    write_glossary(glossary_path)
    manager = TerminologyManager(make_config(tmp_path, glossary_path))

    manager.add_live_term(
        Term(
            id="game.example.title",
            category="game_title",
            source=["Azur Promilia"],
            zh_cn="直播修正名",
            priority=200,
        )
    )

    terms = manager.retrieve_candidate_terms("Azur Promilia")
    assert terms[0].zh_cn == "直播修正名"

