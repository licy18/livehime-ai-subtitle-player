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

    assert client.get("/api/health").json()["ok"] is True
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
    hls_dir = tmp_path / "runtime" / "hls"
    hls_dir.mkdir(parents=True)
    (hls_dir / "index.m3u8").write_text("old playlist", encoding="utf-8")
    (hls_dir / "seg_000001.ts").write_text("old segment", encoding="utf-8")
    config = AppConfig(
        glossary=GlossaryConfig(paths=[str(PROJECT_ROOT / "glossaries" / "example_game.yaml")]),
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(hls_dir),
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
    assert not (hls_dir / "index.m3u8").exists()
    assert not (hls_dir / "seg_000001.ts").exists()


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


def test_api_subtitle_history_and_clear(tmp_path: Path):
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

    client.post("/api/runtime/mock-subtitle")
    history = client.get("/api/subtitles/history").json()

    assert len(history) == 1
    assert history[0]["source_text"]

    assert client.post("/api/subtitles/history/clear").json() == {"ok": True}
    assert client.get("/api/subtitles/history").json() == []


def test_api_deepseek_test_without_key(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr("app.core.env._read_windows_env_var", lambda name, scope: None)
    config = AppConfig(
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )
    client = TestClient(create_app(config))

    payload = client.post("/api/diagnostics/deepseek-test").json()

    assert payload["ok"] is False
    assert payload["error_type"] == "api_key_missing"


def test_clear_hls_preserves_non_hls_files(tmp_path: Path):
    hls_dir = tmp_path / "runtime" / "hls"
    hls_dir.mkdir(parents=True)
    (hls_dir / ".gitkeep").write_text("", encoding="utf-8")
    (hls_dir / "index.m3u8").write_text("playlist", encoding="utf-8")
    (hls_dir / "seg_000001.ts").write_text("segment", encoding="utf-8")
    (hls_dir / "notes.txt").write_text("keep", encoding="utf-8")
    config = AppConfig(
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(hls_dir),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )
    client = TestClient(create_app(config))

    response = client.post("/api/runtime/clear-hls")

    assert response.status_code == 200
    assert not (hls_dir / "index.m3u8").exists()
    assert not (hls_dir / "seg_000001.ts").exists()
    assert (hls_dir / ".gitkeep").exists()
    assert (hls_dir / "notes.txt").exists()


def test_api_input_qualities_direct(tmp_path: Path):
    config = AppConfig(
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )
    client = TestClient(create_app(config))

    payload = client.post(
        "/api/input/qualities",
        json={"input": {"url": "tests/assets/sample.mp4", "resolver": "direct", "quality": "best"}},
    ).json()

    assert payload["ok"] is True
    assert payload["qualities"] == ["direct"]
