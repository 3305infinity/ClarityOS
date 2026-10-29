# ClarityOS for Windows — Website + Chrome extension + Windows helper

```
Website (browser)  ──WS /ws──►  ClarityOS Hub  ◄──WS /helper──  Windows helper  (screen, mouse, keyboard, apps)
chat · voice · history           SPAV agent · policy · approvals ·
settings · approvals             task store · STT/TTS · cancel   Chrome extension ──WS :8765──►  Hub  (tabs, DOM, forms)
```
Everything runs on your PC and binds to `127.0.0.1`. The hub is the original ClarityOS agent (Sense → Plan → Act → Verify) — one tool registry for browser and desktop.

## 1. Quick start
1. Install **Python 3.10+** (tick *Add to PATH*) and **Chrome**.
2. In PowerShell, from this folder: `.\start-hub.ps1` — creates a venv, installs `backend\requirements-hub.txt`, starts the hub and opens the website with your private token in the URL (`#token=…`, never sent to the server).
3. Website → **Settings → API keys** → paste your **Gemini** key (the agent's brain). Keys go to Windows Credential Manager and are never shown again.
4. **Chrome extension:** `chrome://extensions` → Developer mode → *Load unpacked* → `chrome_extension\`. Then click the ClarityOS toolbar icon → **Pair this extension**, and click **Approve** in the ClarityOS window. The Chrome pill turns green. One approval, once — the code is provisioned over the loopback bridge and reused across restarts; nothing is copied by hand. Pasting the code from *Settings → Connections* into the extension's Options still works as a fallback.
5. **Windows helper (optional, for whole-PC control):** in a second PowerShell, `.\windows_helper\start.ps1`. It reads its token automatically on the same PC. Add `-AllowShell` only if you want PowerShell access. **Kill-switch: `Ctrl+Alt+Esc`** (or mouse to the top-left corner).

Try: *"Chrome kholo, meri GitHub repository check karo, failing tests identify karo aur mujhe Hinglish mein batao kya fix karna hai."*

## 2. Hinglish voice
Settings → **Voice**.

| Goal | Speech recognition | Voice output |
|---|---|---|
| Hindi/English/Hinglish, Indian accents | `gnani`, language `en-IN` (mixed speech, Latin output) or `hi-IN` (Devanagari) | `gnani`, model `timbre-v2.5`, voice `Poorvi`, language `hi-en` |
| Alternative | `sarvam` (Saaras, auto-detects language if left blank) | `sarvam` (Bulbul, speaker `shubh`) |
| No keys | `google` | `google` (needs Google Cloud credentials) |

- If the chosen engine fails (bad key, quota, offline) ClarityOS falls back to Google automatically for speech recognition and speech output.
- Mic: **hold Space** (push-to-talk) or click 🎤 (stops after ~1.3 s of silence). Pressing the mic, **Esc**, or Stop interrupts speech. Too-quiet audio is rejected before any paid API call; if recognition fails you get a clear message and can type instead.
- Not in the website: wake-word (Porcupine is only in the old Electron overlay).
- **Bolna** runs *phone-call* agents; it is not an STT/TTS engine. Create an agent in the Bolna dashboard, put its **Agent ID** in Settings → Voice, add the Bolna key, then say *"Rahul ko +91… par call karo"*. Every call needs your approval.

## 3. Safety model
- **Auth:** random tokens (ui / helper / extension) in `%APPDATA%\ClarityOS\tokens.json`. Every API call and WebSocket needs one; `Host` and `Origin` are validated (DNS-rebinding and cross-site WebSocket defence); extension bridge accepts only `chrome-extension://` origins. The old default token `dev-bridge-token` is gone.
- **Permissions** (Settings → Permissions): per capability `allow / ask / deny` — browser, desktop, shell, files_read, files_write, messaging, calls, google, downloads.
- **Approvals:** shell, file writes, messages, phone calls, quitting apps — and clicks whose label/reason looks like *buy, pay, submit, send, delete…* — pause for your OK. The click heuristic is a safety net, not a guarantee. "Auto-approve risky actions" is off by default.
- **Helper:** never touches UAC/secure-desktop prompts, and tells you when an app runs as Administrator instead of bypassing it. Shell group is off unless you pass `-AllowShell`. CAPTCHAs are reported to you, not bypassed.
- **Cancel** (Stop button / Esc / helper kill-switch) stops the agent, speech, pending approvals, helper actions and queued extension actions. **Pause** holds the agent before its next tool call.
- Task logs are redacted (keys, tokens, passwords) before they are stored; export from Settings → History.
- File tools can no longer read `%APPDATA%\ClarityOS`, browser profiles, Windows credential stores or `.ssh`.

## 4. Known limits (read before relying on it)
- `windows_helper\desktop.py` was written without a Windows machine and has **only been compile/logic-tested**. Expect first-run fixes; run it with a harmless app (Notepad) first.
- Chrome/Electron apps expose little UI Automation data until accessibility is on — the agent then falls back to screenshots + the Gemini vision model.
- Google Workspace tools that drove the UI with AppleScript don't work on Windows; `send_imessage` is removed.
- Workflows are saved lists of natural-language steps replayed through the agent (permissions still apply), not recorded click-by-click macros.
- The legacy Electron overlay (`main.js`) and `local_server.py` are unchanged and still macOS-oriented; `local_server.py` still accepts any WebSocket origin — don't run it alongside, use the hub. The Picovoice key hard-coded in `main.js` is public in the repo: rotate it.
- Gnani, Sarvam and Bolna were implemented from their public API docs and tested against mocked responses, not live accounts.

## 5. Tests
`pip install pytest pytest-timeout -r backend\requirements-hub.txt` then `cd tests\hub && pytest` (42 tests: auth/origin, approvals, policy, helper protocol, bridge hardening, cancel, redaction, voice providers, Bolna).
