from __future__ import annotations

import json
import sqlite3
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS power_payloads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    baseline_power_w REAL NOT NULL CHECK(baseline_power_w >= 0),
    description TEXT NOT NULL DEFAULT '',
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS power_sun_windows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    power_w REAL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_power_sun_time ON power_sun_windows(start_at, end_at);
CREATE TABLE IF NOT EXISTS power_deratings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    start_at TEXT NOT NULL,
    end_at TEXT NOT NULL,
    available_power_w REAL NOT NULL CHECK(available_power_w >= 0),
    reason TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_power_derating_time ON power_deratings(start_at, end_at);
CREATE TABLE IF NOT EXISTS power_budget_revisions (
    revision INTEGER PRIMARY KEY AUTOINCREMENT,
    sunlit_power_w REAL NOT NULL CHECK(sunlit_power_w >= 0),
    eclipse_power_w REAL NOT NULL CHECK(eclipse_power_w >= 0),
    battery_capacity_wh REAL NOT NULL DEFAULT 0 CHECK(battery_capacity_wh >= 0),
    max_depth_of_discharge REAL NOT NULL DEFAULT 0.8 CHECK(max_depth_of_discharge BETWEEN 0 AND 1),
    reason TEXT NOT NULL,
    changed_by TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS power_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    current_revision INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS power_job_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES power_jobs(id) ON DELETE RESTRICT,
    revision INTEGER NOT NULL,
    name TEXT NOT NULL,
    payload_code TEXT NOT NULL,
    priority INTEGER NOT NULL CHECK(priority BETWEEN 0 AND 100),
    scheduled_start_at TEXT NOT NULL,
    phases_json TEXT NOT NULL,
    change_note TEXT NOT NULL DEFAULT '',
    submitted_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, revision)
);
CREATE INDEX IF NOT EXISTS idx_power_job_revisions_job ON power_job_revisions(job_id, revision);
CREATE TABLE IF NOT EXISTS power_plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft' CHECK(status IN ('draft','published','withdrawn')),
    current_version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS power_plan_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES power_plans(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    name TEXT NOT NULL,
    job_codes_json TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(plan_id, version)
);
CREATE INDEX IF NOT EXISTS idx_power_plan_versions_plan ON power_plan_versions(plan_id, version);
CREATE TABLE IF NOT EXISTS power_plan_publications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_id INTEGER NOT NULL REFERENCES power_plans(id) ON DELETE RESTRICT,
    version INTEGER NOT NULL,
    action TEXT NOT NULL CHECK(action IN ('publish','withdraw')),
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    evaluation_id INTEGER REFERENCES power_evaluations(id),
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_power_plan_publications_plan ON power_plan_publications(plan_id, id);
CREATE TABLE IF NOT EXISTS power_evaluations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL CHECK(scope IN ('job','plan','recheck')),
    subject_key TEXT NOT NULL,
    subject_version INTEGER,
    budget_revision INTEGER NOT NULL,
    formula_version TEXT NOT NULL,
    feasible INTEGER NOT NULL CHECK(feasible IN (0,1)),
    snapshot_digest TEXT NOT NULL,
    snapshot_json TEXT NOT NULL,
    result_json TEXT NOT NULL,
    actor TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_power_eval_subject ON power_evaluations(scope, subject_key, id);
CREATE INDEX IF NOT EXISTS idx_power_eval_created ON power_evaluations(created_at DESC);
"""

INITIAL_BUDGET = {
    "sunlit_power_w": 1000.0,
    "eclipse_power_w": 1000.0,
    "battery_capacity_wh": 800.0,
    "max_depth_of_discharge": 0.8,
    "reason": "卫星初始 1 千瓦功率预算",
    "changed_by": "system",
}


def dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def loads(value: str) -> Any:
    return json.loads(value)


class PowerRepository:
    """功率预算领域的 SQLite 读写。"""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def ensure_schema(self, now: str) -> None:
        self.connection.executescript(SCHEMA)
        existing = self.connection.execute("SELECT COUNT(*) FROM power_budget_revisions").fetchone()[0]
        if not existing:
            self.connection.execute(
                "INSERT INTO power_budget_revisions(sunlit_power_w,eclipse_power_w,battery_capacity_wh,max_depth_of_discharge,reason,changed_by,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    INITIAL_BUDGET["sunlit_power_w"],
                    INITIAL_BUDGET["eclipse_power_w"],
                    INITIAL_BUDGET["battery_capacity_wh"],
                    INITIAL_BUDGET["max_depth_of_discharge"],
                    INITIAL_BUDGET["reason"],
                    INITIAL_BUDGET["changed_by"],
                    now,
                ),
            )

    # ---- 载荷 ----
    def payload_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_payloads WHERE code=?", (code,)).fetchone()

    def list_payloads(self, *, include_inactive: bool = False) -> list[dict[str, Any]]:
        sql = "SELECT * FROM power_payloads"
        if not include_inactive:
            sql += " WHERE active=1"
        sql += " ORDER BY code"
        return [dict(row) for row in self.connection.execute(sql).fetchall()]

    def create_payload(self, *, payload: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_payloads(code,name,baseline_power_w,description,active,revision,created_by,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?,?)",
            (payload["code"], payload["name"], payload["baseline_power_w"], payload["description"], 1 if payload["active"] else 0, actor, now, now),
        )
        return dict(self.payload_by_code(cursor.lastrowid) or self.payload_by_code(payload["code"]))

    def update_payload(self, *, code: str, changes: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        current = self.payload_by_code(code)
        fields = {
            "name": changes.get("name", current["name"]),
            "baseline_power_w": changes.get("baseline_power_w", current["baseline_power_w"]) if changes.get("baseline_power_w") is not None else current["baseline_power_w"],
            "description": changes.get("description", current["description"]) if changes.get("description") is not None else current["description"],
            "active": current["active"] if changes.get("active") is None else (1 if changes["active"] else 0),
        }
        self.connection.execute(
            "UPDATE power_payloads SET name=?,baseline_power_w=?,description=?,active=?,revision=revision+1,created_by=COALESCE(created_by,?),updated_at=? WHERE code=?",
            (fields["name"], fields["baseline_power_w"], fields["description"], fields["active"], actor, now, code),
        )
        return dict(self.payload_by_code(code))

    # ---- 日照段与降额 ----
    def list_sun_windows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM power_sun_windows ORDER BY start_at,id").fetchall()]

    def create_sun_window(self, *, start_at: str, end_at: str, power_w: float | None, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_sun_windows(start_at,end_at,power_w,created_by,created_at) VALUES(?,?,?,?,?)",
            (start_at, end_at, power_w, actor, now),
        )
        return dict(self.connection.execute("SELECT * FROM power_sun_windows WHERE id=?", (cursor.lastrowid,)).fetchone())

    def list_deratings(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM power_deratings ORDER BY start_at,id").fetchall()]

    def create_derating(self, *, start_at: str, end_at: str, available_power_w: float, reason: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_deratings(start_at,end_at,available_power_w,reason,created_by,created_at) VALUES(?,?,?,?,?,?)",
            (start_at, end_at, available_power_w, reason, actor, now),
        )
        return dict(self.connection.execute("SELECT * FROM power_deratings WHERE id=?", (cursor.lastrowid,)).fetchone())

    # ---- 预算版本 ----
    def current_budget(self) -> sqlite3.Row:
        return self.connection.execute("SELECT * FROM power_budget_revisions ORDER BY revision DESC LIMIT 1").fetchone()

    def budget_by_revision(self, revision: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_budget_revisions WHERE revision=?", (revision,)).fetchone()

    def list_budget_revisions(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM power_budget_revisions ORDER BY revision DESC").fetchall()]

    def add_budget_revision(self, *, values: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_budget_revisions(sunlit_power_w,eclipse_power_w,battery_capacity_wh,max_depth_of_discharge,reason,changed_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (values["sunlit_power_w"], values["eclipse_power_w"], values["battery_capacity_wh"], values["max_depth_of_discharge"], values["reason"], actor, now),
        )
        return dict(self.budget_by_revision(cursor.lastrowid))

    # ---- 作业申请（保留每次修订原文）----
    def job_by_code(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_jobs WHERE code=?", (code,)).fetchone()

    def job_revision(self, job_id: int, revision: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_job_revisions WHERE job_id=? AND revision=?", (job_id, revision)).fetchone()

    def current_job_revision_row(self, code: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT r.* FROM power_job_revisions r JOIN power_jobs j ON j.id=r.job_id WHERE j.code=? AND j.current_revision=r.revision",
            (code,),
        ).fetchone()

    def create_job(self, *, values: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute("INSERT INTO power_jobs(code,current_revision,created_at) VALUES(?,1,?)", (values["code"], now))
        job_id = cursor.lastrowid
        self.connection.execute(
            "INSERT INTO power_job_revisions(job_id,revision,name,payload_code,priority,scheduled_start_at,phases_json,change_note,submitted_by,created_at) VALUES(?,1,?,?,?,?,?,'原始申请',?,?)",
            (job_id, values["name"], values["payload_code"], values["priority"], values["scheduled_start_at"], dumps(values["phases"]), actor, now),
        )
        return self.get_job_detail(values["code"])

    def amend_job(self, *, code: str, changes: dict[str, Any], note: str, actor: str, now: str) -> dict[str, Any]:
        job = self.job_by_code(code)
        current = self.current_job_revision_row(code)
        revision = job["current_revision"] + 1
        phases = changes["phases"] if changes.get("phases") is not None else loads(current["phases_json"])
        self.connection.execute(
            "INSERT INTO power_job_revisions(job_id,revision,name,payload_code,priority,scheduled_start_at,phases_json,change_note,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                job["id"],
                revision,
                current["name"] if changes.get("name") is None else changes["name"],
                current["payload_code"] if changes.get("payload_code") is None else changes["payload_code"],
                current["priority"] if changes.get("priority") is None else changes["priority"],
                current["scheduled_start_at"] if changes.get("scheduled_start_at") is None else changes["scheduled_start_at"],
                dumps(phases),
                note,
                actor,
                now,
            ),
        )
        self.connection.execute("UPDATE power_jobs SET current_revision=? WHERE id=?", (revision, job["id"]))
        return self.get_job_detail(code)

    def list_job_revisions(self, code: str) -> list[dict[str, Any]]:
        job = self.job_by_code(code)
        rows = self.connection.execute("SELECT * FROM power_job_revisions WHERE job_id=? ORDER BY revision", (job["id"],)).fetchall()
        return [dict(row) for row in rows]

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self.get_job_detail(row["code"]) for row in self.connection.execute("SELECT code FROM power_jobs ORDER BY code").fetchall()]

    def get_job_detail(self, code: str) -> dict[str, Any]:
        row = self.current_job_revision_row(code)
        if row is None:
            return {}
        return {
            "code": code,
            "current_revision": self.job_by_code(code)["current_revision"],
            "name": row["name"],
            "payload_code": row["payload_code"],
            "priority": row["priority"],
            "scheduled_start_at": row["scheduled_start_at"],
            "phases": loads(row["phases_json"]),
            "submitted_by": row["submitted_by"],
            "updated_at": row["created_at"],
        }

    # ---- 计划草案 / 版本 / 发布 ----
    def plan_by_id(self, plan_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_plans WHERE id=?", (plan_id,)).fetchone()

    def plan_version(self, plan_id: int, version: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_plan_versions WHERE plan_id=? AND version=?", (plan_id, version)).fetchone()

    def create_plan(self, *, name: str, job_codes: list[str], note: str, actor: str, now: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_plans(name,status,current_version,created_by,created_at,updated_at) VALUES(?,'draft',1,?,?,?)",
            (name, actor, now, now),
        )
        plan_id = cursor.lastrowid
        self.connection.execute(
            "INSERT INTO power_plan_versions(plan_id,version,name,job_codes_json,note,created_by,created_at) VALUES(?,1,?,?,?,?,?)",
            (plan_id, name, dumps(job_codes), note, actor, now),
        )
        return self.get_plan_detail(plan_id)

    def update_plan(self, *, plan_id: int, changes: dict[str, Any], actor: str, now: str) -> dict[str, Any]:
        plan = self.plan_by_id(plan_id)
        current = self.plan_version(plan_id, plan["current_version"])
        version = plan["current_version"] + 1
        name = changes.get("name", current["name"])
        job_codes = changes.get("job_codes", loads(current["job_codes_json"]))
        note = changes.get("note", current["note"])
        self.connection.execute(
            "INSERT INTO power_plan_versions(plan_id,version,name,job_codes_json,note,created_by,created_at) VALUES(?,?,?,?,?,?,?)",
            (plan_id, version, name, dumps(job_codes), note, actor, now),
        )
        self.connection.execute("UPDATE power_plans SET name=?,current_version=?,updated_at=? WHERE id=?", (name, version, now, plan_id))
        return self.get_plan_detail(plan_id)

    def set_plan_status(self, plan_id: int, status: str, now: str) -> None:
        self.connection.execute("UPDATE power_plans SET status=?,updated_at=? WHERE id=?", (status, now, plan_id))

    def add_publication(self, *, plan_id: int, version: int, action: str, actor: str, reason: str, evaluation_id: int | None, now: str) -> None:
        self.connection.execute(
            "INSERT INTO power_plan_publications(plan_id,version,action,actor,reason,evaluation_id,created_at) VALUES(?,?,?,?,?,?,?)",
            (plan_id, version, action, actor, reason, evaluation_id, now),
        )

    def list_plans(self, *, status: str | None = None) -> list[dict[str, Any]]:
        if status:
            rows = self.connection.execute("SELECT id FROM power_plans WHERE status=? ORDER BY id", (status,)).fetchall()
        else:
            rows = self.connection.execute("SELECT id FROM power_plans ORDER BY id").fetchall()
        return [self.get_plan_detail(row["id"]) for row in rows]

    def list_plan_versions(self, plan_id: int) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM power_plan_versions WHERE plan_id=? ORDER BY version", (plan_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["job_codes"] = loads(item.pop("job_codes_json"))
            result.append(item)
        return result

    def list_publications(self, plan_id: int) -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute("SELECT * FROM power_plan_publications WHERE plan_id=? ORDER BY id", (plan_id,)).fetchall()]

    def get_plan_detail(self, plan_id: int) -> dict[str, Any]:
        plan = self.plan_by_id(plan_id)
        version = self.plan_version(plan_id, plan["current_version"])
        return {
            "id": plan_id,
            "name": plan["name"],
            "status": plan["status"],
            "current_version": plan["current_version"],
            "job_codes": loads(version["job_codes_json"]),
            "note": version["note"],
            "created_by": plan["created_by"],
            "created_at": plan["created_at"],
            "updated_at": plan["updated_at"],
        }

    # ---- 评估记录 ----
    def save_evaluation(self, *, scope: str, subject_key: str, subject_version: int | None, budget_revision: int, formula_version: str, feasible: bool, snapshot: dict[str, Any], result: dict[str, Any], actor: str, now: str, snapshot_digest: str) -> dict[str, Any]:
        cursor = self.connection.execute(
            "INSERT INTO power_evaluations(scope,subject_key,subject_version,budget_revision,formula_version,feasible,snapshot_digest,snapshot_json,result_json,actor,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (scope, subject_key, subject_version, budget_revision, formula_version, 1 if feasible else 0, snapshot_digest, dumps(snapshot), dumps(result), actor, now),
        )
        return dict(self.connection.execute("SELECT * FROM power_evaluations WHERE id=?", (cursor.lastrowid,)).fetchone())

    def evaluation_by_id(self, evaluation_id: int) -> sqlite3.Row | None:
        return self.connection.execute("SELECT * FROM power_evaluations WHERE id=?", (evaluation_id,)).fetchone()

    def list_evaluations(self, *, scope: str | None = None, subject_key: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        clauses: list[str] = []
        values: list[Any] = []
        if scope:
            clauses.append("scope=?")
            values.append(scope)
        if subject_key:
            clauses.append("subject_key=?")
            values.append(subject_key)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        values.append(limit)
        rows = self.connection.execute(
            "SELECT id,scope,subject_key,subject_version,budget_revision,formula_version,feasible,snapshot_digest,actor,created_at FROM power_evaluations" + where + " ORDER BY id DESC LIMIT ?",
            values,
        ).fetchall()
        return [dict(row) for row in rows]
