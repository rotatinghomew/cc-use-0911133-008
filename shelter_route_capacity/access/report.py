"""把分析报告字典渲染成中文 Markdown，供终端展示与存档。"""
from __future__ import annotations

from typing import Any

CASE_TITLES = {
    "households": "小家庭集 households",
    "larger_households": "大家庭集 larger_households",
}
MAX_LISTED_UNSERVED = 12


def render_markdown(report: dict[str, Any]) -> str:
    lines: list[str] = []
    lines.append("# 安置点增容方案对比（离线分析）")
    lines.append("")
    lines.append(f"- 分析时刻：{report['analysis_time']}；服务圈时限：{report['maximum_minutes']} 分钟；基线方案：{report['baseline_scenario']}")
    if report["closures_active"]:
        closed = "、".join(f"{a}—{b}（{row['start']}–{row['end']}）" for row in report["closures_active"] for a, b in [row["edge"]])
        lines.append(f"- 生效封闭：{closed}")
    else:
        lines.append("- 生效封闭：无")
    lines.append("- 口径：家庭不可拆、必要设施为硬约束、床位多村共享；只比较方案，不输出最近距离表，不接管现场调度。")
    lines.append("")
    for case in report["cases"]:
        _render_case(lines, case)
    lines.append("## 方法说明")
    lines.append("")
    for note in report["method_notes"]:
        lines.append(f"- {note}")
    lines.append("")
    return "\n".join(lines)


def _render_case(lines: list[str], case: dict[str, Any]) -> None:
    title = CASE_TITLES.get(case["case"], case["case"])
    lines.append(f"## 案例：{title}（{case['households']} 户 {case['people']} 人）")
    lines.append("")
    lines.append("### 方案总览")
    lines.append("")
    lines.append("| 方案 | 路网可达上界 | 设施可行上界 | 实际安置 | 净增 | 出行分钟 | 最优解数 | 最弱群体 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for scenario in case["scenarios"]:
        bounds = scenario["bounds"]
        alloc = scenario["allocation"]
        optima = scenario["optima"]
        count = f"≥{optima['count']}" if optima["count_capped"] else str(optima["count"])
        if scenario["is_baseline"]:
            net = "—（基线）"
        else:
            net = f"{scenario['comparison']['net_served_people']:+d}"
        weakest = "、".join(
            f"{g} {scenario['weakest']['groups'][g]['coverage']:.0%}" for g in scenario["weakest"]["weakest_groups"]
        )
        lines.append(
            f"| {scenario['name']} | {bounds['network_reachable_people']} | {bounds['facility_feasible_people']} "
            f"| {alloc['served_people']} | {net} | {alloc['total_travel_minutes']} | {count} | {weakest} |"
        )
    lines.append("")
    for scenario in case["scenarios"]:
        _render_scenario(lines, scenario)


def _render_scenario(lines: list[str], scenario: dict[str, Any]) -> None:
    tag = "（基线）" if scenario["is_baseline"] else ""
    lines.append(f"### 方案 {scenario['name']}{tag}")
    lines.append("")
    for note in scenario["narrative"]:
        lines.append(f"- {note}")
    lines.append("")
    alloc = scenario["allocation"]
    lines.append(f"- 各村覆盖（最优解范围）：{_coverage_text(scenario['weakest']['villages'])}")
    lines.append(f"- 各群体覆盖（最优解范围）：{_coverage_text(scenario['weakest']['groups'])}")
    travel = scenario["optima"]["travel_minutes_range"]
    lines.append(f"- 出行负担：规范解 {alloc['total_travel_minutes']} 分钟；只追求人数最多时最高 {travel[1]} 分钟")
    lines.append("")
    unserved = scenario["unserved"]
    if unserved:
        lines.append(f"未安置 {len(unserved)} 户：")
        lines.append("")
        for row in unserved[:MAX_LISTED_UNSERVED]:
            lines.append(f"- {row['household']}（{row['village']}/{row['group']}/{row['size']} 人）：{row['reason_text']}——{row['detail']}")
        if len(unserved) > MAX_LISTED_UNSERVED:
            lines.append(f"- ……其余 {len(unserved) - MAX_LISTED_UNSERVED} 户见 report.json")
        lines.append("")


def _coverage_text(rows: dict[str, Any]) -> str:
    parts = []
    for key, row in rows.items():
        lo, hi = row["optimal_range"]
        span = f"[{lo}, {hi}]" if lo != hi else f"[{lo}]"
        parts.append(f"{key} {row['covered_people']}/{row['total_people']}（{row['coverage']:.0%}，{span}）")
    return "；".join(parts)
