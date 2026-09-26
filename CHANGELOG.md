# Changelog

## 0.7.0: proof, not promises, and a ghost that works nights

- **🔴→🟢 Red-green proof.** After every fix, GhostPatch runs the new tests itself: with the fix
  taken out (they must fail), then with it in place (they must pass). The result is shown in the
  terminal, the dashboard and the pull request, and moves the confidence score. Tests that pass
  either way are reported as proving nothing. `--no-proof` skips it.
- **🏆 Fix tournament** (`--candidates N`). Independent fixes compete, each with its own strategy
  (direct, test first, graph first), one after another from the same starting point. They are
  judged on their proofs, confidence scores and cross-examination (every fix must pass its rivals'
  proven tests), with a small penalty for big diffs. The winner is applied; the rest are discarded.
- **👻 Haunt mode** (`ghostpatch haunt`). Ranks functions by risk from the code graph and git history,
  sends a test-only haunter at the riskiest, runs its tests, and asks a skeptic to confirm every
  failure. Confirmed bugs keep their failing test as proof; `--fix` (or one click in the dashboard)
  fixes them, proven by that same test. `--list` shows the ranking without using the model.
- **🌙 Night shift** (`ghostpatch nightshift`). Fixes every open issue with a label, opens a pull
  request for each verified fix (a draft if it isn't proven), rolls back failed attempts, optionally
  haunts and fixes new bugs, and writes a morning report (terminal, `.ghostpatch/reports/`, the
  Actions summary and the dashboard's new Night shift tab). It refuses to run in `ask` mode, and in
  `safe` mode it declines any command that would need approval.
- **💬 Ask the graph** (`ghostpatch ask`). A read-only agent answers questions about the code, citing
  `path:line`, and GhostPatch draws the call flow between the functions it mentions from the code graph.
- **The GitHub Action gains tasks**: `ci-fix`, `fix-issue` (label an issue, get a pull request),
  `nightshift` and `haunt`, with example workflows in `docs/`. Inputs reach the script as environment
  variables, so text from an issue can't inject shell commands.
- **Dashboard**: a Fix / Haunt / Ask switch with a line explaining each mode, Proof and tournament
  controls, proof and tournament cards, haunt findings with "Fix this bug", answers with their call
  flow on the graph, and night shift reports. An empty timeline shows everything the ghost can do,
  each one a click away.
- **Setup view**: the model and fallback chain, which providers have keys, the `doctor` health checks,
  repo memory (read, add and forget notes), and the three GitHub workflows, added in one click.
- **`ghostpatch workflow`** lists, prints or writes (`--write`) the GitHub Actions workflows.
- Test runs GhostPatch starts itself use the project's own Python (its virtualenv, an activated one,
  or `python` on PATH, whichever has pytest), so they work when GhostPatch is installed with pipx.
  A missing test runner is reported as such, never as a failing fix.

## 0.6.0: ready for everyone, and built to last on free quotas

**Install from PyPI.** `pipx install ghostpatch` (or `uv tool install ghostpatch`). Releases are
published by GitHub Actions with PyPI trusted publishing. The source package no longer includes
local run data, and the README renders on PyPI. The GitHub Action installs from its own checkout,
so `uses: Nitin23123/GhostPatch@v0.6.0` runs exactly that version.

**Built for free-tier quotas.**
- **OpenRouter** is a new free provider (`OPENROUTER_API_KEY`, default `qwen/qwen3.8-27b:free`).
- **Groq is now the default provider.** Gemini's free tier is down to 20 requests a day for its
  flash model, so it moves to the back of the fallback chain.
- **`ghostpatch init` offers backup providers**, so a used-up daily quota switches provider
  instead of ending the run. `ghostpatch doctor` shows the fallback chain and warns when there is none.
- **Shorter requests.** Every step re-sends the whole conversation, so old tool output was paid for
  again and again. Once a conversation passes about 6k tokens, old long results are cut down to
  their first and last lines (the newest three stay whole). In a test with eight reads of a large
  file, the last request shrank from 98k to 41k characters and the whole run sent 38% less.

**Fixes from a live run against the free models:**
- Edits keep their indentation. Models often send replacement code flush-left; `edit_file` and
  `replace_lines` now indent it to match the code it replaces, and tell the model they did.
- The ghost hears about syntax errors right away: if an edit leaves a Python, JavaScript or
  TypeScript file unparseable, the tool result says where.
- Quota errors are readable: "groq: daily token limit of 500,000 reached; try again in 2m9s"
  instead of the provider's raw JSON. When every provider is used up, the error names them all.
- Switching providers mid-run is now saved with the run, so replays show it.

## 0.5.0: a new dashboard

- A completely rebuilt dashboard, based on a professional design made in Google Stitch: graphite
  surfaces, one mint accent, Geist and JetBrains Mono, and no build step or framework.
- The code graph now shows **files as cards and functions as rows**, laid out by dependency (called
  code on the left, its callers to the right, tests last). Edits, the blast radius, crash paths, new
  code and untested functions each have their own marking. Click any function for details.
- **Dashboard**: session composer with poltergeist rounds, a timeline with per-step timings, inline
  diffs and approval cards, a result card, the verification-quality score, poltergeist rounds and a
  diff viewer with line numbers, plus Share, Undo and Open pull request.
- **Runs**: run history with filters and confidence scores, and a replay scrubber with a live
  blast-radius graph for every step.
- **Insights**: test gaps (select functions and have the ghost write tests), the architecture
  time-lapse with a commit slider, and blast-radius review of local changes or a pull request.
- Works on phones: the layout stacks into cards.
- `safe` approval mode now also recognises test runs through a full path to Python.
- `scripts/demo_server.py` serves the dashboard with a scripted offline model, for UI work and demos.

## 0.4.0: ten new ways to trust (and use) the ghost

- **Poltergeist mode** (`--poltergeist`): after a fix, an adversarial agent that may only write tests
  tries to break it, armed with the diff and the fix's blast radius. If it succeeds, the ghost gets its
  failing tests and fixes the code again.
- **Crash-to-graph tracing**: Python and Node/TypeScript stack traces are mapped onto the code graph.
  Paste one into `fix` or the dashboard and the ghost starts from the crash path; `ghostpatch trace`
  shows it on its own. The dashboard receives a `trace` event to animate.
- **Blast-radius PR review** (`ghostpatch review [PR] [--post]`): maps a change to functions, walks up
  the call graph and flags affected code that no test reaches. Works on uncommitted changes and on any
  GitHub pull request, analysed at the pull request's own version of the code.
- **CI auto-fixer** (`ghostpatch ci-fix`, plus a GitHub Action in `action.yml`): when the tests fail,
  fix the code, re-run the tests independently, then report, push or open a pull request.
- **Run replay and share**: every run is recorded step by step. `ghostpatch share` exports a run as one
  self-contained HTML page with a replay scrubber; the dashboard API serves runs and share pages.
- **Test-gap map** (`ghostpatch gaps [--write-tests N]`): functions no test reaches, and a way to have
  the ghost write tests for them. Graph nodes now carry a `tested` flag.
- **Architecture time-lapse** (`ghostpatch timelapse`): the code graph at each of the last N commits,
  read straight from git, with what was added and removed at each step.
- **Free-model auto-fallback**: when a provider's daily quota runs out mid-fix, GhostPatch switches to
  the next configured free provider and carries on with the same conversation (`GHOSTPATCH_FALLBACK`).
- **Repo memory**: `GHOSTPATCH.md` for team conventions plus facts the ghost learns with its new
  `remember` tool, fed into every run (`ghostpatch memory`).
- **Confidence score**: every fix gets 0-100 from "did the tests pass after the last edit?" and "how much
  of the blast radius do tests actually reach?".
- One shared session runner now powers the CLI, the dashboard, CI and the benchmark.

## 0.3.0: GitHub-native

- `ghostpatch fix https://github.com/owner/repo/issues/42` reads the issue's title, description
  and comments and fixes it. The dashboard accepts issue links too.
- `--pr` opens a pull request when the fix is verified; `ghostpatch pr [RUN]` or the dashboard's
  **Open pull request** button does it for any earlier run. The pull request says `Fixes #42`,
  lists the changed files and explains how the fix was made.
- Only the run's own files are committed, on a new `ghostpatch/issue-42-…` branch. GhostPatch
  refuses if those files changed after the run, or if other changes are already staged, so
  nothing unrelated ever ends up in a pull request.
- Everything goes through the GitHub CLI (`gh`): GhostPatch never handles a GitHub token.
  Public issues can be read without it.

## 0.2.0: a product you can trust with your code

**Safety and control**
- `ghostpatch undo` rolls back every file a run changed, including files it created. It refuses
  to overwrite edits you made after the run unless you pass `--force`.
- Run history: every run is saved under `.ghostpatch/runs/`. See it with `ghostpatch history` or
  in the dashboard's new **Past runs** panel, where any run can be undone with one click.
- Approval modes (`--approve ask|safe|all`, or `GHOSTPATCH_APPROVE`): `safe` runs recognised test
  commands and read-only git commands automatically and still asks for anything else, including
  anything that chains, redirects or substitutes commands.

**Setup**
- `ghostpatch init`: a setup wizard that saves your provider, API key and approval mode to your
  user settings, so one setup works in every repository. Keys are typed into a hidden prompt.
- `ghostpatch doctor`: checks Python, settings, the API key (with a live call), the model, the
  code graph, git and Node.js, and says exactly what to fix.
- Settings are read from the repository's `.env`, the current folder's `.env`, then your user
  settings.

**Quality**
- Continuous integration on Windows, macOS and Linux, Python 3.10 to 3.13, plus a job that builds
  the package and smoke-tests it in a clean environment.
- Git repositories are detected correctly from any subfolder.
- `scripts/screenshot.mjs` captures the dashboard for documentation.
- 89 automated tests.

## 0.1.0: first release

- Autonomous bug-fixing agent with sandboxed file tools and approval-gated commands.
- Living code graph for Python, JavaScript and TypeScript with automatic impact reports.
- Live web dashboard (`ghostpatch serve`).
- Free model providers: Groq, Gemini, Ollama, plus OpenAI.
