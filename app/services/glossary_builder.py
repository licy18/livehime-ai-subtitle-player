from __future__ import annotations

import csv
import io
from dataclasses import dataclass, field
from uuid import uuid4

import httpx
import yaml

from app.core.config import AppConfig, httpx_proxy
from app.models.term import Term
from app.services.translator import Translator


@dataclass
class GlossaryJob:
    id: str
    status: str = "pending"
    draft_terms: list[Term] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    request: dict = field(default_factory=dict)


class GlossaryBuilder:
    def __init__(self, translator: Translator | None = None, config: AppConfig | None = None) -> None:
        self.translator = translator
        self.config = config
        self.jobs: dict[str, GlossaryJob] = {}

    async def create_job(self, request: dict) -> GlossaryJob:
        job = GlossaryJob(id=str(uuid4()), status="running", request=request)
        self.jobs[job.id] = job
        try:
            text_chunks = await self._collect_sources(request.get("sources", []), job)
            job.draft_terms = self._heuristic_extract(request, text_chunks)
            job.status = "completed"
        except Exception as exc:
            job.status = "failed"
            job.warnings.append(str(exc))
        return job

    def get_job(self, job_id: str) -> GlossaryJob:
        if job_id not in self.jobs:
            raise KeyError(job_id)
        return self.jobs[job_id]

    def review_job(self, job_id: str, draft_terms: list[dict]) -> GlossaryJob:
        job = self.get_job(job_id)
        job.draft_terms = [Term.model_validate(item) for item in draft_terms]
        job.status = "reviewed"
        return job

    def save_job(self, job_id: str, path: str) -> dict:
        job = self.get_job(job_id)
        payload = {
            "game_id": job.request.get("game_id", "unknown_game"),
            "zh_cn_title": job.request.get("zh_cn_title", ""),
            "terms": [term.model_dump(mode="json") for term in job.draft_terms],
        }
        with open(path, "w", encoding="utf-8") as fh:
            yaml.safe_dump(payload, fh, allow_unicode=True, sort_keys=False)
        job.status = "saved"
        return {"path": path, "terms": len(job.draft_terms)}

    async def _collect_sources(self, sources: list[dict], job: GlossaryJob) -> list[str]:
        chunks: list[str] = []
        for source in sources:
            source_type = source.get("type")
            if source_type == "url":
                url = source.get("url", "")
                if not url.startswith(("http://", "https://")):
                    job.warnings.append(f"Skipped invalid URL: {url}")
                    continue
                async with httpx.AsyncClient(
                    timeout=10,
                    proxy=httpx_proxy(self.config) if self.config else None,
                ) as client:
                    response = await client.get(url)
                    response.raise_for_status()
                    chunks.append(response.text)
            elif source_type == "csv":
                reader = csv.reader(io.StringIO(source.get("content", "")))
                for row in reader:
                    chunks.extend(cell for cell in row if cell.strip())
            else:
                chunks.append(source.get("content", ""))
        return chunks

    def _heuristic_extract(self, request: dict, chunks: list[str]) -> list[Term]:
        terms: dict[str, Term] = {}
        game_id = request.get("game_id", "game")
        title = request.get("zh_cn_title", "")
        if title:
            terms[f"game.{game_id}.title"] = Term(
                id=f"game.{game_id}.title",
                category="game_title",
                source=[request.get("source_title", game_id.replace("_", " ").title())],
                aliases=[],
                zh_cn=title,
                priority=100,
            )
        for raw in chunks:
            for line in raw.splitlines():
                clean = line.strip().strip("-*|,;")
                if not clean or len(clean) > 60 or len(clean) < 2:
                    continue
                if any("\u4e00" <= char <= "\u9fff" for char in clean):
                    term_id = f"{game_id}.term.{abs(hash(clean))}"
                    terms.setdefault(
                        term_id,
                        Term(
                            id=term_id,
                            category="manual",
                            source=[clean],
                            zh_cn=clean,
                            priority=10,
                        ),
                    )
        return list(terms.values())
