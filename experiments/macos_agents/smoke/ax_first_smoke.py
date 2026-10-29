from __future__ import annotations

import asyncio

from ..bench.harness import run_agent_scenario
from ..provider_factory import load_gemini_provider
from ..scenarios import get_scenario
from ..utils import new_artifact_dir


async def _main_async() -> int:
    provider = load_gemini_provider()
    scenario = get_scenario("open_notes")
    result = await run_agent_scenario(
        "ax_first",
        scenario,
        provider=provider,
        run_mode="live",
        artifact_root=new_artifact_dir("smoke_ax_first"),
    )
    print(result.final_state_summary)
    return 0 if result.success else 1


def main() -> int:
    return asyncio.run(_main_async())


if __name__ == "__main__":
    raise SystemExit(main())

