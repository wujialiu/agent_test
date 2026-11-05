"""Typed records for one synthetic clinical study summary.

Population, arm, and time point belong to the fact identity. Comparing two
values without first comparing those fields creates false conflicts.
"""

import json
from hashlib import sha256
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Slug = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,59}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=400)]
TEMPLATE_VERSION = "study-summary-v1"
Sections = Literal["design", "disposition", "efficacy", "safety"]


class Record(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class FactKey(Record):
    metric: Slug
    population: Slug
    arm: Slug
    timepoint: Slug

    def describe(self) -> str:
        return f"{self.metric} · {self.population} · {self.arm} · {self.timepoint}"


class FactValue(Record):
    value: ShortText
    unit: Literal["text", "participants"]
    denominator: Annotated[int, Field(gt=0, le=100_000_000, strict=True)] | None

    @model_validator(mode="after")
    def check_value(self):
        if self.unit == "text":
            if self.denominator is not None:
                raise ValueError("a text fact cannot have a denominator")
        elif (
            not self.value.isascii()
            or not self.value.isdecimal()
            or len(self.value) > 8
            or (self.value.startswith("0") and len(self.value) > 1)
            or (self.denominator is not None and int(self.value) > self.denominator)
        ):
            raise ValueError("participant count must be canonical and within the denominator")
        return self

    def signature(self) -> tuple[str, str, int | None]:
        return self.value, self.unit, self.denominator


class SourceRef(Record):
    document_id: Slug
    version: Slug
    filename: ShortText
    sha256: str


class Evidence(Record):
    id: str
    source: SourceRef
    locator: str
    excerpt: str
    key: FactKey
    fact: FactValue


class Snapshot(Record):
    id: str
    study: Slug
    label: ShortText
    sources: tuple[SourceRef, ...]
    evidence: tuple[Evidence, ...]


class Slot(Record):
    id: Slug
    label: ShortText
    key: FactKey
    unit: Literal["text", "participants"]
    needs_denominator: bool


class Section(Record):
    id: Sections
    title: str
    slots: tuple[Slot, ...]


def slot(
    slot_id: str,
    label: str,
    metric: str,
    population: str,
    arm: str,
    timepoint: str,
    unit: Literal["text", "participants"] = "participants",
    ratio: bool = False,
) -> Slot:
    return Slot(
        id=slot_id,
        label=label,
        key=FactKey(metric=metric, population=population, arm=arm, timepoint=timepoint),
        unit=unit,
        needs_denominator=ratio,
    )


def arm_section(
    section_id: Sections, title: str, metric: str, population: str, timepoint: str, ratio: bool
) -> Section:
    return Section(
        id=section_id,
        title=title,
        slots=tuple(
            slot(arm, label, metric, population, arm, timepoint, ratio=ratio)
            for arm, label in (("active", "Active arm"), ("placebo", "Placebo arm"))
        ),
    )


TEMPLATE: dict[str, Section] = {
    section.id: section
    for section in (
        Section(
            id="design",
            title="Study design",
            slots=(slot("design", "Design", "design", "protocol", "all", "baseline", "text"),),
        ),
        arm_section("disposition", "Enrollment", "randomized", "itt", "baseline", False),
        arm_section("efficacy", "Week 12 efficacy", "responders", "itt", "week-12", True),
        arm_section("safety", "Week 12 safety", "participants_with_ae", "safety", "week-12", True),
    )
}


class Proposal(Record):
    slot_id: Slug
    evidence_id: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    fact: FactValue


class Proposals(Record):
    claims: Annotated[tuple[Proposal, ...], Field(max_length=8)]


class Finding(Record):
    code: Literal[
        "missing_evidence",
        "conflicting_evidence",
        "incompatible_evidence",
        "missing_proposal",
        "unexpected_proposal",
        "duplicate_proposal",
        "invalid_citation",
        "claim_mismatch",
    ]
    slot_id: Slug
    message: str
    evidence_ids: tuple[str, ...] = ()


class Claim(Record):
    slot_id: Slug
    sentence: str
    evidence_id: str


class Draft(Record):
    template_version: str = TEMPLATE_VERSION
    evidence_signature: str
    generator: str
    claims: tuple[Claim, ...]
    findings: tuple[Finding, ...]
    evidence: tuple[Evidence, ...]


def fingerprint(parts: list[str]) -> str:
    return sha256(json.dumps(parts, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
