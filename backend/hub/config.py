"""
ClarityOS Hub — configuration, tokens, settings and secrets
===========================================================

* tokens.json   – three random tokens created on first run (never hard-coded):
                    ui      website  <-> hub        (REST bearer + WebSocket auth)
                    helper  Windows helper <-> hub
                    bridge  Chrome extension <-> hub (the "pairing code")
* settings.json – NON-secret preferences (providers, language, permissions, theme)
* secrets       – API keys, kept in the OS credential store via `keyring`
                  (Windows Credential Manager). If no keyring backend is available they
                  are held in memory for this session only and the UI says so.
                  Secret values are NEVER returned by any API — only "configured: true/false".
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from pathlib import Path
from typing import Any, Dict, Optional

VERSION = "0.1.0"
KEYRING_SERVICE = "ClarityOS"
# Secrets saved before the rename live under the old Credential Manager service.
# Reads fall back to it so an existing install does not lose its API keys; new
# writes always go to KEYRING_SERVICE.
LEGACY_KEYRING_SERVICE = "Moon" + "walk"

# Secrets the UI may configure. name -> human label
SECRET_NAMES = {
    "GEMINI_API_KEY": "Gemini (agent brain)",
    "GNANI_API_KEY": "Gnani (Vachana STT / Timbre TTS)",
    "BOLNA_API_KEY": "Bolna (phone voice agents)",
}


def user_dir() -> Path:
    env = os.environ.get("CLARITYOS_USER_DIR")
    if env:
        p = Path(env)
        p.mkdir(parents=True, exist_ok=True)
        return p

    if os.name == "nt":
        base = Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
        p, legacy = base / "ClarityOS", base / ("Moon" + "walk")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
        p, legacy = base / "clarityos", base / ("moon" + "walk")

    p.mkdir(parents=True, exist_ok=True)

    # Carry a pre-rename install forward rather than stranding its tokens and
    # pairing state, which would silently force the user to pair the extension
    # again. Copied file-by-file: the target directory usually already exists
    # (Electron creates it), so renaming the whole folder would never fire.
    if legacy.exists() and legacy.resolve() != p.resolve():
        for name in ("tokens.json", "settings.json", "hub.db"):
            src, dst = legacy / name, p / name
            try:
                if src.is_file() and not dst.exists():
                    dst.write_bytes(src.read_bytes())
                    print(f"[Config] Carried {name} over from the previous install.")
            except Exception as exc:
                print(f"[Config] Could not carry {name} over ({exc}).")

    return p


def _write_private(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")
    try:
        os.chmod(path, 0o600)  # best effort; on Windows the profile ACL already restricts it
    except OSError:
        pass


# ═══════════════════════════════════════════════════════════════
#  Tokens
# ═══════════════════════════════════════════════════════════════

def load_tokens() -> Dict[str, str]:
    path = user_dir() / "tokens.json"
    tokens: Dict[str, str] = {}
    if path.exists():
        try:
            tokens = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            tokens = {}
    changed = False
    for name in ("ui", "helper", "bridge"):
        if not isinstance(tokens.get(name), str) or len(tokens[name]) < 24:
            tokens[name] = secrets.token_urlsafe(32)
            changed = True
    if changed:
        _write_private(path, json.dumps(tokens, indent=2))
    return tokens


def rotate_token(name: str) -> str:
    tokens = load_tokens()
    if name not in tokens:
        raise KeyError(name)
    tokens[name] = secrets.token_urlsafe(32)
    _write_private(user_dir() / "tokens.json", json.dumps(tokens, indent=2))
    return tokens[name]


# ═══════════════════════════════════════════════════════════════
#  Settings (non-secret)
# ═══════════════════════════════════════════════════════════════

CAPABILITIES = ["browser", "desktop", "shell", "files_read", "files_write",
                "messaging", "calls", "google", "downloads"]
PERMISSION_MODES = ("allow", "ask", "deny")

DEFAULT_SETTINGS: Dict[str, Any] = {
    # Voice is Gnani: Prisma v2.5 over the realtime socket for STT, Timbre
    # v2.5 for TTS. There is no other vendor to fall back to — GNANI_API_KEY
    # is what makes the microphone and the speaker work.
    "stt_provider": "gnani-stream",    # gnani-stream | gnani
    "stt_language": "en-IN",           # en-IN also understands Hindi-English mixed speech
    "tts_provider": "gnani",           # gnani
    "tts_enabled": True,
    "tts_speed": 1.0,
    "gnani_tts_model": "timbre-v2.5",
    "gnani_tts_voice": "Poorvi",
    "gnani_tts_language": "hi-en",
    "bolna_agent_id": "",
    "voice_mode": "push_to_talk",      # push_to_talk | hands_free
    "theme": "system",                 # system | light | dark
    "auto_approve_sensitive": False,   # NEVER true by default
    "permissions": {
        "browser": "allow", "desktop": "allow", "shell": "ask", "files_read": "allow",
        "files_write": "ask", "messaging": "ask", "calls": "ask", "google": "allow",
        "downloads": "ask",
    },
}

_ENUMS = {
    # gnani-stream is the same Prisma v2.5 model over the realtime socket.
    "stt_provider": ("gnani", "gnani-stream"),
    "tts_provider": ("gnani",),
    "voice_mode": ("push_to_talk", "hands_free"),
    "theme": ("system", "light", "dark"),
}
_BOOLS = ("tts_enabled", "auto_approve_sensitive")
_STRINGS = ("stt_language", "gnani_tts_model", "gnani_tts_voice", "gnani_tts_language",
            "bolna_agent_id")


class SettingsError(ValueError):
    pass


class Settings:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or (user_dir() / "settings.json")
        self._lock = threading.Lock()
        self._data: Dict[str, Any] = json.loads(json.dumps(DEFAULT_SETTINGS))
        self._load()

    @staticmethod
    def _migrate(stored: Dict[str, Any]) -> Dict[str, Any]:
        """Bring a settings.json written before the Gnani-only switch forward.

        Without this, an existing install would hit the stricter enums below,
        the whole file would be rejected as invalid, and every other setting
        the user had chosen — permissions, theme, voice mode — would silently
        revert to defaults. Drop what no longer exists, point the rest at
        Gnani, and keep everything else.
        """
        if not isinstance(stored, dict):
            return stored
        migrated = dict(stored)
        for gone in ("stt_fallback", "sarvam_tts_speaker", "sarvam_tts_language"):
            migrated.pop(gone, None)
        if migrated.get("stt_provider") not in ("gnani", "gnani-stream"):
            if "stt_provider" in migrated:
                print(f"[Settings] stt_provider '{migrated['stt_provider']}' is retired; "
                      f"voice runs on Gnani — using gnani-stream")
            migrated["stt_provider"] = "gnani-stream"
        if migrated.get("tts_provider") != "gnani":
            if "tts_provider" in migrated:
                print(f"[Settings] tts_provider '{migrated['tts_provider']}' is retired; "
                      f"voice runs on Gnani — using gnani")
            migrated["tts_provider"] = "gnani"
        return migrated

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            self.update(self._migrate(stored), persist=False)
        except (OSError, ValueError, SettingsError):
            pass  # corrupt file -> keep defaults

    def get(self) -> Dict[str, Any]:
        with self._lock:
            return json.loads(json.dumps(self._data))

    def update(self, patch: Dict[str, Any], persist: bool = True) -> Dict[str, Any]:
        if not isinstance(patch, dict):
            raise SettingsError("settings must be an object")
        with self._lock:
            new = json.loads(json.dumps(self._data))
            for key, value in patch.items():
                if key == "permissions":
                    if not isinstance(value, dict):
                        raise SettingsError("permissions must be an object")
                    for cap, mode in value.items():
                        if cap not in CAPABILITIES:
                            raise SettingsError(f"unknown capability '{cap}'")
                        if mode not in PERMISSION_MODES:
                            raise SettingsError(f"invalid mode '{mode}' for '{cap}'")
                        new["permissions"][cap] = mode
                elif key in _ENUMS:
                    if value not in _ENUMS[key]:
                        raise SettingsError(f"invalid value for {key}")
                    new[key] = value
                elif key in _BOOLS:
                    if not isinstance(value, bool):
                        raise SettingsError(f"{key} must be true/false")
                    new[key] = value
                elif key == "tts_speed":
                    try:
                        speed = float(value)
                    except (TypeError, ValueError):
                        raise SettingsError("tts_speed must be a number")
                    if not 0.5 <= speed <= 2.0:
                        raise SettingsError("tts_speed must be between 0.5 and 2.0")
                    new[key] = round(speed, 2)
                elif key in _STRINGS:
                    if not isinstance(value, str) or len(value) > 120:
                        raise SettingsError(f"invalid value for {key}")
                    new[key] = value.strip()
                else:
                    raise SettingsError(f"unknown setting '{key}'")
            self._data = new
            if persist:
                _write_private(self.path, json.dumps(new, indent=2))
            return json.loads(json.dumps(new))


def apply_settings_to_env(s: Dict[str, Any]) -> None:
    """Push settings into the env vars the voice modules read, then reset their singletons."""
    env = {
        "CLARITYOS_STT_PROVIDER": s["stt_provider"],
        "CLARITYOS_STT_LANGUAGE": s["stt_language"],
        "CLARITYOS_TTS_PROVIDER": s["tts_provider"],
        "GNANI_TTS_MODEL": s["gnani_tts_model"],
        "GNANI_TTS_VOICE": s["gnani_tts_voice"],
        "GNANI_TTS_LANGUAGE": s["gnani_tts_language"],
        "GNANI_TTS_SPEED": str(s["tts_speed"]),
        "BOLNA_AGENT_ID": s["bolna_agent_id"],
    }
    for k, v in env.items():
        os.environ[k] = v
    try:
        from voice import stt, tts
        stt.reset_stt_provider()
        tts.reset_tts_engine()
    except Exception as e:  # voice modules optional in tests
        print(f"[Hub] voice reset skipped: {e}")


# ═══════════════════════════════════════════════════════════════
#  Secrets
# ═══════════════════════════════════════════════════════════════

class SecretStore:
    def __init__(self, use_keyring: bool = True):
        self._mem: Dict[str, str] = {}
        self._keyring = None
        self.persistent = False
        if use_keyring:
            try:
                import keyring
                from keyring.backends import fail
                backend = keyring.get_keyring()
                if not isinstance(backend, fail.Keyring):
                    self._keyring = keyring
                    self.persistent = True
            except Exception:
                self._keyring = None

    def get(self, name: str) -> str:
        if name in self._mem:
            return self._mem[name]
        if self._keyring:
            try:
                found = self._keyring.get_password(KEYRING_SERVICE, name) or ""
                if found:
                    return found
                # Pre-rename install: read it once from the old service so the
                # key keeps working. Not copied over — deleting the old entry
                # is the user's call, not ours.
                return self._keyring.get_password(LEGACY_KEYRING_SERVICE, name) or ""
            except Exception:
                return ""
        return ""

    def set(self, name: str, value: str) -> None:
        if name not in SECRET_NAMES:
            raise SettingsError(f"unknown secret '{name}'")
        value = (value or "").strip()
        if not value or len(value) > 512 or any(c in value for c in "\r\n"):
            raise SettingsError("invalid secret value")
        if self._keyring:
            try:
                self._keyring.set_password(KEYRING_SERVICE, name, value)
                self._mem.pop(name, None)
            except Exception:
                self._mem[name] = value
                self.persistent = False
        else:
            self._mem[name] = value
        os.environ[name] = value

    def delete(self, name: str) -> None:
        self._mem.pop(name, None)
        if self._keyring:
            try:
                self._keyring.delete_password(KEYRING_SERVICE, name)
            except Exception:
                pass
        os.environ.pop(name, None)

    def configured(self) -> Dict[str, bool]:
        return {n: bool(self.get(n) or os.environ.get(n)) for n in SECRET_NAMES}

    def load_into_env(self, override: bool = True) -> None:
        """
        Copy saved secrets into the process environment, where the tools read them.

        `override=False` fills only the names that are not already set, so a
        caller that has deliberately configured a key (an .env file, or a value
        the Electron main process injected when spawning the backend) keeps it.
        The hub keeps the default, where the credential store is authoritative.
        """
        for n in SECRET_NAMES:
            if not override and (os.environ.get(n) or "").strip():
                continue
            v = self.get(n)
            if v:
                os.environ[n] = v

    def values(self) -> list[str]:
        """Current secret values — used ONLY by the log redactor."""
        vals = [self.get(n) or os.environ.get(n, "") for n in SECRET_NAMES]
        return [v for v in vals if v and len(v) >= 8]
