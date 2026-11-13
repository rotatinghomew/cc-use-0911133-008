"""分析资料的载入、校验与方案套用。

资料约定（见 docs/data.md）：
- 路段为双向，封闭区间左闭右开（start <= 分析时刻 < end）；
- 床位和设施均以人计；要求某设施的家庭按家庭人数占用该设施名额；
- larger_households 可替换 households 作为更大的家庭集使用。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from shelter_route_capacity.network.runtime import DomainError

CONSUMPTION_PER_PERSON = "one-per-person-in-requiring-household"
SUPPORTED_CONSUMPTION = frozenset({CONSUMPTION_PER_PERSON})
CASE_KEYS = ("households", "larger_households")


@dataclass(frozen=True, slots=True)
class Household:
    id: str
    village: str
    size: int
    required: tuple[str, ...]
    group: str


@dataclass(frozen=True, slots=True)
class Shelter:
    id: str
    beds: int
    facility_slots: Mapping[str, int]

    def slots(self, facility: str) -> int:
        return int(self.facility_slots.get(facility, 0))


@dataclass(frozen=True, slots=True)
class Edge:
    a: str
    b: str
    minutes: float

    def involves(self, x: str, y: str) -> bool:
        return {self.a, self.b} == {x, y}


@dataclass(frozen=True, slots=True)
class Closure:
    a: str
    b: str
    start: datetime
    end: datetime

    def active_at(self, moment: datetime) -> bool:
        """封闭区间左闭右开。"""
        return self.start <= moment < self.end

    def involves(self, x: str, y: str) -> bool:
        return {self.a, self.b} == {x, y}


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    changes: tuple[Mapping[str, Any], ...]


@dataclass(slots=True)
class Dataset:
    analysis_time: datetime
    maximum_minutes: float
    shelters: list[Shelter]
    edges: list[Edge]
    closures: list[Closure]
    facility_consumption: dict[str, str]
    scenarios: list[Scenario]
    cases: dict[str, list[Household]]

    @property
    def facilities(self) -> list[str]:
        return sorted(self.facility_consumption)


@dataclass(slots=True)
class Instance:
    """某一家庭集在某方案下的待解问题（方案变更已套用）。"""

    analysis_time: datetime
    maximum_minutes: float
    households: list[Household]
    shelters: list[Shelter]
    edges: list[Edge]
    closures: list[Closure]
    facility_consumption: dict[str, str]

    @property
    def facilities(self) -> list[str]:
        return sorted(self.facility_consumption)


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{field} 必须是非空字符串")
    return value.strip()


def _integer(value: Any, field: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise DomainError(f"{field} 必须是不小于 {minimum} 的整数")
    return value


def _minutes(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not float(value) > 0:
        raise DomainError(f"{field} 必须是正数（分钟）")
    return float(value)


def _moment(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise DomainError(f"{field} 必须是 ISO 时间字符串")
    try:
        return datetime.fromisoformat(value)
    except ValueError as exc:
        raise DomainError(f"{field} 不是合法的 ISO 时间: {value!r}") from exc


def _rows(payload: Mapping[str, Any], key: str) -> list[Any]:
    rows = payload.get(key)
    if not isinstance(rows, list) or not rows:
        raise DomainError(f"{key} 必须是非空数组")
    return rows


def _household(row: Any, field: str, known_facilities: set[str]) -> Household:
    if not isinstance(row, Mapping):
        raise DomainError(f"{field} 必须是对象")
    required_raw = row.get("required", [])
    if not isinstance(required_raw, list) or any(not isinstance(item, str) for item in required_raw):
        raise DomainError(f"{field}.required 必须是字符串数组")
    required = tuple(sorted(set(required_raw)))
    unknown = [facility for facility in required if facility not in known_facilities]
    if unknown:
        raise DomainError(f"{field}.required 引用了未在 facility_consumption 登记的设施: {unknown}")
    return Household(
        id=_text(row.get("id"), field + ".id"),
        village=_text(row.get("village"), field + ".village"),
        size=_integer(row.get("size"), field + ".size", 1),
        required=required,
        group=str(row.get("group", "unspecified")),
    )


def _shelter(row: Any, field: str) -> Shelter:
    if not isinstance(row, Mapping):
        raise DomainError(f"{field} 必须是对象")
    slots_raw = row.get("facility_slots", {})
    if not isinstance(slots_raw, Mapping):
        raise DomainError(f"{field}.facility_slots 必须是对象")
    slots = {
        _text(facility, field + ".facility_slots 的设施名"): _integer(amount, field + f".facility_slots.{facility}", 0)
        for facility, amount in slots_raw.items()
    }
    return Shelter(
        id=_text(row.get("id"), field + ".id"),
        beds=_integer(row.get("beds"), field + ".beds", 0),
        facility_slots=slots,
    )


def _edge(row: Any, field: str, nodes: set[str]) -> Edge:
    if not isinstance(row, Mapping):
        raise DomainError(f"{field} 必须是对象")
    a = _text(row.get("from"), field + ".from")
    b = _text(row.get("to"), field + ".to")
    if a == b:
        raise DomainError(f"{field} 两端不能相同: {a}")
    for endpoint, role in ((a, "from"), (b, "to")):
        if endpoint not in nodes:
            raise DomainError(f"{field}.{role} 引用了未知节点: {endpoint}")
    return Edge(a, b, _minutes(row.get("minutes"), field + ".minutes"))


def _closure(row: Any, field: str, nodes: set[str], edges: list[Edge]) -> Closure:
    if not isinstance(row, Mapping):
        raise DomainError(f"{field} 必须是对象")
    a = _text(row.get("from"), field + ".from")
    b = _text(row.get("to"), field + ".to")
    for endpoint, role in ((a, "from"), (b, "to")):
        if endpoint not in nodes:
            raise DomainError(f"{field}.{role} 引用了未知节点: {endpoint}")
    if not any(edge.involves(a, b) for edge in edges):
        raise DomainError(f"{field} 封闭的路段 {a}-{b} 不在 edges 中")
    start = _moment(row.get("start"), field + ".start")
    end = _moment(row.get("end"), field + ".end")
    if not start < end:
        raise DomainError(f"{field} 的 start 必须早于 end")
    return Closure(a, b, start, end)


def _scenario(row: Any, field: str) -> Scenario:
    if not isinstance(row, Mapping):
        raise DomainError(f"{field} 必须是对象")
    changes = row.get("changes", [])
    if not isinstance(changes, list):
        raise DomainError(f"{field}.changes 必须是数组")
    return Scenario(name=_text(row.get("name"), field + ".name"), changes=tuple(changes))


def load_dataset(payload: Mapping[str, Any]) -> Dataset:
    """载入并校验批量分析资料，问题直接以 DomainError 报出。"""
    if not isinstance(payload, Mapping):
        raise DomainError("输入必须是 JSON 对象")
    if payload.get("schema_version") != 1:
        raise DomainError("schema_version 只支持 1")
    analysis_time = _moment(payload.get("analysis_time"), "analysis_time")
    maximum_minutes = _minutes(payload.get("maximum_minutes"), "maximum_minutes")

    consumption_raw = payload.get("facility_consumption") or {}
    if not isinstance(consumption_raw, Mapping):
        raise DomainError("facility_consumption 必须是对象")
    consumption: dict[str, str] = {}
    for facility, mode in consumption_raw.items():
        facility_name = _text(facility, "facility_consumption 的设施名")
        if mode not in SUPPORTED_CONSUMPTION:
            raise DomainError(f"暂不支持的设施消耗方式: {mode!r}（设施 {facility_name}）")
        consumption[facility_name] = str(mode)
    known_facilities = set(consumption)

    shelters = [_shelter(row, f"shelters[{index}]") for index, row in enumerate(_rows(payload, "shelters"))]
    shelter_ids = [shelter.id for shelter in shelters]
    if len(set(shelter_ids)) != len(shelter_ids):
        raise DomainError("shelters 存在重复 id")

    cases: dict[str, list[Household]] = {}
    for key in CASE_KEYS:
        rows = payload.get(key)
        if rows is None:
            continue
        if not isinstance(rows, list) or not rows:
            raise DomainError(f"{key} 必须是非空数组")
        households = [_household(row, f"{key}[{index}]", known_facilities) for index, row in enumerate(rows)]
        household_ids = [household.id for household in households]
        if len(set(household_ids)) != len(household_ids):
            raise DomainError(f"{key} 存在重复 id")
        cases[key] = households
    if "households" not in cases:
        raise DomainError("households 不能为空")

    villages = {household.village for households in cases.values() for household in households}
    nodes = villages | set(shelter_ids)
    edges = [_edge(row, f"edges[{index}]", nodes) for index, row in enumerate(_rows(payload, "edges"))]
    closures_raw = payload.get("closures", [])
    if not isinstance(closures_raw, list):
        raise DomainError("closures 必须是数组")
    closures = [_closure(row, f"closures[{index}]", nodes, edges) for index, row in enumerate(closures_raw)]

    moments = [analysis_time]
    for closure in closures:
        moments.extend((closure.start, closure.end))
    if any(moment.tzinfo is None for moment in moments) and any(moment.tzinfo is not None for moment in moments):
        raise DomainError("analysis_time 与 closures 的时区写法不一致")

    scenarios = [_scenario(row, f"scenarios[{index}]") for index, row in enumerate(_rows(payload, "scenarios"))]
    names = [scenario.name for scenario in scenarios]
    if len(set(names)) != len(names):
        raise DomainError("scenarios 存在重复 name")

    return Dataset(
        analysis_time=analysis_time,
        maximum_minutes=maximum_minutes,
        shelters=shelters,
        edges=edges,
        closures=closures,
        facility_consumption=consumption,
        scenarios=scenarios,
        cases=cases,
    )


def base_instance(dataset: Dataset, households: list[Household]) -> Instance:
    return Instance(
        analysis_time=dataset.analysis_time,
        maximum_minutes=dataset.maximum_minutes,
        households=list(households),
        shelters=list(dataset.shelters),
        edges=list(dataset.edges),
        closures=list(dataset.closures),
        facility_consumption=dict(dataset.facility_consumption),
    )


def apply_scenario(instance: Instance, scenario: Scenario) -> tuple[Instance, list[dict[str, Any]]]:
    """套用方案变更，返回新实例与变更摘要（供报告解释使用）。"""
    shelters = {shelter.id: shelter for shelter in instance.shelters}
    closures = list(instance.closures)
    summaries: list[dict[str, Any]] = []
    for index, change in enumerate(scenario.changes):
        field = f"scenarios[{scenario.name}].changes[{index}]"
        if not isinstance(change, Mapping):
            raise DomainError(f"{field} 必须是对象")
        if "restore_edge" in change:
            pair = change["restore_edge"]
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise DomainError(f"{field}.restore_edge 必须是两个节点")
            a = _text(pair[0], field + ".restore_edge[0]")
            b = _text(pair[1], field + ".restore_edge[1]")
            if not any(edge.involves(a, b) for edge in instance.edges):
                raise DomainError(f"{field}.restore_edge 引用了不存在的路段 {a}-{b}")
            before = len(closures)
            closures = [closure for closure in closures if not closure.involves(a, b)]
            summaries.append({"type": "restore_edge", "edge": [a, b], "removed_closures": before - len(closures)})
            continue
        shelter_id = _text(change.get("shelter"), field + ".shelter")
        if shelter_id not in shelters:
            raise DomainError(f"{field}.shelter 不存在: {shelter_id}")
        shelter = shelters[shelter_id]
        beds = shelter.beds
        slots = dict(shelter.facility_slots)
        touched = False
        if "extra_beds" in change:
            amount = _integer(change["extra_beds"], field + ".extra_beds", 0)
            summaries.append({
                "type": "extra_beds", "shelter": shelter_id, "amount": amount,
                "beds_before": beds, "beds_after": beds + amount,
            })
            beds += amount
            touched = True
        if "extra_facility_slots" in change:
            extra = change["extra_facility_slots"]
            if not isinstance(extra, Mapping) or not extra:
                raise DomainError(f"{field}.extra_facility_slots 必须是非空对象")
            for facility, amount in extra.items():
                facility_name = _text(facility, field + ".extra_facility_slots 的设施名")
                if facility_name not in instance.facility_consumption:
                    raise DomainError(f"{field}.extra_facility_slots 引用了未登记的设施: {facility_name}")
                amount = _integer(amount, field + f".extra_facility_slots.{facility_name}", 0)
                before = slots.get(facility_name, 0)
                slots[facility_name] = before + amount
                summaries.append({
                    "type": "extra_facility_slots", "shelter": shelter_id, "facility": facility_name,
                    "amount": amount, "slots_before": before, "slots_after": before + amount,
                })
            touched = True
        if not touched:
            raise DomainError(f"{field} 没有可识别的变更（支持 extra_beds / extra_facility_slots / restore_edge）")
        shelters[shelter_id] = Shelter(shelter.id, beds, slots)
    ordered = [shelters[shelter.id] for shelter in instance.shelters]
    new_instance = Instance(
        analysis_time=instance.analysis_time,
        maximum_minutes=instance.maximum_minutes,
        households=instance.households,
        shelters=ordered,
        edges=instance.edges,
        closures=closures,
        facility_consumption=instance.facility_consumption,
    )
    return new_instance, summaries
