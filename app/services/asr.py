from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass
from uuid import uuid4

import numpy as np

from app.core.config import AppConfig
from app.models.subtitle import TranscriptSegment
from app.services.terminology_manager import TerminologyManager


@dataclass
class ASRResult:
    text: str
    language: str | None
    start_seconds: float = 0.0
    end_seconds: float | None = None


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
        hotwords = ", ".join(
            filter_asr_hotwords(
                self.terminology.get_asr_hotwords(self.config.asr.initial_prompt_terms),
                self.config.asr.language,
            )
        )
        audio = np.frombuffer(audio_bytes, dtype=np.int16).astype(np.float32) / 32768.0
        result = await asyncio.to_thread(self._transcribe_sync, model, audio, hotwords)
        speech_start = min(ended_at, started_at + result.start_seconds)
        speech_end = ended_at if result.end_seconds is None else min(ended_at, started_at + result.end_seconds)
        return TranscriptSegment(
            id=str(uuid4()),
            source_text=result.text,
            start_monotonic=speech_start,
            end_monotonic=max(speech_start, speech_end),
            language=result.language,
            asr_latency=time.perf_counter() - started,
        )

    def _transcribe_sync(self, model, audio: np.ndarray, hotwords: str) -> ASRResult:
        language = self._effective_language()
        segments, info = model.transcribe(
            audio,
            language=language,
            task="transcribe",
            beam_size=self.config.asr.beam_size,
            initial_prompt=hotwords or None,
            condition_on_previous_text=False,
            vad_filter=False,
            temperature=0.0,
            log_prob_threshold=-1.0,
            no_speech_threshold=0.45,
            compression_ratio_threshold=2.4,
        )
        accepted_texts: list[str] = []
        start_seconds = None
        end_seconds = None
        for segment in segments:
            text = segment.text.strip()
            if should_drop_segment(text, segment, language):
                continue
            accepted_texts.append(text)
            if start_seconds is None:
                start_seconds = segment.start
            end_seconds = segment.end
        source_text = " ".join(accepted_texts).strip()
        if is_repetitive_hallucination(source_text) or is_garbage_transcript(source_text, language):
            source_text = ""
        return ASRResult(source_text, getattr(info, "language", language), start_seconds or 0.0, end_seconds)

    def _effective_language(self) -> str | None:
        if self.config.asr.language:
            return self.config.asr.language
        if self.config.asr.model.lower() == "tiny":
            return "ko"
        return None

    def _get_model(self):
        if self._model is None:
            cuda_library_dir = self.config.asr.cuda_library_dir
            if os.name == "nt" and self.config.asr.device == "cuda" and cuda_library_dir:
                # CTranslate2 resolves CUDA DLLs through the Windows process PATH.
                process_path = os.environ.get("PATH", "")
                if process_path.split(os.pathsep, 1)[0] != cuda_library_dir:
                    os.environ["PATH"] = f"{cuda_library_dir}{os.pathsep}{process_path}"
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


def is_repetitive_hallucination(text: str) -> bool:
    compact = "".join(char for char in text if not char.isspace() and char not in ",.?!，。？！、")
    if len(compact) < 12:
        return False
    unique_ratio = len(set(compact)) / len(compact)
    longest_run = 1
    current_run = 1
    previous = ""
    for char in compact:
        if char == previous:
            current_run += 1
            longest_run = max(longest_run, current_run)
        else:
            current_run = 1
        previous = char
    return unique_ratio < 0.18 or longest_run >= 8


def should_drop_segment(text: str, segment, expected_language: str | None = None) -> bool:
    if not text:
        return True
    if is_garbage_transcript(text, expected_language):
        return True
    if getattr(segment, "no_speech_prob", 0.0) >= 0.95:
        return True
    if getattr(segment, "compression_ratio", 0.0) > 4.0:
        return True
    return False


def is_garbage_transcript(text: str, expected_language: str | None = None) -> bool:
    compact = "".join(char for char in text if not char.isspace())
    if not compact:
        return False
    if "�" in compact:
        return True
    if len(compact) > 80 and compact.count(",") + compact.count("，") >= 8:
        return True

    counts = _script_counts(compact)
    if expected_language == "ko":
        has_hangul = counts["hangul"] >= 1
        if not has_hangul and (counts["han"] >= 1 or counts["kana"] >= 1 or counts["latin"] >= 4):
            return True
        if counts["kana"] >= 1 and not has_hangul:
            return True
        if counts["kana"] >= 2 and counts["kana"] >= counts["hangul"]:
            return True
        mixed_noise = counts["latin"] >= 6 or counts["han"] >= 12 or counts["kana"] >= 8
        if not has_hangul and mixed_noise:
            return True
        if len(compact) > 30 and counts["hangul"] == 0:
            return True
    return False


def _script_counts(text: str) -> dict[str, int]:
    counts = {"hangul": 0, "kana": 0, "han": 0, "latin": 0}
    for char in text:
        code = ord(char)
        if 0xAC00 <= code <= 0xD7AF or 0x1100 <= code <= 0x11FF:
            counts["hangul"] += 1
        elif 0x3040 <= code <= 0x30FF:
            counts["kana"] += 1
        elif "\u4e00" <= char <= "\u9fff":
            counts["han"] += 1
        elif "a" <= char.lower() <= "z":
            counts["latin"] += 1
    return counts


def filter_asr_hotwords(hotwords: list[str], language: str | None) -> list[str]:
    if not language:
        return hotwords
    filtered: list[str] = []
    for word in hotwords:
        counts = _script_counts(word)
        if language == "ko" and counts["hangul"] == 0:
            continue
        if language == "ja" and counts["kana"] == 0:
            continue
        filtered.append(word)
    return filtered
