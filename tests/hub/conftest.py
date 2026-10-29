import os
import sys

BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
os.environ.setdefault("GEMINI_API_KEY", "test-not-real")

# The repo has a pre-existing import cycle (tools <-> agent): `agent` must be imported first.
import agent  # noqa: E402,F401
import tools  # noqa: E402,F401
