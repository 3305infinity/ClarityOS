from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from .bench.harness import run_agent_scenario
from .provider_factory import load_gemini_provider
from .scenarios import get_scenario, make_adhoc_scenario
from .utils import json_dumps, merge_seed_context, new_artifact_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one experiment-local macOS agent scenario.")
    parser.add_argument("--agent", required=True, choices=["ax_first", "vision_first", "hybrid_router"])
    parser.add_argument("--scenario", default="", help="Scenario name from the experiment catalog.")
    parser.add_argument("--task", default="", help="Ad hoc task text. Used when --scenario is omitted.")
    parser.add_argument("--run-mode", default="live", choices=["live", "dry"])
    parser.add_argument("--artifact-root", default="", help="Optional artifact directory for this run.")
    parser.add_argument("--seed-context-json", default="", help="Optional JSON object merged into the scenario seed context.")
    parser.add_argument("--json", action="store_true", help="Print the full RunResult JSON.")
    return parser


async def _main_async(args: argparse.Namespace) -> int:
    if not args.scenario and not args.task:
        raise SystemExit("Either --scenario or --task is required.")

    scenario = get_scenario(args.scenario) if args.scenario else make_adhoc_scenario(args.task)
    provider = load_gemini_provider()
    artifact_root = Path(args.artifact_root) if args.artifact_root else new_artifact_dir(f"{args.agent}_{scenario.name}")
    extra_seed = {}
    if args.seed_context_json:
        extra_seed = __import__("json").loads(args.seed_context_json)

    result = await run_agent_scenario(
        args.agent,
        scenario,
        provider=provider,
        run_mode=args.run_mode,
        artifact_root=artifact_root,
        seed_context=merge_seed_context(scenario.seed_context, extra_seed),
    )

    print(f"agent={result.agent_name} scenario={result.scenario_name} success={result.success} latency_ms={result.latency_ms}")
    print(result.final_state_summary)
    print(f"artifacts={artifact_root}")
    if args.json:
        print(json_dumps(result.as_dict()))
    return 0 if result.success else 1


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return asyncio.run(_main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())

