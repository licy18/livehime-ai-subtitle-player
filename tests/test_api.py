from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import AppConfig, GlossaryConfig, RuntimeConfig
import app.main as main_module
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


def test_api_config_save_and_restart_request(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config = AppConfig(
        glossary=GlossaryConfig(paths=[str(PROJECT_ROOT / "glossaries" / "example_game.yaml")]),
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )
    restarted = {"called": False}

    def fake_restart(path):
        restarted["called"] = True
        assert path == config_path

    monkeypatch.setattr(main_module, "_restart_after_response", fake_restart)
    client = TestClient(create_app(config, config_path))
    payload = client.get("/api/config").json()
    payload["input"]["url"] = "D:/media/test.mp4"
    payload["input"]["resolver"] = "direct"
    payload["network"]["proxy_enabled"] = True
    payload["network"]["proxy_url"] = "http://127.0.0.1:7897"
    payload["asr"]["provider"] = "faster_whisper"

    response = client.post("/api/runtime/save-config-and-restart", json=payload)

    assert response.status_code == 200
    assert restarted["called"] is True
    assert "D:/media/test.mp4" in config_path.read_text(encoding="utf-8")
    assert "127.0.0.1:7897" in config_path.read_text(encoding="utf-8")


def test_api_diagnostics(tmp_path: Path):
    config = AppConfig(
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )
    client = TestClient(create_app(config))

    payload = client.get("/api/diagnostics").json()

    assert "tools" in payload
    assert payload["input"]["resolution"]["mode"] == "direct"
