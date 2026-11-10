# ClarityOS — one-page solution pitch

**Great Indian AI Internship Challenge 2026 · Gnani AI**
**Solo submission — Nishtha Pandey**

> A desktop agent that operates the screen you are already looking at — by voice, in English or Hindi.

## The problem

Every small computer task costs the same tax: switch apps, hunt a menu, copy, paste, repeat. Assistants answer
questions about that work; they do not do it. The gap is not language understanding — it is that nothing is
actually holding the mouse.

## What we built

ClarityOS is a Windows desktop agent (Electron + Python). You speak; it reads the live screen or browser tab,
shows you a plan, drives the real applications, and then checks that what it claimed happened actually did.
Both directions of speech run on Gnani: **Prisma v2.5** for speech-in over a realtime WebSocket, **Timbre v2.5**
for speech-out over server-sent events, so it starts replying before the sentence is finished.

## How it works — one loop, every request

| Stage | What happens | Where |
|---|---|---|
| **Sense** | Reads the live DOM via our Chrome extension, or the foreground window — never a guess about what is open | `backend/agent/perception.py`, `backend/agent/current_context.py` |
| **Plan** | Breaks the request into milestones and shows them before acting | `backend/agent/task_planner.py` |
| **Act** | Drives the real app — browser, Calendar, Gmail, Spotify, the Windows shell | `backend/agent/milestone_executor.py`, `backend/tools/` |
| **Verify** | Confirms the result against retrieved evidence, or says plainly that it could not | `backend/agent/retrieval_guard.py`, `backend/agent/verifier.py` |

The fourth stage is the one that matters. A screenshot is a paraphrase of pixels; the extension returns the page
itself, so a summary cites real text and real links. If the evidence is not there, ClarityOS reports failure
rather than inventing a plausible answer.

## Built to ask first

Every consequential action — shell commands, file writes, purchases, form submits — is classified and held for
approval. The plan appears; nothing runs until you press Proceed. The hub binds to `127.0.0.1` only, the Chrome
bridge accepts a paired token from our extension's origin and nothing else, and secrets are redacted before
anything is logged or sent to a model.

`backend/hub/policy.py` · `backend/hub/approval.py` · `backend/hub/redact.py` · `backend/hub/security.py`

## What the demo shows

Four unscripted tasks, end to end, with no clicks:

1. **"Open Spotify"** — the window is brought forward if it is already running, rather than launching a second copy.
2. **"Kal subah saat baje ka Calendar event bana do — doodhwale ko ₹250 dene hain"** — Hindi in, a real Google
   Calendar event out, after an explicit Proceed.
3. **"Summarise this page and suggest 3 similar films"** — the page is read, not screenshotted; the summary and
   all three recommendations are grounded in what was on it.
4. **"Read the current email in Chrome and draft a reply"** — the open thread is read first, then a Gmail compose
   window opens filled in. It drafts; it never sends.

## Why Gnani fits

Indian users do not speak one language per sentence. Prisma handles code-mixed Hindi-English in the same
utterance, and Timbre answers in a voice that belongs in that conversation. Both are streaming, which is what
makes an agent feel like it is listening rather than transcribing.

## Links

| | |
|---|---|
| Repository | https://github.com/3305infinity/ClarityOS |
| Demo video — 1 minute | `videos/ClarityOS_Demo_Gnani_60s.mp4` |
| Demo video — full walkthrough | `videos/ClarityOS_Demo_Gnani.mp4` |
| Gnani integration detail | [`docs/GNANI_SUBMISSION.md`](GNANI_SUBMISSION.md) |
| Architecture | [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) |

*No benchmark figures are quoted here. Everything above is a claim about behaviour you can reproduce from the
repository.*
