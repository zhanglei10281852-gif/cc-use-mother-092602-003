from __future__ import annotations

import argparse
import json

from fastapi.testclient import TestClient

from app.database import database_path, get_connection, init_db
from app.main import app


def command_init() -> int:
    init_db()
    print(json.dumps({"database": str(database_path()), "status": "initialized"}, ensure_ascii=False))
    return 0


def command_check() -> int:
    init_db()
    connection = get_connection()
    result = {
        "database": str(database_path()),
        "integrity": connection.execute("PRAGMA integrity_check").fetchone()[0],
        "foreign_keys": connection.execute("PRAGMA foreign_keys").fetchone()[0],
        "journal_mode": connection.execute("PRAGMA journal_mode").fetchone()[0],
        "tables": connection.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0],
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["integrity"] == "ok" and result["foreign_keys"] == 1 else 1


def command_smoke() -> int:
    with TestClient(app) as client:
        root = client.get("/")
        health = client.get("/api/system/health")
    result = {"root": root.json(), "health": health.json(), "status_codes": [root.status_code, health.status_code]}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status_codes"] == [200, 200] else 1


def command_compute_demo() -> int:
    template = {
        "code": "monte-carlo-demo",
        "name": "蒙特卡洛演示",
        "algorithm": "monte-carlo",
        "parameter_schema": {
            "samples": {"type": "integer", "required": True, "minimum": 10, "maximum": 1000000},
            "seed": {"type": "integer", "required": True},
        },
        "default_parameters": {},
        "max_runtime_seconds": 60,
        "max_attempts": 3,
    }
    with TestClient(app) as client:
        created = client.post("/api/compute/templates?actor=cli-demo", json=template)
        if created.status_code not in {201, 409}:
            print(created.text)
            return 1
        task = client.post(
            "/api/compute/tasks",
            json={
                "template_code": "monte-carlo-demo",
                "project_code": "demo",
                "requested_by": "cli-user",
                "parameters": {"samples": 1000, "seed": 42},
                "priority": 80,
                "idempotency_key": "compute-demo-000001",
            },
        )
        claimed = client.post(
            "/api/compute/tasks/claim",
            json={"worker_id": "cli-worker", "capabilities": ["monte-carlo"], "lease_seconds": 60},
        )
    result = {"task": task.status_code, "claimed": claimed.status_code, "task_id": task.json().get("id")}
    print(json.dumps(result, ensure_ascii=False))
    return 0 if task.status_code == 202 and claimed.status_code == 200 and claimed.json().get("task") else 1


def command_power_demo() -> int:
    with TestClient(app) as client:
        payload = client.post(
            "/api/power/payloads?actor=cli-demo",
            json={"code": "ai-inference-a", "name": "AI 推理载荷 A", "kind": "ai_inference", "nominal_watts": 300, "max_watts": 600},
        )
        if payload.status_code not in {201, 409}:
            print(payload.text)
            return 1
        segment = client.post(
            "/api/power/sunlight-segments?actor=cli-demo",
            json={"starts_at": "2026-10-01T00:00:00+00:00", "ends_at": "2026-10-01T04:00:00+00:00", "available_watts": 1000, "source": "orbit-propagator"},
        )
        if segment.status_code not in {201, 409}:
            print(segment.text)
            return 1
        budget = client.post(
            "/api/power/budgets?actor=cli-demo",
            json={"name": "main-bus", "total_watts": 1000, "reserve_watts": 100, "eclipse_energy_wh": 0, "reason": "初始 1kW 功率预算"},
        )
        if budget.status_code not in {201, 409}:
            print(budget.text)
            return 1
        plan = client.post(
            "/api/power/plans?actor=cli-demo",
            json={
                "code": "launch-batch-1",
                "name": "发射前推理批次 1",
                "requested_by": "cli-user",
                "idempotency_key": "power-demo-000001",
                "jobs": [
                    {
                        "job_code": "infer-1",
                        "payload_code": "ai-inference-a",
                        "starts_at": "2026-10-01T00:30:00+00:00",
                        "power_curve": [{"start_offset_seconds": 0, "end_offset_seconds": 3600, "watts": 400}],
                    }
                ],
            },
        )
        if plan.status_code not in {201, 409}:
            print(plan.text)
            return 1
        if plan.status_code == 201:
            plan_id = plan.json()["id"]
        else:
            existing = client.get("/api/power/plans", params={"code": "launch-batch-1"}).json()["items"]
            if not existing:
                print(plan.text)
                return 1
            plan_id = existing[0]["id"]
        evaluation = client.post(f"/api/power/plans/{plan_id}/evaluate", json={"actor": "cli-demo"})
    result = {
        "plan_id": plan_id,
        "evaluation_status": evaluation.status_code,
        "decision": evaluation.json().get("decision"),
        "peak_watts": evaluation.json().get("peak_watts"),
        "total_energy_wh": evaluation.json().get("total_energy_wh"),
    }
    print(json.dumps(result, ensure_ascii=False))
    return 0 if evaluation.status_code == 201 else 1


def main() -> int:
    parser = argparse.ArgumentParser(prog="compute-operations", description="科学计算任务运营服务维护入口")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("init-db", help="初始化 SQLite 数据库")
    subparsers.add_parser("check-db", help="检查数据库完整性")
    subparsers.add_parser("smoke", help="执行本地 API 冒烟检查")
    subparsers.add_parser("compute-demo", help="执行计算任务提交与领取演示")
    subparsers.add_parser("power-demo", help="执行卫星功率预算登记、计划与评估演示")
    args = parser.parse_args()
    commands = {
        "init-db": command_init,
        "check-db": command_check,
        "smoke": command_smoke,
        "compute-demo": command_compute_demo,
        "power-demo": command_power_demo,
    }
    return commands[args.command]()


if __name__ == "__main__":
    raise SystemExit(main())
