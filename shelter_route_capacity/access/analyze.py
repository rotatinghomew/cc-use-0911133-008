"""逐方案计算：可达上界、容量竞争安置、不可行原因、方案间受益转移。

输出结构同时供 JSON 报告与 Markdown 渲染使用；所有结论都来自同一次
精确求解，保证"纯路网可达上界"与"容量竞争结果"分开陈述。
"""
from __future__ import annotations

import math
from typing import Any, Mapping

from shelter_route_capacity.access.model import (
    CASE_KEYS,
    Dataset,
    Household,
    Instance,
    apply_scenario,
    base_instance,
    load_dataset,
)
from shelter_route_capacity.access.network import closed_pairs, shortest_minutes
from shelter_route_capacity.access.optimize import Candidate, Dimension, solve

EPS = 1e-9

REASON_TEXT = {
    "no_route": "时限内无可达安置点",
    "no_facility": "可达安置点不提供所需设施",
    "facility_absolute": "设施名额总量装不下该户",
    "beds_absolute": "床位总量装不下该户",
    "facility_contended": "设施名额在竞争中被占完",
    "beds_contended": "床位在竞争中被占完",
    "capacity_contended": "床位与设施在竞争中同时受限",
}

METHOD_NOTES = [
    "家庭不可拆分：一户要么整体安置，要么不安置。",
    "必要设施是硬约束：不能用出行更短或偏好分抵扣；要求设施的家庭按家庭人数占用设施名额。",
    "床位与设施名额多村共享：同一张床只算一次，不在各村覆盖率中重复计数。",
    "封闭区间左闭右开：start <= 分析时刻 < end 时该路段双向封闭。",
    "优化目标：先最大化安置人数，再最小化总出行分钟。",
    "规范解只是最优解集合中按固定规则（家庭与安置点按编号排序）取出的一个代表，不依赖输入列表顺序，也不代表现场优先级。",
    "范围列在（人数最多、出行最少）的最优解集合内统计各村/群体安置人数的最小—最大；出行范围另给出只追求人数最多时出行分钟的最大值。",
    "本模块为离线方案比较：不输出最近距离表，不接管现场调度；既有单笔容量预留流程（shelter_route_capacity.allocation.cli）行为不变。",
]


def _num(value: float) -> float | int:
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _ratio(part: float, whole: float) -> float:
    return round(part / whole, 4) if whole else 0.0


def analyze_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """分析整份批量资料，返回可直接 JSON 序列化的报告字典。"""
    dataset = load_dataset(payload)
    baseline_name = next((s.name for s in dataset.scenarios if s.name == "current"), dataset.scenarios[0].name)
    cases = []
    for key in CASE_KEYS:
        households = dataset.cases.get(key)
        if households:
            cases.append(_analyze_case(dataset, key, households, baseline_name))
    return {
        "schema_version": 1,
        "analysis_time": dataset.analysis_time.isoformat(),
        "maximum_minutes": _num(dataset.maximum_minutes),
        "baseline_scenario": baseline_name,
        "closures_active": _closure_rows(dataset.closures, dataset),
        "cases": cases,
        "method_notes": list(METHOD_NOTES),
    }


def _closure_rows(closures, timing) -> list[dict[str, Any]]:
    return [
        {"edge": [c.a, c.b], "start": c.start.isoformat(), "end": c.end.isoformat()}
        for c in closures
        if c.active_at(timing.analysis_time)
    ]


def _analyze_case(dataset: Dataset, case_key: str, households: list[Household], baseline_name: str) -> dict[str, Any]:
    results = []
    for scenario in dataset.scenarios:
        instance, summaries = apply_scenario(base_instance(dataset, households), scenario)
        results.append(_analyze_scenario(dataset, instance, scenario.name, summaries, baseline_name))
    baseline = next(item for item in results if item["is_baseline"])
    for item in results:
        if not item["is_baseline"]:
            item["comparison"] = _compare(baseline, item)
            item["narrative"].extend(item["comparison"]["narrative"])
    people = sum(h.size for h in households)
    return {
        "case": case_key,
        "households": len(households),
        "people": people,
        "villages": sorted({h.village for h in households}),
        "groups": sorted({h.group for h in households}),
        "scenarios": results,
    }


def _analyze_scenario(
    dataset: Dataset,
    instance: Instance,
    name: str,
    summaries: list[dict[str, Any]],
    baseline_name: str,
) -> dict[str, Any]:
    households = sorted(instance.households, key=lambda h: h.id)
    shelters = sorted(instance.shelters, key=lambda s: s.id)
    facilities = instance.facilities
    villages = sorted({h.village for h in households})
    groups = sorted({h.group for h in households})

    reach = _reachability(instance, households, shelters)
    candidates = _candidates(households, shelters, reach)
    solution = solve(households, candidates, shelters, facilities, keep_back=True, keep_count=True)
    travel_max = solve(households, candidates, shelters, facilities, travel_sign=+1)

    village_ranges = _dimension_ranges(households, candidates, shelters, facilities, villages, lambda h: h.village)
    group_ranges = _dimension_ranges(households, candidates, shelters, facilities, groups, lambda h: h.group)

    usage = _usage(shelters, households, solution.assignment, facilities)
    assignments = _assignment_rows(households, shelters, candidates, solution.assignment)
    unserved = _unserved_rows(instance, households, shelters, facilities, reach, usage, solution.assignment)
    by_village = _coverage_rows(households, solution.assignment, villages, lambda h: h.village, village_ranges)
    by_group = _coverage_rows(households, solution.assignment, groups, lambda h: h.group, group_ranges)

    bounds = _bounds(households, reach, candidates)
    result = {
        "name": name,
        "is_baseline": name == baseline_name,
        "changes": summaries,
        "closures_active": _closure_rows(instance.closures, instance),
        "bounds": bounds,
        "allocation": {
            "served_people": solution.served_people,
            "served_households": sum(1 for s in solution.assignment.values() if s is not None),
            "unserved_people": sum(h.size for i, h in enumerate(households) if solution.assignment.get(i) is None),
            "total_travel_minutes": _num(solution.travel_minutes),
            "assignments": assignments,
            "by_village": by_village,
            "by_group": by_group,
            "shelter_usage": usage,
        },
        "optima": {
            "count": solution.optimal_count,
            "count_capped": solution.count_capped,
            "travel_minutes_range": [_num(solution.travel_minutes), _num(travel_max.travel_minutes)],
            "note": "规范解只是最优解集合的一个代表；范围列给出最优解集合内各村/群体覆盖与出行负担的变动区间。",
        },
        "weakest": _weakest(by_village, by_group),
        "unserved": unserved,
        "narrative": [],
    }
    result["narrative"] = _fact_lines(instance, households, facilities, reach, usage, unserved, bounds, result)
    return result


def _reachability(instance: Instance, households, shelters) -> list[dict[str, Any]]:
    blocked = closed_pairs(instance.closures, instance.analysis_time)
    rows = []
    for h in households:
        dist = shortest_minutes(instance.edges, blocked, h.village)
        open_dist = shortest_minutes(instance.edges, frozenset(), h.village) if blocked else dist
        reachable = sorted(
            ((s.id, dist[s.id]) for s in shelters if dist.get(s.id, math.inf) <= instance.maximum_minutes + EPS),
            key=lambda item: (item[1], item[0]),
        )
        open_reachable = sorted(
            ((s.id, open_dist[s.id]) for s in shelters if open_dist.get(s.id, math.inf) <= instance.maximum_minutes + EPS),
            key=lambda item: (item[1], item[0]),
        )
        rows.append({"reachable": reachable, "open_reachable": open_reachable})
    return rows


def _candidates(households, shelters, reach) -> list[list[Candidate]]:
    """容量可行候选：可达且即使独占也装得下（床位与所需设施名额都够）。"""
    result = []
    for h, entry in zip(households, reach):
        minutes_by_id = dict(entry["reachable"])
        options = []
        for index, shelter in enumerate(shelters):
            if shelter.id not in minutes_by_id:
                continue
            if shelter.beds < h.size:
                continue
            if any(shelter.slots(f) < h.size for f in h.required):
                continue
            options.append(Candidate(index, minutes_by_id[shelter.id]))
        result.append(options)
    return result


def _dimension_ranges(households, candidates, shelters, facilities, keys, key_of) -> dict[str, list[float]]:
    ranges = {}
    for key in keys:
        members = frozenset(i for i, h in enumerate(households) if key_of(h) == key)
        hi = solve(households, candidates, shelters, facilities,
                   dimensions=[Dimension("d", members, True)])
        lo = solve(households, candidates, shelters, facilities,
                   dimensions=[Dimension("d", members, False)])
        ranges[key] = [_num(lo.dimension_values["d"]), _num(hi.dimension_values["d"])]
    return ranges


def _usage(shelters, households, assignment, facilities) -> dict[str, Any]:
    usage = {
        s.id: {
            "beds": s.beds,
            "beds_used": 0,
            "facilities": {
                f: {"slots": s.slots(f), "used": 0, "by_village": {}} for f in facilities
            },
            "occupants_by_village": {},
            "households": [],
        }
        for s in shelters
    }
    for h_idx, s_idx in assignment.items():
        if s_idx is None:
            continue
        h = households[h_idx]
        entry = usage[shelters[s_idx].id]
        entry["beds_used"] += h.size
        entry["households"].append(h.id)
        entry["occupants_by_village"][h.village] = entry["occupants_by_village"].get(h.village, 0) + h.size
        for f in h.required:
            fu = entry["facilities"][f]
            fu["used"] += h.size
            fu["by_village"][h.village] = fu["by_village"].get(h.village, 0) + h.size
    for entry in usage.values():
        entry["households"].sort()
    return usage


def _assignment_rows(households, shelters, candidates, assignment) -> list[dict[str, Any]]:
    rows = []
    for h_idx, h in enumerate(households):
        s_idx = assignment.get(h_idx)
        minutes = None
        if s_idx is not None:
            minutes = next(c.minutes for c in candidates[h_idx] if c.shelter_index == s_idx)
        rows.append({
            "household": h.id,
            "village": h.village,
            "group": h.group,
            "size": h.size,
            "required": list(h.required),
            "shelter": shelters[s_idx].id if s_idx is not None else None,
            "minutes": _num(minutes) if minutes is not None else None,
        })
    return rows


def _coverage_rows(households, assignment, keys, key_of, ranges) -> dict[str, Any]:
    totals = {key: 0 for key in keys}
    covered = {key: 0 for key in keys}
    for h_idx, h in enumerate(households):
        totals[key_of(h)] += h.size
        if assignment.get(h_idx) is not None:
            covered[key_of(h)] += h.size
    return {
        key: {
            "covered_people": covered[key],
            "total_people": totals[key],
            "coverage": _ratio(covered[key], totals[key]),
            "optimal_range": ranges[key],
        }
        for key in keys
    }


def _weakest(by_village, by_group) -> dict[str, Any]:
    def weakest(rows):
        floor = min(row["coverage"] for row in rows.values())
        return sorted(key for key, row in rows.items() if row["coverage"] == floor)

    return {
        "groups": by_group,
        "weakest_groups": weakest(by_group),
        "villages": by_village,
        "weakest_villages": weakest(by_village),
        "note": "最弱归属基于规范解；若各对象的最优解范围相互重叠，归属可能随最优解选择而变化。",
    }


def _bounds(households, reach, candidates) -> dict[str, Any]:
    network = [i for i, entry in enumerate(reach) if entry["reachable"]]
    facility = [i for i, options in enumerate(candidates) if options]
    return {
        "network_reachable_people": sum(households[i].size for i in network),
        "network_reachable_households": len(network),
        "facility_feasible_people": sum(households[i].size for i in facility),
        "facility_feasible_households": len(facility),
    }


def _unserved_rows(instance, households, shelters, facilities, reach, usage, assignment) -> list[dict[str, Any]]:
    shelters_by_id = {s.id: s for s in shelters}
    rows = []
    for h_idx, h in enumerate(households):
        if assignment.get(h_idx) is not None:
            continue
        entry = reach[h_idx]
        reason, detail, blocks = _classify(instance, h, entry, shelters_by_id, usage)
        rows.append({
            "household": h.id,
            "village": h.village,
            "group": h.group,
            "size": h.size,
            "required": list(h.required),
            "reason": reason,
            "reason_text": REASON_TEXT[reason],
            "detail": detail,
            "blocks": blocks,
        })
    return rows


def _classify(instance, h: Household, entry, shelters_by_id, usage):
    """对照规范解的剩余容量，给出该户无法安置的原因与证据。"""
    reachable = entry["reachable"]
    cap = _num(instance.maximum_minutes)
    if not reachable:
        detail = f"{h.village} 在 {cap} 分钟内没有可达安置点"
        extra = [f"{sid}（{_num(m)} 分钟）" for sid, m in entry["open_reachable"]]
        if extra:
            detail += "；若无路段封闭，本可到 " + "、".join(extra)
        return "no_route", detail, {}

    offering = [sid for sid, _ in reachable
                if all(shelters_by_id[sid].slots(f) > 0 for f in h.required)]
    if h.required and not offering:
        names = "、".join(sid for sid, _ in reachable)
        return "no_facility", f"可达的 {names} 均不提供 {('、'.join(h.required))} 设施", {}

    pool = offering if h.required else [sid for sid, _ in reachable]
    fit_empty = [sid for sid in pool
                 if shelters_by_id[sid].beds >= h.size
                 and all(shelters_by_id[sid].slots(f) >= h.size for f in h.required)]
    if not fit_empty:
        if all(shelters_by_id[sid].beds < h.size for sid in pool):
            top = max(shelters_by_id[sid].beds for sid in pool)
            return "beds_absolute", f"可达安置点床位最多 {top} 张，装不下该户 {h.size} 人", {}
        parts = []
        for f in h.required:
            top = max(shelters_by_id[sid].slots(f) for sid in pool)
            parts.append(f"{f} 名额最多 {top} 个")
        return "facility_absolute", f"可达安置点{'、'.join(parts)}，装不下该户 {h.size} 人", {}

    blocks: dict[str, Any] = {}
    only_beds = True
    only_facility = True
    for sid in fit_empty:
        shelter = shelters_by_id[sid]
        used = usage[sid]
        remaining_beds = shelter.beds - used["beds_used"]
        reasons = []
        if remaining_beds < h.size:
            reasons.append("beds")
        remaining_slots = {}
        for f in h.required:
            fu = used["facilities"][f]
            remaining = shelter.slots(f) - fu["used"]
            remaining_slots[f] = remaining
            if remaining < h.size:
                reasons.append(f)
        if not reasons:
            reasons.append("contended")  # 理论上最优解不会出现，兜底
        blocks[sid] = {
            "remaining_beds": remaining_beds,
            "remaining_facility_slots": remaining_slots,
            "blocked_by": reasons,
            "occupants_by_village": dict(used["occupants_by_village"]),
        }
        if reasons != ["beds"]:
            only_beds = False
        if any(reason == "beds" for reason in reasons):
            only_facility = False
    if only_beds:
        reason = "beds_contended"
    elif only_facility:
        reason = "facility_contended"
    else:
        reason = "capacity_contended"
    detail = "；".join(
        f"{sid} 剩余床位 {block['remaining_beds']}、"
        + "、".join(f"{f} 名额 {n}" for f, n in block["remaining_facility_slots"].items())
        + f"（{_village_text(block['occupants_by_village'])}占用）"
        for sid, block in blocks.items()
    )
    return reason, detail, blocks


def _village_text(by_village: Mapping[str, int]) -> str:
    if not by_village:
        return "无人"
    return "、".join(f"{village} {n} 人" for village, n in sorted(by_village.items()))


def _fact_lines(instance, households, facilities, reach, usage, unserved, bounds, result) -> list[str]:
    lines: list[str] = []
    for closure in result["closures_active"]:
        a, b = closure["edge"]
        lines.append(f"{a}—{b} 在 {closure['start']}–{closure['end']} 封闭（左闭右开，分析时刻生效）。")
    blocked_out = [
        (h, entry) for h, entry in zip(households, reach)
        if {sid for sid, _ in entry["reachable"]} != {sid for sid, _ in entry["open_reachable"]}
    ]
    if len(blocked_out) <= 5:
        for h, entry in blocked_out:
            extra = sorted({sid for sid, _ in entry["open_reachable"]} - {sid for sid, _ in entry["reachable"]})
            lines.append(f"封闭使 {h.id}（{h.village}）失去本可达的 {'、'.join(extra)}。")
    else:
        grouped: dict[tuple[str, tuple[str, ...]], list[str]] = {}
        for h, entry in blocked_out:
            extra = tuple(sorted({sid for sid, _ in entry["open_reachable"]} - {sid for sid, _ in entry["reachable"]}))
            grouped.setdefault((h.village, extra), []).append(h.id)
        for (village, extra), ids in sorted(grouped.items()):
            lines.append(
                f"封闭使 {village} 的 {len(ids)} 户失去本可达的 {'、'.join(extra)}"
                f"（如 {'、'.join(ids[:3])} 等）。"
            )
    lines.append(
        f"纯路网可达上界 {bounds['network_reachable_people']} 人；"
        f"叠加设施与整户装入条件后 {bounds['facility_feasible_people']} 人；"
        f"考虑床位与名额竞争后实际安置 {result['allocation']['served_people']} 人。"
    )
    for f in facilities:
        demand: dict[str, list[dict[str, Any]]] = {}
        for row in unserved:
            if f in row["required"]:
                demand.setdefault(row["village"], []).append(row)
        if not demand:
            continue
        parts = []
        for sid, used in usage.items():
            fu = used["facilities"][f]
            if fu["slots"] or fu["used"]:
                parts.append(f"{sid} {fu['used']}/{fu['slots']}（{_village_text(fu['by_village'])}占用）")
        demand_text = "、".join(
            f"{village} {sum(row['size'] for row in rows)} 人（{'、'.join(row['household'] for row in rows)}）"
            for village, rows in sorted(demand.items())
        )
        lines.append(f"{f} 名额紧张：{'；'.join(parts)}；未安置需求：{demand_text}。")
    if len(unserved) <= 8:
        for row in unserved:
            lines.append(f"{row['household']}（{row['village']}/{row['group']}/{row['size']} 人）：{row['reason_text']}——{row['detail']}。")
    else:
        by_reason: dict[str, list[dict[str, Any]]] = {}
        for row in unserved:
            by_reason.setdefault(row["reason_text"], []).append(row)
        for reason_text, rows in sorted(by_reason.items()):
            ids = [row["household"] for row in rows]
            lines.append(f"{reason_text}：{len(rows)} 户（如 {'、'.join(ids[:6])} 等，逐户明细见 unserved 列表）。")
    return lines


def _compare(baseline: dict[str, Any], scenario: dict[str, Any]) -> dict[str, Any]:
    base_map = {row["household"]: row for row in baseline["allocation"]["assignments"]}
    scen_map = {row["household"]: row for row in scenario["allocation"]["assignments"]}
    gained, lost, moved = [], [], []
    for hid, row in sorted(scen_map.items()):
        before = base_map[hid]["shelter"]
        after = row["shelter"]
        if before is None and after is not None:
            gained.append({"household": hid, "village": row["village"], "group": row["group"],
                           "size": row["size"], "shelter": after})
        elif before is not None and after is None:
            lost.append({"household": hid, "village": row["village"], "group": row["group"],
                         "size": row["size"], "shelter": before})
        elif before is not None and after is not None and before != after:
            moved.append({"household": hid, "village": row["village"], "group": row["group"],
                          "size": row["size"], "from": before, "to": after,
                          "minutes_delta": _num(row["minutes"] - base_map[hid]["minutes"])})
    village_delta = {
        key: scenario["allocation"]["by_village"][key]["covered_people"]
        - baseline["allocation"]["by_village"][key]["covered_people"]
        for key in scenario["allocation"]["by_village"]
    }
    group_delta = {
        key: scenario["allocation"]["by_group"][key]["covered_people"]
        - baseline["allocation"]["by_group"][key]["covered_people"]
        for key in scenario["allocation"]["by_group"]
    }
    net_people = scenario["allocation"]["served_people"] - baseline["allocation"]["served_people"]
    travel_delta = _num(scenario["allocation"]["total_travel_minutes"] - baseline["allocation"]["total_travel_minutes"])
    comparison = {
        "net_served_people": net_people,
        "net_served_households": scenario["allocation"]["served_households"] - baseline["allocation"]["served_households"],
        "travel_minutes_delta": travel_delta,
        "gained": gained,
        "lost": lost,
        "moved": moved,
        "by_village_people_delta": village_delta,
        "by_group_people_delta": group_delta,
        "narrative": [],
        "note": "转移基于双方的规范解；最优解集合内的等价替代见各方案 optima 范围。",
    }
    comparison["narrative"] = _comparison_lines(baseline, scenario, comparison)
    return comparison


def _comparison_lines(baseline, scenario, comparison) -> list[str]:
    lines: list[str] = []
    for change in scenario["changes"]:
        if change["type"] == "extra_beds":
            lines.append(f"{change['shelter']} 床位 {change['beds_before']}→{change['beds_after']}（+{change['amount']}）。")
        elif change["type"] == "extra_facility_slots":
            lines.append(
                f"{change['shelter']} {change['facility']} 名额 "
                f"{change['slots_before']}→{change['slots_after']}（+{change['amount']}）。"
            )
        elif change["type"] == "restore_edge":
            a, b = change["edge"]
            tail = "封闭解除" if change["removed_closures"] else "该路段本无生效封闭"
            lines.append(f"恢复路段 {a}—{b}（{tail}）。")
    net = comparison["net_served_people"]
    lines.append(
        f"净增服务 {net:+d} 人（新安置 {len(comparison['gained'])} 户、失去 {len(comparison['lost'])} 户），"
        f"总出行 {comparison['travel_minutes_delta']:+d} 分钟。"
    )
    for item in comparison["gained"]:
        lines.append(f"新安置 {item['household']}（{item['village']}/{item['group']}/{item['size']} 人 → {item['shelter']}）。")
    for item in comparison["lost"]:
        lines.append(f"失去安置 {item['household']}（{item['village']}/{item['group']}/{item['size']} 人，原在 {item['shelter']}）。")
    for item in comparison["moved"]:
        lines.append(
            f"{item['household']} 自 {item['from']} 移至 {item['to']}（出行 {item['minutes_delta']:+d} 分钟）。"
        )
    lines.extend(_insight_lines(scenario, comparison))
    return lines


def _insight_lines(scenario, comparison) -> list[str]:
    """从计算结果生成解释：扩床不解设施稀缺、设施升级与道路恢复的作用。"""
    lines: list[str] = []
    added_beds = [c for c in scenario["changes"] if c["type"] == "extra_beds"]
    added_slots = [c for c in scenario["changes"] if c["type"] == "extra_facility_slots"]
    restored = [c for c in scenario["changes"] if c["type"] == "restore_edge"]
    usage = scenario["allocation"]["shelter_usage"]
    facility_blocked = [
        row for row in scenario["unserved"]
        if row["required"] and row["reason"] in ("no_facility", "facility_absolute", "facility_contended", "capacity_contended")
    ]
    if added_beds and facility_blocked:
        by_facility: dict[str, list[str]] = {}
        for row in facility_blocked:
            for f in row["required"]:
                by_facility.setdefault(f, []).append(row)
        for f, rows in sorted(by_facility.items()):
            holders = []
            for sid, used in usage.items():
                fu = used["facilities"].get(f)
                if fu and (fu["slots"] or fu["used"]):
                    holders.append(f"{sid} {fu['used']}/{fu['slots']}（{_village_text(fu['by_village'])}占用）")
            waiting = "、".join(f"{row['household']}（{row['village']}）" for row in rows)
            villages = "、".join(sorted({row["village"] for row in rows}))
            lines.append(
                f"扩床没有增加 {f} 名额：{'；'.join(holders)}；{waiting} 仍因名额不足无法安置——"
                f"床位压力缓解了，稀缺设施的压力仍落在 {villages}，只是在使用者之间转移。"
            )
    if added_slots:
        gained_with_need = [
            item["household"] for item in comparison["gained"]
            if any(row["household"] == item["household"] and row["required"] for row in scenario["allocation"]["assignments"])
        ]
        if gained_with_need:
            lines.append(f"新增设施名额使 {'、'.join(gained_with_need)} 这类有设施需求的家庭得以安置。")
    if restored and comparison["gained"]:
        lines.append(f"道路恢复让 {'、'.join(item['household'] for item in comparison['gained'])} 获得新的可达安置点。")
    if not comparison["gained"] and not comparison["lost"]:
        if comparison["moved"]:
            lines.append("覆盖人数与现状相同，仅安置位置发生变化。")
        else:
            lines.append("覆盖人数与安置位置均与现状相同。")
    return lines
