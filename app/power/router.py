from __future__ import annotations

from fastapi import APIRouter, Query

from app.power.schemas import (
    BudgetAdjust,
    BudgetCreate,
    DeratingCreate,
    EvaluateRequest,
    PayloadCreate,
    PayloadUpdate,
    PlanCreate,
    PlanTransition,
    PlanVersionCreate,
    SegmentCreate,
)
from app.power.service import PowerBudgetService

router = APIRouter(prefix="/api/power", tags=["卫星功率预算"])


def service() -> PowerBudgetService:
    return PowerBudgetService()


# 载荷
@router.post("/payloads", status_code=201)
def create_payload(payload: PayloadCreate, actor: str = Query(..., min_length=1)):
    return service().create_payload(payload.model_dump(), actor)


@router.get("/payloads")
def list_payloads():
    return {"items": service().list_payloads()}


@router.patch("/payloads/{payload_id}")
def update_payload(payload_id: int, payload: PayloadUpdate, actor: str = Query(..., min_length=1)):
    return service().update_payload(payload_id, payload.model_dump(), actor)


# 日照段
@router.post("/sunlight-segments", status_code=201)
def create_segment(payload: SegmentCreate, actor: str = Query(..., min_length=1)):
    return service().create_segment(payload.model_dump(), actor)


@router.get("/sunlight-segments")
def list_segments():
    return {"items": service().list_segments()}


@router.delete("/sunlight-segments/{segment_id}")
def delete_segment(segment_id: int, actor: str = Query(..., min_length=1)):
    return service().delete_segment(segment_id, actor)


# 功率预算
@router.post("/budgets", status_code=201)
def create_budget(payload: BudgetCreate, actor: str = Query(..., min_length=1)):
    return service().create_budget(payload.model_dump(), actor)


@router.get("/budgets")
def list_budgets():
    return {"items": service().list_budgets()}


@router.get("/budgets/{budget_id}")
def get_budget(budget_id: int):
    return service().get_budget(budget_id)


@router.post("/budgets/{budget_id}/adjust", status_code=201)
def adjust_budget(budget_id: int, payload: BudgetAdjust, actor: str = Query(..., min_length=1)):
    return service().adjust_budget(budget_id, payload.model_dump(), actor)


@router.post("/budgets/{budget_id}/deratings", status_code=201)
def add_derating(budget_id: int, payload: DeratingCreate, actor: str = Query(..., min_length=1)):
    return service().add_derating(budget_id, payload.model_dump(), actor)


@router.get("/budgets/{budget_id}/deratings")
def list_deratings(budget_id: int):
    return {"items": service().list_deratings(budget_id)}


@router.delete("/deratings/{derating_id}")
def deactivate_derating(derating_id: int, actor: str = Query(..., min_length=1)):
    return service().deactivate_derating(derating_id, actor)


# 作业计划
@router.post("/plans", status_code=201)
def create_plan(payload: PlanCreate, actor: str = Query(..., min_length=1)):
    return service().create_plan(payload.model_dump(), actor)


@router.get("/plans")
def list_plans(status: str | None = None, code: str | None = None):
    return {"items": service().list_plans(status=status, code=code)}


@router.get("/plans/{plan_id}")
def get_plan(plan_id: int):
    return service().get_plan(plan_id)


@router.put("/plans/{plan_id}/versions", status_code=201)
def add_plan_version(plan_id: int, payload: PlanVersionCreate, actor: str = Query(..., min_length=1)):
    return service().add_plan_version(plan_id, payload.model_dump(), actor)


@router.get("/plans/{plan_id}/versions")
def list_plan_versions(plan_id: int):
    plan = service().get_plan(plan_id)
    return {"items": plan["versions"]}


@router.post("/plans/{plan_id}/submit")
def submit_plan(plan_id: int, payload: PlanTransition):
    return service().transition(plan_id, "submit", payload.model_dump())


@router.post("/plans/{plan_id}/approve")
def approve_plan(plan_id: int, payload: PlanTransition):
    return service().transition(plan_id, "approve", payload.model_dump())


@router.post("/plans/{plan_id}/publish")
def publish_plan(plan_id: int, payload: PlanTransition):
    return service().transition(plan_id, "publish", payload.model_dump())


@router.post("/plans/{plan_id}/withdraw")
def withdraw_plan(plan_id: int, payload: PlanTransition):
    return service().transition(plan_id, "withdraw", payload.model_dump())


@router.get("/plans/{plan_id}/compare")
def compare_plan_versions(plan_id: int, from_version: int = Query(ge=1), to_version: int = Query(ge=1)):
    return service().compare_versions(plan_id, from_version, to_version)


@router.post("/plans/{plan_id}/evaluate", status_code=201)
def evaluate_plan(plan_id: int, payload: EvaluateRequest):
    return service().evaluate(plan_id, payload.model_dump(), payload.actor)


@router.get("/plans/{plan_id}/evaluations")
def list_plan_evaluations(plan_id: int):
    return {"items": service().list_evaluations(plan_id=plan_id)}


# 评估快照（管理员审计）
@router.get("/evaluations")
def list_evaluations(plan_id: int | None = None, budget_id: int | None = None, limit: int = Query(default=100, ge=1, le=500)):
    return {"items": service().list_evaluations(plan_id=plan_id, budget_id=budget_id, limit=limit)}


@router.get("/evaluations/{evaluation_id}")
def get_evaluation(evaluation_id: int):
    return service().get_evaluation(evaluation_id)
