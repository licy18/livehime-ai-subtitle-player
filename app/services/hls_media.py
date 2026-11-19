from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import av


SAMPLE_RATE = 16_000
PCM_SAMPLE_BYTES = 2


@dataclass(frozen=True)
class MediaFragment:
    sequence: int
    name: str
    start_epoch: float
    duration: float


@dataclass(frozen=True)
class MediaPlaylist:
    fragments: list[MediaFragment]
    ended: bool

    @property
    def duration(self) -> float:
        return sum(fragment.duration for fragment in self.fragments)


@dataclass(frozen=True)
class DecodedAudio:
    start_epoch: float
    start_pts: float | None
    video_pts: float | None
    pcm: bytes


def read_media_playlist(path: Path) -> MediaPlaylist:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (FileNotFoundError, PermissionError):
        return MediaPlaylist([], False)

    sequence = 0
    start_epoch = None
    duration = 0.0
    fragments: list[MediaFragment] = []
    for line in lines:
        if line.startswith('#EXT-X-MEDIA-SEQUENCE:'):
            sequence = int(line.split(':', 1)[1])
        elif line.startswith('#EXT-X-PROGRAM-DATE-TIME:'):
            start_epoch = datetime.fromisoformat(line.split(':', 1)[1]).timestamp()
        elif line.startswith('#EXTINF:'):
            duration = float(line.split(':', 1)[1].split(',', 1)[0])
        elif re.fullmatch(r'seg_(?:[0-9a-f]+_)?\d+\.ts', line) and start_epoch is not None and duration > 0:
            fragments.append(MediaFragment(sequence, line, start_epoch, duration))
            sequence += 1
            start_epoch += duration
            duration = 0.0
    return MediaPlaylist(fragments, '#EXT-X-ENDLIST' in lines)


def recording_video_offset(path: Path) -> float | None:
    """First video frame on the recording container's zero-based timeline."""
    try:
        with av.open(str(path), options={"skip_estimate_duration_from_pts": "1"}) as container:
            if container.start_time is not None and container.streams.video:
                video = container.streams.video[0]
                if video.start_time is not None:
                    return float(video.start_time * video.time_base) - container.start_time / av.time_base
    except (OSError, av.error.FFmpegError):
        pass
    return None


def decode_fragment_audio(path: Path, fragment: MediaFragment) -> DecodedAudio:
    with av.open(str(path / fragment.name)) as container:
        video = container.streams.video[0]
        video_pts = float(video.start_time * video.time_base) if video.start_time is not None else None
        if not container.streams.audio:
            return DecodedAudio(fragment.start_epoch, None, video_pts, b'')

        audio = container.streams.audio[0]
        resampler = av.AudioResampler(format='s16', layout='mono', rate=SAMPLE_RATE)
        pcm = bytearray()
        audio_pts = None
        for frame in container.decode(audio):
            if audio_pts is None:
                audio_pts = frame.time
            for output in resampler.resample(frame):
                pcm.extend(output.to_ndarray().tobytes())
        for output in resampler.resample(None):
            pcm.extend(output.to_ndarray().tobytes())
    offset = audio_pts - video_pts if audio_pts is not None and video_pts is not None else 0.0
    return DecodedAudio(fragment.start_epoch + offset, audio_pts, video_pts, bytes(pcm))
