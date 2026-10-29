from __future__ import annotations

from ..models import ScenarioDefinition


SCENARIOS: dict[str, ScenarioDefinition] = {
    "open_notes": ScenarioDefinition(
        name="open_notes",
        task="Open Notes and make sure it is the active app.",
        preconditions=["Notes is installed on the Mac."],
        success_checks=["Notes is frontmost."],
        max_steps=4,
        timeout_s=45,
        seed_context={"target_app": "Notes"},
    ),
    "whatsapp_message": ScenarioDefinition(
        name="whatsapp_message",
        task="Find a contact chat in WhatsApp and send a short greeting.",
        preconditions=["WhatsApp is installed and logged in.", "A visible chat list is available."],
        success_checks=["The target chat is open.", "A short message is entered and sent."],
        max_steps=8,
        timeout_s=120,
        seed_context={"target_app": "WhatsApp", "contact_name": "Kris", "message_text": "Hey Kris, checking the experiment harness."},
    ),
    "search_field_confirm": ScenarioDefinition(
        name="search_field_confirm",
        task="Focus a search field, enter a query, and confirm the first result.",
        preconditions=["The active app exposes a search field or search affordance."],
        success_checks=["A search field is focused.", "The query is entered.", "The first result is confirmed."],
        max_steps=7,
        timeout_s=90,
        seed_context={"query": "Kris"},
    ),
    "menu_shortcut_action": ScenarioDefinition(
        name="menu_shortcut_action",
        task="Trigger a simple menu or shortcut-driven action in the active app.",
        preconditions=["The active app supports a visible shortcut or standard menu action."],
        success_checks=["A shortcut or menu action is executed.", "The app state changes accordingly."],
        max_steps=5,
        timeout_s=60,
        seed_context={"preferred_shortcut": "command+f"},
    ),
    "scroll_navigation": ScenarioDefinition(
        name="scroll_navigation",
        task="Navigate within the active window by scrolling until additional content is revealed.",
        preconditions=["The active window contains scrollable content."],
        success_checks=["The view scrolls.", "More content is visible after scrolling."],
        max_steps=5,
        timeout_s=60,
    ),
    "low_level_drag": ScenarioDefinition(
        name="low_level_drag",
        task="Use low-level pointer control to move the mouse and perform a drag interaction.",
        preconditions=["A draggable surface or selection area is visible."],
        success_checks=["The pointer moves.", "A drag action is completed."],
        max_steps=6,
        timeout_s=60,
        seed_context={"drag_start": {"x": 320, "y": 240}, "drag_end": {"x": 520, "y": 240}},
    ),
}

SCENARIO_SETS: dict[str, list[str]] = {
    "core_desktop": [
        "open_notes",
        "whatsapp_message",
        "search_field_confirm",
        "menu_shortcut_action",
        "scroll_navigation",
        "low_level_drag",
    ]
}


def get_scenario(name: str) -> ScenarioDefinition:
    try:
        return SCENARIOS[name]
    except KeyError as exc:
        raise KeyError(f"Unknown experiment scenario '{name}'") from exc


def get_scenario_set(name: str) -> list[ScenarioDefinition]:
    scenario_names = SCENARIO_SETS.get(name)
    if scenario_names is None:
        raise KeyError(f"Unknown scenario set '{name}'")
    return [get_scenario(scenario_name) for scenario_name in scenario_names]


def make_adhoc_scenario(task: str) -> ScenarioDefinition:
    return ScenarioDefinition(
        name="adhoc_task",
        task=task,
        preconditions=[],
        success_checks=["Agent reports that the task is complete."],
        max_steps=8,
        timeout_s=90,
    )

