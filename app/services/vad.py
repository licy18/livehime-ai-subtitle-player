from __future__ import annotations

from dataclasses import dataclass
from math import sqrt


@dataclass
class AudioSegment:
    pcm: bytes
    start_seconds: float
    end_seconds: float


class VADSegmenter:
    """Small PCM energy VAD fallback for 16 kHz mono s16le audio."""

    def __init__(
        self,
        sample_rate: int = 16000,
        frame_ms: int = 30,
        silence_ms: int = 450,
        min_voice_ms: int = 180,
        max_segment_ms: int = 4500,
        rms_threshold: int = 260,
    ) -> None:
        self.sample_rate = sample_rate
        self.frame_ms = frame_ms
        self.silence_ms = silence_ms
        self.min_voice_ms = min_voice_ms
        self.max_segment_ms = max_segment_ms
        self.rms_threshold = rms_threshold
        self.frame_bytes = int(sample_rate * frame_ms / 1000) * 2
        self._buffer = bytearray()
        self._voice = bytearray()
        self._voice_start_ms: int | None = None
        self._cursor_ms = 0
        self._silence_run_ms = 0

    def push(self, pcm: bytes) -> list[AudioSegment]:
        self._buffer.extend(pcm)
        emitted: list[AudioSegment] = []
        while len(self._buffer) >= self.frame_bytes:
            frame = bytes(self._buffer[: self.frame_bytes])
            del self._buffer[: self.frame_bytes]
            segment = self._consume_frame(frame)
            if segment:
                emitted.append(segment)
        return emitted

    def flush(self) -> list[AudioSegment]:
        if self._voice and len(self._voice) >= self._duration_to_bytes(self.min_voice_ms):
            segment = self._emit_segment()
            return [segment] if segment else []
        self._voice.clear()
        self._voice_start_ms = None
        self._silence_run_ms = 0
        return []

    def _consume_frame(self, frame: bytes) -> AudioSegment | None:
        is_voice = _rms_s16le(frame) >= self.rms_threshold
        if is_voice and self._voice_start_ms is None:
            self._voice_start_ms = self._cursor_ms

        if self._voice_start_ms is not None:
            self._voice.extend(frame)

        self._silence_run_ms = 0 if is_voice else self._silence_run_ms + self.frame_ms
        self._cursor_ms += self.frame_ms

        duration_ms = len(self._voice) / 2 / self.sample_rate * 1000
        if self._voice and (
            self._silence_run_ms >= self.silence_ms or duration_ms >= self.max_segment_ms
        ):
            if duration_ms >= self.min_voice_ms:
                return self._emit_segment()
            self._voice.clear()
            self._voice_start_ms = None
            self._silence_run_ms = 0
        return None

    def _emit_segment(self) -> AudioSegment | None:
        if self._voice_start_ms is None:
            return None
        start = self._voice_start_ms / 1000
        end = start + (len(self._voice) / 2 / self.sample_rate)
        segment = AudioSegment(pcm=bytes(self._voice), start_seconds=start, end_seconds=end)
        self._voice.clear()
        self._voice_start_ms = None
        self._silence_run_ms = 0
        return segment

    def _duration_to_bytes(self, duration_ms: int) -> int:
        return int(self.sample_rate * duration_ms / 1000) * 2


def _rms_s16le(frame: bytes) -> float:
    if not frame:
        return 0.0
    sample_count = len(frame) // 2
    if sample_count == 0:
        return 0.0
    total = 0
    for index in range(0, sample_count * 2, 2):
        sample = int.from_bytes(frame[index : index + 2], "little", signed=True)
        total += sample * sample
    return sqrt(total / sample_count)
