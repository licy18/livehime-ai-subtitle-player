from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from app.core.config import AppConfig
from app.core.env import refresh_env_var_from_windows
from app.models.subtitle import TranscriptSegment, TranslationResult
from app.services.asr import ASRWorker
from app.services.streaming import AudioExtractor, HLSBuffer, StreamResolver
from app.services.subtitle_scheduler import SubtitleScheduler
from app.services.terminology_manager import TerminologyManager
from app.services.translation_verifier import TranslationVerifier
from app.services.translator import DeepSeekV4Translator, Translator
from app.services.vad import VADSegmenter
from app.services.websocket_manager import WebSocketManager


@dataclass
class QueuedAudioSegment:
    pcm: bytes
    start_monotonic: float
    end_monotonic: float


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

        self.audio_queue: asyncio.Queue[QueuedAudioSegment] = asyncio.Queue(maxsize=config.queues.audio_queue_max)
        self.translation_queue: asyncio.Queue[TranscriptSegment] = asyncio.Queue(
            maxsize=config.queues.translation_queue_max
        )
        self.subtitle_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=config.queues.translation_queue_max)
        self.queue_dropped = {"audio": 0, "translation": 0, "subtitle": 0}
        self.history: deque[dict] = deque(maxlen=config.queues.subtitle_history_limit)
        self.asr_latencies: deque[float] = deque(maxlen=50)
        self.translation_latencies: deque[float] = deque(maxlen=50)

        self._mock_task: asyncio.Task | None = None
        self._audio_task: asyncio.Task | None = None
        self._hls_trim_task: asyncio.Task | None = None
        self._asr_worker_tasks: list[asyncio.Task] = []
        self._translation_worker_tasks: list[asyncio.Task] = []
        self._subtitle_worker_task: asyncio.Task | None = None
        self.asr_segments_seen = 0
        self.asr_segments_processing = 0
        self.translation_segments_processing = 0
        self.last_source_text: str | None = None
        self.last_normalized_text: str | None = None
        self.last_subtitle_text: str | None = None
        self.last_asr_latency: float | None = None
        self.last_translation_latency: float | None = None
        self.last_terms_hit = 0
        self.audio_chunks_seen = 0
        self.audio_bytes_seen = 0
        self.vad_segments_seen = 0
        self.last_audio_at: float | None = None

    async def start_hls(self) -> dict:
        try:
            if self.hls_buffer.is_running():
                await self.websocket_manager.broadcast({"type": "status", "message": "HLS buffer already running"})
                return self.hls_buffer.status()
            self.last_error = None
            if self.config.input.resolver == "streamlink":
                self.source_url = self.config.input.url
                self.hls_buffer.start_streamlink_pipe(self.config.input.url)
            else:
                self.source_url = await self.stream_resolver.resolve()
                self.hls_buffer.start(self.source_url)
            self._ensure_hls_trim_task()
            await self.websocket_manager.broadcast({"type": "status", "message": "HLS buffer started"})
            return self.hls_buffer.status()
        except Exception as exc:
            self.last_error = str(exc)
            await self.websocket_manager.broadcast({"type": "error", "message": self.last_error})
            raise

    async def emit_transcript(self, transcript: TranscriptSegment) -> dict:
        payload = await self._build_subtitle_payload(transcript)
        await self.websocket_manager.broadcast(payload)
        return payload

    async def emit_mock_once(self) -> dict:
        transcript = await self.asr.transcribe_mock()
        return await self.emit_transcript(transcript)

    def start_mock_loop(self) -> None:
        self._ensure_output_workers()
        if self._mock_task and not self._mock_task.done():
            return
        self._mock_task = asyncio.create_task(self._mock_loop())

    async def start_asr(self) -> dict:
        self._ensure_output_workers()
        if self.config.asr.provider == "mock":
            self.start_mock_loop()
            await self.websocket_manager.broadcast({"type": "status", "message": "Mock ASR loop started"})
            return self.status()
        if self._audio_task and not self._audio_task.done():
            return self.status()

        try:
            if self.source_url is None:
                self.source_url = await self.stream_resolver.resolve()
            self._ensure_asr_workers()
            self._audio_task = asyncio.create_task(self._audio_loop(self.source_url))
            await self.websocket_manager.broadcast({"type": "status", "message": "ASR loop started"})
            return self.status()
        except Exception as exc:
            self.last_error = str(exc)
            await self.websocket_manager.broadcast({"type": "error", "message": self.last_error})
            raise

    async def _mock_loop(self) -> None:
        while True:
            try:
                transcript = await self.asr.transcribe_mock()
                await self._put_drop_oldest(self.translation_queue, transcript, "translation")
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._record_error(exc)
                await asyncio.sleep(5)

    async def _audio_loop(self, source_url: str) -> None:
        vad = VADSegmenter()
        audio_base_monotonic = time.monotonic()
        while True:
            chunks_seen = 0
            bytes_seen = 0
            try:
                if self.config.input.resolver == "streamlink":
                    try:
                        audio_source_url = await self.stream_resolver.resolve(
                            self.config.input.url,
                            self.config.input.quality,
                        )
                        process = self.audio_extractor.start(audio_source_url)
                    except Exception:
                        process = self.audio_extractor.start_streamlink_pipe(self.config.input.url)
                else:
                    process = self.audio_extractor.start(source_url)
                stdout = process.stdout
                if stdout is None:
                    raise RuntimeError("Audio extractor stdout is not available")
                while True:
                    try:
                        chunk = await asyncio.wait_for(asyncio.to_thread(stdout.read, 4096), timeout=8)
                    except TimeoutError as exc:
                        raise RuntimeError("Audio extractor read timed out") from exc
                    if not chunk:
                        break
                    chunks_seen += 1
                    bytes_seen += len(chunk)
                    self.audio_chunks_seen += 1
                    self.audio_bytes_seen += len(chunk)
                    self.last_audio_at = time.monotonic()
                    if self.last_error and (
                        "Audio extractor" in self.last_error
                        or "Unable to open URL" in self.last_error
                        or "audio_status" in self.last_error
                    ):
                        self.last_error = None
                    for audio_segment in vad.push(chunk):
                        await self._enqueue_audio_segment(audio_base_monotonic, audio_segment)
                for audio_segment in vad.flush():
                    await self._enqueue_audio_segment(audio_base_monotonic, audio_segment)

                return_code = process.poll()
                if return_code not in (0, None):
                    stderr = ""
                    if process.stderr:
                        stderr = process.stderr.read().decode("utf-8", errors="ignore")
                    raise RuntimeError(stderr.strip() or f"Audio extractor exited with {return_code}")
                if bytes_seen == 0:
                    raise RuntimeError("Audio extractor ended without producing PCM bytes")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status = self.audio_extractor.status()
                await self._record_error(
                    RuntimeError(f"{exc}; audio_status={status}; chunks={chunks_seen}; bytes={bytes_seen}")
                )
                await asyncio.sleep(3)
            finally:
                self.audio_extractor.stop()

    async def _enqueue_audio_segment(self, audio_base_monotonic: float, audio_segment: Any) -> None:
        item = QueuedAudioSegment(
            pcm=audio_segment.pcm,
            start_monotonic=audio_base_monotonic + audio_segment.start_seconds,
            end_monotonic=audio_base_monotonic + audio_segment.end_seconds,
        )
        self.vad_segments_seen += 1
        await self._put_drop_oldest(self.audio_queue, item, "audio")

    async def _asr_worker(self) -> None:
        while True:
            item = await self.audio_queue.get()
            self.asr_segments_processing += 1
            try:
                transcript = await self.asr.transcribe_bytes(item.pcm, item.start_monotonic, item.end_monotonic)
                if transcript.source_text.strip():
                    await self._put_drop_oldest(self.translation_queue, transcript, "translation")
            except Exception as exc:
                await self._record_error(exc)
            finally:
                self.asr_segments_processing = max(0, self.asr_segments_processing - 1)
                self.audio_queue.task_done()

    async def _translation_worker(self) -> None:
        while True:
            transcript = await self.translation_queue.get()
            self.translation_segments_processing += 1
            try:
                payload = await self._build_subtitle_payload(transcript)
                await self._put_drop_oldest(self.subtitle_queue, payload, "subtitle")
            except Exception as exc:
                await self._record_error(exc)
            finally:
                self.translation_segments_processing = max(0, self.translation_segments_processing - 1)
                self.translation_queue.task_done()

    async def _subtitle_output_worker(self) -> None:
        while True:
            payload = await self.subtitle_queue.get()
            try:
                await self.websocket_manager.broadcast(payload)
            finally:
                self.subtitle_queue.task_done()

    async def _hls_trim_loop(self) -> None:
        while True:
            try:
                self.hls_buffer.trim_segments()
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._record_error(exc)
                await asyncio.sleep(10)

    async def _build_subtitle_payload(self, transcript: TranscriptSegment) -> dict:
        normalization = self.terminology.normalize_transcript(transcript.source_text)
        candidate_terms = self.terminology.retrieve_candidate_terms(
            normalization.text,
            self.config.glossary.max_candidate_terms,
        )
        translation_error: str | None = None
        translation_error_type: str | None = None
        try:
            translation = await self.translator.translate(
                source_text=transcript.source_text,
                normalized_text=normalization.text,
                source_language=transcript.language,
                candidate_terms=candidate_terms,
            )
        except Exception as exc:
            translation_error = str(exc)
            translation_error_type = classify_runtime_error(exc)
            await self._record_error(exc)
            translation = TranslationResult(
                zh=normalization.text or transcript.source_text,
                terms_used=[],
                uncertain_terms=["translation_failed"],
                latency=0.0,
            )
        verification = self.verifier.verify(transcript.source_text, normalization.text, translation.zh, candidate_terms)
        event = self.scheduler.schedule(transcript, normalization.text, translation, verification)
        payload = event.model_dump(mode="json")
        payload["transcript_id"] = transcript.id
        if translation_error:
            payload["error"] = translation_error
            payload["error_type"] = translation_error_type

        self.asr_segments_seen += 1
        self.last_source_text = transcript.source_text
        self.last_normalized_text = normalization.text
        self.last_subtitle_text = event.text
        self.last_asr_latency = transcript.asr_latency
        self.last_translation_latency = translation.latency
        self.last_terms_hit = len(candidate_terms)
        self.asr_latencies.append(transcript.asr_latency)
        self.translation_latencies.append(translation.latency)
        self._append_history(payload, candidate_terms, translation_error_type, translation_error)
        return payload

    async def _put_drop_oldest(self, queue: asyncio.Queue, item: Any, queue_name: str) -> None:
        if queue.full():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                pass
            self.queue_dropped[queue_name] += 1
            await self.websocket_manager.broadcast(
                {
                    "type": "error",
                    "message": f"{queue_name} queue overloaded; dropped oldest item",
                    "queue": queue_name,
                }
            )
        await queue.put(item)

    def _ensure_asr_workers(self) -> None:
        live = [task for task in self._asr_worker_tasks if not task.done()]
        needed = max(1, self.config.queues.asr_concurrency) - len(live)
        self._asr_worker_tasks = live
        for _ in range(max(0, needed)):
            self._asr_worker_tasks.append(asyncio.create_task(self._asr_worker()))

    def _ensure_output_workers(self) -> None:
        live = [task for task in self._translation_worker_tasks if not task.done()]
        needed = max(1, self.config.queues.translation_concurrency) - len(live)
        self._translation_worker_tasks = live
        for _ in range(max(0, needed)):
            self._translation_worker_tasks.append(asyncio.create_task(self._translation_worker()))
        if self._subtitle_worker_task is None or self._subtitle_worker_task.done():
            self._subtitle_worker_task = asyncio.create_task(self._subtitle_output_worker())

    def _ensure_hls_trim_task(self) -> None:
        if self._hls_trim_task is None or self._hls_trim_task.done():
            self._hls_trim_task = asyncio.create_task(self._hls_trim_loop())

    def _append_history(
        self,
        payload: dict,
        candidate_terms: list,
        error_type: str | None,
        error_message: str | None,
    ) -> None:
        item = {
            "id": payload["id"],
            "transcript_id": payload.get("transcript_id"),
            "source_text": payload["source_text"],
            "normalized_text": payload["normalized_text"],
            "text": payload["text"],
            "terms_used": payload.get("terms_used", []),
            "candidate_terms": [
                {"id": term.id, "source": term.source, "zh_cn": term.zh_cn, "category": term.category}
                for term in candidate_terms
            ],
            "terms_hit": len(candidate_terms),
            "asr_latency": payload.get("asr_latency", 0.0),
            "translation_latency": payload.get("translation_latency", 0.0),
            "error_type": error_type,
            "error": error_message,
            "created_at": time.time(),
        }
        self.history.appendleft(item)
        if error_message:
            self._write_jsonl(self.config.runtime.subtitle_failures_log, item)

    def history_items(self) -> list[dict]:
        return list(self.history)

    def clear_history(self) -> None:
        self.history.clear()

    def clear_hls(self) -> dict:
        self.hls_buffer.clear_runtime_files()
        return self.hls_buffer.status()

    async def deepseek_test(self) -> dict:
        if not refresh_env_var_from_windows("DEEPSEEK_API_KEY"):
            return {"ok": False, "error_type": "api_key_missing", "error": "DEEPSEEK_API_KEY is not set"}
        translator = DeepSeekV4Translator(self.config)
        started = time.perf_counter()
        try:
            result = await translator.translate(
                source_text="Start the mission.",
                normalized_text="Start the mission.",
                source_language="en",
                candidate_terms=[],
            )
            return {
                "ok": True,
                "model": self.config.translation.deepseek_v4.model,
                "latency": round(time.perf_counter() - started, 3),
                "text": result.zh,
            }
        except Exception as exc:
            return {
                "ok": False,
                "model": self.config.translation.deepseek_v4.model,
                "latency": round(time.perf_counter() - started, 3),
                "error_type": classify_runtime_error(exc),
                "error": str(exc)[-1000:],
            }

    def stop(self) -> None:
        self.hls_buffer.stop()
        self.audio_extractor.stop()
        tasks = [
            self._mock_task,
            self._audio_task,
            self._hls_trim_task,
            self._subtitle_worker_task,
            *self._asr_worker_tasks,
            *self._translation_worker_tasks,
        ]
        for task in tasks:
            if task and not task.done():
                task.cancel()
        self._mock_task = None
        self._audio_task = None
        self._hls_trim_task = None
        self._subtitle_worker_task = None
        self._asr_worker_tasks = []
        self._translation_worker_tasks = []
        self._clear_queue(self.audio_queue)
        self._clear_queue(self.translation_queue)
        self._clear_queue(self.subtitle_queue)
        self.asr_segments_processing = 0
        self.translation_segments_processing = 0

    def status(self) -> dict:
        return {
            "uptime_seconds": round(time.monotonic() - self.started_at, 2),
            "source_url": self.source_url,
            "last_error": self.last_error,
            "hls": self.hls_buffer.status(),
            "queues": {
                "audio": self.audio_queue.qsize(),
                "asr": self.audio_queue.qsize(),
                "translation": self.translation_queue.qsize(),
                "subtitle": self.subtitle_queue.qsize(),
                "dropped": dict(self.queue_dropped),
                "asr_avg_latency": self._avg(self.asr_latencies),
                "translation_avg_latency": self._avg(self.translation_latencies),
            },
            "asr": {
                "provider": self.config.asr.provider,
                "running": self._is_task_running(self._audio_task) or self._is_task_running(self._mock_task),
                "audio": self.audio_extractor.status(),
                "segments_seen": self.asr_segments_seen,
                "segments_processing": self.asr_segments_processing,
                "translation_processing": self.translation_segments_processing,
                "last_source_text": self.last_source_text,
                "last_normalized_text": self.last_normalized_text,
                "last_subtitle_text": self.last_subtitle_text,
                "last_asr_latency": self.last_asr_latency,
                "last_translation_latency": self.last_translation_latency,
                "last_terms_hit": self.last_terms_hit,
                "audio_chunks_seen": self.audio_chunks_seen,
                "audio_bytes_seen": self.audio_bytes_seen,
                "vad_segments_seen": self.vad_segments_seen,
                "seconds_since_last_audio": (
                    None if self.last_audio_at is None else round(time.monotonic() - self.last_audio_at, 2)
                ),
            },
            "websocket_connections": self.websocket_manager.connection_count,
            "terms": len(self.terminology.all_terms()),
            "history_size": len(self.history),
        }

    async def _record_error(self, exc: Exception) -> None:
        self.last_error = str(exc)
        await self.websocket_manager.broadcast(
            {"type": "error", "message": self.last_error, "error_type": classify_runtime_error(exc)}
        )

    @staticmethod
    def _write_jsonl(path: str, item: dict) -> None:
        log_path = Path(path)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")

    @staticmethod
    def _clear_queue(queue: asyncio.Queue) -> None:
        while True:
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break

    def _running(self) -> bool:
        tasks = [self._mock_task, self._audio_task, *self._asr_worker_tasks, *self._translation_worker_tasks]
        return any(self._is_task_running(task) for task in tasks)

    @staticmethod
    def _is_task_running(task: asyncio.Task | None) -> bool:
        return task is not None and not task.done()

    @staticmethod
    def _avg(values: deque[float]) -> float | None:
        if not values:
            return None
        return round(sum(values) / len(values), 3)


def classify_runtime_error(exc: Exception) -> str:
    message = str(exc).lower()
    if "deepseek_api_key" in message or "api_key" in message:
        return "api_key_missing"
    if isinstance(exc, httpx.TimeoutException) or "timed out" in message or "timeout" in message:
        return "timeout"
    if isinstance(exc, httpx.HTTPStatusError) or "client error" in message or "server error" in message:
        return "http_error"
    if isinstance(exc, (httpx.ConnectError, httpx.ProxyError, httpx.NetworkError)) or "proxy" in message:
        return "network_or_proxy_failed"
    if "json" in message:
        return "non_json_response"
    return "runtime_error"
