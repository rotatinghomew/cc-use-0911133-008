"""容量竞争下的家庭安置精确求解。

模型：
- 家庭不可拆分：一户要么整体安置到某个可达安置点，要么不安置；
- 必要设施是硬约束：名额不足即不可安置，不能用出行或偏好分抵扣；
- 床位与设施名额多村共享：状态即各安置点的已用量，同一张床只算一次。

目标（字典序）：先最大化安置人数，再最小化总出行分钟。
求解用动态规划：状态为各安置点（床位 + 各设施）的已用量向量，
值为准则向量（人数、负出行分钟、附加公平性维度），按字典序比较。
附加维度只在前面的准则完全相同时起作用，因此可以精确回答
"在最优解集合内，某村/某群体的安置人数能落到哪个区间"。

规范解（canonical）的选取不依赖输入列表顺序：家庭与安置点先按 id 排序，
DP 状态按字典序处理；值完全相同时后到的决策覆盖回溯指针，
净效果是同等最优下倾向保留编号靠前家庭的既有安排。
这只是最优解集合中的一个代表，不代表现场优先级。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from shelter_route_capacity.access.model import Household, Shelter

COUNT_CAP = 1_000_000


@dataclass(frozen=True, slots=True)
class Candidate:
    shelter_index: int
    minutes: float


@dataclass(frozen=True, slots=True)
class Dimension:
    """附加公平性维度：统计成员集合内家庭被安置的人数。"""

    name: str
    members: frozenset
    maximize: bool


@dataclass(slots=True)
class Solution:
    served_people: int
    travel_minutes: float
    assignment: dict[int, int | None]
    optimal_count: int
    count_capped: bool
    dimension_values: dict[str, float] = field(default_factory=dict)


def _capacity_caps(shelters: Sequence[Shelter], facilities: Sequence[str]) -> tuple[int, ...]:
    caps: list[int] = []
    for shelter in shelters:
        caps.append(shelter.beds)
        caps.extend(shelter.slots(facility) for facility in facilities)
    return tuple(caps)


def _cap_delta(shelter_index: int, household: Household, facilities: Sequence[str], n_shelters: int) -> tuple[int, ...]:
    width = 1 + len(facilities)
    delta = [0] * (n_shelters * width)
    base = shelter_index * width
    delta[base] = household.size
    required = set(household.required)
    for offset, facility in enumerate(facilities, start=1):
        if facility in required:
            delta[base + offset] = household.size
    return tuple(delta)


def solve(
    households: Sequence[Household],
    candidates: Sequence[Sequence[Candidate]],
    shelters: Sequence[Shelter],
    facilities: Sequence[str],
    dimensions: Sequence[Dimension] = (),
    travel_sign: int = -1,
    keep_back: bool = False,
    keep_count: bool = False,
) -> Solution:
    """精确求解一次安置。

    travel_sign=-1 时第二准则为"出行越少越好"（默认）；
    travel_sign=+1 时改为"出行越多越好"，用于求最大覆盖下的出行上界。
    dimensions 中的附加准则依次参与字典序比较，用于范围分析。
    """
    n_shelters = len(shelters)
    caps = _capacity_caps(shelters, facilities)
    dims = len(caps)
    n_crit = 2 + len(dimensions)
    zero_val = (0.0,) * n_crit
    zero_state = (0,) * dims

    # 每户的决策序列：(-1 表示不安置) + 各候选安置点。
    transitions: list[list[tuple[int, tuple[int, ...] | None, tuple[float, ...]]]] = []
    for h_idx, household in enumerate(households):
        options: list[tuple[int, tuple[int, ...] | None, tuple[float, ...]]] = [(-1, None, zero_val)]
        for cand_index, cand in enumerate(candidates[h_idx]):
            cap_d = _cap_delta(cand.shelter_index, household, facilities, n_shelters)
            val = [float(household.size), travel_sign * cand.minutes]
            for dim in dimensions:
                if h_idx in dim.members:
                    val.append(float(household.size) if dim.maximize else float(-household.size))
                else:
                    val.append(0.0)
            options.append((cand_index, cap_d, tuple(val)))
        transitions.append(options)

    best: dict[tuple[int, ...], tuple[float, ...]] = {zero_state: zero_val}
    counts: dict[tuple[int, ...], int] | None = {zero_state: 1} if keep_count else None
    backs: list[dict[tuple[int, ...], tuple[tuple[int, ...], int]]] = []
    capped = False
    for h_idx in range(len(households)):
        layer: dict[tuple[int, ...], tuple[tuple[int, ...], int]] = {}
        new_best: dict[tuple[int, ...], tuple[float, ...]] = {}
        new_counts: dict[tuple[int, ...], int] | None = {} if keep_count else None
        for state in sorted(best):
            base_val = best[state]
            base_cnt = counts[state] if keep_count else 0
            for cand_index, cap_d, val_d in transitions[h_idx]:
                if cap_d is None:
                    new_state = state
                else:
                    new_state = tuple(a + b for a, b in zip(state, cap_d))
                    if any(new_state[i] > caps[i] for i in range(dims)):
                        continue
                new_val = tuple(a + b for a, b in zip(base_val, val_d))
                old = new_best.get(new_state)
                if old is None or new_val > old:
                    new_best[new_state] = new_val
                    if keep_count:
                        new_counts[new_state] = base_cnt
                    if keep_back:
                        layer[new_state] = (state, cand_index)
                elif new_val == old:
                    if keep_count:
                        merged = new_counts[new_state] + base_cnt
                        if merged > COUNT_CAP:
                            merged = COUNT_CAP
                            capped = True
                        new_counts[new_state] = merged
                    if keep_back:
                        # 同等最优：覆盖回溯，倾向保留编号靠前家庭的既有安排。
                        layer[new_state] = (state, cand_index)
        best = new_best
        counts = new_counts
        if keep_back:
            backs.append(layer)

    best_val = max(best.values())
    final_states = [state for state, val in best.items() if val == best_val]
    total_count = 0
    if keep_count:
        for state in final_states:
            total_count += counts[state]
            if total_count > COUNT_CAP:
                total_count = COUNT_CAP
                capped = True

    assignment: dict[int, int | None] = {}
    if keep_back:
        state = min(final_states)
        for h_idx in range(len(households) - 1, -1, -1):
            prev_state, cand_index = backs[h_idx][state]
            assignment[h_idx] = None if cand_index < 0 else candidates[h_idx][cand_index].shelter_index
            state = prev_state

    dimension_values = {}
    for offset, dim in enumerate(dimensions, start=2):
        raw = best_val[offset]
        dimension_values[dim.name] = raw if dim.maximize else -raw
    return Solution(
        served_people=int(round(best_val[0])),
        travel_minutes=best_val[1] * travel_sign,
        assignment=assignment,
        optimal_count=total_count,
        count_capped=capped,
        dimension_values=dimension_values,
    )
