from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from pathlib import Path

from app.core.config import AppConfig, proxy_env
from app.services.streaming import StreamResolver


async def run_diagnostics(config: AppConfig) -> dict:
    hls_dir = Path(config.runtime.hls_dir)
    stream_result = await _check_stream_resolution(config)
    return {
        "python": {
            "executable": sys.executable,
            "version": sys.version.split()[0],
        },
        "tools": {
            "ffmpeg": _tool_version("ffmpeg", ["ffmpeg", "-version"]),
            "streamlink": _streamlink_version(),
        },
        "input": {
            "url": config.input.url,
            "resolver": config.input.resolver,
            "quality": config.input.quality,
            "resolution": stream_result,
        },
        "network": {
            "proxy_enabled": config.network.proxy_enabled,
            "proxy_url": config.network.proxy_url,
            "proxy_env_active": _proxy_env_active(config),
        },
        "translation": {
            "provider": config.translation.provider,
            "deepseek_api_key_present": bool(os.getenv("DEEPSEEK_API_KEY")),
        },
        "hls": {
            "dir": str(hls_dir),
            "playlist_exists": (hls_dir / "index.m3u8").exists(),
            "segment_count": len(list(hls_dir.glob("*.ts"))) if hls_dir.exists() else 0,
        },
    }


async def _check_stream_resolution(config: AppConfig) -> dict:
    if config.input.resolver == "direct":
        path = Path(config.input.url)
        return {
            "ok": True,
            "mode": "direct",
            "local_path_exists": path.exists() if not _looks_like_url(config.input.url) else None,
        }
    try:
        resolved = await asyncio.wait_for(StreamResolver(config).resolve(), timeout=20)
        return {"ok": True, "mode": "streamlink", "resolved_url": resolved[:300]}
    except Exception as exc:
        return {"ok": False, "mode": "streamlink", "error": str(exc)[-1000:]}


def _tool_version(command_name: str, command: list[str]) -> dict:
    source = shutil.which(command_name)
    if not source:
        return {"ok": False, "path": None, "version": None, "error": f"{command_name} not found"}
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
        first_line = (result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else ""
        return {
            "ok": result.returncode == 0,
            "path": source,
            "version": first_line,
            "error": None if result.returncode == 0 else (result.stderr or result.stdout)[-1000:],
        }
    except Exception as exc:
        return {"ok": False, "path": source, "version": None, "error": str(exc)}


def _streamlink_version() -> dict:
    source = shutil.which("streamlink")
    command = ["streamlink", "--version"] if source else [sys.executable, "-m", "streamlink", "--version"]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, env=proxy_env_for_subprocess())
        first_line = (result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else ""
        return {
            "ok": result.returncode == 0,
            "path": source or f"{sys.executable} -m streamlink",
            "version": first_line,
            "error": None if result.returncode == 0 else (result.stderr or result.stdout)[-1000:],
        }
    except Exception as exc:
        return {"ok": False, "path": source, "version": None, "error": str(exc)}


def proxy_env_for_subprocess() -> dict[str, str]:
    return os.environ.copy()


def _proxy_env_active(config: AppConfig) -> bool:
    env = proxy_env(config)
    proxy = config.network.proxy_url.strip()
    return bool(proxy and env.get("HTTPS_PROXY") == proxy and env.get("HTTP_PROXY") == proxy)


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))

