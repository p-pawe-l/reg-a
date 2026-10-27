"""Hook entry points and installers.

The hook process must return immediately so the agent never waits on the LLM:
it resolves the transcript path, spawns a detached `reg-a sync` worker,
and exits 0. Worker output goes to `<output_dir>/reg-a.log`.
"""

import json
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from reg_a.config import Config
from reg_a.models import Tool
from reg_a.parsers.codex import find_rollout

HOOK_EVENTS = ("Stop", "SessionEnd")
WORKER_DELAY_S = 1.0  # let the agent finish flushing the transcript before we parse it


def reg_a_command() -> list[str]:
    exe = shutil.which("reg-a")
    return [exe] if exe else [sys.executable, "-m", "reg_a"]


def spawn_sync(tool: Tool, transcript: Path, cfg: Config, config_path: Path | None) -> None:
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    config_args = ["--config", str(config_path.resolve())] if config_path else []
    with open(cfg.output_dir / "reg-a.log", "a") as log:
        subprocess.Popen(
            [
                *reg_a_command(),
                *config_args,
                "sync",
                tool,
                str(transcript),
                "--delay",
                str(WORKER_DELAY_S),
                "--retry-failed",
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )


def claude_transcript_from_hook(stdin_text: str) -> Path | None:
    try:
        payload = json.loads(stdin_text)
    except json.JSONDecodeError:
        return None
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    return Path(path).expanduser() if path else None


def codex_rollout_from_notify(arg: str | None, cfg: Config) -> Path | None:
    """Codex calls `notify` with a JSON payload as the last argument."""
    session_id = None
    if arg:
        try:
            payload = json.loads(arg)
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            if payload.get("type") not in (None, "agent-turn-complete"):
                return None
            session_id = next(
                (
                    payload[k]
                    for k in ("thread-id", "thread_id", "session-id", "session_id")
                    if payload.get(k)
                ),
                None,
            )
    return find_rollout(cfg.codex_home, session_id)


# --- installers -----------------------------------------------------------------------------


def _is_our_claude_hook(command: str) -> bool:
    # Matches both the `reg-a hook claude` and `python -m reg_a hook claude` forms.
    argv = shlex.split(command) if command else []
    return argv[-2:] == ["hook", "claude"] and any(
        Path(a).name == "reg-a" or a == "reg_a" for a in argv[:-2]
    )


def install_claude(settings_path: Path) -> str:
    settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
    command = shlex.join([*reg_a_command(), "hook", "claude"])
    hooks = settings.setdefault("hooks", {})
    added = []
    for event in HOOK_EVENTS:
        groups = hooks.setdefault(event, [])
        if any(
            _is_our_claude_hook(h.get("command", "")) for g in groups for h in g.get("hooks", [])
        ):
            continue
        groups.append({"hooks": [{"type": "command", "command": command, "timeout": 10}]})
        added.append(event)
    if not added:
        return f"reg-a hooks already present in {settings_path}"
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        shutil.copy2(settings_path, settings_path.with_suffix(".json.bak"))
    settings_path.write_text(json.dumps(settings, indent=2) + "\n")
    return f"added {', '.join(added)} hook(s) to {settings_path}"


def install_codex(config_path: Path) -> str:
    text = config_path.read_text() if config_path.exists() else ""
    current = tomllib.loads(text).get("notify") if text else None
    notify = [*reg_a_command(), "hook", "codex"]
    if current == notify:
        return f"reg-a notify already set in {config_path}"
    if current is not None:
        raise SystemExit(
            f"{config_path} already sets notify = {current!r}; reg-a won't overwrite it. "
            f"Replace it by hand with: notify = {json.dumps(notify)}"
        )
    # `notify` is a top-level key, so it must precede the first [table].
    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        shutil.copy2(config_path, config_path.with_suffix(".toml.bak"))
    config_path.write_text(f"notify = {json.dumps(notify)}\n{text}")
    return f"set notify in {config_path}"
