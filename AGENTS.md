# reg-a

Logs every Claude Code and Codex session: captures each user prompt and what the agent did, has a local self-hosted LLM summarize each turn, stores one JSON file per session, and renders sessions to a LaTeX/PDF report on demand. No database: the per-session JSON file is the record.

The code is the source of truth for schemas, config keys and commands; this file points at the modules and keeps the reasoning and the gotchas the code can't show. User-facing docs are in `README.md`.

## Status

### Implemented

| Feature | Module |
|---|---|
| CLI: `hook`, `sync`, `list`, `convert-to-pdf`, `install`, `init-config` | `src/reg_a/cli.py` |
| Config (TOML, pydantic, defaults when the file is missing) | `src/reg_a/config.py` |
| Session schema; `TurnSummary` is the only LLM-produced model | `src/reg_a/models.py` |
| Claude Code transcript parser (live-branch walk, subagent work) | `src/reg_a/parsers/claude.py` |
| Codex rollout parser; rollout lookup by session id | `src/reg_a/parsers/codex.py` |
| Shared parsing helpers: lenient JSONL, timestamps, size-capped excerpt | `src/reg_a/parsers/__init__.py` |
| LLM summarizer over an OpenAI-compatible chat API | `src/reg_a/summarizer.py` |
| Idempotent sync: transcript → session JSON | `src/reg_a/sync.py` |
| Atomic, locked session store | `src/reg_a/store.py` |
| Hook entry points, detached worker, installers for Claude Code / Codex | `src/reg_a/hooks.py` |
| LaTeX report (pl and en labels) + PDF build via latexmk/lualatex | `src/reg_a/report.py`, `src/reg_a/templates/report.tex.j2` |

Verification so far: 222 hermetic tests pass (`uv run pytest`), `ruff check` is clean. **Not yet verified:**
- The PDF build itself. TeX Live isn't installed on the dev machine; the `.tex` output is checked structurally only.
- A real LLM. Ollama isn't installed; the LLM path was tested against a stub OpenAI-compatible server.
- Real hooks. Nothing has been installed into the user's `~/.claude/settings.json` or `~/.codex/config.toml` yet.

### To-do (next first)

1. Install TeX Live, run `reg-a convert-to-pdf` on a real session, fix whatever the template breaks on.
2. Install Ollama + `qwen3:8b`, run `reg-a sync` on a real transcript, check the summaries' quality and language.
3. Run `reg-a install claude` / `install codex` for real and confirm the actual Codex `notify` payload (key names, see Gotchas); adjust `hooks.codex_rollout_from_notify` and add a fixture.
4. `reg-a sync --all`: backfill by scanning `~/.claude/projects/**/*.jsonl` and `~/.codex/sessions/**/rollout-*.jsonl`.
5. Codex turns without a `UserMessage` (agent-triggered) are skipped by `parse_codex_rollout`; decide how to record them.

## Architecture

```
claude-code: Stop + SessionEnd hooks ─┐   stdin JSON carries transcript_path
codex:       notify (turn complete) ──┤   argv JSON payload → rollout file
                                      ▼
  reg-a hook <tool>   (cli.cmd_hook → hooks.py; always exits 0)
     resolves the transcript, spawns detached
     `reg-a sync <tool> <transcript> --delay 1 --retry-failed`, exits
                                      ▼
  reg-a sync          (sync.sync_session; log → <output_dir>/reg-a.log)
     1. parse the WHOLE transcript → ParsedSession (facts, by code)
     2. for each turn that is new, or whose facts changed, then (with
        --retry-failed) each stored turn with a summary_error:
        summarize → TurnSummary (LLM)
     3. under a file lock: merge by turn_id, renumber, atomic write
        <output_dir>/<tool>-<session_id>.json
                                      ▼
  reg-a convert-to-pdf <id | path | --all>   (report.py)
     JSON → Jinja2 → <output_dir>/reports/<stem>.tex → latexmk -lualatex → .pdf
```

### The facts/summary split

The core rule. **Facts** are everything readable from hook payloads and transcripts; code fills them in. The **summary** is the only part the LLM writes. The LLM's output schema is `TurnSummary` alone (`TurnSummary.llm_schema()`, sent as a strict `json_schema` response format), so it can never corrupt a prompt, ID, or timestamp. See `src/reg_a/models.py` for `TurnSummary`, `Turn`, `Session`, and the parser-side `ParsedTurn` / `ParsedSession`.

The summarizer (`summarizer.py`) strips `<think>…</think>` blocks and code fences that local models add, validates with pydantic, retries `llm.retries` times, then raises `SummaryError`. `sync` then stores the turn with `summary_error` set (keeping the previous summary, if the turn had one, as stale-but-better-than-nothing): the JSON file is always written.

**Circuit breaker:** `SummaryError.unavailable` is true when the server itself failed (connection, timeout, HTTP error). After that, the rest of the sync run skips the LLM and marks remaining turns "not attempted", so a hung server costs each hook worker one timeout, not one per failed turn. A bad reply for one turn (invalid JSON) does not trip it.

### Why sync re-parses everything

There is no `UserPromptSubmit` hook. Each hook run re-parses the whole transcript and only summarizes what's new, keyed by `turn_id` (Claude: the prompt entry's `uuid`; Codex: the event's `turn_id`). This makes sync idempotent: a missed hook is caught up by the next one, and running it by hand is safe. Stored turns are never deleted, so history survives `/compact` and rewinds.

A turn can complete more than once: Claude Code resumes a turn under the same prompt when a background task notification arrives. So `sync` compares a stored turn's facts (`finished_at`, `work_time_s`, `interrupted`, files, commands, final message) with the fresh parse and re-summarizes when they differ.

### Parsers

- **Claude Code** (`parsers/claude.py`): the transcript is a tree via `parentUuid`; rewinding or editing a prompt leaves dead branches in the file. The parser walks back from the newest non-sidechain entry to get the live chain, then splits it at human prompts (`origin.kind == "human"` when present; otherwise heuristics that skip tool results, `isMeta`, interrupt markers and `<local-command` output). A turn counts as complete if it has a `turn_duration` system entry, was interrupted, or its last stop reason isn't `tool_use`. Subagent work is read from `<session_id>/subagents/*.jsonl` next to the transcript and credited (files, commands) to the latest turn that started before it, so background subagents still working after their turn's last entry count toward that turn. The title comes from `ai-title` entries.
- **Codex** (`parsers/codex.py`): turns are delimited by `task_started` and `task_complete` / `turn_aborted` events with a `turn_id`; content comes from `item_completed` events (`UserMessage`, `AgentMessage`, `CommandExecution`, `FileChange`, others into the excerpt). The model comes from `turn_context` / `thread_settings_applied`. Turns with no `UserMessage` are skipped (to-do 5).

Both feed the summarizer an `Excerpt`: labelled lines, each piece clipped (head + tail kept), the whole capped at `llm.max_input_chars`.

### Config

`src/reg_a/config.py` (`Config`, `LLMConfig`; `extra="forbid"`, so typos fail loudly). Path: `$REG_A_CONFIG`, else `~/.config/reg-a/config.toml`; a missing file means all defaults. `reg-a init-config` writes `EXAMPLE_CONFIG`. Note the code default `language = "en"` while the example sets `"pl"`. `codex_home` defaults to `$CODEX_HOME` or `~/.codex`.

### Report

`report.py` + `templates/report.tex.j2`: Jinja2 with `\BLOCK{}`, `\VAR{}`, `\#{}` delimiters so LaTeX braces don't clash. Overview table, a turn summary longtable, then per-turn detail (prompt, summary/actions/open issues, or the agent's final message when the summary is missing; files; up to 10 commands). Labels in `LABELS` exist for `pl` and `en`; other languages fall back to English labels with the matching babel language from `BABEL_LANGUAGES`.

## Session identity

Key everything by `(tool, session_id)`; the file is `<output_dir>/<tool>-<session_id>.json` (`store.session_path`).

- **Claude Code**: every hook payload carries `session_id` and `transcript_path` (`~/.claude/projects/<cwd with / → ->/<uuid>.jsonl`). `--continue`/`--resume` keep the ID, so turns append to the existing file; `--fork-session` and `/clear` start a new ID.
- **Codex**: ID is the UUIDv7 in `~/.codex/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl` (or `session_meta.payload.id`); `codex resume` keeps it. Turn completion arrives via `notify = [...]` in `~/.codex/config.toml`, which `reg-a install codex` sets.

## Commands

```sh
uv sync                                   # dev environment
uv run pytest                             # all tests (hermetic: no LLM, no TeX, no real ~)
uv run ruff format                        # format (CI fails on unformatted code)
uv run ruff check                         # lint
uv run reg-a --help                    # CLI; each subcommand has --help
uv tool install -e .                      # put `reg-a` on PATH (needed before `install`)
reg-a sync claude <transcript.jsonl>   # run a sync by hand (foreground)
reg-a convert-to-pdf <id> --tex-only   # render .tex without TeX Live
```

**CI** (`.github/workflows/ci.yml`, GitHub Actions, on push to `master`, every PR, and manual dispatch) runs three jobs: *Format & lint* (`ruff format --check`, `ruff check`), *Tests* (`pytest`, JUnit report uploaded), and *Build package* (`uv build`, installs the wheel into a clean venv and runs `reg-a --help`, uploads `dist/`). Build runs only when the first two pass. All jobs use `uv sync --locked`, so after changing dependencies, commit the updated `uv.lock`. There is no publish step; releasing to PyPI is not set up. Before every commit, run the same checks locally: `uv run ruff format && uv run ruff check && uv run pytest`.

Python ≥ 3.14, dependencies: httpx, jinja2, pydantic. Runtime prerequisites (not installed on the dev machine yet): Ollama (`ollama pull qwen3:8b`) and TeX Live (Arch: `texlive-basic texlive-latexextra texlive-langextra texlive-binextra texlive-fontsrecommended`).

## Gotchas

- **Hooks must return fast and never fail.** `cmd_hook` loads config inside its own try/except and always exits 0; a broken hook must not disturb the agent. The worker is detached (`start_new_session`) and waits `--delay 1` so the agent finishes flushing the transcript. Worker output goes to `<output_dir>/reg-a.log`; look there first when nothing is recorded.
- **Installers embed an absolute path.** `hooks.reg_a_command()` resolves `reg-a` via `shutil.which` at install time (fallback: `python -m reg_a`). Run `install` from the `uv tool install -e .` binary, not `uv run`, or the hook points into `.venv`. `install codex` refuses to overwrite an existing different `notify`; both installers back up the file (`.bak`) before writing.
- **Codex notify payload keys are inferred, not confirmed.** `codex_rollout_from_notify` tries `thread-id`, `thread_id`, `session-id`, `session_id`, ignores non-`agent-turn-complete` types, and falls back to the newest rollout file. Confirm against a real run (to-do 3).
- **Transcript formats are unversioned.** Both JSONL formats change between releases: parse leniently, skip unknown entry types and undecodable lines (a partially flushed last line is normal), and keep fixture transcripts in `tests/fixtures/`.
- **Truncate before summarizing.** Tool outputs and diffs can be huge; `Excerpt` clips each piece and the whole to `max_input_chars`.
- **Concurrent writes.** Two hooks can fire together: `sync` holds `store.session_lock` (flock on a `.lock` file beside the JSON) across read-modify-write, and `save_session` writes temp file + `os.replace`. It saves after every turn, so a long backfill keeps its progress.
- **Corrupt session JSON** is renamed `*.corrupt-<timestamp>` and the session rebuilt from the transcript (summaries are regenerated).
- **LaTeX.** Every session value in the template must go through the `tex`, `tt` or `paragraphs` filter. `tex()` escapes `\ { } $ & # % _ ~ ^`, drops control characters, and breaks ligatures (`--` → en dash, `` ` ``, `''`, `<<`, `>>`, `!``, `?``) with `{}`. Use `\item{}` and `\newline{}` (not `\\`) before user text so a leading `[` or `*` isn't read as an argument. Build with `lualatex` + `fontspec` + `babel` with `shorthands=off` (Polish babel shorthands break on `"`); Latin Modern fonts come from `texlive-fontsrecommended`.
- **Secrets.** Prompts and tool output can contain keys or `.env` contents; output stays local, and fixtures are scrubbed before committing.

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
- **Claude Code**: launch through the Agent tool with `subagent_type` set to the agent's name. Agents in `.claude/agents/` only register after Claude Code restarts; until then, launch a `general-purpose` agent and give it the role file's body as its instructions.
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
