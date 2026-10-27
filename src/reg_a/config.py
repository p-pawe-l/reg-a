"""Configuration loaded from TOML.

Location: $REG_A_CONFIG, else ~/.config/reg-a/config.toml. A missing
file means all defaults.
"""

import os
import tomllib
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator


class LLMConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    base_url: str = "http://localhost:11434/v1"
    model: str = "qwen3:8b"
    api_key_env: str | None = None  # name of an env var holding the key, if the server needs one
    max_input_chars: int = Field(20_000, gt=0)
    timeout_s: float = Field(120, gt=0)
    retries: int = Field(1, ge=0)
    temperature: float = 0.2


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid", validate_default=True)

    language: str = "en"
    output_dir: Path = Path("~/reg-a/sessions")
    claude_home: Path = Path("~/.claude")
    codex_home: Path = Path(os.environ.get("CODEX_HOME", "~/.codex"))
    llm: LLMConfig = LLMConfig()

    @field_validator("output_dir", "claude_home", "codex_home")
    @classmethod
    def _expand(cls, v: Path) -> Path:
        return v.expanduser()


def default_config_path() -> Path:
    env = os.environ.get("REG_A_CONFIG")
    return Path(env).expanduser() if env else Path("~/.config/reg-a/config.toml").expanduser()


def load_config(path: Path | None = None) -> Config:
    path = path or default_config_path()
    if not path.exists():
        return Config()
    with path.open("rb") as f:
        return Config.model_validate(tomllib.load(f))


EXAMPLE_CONFIG = """\
language = "pl"                          # summary + report language: "pl", "en", or a language name
output_dir = "~/reg-a/sessions"

[llm]
base_url = "http://localhost:11434/v1"   # any OpenAI-compatible server (Ollama, llama.cpp, vLLM)
model = "qwen3:8b"
# api_key_env = "MY_LLM_KEY"             # env var holding an API key, if the server needs one
max_input_chars = 20000
timeout_s = 120
retries = 1
"""
