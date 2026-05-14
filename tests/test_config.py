from app.core.config import load_config


def test_config_example_loads():
    config = load_config("config.example.yaml")

    assert config.server.port == 8765
    assert config.translation.provider == "mock"
    assert config.glossary.game_id == "example_game"

