"""
Regression tests for the 2026-10-26 Windows fixes:

  * model configuration precedence and the alias/billed-model mismatch
  * Gemini 429 handling — daily exhaustion vs a short rate limit
  * no repeated calls into an exhausted model across FAST/POWERFUL/FALLBACK
  * actionable UI errors instead of "try rephrasing"
  * deterministic Notepad / File Explorer / Calculator dispatch with no LLM
  * no shell execution reachable from arbitrary user text
  * graceful operation when Gemini or TTS is unavailable
"""
import asyncio
import os
import sys

import pytest

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "backend"))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.environ.setdefault("GEMINI_API_KEY", "test-not-real")

# The repo has a pre-existing import cycle (tools <-> agent): `agent` first.
import agent  # noqa: E402,F401
import tools  # noqa: E402,F401

from agent.direct_actions import WINDOWS_APPS, match_direct_open  # noqa: E402
from providers import models as model_config  # noqa: E402
from providers import quota  # noqa: E402
from providers.errors import (  # noqa: E402
    ProviderErrorKind,
    classify_provider_error,
)


# ── fixtures ────────────────────────────────────────────────────

class FakeAPIError(Exception):
    """Mimics google.genai.errors.APIError: code/status/message/details."""

    def __init__(self, code, body):
        self.code = code
        self.details = {"error": body}
        self.status = body.get("status", "")
        self.message = body.get("message", "")
        super().__init__(f"{code} {self.status}")


def daily_quota_error(billed_model="gemini-3.6-flash", retry="37666s"):
    """The exact shape Google returns for free-tier daily exhaustion."""
    return FakeAPIError(429, {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": ("You exceeded your current quota. * Quota exceeded for metric: "
                    "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
                    f"limit: 20, model: {billed_model}, description: Generate content requests "
                    "per day limit exceeded on Free Tier."),
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{
                 "quotaMetric": "generativelanguage.googleapis.com/generate_content_free_tier_requests",
                 "quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                 "quotaDimensions": {"location": "global", "model": billed_model},
                 "quotaValue": "20"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry},
        ],
    })


def per_minute_quota_error():
    return FakeAPIError(429, {
        "code": 429,
        "status": "RESOURCE_EXHAUSTED",
        "message": "Quota exceeded for quota metric 'Generate requests per minute'.",
        "details": [
            {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
             "violations": [{
                 "quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                 "quotaDimensions": {"model": "gemini-3.6-flash"},
                 "quotaValue": "10"}]},
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "28s"},
        ],
    })


@pytest.fixture(autouse=True)
def clean_quota_state():
    quota.clear_quota_state()
    yield
    quota.clear_quota_state()


@pytest.fixture
def clean_model_env(monkeypatch):
    for var in ("GEMINI_ROUTING_MODEL", "GEMINI_FAST_MODEL",
                "GEMINI_POWERFUL_MODEL", "GEMINI_FALLBACK_MODEL"):
        monkeypatch.delenv(var, raising=False)
    return monkeypatch


# ── 1. model configuration & precedence ─────────────────────────

def test_defaults_used_when_env_unset(clean_model_env):
    assert model_config.fast_model() == model_config.DEFAULT_FAST_MODEL
    assert model_config.powerful_model() == model_config.DEFAULT_POWERFUL_MODEL
    assert model_config.routing_model() == model_config.DEFAULT_ROUTING_MODEL
    assert model_config.fallback_model() == model_config.DEFAULT_FALLBACK_MODEL


def test_env_overrides_default(clean_model_env):
    clean_model_env.setenv("GEMINI_FAST_MODEL", "gemini-3.1-flash-lite")
    assert model_config.fast_model() == "gemini-3.1-flash-lite"
    # Unset tiers keep their defaults — one override must not move the others.
    assert model_config.powerful_model() == model_config.DEFAULT_POWERFUL_MODEL


def test_blank_env_falls_back_to_default(clean_model_env):
    """An empty or whitespace override must not produce an empty model id."""
    clean_model_env.setenv("GEMINI_FAST_MODEL", "   ")
    assert model_config.fast_model() == model_config.DEFAULT_FAST_MODEL


def test_router_constants_match_central_config():
    """router.FAST_MODEL etc. must not drift from providers/models.py."""
    from providers import router
    assert router.FAST_MODEL == model_config.DEFAULT_FAST_MODEL
    assert router.POWERFUL_MODEL == model_config.DEFAULT_POWERFUL_MODEL
    assert router.ROUTING_MODEL == model_config.DEFAULT_ROUTING_MODEL


def test_startup_log_reports_requested_model(clean_model_env):
    """configured_models() is what startup logs print; it must reflect env."""
    clean_model_env.setenv("GEMINI_POWERFUL_MODEL", "gemini-3.1-pro-preview-customtools")
    assert model_config.configured_models()["powerful"] == "gemini-3.1-pro-preview-customtools"


# ── 2. 429 classification ───────────────────────────────────────

def test_daily_quota_is_classified_as_daily():
    err = classify_provider_error(daily_quota_error(), requested_model="gemini-3.5-flash")
    assert err.kind is ProviderErrorKind.QUOTA_DAILY
    assert err.retryable is False


def test_per_minute_quota_is_classified_as_rate_limit():
    err = classify_provider_error(per_minute_quota_error(), requested_model="gemini-3.5-flash")
    assert err.kind is ProviderErrorKind.QUOTA_RATE
    assert err.retryable is True
    assert err.retry_after_s == pytest.approx(28.0)


def test_alias_mismatch_is_captured():
    """Requested id and billed id differ; both must survive into the error."""
    err = classify_provider_error(daily_quota_error(), requested_model="gemini-3.5-flash")
    assert err.requested_model == "gemini-3.5-flash"
    assert err.billed_model == "gemini-3.6-flash"
    assert err.exhausted_model == "gemini-3.6-flash"
    assert "requested=gemini-3.5-flash" in err.log_line()
    assert "billed=gemini-3.6-flash" in err.log_line()


def test_retry_delay_parsing_handles_both_formats():
    assert classify_provider_error(daily_quota_error(retry="37666s")).retry_after_s == pytest.approx(37666.0)
    composite = classify_provider_error(daily_quota_error(retry="10h27m46.5s"))
    assert composite.retry_after_s == pytest.approx(10 * 3600 + 27 * 60 + 46.5)


def test_long_retry_delay_downgrades_to_daily():
    """A 'per minute' bucket asking for hours is a daily bucket in disguise."""
    err = FakeAPIError(429, {
        "code": 429, "status": "RESOURCE_EXHAUSTED", "message": "quota",
        "details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7200s"}],
    })
    assert classify_provider_error(err).kind is ProviderErrorKind.QUOTA_DAILY


def test_other_error_categories():
    def body(code, status, msg="x"):
        return FakeAPIError(code, {"code": code, "status": status, "message": msg})

    assert classify_provider_error(body(403, "PERMISSION_DENIED")).kind is ProviderErrorKind.AUTH
    assert classify_provider_error(body(404, "NOT_FOUND")).kind is ProviderErrorKind.MODEL_UNAVAILABLE
    assert classify_provider_error(body(503, "UNAVAILABLE")).kind is ProviderErrorKind.OVERLOADED
    assert classify_provider_error(ConnectionError("Connection aborted")).kind is ProviderErrorKind.NETWORK


def test_unknown_error_never_raises():
    err = classify_provider_error(ValueError("something odd"))
    assert err.kind is ProviderErrorKind.UNKNOWN
    assert err.user_message()


# ── 3. UI messages are actionable, not "rephrase" ───────────────

def test_quota_message_is_specific_and_mentions_project_scope():
    msg = classify_provider_error(daily_quota_error(), requested_model="gemini-3.5-flash").user_message()
    assert "rephras" not in msg.lower()
    assert "quota" in msg.lower()
    assert "project" in msg.lower()          # a new key in the same project won't help
    assert "gemini-3.6-flash" in msg         # names the model actually billed


def test_auth_message_points_at_the_key_not_the_request():
    msg = classify_provider_error(
        FakeAPIError(403, {"code": 403, "status": "PERMISSION_DENIED", "message": "bad key"})
    ).user_message()
    assert "GEMINI_API_KEY" in msg
    assert "rephras" not in msg.lower()


def test_user_messages_never_leak_secrets():
    """The key is sent as a header, but assert no message echoes it."""
    secret = "AIzaSyFAKE-not-a-real-key-000000000000000"
    err = classify_provider_error(
        FakeAPIError(403, {"code": 403, "status": "PERMISSION_DENIED",
                           "message": f"API key not valid: {secret}"}),
        requested_model="gemini-3.5-flash",
    )
    assert secret not in err.user_message()
    assert secret not in err.log_line()


def test_planner_surfaces_quota_instead_of_rephrase():
    from agent.task_planner import TaskPlanner
    planner = TaskPlanner(provider=None)
    # No failure recorded -> generic prompt is still correct.
    assert "rephras" in planner.planning_failure_message().lower()
    # With a classified failure -> the real reason wins.
    planner.last_provider_error = classify_provider_error(
        daily_quota_error(), requested_model="gemini-3.5-flash")
    msg = planner.planning_failure_message()
    assert "rephras" not in msg.lower()
    assert "quota" in msg.lower()


# ── 4. exhausted models are not retried ─────────────────────────

def test_mark_and_detect_exhaustion():
    err = classify_provider_error(daily_quota_error(), requested_model="gemini-3.5-flash")
    quota.mark_quota_exhausted(err)
    # Both the requested alias and the billed family member are blocked.
    assert quota.is_quota_exhausted("gemini-3.5-flash")
    assert quota.is_quota_exhausted("gemini-3.6-flash")
    assert not quota.is_quota_exhausted("gemini-3.1-flash-lite")


def test_rate_limit_does_not_mark_exhausted():
    quota.mark_quota_exhausted(classify_provider_error(per_minute_quota_error()))
    assert not quota.is_quota_exhausted("gemini-3.6-flash")


def test_expired_cooldown_clears():
    quota.mark_quota_exhausted(
        classify_provider_error(daily_quota_error(retry="0.001s"), requested_model="gemini-3.5-flash"))
    import time
    time.sleep(0.01)
    assert not quota.is_quota_exhausted("gemini-3.5-flash")


def test_provider_does_not_retry_daily_quota():
    """One API call, not four: a spent daily bucket cannot recover mid-request."""
    from providers.gemini import GeminiProvider

    provider = GeminiProvider(api_key="", model="gemini-3.5-flash")
    calls = {"n": 0}

    class FakeModels:
        async def generate_content(self, **kwargs):
            calls["n"] += 1
            raise daily_quota_error()

    class FakeAio:
        models = FakeModels()

    class FakeClient:
        aio = FakeAio()

    provider._client = FakeClient()
    from google.genai import types as genai_types
    provider._genai_types = genai_types

    resp = asyncio.run(provider.generate(
        messages=[{"role": "user", "parts": [{"text": "hi"}]}],
        system_prompt="s", tools=[]))

    assert calls["n"] == 1, f"expected a single attempt, made {calls['n']}"
    assert resp.error_kind is ProviderErrorKind.QUOTA_DAILY
    assert "rephras" not in (resp.error or "").lower()
    # And the exhaustion is published so other tiers skip it.
    assert quota.is_quota_exhausted("gemini-3.6-flash")


def test_router_skips_exhausted_tiers():
    """FAST/POWERFUL/FALLBACK on one exhausted model -> no model offered."""
    from providers.router import ModelRouter
    from providers.gemini import GeminiProvider

    router = ModelRouter()
    shared = GeminiProvider(api_key="", model="gemini-3.5-flash")
    router._router = router._fast = router._powerful = router._fallback = shared
    router._initialized = True

    assert router._usable(shared) is not None
    quota.mark_quota_exhausted(
        classify_provider_error(daily_quota_error(), requested_model="gemini-3.5-flash"))
    assert router._usable(shared) is None

    with pytest.raises(RuntimeError) as excinfo:
        asyncio.run(router.route("open notepad"))
    message = str(excinfo.value)
    assert "quota" in message.lower()
    assert "project" in message.lower()


def test_router_falls_back_to_a_model_with_quota():
    """An exhausted FAST tier must not block a healthy fallback model."""
    from providers.router import ModelRouter
    from providers.gemini import GeminiProvider

    router = ModelRouter()
    router._router = router._fast = router._powerful = GeminiProvider(api_key="", model="gemini-3.5-flash")
    router._fallback = GeminiProvider(api_key="", model="gemini-3.1-flash-lite")
    router._initialized = True

    quota.mark_quota_exhausted(
        classify_provider_error(daily_quota_error(), requested_model="gemini-3.5-flash"))

    decision = asyncio.run(router.route("open notepad"))
    assert decision.model_name == "gemini-3.1-flash-lite"


# ── 5. deterministic Windows dispatch ───────────────────────────

@pytest.mark.parametrize("text,expected", [
    ("open notepad", "Notepad"),
    ("Open Notepad.", "Notepad"),
    ("open notepad on windows", "Notepad"),
    ("open file explorer", "File Explorer"),
    ("open File Explorer", "File Explorer"),
    ("launch windows explorer", "File Explorer"),
    ("open calculator", "Calculator"),
    ("start the calculator please", "Calculator"),
    ("open docker", "Docker Desktop"),
    ("run task manager", "Task Manager"),
])
def test_direct_open_matches_known_apps(text, expected):
    assert match_direct_open(text) == expected


@pytest.mark.parametrize("text", [
    "open notepad and type hello",          # compound — needs planning
    "open chrome then search for cats",
    "open my resume.docx",                  # a file, not an app
    "open the thing",                       # unknown
    "close notepad",                        # not a launch
    "what is notepad",
    "",
    "open",
])
def test_direct_open_declines_everything_else(text):
    assert match_direct_open(text) is None


def test_notepad_does_not_map_to_a_macos_app():
    """Regression: the Windows port must not route Notepad to TextEdit."""
    from agent.world_state import IntentParser
    assert WINDOWS_APPS["notepad"] == "Notepad"
    assert IntentParser.APP_ALIASES["notepad"] == "Notepad"
    assert IntentParser.APP_ALIASES["file explorer"] == "File Explorer"


def test_intent_parser_resolves_windows_apps():
    from agent.task_planner import TaskPlanner
    from agent.world_state import TargetType, WorldState

    planner = TaskPlanner(provider=None)
    for text, expected in [("open notepad on windows", "Notepad"),
                           ("open File Explorer", "File Explorer"),
                           ("open docker", "Docker Desktop")]:
        intent = planner.intent_parser.parse(text, WorldState())
        assert intent.target_type is TargetType.APP, text
        assert intent.target_value == expected, text
        assert not intent.ambiguous, text


# ── 6. no shell execution from arbitrary user text ──────────────

@pytest.mark.parametrize("hostile", [
    "open notepad; rm -rf /",
    "open notepad && shutdown /s",
    "open calc | whoami",
    "open $(whoami)",
    "open `id`",
    "open notepad.exe & format c:",
    "open C:\\Windows\\System32\\cmd.exe /c del *",
    "open powershell -enc ZQBjAGgAbwA=",
])
def test_no_shell_injection_reaches_a_command(hostile):
    """Hostile text must never select a table entry, so nothing is executed."""
    assert match_direct_open(hostile) is None


def test_dispatch_only_emits_canonical_table_values():
    """Whatever matches, the value handed to open_app comes from the table."""
    canonical = set(WINDOWS_APPS.values())
    probes = ["open notepad", "open calc", "open file explorer", "open docker",
              "open vs code", "open cmd", "open ms paint"]
    for probe in probes:
        result = match_direct_open(probe)
        if result is not None:
            assert result in canonical, probe


def test_table_values_contain_no_shell_metacharacters():
    for spoken, canonical in WINDOWS_APPS.items():
        assert not set(canonical) & set(";|&$`<>\n\r"), f"{spoken} -> {canonical}"


# ── 7. graceful degradation ─────────────────────────────────────

def test_agent_direct_action_declines_when_tool_missing():
    """With no open_app registered, the fast path defers instead of erroring."""
    from agent.core_v2 import MoonwalkAgentV2
    from tools import registry as tool_registry

    agent_obj = MoonwalkAgentV2.__new__(MoonwalkAgentV2)   # no heavy __init__
    original = tool_registry.list_names
    tool_registry.list_names = lambda: []
    try:
        result = asyncio.run(agent_obj._try_direct_windows_action("open notepad"))
    finally:
        tool_registry.list_names = original
    assert result is None, "must fall through to the planner, not fail the request"


def test_direct_action_failure_messages_are_actionable():
    from agent.core_v2 import MoonwalkAgentV2
    fail = MoonwalkAgentV2._direct_action_failure
    assert "helper" in fail("Notepad", "ERROR: helper is not connected").lower()
    assert "installed" in fail("Docker Desktop", "ERROR: No installed app matching 'docker'").lower()


def test_tts_failure_does_not_disable_the_agent():
    """
    Missing Google ADC must not silence the assistant or affect planning.

    This previously asserted `enabled is False`: a failed Google client disabled
    the engine outright, so `stream_synthesize` returned zero chunks and nothing
    ever reached the renderer. The engine now stands down from Google and falls
    back to the built-in OS voice, so it stays enabled.
    """
    from voice.tts import TTSEngine
    engine = TTSEngine()
    engine._client = None

    import builtins
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "google.cloud" or name.startswith("google.cloud.texttospeech"):
            raise Exception("Your default credentials were not found")
        return real_import(name, *a, **k)

    builtins.__import__ = fake_import
    try:
        assert engine._get_client() is None
    finally:
        builtins.__import__ = real_import

    assert engine._google_ok is False       # Google stood down…
    assert engine.enabled is True           # …but speech is not disabled
    assert model_config.fast_model()        # model config untouched by TTS
