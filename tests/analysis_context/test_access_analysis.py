"""access 离线分析的校验：人工可枚举小例子 + 本地批量资料的不变量。

小例子覆盖：资源守恒、必要设施硬约束、多解与范围、封闭区间边界、
床位充足但设施不足、多村共享床位不重复计数、道路恢复。
"""
from __future__ import annotations

import contextlib
import copy
import io
import json
import random
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from shelter_route_capacity.access.__main__ import main as cli_main
from shelter_route_capacity.access.analyze import REASON_TEXT, analyze_payload
from shelter_route_capacity.access.model import Closure, load_dataset
from shelter_route_capacity.access.report import render_markdown
from shelter_route_capacity.network.runtime import DomainError

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "access_scenarios.json"


def payload(households, shelters, edges, *, closures=(), scenarios=None, analysis_time="2026-08-01T10:00:00"):
    """构造最小合法批量资料。"""
    return {
        "schema_version": 1,
        "analysis_time": analysis_time,
        "maximum_minutes": 30,
        "households": households,
        "shelters": shelters,
        "facility_consumption": {"accessible": "one-per-person-in-requiring-household"},
        "edges": edges,
        "closures": list(closures),
        "scenarios": scenarios if scenarios is not None else [{"name": "current", "changes": []}],
    }


def scenario_map(report, case="households"):
    block = next(item for item in report["cases"] if item["case"] == case)
    return {item["name"]: item for item in block["scenarios"]}


def assigned(report_row):
    return {row["household"]: row["shelter"] for row in report_row["allocation"]["assignments"]}


class SmallHandEnumerableTests(unittest.TestCase):
    """人工可枚举的小例子：资源守恒、硬约束、多解。"""

    def test_facility_is_hard_constraint_not_tradeable(self):
        # A 需要 accessible：S1 更近但没有名额，必须绕远去 S2，不能用出行分抵扣。
        data = payload(
            households=[
                {"id": "A", "village": "V1", "size": 2, "required": ["accessible"], "group": "mobility"},
                {"id": "B", "village": "V1", "size": 2, "required": [], "group": "ordinary"},
            ],
            shelters=[
                {"id": "S1", "beds": 2, "facility_slots": {}},
                {"id": "S2", "beds": 2, "facility_slots": {"accessible": 2}},
            ],
            edges=[{"from": "V1", "to": "S1", "minutes": 5}, {"from": "V1", "to": "S2", "minutes": 25}],
        )
        (current,) = scenario_map(analyze_payload(data)).values()
        self.assertEqual(assigned(current), {"A": "S2", "B": "S1"})
        self.assertEqual(current["allocation"]["served_people"], 4)
        self.assertEqual(current["allocation"]["total_travel_minutes"], 30)
        usage = current["allocation"]["shelter_usage"]
        self.assertEqual(usage["S2"]["facilities"]["accessible"]["used"], 2)  # 资源守恒：名额不超载
        self.assertEqual(usage["S1"]["beds_used"], 2)

    def test_multiple_optima_ranges_and_order_independence(self):
        # 一张床两户抢：两个同等最优解，规范解不依赖输入顺序，范围如实给出。
        households = [
            {"id": "A1", "village": "V1", "size": 2, "required": [], "group": "g1"},
            {"id": "B1", "village": "V2", "size": 2, "required": [], "group": "g2"},
            {"id": "B2", "village": "V2", "size": 1, "required": [], "group": "g2"},
        ]
        shelters = [{"id": "S1", "beds": 2, "facility_slots": {}}, {"id": "S2", "beds": 1, "facility_slots": {}}]
        edges = [
            {"from": "V1", "to": "S1", "minutes": 10},
            {"from": "V2", "to": "S1", "minutes": 10},
            {"from": "V2", "to": "S2", "minutes": 10},
        ]
        data = payload(households=households, shelters=shelters, edges=edges)
        (current,) = scenario_map(analyze_payload(data)).values()
        self.assertEqual(current["allocation"]["served_people"], 3)
        self.assertEqual(current["optima"]["count"], 2)  # A1→S1 或 B1→S1，同等最优
        self.assertEqual(assigned(current), {"A1": "S1", "B1": None, "B2": "S2"})
        self.assertEqual(current["allocation"]["by_village"]["V1"]["optimal_range"], [0, 2])
        self.assertEqual(current["allocation"]["by_village"]["V2"]["optimal_range"], [1, 3])
        # 输入列表顺序不影响结论
        shuffled = copy.deepcopy(data)
        shuffled["households"] = [households[2], households[0], households[1]]
        shuffled["edges"] = list(reversed(edges))
        (again,) = scenario_map(analyze_payload(shuffled)).values()
        self.assertEqual(current, again)

    def test_closure_interval_is_left_closed_right_open(self):
        closure = Closure("V1", "S1", datetime.fromisoformat("2026-08-01T10:00:00"),
                          datetime.fromisoformat("2026-08-01T11:00:00"))
        self.assertTrue(closure.active_at(datetime.fromisoformat("2026-08-01T10:00:00")))
        self.assertTrue(closure.active_at(datetime.fromisoformat("2026-08-01T10:59:59")))
        self.assertFalse(closure.active_at(datetime.fromisoformat("2026-08-01T11:00:00")))
        self.assertFalse(closure.active_at(datetime.fromisoformat("2026-08-01T09:59:59")))

    def _closure_payload(self, analysis_time):
        return payload(
            households=[{"id": "A", "village": "V1", "size": 2, "required": [], "group": "g"}],
            shelters=[{"id": "S1", "beds": 2, "facility_slots": {}}],
            edges=[{"from": "V1", "to": "S1", "minutes": 10}],
            closures=[{"from": "V1", "to": "S1", "start": "2026-08-01T10:00:00", "end": "2026-08-01T11:00:00"}],
            analysis_time=analysis_time,
        )

    def test_closure_blocks_then_recovers(self):
        closed = scenario_map(analyze_payload(self._closure_payload("2026-08-01T10:00:00")))["current"]
        self.assertEqual(closed["allocation"]["served_people"], 0)
        self.assertEqual(closed["unserved"][0]["reason"], "no_route")
        self.assertIn("本可到 S1", closed["unserved"][0]["detail"])
        recovered = scenario_map(analyze_payload(self._closure_payload("2026-08-01T11:00:00")))["current"]
        self.assertEqual(recovered["allocation"]["served_people"], 2)

    def test_beds_plenty_but_facility_short(self):
        # 床位充足、设施名额总量装不下该户：原因是设施而不是床。
        data = payload(
            households=[{"id": "A", "village": "V1", "size": 3, "required": ["accessible"], "group": "mobility"}],
            shelters=[{"id": "S1", "beds": 10, "facility_slots": {"accessible": 2}}],
            edges=[{"from": "V1", "to": "S1", "minutes": 10}],
        )
        (current,) = scenario_map(analyze_payload(data)).values()
        self.assertEqual(current["allocation"]["served_people"], 0)
        self.assertEqual(current["bounds"]["network_reachable_people"], 3)
        self.assertEqual(current["bounds"]["facility_feasible_people"], 0)
        self.assertEqual(current["unserved"][0]["reason"], "facility_absolute")

    def test_shared_bed_is_not_counted_twice_across_villages(self):
        # 两村各自看都"可达且有床"，但同一张床只能安置一户。
        data = payload(
            households=[
                {"id": "A", "village": "V1", "size": 3, "required": [], "group": "g"},
                {"id": "B", "village": "V2", "size": 3, "required": [], "group": "g"},
            ],
            shelters=[{"id": "S1", "beds": 4, "facility_slots": {}}],
            edges=[{"from": "V1", "to": "S1", "minutes": 10}, {"from": "V2", "to": "S1", "minutes": 10}],
        )
        (current,) = scenario_map(analyze_payload(data)).values()
        self.assertEqual(current["bounds"]["network_reachable_people"], 6)
        self.assertEqual(current["allocation"]["served_people"], 3)
        self.assertEqual(current["optima"]["count"], 2)
        self.assertEqual(current["allocation"]["by_village"]["V1"]["optimal_range"], [0, 3])
        self.assertEqual(current["allocation"]["by_village"]["V2"]["optimal_range"], [0, 3])
        by_village = current["allocation"]["by_village"]
        self.assertEqual(sum(row["covered_people"] for row in by_village.values()), 3)

    def test_restore_edge_and_invalid_changes(self):
        data = self._closure_payload("2026-08-01T10:00:00")
        data["scenarios"] = [
            {"name": "current", "changes": []},
            {"name": "reopened", "changes": [{"restore_edge": ["V1", "S1"]}]},
        ]
        scenarios = scenario_map(analyze_payload(data))
        self.assertEqual(scenarios["current"]["allocation"]["served_people"], 0)
        self.assertEqual(scenarios["reopened"]["allocation"]["served_people"], 2)
        self.assertEqual(scenarios["reopened"]["comparison"]["net_served_people"], 2)

        for bad_change in (
            [{"shelter": "S9", "extra_beds": 1}],
            [{"restore_edge": ["V1", "S9"]}],
            [{"restore_edge": ["V1"]}],
            [{"shelter": "S1"}],
            [{"shelter": "S1", "extra_facility_slots": {"unknown": 1}}],
        ):
            broken = self._closure_payload("2026-08-01T10:00:00")
            broken["scenarios"] = [{"name": "current", "changes": []}, {"name": "bad", "changes": bad_change}]
            with self.assertRaises(DomainError, msg=f"应拒绝变更 {bad_change}"):
                analyze_payload(broken)

    def test_unknown_consumption_mode_rejected(self):
        data = payload(
            households=[{"id": "A", "village": "V1", "size": 1, "required": [], "group": "g"}],
            shelters=[{"id": "S1", "beds": 1, "facility_slots": {}}],
            edges=[{"from": "V1", "to": "S1", "minutes": 5}],
        )
        data["facility_consumption"] = {"accessible": "by-preference-score"}
        with self.assertRaises(DomainError):
            analyze_payload(data)


class FixtureAnalysisTests(unittest.TestCase):
    """本地批量资料：手工核算的数值 + 跨方案不变量。"""

    @classmethod
    def setUpClass(cls):
        cls.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.report = analyze_payload(cls.payload)

    def test_hand_verified_small_case_numbers(self):
        scenarios = scenario_map(self.report)
        self.assertEqual(
            {name: s["allocation"]["served_people"] for name, s in scenarios.items()},
            {"current": 7, "more-beds": 8, "facility-upgrade": 10, "road-recovered": 7},
        )
        self.assertEqual(
            {name: s["allocation"]["total_travel_minutes"] for name, s in scenarios.items()},
            {"current": 52, "more-beds": 42, "facility-upgrade": 72, "road-recovered": 28},
        )
        self.assertEqual(scenarios["current"]["allocation"]["served_people"],
                         sum(row["covered_people"] for row in scenarios["current"]["allocation"]["by_village"].values()))
        # 基线：H1（轮椅家庭）的无障碍名额竞争失败
        self.assertEqual([row["household"] for row in scenarios["current"]["unserved"]], ["H1"])
        self.assertEqual(scenarios["current"]["unserved"][0]["reason"], "capacity_contended")

    def test_bed_expansion_only_shifts_facility_pressure(self):
        scenarios = scenario_map(self.report)
        more_beds = scenarios["more-beds"]["comparison"]
        self.assertEqual(more_beds["net_served_people"], 1)
        self.assertEqual([row["household"] for row in more_beds["gained"]], ["H1"])
        self.assertEqual([row["household"] for row in more_beds["lost"]], ["H3"])
        # 扩床后 H3 仍进不去：无障碍名额没有增加，只是从 V1 转到 V3 的轮椅家庭身上
        self.assertEqual(scenarios["more-beds"]["unserved"][0]["household"], "H3")
        self.assertTrue(any("扩床没有增加 accessible 名额" in line for line in scenarios["more-beds"]["narrative"]))
        self.assertTrue(any("压力仍落在 V3" in line for line in scenarios["more-beds"]["narrative"]))

    def test_facility_upgrade_and_road_recovery_explained(self):
        scenarios = scenario_map(self.report)
        upgrade = scenarios["facility-upgrade"]["comparison"]
        self.assertEqual(upgrade["net_served_people"], 3)
        self.assertEqual([row["household"] for row in upgrade["gained"]], ["H1"])
        recovered = scenarios["road-recovered"]
        self.assertEqual(recovered["comparison"]["net_served_people"], 0)  # 路通了但床位没变
        self.assertEqual([row["household"] for row in recovered["comparison"]["lost"]], ["H2"])
        self.assertEqual(recovered["optima"]["travel_minutes_range"], [28, 52])
        self.assertEqual(recovered["closures_active"], [])

    def test_conservation_and_hard_constraints_everywhere(self):
        for case in self.report["cases"]:
            for scenario in case["scenarios"]:
                with self.subTest(case=case["case"], scenario=scenario["name"]):
                    alloc = scenario["allocation"]
                    usage = alloc["shelter_usage"]
                    for sid, used in usage.items():
                        self.assertLessEqual(used["beds_used"], used["beds"])
                        for fu in used["facilities"].values():
                            self.assertLessEqual(fu["used"], fu["slots"])
                    rows = alloc["assignments"]
                    self.assertEqual(len({row["household"] for row in rows}), len(rows))  # 一户最多一次
                    served = sum(row["size"] for row in rows if row["shelter"])
                    self.assertEqual(served, alloc["served_people"])
                    self.assertEqual(served, sum(u["beds_used"] for u in usage.values()))  # 床只算一次
                    self.assertEqual(served, sum(v["covered_people"] for v in alloc["by_village"].values()))
                    for row in rows:
                        if row["shelter"]:
                            self.assertLessEqual(row["minutes"], self.report["maximum_minutes"])
                            shelter = usage[row["shelter"]]
                            for facility in row["required"]:
                                self.assertGreater(shelter["facilities"][facility]["slots"], 0)
                    for row in scenario["unserved"]:
                        self.assertIn(row["reason"], REASON_TEXT)
                        if row["reason"] not in ("no_route", "no_facility", "facility_absolute", "beds_absolute"):
                            self.assertTrue(row["blocks"])
                            for block in row["blocks"].values():
                                self.assertTrue(block["blocked_by"])  # 最优解下未安置者处处受阻
                    for key, row in alloc["by_village"].items():
                        lo, hi = row["optimal_range"]
                        self.assertLessEqual(lo, row["covered_people"])
                        self.assertLessEqual(row["covered_people"], hi)
                    lo, hi = scenario["optima"]["travel_minutes_range"]
                    self.assertEqual(lo, alloc["total_travel_minutes"])
                    self.assertLessEqual(lo, hi)

    def test_deterministic_and_input_order_independent(self):
        again = analyze_payload(self.payload)
        self.assertEqual(self.report, again)
        shuffled = copy.deepcopy(self.payload)
        rng = random.Random(20260801)
        for key in ("households", "larger_households", "edges", "shelters", "closures"):
            rng.shuffle(shuffled[key])
        self.assertEqual(self.report, analyze_payload(shuffled))

    def test_larger_case_runs_and_stays_consistent(self):
        scenarios = scenario_map(self.report, case="larger_households")
        self.assertEqual(
            {name: s["allocation"]["served_people"] for name, s in scenarios.items()},
            {"current": 10, "more-beds": 12, "facility-upgrade": 10, "road-recovered": 10},
        )
        current = scenarios["current"]
        self.assertEqual(current["allocation"]["by_group"]["mobility"]["covered_people"], 0)
        self.assertEqual(current["allocation"]["by_group"]["mobility"]["optimal_range"], [0, 2])
        self.assertLessEqual(current["allocation"]["by_group"]["mobility"]["optimal_range"][1], 5)  # 名额总上限
        self.assertFalse(current["optima"]["count_capped"])
        self.assertGreater(current["optima"]["count"], 1)  # 大案例存在大量同等最优解
        # 不能只凭规范解断言某村永久缺口最大：范围列必须存在
        for row in current["allocation"]["by_village"].values():
            self.assertEqual(len(row["optimal_range"]), 2)

    def test_report_is_json_serializable_and_markdown_renders(self):
        json.dumps(self.report, ensure_ascii=False, sort_keys=True)
        markdown = render_markdown(self.report)
        self.assertIn("净增服务", markdown)
        self.assertIn("最弱群体", markdown)

    def test_cli_writes_report_files(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "access-scenarios"
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli_main([str(FIXTURE), "--output", str(out)]), 0)
            saved = json.loads((out / "report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["baseline_scenario"], "current")
            self.assertTrue((out / "report.md").read_text(encoding="utf-8"))

    def test_cli_rejects_bad_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.json"
            bad.write_text(json.dumps({"schema_version": 1}), encoding="utf-8")
            self.assertEqual(cli_main([str(bad), "--output", str(Path(directory) / "out")]), 2)


if __name__ == "__main__":
    unittest.main()
