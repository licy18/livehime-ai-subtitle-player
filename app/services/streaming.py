from __future__ import annotations

import asyncio
import json
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

from app.core.config import AppConfig, proxy_env
from app.services.hls_media import MediaPlaylist, read_media_playlist


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
    hls_flags = "delete_segments+program_date_time+temp_file"

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None
        self.upstream_process: subprocess.Popen | None = None
        self.last_error: str | None = None
        self.stream_id: str | None = None
        self.recording_path: Path | None = None
        self.recording_setup_error: str | None = None
        self.process_errors: deque[str] = deque(maxlen=24)
        self.recording_errors: deque[str] = deque(maxlen=1)

    @property
    def directory(self) -> Path:
        return Path(self.config.runtime.hls_dir).resolve()

    @property
    def list_size(self) -> int:
        video = self.config.video
        required = math.ceil(max(0, video.delay_seconds) / max(1, video.hls_time)) + 6
        return max(video.hls_list_size, required)

    def playlist(self) -> MediaPlaylist:
        if self.stream_id is None:
            return MediaPlaylist([], False)
        return read_media_playlist(self.directory / "index.m3u8")

    def start(self, source_url: str) -> None:
        self._start(source_url, use_streamlink=False)

    def start_streamlink_pipe(self, source_url: str) -> None:
        self._start(source_url, use_streamlink=True)

    def _start(self, source_url: str, *, use_streamlink: bool) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is not installed or not on PATH")

        hls_dir = self.directory
        hls_dir.mkdir(parents=True, exist_ok=True)
        self.stop()
        self.stop_orphaned_hls_processes()
        self.clear()
        index = hls_dir / "index.m3u8"
        stream_id = uuid4().hex
        segment = hls_dir / f"seg_{stream_id}_%06d.ts"
        self.recording_path = self._recording_target(stream_id)
        self.last_error = None
        self.process_errors = deque(maxlen=24)
        self.recording_errors = deque(maxlen=1)

        env = proxy_env(self.config)
        input_args = []
        source = source_url
        stdin = subprocess.PIPE
        if use_streamlink:
            self.upstream_process = subprocess.Popen(
                streamlink_live_command(self.config, source_url),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
            )
            self._drain_stderr(self.upstream_process)
            stdin = self.upstream_process.stdout
            source = "pipe:0"
        else:
            if Path(source_url).is_file():
                input_args.append("-re")
            if source_url.split("?", 1)[0].lower().endswith(".m3u8"):
                input_args += [
                    "-allowed_extensions", "ALL", "-allowed_segment_extensions", "ALL", "-extension_picky", "0",
                ]

        try:
            self.process = subprocess.Popen(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "warning",
                    *([] if use_streamlink else ["-fflags", "+genpts"]),
                    *input_args, "-i", source,
                    *self._output_args(index, segment, self.recording_path),
                ],
                stdin=stdin,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                env=env,
            )
        except OSError:
            self.stop()
            raise
        finally:
            if self.upstream_process and self.upstream_process.stdout:
                self.upstream_process.stdout.close()

        self._drain_stderr(self.process, watch_recording=self.recording_path is not None)
        self.stream_id = stream_id

    def _recording_target(self, stream_id: str) -> Path | None:
        self.recording_setup_error = None
        if not self.config.video.record_source:
            return None

        try:
            directory = Path(self.config.runtime.source_recordings_dir).resolve()
            if directory.is_relative_to(self.directory):
                self.recording_setup_error = "录制目录须位于 HLS 缓存目录之外。"
                return None
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.recording_setup_error = str(exc)
            return None
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        return directory / f"source-{timestamp}-{stream_id[:8]}.ts"

    def _output_args(self, index: Path, segment: Path, recording_path: Path | None) -> list[str]:
        copy_video = self.config.video.prefer_copy
        video_args = ["-c:v", "copy"] if copy_video else [
            "-c:v",
            "libx264",
            "-preset",
            "veryfast",
            "-tune",
            "zerolatency",
            "-pix_fmt",
            "yuv420p",
            "-force_key_frames",
            f"expr:gte(t,n_forced*{self.config.video.hls_time})",
        ]
        maps = ["-map", "0:v:0", "-map", "0:a:0?"]
        audio_args = ["-c:a", "aac", "-b:a", "160k", "-ar", "48000", "-ac", "2"]
        hls_options = {
            "hls_time": str(self.config.video.hls_time),
            "hls_list_size": str(self.list_size),
            "hls_flags": self.hls_flags,
            "hls_segment_filename": segment.as_posix(),
        }
        if recording_path is None:
            hls_args = [arg for key, value in hls_options.items() for arg in (f"-{key}", value)]
            return [*maps, *video_args, *audio_args, "-f", "hls", *hls_args, index.as_posix()]

        # Share the mapped video when it is already copied. Preserve the original
        # audio separately because the player always converts its audio to AAC.
        archive_video = "v:0"
        if not copy_video:
            maps += ["-map", "0:v:0"]
            video_args += ["-c:v:1", "copy"]
            archive_video = "v:1"
        maps += ["-map", "0:a:0?"]
        audio_args += ["-c:a:1", "copy"]
        hls = _tee_output(index, f="hls", select="v:0,a:0", **hls_options)
        archive = _tee_output(recording_path, f="mpegts", select=f"{archive_video},a:1", onfail="ignore")
        return [*maps, *video_args, *audio_args, "-f", "tee", f"{hls}|{archive}"]

    def _drain_stderr(self, process: subprocess.Popen, *, watch_recording: bool = False) -> None:
        if process.stderr is None:
            return

        def read_lines(pipe: BinaryIO, process_errors: deque[str], recording_errors: deque[str]) -> None:
            with pipe:
                for raw in pipe:
                    line = raw.decode("utf-8", errors="replace").strip()
                    process_errors.append(line)
                    if watch_recording and "Slave muxer #1 failed" in line:
                        recording_errors.append(line)

        threading.Thread(
            target=read_lines,
            args=(process.stderr, self.process_errors, self.recording_errors),
            daemon=True,
        ).start()

    def is_running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def stop(self) -> None:
        self.stream_id = None
        self.finish_input()

    def finish_input(self) -> None:
        if self.upstream_process and self.upstream_process.poll() is None:
            self.upstream_process.terminate()
            try:
                self.upstream_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.upstream_process.kill()
                self.upstream_process.wait(timeout=3)
        if self.process and self.process.poll() is None:
            try:
                if self.process.stdin:
                    self.process.stdin.write(b"q\n")
                    self.process.stdin.flush()
                self.process.wait(timeout=3)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                self.process.wait(timeout=3)
        if self.process and self.process.stdin:
            self.process.stdin.close()
        if self.upstream_process and self.upstream_process.stdout:
            self.upstream_process.stdout.close()
        self.process = None
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
        limit = max_segments or self.list_size + 8
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
        marker = str(self.directory).replace("'", "''")
        tee_marker = _escape_token(self.directory.as_posix(), "|").replace("'", "''")
        script = (
            "$marker = '"
            + marker
            + "'; $teeMarker = '"
            + tee_marker
            + "'; "
            "Get-CimInstance Win32_Process -Filter \"name = 'ffmpeg.exe'\" | "
            "Where-Object { $_.CommandLine -and ($_.CommandLine.Contains($marker) -or $_.CommandLine.Contains($teeMarker)) } | "
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
        if self.process is not None and not running and self.process.returncode != 0:
            self.last_error = "\n".join(self.process_errors.copy())[-1200:] or None
        playlist = self.playlist()
        return {
            "running": running,
            "playlist_exists": index.exists(),
            "playlist": "/hls/index.m3u8",
            "segment_count": self.segment_count(),
            "last_error": self.last_error,
            "stream_id": self.stream_id,
            "buffered_seconds": playlist.duration,
            "first_media_epoch": playlist.fragments[0].start_epoch if playlist.fragments else None,
            "ended": playlist.ended,
            "recording": self._recording_status(running),
        }

    def _recording_status(self, running: bool) -> dict:
        path = self.recording_path
        error = self.recording_setup_error or next(iter(self.recording_errors.copy()), None)
        size = 0
        if path:
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                pass
            except OSError as exc:
                error = error or str(exc)

        if error:
            state = "failed"
        elif size:
            state = "recording" if running else "saved"
        elif path and running:
            state = "pending"
        elif path:
            state, error = "failed", "此次抓取未写入源视频。"
        else:
            state = "idle" if self.config.video.record_source else "disabled"
        return {"state": state, "path": str(path) if path else None, "bytes": size, "error": error}


def _escape_token(value: str, separators: str) -> str:
    return "".join("\\" + char if char in "\\'" + separators else char for char in value)


def _tee_output(path: Path, **options: str) -> str:
    # FFmpeg parses the slave list before it parses the colon-separated options.
    args = ":".join(f"{key}={_escape_token(value, ':]')}" for key, value in options.items())
    return _escape_token(f"[{args}]{path.as_posix()}", "|")


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
