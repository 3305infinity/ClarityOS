"""
Moonwalk — Google Workspace Tools
===================================
Direct, high-level tools for Google Docs, Sheets, Slides, Drive, Gmail,
and Calendar.

Zero-setup design
~~~~~~~~~~~~~~~~~
Every tool works immediately for any user who is already signed into
Google in Chrome — NO OAuth, NO API keys, NO extra configuration needed.

Execution channels (tried in priority order):

1. **Browser automation** (always available) — Chrome JS injection via
   AppleScript + clipboard paste + keyboard shortcuts. Works because the
   user is already signed in to Google in their browser.

2. **Google REST API** (optional speed enhancement) — if the user has set
   up an OAuth2 token at ``~/.moonwalk/gcloud_token.json`` the tool uses
   the official API for faster, more reliable, bulk operations (100s of
   rows, background writes). This is NEVER required.

The agent just calls ``gsheets_write`` or ``gdocs_create`` — the tool
picks the best available channel automatically.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import webbrowser
from typing import Optional, List

from runtime_state import runtime_state_store
from tools.registry import registry, _osascript


# ═══════════════════════════════════════════════════════════════
#  Internal helpers
# ═══════════════════════════════════════════════════════════════

_TOKEN_PATH = os.path.expanduser("~/.moonwalk/gcloud_token.json")
_VISION_COORD_RE = re.compile(r"\(?\b(?:x\s*[:=]\s*)?(\d{1,4})\s*,\s*(?:y\s*[:=]\s*)?(\d{1,4})\b\)?", re.I)


def _load_token() -> Optional[str]:
    """Load the OAuth2 access token if available."""
    if not os.path.exists(_TOKEN_PATH):
        return None
    try:
        with open(_TOKEN_PATH, "r") as f:
            data = json.load(f)
        return data.get("access_token")
    except Exception:
        return None


async def _gapi(method: str, url: str, body: dict | None = None,
                token: str | None = None, timeout: float = 15.0) -> dict:
    """Lightweight async wrapper around Google REST APIs."""
    import httpx  # available in the project venv

    headers = {"Content-Type": "application/json"}
    tok = token or _load_token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"

    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        if method.upper() == "GET":
            resp = await client.get(url, headers=headers)
        elif method.upper() == "POST":
            resp = await client.post(url, headers=headers, json=body or {})
        elif method.upper() == "PUT":
            resp = await client.put(url, headers=headers, json=body or {})
        elif method.upper() == "PATCH":
            resp = await client.patch(url, headers=headers, json=body or {})
        elif method.upper() == "DELETE":
            resp = await client.delete(url, headers=headers)
        else:
            return {"error": f"Unsupported HTTP method: {method}"}

    try:
        return resp.json()
    except Exception:
        return {"status": resp.status_code, "body": resp.text[:2000]}


def _extract_doc_id(url_or_id: str) -> str:
    """Extract a Google Doc/Sheet/Slide ID from a full URL or raw ID."""
    m = re.search(r"/d/([a-zA-Z0-9_-]{20,})", url_or_id)
    if m:
        return m.group(1)
    # Already a bare ID
    if re.match(r"^[a-zA-Z0-9_-]{20,}$", url_or_id):
        return url_or_id
    return url_or_id


async def _chrome_js(script: str) -> str:
    """Execute a JavaScript snippet in Chrome's active tab via AppleScript."""
    escaped = script.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return await _osascript(
        f'tell application "Google Chrome" to execute active tab of front window javascript "{escaped}"'
    )


async def _bridge_js(script: str, timeout: float = 4.0) -> str | None:
    """Execute a JavaScript snippet in Chrome via the BrowserBridge WebSocket.
    Returns the evaluated result as a string, or None if the bridge is disconnected or times out."""
    from browser.bridge import browser_bridge
    from browser.models import ActionRequest
    from browser.store import browser_store

    if not browser_bridge.is_connected():
        print("[_bridge_js] Bridge NOT connected — skipping")
        return None

    session_id = browser_bridge.connected_session_id()
    if not session_id:
        snapshot = browser_store.get_snapshot()
        if snapshot:
            session_id = snapshot.session_id
        if not session_id:
            print("[_bridge_js] No session_id available — skipping")
            return None

    print(f"[_bridge_js] Sending evaluate_js (session={session_id[:16]}…)")

    request = ActionRequest(
        action="evaluate_js",
        ref_id="",
        session_id=session_id,
        text=script,
        timeout=timeout,
    )
    
    queued = browser_bridge.queue_action(request)
    if not queued.ok:
        print(f"[_bridge_js] Queue FAILED: {queued.message}")
        return None

    print(f"[_bridge_js] Queued action_id={queued.action_id}, waiting up to {timeout}s…")
    started = time.time()
    while time.time() - started < timeout:
        result = browser_bridge.latest_action_result(queued.action_id)
        if result:
            if result.ok:
                print(f"[_bridge_js] ✓ Got result ({len(result.details.get('result', ''))} chars)")
                return result.details.get("result", "")
            
            error_msg = result.details.get("error", "")
            print(f"[_bridge_js] ✗ Action failed: {result.message}. Error: {error_msg}")
            return None
        await asyncio.sleep(0.1)
    print("[_bridge_js] ✗ Timed out waiting for result")
    return None


async def _bridge_extract(target: str, timeout: float = 4.0) -> str | None:
    """Safely extract predefined data directly from Chrome DOM via BrowserBridge (bypasses CSP unsafe-eval).
    target: 'gdocs', 'gcal', or 'body'."""
    from browser.bridge import browser_bridge
    from browser.models import ActionRequest
    from browser.store import browser_store

    if not browser_bridge.is_connected():
        print("[_bridge_extract] Bridge NOT connected — skipping")
        return None

    session_id = browser_bridge.connected_session_id()
    if not session_id:
        snapshot = browser_store.get_snapshot()
        if snapshot:
            session_id = snapshot.session_id
        if not session_id:
            print("[_bridge_extract] No session_id available — skipping")
            return None

    print(f"[_bridge_extract] Sending extract_data (target={target}, session={session_id[:16]}…)")

    request = ActionRequest(
        action="extract_data",
        ref_id="",
        session_id=session_id,
        text=target,
        timeout=timeout,
    )
    
    queued = browser_bridge.queue_action(request)
    if not queued.ok:
        print(f"[_bridge_extract] Queue FAILED: {queued.message}")
        return None

    print(f"[_bridge_extract] Queued action_id={queued.action_id}, waiting up to {timeout}s…")
    started = time.time()
    while time.time() - started < timeout:
        result = browser_bridge.latest_action_result(queued.action_id)
        if result:
            if result.ok:
                print(f"[_bridge_extract] ✓ Got result ({len(result.details.get('result', ''))} chars)")
                return result.details.get("result", "")
            
            error_msg = result.details.get("error", "")
            print(f"[_bridge_extract] ✗ Action failed: {result.message}. Error: {error_msg}")
            return None
        await asyncio.sleep(0.1)
    print("[_bridge_extract] ✗ Timed out waiting for result")
    return None


async def _paste_text(text: str) -> str:
    """Copy text to clipboard and paste via Cmd+V (reliable for canvas editors)."""
    proc = await asyncio.create_subprocess_exec(
        "pbcopy",
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate(input=text.encode("utf-8"))

    await asyncio.sleep(0.1)
    return await _osascript(
        'tell application "System Events" to keystroke "v" using command down'
    )


async def _copy_text() -> str:
    """Copy text from the active Google Chrome window (useful for Canvas-based Google Docs).
    This sends Cmd+A and Cmd+C to the window and reads the clipboard."""
    await _osascript('tell application "Google Chrome" to activate')
    await asyncio.sleep(0.4)
    
    await _osascript('tell application "System Events" to keystroke "a" using command down')
    await asyncio.sleep(0.4)
    
    await _osascript('tell application "System Events" to keystroke "c" using command down')
    await asyncio.sleep(0.4)
    
    await _osascript('tell application "System Events" to key code 124') # right arrow
    await asyncio.sleep(0.2)
    
    proc = await asyncio.create_subprocess_exec(
        "pbpaste",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return stdout.decode("utf-8").strip()


def _md_to_html(text: str) -> str:
    """Convert Markdown text to HTML. Falls back to <pre> wrapping if the
    markdown library is unavailable."""
    try:
        import markdown
        return markdown.markdown(
            text,
            extensions=["tables", "fenced_code", "nl2br"],
        )
    except ImportError:
        import html as html_mod
        return f"<pre>{html_mod.escape(text)}</pre>"


async def _paste_html(text: str) -> str:
    """Convert Markdown *text* to HTML and place both the HTML and plain-text
    representations on the macOS clipboard (public.html + public.utf8-plain-text),
    then paste via Cmd+V.  Google Docs will pick up the rich HTML version."""
    html_content = _md_to_html(text)
    # Use a small Python one-liner to set both UTI types on the pasteboard.
    # This avoids needing a compiled Swift helper.
    py_script = (
        "import AppKit, Foundation\n"
        "pb = AppKit.NSPasteboard.generalPasteboard()\n"
        "pb.clearContents()\n"
        "pb.setData_forType_(Foundation.NSData.dataWithBytes_length_("
        "html_bytes, len(html_bytes)), AppKit.NSPasteboard.PasteboardType.html)\n"
        "pb.setString_forType_(plain, AppKit.NSPasteboard.PasteboardType.string)\n"
    )
    # Build the full script with the content embedded safely
    import base64
    html_b64 = base64.b64encode(html_content.encode("utf-8")).decode("ascii")
    plain_b64 = base64.b64encode(text.encode("utf-8")).decode("ascii")
    full_script = (
        "import AppKit, Foundation, base64\n"
        f"html_bytes = base64.b64decode('{html_b64}')\n"
        f"plain = base64.b64decode('{plain_b64}').decode('utf-8')\n"
        "pb = AppKit.NSPasteboard.generalPasteboard()\n"
        "pb.clearContents()\n"
        "pb.setData_forType_(Foundation.NSData.dataWithBytes_length_("
        "html_bytes, len(html_bytes)), AppKit.NSPasteboard.PasteboardType.html)\n"
        "pb.setString_forType_(plain, AppKit.NSPasteboard.PasteboardType.string)\n"
    )
    proc = await asyncio.create_subprocess_exec(
        "python3", "-c", full_script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    await proc.communicate()
    await asyncio.sleep(0.15)
    return await _osascript(
        'tell application "System Events" to keystroke "v" using command down'
    )


async def _wait_for_gdocs_ready(timeout: float = 15.0) -> dict:
    started = time.time()
    while time.time() - started < timeout:
        snapshot = await _gdocs_state_via_bridge()
        if snapshot.get("url", "").startswith("https://docs.google.com/document/d/"):
            return snapshot
        await asyncio.sleep(0.5)
    return {}


async def _gdocs_state_via_bridge() -> dict:
    raw = await _bridge_extract("gdocs_state", timeout=4.0)
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


async def _wait_for_kix_rendered(timeout: float = 5.0) -> bool:
    """Poll gdocs_state until the kix editor canvas has initialised (editor_ready=True).
    On fresh documents kix renders asynchronously — reading the body before it is
    ready returns an empty string even though the doc is open."""
    started = time.time()
    while time.time() - started < timeout:
        state = await _gdocs_state_via_bridge()
        if state.get("editor_ready"):
            await asyncio.sleep(0.3)  # brief settle after canvas init
            return True
        await asyncio.sleep(0.25)
    print("[_wait_for_kix_rendered] ⚠ Timed out waiting for kix editor to render")
    return False


async def _gdocs_set_title(title: str) -> bool:
    """Set the Google Doc title.  DOM fast-path → vision fallback."""
    if not title:
        return True
    # ── Fast path: DOM React-style value setter ──
    title_b64 = __import__("base64").b64encode(title.encode("utf-8")).decode("ascii")
    result = await _bridge_extract(f"gdocs_set_title:{title_b64}", timeout=4.0)
    if result and title in str(result):
        print(f"[_gdocs_set_title] ✓ Title set via DOM bridge")
        return True
    # ── Reliable path: vision-based title entry ──
    print("[_gdocs_set_title] DOM title setter failed — using vision")
    return await _vision_gdocs_set_title(title)


async def _gdocs_focus_editor() -> bool:
    """Focus the Google Docs editor.  DOM fast-path → vision fallback.

    The DOM selectors (kix-page, kix-appview-editor …) break whenever
    Google ships an internal CSS rename.  Vision is immune to that.
    """
    # ── Fast path: DOM-based focus via bridge (0 latency) ──
    result = await _bridge_extract("gdocs_focus_editor", timeout=4.0)
    if result and result.strip() and result.strip() != "no-editor":
        click_result = await _bridge_extract("gdocs_click_editor", timeout=4.0)
        if click_result and click_result.strip() and click_result.strip() != "no-editor":
            print("[_gdocs_focus_editor] ✓ Focused via DOM bridge")
            return True

    # ── Reliable path: vision-based click on the editor page ──
    print("[_gdocs_focus_editor] DOM focus failed — using vision")
    await _osascript('tell application "Google Chrome" to activate')
    await asyncio.sleep(0.3)
    return await _gdocs_focus_editor_with_vision()


def _normalize_doc_text(text: str) -> str:
    return " ".join(str(text or "").split()).strip().lower()


def _extract_doc_id_from_url(url: str) -> str:
    match = re.search(r"/document/d/([a-zA-Z0-9_-]{20,})", str(url or ""))
    return match.group(1) if match else ""


async def _gdocs_read_body() -> str:
    """Read the body of the current Google Doc.

    Channels (tried in order):
    1. DOM selectors via bridge  — fast, 0 tokens, but breaks on canvas docs
    2. Clipboard extraction      — reliable for most docs
    3. Vision OCR                 — always works, ~500 tokens
    """
    # ── Channel 1: DOM bridge extraction ──
    await _wait_for_kix_rendered(timeout=5.0)
    raw = await _bridge_extract("gdocs_read_body", timeout=4.0)
    if raw and len(_normalize_doc_text(raw)) >= 12:
        print(f"[_gdocs_read_body] ✓ Read {len(raw)} chars via DOM bridge")
        return raw

    # ── Channel 2: Clipboard (Cmd+A → Cmd+C → pbpaste) ──
    await _gdocs_focus_editor()
    await asyncio.sleep(0.2)
    clipboard_text = await _copy_text()
    if clipboard_text and len(_normalize_doc_text(clipboard_text)) >= 12:
        print(f"[_gdocs_read_body] ✓ Read {len(clipboard_text)} chars via clipboard")
        return clipboard_text

    # ── Channel 3: Vision OCR fallback (canvas-based docs) ──
    print("[_gdocs_read_body] DOM + clipboard both empty — falling back to vision")
    vision_text = await _vision_gdocs_read_content()
    return vision_text or clipboard_text or ""


def _body_matches_expected(actual: str, expected: str) -> bool:
    actual_norm = _normalize_doc_text(actual)
    expected_norm = _normalize_doc_text(expected)
    if not expected_norm:
        return True
    if not actual_norm:
        return False
    if expected_norm[:120] and expected_norm[:120] in actual_norm:
        return True
    expected_tokens = [token for token in re.split(r"[^a-z0-9]+", expected_norm) if len(token) > 4]
    if not expected_tokens:
        return len(actual_norm) >= min(40, len(expected_norm))
    overlap = sum(1 for token in expected_tokens[:12] if token in actual_norm)
    return overlap >= min(4, max(2, len(expected_tokens[:12]) // 3))


async def _gdocs_replace_body(body: str) -> bool:
    """Replace the Google Doc body.  Tries DOM focus first, then vision focus,
    with vision-based content verification for maximum reliability."""
    if not body:
        return True
    await _osascript('tell application "Google Chrome" to activate')
    await asyncio.sleep(0.2)

    for attempt, focus_fn in enumerate([
        _gdocs_focus_editor,              # attempt 0: DOM fast-path → vision fallback
        _gdocs_focus_editor_with_vision,  # attempt 1: pure vision focus (fresh screenshot)
    ], start=1):
        print(f"[_gdocs_replace_body] Attempt {attempt}/2")
        focused = await focus_fn()
        if not focused and attempt == 1:
            continue  # skip to vision retry
        await asyncio.sleep(0.3)
        # Select all + paste
        await _osascript('tell application "System Events" to keystroke "a" using command down')
        await asyncio.sleep(0.2)
        await _paste_html(body)
        await asyncio.sleep(2.5)

        # ── Verify: DOM read-back first (fast), then vision verify (reliable) ──
        readback = await _gdocs_read_body()
        if _body_matches_expected(readback, body):
            print(f"[_gdocs_replace_body] ✓ Verified via DOM readback (attempt {attempt})")
            return True
        # DOM readback may fail on canvas docs — ask vision to confirm
        if await _vision_verify_content(body):
            print(f"[_gdocs_replace_body] ✓ Verified via vision (attempt {attempt})")
            return True
        print(f"[_gdocs_replace_body] ✗ Attempt {attempt} paste not verified")

    return False


async def _open_gdoc_url(url: str) -> dict:
    await _osascript('tell application "Google Chrome" to activate')
    if url:
        await _osascript(f'tell application "Google Chrome" to open location "{url}"')
        await asyncio.sleep(0.8)
    return await _wait_for_gdocs_ready(timeout=18.0)


async def _gdocs_append_body(text: str) -> bool:
    """Append text to the end of a Google Doc.  Vision-verified with retry."""
    if not text:
        return True
    await _osascript('tell application "Google Chrome" to activate')
    await asyncio.sleep(0.2)

    for attempt, focus_fn in enumerate([
        _gdocs_focus_editor,              # attempt 0: DOM fast-path → vision fallback
        _gdocs_focus_editor_with_vision,  # attempt 1: pure vision focus
    ], start=1):
        print(f"[_gdocs_append_body] Attempt {attempt}/2")
        focused = await focus_fn()
        if not focused and attempt == 1:
            continue
        await asyncio.sleep(0.3)
        # Move to end of document
        await _osascript('tell application "System Events" to key code 119 using command down')
        await asyncio.sleep(0.3)
        await _paste_html("\n" + text)
        await asyncio.sleep(2.5)

        # ── Verify: DOM read-back first (fast), then vision verify (reliable) ──
        readback = await _gdocs_read_body()
        if _body_matches_expected(readback, text):
            print(f"[_gdocs_append_body] ✓ Verified via DOM readback (attempt {attempt})")
            return True
        if await _vision_verify_content(text):
            print(f"[_gdocs_append_body] ✓ Verified via vision (attempt {attempt})")
            return True
        print(f"[_gdocs_append_body] ✗ Attempt {attempt} paste not verified")

    return False


def _extract_visual_coordinates(screen_result: str) -> tuple[int, int] | None:
    match = _VISION_COORD_RE.search(str(screen_result or ""))
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


# ═══════════════════════════════════════════════════════════════
#  Vision-first GDocs helpers
# ═══════════════════════════════════════════════════════════════

async def _vision_locate_and_click(description: str, hint: str = "") -> bool:
    """Use Gemini Vision to find a UI element on screen and click it.

    Uses _fast_visual_locate (screenshot → Gemini Flash → coordinates)
    which is immune to DOM class-name changes in Google Docs.

    Returns True if element was found and clicked.
    """
    try:
        from tools.mac_tools import _fast_visual_locate
        coords = await _fast_visual_locate(description, hint=hint)
        if not coords:
            print(f"[_vision_locate_and_click] ✗ Could not find: {description[:60]}")
            return False
        x, y = coords
        result = await registry.execute("click_element", {"x": x, "y": y})
        ok = "failed" not in str(result or "").lower()
        print(f"[_vision_locate_and_click] {'✓' if ok else '✗'} Click at ({x},{y}) for: {description[:60]}")
        return ok
    except Exception as e:
        print(f"[_vision_locate_and_click] ⚠ Error: {e}")
        return False


async def _vision_gdocs_set_title(title: str) -> bool:
    """Use vision to find the Google Docs title field, click it, clear it, and type the title."""
    if not title:
        return True
    print(f"[_vision_gdocs_set_title] Setting title via vision: {title[:50]}")
    clicked = await _vision_locate_and_click(
        "the document title input field at the top of the Google Doc — "
        "it usually says 'Untitled document' or shows the current document title, "
        "located above the menu bar (File, Edit, View)",
        hint="Google Docs title area, very top of the page, above toolbar"
    )
    if not clicked:
        print("[_vision_gdocs_set_title] ✗ Could not locate title field via vision")
        return False
    await asyncio.sleep(0.4)
    # Triple-click to select all text in the title field
    await _osascript('tell application "System Events" to keystroke "a" using command down')
    await asyncio.sleep(0.15)
    # Type the new title via clipboard paste (handles special chars)
    await _paste_text(title)
    await asyncio.sleep(0.4)
    # Press Tab to confirm title and move focus away
    await _osascript('tell application "System Events" to key code 48')  # Tab
    await asyncio.sleep(0.3)
    print(f"[_vision_gdocs_set_title] ✓ Title typed via vision")
    return True


async def _vision_verify_content(expected_snippet: str) -> bool:
    """Take a screenshot and ask Vision if the expected content is visible in the document.

    This is the most reliable way to verify paste/type operations because
    it sees the actual rendered pixels, not DOM nodes that may be stale.
    """
    if not expected_snippet:
        return True
    # Use a concise snippet for the check (first 200 chars is enough)
    snippet = expected_snippet[:200].replace('\n', ' ').strip()
    question = (
        f"Look at the Google Doc editor area on screen. "
        f"Does the document body contain text that matches or closely resembles this?\n\n"
        f"\"{snippet}\"\n\n"
        f"Answer ONLY 'YES' or 'NO'."
    )
    try:
        result = await registry.execute("read_screen", {"question": question})
        answer = str(result or "").strip().upper()
        found = answer.startswith("YES") or "YES" in answer
        print(f"[_vision_verify_content] {'✓' if found else '✗'} Vision says: {answer[:20]}")
        return found
    except Exception as e:
        print(f"[_vision_verify_content] ⚠ Error: {e}")
        return False


async def _vision_gdocs_read_content() -> str:
    """Use vision to read the content visible in the Google Doc.

    Fallback for when DOM-based body extraction returns empty
    (e.g. canvas-based Google Docs rendering).
    """
    question = (
        "Read and transcribe ALL the text content visible in the Google Doc editor area. "
        "Include all paragraphs, headings, lists, and any other text. "
        "Do NOT include toolbar text, menu items, sidebar content, or UI labels. "
        "Return ONLY the document text content, nothing else."
    )
    try:
        result = await registry.execute("read_screen", {"question": question})
        text = str(result or "").strip()
        if text and not text.startswith("ERROR"):
            print(f"[_vision_gdocs_read_content] ✓ Read {len(text)} chars via vision")
            return text
        print(f"[_vision_gdocs_read_content] ✗ Vision returned empty or error")
        return ""
    except Exception as e:
        print(f"[_vision_gdocs_read_content] ⚠ Error: {e}")
        return ""


async def _gdocs_focus_editor_with_vision() -> bool:
    """Use Gemini Vision to find and click the Google Docs editor body.

    This is the most reliable way to place the cursor in the editor
    because it works regardless of DOM class-name changes.
    """
    return await _vision_locate_and_click(
        "the main white writable page area in the center of the Google Doc "
        "where typed/pasted document text should appear — not the toolbar, "
        "not the title field at the top, not comments, not any sidebar",
        hint="Center of the Google Docs editor page body"
    )


# ═══════════════════════════════════════════════════════════════
#  Google Docs Tools
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="gdocs_create",
    description=(
        "Create a new Google Doc and open it in Chrome. Returns the document URL "
        "and ID. Optionally pre-fill it with content (supports long-form essays, "
        "reports, etc.)."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Title of the new document"
            },
            "body": {
                "type": "string",
                "description": "Optional initial body text (plain text or simple markdown). "
                               "Can be very long — essays, reports, articles."
            },
        },
        "required": ["title"],
    },
)
async def gdocs_create(title: str, body: str = "") -> str:
    token = _load_token()
    if token:
        # ── REST API path ──
        doc = await _gapi("POST", "https://docs.googleapis.com/v1/documents",
                          body={"title": title}, token=token)
        doc_id = doc.get("documentId", "")
        if doc_id and body:
            # Batch-insert the body text
            requests = [{"insertText": {"location": {"index": 1}, "text": body}}]
            await _gapi("POST",
                        f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
                        body={"requests": requests}, token=token)
        url = f"https://docs.google.com/document/d/{doc_id}/edit"
        webbrowser.open(url)
        runtime_state_store.update_request_state(active_doc_url=url)
        return json.dumps({"ok": True, "doc_id": doc_id, "url": url,
                           "body_length": len(body)})

    # ── Fallback: reuse the current request doc when possible, otherwise create one ──
    prior_doc_url = str(runtime_state_store.snapshot().request_state.active_doc_url or "")
    target_url = prior_doc_url if prior_doc_url.startswith("https://docs.google.com/document/d/") else "https://docs.new"
    doc_state = await _open_gdoc_url(target_url)
    if not doc_state:
        return json.dumps({
            "ok": False,
            "url": target_url,
            "note": "Google Docs did not become ready in the browser.",
            "error_code": "gdocs_not_ready",
            "repairable": False,
        })

    title_ok = await _gdocs_set_title(title)
    await asyncio.sleep(0.4)

    body_ok = True
    readback = ""
    if body:
        body_ok = await _gdocs_replace_body(body)
        readback = await _gdocs_read_body()
        post_state = await _gdocs_state_via_bridge()
        body_ok = body_ok or _body_matches_expected(readback, body)
        doc_state = post_state or doc_state

    url = str(doc_state.get("url", "") or "https://docs.new")
    runtime_state_store.update_request_state(active_doc_url=url)
    doc_id = _extract_doc_id_from_url(url)
    if title_ok and body_ok:
        return json.dumps({
            "ok": True,
            "url": url,
            "doc_id": doc_id,
            "note": "Opened a new Google Doc and applied the requested title/content.",
            "title_applied": True,
            "body_applied": True,
            "body_length": len(body),
            "repairable": False,
        })

    return json.dumps({
        "ok": False,
        "url": url,
        "doc_id": doc_id,
        "note": "Google Doc opened, but title or body was not applied reliably.",
        "error_code": "gdocs_apply_failed",
        "title_applied": bool(title_ok),
        "body_applied": bool(body_ok),
        "body_length": len(body),
        "repairable": bool(url.startswith("https://docs.google.com/document/d/")),
    })


@registry.register(
    name="gdocs_read",
    description=(
        "Read the full text content of a Google Doc by its URL or document ID. "
        "Returns the document title and body as plain text. Ideal for letting the "
        "vision model analyse and summarise a document without copy-pasting."
    ),
    parameters={
        "type": "object",
        "properties": {
            "doc_url_or_id": {
                "type": "string",
                "description": "Google Docs URL or document ID"
            },
        },
        "required": ["doc_url_or_id"],
    },
)
async def gdocs_read(doc_url_or_id: str) -> str:
    doc_id = _extract_doc_id(doc_url_or_id)
    token = _load_token()

    if token:
        doc = await _gapi("GET",
                          f"https://docs.googleapis.com/v1/documents/{doc_id}",
                          token=token)
        if "error" in doc:
            return json.dumps({"error": doc["error"]})

        title = doc.get("title", "")
        # Walk the document body to extract plain text
        body_content = doc.get("body", {}).get("content", [])
        text_parts: list[str] = []
        for element in body_content:
            paragraph = element.get("paragraph", {})
            for pe in paragraph.get("elements", []):
                text_run = pe.get("textRun", {})
                text_parts.append(text_run.get("content", ""))
            table = element.get("table", {})
            for row in table.get("tableRows", []):
                row_cells: list[str] = []
                for cell in row.get("tableCells", []):
                    cell_text = ""
                    for ce in cell.get("content", []):
                        for pe in ce.get("paragraph", {}).get("elements", []):
                            cell_text += pe.get("textRun", {}).get("content", "")
                    row_cells.append(cell_text.strip())
                text_parts.append(" | ".join(row_cells))

        full_text = "".join(text_parts).strip()
        return json.dumps({"ok": True, "doc_id": doc_id, "title": title,
                           "char_count": len(full_text),
                           "text": full_text[:12000]}, ensure_ascii=False)

    # Fallback: DOM bridge → clipboard → vision OCR cascade
    # _gdocs_read_body focuses the editor before clipboard copy and falls
    # back to Gemini Vision OCR for canvas-based docs.
    text = await _gdocs_read_body()

    if text and len(_normalize_doc_text(text)) >= 5:
        return json.dumps({"ok": True, "doc_id": doc_id,
                           "text": text[:8000],
                           "note": "Read via browser automation (DOM/clipboard/vision). "
                                   "For full fidelity set up OAuth in ~/.moonwalk/gcloud_token.json."},
                          ensure_ascii=False)

    return json.dumps({"ok": False, "doc_id": doc_id,
                       "error": "Failed to read document content (DOM, clipboard, and vision all returned empty).",
                       "note": "For full fidelity and reliability without the browser extension, "
                               "set up OAuth in ~/.moonwalk/gcloud_token.json."},
                      ensure_ascii=False)


@registry.register(
    name="gdocs_append",
    description=(
        "Append text to the end of an existing Google Doc. "
        "Use for adding paragraphs, sections, or long-form content to an already "
        "open document. Supports multi-paragraph text."
    ),
    parameters={
        "type": "object",
        "properties": {
            "doc_url_or_id": {
                "type": "string",
                "description": "Google Docs URL or document ID"
            },
            "text": {
                "type": "string",
                "description": "Text to append (plain text, can be multi-paragraph)"
            },
        },
        "required": ["doc_url_or_id", "text"],
    },
)
async def gdocs_append(doc_url_or_id: str, text: str) -> str:
    doc_id = _extract_doc_id(doc_url_or_id)
    token = _load_token()

    if token:
        # Get current document length
        doc = await _gapi("GET",
                          f"https://docs.googleapis.com/v1/documents/{doc_id}",
                          token=token)
        body_content = doc.get("body", {}).get("content", [])
        # Find end index
        end_index = 1
        for element in body_content:
            end_index = max(end_index, element.get("endIndex", 1))

        insert_idx = max(1, end_index - 1)
        requests = [{"insertText": {"location": {"index": insert_idx},
                                    "text": "\n" + text}}]
        result = await _gapi("POST",
                             f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
                             body={"requests": requests}, token=token)
        return json.dumps({"ok": True, "doc_id": doc_id,
                           "appended_chars": len(text),
                           "api_result": str(result)[:500]})

    doc_url = str(doc_url_or_id or "").strip()
    if doc_id and not doc_url.startswith("http"):
        doc_url = f"https://docs.google.com/document/d/{doc_id}/edit"

    doc_state = await _open_gdoc_url(doc_url)
    if not doc_state:
        return json.dumps({
            "ok": False,
            "doc_id": doc_id,
            "url": doc_url,
            "note": "Google Doc did not become ready for append.",
            "error_code": "gdocs_not_ready",
        })

    appended = await _gdocs_append_body(text)
    final_url = str(doc_state.get("url", "") or doc_url)
    return json.dumps({
        "ok": bool(appended),
        "doc_id": doc_id or _extract_doc_id_from_url(final_url),
        "url": final_url,
        "appended_chars": len(text),
        "method": "keyboard_paste_html",
        "error_code": "" if appended else "gdocs_append_failed",
    })


@registry.register(
    name="gdocs_insert_image",
    description=(
        "Insert an image into a Google Doc at the end of the document. "
        "Provide the image as a publicly accessible URL."
    ),
    parameters={
        "type": "object",
        "properties": {
            "doc_url_or_id": {
                "type": "string",
                "description": "Google Docs URL or document ID"
            },
            "image_url": {
                "type": "string",
                "description": "Publicly accessible URL of the image to insert"
            },
            "width": {
                "type": "number",
                "description": "Optional width in points (72 points = 1 inch). Defaults to 400."
            },
            "height": {
                "type": "number",
                "description": "Optional height in points. Defaults to 300."
            },
        },
        "required": ["doc_url_or_id", "image_url"],
    },
)
async def gdocs_insert_image(doc_url_or_id: str, image_url: str,
                             width: float = 400, height: float = 300) -> str:
    doc_id = _extract_doc_id(doc_url_or_id)
    token = _load_token()

    doc = await _gapi("GET",
                      f"https://docs.googleapis.com/v1/documents/{doc_id}",
                      token=token)
    body_content = doc.get("body", {}).get("content", [])
    end_index = 1
    for element in body_content:
        end_index = max(end_index, element.get("endIndex", 1))

    insert_idx = max(1, end_index - 1)
    requests = [
        {
            "insertInlineImage": {
                "location": {"index": insert_idx},
                "uri": image_url,
                "objectSize": {
                    "width": {"magnitude": width, "unit": "PT"},
                    "height": {"magnitude": height, "unit": "PT"},
                },
            }
        }
    ]
    result = await _gapi("POST",
                         f"https://docs.googleapis.com/v1/documents/{doc_id}:batchUpdate",
                         body={"requests": requests}, token=token)
    return json.dumps({"ok": True, "doc_id": doc_id, "image_url": image_url,
                       "api_result": str(result)[:500]})


# ═══════════════════════════════════════════════════════════════
#  Google Sheets Tools
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="gsheets_create",
    description=(
        "Create a new Google Sheets spreadsheet and open it in Chrome. "
        "Optionally populate it with initial data (multi-row, multi-column). "
        "Perfect for creating tables, trackers, budgets, or data exports."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Title of the new spreadsheet"
            },
            "headers": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional column headers (first row)"
            },
            "rows": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "description": (
                    "Optional data rows. Each row is an array of cell values. "
                    "Can contain hundreds of rows for bulk data entry."
                ),
            },
        },
        "required": ["title"],
    },
)
async def gsheets_create(title: str, headers: list[str] | None = None,
                         rows: list[list[str]] | None = None) -> str:
    token = _load_token()

    if token:
        # Build sheet data
        sheet_data: list[dict] = []
        if headers:
            sheet_data.append({
                "values": [{"userEnteredValue": {"stringValue": h}} for h in headers]
            })
        for row in (rows or []):
            sheet_data.append({
                "values": [{"userEnteredValue": {"stringValue": str(cell)}} for cell in row]
            })

        body: dict = {
            "properties": {"title": title},
        }
        if sheet_data:
            body["sheets"] = [{
                "properties": {"title": "Sheet1"},
                "data": [{"startRow": 0, "startColumn": 0, "rowData": sheet_data}],
            }]

        result = await _gapi("POST",
                             "https://sheets.googleapis.com/v4/spreadsheets",
                             body=body, token=token)
        spreadsheet_id = result.get("spreadsheetId", "")
        url = f"https://docs.google.com/spreadsheets/d/{spreadsheet_id}/edit"
        webbrowser.open(url)
        return json.dumps({
            "ok": True,
            "spreadsheet_id": spreadsheet_id,
            "url": url,
            "rows_written": len(sheet_data),
        })

    # Fallback: open blank sheet and paste TSV
    webbrowser.open("https://sheets.new")
    await asyncio.sleep(3.0)
    if headers or rows:
        tsv_lines: list[str] = []
        if headers:
            tsv_lines.append("\t".join(headers))
        for row in (rows or []):
            tsv_lines.append("\t".join(str(c) for c in row))
        tsv = "\n".join(tsv_lines)
        await _paste_text(tsv)
    return json.dumps({"ok": True, "url": "https://sheets.new",
                       "method": "clipboard_paste",
                       "rows_written": (1 if headers else 0) + len(rows or [])})


@registry.register(
    name="gsheets_read",
    description=(
        "Read data from a Google Sheets spreadsheet. Returns cell values as "
        "a 2D array. Specify a range like 'Sheet1!A1:D50' or leave empty to "
        "read the entire first sheet. Great for the vision model to analyse "
        "spreadsheet contents directly."
    ),
    parameters={
        "type": "object",
        "properties": {
            "spreadsheet_url_or_id": {
                "type": "string",
                "description": "Spreadsheet URL or ID"
            },
            "range": {
                "type": "string",
                "description": "A1-notation range, e.g. 'Sheet1!A1:Z100'. Defaults to entire first sheet."
            },
        },
        "required": ["spreadsheet_url_or_id"],
    },
)
async def gsheets_read(spreadsheet_url_or_id: str, range: str = "") -> str:
    sheet_id = _extract_doc_id(spreadsheet_url_or_id)
    token = _load_token()

    if token:
        range_part = range or "Sheet1"
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
               f"/values/{range_part}")
        result = await _gapi("GET", url, token=token)
        values = result.get("values", [])
        return json.dumps({
            "ok": True,
            "spreadsheet_id": sheet_id,
            "range": result.get("range", range_part),
            "row_count": len(values),
            "values": values[:500],  # cap at 500 rows for context window
        }, ensure_ascii=False)

    # Fallback: JS injection to read visible cells
    js = (
        "(function(){var rows=document.querySelectorAll('tr');"
        "var data=[];for(var i=0;i<Math.min(rows.length,100);i++){"
        "var cells=rows[i].querySelectorAll('td,th');"
        "var r=[];for(var j=0;j<cells.length;j++)r.push(cells[j].innerText);"
        "data.push(r);}return JSON.stringify(data);})()"
    )
    text = await _chrome_js(js)
    try:
        values = json.loads(text or "[]")
    except Exception:
        values = []
    return json.dumps({"ok": True, "spreadsheet_id": sheet_id,
                       "row_count": len(values), "values": values[:200],
                       "method": "browser_dom"}, ensure_ascii=False)


@registry.register(
    name="gsheets_write",
    description=(
        "Write or overwrite data in a Google Sheets range. Accepts a 2D array "
        "of cell values. Use for bulk data entry — hundreds of rows at once. "
        "Specify a range like 'Sheet1!A1' to set the starting cell."
    ),
    parameters={
        "type": "object",
        "properties": {
            "spreadsheet_url_or_id": {
                "type": "string",
                "description": "Spreadsheet URL or ID"
            },
            "range": {
                "type": "string",
                "description": "Starting cell in A1-notation, e.g. 'Sheet1!A1'"
            },
            "values": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "description": "2D array of cell values. Each inner array is a row."
            },
        },
        "required": ["spreadsheet_url_or_id", "range", "values"],
    },
)
async def gsheets_write(spreadsheet_url_or_id: str, range: str,
                        values: list[list[str]]) -> str:
    sheet_id = _extract_doc_id(spreadsheet_url_or_id)
    token = _load_token()

    if token:
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
               f"/values/{range}?valueInputOption=USER_ENTERED")
        result = await _gapi("PUT", url,
                             body={"range": range, "values": values},
                             token=token)
        return json.dumps({
            "ok": True,
            "spreadsheet_id": sheet_id,
            "updated_range": result.get("updatedRange", range),
            "updated_rows": result.get("updatedRows", len(values)),
            "updated_cells": result.get("updatedCells", 0),
        })

    # Fallback: paste TSV
    tsv_lines = ["\t".join(str(c) for c in row) for row in values]
    tsv = "\n".join(tsv_lines)
    await _paste_text(tsv)
    return json.dumps({"ok": True, "spreadsheet_id": sheet_id,
                       "rows_pasted": len(values), "method": "clipboard_paste"})


@registry.register(
    name="gsheets_append_rows",
    description=(
        "Append rows to the end of a Google Sheets spreadsheet. "
        "Data is added after the last row with content. "
        "Ideal for logging, data collection, and incremental updates."
    ),
    parameters={
        "type": "object",
        "properties": {
            "spreadsheet_url_or_id": {
                "type": "string",
                "description": "Spreadsheet URL or ID"
            },
            "values": {
                "type": "array",
                "items": {
                    "type": "array",
                    "items": {"type": "string"},
                },
                "description": "Rows to append. Each inner array is a row."
            },
            "sheet_name": {
                "type": "string",
                "description": "Sheet tab name (default 'Sheet1')"
            },
        },
        "required": ["spreadsheet_url_or_id", "values"],
    },
)
async def gsheets_append_rows(spreadsheet_url_or_id: str,
                              values: list[list[str]],
                              sheet_name: str = "Sheet1") -> str:
    sheet_id = _extract_doc_id(spreadsheet_url_or_id)
    token = _load_token()

    if token:
        range_spec = f"{sheet_name}!A:Z"
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
               f"/values/{range_spec}:append?valueInputOption=USER_ENTERED"
               f"&insertDataOption=INSERT_ROWS")
        result = await _gapi("POST", url,
                             body={"range": range_spec, "values": values},
                             token=token)
        updates = result.get("updates", {})
        return json.dumps({
            "ok": True,
            "spreadsheet_id": sheet_id,
            "updated_range": updates.get("updatedRange", ""),
            "appended_rows": updates.get("updatedRows", len(values)),
        })

    # Fallback: navigate to last row and paste
    await _osascript(
        'tell application "System Events" to key code 125 using {command down}'
    )
    await asyncio.sleep(0.3)
    tsv_lines = ["\t".join(str(c) for c in row) for row in values]
    await _paste_text("\n".join(tsv_lines))
    return json.dumps({"ok": True, "spreadsheet_id": sheet_id,
                       "appended_rows": len(values), "method": "clipboard_paste"})


@registry.register(
    name="gsheets_formula",
    description=(
        "Write a formula into a specific cell in Google Sheets. "
        "Useful for adding SUM, AVERAGE, VLOOKUP, or any Sheets formula."
    ),
    parameters={
        "type": "object",
        "properties": {
            "spreadsheet_url_or_id": {
                "type": "string",
                "description": "Spreadsheet URL or ID"
            },
            "cell": {
                "type": "string",
                "description": "Cell reference in A1-notation, e.g. 'A10' or 'Sheet1!E5'"
            },
            "formula": {
                "type": "string",
                "description": "The formula string, e.g. '=SUM(A1:A9)' or '=VLOOKUP(B2,D:E,2,FALSE)'"
            },
        },
        "required": ["spreadsheet_url_or_id", "cell", "formula"],
    },
)
async def gsheets_formula(spreadsheet_url_or_id: str, cell: str,
                          formula: str) -> str:
    sheet_id = _extract_doc_id(spreadsheet_url_or_id)
    token = _load_token()

    if not cell.startswith("Sheet"):
        cell = f"Sheet1!{cell}"

    if token:
        url = (f"https://sheets.googleapis.com/v4/spreadsheets/{sheet_id}"
               f"/values/{cell}?valueInputOption=USER_ENTERED")
        result = await _gapi("PUT", url,
                             body={"range": cell, "values": [[formula]]},
                             token=token)
        return json.dumps({
            "ok": True, "spreadsheet_id": sheet_id, "cell": cell,
            "formula": formula,
            "updated_cells": result.get("updatedCells", 0),
        })

    # Fallback: click cell and type formula
    await _paste_text(formula)
    await _osascript('tell application "System Events" to keystroke return')
    return json.dumps({"ok": True, "spreadsheet_id": sheet_id,
                       "cell": cell, "formula": formula, "method": "keyboard"})


# ═══════════════════════════════════════════════════════════════
#  Google Slides Tools
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="gslides_create",
    description=(
        "Create a new Google Slides presentation and open it in Chrome. "
        "Optionally provide an array of slide definitions to pre-fill the deck."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {
                "type": "string",
                "description": "Presentation title"
            },
            "slides": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {"type": "string"},
                        "body": {"type": "string"},
                    },
                },
                "description": (
                    "Optional slide content. Each object has 'title' and 'body' fields. "
                    "The first slide uses the TITLE layout; subsequent slides use TITLE_AND_BODY."
                ),
            },
        },
        "required": ["title"],
    },
)
async def gslides_create(title: str, slides: list[dict] | None = None) -> str:
    token = _load_token()

    if token:
        body: dict = {"title": title}
        result = await _gapi("POST",
                             "https://slides.googleapis.com/v1/presentations",
                             body=body, token=token)
        pres_id = result.get("presentationId", "")
        if not pres_id:
            return json.dumps({"error": "Failed to create presentation", "detail": str(result)[:500]})

        if slides:
            requests: list[dict] = []
            for i, slide in enumerate(slides):
                slide_id = f"slide_{i}"
                layout = "TITLE" if i == 0 else "TITLE_AND_BODY"
                requests.append({
                    "createSlide": {
                        "objectId": slide_id,
                        "insertionIndex": i,
                        "slideLayoutReference": {"predefinedLayout": layout},
                    }
                })
            # Create all slides first
            await _gapi("POST",
                        f"https://slides.googleapis.com/v1/presentations/{pres_id}:batchUpdate",
                        body={"requests": requests}, token=token)

            # Now populate text
            text_requests: list[dict] = []
            for i, slide in enumerate(slides):
                slide_id = f"slide_{i}"
                # Get the created slide's placeholder IDs
                pres = await _gapi("GET",
                                   f"https://slides.googleapis.com/v1/presentations/{pres_id}",
                                   token=token)
                for s in pres.get("slides", []):
                    if s.get("objectId") == slide_id:
                        for pe in s.get("pageElements", []):
                            placeholder = pe.get("placeholder", {})
                            shape_id = pe.get("objectId", "")
                            if placeholder.get("type") == "TITLE" and slide.get("title"):
                                text_requests.append({
                                    "insertText": {
                                        "objectId": shape_id,
                                        "text": slide["title"],
                                    }
                                })
                            elif placeholder.get("type") == "BODY" and slide.get("body"):
                                text_requests.append({
                                    "insertText": {
                                        "objectId": shape_id,
                                        "text": slide["body"],
                                    }
                                })
                        break
            if text_requests:
                await _gapi("POST",
                            f"https://slides.googleapis.com/v1/presentations/{pres_id}:batchUpdate",
                            body={"requests": text_requests}, token=token)

        url = f"https://docs.google.com/presentation/d/{pres_id}/edit"
        webbrowser.open(url)
        return json.dumps({"ok": True, "presentation_id": pres_id, "url": url,
                           "slides_created": len(slides or [])})

    # Fallback
    webbrowser.open("https://slides.new")
    await asyncio.sleep(3.0)
    return json.dumps({"ok": True, "url": "https://slides.new",
                       "note": "Opened blank presentation. Use keyboard tools to add content."})


@registry.register(
    name="gslides_add_slide",
    description=(
        "Add a new slide to an existing Google Slides presentation. "
        "Supports title + body text content."
    ),
    parameters={
        "type": "object",
        "properties": {
            "presentation_url_or_id": {
                "type": "string",
                "description": "Presentation URL or ID"
            },
            "title": {
                "type": "string",
                "description": "Slide title"
            },
            "body": {
                "type": "string",
                "description": "Slide body text"
            },
            "layout": {
                "type": "string",
                "description": "Slide layout: TITLE, TITLE_AND_BODY, BLANK, etc. Default TITLE_AND_BODY."
            },
        },
        "required": ["presentation_url_or_id"],
    },
)
async def gslides_add_slide(presentation_url_or_id: str, title: str = "",
                            body: str = "", layout: str = "TITLE_AND_BODY") -> str:
    pres_id = _extract_doc_id(presentation_url_or_id)
    token = _load_token()

    if token:
        # Fast path: REST API
        slide_id = f"slide_{int(time.time())}"
        await _gapi("POST",
                    f"https://slides.googleapis.com/v1/presentations/{pres_id}:batchUpdate",
                    body={"requests": [{"createSlide": {"objectId": slide_id,
                          "slideLayoutReference": {"predefinedLayout": layout}}}]},
                    token=token)
        pres = await _gapi("GET",
                           f"https://slides.googleapis.com/v1/presentations/{pres_id}",
                           token=token)
        text_requests: list[dict] = []
        for s in pres.get("slides", []):
            if s.get("objectId") == slide_id:
                for pe in s.get("pageElements", []):
                    ph = pe.get("placeholder", {})
                    oid = pe.get("objectId", "")
                    if ph.get("type") == "TITLE" and title:
                        text_requests.append({"insertText": {"objectId": oid, "text": title}})
                    elif ph.get("type") == "BODY" and body:
                        text_requests.append({"insertText": {"objectId": oid, "text": body}})
                break
        if text_requests:
            await _gapi("POST",
                        f"https://slides.googleapis.com/v1/presentations/{pres_id}:batchUpdate",
                        body={"requests": text_requests}, token=token)
        return json.dumps({"ok": True, "presentation_id": pres_id,
                           "slide_id": slide_id, "title": title})

    # Browser path: keyboard shortcuts work reliably in Google Slides
    # Cmd+M = new slide, then type into the title/body placeholders
    # First ensure the presentation is open
    if presentation_url_or_id and presentation_url_or_id.startswith("http"):
        webbrowser.open(presentation_url_or_id)
        await asyncio.sleep(2.5)
    # Insert new slide (Cmd+M in Slides)
    await _osascript('tell application "System Events" to keystroke "m" using command down')
    await asyncio.sleep(0.5)
    # Type title (first placeholder is focused after new slide)
    if title:
        await _paste_text(title)
        await asyncio.sleep(0.3)
    # Tab to body placeholder
    if body:
        await _osascript('tell application "System Events" to keystroke tab')
        await asyncio.sleep(0.3)
        await _paste_text(body)
    return json.dumps({"ok": True, "presentation_id": pres_id,
                       "title": title, "method": "keyboard_automation",
                       "note": "Added slide via keyboard shortcut. No OAuth needed."})


# ═══════════════════════════════════════════════════════════════
#  Google Drive Tools
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="gdrive_search",
    description=(
        "Search Google Drive for files by name, type, or content. "
        "Returns file names, IDs, types, and URLs. "
        "Use to find documents before reading or editing them."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Search query (file name, keywords, or Drive query syntax like \"mimeType='application/vnd.google-apps.spreadsheet'\")"
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of results (default 10)"
            },
        },
        "required": ["query"],
    },
)
async def gdrive_search(query: str, max_results: int = 10) -> str:
    import urllib.parse

    token = _load_token()
    if token:
        # Fast path: REST API
        if "mimeType" not in query and "fullText" not in query and "name" not in query:
            q = f"name contains '{query}' or fullText contains '{query}'"
        else:
            q = query
        url = (f"https://www.googleapis.com/drive/v3/files"
               f"?q={q}&pageSize={max_results}"
               f"&fields=files(id,name,mimeType,webViewLink,modifiedTime,size)")
        result = await _gapi("GET", url, token=token)
        files = result.get("files", [])
        return json.dumps({"ok": True, "query": query,
                           "result_count": len(files), "files": files},
                          ensure_ascii=False)

    # Browser path: open Drive search page and scrape results
    search_url = f"https://drive.google.com/drive/search?q={urllib.parse.quote(query)}"
    webbrowser.open(search_url)
    await asyncio.sleep(3.0)
    js = (
        "(function(){"
        "var items=document.querySelectorAll('[data-id],[data-target],.KL4pp');"
        "var files=[];"
        "for(var i=0;i<Math.min(items.length," + str(max_results) + ");i++){"
        "var el=items[i];"
        "files.push({name:el.getAttribute('aria-label')||el.getAttribute('data-tooltip')||el.innerText.slice(0,80)});"
        "}"
        "return JSON.stringify(files);"
        "})()"
    )
    raw = await _chrome_js(js)
    try:
        files_dom = json.loads(raw or "[]")
    except Exception:
        files_dom = []
    return json.dumps({"ok": True, "query": query, "result_count": len(files_dom),
                       "files": files_dom, "method": "browser_dom",
                       "note": "Searched Drive in browser. For full metadata, set up optional OAuth."},
                      ensure_ascii=False)


@registry.register(
    name="gdrive_upload",
    description=(
        "Upload a local file to Google Drive. "
        "Supports any file type. Returns the Drive file ID and URL."
    ),
    parameters={
        "type": "object",
        "properties": {
            "local_path": {
                "type": "string",
                "description": "Path to the local file to upload"
            },
            "drive_name": {
                "type": "string",
                "description": "Optional name for the file in Drive (defaults to local filename)"
            },
            "folder_id": {
                "type": "string",
                "description": "Optional Drive folder ID to upload into"
            },
        },
        "required": ["local_path"],
    },
)
async def gdrive_upload(local_path: str, drive_name: str = "",
                        folder_id: str = "") -> str:
    import httpx

    token = _load_token()

    expanded = os.path.expanduser(local_path)
    if not os.path.exists(expanded):
        return json.dumps({"error": f"File not found: {expanded}"})

    filename = drive_name or os.path.basename(expanded)
    file_size = os.path.getsize(expanded)

    token = _load_token()
    if token:
        # Fast path: REST API multipart upload
        metadata: dict = {"name": filename}
        if folder_id:
            metadata["parents"] = [folder_id]
        with open(expanded, "rb") as f:
            file_data = f.read()
        import io
        boundary = "moonwalk_boundary"
        body_parts = [
            f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{json.dumps(metadata)}\r\n",
            f"--{boundary}\r\nContent-Type: application/octet-stream\r\n\r\n",
        ]
        body = body_parts[0].encode() + body_parts[1].encode() + file_data + f"\r\n--{boundary}--".encode()
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                "https://www.googleapis.com/upload/drive/v3/files?uploadType=multipart",
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": f"multipart/related; boundary={boundary}",
                },
                content=body,
            )
        result = resp.json()
        file_id = result.get("id", "")
        return json.dumps({
            "ok": True, "file_id": file_id, "name": filename,
            "url": f"https://drive.google.com/file/d/{file_id}/view",
            "size_bytes": file_size,
        })

    # Browser path: reveal file in Finder and open Drive — user drags to upload
    await asyncio.create_subprocess_exec("open", "-R", expanded)
    webbrowser.open("https://drive.google.com/drive/my-drive")
    return json.dumps({"ok": True, "local_path": expanded, "name": filename,
                       "method": "manual_drag_and_drop",
                       "note": f"Opened Google Drive and revealed '{filename}' in Finder. "
                               f"Drag it into the Drive window to upload. "
                               f"For automatic background upload, set up optional OAuth."})


# ═══════════════════════════════════════════════════════════════
#  Gmail Tools
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="gmail_send",
    description=(
        "Send an email via Gmail API. Supports plain text body and "
        "optional CC/BCC recipients."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {
                "type": "string",
                "description": "Recipient email address"
            },
            "subject": {
                "type": "string",
                "description": "Email subject line"
            },
            "body": {
                "type": "string",
                "description": "Email body (plain text)"
            },
            "cc": {
                "type": "string",
                "description": "Optional CC recipients (comma-separated)"
            },
            "bcc": {
                "type": "string",
                "description": "Optional BCC recipients (comma-separated)"
            },
        },
        "required": ["to", "subject", "body"],
    },
)
async def gmail_send(to: str, subject: str, body: str,
                     cc: str = "", bcc: str = "") -> str:
    import urllib.parse

    token = _load_token()
    if token:
        # Fast path: REST API
        import base64 as b64
        lines = [f"To: {to}", f"Subject: {subject}",
                 "Content-Type: text/plain; charset=utf-8"]
        if cc:
            lines.insert(1, f"Cc: {cc}")
        if bcc:
            lines.insert(1, f"Bcc: {bcc}")
        lines += ["", body]
        encoded = b64.urlsafe_b64encode("\r\n".join(lines).encode("utf-8")).decode("ascii")
        result = await _gapi("POST",
                             "https://gmail.googleapis.com/gmail/v1/users/me/messages/send",
                             body={"raw": encoded}, token=token)
        msg_id = result.get("id", "")
        return json.dumps({"ok": True, "message_id": msg_id, "to": to, "subject": subject})

    # Browser path (zero-setup): Gmail compose URL pre-fills all fields
    # The user just needs to click Send (or we can automate that too)
    params: dict = {"view": "cm", "to": to, "su": subject, "body": body}
    if cc:
        params["cc"] = cc
    if bcc:
        params["bcc"] = bcc
    compose_url = "https://mail.google.com/mail/?" + urllib.parse.urlencode(params)
    webbrowser.open(compose_url)
    await asyncio.sleep(2.5)
    # Auto-click Send button via keyboard shortcut (Ctrl+Enter in Gmail)
    await _osascript('tell application "System Events" to keystroke return using {command down}')
    return json.dumps({"ok": True, "to": to, "subject": subject,
                       "method": "gmail_compose_url",
                       "note": "Opened Gmail compose window and triggered send. No OAuth needed."})


@registry.register(
    name="gmail_read",
    description=(
        "Read recent emails from Gmail inbox. Returns subject, sender, date, "
        "and snippet for each message. Optionally filter by query."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Gmail search query (same syntax as Gmail search bar), e.g. 'from:boss subject:meeting'"
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum number of emails to return (default 10)"
            },
        },
        "required": [],
    },
)
async def gmail_read(query: str = "", max_results: int = 10) -> str:
    import urllib.parse

    token = _load_token()
    if token:
        # Fast path: REST API
        url = f"https://gmail.googleapis.com/gmail/v1/users/me/messages?maxResults={max_results}"
        if query:
            url += f"&q={urllib.parse.quote(query)}"
        listing = await _gapi("GET", url, token=token)
        messages = listing.get("messages", [])

        async def _fetch_msg(msg_id: str) -> dict:
            msg = await _gapi("GET",
                              f"https://gmail.googleapis.com/gmail/v1/users/me/messages/{msg_id}?format=metadata"
                              f"&metadataHeaders=Subject&metadataHeaders=From&metadataHeaders=Date",
                              token=token)
            headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
            return {"id": msg_id, "subject": headers.get("Subject", ""),
                    "from": headers.get("From", ""), "date": headers.get("Date", ""),
                    "snippet": msg.get("snippet", "")}

        results = await asyncio.gather(*[_fetch_msg(m["id"]) for m in messages[:max_results]])
        return json.dumps({"ok": True, "query": query, "count": len(results),
                           "emails": list(results)}, ensure_ascii=False)

    # Browser path: navigate to Gmail (optionally with search query) and read DOM
    gmail_url = "https://mail.google.com/mail/u/0/"
    if query:
        gmail_url += f"#search/{urllib.parse.quote(query)}"
    webbrowser.open(gmail_url)
    await asyncio.sleep(3.0)  # wait for Gmail to load

    # Scrape visible email rows from the Gmail thread list
    js = (
        "(function(){"
        "var rows=document.querySelectorAll('[data-thread-id],[jsmodel]');"
        "var emails=[];"
        "for(var i=0;i<Math.min(rows.length," + str(max_results) + ");i++){"
        "var r=rows[i];"
        "var sender=r.querySelector('[email],[data-hovercard-id]');"
        "var subj=r.querySelector('[data-thread-subject],.bog,.bqe');"
        "var snippet=r.querySelector('.y2');"
        "emails.push({"
        "from:sender?sender.getAttribute('email')||sender.innerText:'',"
        "subject:subj?subj.innerText:'',"
        "snippet:snippet?snippet.innerText:''"
        "});"
        "}"
        "return JSON.stringify(emails);"
        "})()"
    )
    raw = await _chrome_js(js)
    try:
        emails = json.loads(raw or "[]")
    except Exception:
        emails = []
    return json.dumps({"ok": True, "query": query, "count": len(emails),
                       "emails": emails, "method": "browser_dom"},
                      ensure_ascii=False)


@registry.register(
    name="gmail_draft",
    description=(
        "Create a Gmail draft (does not send). Useful for composing emails "
        "for the user to review before sending."
    ),
    parameters={
        "type": "object",
        "properties": {
            "to": {"type": "string", "description": "Recipient email"},
            "subject": {"type": "string", "description": "Subject line"},
            "body": {"type": "string", "description": "Email body text"},
        },
        "required": ["to", "subject", "body"],
    },
)
async def gmail_draft(to: str, subject: str, body: str) -> str:
    import urllib.parse

    token = _load_token()
    if token:
        import base64 as b64
        raw_msg = f"To: {to}\r\nSubject: {subject}\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n{body}"
        encoded = b64.urlsafe_b64encode(raw_msg.encode("utf-8")).decode("ascii")
        result = await _gapi("POST",
                             "https://gmail.googleapis.com/gmail/v1/users/me/drafts",
                             body={"message": {"raw": encoded}}, token=token)
        return json.dumps({"ok": True, "draft_id": result.get("id", ""),
                           "to": to, "subject": subject})

    # Browser path: open compose URL without sending
    params = {"view": "cm", "to": to, "su": subject, "body": body}
    compose_url = "https://mail.google.com/mail/?" + urllib.parse.urlencode(params)
    webbrowser.open(compose_url)
    return json.dumps({"ok": True, "to": to, "subject": subject,
                       "method": "gmail_compose_url",
                       "note": "Opened Gmail compose window as a draft. No OAuth needed."})


# ═══════════════════════════════════════════════════════════════
#  Google Calendar Tools
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="gcal_create_event",
    description=(
        "Create a Google Calendar event. Supports title, start/end times, "
        "description, location, and attendees."
    ),
    parameters={
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Event title"},
            "start": {
                "type": "string",
                "description": "Start time in ISO 8601 format, e.g. '2026-03-10T14:00:00'"
            },
            "end": {
                "type": "string",
                "description": "End time in ISO 8601 format, e.g. '2026-03-10T15:00:00'"
            },
            "description": {"type": "string", "description": "Optional event description"},
            "location": {"type": "string", "description": "Optional event location"},
            "attendees": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional list of attendee email addresses"
            },
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID (default 'primary')"
            },
        },
        "required": ["title", "start", "end"],
    },
)
async def gcal_create_event(title: str, start: str, end: str,
                            description: str = "", location: str = "",
                            attendees: list[str] | None = None,
                            calendar_id: str = "primary") -> str:
    import urllib.parse

    token = _load_token()
    if token:
        # Fast path: REST API
        event_body: dict = {
            "summary": title,
            "start": {"dateTime": start, "timeZone": "America/New_York"},
            "end": {"dateTime": end, "timeZone": "America/New_York"},
        }
        if description:
            event_body["description"] = description
        if location:
            event_body["location"] = location
        if attendees:
            event_body["attendees"] = [{"email": e} for e in attendees]
        result = await _gapi("POST",
                             f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events",
                             body=event_body, token=token)
        return json.dumps({"ok": True, "event_id": result.get("id", ""),
                           "title": title, "start": start, "end": end,
                           "url": result.get("htmlLink", "")})

    # Browser path: Google Calendar supports a pre-fill URL that opens the
    # new-event form with all fields already populated. Works for any signed-in user.
    # Dates must be in YYYYMMDDTHHmmSSZ format for the URL parameter.
    def _to_cal_date(iso: str) -> str:
        return iso.replace("-", "").replace(":", "").replace(" ", "T").rstrip("Z") + "Z"

    params: dict = {
        "action": "TEMPLATE",
        "text": title,
        "dates": f"{_to_cal_date(start)}/{_to_cal_date(end)}",
    }
    if description:
        params["details"] = description
    if location:
        params["location"] = location
    if attendees:
        params["add"] = ",".join(attendees)

    cal_url = "https://calendar.google.com/calendar/render?" + urllib.parse.urlencode(params)
    webbrowser.open(cal_url)
    return json.dumps({"ok": True, "title": title, "start": start, "end": end,
                       "method": "calendar_prefill_url",
                       "note": "Opened Google Calendar new-event form with all fields pre-filled. No OAuth needed."})


@registry.register(
    name="gcal_list_events",
    description=(
        "List upcoming Google Calendar events. "
        "Returns event titles, times, locations, and attendees."
    ),
    parameters={
        "type": "object",
        "properties": {
            "time_min": {
                "type": "string",
                "description": "Start of time range (ISO 8601). Defaults to now."
            },
            "time_max": {
                "type": "string",
                "description": "End of time range (ISO 8601). Defaults to 7 days from now."
            },
            "max_results": {
                "type": "integer",
                "description": "Maximum events to return (default 20)"
            },
            "calendar_id": {
                "type": "string",
                "description": "Calendar ID (default 'primary')"
            },
        },
        "required": [],
    },
)
async def gcal_list_events(time_min: str = "", time_max: str = "",
                           max_results: int = 20,
                           calendar_id: str = "primary") -> str:
    from datetime import datetime, timedelta, timezone

    token = _load_token()
    if token:
        # Fast path: REST API
        now = datetime.now(timezone.utc)
        if not time_min:
            time_min = now.isoformat()
        if not time_max:
            time_max = (now + timedelta(days=7)).isoformat()
        url = (f"https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events"
               f"?timeMin={time_min}&timeMax={time_max}&maxResults={max_results}"
               f"&singleEvents=true&orderBy=startTime")
        result = await _gapi("GET", url, token=token)
        events = result.get("items", [])
        summary = [{
            "id": ev.get("id"),
            "title": ev.get("summary", ""),
            "start": ev.get("start", {}).get("dateTime", ev.get("start", {}).get("date", "")),
            "end": ev.get("end", {}).get("dateTime", ev.get("end", {}).get("date", "")),
            "location": ev.get("location", ""),
            "attendees": [a.get("email") for a in ev.get("attendees", [])],
            "url": ev.get("htmlLink", ""),
        } for ev in events]
        return json.dumps({"ok": True, "event_count": len(summary),
                           "events": summary}, ensure_ascii=False)

    # Browser path: open Google Calendar and scrape visible events from the DOM
    webbrowser.open("https://calendar.google.com/calendar/r/week")
    await asyncio.sleep(3.5)  # wait for calendar to load
    raw = await _bridge_extract("gcal")
    if raw is None:
        js = (
            "(function(){"
            "var chips=document.querySelectorAll('[data-eventid],[data-eventchip-action],.KF4T6b,.lKHqkb');"
            "var evs=[];"
            "for(var i=0;i<Math.min(chips.length," + str(max_results) + ");i++){"
            "var c=chips[i];"
            "evs.push({title:c.getAttribute('data-tooltip')||c.getAttribute('aria-label')||c.innerText.slice(0,80)});"
            "}"
            "return JSON.stringify(evs);"
            "})()"
        )
        raw = await _chrome_js(js)
    try:
        events_dom = json.loads(raw or "[]")
    except Exception:
        events_dom = []
    return json.dumps({"ok": True, "event_count": len(events_dom),
                       "events": events_dom, "method": "browser_dom",
                       "note": "Read from Google Calendar in browser. For structured data, set up optional OAuth."},
                      ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  Document Vision / Analysis Tool
# ═══════════════════════════════════════════════════════════════

@registry.register(
    name="gworkspace_analyze",
    description=(
        "Analyse the content of a currently-open Google Workspace document "
        "(Docs, Sheets, or Slides) using the vision model and DOM extraction. "
        "Returns a structured summary of the document's content, layout, and key "
        "information — no manual copy-pasting needed. The agent can store the "
        "summary in memory for future reference."
    ),
    parameters={
        "type": "object",
        "properties": {
            "doc_url_or_id": {
                "type": "string",
                "description": "Document URL or ID. If empty, analyses whatever is currently open."
            },
            "focus": {
                "type": "string",
                "description": (
                    "Optional focus area: 'full' for complete analysis, 'summary' for key points, "
                    "'data' for tables/numbers, 'structure' for document outline."
                ),
            },
        },
        "required": [],
    },
)
async def gworkspace_analyze(doc_url_or_id: str = "", focus: str = "summary") -> str:
    """
    Multi-channel document analysis:
    1. Try REST API for structured content (Docs/Sheets)
    2. Fall back to DOM extraction via Chrome JS
    3. Layer in screenshot-based vision analysis for visual elements
    """
    analysis: dict = {"focus": focus}

    token = _load_token()
    doc_id = _extract_doc_id(doc_url_or_id) if doc_url_or_id else ""

    # ── Determine document type from URL or active page ──
    doc_type = ""
    active_url = ""
    if doc_url_or_id:
        lower = doc_url_or_id.lower()
        if "document" in lower or "docs.new" in lower:
            doc_type = "docs"
        elif "spreadsheet" in lower or "sheets" in lower:
            doc_type = "sheets"
        elif "presentation" in lower or "slides" in lower:
            doc_type = "slides"

    # Always check the active Chrome tab — helps when a bare ID is passed
    # (no URL keywords to match) or when no URL is given at all.
    if not doc_type or not doc_url_or_id:
        active_url = await _osascript(
            'tell application "Google Chrome" to get URL of active tab of front window'
        )
        if active_url:
            if not doc_id:
                doc_id = _extract_doc_id(active_url)
            if not doc_type:
                if "document" in active_url or "docs.google.com" in active_url:
                    doc_type = "docs"
                elif "spreadsheet" in active_url or "sheets" in active_url:
                    doc_type = "sheets"
                elif "presentation" in active_url or "slides" in active_url:
                    doc_type = "slides"
            analysis["detected_url"] = active_url

    analysis["doc_type"] = doc_type or "unknown"
    analysis["doc_id"] = doc_id

    # ── Channel 1: REST API (structured data) ──
    if token and doc_id:
        if doc_type == "docs":
            doc = await _gapi("GET",
                              f"https://docs.googleapis.com/v1/documents/{doc_id}",
                              token=token)
            title = doc.get("title", "")
            body_content = doc.get("body", {}).get("content", [])
            text_parts: list[str] = []
            headings: list[str] = []
            for element in body_content:
                para = element.get("paragraph", {})
                style = para.get("paragraphStyle", {}).get("namedStyleType", "")
                para_text = ""
                for pe in para.get("elements", []):
                    para_text += pe.get("textRun", {}).get("content", "")
                if para_text.strip():
                    text_parts.append(para_text)
                    if "HEADING" in style:
                        headings.append(para_text.strip())
            full_text = "".join(text_parts)
            analysis["title"] = title
            analysis["char_count"] = len(full_text)
            analysis["word_count"] = len(full_text.split())
            analysis["headings"] = headings[:20]
            analysis["text_preview"] = full_text[:6000]

        elif doc_type == "sheets":
            url = f"https://sheets.googleapis.com/v4/spreadsheets/{doc_id}/values/Sheet1"
            result = await _gapi("GET", url, token=token)
            values = result.get("values", [])
            analysis["sheet_name"] = "Sheet1"
            analysis["row_count"] = len(values)
            analysis["col_count"] = max(len(r) for r in values) if values else 0
            analysis["headers"] = values[0] if values else []
            analysis["sample_rows"] = values[:20]

        elif doc_type == "slides":
            pres = await _gapi("GET",
                               f"https://slides.googleapis.com/v1/presentations/{doc_id}",
                               token=token)
            title = pres.get("title", "")
            slides_list = pres.get("slides", [])
            slide_summaries: list[dict] = []
            for i, s in enumerate(slides_list):
                texts: list[str] = []
                for pe in s.get("pageElements", []):
                    shape = pe.get("shape", {})
                    text_body = shape.get("text", {})
                    for te in text_body.get("textElements", []):
                        run = te.get("textRun", {})
                        if run.get("content", "").strip():
                            texts.append(run["content"].strip())
                slide_summaries.append({
                    "slide_number": i + 1,
                    "texts": texts,
                })
            analysis["title"] = title
            analysis["slide_count"] = len(slides_list)
            analysis["slides"] = slide_summaries[:30]

        analysis["method"] = "rest_api"
    else:
        # ── Channel 2: DOM/Clipboard/Vision extraction fallback ──
        if doc_type == "docs":
            # _gdocs_read_body: DOM bridge → focused clipboard → vision OCR
            text = await _gdocs_read_body()
            analysis["method"] = "gdocs_read_body"
        else:
            # For Sheets/Slides/unknown, try bridge body then generic DOM
            text = await _bridge_extract("body")
            if text is not None:
                analysis["method"] = "browser_bridge"
            else:
                js = "(function(){var t=document.body.innerText;return t?t.substring(0,8000):''})()"
                text = await _chrome_js(js)
                analysis["method"] = "browser_dom"

            # If the bridge returned mostly toolbar/UI text for what might be a doc,
            # fall back to the proper doc-body reader
            if text and active_url and "docs.google.com/document" in active_url:
                if text.startswith("Untitled document") or text.startswith("Close menu"):
                    print("[gworkspace_analyze] Detected toolbar text — retrying with _gdocs_read_body")
                    doc_text = await _gdocs_read_body()
                    if doc_text and len(_normalize_doc_text(doc_text)) > len(_normalize_doc_text(text or "")):
                        text = doc_text
                        analysis["method"] = "gdocs_read_body_fallback"

        analysis["text_preview"] = (text or "")[:8000]

    analysis["ok"] = True
    return json.dumps(analysis, ensure_ascii=False)
