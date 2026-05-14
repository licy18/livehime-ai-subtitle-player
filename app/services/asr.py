from __future__ import annotations

import asyncio
import time
from uuid import uuid4

import numpy as np

from app.core.config import AppConfig
from app.models.subtitle import TranscriptSegment
from app.services.terminology_manager import TerminologyManager


class ASRWorker:
    def __init__(self, config: AppConfig, terminology: TerminologyManager) -> None:
        self.config = config
        self.terminology = terminology
        self._model = None

    async def transcribe_mock(self) -> TranscriptSegment:
        now = time.monotonic()
        await asyncio.sleep(0.05)
        return TranscriptSegment(
            id=str(uuid4()),
            source_text="I think Azure Familiar's new character looks insane",
            start_monotonic=now,
            end_monotonic=now + 2.4,
            language="en",
            asr_latency=0.05,
        )

    async def transcribe_bytes(self, audio_bytes: bytes, started_at: float, ended_at: float) -> TranscriptSegment:
        if self.config.asr.provider == "mock":
            return await self.transcribe_mock()
        started = time.perf_counter()
        model = self._get_model()
        hotwords = ", ".join(self.terminology.get_asr_hotwords(self.config.asr.initial_prompt_terms))
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        segments, info = await asyncio.to_thread(
            model.transcribe,
            audio,
            language=self.config.asr.language,
            beam_size=self.config.asr.beam_size,
            initial_prompt=hotwords or None,
        )
        source_text = " ".join(segment.text.strip() for segment in segments).strip()
        return TranscriptSegment(
            id=str(uuid4()),
            source_text=source_text,
            start_monotonic=started_at,
            end_monotonic=ended_at,
            language=getattr(info, "language", self.config.asr.language),
            asr_latency=time.perf_counter() - started,
        )

    def _get_model(self):
        if self._model is None:
            try:
                from faster_whisper import WhisperModel
            except ImportError as exc:
                raise RuntimeError("faster-whisper is not installed") from exc
            self._model = WhisperModel(
                self.config.asr.model,
                device=self.config.asr.device,
                compute_type=self.config.asr.compute_type,
            )
        return self._model
