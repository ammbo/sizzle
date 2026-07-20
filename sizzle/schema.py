"""Data contracts (PRD §8). The beat sheet is the spine; everything downstream fills slots."""

from __future__ import annotations

from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field, field_validator, model_validator


class ShotType(str, Enum):
    GENERATE = "GENERATE"
    CAPTURE = "CAPTURE"
    RENDER = "RENDER"


# ---------------------------------------------------------------- shot specs


class GenerateSpec(BaseModel):
    kind: Literal["generate"] = "generate"
    prompt: str
    ref_image: str | None = None
    continuity_ref: str | None = None  # shot id whose last frame seeds this one (r2v)
    native_audio: bool = False  # PRD R6: if true, vo must be None and native audio wins


class CaptureSpec(BaseModel):
    kind: Literal["capture"] = "capture"
    goal: str  # a goal, not a script - the agent figures out the clicks
    start_url: str = ""
    viewport: tuple[int, int] = (1280, 720)
    max_steps: int = 25


RenderTemplate = Literal["title_card", "code_snippet", "architecture", "metric_plate"]


class RenderSpec(BaseModel):
    kind: Literal["render"] = "render"
    template: RenderTemplate
    payload: dict = Field(default_factory=dict)


ShotSpec = Annotated[Union[GenerateSpec, CaptureSpec, RenderSpec], Field(discriminator="kind")]


# ---------------------------------------------------------------- beat sheet


class CostEstimate(BaseModel):
    unit: str = "tokens"
    value: int = 0


class Shot(BaseModel):
    id: str
    beat_id: str
    type: ShotType = ShotType.RENDER  # assigned by the allocator, not the story engine
    duration_s: float
    spec: ShotSpec
    acceptance: str = ""
    vo: str | None = None
    cost_estimate: CostEstimate = Field(default_factory=CostEstimate)

    @model_validator(mode="after")
    def _native_audio_excludes_vo(self) -> "Shot":
        if isinstance(self.spec, GenerateSpec) and self.spec.native_audio and self.vo:
            raise ValueError(f"shot {self.id}: native_audio and vo are mutually exclusive (PRD R6)")
        return self


class Beat(BaseModel):
    id: str
    name: str
    intent: str
    duration_s: float
    narrative_weight: float = Field(ge=0.0, le=1.0)
    filmable: bool
    is_static_information: bool = False
    shots: list[str] = Field(default_factory=list)


class ProjectInfo(BaseModel):
    name: str
    one_liner: str = ""
    pain: str = ""
    mechanism: str = ""


class BeatSheet(BaseModel):
    max_duration_s: int = 180
    project: ProjectInfo
    beats: list[Beat]
    shots: list[Shot]

    @model_validator(mode="after")
    def _consistent(self) -> "BeatSheet":
        beat_ids = {b.id for b in self.beats}
        for s in self.shots:
            if s.beat_id not in beat_ids:
                raise ValueError(f"shot {s.id} references unknown beat {s.beat_id}")
        return self

    def total_duration(self) -> float:
        return sum(s.duration_s for s in self.shots)

    def shot(self, shot_id: str) -> Shot:
        for s in self.shots:
            if s.id == shot_id:
                return s
        raise KeyError(shot_id)

    def beat(self, beat_id: str) -> Beat:
        for b in self.beats:
            if b.id == beat_id:
                return b
        raise KeyError(beat_id)

    def shots_for_beat(self, beat_id: str) -> list[Shot]:
        return [s for s in self.shots if s.beat_id == beat_id]


# ---------------------------------------------------------------- manifest


class ShotRecord(BaseModel):
    """One row per finished shot (PRD §8.3). This is what makes the cost report and eval possible."""

    shot_id: str
    type: ShotType
    model: str | None = None
    tokens_spent: int = 0
    wall_time_s: float = 0.0
    attempts: int = 1
    verifier_outcome: str | None = None  # "pass" / failure_mode / None if unverified
    final_duration_s: float = 0.0
    output_path: str = ""


class CostReport(BaseModel):
    total_tokens: int = 0
    spend_by_type: dict[str, int] = Field(default_factory=dict)
    cost_per_finished_second: float = 0.0
    demoted_shots: list[str] = Field(default_factory=list)


class RunManifest(BaseModel):
    run_id: str
    repo_url: str
    app_url: str | None = None
    is_private_repo: bool = False
    shots: list[ShotRecord] = Field(default_factory=list)
    cost: CostReport = Field(default_factory=CostReport)
    critic_scores: list[float] = Field(default_factory=list)
    final_cut: str = ""
    final_duration_s: float = 0.0


# ---------------------------------------------------------------- critic directives


class Retime(BaseModel):
    op: Literal["RETIME"] = "RETIME"
    beat_id: str
    delta_s: float


class Reshoot(BaseModel):
    op: Literal["RESHOOT"] = "RESHOOT"
    shot_id: str
    revised_spec: dict


class Cut(BaseModel):
    op: Literal["CUT"] = "CUT"
    shot_id: str


class Reorder(BaseModel):
    op: Literal["REORDER"] = "REORDER"
    beat_ids: list[str]


class RewriteVO(BaseModel):
    op: Literal["REWRITE_VO"] = "REWRITE_VO"
    beat_id: str
    text: str


Directive = Annotated[Union[Retime, Reshoot, Cut, Reorder, RewriteVO], Field(discriminator="op")]


class CriticVerdict(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    rubric_scores: dict[str, float] = Field(default_factory=dict)
    notes: str = ""
    directives: list[Directive] = Field(default_factory=list)


# ---------------------------------------------------------------- capture verification


FailureMode = Literal["SPINNER", "ERROR_STATE", "WRONG_SCREEN", "OCCLUDED", "TIMEOUT"]


class VerifierResult(BaseModel):
    satisfied: bool
    failure_mode: FailureMode | None = None
    evidence_frame: int | None = None
    suggested_fix: str | None = None

    @field_validator("failure_mode", mode="before")
    @classmethod
    def _none_on_pass(cls, v):
        return v or None
