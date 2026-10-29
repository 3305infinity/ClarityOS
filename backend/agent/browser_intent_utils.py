"""
Browser-intent helper predicates shared across tool selection and tests.
"""


BROWSER_UI_SHELL_PATTERNS = (
    "osascript",
    "system events",
    "google chrome",
    "safari",
    "arc",
    "brave",
    "firefox",
    "ui element",
    "front window",
    "window 1",
    "active tab",
)


def normalize_phrase(text: str) -> str:
    return " ".join((text or "").strip().lower().split())


def looks_like_browser_ui_shell_command(command: str) -> bool:
    lowered = (command or "").lower()
    return any(pattern in lowered for pattern in BROWSER_UI_SHELL_PATTERNS)


def is_browser_chrome_action(text: str) -> bool:
    normalized = normalize_phrase(text)
    chrome_phrases = (
        "switch to my",
        "switch to the",
        "go to my",
        "go to the",
        "next tab",
        "previous tab",
        "prev tab",
        "third tab",
        "second tab",
        "first tab",
        "last tab",
        "new tab",
        "close tab",
        "reopen tab",
        "duplicate tab",
        "pin tab",
        "reload tab",
        "refresh tab",
        "address bar",
        "omnibox",
        "go back",
        "go forward",
        "refresh page",
        "reload page",
    )
    if any(phrase in normalized for phrase in chrome_phrases):
        return True
    return " tab" in normalized and any(
        token in normalized
        for token in (
            "switch",
            "go to",
            "move to",
            "next",
            "previous",
            "prev",
            "close",
            "open",
            "new",
            "third",
            "second",
            "first",
            "last",
        )
    )
