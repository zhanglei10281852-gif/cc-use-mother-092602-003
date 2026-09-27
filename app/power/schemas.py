from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, model_validator

MAX_OFFSET_SECONDS = 31 * 86400


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


class PayloadCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    kind: str = Field(min_length=2, max_length=60)
    nominal_watts: float = Field(ge=0, le=100000)
    max_watts: float = Field(gt=0, le=100000)
    notes: str = Field(default="", max_length=500)


class PayloadUpdate(BaseModel):
    expected_version: int = Field(ge=1)
    name: str | None = Field(default=None, min_length=2, max_length=120)
    nominal_watts: float | None = Field(default=None, ge=0, le=100000)
    max_watts: float | None = Field(default=None, gt=0, le=100000)
    active: bool | None = None
    notes: str | None = Field(default=None, max_length=500)


class SegmentCreate(BaseModel):
    starts_at: datetime
    ends_at: datetime
    available_watts: float = Field(gt=0, le=100000)
    source: str = Field(default="", max_length=120)

    @model_validator(mode="after")
    def check_window(self) -> "SegmentCreate":
        self.starts_at = _utc(self.starts_at)
        self.ends_at = _utc(self.ends_at)
        if self.ends_at <= self.starts_at:
            raise ValueError("日照段结束时间必须晚于开始时间")
        return self


class BudgetCreate(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    total_watts: float = Field(gt=0, le=100000)
    reserve_watts: float = Field(default=0, ge=0, le=100000)
    eclipse_energy_wh: float = Field(default=0, ge=0, le=1000000)
    reason: str = Field(default="", max_length=1000)


class BudgetAdjust(BaseModel):
    expected_version: int = Field(ge=1)
    total_watts: float = Field(gt=0, le=100000)
    reserve_watts: float = Field(default=0, ge=0, le=100000)
    eclipse_energy_wh: float = Field(default=0, ge=0, le=1000000)
    reason: str = Field(min_length=2, max_length=1000)
    reevaluate: bool = False


class DeratingCreate(BaseModel):
    starts_at: datetime
    ends_at: datetime
    watts: float = Field(gt=0, le=100000)
    reason: str = Field(min_length=2, max_length=1000)

    @model_validator(mode="after")
    def check_window(self) -> "DeratingCreate":
        self.starts_at = _utc(self.starts_at)
        self.ends_at = _utc(self.ends_at)
        if self.ends_at <= self.starts_at:
            raise ValueError("降额结束时间必须晚于开始时间")
        return self


class PowerPiece(BaseModel):
    start_offset_seconds: int = Field(ge=0, le=MAX_OFFSET_SECONDS)
    end_offset_seconds: int = Field(gt=0, le=MAX_OFFSET_SECONDS)
    watts: float = Field(gt=0, le=100000)

    @model_validator(mode="after")
    def check_order(self) -> "PowerPiece":
        if self.end_offset_seconds <= self.start_offset_seconds:
            raise ValueError("功耗曲线区间的结束偏移必须大于开始偏移")
        return self


class JobSpec(BaseModel):
    job_code: str = Field(min_length=2, max_length=80, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]+$")
    payload_code: str = Field(min_length=2, max_length=64)
    starts_at: datetime
    priority: int = Field(default=50, ge=0, le=100)
    power_curve: list[PowerPiece] = Field(min_length=1, max_length=500)


class PlanCreate(BaseModel):
    code: str = Field(min_length=2, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=2, max_length=120)
    requested_by: str = Field(min_length=1, max_length=80)
    idempotency_key: str = Field(min_length=6, max_length=160)
    change_note: str = Field(default="", max_length=500)
    jobs: list[JobSpec] = Field(min_length=1, max_length=200)


class PlanVersionCreate(BaseModel):
    expected_version: int = Field(ge=1)
    change_note: str = Field(min_length=2, max_length=500)
    jobs: list[JobSpec] = Field(min_length=1, max_length=200)


class PlanTransition(BaseModel):
    expected_version: int = Field(ge=1)
    actor: str = Field(min_length=1, max_length=120)
    reason: str = Field(default="", max_length=1000)


class EvaluateRequest(BaseModel):
    budget_id: int | None = Field(default=None, ge=1)
    plan_version: int | None = Field(default=None, ge=1)
    actor: str = Field(default="system", min_length=1, max_length=120)
