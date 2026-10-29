"""
Swap the macOS-only desktop primitives for proxies that talk to the Windows helper.

The existing agent code (click_ui, type_in_field, glance, perception…) calls module-level
functions in tools.mac_tools / agent.perception. We keep all of that high-level logic and only
replace the OS-touching primitives, so the SPAV agent works unchanged.
"""
from __future__ import annotations

import asyncio
import base64
import inspect
import io
import os
import tempfile
import time
from typing import Any, Optional

from hub.helper_link import HelperLink, HelperError, NOT_CONNECTED_MSG
from providers.models import fast_model, powerful_model

# Tool name -> executed by the helper with the same arguments.
PROXIED_TOOLS = [
    "open_app", "close_window", "quit_app", "type_text", "run_shortcut", "get_running_apps",
    "set_volume", "click_element", "clipboard_ops", "get_ui_tree", "press_key", "mouse_action",
    "window_manager", "capture_region_screenshot", "copy_image_to_clipboard", "run_shell",
]
# macOS-only tools with no Windows equivalent (AppleScript / iMessage) — removed from the registry.
REMOVED_TOOLS = ["send_imessage", "browser_click", "browser_fill"]

_OSASCRIPT_MSG = "ERROR: AppleScript is not available on Windows."


async def _osascript_stub(script: str) -> str:
    return _OSASCRIPT_MSG


def _make_tool_proxy(link: HelperLink, name: str, original):
    sig = inspect.signature(original)

    async def proxy(*args, **kwargs):
        try:
            bound = sig.bind(*args, **kwargs)
        except TypeError as e:
            return f"ERROR: invalid arguments for {name}: {e}"
        bound.apply_defaults()
        try:
            result = await link.call(name, dict(bound.arguments))
        except HelperError as e:
            return str(e) if str(e).startswith("ERROR") else f"ERROR: {e}"
        return result if isinstance(result, str) else str(result)

    proxy.__name__ = getattr(original, "__name__", name)
    proxy.__doc__ = getattr(original, "__doc__", None)
    return proxy


async def _safe_call(link: HelperLink, tool: str, args: dict, default: Any, timeout: float = 3.0) -> Any:
    if not link.connected:
        return default
    try:
        return await link.call(tool, args, timeout=timeout)
    except Exception:
        return default


async def capture_screen(link: HelperLink, timeout: float = 10.0) -> Optional[dict]:
    """Ask the helper for a screenshot. Returns {jpeg_b64,width,height,origin_x,origin_y} or None."""
    res = await _safe_call(link, "capture_screen", {}, None, timeout=timeout)
    return res if isinstance(res, dict) and res.get("jpeg_b64") else None


def _draw_grid(jpeg_bytes: bytes) -> bytes:
    """Coordinate grid overlay (same idea as the macOS read_screen) so the vision model can read pixels."""
    from PIL import Image, ImageDraw, ImageFont
    img = Image.open(io.BytesIO(jpeg_bytes)).convert("RGBA")
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    w, h = img.size
    font = ImageFont.load_default()
    for x in range(100, w, 100):
        d.line([(x, 0), (x, h)], fill=(255, 50, 50, 60), width=1)
        d.text((x + 2, 2), str(x), fill=(255, 50, 50, 140), font=font)
    for y in range(100, h, 100):
        d.line([(0, y), (w, y)], fill=(255, 50, 50, 60), width=1)
        d.text((2, y + 2), str(y), fill=(255, 50, 50, 140), font=font)
    out = io.BytesIO()
    Image.alpha_composite(img, overlay).convert("RGB").save(out, format="JPEG", quality=85)
    return out.getvalue()


def install(link: HelperLink, registry) -> None:
    """Patch the tool registry and the perception / mac_tools modules. Safe to call once at start-up."""
    import agent.perception as perception
    import tools.mac_tools as mac
    import tools.registry as reg_mod

    # 1. tool proxies (registry entry + module attribute so internal callers use them too)
    for name in PROXIED_TOOLS:
        tool = registry._tools.get(name)
        if tool is None:
            continue
        original = getattr(tool, "_mw_orig_func", None) or tool.func   # idempotent re-install
        tool._mw_orig_func = original
        proxy = _make_tool_proxy(link, name, original)
        tool.func = proxy
        if hasattr(mac, name):
            setattr(mac, name, proxy)
    for name in REMOVED_TOOLS:
        registry._tools.pop(name, None)

    # 2. AppleScript helper -> clear error, everywhere it was imported by name
    reg_mod._osascript = _osascript_stub
    for modname in ("tools.mac_tools", "tools.browser_tools", "tools.gworkspace_tools", "tools"):
        m = __import__(modname, fromlist=["x"])
        if hasattr(m, "_osascript"):
            m._osascript = _osascript_stub

    # 3. perception layer (fast, never raises, empty when the helper is away)
    async def get_active_app() -> str:
        return await _safe_call(link, "get_active_app", {}, "")

    async def get_window_title() -> str:
        return await _safe_call(link, "get_window_title", {}, "")

    async def get_clipboard() -> Optional[str]:
        return (await _safe_call(link, "get_clipboard", {}, None, timeout=2.0)) or None

    async def _none(*_a, **_k):
        return None

    async def capture_screenshot() -> Optional[str]:
        shot = await capture_screen(link)
        if not shot:
            return None
        d = os.path.join(tempfile.gettempdir(), "moonwalk")
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f"screen_{int(time.time())}.jpg")
        with open(path, "wb") as f:
            f.write(base64.b64decode(shot["jpeg_b64"]))
        return path

    perception.get_active_app = get_active_app
    perception.get_window_title = get_window_title
    perception.get_clipboard = get_clipboard
    perception.get_browser_url = _none            # the Chrome extension bridge supplies URL / DOM / selection
    perception.get_browser_selected_text = _none
    perception.get_browser_page_content = _none
    perception.capture_screenshot = capture_screenshot

    # 4. vision tools: screenshot comes from the helper, analysis happens in the hub
    async def read_screen(question: str = "") -> str:
        shot = await capture_screen(link)
        if not shot:
            return NOT_CONNECTED_MSG if not link.connected else "ERROR: Failed to capture screenshot"
        try:
            jpeg = _draw_grid(base64.b64decode(shot["jpeg_b64"]))
        except Exception as e:
            return f"ERROR processing screenshot: {e}"
        w, h = shot.get("width"), shot.get("height")
        prompt = question or ("Describe what's on this screen. Include any visible text, buttons, UI elements, "
                              "error messages, dialogs, login screens, CAPTCHAs and the overall layout. Be concise but thorough.")
        prompt += (f" The image is {w} x {h} pixels and pixel positions correspond 1:1 to click coordinates."
                   " It has a coordinate grid overlay with labeled X values along the top and Y values along the left;"
                   " output PRECISE (X, Y) coordinates for each clickable element you mention.")
        try:
            from google import genai
            from google.genai import types
            client = genai.Client()
            resp = client.models.generate_content(
                model=powerful_model(),
                contents=[types.Content(role="user", parts=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(data=jpeg, mime_type="image/jpeg")])])
            text = "".join(p.text + "\n" for p in (resp.candidates[0].content.parts if resp.candidates else []) if p.text)
            return text.strip()[:3000] or "Could not analyze screenshot"
        except Exception as e:
            return f"ERROR analyzing screen: {str(e)[:200]}"

    async def _fast_visual_locate(description: str, hint: str = "") -> Optional[tuple]:
        import re
        shot = await capture_screen(link)
        if not shot:
            return None
        try:
            from google import genai
            from google.genai import types
            client = genai.Client()
            prompt = (f"Find the UI element best described as: '{description}'"
                      f"{(' (context: ' + hint + ')') if hint else ''}. Image is {shot.get('width')}x{shot.get('height')}. "
                      "Return ONLY the pixel coordinates of its CENTER as two integers: x,y. If you cannot find it, return exactly: NOTFOUND")
            resp = client.models.generate_content(
                model=fast_model(),
                contents=[types.Content(role="user", parts=[
                    types.Part.from_text(text=prompt),
                    types.Part.from_bytes(data=base64.b64decode(shot["jpeg_b64"]), mime_type="image/jpeg")])])
            text = "".join(p.text for p in (resp.candidates[0].content.parts if resp.candidates else []) if p.text).strip()
            if "NOTFOUND" in text.upper():
                return None
            m = re.search(r"(\d{1,5})\s*[,;x]\s*(\d{1,5})", text)
            return (int(m.group(1)), int(m.group(2))) if m else None
        except Exception as e:
            print(f"[VisualLocate] ⚠ {e}")
            return None

    registry._tools["read_screen"].func = read_screen
    mac.read_screen = read_screen
    mac._fast_visual_locate = _fast_visual_locate
    try:
        import tools.gworkspace_tools as gw
        gw._fast_visual_locate = _fast_visual_locate  # imported by name inside functions at call time
    except Exception:
        pass

    # 5. open_url: let the helper open it in the user's browser, else open locally (hub runs on the same PC)
    registry._tools["open_url"]._mw_orig_func = getattr(registry._tools["open_url"], "_mw_orig_func", None) or registry._tools["open_url"].func

    async def open_url(url: str, force_new_tab: bool = False, browser: str = "") -> str:
        if not (url.startswith("http://") or url.startswith("https://")):
            return "ERROR: only http(s) URLs can be opened"

        # An explicitly requested browser is handled here and never handed to
        # the helper or to webbrowser.open(): both end at the *system default*,
        # which is how "open it in Chrome" opened Edge. This override used to
        # drop the argument entirely, so the backend tool's targeting never ran
        # on Windows — where this proxy is always installed.
        target = (browser or "").strip().lower()
        if target:
            try:
                from tools.browser_target import launch_url, verify_foreground
            except Exception as exc:
                return f"ERROR: browser targeting unavailable ({exc.__class__.__name__})"
            ok, message = launch_url(target, url)
            if not ok:
                return f"ERROR: {message}"
            await asyncio.sleep(1.2)
            confirmed, detail = verify_foreground(target)
            if confirmed:
                return f"{message} Confirmed in front: {detail[:120]}"
            return (f"{message} I could not confirm it came to the foreground "
                    f"({detail}).")

        if link.connected and "open_url" in link.tools:
            try:
                return str(await link.call("open_url", {"url": url}))
            except HelperError as e:
                return f"ERROR: {e}"
        import webbrowser
        webbrowser.open(url)
        return f"Opened {url} in the default browser"

    registry._tools["open_url"].func = open_url
    mac.open_url = open_url
