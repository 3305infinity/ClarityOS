"""
ClarityOS — legacy environment variable names
=============================================

The project was renamed from its old codename, whose settings were spelled with
a different prefix. Everything in the codebase now reads ``CLARITYOS_*``.

Importing this module early mirrors any still-set legacy variable onto its
``CLARITYOS_*`` name, so an existing ``backend/.env``, shell profile or CI
config keeps working without edits. The new name always wins when both are set.

This is a compatibility shim, not an interface: new code should read
``CLARITYOS_*`` directly.
"""

from __future__ import annotations

import os

# Built at runtime so a project-wide prefix rename cannot silently rewrite the
# very constant that makes the old names keep working.
LEGACY_PREFIX = "MOON" + "WALK_"
CURRENT_PREFIX = "CLARITYOS_"


def apply() -> list[str]:
    """Mirror legacy names onto current ones. Returns the names it filled in."""
    filled: list[str] = []
    for key, value in list(os.environ.items()):
        if not key.startswith(LEGACY_PREFIX):
            continue
        new_key = CURRENT_PREFIX + key[len(LEGACY_PREFIX):]
        if not os.environ.get(new_key):
            os.environ[new_key] = value
            filled.append(new_key)
    return filled


# Applied on import so a single `import compat_env` at a process entry point is
# enough; it is idempotent and safe to import more than once.
_filled = apply()
if _filled:
    print(f"[Compat] Mapped {len(_filled)} legacy environment setting(s) onto "
          f"{CURRENT_PREFIX}* names; rename them when convenient.")
