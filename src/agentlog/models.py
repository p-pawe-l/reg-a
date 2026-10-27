"""Session schema.

Only `TurnSummary` is produced by the LLM; every other field is a fact
read from the agent's transcript by code.
"""

from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

Tool = Literal["claude-code", "codex"]


class TurnSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", json_schema_serialization_defaults_required=True)

    summary: str = Field(description="2-4 sentences: what the agent did in this turn and why")
    actions: list[str] = Field(description="Concrete actions taken, e.g. 'edited auth.py'")
    outcome: Literal["success", "partial", "failed", "question"] = Field(
        description="success: request fulfilled; partial: some of it; failed: not achieved; "
        "question: the agent answered or asked a question without changing anything"
    )
    open_issues: list[str] = Field(
        default_factory=list, description="Problems or follow-ups left unresolved"
    )

    @classmethod
    def llm_schema(cls) -> dict:
        """Schema with every field required, as strict JSON-schema mode demands."""
        return cls.model_json_schema(mode="serialization")


class Turn(BaseModel):
    turn_id: str
    index: int
    user_prompt: str
    started_at: AwareDatetime
    finished_at: AwareDatetime
    work_time_s: float
    model: str | None
    interrupted: bool = False
    files_changed: list[str] = []
    commands_run: list[str] = []
    final_message: str = ""
    summary: TurnSummary | None = None
    summary_error: str | None = None


class Session(BaseModel):
    session_id: str
    tool: Tool
    title: str | None = None
    cwd: str | None = None
    started_at: AwareDatetime
    transcript_path: str
    turns: list[Turn] = []

    @property
    def total_work_time_s(self) -> float:
        return sum(t.work_time_s for t in self.turns)


class ParsedTurn(BaseModel):
    """A completed turn as extracted from a transcript, before summarization."""

    turn_id: str
    user_prompt: str
    started_at: datetime
    finished_at: datetime
    work_time_s: float
    model: str | None
    interrupted: bool
    files_changed: list[str]
    commands_run: list[str]
    final_message: str
    transcript_excerpt: str  # condensed turn log fed to the summarizer


class ParsedSession(BaseModel):
    session_id: str
    tool: Tool
    title: str | None
    cwd: str | None
    started_at: datetime
    transcript_path: str
    turns: list[ParsedTurn]
