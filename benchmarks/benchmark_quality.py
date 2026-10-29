"""
Moonwalk — Output Quality Benchmark
====================================
Tests whether the agent produces elite-level responses
comparable to Claude/GPT-4 — not just correct, but
exceptionally well-crafted.

Uses 6-dimension LLM-as-Judge scoring via Gemini Flash.

Run:
    python benchmarks/benchmark_quality.py
"""

import asyncio
import time
import json
import os
import sys
from typing import Dict, Any, List, Optional
from datetime import datetime

# Add backend to path
sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend"))

from agent import MoonwalkAgent
from tools import registry as tool_registry
import perception

# ═══════════════════════════════════════════════════════════════
#  Constants
# ═══════════════════════════════════════════════════════════════

DIFFICULTY_WEIGHTS = {"easy": 1, "medium": 2, "hard": 3}
SCENARIOS_PATH = os.path.join(os.path.dirname(__file__), "quality_scenarios.json")
RESULTS_PATH = os.path.join(os.path.dirname(__file__), "quality_results.json")
DEFAULT_TIMEOUT = 180  # Longer — quality responses need more time

QUALITY_DIMENSIONS = {
    "completeness": "Does the response fully address every part of the user's request? Nothing missing?",
    "accuracy": "Is all information factually correct and technically sound? No hallucinations?",
    "depth": "Does it show expert-level understanding, not just surface knowledge? Demonstrates mastery?",
    "formatting": "Is it well-structured with headers, lists, code blocks as appropriate? Easy to scan?",
    "actionability": "Can the user immediately act on this? Are steps clear, specific, and copy-paste ready?",
    "tone": "Is the tone natural, helpful, and appropriately professional? Not robotic or overly verbose?",
}


class BenchmarkDone(BaseException):
    """Raised to terminate the agent loop when send_response is called.
    Inherits from BaseException (not Exception) so it won't be caught
    by the agent's generic 'except Exception' handlers."""
    pass


# ═══════════════════════════════════════════════════════════════
#  6-Dimension LLM Judge
# ═══════════════════════════════════════════════════════════════

async def quality_judge(output: str, elite_criteria: str, prompt: str, router) -> dict:
    """
    Score the agent's response on 6 quality dimensions (0-10 each).
    Returns per-dimension scores + overall average.
    """
    dimensions_text = "\n".join(
        f"- **{dim}**: {desc}" for dim, desc in QUALITY_DIMENSIONS.items()
    )

    judge_prompt = f"""You are an elite AI output quality evaluator. You are comparing this response against what the BEST AI models (Claude 3.5 Sonnet, GPT-4) would produce.

## User's Original Prompt
{prompt}

## Elite-Level Criteria (what a 10/10 looks like)
{elite_criteria}

## Agent's Actual Response
{output}

## Score each dimension 0-10:
{dimensions_text}

## Scoring Guide
- 0-3: Poor — significant issues, below average AI quality
- 4-5: Mediocre — basic but uninspiring, missing key elements
- 6-7: Good — solid response but not elite, some room for improvement
- 8-9: Excellent — near-elite quality, minor nitpicks only
- 10: Perfect — indistinguishable from the best AI models

Return ONLY valid JSON (no markdown, no backticks):
{{"completeness": <0-10>, "accuracy": <0-10>, "depth": <0-10>, "formatting": <0-10>, "actionability": <0-10>, "tone": <0-10>, "overall_reasoning": "<2-3 sentence summary of quality>"}}"""

    try:
        from providers import LLMResponse
        response = await router.route_and_call(
            user_message=judge_prompt,
            system_prompt="You are a benchmark evaluator. Return only JSON.",
            force_tier="fast",
        )

        text = ""
        if isinstance(response, LLMResponse):
            text = response.text or ""
        else:
            text = str(response)

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

        result = json.loads(text)
        scores = {}
        for dim in QUALITY_DIMENSIONS:
            scores[dim] = int(result.get(dim, 0))

        avg = sum(scores.values()) / len(scores) if scores else 0
        return {
            "dimensions": scores,
            "average": round(avg, 1),
            "reasoning": str(result.get("overall_reasoning", "No reasoning")),
        }
    except Exception as e:
        return {
            "dimensions": {d: -1 for d in QUALITY_DIMENSIONS},
            "average": -1,
            "reasoning": f"Judge failed: {e}",
        }


# ═══════════════════════════════════════════════════════════════
#  Scenario Result
# ═══════════════════════════════════════════════════════════════

class ScenarioResult:
    def __init__(self, scenario_id: str, difficulty: str):
        self.scenario_id = scenario_id
        self.difficulty = difficulty
        self.weight = DIFFICULTY_WEIGHTS.get(difficulty, 1)
        self.passed = False
        self.scores: Dict[str, int] = {}
        self.avg_score: float = 0.0
        self.judge_reasoning: str = ""
        self.failure_reasons: List[str] = []
        self.tools_called: List[str] = []
        self.latency: float = 0.0
        self.final_output: str = ""
        self.prompt_text: str = ""
        self.timed_out: bool = False
        self.contains_code: bool = False

    def to_dict(self) -> dict:
        return {
            "scenario_id": self.scenario_id,
            "difficulty": self.difficulty,
            "weight": self.weight,
            "passed": self.passed,
            "dimension_scores": self.scores,
            "avg_score": self.avg_score,
            "judge_reasoning": self.judge_reasoning,
            "failure_reasons": self.failure_reasons,
            "tools_called": self.tools_called,
            "latency": round(self.latency, 2),
            "final_output": self.final_output[:2000],
            "prompt_text": self.prompt_text,
            "timed_out": self.timed_out,
            "contains_code": self.contains_code,
        }


class SuiteResult:
    def __init__(self, name: str):
        self.name = name
        self.scenario_results: List[ScenarioResult] = []

    @property
    def total(self): return len(self.scenario_results)

    @property
    def passed(self): return sum(1 for r in self.scenario_results if r.passed)

    @property
    def avg_score(self):
        scores = [r.avg_score for r in self.scenario_results if r.avg_score >= 0]
        return sum(scores) / len(scores) if scores else 0.0

    @property
    def weighted_score(self): return sum(r.weight for r in self.scenario_results if r.passed)

    @property
    def max_weighted_score(self): return sum(r.weight for r in self.scenario_results)

    @property
    def avg_latency(self):
        lats = [r.latency for r in self.scenario_results if r.latency > 0]
        return sum(lats) / len(lats) if lats else 0.0

    @property
    def dimension_averages(self) -> Dict[str, float]:
        """Average score per dimension across all scenarios."""
        dim_sums: Dict[str, list] = {d: [] for d in QUALITY_DIMENSIONS}
        for r in self.scenario_results:
            for d, s in r.scores.items():
                if s >= 0 and d in dim_sums:
                    dim_sums[d].append(s)
        return {d: (sum(v)/len(v) if v else 0) for d, v in dim_sums.items()}

    def to_dict(self) -> dict:
        return {
            "suite_name": self.name,
            "passed": self.passed,
            "total": self.total,
            "avg_score": round(self.avg_score, 1),
            "dimension_averages": {d: round(v, 1) for d, v in self.dimension_averages.items()},
            "weighted_score": self.weighted_score,
            "max_weighted_score": self.max_weighted_score,
            "avg_latency": round(self.avg_latency, 2),
            "scenarios": [r.to_dict() for r in self.scenario_results],
        }


# ═══════════════════════════════════════════════════════════════
#  Benchmark Runner
# ═══════════════════════════════════════════════════════════════

class QualityBenchmark:
    def __init__(self, name: str, description: str, shared_router=None):
        self.name = name
        self.description = description
        self.scenarios: List[dict] = []
        self._shared_router = shared_router

    def add_scenario(self, scenario: dict):
        self.scenarios.append(scenario)

    async def run(self) -> SuiteResult:
        suite_result = SuiteResult(self.name)

        print(f"\n{'='*70}", flush=True)
        print(f"💎 Suite: {self.name}", flush=True)
        print(f"   {self.description}", flush=True)
        print(f"{'='*70}\n", flush=True)

        for scenario in self.scenarios:
            sid = scenario.get("id", "?")
            difficulty = scenario.get("difficulty", "easy")
            prompt = scenario.get("prompt", "")[:55]
            timeout = scenario.get("timeout", DEFAULT_TIMEOUT)
            diff_icon = {"easy": "🟢", "medium": "🟡", "hard": "🔴"}.get(difficulty, "⚪")

            print(f"  [{sid}] {diff_icon} {difficulty.upper()} — '{prompt}...'", flush=True)

            result = ScenarioResult(sid, difficulty)

            # Retry with backoff for API rate limiting (503/404 errors)
            max_retries = 2
            for attempt in range(max_retries + 1):
                try:
                    await asyncio.wait_for(
                        self._run_single(scenario, result),
                        timeout=timeout
                    )
                except asyncio.TimeoutError:
                    result.timed_out = True
                    result.failure_reasons.append(f"TIMEOUT ({timeout}s)")
                except BenchmarkDone:
                    pass  # Expected — send_response was called
                except Exception as e:
                    result.failure_reasons.append(f"CRASH: {e}")

                # Check if we got a rate-limit error and should retry
                if "high server demand" in result.final_output.lower() and attempt < max_retries:
                    delay = 5 * (2 ** attempt)  # 5s, 10s
                    print(f"       🔄 API error, retrying in {delay}s (attempt {attempt + 2}/{max_retries + 1})...", flush=True)
                    result = ScenarioResult(sid, difficulty)  # Reset result
                    await asyncio.sleep(delay)
                    continue
                break

            suite_result.scenario_results.append(result)

            if result.timed_out:
                print(f"       ⏰ TIMEOUT ({timeout}s)", flush=True)
            elif result.passed:
                dims = " | ".join(f"{d[:4]}:{s}" for d, s in result.scores.items())
                print(f"       ✅ PASS (avg: {result.avg_score:.1f}/10, {result.latency:.1f}s)", flush=True)
                print(f"          [{dims}]", flush=True)
            else:
                dims = " | ".join(f"{d[:4]}:{s}" for d, s in result.scores.items())
                print(f"       ❌ FAIL (avg: {result.avg_score:.1f}/10, {result.latency:.1f}s)", flush=True)
                print(f"          [{dims}]", flush=True)
                for r in result.failure_reasons[:2]:
                    print(f"          └─ {r[:120]}", flush=True)

            # Brief delay between scenarios to avoid API rate limiting
            await asyncio.sleep(3)

        bar = "█" * suite_result.passed + "░" * (suite_result.total - suite_result.passed)
        print(f"\n  [{bar}] {suite_result.passed}/{suite_result.total} passed "
              f"(avg: {suite_result.avg_score:.1f}/10, avg {suite_result.avg_latency:.1f}s)\n", flush=True)

        return suite_result

    async def _run_single(self, scenario: dict, result: ScenarioResult):
        agent = MoonwalkAgent()
        if self._shared_router:
            agent.router = self._shared_router

        # Intercept tool calls
        called_tools: list = []
        final_response = ""
        original_execute = tool_registry.execute

        async def mock_execute(tool_name: str, tool_args: dict) -> str:
            nonlocal final_response
            called_tools.append(tool_name)

            if tool_name == "send_response":
                msg = tool_args.get("message", "")
                final_response = msg
                # Terminate the loop immediately — prevents duplicate responses
                raise BenchmarkDone(msg)
            if tool_name == "await_reply":
                return "User replied: Please provide the best answer you can."
            if tool_name == "run_python":
                code = tool_args.get("code", "")
                try:
                    import subprocess
                    proc = subprocess.run(
                        ["python3", "-c", code],
                        capture_output=True, text=True, timeout=10
                    )
                    return f"[STDOUT]\n{proc.stdout}\n[STDERR]\n{proc.stderr}"
                except Exception as e:
                    return f"Error: {e}"
            if tool_name == "think":
                return "Thought recorded."
            if tool_name == "read_file":
                return "File contents: mock data."
            if tool_name == "run_shell":
                return "Command output: mock result."
            if tool_name == "write_file":
                content = tool_args.get("content", "")
                # Capture code written to files as part of the output
                final_response = final_response + "\n\n```\n" + content + "\n```"
                return f"File written ({len(content)} chars)."

            return f"Tool '{tool_name}' executed (mock)."

        tool_registry.execute = mock_execute  # type: ignore

        # Override system prompt for quality testing
        agent._build_system_prompt = lambda: (
            "You are in a STRICT automated testing environment.\n"
            "Your goal is to produce the HIGHEST QUALITY response possible.\n"
            "CRITICAL: Do NOT explore the system. Do NOT call run_shell, read_screen, get_ui_tree, "
            "or any OS tools unless the task specifically requires it.\n"
            "Focus on producing expert-level content. Answer in a SINGLE send_response call.\n"
            "For coding tasks, include complete, production-ready code.\n"
            "Be thorough, well-structured, and actionable."
        )

        # Suppress stdout
        saved_stdout = sys.stdout
        sys.stdout = open(os.devnull, 'w')

        original_get_minimal_context = perception.get_minimal_context
        async def mock_ctx(): return ""
        perception.get_minimal_context = mock_ctx  # type: ignore

        context = perception.ContextSnapshot(
            active_app="Terminal", window_title="Benchmark", browser_url=None
        )

        start_t = time.time()
        try:
            if not self._shared_router:
                await agent.router.initialize()
            response = await agent.run(scenario["prompt"], context)
            if isinstance(response, tuple):
                final_response = final_response or str(response[0])
            else:
                final_response = final_response or str(response)
        except BenchmarkDone as e:
            final_response = final_response or str(e)
        except Exception as e:
            final_response = f"CRASH: {e}"
        finally:
            sys.stdout.close()
            sys.stdout = saved_stdout
            sys.stdout.flush()
            result.latency = time.time() - start_t
            tool_registry.execute = original_execute  # type: ignore
            perception.get_minimal_context = original_get_minimal_context

        result.tools_called = called_tools
        result.final_output = str(final_response)[:5000]  # Keep more for quality eval
        result.prompt_text = scenario["prompt"]

        # Check if output contains code
        if scenario.get("must_contain_code"):
            result.contains_code = "```" in result.final_output or "def " in result.final_output or "function " in result.final_output

        # ── 6-Dimension Judge ──
        judge_result = await quality_judge(
            output=result.final_output,
            elite_criteria=scenario.get("elite_criteria", ""),
            prompt=scenario["prompt"],
            router=self._shared_router or agent.router,
        )

        result.scores = judge_result["dimensions"]
        result.avg_score = judge_result["average"]
        result.judge_reasoning = judge_result["reasoning"]

        # Check for code requirement
        if scenario.get("must_contain_code") and not result.contains_code:
            result.failure_reasons.append("Response should contain code but none found")
            result.passed = False
            return

        # Pass = average score >= 7.0
        result.passed = result.avg_score >= 7.0


# ═══════════════════════════════════════════════════════════════
#  Main
# ═══════════════════════════════════════════════════════════════

async def main():
    with open(SCENARIOS_PATH, "r") as f:
        data = json.load(f)

    suites_data = data.get("quality_suites", [])
    if not suites_data:
        print("❌ No quality_suites found.", flush=True)
        return

    print("⚡ Initializing model router...", flush=True)
    from model_router import ModelRouter
    router = ModelRouter()
    await router.initialize()
    print("✅ Router ready.\n", flush=True)

    all_results: List[SuiteResult] = []

    for suite_data in suites_data:
        bench = QualityBenchmark(
            name=suite_data["name"],
            description=suite_data.get("description", ""),
            shared_router=router,
        )
        for scenario in suite_data.get("scenarios", []):
            bench.add_scenario(scenario)
        suite_result = await bench.run()
        all_results.append(suite_result)

    # ── Final Report ──
    total = sum(r.total for r in all_results)
    passed = sum(r.passed for r in all_results)
    weighted = sum(r.weighted_score for r in all_results)
    max_wt = sum(r.max_weighted_score for r in all_results)

    all_scores = [s.avg_score for r in all_results for s in r.scenario_results if s.avg_score >= 0]
    avg_score = sum(all_scores) / len(all_scores) if all_scores else 0

    # Aggregate dimension averages
    dim_all: Dict[str, list] = {d: [] for d in QUALITY_DIMENSIONS}
    for r in all_results:
        for s in r.scenario_results:
            for d, v in s.scores.items():
                if v >= 0 and d in dim_all:
                    dim_all[d].append(v)
    dim_avgs = {d: (sum(v)/len(v) if v else 0) for d, v in dim_all.items()}

    print(f"\n{'═'*70}", flush=True)
    print(f"  💎  OUTPUT QUALITY BENCHMARK REPORT", flush=True)
    print(f"{'═'*70}\n", flush=True)

    print(f"  {'Suite':<40} {'Pass':>6}   {'Avg Score':>10}   {'Weighted':>10}", flush=True)
    print(f"  {'─'*40} {'─'*6} {'─'*10} {'─'*10}", flush=True)

    for r in all_results:
        icon = "✅" if r.passed == r.total else ("⚠️" if r.passed > 0 else "❌")
        print(f"  {icon}  {r.name:<37} {r.passed}/{r.total:>4}   "
              f"  {r.avg_score:>5.1f}/10   "
              f"{r.weighted_score}/{r.max_weighted_score:>6}", flush=True)

    print(f"\n  📊 Quality Dimensions (averaged across all scenarios):", flush=True)
    for dim, avg in dim_avgs.items():
        bar_len = int(avg)
        bar = "█" * bar_len + "░" * (10 - bar_len)
        print(f"     {dim:<16} [{bar}] {avg:.1f}/10", flush=True)

    pct = (weighted / max_wt * 100) if max_wt > 0 else 0
    grade = ("🏆 ELITE" if avg_score >= 8.5 else
             ("✅ EXCELLENT" if avg_score >= 7.5 else
              ("⚠️ GOOD" if avg_score >= 6.0 else "❌ NEEDS WORK")))

    print(f"\n  {'─'*70}", flush=True)
    print(f"\n  Passed:         {passed}/{total}", flush=True)
    print(f"  Avg Score:      {avg_score:.1f}/10", flush=True)
    print(f"  Weighted:       {weighted}/{max_wt} ({pct:.1f}%)", flush=True)
    print(f"  Quality Grade:  {grade}", flush=True)
    print(f"\n{'═'*70}\n", flush=True)

    report = {
        "benchmark": "Output Quality",
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "total_scenarios": total,
            "total_passed": passed,
            "avg_score": round(avg_score, 1),
            "weighted_score": weighted,
            "max_weighted": max_wt,
            "weighted_pct": round(pct, 1),
            "grade": grade,
            "dimension_averages": {d: round(v, 1) for d, v in dim_avgs.items()},
        },
        "suites": [r.to_dict() for r in all_results],
    }

    with open(RESULTS_PATH, "w") as f:
        json.dump(report, f, indent=2)

    print(f"📁 Full report saved to: {RESULTS_PATH}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
