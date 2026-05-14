from app.core.config import SubtitleConfig, VideoConfig
from app.models.subtitle import TranscriptSegment, TranslationResult
from app.models.term import VerificationResult
from app.services.subtitle_scheduler import SubtitleScheduler


def test_scheduler_calculates_show_and_hide_times():
    scheduler = SubtitleScheduler(
        VideoConfig(delay_seconds=15),
        SubtitleConfig(extra_offset_seconds=0.5, hold_seconds_after_speech=1.0),
    )
    event = scheduler.schedule(
        TranscriptSegment(id="seg-1", source_text="hello", start_monotonic=10, end_monotonic=12),
        "hello",
        TranslationResult(zh="你好"),
        VerificationResult(text="你好", actions=[]),
    )

    assert event.show_at == 25.5
    assert event.hide_at == 28
    assert event.text == "你好"


def test_scheduler_splits_long_subtitles():
    scheduler = SubtitleScheduler(VideoConfig(), SubtitleConfig(max_chars_per_line=4, max_lines=2))

    assert scheduler.split_lines("hello world again") == "hello\nworld"

