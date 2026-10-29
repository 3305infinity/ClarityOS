"""
Test Stage 1: Reasoning injection into tool declarations.
"""
import asyncio
import sys
import os

backend_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "backend")
sys.path.insert(0, backend_path)

from tools.registry import registry, _REASONING_EXEMPT_TOOLS


def test_all_tools_have_reasoning():
    """Every non-exempt tool declaration must have a required `reasoning` property."""
    decls = registry.declarations()
    assert len(decls) > 0, "No tools registered"

    for d in decls:
        props = d.get("parameters", {}).get("properties", {})
        req = d.get("parameters", {}).get("required", [])
        if d["name"] in _REASONING_EXEMPT_TOOLS:
            assert "reasoning" not in props, f"{d['name']} is exempt but has reasoning"
        else:
            assert "reasoning" in props, f"{d['name']} missing reasoning property"
            assert "reasoning" in req, f"{d['name']} reasoning not in required list"


def test_exempt_tools_exist():
    """Exempt tools (send_response, await_reply) should not have reasoning."""
    decls = {d["name"]: d for d in registry.declarations()}
    for name in _REASONING_EXEMPT_TOOLS:
        if name in decls:
            props = decls[name].get("parameters", {}).get("properties", {})
            assert "reasoning" not in props, f"Exempt tool {name} should not have reasoning"


def test_execute_strips_reasoning():
    """execute() must silently strip the `reasoning` key so tools don't crash."""
    async def _run():
        # browser_snapshot accepts session_id only — if reasoning leaked it would TypeError
        result = await registry.execute("browser_snapshot", {"session_id": "", "reasoning": "test"})
        assert "Error executing" not in result or "unexpected keyword" not in result
    asyncio.run(_run())


def test_reasoning_property_schema():
    """The injected reasoning property must have correct schema shape."""
    decls = registry.declarations()
    # Pick any non-exempt tool
    for d in decls:
        if d["name"] not in _REASONING_EXEMPT_TOOLS:
            reasoning_schema = d["parameters"]["properties"]["reasoning"]
            assert reasoning_schema["type"] == "string"
            assert "description" in reasoning_schema
            assert len(reasoning_schema["description"]) > 10
            break


if __name__ == "__main__":
    test_all_tools_have_reasoning()
    print("✓ All tools have reasoning")
    test_exempt_tools_exist()
    print("✓ Exempt tools confirmed")
    test_execute_strips_reasoning()
    print("✓ execute() strips reasoning")
    test_reasoning_property_schema()
    print("✓ Reasoning schema correct")
    print("\nAll Stage 1 tests passed!")
