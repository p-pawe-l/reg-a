"""Session JSON → LaTeX report → PDF (lualatex via latexmk).

Every string from a prompt, transcript or model passes through `tex()` (or
`tt()` for monospace) before reaching the template.
"""

import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from jinja2 import Environment, PackageLoader, StrictUndefined

from reg_a.models import Session

TEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "{": r"\{",
    "}": r"\}",
    "$": r"\$",
    "&": r"\&",
    "#": r"\#",
    "%": r"\%",
    "_": r"\_",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}
_SPECIALS_RE = re.compile(r"[\\{}$&#%_~^]")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_LIGATURE_RE = re.compile(r"-(?=-)|`|'(?=')|<(?=<)|>(?=>)|!(?=`)|\?(?=`)")
_BREAK_AFTER_RE = re.compile(r"(/|\\_|\.|-|=|,)")

LABELS = {
    "en": {
        "report": "Session report",
        "tool": "Tool",
        "session": "Session",
        "directory": "Directory",
        "started": "Started",
        "turns": "Turns",
        "work_time": "Total work time",
        "models": "Models",
        "overview": "Overview",
        "time": "Time",
        "duration": "Duration",
        "outcome": "Outcome",
        "prompt": "Prompt",
        "details": "Turns in detail",
        "turn": "Turn",
        "summary": "Summary",
        "actions": "Actions",
        "files": "Files changed",
        "commands": "Commands run",
        "open_issues": "Open issues",
        "interrupted": "interrupted",
        "no_summary": "No summary available",
        "agent_reply": "Agent's final message",
        "more": "more",
        "none": "none",
        "outcomes": {
            "success": "success",
            "partial": "partial",
            "failed": "failed",
            "question": "question",
        },
    },
    "pl": {
        "report": "Raport sesji",
        "tool": "Narzędzie",
        "session": "Sesja",
        "directory": "Katalog",
        "started": "Rozpoczęto",
        "turns": "Tury",
        "work_time": "Łączny czas pracy",
        "models": "Modele",
        "overview": "Przegląd",
        "time": "Godzina",
        "duration": "Czas",
        "outcome": "Wynik",
        "prompt": "Polecenie",
        "details": "Szczegóły tur",
        "turn": "Tura",
        "summary": "Podsumowanie",
        "actions": "Działania",
        "files": "Zmienione pliki",
        "commands": "Wykonane polecenia",
        "open_issues": "Otwarte kwestie",
        "interrupted": "przerwano",
        "no_summary": "Brak podsumowania",
        "agent_reply": "Ostatnia odpowiedź agenta",
        "more": "więcej",
        "none": "brak",
        "outcomes": {
            "success": "sukces",
            "partial": "częściowo",
            "failed": "porażka",
            "question": "pytanie",
        },
    },
}
BABEL_LANGUAGES = {
    "pl": "polish",
    "en": "english",
    "de": "ngerman",
    "fr": "french",
    "es": "spanish",
    "uk": "ukrainian",
}


def tex(value: object) -> str:
    text = _CONTROL_RE.sub("", str(value))
    text = _SPECIALS_RE.sub(lambda m: TEX_SPECIALS[m.group()], text)
    return _LIGATURE_RE.sub(r"\g<0>{}", text)  # keep "--force" from becoming an en dash


def tt(value: object) -> str:
    """Monospace text with break points, so long paths and commands wrap."""
    return r"\texttt{" + _BREAK_AFTER_RE.sub(r"\1\\allowbreak{}", tex(value)) + "}"


def clip(value: str, limit: int) -> str:
    value = " ".join(value.split()) if limit < 300 else value
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"


def duration(seconds: float) -> str:
    s = round(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h {m:02d}m"


def localtime(dt: datetime, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return dt.astimezone().strftime(fmt)


def paragraphs(value: str) -> str:
    """Escaped text keeping blank-line paragraph breaks and single newlines."""
    blocks = [b.strip() for b in re.split(r"\n\s*\n", value) if b.strip()]
    # \newline{} rather than \\, which would read a following "[" or "*" as its argument
    return "\n\n".join(tex(b).replace("\n", r"\newline{}" + "\n") for b in blocks)


def _env() -> Environment:
    env = Environment(
        loader=PackageLoader("reg_a", "templates"),
        block_start_string=r"\BLOCK{",
        block_end_string="}",
        variable_start_string=r"\VAR{",
        variable_end_string="}",
        comment_start_string=r"\#{",
        comment_end_string="}",
        trim_blocks=True,
        lstrip_blocks=True,
        autoescape=False,
        undefined=StrictUndefined,
    )
    env.filters.update(
        tex=tex, tt=tt, clip=clip, duration=duration, localtime=localtime, paragraphs=paragraphs
    )
    return env


def render_tex(session: Session, language: str) -> str:
    lang = language.lower()
    models = sorted({t.model for t in session.turns if t.model})
    return (
        _env()
        .get_template("report.tex.j2")
        .render(
            s=session,
            L=LABELS.get(lang, LABELS["en"]),
            babel=BABEL_LANGUAGES.get(lang, "english"),
            models=models,
            max_commands=10,
        )
    )


class LatexError(RuntimeError):
    pass


def build_pdf(tex_path: Path) -> Path:
    latexmk = shutil.which("latexmk")
    if not latexmk or not shutil.which("lualatex"):
        raise LatexError(
            "latexmk and lualatex are required to build the PDF. On Arch: "
            "sudo pacman -S texlive-basic texlive-latexextra texlive-langextra "
            "texlive-binextra texlive-fontsrecommended. The .tex file was written to "
            f"{tex_path}."
        )
    outdir = tex_path.parent
    proc = subprocess.run(
        [
            latexmk,
            "-lualatex",
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-outdir={outdir}",
            str(tex_path),
        ],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        log = tex_path.with_suffix(".log")
        raise LatexError(f"LaTeX build failed; see {log}\n{proc.stdout[-2000:]}")
    subprocess.run([latexmk, "-c", f"-outdir={outdir}", str(tex_path)], capture_output=True)
    return tex_path.with_suffix(".pdf")


def convert(session_json: Path, out_dir: Path, language: str, tex_only: bool = False) -> Path:
    session = Session.model_validate_json(session_json.read_text(encoding="utf-8"))
    out_dir.mkdir(parents=True, exist_ok=True)
    tex_path = out_dir / f"{session_json.stem}.tex"
    tex_path.write_text(render_tex(session, language), encoding="utf-8")
    return tex_path if tex_only else build_pdf(tex_path)
