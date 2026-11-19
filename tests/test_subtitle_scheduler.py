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


def test_scheduler_caps_display_duration():
    scheduler = SubtitleScheduler(
        VideoConfig(delay_seconds=10),
        SubtitleConfig(min_display_seconds=1.5, max_display_seconds=3, hold_seconds_after_speech=10),
    )
    event = scheduler.schedule(
        TranscriptSegment(id="seg-1", source_text="hello", start_monotonic=0, end_monotonic=1),
        "hello",
        TranslationResult(zh="你好"),
        VerificationResult(text="你好", actions=[]),
    )

    assert event.show_at == 10
    assert event.hide_at == 13


def test_scheduler_aligns_cue_to_media_clock_without_adding_playback_delay_twice():
    scheduler = SubtitleScheduler(
        VideoConfig(delay_seconds=15),
        SubtitleConfig(extra_offset_seconds=0.25, hold_seconds_after_speech=1),
    )
    event = scheduler.schedule(
        TranscriptSegment(
            id="seg-1", source_text="hello", start_monotonic=10, end_monotonic=12,
            stream_id="current-stream", media_start=1000, media_end=1002,
        ),
        "hello",
        TranslationResult(zh="你好"),
        VerificationResult(text="你好", actions=[]),
    )
    assert event.stream_id == "current-stream"
    assert event.show_at == 25.25
    assert event.media_show_at == 1000.25
    assert event.media_hide_at == 1003
