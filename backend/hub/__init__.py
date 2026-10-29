"""ClarityOS Hub — the single shared backend for the website, Chrome extension and Windows helper."""

# Legacy environment-variable names, mapped before any hub module reads config.
try:
    import compat_env  # noqa: F401
except Exception:
    pass
