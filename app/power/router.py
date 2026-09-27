from __future__ import annotations

from fastapi import APIRouter, Query

from app.power.schemas import (
    BudgetAdjustment,
    DeratingCreate,
    EvaluationRequest,
    JobAmend,
    JobSubmit,
    PayloadCreate,
    PayloadUpdate,
    PlanAction,
    PlanCreate,
    PlanUpdate,
    SunWindowCreate,
)
from app.power.service import PowerBudgetService

router = APIRouter(prefix="/api/power", tags=["卫星功率预算"])


def service() -> PowerBudgetService:
    return PowerBudgetService()


# ---------- 载荷 ----------
@router.post("/payloads", status_code=201)
def register_payload(payload: PayloadCreate, actor: str = Query(..., min_length=1)):
    return service().register_payload(payload.model_dump(), actor)


@router.get("/payloads")
def list_payloads():
    return {"items": service().list_payloads()}


@router.put("/payloads/{code}")
def update_payload(code: str, payload: PayloadUpdate, actor: str = Query(..., min_length=1)):
    changes = payload.model_dump(exclude_unset=True)
    changes["expected_version"] = payload.expected_version
    return service().update_payload(code, changes, actor)


# ---------- 日照段 / 临时降额 ----------
@router.post("/sun-windows", status_code=201)
def add_sun_window(payload: SunWindowCreate, actor: str = Query(..., min_length=1)):
    return service().add_sun_window(payload.model_dump(), actor)


@router.get("/sun-windows")
def list_sun_windows():
    return {"items": service().list_sun_windows()}


@router.post("/deratings", status_code=201)
def add_derating(payload: DeratingCreate, actor: str = Query(..., min_length=1)):
    return service().add_derating(payload.model_dump(), actor)


@router.get("/deratings")
def list_deratings():
    return {"items": service().list_deratings()}


# ---------- 功率预算版本 ----------
@router.get("/budget")
def current_budget():
    return service().current_budget()


@router.get("/budget/revisions")
def list_budget_revisions():
    return {"items": service().list_budget_revisions()}


@router.post("/budget/revisions", status_code=201)
def adjust_budget(payload: BudgetAdjustment, actor: str = Query(..., min_length=1)):
    return service().adjust_budget(payload.model_dump(), actor)


# ---------- 作业申请 ----------
@router.post("/jobs", status_code=201)
def submit_job(payload: JobSubmit, actor: str = Query(..., min_length=1)):
    return service().submit_job(payload.model_dump(), actor)


@router.get("/jobs")
def list_jobs():
    return {"items": service().list_jobs()}


@router.get("/jobs/{code}")
def get_job(code: str):
    return service().get_job(code)


@router.post("/jobs/{code}/amendments", status_code=201)
def amend_job(code: str, payload: JobAmend, actor: str = Query(..., min_length=1)):
    return service().amend_job(code, payload.model_dump(exclude_unset=True), actor)


@router.get("/jobs/{code}/revisions")
def list_job_revisions(code: str):
    return {"items": service().list_job_revisions(code)}


@router.get("/jobs/{code}/revisions/compare")
def compare_job_revisions(code: str, from_revision: int = Query(..., ge=1), to_revision: int = Query(..., ge=1)):
    return service().compare_job_revisions(code, from_revision, to_revision)


@router.post("/jobs/{code}/evaluations", status_code=201)
def evaluate_job(code: str, payload: EvaluationRequest):
    return service().evaluate_job(code, payload.actor, payload.budget_revision)


# ---------- 计划草案 / 发布 / 撤回 ----------
@router.post("/plans", status_code=201)
def create_plan(payload: PlanCreate, actor: str = Query(..., min_length=1)):
    return service().create_plan(payload.model_dump(), actor)


@router.get("/plans")
def list_plans(status: str | None = Query(default=None, pattern="^(draft|published|withdrawn)$")):
    return {"items": service().list_plans(status)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int):
    return service().get_plan(plan_id)


@router.put("/plans/{plan_id}")
def update_plan(plan_id: int, payload: PlanUpdate, actor: str = Query(..., min_length=1)):
    return service().update_plan(plan_id, payload.model_dump(exclude_unset=True), actor)


@router.post("/plans/{plan_id}/evaluations", status_code=201)
def evaluate_plan(plan_id: int, payload: EvaluationRequest):
    return service().evaluate_plan(plan_id, payload.actor, payload.budget_revision)


@router.post("/plans/{plan_id}/publish")
def publish_plan(plan_id: int, payload: PlanAction):
    return service().publish_plan(plan_id, payload.actor, payload.reason, payload.expected_version)


@router.post("/plans/{plan_id}/withdraw")
def withdraw_plan(plan_id: int, payload: PlanAction):
    return service().withdraw_plan(plan_id, payload.actor, payload.reason, payload.expected_version)


@router.get("/plans/{plan_id}/versions")
def list_plan_versions(plan_id: int):
    return {"items": service().list_plan_versions(plan_id)}


@router.get("/plans/{plan_id}/versions/compare")
def compare_plan_versions(plan_id: int, from_version: int = Query(..., ge=1), to_version: int = Query(..., ge=1)):
    return service().compare_plan_versions(plan_id, from_version, to_version)


# ---------- 重评与计算可追溯 ----------
@router.post("/recheck/{scope}/{subject_key}", status_code=201)
def recheck(scope: str, subject_key: str, payload: EvaluationRequest):
    return service().recheck(scope, subject_key, payload.actor, payload.budget_revision)


@router.get("/evaluations")
def list_evaluations(
    scope: str | None = Query(default=None, pattern="^(job|plan|recheck)$"),
    subject_key: str | None = None,
    limit: int = Query(default=100, ge=1, le=500),
):
    return {"items": service().list_evaluations(scope=scope, subject_key=subject_key, limit=limit)}


@router.get("/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: int):
    return service().get_evaluation(evaluation_id)
