from __future__ import annotations

import pytest

from app.power.service import FORMULA_VERSION, digest

SEGMENT_START = "2026-10-01T00:00:00+00:00"
SEGMENT_END = "2026-10-01T04:00:00+00:00"


def make_payload(client, code: str = "ai-infer-a", max_watts: float = 600) -> dict:
    response = client.post(
        "/api/power/payloads?actor=pm",
        json={"code": code, "name": f"载荷 {code}", "kind": "ai_inference", "nominal_watts": 300, "max_watts": max_watts},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_segment(client, starts: str = SEGMENT_START, ends: str = SEGMENT_END, watts: float = 1000) -> dict:
    response = client.post(
        "/api/power/sunlight-segments?actor=pm",
        json={"starts_at": starts, "ends_at": ends, "available_watts": watts, "source": "orbit"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def make_budget(client, name: str = "main-bus", total: float = 1000, reserve: float = 100, eclipse: float = 0) -> dict:
    response = client.post(
        "/api/power/budgets?actor=pm",
        json={"name": name, "total_watts": total, "reserve_watts": reserve, "eclipse_energy_wh": eclipse, "reason": "初始预算"},
    )
    assert response.status_code == 201, response.text
    return response.json()


def job(job_code: str, payload: str, starts_at: str, pieces: list[dict], priority: int = 50) -> dict:
    return {"job_code": job_code, "payload_code": payload, "starts_at": starts_at, "priority": priority, "power_curve": pieces}


def flat(offset_start: int, offset_end: int, watts: float) -> dict:
    return {"start_offset_seconds": offset_start, "end_offset_seconds": offset_end, "watts": watts}


def make_plan(client, jobs: list[dict], code: str = "batch-1", key: str = "plan-key-000001", user: str = "pm") -> dict:
    response = client.post(
        "/api/power/plans?actor=pm",
        json={"code": code, "name": f"计划 {code}", "requested_by": user, "idempotency_key": key, "jobs": jobs},
    )
    assert response.status_code == 201, response.text
    return response.json()


def accepted_setup(client) -> dict:
    """两个错峰作业：峰值 800W、能量 800Wh，落在 1000W/预留100W 预算与 4000Wh 日照段内。"""
    make_payload(client)
    make_segment(client)
    make_budget(client)
    jobs = [
        job("infer-a", "ai-infer-a", "2026-10-01T00:00:00+00:00", [flat(0, 3600, 300)]),
        job("infer-b", "ai-infer-a", "2026-10-01T00:30:00+00:00", [flat(0, 3600, 500)]),
    ]
    return make_plan(client, jobs)


def transition(client, plan_id: int, action: str, version: int, actor: str = "pm", reason: str = "流程推进"):
    return client.post(f"/api/power/plans/{plan_id}/{action}", json={"expected_version": version, "actor": actor, "reason": reason})


def test_payload_segment_budget_registration_and_conflicts(client):
    payload = make_payload(client)
    assert payload["code"] == "ai-infer-a" and payload["version"] == 1
    duplicate = client.post(
        "/api/power/payloads?actor=pm",
        json={"code": "ai-infer-a", "name": "重复", "kind": "ai_inference", "nominal_watts": 1, "max_watts": 2},
    )
    assert duplicate.status_code == 409

    segment = make_segment(client)
    overlap = client.post(
        "/api/power/sunlight-segments?actor=pm",
        json={"starts_at": "2026-10-01T02:00:00+00:00", "ends_at": "2026-10-01T06:00:00+00:00", "available_watts": 800},
    )
    assert overlap.status_code == 409
    assert overlap.json()["error"]["context"]["conflict_segment_id"] == segment["id"]
    adjacent = client.post(
        "/api/power/sunlight-segments?actor=pm",
        json={"starts_at": SEGMENT_END, "ends_at": "2026-10-01T06:00:00+00:00", "available_watts": 800},
    )
    assert adjacent.status_code == 201

    budget = make_budget(client)
    assert budget["current_version"] == 1 and budget["current"]["total_watts"] == 1000
    stale = client.post(
        f"/api/power/budgets/{budget['id']}/adjust?actor=pm",
        json={"expected_version": 99, "total_watts": 900, "reason": " stale "},
    )
    assert stale.status_code == 409
    adjusted = client.post(
        f"/api/power/budgets/{budget['id']}/adjust?actor=pm",
        json={"expected_version": 1, "total_watts": 850, "reserve_watts": 50, "eclipse_energy_wh": 20, "reason": "电池老化降额"},
    )
    assert adjusted.status_code == 201
    assert adjusted.json()["current_version"] == 2
    assert [item["version"] for item in adjusted.json()["versions"]] == [1, 2]

    renamed = client.patch(
        f"/api/power/payloads/{payload['id']}?actor=pm",
        json={"expected_version": 1, "max_watts": 550},
    )
    assert renamed.status_code == 200 and renamed.json()["version"] == 2 and renamed.json()["max_watts"] == 550
    stale_payload = client.patch(f"/api/power/payloads/{payload['id']}?actor=pm", json={"expected_version": 1, "max_watts": 500})
    assert stale_payload.status_code == 409


def test_plan_draft_versions_idempotency_and_optimistic_lock(client):
    make_payload(client)
    jobs = [job("infer-a", "ai-infer-a", "2026-10-01T00:00:00+00:00", [flat(0, 600, 300)])]
    plan = make_plan(client, jobs)
    assert plan["status"] == "draft" and plan["current_version"] == 1

    replay = client.post(
        "/api/power/plans?actor=pm",
        json={"code": "batch-1", "name": "计划 batch-1", "requested_by": "pm", "idempotency_key": "plan-key-000001", "jobs": jobs},
    )
    assert replay.status_code == 201 and replay.json()["id"] == plan["id"]
    conflicting = client.post(
        "/api/power/plans?actor=pm",
        json={"code": "batch-1", "name": "计划 batch-1", "requested_by": "pm", "idempotency_key": "plan-key-000001", "jobs": [job("infer-b", "ai-infer-a", "2026-10-01T00:00:00+00:00", [flat(0, 600, 300)])]},
    )
    assert conflicting.status_code == 409

    stale = client.put(
        f"/api/power/plans/{plan['id']}/versions?actor=pm",
        json={"expected_version": 7, "change_note": "过期版本", "jobs": jobs},
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["context"]["current_version"] == 1

    updated_jobs = jobs + [job("infer-b", "ai-infer-a", "2026-10-01T01:00:00+00:00", [flat(0, 600, 200)])]
    second = client.put(
        f"/api/power/plans/{plan['id']}/versions?actor=pm",
        json={"expected_version": 1, "change_note": "追加作业", "jobs": updated_jobs},
    )
    assert second.status_code == 201
    assert second.json()["current_version"] == 2
    assert [item["version"] for item in second.json()["versions"]] == [1, 2]

    overlapping = client.post(
        "/api/power/plans?actor=pm",
        json={
            "code": "batch-bad", "name": "坏曲线", "requested_by": "pm", "idempotency_key": "plan-key-000002",
            "jobs": [job("infer-x", "ai-infer-a", "2026-10-01T00:00:00+00:00", [flat(0, 600, 100), flat(300, 900, 100)])],
        },
    )
    assert overlapping.status_code == 422
    missing_payload = client.post(
        "/api/power/plans?actor=pm",
        json={
            "code": "batch-missing", "name": "缺载荷", "requested_by": "pm", "idempotency_key": "plan-key-000003",
            "jobs": [job("infer-y", "ghost", "2026-10-01T00:00:00+00:00", [flat(0, 600, 100)])],
        },
    )
    assert missing_payload.status_code == 422


def test_evaluation_accepted_reports_peak_and_energy(client):
    plan = accepted_setup(client)
    response = client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm"})
    assert response.status_code == 201, response.text
    evaluation = response.json()
    assert evaluation["decision"] == "accepted"
    assert evaluation["peak_watts"] == 800
    assert evaluation["peak_at"] == "2026-10-01T00:30:00+00:00"
    assert evaluation["total_energy_wh"] == 800
    assert evaluation["reasons"] == []
    assert evaluation["formula_version"] == FORMULA_VERSION
    segment = evaluation["result"]["segments"][0]
    assert segment["demand_wh"] == 800 and segment["available_wh"] == 4000 and segment["ok"]
    assert evaluation["result"]["eclipse"] == {"demand_wh": 0, "allowance_wh": 0, "ok": True}


def test_evaluation_rejection_reasons(client):
    make_payload(client)
    make_segment(client)
    make_budget(client)

    # 峰值超上限：600W + 500W 在 01:00-02:00 重叠，超过 1000-100=900W。
    peak_plan = make_plan(
        client,
        [
            job("infer-a", "ai-infer-a", "2026-10-01T00:00:00+00:00", [flat(0, 7200, 600)]),
            job("infer-b", "ai-infer-a", "2026-10-01T01:00:00+00:00", [flat(0, 7200, 500)]),
        ],
        code="batch-peak",
        key="plan-key-000010",
    )
    peak_eval = client.post(f"/api/power/plans/{peak_plan['id']}/evaluate", json={"actor": "pm"}).json()
    assert peak_eval["decision"] == "rejected"
    reason = next(item for item in peak_eval["reasons"] if item["code"] == "peak_exceeds_cap")
    assert reason["context"]["max_demand_watts"] == 1100
    assert reason["context"]["min_cap_watts"] == 900
    assert reason["context"]["starts_at"] == "2026-10-01T01:00:00+00:00"

    # 日照段能量不足：200W 段 1 小时只有 200Wh，作业需要 300Wh。
    make_segment(client, "2026-10-02T00:00:00+00:00", "2026-10-02T01:00:00+00:00", watts=200)
    energy_plan = make_plan(
        client,
        [job("infer-c", "ai-infer-a", "2026-10-02T00:00:00+00:00", [flat(0, 3600, 300)])],
        code="batch-energy",
        key="plan-key-000011",
    )
    energy_eval = client.post(f"/api/power/plans/{energy_plan['id']}/evaluate", json={"actor": "pm"}).json()
    energy_reason = next(item for item in energy_eval["reasons"] if item["code"] == "segment_energy_exceeded")
    assert energy_reason["context"]["demand_wh"] == 300
    assert energy_reason["context"]["available_wh"] == 200

    # 阴影区消耗：10:00 不在任何日照段内，预算阴影余量为 0。
    eclipse_plan = make_plan(
        client,
        [job("infer-d", "ai-infer-a", "2026-10-01T10:00:00+00:00", [flat(0, 3600, 100)])],
        code="batch-eclipse",
        key="plan-key-000012",
    )
    eclipse_eval = client.post(f"/api/power/plans/{eclipse_plan['id']}/evaluate", json={"actor": "pm"}).json()
    eclipse_reason = next(item for item in eclipse_eval["reasons"] if item["code"] == "eclipse_energy_exceeded")
    assert eclipse_reason["context"]["demand_wh"] == 100

    # 载荷上限：作业 700W 超过载荷 600W 上限。
    payload_plan = make_plan(
        client,
        [job("infer-e", "ai-infer-a", "2026-10-01T00:00:00+00:00", [flat(0, 600, 700)])],
        code="batch-payload",
        key="plan-key-000013",
    )
    payload_eval = client.post(f"/api/power/plans/{payload_plan['id']}/evaluate", json={"actor": "pm"}).json()
    assert any(item["code"] == "payload_max_exceeded" for item in payload_eval["reasons"])


def test_derating_and_budget_adjust_reevaluate_without_touching_plan(client):
    plan = accepted_setup(client)
    budget = client.get("/api/power/budgets").json()["items"][0]
    first = client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm"}).json()
    assert first["decision"] == "accepted"

    # 临时降额 200W：01:00-01:30 有效上限降为 700W，峰值 800W 不再可行。
    derating = client.post(
        f"/api/power/budgets/{budget['id']}/deratings?actor=pm",
        json={"starts_at": "2026-10-01T00:30:00+00:00", "ends_at": "2026-10-01T01:30:00+00:00", "watts": 200, "reason": "电池加热器优先"},
    )
    assert derating.status_code == 201
    second = client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm"}).json()
    assert second["decision"] == "rejected"
    reason = next(item for item in second["reasons"] if item["code"] == "peak_exceeds_cap")
    assert reason["context"]["min_cap_watts"] == 700

    # 取消降额后恢复可行。
    client.delete(f"/api/power/deratings/{derating.json()['id']}?actor=pm")
    third = client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm"}).json()
    assert third["decision"] == "accepted"

    # 走完审批发布流程，再调整预算并触发重估。
    for action in ("submit", "approve", "publish"):
        response = transition(client, plan["id"], action, 1)
        assert response.status_code == 200, response.text
    adjusted = client.post(
        f"/api/power/budgets/{budget['id']}/adjust?actor=pm",
        json={"expected_version": 1, "total_watts": 750, "reserve_watts": 0, "eclipse_energy_wh": 0, "reason": "帆板衰减，预算收紧", "reevaluate": True},
    )
    assert adjusted.status_code == 201
    reevaluations = adjusted.json()["reevaluations"]
    assert len(reevaluations) == 1 and reevaluations[0]["decision"] == "rejected"
    assert reevaluations[0]["budget_version"] == 2

    # 原始申请未被篡改：计划版本与作业摘要保持不变。
    after = client.get(f"/api/power/plans/{plan['id']}").json()
    assert after["current_version"] == 1
    assert after["versions"][0]["jobs_digest"] == plan["versions"][0]["jobs_digest"]
    evaluations = client.get(f"/api/power/plans/{plan['id']}/evaluations").json()["items"]
    assert len(evaluations) == 4
    assert {item["budget_version"] for item in evaluations} == {1, 2}


def test_plan_lifecycle_and_publish_conflict(client):
    plan_a = accepted_setup(client)
    edit_after_submit = transition(client, plan_a["id"], "submit", 1)
    assert edit_after_submit.status_code == 200
    blocked_edit = client.put(
        f"/api/power/plans/{plan_a['id']}/versions?actor=pm",
        json={"expected_version": 1, "change_note": "提交后修改", "jobs": plan_a["jobs"]},
    )
    assert blocked_edit.status_code == 409

    recalled = transition(client, plan_a["id"], "withdraw", 1)
    assert recalled.status_code == 200 and recalled.json()["status"] == "draft"
    assert transition(client, plan_a["id"], "submit", 1).status_code == 200
    assert transition(client, plan_a["id"], "approve", 1).status_code == 200
    published = transition(client, plan_a["id"], "publish", 1)
    assert published.status_code == 200 and published.json()["status"] == "published"

    plan_b = make_plan(
        client,
        [job("infer-z", "ai-infer-a", "2026-10-01T00:00:00+00:00", [flat(0, 600, 100)])],
        code="batch-2",
        key="plan-key-000020",
    )
    transition(client, plan_b["id"], "submit", 1)
    transition(client, plan_b["id"], "approve", 1)
    conflict = transition(client, plan_b["id"], "publish", 1)
    assert conflict.status_code == 409
    assert conflict.json()["error"]["context"]["published_plan_ids"] == [plan_a["id"]]

    withdrawn = transition(client, plan_a["id"], "withdraw", 1, reason="发射计划调整")
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    assert transition(client, plan_b["id"], "publish", 1).status_code == 200

    stale = transition(client, plan_b["id"], "withdraw", 99)
    assert stale.status_code == 409
    gone = transition(client, plan_a["id"], "submit", 1)
    assert gone.status_code == 409

    events = client.get(f"/api/power/plans/{plan_a['id']}").json()["events"]
    assert [item["action"] for item in events] == ["create", "submit", "withdraw", "submit", "approve", "publish", "withdraw"]


def test_compare_versions(client):
    make_payload(client)
    jobs_v1 = [
        job("infer-a", "ai-infer-a", "2026-10-01T00:00:00+00:00", [flat(0, 600, 300)], priority=50),
        job("infer-b", "ai-infer-a", "2026-10-01T01:00:00+00:00", [flat(0, 600, 200)]),
    ]
    plan = make_plan(client, jobs_v1)
    jobs_v2 = [
        job("infer-a", "ai-infer-a", "2026-10-01T00:00:00+00:00", [flat(0, 600, 300)], priority=90),
        job("infer-c", "ai-infer-a", "2026-10-01T02:00:00+00:00", [flat(0, 900, 150)]),
    ]
    updated = client.put(
        f"/api/power/plans/{plan['id']}/versions?actor=pm",
        json={"expected_version": 1, "change_note": "调整优先级并替换作业", "jobs": jobs_v2},
    )
    assert updated.status_code == 201

    make_segment(client)
    make_budget(client)
    client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm", "plan_version": 2})

    compare = client.get(f"/api/power/plans/{plan['id']}/compare", params={"from_version": 1, "to_version": 2}).json()
    assert compare["added_jobs"] == ["infer-c"]
    assert compare["removed_jobs"] == ["infer-b"]
    assert compare["unchanged_jobs"] == 0
    assert compare["changed_jobs"][0]["job_code"] == "infer-a"
    assert compare["changed_jobs"][0]["changes"]["priority"] == {"from": 50, "to": 90}
    assert compare["from"]["latest_evaluation"] is None
    assert compare["to"]["latest_evaluation"]["decision"] == "accepted"
    assert compare["to"]["latest_evaluation"]["formula_version"] == FORMULA_VERSION


def test_evaluation_snapshot_and_formula_version_for_admin(client):
    plan = accepted_setup(client)
    evaluation = client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm"}).json()
    detail = client.get(f"/api/power/evaluations/{evaluation['id']}").json()
    assert detail["formula_version"] == FORMULA_VERSION
    assert detail["input_digest"] == digest(detail["input_snapshot"])
    snapshot = detail["input_snapshot"]
    assert snapshot["budget"]["version"] == 1
    assert snapshot["budget"]["total_watts"] == 1000
    assert len(snapshot["segments"]) == 1
    assert snapshot["payloads"][0]["code"] == "ai-infer-a"

    # 预算调整后，历史评估的输入快照仍然保持原样。
    budget = client.get("/api/power/budgets").json()["items"][0]
    client.post(
        f"/api/power/budgets/{budget['id']}/adjust?actor=pm",
        json={"expected_version": 1, "total_watts": 500, "reason": "进一步收紧"},
    )
    again = client.get(f"/api/power/evaluations/{evaluation['id']}").json()
    assert again["input_snapshot"]["budget"]["total_watts"] == 1000
    assert again["input_digest"] == detail["input_digest"]

    listed = client.get("/api/power/evaluations", params={"plan_id": plan["id"]}).json()["items"]
    assert [item["id"] for item in listed] == [evaluation["id"]]
    assert "input_snapshot" not in listed[0]


def test_evaluate_requires_budget_selection_and_valid_state(client):
    plan = accepted_setup(client)
    make_budget(client, name="backup-bus", total=500)
    ambiguous = client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm"})
    assert ambiguous.status_code == 422
    budgets = client.get("/api/power/budgets").json()["items"]
    explicit = client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm", "budget_id": budgets[1]["id"]})
    assert explicit.status_code == 201
    assert explicit.json()["budget_id"] == budgets[1]["id"]

    transition(client, plan["id"], "submit", 1)
    transition(client, plan["id"], "approve", 1)
    transition(client, plan["id"], "publish", 1)
    transition(client, plan["id"], "withdraw", 1)
    withdrawn = client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm", "budget_id": budgets[0]["id"]})
    assert withdrawn.status_code == 409
    missing = client.post("/api/power/plans/9999/evaluate", json={"actor": "pm"})
    assert missing.status_code == 404


def test_multi_segment_job_spanning(client):
    """跨多个日照段的作业：能量按段归属，段间阴影时间单独核算。"""
    make_payload(client)
    make_segment(client, "2026-10-01T00:00:00+00:00", "2026-10-01T01:00:00+00:00", watts=1000)
    make_segment(client, "2026-10-01T02:00:00+00:00", "2026-10-01T03:00:00+00:00", watts=1000)
    make_budget(client, eclipse=200)
    # 作业 00:30 开始运行 2 小时（跨越段1、阴影 01:00-02:00、段2），恒定 100W。
    plan = make_plan(
        client,
        [job("infer-a", "ai-infer-a", "2026-10-01T00:30:00+00:00", [flat(0, 7200, 100)])],
    )
    evaluation = client.post(f"/api/power/plans/{plan['id']}/evaluate", json={"actor": "pm"}).json()
    assert evaluation["decision"] == "accepted"
    assert evaluation["total_energy_wh"] == 200
    demands = {item["segment_id"]: item["demand_wh"] for item in evaluation["result"]["segments"]}
    assert sorted(demands.values()) == [50, 50]
    assert evaluation["result"]["eclipse"]["demand_wh"] == 100
    assert evaluation["result"]["sunlit_energy_wh"] == 100
