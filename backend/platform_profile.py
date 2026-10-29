"""
ClarityOS — platform profile
============================
One place that answers "which OS is this running on?" for the LLM prompts and
the tool descriptions.

Why this exists: the planner and executor system prompts both opened with
"a macOS desktop AI assistant", and several tool descriptions said things like
"on the Mac" or "the macOS terminal". On a Windows install the model read those
and produced macOS plans — reaching for `osascript` through `run_shell` instead
of the native `get_running_apps` tool that the Windows helper already proxies.

Kept at the backend root (next to runtime_state.py) so both `agent.*` and
`tools.*` can import it without touching the pre-existing tools ↔ agent cycle.
"""

from __future__ import annotations

import os
import platform

__all__ = ["current_os", "is_windows", "native_shell", "platform_prompt_block", "describe_for_os"]


def current_os() -> str:
    """Friendly OS name used in prompts: 'Windows' | 'macOS' | 'Linux'."""
    system = platform.system()
    if system == "Darwin":
        return "macOS"
    if system == "Windows":
        return "Windows"
    return system or "Linux"


def is_windows() -> bool:
    return os.name == "nt"


def native_shell() -> str:
    return "PowerShell" if is_windows() else "zsh/bash"


def describe_for_os(mac_text: str, windows_text: str) -> str:
    """Pick the right wording for a tool description at registration time."""
    return windows_text if is_windows() else mac_text


def platform_prompt_block() -> str:
    """
    A short, explicit platform contract appended to the planner and executor
    system prompts. States the OS, and bans the *other* platform's scripting
    so the model cannot fall back to muscle memory.
    """
    os_name = current_os()
    if is_windows():
        wrong, wrong_eg = "AppleScript / osascript", "osascript -e 'tell application ...'"
    elif os_name == "macOS":
        wrong, wrong_eg = "PowerShell / cmd.exe", "Get-Process | Select-Object Name"
    else:
        wrong, wrong_eg = "AppleScript / osascript and PowerShell", "osascript / Get-Process"

    return f"""

## Platform — read this before choosing tools
This machine is running **{os_name}**. Its native shell is {native_shell()}.

1. Prefer a purpose-built tool over the shell. If a registered tool already does
   the job (for example listing running applications, opening an app, reading a
   window title), use it — it works on every supported platform and does not
   need the user's approval for something routine.
2. Never emit {wrong} on {os_name}. Commands like `{wrong_eg}` will fail here.
3. Only reach for `run_shell` when no registered tool covers the task. Shell
   commands are consequential, so the user is asked to approve every one of
   them — an unnecessary shell call stalls the task behind a prompt.
4. If you do use the shell, write it for {native_shell()}.
"""
