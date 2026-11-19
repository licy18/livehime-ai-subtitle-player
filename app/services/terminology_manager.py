from __future__ import annotations

import glob
import hashlib
import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Iterable

import yaml

from app.core.config import AppConfig
from app.models.term import NormalizationResult, Term

try:
    from rapidfuzz import fuzz
except Exception:  # pragma: no cover - exercised when optional dependency is absent
    fuzz = None


class TerminologyManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.terms: dict[str, Term] = {}
        self.live_terms: dict[str, Term] = {}
        self.exact_index: dict[str, Term] = {}
        self.load_all()

    def load_all(self) -> None:
        terms: dict[str, Term] = {}
        for pattern in self.config.glossary.paths:
            for file_name in glob.glob(pattern):
                for term in self._load_terms(Path(file_name)):
                    terms[term.id] = term
        live_terms = self._load_live_terms()
        self.terms = terms
        self.live_terms = live_terms
        self._rebuild_index()

    def reload(self) -> None:
        self.load_all()

    def all_terms(self) -> list[Term]:
        merged = {**self.terms, **self.live_terms}
        return sorted(merged.values(), key=lambda item: (item.priority, item.id), reverse=True)

    def get_asr_hotwords(self, max_terms: int) -> list[str]:
        hotwords: list[str] = []
        for term in self.all_terms():
            hotwords.extend(term.source[:1])
            if len(hotwords) >= max_terms:
                break
        return hotwords[:max_terms]

    def normalize_transcript(self, text: str) -> NormalizationResult:
        normalized = text
        replacements: list[dict] = []
        matched: list[Term] = []
        for term in self.all_terms():
            for wrong in term.asr_mishearings:
                preferred = term.source[0] if term.source else term.zh_cn
                if wrong and wrong.lower() in normalized.lower():
                    normalized = self._case_insensitive_replace(normalized, wrong, preferred)
                    replacements.append({"from": wrong, "to": preferred, "term_id": term.id})
                    matched.append(term)
        return NormalizationResult(text=normalized, replacements=replacements, terms=self._dedupe_terms(matched))

    def retrieve_candidate_terms(self, text: str, max_terms: int | None = None) -> list[Term]:
        limit = max_terms or self.config.glossary.max_candidate_terms
        transcript = normalize_match_text(text)
        if not transcript:
            return []

        term_phrases = [
            (term, tuple(normalize_match_text(phrase) for phrase in term.match_phrases))
            for term in self.all_terms()
        ]
        exact_phrases = {phrase for _, phrases in term_phrases for phrase in phrases if phrase and phrase in transcript}
        standalone = standalone_exact_phrases(exact_phrases, transcript)
        scored: list[tuple[tuple[bool, float, int], Term]] = []
        for term, phrases in term_phrases:
            exact = any(phrase in standalone for phrase in phrases)
            if exact:
                score = 100.0
            elif any(phrase in exact_phrases for phrase in phrases):
                continue
            else:
                score = max(
                    (fuzzy_match_score(phrase, transcript, self.config.glossary.fuzzy_threshold) for phrase in phrases),
                    default=0.0,
                )
            if score:
                scored.append(((exact, score, term.priority), term))
        scored.sort(key=lambda item: item[0], reverse=True)
        return self._dedupe_terms([term for _, term in scored])[:limit]

    def add_live_term(self, term: Term) -> None:
        self._ensure_db()
        with sqlite3.connect(self.config.runtime.live_terms_db) as conn:
            conn.execute(
                "insert or replace into terms(id, payload) values(?, ?)",
                (term.id, term.model_dump_json()),
            )
        self.live_terms[term.id] = term
        self._rebuild_index()

    def update_live_term(self, term_id: str, patch: dict) -> Term:
        current = {**self.terms, **self.live_terms}.get(term_id)
        if current is None:
            raise KeyError(term_id)
        updated = current.model_copy(update=patch)
        self.add_live_term(updated)
        return updated

    def delete_live_term(self, term_id: str) -> None:
        self._ensure_db()
        with sqlite3.connect(self.config.runtime.live_terms_db) as conn:
            conn.execute("delete from terms where id = ?", (term_id,))
        self.live_terms.pop(term_id, None)
        self._rebuild_index()

    def record_unknown_term(self, text: str) -> None:
        log_path = Path(self.config.runtime.unknown_terms_log)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")

    def unknown_terms(self) -> list[dict]:
        path = Path(self.config.runtime.unknown_terms_log)
        if not path.exists():
            return []
        items: list[dict] = []
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return items

    def _load_terms(self, path: Path) -> Iterable[Term]:
        with path.open("r", encoding="utf-8-sig") as fh:
            payload = (json.load(fh) if path.suffix.lower() == ".json" else yaml.safe_load(fh)) or {}
        raw_terms = payload if isinstance(payload, list) else payload.get("terms", [])
        for item in raw_terms:
            if "sources" in item and "zhCN" in item:
                term = bilibot_term(item, self.config.asr.language)
                if term is not None:
                    yield term
            else:
                yield Term.model_validate(item)

    def _load_live_terms(self) -> dict[str, Term]:
        self._ensure_db()
        loaded: dict[str, Term] = {}
        with sqlite3.connect(self.config.runtime.live_terms_db) as conn:
            rows = conn.execute("select id, payload from terms").fetchall()
        for term_id, payload in rows:
            try:
                loaded[term_id] = Term.model_validate_json(payload)
            except Exception:
                continue
        return loaded

    def _ensure_db(self) -> None:
        db_path = Path(self.config.runtime.live_terms_db)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("create table if not exists terms(id text primary key, payload text not null)")

    def _rebuild_index(self) -> None:
        self.exact_index = {}
        for term in self.all_terms():
            for phrase in term.match_phrases:
                self.exact_index[phrase.lower()] = term

    @staticmethod
    def _dedupe_terms(terms: Iterable[Term]) -> list[Term]:
        seen: set[str] = set()
        result: list[Term] = []
        for term in terms:
            if term.id not in seen:
                seen.add(term.id)
                result.append(term)
        return result

    @staticmethod
    def _case_insensitive_replace(text: str, old: str, new: str) -> str:
        lower = text.lower()
        old_lower = old.lower()
        start = lower.find(old_lower)
        while start >= 0:
            end = start + len(old)
            text = text[:start] + new + text[end:]
            lower = text.lower()
            start = lower.find(old_lower, start + len(new))
        return text


def bilibot_term(item: dict, language: str | None) -> Term | None:
    sources = item["sources"]
    languages = [language] if language else sources
    forms = list(dict.fromkeys(form for key in languages for form in sources.get(key, []) if form.strip()))
    if not forms:
        return None
    category = item["category"]
    zh_cn = item["zhCN"]
    # Stable across catalogue reordering so a live term can override this entry.
    digest = hashlib.sha256(f"{category}\n{zh_cn}".encode("utf-8")).hexdigest()[:16]
    return Term(
        id=f"bilibot.{digest}",
        category=category,
        source=forms,
        zh_cn=zh_cn,
        priority={"title": 100, "character": 80, "kibo": 70}.get(category, 60),
    )


def normalize_match_text(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).casefold()
    kana = (chr(ord(char) + 0x60) if "\u3041" <= char <= "\u3096" else char for char in text)
    return " ".join("".join(kana).split())


def has_exact_source(term: Term, transcript: str) -> bool:
    return any(phrase and phrase in transcript for phrase in map(normalize_match_text, term.match_phrases))


def standalone_exact_phrases(phrases: set[str], transcript: str) -> set[str]:
    # A short name inside a longer name is evidence only if it also occurs on its own.
    accepted: set[str] = set()
    spans: list[tuple[int, int]] = []
    for phrase in sorted(phrases, key=len, reverse=True):
        for match in re.finditer(re.escape(phrase), transcript):
            start, end = match.span()
            if not any(left <= start and end <= right for left, right in spans):
                accepted.add(phrase)
                spans.append((start, end))
    return accepted


def fuzzy_match_score(phrase: str, transcript: str, threshold: int) -> float:
    if fuzz is None or len(phrase.replace(" ", "")) < 4 or len(transcript) < len(phrase) - 1:
        return 0.0
    alignment = fuzz.partial_ratio_alignment(phrase, transcript, score_cutoff=threshold)
    if alignment is None:
        return 0.0
    observed = transcript[alignment.dest_start:alignment.dest_end]
    return fuzz.ratio(phrase, observed, score_cutoff=threshold)

