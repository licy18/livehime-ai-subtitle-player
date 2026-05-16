import pytest

from app.core.config import AppConfig, QueueConfig, RuntimeConfig
from app.models.subtitle import TranscriptSegment
from app.services.pipeline import RuntimePipeline
from app.services.terminology_manager import TerminologyManager
from app.services.translator import Translator
from app.services.websocket_manager import WebSocketManager


class FailingTranslator(Translator):
    async def translate(self, source_text, normalized_text, source_language, candidate_terms, recent_context=None):
        raise RuntimeError("translator unavailable")


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
