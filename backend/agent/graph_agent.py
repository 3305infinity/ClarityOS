"""
ClarityOS — LangGraph orchestration layer
=========================================
Makes the agent's top-level pipeline an explicit, inspectable state machine:

    understand → plan → validate → execute → evaluate → respond

Today that pipeline lives implicitly inside `MoonwalkAgentV2.process()`, a long
method where the stages, the retry bounds and the exit conditions are tangled
together with UI streaming and pending-state bookkeeping. LangGraph does not
replace any of that work — every node *delegates* to the component that already
does the job:

    understand  →  ModelRouter.route()          (model/tier selection)
    plan        →  TaskPlanner.create_plan()    (milestone planning)
    validate    →  local checks only            (new: bounded plan correction)
    execute     →  MoonwalkAgentV2._execute_milestone_plan()
                   └─ MilestoneExecutor → tool_registry.execute()  (guarded)
    evaluate    →  local checks only
    respond     →  returns the text the caller already expects

What the graph adds that did not exist before:

  * a typed, serializable `ClarityState` describing the run
  * an explicit `validate` stage with a **bounded** replan loop
  * one, and only one, side-effecting execution per run (`executed` latch)
  * cancellation checked at every node boundary

Safety
------
The graph never touches tools directly. Execution funnels into the existing
`_execute_milestone_plan`, which calls `tool_registry.execute` — already wrapped
by `VoiceToolGuard` (Electron) or `Hub.install_guard` (website). So the
PolicyEngine and ApprovalBroker apply unchanged, and a denied approval still
returns `policy.user_denied` without running anything.

The graph is opt-in. See `MOONWALK_USE_LANGGRAPH` in is_enabled().
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from functools import partial
from typing import Any, Optional, TypedDict

print = partial(print, flush=True)

__all__ = ["ClarityState", "GraphRunContext", "is_enabled", "is_available", "run_graph"]


# ═══════════════════════════════════════════════════════════════
#  Configuration
# ═══════════════════════════════════════════════════════════════

def is_enabled() -> bool:
    """
    MOONWALK_USE_LANGGRAPH = 1 | true | yes | on   (default: OFF)

    Off by default so the original agent path stays the shipping behaviour
    until this has been exercised on real traffic.
    """
    return (os.environ.get("MOONWALK_USE_LANGGRAPH", "") or "").strip().lower() in ("1", "true", "yes", "on")


def is_available() -> bool:
    """True when langgraph is importable. A missing package must never block startup."""
    try:
        import langgraph  # noqa: F401
        return True
    except Exception:
        return False


# Bounded loops — the graph must terminate.
MAX_PLAN_ATTEMPTS = 2        # one initial plan + one correction


# ═══════════════════════════════════════════════════════════════
#  State
# ═══════════════════════════════════════════════════════════════

class ClarityState(TypedDict, total=False):
    """
    Serializable description of one request's journey through the graph.

    Deliberately excludes the live provider, WorldState, websocket callback and
    context snapshot — those are non-serializable runtime handles and live in
    `GraphRunContext` instead. No credentials and no audio ever enter state.
    """
    # input
    user_text: str
    conversation_summary: str

    # understand
    intent: str
    route_tier: str
    model_name: str

    # plan
    plan_summary: str
    milestone_count: int
    needs_clarification: bool
    clarification_prompt: str
    plan_attempts: int

    # execute — `executed` is a latch: once true the plan has had real-world
    # effects and must never be re-run, whatever happens downstream.
    executed: bool
    success: bool
    result: str
    awaiting_reply: bool

    # control
    cancelled: bool
    error: str
    final_response: str
    trace: list


@dataclass
class GraphRunContext:
    """Live handles the nodes need but which must not be stored in graph state."""
    agent: Any                      # MoonwalkAgentV2
    context: Any                    # perception.ContextSnapshot
    world_state: Any = None
    provider: Any = None
    plan: Any = None                # MilestonePlan
    llm_tool_declarations: Optional[list] = None
    ws_callback: Any = None


def _cancelled() -> bool:
    try:
        from runtime_state import runtime_state_store
        return runtime_state_store.is_request_cancelled()
    except Exception:
        return False


def _note(state: ClarityState, line: str) -> None:
    state.setdefault("trace", []).append(line)
    print(f"[Graph] {line}")


# ═══════════════════════════════════════════════════════════════
#  Nodes
# ═══════════════════════════════════════════════════════════════

async def node_understand(state: ClarityState, ctx: GraphRunContext) -> ClarityState:
    """Sense the environment and pick a model. Delegates to ModelRouter."""
    if _cancelled():
        return {"cancelled": True}

    agent = ctx.agent
    ctx.world_state = await agent._build_world_state(state["user_text"], ctx.context)
    intent = getattr(ctx.world_state.intent, "action", None)
    intent_name = getattr(intent, "value", "") or "unknown"

    try:
        decision = await agent.router.route(
            state["user_text"],
            context_summary=f"App: {ctx.context.active_app}, Window: {ctx.context.window_title}",
            has_screenshot=ctx.context.screenshot_path is not None,
        )
    except RuntimeError as e:
        # No usable model (no key, or every model out of quota). The router's
        # message is already user-facing and actionable.
        return {"error": str(e), "intent": intent_name}

    ctx.provider = decision.provider
    out: ClarityState = {
        "intent": intent_name,
        "route_tier": decision.tier.name,
        "model_name": decision.model_name,
    }
    _note(state, f"understand: intent={intent_name} tier={decision.tier.name} model={decision.model_name}")
    return out


async def node_plan(state: ClarityState, ctx: GraphRunContext) -> ClarityState:
    """Build a milestone plan. Delegates to the existing TaskPlanner."""
    if _cancelled():
        return {"cancelled": True}

    agent = ctx.agent
    if agent.planner is None:
        from agent.task_planner import TaskPlanner
        from tools import registry as tool_registry
        agent.planner = TaskPlanner(provider=ctx.provider, tool_registry=tool_registry)
    else:
        agent.planner.provider = ctx.provider

    selected_tools, decls = agent._select_tool_surface(state["user_text"], ctx.context, ctx.world_state)
    ctx.llm_tool_declarations = decls

    if ctx.ws_callback:
        await ctx.ws_callback({"type": "doing", "text": "Planning…", "tool": "planner"})

    # Remember what the planner had already recorded, so a *fresh* provider
    # failure can be told apart from a stale one left by an earlier request.
    previous_error = getattr(agent.planner, "last_provider_error", None)

    plan = await agent.planner.create_plan(
        state["user_text"],
        ctx.world_state,
        available_tools=selected_tools,
        conversation_history=agent.conversation.get_history(),
    )
    ctx.plan = plan

    # A model auth/quota failure used to arrive as needs_clarification, so the
    # user was asked to "add more detail" for a request that was perfectly
    # clear — the real problem was the API key or the quota. Surface it.
    provider_error = getattr(agent.planner, "last_provider_error", None)
    if provider_error is not None and provider_error is not previous_error:
        message = provider_error.user_message()
        _note(state, f"plan: provider failure ({provider_error.kind.value}) — reporting it")
        return {
            "plan_attempts": int(state.get("plan_attempts", 0)) + 1,
            "milestone_count": 0,
            "needs_clarification": False,
            "clarification_prompt": "",
            "error": message,
        }

    attempts = int(state.get("plan_attempts", 0)) + 1
    out: ClarityState = {
        "plan_attempts": attempts,
        "plan_summary": getattr(plan, "task_summary", "") or "",
        "milestone_count": len(getattr(plan, "milestones", []) or []),
        "needs_clarification": bool(getattr(plan, "needs_clarification", False)),
        "clarification_prompt": getattr(plan, "clarification_prompt", "") or "",
    }
    _note(state, f"plan: attempt={attempts} milestones={out['milestone_count']} "
                 f"clarify={out['needs_clarification']}")
    return out


async def node_validate(state: ClarityState, ctx: GraphRunContext) -> ClarityState:
    """
    Check the plan is runnable before anything touches the real world.

    This stage did not exist before: an empty plan previously fell through to a
    generic "try rephrasing". Now it is a routing decision with a bounded retry.
    """
    if _cancelled():
        return {"cancelled": True}

    if state.get("needs_clarification"):
        _note(state, "validate: planner asked for clarification")
        return {}
    if state.get("milestone_count", 0) > 0:
        _note(state, "validate: plan is runnable")
        return {}
    _note(state, f"validate: empty plan (attempt {state.get('plan_attempts')})")
    return {}


async def node_execute(state: ClarityState, ctx: GraphRunContext) -> ClarityState:
    """
    Run the plan. Delegates to the existing `_execute_milestone_plan`, which
    drives MilestoneExecutor → tool_registry.execute (policy-guarded).

    The `executed` latch is set *before* awaiting, so a retry edge added later
    can never re-enter this node and repeat a side effect that already ran.
    """
    if _cancelled():
        return {"cancelled": True}
    if state.get("executed"):
        _note(state, "execute: already ran — refusing to repeat side effects")
        return {}

    agent, plan = ctx.agent, ctx.plan
    if ctx.ws_callback and getattr(plan, "milestones", None):
        await ctx.ws_callback({"type": "doing", "text": plan.milestones[0].goal, "tool": "executor"})

    state["executed"] = True        # latch first; the run below has real effects
    try:
        final_response, awaiting = await agent._execute_milestone_plan(
            plan=plan,
            world_state=ctx.world_state,
            provider=ctx.provider,
            context=ctx.context,
            user_text=state["user_text"],
            llm_tool_declarations=ctx.llm_tool_declarations,
            ws_callback=ctx.ws_callback,
        )
    except Exception as e:
        _note(state, f"execute: failed ({e.__class__.__name__})")
        return {"executed": True, "success": False, "error": f"{e.__class__.__name__}: {e}"}

    _note(state, f"execute: done awaiting_reply={awaiting}")
    return {"executed": True, "success": True, "result": final_response or "", "awaiting_reply": bool(awaiting)}


async def node_evaluate(state: ClarityState, ctx: GraphRunContext) -> ClarityState:
    """Decide whether the run finished cleanly. No side effects here."""
    if _cancelled():
        return {"cancelled": True}
    ok = bool(state.get("success")) and not state.get("error")
    _note(state, f"evaluate: success={ok}")
    return {}


async def node_respond(state: ClarityState, ctx: GraphRunContext) -> ClarityState:
    """Produce the single string the caller returns to the UI / TTS pipeline."""
    if state.get("cancelled"):
        return {"final_response": "Cancelled."}
    if state.get("needs_clarification"):
        return {"final_response": state.get("clarification_prompt") or
                "I need a bit more detail to do that."}
    if state.get("error"):
        return {"final_response": state["error"]}
    if state.get("success"):
        return {"final_response": state.get("result") or ""}
    return {"final_response": "I couldn't complete that. See the activity log for what happened."}


# ═══════════════════════════════════════════════════════════════
#  Conditional edges
# ═══════════════════════════════════════════════════════════════

def route_after_understand(state: ClarityState) -> str:
    if state.get("cancelled"):
        return "respond"
    if state.get("error"):
        return "respond"            # no model available — say so, plan nothing
    return "plan"


def route_after_validate(state: ClarityState) -> str:
    """
    Three ways out, and the replan loop is bounded by MAX_PLAN_ATTEMPTS so the
    graph always terminates.
    """
    if state.get("cancelled"):
        return "respond"
    if state.get("error"):
        return "respond"            # provider failure — retrying cannot help
    if state.get("needs_clarification"):
        return "respond"
    if state.get("milestone_count", 0) > 0:
        return "execute"
    if state.get("plan_attempts", 0) < MAX_PLAN_ATTEMPTS:
        return "plan"               # bounded correction
    return "respond"                # give up cleanly rather than loop


def route_after_evaluate(state: ClarityState) -> str:
    return "respond"


# ═══════════════════════════════════════════════════════════════
#  Graph construction
# ═══════════════════════════════════════════════════════════════

def build_graph(ctx: GraphRunContext):
    """Compile the workflow. `ctx` is bound into each node via partial()."""
    from langgraph.graph import StateGraph, START, END

    g = StateGraph(ClarityState)

    g.add_node("understand", partial(node_understand, ctx=ctx))
    g.add_node("plan", partial(node_plan, ctx=ctx))
    g.add_node("validate", partial(node_validate, ctx=ctx))
    g.add_node("execute", partial(node_execute, ctx=ctx))
    g.add_node("evaluate", partial(node_evaluate, ctx=ctx))
    g.add_node("respond", partial(node_respond, ctx=ctx))

    g.add_edge(START, "understand")
    g.add_conditional_edges("understand", route_after_understand,
                            {"plan": "plan", "respond": "respond"})
    g.add_edge("plan", "validate")
    g.add_conditional_edges("validate", route_after_validate,
                            {"execute": "execute", "plan": "plan", "respond": "respond"})
    g.add_edge("execute", "evaluate")
    g.add_conditional_edges("evaluate", route_after_evaluate, {"respond": "respond"})
    g.add_edge("respond", END)

    return g.compile()


# ═══════════════════════════════════════════════════════════════
#  Entry point used by MoonwalkAgentV2
# ═══════════════════════════════════════════════════════════════

async def run_graph(agent: Any, user_text: str, context: Any, ws_callback: Any = None) -> tuple:
    """
    Run one request through the graph.

    Returns the same `(response_text, awaiting_reply)` tuple the original path
    returns, so callers (local_server, hub, TTS) need no changes.
    """
    ctx = GraphRunContext(agent=agent, context=context, ws_callback=ws_callback)
    app = build_graph(ctx)

    initial: ClarityState = {
        "user_text": user_text,
        "plan_attempts": 0,
        "executed": False,
        "trace": [],
    }

    t0 = time.time()
    print(f"[Graph] ▶ entering compiled StateGraph")
    final = await app.ainvoke(initial)

    path = " → ".join(l.split(":")[0] for l in final.get("trace", [])) or "(no nodes ran)"
    if final.get("cancelled"):
        outcome = "CANCELLED"
    elif final.get("error"):
        outcome = "ERROR"
    elif final.get("needs_clarification"):
        outcome = "CLARIFICATION"
    elif final.get("success"):
        outcome = "COMPLETED"
    else:
        outcome = "NO-PLAN"
    # Node path and outcome only — never the request text or the response body.
    print(f"[Graph] ■ {outcome} in {time.time() - t0:.2f}s | nodes: {path} → respond "
          f"| executed={bool(final.get('executed'))} plan_attempts={final.get('plan_attempts', 0)}")

    return (final.get("final_response", ""), bool(final.get("awaiting_reply")))
