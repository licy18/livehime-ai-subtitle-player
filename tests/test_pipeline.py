import pytest

from app.core.config import AppConfig, RuntimeConfig
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

    assert event["text"] == "hello world"
    assert event["uncertain_terms"] == ["translation_failed"]
    assert pipeline.status()["last_error"] == "translator unavailable"


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

