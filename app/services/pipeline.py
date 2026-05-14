from __future__ import annotations

import asyncio
import time

from app.core.config import AppConfig
from app.models.subtitle import TranscriptSegment, TranslationResult
from app.services.asr import ASRWorker
from app.services.streaming import AudioExtractor, HLSBuffer, StreamResolver
from app.services.subtitle_scheduler import SubtitleScheduler
from app.services.terminology_manager import TerminologyManager
from app.services.translation_verifier import TranslationVerifier
from app.services.translator import Translator
from app.services.vad import VADSegmenter
from app.services.websocket_manager import WebSocketManager


class RuntimePipeline:
    def __init__(
        self,
        config: AppConfig,
        terminology: TerminologyManager,
        translator: Translator,
        websocket_manager: WebSocketManager,
    ) -> None:
        self.config = config
        self.terminology = terminology
        self.translator = translator
        self.websocket_manager = websocket_manager
        self.stream_resolver = StreamResolver(config)
        self.hls_buffer = HLSBuffer(config)
        self.audio_extractor = AudioExtractor(config)
        self.asr = ASRWorker(config, terminology)
        self.scheduler = SubtitleScheduler(config.video, config.subtitle)
        self.verifier = TranslationVerifier()
        self.started_at = time.monotonic()
        self.source_url: str | None = None
        self.last_error: str | None = None
        self._mock_task: asyncio.Task | None = None
        self._asr_task: asyncio.Task | None = None
        self.asr_segments_seen = 0
        self.asr_segments_processing = 0
        self.last_source_text: str | None = None
        self.last_normalized_text: str | None = None
        self.last_subtitle_text: str | None = None
        self.last_asr_latency: float | None = None
        self.last_translation_latency: float | None = None
        self.last_terms_hit = 0

    async def start_hls(self) -> dict:
        try:
            self.source_url = await self.stream_resolver.resolve()
            self.hls_buffer.start(self.source_url)
            await self.websocket_manager.broadcast({"type": "status", "message": "HLS buffer started"})
            return self.hls_buffer.status()
        except Exception as exc:
            self.last_error = str(exc)
            await self.websocket_manager.broadcast({"type": "error", "message": self.last_error})
            raise

    async def emit_transcript(self, transcript: TranscriptSegment) -> dict:
        normalization = self.terminology.normalize_transcript(transcript.source_text)
        candidate_terms = self.terminology.retrieve_candidate_terms(
            normalization.text,
            self.config.glossary.max_candidate_terms,
        )
        try:
            translation = await self.translator.translate(
                source_text=transcript.source_text,
                normalized_text=normalization.text,
                source_language=transcript.language,
                candidate_terms=candidate_terms,
            )
        except Exception as exc:
            self.last_error = str(exc)
            await self.websocket_manager.broadcast({"type": "error", "message": self.last_error})
            translation = TranslationResult(
                zh=normalization.text or transcript.source_text,
                terms_used=[],
                uncertain_terms=["translation_failed"],
                latency=0.0,
            )
        verification = self.verifier.verify(transcript.source_text, normalization.text, translation.zh, candidate_terms)
        event = self.scheduler.schedule(transcript, normalization.text, translation, verification)
        payload = event.model_dump(mode="json")
        self.asr_segments_seen += 1
        self.last_source_text = transcript.source_text
        self.last_normalized_text = normalization.text
        self.last_subtitle_text = event.text
        self.last_asr_latency = transcript.asr_latency
        self.last_translation_latency = translation.latency
        self.last_terms_hit = len(candidate_terms)
        await self.websocket_manager.broadcast(payload)
        return payload

    async def emit_mock_once(self) -> dict:
        transcript = await self.asr.transcribe_mock()
        return await self.emit_transcript(transcript)

    def start_mock_loop(self) -> None:
        if self._mock_task and not self._mock_task.done():
            return
        self._mock_task = asyncio.create_task(self._mock_loop())

    async def start_asr(self) -> dict:
        if self._asr_task and not self._asr_task.done():
            return self.status()
        if self.config.asr.provider == "mock":
            self.start_mock_loop()
            await self.websocket_manager.broadcast({"type": "status", "message": "Mock ASR loop started"})
            return self.status()

        try:
            if self.source_url is None:
                self.source_url = await self.stream_resolver.resolve()
            self._asr_task = asyncio.create_task(self._asr_loop(self.source_url))
            await self.websocket_manager.broadcast({"type": "status", "message": "ASR loop started"})
            return self.status()
        except Exception as exc:
            self.last_error = str(exc)
            await self.websocket_manager.broadcast({"type": "error", "message": self.last_error})
            raise

    async def _mock_loop(self) -> None:
        while True:
            try:
                await self.emit_mock_once()
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = str(exc)
                await self.websocket_manager.broadcast({"type": "error", "message": self.last_error})
                await asyncio.sleep(5)

    async def _asr_loop(self, source_url: str) -> None:
        vad = VADSegmenter()
        audio_base_monotonic = time.monotonic()
        try:
            process = self.audio_extractor.start(source_url)
            stdout = process.stdout
            if stdout is None:
                raise RuntimeError("Audio extractor stdout is not available")
            while True:
                chunk = await asyncio.to_thread(stdout.read, 4096)
                if not chunk:
                    break
                for audio_segment in vad.push(chunk):
                    await self._process_audio_segment(audio_base_monotonic, audio_segment)
            for audio_segment in vad.flush():
                await self._process_audio_segment(audio_base_monotonic, audio_segment)

            return_code = process.poll()
            if return_code not in (0, None):
                stderr = ""
                if process.stderr:
                    stderr = process.stderr.read().decode("utf-8", errors="ignore")
                raise RuntimeError(stderr.strip() or f"Audio extractor exited with {return_code}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            await self.websocket_manager.broadcast({"type": "error", "message": self.last_error})
        finally:
            self.audio_extractor.stop()

    async def _process_audio_segment(self, audio_base_monotonic: float, audio_segment) -> None:
        self.asr_segments_processing += 1
        try:
            transcript = await self.asr.transcribe_bytes(
                audio_segment.pcm,
                audio_base_monotonic + audio_segment.start_seconds,
                audio_base_monotonic + audio_segment.end_seconds,
            )
            if transcript.source_text.strip():
                await self.emit_transcript(transcript)
        finally:
            self.asr_segments_processing = max(0, self.asr_segments_processing - 1)

    def stop(self) -> None:
        self.hls_buffer.stop()
        self.audio_extractor.stop()
        if self._mock_task:
            self._mock_task.cancel()
            self._mock_task = None
        if self._asr_task:
            self._asr_task.cancel()
            self._asr_task = None

    def status(self) -> dict:
        return {
            "uptime_seconds": round(time.monotonic() - self.started_at, 2),
            "source_url": self.source_url,
            "last_error": self.last_error,
            "hls": self.hls_buffer.status(),
            "asr": {
                "provider": self.config.asr.provider,
                "running": self._is_task_running(self._asr_task) or self._is_task_running(self._mock_task),
                "segments_seen": self.asr_segments_seen,
                "segments_processing": self.asr_segments_processing,
                "last_source_text": self.last_source_text,
                "last_normalized_text": self.last_normalized_text,
                "last_subtitle_text": self.last_subtitle_text,
                "last_asr_latency": self.last_asr_latency,
                "last_translation_latency": self.last_translation_latency,
                "last_terms_hit": self.last_terms_hit,
            },
            "websocket_connections": self.websocket_manager.connection_count,
            "terms": len(self.terminology.all_terms()),
        }

    @staticmethod
    def _is_task_running(task: asyncio.Task | None) -> bool:
        return task is not None and not task.done()
