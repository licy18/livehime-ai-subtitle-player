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
from app.services.hls_media import PCM_SAMPLE_BYTES, SAMPLE_RATE, decode_fragment_audio
from app.services.srt_archive import RecordingSubtitles
from app.services.streaming import HLSBuffer, StreamResolver
from app.services.subtitle_scheduler import SubtitleScheduler
from app.services.terminology_manager import TerminologyManager
from app.services.translation_verifier import TranslationVerifier
from app.services.translator import DeepSeekV4Translator, Translator
from app.services.vad import AudioSegment, VADSegmenter
from app.services.websocket_manager import WebSocketManager


@dataclass
class QueuedAudioSegment:
    pcm: bytes
    start_epoch: float
    end_epoch: float
    stream_id: str
    start_pts: float | None


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
        self.asr = ASRWorker(config, terminology)
        self.scheduler = SubtitleScheduler(config.video, config.subtitle)
        self.verifier = TranslationVerifier()
        self.started_at = time.monotonic()
        self.source_url: str | None = None
        self.last_error: str | None = None
        self.recording_subtitles: RecordingSubtitles | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._finishing = False

        self.audio_queue: asyncio.Queue[QueuedAudioSegment] = asyncio.Queue(maxsize=config.queues.audio_queue_max)
        self.translation_queue: asyncio.Queue[TranscriptSegment] = asyncio.Queue(
            maxsize=config.queues.translation_queue_max
        )
        self.subtitle_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=config.queues.translation_queue_max)
        self.queue_dropped = {"audio": 0, "translation": 0, "subtitle": 0}
        self.history: deque[dict] = deque(maxlen=config.queues.subtitle_history_limit)
        self._recent_transcripts: deque[TranscriptSegment] = deque(maxlen=3)
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
        async with self._lifecycle_lock:
            return await self._start_hls()

    async def _start_hls(self) -> dict:
        try:
            if self.hls_buffer.is_running():
                await self.websocket_manager.broadcast({"type": "status", "message": "HLS buffer already running"})
                return self.hls_buffer.status()
            if self.hls_buffer.stream_id is not None:
                await self._finish()
            self.last_error = None
            if self.config.input.resolver == "streamlink":
                self.source_url = self.config.input.url
                self.hls_buffer.start_streamlink_pipe(self.config.input.url)
            else:
                self.source_url = await self.stream_resolver.resolve()
                self.hls_buffer.start(self.source_url)
            stream_id = self.hls_buffer.stream_id
            recording = self.hls_buffer.recording_path
            self.recording_subtitles = RecordingSubtitles(stream_id, recording) if stream_id and recording else None
            self._ensure_hls_trim_task()
            if self.recording_subtitles and self.config.asr.enabled and self.config.asr.provider != "mock":
                self._start_audio_analysis(self.recording_subtitles.stream_id)
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
        async with self._lifecycle_lock:
            return await self._start_asr()

    async def _start_asr(self) -> dict:
        if self.config.asr.provider == "mock":
            self.start_mock_loop()
            await self.websocket_manager.broadcast({"type": "status", "message": "Mock ASR loop started"})
            return self.status()
        if self._audio_task and (
            not self._audio_task.done() or self._archive_for(self.hls_buffer.stream_id)
        ):
            return self.status()

        try:
            if not self.hls_buffer.is_running() and not self.hls_buffer.playlist().fragments:
                await self._start_hls()
            stream_id = self.hls_buffer.stream_id
            if stream_id is None:
                raise RuntimeError("HLS input is not available")
            self._start_audio_analysis(stream_id)
            await self.websocket_manager.broadcast({"type": "status", "message": "ASR loop started"})
            return self.status()
        except Exception as exc:
            self.last_error = str(exc)
            await self.websocket_manager.broadcast({"type": "error", "message": self.last_error})
            raise

    def _start_audio_analysis(self, stream_id: str) -> None:
        if self._is_task_running(self._audio_task):
            return
        self._ensure_output_workers()
        self._ensure_asr_workers()
        self._audio_task = asyncio.create_task(self._audio_loop(stream_id))

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

    async def _audio_loop(self, stream_id: str) -> None:
        vad = VADSegmenter()
        archive = self._archive_for(stream_id)
        next_sequence = 0 if archive else None
        audio_base = None
        audio_end = None
        audio_base_pts = None
        while self.hls_buffer.stream_id == stream_id:
            playlist = self.hls_buffer.playlist()
            fragments = playlist.fragments
            if next_sequence is None and fragments:
                edge = fragments[-1].start_epoch + fragments[-1].duration
                cutoff = edge - self.config.video.delay_seconds
                if playlist.ended:
                    cutoff = fragments[0].start_epoch
                next_sequence = next(
                    (fragment.sequence for fragment in fragments if fragment.start_epoch + fragment.duration > cutoff),
                    fragments[-1].sequence,
                )
            for fragment in fragments:
                if fragment.sequence < next_sequence:
                    continue
                if archive:
                    archive.missing_fragments += fragment.sequence - next_sequence
                next_sequence = fragment.sequence + 1
                try:
                    decoded = await asyncio.to_thread(decode_fragment_audio, self.hls_buffer.directory, fragment)
                except FileNotFoundError:
                    if archive:
                        archive.missing_fragments += 1
                    continue
                except Exception as exc:
                    if archive:
                        archive.missing_fragments += 1
                    await self._record_error(exc)
                    continue
                if archive and fragment.sequence == 0:
                    archive.first_hls_video_pts = decoded.video_pts
                audio_start, pcm = decoded.start_epoch, decoded.pcm
                if not pcm:
                    continue
                if self.hls_buffer.stream_id != stream_id:
                    return
                self.audio_chunks_seen += 1
                self.audio_bytes_seen += len(pcm)
                self.last_audio_at = time.monotonic()
                gap = 0 if audio_end is None else round((audio_start - audio_end) * SAMPLE_RATE)
                if archive and audio_base_pts is not None and decoded.start_pts is not None:
                    consumed = audio_end - audio_base
                    gap = round((decoded.start_pts - audio_base_pts - consumed) * SAMPLE_RATE)
                if audio_base is None or abs(gap) > SAMPLE_RATE // 4:
                    if audio_base is not None:
                        await self._enqueue_audio(audio_base, audio_base_pts, stream_id, vad.flush())
                        vad = VADSegmenter()
                    audio_base = audio_end = audio_start
                    audio_base_pts = decoded.start_pts
                    gap = 0
                if gap > 0:
                    pcm = bytes(gap * PCM_SAMPLE_BYTES) + pcm
                elif gap < 0:
                    pcm = pcm[-gap * PCM_SAMPLE_BYTES:]
                audio_end += len(pcm) / (SAMPLE_RATE * PCM_SAMPLE_BYTES)
                await self._enqueue_audio(audio_base, audio_base_pts, stream_id, vad.push(pcm))

            if playlist.ended or not self.hls_buffer.is_running():
                if not playlist.ended and self.hls_buffer.playlist() != playlist:
                    continue
                if audio_base is not None:
                    await self._enqueue_audio(audio_base, audio_base_pts, stream_id, vad.flush())
                if archive:
                    archive.flush()
                return
            await asyncio.sleep(0.15)

    async def _enqueue_audio(
        self, audio_base: float, audio_base_pts: float | None, stream_id: str, segments: list[AudioSegment],
    ) -> None:
        for segment in segments:
            item = QueuedAudioSegment(
                pcm=segment.pcm,
                start_epoch=audio_base + segment.start_seconds,
                end_epoch=audio_base + segment.end_seconds,
                stream_id=stream_id,
                start_pts=audio_base_pts + segment.start_seconds if audio_base_pts is not None else None,
            )
            self.vad_segments_seen += 1
            if self._archive_for(stream_id):
                await self.audio_queue.put(item)
            else:
                await self._put_drop_oldest(self.audio_queue, item, "audio")

    async def _asr_worker(self) -> None:
        while True:
            item = await self.audio_queue.get()
            self.asr_segments_processing += 1
            try:
                epoch_offset = time.time() - time.monotonic()
                input_start = item.start_epoch - epoch_offset
                transcript = await self.asr.transcribe_bytes(
                    item.pcm, input_start, item.end_epoch - epoch_offset,
                )
                if transcript.source_text.strip():
                    transcript.stream_id = item.stream_id
                    transcript.media_start = transcript.start_monotonic + epoch_offset
                    transcript.media_end = transcript.end_monotonic + epoch_offset
                    archive = self._archive_for(item.stream_id)
                    if archive:
                        if item.start_pts is not None:
                            archive.add_source(
                                transcript.id,
                                item.start_pts + (transcript.start_monotonic - input_start),
                                item.start_pts + (transcript.end_monotonic - input_start),
                                transcript.source_text,
                            )
                        await self.translation_queue.put(transcript)
                    else:
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
        recent_context = []
        if transcript.stream_id and transcript.source_text.strip():
            preceding = sorted(
                (
                    segment for segment in self._recent_transcripts
                    if segment.stream_id == transcript.stream_id
                    and 0 < transcript.start_monotonic - segment.start_monotonic <= 20
                ),
                key=lambda segment: segment.start_monotonic,
            )
            recent_context = [segment.source_text.strip()[:160] for segment in preceding]
            # Store at dispatch time so concurrent translations need not wait for previous results.
            self._recent_transcripts.append(transcript)
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
                recent_context=recent_context,
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
        archive = self._archive_for(transcript.stream_id)
        if archive:
            text = "【待翻译】" if translation_error is not None else verification.text
            archive.add_translation(transcript.id, text)
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
            "stream_id": payload.get("stream_id"),
            "media_show_at": payload.get("media_show_at"),
            "media_hide_at": payload.get("media_hide_at"),
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

    def _archive_for(self, stream_id: str | None) -> RecordingSubtitles | None:
        archive = self.recording_subtitles
        return archive if archive and archive.stream_id == stream_id else None

    async def finish(self, *, clear_hls: bool = False) -> None:
        async with self._lifecycle_lock:
            await self._finish()
            if clear_hls:
                self.hls_buffer.clear_runtime_files()

    async def _finish(self) -> None:
        archive = self._archive_for(self.hls_buffer.stream_id)
        if not archive:
            self.stop()
            return
        self._finishing = True
        try:
            self.hls_buffer.finish_input()
            if self._audio_task is not None:
                await self._audio_task
            await self.audio_queue.join()
            await self.translation_queue.join()
            archive.flush()
        finally:
            self.stop()
            self._finishing = False

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
        self._recent_transcripts.clear()
        self.asr_segments_processing = 0
        self.translation_segments_processing = 0

    def status(self) -> dict:
        return {
            "uptime_seconds": round(time.monotonic() - self.started_at, 2),
            "source_url": self.source_url,
            "last_error": self.last_error,
            "hls": self.hls_buffer.status(),
            "recording_subtitles": {
                **self.recording_subtitles.status(), "finishing": self._finishing,
            } if self.recording_subtitles else None,
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
                "audio": {"source": "hls", "process_running": self._is_task_running(self._audio_task)},
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
