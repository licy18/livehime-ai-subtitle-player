from __future__ import annotations

import textwrap
from uuid import uuid4

from app.core.config import SubtitleConfig, VideoConfig
from app.models.subtitle import SubtitleEvent, TranscriptSegment, TranslationResult
from app.models.term import VerificationResult


class SubtitleScheduler:
    def __init__(self, video: VideoConfig, subtitle: SubtitleConfig) -> None:
        self.video = video
        self.subtitle = subtitle

    def schedule(
        self,
        transcript: TranscriptSegment,
        normalized_text: str,
        translation: TranslationResult,
        verification: VerificationResult,
    ) -> SubtitleEvent:
        show_at = transcript.start_monotonic + self.video.delay_seconds + self.subtitle.extra_offset_seconds
        hide_at = self._hide_at(show_at, transcript.end_monotonic + self.video.delay_seconds)
        media_show_at = media_hide_at = None
        if transcript.media_start is not None and transcript.media_end is not None:
            media_show_at = transcript.media_start + self.subtitle.extra_offset_seconds
            media_hide_at = self._hide_at(media_show_at, transcript.media_end)
        return SubtitleEvent(
            id=str(uuid4()),
            show_at=show_at,
            hide_at=hide_at,
            stream_id=transcript.stream_id,
            media_show_at=media_show_at,
            media_hide_at=media_hide_at,
            source_text=transcript.source_text,
            normalized_text=normalized_text,
            text=self.split_lines(verification.text),
            terms_used=translation.terms_used,
            uncertain_terms=translation.uncertain_terms,
            asr_latency=transcript.asr_latency,
            translation_latency=translation.latency,
            verification_actions=verification.actions,
        )

    def _hide_at(self, show_at: float, speech_end: float) -> float:
        return min(
            max(speech_end + self.subtitle.hold_seconds_after_speech, show_at + self.subtitle.min_display_seconds),
            show_at + self.subtitle.max_display_seconds,
        )

    def split_lines(self, text: str) -> str:
        clean = " ".join(text.split())
        if len(clean) <= self.subtitle.max_chars_per_line:
            return clean
        lines = textwrap.wrap(clean, width=self.subtitle.max_chars_per_line, break_long_words=False)
        return "\n".join(lines[: self.subtitle.max_lines])
