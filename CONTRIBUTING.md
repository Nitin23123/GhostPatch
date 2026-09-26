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
| `src/ghostpatch/bench.py` | The benchmark runner (`ghostpatch bench`) |
| `src/ghostpatch/session.py` | One fixing session end to end; shared by the CLI, dashboard, CI and benchmark |
| `src/ghostpatch/poltergeist.py` | Poltergeist mode: the adversarial tester and re-fix rounds |
| `src/ghostpatch/trace.py` | Stack-trace parsing and crash-to-graph mapping |
| `src/ghostpatch/review.py` | Blast-radius review of local changes and pull requests |
| `src/ghostpatch/cifix.py` | `ghostpatch ci-fix`; `action.yml` wraps it as a GitHub Action |
| `src/ghostpatch/replay.py` | Step recording and the shareable HTML replay page |
| `src/ghostpatch/timelapse.py` | The code graph across git history |
| `src/ghostpatch/fallback.py` | Switching providers when a quota runs out |
| `src/ghostpatch/memory.py` | `GHOSTPATCH.md` and learned notes |
| `src/ghostpatch/confidence.py` | The 0-100 confidence score for a fix |
| `bench/cases/` | Benchmark cases: buggy repo, hidden tests, reference fix |
| `scripts/screenshot.mjs` | Captures the dashboard for the README (`node scripts/screenshot.mjs`) |
| `tests/` | Tests; the agent tests use a fake model, so no API key is needed |
| `examples/` | Small buggy projects for demos |

## The benchmark

`bench/cases/` holds realistic bug cases. Each has the buggy project (`repo/`), hidden tests the
agent never sees (`hidden/`) and a reference fix (`solution/`).

```bash
python -m ghostpatch bench --validate          # check every case is valid (no model needed)
python -m ghostpatch bench --compare           # run all cases with and without the code graph
python -m ghostpatch bench --report            # print the results table
```

To add a case, copy an existing one. The visible tests must pass on the buggy code, the hidden
tests must fail on it, and the reference fix must pass everything. `tests/test_bench.py` checks
this for every case in CI.

## Guidelines

- Keep pull requests small and focused on one thing.
- Add or update tests for any behaviour you change.
- Never commit API keys. `.env` is gitignored for a reason.
- Adding a new tool? Add its handler and schema in `tools.py`, plus a test.

## Good first contributions

- New example repos with realistic bugs in `examples/`
- Better error messages
- Support for another model provider
