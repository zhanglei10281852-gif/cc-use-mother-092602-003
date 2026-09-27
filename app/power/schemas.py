from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class PayloadCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=120)
    baseline_power_w: float = Field(ge=0, le=100000)
    description: str = Field(default="", max_length=500)
    active: bool = True


class PayloadUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    baseline_power_w: float | None = Field(default=None, ge=0, le=100000)
    description: str | None = Field(default=None, max_length=500)
    active: bool | None = None
    expected_version: int = Field(ge=1)


class SunWindowCreate(BaseModel):
    start_at: str = Field(min_length=5, max_length=40)
    end_at: str = Field(min_length=5, max_length=40)
    power_w: float | None = Field(default=None, ge=0, le=100000)


class DeratingCreate(BaseModel):
    start_at: str = Field(min_length=5, max_length=40)
    end_at: str = Field(min_length=5, max_length=40)
    available_power_w: float = Field(ge=0, le=100000)
    reason: str = Field(min_length=2, max_length=500)


class BudgetAdjustment(BaseModel):
    sunlit_power_w: float = Field(ge=0, le=100000)
    eclipse_power_w: float = Field(ge=0, le=100000)
    battery_capacity_wh: float = Field(default=0, ge=0, le=100000000)
    max_depth_of_discharge: float = Field(default=0.8, ge=0, le=1)
    reason: str = Field(min_length=2, max_length=500)
    expected_revision: int | None = Field(default=None, ge=1)


class JobPhase(BaseModel):
    power_w: float = Field(ge=0, le=100000)
    duration_s: int = Field(ge=1, le=604800)


class JobSubmit(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str | None = Field(default=None, min_length=1, max_length=120)
    payload_code: str = Field(min_length=1, max_length=64)
    priority: int = Field(default=50, ge=0, le=100)
    scheduled_start_at: str = Field(min_length=5, max_length=40)
    phases: list[JobPhase] = Field(min_length=1, max_length=200)


class JobAmend(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    payload_code: str | None = Field(default=None, min_length=1, max_length=64)
    priority: int | None = Field(default=None, ge=0, le=100)
    scheduled_start_at: str | None = Field(default=None, min_length=5, max_length=40)
    phases: list[JobPhase] | None = Field(default=None, min_length=1, max_length=200)
    change_note: str = Field(min_length=2, max_length=500)
    expected_revision: int = Field(ge=1)


class PlanCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    job_codes: list[str] = Field(min_length=1, max_length=200)
    note: str = Field(default="", max_length=500)


class PlanUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    job_codes: list[str] | None = Field(default=None, min_length=1, max_length=200)
    expected_version: int = Field(ge=1)
    note: str = Field(default="", max_length=500)


class PlanAction(BaseModel):
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(min_length=2, max_length=500)
    expected_version: int | None = Field(default=None, ge=1)


class EvaluationRequest(BaseModel):
    actor: str = Field(default="planner", min_length=1, max_length=120)
    budget_revision: int | None = Field(default=None, ge=1)
