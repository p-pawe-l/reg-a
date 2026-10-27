import json
from pathlib import Path

import pytest

from reg_a.config import Config, LLMConfig
from reg_a.models import TurnSummary

FIXTURES = Path(__file__).parent / "fixtures"
CLAUDE_FIXTURE = FIXTURES / "claude_session.jsonl"
CLAUDE_SESSION_ID = "11111111-2222-4333-8444-555555555555"
CODEX_SESSION_ID = "01990000-0000-7000-8000-000000000001"
CODEX_FIXTURE = FIXTURES / f"rollout-2026-01-02T10-00-00-{CODEX_SESSION_ID}.jsonl"


@pytest.fixture(autouse=True)
def hermetic_home(tmp_path, monkeypatch):
    """Never touch the real ~/.claude, ~/.codex or ~/.config."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("REG_A_CONFIG", str(home / "reg-a.toml"))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    return home


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config(
        output_dir=tmp_path / "sessions",
        claude_home=tmp_path / "claude",
        codex_home=tmp_path / "codex",
        llm=LLMConfig(base_url="http://llm.test/v1"),
    )


def make_summary(text: str = "Did the thing.") -> TurnSummary:
    return TurnSummary(summary=text, actions=["acted"], outcome="success")


def write_jsonl(path: Path, entries: list[dict | str]) -> Path:
    path.write_text(
        "\n".join(e if isinstance(e, str) else json.dumps(e) for e in entries) + "\n",
        encoding="utf-8",
    )
    return path
