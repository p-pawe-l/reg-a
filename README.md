# reg-a

reg-a keeps a log of your Claude Code and Codex sessions. After every turn it records your prompt and what the agent did (files changed, commands run, time spent, model), asks a local LLM for a short summary, and stores everything in one JSON file per session. You can render any session to a PDF report.

Everything stays on your machine. The summaries come from a model you host yourself (Ollama, llama.cpp, vLLM or another OpenAI-compatible server).

> Status: early. The tests pass, but the PDF build and real-LLM summaries haven't been checked end to end yet.

## Prerequisites

- **Python 3.14+** and [**uv**](https://docs.astral.sh/uv/).
- **A local LLM server** with an OpenAI-compatible API. With Ollama:
  ```sh
  ollama pull qwen3:8b
  ```
- **TeX Live** with LuaLaTeX and latexmk, only for PDF reports. On Arch:
  ```sh
  sudo pacman -S texlive-basic texlive-latexextra texlive-langextra texlive-binextra texlive-fontsrecommended
  ```

## Install

From the repository:

```sh
uv tool install -e .
reg-a --help
```

This puts `reg-a` on your `PATH`. The hooks call that binary, so install it this way before running `reg-a install`.

## Configure

```sh
reg-a init-config
```

This writes `~/.config/reg-a/config.toml` (or the path in `$REG_A_CONFIG`, or `--config PATH`). Without a config file, reg-a uses the defaults below.

```toml
language = "pl"                          # summary + report language: "pl", "en", or a language name
output_dir = "~/reg-a/sessions"
# claude_home = "~/.claude"
# codex_home = "~/.codex"                # default: $CODEX_HOME or ~/.codex

[llm]
base_url = "http://localhost:11434/v1"   # any OpenAI-compatible server
model = "qwen3:8b"
# api_key_env = "MY_LLM_KEY"             # env var holding an API key, if the server needs one
max_input_chars = 20000
timeout_s = 120
retries = 1
# temperature = 0.2
```

| Key | Default | Meaning |
|---|---|---|
| `language` | `"en"` | Language of the summaries and report labels. Report labels exist for `pl` and `en`; other languages get English labels. |
| `output_dir` | `~/reg-a/sessions` | Where session JSON, reports and the log go. |
| `claude_home` | `~/.claude` | Claude Code's config directory. |
| `codex_home` | `$CODEX_HOME` or `~/.codex` | Codex's config directory. |
| `llm.base_url` | `http://localhost:11434/v1` | OpenAI-compatible API base URL. |
| `llm.model` | `qwen3:8b` | Model name sent to the server. |
| `llm.api_key_env` | none | Name of an environment variable holding a bearer token. |
| `llm.max_input_chars` | `20000` | Cap on the turn log sent to the model. |
| `llm.timeout_s` | `120` | Request timeout in seconds. |
| `llm.retries` | `1` | Extra attempts when the model's reply is invalid or the request fails. |
| `llm.temperature` | `0.2` | Sampling temperature. |

Unknown keys are an error, so typos show up right away.

## Hook it up

```sh
reg-a install claude            # adds Stop + SessionEnd hooks to ~/.claude/settings.json
reg-a install claude --project  # ...or to ./.claude/settings.json
reg-a install codex             # sets notify = [...] in ~/.codex/config.toml
```

Both commands back up the file they change (`.bak`) and do nothing if reg-a is already set up. `install codex` won't replace an existing `notify` setting; it prints the line to put in by hand.

From then on, each finished turn runs `reg-a hook`, which starts `reg-a sync` in the background and returns immediately, so the agent never waits for the LLM. The background worker writes to `<output_dir>/reg-a.log`.

If the LLM is down, the turn is still recorded, without a summary. The next hook in that session tries to summarize it again.

## Usage

### `reg-a sync <claude|codex> <transcript>`

Brings a session's JSON file up to date with its transcript. The hooks run this for you; run it yourself to record a session from before you installed the hooks, or to retry.

```sh
reg-a sync claude ~/.claude/projects/-home-me-myproject/<session-id>.jsonl
reg-a sync codex ~/.codex/sessions/2026/10/27/rollout-<timestamp>-<session-id>.jsonl
```

Running it again is safe: turns already summarized are kept, and only new or changed turns go to the LLM.

- `--retry-failed`: also re-summarize stored turns whose summary failed (new turns go first; if the LLM server is unreachable, the run stops calling it after the first failure). Hooks always pass this flag.
- `--delay SECONDS`: wait before parsing.

### `reg-a list`

Lists recorded sessions, most recently updated first: start time (UTC), tool, session id, number of turns, and title or working directory.

```
2026-10-27 18:41  claude-code  4f9c2e1a-...  12 turns  Session logging tool
```

### `reg-a convert-to-pdf <session | --all>`

Renders a session to a LaTeX report and builds the PDF. `session` is a session id, a unique prefix of one, or a path to a session JSON file.

```sh
reg-a convert-to-pdf 4f9c2e1a
reg-a convert-to-pdf --all
reg-a convert-to-pdf 4f9c2e1a -o ~/reports
reg-a convert-to-pdf 4f9c2e1a --tex-only   # write the .tex only, no TeX Live needed
```

Output goes to `<output_dir>/reports/` unless you pass `-o`. The report has an overview (tool, directory, total work time, models), a table of all turns, and a detail section per turn with the prompt, summary, actions, open issues, files changed and commands run.

### Other commands

- `reg-a init-config`: write the example config (see above).
- `reg-a hook <claude|codex> [payload]`: the hook entry point. Claude Code and Codex call it; you don't need to.
- `--config PATH` (before the subcommand) uses another config file with any command.

## Output

```
~/reg-a/sessions/
├── claude-code-<session-id>.json    # one file per session
├── codex-<session-id>.json
├── *.lock                           # lock files used while writing
├── reg-a.log                     # background worker log
└── reports/
    ├── claude-code-<session-id>.tex
    └── claude-code-<session-id>.pdf
```

A damaged session file is moved aside as `*.json.corrupt-<timestamp>` and rebuilt from the transcript.

A session file looks like this (shortened):

```json
{
  "session_id": "11111111-2222-4333-8444-555555555555",
  "tool": "claude-code",
  "title": "Greeting function",
  "cwd": "/work/demo",
  "started_at": "2026-01-02T10:00:00Z",
  "transcript_path": "/home/me/.claude/projects/-work-demo/11111111-2222-4333-8444-555555555555.jsonl",
  "turns": [
    {
      "turn_id": "u1",
      "index": 1,
      "user_prompt": "Add a greeting function to hello.py",
      "started_at": "2026-01-02T10:00:00Z",
      "finished_at": "2026-01-02T10:00:11Z",
      "work_time_s": 9.5,
      "model": "claude-sonnet-4-5",
      "interrupted": false,
      "files_changed": ["/work/demo/hello.py"],
      "commands_run": ["python hello.py"],
      "final_message": "Done: added greet().",
      "summary": {
        "summary": "Added a greet(name) function to hello.py and ran the script to check it.",
        "actions": ["edited hello.py", "ran python hello.py"],
        "outcome": "success",
        "open_issues": []
      },
      "summary_error": null
    }
  ]
}
```

Everything except `summary` comes straight from the transcript. `summary` is written by the LLM; if summarizing failed, it is `null` and `summary_error` says why. `outcome` is one of `success`, `partial`, `failed`, `question`.

## Development

```sh
uv sync
uv run ruff format      # format
uv run ruff check       # lint
uv run pytest           # tests
```

GitHub Actions (`.github/workflows/ci.yml`) runs the format check, lint and tests on every push to `master` and every pull request, then builds the wheel and smoke-tests it.

See `AGENTS.md` for the architecture, design decisions and open to-dos.
