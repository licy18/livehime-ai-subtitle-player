from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

from app.services.hls_media import recording_video_offset


@dataclass(frozen=True)
class Cue:
    start: float
    end: float
    text: str


class RecordingSubtitles:
    def __init__(self, stream_id: str, video_path: Path) -> None:
        self.stream_id = stream_id
        self.video_path = video_path
        self.origin: float | None = None
        self.first_hls_video_pts: float | None = None
        self.cues: dict[str, dict[str, Cue]] = {"ja": {}, "zh-CN": {}}
        self.saved_counts = dict.fromkeys(self.cues, 0)
        self.errors: dict[str, str] = {}
        self.dirty: set[str] = set()
        self.published: set[str] = set()
        self.missing_fragments = 0

    def add_source(self, cue_id: str, start: float, end: float, text: str) -> None:
        self.cues["ja"][cue_id] = Cue(start, end, text)
        self.dirty.add("ja")
        self._save("ja")

    def add_translation(self, cue_id: str, text: str) -> None:
        source = self.cues["ja"].get(cue_id)
        if source is not None:
            self.cues["zh-CN"][cue_id] = replace(source, text=text)
            self.dirty.add("zh-CN")
            self._save("zh-CN")

    def flush(self) -> None:
        for language in self.cues:
            self._save(language, final=True)

    def status(self) -> dict:
        return {
            "tracks": {
                language: {"path": str(self._path(language)), "cues": count}
                for language, count in self.saved_counts.items()
            },
            "error": next(iter(self.errors.values()), None),
            "missing_fragments": self.missing_fragments,
        }

    def _path(self, language: str) -> Path:
        return self.video_path.with_suffix(f".{language}.srt")

    def _save(self, language: str, *, final: bool = False) -> None:
        if language not in self.dirty and (not final or language in self.published):
            return
        if self.origin is None and self.first_hls_video_pts is not None:
            offset = recording_video_offset(self.video_path)
            if offset is not None:
                self.origin = self.first_hls_video_pts - offset
        if self.origin is None:
            if final and self.cues[language]:
                self.errors[language] = "无法读取源录像起始时间，未导出此字幕。"
            return

        cues = sorted(self.cues[language].values(), key=lambda cue: (cue.start, cue.end))
        blocks = []
        for cue in cues:
            start_ms = max(0, round((cue.start - self.origin) * 1000))
            end_ms = round((cue.end - self.origin) * 1000)
            if end_ms <= 0:
                continue
            timing = f"{_timestamp(start_ms)} --> {_timestamp(max(start_ms + 1, end_ms))}"
            text = "\n".join(line for line in cue.text.splitlines() if line.strip())
            blocks.append(f"{len(blocks) + 1}\n{timing}\n{text}\n\n")
        path = self._path(language)
        temp = path.with_suffix(".srt.tmp")
        try:
            temp.write_text("".join(blocks), encoding="utf-8")
            temp.replace(path)
        except OSError as exc:
            self.errors[language] = str(exc)
        else:
            self.errors.pop(language, None)
            self.saved_counts[language] = len(blocks)
            self.published.add(language)
            self.dirty.discard(language)


def _timestamp(total_ms: int) -> str:
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds, milliseconds = divmod(remainder, 1_000)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{milliseconds:03d}"
