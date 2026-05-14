from __future__ import annotations

import glob
import json
import sqlite3
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
        self.terms = {}
        for pattern in self.config.glossary.paths:
            for file_name in glob.glob(pattern):
                self._load_yaml(Path(file_name))
        self.live_terms = self._load_live_terms()
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
        lower = text.lower()
        scored: list[tuple[int, Term]] = []
        for term in self.all_terms():
            score = 0
            for phrase in term.match_phrases:
                phrase_lower = phrase.lower()
                if phrase_lower and phrase_lower in lower:
                    score = max(score, 100 + term.priority)
                elif fuzz and phrase_lower:
                    fuzzy_score = fuzz.partial_ratio(phrase_lower, lower)
                    if fuzzy_score >= self.config.glossary.fuzzy_threshold:
                        score = max(score, fuzzy_score + term.priority)
            if score:
                scored.append((score, term))
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

    def _load_yaml(self, path: Path) -> None:
        with path.open("r", encoding="utf-8") as fh:
            payload = yaml.safe_load(fh) or {}
        raw_terms = payload.get("terms", payload if isinstance(payload, list) else [])
        for item in raw_terms:
            term = Term.model_validate(item)
            self.terms[term.id] = term

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

