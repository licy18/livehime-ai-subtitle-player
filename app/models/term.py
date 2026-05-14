from __future__ import annotations

from pydantic import BaseModel, Field, field_validator


class Term(BaseModel):
    id: str
    category: str
    source: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    zh_cn: str
    notes: str = ""
    priority: int = 0
    forbidden_translations: list[str] = Field(default_factory=list)
    asr_mishearings: list[str] = Field(default_factory=list)

    @field_validator("source", "aliases", "forbidden_translations", "asr_mishearings", mode="before")
    @classmethod
    def normalize_list(cls, value: object) -> list[str]:
        if value is None:
            return []
        if isinstance(value, str):
            return [value]
        return [str(item) for item in value]

    @field_validator("zh_cn")
    @classmethod
    def zh_cn_not_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("zh_cn must not be empty")
        return value

    @property
    def match_phrases(self) -> list[str]:
        phrases = [*self.source, *self.aliases, *self.asr_mishearings]
        seen: set[str] = set()
        ordered: list[str] = []
        for phrase in phrases:
            key = phrase.strip().lower()
            if key and key not in seen:
                seen.add(key)
                ordered.append(phrase.strip())
        return ordered


class NormalizationResult(BaseModel):
    text: str
    replacements: list[dict] = Field(default_factory=list)
    terms: list[Term] = Field(default_factory=list)


class VerificationResult(BaseModel):
    text: str
    actions: list[str] = Field(default_factory=list)
    missing_terms: list[Term] = Field(default_factory=list)

