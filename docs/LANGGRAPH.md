# LangGraph in ClarityOS

> Status: **opt-in**, off by default. Enable with `MOONWALK_USE_LANGGRAPH=1`.

## What it does

ClarityOS already had planning, tool execution, retries and approvals — spread
across `MoonwalkAgentV2.process()`, `TaskPlanner` and `MilestoneExecutor`. The
pipeline existed, but only implicitly: the stages, the retry bounds and the exit
conditions were interleaved with UI streaming and pending-state bookkeeping
inside one ~900-line method.

LangGraph makes that pipeline an **explicit state machine**. It does not replace
any component — each node delegates to the thing that already does the work.

```
          ┌──────────────┐
  START → │  understand  │  ModelRouter.route()  →  tier + model
          └──────┬───────┘
                 │ no model / cancelled ─────────────┐
                 ▼                                   │
          ┌──────────────┐                           │
     ┌───▶│     plan     │  TaskPlanner.create_plan()│
     │    └──────┬───────┘                           │
     │           ▼                                   │
     │    ┌──────────────┐                           │
     │    │   validate   │  (new) is the plan runnable?
     │    └──────┬───────┘                           │
     │   empty   │ runnable      needs clarification │
     └───────────┤               ────────────────────┤
    (bounded:    ▼                                   │
 MAX_PLAN_   ┌──────────────┐                        │
 ATTEMPTS=2) │   execute    │ _execute_milestone_plan│
             └──────┬───────┘   └─ MilestoneExecutor │
                    ▼               └─ registry.execute (guarded)
             ┌──────────────┐                        │
             │   evaluate   │                        │
             └──────┬───────┘                        │
                    ▼                                │
             ┌──────────────┐ ◀──────────────────────┘
             │   respond    │ → (text, awaiting_reply) → END
             └──────────────┘
```

## State

`ClarityState` is a `TypedDict` — typed, serializable, and deliberately small.

| Field | Meaning |
|---|---|
| `user_text` | the request |
| `intent`, `route_tier`, `model_name` | what `understand` decided |
| `plan_summary`, `milestone_count` | what `plan` produced |
| `needs_clarification`, `clarification_prompt` | planner wants more detail |
| `plan_attempts` | bounds the replan loop |
| `executed` | **latch** — true once the plan has had real-world effects |
| `success`, `result`, `awaiting_reply` | execution outcome |
| `cancelled`, `error`, `final_response` | control + output |
| `trace` | node-by-node log, printed at the end of a run |

**Not** in state: the provider, `WorldState`, the websocket callback, the context
snapshot. Those are live handles in `GraphRunContext`, which keeps state JSON-
serializable and keeps credentials and audio out of it entirely.

## Conditional routing

Three decision functions, each total (no dead ends):

- **`route_after_understand`** — cancelled or no usable model → `respond`;
  otherwise → `plan`. A quota-exhausted router short-circuits here, so we never
  plan a request we cannot run.
- **`route_after_validate`** — clarification → `respond`; runnable plan →
  `execute`; empty plan and `plan_attempts < MAX_PLAN_ATTEMPTS` → `plan` (one
  bounded correction); otherwise → `respond`. The counter is what guarantees
  termination.
- **`route_after_evaluate`** — → `respond`. Deliberately *not* a retry edge:
  `execute` has already caused side effects.

## Approval and cancellation

**Approvals are untouched.** The graph never calls a tool. `execute` delegates to
`_execute_milestone_plan` → `MilestoneExecutor` → `tool_registry.execute`, which
is already wrapped by `VoiceToolGuard` (Electron) or `Hub.install_guard`
(website). The `PolicyEngine` and `ApprovalBroker` run exactly as before, and a
denied approval returns `policy.user_denied` to the executor without the action
running. The LLM cannot authorise its own calls — nothing in graph state
participates in the approval decision.

**Cancellation** is checked at every node boundary via
`runtime_state_store.is_request_cancelled()`. A cancelled run routes straight to
`respond` and never reaches `execute`.

**No duplicate side effects.** `execute` sets `executed = True` *before* awaiting
the call. Re-entering the node is a no-op, and there is no edge back into it.
`_try_langgraph` only falls back to the original path on an exception, which can
only be raised before the side-effecting call.

## Reused components

| Node | Delegates to | Why |
|---|---|---|
| `understand` | `MoonwalkAgentV2._build_world_state`, `ModelRouter.route` | perception + model/tier selection already exist |
| `plan` | `TaskPlanner.create_plan`, `_select_tool_surface` | milestone planning already exists |
| `validate` | — | new, and the only genuinely new logic |
| `execute` | `_execute_milestone_plan` → `MilestoneExecutor` → `tool_registry` | keeps one execution path, so approvals and `SubAgentManager` still apply |
| `evaluate` / `respond` | — | pure state inspection |

No second model client, no second tool registry, no competing agent.

## Enabling it

```powershell
# backend\.env  — default is OFF
MOONWALK_USE_LANGGRAPH=1
```

| Value | Behaviour |
|---|---|
| unset / `0` / `false` | **Default.** Original agent path. Graph code never runs. |
| `1` / `true` / `yes` / `on` | Fresh requests go through the graph. |

If `langgraph` is not installed the flag is ignored with a log line and the
original path runs — a missing optional package never blocks startup.

**Scope:** only fresh requests. A follow-up to a pending plan or a suspended
`await_reply` execution carries state the graph does not model, so those always
take the original path. Small-talk and the deterministic "open notepad" fast path
run before the graph and are unaffected.

## Worked example — "open notepad"

```
[Graph] understand: intent=open tier=FAST model=gemini-3.5-flash
[Graph] plan: attempt=1 milestones=1 clarify=False
[Graph] validate: plan is runnable
[Graph] execute: done awaiting_reply=False
[Graph] evaluate: success=True
[Graph] ── run complete in 1.84s (understand → plan → validate → execute → evaluate)
```

1. **understand** — `_build_world_state` snapshots the active app and window;
   `ModelRouter.route()` classifies it as trivial and returns the FAST provider.
   State gains `intent=open`, `route_tier=FAST`.
2. **plan** — `TaskPlanner.create_plan()` returns a `MilestonePlan` with one
   milestone (*goal: open Notepad, success_signal: window is in front*).
   `milestone_count=1`.
3. **validate** — one milestone, no clarification → `route_after_validate`
   returns `"execute"`.
4. **execute** — latches `executed=True`, then calls `_execute_milestone_plan`.
   The executor's micro-loop picks `open_app`, which goes through
   `registry.execute` → the policy guard. `open_app` is a capability tool and
   not in `ALWAYS_SENSITIVE`, so with `desktop: allow` it runs without a prompt.
   Had it been `bolna_call`, the guard would have raised an approval request and
   blocked until the user answered.
5. **evaluate** — `success=True`, no error.
6. **respond** — returns `("Opened Notepad.", False)` — the same tuple shape the
   original path returns, so the TTS pipeline and websocket callers are unchanged.

Contrast: if step 2 had returned an empty plan, `validate` would route back to
`plan` once, and on a second empty plan route to `respond` instead of looping.
