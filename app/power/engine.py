"""卫星功率预算计算引擎。

引擎是纯函数：输入为可 JSON 序列化的输入快照，输出为峰值、累计能耗、
违规区间和拒绝原因。任何一次评估都把快照与公式版本一并持久化，
预算调整后重新评估只会产生新的评估记录，不会改动作业申请原文。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# 公式版本：当峰值积分、降额叠加或地影能量判定规则发生变化时必须抬升版本，
# 历史评估记录继续保留其当时使用的版本号。
FORMULA_VERSION = "satellite-power-budget/v1.0.0"

EPS_W = 1e-6
EPS_WH = 1e-9

REGIME_LABELS = {"sunlit": "日照", "eclipse": "地影", "derated": "临时降额"}


@dataclass(frozen=True)
class Phase:
    power_w: float
    duration_s: int


def parse_datetime(value: str | datetime) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"时间格式不合法：{value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds")


def canonical_digest(value: Any) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _round3(value: float) -> float:
    return round(float(value), 3)


def _build_jobs(raw_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    jobs: list[dict[str, Any]] = []
    for raw in raw_jobs:
        start = parse_datetime(raw["scheduled_start_at"])
        phases = [(float(item["power_w"]), int(item["duration_s"])) for item in raw["phases"]]
        intervals: list[tuple[datetime, datetime, float]] = []
        cursor = start
        for power_w, duration_s in phases:
            end = cursor + timedelta(seconds=duration_s)
            intervals.append((cursor, end, power_w))
            cursor = end
        jobs.append(
            {
                "code": raw["code"],
                "name": raw.get("name", raw["code"]),
                "payload_code": raw.get("payload_code", ""),
                "priority": int(raw.get("priority", 50)),
                "start": start,
                "end": cursor,
                "intervals": intervals,
            }
        )
    return jobs


def _availability(t: datetime, settings: dict[str, Any], windows: list[tuple[datetime, datetime, float | None]], deratings: list[tuple[datetime, datetime, float]]) -> tuple[float, str, bool]:
    """返回时刻 t 的可用功率上限、时段标签和是否处于日照段。"""
    cap = float(settings["eclipse_power_w"])
    regime = "eclipse"
    sunlit = False
    for start, end, power_w in windows:
        if start <= t < end:
            cap = float(settings["sunlit_power_w"]) if power_w is None else float(power_w)
            regime = "sunlit"
            sunlit = True
            break
    derated = min((power for start, end, power in deratings if start <= t < end), default=None)
    if derated is not None and derated < cap - EPS_W:
        cap = derated
        regime = "derated"
    return cap, regime, sunlit


def _empty_result(static_w: float) -> dict[str, Any]:
    return {
        "formula_version": FORMULA_VERSION,
        "feasible": True,
        "summary": {
            "horizon_start": None,
            "horizon_end": None,
            "duration_s": 0,
            "static_power_w": _round3(static_w),
            "peak_w": _round3(static_w),
            "peak_at": None,
            "total_energy_wh": 0.0,
            "sunlit_energy_wh": 0.0,
            "eclipse_energy_wh": 0.0,
            "job_count": 0,
            "violation_count": 0,
        },
        "violations": [],
        "rejection_reasons": [],
        "jobs": [],
        "execution_order": [],
        "segments": [],
    }


def evaluate(snapshot: dict[str, Any]) -> dict[str, Any]:
    """对输入快照执行功率预算评估。

    快照结构：
      settings: sunlit_power_w / eclipse_power_w / battery_capacity_wh / max_depth_of_discharge
      payloads: 活动载荷（基线功耗恒定计入）
      sun_windows: 日照段 [start_at, end_at)，power_w 可空表示沿用日照上限
      deratings: 临时降额段，与日照/地影上限取最小值
      jobs: 作业申请，phases 为顺序连接的恒定功耗曲线
    """
    settings = snapshot["settings"]
    payloads = snapshot.get("payloads", [])
    static_w = float(sum(float(item["baseline_power_w"]) for item in payloads))
    windows = [
        (parse_datetime(item["start_at"]), parse_datetime(item["end_at"]), None if item.get("power_w") is None else float(item["power_w"]))
        for item in snapshot.get("sun_windows", [])
    ]
    deratings = [
        (parse_datetime(item["start_at"]), parse_datetime(item["end_at"]), float(item["available_power_w"]))
        for item in snapshot.get("deratings", [])
    ]
    jobs = _build_jobs(snapshot.get("jobs", []))
    if not jobs:
        return _empty_result(static_w)

    windows.sort(key=lambda item: (item[0], item[1]))
    deratings.sort(key=lambda item: (item[0], item[1]))
    horizon_start = min(job["start"] for job in jobs)
    horizon_end = max(job["end"] for job in jobs)

    breaks = {horizon_start, horizon_end}
    for start, end in [(item[0], item[1]) for item in windows] + [(item[0], item[1]) for item in deratings]:
        if end > horizon_start and start < horizon_end:
            breaks.add(max(start, horizon_start))
            breaks.add(min(end, horizon_end))
    for job in jobs:
        for start, end, _ in job["intervals"]:
            if start > horizon_start and start < horizon_end:
                breaks.add(start)
            if end > horizon_start and end < horizon_end:
                breaks.add(end)
    timeline = sorted(breaks)

    job_peak = {job["code"]: max(power for _, _, power in job["intervals"]) for job in jobs}
    job_energy: dict[str, float] = {job["code"]: 0.0 for job in jobs}
    blocked: dict[str, list[str]] = {job["code"]: [] for job in jobs}

    segments: list[dict[str, Any]] = []
    total_energy = 0.0
    sunlit_energy = 0.0
    eclipse_energy = 0.0
    peak_w = static_w
    peak_at = horizon_start

    for index in range(len(timeline) - 1):
        t0, t1 = timeline[index], timeline[index + 1]
        if t1 <= t0:
            continue
        duration_s = (t1 - t0).total_seconds()
        midpoint = t0 + (t1 - t0) / 2
        cap_w, regime, sunlit = _availability(midpoint, settings, windows, deratings)
        active: list[tuple[dict[str, Any], float]] = []
        for job in jobs:
            for start, end, power_w in job["intervals"]:
                if start <= midpoint < end:
                    active.append((job, power_w))
                    break
        load_w = static_w + sum(power_w for _, power_w in active)
        hours = duration_s / 3600.0
        energy = load_w * hours
        total_energy += energy
        if sunlit:
            sunlit_energy += energy
        else:
            eclipse_energy += energy
        contributions = [(job["code"], power_w * hours) for job, power_w in active]
        for code, job_energy_value in contributions:
            job_energy[code] += job_energy_value
        if load_w > peak_w + EPS_W:
            peak_w, peak_at = load_w, t0
        segments.append(
            {
                "start": t0,
                "end": t1,
                "duration_s": int(duration_s),
                "cap_w": cap_w,
                "load_w": load_w,
                "regime": regime,
                "sunlit": sunlit,
                "active": [(job["code"], power_w) for job, power_w in active],
                "contributions": contributions,
                "violation": load_w > cap_w + EPS_W,
            }
        )

    violations: list[dict[str, Any]] = []
    rejection_reasons: list[str] = []

    def remember_reason(code: str | None, reason: str) -> None:
        if reason not in rejection_reasons:
            rejection_reasons.append(reason)
        if code is not None and reason not in blocked[code]:
            blocked[code].append(reason)

    # 瞬时功率违规，合并相邻且上限、主因作业相同的区间。
    run: dict[str, Any] | None = None
    for segment in segments:
        if not segment["violation"]:
            run = None
            continue
        if segment["active"]:
            # 主因作业：占用功率最大，其次优先级更高，再以编码保证确定性。
            active_map = dict(segment["active"])
            primary_code = sorted(
                active_map,
                key=lambda code: (-active_map[code], -_priority_of(jobs, code), code),
            )[0]
            kind = "power_exceeded"
        else:
            primary_code, primary_power = None, None
            kind = "static_load_exceeded"
        same = (
            run is not None
            and run["type"] == kind
            and run["end"] == segment["start"]
            and abs(run["cap_w"] - segment["cap_w"]) <= EPS_W
            and run["regime"] == segment["regime"]
            and run["primary"] == primary_code
        )
        if not same:
            run = {
                "type": kind,
                "start": segment["start"],
                "end": segment["end"],
                "cap_w": segment["cap_w"],
                "regime": segment["regime"],
                "primary": primary_code,
                "peak_load": segment["load_w"],
                "offenders": {},
            }
            violations.append(run)
        run["end"] = segment["end"]
        run["peak_load"] = max(run["peak_load"], segment["load_w"])
        for code, power_w in segment["active"]:
            run["offenders"][code] = max(run["offenders"].get(code, 0.0), power_w)

    for item in violations:
        label = REGIME_LABELS[item["regime"]]
        excess = item["peak_load"] - item["cap_w"]
        if item["type"] == "static_load_exceeded":
            reason = f"载荷基线功耗 {_round3(item['peak_load'])}W 超过{label}时段功率上限 {_round3(item['cap_w'])}W"
            primary = None
        else:
            reason = (
                f"{label}时段总负载峰值 {_round3(item['peak_load'])}W 超过功率上限 "
                f"{_round3(item['cap_w'])}W（超出 {_round3(excess)}W），主要占用作业为 {item['primary']}"
            )
            primary = item["primary"]
        remember_reason(primary, reason)
        item["reason"] = reason
        item["excess_w"] = _round3(excess)

    # 地影（含叠加在地影上的降额）放电能量不得超过电池可用容量。
    capacity = float(settings.get("battery_capacity_wh", 0) or 0)
    usable_capacity = capacity * float(settings.get("max_depth_of_discharge", 0.0) or 0.0)

    def close_eclipse_gap(gap: dict[str, Any]) -> None:
        if gap["energy_wh"] <= usable_capacity + EPS_WH:
            return
        primary = sorted(gap["jobs"], key=lambda code: (-gap["jobs"][code], -_priority_of(jobs, code), code))[0] if gap["jobs"] else None
        excess_energy = gap["energy_wh"] - usable_capacity
        reason = (
            f"连续地影段 {iso(gap['start'])} 至 {iso(gap['end'])} 累计放电 "
            f"{_round3(gap['energy_wh'])}Wh 超过电池可用容量 {_round3(usable_capacity)}Wh"
            f"（超出 {_round3(excess_energy)}Wh），主要耗能作业为 {primary}"
        )
        violations.append(
            {
                "type": "eclipse_energy_exceeded",
                "start_at": iso(gap["start"]),
                "end_at": iso(gap["end"]),
                "duration_s": int((gap["end"] - gap["start"]).total_seconds()),
                "energy_wh": _round3(gap["energy_wh"]),
                "usable_capacity_wh": _round3(usable_capacity),
                "excess_wh": _round3(excess_energy),
                "regime": "eclipse",
                "primary": primary,
                "offenders": dict(gap["jobs"]),
                "reason": reason,
            }
        )
        remember_reason(primary, reason)

    if usable_capacity > EPS_WH:
        gap: dict[str, Any] | None = None
        for segment in segments:
            if segment["sunlit"] or (gap is not None and segment["start"] != gap["end"]):
                if gap is not None:
                    close_eclipse_gap(gap)
                    gap = None
            if segment["sunlit"]:
                continue
            if gap is None:
                gap = {"start": segment["start"], "end": segment["end"], "energy_wh": 0.0, "jobs": {}}
            gap["end"] = segment["end"]
            gap["energy_wh"] += segment["load_w"] * segment["duration_s"] / 3600.0
            for code, value in segment["contributions"]:
                gap["jobs"][code] = gap["jobs"].get(code, 0.0) + value
        if gap is not None:
            close_eclipse_gap(gap)

    power_violations = [item for item in violations if item["type"] in {"power_exceeded", "static_load_exceeded"}]
    rendered_violations: list[dict[str, Any]] = []
    for item in power_violations:
        rendered_violations.append(
            {
                "type": item["type"],
                "start_at": iso(item["start"]),
                "end_at": iso(item["end"]),
                "duration_s": int((item["end"] - item["start"]).total_seconds()),
                "regime": item["regime"],
                "load_w": _round3(item["peak_load"]),
                "cap_w": _round3(item["cap_w"]),
                "excess_w": item["excess_w"],
                "primary_job": item["primary"],
                "offending_jobs": [
                    {"code": code, "power_w": _round3(power), "primary": code == item["primary"]}
                    for code, power in sorted(item["offenders"].items(), key=lambda entry: (-entry[1], entry[0]))
                ],
                "reason": item["reason"],
            }
        )
    rendered_violations.extend(
        dict(item)
        | {"offending_jobs": [
            {"code": code, "energy_wh": _round3(energy), "primary": code == item["primary"]}
            for code, energy in sorted(item["offenders"].items(), key=lambda entry: (-entry[1], entry[0]))
        ]}
        for item in violations
        if item["type"] == "eclipse_energy_exceeded"
    )

    job_results = []
    for job in sorted(jobs, key=lambda item: (item["start"], -item["priority"], item["code"])):
        code = job["code"]
        job_results.append(
            {
                "code": code,
                "name": job["name"],
                "payload_code": job["payload_code"],
                "priority": job["priority"],
                "start_at": iso(job["start"]),
                "end_at": iso(job["end"]),
                "duration_s": int((job["end"] - job["start"]).total_seconds()),
                "peak_w": _round3(job_peak[code]),
                "energy_wh": _round3(job_energy[code]),
                "executable": not blocked[code],
                "blocking_reasons": list(blocked[code]),
            }
        )
    execution_order = [item["code"] for item in job_results if item["executable"]]

    return {
        "formula_version": FORMULA_VERSION,
        "feasible": not rendered_violations,
        "summary": {
            "horizon_start": iso(horizon_start),
            "horizon_end": iso(horizon_end),
            "duration_s": int((horizon_end - horizon_start).total_seconds()),
            "static_power_w": _round3(static_w),
            "peak_w": _round3(peak_w),
            "peak_at": iso(peak_at),
            "total_energy_wh": _round3(total_energy),
            "sunlit_energy_wh": _round3(sunlit_energy),
            "eclipse_energy_wh": _round3(eclipse_energy),
            "job_count": len(jobs),
            "violation_count": len(rendered_violations),
        },
        "violations": rendered_violations,
        "rejection_reasons": list(rejection_reasons),
        "jobs": job_results,
        "execution_order": execution_order,
        "segments": [
            {
                "start_at": iso(item["start"]),
                "end_at": iso(item["end"]),
                "duration_s": item["duration_s"],
                "regime": item["regime"],
                "cap_w": _round3(item["cap_w"]),
                "load_w": _round3(item["load_w"]),
                "violation": item["violation"],
            }
            for item in segments
        ],
    }


def _priority_of(jobs: list[dict[str, Any]], code: str) -> int:
    return next(job["priority"] for job in jobs if job["code"] == code)
