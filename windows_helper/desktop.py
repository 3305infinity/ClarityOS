"""
ClarityOS Windows helper — desktop tool implementations.

Design rules
  * Windows 10/11 only for actual actions. The module imports cleanly on any OS so its pure logic
    (key mapping, role mapping, shell policy, tree formatting) can be unit-tested anywhere.
  * Perception first: UI Automation (accessible controls) -> screenshot -> (hub-side) vision model.
  * The helper NEVER tries to bypass Windows security: it refuses to touch UAC / secure-desktop
    prompts, and it tells you when a target app runs elevated (Windows blocks input into it
    unless this helper is started elevated *by you*).
  * Every blocking tool checks `CANCEL` / `STOPPED` so a user "Stop" or the kill-switch hotkey
    takes effect immediately.

Tool functions are synchronous, take the same argument names as the macOS tools, and return str
(or dict for capture_screen). They are executed in worker threads by helper.py.
"""
from __future__ import annotations

import base64
import difflib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

IS_WIN = sys.platform == "win32"
VERSION = "0.1.0"

# ── cooperative stop ──
CANCEL = threading.Event()     # set by hub "cancel": abort current work (cleared by "arm")
STOPPED = threading.Event()    # kill-switch: refuse every input action until re-armed


class ToolError(Exception):
    """Reported to the agent as `ERROR: <message>`."""


def check_alive() -> None:
    if STOPPED.is_set() or CANCEL.is_set():
        raise ToolError("Stopped by the user — no further desktop actions will run until you start a new task.")


def sleep(seconds: float) -> None:
    """Interruptible sleep."""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        check_alive()
        time.sleep(min(0.05, max(0.0, end - time.monotonic())))


# ═══════════════════════════════════════════════════════════════
#  Pure helpers (testable anywhere)
# ═══════════════════════════════════════════════════════════════

# UI Automation ControlType -> the AX* role names the agent / glance / click_ui already understand
UIA_TO_AX = {
    "ButtonControl": "AXButton", "SplitButtonControl": "AXButton", "EditControl": "AXTextField",
    "DocumentControl": "AXTextArea", "CheckBoxControl": "AXCheckBox", "RadioButtonControl": "AXRadioButton",
    "ComboBoxControl": "AXComboBox", "MenuItemControl": "AXMenuItem", "MenuControl": "AXMenu",
    "MenuBarControl": "AXMenuBar", "HyperlinkControl": "AXLink", "TabItemControl": "AXRadioButton",
    "TabControl": "AXTabGroup", "ListControl": "AXList", "ListItemControl": "AXRow", "DataItemControl": "AXRow",
    "TreeItemControl": "AXRow", "TreeControl": "AXOutline", "TextControl": "AXStaticText",
    "ImageControl": "AXImage", "WindowControl": "AXWindow", "PaneControl": "AXGroup", "GroupControl": "AXGroup",
    "ToolBarControl": "AXToolbar", "SliderControl": "AXSlider", "ScrollBarControl": "AXScrollBar",
    "SpinnerControl": "AXIncrementor", "TableControl": "AXTable", "TitleBarControl": "AXGroup",
    "ProgressBarControl": "AXProgressIndicator", "ToolTipControl": "AXHelpTag", "StatusBarControl": "AXGroup",
}
INTERACTIVE = {"AXButton", "AXTextField", "AXTextArea", "AXCheckBox", "AXRadioButton", "AXComboBox",
               "AXMenuItem", "AXLink", "AXSlider", "AXIncrementor", "AXRow", "AXMenu", "AXMenuBar"}

_MOD_MAP = {
    "cmd": "ctrl", "command": "ctrl", "⌘": "ctrl", "super": "win", "meta": "win", "win": "win", "windows": "win",
    "ctrl": "ctrl", "control": "ctrl", "⌃": "ctrl", "alt": "alt", "option": "alt", "opt": "alt", "⌥": "alt",
    "shift": "shift", "⇧": "shift",
}
_KEY_MAP = {
    "return": "enter", "enter": "enter", "esc": "esc", "escape": "esc", "delete": "backspace", "backspace": "backspace",
    "forwarddelete": "delete", "del": "delete", "space": "space", "spacebar": "space", "tab": "tab",
    "up": "up", "down": "down", "left": "left", "right": "right", "home": "home", "end": "end",
    "pageup": "pageup", "pagedown": "pagedown", "capslock": "capslock", "`": "`", "~": "`",
}
_FRIENDLY = {
    "chrome": "Google Chrome", "msedge": "Microsoft Edge", "firefox": "Firefox", "brave": "Brave", "code": "Visual Studio Code",
    "winword": "Microsoft Word", "excel": "Microsoft Excel", "powerpnt": "Microsoft PowerPoint", "outlook": "Outlook",
    "notepad": "Notepad", "calc": "Calculator", "calculatorapp": "Calculator", "explorer": "File Explorer",
    "whatsapp": "WhatsApp", "spotify": "Spotify", "telegram": "Telegram", "teams": "Microsoft Teams", "ms-teams": "Microsoft Teams",
    "slack": "Slack", "discord": "Discord", "windowsterminal": "Terminal", "powershell": "PowerShell", "cmd": "Command Prompt",
    "applicationframehost": "Windows App", "mspaint": "Paint", "snippingtool": "Snipping Tool", "zoom": "Zoom",
}
_ALIASES = {  # spoken names -> something Start / PATH can launch
    "chrome": "chrome", "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge", "firefox": "firefox",
    "notepad": "notepad", "calculator": "calc", "calc": "calc", "paint": "mspaint", "file explorer": "explorer",
    "explorer": "explorer", "files": "explorer", "terminal": "wt", "cmd": "cmd", "command prompt": "cmd",
    "powershell": "powershell", "settings": "ms-settings:", "task manager": "taskmgr", "word": "winword",
    "excel": "excel", "powerpoint": "powerpnt", "outlook": "outlook", "vs code": "code", "vscode": "code",
    "visual studio code": "code", "snipping tool": "snippingtool",
}
_SECURE_PROCS = {"consent", "logonui", "winlogon", "credentialuibroker", "securityhealthsystray"}

SHELL_BLOCK_RE = [re.compile(p, re.I) for p in (
    r"\bformat(-volume)?\b\s+[a-z]:", r"\bdiskpart\b", r"\bbcdedit\b", r"\bcipher\s+/w", r"\bvssadmin\s+delete",
    r"\brd\s+/s\s+/q\s+[a-z]:\\?(\s|$)", r"\bdel\s+(/\w+\s+)*[a-z]:\\(\s|$|\*)", r"\bRemove-Item\b(?=.*-Recurse)(?=.*\b[a-z]:\\(\s|$|\*))",
    r"\breg\s+delete\s+hk(lm|cr)", r"\bshutdown\b", r"\bRestart-Computer\b", r"\bStop-Computer\b", r"\bnet\s+user\b.*\/add",
    r"\bSet-ExecutionPolicy\b", r"\bInvoke-Expression\b.*\b(iwr|Invoke-WebRequest|curl|wget)\b",
    r"\b(iwr|Invoke-WebRequest|curl|wget)\b.*\|\s*(iex|Invoke-Expression)\b", r"\bpowershell\b.*-enc(odedcommand)?\b",
    r"\bmkfs\b", r"\bdd\s+if=", r"\brm\s+(-\w+\s+)*(/|~)(\s|$)",
)]


def shell_blocked(command: str) -> Optional[str]:
    for rx in SHELL_BLOCK_RE:
        if rx.search(command or ""):
            return f"matches blocked pattern /{rx.pattern[:40]}…/"
    return None


def parse_shortcut(spec: str) -> List[str]:
    """'Cmd+Shift+T' / 'ctrl + c' / 'command+option+esc' -> pyautogui key names for Windows."""
    parts = [p.strip().lower() for p in re.split(r"\s*\+\s*", spec.strip()) if p.strip()]
    if not parts:
        raise ToolError("empty shortcut")
    keys = []
    for p in parts:
        keys.append(_MOD_MAP.get(p) or _KEY_MAP.get(p) or p)
    # de-duplicate modifiers while preserving order (cmd+ctrl -> ctrl)
    seen, out = set(), []
    for k in keys:
        if k in ("ctrl", "alt", "shift", "win") and k in seen:
            continue
        seen.add(k)
        out.append(k)
    return out


def friendly_name(process_name: str) -> str:
    base = re.sub(r"\.exe$", "", process_name or "", flags=re.I)
    return _FRIENDLY.get(base.lower(), base)


def ui_line(role: str, name: str, x: int, y: int, w: int, h: int, depth: int) -> str:
    name = (name or "").replace('"', "'").replace("\n", " ").strip()[:120]
    return f'{"  " * depth}- [{role}] "{name}" at {x},{y} (size: {w}x{h})'


# ═══════════════════════════════════════════════════════════════
#  Windows plumbing
# ═══════════════════════════════════════════════════════════════

def _require_windows() -> None:
    if not IS_WIN:
        raise ToolError("This tool only runs on Windows.")


def init_process() -> None:
    """Per-monitor DPI awareness: screenshot pixels == click coordinates == UIA rectangles."""
    if not IS_WIN:
        return
    import ctypes
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass
    try:
        import pyautogui
        pyautogui.PAUSE = 0.02
        pyautogui.FAILSAFE = True      # slam the mouse into the top-left corner to abort
    except Exception:
        pass


def is_admin() -> bool:
    if not IS_WIN:
        return False
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def _user32():
    import ctypes
    return ctypes.windll.user32


def _fg_hwnd() -> int:
    return int(_user32().GetForegroundWindow() or 0)


def _text(hwnd: int) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(512)
    _user32().GetWindowTextW(hwnd, buf, 512)
    return buf.value


def _class(hwnd: int) -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(256)
    _user32().GetClassNameW(hwnd, buf, 256)
    return buf.value


def _pid(hwnd: int) -> int:
    import ctypes
    pid = ctypes.c_ulong()
    _user32().GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


def _proc_name(pid: int) -> str:
    """Executable name for a pid, e.g. "spotify.exe".

    psutil is declared in windows_helper/requirements.txt, but these tools now
    also run in-process inside backend/.venv (servers/windows_local_tools.py),
    where it is not installed. The import failed silently for every process, so
    get_running_apps() filtered everything out and reported "No visible
    applications found" on a desktop full of windows — which in turn made every
    app-launch confirmation fail.
    """
    if not pid:
        return ""
    try:
        import psutil
        return psutil.Process(pid).name()
    except ImportError:
        pass
    except Exception:
        return ""

    # Win32 fallback: no third-party package needed.
    import ctypes
    from ctypes import wintypes
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    k32 = ctypes.windll.kernel32
    handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return ""
    try:
        size = wintypes.DWORD(260)
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return buf.value.rsplit("\\", 1)[-1]
        return ""
    except Exception:
        return ""
    finally:
        k32.CloseHandle(handle)


def _pid_elevated(pid: int) -> bool:
    """True if the process runs elevated (Administrator)."""
    import ctypes
    from ctypes import wintypes
    k32, adv = ctypes.windll.kernel32, ctypes.windll.advapi32
    h = k32.OpenProcess(0x1000, False, pid)        # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return False
    try:
        tok = wintypes.HANDLE()
        if not adv.OpenProcessToken(h, 0x0008, ctypes.byref(tok)):   # TOKEN_QUERY
            return False
        try:
            elev = wintypes.DWORD()
            size = wintypes.DWORD()
            if adv.GetTokenInformation(tok, 20, ctypes.byref(elev), ctypes.sizeof(elev), ctypes.byref(size)):  # TokenElevation
                return bool(elev.value)
        finally:
            k32.CloseHandle(tok)
    finally:
        k32.CloseHandle(h)
    return False


def assert_can_control_foreground() -> None:
    """Refuse UAC / secure-desktop prompts and elevated windows we are not allowed to drive."""
    _require_windows()
    hwnd = _fg_hwnd()
    if not hwnd:
        return
    pid = _pid(hwnd)
    name = re.sub(r"\.exe$", "", _proc_name(pid), flags=re.I).lower()
    title = _text(hwnd).lower()
    if name in _SECURE_PROCS or "user account control" in title:
        raise ToolError("A Windows security prompt (UAC / sign-in) is in front. ClarityOS never interacts with these — "
                        "please respond to it yourself, then ask me to continue.")
    if not is_admin() and _pid_elevated(pid):
        raise ToolError(f"'{friendly_name(name)}' is running as Administrator, and Windows blocks normal programs from "
                        "controlling it. ClarityOS will not bypass this. Close it and reopen it normally, or start the "
                        "ClarityOS helper as Administrator yourself if you really want that.")


# ═══════════════════════════════════════════════════════════════
#  Perception tools
# ═══════════════════════════════════════════════════════════════

def get_active_app() -> str:
    _require_windows()
    hwnd = _fg_hwnd()
    return friendly_name(_proc_name(_pid(hwnd))) if hwnd else ""


def get_window_title() -> str:
    _require_windows()
    hwnd = _fg_hwnd()
    return _text(hwnd) if hwnd else ""


def get_clipboard() -> str:
    _require_windows()
    try:
        import pyperclip
        return (pyperclip.paste() or "")[:4000]
    except Exception:
        return ""


_display_origin: Tuple[int, int] = (0, 0)


def capture_screen() -> Dict[str, Any]:
    """Screenshot of the monitor that holds the foreground window (JPEG, 1:1 with click coordinates)."""
    global _display_origin
    _require_windows()
    import mss
    from PIL import Image
    with mss.mss() as sct:
        monitors = sct.monitors[1:] or [sct.monitors[0]]
        target = monitors[0]
        hwnd = _fg_hwnd()
        if hwnd:
            import ctypes
            from ctypes import wintypes
            r = wintypes.RECT()
            _user32().GetWindowRect(hwnd, ctypes.byref(r))
            cx, cy = (r.left + r.right) // 2, (r.top + r.bottom) // 2
            for m in monitors:
                if m["left"] <= cx < m["left"] + m["width"] and m["top"] <= cy < m["top"] + m["height"]:
                    target = m
                    break
        shot = sct.grab(target)
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    _display_origin = (target["left"], target["top"])
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=80)
    return {"jpeg_b64": base64.b64encode(buf.getvalue()).decode("ascii"), "width": img.width, "height": img.height,
            "origin_x": target["left"], "origin_y": target["top"]}


def capture_region_screenshot(x: int, y: int, width: int, height: int) -> str:
    _require_windows()
    if width <= 0 or height <= 0:
        return "ERROR: Width and height must be positive"
    import mss
    from PIL import Image
    with mss.mss() as sct:
        shot = sct.grab({"left": int(x), "top": int(y), "width": int(width), "height": int(height)})
        img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    d = os.path.join(tempfile.gettempdir(), "clarityos")
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"region_{int(time.time())}.png")
    img.save(path)
    return f"Screenshot saved to {path}"


def _find_top_window(uia, app_name: str):
    app = (app_name or "").lower().strip()
    for w in uia.GetRootControl().GetChildren():
        try:
            pname = friendly_name(_proc_name(w.ProcessId)).lower()
            if app in (w.Name or "").lower() or app in pname:
                return w
        except Exception:
            continue
    return None


def get_ui_tree(app_name: str = "", search_term: str = "") -> str:
    """Accessible controls of a window as `- [AXRole] "Name" at x,y (size: wxh)` lines (same format as macOS)."""
    _require_windows()
    import uiautomation as uia
    uia.SetGlobalSearchTimeout(2.0)
    root = _find_top_window(uia, app_name) if app_name else uia.GetForegroundControl()
    if root is None:
        return f"ERROR: Window for '{app_name}' not found. Use get_running_apps to see what is open."
    try:
        root = root.GetTopLevelControl() or root
    except Exception:
        pass
    search = (search_term or "").lower().strip()
    lines: List[str] = []
    title = root.Name or ""
    cls = root.ClassName or ""
    hint = ""
    if cls == "#32770" or re.search(r"(?i)\b(error|warning|permission|allow|confirm|are you sure|save changes)\b", title):
        hint = "  [DIALOG DETECTED — read it before continuing]"
    lines.append(f"Window: {title} ({friendly_name(_proc_name(root.ProcessId))}){hint}")
    deadline = time.monotonic() + 6.0
    count = 0
    stack: List[Tuple[Any, int]] = [(root, 0)]
    while stack and count < 450 and time.monotonic() < deadline:
        check_alive()
        node, depth = stack.pop()
        try:
            rect = node.BoundingRectangle
            w, h = rect.width(), rect.height()
            role = UIA_TO_AX.get(node.ControlTypeName, "AXGroup")
            name = node.Name or ""
            if w > 0 and h > 0 and (name or role in INTERACTIVE) and not node.IsOffscreen:
                if not search or search in name.lower() or search in (node.AutomationId or "").lower():
                    lines.append(ui_line(role, name or node.AutomationId or "", rect.left, rect.top, w, h, min(depth, 6)))
                    count += 1
            if depth < 9:
                children = node.GetChildren()
                for c in reversed(children):
                    stack.append((c, depth + 1))
        except Exception:
            continue
    if count == 0:
        lines.append("(No accessible controls found — this app may not expose UI Automation. Use read_screen to look at the screen instead.)")
    return "\n".join(lines)


def get_running_apps() -> str:
    _require_windows()
    import ctypes
    from ctypes import wintypes
    found: Dict[str, str] = {}
    fg = _fg_hwnd()
    fg_name = friendly_name(_proc_name(_pid(fg))) if fg else ""
    user32 = _user32()
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _text(hwnd) and not (user32.GetWindowLongW(hwnd, -20) & 0x80):  # skip tool windows
            name = friendly_name(_proc_name(_pid(hwnd)))
            if name and name not in ("Windows App",):
                found.setdefault(name, _text(hwnd))
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not found:
        return "No visible applications found."
    out = [f"{n}{'  (frontmost)' if n == fg_name else ''}" for n in sorted(found)]
    return "Running applications: " + ", ".join(out)


# ═══════════════════════════════════════════════════════════════
#  Input tools
# ═══════════════════════════════════════════════════════════════

def _origin() -> Tuple[int, int]:
    return _display_origin


def click_element(x: int, y: int, click_type: str = "single") -> str:
    check_alive()
    assert_can_control_foreground()
    import pyautogui
    ox, oy = _origin()
    ax, ay = int(x) + ox, int(y) + oy
    pyautogui.moveTo(ax, ay, duration=0.12)
    check_alive()
    if click_type == "double":
        pyautogui.doubleClick()
    elif click_type == "right":
        pyautogui.rightClick()
    else:
        pyautogui.click()
    return f"Clicked ({x}, {y}) [{click_type}]"


def mouse_action(action: str, x: int = 0, y: int = 0, x2: int = 0, y2: int = 0, lines: int = 5) -> str:
    check_alive()
    assert_can_control_foreground()
    import pyautogui
    ox, oy = _origin()
    if action == "move":
        pyautogui.moveTo(x + ox, y + oy, duration=0.12)
        return f"Moved mouse to ({x}, {y})"
    if action == "scroll":
        pyautogui.scroll(int(lines) * 120)   # one wheel notch = 120; >0 up, <0 down (same as the macOS tool)
        return f"Scrolled {'up' if lines > 0 else 'down'} {abs(lines)} lines"
    if action == "drag":
        pyautogui.moveTo(x + ox, y + oy, duration=0.1)
        check_alive()
        pyautogui.dragTo(x2 + ox, y2 + oy, duration=0.4, button="left")
        return f"Dragged ({x}, {y}) → ({x2}, {y2})"
    return f"ERROR: unknown mouse action '{action}'"


# SendInput with KEYEVENTF_UNICODE — types ANY character (Devanagari/Hinglish/emoji) without touching the clipboard
def _send_unicode(text: str) -> None:
    import ctypes
    from ctypes import wintypes
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG), ("mouseData", wintypes.DWORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD), ("wParamH", wintypes.WORD)]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    send = ctypes.windll.user32.SendInput
    data = text.replace("\r\n", "\n").encode("utf-16-le")
    units = [int.from_bytes(data[i:i + 2], "little") for i in range(0, len(data), 2)]
    for n, unit in enumerate(units):
        if n % 20 == 0:
            check_alive()
        if unit == 0x0A:                       # newline -> Enter key
            vk_down = INPUT(type=1, ki=KEYBDINPUT(wVk=0x0D, wScan=0, dwFlags=0, time=0, dwExtraInfo=0))
            vk_up = INPUT(type=1, ki=KEYBDINPUT(wVk=0x0D, wScan=0, dwFlags=2, time=0, dwExtraInfo=0))
            send(2, (INPUT * 2)(vk_down, vk_up), ctypes.sizeof(INPUT))
        else:
            down = INPUT(type=1, ki=KEYBDINPUT(wVk=0, wScan=unit, dwFlags=0x0004, time=0, dwExtraInfo=0))
            up = INPUT(type=1, ki=KEYBDINPUT(wVk=0, wScan=unit, dwFlags=0x0004 | 0x0002, time=0, dwExtraInfo=0))
            send(2, (INPUT * 2)(down, up), ctypes.sizeof(INPUT))
        time.sleep(0.004)


def type_text(text: str) -> str:
    check_alive()
    assert_can_control_foreground()
    if len(text) > 5000:
        return "ERROR: text too long (max 5000 characters)"
    _send_unicode(text)
    return f"Typed {len(text)} characters"


def press_key(key: str, times: int = 1) -> str:
    check_alive()
    assert_can_control_foreground()
    import pyautogui
    k = _KEY_MAP.get(key.lower(), key.lower())
    times = max(1, min(int(times), 50))
    for _ in range(times):
        check_alive()
        pyautogui.press(k)
    return f"Pressed {key} ×{times}" if times > 1 else f"Pressed {key}"


def run_shortcut(keys: str) -> str:
    check_alive()
    assert_can_control_foreground()
    import pyautogui
    combo = parse_shortcut(keys)
    pyautogui.hotkey(*combo)
    return f"Pressed {'+'.join(combo)}"


# ── apps ──
_start_apps: Tuple[float, List[Dict[str, str]]] = (0.0, [])


def _powershell(script: str, timeout: float = 20.0) -> str:
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=timeout, creationflags=0x08000000 if IS_WIN else 0)
    return (r.stdout or "").strip()


def _list_start_apps() -> List[Dict[str, str]]:
    global _start_apps
    ts, cached = _start_apps
    if cached and time.time() - ts < 300:
        return cached
    try:
        raw = _powershell("Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
        data = json.loads(raw) if raw else []
        if isinstance(data, dict):
            data = [data]
        apps = [{"Name": str(a.get("Name", "")), "AppID": str(a.get("AppID", ""))} for a in data]
    except Exception:
        apps = []
    _start_apps = (time.time(), apps)
    return apps


def best_start_app(name: str, apps: List[Dict[str, str]]) -> Optional[Dict[str, str]]:
    want = name.lower().strip()
    names = [a["Name"].lower() for a in apps]
    if want in names:
        return apps[names.index(want)]
    subs = [a for a in apps if want in a["Name"].lower()]
    if subs:
        return sorted(subs, key=lambda a: len(a["Name"]))[0]
    close = difflib.get_close_matches(want, names, n=1, cutoff=0.6)
    return apps[names.index(close[0])] if close else None


def open_app(app_name: str) -> str:
    check_alive()
    _require_windows()
    want = (app_name or "").strip()
    if not want or len(want) > 100:
        return "ERROR: invalid app name"
    alias = _ALIASES.get(want.lower())
    launched = ""
    try:
        if alias and alias.endswith(":"):
            os.startfile(alias)                        # ms-settings:
            launched = want
        elif alias and shutil.which(alias):
            subprocess.Popen([shutil.which(alias)])
            launched = want
        else:
            hit = best_start_app(want, _list_start_apps())
            if hit:
                subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{hit['AppID']}"])
                launched = hit["Name"]
            elif alias:
                subprocess.Popen(["cmd", "/c", "start", "", alias], creationflags=0x08000000)
                launched = want
    except Exception as e:
        return f"ERROR: could not launch '{want}': {e}"
    if not launched:
        return f"ERROR: No installed app matching '{want}' was found."
    tokens = [t for t in re.split(r"\W+", launched.lower()) if len(t) > 2] or [launched.lower()]
    for _ in range(30):
        sleep(0.1)
        fg = get_active_app().lower()
        if any(t in fg for t in tokens):
            return f"Opened {launched} (now in front)"
    return f"Launched {launched}; its window may still be loading"


def close_window() -> str:
    check_alive()
    assert_can_control_foreground()
    hwnd = _fg_hwnd()
    if not hwnd or _class(hwnd) in ("Progman", "WorkerW", "Shell_TrayWnd"):
        return "ERROR: no closable window is in front"
    title = _text(hwnd)
    _user32().PostMessageW(hwnd, 0x0010, 0, 0)       # WM_CLOSE — the app may ask to save first
    return f"Asked '{title}' to close"


def quit_app(app_name: str) -> str:
    check_alive()
    _require_windows()
    import ctypes
    from ctypes import wintypes
    want = (app_name or "").lower().strip()
    if not want:
        return "ERROR: app_name required"
    user32 = _user32()
    targets: List[int] = []
    EnumProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if user32.IsWindowVisible(hwnd) and _text(hwnd):
            pname = friendly_name(_proc_name(_pid(hwnd))).lower()
            if want in pname:
                targets.append(hwnd)
        return True

    user32.EnumWindows(EnumProc(cb), 0)
    if not targets:
        return f"'{app_name}' is not running"
    for hwnd in targets:
        user32.PostMessageW(hwnd, 0x0010, 0, 0)      # graceful WM_CLOSE only — never force-kill (unsaved work)
    sleep(2.0)
    still = [h for h in targets if user32.IsWindow(h) and user32.IsWindowVisible(h)]
    if still:
        return f"Asked {app_name} to quit, but it is still open (it may be asking to save). I did not force-close it."
    return f"Quit {app_name}"


def set_volume(level: int) -> str:
    _require_windows()
    level = max(0, min(100, int(level)))
    try:
        import comtypes
        comtypes.CoInitialize()
        from ctypes import POINTER, cast
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        dev = AudioUtilities.GetSpeakers()
        if hasattr(dev, "EndpointVolume"):
            vol = dev.EndpointVolume
        else:
            iface = dev.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = cast(iface, POINTER(IAudioEndpointVolume))
        vol.SetMasterVolumeLevelScalar(level / 100.0, None)
        return f"Volume set to {level}%"
    except Exception:
        import pyautogui                              # fallback: each media key press = 2%
        for _ in range(50):
            pyautogui.press("volumedown")
        for _ in range(level // 2):
            pyautogui.press("volumeup")
        return f"Volume set to about {level}%"


def clipboard_ops(action: str, text: str = "", image_path: str = "") -> str:
    _require_windows()
    import pyperclip
    if action == "get":
        return get_clipboard() or "(clipboard is empty)"
    if action == "set":
        pyperclip.copy(text)
        return f"Copied {len(text)} characters to the clipboard"
    if action == "paste":
        check_alive()
        assert_can_control_foreground()
        import pyautogui
        if text:
            pyperclip.copy(text)
        pyautogui.hotkey("ctrl", "v")
        return "Pasted"
    if action == "set_image":
        return copy_image_to_clipboard(image_path)
    if action == "get_image_info":
        from PIL import ImageGrab
        img = ImageGrab.grabclipboard()
        if img is None or isinstance(img, list):
            return "No image on the clipboard"
        return f"Clipboard image: {img.width}x{img.height}"
    return f"ERROR: unknown clipboard action '{action}'"


def copy_image_to_clipboard(source: str) -> str:
    _require_windows()
    path = source
    if source.startswith(("http://", "https://")):
        import httpx
        r = httpx.get(source, timeout=20, follow_redirects=True)
        r.raise_for_status()
        path = os.path.join(tempfile.gettempdir(), f"clarityos_img_{int(time.time())}")
        with open(path, "wb") as f:
            f.write(r.content[:15_000_000])
    path = os.path.expandvars(os.path.expanduser(path))
    if not os.path.isfile(path):
        return f"ERROR: image not found: {source}"
    ps = ("Add-Type -AssemblyName System.Windows.Forms,System.Drawing; "
          f"$i=[System.Drawing.Image]::FromFile('{path.replace(chr(39), chr(39) * 2)}'); [System.Windows.Forms.Clipboard]::SetImage($i)")
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-STA", "-Command", ps], capture_output=True, timeout=20,
                   creationflags=0x08000000)
    return "Image copied to the clipboard"


def window_manager(action: str, x: int = 0, y: int = 0, width: int = 800, height: int = 600, layout_type: str = "") -> str:
    check_alive()
    assert_can_control_foreground()
    import ctypes
    from ctypes import wintypes
    user32 = _user32()
    hwnd = _fg_hwnd()
    if not hwnd:
        return "ERROR: no window in front"
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    if action == "get":
        return f"Window '{_text(hwnd)}' at ({r.left}, {r.top}) size {r.right - r.left}x{r.bottom - r.top}"
    SWP = 0x0004 | 0x0010   # NOZORDER | NOACTIVATE
    if action == "move":
        user32.SetWindowPos(hwnd, 0, int(x), int(y), 0, 0, SWP | 0x0001)    # SWP_NOSIZE
        return f"Moved window to ({x}, {y})"
    if action == "resize":
        if width <= 0 or height <= 0:
            return "ERROR: Width and height must be positive"
        user32.SetWindowPos(hwnd, 0, 0, 0, int(width), int(height), SWP | 0x0002)   # SWP_NOMOVE
        return f"Resized window to {width}x{height}"
    if action == "layout":
        area = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(area), 0)       # SPI_GETWORKAREA
        W, H = area.right - area.left, area.bottom - area.top
        user32.ShowWindow(hwnd, 9)                                            # SW_RESTORE
        if layout_type == "fullscreen":
            user32.ShowWindow(hwnd, 3)                                        # SW_MAXIMIZE
        elif layout_type == "left_half":
            user32.SetWindowPos(hwnd, 0, area.left, area.top, W // 2, H, SWP)
        elif layout_type == "right_half":
            user32.SetWindowPos(hwnd, 0, area.left + W // 2, area.top, W // 2, H, SWP)
        elif layout_type == "center":
            w, h = int(W * 0.7), int(H * 0.75)
            user32.SetWindowPos(hwnd, 0, area.left + (W - w) // 2, area.top + (H - h) // 2, w, h, SWP)
        else:
            return f"ERROR: unknown layout '{layout_type}'"
        return f"Window layout: {layout_type}"
    return f"ERROR: unknown window action '{action}'"


def open_url(url: str) -> str:
    if not re.match(r"^https?://", url or ""):
        return "ERROR: only http(s) URLs can be opened"
    import webbrowser
    webbrowser.open(url, new=2)
    return f"Opened {url}"


def run_shell(command: str) -> str:
    check_alive()
    command = (command or "").strip()
    if not command:
        return "ERROR: empty command"
    why = shell_blocked(command)
    if why:
        return f"BLOCKED: command refused ({why})"
    try:
        r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
                           capture_output=True, text=True, timeout=30, cwd=os.path.expanduser("~"),
                           creationflags=0x08000000 if IS_WIN else 0)
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out after 30s"
    out = ((r.stdout or "") + (("\n[stderr] " + r.stderr) if r.stderr else "")).strip()
    if len(out) > 2000:
        out = out[:2000] + "\n…(truncated)"
    return out or f"(exit code {r.returncode}, no output)"


# name -> (function, group).  Groups are what `--allow` switches on/off in helper.py.
TOOLS: Dict[str, Tuple[Callable[..., Any], str]] = {
    "get_active_app": (get_active_app, "perceive"), "get_window_title": (get_window_title, "perceive"),
    "get_clipboard": (get_clipboard, "perceive"), "capture_screen": (capture_screen, "perceive"),
    "capture_region_screenshot": (capture_region_screenshot, "perceive"), "get_ui_tree": (get_ui_tree, "perceive"),
    "get_running_apps": (get_running_apps, "perceive"),
    "click_element": (click_element, "input"), "mouse_action": (mouse_action, "input"), "type_text": (type_text, "input"),
    "press_key": (press_key, "input"), "run_shortcut": (run_shortcut, "input"), "open_app": (open_app, "input"),
    "close_window": (close_window, "input"), "quit_app": (quit_app, "input"), "set_volume": (set_volume, "input"),
    "clipboard_ops": (clipboard_ops, "input"), "copy_image_to_clipboard": (copy_image_to_clipboard, "input"),
    "window_manager": (window_manager, "input"), "open_url": (open_url, "input"),
    "run_shell": (run_shell, "shell"),
}
