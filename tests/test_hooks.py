import json
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from agentlog import hooks

FAKE_EXE = "/opt/bin/agentlog"


@pytest.fixture
def fake_which(monkeypatch):
    monkeypatch.setattr(hooks.shutil, "which", lambda name: FAKE_EXE)


@pytest.fixture
def popen_calls(monkeypatch):
    calls = []

    def fake_popen(args, **kwargs):
        calls.append((args, kwargs))

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    return calls


def test_agentlog_command_prefers_installed_executable(fake_which):
    assert hooks.agentlog_command() == [FAKE_EXE]


def test_agentlog_command_falls_back_to_python_module(monkeypatch):
    monkeypatch.setattr(hooks.shutil, "which", lambda name: None)
    assert hooks.agentlog_command() == [sys.executable, "-m", "agentlog"]


def test_spawn_sync_launches_detached_worker_with_delay(cfg, fake_which, popen_calls, tmp_path):
    cfg_file = tmp_path / "c.toml"
    hooks.spawn_sync("codex", Path("/t/rollout.jsonl"), cfg, cfg_file)
    (args, kwargs), = popen_calls
    assert args == [FAKE_EXE, "--config", str(cfg_file.resolve()), "sync", "codex",
                    "/t/rollout.jsonl", "--delay", str(hooks.WORKER_DELAY_S), "--retry-failed"]
    assert kwargs["start_new_session"] is True
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert kwargs["stdout"].name == str(cfg.output_dir / "agentlog.log")
    assert (cfg.output_dir / "agentlog.log").exists()


def test_spawn_sync_without_config_path_omits_flag(cfg, fake_which, popen_calls):
    hooks.spawn_sync("claude-code", Path("/t/x.jsonl"), cfg, None)
    args = popen_calls[0][0]
    assert "--config" not in args
    assert args[1:4] == ["sync", "claude-code", "/t/x.jsonl"]


def test_claude_hook_payload_gives_transcript_path(hermetic_home):
    assert hooks.claude_transcript_from_hook(
        json.dumps({"session_id": "s", "transcript_path": "~/p/s.jsonl"})
    ) == hermetic_home / "p" / "s.jsonl"


@pytest.mark.parametrize("stdin", ["", "not json", "[1]", json.dumps({"session_id": "s"}),
                                   json.dumps({"transcript_path": ""})])
def test_claude_hook_bad_payload_gives_none(stdin):
    assert hooks.claude_transcript_from_hook(stdin) is None


def _rollout(codex_home, sid):
    d = codex_home / "sessions" / "2026" / "01" / "02"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-01-02T10-00-00-{sid}.jsonl"
    p.write_text("{}\n")
    return p


@pytest.mark.parametrize("key", ["thread-id", "thread_id", "session-id", "session_id"])
def test_codex_notify_finds_rollout_by_thread_id(cfg, key):
    target = _rollout(cfg.codex_home, "id-a")
    _rollout(cfg.codex_home, "id-b")
    payload = json.dumps({"type": "agent-turn-complete", key: "id-a"})
    assert hooks.codex_rollout_from_notify(payload, cfg) == target


def test_codex_notify_ignores_other_event_types(cfg):
    _rollout(cfg.codex_home, "id-a")
    assert hooks.codex_rollout_from_notify(json.dumps({"type": "approval-requested",
                                                       "thread-id": "id-a"}), cfg) is None


@pytest.mark.parametrize("arg", [None, "", "not json", json.dumps({"type": "agent-turn-complete"})])
def test_codex_notify_without_id_falls_back_to_latest_rollout(cfg, arg):
    target = _rollout(cfg.codex_home, "id-a")
    assert hooks.codex_rollout_from_notify(arg, cfg) == target


def test_codex_notify_with_unknown_id_gives_none(cfg):
    _rollout(cfg.codex_home, "id-a")
    payload = json.dumps({"type": "agent-turn-complete", "thread-id": "other"})
    assert hooks.codex_rollout_from_notify(payload, cfg) is None


# --- installers -------------------------------------------------------------------------------


def test_install_claude_creates_settings_with_stop_and_session_end_hooks(tmp_path, fake_which):
    settings = tmp_path / "claude" / "settings.json"
    msg = hooks.install_claude(settings)
    assert "Stop" in msg and "SessionEnd" in msg
    data = json.loads(settings.read_text())
    for event in hooks.HOOK_EVENTS:
        (group,) = data["hooks"][event]
        assert group["hooks"] == [{"type": "command", "command": f"{FAKE_EXE} hook claude",
                                   "timeout": 10}]
    assert not settings.with_suffix(".json.bak").exists()


def test_install_claude_preserves_existing_settings_and_backs_up(tmp_path, fake_which):
    settings = tmp_path / "settings.json"
    other = {"hooks": [{"type": "command", "command": "notify-send done"}]}
    original = {"model": "x", "hooks": {"Stop": [other]}}
    settings.write_text(json.dumps(original))
    hooks.install_claude(settings)
    data = json.loads(settings.read_text())
    assert data["model"] == "x"
    assert data["hooks"]["Stop"][0] == other
    assert len(data["hooks"]["Stop"]) == 2
    assert json.loads(settings.with_suffix(".json.bak").read_text()) == original


def test_install_claude_twice_is_a_no_op(tmp_path, fake_which):
    settings = tmp_path / "settings.json"
    hooks.install_claude(settings)
    before = settings.read_text()
    assert "already present" in hooks.install_claude(settings)
    assert settings.read_text() == before


def test_install_codex_prepends_notify_before_tables(tmp_path, fake_which):
    config = tmp_path / "codex" / "config.toml"
    config.parent.mkdir()
    config.write_text('model = "gpt-x"\n\n[profiles.fast]\nmodel = "gpt-y"\n')
    msg = hooks.install_codex(config)
    assert msg.startswith("set notify")
    data = tomllib.loads(config.read_text())
    assert data["notify"] == [FAKE_EXE, "hook", "codex"]
    assert data["model"] == "gpt-x"
    assert data["profiles"]["fast"]["model"] == "gpt-y"
    assert config.with_suffix(".toml.bak").read_text().startswith('model = "gpt-x"')


def test_install_codex_creates_missing_config(tmp_path, fake_which):
    config = tmp_path / "new" / "config.toml"
    hooks.install_codex(config)
    assert tomllib.loads(config.read_text()) == {"notify": [FAKE_EXE, "hook", "codex"]}


def test_install_codex_twice_is_a_no_op(tmp_path, fake_which):
    config = tmp_path / "config.toml"
    hooks.install_codex(config)
    before = config.read_text()
    assert "already set" in hooks.install_codex(config)
    assert config.read_text() == before


def test_install_codex_refuses_to_overwrite_foreign_notify(tmp_path, fake_which):
    config = tmp_path / "config.toml"
    config.write_text('notify = ["my-notifier"]\n')
    with pytest.raises(SystemExit, match="won't overwrite"):
        hooks.install_codex(config)
    assert config.read_text() == 'notify = ["my-notifier"]\n'
