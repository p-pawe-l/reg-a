"""Bring a session's JSON file up to date with its transcript.

Idempotent: turns already stored (matched by `turn_id`) are kept as-is, so
running it from every hook invocation, or after a missed one, is safe. Turns
are never deleted, so history survives transcript compaction or rewinds.
"""

import logging
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from reg_a.config import Config
from reg_a.models import ParsedSession, ParsedTurn, Session, Tool, Turn, TurnSummary
from reg_a.parsers.claude import parse_claude_transcript
from reg_a.parsers.codex import parse_codex_rollout
from reg_a.store import load_session, save_session, session_lock, session_path
from reg_a.summarizer import Summarizer, SummaryError

log = logging.getLogger(__name__)

SummarizeFn = Callable[[ParsedTurn, str], TurnSummary]


def parse_transcript(tool: Tool, path: Path, max_chars: int) -> ParsedSession:
    if tool == "claude-code":
        return parse_claude_transcript(path, max_chars)
    return parse_codex_rollout(path, max_chars)


def sync_session(
    tool: Tool,
    transcript: Path,
    cfg: Config,
    summarize: SummarizeFn | None = None,
    resummarize_failed: bool = False,
) -> Path:
    """Parse `transcript`, summarize new or changed turns, and save. Returns the JSON path."""
    parsed = parse_transcript(tool, transcript, cfg.llm.max_input_chars)
    out = session_path(cfg.output_dir, tool, parsed.session_id)
    if summarize is None:
        summarize = Summarizer(cfg.llm, cfg.language).summarize

    with session_lock(out):
        session = _load_or_set_aside(out) or Session(
            session_id=parsed.session_id,
            tool=tool,
            started_at=parsed.started_at,
            transcript_path=str(transcript),
        )
        session.title = parsed.title or session.title
        session.cwd = parsed.cwd or session.cwd
        session.transcript_path = str(transcript)

        stored = {t.turn_id: t for t in session.turns}
        # A turn can complete more than once (e.g. resumed by a background task under the
        # same prompt), so stored turns are redone whenever their facts changed.
        changed = [
            pt
            for pt in parsed.turns
            if pt.turn_id not in stored or _facts(stored[pt.turn_id]) != _facts(pt)
        ]
        retries = [
            pt
            for pt in parsed.turns
            if resummarize_failed
            and pt not in changed
            and stored[pt.turn_id].summary_error is not None
        ]

        llm_down: str | None = None  # circuit breaker: once the server fails, stop calling it
        for pt in changed + retries:  # new work first, old failures after
            existing = stored.get(pt.turn_id)
            summary, error = None, None
            if llm_down:
                error = f"not attempted, LLM failed earlier in this run: {llm_down}"
            else:
                try:
                    summary = summarize(pt, tool)
                except SummaryError as e:
                    error = str(e)
                    if e.unavailable:
                        llm_down = error
                except Exception as e:  # keep the JSON file complete even if summarizing breaks
                    log.exception("summarizer crashed on turn %s", pt.turn_id)
                    error = f"{type(e).__name__}: {e}"
            if summary is None and existing and existing.summary:
                summary = existing.summary  # stale beats none; summary_error marks it for retry
            stored[pt.turn_id] = Turn(
                index=0,
                summary=summary,
                summary_error=error,
                **pt.model_dump(exclude={"transcript_excerpt"}),
            )
            _save(out, session, stored)  # per turn, so a long backfill keeps its progress

        _save(out, session, stored)
    return out


def _facts(t: Turn | ParsedTurn) -> tuple:
    return (
        t.finished_at,
        t.work_time_s,
        t.interrupted,
        tuple(t.files_changed),
        tuple(t.commands_run),
        t.final_message,
    )


def _load_or_set_aside(path: Path) -> Session | None:
    """Load the session file; a corrupt one is renamed so the session is rebuilt."""
    try:
        return load_session(path)
    except ValueError:  # pydantic ValidationError, or invalid UTF-8
        aside = path.with_name(f"{path.name}.corrupt-{datetime.now():%Y%m%d-%H%M%S}")
        path.rename(aside)
        log.warning("corrupt session file moved to %s; rebuilding from transcript", aside)
        return None


def _save(out: Path, session: Session, turns: dict[str, Turn]) -> None:
    session.turns = sorted(turns.values(), key=lambda t: t.started_at)
    for i, t in enumerate(session.turns, 1):
        t.index = i
    save_session(out, session)
