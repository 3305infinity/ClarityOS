# Moonwalk Agentic IDE

Browser-based IDE for the current workspace with:

- file explorer, search, and text editor
- terminal sessions with live streaming output
- autonomous agent runs with workspace tools
- local deterministic planner fallback when no model provider is configured

## Run

```bash
npm start
```

Then open `http://localhost:4173`.

## Agent Modes

- Local planner: works with slash commands such as `/analyze`, `/search foo`, `/read path`, `/run command`, `/write path`
- OpenAI-compatible provider: fill in `Base URL`, `Model`, and `API Key` in the UI, or set `OPENAI_BASE_URL`, `OPENAI_MODEL`, and `OPENAI_API_KEY` before starting the server

## Notes

- All file operations are constrained to the current workspace root.
- The terminal runs in a persistent shell session and streams output over Server-Sent Events.
- Agent writes trigger workspace refreshes in the editor and explorer.
