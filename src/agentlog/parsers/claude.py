"""Claude Code transcript parser (~/.claude/projects/<cwd>/<session_id>.jsonl).

The transcript is a tree: each entry points at its parent via `parentUuid`,
and rewinding or editing a prompt leaves the abandoned branch in the file. We
walk back from the newest entry to recover the live conversation, then split
it into turns at each human prompt.
"""

from collections import Counter
from pathlib import Path

from agentlog.models import ParsedSession, ParsedTurn
from agentlog.parsers import EmptyTranscriptError, Excerpt, parse_ts, read_jsonl

CONVERSATION_TYPES = {"user", "assistant", "system"}
FILE_TOOLS = {
    "Write": "file_path",
    "Edit": "file_path",
    "MultiEdit": "file_path",
    "NotebookEdit": "notebook_path",
}
INTERRUPT_PREFIX = "[Request interrupted"


def _text_of(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _has_tool_result(content: object) -> bool:
    return isinstance(content, list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in content
    )


def _is_human_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    content = entry.get("message", {}).get("content")
    if _has_tool_result(content):
        return False
    if "origin" in entry:  # newer versions mark the source explicitly
        return (entry["origin"] or {}).get("kind") == "human"
    text = _text_of(content)
    return bool(text) and not text.startswith((INTERRUPT_PREFIX, "<local-command"))


def _live_chain(entries: list[dict]) -> list[dict]:
    by_uuid = {e["uuid"]: e for e in entries if "uuid" in e}
    leaf = next(
        (
            e
            for e in reversed(entries)
            if e.get("type") in CONVERSATION_TYPES and "uuid" in e and not e.get("isSidechain")
        ),
        None,
    )
    chain: list[dict] = []
    seen: set[str] = set()
    while leaf is not None and leaf["uuid"] not in seen:
        seen.add(leaf["uuid"])
        chain.append(leaf)
        leaf = by_uuid.get(leaf.get("parentUuid") or "")
    chain.reverse()
    return chain


def _build_turn(prompt: dict, body: list[dict], max_chars: int) -> ParsedTurn | None:
    started = parse_ts(prompt.get("timestamp"))
    if started is None:
        return None
    user_prompt = _text_of(prompt["message"].get("content"))
    excerpt = Excerpt()
    excerpt.add("user", user_prompt, limit=4000)

    models: Counter[str] = Counter()
    files: list[str] = []
    commands: list[str] = []
    final_message = ""
    duration_ms = 0
    interrupted = False
    last_ts = started
    last_stop_reason: str | None = None

    for e in body:
        ts = parse_ts(e.get("timestamp"))
        if ts and ts > last_ts:
            last_ts = ts
        if e.get("type") == "system":
            if e.get("subtype") == "turn_duration":
                duration_ms += int(e.get("durationMs") or 0)
            continue
        msg = e.get("message") or {}
        content = msg.get("content")
        if e.get("type") == "user":
            if _text_of(content).startswith(INTERRUPT_PREFIX):
                interrupted = True
            for b in content if isinstance(content, list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    excerpt.add("result", _text_of(b.get("content")) or str(b.get("content")), 400)
            continue
        if e.get("type") != "assistant":
            continue  # attachments and other entry kinds carry no reply
        if msg.get("model") and not msg["model"].startswith("<"):
            models[msg["model"]] += 1
        last_stop_reason = msg.get("stop_reason")
        for b in content if isinstance(content, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text" and b.get("text", "").strip():
                final_message = b["text"]
                excerpt.add("assistant", b["text"])
            elif b.get("type") == "tool_use":
                name, inp = b.get("name", "?"), b.get("input") or {}
                if name in FILE_TOOLS and inp.get(FILE_TOOLS[name]):
                    path = inp[FILE_TOOLS[name]]
                    if path not in files:
                        files.append(path)
                if name == "Bash" and inp.get("command"):
                    commands.append(inp["command"])
                excerpt.add(f"tool {name}", _describe_tool_input(name, inp), 600)

    complete = interrupted or duration_ms > 0 or last_stop_reason not in (None, "tool_use")
    if not complete:
        return None
    return ParsedTurn(
        turn_id=prompt["uuid"],
        user_prompt=user_prompt,
        started_at=started,
        finished_at=last_ts,
        work_time_s=duration_ms / 1000 if duration_ms else (last_ts - started).total_seconds(),
        model=models.most_common(1)[0][0] if models else None,
        interrupted=interrupted,
        files_changed=files,
        commands_run=commands,
        final_message=final_message,
        transcript_excerpt=excerpt.render(max_chars),
    )


def _describe_tool_input(name: str, inp: dict) -> str:
    for key in (
        "command",
        "file_path",
        "notebook_path",
        "pattern",
        "url",
        "query",
        "description",
        "prompt",
    ):
        if inp.get(key):
            return str(inp[key])
    return name


def _add_subagent_work(subagents_dir: Path, turns: list[ParsedTurn]) -> None:
    """Subagents log to <session_id>/subagents/agent-*.jsonl, outside the main chain.
    Credit their file edits and commands to the latest turn started before them, which also
    catches background subagents still working after their turn's last entry."""
    if not turns or not subagents_dir.is_dir():
        return
    for path in sorted(subagents_dir.glob("*.jsonl")):
        for e in read_jsonl(path):
            ts = parse_ts(e.get("timestamp"))
            content = (e.get("message") or {}).get("content")
            if e.get("type") != "assistant" or ts is None or not isinstance(content, list):
                continue
            turn = next((t for t in reversed(turns) if t.started_at <= ts), None)
            if turn is None:
                continue
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name, inp = b.get("name"), b.get("input") or {}
                if name in FILE_TOOLS and (fp := inp.get(FILE_TOOLS[name])):
                    if fp not in turn.files_changed:
                        turn.files_changed.append(fp)
                elif name == "Bash" and inp.get("command"):
                    turn.commands_run.append(inp["command"])


def parse_claude_transcript(path: Path, max_chars: int = 20_000) -> ParsedSession:
    entries = list(read_jsonl(path))
    chain = _live_chain(entries)
    title = next((e.get("aiTitle") for e in reversed(entries) if e.get("type") == "ai-title"), None)
    session_id = next((e["sessionId"] for e in entries if e.get("sessionId")), path.stem)
    cwd = next((e["cwd"] for e in chain if e.get("cwd")), None)

    turns: list[ParsedTurn] = []
    prompt: dict | None = None
    body: list[dict] = []
    for e in chain + [None]:  # sentinel flushes the last turn
        if e is None or _is_human_prompt(e):
            if prompt is not None and (turn := _build_turn(prompt, body, max_chars)):
                turns.append(turn)
            prompt, body = e, []
        elif prompt is not None:
            body.append(e)

    _add_subagent_work(path.parent / session_id / "subagents", turns)

    started = (
        turns[0].started_at
        if turns
        else next((t for e in entries if (t := parse_ts(e.get("timestamp")))), None)
    )
    if started is None:
        raise EmptyTranscriptError(f"no timestamps in {path}")
    return ParsedSession(
        session_id=session_id,
        tool="claude-code",
        title=title,
        cwd=cwd,
        started_at=started,
        transcript_path=str(path),
        turns=turns,
    )
