from datetime import UTC, datetime

import pytest
from conftest import CLAUDE_FIXTURE, CLAUDE_SESSION_ID, write_jsonl

from reg_a.parsers.claude import parse_claude_transcript

SID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"


def ts(sec: int) -> str:
    return f"2026-01-02T10:00:{sec:02d}.000Z"


def prompt(uuid, parent, sec, text="Do something", human=True, **extra):
    e = {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SID,
        "cwd": "/work/x",
        "timestamp": ts(sec),
        "message": {"role": "user", "content": text},
        **extra,
    }
    if human:
        e["origin"] = {"kind": "human"}
    return e


def assistant(uuid, parent, sec, text=None, stop="end_turn", model="claude-test", tools=()):
    content = [{"type": "text", "text": text}] if text else []
    content += [
        {"type": "tool_use", "id": f"tu-{uuid}-{i}", "name": n, "input": inp}
        for i, (n, inp) in enumerate(tools)
    ]
    return {
        "type": "assistant",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SID,
        "timestamp": ts(sec),
        "message": {"role": "assistant", "model": model, "stop_reason": stop, "content": content},
    }


def tool_result(uuid, parent, sec, text="ok"):
    return {
        "type": "user",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SID,
        "timestamp": ts(sec),
        "message": {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "x", "content": text}],
        },
    }


def duration(uuid, parent, sec, ms):
    return {
        "type": "system",
        "subtype": "turn_duration",
        "uuid": uuid,
        "parentUuid": parent,
        "sessionId": SID,
        "timestamp": ts(sec),
        "durationMs": ms,
        "isMeta": True,
    }


def parse(tmp_path, entries, **kw):
    return parse_claude_transcript(write_jsonl(tmp_path / f"{SID}.jsonl", entries), **kw)


# --- the realistic fixture --------------------------------------------------------------------


@pytest.fixture
def session():
    return parse_claude_transcript(CLAUDE_FIXTURE)


def test_fixture_session_metadata_comes_from_transcript(session):
    assert session.session_id == CLAUDE_SESSION_ID
    assert session.tool == "claude-code"
    assert session.title == "Greeting function"
    assert session.cwd == "/work/demo"
    assert session.transcript_path == str(CLAUDE_FIXTURE)
    assert session.started_at == datetime(2026, 1, 2, 10, 0, 0, tzinfo=UTC)


def test_rewound_branch_is_dropped_and_live_turns_kept_in_order(session):
    assert [t.turn_id for t in session.turns] == ["u1", "u3", "u4"]
    assert "Write a README" not in [t.user_prompt for t in session.turns]


def test_completed_turn_collects_files_commands_model_and_final_message(session):
    t = session.turns[0]
    assert t.user_prompt == "Add a greeting function to hello.py"
    assert t.files_changed == ["/work/demo/hello.py"]
    assert t.commands_run == ["python hello.py"]
    assert t.model == "claude-test-1"
    assert t.final_message == "Done: added greet()."
    assert t.interrupted is False


def test_turn_duration_entry_sets_work_time(session):
    t = session.turns[0]
    assert t.work_time_s == 9.5
    assert t.started_at == datetime(2026, 1, 2, 10, 0, 0, tzinfo=UTC)
    assert t.finished_at == datetime(2026, 1, 2, 10, 0, 11, tzinfo=UTC)


def test_meta_user_entry_does_not_start_a_new_turn(session):
    assert all("system-reminder" not in t.user_prompt for t in session.turns)


def test_interrupted_turn_is_kept_and_flagged(session):
    t = session.turns[1]
    assert t.user_prompt == "Write tests instead"
    assert t.interrupted is True
    assert t.files_changed == ["/work/demo/test_hello.py"]
    assert t.work_time_s == 10.0  # no turn_duration: prompt → interrupt marker
    assert t.final_message == ""


def test_turn_without_duration_uses_wall_clock_and_its_own_model(session):
    t = session.turns[2]
    assert t.model == "claude-test-2"
    assert t.work_time_s == 4.0
    assert t.final_message == "It prints a greeting."


def test_sidechain_entry_at_end_does_not_become_the_leaf(session):
    assert session.turns[-1].turn_id == "u4"
    assert all("Subagent" not in t.final_message for t in session.turns)


def test_excerpt_contains_prompt_messages_tools_and_results_but_not_thinking(session):
    ex = session.turns[0].transcript_excerpt
    assert "[user] Add a greeting function to hello.py" in ex
    assert "[assistant] I'll add it." in ex
    assert "[tool Edit] /work/demo/hello.py" in ex
    assert "[tool Bash] python hello.py" in ex
    assert "[result] Hello" in ex
    assert "synthetic thought" not in ex


# --- boundary and failure cases ---------------------------------------------------------------


def test_empty_transcript_raises_value_error(tmp_path):
    with pytest.raises(ValueError, match="no timestamps"):
        parse(tmp_path, [])


def test_transcript_with_only_unknown_entries_and_a_timestamp_has_no_turns(tmp_path):
    s = parse(
        tmp_path,
        [
            {"type": "mode", "sessionId": SID},
            {"type": "api-request", "timestamp": ts(3), "sessionId": SID},
        ],
    )
    assert s.turns == []
    assert s.started_at == datetime(2026, 1, 2, 10, 0, 3, tzinfo=UTC)
    assert s.session_id == SID


def test_session_id_falls_back_to_file_stem(tmp_path):
    entries = [prompt("p1", None, 0), assistant("a1", "p1", 2, "hi")]
    for e in entries:
        del e["sessionId"]
    s = parse_claude_transcript(write_jsonl(tmp_path / "stem-id.jsonl", entries))
    assert s.session_id == "stem-id"


def test_prompt_without_assistant_reply_is_not_a_turn_yet(tmp_path):
    s = parse(
        tmp_path,
        [
            prompt("p1", None, 0, "first"),
            assistant("a1", "p1", 2, "done"),
            prompt("p2", "a1", 5, "second"),
        ],
    )
    assert [t.turn_id for t in s.turns] == ["p1"]


def test_turn_still_running_tool_use_is_not_a_turn_yet(tmp_path):
    s = parse(
        tmp_path,
        [
            prompt("p1", None, 0),
            assistant("a1", "p1", 2, stop="tool_use", tools=[("Bash", {"command": "make"})]),
            tool_result("r1", "a1", 3),
        ],
    )
    assert s.turns == []


def test_assistant_with_null_stop_reason_is_not_complete(tmp_path):
    s = parse(tmp_path, [prompt("p1", None, 0), assistant("a1", "p1", 2, "partial", stop=None)])
    assert s.turns == []


def test_any_non_tool_stop_reason_completes_the_turn(tmp_path):
    s = parse(tmp_path, [prompt("p1", None, 0), assistant("a1", "p1", 2, "cut", stop="max_tokens")])
    assert len(s.turns) == 1
    assert s.turns[0].work_time_s == 2.0


def test_turn_duration_alone_completes_a_turn_ending_in_tool_use(tmp_path):
    s = parse(
        tmp_path,
        [
            prompt("p1", None, 0),
            assistant("a1", "p1", 1, stop="tool_use"),
            duration("d1", "a1", 2, 1500),
        ],
    )
    assert len(s.turns) == 1
    assert s.turns[0].work_time_s == 1.5


def test_interrupt_with_plain_string_content_marks_turn_interrupted(tmp_path):
    interrupt = {
        "type": "user",
        "uuid": "i1",
        "parentUuid": "a1",
        "sessionId": SID,
        "timestamp": ts(4),
        "message": {"role": "user", "content": "[Request interrupted by user for tool use]"},
    }
    s = parse(
        tmp_path, [prompt("p1", None, 0), assistant("a1", "p1", 1, stop="tool_use"), interrupt]
    )
    assert len(s.turns) == 1
    assert s.turns[0].interrupted is True


def test_entries_without_origin_use_text_heuristic_for_prompts(tmp_path):
    entries = [
        prompt("p1", None, 0, "legacy prompt", human=False),
        assistant("a1", "p1", 1, "answer"),
        prompt("c1", "a1", 2, "<local-command-stdout>x</local-command-stdout>", human=False),
        prompt("p2", "c1", 3, "second legacy prompt", human=False),
        assistant("a2", "p2", 4, "answer 2"),
    ]
    s = parse(tmp_path, entries)
    assert [t.user_prompt for t in s.turns] == ["legacy prompt", "second legacy prompt"]


def test_user_entry_with_non_human_origin_is_not_a_prompt(tmp_path):
    notification = prompt(
        "n1", "a1", 2, "task notification", human=False, origin={"kind": "task-notification"}
    )
    entries = [
        prompt("p1", None, 0, "real prompt"),
        assistant("a1", "p1", 1, "answer"),
        notification,
        assistant("a2", "n1", 3, "follow-up"),
    ]
    s = parse(tmp_path, entries)
    assert [t.turn_id for t in s.turns] == ["p1"]
    assert s.turns[0].final_message == "follow-up"


def test_synthetic_model_names_are_ignored(tmp_path):
    s = parse(
        tmp_path,
        [
            prompt("p1", None, 0),
            assistant("a1", "p1", 1, "x", stop="tool_use", model="claude-real"),
            assistant("a2", "a1", 2, "y", model="<synthetic>"),
        ],
    )
    assert s.turns[0].model == "claude-real"


def test_turn_with_only_synthetic_model_has_no_model(tmp_path):
    s = parse(tmp_path, [prompt("p1", None, 0), assistant("a1", "p1", 1, "x", model="<synthetic>")])
    assert s.turns[0].model is None


def test_most_used_model_wins(tmp_path):
    s = parse(
        tmp_path,
        [
            prompt("p1", None, 0),
            assistant("a1", "p1", 1, stop="tool_use", model="m-small"),
            assistant("a2", "a1", 2, stop="tool_use", model="m-big"),
            assistant("a3", "a2", 3, "done", model="m-big"),
        ],
    )
    assert s.turns[0].model == "m-big"


def test_file_tools_are_deduplicated_and_notebooks_counted(tmp_path):
    tools = [
        ("Edit", {"file_path": "/w/a.py"}),
        ("MultiEdit", {"file_path": "/w/a.py"}),
        ("NotebookEdit", {"notebook_path": "/w/n.ipynb"}),
        ("Read", {"file_path": "/w/r.py"}),
        ("Write", {"file_path": "/w/b.py"}),
    ]
    s = parse(
        tmp_path,
        [
            prompt("p1", None, 0),
            assistant("a1", "p1", 1, stop="tool_use", tools=tools),
            assistant("a2", "a1", 2, "done"),
        ],
    )
    assert s.turns[0].files_changed == ["/w/a.py", "/w/n.ipynb", "/w/b.py"]


def test_final_message_is_last_non_empty_text(tmp_path):
    s = parse(
        tmp_path,
        [
            prompt("p1", None, 0),
            assistant("a1", "p1", 1, "first", stop="tool_use"),
            assistant("a2", "a1", 2, "   ", stop="tool_use"),
            assistant("a3", "a2", 3, "last"),
        ],
    )
    assert s.turns[0].final_message == "last"


def test_excerpt_is_capped_at_max_chars(tmp_path):
    tools = [("Bash", {"command": f"echo {i} " + "x" * 500}) for i in range(100)]
    s = parse(
        tmp_path,
        [
            prompt("p1", None, 0),
            assistant("a1", "p1", 1, stop="tool_use", tools=tools),
            assistant("a2", "a1", 2, "done"),
        ],
        max_chars=1000,
    )
    assert len(s.turns[0].transcript_excerpt) < 1100
    assert len(s.turns[0].commands_run) == 100  # facts are not truncated


def test_cycle_in_parent_links_does_not_hang(tmp_path):
    a = prompt("p1", "a1", 0)
    s = parse(tmp_path, [a, assistant("a1", "p1", 1, "done")])
    assert [t.turn_id for t in s.turns] == ["p1"]


def test_entry_with_unparseable_prompt_timestamp_is_skipped(tmp_path):
    bad = prompt("p1", None, 0)
    bad["timestamp"] = "garbage"
    s = parse(
        tmp_path,
        [
            bad,
            assistant("a1", "p1", 1, "done"),
            prompt("p2", "a1", 2),
            assistant("a2", "p2", 3, "done"),
        ],
    )
    assert [t.turn_id for t in s.turns] == ["p2"]


def test_attachment_after_final_reply_does_not_make_turn_incomplete(tmp_path):
    # Real transcripts interleave `attachment` entries (uuid + parentUuid, no message) in the
    # chain. An attachment after the end_turn reply must not reset the completion state.
    attachment = {
        "type": "attachment",
        "uuid": "at1",
        "parentUuid": "a1",
        "sessionId": SID,
        "timestamp": ts(3),
        "attachment": {"type": "synthetic"},
    }
    away = {
        "type": "system",
        "subtype": "away_summary",
        "uuid": "sy1",
        "parentUuid": "at1",
        "sessionId": SID,
        "timestamp": ts(4),
        "isMeta": True,
        "content": "synthetic",
    }
    s = parse(tmp_path, [prompt("p1", None, 0), assistant("a1", "p1", 2, "done"), attachment, away])
    assert [t.turn_id for t in s.turns] == ["p1"]


# --- subagent transcripts -----------------------------------------------------------------------


def test_subagent_edits_and_commands_are_credited_to_the_turn_that_ran_them(tmp_path):
    entries = [
        prompt("p1", None, 0, "First"),
        assistant("a1", "p1", 2, "done", tools=[("Agent", {"prompt": "go"})]),
        duration("d1", "a1", 20, 20_000),
        prompt("p2", "d1", 30, "Second"),
        assistant("a2", "p2", 35, "ok"),
    ]
    subagents = tmp_path / SID / "subagents"
    subagents.mkdir(parents=True)
    write_jsonl(
        subagents / "agent-x.jsonl",
        [
            {
                **assistant(
                    "s1",
                    None,
                    5,
                    tools=[
                        ("Write", {"file_path": "/work/x/new.py"}),
                        ("Bash", {"command": "pytest"}),
                    ],
                ),
                "isSidechain": True,
            },
            {
                **assistant(
                    "s2", "s1", 25, tools=[("Bash", {"command": "background, after turn"})]
                ),
                "isSidechain": True,
            },
            {
                **assistant("s3", "s2", 31, tools=[("Bash", {"command": "during turn 2"})]),
                "isSidechain": True,
            },
        ],
    )
    first, second = parse(tmp_path, entries).turns
    assert first.files_changed == ["/work/x/new.py"]
    assert first.commands_run == ["pytest", "background, after turn"]
    assert second.commands_run == ["during turn 2"]
