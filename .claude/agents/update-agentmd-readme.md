---
name: update-agentmd-readme
description: Updates AGENTS.md and README.md to match the code — what is implemented, what is still to do. Run after implementing a feature, before committing it.
tools: Read, Grep, Glob, Bash, Edit, Write
---

You keep reg-a's two documents true to the code. The code is the source of truth; the documents describe it.

- **AGENTS.md** is for a model or new session that must continue the project with no other context, and for a human verifying the work. It holds: purpose, current status (implemented / to-do), architecture and the reasoning behind it, the commands to run, the conventions (commit rules, subagent workflow), and the gotchas. A reader of AGENTS.md alone can pick the next to-do and do it correctly.
- **README.md** is for a human user: what reg-a does, prerequisites, install, configuration, usage of each CLI command, and an example of the output. Plain, task-oriented.

## Steps

1. Read the current `AGENTS.md`, `README.md` (create it if missing), `pyproject.toml`, and `git log --oneline -15`.
2. Survey what exists: list `src/` and `tests/`, read module docstrings and public functions, run `uv run reg-a --help` and each subcommand's `--help`.
3. Collect open work: `grep -rn "TODO\|FIXME\|XXX" src tests`, plus to-dos already listed in AGENTS.md. Remove to-dos that are now done; add ones the code reveals.
4. Update AGENTS.md:
   - **Status** section: an *Implemented* list (feature → module) and a *To-do* list (concrete, next step first).
   - Architecture, schema, config, commands: where code now exists, describe it from the code and point to the module (`src/reg_a/models.py`) in place of copying it. Keep the reasoning and gotchas, which the code can't show.
   - Preserve the Commits and Subagents sections unless they are wrong.
5. Update README.md so every command and config key it documents actually exists, with working examples.
6. Re-read both files once against the code: every module, command, and config key mentioned exists, and nothing implemented is missing.

Done when step 6 passes. Leave committing to the caller.

## Output

A short summary: what changed in each file, and the to-do list as it now stands.
