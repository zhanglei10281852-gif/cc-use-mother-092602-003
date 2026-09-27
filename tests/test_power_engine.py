from __future__ import annotations

from app.power import engine


def make_snapshot(jobs, *, sunlit=1000, eclipse=300, battery=800, dod=0.8, windows=None, deratings=None, baseline=200):
    return {
        "settings": {
            "revision": 1,
            "sunlit_power_w": sunlit,
            "eclipse_power_w": eclipse,
            "battery_capacity_wh": battery,
            "max_depth_of_discharge": dod,
        },
        "payloads": [{"code": "ocr", "name": "光学相机", "baseline_power_w": baseline}],
        "sun_windows": windows or [],
        "deratings": deratings or [],
        "jobs": jobs,
    }


def job(code, *, start, phases, priority=50, payload="ocr"):
    return {"code": code, "name": code, "payload_code": payload, "priority": priority, "scheduled_start_at": start, "phases": phases}


def test_feasible_plan_across_two_sun_windows():
    snapshot = make_snapshot(
        windows=[
            {"start_at": "2026-10-01T00:00:00+00:00", "end_at": "2026-10-01T00:30:00+00:00", "power_w": None},
            {"start_at": "2026-10-01T01:30:00+00:00", "end_at": "2026-10-01T02:00:00+00:00", "power_w": None},
        ],
        jobs=[
            job("j1", start="2026-10-01T00:00:00+00:00", phases=[{"power_w": 700, "duration_s": 1800}]),
            job("j2", start="2026-10-01T01:30:00+00:00", phases=[{"power_w": 500, "duration_s": 1800}]),
        ],
    )
    result = engine.evaluate(snapshot)
    assert result["feasible"] is True
    assert result["summary"]["peak_w"] == 900
    # 作业均在各自日照段内执行：(200+700)*0.5h + (200+500)*0.5h
    assert result["summary"]["sunlit_energy_wh"] == 800
    # 两个日照段之间基线载荷仍由电池供电：200W * 1h
    assert result["summary"]["eclipse_energy_wh"] == 200
    assert result["execution_order"] == ["j1", "j2"]


def test_peak_violation_names_primary_job_and_blocks_only_offender():
    snapshot = make_snapshot(
        windows=[{"start_at": "2026-10-01T00:00:00+00:00", "end_at": "2026-10-01T01:00:00+00:00", "power_w": None}],
        jobs=[
            job("low", start="2026-10-01T00:00:00+00:00", phases=[{"power_w": 100, "duration_s": 3600}], priority=90),
            job("high", start="2026-10-01T00:00:00+00:00", phases=[{"power_w": 750, "duration_s": 3600}]),
        ],
    )
    result = engine.evaluate(snapshot)
    assert result["feasible"] is False
    violation = result["violations"][0]
    assert violation["type"] == "power_exceeded"
    assert violation["primary_job"] == "high"
    by_code = {item["code"]: item for item in result["jobs"]}
    assert by_code["high"]["executable"] is False
    assert by_code["low"]["executable"] is True
    assert result["execution_order"] == ["low"]


def test_derating_takes_precedence_over_sunlit_cap():
    snapshot = make_snapshot(
        windows=[{"start_at": "2026-10-01T00:00:00+00:00", "end_at": "2026-10-01T02:00:00+00:00", "power_w": None}],
        deratings=[{"start_at": "2026-10-01T00:00:00+00:00", "end_at": "2026-10-01T01:00:00+00:00", "available_power_w": 600, "reason": "热控临时降额"}],
        jobs=[job("j1", start="2026-10-01T00:00:00+00:00", phases=[{"power_w": 500, "duration_s": 3600}, {"power_w": 500, "duration_s": 3600}])],
    )
    result = engine.evaluate(snapshot)
    assert result["feasible"] is False
    assert result["violations"][0]["regime"] == "derated"
    assert result["violations"][0]["cap_w"] == 600


def test_eclipse_battery_energy_limit():
    snapshot = make_snapshot(
        baseline=100,
        battery=500,
        dod=0.8,
        jobs=[job("j1", start="2026-10-01T00:00:00+00:00", phases=[{"power_w": 200, "duration_s": 7200}])],
    )
    result = engine.evaluate(snapshot)
    types = {item["type"] for item in result["violations"]}
    assert "eclipse_energy_exceeded" in types
    violation = next(item for item in result["violations"] if item["type"] == "eclipse_energy_exceeded")
    assert violation["energy_wh"] == 600
    assert violation["usable_capacity_wh"] == 400


def test_multiphase_curve_peak_and_total_energy():
    snapshot = make_snapshot(
        windows=[{"start_at": "2026-10-01T00:00:00+00:00", "end_at": "2026-10-01T02:00:00+00:00", "power_w": None}],
        jobs=[job("j1", start="2026-10-01T00:00:00+00:00", phases=[{"power_w": 300, "duration_s": 1800}, {"power_w": 600, "duration_s": 1800}])],
    )
    result = engine.evaluate(snapshot)
    assert result["feasible"] is True
    assert result["summary"]["peak_w"] == 800
    # 时间轴覆盖作业跨度 1h：(200+300)*0.5h + (200+600)*0.5h
    assert result["summary"]["total_energy_wh"] == 650
    job_result = result["jobs"][0]
    assert job_result["peak_w"] == 600
    assert job_result["energy_wh"] == 450
