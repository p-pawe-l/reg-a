"""Codex rollout parser (~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<session_id>.jsonl).

Turns are delimited by `task_started` and `task_complete` / `turn_aborted`
events carrying a `turn_id`; the turn's content arrives as `item_completed`
events (UserMessage, AgentMessage, CommandExecution, FileChange, ...).
"""

from collections import Counter
from pathlib import Path

from reg_a.models import ParsedSession, ParsedTurn
from reg_a.parsers import EmptyTranscriptError, Excerpt, parse_ts, read_jsonl


class _OpenTurn:
    def __init__(self, turn_id: str, started_at) -> None:
        self.turn_id = turn_id
        self.started_at = started_at
        self.prompt_parts: list[str] = []
        self.models: Counter[str] = Counter()
        self.files: list[str] = []
        self.commands: list[str] = []
        self.final_message = ""
        self.has_final_answer = False
        self.excerpt = Excerpt()


def _content_text(content: object) -> str:
    if not isinstance(content, list):
        return ""
    return "\n".join(
        b.get("text", "") for b in content if isinstance(b, dict) and isinstance(b.get("text"), str)
    )


def _command_text(command: object) -> str:
    # ["/usr/bin/bash", "-lc", "<script>"] → "<script>"
    if isinstance(command, list) and command:
        return str(command[-1])
    return str(command or "")


def _add_item(turn: _OpenTurn, item: dict) -> None:
    kind = item.get("type")
    if kind == "UserMessage":
        text = _content_text(item.get("content"))
        turn.prompt_parts.append(text)
        turn.excerpt.add("user", text, limit=4000)
    elif kind == "AgentMessage":
        text = _content_text(item.get("content"))
        if text and item.get("phase") == "final_answer":
            turn.final_message, turn.has_final_answer = text, True
        elif text and not turn.has_final_answer:
            turn.final_message = text
        turn.excerpt.add("assistant", text)
    elif kind == "CommandExecution":
        cmd = _command_text(item.get("command"))
        turn.commands.append(cmd)
        turn.excerpt.add(f"command exit={item.get('exit_code')}", cmd, 600)
        turn.excerpt.add("result", str(item.get("aggregated_output") or ""), 400)
    elif kind == "FileChange":
        for path, change in (item.get("changes") or {}).items():
            if path not in turn.files:
                turn.files.append(path)
            change_type = change.get("type") if isinstance(change, dict) else "?"
            turn.excerpt.add(f"file {change_type}", path)
    elif kind:
        turn.excerpt.add(f"item {kind}", str(item.get("query") or item.get("kind") or ""), 300)


def parse_codex_rollout(path: Path, max_chars: int = 20_000) -> ParsedSession:
    session_id = path.stem.split("-", 6)[-1] if path.stem.startswith("rollout-") else path.stem
    cwd = None
    session_started = None
    model: str | None = None
    turns: list[ParsedTurn] = []
    open_turns: dict[str, _OpenTurn] = {}

    for e in read_jsonl(path):
        p = e.get("payload") or {}
        ts = parse_ts(e.get("timestamp"))
        etype, ptype = e.get("type"), p.get("type")
        if session_started is None and ts:
            session_started = ts

        if etype == "session_meta":
            session_id = p.get("session_id") or p.get("id") or session_id
            cwd = p.get("cwd") or cwd
            session_started = parse_ts(p.get("timestamp")) or session_started
        elif etype == "turn_context":
            model = p.get("model") or model
            cwd = p.get("cwd") or cwd
        elif ptype == "thread_settings_applied":
            model = (p.get("thread_settings") or {}).get("model") or model
        elif ptype == "task_started" and p.get("turn_id"):
            started = parse_ts(p.get("started_at")) or ts
            open_turns[p["turn_id"]] = _OpenTurn(p["turn_id"], started)
        elif ptype == "item_completed" and p.get("turn_id") in open_turns:
            turn = open_turns[p["turn_id"]]
            if model:
                turn.models[model] += 1
            _add_item(turn, p.get("item") or {})
        elif ptype in ("task_complete", "turn_aborted") and p.get("turn_id") in open_turns:
            turn = open_turns.pop(p["turn_id"])
            if not turn.prompt_parts:
                continue  # not user-initiated (e.g. a background/agent-triggered turn)
            finished = parse_ts(p.get("completed_at")) or ts or turn.started_at
            duration_ms = p.get("duration_ms")
            if ptype == "task_complete" and p.get("last_agent_message"):
                turn.final_message = p["last_agent_message"]
            turns.append(
                ParsedTurn(
                    turn_id=turn.turn_id,
                    user_prompt="\n".join(turn.prompt_parts),
                    started_at=turn.started_at,
                    finished_at=finished,
                    work_time_s=duration_ms / 1000
                    if duration_ms
                    else (finished - turn.started_at).total_seconds(),
                    model=turn.models.most_common(1)[0][0] if turn.models else model,
                    interrupted=ptype == "turn_aborted",
                    files_changed=turn.files,
                    commands_run=turn.commands,
                    final_message=turn.final_message,
                    transcript_excerpt=turn.excerpt.render(max_chars),
                )
            )

    if session_started is None:
        raise EmptyTranscriptError(f"no timestamps in {path}")
    return ParsedSession(
        session_id=session_id,
        tool="codex",
        title=None,
        cwd=cwd,
        started_at=session_started,
        transcript_path=str(path),
        turns=turns,
    )


def find_rollout(codex_home: Path, session_id: str | None) -> Path | None:
    """Rollout file for a session id, or the most recently modified one."""
    sessions = codex_home / "sessions"
    pattern = f"rollout-*-{session_id}.jsonl" if session_id else "rollout-*.jsonl"
    matches = sorted(sessions.rglob(pattern), key=lambda p: p.stat().st_mtime)
    return matches[-1] if matches else None
