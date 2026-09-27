from __future__ import annotations

PAYLOAD = {"code": "ocr-cam", "name": "光学相机", "baseline_power_w": 200.0, "description": "对地观测相机"}

SUN_WINDOWS = [
    {"start_at": "2026-10-01T00:00:00+00:00", "end_at": "2026-10-01T01:00:00+00:00"},
    {"start_at": "2026-10-01T02:00:00+00:00", "end_at": "2026-10-01T03:00:00+00:00"},
]


def setup_environment(client) -> None:
    created = client.post("/api/power/payloads?actor=pm", json=PAYLOAD)
    assert created.status_code == 201, created.text
    for window in SUN_WINDOWS:
        response = client.post("/api/power/sun-windows?actor=pm", json=window)
        assert response.status_code == 201, response.text


def job_payload(code: str, *, power_w: float = 700.0, start: str = "2026-10-01T00:00:00+00:00", duration_s: int = 1800, priority: int = 50):
    return {
        "code": code,
        "name": f"作业-{code}",
        "payload_code": "ocr-cam",
        "priority": priority,
        "scheduled_start_at": start,
        "phases": [{"power_w": power_w, "duration_s": duration_s}],
    }


def submit_job(client, code: str, **kwargs) -> dict:
    response = client.post("/api/power/jobs?actor=researcher", json=job_payload(code, **kwargs))
    assert response.status_code == 201, response.text
    return response.json()


def test_register_payload_windows_and_duplicate_conflict(client):
    setup_environment(client)
    duplicate = client.post("/api/power/payloads?actor=pm", json=PAYLOAD)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "conflict"
    overlap = client.post(
        "/api/power/sun-windows?actor=pm",
        json={"start_at": "2026-10-01T00:30:00+00:00", "end_at": "2026-10-01T01:30:00+00:00"},
    )
    assert overlap.status_code == 409
    assert "existing_window_id" in overlap.json()["error"]["context"]


def test_job_evaluation_peak_and_rejection_reasons(client):
    setup_environment(client)
    submit_job(client, "job-feasible", power_w=700)
    submit_job(client, "job-heavy", power_w=900)
    ok = client.post("/api/power/jobs/job-feasible/evaluations", json={"actor": "pm"})
    assert ok.status_code == 201, ok.text
    assert ok.json()["result"]["feasible"] is True
    assert ok.json()["result"]["summary"]["peak_w"] == 900
    bad = client.post("/api/power/jobs/job-heavy/evaluations", json={"actor": "pm"})
    assert bad.status_code == 201
    result = bad.json()["result"]
    assert result["feasible"] is False
    assert result["violations"][0]["type"] == "power_exceeded"
    assert "job-heavy" in result["violations"][0]["reason"]
    assert result["jobs"][0]["executable"] is False
    assert result["jobs"][0]["blocking_reasons"]


def test_budget_adjustment_recheck_preserves_original_application(client):
    setup_environment(client)
    job = submit_job(client, "job-a", power_w=700)
    assert job["current_revision"] == 1
    first = client.post("/api/power/jobs/job-a/evaluations", json={"actor": "pm"})
    assert first.json()["result"]["feasible"] is True
    adjusted = client.post(
        "/api/power/budget/revisions?actor=director",
        json={"sunlit_power_w": 800, "eclipse_power_w": 300, "reason": "太阳翼老化降额", "expected_revision": 1},
    )
    assert adjusted.status_code == 201, adjusted.text
    assert adjusted.json()["revision"] == 2
    rechecked = client.post("/api/power/recheck/job/job-a", json={"actor": "pm", "budget_revision": 2})
    assert rechecked.status_code == 201, rechecked.text
    body = rechecked.json()
    assert body["evaluation"]["scope"] == "recheck"
    assert body["result"]["feasible"] is False
    assert body["evaluation"]["budget_revision"] == 2
    # 原始申请不被篡改
    detail = client.get("/api/power/jobs/job-a").json()
    assert detail["phases"] == [{"power_w": 700.0, "duration_s": 1800}]
    assert detail["current_revision"] == 1
    revisions = client.get("/api/power/jobs/job-a/revisions").json()["items"]
    assert len(revisions) == 1 and revisions[0]["change_note"] == "原始申请"
    # 仍可按旧预算版本复算
    legacy = client.post("/api/power/recheck/job/job-a", json={"actor": "pm", "budget_revision": 1})
    assert legacy.json()["result"]["feasible"] is True


def test_stale_budget_and_payload_concurrent_updates_are_rejected(client):
    setup_environment(client)
    stale = client.post(
        "/api/power/budget/revisions?actor=director",
        json={"sunlit_power_w": 900, "eclipse_power_w": 300, "reason": "第一次调整", "expected_revision": 1},
    )
    assert stale.status_code == 201
    conflict = client.post(
        "/api/power/budget/revisions?actor=director",
        json={"sunlit_power_w": 850, "eclipse_power_w": 300, "reason": "基于过期版本的调整", "expected_revision": 1},
    )
    assert conflict.status_code == 409
    assert conflict.json()["error"]["context"]["current_revision"] == 2
    payload_conflict = client.put(
        "/api/power/payloads/ocr-cam?actor=pm",
        json={"baseline_power_w": 210.0, "expected_version": 99},
    )
    assert payload_conflict.status_code == 409


def test_job_amendment_conflict_and_revision_comparison(client):
    setup_environment(client)
    submit_job(client, "job-x")
    amended = client.post(
        "/api/power/jobs/job-x/amendments?actor=researcher",
        json={"phases": [{"power_w": 600, "duration_s": 900}], "change_note": "缩短推理时长", "expected_revision": 1},
    )
    assert amended.status_code == 201, amended.text
    assert amended.json()["current_revision"] == 2
    stale = client.post(
        "/api/power/jobs/job-x/amendments?actor=researcher",
        json={"priority": 10, "change_note": "过期修订", "expected_revision": 1},
    )
    assert stale.status_code == 409
    comparison = client.get("/api/power/jobs/job-x/revisions/compare?from_revision=1&to_revision=2")
    assert comparison.status_code == 200
    diff = comparison.json()["diff"]
    assert "phases" in diff["changed_fields"]
    assert diff["duration_s"] == {"from": 1800, "to": 900, "delta": -900}
    assert diff["peak_w"] == {"from": 700.0, "to": 600.0}


def test_plan_draft_conflict_publish_rejection_traceability_and_withdraw(client):
    setup_environment(client)
    submit_job(client, "plan-job-1", power_w=700)
    submit_job(client, "plan-job-2", power_w=300, start="2026-10-01T00:30:00+00:00")
    created = client.post("/api/power/plans?actor=pm", json={"name": "发射前推理批次", "job_codes": ["plan-job-1", "plan-job-2"]})
    assert created.status_code == 201, created.text
    plan_id = created.json()["id"]
    # 草案修改：过期版本冲突
    stale_update = client.put(f"/api/power/plans/{plan_id}?actor=pm", json={"name": "新名称", "expected_version": 99})
    assert stale_update.status_code == 409
    updated = client.put(f"/api/power/plans/{plan_id}?actor=pm", json={"job_codes": ["plan-job-1"], "expected_version": 1, "note": "移除冲突作业"})
    assert updated.status_code == 200
    assert updated.json()["current_version"] == 2
    comparison = client.get(f"/api/power/plans/{plan_id}/versions/compare?from_version=1&to_version=2").json()
    assert comparison["diff"]["jobs_removed"] == ["plan-job-2"]
    # 加入超额作业使计划不可行
    submit_job(client, "plan-job-3", power_w=900)
    client.put(f"/api/power/plans/{plan_id}?actor=pm", json={"job_codes": ["plan-job-1", "plan-job-3"], "expected_version": 2})
    rejected = client.post(
        f"/api/power/plans/{plan_id}/publish",
        json={"actor": "director", "reason": "申请发射窗口", "expected_version": 3},
    )
    assert rejected.status_code == 409, rejected.text
    error_context = rejected.json()["error"]["context"]
    evaluation_id = error_context["evaluation_id"]
    assert error_context["rejection_reasons"]
    # 计划仍为草案，评估快照与公式版本可被管理员查询
    assert client.get(f"/api/power/plans/{plan_id}").json()["status"] == "draft"
    evaluation = client.get(f"/api/power/evaluations/{evaluation_id}")
    assert evaluation.status_code == 200
    body = evaluation.json()
    assert body["feasible"] is False
    assert body["formula_version"].startswith("satellite-power-budget/")
    assert body["snapshot"]["settings"]["sunlit_power_w"] == 1000
    assert {job["code"] for job in body["snapshot"]["jobs"]} == {"plan-job-1", "plan-job-3"}
    assert body["snapshot_digest"]
    # 恢复可行内容后发布
    client.put(f"/api/power/plans/{plan_id}?actor=pm", json={"job_codes": ["plan-job-1"], "expected_version": 3})
    published = client.post(
        f"/api/power/plans/{plan_id}/publish",
        json={"actor": "director", "reason": "评估通过", "expected_version": 4},
    )
    assert published.status_code == 200, published.text
    assert published.json()["plan"]["status"] == "published"
    # 重复发布明确冲突而非静默覆盖
    duplicate = client.post(
        f"/api/power/plans/{plan_id}/publish",
        json={"actor": "director", "reason": "再次提交", "expected_version": 4},
    )
    assert duplicate.status_code == 409
    # 撤回后不能再发布
    withdrawn = client.post(
        f"/api/power/plans/{plan_id}/withdraw",
        json={"actor": "director", "reason": "窗口推迟", "expected_version": 4},
    )
    assert withdrawn.status_code == 200 and withdrawn.json()["status"] == "withdrawn"
    republish = client.post(
        f"/api/power/plans/{plan_id}/publish",
        json={"actor": "director", "reason": "重新申请", "expected_version": 4},
    )
    assert republish.status_code == 409
    detail = client.get(f"/api/power/plans/{plan_id}").json()
    assert [item["action"] for item in detail["publications"]] == ["publish", "withdraw"]
    # 已发布计划不能修改
    locked = client.put(f"/api/power/plans/{plan_id}?actor=pm", json={"name": "x", "expected_version": 4})
    assert locked.status_code == 409


def test_evaluation_listing_and_budget_revisions_queryable(client):
    setup_environment(client)
    submit_job(client, "job-list")
    client.post("/api/power/jobs/job-list/evaluations", json={"actor": "pm"})
    listing = client.get("/api/power/evaluations?scope=job&subject_key=job-list")
    assert listing.status_code == 200
    items = listing.json()["items"]
    assert len(items) == 1 and items[0]["subject_key"] == "job-list"
    assert "snapshot_digest" in items[0] and "formula_version" in items[0]
    revisions = client.get("/api/power/budget/revisions").json()["items"]
    assert revisions[0]["revision"] == 1
    assert client.get("/api/power/budget").json()["sunlit_power_w"] == 1000


def test_derating_registration_changes_execution_order(client):
    setup_environment(client)
    client.post(
        "/api/power/deratings?actor=director",
        json={
            "start_at": "2026-10-01T00:00:00+00:00",
            "end_at": "2026-10-01T00:30:00+00:00",
            "available_power_w": 500,
            "reason": "热控临时降额",
        },
    )
    submit_job(client, "derated-job", power_w=700)
    result = client.post("/api/power/jobs/derated-job/evaluations", json={"actor": "pm"}).json()["result"]
    assert result["feasible"] is False
    assert result["violations"][0]["regime"] == "derated"
