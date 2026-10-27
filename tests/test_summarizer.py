import json
from datetime import UTC, datetime

import httpx
import pytest

from reg_a.config import LLMConfig
from reg_a.models import ParsedTurn, TurnSummary
from reg_a.summarizer import (
    Summarizer,
    SummaryError,
    _extract_json,
    build_messages,
    language_name,
)

GOOD = {"summary": "Added greet().", "actions": ["edited hello.py"], "outcome": "success"}


def parsed_turn(**kw) -> ParsedTurn:
    base = dict(
        turn_id="t1",
        user_prompt="Add greet",
        started_at=datetime(2026, 1, 2, tzinfo=UTC),
        finished_at=datetime(2026, 1, 2, 0, 1, tzinfo=UTC),
        work_time_s=60,
        model="m",
        interrupted=False,
        files_changed=[],
        commands_run=[],
        final_message="",
        transcript_excerpt="[user] Add greet\n[assistant] done",
    )
    return ParsedTurn(**(base | kw))


def chat_reply(content):
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": content}}]}
    )


class FakeLLM:
    """Serves queued responses and records each request body."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r if isinstance(r, httpx.Response) else chat_reply(r)

    def bodies(self):
        return [json.loads(r.content) for r in self.requests]


def summarizer(fake, retries=1, language="en", **cfg):
    client = httpx.Client(transport=httpx.MockTransport(fake), base_url="http://llm.test/v1")
    return Summarizer(
        LLMConfig(retries=retries, model="test-model", **cfg), language, client=client
    )


def test_valid_reply_is_parsed_into_turn_summary():
    fake = FakeLLM(json.dumps(GOOD))
    s = summarizer(fake).summarize(parsed_turn(), "claude-code")
    assert s == TurnSummary(**GOOD)
    assert s.open_issues == []
    assert len(fake.requests) == 1


def test_request_targets_chat_completions_with_json_schema_and_model():
    fake = FakeLLM(json.dumps(GOOD))
    summarizer(fake, temperature=0.5).summarize(parsed_turn(), "codex")
    req = fake.requests[0]
    assert req.method == "POST"
    assert req.url == "http://llm.test/v1/chat/completions"
    body = fake.bodies()[0]
    assert body["model"] == "test-model"
    assert body["temperature"] == 0.5
    assert body["response_format"]["type"] == "json_schema"
    schema = body["response_format"]["json_schema"]["schema"]
    assert schema == TurnSummary.llm_schema()
    # strict mode requires every property to be listed as required
    assert set(schema["required"]) == set(schema["properties"])
    assert "[user] Add greet" in body["messages"][1]["content"]


def test_invalid_json_is_retried_then_succeeds():
    fake = FakeLLM("not json at all", json.dumps(GOOD))
    s = summarizer(fake).summarize(parsed_turn(), "claude-code")
    assert s.summary == "Added greet()."
    assert len(fake.requests) == 2


def test_invalid_json_twice_raises_summary_error():
    fake = FakeLLM("nope", '{"summary": 1}')
    with pytest.raises(SummaryError, match="invalid summary JSON"):
        summarizer(fake).summarize(parsed_turn(), "claude-code")
    assert len(fake.requests) == 2


def test_schema_violation_is_reported_with_error_count():
    bad = GOOD | {"outcome": "maybe", "extra": 1}
    fake = FakeLLM(json.dumps(bad), json.dumps(bad))
    with pytest.raises(SummaryError, match=r"2 error\(s\)"):
        summarizer(fake).summarize(parsed_turn(), "claude-code")


def test_retries_setting_controls_attempt_count():
    fake = FakeLLM(*["bad"] * 4)
    with pytest.raises(SummaryError):
        summarizer(fake, retries=3).summarize(parsed_turn(), "codex")
    assert len(fake.requests) == 4

    fake = FakeLLM("bad")
    with pytest.raises(SummaryError):
        summarizer(fake, retries=0).summarize(parsed_turn(), "codex")
    assert len(fake.requests) == 1


def test_http_error_status_is_retried_then_reported():
    fake = FakeLLM(httpx.Response(500, text="boom"), httpx.Response(503, text="busy"))
    with pytest.raises(SummaryError, match="HTTPStatusError"):
        summarizer(fake).summarize(parsed_turn(), "codex")
    assert len(fake.requests) == 2


def test_connection_error_is_retried_then_succeeds():
    fake = FakeLLM(httpx.ConnectError("refused"), json.dumps(GOOD))
    assert summarizer(fake).summarize(parsed_turn(), "codex").outcome == "success"


def test_malformed_response_body_raises_summary_error():
    fake = FakeLLM(httpx.Response(200, json={"choices": []}), httpx.Response(200, json={}))
    with pytest.raises(SummaryError, match="IndexError|KeyError"):
        summarizer(fake).summarize(parsed_turn(), "codex")


def test_null_content_counts_as_invalid_reply():
    fake = FakeLLM(None, None)
    with pytest.raises(SummaryError, match="invalid summary JSON"):
        summarizer(fake).summarize(parsed_turn(), "codex")


def test_reply_wrapped_in_think_block_and_code_fence_is_accepted():
    raw = f'<think>{{"not": "this"}}</think>\n```json\n{json.dumps(GOOD)}\n```'
    fake = FakeLLM(raw)
    assert summarizer(fake).summarize(parsed_turn(), "codex") == TurnSummary(**GOOD)


def test_extract_json_cuts_prose_around_the_object():
    assert _extract_json('Sure! {"a": {"b": 1}} hope this helps') == '{"a": {"b": 1}}'
    assert _extract_json("no braces") == "no braces"
    assert _extract_json("```\n```") == ""


def test_api_key_header_is_sent_only_when_env_var_is_set(monkeypatch):
    monkeypatch.setenv("REG_A_TEST_KEY", "sk-test")
    s = Summarizer(LLMConfig(api_key_env="REG_A_TEST_KEY"), "en")
    assert s.client.headers["Authorization"] == "Bearer sk-test"
    assert str(s.client.base_url) == "http://localhost:11434/v1/"

    monkeypatch.delenv("REG_A_TEST_KEY")
    s = Summarizer(LLMConfig(api_key_env="REG_A_TEST_KEY"), "en")
    assert "Authorization" not in s.client.headers


def test_language_code_is_named_in_the_system_prompt():
    msgs = build_messages(parsed_turn(), "claude-code", "pl")
    assert msgs[0]["role"] == "system"
    assert "Polish" in msgs[0]["content"]
    assert "claude-code" in msgs[0]["content"]
    assert msgs[1]["role"] == "user"
    assert "JSON schema" in msgs[1]["content"]


def test_language_name_maps_codes_case_insensitively_and_passes_names_through():
    assert language_name("PL") == "Polish"
    assert language_name("en") == "English"
    assert language_name("Esperanto") == "Esperanto"


def test_connection_failure_marks_error_as_unavailable():
    from reg_a.summarizer import SummaryError

    def handler(request):
        raise httpx.ConnectError("refused")

    s = Summarizer(
        LLMConfig(base_url="http://llm.test/v1", retries=0),
        "en",
        client=httpx.Client(transport=httpx.MockTransport(handler), base_url="http://llm.test/v1"),
    )
    with pytest.raises(SummaryError) as exc:
        s.summarize(parsed_turn(), "claude-code")
    assert exc.value.unavailable is True
