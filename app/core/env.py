from __future__ import annotations

import os
import subprocess


def refresh_env_var_from_windows(name: str) -> str | None:
    value = os.getenv(name)
    if value:
        return value
    if os.name != "nt":
        return None
    for scope in ("User", "Machine"):
        value = _read_windows_env_var(name, scope)
        if value:
            os.environ[name] = value
            return value
    return None


def _read_windows_env_var(name: str, scope: str) -> str | None:
    try:
        result = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-Command",
                f"[Environment]::GetEnvironmentVariable('{name}', '{scope}')",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    value = result.stdout.strip()
    return value or None

