from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Stage(StrEnum):
    REVIEW = "Case review"
    PRECONFERENCE = "Pre-conference"
    CONFERENCE = "Conference"
    REPAIR = "Repair & follow-up"
    REINTEGRATION = "Reintegration"
    CLOSED = "Closed"
    PAUSED = "Paused"


STAGES = list(Stage)
ROLE_SLOTS = {
    "Facilitator": 1,
    "Co-facilitator": 1,
    "Community member": 3,
    "Support liaison": 1,
}
MINIMUM_TEAM = {
    "Facilitator": 1,
    "Co-facilitator": 1,
    "Community member": 2,
    "Support liaison": 1,
}


class DomainError(Exception):
    """An intentional, safe-to-display workflow or authorization error."""


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=800)
    source_ids: list[str] = Field(min_length=1, max_length=10)


class LanguageTip(BaseModel):
    model_config = ConfigDict(extra="forbid")
    suggested: str = Field(max_length=500)
    reconsider: str = Field(max_length=500)
    reason: str = Field(max_length=500)


class ProposedAction(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["task", "flag"]
    title: str = Field(min_length=1, max_length=240)
    owner: str = Field(min_length=1, max_length=80)
    due_days: int = Field(ge=0, le=90)
    source_ids: list[str] = Field(min_length=1, max_length=10)


class AIDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(min_length=1, max_length=4000)
    facts: list[Fact] = Field(default_factory=list, max_length=15)
    questions: list[str] = Field(default_factory=list, max_length=8)
    language_tips: list[LanguageTip] = Field(default_factory=list, max_length=5)
    actions: list[ProposedAction] = Field(default_factory=list, max_length=10)
    learning_points: list[str] = Field(default_factory=list, max_length=5)


AI_TASKS = ["Preparation plan", "Follow-up plan", "Case summary", "Learning reflection"]
