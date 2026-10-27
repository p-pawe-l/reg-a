# agentlog

Logs every Claude Code and Codex session: captures each user prompt and what the agent did, has a local self-hosted LLM summarize each turn, stores one JSON file per session, and renders sessions to a LaTeX/PDF report on demand. No database: the per-session JSON file is the record.

Status: design stage, no code yet. This file holds the agreed architecture; once code exists, the code is the source of truth for schemas and commands, and this file keeps only the reasoning and the gotchas.

## Architecture

```
claude-code: UserPromptSubmit hook ──► record prompt + start time
             Stop hook ───────────────► read this turn's slice of transcript_path
codex:       notify (turn complete) ──► read this turn's slice of the rollout file
                     │
                     ▼
        collector — the hook spawns a background worker and exits at once
          1. facts (code):  prompt, session_id, tool, model, timestamps,
                            work time, files changed, commands run
          2. summary (LLM): TurnSummary only, forced to its JSON schema,
                            in the configured language
          3. pydantic validation → one retry → fallback record on failure
          4. merge into sessions/<tool>-<session_id>.json
                     │
   CLI: agentlog convert-to-pdf <session_id | --all>
        JSON → Jinja2 → .tex (summary table + per-turn detail) → latexmk → .pdf
```

### The facts/summary split

The core rule. **Facts** are everything readable from hook payloads and transcripts; code fills them in. The **summary** is the only part the LLM writes. Keep the LLM's output schema limited to `TurnSummary` so it can never corrupt a prompt, ID, or timestamp.

### Schema (pydantic v2)

```python
class TurnSummary(BaseModel):        # LLM-produced
    summary: str                     # 2–4 sentences: what the agent did
    actions: list[str]
    outcome: Literal["success", "partial", "failed", "question"]
    open_issues: list[str] = []

class Turn(BaseModel):               # code-produced
    index: int
    user_prompt: str
    started_at: datetime
    finished_at: datetime
    work_time_s: float
    model: str
    files_changed: list[str]
    commands_run: list[str]
    summary: TurnSummary | None      # None → summarizer failed
    summary_error: str | None = None

class Session(BaseModel):
    session_id: UUID
    tool: Literal["claude-code", "codex"]
    cwd: str
    started_at: datetime
    turns: list[Turn]
```

The JSON file is always written: a turn whose summary fails validation twice is stored with `summary=None` and `summary_error` set.

### Config — `~/.config/agentlog/config.toml`

```toml
language = "pl"                          # summary language: "pl" | "en"
output_dir = "~/agentlog/sessions"

[llm]
base_url = "http://localhost:11434/v1"   # any OpenAI-compatible server (Ollama, llama.cpp, vLLM)
model = "qwen3:8b"
max_input_chars = 20000
timeout_s = 120
```

## Session identity

Key everything by `(tool, session_id)`.

- **Claude Code**: every hook payload carries `session_id` and `transcript_path` (`~/.claude/projects/<cwd with / → ->/<uuid>.jsonl`). `--continue`/`--resume` keep the ID, so turns append to the existing file; `--fork-session` and `/clear` start a new ID. `SessionStart.source` (`startup|resume|clear|compact`) tells them apart.
- **Codex**: ID is the UUIDv7 in `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl`; `codex resume` keeps it. Turn completion arrives via `notify = [...]` in `~/.codex/config.toml`.

## Gotchas

- **Hooks must return fast.** Summarizing takes seconds; the hook detaches a worker and exits, so the agent never waits.
- **Transcript formats are unversioned.** Both JSONL formats change between releases: parse leniently, skip unknown entry types, and keep fixture transcripts from real sessions in `tests/fixtures/`.
- **Truncate before summarizing.** Tool outputs and diffs can be huge; cap each at `max_input_chars` before sending to a small model.
- **Concurrent writes.** Two turns can finish together: write the session file atomically (temp file + rename) under a file lock.
- **LaTeX.** Escape `_ % & # $ { } ~ ^ \` in every user- or model-provided string. Build with `lualatex` + `fontspec` + `babel` so Polish characters render.
- **Secrets.** Prompts and tool output can contain keys or `.env` contents; output stays local, and fixtures are scrubbed before committing.

## Toolchain

Python with `uv`; package under `src/agentlog/`, tests under `tests/` (pytest). Runtime prerequisites not yet installed on the dev machine: Ollama (`ollama pull qwen3:8b`) and TeX Live (`texlive-basic texlive-latexextra texlive-langextra texlive-binextra`). Record real commands here once `pyproject.toml` exists.

## Subagents and workflow

Three project subagents live in `.claude/agents/`. Each file is a self-contained role prompt.

| Agent | Role | When |
|---|---|---|
| `test-code` | Writes and runs hermetic pytest tests for the new feature | After implementing a feature |
| `code-review` | Scans the repo and lists every problem to fix (read-only) | After tests are written |
| `update-agentmd-readme` | Brings AGENTS.md (status, implemented, to-do) and README.md in line with the code | After review findings are fixed |

Every time you implement a part of the system, run this loop:

1. Implement the part.
2. **test-code**: tests for it; fix code bugs it reports.
3. **code-review**: fix every `high` and `medium` finding; rerun until the verdict is `READY TO COMMIT`. Note any skipped `low` findings as to-dos.
4. **update-agentmd-readme**: docs match the code.
5. Commit (rules below).

How to run them:
- **Claude Code**: launch through the Agent tool with `subagent_type` set to the agent's name.
- **Codex** (or any agent without `.claude/agents` support): read the agent's file and either hand its body to a subagent as instructions, or perform the role yourself, following its steps and output format exactly.

## Commits

Commit on your own initiative every time a change is **good**: one logical change, finished, and verified (tests pass, or the behaviour was checked by running it). Commit each good change as soon as it's done, before starting the next one. Keep unrelated changes in separate commits, and leave unfinished work uncommitted.

Message format:

```
<imperative subject, ≤72 chars: Add Codex rollout parser>

<why the change was needed and what it does: behaviour, not a file list>

Verified: <how: `uv run pytest tests/test_codex.py`, manual hook run, ...>
```

Commits stay local; pushing is the user's call. Placeholder config examples only; real configs and session outputs stay out of the repo.
