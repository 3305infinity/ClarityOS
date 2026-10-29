from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .bench.harness import compare_agent_set
from .provider_factory import load_gemini_provider
from .utils import json_dumps, new_artifact_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Compare experiment-local macOS agents across a scenario set.")
    parser.add_argument("--scenario-set", default="core_desktop")
    parser.add_argument("--agents", nargs="*", default=["ax_first", "vision_first", "hybrid_router"])
    parser.add_argument("--run-mode", default="live", choices=["live", "dry"])
    parser.add_argument("--artifact-root", default="", help="Optional directory for comparison outputs.")
    parser.add_argument("--json", action="store_true", help="Print the full comparison JSON.")
    return parser


def _render_table(summary) -> str:
    rows = ["agent | scenario | success | latency_ms | fallback_count", "--- | --- | --- | ---: | ---:"]
    for row in summary.rows:
        rows.append(
            f"{row.agent_name} | {row.scenario_name} | {'yes' if row.success else 'no'} | {row.latency_ms} | {row.fallback_count}"
        )
    return "\n".join(rows)


def _render_aggregate_table(summary) -> str:
    rows = ["agent | runs | success_rate | avg_latency_ms | avg_fallback_count", "--- | ---: | ---: | ---: | ---:"]
    for metric in summary.agent_metrics:
        rows.append(
            f"{metric.agent_name} | {metric.run_count} | {metric.success_rate:.2f} | {metric.average_latency_ms} | {metric.average_fallback_count:.2f}"
        )
    return "\n".join(rows)


async def _main_async(args: argparse.Namespace) -> int:
    provider = load_gemini_provider()
    artifact_root = Path(args.artifact_root) if args.artifact_root else new_artifact_dir(f"compare_{args.scenario_set}")
    summary = await compare_agent_set(
        args.scenario_set,
        args.agents,
        provider=provider,
        run_mode=args.run_mode,
        artifact_root=artifact_root,
    )
    print(_render_table(summary))
    if summary.agent_metrics:
        print()
        print(_render_aggregate_table(summary))
    print(f"artifacts={artifact_root}")
    if args.json:
        print(json_dumps(summary.as_dict()))
    return 0


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
