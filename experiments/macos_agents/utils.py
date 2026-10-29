from __future__ import annotations

import asyncio
import json
import os
import re
import time
from pathlib import Path
from typing import Any, Optional


def ensure_directory(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def now_ms() -> int:
    return int(time.time() * 1000)


def parse_json_object(text: str) -> dict[str, Any]:
    raw = str(text or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        if start >= 0 and end > start:
            try:
                parsed = json.loads(raw[start:end])
                return parsed if isinstance(parsed, dict) else {}
            except json.JSONDecodeError:
                return {}
    return {}


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True)


def write_json(path: Path, data: Any) -> None:
    ensure_directory(path.parent)
    path.write_text(json_dumps(data) + "\n", encoding="utf-8")


def write_text(path: Path, text: str) -> None:
    ensure_directory(path.parent)
    path.write_text(text, encoding="utf-8")


def escape_applescript_string(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


async def run_exec(*args: str, timeout: float = 10.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def run_shell(command: str, timeout: float = 10.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    return (
        proc.returncode,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


async def osascript(script: str, timeout: float = 8.0) -> str:
    code, stdout, stderr = await run_exec("osascript", "-e", script, timeout=timeout)
    if code != 0:
        return f"AppleScript error: {stderr or stdout}"
    return stdout


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def default_artifact_root() -> Path:
    return repo_root() / "experiments" / "macos_agents" / "artifacts"


def new_artifact_dir(prefix: str) -> Path:
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    slug = re.sub(r"[^a-z0-9_-]+", "-", prefix.lower()).strip("-") or "run"
    return ensure_directory(default_artifact_root() / f"{timestamp}-{slug}")


def read_env_value(name: str, default: str = "") -> str:
    value = os.environ.get(name, "").strip()
    return value or default


def shorten(text: str, limit: int = 240) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def merge_seed_context(base: Optional[dict[str, Any]], extra: Optional[dict[str, Any]]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    if base:
        merged.update(base)
    if extra:
        merged.update(extra)
    return merged

