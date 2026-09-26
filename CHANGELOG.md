# Changelog

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
