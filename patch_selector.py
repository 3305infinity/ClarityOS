"""Route editor/code requests to the registered editor tool, and stop offering
open_app when the user is asking about an application that is already in front.

Written as a file so shell escaping cannot corrupt the patterns.
"""
import io

PATH = "backend/tools/selector.py"

OLD = '''        if (self._looks_like_inspection(user_request)
                and self._is_browser_app(context_app)
                and not self._asks_to_launch(user_request)):
            selected = [t for t in selected if t != "open_app"] if isinstance(selected, list) \\
                else {t for t in selected if t != "open_app"}
'''

NEW = '''        if (self._looks_like_inspection(user_request)
                and (context_app or context_url)
                and not self._asks_to_launch(user_request)):
            # Widened from browsers to any identified foreground app. Asking
            # "summarise the current VS Code window" was offering open_app, and
            # the executor duly launched a *second* VS Code instead of reading
            # the one already in front.
            selected = [t for t in selected if t != "open_app"] if isinstance(selected, list) \\
                else {t for t in selected if t != "open_app"}

        # Code/editor questions about an editor that is already open should be
        # able to reach the registered editor reader. It was in the registry but
        # never offered, so the planner fell back to screenshots, UI Automation,
        # or asking the user to paste the code.
        if (self._is_editor_app(context_app)
                and self._looks_like_code_request(user_request)
                and "read_active_editor_file" in self._all_tool_names):
            if "read_active_editor_file" not in selected:
                selected = (list(selected) + ["read_active_editor_file"]
                            if isinstance(selected, list)
                            else set(selected) | {"read_active_editor_file"})
'''

HELPERS_ANCHOR = '''    @staticmethod
    def _is_browser_app(context_app: str) -> bool:'''

HELPERS = '''    _EDITOR_APPS = {
        "visual studio code", "vscode", "code", "vscodium", "cursor",
        "visual studio", "sublime text", "notepad++", "pycharm", "intellij idea",
        "webstorm", "neovim", "vim", "emacs",
    }

    # Words that mean "the source the user is looking at", not a web page.
    _CODE_WORDS = (
        "code", "function", "method", "class", "algorithm", "complexity",
        "bug", "error in", "file", "script", "variable", "refactor", "syntax",
        "implementation", "snippet",
    )

    @classmethod
    def _is_editor_app(cls, context_app: str) -> bool:
        return (context_app or "").strip().lower() in cls._EDITOR_APPS

    @classmethod
    def _looks_like_code_request(cls, user_request: str) -> bool:
        low = " ".join((user_request or "").lower().split())
        if any(word in low for word in cls._CODE_WORDS):
            return True
        # "summarise the current VS Code window" names the editor itself.
        return any(app in low for app in ("vs code", "vscode", "visual studio code", "editor"))

''' + HELPERS_ANCHOR

with io.open(PATH, encoding="utf-8") as fh:
    src = fh.read()

assert OLD in src, "browser-only withhold block not found"
src = src.replace(OLD, NEW, 1)

assert HELPERS_ANCHOR in src, "helper anchor not found"
assert "_is_editor_app" not in src.split(HELPERS_ANCHOR)[0], "helpers already present"
src = src.replace(HELPERS_ANCHOR, HELPERS, 1)

with io.open(PATH, "w", encoding="utf-8") as fh:
    fh.write(src)

print("selector patched")
