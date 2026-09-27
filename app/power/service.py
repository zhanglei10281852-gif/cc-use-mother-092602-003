from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from typing import Any

from app.core.clock import Clock, SystemClock, from_storage, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.power.repository import PowerRepository

FORMULA_VERSION = "power-budget-v1"
EPSILON = 1e-6
MAX_JOBS_PER_PLAN = 200
MAX_JOB_DURATION_SECONDS = 31 * 86400

# 计划状态机：草案 -> 提交 -> 审批 -> 发布；撤回按当前状态回到草案或终止。
TRANSITIONS: dict[str, dict[str, str]] = {
    "submit": {"draft": "submitted"},
    "approve": {"submitted": "approved"},
    "publish": {"approved": "published"},
    "withdraw": {"submitted": "draft", "approved": "draft", "published": "withdrawn"},
}


def digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode()).hexdigest()


def run_evaluation(snapshot: dict[str, Any]) -> dict[str, Any]:
    """公式版本 power-budget-v1：峰值扫描 + 分段能量核算。

    输入快照包含计划作业、预算版本、生效降额、日照段与涉及载荷；
    输出决策、峰值、累计消耗与拒绝原因，不修改任何输入。
    """
    jobs = snapshot["plan"]["jobs"]
    budget = snapshot["budget"]
    deratings = snapshot["deratings"]
    segments = snapshot["segments"]
    payloads = {item["code"]: item for item in snapshot["payloads"]}

    reasons: list[dict[str, Any]] = []
    intervals: list[tuple[datetime, datetime, float, str]] = []
    for job in jobs:
        base = from_storage(job["starts_at"])
        job_peak = 0.0
        for piece in job["power_curve"]:
            start = base + timedelta(seconds=int(piece["start_offset_seconds"]))
            end = base + timedelta(seconds=int(piece["end_offset_seconds"]))
            watts = float(piece["watts"])
            intervals.append((start, end, watts, job["job_code"]))
            job_peak = max(job_peak, watts)
        payload = payloads.get(job["payload_code"])
        if payload is None:
            reasons.append({
                "code": "payload_missing",
                "message": f"作业 {job['job_code']} 引用的载荷 {job['payload_code']} 不存在",
                "context": {"job_code": job["job_code"], "payload_code": job["payload_code"]},
            })
        elif not payload["active"]:
            reasons.append({
                "code": "payload_inactive",
                "message": f"作业 {job['job_code']} 引用的载荷 {job['payload_code']} 已停用",
                "context": {"job_code": job["job_code"], "payload_code": job["payload_code"]},
            })
        elif job_peak > float(payload["max_watts"]) + EPSILON:
            reasons.append({
                "code": "payload_max_exceeded",
                "message": f"作业 {job['job_code']} 峰值 {job_peak:.1f}W 超过载荷 {job['payload_code']} 上限 {float(payload['max_watts']):.1f}W",
                "context": {"job_code": job["job_code"], "payload_code": job["payload_code"], "job_peak_watts": round(job_peak, 3), "payload_max_watts": float(payload["max_watts"])},
            })

    # 瞬时功率扫描：事件点来自作业区间与降额窗口的边界。
    events: dict[datetime, float] = {}
    points: set[datetime] = set()
    for start, end, watts, _job_code in intervals:
        events[start] = events.get(start, 0.0) + watts
        events[end] = events.get(end, 0.0) - watts
        points.add(start)
        points.add(end)
    derating_windows = [
        (from_storage(item["starts_at"]), from_storage(item["ends_at"]), float(item["watts"]))
        for item in deratings
    ]
    for start, end, _watts in derating_windows:
        points.add(start)
        points.add(end)
    ordered = sorted(points)

    total_watts = float(budget["total_watts"])
    reserve_watts = float(budget["reserve_watts"])
    demand = 0.0
    peak = 0.0
    peak_at = ""
    violations: list[tuple[datetime, datetime, float, float]] = []
    for index, point in enumerate(ordered):
        demand += events.get(point, 0.0)
        if index + 1 >= len(ordered):
            break
        following = ordered[index + 1]
        derated = sum(watts for start, end, watts in derating_windows if start <= point < end)
        cap = total_watts - reserve_watts - derated
        if demand > peak:
            peak, peak_at = demand, to_storage(point)
        if demand > cap + EPSILON:
            violations.append((point, following, demand, cap))

    merged: list[dict[str, Any]] = []
    for start, end, demand_value, cap in violations:
        if merged and merged[-1]["_end"] == start:
            merged[-1]["_end"] = end
            merged[-1]["max_demand"] = max(merged[-1]["max_demand"], demand_value)
            merged[-1]["min_cap"] = min(merged[-1]["min_cap"], cap)
        else:
            merged.append({"_end": end, "start": start, "max_demand": demand_value, "min_cap": cap})
    for item in merged:
        reasons.append({
            "code": "peak_exceeds_cap",
            "message": (
                f"{to_storage(item['start'])} 至 {to_storage(item['_end'])} 期间需求 "
                f"{item['max_demand']:.1f}W 超过有效上限 {item['min_cap']:.1f}W"
            ),
            "context": {
                "starts_at": to_storage(item["start"]),
                "ends_at": to_storage(item["_end"]),
                "max_demand_watts": round(item["max_demand"], 3),
                "min_cap_watts": round(item["min_cap"], 3),
                "budget_total_watts": total_watts,
                "reserve_watts": reserve_watts,
            },
        })

    # 能量核算：按日照段归属，段外计入阴影区消耗。
    segment_windows = sorted(
        (
            (from_storage(item["starts_at"]), from_storage(item["ends_at"]), float(item["available_watts"]), int(item["id"]))
            for item in segments
        ),
        key=lambda item: item[0],
    )
    segment_demand = {segment_id: 0.0 for _s, _e, _w, segment_id in segment_windows}
    eclipse_wh = 0.0
    total_wh = 0.0
    for start, end, watts, _job_code in intervals:
        duration = (end - start).total_seconds()
        total_wh += watts * duration / 3600.0
        covered = 0.0
        for seg_start, seg_end, _available, segment_id in segment_windows:
            overlap = (min(end, seg_end) - max(start, seg_start)).total_seconds()
            if overlap > 0:
                covered += overlap
                segment_demand[segment_id] += watts * overlap / 3600.0
        eclipse_wh += watts * max(0.0, duration - covered) / 3600.0

    segment_results: list[dict[str, Any]] = []
    for seg_start, seg_end, available_watts, segment_id in segment_windows:
        available_wh = available_watts * (seg_end - seg_start).total_seconds() / 3600.0
        demand_wh = segment_demand[segment_id]
        ok = demand_wh <= available_wh + EPSILON
        segment_results.append({
            "segment_id": segment_id,
            "starts_at": to_storage(seg_start),
            "ends_at": to_storage(seg_end),
            "demand_wh": round(demand_wh, 3),
            "available_wh": round(available_wh, 3),
            "ok": ok,
        })
        if not ok:
            reasons.append({
                "code": "segment_energy_exceeded",
                "message": f"日照段 {segment_id} 能量需求 {demand_wh:.1f}Wh 超过可供 {available_wh:.1f}Wh",
                "context": {"segment_id": segment_id, "demand_wh": round(demand_wh, 3), "available_wh": round(available_wh, 3)},
            })

    eclipse_allowance = float(budget["eclipse_energy_wh"])
    eclipse_ok = eclipse_wh <= eclipse_allowance + EPSILON
    if not eclipse_ok:
        reasons.append({
            "code": "eclipse_energy_exceeded",
            "message": f"阴影区能量需求 {eclipse_wh:.1f}Wh 超过预算余量 {eclipse_allowance:.1f}Wh",
            "context": {"demand_wh": round(eclipse_wh, 3), "allowance_wh": eclipse_allowance},
        })

    return {
        "decision": "accepted" if not reasons else "rejected",
        "peak_watts": round(peak, 3),
        "peak_at": peak_at,
        "total_energy_wh": round(total_wh, 3),
        "sunlit_energy_wh": round(total_wh - eclipse_wh, 3),
        "eclipse": {"demand_wh": round(eclipse_wh, 3), "allowance_wh": eclipse_allowance, "ok": eclipse_ok},
        "segments": segment_results,
        "reasons": reasons,
    }


class PowerBudgetService:
    """管理载荷、日照段、预算版本、作业计划生命周期与评估。"""

    formula_version = FORMULA_VERSION

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = PowerRepository(self.connection)

    # 载荷
    def create_payload(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            if repository.payload_by_code(payload["code"]):
                raise ConflictError("载荷编码已存在")
            return repository.create_payload(
                code=payload["code"], name=payload["name"], kind=payload["kind"],
                nominal_watts=payload["nominal_watts"], max_watts=payload["max_watts"],
                notes=payload["notes"], actor=actor, now=now,
            )

    def list_payloads(self) -> list[dict[str, Any]]:
        return self.repository.list_payloads()

    def update_payload(self, payload_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        del actor
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            current = repository.payload_by_id(payload_id)
            if current is None:
                raise NotFoundError("载荷不存在")
            if int(current["version"]) != payload["expected_version"]:
                raise ConflictError("载荷版本已变化，请刷新后重试", context={"current_version": current["version"]})
            values = {key: payload[key] for key in ("name", "nominal_watts", "max_watts", "active", "notes") if payload.get(key) is not None}
            if "active" in values:
                values["active"] = 1 if values["active"] else 0
            if not values:
                raise ValidationError("没有需要更新的字段")
            return repository.update_payload(payload_id, values, now)

    # 日照段
    def create_segment(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        starts_at = to_storage(payload["starts_at"])
        ends_at = to_storage(payload["ends_at"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            conflict = repository.overlapping_segment(starts_at, ends_at)
            if conflict is not None:
                raise ConflictError(
                    "日照段与现有段重叠",
                    context={"conflict_segment_id": conflict["id"], "conflict_starts_at": conflict["starts_at"], "conflict_ends_at": conflict["ends_at"]},
                )
            return repository.create_segment(starts_at=starts_at, ends_at=ends_at, available_watts=payload["available_watts"], source=payload["source"], actor=actor, now=now)

    def list_segments(self) -> list[dict[str, Any]]:
        return self.repository.list_segments()

    def delete_segment(self, segment_id: int, actor: str) -> dict[str, Any]:
        del actor
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            if repository.segment_by_id(segment_id) is None:
                raise NotFoundError("日照段不存在")
            repository.delete_segment(segment_id)
            return {"deleted": True, "segment_id": segment_id}

    # 预算
    def create_budget(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            if repository.budget_by_name(payload["name"]):
                raise ConflictError("功率预算名称已存在")
            budget = repository.create_budget(
                name=payload["name"], total_watts=payload["total_watts"], reserve_watts=payload["reserve_watts"],
                eclipse_energy_wh=payload["eclipse_energy_wh"], reason=payload["reason"], actor=actor, now=now,
            )
        return self.get_budget(budget["id"])

    def list_budgets(self) -> list[dict[str, Any]]:
        return self.repository.list_budgets()

    def get_budget(self, budget_id: int) -> dict[str, Any]:
        budget = self.repository.budget_by_id(budget_id)
        if budget is None:
            raise NotFoundError("功率预算不存在")
        result = dict(budget)
        result["current"] = dict(self.repository.budget_version(budget_id, budget["current_version"]))
        result["versions"] = self.repository.budget_versions(budget_id)
        result["deratings"] = self.repository.list_deratings(budget_id)
        return result

    def adjust_budget(self, budget_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            budget = repository.budget_by_id(budget_id)
            if budget is None:
                raise NotFoundError("功率预算不存在")
            if int(budget["current_version"]) != payload["expected_version"]:
                raise ConflictError("预算版本已变化，请刷新后重试", context={"current_version": budget["current_version"]})
            version = int(budget["current_version"]) + 1
            repository.insert_budget_version(
                budget_id=budget_id, version=version, total_watts=payload["total_watts"],
                reserve_watts=payload["reserve_watts"], eclipse_energy_wh=payload["eclipse_energy_wh"],
                reason=payload["reason"], actor=actor, now=now,
            )
            repository.set_budget_current_version(budget_id, version, now)
        result = self.get_budget(budget_id)
        reevaluations: list[dict[str, Any]] = []
        if payload.get("reevaluate"):
            for plan_id in self.repository.published_plan_ids():
                evaluation = self.evaluate(plan_id, {"budget_id": budget_id}, actor)
                reevaluations.append({
                    "plan_id": plan_id,
                    "evaluation_id": evaluation["id"],
                    "decision": evaluation["decision"],
                    "budget_version": evaluation["budget_version"],
                })
        result["reevaluations"] = reevaluations
        return result

    # 临时降额
    def add_derating(self, budget_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            if repository.budget_by_id(budget_id) is None:
                raise NotFoundError("功率预算不存在")
            return repository.create_derating(
                budget_id=budget_id, starts_at=to_storage(payload["starts_at"]), ends_at=to_storage(payload["ends_at"]),
                watts=payload["watts"], reason=payload["reason"], actor=actor, now=now,
            )

    def list_deratings(self, budget_id: int) -> list[dict[str, Any]]:
        if self.repository.budget_by_id(budget_id) is None:
            raise NotFoundError("功率预算不存在")
        return self.repository.list_deratings(budget_id)

    def deactivate_derating(self, derating_id: int, actor: str) -> dict[str, Any]:
        del actor
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            row = repository.derating_by_id(derating_id)
            if row is None:
                raise NotFoundError("降额记录不存在")
            repository.deactivate_derating(derating_id)
            return dict(repository.derating_by_id(derating_id))

    # 作业计划
    def create_plan(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            jobs = self._normalize_jobs(payload["jobs"], repository)
            request_digest = digest({"code": payload["code"], "name": payload["name"], "jobs": jobs})
            existing = repository.plan_by_idempotency(payload["requested_by"], payload["idempotency_key"])
            if existing is not None:
                if existing["request_digest"] != request_digest:
                    raise ConflictError("同一幂等键对应了不同的计划内容")
                return self.get_plan(existing["id"])
            if repository.plan_by_code(payload["code"]):
                raise ConflictError("计划编码已存在")
            try:
                plan = repository.create_plan(
                    code=payload["code"], name=payload["name"], requested_by=payload["requested_by"],
                    idempotency_key=payload["idempotency_key"], request_digest=request_digest, now=now,
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("计划编码或幂等键冲突") from exc
            repository.insert_plan_version(plan_id=plan["id"], version=1, jobs=jobs, jobs_digest=digest(jobs), change_note=payload["change_note"], actor=actor, now=now)
            repository.add_plan_event(plan_id=plan["id"], actor=actor, action="create", reason=payload["change_note"], from_status="", to_status="draft", plan_version=1, now=now)
        return self.get_plan(plan["id"])

    def list_plans(self, *, status: str | None = None, code: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_plans(status=status, code=code)

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        plan = self.repository.plan_by_id(plan_id)
        if plan is None:
            raise NotFoundError("作业计划不存在")
        result = dict(plan)
        current = self.repository.plan_version(plan_id, plan["current_version"])
        result["jobs"] = json.loads(current["jobs_json"])
        result["versions"] = [self._version_meta(row) for row in self.repository.plan_versions(plan_id)]
        result["events"] = self.repository.plan_events(plan_id)
        result["evaluations"] = [self._evaluation_summary(row) for row in self.repository.list_evaluations(plan_id=plan_id, limit=20)]
        return result

    def add_plan_version(self, plan_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            plan = repository.plan_by_id(plan_id)
            if plan is None:
                raise NotFoundError("作业计划不存在")
            if plan["status"] != "draft":
                raise ConflictError("只有草案状态的计划可以修改", context={"status": plan["status"]})
            if int(plan["current_version"]) != payload["expected_version"]:
                raise ConflictError("计划版本已变化，请刷新后重试", context={"current_version": plan["current_version"]})
            jobs = self._normalize_jobs(payload["jobs"], repository)
            version = int(plan["current_version"]) + 1
            repository.insert_plan_version(plan_id=plan_id, version=version, jobs=jobs, jobs_digest=digest(jobs), change_note=payload["change_note"], actor=actor, now=now)
            repository.update_plan_pointer(plan_id, version, now)
            repository.add_plan_event(plan_id=plan_id, actor=actor, action="edit", reason=payload["change_note"], from_status="draft", to_status="draft", plan_version=version, now=now)
        return self.get_plan(plan_id)

    def transition(self, plan_id: int, action: str, payload: dict[str, Any]) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            plan = repository.plan_by_id(plan_id)
            if plan is None:
                raise NotFoundError("作业计划不存在")
            if int(plan["current_version"]) != payload["expected_version"]:
                raise ConflictError("计划版本已变化，请刷新后重试", context={"current_version": plan["current_version"]})
            from_status = plan["status"]
            targets = TRANSITIONS[action]
            if from_status not in targets:
                raise ConflictError(f"当前状态 {from_status} 不允许执行 {action}", context={"status": from_status, "action": action})
            to_status = targets[from_status]
            if action == "publish":
                others = repository.published_plan_ids(exclude_plan_id=plan_id)
                if others:
                    raise ConflictError("已存在发布中的计划，请先撤回", context={"published_plan_ids": others})
            repository.update_plan_status(plan_id, to_status, now)
            repository.add_plan_event(plan_id=plan_id, actor=payload["actor"], action=action, reason=payload["reason"], from_status=from_status, to_status=to_status, plan_version=plan["current_version"], now=now)
        return self.get_plan(plan_id)

    def compare_versions(self, plan_id: int, from_version: int, to_version: int) -> dict[str, Any]:
        if self.repository.plan_by_id(plan_id) is None:
            raise NotFoundError("作业计划不存在")
        first = self.repository.plan_version(plan_id, from_version)
        second = self.repository.plan_version(plan_id, to_version)
        if first is None or second is None:
            raise NotFoundError("计划版本不存在")
        first_jobs = {job["job_code"]: job for job in json.loads(first["jobs_json"])}
        second_jobs = {job["job_code"]: job for job in json.loads(second["jobs_json"])}
        added = sorted(set(second_jobs) - set(first_jobs))
        removed = sorted(set(first_jobs) - set(second_jobs))
        changed: list[dict[str, Any]] = []
        for code in sorted(set(first_jobs) & set(second_jobs)):
            before, after = first_jobs[code], second_jobs[code]
            if digest(before) == digest(after):
                continue
            changes: dict[str, Any] = {}
            for field in ("payload_code", "starts_at", "priority"):
                if before[field] != after[field]:
                    changes[field] = {"from": before[field], "to": after[field]}
            if digest(before["power_curve"]) != digest(after["power_curve"]):
                changes["power_curve"] = {
                    "from_pieces": len(before["power_curve"]),
                    "to_pieces": len(after["power_curve"]),
                    "from_digest": digest(before["power_curve"]),
                    "to_digest": digest(after["power_curve"]),
                }
            changed.append({"job_code": code, "changes": changes})
        unchanged = len(set(first_jobs) & set(second_jobs)) - len(changed)
        return {
            "plan_id": plan_id,
            "from": self._version_meta(first, include_evaluation=True),
            "to": self._version_meta(second, include_evaluation=True),
            "added_jobs": added,
            "removed_jobs": removed,
            "changed_jobs": changed,
            "unchanged_jobs": unchanged,
        }

    # 评估
    def evaluate(self, plan_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            plan = repository.plan_by_id(plan_id)
            if plan is None:
                raise NotFoundError("作业计划不存在")
            if plan["status"] == "withdrawn":
                raise ConflictError("已撤回的计划不能评估")
            plan_version = payload.get("plan_version") or plan["current_version"]
            version_row = repository.plan_version(plan_id, plan_version)
            if version_row is None:
                raise NotFoundError("计划版本不存在")
            budget_id = payload.get("budget_id")
            if budget_id is None:
                budgets = repository.list_budgets()
                if len(budgets) != 1:
                    raise ValidationError("存在多个功率预算，必须指定 budget_id")
                budget_id = budgets[0]["id"]
            budget = repository.budget_by_id(budget_id)
            if budget is None:
                raise NotFoundError("功率预算不存在")
            budget_version = repository.budget_version(budget_id, budget["current_version"])
            jobs = json.loads(version_row["jobs_json"])
            payload_rows = repository.payloads_by_codes([job["payload_code"] for job in jobs])
            snapshot = {
                "plan": {"id": plan["id"], "code": plan["code"], "status": plan["status"], "version": plan_version, "jobs": jobs},
                "budget": {
                    "id": budget["id"], "name": budget["name"], "version": budget_version["version"],
                    "total_watts": budget_version["total_watts"], "reserve_watts": budget_version["reserve_watts"],
                    "eclipse_energy_wh": budget_version["eclipse_energy_wh"],
                },
                "deratings": [
                    {"id": item["id"], "starts_at": item["starts_at"], "ends_at": item["ends_at"], "watts": item["watts"], "reason": item["reason"]}
                    for item in repository.list_deratings(budget_id, active_only=True)
                ],
                "segments": [
                    {"id": item["id"], "starts_at": item["starts_at"], "ends_at": item["ends_at"], "available_watts": item["available_watts"]}
                    for item in repository.list_segments()
                ],
                "payloads": [
                    {"code": item["code"], "name": item["name"], "max_watts": item["max_watts"], "active": bool(item["active"])}
                    for item in payload_rows
                ],
                "generated_at": now,
            }
            result = run_evaluation(snapshot)
            evaluation_id = repository.create_evaluation(
                plan_id=plan_id, plan_version=plan_version, budget_id=budget_id,
                budget_version=budget_version["version"], formula_version=FORMULA_VERSION,
                snapshot=snapshot, input_digest=digest(snapshot), result=result, actor=actor, now=now,
            )
            row = repository.evaluation_by_id(evaluation_id)
            return self._evaluation_detail(row)

    def get_evaluation(self, evaluation_id: int) -> dict[str, Any]:
        row = self.repository.evaluation_by_id(evaluation_id)
        if row is None:
            raise NotFoundError("评估记录不存在")
        return self._evaluation_detail(row)

    def list_evaluations(self, *, plan_id: int | None = None, budget_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.repository.list_evaluations(plan_id=plan_id, budget_id=budget_id, limit=max(1, min(limit, 500)))
        return [self._evaluation_summary(row) for row in rows]

    # 内部工具
    def _normalize_jobs(self, jobs: list[dict[str, Any]], repository: PowerRepository) -> list[dict[str, Any]]:
        if not jobs:
            raise ValidationError("作业计划至少需要一个作业")
        if len(jobs) > MAX_JOBS_PER_PLAN:
            raise ValidationError(f"作业数量超过上限 {MAX_JOBS_PER_PLAN}")
        seen: set[str] = set()
        normalized: list[dict[str, Any]] = []
        for raw in jobs:
            job_code = raw["job_code"]
            if job_code in seen:
                raise ValidationError(f"作业编码重复：{job_code}")
            seen.add(job_code)
            pieces = sorted(
                (
                    {
                        "start_offset_seconds": int(piece["start_offset_seconds"]),
                        "end_offset_seconds": int(piece["end_offset_seconds"]),
                        "watts": float(piece["watts"]),
                    }
                    for piece in raw["power_curve"]
                ),
                key=lambda piece: piece["start_offset_seconds"],
            )
            previous_end: int | None = None
            for piece in pieces:
                if previous_end is not None and piece["start_offset_seconds"] < previous_end:
                    raise ValidationError(f"作业 {job_code} 的功耗曲线存在重叠区间")
                previous_end = piece["end_offset_seconds"]
            if pieces[-1]["end_offset_seconds"] > MAX_JOB_DURATION_SECONDS:
                raise ValidationError(f"作业 {job_code} 的时长超过上限")
            normalized.append({
                "job_code": job_code,
                "payload_code": raw["payload_code"],
                "starts_at": to_storage(raw["starts_at"]),
                "priority": int(raw["priority"]),
                "power_curve": pieces,
            })
        missing = sorted({job["payload_code"] for job in normalized} - {row["code"] for row in repository.payloads_by_codes([job["payload_code"] for job in normalized])})
        if missing:
            raise ValidationError("载荷不存在", context={"payload_codes": missing})
        return normalized

    def _version_meta(self, row: sqlite3.Row, *, include_evaluation: bool = False) -> dict[str, Any]:
        meta = {
            "version": row["version"],
            "jobs_digest": row["jobs_digest"],
            "job_count": len(json.loads(row["jobs_json"])),
            "change_note": row["change_note"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }
        if include_evaluation:
            evaluation = self.repository.latest_evaluation_for_version(row["plan_id"], row["version"])
            meta["latest_evaluation"] = self._evaluation_summary(evaluation) if evaluation is not None else None
        return meta

    @staticmethod
    def _evaluation_summary(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "plan_id": row["plan_id"],
            "plan_version": row["plan_version"],
            "budget_id": row["budget_id"],
            "budget_version": row["budget_version"],
            "formula_version": row["formula_version"],
            "decision": row["decision"],
            "peak_watts": row["peak_watts"],
            "peak_at": row["peak_at"],
            "total_energy_wh": row["total_energy_wh"],
            "input_digest": row["input_digest"],
            "created_by": row["created_by"],
            "created_at": row["created_at"],
        }

    def _evaluation_detail(self, row: sqlite3.Row) -> dict[str, Any]:
        result = self._evaluation_summary(row)
        result["result"] = json.loads(row["result_json"])
        result["reasons"] = json.loads(row["reasons_json"])
        result["input_snapshot"] = json.loads(row["input_snapshot_json"])
        return result
