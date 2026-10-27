"""Turn summaries from a local LLM over an OpenAI-compatible chat API.

The model's reply is constrained to the `TurnSummary` JSON schema and then
validated by pydantic; on failure we retry, then give up with an error string
so the caller can still store the turn.
"""

import json
import os

import httpx
from pydantic import ValidationError

from agentlog.config import LLMConfig
from agentlog.models import ParsedTurn, TurnSummary

LANGUAGE_NAMES = {
    "pl": "Polish",
    "en": "English",
    "de": "German",
    "fr": "French",
    "es": "Spanish",
    "uk": "Ukrainian",
}

SYSTEM_PROMPT = """\
You summarize one turn of a session between a user and an AI coding agent \
({tool}). You get the user's prompt and a condensed log of what the agent did: \
its messages, tool calls, commands, file changes and (truncated) results.

Report what the agent actually did, not what it planned. Be concrete: name \
files, commands, and decisions. Write every text value in {language}. \
Reply with JSON only, matching the given schema."""


class SummaryError(Exception):
    def __init__(self, message: str, unavailable: bool = False):
        super().__init__(message)
        self.unavailable = unavailable  # the LLM server itself failed, not just this turn


def language_name(code: str) -> str:
    return LANGUAGE_NAMES.get(code.lower(), code)


def build_messages(turn: ParsedTurn, tool: str, language: str) -> list[dict]:
    schema = json.dumps(TurnSummary.llm_schema())
    return [
        {
            "role": "system",
            "content": SYSTEM_PROMPT.format(tool=tool, language=language_name(language)),
        },
        {
            "role": "user",
            "content": f"JSON schema:\n{schema}\n\nTurn log:\n{turn.transcript_excerpt}",
        },
    ]


def _extract_json(text: str) -> str:
    """Strip reasoning blocks and code fences some local models add."""
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1]
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    start, end = text.find("{"), text.rfind("}")
    return text[start : end + 1] if start != -1 and end > start else text


class Summarizer:
    def __init__(self, cfg: LLMConfig, language: str, client: httpx.Client | None = None):
        self.cfg = cfg
        self.language = language
        headers = {}
        if cfg.api_key_env and (key := os.environ.get(cfg.api_key_env)):
            headers["Authorization"] = f"Bearer {key}"
        self.client = client or httpx.Client(
            base_url=cfg.base_url.rstrip("/"), timeout=cfg.timeout_s, headers=headers
        )

    def _request(self, messages: list[dict]) -> str:
        resp = self.client.post(
            "/chat/completions",
            json={
                "model": self.cfg.model,
                "messages": messages,
                "temperature": self.cfg.temperature,
                "response_format": {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "turn_summary",
                        "strict": True,
                        "schema": TurnSummary.llm_schema(),
                    },
                },
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"] or ""

    def summarize(self, turn: ParsedTurn, tool: str) -> TurnSummary:
        messages = build_messages(turn, tool, self.language)
        last_error, unavailable = "no attempt made", False
        for _ in range(self.cfg.retries + 1):
            try:
                raw = self._request(messages)
                return TurnSummary.model_validate_json(_extract_json(raw))
            except ValidationError as e:
                first = e.errors()[0]["msg"]
                last_error = f"invalid summary JSON: {e.error_count()} error(s), first: {first}"
                unavailable = False
            except httpx.HTTPError as e:
                last_error, unavailable = f"{type(e).__name__}: {e}", True
            except (KeyError, IndexError, TypeError, ValueError) as e:
                last_error, unavailable = f"{type(e).__name__}: {e}", False
        raise SummaryError(last_error, unavailable)
