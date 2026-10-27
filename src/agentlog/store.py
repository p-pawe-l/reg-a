"""Per-session JSON files: `<output_dir>/<tool>-<session_id>.json`.

Writes are atomic (temp file + rename) and callers hold `session_lock`
across read-modify-write so concurrent hook workers cannot clobber each other.
"""

import fcntl
import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from agentlog.models import Session, Tool


def session_path(output_dir: Path, tool: Tool, session_id: str) -> Path:
    return output_dir / f"{tool}-{session_id}.json"


@contextmanager
def session_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def load_session(path: Path) -> Session | None:
    if not path.exists():
        return None
    return Session.model_validate_json(path.read_text(encoding="utf-8"))


def save_session(path: Path, session: Session) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(session.model_dump_json(indent=2))
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def list_sessions(output_dir: Path) -> list[Path]:
    return sorted(output_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
