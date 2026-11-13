from __future__ import annotations
from typing import Any
from shelter_route_capacity.network.runtime import RunState, SQLiteLedger, fingerprint, number, sequence

class CapacityAllocator:
    artifact_key = 'capacity_plan'

    def execute(self, state: RunState, stages: list[Any], ledger: SQLiteLedger) -> None:
        graph_stages, capacity_stages, reconcile_stages = stages[:2], stages[2:6], stages[6:]
        phases = []
        for name, selected in (("network", graph_stages), ("allocation", capacity_stages), ("reconcile", reconcile_stages)):
            for stage in selected:
                stage.apply(state)
                ledger.save(state)
            phases.append({"phase": name, "stages": [stage.name for stage in selected], "score": round(sum(state.artifacts[stage.name]["score"] for stage in selected) / len(selected), 6)})
        demand = number(state.payload.get("total"))
        capacity = number(state.payload.get("attributes", {}).get("capacity"))
        overflow = max(0.0, demand - capacity)
        state.artifacts[self.artifact_key] = {"phases": phases, "demand": demand, "capacity": capacity, "overflow": overflow, "requires_rebalance": overflow > 0, "signature": fingerprint(phases)}
        state.emit(self.artifact_key, "capacity_plan_completed", overflow=overflow)
