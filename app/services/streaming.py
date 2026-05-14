from __future__ import annotations

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from app.core.config import AppConfig


class StreamResolver:
    def __init__(self, config: AppConfig) -> None:
        self.config = config

    async def resolve(self, url: str | None = None, quality: str | None = None) -> str:
        source = url or self.config.input.url
        resolver = self.config.input.resolver
        if resolver == "direct":
            path = Path(source)
            return str(path.resolve()) if path.exists() else source

        streamlink_command = ["streamlink"]
        if not shutil.which("streamlink"):
            streamlink_command = [sys.executable, "-m", "streamlink"]

        proc = await asyncio.create_subprocess_exec(
            *streamlink_command,
            "--stream-url",
            source,
            quality or self.config.input.quality,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode("utf-8", errors="ignore") or "Streamlink failed")
        return stdout.decode("utf-8", errors="ignore").strip()


class HLSBuffer:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None

    def start(self, source_url: str) -> None:
        if not shutil.which("ffmpeg"):
            raise RuntimeError("FFmpeg is not installed or not on PATH")

        hls_dir = Path(self.config.runtime.hls_dir)
        hls_dir.mkdir(parents=True, exist_ok=True)
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
            "delete_segments+append_list+omit_endlist+program_date_time",
            "-hls_segment_filename",
            str(segment),
            str(index),
        ]
        self.stop()
        self.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None

    def status(self) -> dict:
        index = Path(self.config.runtime.hls_dir) / "index.m3u8"
        return {
            "running": self.process is not None and self.process.poll() is None,
            "playlist_exists": index.exists(),
            "playlist": "/hls/index.m3u8",
        }


class AudioExtractor:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.process: subprocess.Popen | None = None

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
        )
        return self.process

    def stop(self) -> None:
        if self.process and self.process.poll() is None:
            self.process.terminate()
        self.process = None
