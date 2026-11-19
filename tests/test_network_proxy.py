import asyncio
import subprocess

from app.core.config import AppConfig, NetworkConfig, proxy_env
from app.services.streaming import HLSBuffer, StreamResolver


def test_proxy_env_sets_common_proxy_variables():
    config = AppConfig(network=NetworkConfig(proxy_enabled=True, proxy_url="http://127.0.0.1:7897"))

    env = proxy_env(config)

    assert env["HTTP_PROXY"] == "http://127.0.0.1:7897"
    assert env["HTTPS_PROXY"] == "http://127.0.0.1:7897"
    assert env["ALL_PROXY"] == "http://127.0.0.1:7897"


def test_hls_buffer_passes_proxy_env(monkeypatch, tmp_path):
    captured = {}
    config = AppConfig(
        network=NetworkConfig(proxy_enabled=True, proxy_url="http://127.0.0.1:7897"),
    )
    config.runtime.hls_dir = str(tmp_path)

    monkeypatch.setattr("shutil.which", lambda name: "ffmpeg")

    def fake_popen(command, stdin=None, stdout=None, stderr=None, env=None):
        captured["env"] = env

        class FakeProcess:
            stderr = None

            def poll(self):
                return None

        return FakeProcess()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    HLSBuffer(config).start("https://example.test/live.m3u8")

    assert captured["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7897"


def test_stream_resolver_passes_proxy_env(monkeypatch):
    captured = {}
    config = AppConfig(
        network=NetworkConfig(proxy_enabled=True, proxy_url="http://127.0.0.1:7897"),
    )
    config.input.resolver = "streamlink"

    class FakeProcess:
        returncode = 0

        async def communicate(self):
            return b"https://example.test/stream.m3u8", b""

    async def fake_exec(*args, stdout=None, stderr=None, env=None):
        captured["env"] = env
        return FakeProcess()

    monkeypatch.setattr("asyncio.create_subprocess_exec", fake_exec)

    result = asyncio.run(StreamResolver(config).resolve("https://youtube.test/watch", "best"))

    assert result == "https://example.test/stream.m3u8"
    assert captured["env"]["HTTPS_PROXY"] == "http://127.0.0.1:7897"

