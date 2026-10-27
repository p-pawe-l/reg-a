import os
import threading
import time
from datetime import UTC, datetime

import pytest

from agentlog.models import Session, Turn
from agentlog.store import list_sessions, load_session, save_session, session_lock, session_path


def make_session(sid="s1", n_turns=1) -> Session:
    t0 = datetime(2026, 1, 2, tzinfo=UTC)
    turns = [
        Turn(
            turn_id=f"t{i}",
            index=i,
            user_prompt=f"p{i}",
            started_at=t0,
            finished_at=t0,
            work_time_s=1.5,
            model=None,
        )
        for i in range(1, n_turns + 1)
    ]
    return Session(
        session_id=sid, tool="codex", started_at=t0, transcript_path="/x.jsonl", turns=turns
    )


def test_session_path_is_tool_dash_id_json(tmp_path):
    assert session_path(tmp_path, "claude-code", "abc") == tmp_path / "claude-code-abc.json"


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "nested" / "codex-s1.json"
    s = make_session(n_turns=2)
    save_session(path, s)
    assert load_session(path) == s
    assert load_session(path).total_work_time_s == 3.0


def test_load_missing_session_returns_none(tmp_path):
    assert load_session(tmp_path / "nope.json") is None


def test_save_leaves_no_temp_files(tmp_path):
    path = tmp_path / "out" / "codex-s1.json"
    save_session(path, make_session())
    save_session(path, make_session(n_turns=3))
    assert [p.name for p in path.parent.iterdir()] == ["codex-s1.json"]
    assert len(load_session(path).turns) == 3


def test_failed_write_keeps_old_file_and_removes_temp(tmp_path, monkeypatch):
    path = tmp_path / "out" / "codex-s1.json"
    save_session(path, make_session(n_turns=1))

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        save_session(path, make_session(n_turns=5))
    assert [p.name for p in path.parent.iterdir()] == ["codex-s1.json"]
    assert len(load_session(path).turns) == 1


def test_list_sessions_returns_json_files_newest_first(tmp_path):
    for name, mtime in [("a.json", 100), ("b.json", 300), ("c.json", 200)]:
        p = tmp_path / name
        save_session(p, make_session(name))
        os.utime(p, (mtime, mtime))
    (tmp_path / "a.lock").write_text("")
    (tmp_path / "agentlog.log").write_text("")
    assert [p.name for p in list_sessions(tmp_path)] == ["b.json", "c.json", "a.json"]


def test_list_sessions_of_missing_dir_is_empty(tmp_path):
    assert list_sessions(tmp_path / "absent") == []


def test_session_lock_serializes_holders(tmp_path):
    path = tmp_path / "x" / "codex-s1.json"
    events: list[str] = []

    def worker(name):
        with session_lock(path):
            events.append(f"{name}-in")
            time.sleep(0.05)
            events.append(f"{name}-out")

    threads = [threading.Thread(target=worker, args=(n,)) for n in ("a", "b")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert events[0][0] == events[1][0]  # first holder leaves before the second enters
    assert events[2][0] == events[3][0]
    assert path.with_suffix(".lock").exists()
