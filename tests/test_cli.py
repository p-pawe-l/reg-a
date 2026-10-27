import io
import json
import tomllib

import pytest
from conftest import CODEX_FIXTURE, CODEX_SESSION_ID, make_summary

from agentlog import cli, hooks
from agentlog import sync as sync_mod
from agentlog.config import EXAMPLE_CONFIG
from agentlog.store import load_session


@pytest.fixture
def cfg_file(tmp_path):
    path = tmp_path / "agentlog.toml"
    path.write_text(
        f'output_dir = "{tmp_path / "sessions"}"\n'
        f'claude_home = "{tmp_path / "claude"}"\n'
        f'codex_home = "{tmp_path / "codex"}"\n'
        '[llm]\nbase_url = "http://llm.test/v1"\n'
    )
    return path


@pytest.fixture
def spawned(monkeypatch):
    calls = []
    monkeypatch.setattr(hooks, "spawn_sync", lambda *a: calls.append(a))
    return calls


@pytest.fixture
def fake_summarizer(monkeypatch):
    class FakeSummarizer:
        def __init__(self, cfg, language, client=None):
            self.language = language

        def summarize(self, turn, tool):
            return make_summary(f"{self.language}: {turn.turn_id}")

    monkeypatch.setattr(sync_mod, "Summarizer", FakeSummarizer)


def test_tool_aliases_are_normalized():
    p = cli.build_parser()
    assert p.parse_args(["hook", "claude"]).tool == "claude-code"
    assert p.parse_args(["hook", "claude-code"]).tool == "claude-code"
    assert p.parse_args(["sync", "codex", "x.jsonl"]).tool == "codex"


def test_unknown_tool_is_a_usage_error(capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["hook", "cursor"])
    assert e.value.code == 2
    assert "tool must be" in capsys.readouterr().err


def test_claude_hook_spawns_sync_for_existing_transcript(tmp_path, cfg_file, spawned,
                                                         monkeypatch):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(transcript)})))
    assert cli.main(["--config", str(cfg_file), "hook", "claude"]) == 0
    (tool, path, cfg, config_path), = spawned
    assert (tool, path, config_path) == ("claude-code", transcript, cfg_file)
    assert cfg.output_dir == tmp_path / "sessions"


def test_claude_hook_with_missing_transcript_does_not_spawn(tmp_path, spawned, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(
        {"transcript_path": str(tmp_path / "absent.jsonl")})))
    assert cli.main(["hook", "claude"]) == 0
    assert spawned == []


def test_claude_hook_with_garbage_stdin_exits_zero(spawned, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("garbage"))
    assert cli.main(["hook", "claude"]) == 0
    assert spawned == []


def test_hook_exits_zero_even_when_spawn_fails(tmp_path, monkeypatch):
    transcript = tmp_path / "s.jsonl"
    transcript.write_text("{}\n")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": str(transcript)})))

    def boom(*a):
        raise OSError("cannot fork")

    monkeypatch.setattr(hooks, "spawn_sync", boom)
    assert cli.main(["hook", "claude"]) == 0


def test_codex_hook_resolves_rollout_from_notify_payload(tmp_path, cfg_file, spawned):
    d = tmp_path / "codex" / "sessions" / "2026" / "01" / "02"
    d.mkdir(parents=True)
    rollout = d / "rollout-2026-01-02T10-00-00-abc.jsonl"
    rollout.write_text("{}\n")
    payload = json.dumps({"type": "agent-turn-complete", "thread-id": "abc"})
    assert cli.main(["--config", str(cfg_file), "hook", "codex", payload]) == 0
    assert spawned[0][:2] == ("codex", rollout)


def test_sync_command_writes_session_and_prints_summary(cfg_file, fake_summarizer, capsys,
                                                        tmp_path):
    assert cli.main(["--config", str(cfg_file), "sync", "codex", str(CODEX_FIXTURE)]) == 0
    out_path = tmp_path / "sessions" / f"codex-{CODEX_SESSION_ID}.json"
    assert f"{out_path}  (2 turns, 0 without summary)" in capsys.readouterr().out
    assert load_session(out_path).turns[0].summary.summary == "en: turn-1"


def test_sync_command_honours_delay(cfg_file, fake_summarizer, monkeypatch):
    slept = []
    monkeypatch.setattr(cli.time, "sleep", slept.append)
    cli.main(["--config", str(cfg_file), "sync", "codex", str(CODEX_FIXTURE), "--delay", "1.5"])
    assert slept == [1.5]


def test_sync_command_retry_failed_resummarizes(cfg_file, monkeypatch, tmp_path, capsys):
    class Failing:
        def __init__(self, *a, **k):
            pass

        def summarize(self, turn, tool):
            raise sync_mod.SummaryError("down")

    monkeypatch.setattr(sync_mod, "Summarizer", Failing)
    cli.main(["--config", str(cfg_file), "sync", "codex", str(CODEX_FIXTURE)])
    assert "2 without summary" in capsys.readouterr().out

    class Working(Failing):
        def summarize(self, turn, tool):
            return make_summary()

    monkeypatch.setattr(sync_mod, "Summarizer", Working)
    cli.main(["--config", str(cfg_file), "sync", "codex", str(CODEX_FIXTURE)])
    assert "2 without summary" in capsys.readouterr().out
    cli.main(["--config", str(cfg_file), "sync", "codex", str(CODEX_FIXTURE), "--retry-failed"])
    assert "0 without summary" in capsys.readouterr().out


def test_list_command_prints_sessions(cfg_file, fake_summarizer, capsys):
    cli.main(["--config", str(cfg_file), "sync", "codex", str(CODEX_FIXTURE)])
    capsys.readouterr()
    assert cli.main(["--config", str(cfg_file), "list"]) == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("2026-01-02 09:59  codex")
    assert CODEX_SESSION_ID in line
    assert "2 turns" in line
    assert line.endswith("/work/demo")


def test_list_command_with_no_sessions_prints_nothing(cfg_file, capsys):
    assert cli.main(["--config", str(cfg_file), "list"]) == 0
    assert capsys.readouterr().out == ""


def test_install_claude_writes_user_settings_under_claude_home(cfg_file, tmp_path, capsys):
    assert cli.main(["--config", str(cfg_file), "install", "claude"]) == 0
    data = json.loads((tmp_path / "claude" / "settings.json").read_text())
    assert set(data["hooks"]) == set(hooks.HOOK_EVENTS)


def test_install_claude_project_writes_local_settings(cfg_file, tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    cli.main(["--config", str(cfg_file), "install", "claude", "--project"])
    assert (proj / ".claude" / "settings.json").exists()
    assert not (tmp_path / "claude" / "settings.json").exists()


def test_install_codex_writes_codex_config(cfg_file, tmp_path):
    cli.main(["--config", str(cfg_file), "install", "codex"])
    data = tomllib.loads((tmp_path / "codex" / "config.toml").read_text())
    assert data["notify"][-2:] == ["hook", "codex"]


def test_init_config_writes_example_to_default_path(hermetic_home, capsys):
    target = hermetic_home / "agentlog.toml"  # $AGENTLOG_CONFIG from conftest
    assert cli.main(["init-config"]) == 0
    assert target.read_text() == EXAMPLE_CONFIG
    assert cli.main(["init-config"]) == 1
    assert "already exists" in capsys.readouterr().out


def test_init_config_writes_to_explicit_config_path(tmp_path, hermetic_home):
    target = tmp_path / "explicit" / "config.toml"
    assert cli.main(["--config", str(target), "init-config"]) == 0
    assert target.read_text() == EXAMPLE_CONFIG


def test_hook_exits_zero_with_invalid_config_file(tmp_path, spawned, monkeypatch):
    bad = tmp_path / "bad.toml"
    bad.write_text("not = [valid toml")
    monkeypatch.setattr("sys.stdin", io.StringIO("{}"))
    assert cli.main(["--config", str(bad), "hook", "claude"]) == 0
    assert spawned == []


def test_sync_on_empty_transcript_records_nothing_and_exits_zero(tmp_path, cfg_file, capsys):
    transcript = tmp_path / "empty.jsonl"
    transcript.write_text("")
    assert cli.main(["--config", str(cfg_file), "sync", "claude", str(transcript)]) == 0
    assert "nothing to record yet" in capsys.readouterr().out
