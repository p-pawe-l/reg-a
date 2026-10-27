import os
from datetime import UTC, datetime

import pytest
from conftest import CODEX_FIXTURE, CODEX_SESSION_ID, write_jsonl

from agentlog.parsers.codex import find_rollout, parse_codex_rollout

SID = "01990000-0000-7000-8000-0000000000aa"
T0 = 1767348000  # 2026-01-02T10:00:00Z


def ev(sec, payload, etype="event_msg"):
    return {"timestamp": f"2026-01-02T10:00:{sec:02d}.000Z", "type": etype, "payload": payload}


def started(turn, sec):
    return ev(sec, {"type": "task_started", "turn_id": turn, "started_at": T0 + sec})


def item(turn, sec, **it):
    return ev(sec, {"type": "item_completed", "turn_id": turn, "item": it})


def user(turn, sec, text):
    return item(turn, sec, type="UserMessage", content=[{"type": "text", "text": text}])


def agent(turn, sec, text, phase=None):
    it = {"type": "AgentMessage", "content": [{"type": "text", "text": text}]}
    if phase:
        it["phase"] = phase
    return item(turn, sec, **it)


def complete(turn, sec, **extra):
    return ev(sec, {"type": "task_complete", "turn_id": turn, **extra})


def parse(tmp_path, entries, name=f"rollout-2026-01-02T10-00-00-{SID}.jsonl", **kw):
    return parse_codex_rollout(write_jsonl(tmp_path / name, entries), **kw)


# --- the realistic fixture --------------------------------------------------------------------


@pytest.fixture
def session():
    return parse_codex_rollout(CODEX_FIXTURE)


def test_fixture_session_metadata_comes_from_session_meta(session):
    assert session.session_id == CODEX_SESSION_ID
    assert session.tool == "codex"
    assert session.title is None
    assert session.cwd == "/work/demo"
    assert session.started_at == datetime(2026, 1, 2, 9, 59, 58, tzinfo=UTC)


def test_only_user_initiated_finished_turns_are_kept(session):
    # turn-bg has no UserMessage; turn-3 never completes.
    assert [t.turn_id for t in session.turns] == ["turn-1", "turn-2"]


def test_completed_turn_collects_facts(session):
    t = session.turns[0]
    assert t.user_prompt == "Fix the typo in README.md"
    assert t.started_at == datetime(2026, 1, 2, 10, tzinfo=UTC)
    assert t.finished_at == datetime(2026, 1, 2, 10, 0, 12, tzinfo=UTC)
    assert t.work_time_s == 12.0
    assert t.model == "gpt-test"
    assert t.commands_run == ["cat README.md"]
    assert t.files_changed == ["/work/demo/README.md"]
    assert t.interrupted is False


def test_task_complete_last_agent_message_is_the_final_message(session):
    assert session.turns[0].final_message == "Fixed the typo in README.md."


def test_aborted_turn_is_flagged_and_timed_from_completed_at(session):
    t = session.turns[1]
    assert t.interrupted is True
    assert t.files_changed == ["/work/demo/CHANGELOG.md"]
    assert t.work_time_s == 7.0
    assert t.model == "gpt-test"


def test_excerpt_lists_commands_results_file_changes_and_unknown_items(session):
    ex = session.turns[0].transcript_excerpt
    assert "[user] Fix the typo in README.md" in ex
    assert "[command exit=0] cat README.md" in ex
    assert "[result] Helo world" in ex
    assert "[file update] /work/demo/README.md" in ex
    assert "[assistant] Fixed the typo." in ex
    assert "[file add] /work/demo/CHANGELOG.md" in session.turns[1].transcript_excerpt


# --- boundary and failure cases ---------------------------------------------------------------


def test_empty_rollout_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="no timestamps"):
        parse(tmp_path, [])


def test_session_id_comes_from_filename_without_session_meta(tmp_path):
    s = parse(tmp_path, [started("t1", 0), user("t1", 1, "hi"), complete("t1", 2)])
    assert s.session_id == SID
    assert s.started_at == datetime(2026, 1, 2, 10, tzinfo=UTC)


def test_session_id_falls_back_to_stem_for_non_rollout_names(tmp_path):
    s = parse(tmp_path, [started("t1", 0)], name="custom.jsonl")
    assert s.session_id == "custom"


def test_session_meta_session_id_key_is_accepted(tmp_path):
    s = parse(tmp_path, [ev(0, {"session_id": "from-meta", "cwd": "/w"}, "session_meta")])
    assert s.session_id == "from-meta"


def test_turn_without_agent_reply_still_records_prompt(tmp_path):
    s = parse(tmp_path, [started("t1", 0), user("t1", 1, "hello?"), complete("t1", 3)])
    t = s.turns[0]
    assert t.user_prompt == "hello?"
    assert t.final_message == ""
    assert t.work_time_s == 3.0  # no duration_ms → wall clock from completion event
    assert t.model is None


def test_multiple_user_messages_are_joined(tmp_path):
    s = parse(tmp_path, [started("t1", 0), user("t1", 1, "part one"), user("t1", 2, "part two"),
                         complete("t1", 3)])
    assert s.turns[0].user_prompt == "part one\npart two"


def test_final_answer_phase_beats_commentary(tmp_path):
    s = parse(tmp_path, [started("t1", 0), user("t1", 1, "q"),
                         agent("t1", 2, "thinking aloud", "commentary"),
                         agent("t1", 3, "the answer", "final_answer"),
                         agent("t1", 4, "late commentary", "commentary"),
                         ev(5, {"type": "turn_aborted", "turn_id": "t1"})])
    assert s.turns[0].final_message == "the answer"


def test_item_for_unknown_turn_is_ignored(tmp_path):
    s = parse(tmp_path, [started("t1", 0), user("t1", 1, "q"),
                         item("ghost", 2, type="CommandExecution", command="rm -rf /tmp/x"),
                         complete("t1", 3)])
    assert s.turns[0].commands_run == []


def test_completion_for_unknown_turn_is_ignored(tmp_path):
    s = parse(tmp_path, [complete("ghost", 0, duration_ms=10)])
    assert s.turns == []


def test_string_command_and_repeated_file_change_are_handled(tmp_path):
    s = parse(tmp_path, [started("t1", 0), user("t1", 1, "q"),
                         item("t1", 2, type="CommandExecution", command="ls -la"),
                         item("t1", 3, type="FileChange", changes={"/w/a": {"type": "update"}}),
                         item("t1", 4, type="FileChange", changes={"/w/a": {"type": "update"},
                                                                   "/w/b": "weird"}),
                         complete("t1", 5)])
    t = s.turns[0]
    assert t.commands_run == ["ls -la"]
    assert t.files_changed == ["/w/a", "/w/b"]
    assert "[file ?] /w/b" in t.transcript_excerpt


def test_model_from_thread_settings_applied(tmp_path):
    s = parse(tmp_path, [ev(0, {"type": "thread_settings_applied",
                                "thread_settings": {"model": "gpt-settings"}}),
                         started("t1", 1), user("t1", 2, "q"), complete("t1", 3)])
    assert s.turns[0].model == "gpt-settings"


def test_task_started_without_started_at_uses_event_timestamp(tmp_path):
    s = parse(tmp_path, [ev(4, {"type": "task_started", "turn_id": "t1"}), user("t1", 5, "q"),
                         complete("t1", 9)])
    assert s.turns[0].started_at == datetime(2026, 1, 2, 10, 0, 4, tzinfo=UTC)
    assert s.turns[0].work_time_s == 5.0


def test_excerpt_is_capped_at_max_chars(tmp_path):
    items = [item("t1", 2, type="CommandExecution", command=["bash", "-lc", f"echo {i}"],
                  aggregated_output="o" * 1000) for i in range(100)]
    s = parse(tmp_path, [started("t1", 0), user("t1", 1, "q"), *items, complete("t1", 3)],
              max_chars=1500)
    assert len(s.turns[0].transcript_excerpt) < 1600
    assert len(s.turns[0].commands_run) == 100


# --- find_rollout -----------------------------------------------------------------------------


def _rollout(codex_home, day, sid, mtime):
    d = codex_home / "sessions" / "2026" / "01" / day
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"rollout-2026-01-{day}T10-00-00-{sid}.jsonl"
    p.write_text("{}\n")
    os.utime(p, (mtime, mtime))
    return p


def test_find_rollout_by_session_id(tmp_path):
    a = _rollout(tmp_path, "01", "id-a", 100)
    _rollout(tmp_path, "02", "id-b", 200)
    assert find_rollout(tmp_path, "id-a") == a


def test_find_rollout_without_id_returns_most_recently_modified(tmp_path):
    _rollout(tmp_path, "02", "id-b", 100)
    a = _rollout(tmp_path, "01", "id-a", 200)
    assert find_rollout(tmp_path, None) == a


def test_find_rollout_returns_none_when_missing(tmp_path):
    assert find_rollout(tmp_path, "nope") is None
    assert find_rollout(tmp_path, None) is None
