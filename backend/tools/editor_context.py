"""
ClarityOS — active editor file resolution
=========================================

VS Code renders its editor into a Chromium surface, so UI Automation exposes
the window and one opaque text area — the window title and nothing of the
buffer. "Explain this code" therefore cannot be grounded from `get_ui_tree`.

What the title *does* give us is the filename and the workspace:

    "env.example file - ClarityOS-windows - Visual Studio Code"
     └── file                └── workspace

This module turns that into a *validated* path and reads it under tight rules.
It is deliberately not a general filesystem tool:

  * only files whose names appear in a recognised editor window title,
  * only inside a known workspace root,
  * never inside a directory `file_tools` already treats as restricted,
  * never a secret-bearing filename (`.env`, keys, credential stores…),
  * size-capped, text-only, and secret-shaped values redacted before return.

`env.example` is explicitly *not* trusted: example files routinely get real
keys pasted into them, so it is treated like any other `.env` member and the
redactor runs over whatever is returned.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Tuple

MAX_FILE_BYTES = 200_000
MAX_RETURN_CHARS = 14_000
_MAX_ROOT_DEPTH = 3
_MAX_FILE_DEPTH = 7

# Window-title shapes we understand. VS Code: "<file> - <workspace> - Visual Studio Code".
_EDITOR_TITLE_RE = re.compile(
    r"^(?P<file>[^-—]+?)\s*(?:file)?\s+-\s+(?P<workspace>[^-—]+?)\s+-\s+"
    r"(?:Visual Studio Code|VSCodium|Cursor)\s*$",
    re.IGNORECASE,
)

# Filenames that carry secrets regardless of where they live.
_SENSITIVE_NAME_RE = re.compile(
    r"""(?ix)
    ^(?:
        \.env(?:\..*)?            # .env, .env.local, .env.production
      | env(?:\..*)?              # env.example, env.local  — NOT assumed safe
      | .*\.(?:pem|key|pfx|p12|keystore|jks|ppk)
      | id_(?:rsa|dsa|ecdsa|ed25519)(?:\.pub)?
      | .*credentials.*
      | .*secret.*
      | tokens\.json
      | \.npmrc | \.netrc | \.pgpass | \.htpasswd
      | service[-_]?account.*\.json
    )$
    """,
)

# Value shapes worth masking even in a file we are willing to read.
_SECRET_LINE_RE = re.compile(
    r"""(?im)^(?P<prefix>\s*(?:export\s+)?[A-Z0-9_]*
        (?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH|BEARER|API)[A-Z0-9_]*\s*[:=]\s*)
        (?P<value>\S.*)$""",
    re.VERBOSE,
)
_LONG_OPAQUE_RE = re.compile(r"\b(?=[A-Za-z0-9_\-]{28,})(?=[^\s]*[0-9])[A-Za-z0-9_\-]{28,}\b")

_TEXT_SUFFIXES = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".json", ".md", ".txt", ".html", ".css",
    ".yml", ".yaml", ".toml", ".ini", ".cfg", ".sh", ".ps1", ".bat", ".java",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".go", ".rs", ".rb", ".php", ".sql",
    ".xml", ".vue", ".svelte", ".swift", ".kt", ".gradle", ".dockerfile", "",
}


_EDITOR_APPS = ("visual studio code", "vscodium", "cursor")


def parse_editor_title(title: str) -> Optional[Tuple[str, str]]:
    """
    (filename, workspace) from a recognised editor window title.

    Split on the " - " separator rather than pattern-matching the segments:
    workspace names routinely contain hyphens ("ClarityOS-windows"), which a
    character-class regex cannot express without also swallowing the separator.
    """
    cleaned = (title or "").strip().lstrip("●*").strip()
    parts = [p.strip() for p in cleaned.split(" - ") if p.strip()]
    if len(parts) < 3:
        return None
    if parts[-1].lower() not in _EDITOR_APPS:
        return None

    filename = parts[0]
    # VS Code appends " file" for some unsaved/preview buffers.
    if filename.lower().endswith(" file"):
        filename = filename[: -len(" file")].strip()
    workspace = " - ".join(parts[1:-1]).strip()
    if not filename or not workspace:
        return None
    return filename, workspace


def looks_sensitive(path: str | os.PathLike) -> bool:
    """True when the filename itself implies secrets."""
    return bool(_SENSITIVE_NAME_RE.match(Path(path).name))


def redact(text: str) -> tuple[str, int]:
    """Mask secret-shaped values. Returns (redacted_text, masked_count)."""
    masked = 0

    def _mask_line(m: re.Match) -> str:
        nonlocal masked
        masked += 1
        return f"{m.group('prefix')}<redacted>"

    out = _SECRET_LINE_RE.sub(_mask_line, text or "")

    def _mask_blob(m: re.Match) -> str:
        nonlocal masked
        masked += 1
        return "<redacted>"

    out = _LONG_OPAQUE_RE.sub(_mask_blob, out)
    return out, masked


def _candidate_roots() -> list[Path]:
    configured = os.environ.get("CLARITYOS_EDITOR_ROOTS", "")
    roots = [Path(p) for p in configured.split(os.pathsep) if p.strip()]
    if not roots:
        home = Path.home()
        roots = [
            Path(__file__).resolve().parents[2],          # the ClarityOS repo itself
            home / "Desktop", home / "Documents", home / "source", home / "projects",
            home / "OneDrive" / "Desktop", home / "OneDrive" / "Documents",
        ]
    return [r for r in roots if r.exists()]


def _find_workspace(workspace: str) -> Optional[Path]:
    target = workspace.strip().lower()
    for root in _candidate_roots():
        if root.name.lower() == target:
            return root
        try:
            for depth, (dirpath, dirnames, _files) in enumerate(os.walk(root)):
                if Path(dirpath).relative_to(root).parts.__len__() >= _MAX_ROOT_DEPTH:
                    dirnames[:] = []
                    continue
                for name in dirnames:
                    if name.lower() == target:
                        return Path(dirpath) / name
        except Exception:
            continue
    return None


def resolve_active_editor_file(window_title: str) -> Tuple[Optional[Path], str]:
    """
    Resolve the file shown in an editor window title.

    Returns (path, reason). `path` is None when the file cannot be resolved
    safely; `reason` always explains why in user-facing terms.
    """
    parsed = parse_editor_title(window_title)
    if not parsed:
        return None, "That window title does not identify an editor file."
    filename, workspace = parsed

    root = _find_workspace(workspace)
    if root is None:
        return None, (f"I could not locate the '{workspace}' workspace on disk. "
                      f"Set CLARITYOS_EDITOR_ROOTS if it lives outside the usual folders.")

    match: Optional[Path] = None
    try:
        for dirpath, dirnames, filenames in os.walk(root):
            rel_depth = len(Path(dirpath).relative_to(root).parts)
            if rel_depth >= _MAX_FILE_DEPTH:
                dirnames[:] = []
                continue
            dirnames[:] = [d for d in dirnames
                           if d not in {".git", "node_modules", "__pycache__", ".venv", "venv", "dist"}]
            if filename in filenames:
                match = Path(dirpath) / filename
                break
    except Exception as exc:
        return None, f"Could not search the workspace ({exc.__class__.__name__})."

    if match is None:
        return None, f"'{filename}' was not found inside the '{workspace}' workspace."

    resolved = match.resolve()
    if root.resolve() not in resolved.parents and resolved != root.resolve():
        return None, "That file resolves outside its workspace; refusing to read it."

    try:
        from tools.file_tools import _is_path_restricted
        if _is_path_restricted(str(resolved)):
            return None, "That file is inside a protected location, so I will not read it."
    except Exception:
        pass

    if looks_sensitive(resolved):
        return None, (f"'{resolved.name}' is a secrets-bearing file, so I will not send its "
                      f"contents anywhere. Open the file you want explained, or tell me which "
                      f"part to look at.")

    if resolved.suffix.lower() not in _TEXT_SUFFIXES:
        return None, f"'{resolved.name}' is not a text/source file I can read."

    try:
        if resolved.stat().st_size > MAX_FILE_BYTES:
            return None, f"'{resolved.name}' is larger than {MAX_FILE_BYTES // 1000} KB."
    except OSError as exc:
        return None, f"Could not stat '{resolved.name}' ({exc.__class__.__name__})."

    return resolved, "ok"
