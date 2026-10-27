import json
import shutil
import threading
import time

import httpx
from conftest import (
    CLAUDE_FIXTURE,
    CLAUDE_SESSION_ID,
    CODEX_FIXTURE,
    CODEX_SESSION_ID,
    make_summary,
)

from reg_a import sync as sync_mod
from reg_a.store import load_session
from reg_a.summarizer import Summarizer, SummaryError
from reg_a.sync import sync_session


class Recorder:
    def __init__(self, fail_on=(), crash_on=()):
        self.calls: list[tuple[str, str]] = []
        self.fail_on, self.crash_on = set(fail_on), set(crash_on)

    def __call__(self, turn, tool):
        self.calls.append((turn.turn_id, tool))
        if turn.turn_id in self.fail_on:
            raise SummaryError("invalid summary JSON: 1 error(s), first: boom")
        if turn.turn_id in self.crash_on:
            raise RuntimeError("summarizer bug")
        return make_summary(f"summary of {turn.turn_id}")


def copy_claude(tmp_path, entries_upto=None):
    dest = tmp_path / "transcripts" / f"{CLAUDE_SESSION_ID}.jsonl"
    dest.parent.mkdir(parents=True, exist_ok=True)
    lines = CLAUDE_FIXTURE.read_text().splitlines(keepends=True)
    dest.write_text("".join(lines[:entries_upto]))
    return dest


def test_claude_sync_writes_session_file_with_all_turns(tmp_path, cfg):
    transcript = copy_claude(tmp_path)
    rec = Recorder()
    out = sync_session("claude-code", transcript, cfg, summarize=rec)

    assert out == cfg.output_dir / f"claude-code-{CLAUDE_SESSION_ID}.json"
    s = load_session(out)
    assert s.session_id == CLAUDE_SESSION_ID
    assert s.tool == "claude-code"
    assert s.title == "Greeting function"
    assert s.cwd == "/work/demo"
    assert s.transcript_path == str(transcript)
    assert [(t.index, t.turn_id) for t in s.turns] == [(1, "u1"), (2, "u3"), (3, "u4")]
    assert s.turns[0].summary.summary == "summary of u1"
    assert s.turns[0].files_changed == ["/work/demo/hello.py"]
    assert s.turns[1].interrupted is True
    assert rec.calls == [("u1", "claude-code"), ("u3", "claude-code"), ("u4", "claude-code")]


def test_stored_json_has_no_transcript_excerpt(tmp_path, cfg):
    out = sync_session("claude-code", copy_claude(tmp_path), cfg, summarize=Recorder())
    raw = json.loads(out.read_text())
    assert "transcript_excerpt" not in raw["turns"][0]


def test_codex_sync_writes_codex_session_file(tmp_path, cfg):
    rec = Recorder()
    out = sync_session("codex", CODEX_FIXTURE, cfg, summarize=rec)
    assert out.name == f"codex-{CODEX_SESSION_ID}.json"
    s = load_session(out)
    assert [t.turn_id for t in s.turns] == ["turn-1", "turn-2"]
    assert {tool for _, tool in rec.calls} == {"codex"}


def test_second_sync_is_idempotent_and_does_not_resummarize(tmp_path, cfg):
    transcript = copy_claude(tmp_path)
    sync_session("claude-code", transcript, cfg, summarize=Recorder())
    first = (cfg.output_dir / f"claude-code-{CLAUDE_SESSION_ID}.json").read_text()

    rec = Recorder()
    out = sync_session("claude-code", transcript, cfg, summarize=rec)
    assert rec.calls == []
    assert out.read_text() == first


def test_resumed_session_appends_turns_to_existing_file(tmp_path, cfg):
    # First sync sees the transcript up to the end of turn 1 (line 10 = turn_duration).
    transcript = copy_claude(tmp_path, entries_upto=10)
    sync_session("claude-code", transcript, cfg, summarize=Recorder())
    out = cfg.output_dir / f"claude-code-{CLAUDE_SESSION_ID}.json"
    assert [t.turn_id for t in load_session(out).turns] == ["u1"]

    transcript.write_text(CLAUDE_FIXTURE.read_text())
    rec = Recorder()
    sync_session("claude-code", transcript, cfg, summarize=rec)
    assert [c[0] for c in rec.calls] == ["u3", "u4"]
    s = load_session(out)
    assert [(t.index, t.turn_id) for t in s.turns] == [(1, "u1"), (2, "u3"), (3, "u4")]
    assert s.turns[0].summary.summary == "summary of u1"


def test_turns_missing_from_a_later_transcript_are_kept(tmp_path, cfg):
    transcript = copy_claude(tmp_path)
    sync_session("claude-code", transcript, cfg, summarize=Recorder())
    # Simulate compaction/rewind: the transcript now only has the first turn.
    transcript.write_text("".join(CLAUDE_FIXTURE.read_text().splitlines(keepends=True)[:10]))
    out = sync_session("claude-code", transcript, cfg, summarize=Recorder())
    assert [t.turn_id for t in load_session(out).turns] == ["u1", "u3", "u4"]


def test_summary_error_stores_fallback_record(tmp_path, cfg):
    out = sync_session(
        "claude-code", copy_claude(tmp_path), cfg, summarize=Recorder(fail_on={"u3"})
    )
    t = load_session(out).turns[1]
    assert t.summary is None
    assert t.summary_error.startswith("invalid summary JSON")
    assert t.user_prompt == "Write tests instead"  # facts survive


def test_unexpected_summarizer_exception_is_stored_not_raised(tmp_path, cfg):
    out = sync_session(
        "claude-code", copy_claude(tmp_path), cfg, summarize=Recorder(crash_on={"u1"})
    )
    s = load_session(out)
    assert s.turns[0].summary is None
    assert s.turns[0].summary_error == "RuntimeError: summarizer bug"
    assert s.turns[1].summary is not None


def test_failed_turns_are_not_retried_by_default(tmp_path, cfg):
    transcript = copy_claude(tmp_path)
    sync_session("claude-code", transcript, cfg, summarize=Recorder(fail_on={"u1"}))
    rec = Recorder()
    sync_session("claude-code", transcript, cfg, summarize=rec)
    assert rec.calls == []


def test_resummarize_failed_retries_only_failed_turns(tmp_path, cfg):
    transcript = copy_claude(tmp_path)
    sync_session("claude-code", transcript, cfg, summarize=Recorder(fail_on={"u1"}))
    rec = Recorder()
    out = sync_session("claude-code", transcript, cfg, summarize=rec, resummarize_failed=True)
    assert rec.calls == [("u1", "claude-code")]
    t = load_session(out).turns[0]
    assert t.summary.summary == "summary of u1"
    assert t.summary_error is None


def test_transcript_without_turns_still_writes_session_file(tmp_path, cfg):
    transcript = copy_claude(tmp_path, entries_upto=4)  # prompt + meta, no reply yet
    out = sync_session("claude-code", transcript, cfg, summarize=Recorder())
    s = load_session(out)
    assert s.turns == []
    assert s.session_id == CLAUDE_SESSION_ID


def test_default_summarizer_uses_config_and_falls_back_on_bad_llm(tmp_path, cfg, monkeypatch):
    replies = iter(["not json"] * 100)

    def handler(request):
        return httpx.Response(200, json={"choices": [{"message": {"content": next(replies)}}]})

    real_init = Summarizer.__init__

    def init(self, llm_cfg, language, client=None):
        real_init(
            self,
            llm_cfg,
            language,
            client=httpx.Client(transport=httpx.MockTransport(handler), base_url=llm_cfg.base_url),
        )

    monkeypatch.setattr(Summarizer, "__init__", init)
    out = sync_session("codex", CODEX_FIXTURE, cfg)
    s = load_session(out)
    assert [t.summary for t in s.turns] == [None, None]
    assert all(t.summary_error.startswith("invalid summary JSON") for t in s.turns)


def test_concurrent_syncs_summarize_each_turn_once(tmp_path, cfg):
    transcript = copy_claude(tmp_path)
    calls: list[str] = []

    def slow(turn, tool):
        calls.append(turn.turn_id)
        time.sleep(0.05)
        return make_summary()

    threads = [
        threading.Thread(
            target=sync_session, args=("claude-code", transcript, cfg), kwargs={"summarize": slow}
        )
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(calls) == ["u1", "u3", "u4"]
    s = load_session(cfg.output_dir / f"claude-code-{CLAUDE_SESSION_ID}.json")
    assert len(s.turns) == 3


def test_progress_is_saved_after_each_turn(tmp_path, cfg, monkeypatch):
    saved_turn_counts: list[int] = []
    real_save = sync_mod.save_session

    def spy(path, session):
        saved_turn_counts.append(len(session.turns))
        real_save(path, session)

    monkeypatch.setattr(sync_mod, "save_session", spy)
    sync_session("claude-code", copy_claude(tmp_path), cfg, summarize=Recorder())
    assert saved_turn_counts[:3] == [1, 2, 3]


def test_transcript_path_is_updated_when_file_moves(tmp_path, cfg):
    sync_session("codex", CODEX_FIXTURE, cfg, summarize=Recorder())
    moved = tmp_path / "moved" / CODEX_FIXTURE.name
    moved.parent.mkdir()
    shutil.copy(CODEX_FIXTURE, moved)
    out = sync_session("codex", moved, cfg, summarize=Recorder())
    assert load_session(out).transcript_path == str(moved)


def test_turn_completing_twice_is_resummarized_with_its_grown_facts(tmp_path, cfg):
    # A background task can resume a finished turn under the same prompt: the second
    # completion adds work, so the stored turn must be refreshed, not skipped.
    from conftest import write_jsonl
    from test_claude_parser import SID, assistant, duration, prompt, tool_result

    transcript = tmp_path / f"{SID}.jsonl"
    first_stop = [
        prompt("p1", None, 0, "Go"),
        assistant("a1", "p1", 1, "started agents"),
        duration("d1", "a1", 2, 2_000),
    ]
    write_jsonl(transcript, first_stop)
    sync_session("claude-code", transcript, cfg, summarize=Recorder())

    write_jsonl(
        transcript,
        first_stop
        + [
            assistant("a2", "d1", 10, tools=[("Bash", {"command": "pytest"})], stop="tool_use"),
            tool_result("r2", "a2", 11),
            assistant("a3", "r2", 12, "all done"),
            duration("d2", "a3", 13, 3_000),
        ],
    )
    rec = Recorder()
    out = sync_session("claude-code", transcript, cfg, summarize=rec)
    (turn,) = load_session(out).turns
    assert rec.calls == [("p1", "claude-code")]
    assert turn.commands_run == ["pytest"]
    assert turn.final_message == "all done"
    assert turn.work_time_s == 5.0


def test_corrupt_session_file_is_set_aside_and_rebuilt(tmp_path, cfg):
    out = cfg.output_dir / f"claude-code-{CLAUDE_SESSION_ID}.json"
    out.parent.mkdir(parents=True)
    out.write_text('{"truncated": ')
    sync_session("claude-code", copy_claude(tmp_path), cfg, summarize=Recorder())
    assert len(load_session(out).turns) == 3
    (aside,) = cfg.output_dir.glob(f"{out.name}.corrupt-*")
    assert aside.read_text() == '{"truncated": '


class Unavailable(Recorder):
    def __call__(self, turn, tool):
        self.calls.append((turn.turn_id, tool))
        raise SummaryError("ConnectError: refused", unavailable=True)


def test_unreachable_llm_trips_circuit_breaker_after_first_call(tmp_path, cfg):
    rec = Unavailable()
    out = sync_session("claude-code", copy_claude(tmp_path), cfg, summarize=rec)
    assert len(rec.calls) == 1
    errors = [t.summary_error for t in load_session(out).turns]
    assert errors[0] == "ConnectError: refused"
    assert all(e.startswith("not attempted") for e in errors[1:])


def test_invalid_reply_for_one_turn_does_not_trip_circuit_breaker(tmp_path, cfg):
    rec = Recorder(fail_on={"u1"})
    sync_session("claude-code", copy_claude(tmp_path), cfg, summarize=rec)
    assert [c[0] for c in rec.calls] == ["u1", "u3", "u4"]


def test_new_turns_are_summarized_before_retrying_old_failures(tmp_path, cfg):
    transcript = copy_claude(tmp_path, entries_upto=10)
    sync_session("claude-code", transcript, cfg, summarize=Recorder(fail_on={"u1"}))
    transcript.write_text(CLAUDE_FIXTURE.read_text())
    rec = Recorder()
    sync_session("claude-code", transcript, cfg, summarize=rec, resummarize_failed=True)
    assert [c[0] for c in rec.calls] == ["u3", "u4", "u1"]


def test_failed_refresh_keeps_previous_good_summary(tmp_path, cfg):
    from conftest import write_jsonl
    from test_claude_parser import SID, assistant, duration, prompt

    transcript = tmp_path / f"{SID}.jsonl"
    first = [
        prompt("p1", None, 0, "Go"),
        assistant("a1", "p1", 1, "v1"),
        duration("d1", "a1", 2, 2_000),
    ]
    write_jsonl(transcript, first)
    sync_session("claude-code", transcript, cfg, summarize=Recorder())
    write_jsonl(transcript, first + [assistant("a2", "d1", 5, "v2"), duration("d2", "a2", 6, 1)])
    out = sync_session("claude-code", transcript, cfg, summarize=Unavailable())
    (turn,) = load_session(out).turns
    assert turn.final_message == "v2"
    assert turn.summary.summary == "summary of p1"
    assert turn.summary_error == "ConnectError: refused"


def test_session_file_with_invalid_utf8_is_set_aside(tmp_path, cfg):
    out = cfg.output_dir / f"claude-code-{CLAUDE_SESSION_ID}.json"
    out.parent.mkdir(parents=True)
    out.write_bytes(b"\xff\xfe garbage")
    sync_session("claude-code", copy_claude(tmp_path), cfg, summarize=Recorder())
    assert len(load_session(out).turns) == 3
    assert len(list(cfg.output_dir.glob(f"{out.name}.corrupt-*"))) == 1
