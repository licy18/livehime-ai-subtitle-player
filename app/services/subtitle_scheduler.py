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
        hide_at = max(
            transcript.end_monotonic + self.video.delay_seconds + self.subtitle.hold_seconds_after_speech,
            show_at + self.subtitle.min_display_seconds,
        )
        hide_at = min(hide_at, show_at + self.subtitle.max_display_seconds)
        return SubtitleEvent(
            id=str(uuid4()),
            show_at=show_at,
            hide_at=hide_at,
            source_text=transcript.source_text,
            normalized_text=normalized_text,
            text=self.split_lines(verification.text),
            terms_used=translation.terms_used,
            uncertain_terms=translation.uncertain_terms,
            asr_latency=transcript.asr_latency,
            translation_latency=translation.latency,
            verification_actions=verification.actions,
        )

    def split_lines(self, text: str) -> str:
        clean = " ".join(text.split())
        if len(clean) <= self.subtitle.max_chars_per_line:
            return clean
        lines = textwrap.wrap(clean, width=self.subtitle.max_chars_per_line, break_long_words=False)
        return "\n".join(lines[: self.subtitle.max_lines])
