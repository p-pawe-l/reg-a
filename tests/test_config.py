import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from agentlog.config import EXAMPLE_CONFIG, Config, default_config_path, load_config


def test_missing_file_gives_defaults(tmp_path):
    cfg = load_config(tmp_path / "absent.toml")
    assert cfg == Config()
    assert cfg.language == "en"
    assert cfg.llm.model == "qwen3:8b"
    assert cfg.llm.retries == 1


def test_paths_are_expanded_against_home(hermetic_home):
    cfg = Config()
    assert cfg.output_dir == hermetic_home / "agentlog" / "sessions"
    assert cfg.claude_home == hermetic_home / ".claude"
    assert cfg.codex_home == hermetic_home / ".codex"


def test_toml_values_override_defaults(tmp_path, hermetic_home):
    path = tmp_path / "c.toml"
    path.write_text('language = "pl"\noutput_dir = "~/out"\n[llm]\nmodel = "m"\ntimeout_s = 5\n')
    cfg = load_config(path)
    assert cfg.language == "pl"
    assert cfg.output_dir == hermetic_home / "out"
    assert cfg.llm.model == "m"
    assert cfg.llm.timeout_s == 5
    assert cfg.llm.base_url == "http://localhost:11434/v1"


def test_example_config_is_valid_and_loadable(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text(EXAMPLE_CONFIG)
    cfg = load_config(path)
    assert cfg.language == "pl"
    assert tomllib.loads(EXAMPLE_CONFIG)["llm"]["max_input_chars"] == cfg.llm.max_input_chars


@pytest.mark.parametrize(
    "text",
    [
        "unknown_key = 1\n",
        "[llm]\nbogus = 1\n",
        "[llm]\nmax_input_chars = 0\n",
        "[llm]\ntimeout_s = -1\n",
        "[llm]\nretries = -1\n",
    ],
)
def test_invalid_config_is_rejected(tmp_path, text):
    path = tmp_path / "c.toml"
    path.write_text(text)
    with pytest.raises(ValidationError):
        load_config(path)


def test_malformed_toml_raises(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text("language = \n")
    with pytest.raises(tomllib.TOMLDecodeError):
        load_config(path)


def test_default_config_path_honours_env_var(monkeypatch, hermetic_home):
    monkeypatch.setenv("AGENTLOG_CONFIG", "~/custom.toml")
    assert default_config_path() == hermetic_home / "custom.toml"
    monkeypatch.delenv("AGENTLOG_CONFIG")
    assert default_config_path() == hermetic_home / ".config" / "agentlog" / "config.toml"


def test_load_config_without_path_uses_env_location(tmp_path, monkeypatch):
    path = tmp_path / "env.toml"
    path.write_text('language = "de"\n')
    monkeypatch.setenv("AGENTLOG_CONFIG", str(path))
    assert load_config().language == "de"
    assert isinstance(load_config().output_dir, Path)
