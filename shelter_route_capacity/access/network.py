"""时变路网：封闭区间生效判定与最短通行时间。"""
from __future__ import annotations

import heapq
import math
from datetime import datetime
from typing import Iterable

from shelter_route_capacity.access.model import Closure, Edge


def closed_pairs(closures: Iterable[Closure], moment: datetime) -> frozenset:
    """分析时刻生效的封闭路段（无向，用 frozenset 表示）。"""
    return frozenset(frozenset((closure.a, closure.b)) for closure in closures if closure.active_at(moment))


def shortest_minutes(edges: Iterable[Edge], blocked: frozenset, origin: str) -> dict[str, float]:
    """从 origin 出发、绕开封闭路段的最短分钟数（Dijkstra，路段双向同行）。"""
    adjacency: dict[str, list[tuple[str, float]]] = {}
    for edge in edges:
        if frozenset((edge.a, edge.b)) in blocked:
            continue
        adjacency.setdefault(edge.a, []).append((edge.b, edge.minutes))
        adjacency.setdefault(edge.b, []).append((edge.a, edge.minutes))
    dist = {origin: 0.0}
    heap = [(0.0, origin)]
    while heap:
        current, node = heapq.heappop(heap)
        if current > dist.get(node, math.inf):
            continue
        for nxt, weight in adjacency.get(node, ()):
            candidate = current + weight
            if candidate < dist.get(nxt, math.inf):
                dist[nxt] = candidate
                heapq.heappush(heap, (candidate, nxt))
    return dist
