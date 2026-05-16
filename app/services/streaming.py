from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from app.core.config import AppConfig, proxy_env


class StreamResolver:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def resolve(self, url: str | None = None, quality: str | None = None) -> str:
        source = normalize_stream_page_url(url or self.config.input.url)
        resolver = self.config.input.resolver
        if resolver == "direct":
            path = Path(source)
            return str(path.resolve()) if path.exists() else source

        streamlink_command = ["streamlink"]
        if not shutil.which("streamlink"):
            streamlink_command = [sys.executable, "-m", "streamlink"]

        requested_quality = quality or self.config.input.quality
        try:
            return await self._resolve_with_streamlink(streamlink_command, source, requested_quality)
        except RuntimeError as exc:
            if requested_quality != "best":
                try:
                    return await self._resolve_with_streamlink(streamlink_command, source, "best")
                except RuntimeError as fallback_exc:
                    raise RuntimeError(f"{exc}; fallback best failed: {fallback_exc}") from fallback_exc
            raise

    async def _resolve_with_streamlink(self, streamlink_command: list[str], source: str, quality: str) -> str:
        proc = await asyncio.create_subprocess_exec(
            *streamlink_command,
            "--stream-url",
            source,
            quality,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=proxy_env(self.config),
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            message = (stderr.decode("utf-8", errors="ignore") or stdout.decode("utf-8", errors="ignore")).strip()
            raise RuntimeError(message or f"Streamlink failed for quality {quality}")
        return stdout.decode("utf-8", errors="ignore").strip()

    async def list_qualities(self, url: str | None = None) -> dict:
        source = normalize_stream_page_url(url or self.config.input.url)
        if self.config.input.resolver == "direct":
            return {"ok": True, "qualities": ["direct"], "selected": "direct", "warnings": []}

        streamlink_command = ["streamlink"]
        if not shutil.which("streamlink"):
            streamlink_command = [sys.executable, "-m", "streamlink"]

        warnings: list[str] = []
        try:
            payload = await self._streamlink_json(streamlink_command, source, proxy_env(self.config))
        except RuntimeError as exc:
            warnings.append(str(exc))
            payload = await self._streamlink_json(streamlink_command, source, os.environ.copy())
            warnings.append("Retried quality lookup without proxy.")

        qualities = parse_streamlink_qualities(payload)
        return {
            "ok": True,
            "qualities": qualities,
            "selected": self.config.input.quality if self.config.input.quality in qualities else "best",
            "warnings": warnings,
            "metadata": payload.get("metadata", {}),
        }

    async def _streamlink_json(self, streamlink_command: list[str], source: str, env: dict[str, str]) -> dict:
        proc = await asyncio.create_subprocess_exec(
            *streamlink_command,
            "--json",
            source,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        raw = (stdout or stderr).decode("utf-8", errors="ignore").strip()
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(raw or "Streamlink quality lookup failed") from exc
        if proc.returncode != 0 or payload.get("error"):
            raise RuntimeError(payload.get("error") or raw or "Streamlink quality lookup failed")
        return payload


def normalize_stream_page_url(source: str) -> str:
    chzzk_match = re.fullmatch(r"https?://chzzk\.naver\.com/([A-Za-z0-9_-]+)/*", source.strip())
    if chzzk_match:
        return f"https://chzzk.naver.com/live/{chzzk_match.group(1)}"
    return source


def parse_streamlink_qualities(payload: dict) -> list[str]:
    streams = payload.get("streams", {})
    if not isinstance(streams, dict):
        return ["best", "worst"]
    qualities = list(streams.keys())
    return sorted(qualities, key=_quality_sort_key)


def streamlink_live_command(config: AppConfig, source_url: str) -> list[str]:
    streamlink_command = ["streamlink"]
    if not shutil.which("streamlink"):
        streamlink_command = [sys.executable, "-m", "streamlink"]
    return [
        *streamlink_command,
        "--retry-open",
        "5",
        "--stream-segment-attempts",
        "5",
        "--stream-segment-timeout",
        "10",
        "--hls-live-edge",
        "4",
        "--stdout",
        normalize_stream_page_url(source_url),
        config.input.quality,
    ]


def _quality_sort_key(value: str) -> tuple[int, int, str]:
    if value == "worst":
        return (0, 0, value)
    if value == "best":
        return (2, 999999, value)
    match = re.search(r"(\d+)", value)
    return (1, int(match.group(1)) if match else 0, value)


class HLSBuffer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self.upstream_process: subprocess.Popen | None = None
        self.last_error: str | None = None

    def start(self, source_url: str) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is not installed or not on PATH")

        hls_dir = Path(self.config.runtime.hls_dir)
        hls_dir.mkdir(parents=True, exist_ok=True)
        self.stop()
        self.stop_orphaned_hls_processes()
        self.clear()
        index = hls_dir / "index.m3u8"
        segment = hls_dir / "seg_%06d.ts"
        video_args = ["-c:v", "copy"] if self.config.video.prefer_copy else [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
        ]
        command = [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-fflags",
            "+genpts",
            "-allowed_extensions",
            "ALL",
            "-allowed_segment_extensions",
            "ALL",
            "-extension_picky",
            "0",
            "-i",
            source_url,
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            *video_args,
            "-c:a",
            "aac",
            "-b:a",
            "160k",
            "-ar",
            "48000",
            "-ac",
            "2",
            "-f",
            "hls",
            "-hls_time",
            str(self.config.video.hls_time),
            "-hls_list_size",
            str(self.config.video.hls_list_size),
            "-hls_flags",
            "delete_segments+omit_endlist+program_date_time",
            "-hls_segment_filename",
            str(segment),
            str(index),
        ]
        self.last_error = None
        self.process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            env=proxy_env(self.config),
        )

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start_streamlink_pipe(self, source_url: str) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is not installed or not on PATH")

        hls_dir = Path(self.config.runtime.hls_dir)
        hls_dir.mkdir(parents=True, exist_ok=True)
        self.stop()
        self.stop_orphaned_hls_processes()
        self.clear()
        index = hls_dir / "index.m3u8"
        segment = hls_dir / "seg_%06d.ts"
        video_args = ["-c:v", "copy"] if self.config.video.prefer_copy else [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
        ]
        self.last_error = None
        self.upstream_process = subprocess.Popen(
            streamlink_live_command(self.config, source_url),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=proxy_env(self.config),
        )
        if self.upstream_process.stdout is None:
            raise RuntimeError("Streamlink stdout is not available")
        self.process = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-i",
                "pipe:0",
                "-map",
                "0:v:0",
                "-map",
                "0:a:0?",
                *video_args,
                "-c:a",
                "aac",
                "-b:a",
                "160k",
                "-ar",
                "48000",
                "-ac",
                "2",
                "-f",
                "hls",
                "-hls_time",
                str(self.config.video.hls_time),
                "-hls_list_size",
                str(self.config.video.hls_list_size),
                "-hls_flags",
                "delete_segments+omit_endlist+program_date_time",
                "-hls_segment_filename",
                str(segment),
                str(index),
            ],
            stdin=self.upstream_process.stdout,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        self.upstream_process.stdout.close()

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process = None
        if self.upstream_process and self.upstream_process.poll() is None:
            self.upstream_process.terminate()
            try:
                self.upstream_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.upstream_process.kill()
                self.upstream_process.wait(timeout=3)
        self.upstream_process = None

    def clear(self) -> None:
        hls_dir = Path(self.config.runtime.hls_dir)
        for pattern in ("index.m3u8", "index.m3u8.tmp", "*.ts", "*.m4s", "*.mp4", "*.tmp"):
            for item in hls_dir.glob(pattern):
                if item.name == ".gitkeep":
                    continue
                if item.is_file():
                    safe_unlink(item)

    def clear_runtime_files(self) -> None:
        self.stop()
        self.stop_orphaned_hls_processes()
        self.clear()

    def segment_count(self) -> int:
        hls_dir = Path(self.config.runtime.hls_dir)
        return len(list(hls_dir.glob("*.ts"))) if hls_dir.exists() else 0

    def trim_segments(self, max_segments: int | None = None) -> int:
        limit = max_segments or self.config.video.hls_list_size + 8
        if limit <= 0:
            return 0
        hls_dir = Path(self.config.runtime.hls_dir)
        segments = sorted(hls_dir.glob("*.ts"), key=lambda path: path.stat().st_mtime)
        overflow = max(0, len(segments) - limit)
        for item in segments[:overflow]:
            if item.is_file():
                if safe_unlink(item):
                    continue
        return overflow

    def stop_orphaned_hls_processes(self) -> None:
        if os.name != "nt":
            return
        marker = str(Path(self.config.runtime.hls_dir)).replace("/", "\\")
        script = (
            "$marker = '"
            + marker.replace("'", "''")
            + "'; "
            "$pidSelf = $PID; "
            "Get-CimInstance Win32_Process -Filter \"name = 'ffmpeg.exe'\" | "
            "Where-Object { $_.CommandLine -and $_.CommandLine.Contains($marker) } | "
            "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
        )
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
            )
        except Exception:
            return

    def status(self) -> dict:
        index = Path(self.config.runtime.hls_dir) / "index.m3u8"
        running = self.is_running()
        if self.process is not None and not running and self.process.stderr and self.last_error is None:
            try:
                error_text = self.process.stderr.read().decode("utf-8", errors="ignore").strip()
                upstream_error = ""
                if self.upstream_process is not None and self.upstream_process.stderr:
                    upstream_error = self.upstream_process.stderr.read().decode("utf-8", errors="ignore").strip()
                combined = "\n".join(part for part in (error_text, upstream_error) if part)
                self.last_error = combined[-1200:] if combined else None
            except Exception:
                self.last_error = "Unable to read FFmpeg error output"
        return {
            "running": running,
            "playlist_exists": index.exists(),
            "playlist": "/hls/index.m3u8",
            "segment_count": self.segment_count(),
            "last_error": self.last_error,
        }


def safe_unlink(path: Path, attempts: int = 3, delay_seconds: float = 0.15) -> bool:
    for attempt in range(attempts):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            if attempt + 1 >= attempts:
                return False
            time.sleep(delay_seconds)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 32:
                if attempt + 1 >= attempts:
                    return False
                time.sleep(delay_seconds)
                continue
            raise
    return False


class AudioExtractor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self.upstream_process: subprocess.Popen | None = None

    def start(self, source_url: str) -> subprocess.Popen:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is not installed or not on PATH")
        self.stop()
        self.process = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-allowed_extensions",
                "ALL",
                "-allowed_segment_extensions",
                "ALL",
                "-extension_picky",
                "0",
                "-i",
                source_url,
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=proxy_env(self.config),
        )
        return self.process

    def start_streamlink_pipe(self, source_url: str) -> subprocess.Popen:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is not installed or not on PATH")
        self.stop()
        self.upstream_process = subprocess.Popen(
            streamlink_live_command(self.config, source_url),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=proxy_env(self.config),
        )
        if self.upstream_process.stdout is None:
            raise RuntimeError("Streamlink stdout is not available")
        self.process = subprocess.Popen(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "warning",
                "-i",
                "pipe:0",
                "-vn",
                "-ac",
                "1",
                "-ar",
                "16000",
                "-f",
                "s16le",
                "pipe:1",
            ],
            stdin=self.upstream_process.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.upstream_process.stdout.close()
        return self.process

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.process = None
        if self.upstream_process and self.upstream_process.poll() is None:
            self.upstream_process.terminate()
            try:
                self.upstream_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.upstream_process.kill()
                self.upstream_process.wait(timeout=3)
        self.upstream_process = None

    def status(self) -> dict:
        process_running = self.process is not None and self.process.poll() is None
        upstream_running = self.upstream_process is not None and self.upstream_process.poll() is None
        return {
            "process_running": process_running,
            "upstream_running": upstream_running,
            "process_returncode": None if self.process is None else self.process.poll(),
            "upstream_returncode": None if self.upstream_process is None else self.upstream_process.poll(),
        }
