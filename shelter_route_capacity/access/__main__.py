"""命令行入口：python3 -m shelter_route_capacity.access <fixture> --output <目录>。"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from shelter_route_capacity.access.analyze import analyze_payload
from shelter_route_capacity.access.report import render_markdown
from shelter_route_capacity.network.runtime import DomainError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="shelter_route_capacity.access",
        description="安置点可达性与容量竞争离线分析（不接管现场调度）",
    )
    parser.add_argument("fixture", help="批量分析资料 JSON（如 fixtures/access_scenarios.json）")
    parser.add_argument("--output", required=True, help="报告输出目录（report.json 与 report.md）")
    args = parser.parse_args(argv)
    try:
        payload = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
        report = analyze_payload(payload)
    except DomainError as exc:
        print(f"输入资料错误: {exc}", file=sys.stderr)
        return 2
    out_dir = Path(args.output)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8"
    )
    markdown = render_markdown(report)
    (out_dir / "report.md").write_text(markdown, encoding="utf-8")
    try:
        print(markdown)
    except BrokenPipeError:
        pass
    print(f"已写入 {out_dir}/report.json 与 {out_dir}/report.md", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
