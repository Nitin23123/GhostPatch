# Changelog

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
