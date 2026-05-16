import os

from app.core.env import refresh_env_var_from_windows


def test_refresh_env_var_uses_existing_process_env(monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "from-process")

    assert refresh_env_var_from_windows("DEEPSEEK_API_KEY") == "from-process"


def test_refresh_env_var_can_read_windows_user_env(monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setattr(os, "name", "nt", raising=False)
    monkeypatch.setattr("app.core.env._read_windows_env_var", lambda name, scope: "from-user" if scope == "User" else None)

    assert refresh_env_var_from_windows("DEEPSEEK_API_KEY") == "from-user"
    assert os.environ["DEEPSEEK_API_KEY"] == "from-user"

