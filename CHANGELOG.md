# Changelog

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
