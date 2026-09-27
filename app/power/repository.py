from __future__ import annotations

import json
import sqlite3
from typing import Any, Iterable


class PowerRepository:
    """封装卫星功率预算领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    # 载荷
    def payload_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_payloads WHERE code=?", (code,)).fetchone()

    def payload_by_id(self, payload_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_payloads WHERE id=?", (payload_id,)).fetchone()

    def list_payloads(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM power_payloads ORDER BY code").fetchall()
        return [dict(row) for row in rows]

    def payloads_by_codes(self, codes: Iterable[str]) -> list[sqlite3.Row]:
        values = sorted(set(codes))
        if not values:
            return []
        placeholders = ",".join("?" for _ in values)
        return self.connection.execute(f"SELECT * FROM power_payloads WHERE code IN ({placeholders})", values).fetchall()

    def create_payload(self, *, code: str, name: str, kind: str, nominal_watts: float, max_watts: float, notes: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_payloads(code,name,kind,nominal_watts,max_watts,notes,created_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (code, name, kind, nominal_watts, max_watts, notes, actor, now, now),
        )
        return dict(self.payload_by_id(cursor.lastrowid))

    def update_payload(self, payload_id: int, values: dict[str, Any], now: str) -> dict[str, Any]:
        assignments = ",".join(f"{key}=?" for key in values)
        self.connection.execute(
            f"UPDATE power_payloads SET {assignments},version=version+1,updated_at=? WHERE id=?",
            (*values.values(), now, payload_id),
        )
        return dict(self.payload_by_id(payload_id))

    # 日照段
    def segment_by_id(self, segment_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_sunlight_segments WHERE id=?", (segment_id,)).fetchone()

    def overlapping_segment(self, starts_at: str, ends_at: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM power_sunlight_segments WHERE starts_at<? AND ends_at>? ORDER BY starts_at LIMIT 1",
            (ends_at, starts_at),
        ).fetchone()

    def create_segment(self, *, starts_at: str, ends_at: str, available_watts: float, source: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_sunlight_segments(starts_at,ends_at,available_watts,source,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (starts_at, ends_at, available_watts, source, actor, now),
        )
        return dict(self.segment_by_id(cursor.lastrowid))

    def list_segments(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM power_sunlight_segments ORDER BY starts_at,id").fetchall()
        return [dict(row) for row in rows]

    def delete_segment(self, segment_id: int) -> None:
        self.connection.execute("DELETE FROM power_sunlight_segments WHERE id=?", (segment_id,))

    # 预算与版本
    def budget_by_id(self, budget_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_budgets WHERE id=?", (budget_id,)).fetchone()

    def budget_by_name(self, name: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_budgets WHERE name=?", (name,)).fetchone()

    def list_budgets(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM power_budgets ORDER BY id").fetchall()
        return [dict(row) for row in rows]

    def create_budget(self, *, name: str, total_watts: float, reserve_watts: float, eclipse_energy_wh: float, reason: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_budgets(name,current_version,created_by,created_at,updated_at) VALUES(?,1,?,?,?)",
            (name, actor, now, now),
        )
        budget_id = cursor.lastrowid
        self.insert_budget_version(budget_id=budget_id, version=1, total_watts=total_watts, reserve_watts=reserve_watts, eclipse_energy_wh=eclipse_energy_wh, reason=reason, actor=actor, now=now)
        return dict(self.budget_by_id(budget_id))

    def budget_version(self, budget_id: int, version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM power_budget_versions WHERE budget_id=? AND version=?",
            (budget_id, version),
        ).fetchone()

    def budget_versions(self, budget_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM power_budget_versions WHERE budget_id=? ORDER BY version", (budget_id,)).fetchall()
        return [dict(row) for row in rows]

    def insert_budget_version(self, *, budget_id: int, version: int, total_watts: float, reserve_watts: float, eclipse_energy_wh: float, reason: str, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO power_budget_versions(budget_id,version,total_watts,reserve_watts,eclipse_energy_wh,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (budget_id, version, total_watts, reserve_watts, eclipse_energy_wh, reason, actor, now),
        )
        return dict(self.budget_version(budget_id, version))

    def set_budget_current_version(self, budget_id: int, version: int, now: str) -> None:
        self.connection.execute("UPDATE power_budgets SET current_version=?,updated_at=? WHERE id=?", (version, now, budget_id))

    # 临时降额
    def derating_by_id(self, derating_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_deratings WHERE id=?", (derating_id,)).fetchone()

    def create_derating(self, *, budget_id: int, starts_at: str, ends_at: str, watts: float, reason: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_deratings(budget_id,starts_at,ends_at,watts,reason,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (budget_id, starts_at, ends_at, watts, reason, actor, now),
        )
        return dict(self.derating_by_id(cursor.lastrowid))

    def list_deratings(self, budget_id: int, *, active_only: bool = False) -> list[dict[str, Any]]:
        condition = "WHERE budget_id=? AND active=1" if active_only else "WHERE budget_id=?"
        rows = self.connection.execute(f"SELECT * FROM power_deratings {condition} ORDER BY starts_at,id", (budget_id,)).fetchall()
        return [dict(row) for row in rows]

    def deactivate_derating(self, derating_id: int) -> None:
        self.connection.execute("UPDATE power_deratings SET active=0 WHERE id=?", (derating_id,))

    # 作业计划与版本
    def plan_by_id(self, plan_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_plans WHERE id=?", (plan_id,)).fetchone()

    def plan_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_plans WHERE code=?", (code,)).fetchone()

    def plan_by_idempotency(self, requested_by: str, key: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_plans WHERE requested_by=? AND idempotency_key=?", (requested_by, key)).fetchone()

    def list_plans(self, *, status: str | None = None, code: str | None = None) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if status:
            clauses.append("status=?")
            values.append(status)
        if code:
            clauses.append("code=?")
            values.append(code)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute("SELECT * FROM power_plans" + where + " ORDER BY id DESC", values).fetchall()
        return [dict(row) for row in rows]

    def published_plan_ids(self, *, exclude_plan_id: int | None = None) -> list[int]:
        if exclude_plan_id is None:
            rows = self.connection.execute("SELECT id FROM power_plans WHERE status='published' ORDER BY id").fetchall()
        else:
            rows = self.connection.execute("SELECT id FROM power_plans WHERE status='published' AND id<>? ORDER BY id", (exclude_plan_id,)).fetchall()
        return [int(row["id"]) for row in rows]

    def create_plan(self, *, code: str, name: str, requested_by: str, idempotency_key: str, request_digest: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_plans(code,name,requested_by,idempotency_key,request_digest,status,current_version,created_at,updated_at) VALUES(?,?,?,?,?,'draft',1,?,?)",
            (code, name, requested_by, idempotency_key, request_digest, now, now),
        )
        return dict(self.plan_by_id(cursor.lastrowid))

    def plan_version(self, plan_id: int, version: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_plan_versions WHERE plan_id=? AND version=?", (plan_id, version)).fetchone()

    def plan_versions(self, plan_id: int) -> list[sqlite3.Row]:
        return self.connection.execute("SELECT * FROM power_plan_versions WHERE plan_id=? ORDER BY version", (plan_id,)).fetchall()

    def insert_plan_version(self, *, plan_id: int, version: int, jobs: list[dict[str, Any]], jobs_digest: str, change_note: str, actor: str, now: str) -> dict[str, Any]:
        self.connection.execute(
            "INSERT INTO power_plan_versions(plan_id,version,jobs_json,jobs_digest,change_note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (plan_id, version, json.dumps(jobs, ensure_ascii=False, sort_keys=True), jobs_digest, change_note, actor, now),
        )
        return dict(self.plan_version(plan_id, version))

    def update_plan_pointer(self, plan_id: int, version: int, now: str) -> None:
        self.connection.execute("UPDATE power_plans SET current_version=?,updated_at=? WHERE id=?", (version, now, plan_id))

    def update_plan_status(self, plan_id: int, status: str, now: str) -> None:
        self.connection.execute("UPDATE power_plans SET status=?,updated_at=? WHERE id=?", (status, now, plan_id))

    def add_plan_event(self, *, plan_id: int, actor: str, action: str, reason: str, from_status: str, to_status: str, plan_version: int, now: str) -> None:
        self.connection.execute(
            "INSERT INTO power_plan_events(plan_id,actor,action,reason,from_status,to_status,plan_version,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (plan_id, actor, action, reason, from_status, to_status, plan_version, now),
        )

    def plan_events(self, plan_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM power_plan_events WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()
        return [dict(row) for row in rows]

    # 评估
    def create_evaluation(self, *, plan_id: int, plan_version: int, budget_id: int, budget_version: int, formula_version: str, snapshot: dict[str, Any], input_digest: str, result: dict[str, Any], actor: str, now: str) -> int:
        cursor = self.connection.execute(
            "INSERT INTO power_evaluations(plan_id,plan_version,budget_id,budget_version,formula_version,input_snapshot_json,input_digest,decision,peak_watts,peak_at,total_energy_wh,result_json,reasons_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                plan_id, plan_version, budget_id, budget_version, formula_version,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True), input_digest,
                result["decision"], result["peak_watts"], result["peak_at"], result["total_energy_wh"],
                json.dumps(result, ensure_ascii=False, sort_keys=True),
                json.dumps(result["reasons"], ensure_ascii=False, sort_keys=True),
                actor, now,
            ),
        )
        return int(cursor.lastrowid)

    def evaluation_by_id(self, evaluation_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_evaluations WHERE id=?", (evaluation_id,)).fetchone()

    def list_evaluations(self, *, plan_id: int | None = None, budget_id: int | None = None, limit: int = 100) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        if plan_id is not None:
            clauses.append("plan_id=?")
            values.append(plan_id)
        if budget_id is not None:
            clauses.append("budget_id=?")
            values.append(budget_id)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        return self.connection.execute("SELECT * FROM power_evaluations" + where + " ORDER BY id DESC LIMIT ?", values).fetchall()

    def latest_evaluation_for_version(self, plan_id: int, plan_version: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM power_evaluations WHERE plan_id=? AND plan_version=? ORDER BY id DESC LIMIT 1",
            (plan_id, plan_version),
        ).fetchone()
