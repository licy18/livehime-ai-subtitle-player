from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import AppConfig, GlossaryConfig, RuntimeConfig
from app.main import create_app


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_api_health_and_mock_subtitle(tmp_path: Path):
    config = AppConfig(
        glossary=GlossaryConfig(paths=[str(PROJECT_ROOT / "glossaries" / "example_game.yaml")]),
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )
    client = TestClient(create_app(config))

    assert client.get("/api/health").json() == {"ok": True}
    event = client.post("/api/runtime/mock-subtitle").json()

    assert event["type"] == "subtitle"
    assert "蓝色星原：旅谣" in event["text"]


def test_api_start_asr_and_stop(tmp_path: Path):
    config = AppConfig(
        glossary=GlossaryConfig(paths=[str(PROJECT_ROOT / "glossaries" / "example_game.yaml")]),
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )
    client = TestClient(create_app(config))

    started = client.post("/api/runtime/start-asr").json()
    assert started["asr"]["running"] is True

    stopped = client.post("/api/runtime/stop").json()
    assert stopped["asr"]["running"] is False
