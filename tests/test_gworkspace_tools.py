import asyncio
import importlib.util
import json
import os
import sys
import types


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "backend"))

from runtime_state import runtime_state_store


def _load_gworkspace_module():
    module_name = "gworkspace_tools_under_test"
    path = os.path.join(REPO_ROOT, "backend/tools/gworkspace_tools.py")

    fake_tools_pkg = types.ModuleType("tools")
    fake_tools_pkg.__path__ = []
    fake_registry_mod = types.ModuleType("tools.registry")

    class _FakeRegistry:
        def register(self, **_kwargs):
            def decorator(func):
                return func
            return decorator

    async def _fake_osascript(_script: str) -> str:
        return ""

    fake_registry_mod.registry = _FakeRegistry()
    fake_registry_mod._osascript = _fake_osascript

    old_tools = sys.modules.get("tools")
    old_registry = sys.modules.get("tools.registry")
    sys.modules["tools"] = fake_tools_pkg
    sys.modules["tools.registry"] = fake_registry_mod

    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        assert spec and spec.loader
        spec.loader.exec_module(module)
        return module
    finally:
        if old_tools is not None:
            sys.modules["tools"] = old_tools
        else:
            sys.modules.pop("tools", None)
        if old_registry is not None:
            sys.modules["tools.registry"] = old_registry
        else:
            sys.modules.pop("tools.registry", None)


def test_gdocs_helpers_use_extract_data_targets():
    module = _load_gworkspace_module()
    seen = []

    async def fake_extract(target: str, timeout: float = 4.0):
        seen.append((target, timeout))
        if target == "gdocs_state":
            return json.dumps({
                "url": "https://docs.google.com/document/d/test/edit",
                "title_value": "Quarterly Report",
                "title_visible": True,
                "editor_ready": True,
                "body_length": 120,
            })
        if target.startswith("gdocs_set_title:"):
            return "Quarterly Report"
        if target == "gdocs_focus_editor":
            return "ok"
        if target == "gdocs_click_editor":
            return "TEXTAREA"
        raise AssertionError(f"Unexpected target: {target}")

    async def fail_bridge_js(*_args, **_kwargs):
        raise AssertionError("_bridge_js should not be used by Google Docs helpers")

    module._bridge_extract = fake_extract
    module._bridge_js = fail_bridge_js

    state = asyncio.run(module._gdocs_state_via_bridge())
    assert state["editor_ready"] is True
    assert asyncio.run(module._gdocs_set_title("Quarterly Report")) is True
    assert asyncio.run(module._gdocs_focus_editor()) is True
    assert [target for target, _ in seen] == [
        "gdocs_state",
        seen[1][0],
        "gdocs_focus_editor",
        "gdocs_click_editor",
    ]
    assert seen[1][0].startswith("gdocs_set_title:")


def test_gdocs_create_fallback_never_uses_eval_bridge():
    module = _load_gworkspace_module()
    seen_targets = []
    pasted = []
    runtime_state_store.reset()
    runtime_state_store.start_request(query="egham apartments")

    async def fake_extract(target: str, timeout: float = 4.0):
        seen_targets.append(target)
        if target == "gdocs_state":
            count = seen_targets.count("gdocs_state")
            if count == 1:
                return json.dumps({
                    "url": "https://docs.google.com/document/d/test/edit",
                    "title_value": "",
                    "title_visible": True,
                    "editor_ready": True,
                    "body_length": 0,
                })
            return json.dumps({
                "url": "https://docs.google.com/document/d/test/edit",
                "title_value": "Egham Housing Market Trends",
                "title_visible": True,
                "editor_ready": True,
                "body_length": 512,
            })
        if target.startswith("gdocs_set_title:"):
            return "Egham Housing Market Trends"
        if target == "gdocs_focus_editor":
            return "ok"
        if target == "gdocs_click_editor":
            return "TEXTAREA"
        if target == "gdocs_read_body":
            return "# Egham Housing Market Analysis"
        raise AssertionError(f"Unexpected target: {target}")

    async def fail_bridge_js(*_args, **_kwargs):
        raise AssertionError("_bridge_js should not be used by gdocs_create")

    async def fake_osascript(_script: str) -> str:
        return ""

    async def fake_paste_html(text: str) -> str:
        pasted.append(text)
        return "ok"

    async def fake_sleep(_seconds: float) -> None:
        return None

    module._bridge_extract = fake_extract
    module._bridge_js = fail_bridge_js
    module._osascript = fake_osascript
    module._paste_html = fake_paste_html
    module._load_token = lambda: None
    module.asyncio.sleep = fake_sleep

    result = json.loads(asyncio.run(module.gdocs_create(
        "Egham Housing Market Trends",
        "# Egham Housing Market Analysis",
    )))

    assert result["ok"] is True
    assert pasted == ["# Egham Housing Market Analysis"]
    assert "gdocs_state" in seen_targets
    assert any(target.startswith("gdocs_set_title:") for target in seen_targets)
    assert "gdocs_focus_editor" in seen_targets
    assert "gdocs_click_editor" in seen_targets


def test_gdocs_create_reuses_same_doc_for_repair():
    module = _load_gworkspace_module()
    runtime_state_store.reset()
    runtime_state_store.start_request(query="egham apartments")
    opened_urls = []
    pasted = []

    async def fake_extract(target: str, timeout: float = 4.0):
        if target == "gdocs_state":
            return json.dumps({
                "url": "https://docs.google.com/document/d/reused-doc/edit",
                "title_value": "Egham Apartment Research",
                "title_visible": True,
                "editor_ready": True,
                "body_length": 600,
            })
        if target.startswith("gdocs_set_title:"):
            return "Egham Apartment Research"
        if target == "gdocs_focus_editor":
            return "ok"
        if target == "gdocs_click_editor":
            return "TEXTAREA"
        if target == "gdocs_read_body":
            return "# Housing Analysis Apartment Rental Market in Egham and Englefield Green"
        raise AssertionError(f"Unexpected target: {target}")

    async def fake_osascript(script: str) -> str:
        opened_urls.append(script)
        return ""

    async def fake_paste_html(text: str) -> str:
        pasted.append(text)
        return "ok"

    async def fake_sleep(_seconds: float) -> None:
        return None

    module._bridge_extract = fake_extract
    module._osascript = fake_osascript
    module._paste_html = fake_paste_html
    module._load_token = lambda: None
    module.asyncio.sleep = fake_sleep

    first = json.loads(asyncio.run(module.gdocs_create("Egham Apartment Research", "# Housing Analysis")))
    second = json.loads(asyncio.run(module.gdocs_create("Egham Apartment Research", "# Housing Analysis")))

    assert first["ok"] is True
    assert second["ok"] is True
    assert first["url"] == second["url"] == "https://docs.google.com/document/d/reused-doc/edit"
    assert any("https://docs.new" in script for script in opened_urls)
    assert any("https://docs.google.com/document/d/reused-doc/edit" in script for script in opened_urls)
    assert pasted == ["# Housing Analysis", "# Housing Analysis"]


def test_gdocs_append_reopens_doc_and_verifies_body():
    module = _load_gworkspace_module()
    runtime_state_store.reset()
    opened_urls = []
    pasted = []

    async def fake_extract(target: str, timeout: float = 4.0):
        if target == "gdocs_state":
            return json.dumps({
                "url": "https://docs.google.com/document/d/reused-doc/edit",
                "title_value": "History of Pizza Research",
                "title_visible": True,
                "editor_ready": True,
                "body_length": 800,
            })
        if target == "gdocs_focus_editor":
            return "ok"
        if target == "gdocs_click_editor":
            return "TEXTAREA"
        if target == "gdocs_read_body":
            return "Existing introduction.\n# Pizza history\nOrigins in Naples and precursor flatbreads."
        raise AssertionError(f"Unexpected target: {target}")

    async def fake_osascript(script: str) -> str:
        opened_urls.append(script)
        return ""

    async def fake_paste_html(text: str) -> str:
        pasted.append(text)
        return "ok"

    async def fake_sleep(_seconds: float) -> None:
        return None

    module._bridge_extract = fake_extract
    module._osascript = fake_osascript
    module._paste_html = fake_paste_html
    module._load_token = lambda: None
    module.asyncio.sleep = fake_sleep

    result = json.loads(asyncio.run(module.gdocs_append("reused-doc", "# Pizza history")))

    assert result["ok"] is True
    assert result["doc_id"] == "reused-doc"
    assert any("https://docs.google.com/document/d/reused-doc/edit" in script for script in opened_urls)
    assert pasted == ["\n# Pizza history"]


def test_gdocs_replace_body_uses_visual_recovery_when_readback_stays_empty():
    module = _load_gworkspace_module()
    pasted = []
    screen_calls = []
    read_calls = {"count": 0}

    async def fake_extract(target: str, timeout: float = 4.0):
        if target == "gdocs_focus_editor":
            return "ok"
        if target == "gdocs_click_editor":
            return "TEXTAREA"
        if target == "gdocs_read_body":
            read_calls["count"] += 1
            if read_calls["count"] == 1:
                return ""
            return "# Recovered body about pizza history"
        raise AssertionError(f"Unexpected target: {target}")

    async def fake_osascript(_script: str) -> str:
        return ""

    async def fake_paste_html(text: str) -> str:
        pasted.append(text)
        return "ok"

    async def fake_copy_text() -> str:
        return ""

    async def fake_sleep(_seconds: float) -> None:
        return None

    async def fake_execute(name: str, args: dict) -> str:
        screen_calls.append((name, dict(args)))
        if name == "read_screen":
            return "The writable Google Docs page is around coordinates (420, 360)."
        if name == "click_element":
            return "Clicked at (420, 360)"
        raise AssertionError(f"Unexpected tool: {name}")

    module._bridge_extract = fake_extract
    module._osascript = fake_osascript
    module._paste_html = fake_paste_html
    module._copy_text = fake_copy_text
    module.asyncio.sleep = fake_sleep
    module.registry.execute = fake_execute

    ok = asyncio.run(module._gdocs_replace_body("# Recovered body about pizza history"))

    assert ok is True
    assert pasted == [
        "# Recovered body about pizza history",
        "# Recovered body about pizza history",
    ]
    assert [name for name, _ in screen_calls] == ["read_screen", "click_element"]
