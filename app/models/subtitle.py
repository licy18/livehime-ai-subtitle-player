from __future__ import annotations

from pydantic import BaseModel, Field


class TranscriptSegment(BaseModel):
    id: str
    source_text: str
    start_monotonic: float
    end_monotonic: float
    language: str | None = None
    asr_latency: float = 0.0
    stream_id: str | None = None
    media_start: float | None = None
    media_end: float | None = None


class TranslationResult(BaseModel):
    zh: str
    terms_used: list[str] = Field(default_factory=list)
    uncertain_terms: list[str] = Field(default_factory=list)
    latency: float = 0.0
    raw: dict | None = None


class SubtitleEvent(BaseModel):
    type: str = "subtitle"
    id: str
    show_at: float
    hide_at: float
    stream_id: str | None = None
    media_show_at: float | None = None
    media_hide_at: float | None = None
    source_text: str
    normalized_text: str
    text: str
    terms_used: list[str] = Field(default_factory=list)
    uncertain_terms: list[str] = Field(default_factory=list)
    asr_latency: float = 0.0
    translation_latency: float = 0.0
    verification_actions: list[str] = Field(default_factory=list)

