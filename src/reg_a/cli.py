"""`reg-a` command line."""

import argparse
import logging
import sys
import time
from pathlib import Path

from reg_a import hooks, report
from reg_a.config import EXAMPLE_CONFIG, default_config_path, load_config
from reg_a.parsers import EmptyTranscriptError
from reg_a.store import list_sessions, load_session
from reg_a.sync import sync_session

TOOL_ALIASES = {"claude": "claude-code", "claude-code": "claude-code", "codex": "codex"}


def _tool(value: str) -> str:
    try:
        return TOOL_ALIASES[value]
    except KeyError:
        raise argparse.ArgumentTypeError("tool must be 'claude' or 'codex'") from None


def cmd_hook(args, cfg=None) -> int:
    # Always exit 0: a failing hook must never disturb the agent.
    try:
        cfg = load_config(args.config)
        if args.tool == "claude-code":
            path = hooks.claude_transcript_from_hook(sys.stdin.read())
        else:
            path = hooks.codex_rollout_from_notify(args.payload, cfg)
        if path and path.exists():
            hooks.spawn_sync(args.tool, path, cfg, args.config)
    except Exception:
        logging.exception("hook failed")
    return 0


def cmd_sync(args, cfg) -> int:
    if args.delay:
        time.sleep(args.delay)
    try:
        out = sync_session(args.tool, args.transcript, cfg, resummarize_failed=args.retry_failed)
    except EmptyTranscriptError as e:
        print(f"nothing to record yet: {e}")
        return 0
    session = load_session(out)
    failed = sum(t.summary is None for t in session.turns) if session else 0
    print(f"{out}  ({len(session.turns) if session else 0} turns, {failed} without summary)")
    return 0


def cmd_list(args, cfg) -> int:
    for path in list_sessions(cfg.output_dir):
        try:
            s = load_session(path)
        except ValueError:
            print(f"{path.name}: unreadable session file", file=sys.stderr)
            continue
        if s:
            print(
                f"{s.started_at:%Y-%m-%d %H:%M}  {s.tool:<11}  {s.session_id}  "
                f"{len(s.turns):>3} turns  {s.title or s.cwd or ''}"
            )
    return 0


def resolve_session(ref: str, output_dir: Path) -> Path:
    """A session JSON path, or a (prefix of a) session id recorded in output_dir."""
    path = Path(ref).expanduser()
    if path.is_file():
        return path
    matches = [
        p
        for p in list_sessions(output_dir)
        if p.stem.removeprefix("claude-code-").removeprefix("codex-").startswith(ref)
    ]
    if len(matches) != 1:
        found = "no session" if not matches else f"{len(matches)} sessions"
        raise SystemExit(f"{found} matching {ref!r} in {output_dir}")
    return matches[0]


def cmd_convert(args, cfg) -> int:
    if args.all:
        targets = list_sessions(cfg.output_dir)
    elif args.session:
        targets = [resolve_session(args.session, cfg.output_dir)]
    else:
        raise SystemExit("give a session id / JSON path, or --all")
    out_dir = (args.output or cfg.output_dir / "reports").expanduser()
    failed = 0
    for path in targets:
        try:
            print(report.convert(path, out_dir, cfg.language, tex_only=args.tex_only))
        except (report.LatexError, ValueError, OSError) as e:  # ValueError: corrupt JSON
            print(f"{path.name}: {e}", file=sys.stderr)
            failed += 1
    return 1 if failed else 0


def cmd_install(args, cfg) -> int:
    if args.tool == "claude-code":
        settings = (
            Path(".claude/settings.json") if args.project else cfg.claude_home / "settings.json"
        )
        print(hooks.install_claude(settings))
    else:
        print(hooks.install_codex(cfg.codex_home / "config.toml"))
    return 0


def cmd_init_config(args, cfg) -> int:
    path = args.config or default_config_path()
    if path.exists():
        print(f"{path} already exists")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EXAMPLE_CONFIG)
    print(f"wrote {path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="reg-a", description=__doc__)
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help="config file (default: $REG_A_CONFIG or ~/.config/reg-a/config.toml)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("hook", help="hook entry point called by Claude Code / Codex")
    s.add_argument("tool", type=_tool)
    s.add_argument("payload", nargs="?", help="Codex notify JSON payload")
    s.set_defaults(func=cmd_hook)

    s = sub.add_parser("sync", help="summarize new turns of a transcript into its session JSON")
    s.add_argument("tool", type=_tool)
    s.add_argument("transcript", type=Path)
    s.add_argument("--delay", type=float, default=0, help="seconds to wait before parsing")
    s.add_argument(
        "--retry-failed",
        action="store_true",
        help="also re-summarize stored turns whose summary failed",
    )
    s.set_defaults(func=cmd_sync)

    s = sub.add_parser("list", help="list recorded sessions")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("convert-to-pdf", help="render session JSON to a LaTeX/PDF report")
    s.add_argument("session", nargs="?", help="session id (or prefix) or path to a session JSON")
    s.add_argument("--all", action="store_true", help="convert every recorded session")
    s.add_argument("-o", "--output", type=Path, help="output dir (default: <output_dir>/reports)")
    s.add_argument("--tex-only", action="store_true", help="write the .tex, skip the PDF build")
    s.set_defaults(func=cmd_convert)

    s = sub.add_parser("install", help="register reg-a hooks with Claude Code or Codex")
    s.add_argument("tool", type=_tool)
    s.add_argument(
        "--project",
        action="store_true",
        help="Claude Code: write ./.claude/settings.json instead of the user settings",
    )
    s.set_defaults(func=cmd_install)

    s = sub.add_parser("init-config", help="write an example config file")
    s.set_defaults(func=cmd_init_config)
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    args = build_parser().parse_args(argv)
    if args.func is cmd_hook:
        return cmd_hook(args)  # loads config itself, inside its never-fail guard
    return args.func(args, load_config(args.config))


if __name__ == "__main__":
    sys.exit(main())
