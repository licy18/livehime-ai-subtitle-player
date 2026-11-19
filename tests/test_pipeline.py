import asyncio
from unittest.mock import AsyncMock

import pytest

from app.core.config import AppConfig, QueueConfig, RuntimeConfig
from app.models.subtitle import TranscriptSegment, TranslationResult
from app.services.pipeline import RuntimePipeline
from app.services.srt_archive import RecordingSubtitles
from app.services.vad import AudioSegment
from app.services.terminology_manager import TerminologyManager
from app.services.translator import Translator
from app.services.websocket_manager import WebSocketManager


class FailingTranslator(Translator):
    async def translate(self, source_text, normalized_text, source_language, candidate_terms, recent_context=None):
        raise RuntimeError("translator unavailable")


@pytest.mark.asyncio
async def test_recent_translation_context_uses_preceding_asr_without_waiting(tmp_path):
    config = AppConfig(runtime=RuntimeConfig(live_terms_db=str(tmp_path / "terms.sqlite3")))
    translator = AsyncMock(spec=Translator)
    started = asyncio.Event()
    release_first = asyncio.Event()

    async def translate(**request):
        if request["source_text"] == "line-1":
            started.set()
            await release_first.wait()
        return TranslationResult(zh="译文")

    translator.translate.side_effect = translate
    pipeline = RuntimePipeline(config, TerminologyManager(config), translator, WebSocketManager())

    async def emit(number, time, stream="one"):
        return await pipeline.emit_transcript(TranscriptSegment(
            id=str(number), source_text=f"line-{number}", start_monotonic=time,
            end_monotonic=time + 1, stream_id=stream,
        ))

    first = asyncio.create_task(emit(1, 1))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await asyncio.wait_for(emit(2, 3), timeout=1)
        assert translator.translate.call_args.kwargs["recent_context"] == ["line-1"]
    finally:
        release_first.set()
        await first

    await emit(3, 4)
    await emit(4, 6)
    await emit(5, 8)
    assert translator.translate.call_args.kwargs["recent_context"] == ["line-2", "line-3", "line-4"]
    await emit(6, 9, stream="two")
    assert translator.translate.call_args.kwargs["recent_context"] == []
    await emit(7, 32, stream="two")
    assert translator.translate.call_args.kwargs["recent_context"] == []

    pipeline.stop()
    await emit(8, 33, stream="two")
    assert translator.translate.call_args.kwargs["recent_context"] == []


@pytest.mark.asyncio
async def test_pipeline_falls_back_when_translation_fails(tmp_path):
    config = AppConfig(
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        )
    )
    pipeline = RuntimePipeline(config, TerminologyManager(config), FailingTranslator(), WebSocketManager())

    event = await pipeline.emit_transcript(
        TranscriptSegment(id="seg", source_text="hello world", start_monotonic=1, end_monotonic=2)
    )

    assert event["text"] == "（翻译未返回中文）"
    assert event["uncertain_terms"] == ["translation_failed"]
    assert pipeline.status()["last_error"] == "translator unavailable"
    assert pipeline.history_items()[0]["error_type"] == "runtime_error"


@pytest.mark.asyncio
async def test_start_asr_mock_loop_is_idempotent(tmp_path):
    config = AppConfig(
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        )
    )
    pipeline = RuntimePipeline(config, TerminologyManager(config), FailingTranslator(), WebSocketManager())

    await pipeline.start_asr()
    first_task = pipeline._mock_task
    await pipeline.start_asr()

    assert first_task is pipeline._mock_task
    assert pipeline.status()["asr"]["running"] is True
    pipeline.stop()


@pytest.mark.asyncio
async def test_queue_overload_drops_oldest(tmp_path):
    config = AppConfig(
        queues=QueueConfig(translation_queue_max=1),
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )
    pipeline = RuntimePipeline(config, TerminologyManager(config), FailingTranslator(), WebSocketManager())

    await pipeline._put_drop_oldest(
        pipeline.translation_queue,
        TranscriptSegment(id="old", source_text="old", start_monotonic=1, end_monotonic=2),
        "translation",
    )
    await pipeline._put_drop_oldest(
        pipeline.translation_queue,
        TranscriptSegment(id="new", source_text="new", start_monotonic=2, end_monotonic=3),
        "translation",
    )

    assert pipeline.queue_dropped["translation"] == 1
    assert pipeline.translation_queue.get_nowait().id == "new"


@pytest.mark.asyncio
async def test_subtitle_history_limit(tmp_path):
    config = AppConfig(
        queues=QueueConfig(subtitle_history_limit=2),
        runtime=RuntimeConfig(
            runtime_dir=str(tmp_path / "runtime"),
            hls_dir=str(tmp_path / "runtime" / "hls"),
            live_terms_db=str(tmp_path / "runtime" / "live_terms.sqlite3"),
            unknown_terms_log=str(tmp_path / "runtime" / "unknown_terms.log"),
        ),
    )
    pipeline = RuntimePipeline(config, TerminologyManager(config), FailingTranslator(), WebSocketManager())

    for index in range(3):
        await pipeline.emit_transcript(
            TranscriptSegment(id=f"seg-{index}", source_text=f"text {index}", start_monotonic=1, end_monotonic=2)
        )

    assert [item["transcript_id"] for item in pipeline.history_items()] == ["seg-2", "seg-1"]


@pytest.mark.asyncio
async def test_stopping_recording_waits_for_final_translated_cue(tmp_path, monkeypatch):
    config = AppConfig(runtime=RuntimeConfig(live_terms_db=str(tmp_path / "terms.sqlite3")))
    config.subtitle.max_chars_per_line = 4
    config.subtitle.max_lines = 1
    config.subtitle.extra_offset_seconds = 2.5
    translator = AsyncMock(spec=Translator)
    translation_started = asyncio.Event()
    translation_complete = asyncio.Event()
    input_ended = asyncio.Event()

    async def translate(**_):
        translation_started.set()
        await translation_complete.wait()
        return TranslationResult(zh="这一句 全部保留")

    async def transcribe(pcm, start, end):
        return TranscriptSegment(
            id="tail", source_text="最後の台詞", start_monotonic=start + 0.1,
            end_monotonic=end - 0.2, language="ja",
        )

    translator.translate.side_effect = translate
    pipeline = RuntimePipeline(config, TerminologyManager(config), translator, WebSocketManager())
    pipeline.asr.transcribe_bytes = transcribe
    pipeline.hls_buffer.stream_id = "stream"
    pipeline.recording_subtitles = RecordingSubtitles("stream", tmp_path / "program.ts")
    pipeline.recording_subtitles.first_hls_video_pts = 11.0
    monkeypatch.setattr(pipeline.hls_buffer, "finish_input", input_ended.set)
    monkeypatch.setattr("app.services.srt_archive.recording_video_offset", lambda _: 0.05)

    async def final_audio():
        await input_ended.wait()
        await pipeline._enqueue_audio(1_000.0, 15.0, "stream", [AudioSegment(b"audio", 0.0, 1.0)])

    pipeline._ensure_output_workers()
    pipeline._ensure_asr_workers()
    pipeline._audio_task = asyncio.create_task(final_audio())
    stop = asyncio.create_task(pipeline.finish())
    try:
        await asyncio.wait_for(translation_started.wait(), timeout=1)
        assert not stop.done()
        assert "最後の台詞" in (tmp_path / "program.ja.srt").read_text(encoding="utf-8")
    finally:
        translation_complete.set()
        await asyncio.wait_for(stop, timeout=1)

    zh = (tmp_path / "program.zh-CN.srt").read_text(encoding="utf-8")
    assert "00:00:04,150 --> 00:00:04,850" in zh
    assert "这一句 全部保留" in zh
