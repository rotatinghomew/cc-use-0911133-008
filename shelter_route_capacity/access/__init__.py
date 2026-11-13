"""安置点可达性与容量竞争离线分析。

只做方案比较：不输出最近距离表，不接管现场调度；
既有单笔容量预留流程（shelter_route_capacity.allocation.cli）行为不变。
"""
from shelter_route_capacity.access.analyze import analyze_payload
from shelter_route_capacity.access.report import render_markdown

__all__ = ["analyze_payload", "render_markdown"]
