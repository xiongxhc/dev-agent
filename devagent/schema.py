"""Typed, validated artifacts that flow between phases: Brief -> Spec -> Plan.

These are the frozen contracts the Executor seam consumes. The defining property of a
Spec is that EVERY acceptance check is machine-checkable (a route+status, or a
selector on a route) — the verify phase computes a bool from each without the model.
The opinionated stack is fixed here (research Q1): the model fills a Spec, it does not
choose a framework."""

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# The one blessed stack (research Q1). The scaffold template matches this.
STACK = "vite-react-tailwind"


class AcceptanceCheck(BaseModel):
    """A single machine-checkable acceptance criterion."""

    kind: Literal["route_status", "selector_present"]
    route: str = Field(..., pattern=r"^/")  # must be a real app path
    expected_status: int = 200
    selector: str | None = None  # required for kind == selector_present

    @field_validator("selector")
    @classmethod
    def _selector_required_for_selector_kind(cls, v, info):
        if info.data.get("kind") == "selector_present" and not v:
            raise ValueError("selector_present check requires a non-empty selector")
        return v


class Brief(BaseModel):
    """Normalized intake output."""

    source: Literal["prd", "url"]
    title: str = Field(..., min_length=1)
    summary: str = Field(..., min_length=1)
    requirements: list[str] = Field(default_factory=list)


class Spec(BaseModel):
    title: str = Field(..., min_length=1)
    stack: Literal["vite-react-tailwind"] = STACK
    pages: list[str] = Field(..., min_length=1)
    components: list[str] = Field(default_factory=list)
    acceptance_checks: list[AcceptanceCheck] = Field(..., min_length=1)


class Task(BaseModel):
    id: str
    description: str = Field(..., min_length=1)
    owned_files: list[str] = Field(..., min_length=1)  # disjoint across tasks (see Plan)


class Plan(BaseModel):
    tasks: list[Task] = Field(..., min_length=1)

    @field_validator("tasks")
    @classmethod
    def _file_ownership_is_disjoint(cls, tasks):
        seen: dict[str, str] = {}
        for t in tasks:
            for f in t.owned_files:
                if f in seen:
                    raise ValueError(
                        f"file {f!r} owned by both task {seen[f]!r} and {t.id!r} "
                        "— build subagents would collide"
                    )
                seen[f] = t.id
        return tasks
