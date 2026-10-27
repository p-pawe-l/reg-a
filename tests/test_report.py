import re
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from reg_a import cli, report
from reg_a.models import Session, Turn, TurnSummary
from reg_a.report import (
    LatexError,
    build_pdf,
    clip,
    convert,
    duration,
    paragraphs,
    render_tex,
    tex,
    tt,
)
from reg_a.store import save_session

SID = "11111111-2222-4333-8444-555555555555"
SID2 = "11112222-0000-4000-8000-000000000000"
CODEX_SID = "01990000-0000-7000-8000-000000000001"
T0 = datetime(2026, 1, 2, 10, 0, tzinfo=UTC)
SPECIALS = "back\\slash {brace} $dollar & amp #hash 50% under_score ~tilde ^caret"


@pytest.fixture(autouse=True)
def utc(monkeypatch):
    """localtime() formats in the local zone; pin it so times are predictable."""
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    yield
    monkeypatch.undo()
    time.tzset()


def make_turn(index: int = 1, **kw) -> Turn:
    fields = dict(
        turn_id=f"turn-{index}",
        index=index,
        user_prompt="Fix the bug",
        started_at=T0 + timedelta(minutes=index),
        finished_at=T0 + timedelta(minutes=index, seconds=42),
        work_time_s=42.0,
        model="claude-test",
        summary=TurnSummary(summary="Fixed it.", actions=["edited a.py"], outcome="success"),
    )
    fields.update(kw)
    return Turn(**fields)


def make_session(turns: list[Turn] | None = None, **kw) -> Session:
    fields = dict(
        session_id=SID,
        tool="claude-code",
        cwd="/work/demo",
        started_at=T0,
        transcript_path="/tmp/t.jsonl",
        turns=[make_turn()] if turns is None else turns,
    )
    fields.update(kw)
    return Session(**fields)


def body(doc: str) -> str:
    return doc.split(r"\begin{document}", 1)[1]


def assert_structurally_valid(doc: str) -> None:
    """Braces balance (ignoring escaped ones), \\begin/\\end nest, and no raw specials
    that would break LaTeX appear after \\begin{document}."""
    depth = 0
    stack: list[str] = []
    i = 0
    while i < len(doc):
        c = doc[i]
        if c == "\\":
            m = re.match(r"\\(begin|end)\{([^}]*)\}", doc[i:])
            if m:
                if m.group(1) == "begin":
                    stack.append(m.group(2))
                else:
                    assert stack and stack[-1] == m.group(2), f"unmatched \\end{{{m.group(2)}}}"
                    stack.pop()
                i += len(m.group(0))
                continue
            i += 2  # control symbol or first letter of a control word
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            assert depth >= 0, f"unbalanced '}}' near {doc[max(0, i - 60) : i + 20]!r}"
        i += 1
    assert depth == 0, "unbalanced braces"
    assert stack == [], f"unclosed environments: {stack}"
    unescaped = re.findall(r"(?<!\\)[$%_^~#]", body(doc))
    assert unescaped == [], f"raw special chars in body: {unescaped}"


# --- tex / tt / clip / duration / paragraphs ----------------------------------------------


def test_tex_escapes_every_special_char():
    assert tex(SPECIALS) == (
        r"back\textbackslash{}slash \{brace\} \$dollar \& amp \#hash 50\% under\_score "
        r"\textasciitilde{}tilde \textasciicircum{}caret"
    )


def test_tex_backslash_escape_is_not_reescaped():
    assert tex("\\{") == r"\textbackslash{}\{"


def test_tex_strips_control_characters_but_keeps_tab_and_newlines():
    assert tex("a\x00b\x07c\x1bd\x7fe\tf\ng\rh") == "abcde\tf\ng\rh"


def test_tex_accepts_non_strings_and_keeps_polish_letters():
    assert tex(42) == "42"
    assert tex("zażółć gęślą jaźń") == "zażółć gęślą jaźń"


def test_tt_wraps_in_texttt_and_allows_breaks_after_separators():
    assert tt("a/b.c-d=e,f_g") == (
        r"\texttt{a/\allowbreak{}b.\allowbreak{}c-\allowbreak{}d=\allowbreak{}"
        r"e,\allowbreak{}f\_\allowbreak{}g}"
    )


def test_tt_escapes_specials():
    assert tt("$HOME & ~") == r"\texttt{\$HOME \& \textasciitilde{}}"


def test_clip_short_value_unchanged_but_whitespace_collapsed_for_small_limits():
    assert clip("a\n  b\tc", 160) == "a b c"


def test_clip_long_value_gets_ellipsis_within_limit():
    out = clip("word " * 100, 20)
    assert out.endswith("…")
    assert len(out) <= 20
    assert not out[:-1].endswith(" ")


def test_clip_large_limit_keeps_newlines():
    assert clip("a\n\nb", 3000) == "a\n\nb"


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (0, "0s"),
        (42.4, "42s"),
        (59.6, "1m 00s"),
        (61, "1m 01s"),
        (3599, "59m 59s"),
        (3600, "1h 00m"),
        (7322, "2h 02m"),
    ],
)
def test_duration_formats_seconds_minutes_hours(seconds, expected):
    assert duration(seconds) == expected


def test_paragraphs_keeps_blank_line_breaks_and_single_newlines():
    assert paragraphs("one\ntwo\n\n\n  three_x  \n") == "one\\newline{}\ntwo\n\nthree\\_x"


def test_paragraphs_line_starting_with_bracket_is_not_parsed_as_linebreak_argument():
    # LaTeX's \\ skips whitespace and reads a following [..] as its optional length, so a
    # prompt line like "[x] done" after a newline makes the build fail ("Missing number").
    out = paragraphs("todo:\n[x] done\n*bold*")
    assert not re.search(r"\\\\\s*[\[*]", out), out


# --- render_tex ----------------------------------------------------------------------------


def test_render_english_by_default_with_overview_and_turn_details():
    doc = render_tex(make_session(), "en")
    assert r"\usepackage[english,shorthands=off]{babel}" in doc
    assert "Session report" in doc and "Overview" in doc and "Turns in detail" in doc
    assert r"\outcome{success}{success}" in doc
    assert "Fixed it." in doc and r"\item{}edited a.py" in doc
    assert "2026-01-02 10:00" in doc and "10:01" in doc and "42s" in doc
    assert r"\label{turn:1}" in doc and r"\hyperref[turn:1]{1}" in doc
    assert_structurally_valid(doc)


def test_render_polish_uses_polish_babel_and_labels():
    doc = render_tex(make_session(), "pl")
    assert r"\usepackage[polish,shorthands=off]{babel}" in doc
    assert "Podsumowanie" in doc and "Raport sesji" in doc and "Łączny czas pracy" in doc
    assert r"\outcome{success}{sukces}" in doc
    assert "Summary" not in doc
    assert_structurally_valid(doc)


def test_render_language_is_case_insensitive():
    assert r"[polish,shorthands=off]{babel}" in render_tex(make_session(), "PL")


def test_render_known_babel_language_without_labels_uses_english_labels():
    doc = render_tex(make_session(), "de")
    assert r"\usepackage[ngerman,shorthands=off]{babel}" in doc
    assert "Session report" in doc


def test_render_unknown_language_falls_back_to_english():
    doc = render_tex(make_session(), "xx")
    assert r"\usepackage[english,shorthands=off]{babel}" in doc
    assert "Summary" in doc


def test_render_escapes_specials_in_every_user_and_model_field():
    turn = make_turn(
        user_prompt=SPECIALS,
        model="m_1",
        files_changed=["/src/a_b/{x}%.py"],
        commands_run=["echo $HOME && grep -r '#x' ~ | sed s/^a/b/"],
        summary=TurnSummary(
            summary=SPECIALS, actions=[SPECIALS], outcome="partial", open_issues=[SPECIALS]
        ),
    )
    session = make_session([turn], title="Title_with & % $", cwd="/home/u/my_proj#1")
    doc = render_tex(session, "en")
    assert_structurally_valid(doc)
    assert doc.count(tex(SPECIALS)) >= 4  # table prompt, prompt quote, summary, action, issue
    assert r"\item \texttt{/\allowbreak{}src/\allowbreak{}a\_\allowbreak{}b/" in doc
    assert r"\$HOME \&\& grep" in doc
    assert r"Title\_with \& \% \$" in doc
    assert r"\outcome{partial}{[partial]}" in doc


def test_render_strips_control_characters_from_prompt():
    doc = render_tex(make_session([make_turn(user_prompt="bell\x07 esc\x1b[31m red")]), "en")
    assert "\x07" not in doc and "\x1b" not in doc
    assert "bell esc[31m red" in doc


@pytest.mark.parametrize(
    "prompt",
    [
        r"\VAR{s.session_id}",
        r"\BLOCK{if true}x\BLOCK{endif}",
        "{{ 7*7 }}",
        "{% if 1 %}x{% endif %}",
        r"\#{ comment }",
    ],
)
def test_render_jinja_looking_prompt_is_text_not_template(prompt):
    doc = render_tex(
        make_session([make_turn(user_prompt=prompt, final_message=prompt, summary=None)]), "en"
    )
    assert tex(prompt) in doc
    assert "49" not in doc
    assert_structurally_valid(doc)


def test_render_turn_without_summary_shows_error_and_final_message():
    turn = make_turn(
        summary=None,
        summary_error="LLM returned invalid_json {",
        final_message="All done & dusted.\n\nSecond para.",
    )
    doc = render_tex(make_session([turn]), "en")
    assert r"\textcolor{failed}{No summary available}: " in doc
    assert r"invalid\_json \{" in doc
    assert "Agent's final message" in doc
    assert "All done \\& dusted.\n\nSecond para." in doc
    assert r"\textcolor{muted}{---}" in doc  # overview outcome column
    assert_structurally_valid(doc)


def test_render_turn_without_summary_or_error_or_reply():
    doc = render_tex(make_session([make_turn(summary=None)]), "pl")
    assert r"\textcolor{failed}{Brak podsumowania}" in doc
    assert "Ostatnia odpowiedź agenta" not in doc
    assert_structurally_valid(doc)


def test_render_interrupted_turn_is_marked_in_table_and_details():
    doc = render_tex(make_session([make_turn(interrupted=True)]), "en")
    assert r"\textcolor{muted}{(interrupted)}" in doc
    assert r" · interrupted}" in doc
    assert "przerwano" in render_tex(make_session([make_turn(interrupted=True)]), "pl")


def test_render_non_interrupted_turn_has_no_marker():
    assert "interrupted" not in render_tex(make_session(), "en")


def test_render_truncates_commands_after_ten_with_more_line():
    doc = render_tex(make_session([make_turn(commands_run=[f"cmd{i}" for i in range(13)])]), "en")
    assert "cmd9" in doc and "cmd10" not in doc
    assert r"\item[] \textcolor{muted}{+3 more}" in doc
    assert "+3 więcej" in render_tex(
        make_session([make_turn(commands_run=[f"c{i}" for i in range(13)])]), "pl"
    )


def test_render_exactly_ten_commands_has_no_more_line():
    doc = render_tex(make_session([make_turn(commands_run=[f"cmd{i}" for i in range(10)])]), "en")
    assert "cmd9" in doc and "more}" not in doc


def test_render_session_with_zero_turns():
    doc = render_tex(make_session([], cwd=None, title=None), "en")
    assert_structurally_valid(doc)
    assert "longtable" not in body(doc) and "Turns in detail" not in doc
    assert "Turns & 0" in doc and "Total work time & 0s" in doc and "Models & none" in doc
    assert "Directory" not in doc
    assert rf"Session report: {tex(SID)}" in doc


def test_render_lists_distinct_models_and_total_work_time():
    turns = [
        make_turn(1, model="b", work_time_s=3000),
        make_turn(2, model="a", work_time_s=700),
        make_turn(3, model=None, work_time_s=0),
    ]
    doc = render_tex(make_session(turns), "en")
    assert "Models & a, b" in doc
    assert "Total work time & 1h 01m" in doc


def test_render_clips_long_prompt_in_table_and_detail():
    prompt = "x" * 5000
    doc = render_tex(make_session([make_turn(user_prompt=prompt)]), "en")
    assert "x" * 159 + "…" in doc
    assert "x" * 2999 + "…" in doc
    assert "x" * 3000 not in doc


def test_render_multiline_prompt_keeps_paragraphs_in_detail_and_flattens_in_table():
    doc = render_tex(make_session([make_turn(user_prompt="line one\nline two\n\npara")]), "en")
    assert "& line one line two para \\\\" in doc
    assert "line one\\newline{}\nline two\n\npara" in doc


# --- build_pdf -----------------------------------------------------------------------------


@pytest.fixture
def tex_file(tmp_path):
    path = tmp_path / "out" / "s.tex"
    path.parent.mkdir()
    path.write_text("x")
    return path


@pytest.mark.parametrize("missing", ["latexmk", "lualatex"])
def test_build_pdf_without_tex_tools_raises_with_install_hint(tex_file, monkeypatch, missing):
    monkeypatch.setattr(
        report.shutil, "which", lambda name: None if name == missing else f"/usr/bin/{name}"
    )
    monkeypatch.setattr(
        report.subprocess, "run", lambda *a, **k: pytest.fail("must not run latexmk")
    )
    with pytest.raises(LatexError) as e:
        build_pdf(tex_file)
    assert "pacman -S texlive-basic" in str(e.value)
    assert str(tex_file) in str(e.value)


def test_build_pdf_failing_latexmk_raises_with_log_hint(tex_file, monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 12, stdout="! Undefined control sequence.", stderr=""
        )

    monkeypatch.setattr(report.subprocess, "run", run)
    with pytest.raises(LatexError) as e:
        build_pdf(tex_file)
    assert str(tex_file.with_suffix(".log")) in str(e.value)
    assert "Undefined control sequence" in str(e.value)
    assert len(calls) == 1  # no cleanup after a failed build


def test_build_pdf_success_returns_pdf_and_cleans_aux_files(tex_file, monkeypatch):
    monkeypatch.setattr(report.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(report.subprocess, "run", run)
    assert build_pdf(tex_file) == tex_file.with_suffix(".pdf")
    build, clean = calls
    assert build[0] == "/usr/bin/latexmk"
    assert "-lualatex" in build and "-halt-on-error" in build
    assert f"-outdir={tex_file.parent}" in build and build[-1] == str(tex_file)
    assert clean[:2] == ["/usr/bin/latexmk", "-c"] and clean[-1] == str(tex_file)


# --- convert -------------------------------------------------------------------------------


def write_session(dir_: Path, session: Session) -> Path:
    path = dir_ / f"{session.tool}-{session.session_id}.json"
    save_session(path, session)
    return path


def test_convert_tex_only_writes_tex_named_after_json(tmp_path, monkeypatch):
    monkeypatch.setattr(report, "build_pdf", lambda p: pytest.fail("must not build"))
    src = write_session(tmp_path / "sessions", make_session())
    out = convert(src, tmp_path / "new" / "reports", "pl", tex_only=True)
    assert out == tmp_path / "new" / "reports" / f"claude-code-{SID}.tex"
    assert "Podsumowanie" in out.read_text(encoding="utf-8")


def test_convert_without_tex_only_builds_pdf(tmp_path, monkeypatch):
    built = []
    monkeypatch.setattr(report, "build_pdf", lambda p: built.append(p) or p.with_suffix(".pdf"))
    src = write_session(tmp_path, make_session())
    out = convert(src, tmp_path / "r", "en")
    assert built == [tmp_path / "r" / f"claude-code-{SID}.tex"]
    assert out.suffix == ".pdf"


# --- CLI: resolve_session / convert-to-pdf -------------------------------------------------


@pytest.fixture
def sessions(tmp_path):
    d = tmp_path / "sessions"
    return {
        "a": write_session(d, make_session()),
        "b": write_session(d, make_session(session_id=SID2)),
        "codex": write_session(d, make_session(session_id=CODEX_SID, tool="codex")),
    }


def config_file(tmp_path, language="en"):
    path = tmp_path / "reg-a.toml"
    path.write_text(
        f'language = "{language}"\noutput_dir = "{tmp_path / "sessions"}"\n'
        '[llm]\nbase_url = "http://llm.test/v1"\n'
    )
    return str(path)


def test_resolve_session_by_unique_prefix(tmp_path, sessions):
    assert cli.resolve_session("11111111", tmp_path / "sessions") == sessions["a"]
    assert cli.resolve_session("0199", tmp_path / "sessions") == sessions["codex"]
    assert cli.resolve_session(SID2, tmp_path / "sessions") == sessions["b"]


def test_resolve_session_by_existing_path(tmp_path, sessions):
    assert cli.resolve_session(str(sessions["b"]), tmp_path / "elsewhere") == sessions["b"]


def test_resolve_session_ambiguous_prefix_exits(tmp_path, sessions):
    with pytest.raises(SystemExit, match="2 sessions matching '1111'"):
        cli.resolve_session("1111", tmp_path / "sessions")


def test_resolve_session_unknown_prefix_exits(tmp_path, sessions):
    with pytest.raises(SystemExit, match="no session matching 'ffff'"):
        cli.resolve_session("ffff", tmp_path / "sessions")


def test_convert_cli_tex_only_by_prefix_writes_to_default_reports_dir(tmp_path, sessions, capsys):
    assert (
        cli.main(["--config", config_file(tmp_path, "pl"), "convert-to-pdf", "0199", "--tex-only"])
        == 0
    )
    out = tmp_path / "sessions" / "reports" / f"codex-{CODEX_SID}.tex"
    assert capsys.readouterr().out.strip() == str(out)
    assert r"\usepackage[polish,shorthands=off]{babel}" in out.read_text(encoding="utf-8")


def test_convert_cli_output_dir_option(tmp_path, sessions):
    cli.main(
        [
            "--config",
            config_file(tmp_path),
            "convert-to-pdf",
            "11111111",
            "--tex-only",
            "-o",
            str(tmp_path / "custom"),
        ]
    )
    assert (tmp_path / "custom" / f"claude-code-{SID}.tex").exists()


def test_convert_cli_all_converts_every_session(tmp_path, sessions, capsys):
    assert (
        cli.main(
            [
                "--config",
                config_file(tmp_path),
                "convert-to-pdf",
                "--all",
                "--tex-only",
                "-o",
                str(tmp_path / "r"),
            ]
        )
        == 0
    )
    assert sorted(p.name for p in (tmp_path / "r").glob("*.tex")) == sorted(
        p.with_suffix(".tex").name for p in sessions.values()
    )
    assert len(capsys.readouterr().out.splitlines()) == 3


@pytest.mark.parametrize("ref", ["1111", "ffff"])
def test_convert_cli_bad_prefix_exits(tmp_path, sessions, ref):
    with pytest.raises(SystemExit) as e:
        cli.main(["--config", config_file(tmp_path), "convert-to-pdf", ref, "--tex-only"])
    assert "matching" in str(e.value.code)


def test_convert_cli_without_session_or_all_exits(tmp_path):
    with pytest.raises(SystemExit, match="--all"):
        cli.main(["--config", config_file(tmp_path), "convert-to-pdf"])


def test_convert_cli_latex_error_exits_1_with_message_on_stderr(
    tmp_path, sessions, monkeypatch, capsys
):
    monkeypatch.setattr(report.shutil, "which", lambda name: None)
    assert cli.main(["--config", config_file(tmp_path), "convert-to-pdf", "--all"]) == 1
    err = capsys.readouterr().err
    assert f"claude-code-{SID}.json: latexmk and lualatex are required" in err
    assert err.count("are required") == 3  # one failure does not stop the others
    assert (tmp_path / "sessions" / "reports" / f"claude-code-{SID}.tex").exists()


def test_item_text_starting_with_bracket_is_not_taken_as_item_label():
    s = make_session(
        [
            make_turn(
                summary=TurnSummary(
                    summary="s",
                    actions=["[README] edited"],
                    outcome="success",
                    open_issues=["[x] left"],
                )
            )
        ]
    )
    doc = render_tex(s, "en")
    assert r"\item{}[README] edited" in doc and r"\item{}[x] left" in doc


def test_tex_breaks_ligatures_so_double_dash_survives():
    assert tex("run --force") == "run -{}-force"


def test_turn_heading_has_hyperref_anchor():
    assert r"\phantomsection\label{turn:1}" in render_tex(make_session(), "en")
