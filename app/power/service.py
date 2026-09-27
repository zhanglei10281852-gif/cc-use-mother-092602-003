from __future__ import annotations

import sqlite3
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, NotFoundError, ValidationError
from app.database import get_connection, transaction
from app.power import engine
from app.power.repository import PowerRepository, loads


def ensure_power_schema() -> None:
    now = to_storage(SystemClock().now())
    PowerRepository(get_connection()).ensure_schema(now)


class PowerBudgetService:
    """卫星功率预算：载荷/日照段登记、作业评估、计划发布与可追溯计算。"""

    def __init__(self, connection: sqlite3.Connection | None = None, clock: Clock | None = None) -> None:
        self.connection = connection or get_connection()
        self.clock = clock or SystemClock()
        self.repository = PowerRepository(self.connection)
        now = to_storage(self.clock.now())
        self.repository.ensure_schema(now)

    # ---------- 载荷 ----------
    def register_payload(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            if repository.payload_by_code(payload["code"]):
                raise ConflictError("载荷编码已存在", context={"code": payload["code"]})
            result = repository.create_payload(payload=payload, actor=actor, now=now)
        result["active"] = bool(result["active"])
        return result

    def update_payload(self, code: str, changes: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            current = repository.payload_by_code(code)
            if current is None:
                raise NotFoundError("载荷不存在", context={"code": code})
            if changes["expected_version"] != current["revision"]:
                raise ConflictError(
                    "载荷已被他人修改，请刷新后重试",
                    context={"code": code, "submitted_version": changes["expected_version"], "current_version": current["revision"]},
                )
            result = repository.update_payload(code=code, changes=changes, actor=actor, now=now)
        result["active"] = bool(result["active"])
        return result

    def list_payloads(self) -> list[dict[str, Any]]:
        items = self.repository.list_payloads()
        for item in items:
            item["active"] = bool(item["active"])
        return items

    # ---------- 日照段 / 临时降额 ----------
    def add_sun_window(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        start, end = self._validate_window(payload["start_at"], payload["end_at"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            self._ensure_no_sun_overlap(repository, start, end)
            return repository.create_sun_window(start_at=payload["start_at"], end_at=payload["end_at"], power_w=payload["power_w"], actor=actor, now=now)

    def list_sun_windows(self) -> list[dict[str, Any]]:
        return self.repository.list_sun_windows()

    def add_derating(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        self._validate_window(payload["start_at"], payload["end_at"])
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            return PowerRepository(connection).create_derating(
                start_at=payload["start_at"], end_at=payload["end_at"],
                available_power_w=payload["available_power_w"], reason=payload["reason"], actor=actor, now=now,
            )

    def list_deratings(self) -> list[dict[str, Any]]:
        return self.repository.list_deratings()

    # ---------- 预算版本 ----------
    def current_budget(self) -> dict[str, Any]:
        return dict(self.repository.current_budget())

    def list_budget_revisions(self) -> list[dict[str, Any]]:
        return self.repository.list_budget_revisions()

    def adjust_budget(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            current = repository.current_budget()
            if payload["expected_revision"] is not None and payload["expected_revision"] != current["revision"]:
                raise ConflictError(
                    "预算基线已被调整，请基于最新版本重新提交",
                    context={"submitted_revision": payload["expected_revision"], "current_revision": current["revision"]},
                )
            revision = repository.add_budget_revision(
                values={
                    "sunlit_power_w": payload["sunlit_power_w"],
                    "eclipse_power_w": payload["eclipse_power_w"],
                    "battery_capacity_wh": payload["battery_capacity_wh"],
                    "max_depth_of_discharge": payload["max_depth_of_discharge"],
                    "reason": payload["reason"],
                },
                actor=actor,
                now=now,
            )
        return revision

    # ---------- 作业申请（原始申请不可变，修订只追加）----------
    def submit_job(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        values = {
            "code": payload["code"],
            "name": payload.get("name") or payload["code"],
            "payload_code": payload["payload_code"],
            "priority": payload["priority"],
            "scheduled_start_at": payload["scheduled_start_at"],
            "phases": [dict(item) for item in payload["phases"]],
        }
        self._validate_job_inputs(values)
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            if repository.job_by_code(values["code"]):
                raise ConflictError("作业编码已存在", context={"code": values["code"]})
            return repository.create_job(values=values, actor=actor, now=now)

    def amend_job(self, code: str, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            job = repository.job_by_code(code)
            if job is None:
                raise NotFoundError("作业不存在", context={"code": code})
            if payload["expected_revision"] != job["current_revision"]:
                raise ConflictError(
                    "作业申请已被修订，请基于最新版本修改",
                    context={"code": code, "submitted_revision": payload["expected_revision"], "current_revision": job["current_revision"]},
                )
            current = repository.current_job_revision_row(code)
            changes = {
                "name": payload.get("name"),
                "payload_code": payload.get("payload_code"),
                "priority": payload.get("priority"),
                "scheduled_start_at": payload.get("scheduled_start_at"),
                "phases": [dict(item) for item in payload["phases"]] if payload.get("phases") is not None else None,
            }
            merged = {
                "code": code,
                "name": changes["name"] or current["name"],
                "payload_code": changes["payload_code"] or current["payload_code"],
                "priority": changes["priority"] if changes["priority"] is not None else current["priority"],
                "scheduled_start_at": changes["scheduled_start_at"] or current["scheduled_start_at"],
                "phases": changes["phases"] if changes["phases"] is not None else loads(current["phases_json"]),
            }
            self._validate_job_inputs(merged, repository=repository)
            return repository.amend_job(code=code, changes=changes, note=payload["change_note"], actor=actor, now=now)

    def list_jobs(self) -> list[dict[str, Any]]:
        return self.repository.list_jobs()

    def get_job(self, code: str) -> dict[str, Any]:
        if self.repository.job_by_code(code) is None:
            raise NotFoundError("作业不存在", context={"code": code})
        return self.repository.get_job_detail(code)

    def list_job_revisions(self, code: str) -> list[dict[str, Any]]:
        if self.repository.job_by_code(code) is None:
            raise NotFoundError("作业不存在", context={"code": code})
        items = self.repository.list_job_revisions(code)
        for item in items:
            item["phases"] = loads(item.pop("phases_json"))
        return items

    def compare_job_revisions(self, code: str, from_revision: int, to_revision: int) -> dict[str, Any]:
        job = self.repository.job_by_code(code)
        if job is None:
            raise NotFoundError("作业不存在", context={"code": code})
        left = self.repository.job_revision(job["id"], from_revision)
        right = self.repository.job_revision(job["id"], to_revision)
        if left is None or right is None:
            raise NotFoundError("作业修订版本不存在", context={"from": from_revision, "to": to_revision})
        return {
            "code": code,
            "from": self._job_revision_view(left),
            "to": self._job_revision_view(right),
            "diff": self._diff_job_revisions(left, right),
        }

    # ---------- 评估 ----------
    def evaluate_job(self, code: str, actor: str, budget_revision: int | None = None) -> dict[str, Any]:
        if self.repository.job_by_code(code) is None:
            raise NotFoundError("作业不存在", context={"code": code})
        return self._run_evaluation(scope="job", subject_key=code, actor=actor, budget_revision=budget_revision)

    def evaluate_plan(self, plan_id: int, actor: str, budget_revision: int | None = None) -> dict[str, Any]:
        if self.repository.plan_by_id(plan_id) is None:
            raise NotFoundError("计划不存在", context={"plan_id": plan_id})
        return self._run_evaluation(scope="plan", subject_key=str(plan_id), actor=actor, budget_revision=budget_revision)

    def recheck(self, scope: str, subject_key: str, actor: str, budget_revision: int | None = None) -> dict[str, Any]:
        if scope not in {"job", "plan"}:
            raise ValidationError("重评范围必须是 job 或 plan")
        if scope == "job":
            if self.repository.job_by_code(subject_key) is None:
                raise NotFoundError("作业不存在", context={"code": subject_key})
        elif self.repository.plan_by_id(int(subject_key)) is None:
            raise NotFoundError("计划不存在", context={"plan_id": subject_key})
        return self._run_evaluation(scope="recheck", subject_key=f"{scope}:{subject_key}", actor=actor, budget_revision=budget_revision, recheck_of=(scope, subject_key))

    def get_evaluation(self, evaluation_id: int) -> dict[str, Any]:
        row = self.repository.evaluation_by_id(evaluation_id)
        if row is None:
            raise NotFoundError("评估记录不存在", context={"evaluation_id": evaluation_id})
        return self._evaluation_view(row)

    def list_evaluations(self, *, scope: str | None = None, subject_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        return self.repository.list_evaluations(scope=scope, subject_key=subject_key, limit=max(1, min(limit, 500)))

    # ---------- 计划草案 / 发布 / 撤回 ----------
    def create_plan(self, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        job_codes = list(dict.fromkeys(payload["job_codes"]))
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            self._require_jobs(repository, job_codes)
            return repository.create_plan(name=payload["name"], job_codes=job_codes, note=payload["note"], actor=actor, now=now)

    def update_plan(self, plan_id: int, payload: dict[str, Any], actor: str) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            plan = repository.plan_by_id(plan_id)
            if plan is None:
                raise NotFoundError("计划不存在", context={"plan_id": plan_id})
            if plan["status"] != "draft":
                raise ConflictError("只有草案状态的计划可以修改", context={"status": plan["status"]})
            if payload["expected_version"] != plan["current_version"]:
                raise ConflictError(
                    "计划草案已被修改，请基于最新版本提交",
                    context={"submitted_version": payload["expected_version"], "current_version": plan["current_version"]},
                )
            changes = {k: v for k, v in payload.items() if k in {"name", "job_codes", "note"} and v is not None}
            if "job_codes" in changes:
                changes["job_codes"] = list(dict.fromkeys(changes["job_codes"]))
                self._require_jobs(repository, changes["job_codes"])
            return repository.update_plan(plan_id=plan_id, changes=changes, actor=actor, now=now)

    def publish_plan(self, plan_id: int, actor: str, reason: str, expected_version: int | None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            plan = repository.plan_by_id(plan_id)
            if plan is None:
                raise NotFoundError("计划不存在", context={"plan_id": plan_id})
            if expected_version is not None and expected_version != plan["current_version"]:
                raise ConflictError(
                    "计划版本与提交时不一致，请比较版本后重新提交",
                    context={"submitted_version": expected_version, "current_version": plan["current_version"]},
                )
            if plan["status"] == "published":
                raise ConflictError("计划已经发布，请勿重复提交", context={"status": plan["status"]})
            if plan["status"] == "withdrawn":
                raise ConflictError("计划已撤回，不能发布", context={"status": plan["status"]})
            snapshot, result = self._build_and_evaluate(repository, "plan", str(plan_id))
            # 评估记录先落库：即使随后因不可行而拒绝发布，管理员仍可按编号回溯输入快照。
            evaluation = self._persist_evaluation(repository, "plan", str(plan_id), plan["current_version"], snapshot, result, actor, now)
            if result["feasible"]:
                repository.set_plan_status(plan_id, "published", now)
                repository.add_publication(plan_id=plan_id, version=plan["current_version"], action="publish", actor=actor, reason=reason, evaluation_id=evaluation["id"], now=now)
            feasible = result["feasible"]
            rejection = result["rejection_reasons"]
            budget_revision = snapshot["settings"]["revision"]
            evaluation_id = evaluation["id"]
            detail = repository.get_plan_detail(plan_id)
        if not feasible:
            raise ConflictError(
                "计划未通过当前功率预算评估，不能发布",
                context={"evaluation_id": evaluation_id, "rejection_reasons": rejection, "budget_revision": budget_revision},
            )
        return {"plan": detail, "evaluation_id": evaluation_id, "result": result}

    def withdraw_plan(self, plan_id: int, actor: str, reason: str, expected_version: int | None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            plan = repository.plan_by_id(plan_id)
            if plan is None:
                raise NotFoundError("计划不存在", context={"plan_id": plan_id})
            if expected_version is not None and expected_version != plan["current_version"]:
                raise ConflictError(
                    "计划版本与提交时不一致，请比较版本后重新提交",
                    context={"submitted_version": expected_version, "current_version": plan["current_version"]},
                )
            if plan["status"] != "published":
                raise ConflictError("只有已发布计划可以撤回", context={"status": plan["status"]})
            repository.set_plan_status(plan_id, "withdrawn", now)
            repository.add_publication(plan_id=plan_id, version=plan["current_version"], action="withdraw", actor=actor, reason=reason, evaluation_id=None, now=now)
            return repository.get_plan_detail(plan_id)

    def list_plans(self, status: str | None = None) -> list[dict[str, Any]]:
        return self.repository.list_plans(status=status)

    def get_plan(self, plan_id: int) -> dict[str, Any]:
        if self.repository.plan_by_id(plan_id) is None:
            raise NotFoundError("计划不存在", context={"plan_id": plan_id})
        detail = self.repository.get_plan_detail(plan_id)
        detail["publications"] = self.repository.list_publications(plan_id)
        return detail

    def compare_plan_versions(self, plan_id: int, from_version: int, to_version: int) -> dict[str, Any]:
        if self.repository.plan_by_id(plan_id) is None:
            raise NotFoundError("计划不存在", context={"plan_id": plan_id})
        left = self.repository.plan_version(plan_id, from_version)
        right = self.repository.plan_version(plan_id, to_version)
        if left is None or right is None:
            raise NotFoundError("计划版本不存在", context={"from": from_version, "to": to_version})
        left_codes, right_codes = loads(left["job_codes_json"]), loads(right["job_codes_json"])
        return {
            "plan_id": plan_id,
            "from": {"version": left["version"], "name": left["name"], "note": left["note"], "job_codes": left_codes, "created_by": left["created_by"], "created_at": left["created_at"]},
            "to": {"version": right["version"], "name": right["name"], "note": right["note"], "job_codes": right_codes, "created_by": right["created_by"], "created_at": right["created_at"]},
            "diff": {
                "jobs_added": [code for code in right_codes if code not in left_codes],
                "jobs_removed": [code for code in left_codes if code not in right_codes],
                "order_changed": left_codes != right_codes and set(left_codes) == set(right_codes),
                "name_changed": left["name"] != right["name"],
            },
        }

    def list_plan_versions(self, plan_id: int) -> list[dict[str, Any]]:
        if self.repository.plan_by_id(plan_id) is None:
            raise NotFoundError("计划不存在", context={"plan_id": plan_id})
        return self.repository.list_plan_versions(plan_id)

    # ---------- 内部：快照与计算 ----------
    def _run_evaluation(self, *, scope: str, subject_key: str, actor: str, budget_revision: int | None, recheck_of: tuple[str, str] | None = None) -> dict[str, Any]:
        now = to_storage(self.clock.now())
        with transaction(immediate=True) as connection:
            repository = PowerRepository(connection)
            if recheck_of is not None:
                snapshot, result = self._build_and_evaluate(repository, recheck_of[0], recheck_of[1], budget_revision=budget_revision)
                subject_version = self._subject_version(repository, recheck_of[0], recheck_of[1])
            else:
                snapshot, result = self._build_and_evaluate(repository, scope, subject_key, budget_revision=budget_revision)
                subject_version = self._subject_version(repository, scope, subject_key)
            evaluation = self._persist_evaluation(repository, scope, subject_key, subject_version, snapshot, result, actor, now)
        return {"evaluation": self._evaluation_view(evaluation), "result": result}

    def _build_and_evaluate(self, repository: PowerRepository, scope: str, subject_key: str, budget_revision: int | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        budget_row = repository.current_budget() if budget_revision is None else repository.budget_by_revision(budget_revision)
        if budget_row is None:
            raise NotFoundError("预算版本不存在", context={"budget_revision": budget_revision})
        if scope == "job":
            job_codes = [subject_key]
        else:
            plan = repository.plan_by_id(int(subject_key))
            job_codes = loads(repository.plan_version(plan["id"], plan["current_version"])["job_codes_json"])
        payload_codes: list[str] = []
        jobs: list[dict[str, Any]] = []
        for code in job_codes:
            row = repository.current_job_revision_row(code)
            if row is None:
                raise NotFoundError("作业不存在或缺少修订", context={"code": code})
            payload = repository.payload_by_code(row["payload_code"])
            if payload is None or not payload["active"]:
                raise ConflictError("作业引用的载荷不存在或已停用", context={"job": code, "payload_code": row["payload_code"]})
            if row["payload_code"] not in payload_codes:
                payload_codes.append(row["payload_code"])
            jobs.append(
                {
                    "code": code,
                    "name": row["name"],
                    "payload_code": row["payload_code"],
                    "priority": row["priority"],
                    "scheduled_start_at": row["scheduled_start_at"],
                    "phases": loads(row["phases_json"]),
                    "revision": self._job_revision_number(repository, code),
                }
            )
        snapshot = {
            "settings": {
                "revision": budget_row["revision"],
                "sunlit_power_w": budget_row["sunlit_power_w"],
                "eclipse_power_w": budget_row["eclipse_power_w"],
                "battery_capacity_wh": budget_row["battery_capacity_wh"],
                "max_depth_of_discharge": budget_row["max_depth_of_discharge"],
            },
            "payloads": [
                {
                    "code": repository.payload_by_code(code)["code"],
                    "name": repository.payload_by_code(code)["name"],
                    "baseline_power_w": repository.payload_by_code(code)["baseline_power_w"],
                }
                for code in payload_codes
            ],
            "sun_windows": [
                {"start_at": item["start_at"], "end_at": item["end_at"], "power_w": item["power_w"]}
                for item in repository.list_sun_windows()
            ],
            "deratings": [
                {"start_at": item["start_at"], "end_at": item["end_at"], "available_power_w": item["available_power_w"], "reason": item["reason"]}
                for item in repository.list_deratings()
            ],
            "jobs": [{k: v for k, v in job.items() if k != "revision"} for job in jobs],
            "job_revisions": {job["code"]: job["revision"] for job in jobs},
        }
        try:
            result = engine.evaluate(snapshot)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        return snapshot, result

    def _persist_evaluation(self, repository: PowerRepository, scope: str, subject_key: str, subject_version: int | None, snapshot: dict[str, Any], result: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        digest_value = engine.canonical_digest({"snapshot": snapshot, "formula_version": result["formula_version"]})
        return repository.save_evaluation(
            scope=scope, subject_key=subject_key, subject_version=subject_version,
            budget_revision=snapshot["settings"]["revision"], formula_version=result["formula_version"],
            feasible=result["feasible"], snapshot=snapshot, result=result, actor=actor, now=now,
            snapshot_digest=digest_value,
        )

    @staticmethod
    def _evaluation_view(row: sqlite3.Row) -> dict[str, Any]:
        item = dict(row)
        item["feasible"] = bool(item["feasible"])
        if "snapshot_json" in item:
            item["snapshot"] = loads(item.pop("snapshot_json"))
            item["result"] = loads(item.pop("result_json"))
        return item

    @staticmethod
    def _subject_version(repository: PowerRepository, scope: str, subject_key: str) -> int | None:
        if scope == "job":
            return repository.job_by_code(subject_key)["current_revision"]
        return repository.plan_by_id(int(subject_key))["current_version"]

    @staticmethod
    def _job_revision_number(repository: PowerRepository, code: str) -> int:
        return repository.job_by_code(code)["current_revision"]

    @staticmethod
    def _job_revision_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "revision": row["revision"],
            "name": row["name"],
            "payload_code": row["payload_code"],
            "priority": row["priority"],
            "scheduled_start_at": row["scheduled_start_at"],
            "phases": loads(row["phases_json"]),
            "change_note": row["change_note"],
            "submitted_by": row["submitted_by"],
            "created_at": row["created_at"],
        }

    @staticmethod
    def _diff_job_revisions(left: sqlite3.Row, right: sqlite3.Row) -> dict[str, Any]:
        left_phases, right_phases = loads(left["phases_json"]), loads(right["phases_json"])
        left_duration = sum(item["duration_s"] for item in left_phases)
        right_duration = sum(item["duration_s"] for item in right_phases)
        return {
            "changed_fields": [
                field
                for field, a, b in (
                    ("name", left["name"], right["name"]),
                    ("payload_code", left["payload_code"], right["payload_code"]),
                    ("priority", left["priority"], right["priority"]),
                    ("scheduled_start_at", left["scheduled_start_at"], right["scheduled_start_at"]),
                    ("phases", left_phases, right_phases),
                )
                if a != b
            ],
            "duration_s": {"from": left_duration, "to": right_duration, "delta": right_duration - left_duration},
            "peak_w": {"from": max((item["power_w"] for item in left_phases), default=0), "to": max((item["power_w"] for item in right_phases), default=0)},
        }

    @staticmethod
    def _require_jobs(repository: PowerRepository, job_codes: list[str]) -> None:
        missing = [code for code in job_codes if repository.job_by_code(code) is None]
        if missing:
            raise NotFoundError("部分作业不存在", context={"missing_job_codes": missing})

    def _validate_job_inputs(self, values: dict[str, Any], repository: PowerRepository | None = None) -> None:
        repo = repository or self.repository
        payload = repo.payload_by_code(values["payload_code"])
        if payload is None or not payload["active"]:
            raise ValidationError("作业引用的载荷不存在或已停用", context={"payload_code": values["payload_code"]})
        self._parse_time(values["scheduled_start_at"])
        for item in values["phases"]:
            if item["power_w"] < 0:
                raise ValidationError("作业阶段功耗不能为负")

    @staticmethod
    def _parse_time(value: str):
        try:
            return engine.parse_datetime(value)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc

    def _validate_window(self, start_raw: str, end_raw: str):
        start = self._parse_time(start_raw)
        end = self._parse_time(end_raw)
        if end <= start:
            raise ValidationError("结束时间必须晚于开始时间")
        return start, end

    @staticmethod
    def _ensure_no_sun_overlap(repository: PowerRepository, start, end) -> None:
        for item in repository.list_sun_windows():
            existing_start = engine.parse_datetime(item["start_at"])
            existing_end = engine.parse_datetime(item["end_at"])
            if start < existing_end and existing_start < end:
                raise ConflictError("日照段时间重叠，请先确认既有登记", context={"existing_window_id": item["id"]})
