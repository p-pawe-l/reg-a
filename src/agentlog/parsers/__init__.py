"""Transcript parsers. Both formats are unversioned: parse leniently and skip
entry types that aren't recognized."""

import json
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path


class EmptyTranscriptError(ValueError):
    """The transcript has no timestamped entries yet (just created, or empty)."""


def read_jsonl(path: Path) -> Iterator[dict]:
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a partially flushed last line, or garbage
            if isinstance(entry, dict):
                yield entry


def parse_ts(value: object) -> datetime | None:
    """ISO-8601 string or unix seconds → aware datetime."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, UTC)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=UTC)
    return None


def clip(text: str, limit: int) -> str:
    """Keep head and tail of long text."""
    if len(text) <= limit:
        return text
    half = limit // 2 - 20
    if half <= 0:
        return text[:limit]
    return f"{text[:half]}\n…[{len(text) - 2 * half} chars cut]…\n{text[-half:]}"


class Excerpt:
    """Condensed, size-capped log of a turn for the summarizer."""

    PIECE_LIMIT = 1500

    def __init__(self) -> None:
        self.lines: list[str] = []

    def add(self, label: str, text: str, limit: int = PIECE_LIMIT) -> None:
        text = text.strip()
        if text:
            self.lines.append(f"[{label}] {clip(text, limit)}")

    def render(self, max_chars: int) -> str:
        return clip("\n".join(self.lines), max_chars)
