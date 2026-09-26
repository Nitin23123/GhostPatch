# Contributing to GhostPatch 👻

Thanks for helping! Here's how to get started.

## Setup

```bash
git clone https://github.com/Nitin23123/GhostPatch
cd GhostPatch
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -e ".[dev]"
pytest
```

## Project layout

| File | What it does |
|---|---|
| `src/ghostpatch/cli.py` | Command-line entry point (`ghostpatch fix ...`) |
| `src/ghostpatch/agent.py` | The agent loop: ask the model, run tools, repeat |
| `src/ghostpatch/tools.py` | Tools the agent can call, and their safety checks |
| `src/ghostpatch/graph.py` | The code graph: incremental indexing in SQLite, callers/impact queries |
| `src/ghostpatch/parsers.py` | Turns Python (ast) and JS/TS (tree-sitter) files into symbols and calls |
| `src/ghostpatch/providers.py` | Model providers (Gemini, Groq, Ollama, OpenAI) |
| `src/ghostpatch/server.py` | The `ghostpatch serve` web server: runs the agent, streams events |
| `src/ghostpatch/web/index.html` | The dashboard page (plain HTML/CSS/JS, no build step) |
| `src/ghostpatch/ui.py` | Terminal output |
| `src/ghostpatch/history.py` | Run history and undo (`.ghostpatch/runs/`) |
| `src/ghostpatch/policy.py` | Approval modes: which commands may run without asking |
| `src/ghostpatch/config.py` | Where settings come from; `ghostpatch init` writes them |
| `src/ghostpatch/doctor.py` | `ghostpatch doctor` setup checks |
| `src/ghostpatch/gitutil.py` | Small helpers around the git command line |
| `src/ghostpatch/github.py` | GitHub issues in, pull requests out (through the `gh` CLI) |
| `scripts/screenshot.mjs` | Captures the dashboard for the README (`node scripts/screenshot.mjs`) |
| `tests/` | Tests; the agent tests use a fake model, so no API key is needed |
| `examples/` | Small buggy projects for demos |

## Guidelines

- Keep pull requests small and focused on one thing.
- Add or update tests for any behaviour you change.
- Never commit API keys. `.env` is gitignored for a reason.
- Adding a new tool? Add its handler and schema in `tools.py`, plus a test.

## Good first contributions

- New example repos with realistic bugs in `examples/`
- Better error messages
- Support for another model provider
