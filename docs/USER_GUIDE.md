# GhostPatch user guide

This guide takes you from nothing to your first fixed bug, then explains every feature. No
experience with AI tools is needed.

**Contents**

1. [What GhostPatch does](#1-what-ghostpatch-does)
2. [What you need](#2-what-you-need)
3. [Install GhostPatch](#3-install-ghostpatch)
4. [Get a free API key](#4-get-a-free-api-key)
5. [Save your key: `ghostpatch init`](#5-save-your-key-ghostpatch-init)
6. [Check everything works: `ghostpatch doctor`](#6-check-everything-works-ghostpatch-doctor)
7. [Fix your first bug](#7-fix-your-first-bug)
8. [The dashboard, screen by screen](#8-the-dashboard-screen-by-screen)
9. [Every command](#9-every-command)
10. [Command approvals: what the ghost may run](#10-command-approvals-what-the-ghost-may-run)
11. [GitHub: issues, pull requests and automation](#11-github-issues-pull-requests-and-automation)
12. [Free quotas: what happens when they run out](#12-free-quotas-what-happens-when-they-run-out)
13. [All settings](#13-all-settings)
14. [Where GhostPatch keeps things](#14-where-ghostpatch-keeps-things)
15. [Privacy and safety](#15-privacy-and-safety)
16. [Troubleshooting](#16-troubleshooting)

---

## 1. What GhostPatch does

GhostPatch is an AI software engineer that runs on your computer. You describe a bug in plain
English (or paste an error, or a GitHub issue link) and it:

1. maps your code into a graph of functions and who calls whom,
2. finds the cause of the bug,
3. edits the code to fix it,
4. works out what else the change could affect,
5. **proves** the fix: it runs the new tests with the fix taken out (they must fail) and with it
   put back (they must pass).

It can also hunt for bugs nobody has reported (**Haunt**), answer questions about your code
(**Ask**), and work through GitHub issues on its own at night (**Night shift**).

It works with Python, JavaScript and TypeScript projects.

## 2. What you need

| What | Why | Where to get it |
|---|---|---|
| **Python 3.10 or newer** | GhostPatch is written in Python | https://www.python.org/downloads/ (on Windows, tick "Add python.exe to PATH" in the installer) |
| **An API key** for an AI model | The "brain". Free ones are fine | See [section 4](#4-get-a-free-api-key) |
| **git** (recommended) | Undo, pull requests, history | https://git-scm.com/downloads |
| **Node.js** (only for JavaScript/TypeScript projects) | To run your project's tests | https://nodejs.org |
| **GitHub CLI** `gh` (optional) | Fixing GitHub issues and opening pull requests | https://cli.github.com |

Check Python is installed by opening a terminal (on Windows: PowerShell) and typing:

```bash
python --version
```

It should say `Python 3.10` or higher.

## 3. Install GhostPatch

The recommended way uses **pipx**, which installs command-line tools in their own space so they
don't clash with anything else:

```bash
python -m pip install --user pipx
python -m pipx ensurepath
```

**Close the terminal and open a new one**, then:

```bash
pipx install ghostpatch
```

If that says the package can't be found, install it straight from GitHub instead:

```bash
pipx install git+https://github.com/Nitin23123/GhostPatch
```

Other ways that also work: `uv tool install ghostpatch`, or plain `pip install ghostpatch`.

Check it worked:

```bash
ghostpatch --version
```

> **Windows tip:** if `ghostpatch` is "not recognised" or blocked by a security policy, every
> command also works as `python -m ghostpatch …`, for example `python -m ghostpatch --version`.

## 4. Get a free API key

GhostPatch doesn't include an AI model; it uses one from a provider. Several give you a free
key. You only need **one** to start, but a **second one is a good idea**: when one runs out of its
free daily allowance, GhostPatch switches to the next and carries on.

An API key is a long secret password, like `gsk_8fK2…`. Treat it like a password (see
[section 15](#15-privacy-and-safety)).

### Groq (free, recommended to start)

1. Go to **https://console.groq.com/keys**.
2. Sign in (Google, GitHub or email).
3. Click **Create API Key**, give it any name (e.g. "ghostpatch"), and confirm.
4. **Copy the key now.** It starts with `gsk_` and is only shown once. If you lose it, just make a new one.

### OpenRouter (free, a good backup)

1. Go to **https://openrouter.ai/keys** and sign in.
2. Click **Create Key**, name it, and copy it. It starts with `sk-or-`.
3. Nothing else to do: GhostPatch uses OpenRouter's free models (their names end in `:free`),
   which cost nothing.

### Google Gemini (free, another backup)

1. Go to **https://aistudio.google.com/apikey** and sign in with a Google account.
2. Click **Create API key** and copy it. It starts with `AIza`.

Gemini's free allowance for its fast model is small (a few dozen requests a day), so it works
best as a backup.

### Ollama (free, runs entirely on your computer, no key)

Use this when your code must never leave your computer. It needs a reasonably powerful machine.

1. Install Ollama from **https://ollama.com/download**.
2. Download a coding model: `ollama pull qwen2.5-coder:7b`
3. Make sure it's running: `ollama serve` (the desktop app does this for you).

### OpenAI (paid)

Stronger models, billed per use. Create a key at **https://platform.openai.com/api-keys**
(it starts with `sk-`). Your OpenAI account needs credit.

## 5. Save your key: `ghostpatch init`

Run this once. It saves your keys so they work in **every** project:

```bash
ghostpatch init
```

It asks, one at a time:

1. **Which provider?** Type `groq` (or another from the list) and press Enter.
2. **Paste your key.** Paste it and press Enter. It's hidden while you type, so you won't see it; that's normal.
3. **Model.** Just press Enter to use the recommended one.
4. **Command approvals.** Press Enter for `safe` (explained in [section 10](#10-command-approvals-what-the-ghost-may-run)).
5. **Add a free backup provider?** Say yes, pick one (e.g. `openrouter`) and paste its key.
   You can add several. Say no when you're done.

**Where your keys are saved:**

| System | File |
|---|---|
| Windows | `%APPDATA%\ghostpatch\config.env` (e.g. `C:\Users\you\AppData\Roaming\ghostpatch\config.env`) |
| macOS / Linux | `~/.config/ghostpatch/config.env` (only your user can read it) |

It's a plain text file with lines like `GROQ_API_KEY=gsk_…`. You can open it in any text editor.

**To change or add a key later:** run `ghostpatch init` again, or edit that file.
**To remove a key:** delete its line from the file.

### Other ways to provide keys

You don't have to use `ghostpatch init`. GhostPatch looks for settings in this order and uses the
first value it finds:

1. **Environment variables** already set in your terminal:
   - macOS / Linux: `export GROQ_API_KEY=gsk_…`
   - Windows PowerShell: `$env:GROQ_API_KEY="gsk_…"`
2. **A `.env` file in the project** you're working on (`ghostpatch init --local` writes one).
   **Add `.env` to your `.gitignore`** so the key is never committed.
3. **A `.env` file in the current folder** or one of its parent folders.
4. **Your user settings file** from `ghostpatch init` (the table above).

## 6. Check everything works: `ghostpatch doctor`

Go to your project's folder and run:

```bash
cd path/to/your-project
ghostpatch doctor
```

It checks Python, your settings, the key (by contacting the provider), the model, the backup
chain, the code graph, git, Node.js and the approval mode, and tells you exactly what to fix.
Everything green means you're ready. (`ghostpatch doctor --offline` skips contacting the provider.)

## 7. Fix your first bug

### With the dashboard (easiest)

```bash
cd path/to/your-project
ghostpatch serve
```

Your browser opens **http://localhost:8765**. (If it doesn't, open that address yourself.)

1. Make sure **Fix** is selected at the top left.
2. Describe the bug in the box, for example:
   *"When a customer uses the SAVE10 coupon the total becomes $0.00 instead of 10% off."*
   You can also paste an error message with its stack trace, or a GitHub issue link.
3. Click **Fix it** (or press Ctrl + Enter).
4. Watch: every step appears in the timeline on the left, and the code graph on the right lights
   up as the ghost reads and edits code.
5. If the ghost wants to run a command that isn't pre-approved, an **Approval required** card
   appears. Click **Allow** or **Deny**.
6. When it's done you get:
   - the **result** and a summary of what was wrong,
   - the **red-green proof** (tests failed without the fix, passed with it),
   - a **verification score** out of 100,
   - the **changes** at the bottom, as a diff.
7. Don't like it? Click **Undo changes**: every file goes back exactly as it was.
   Happy? Commit it yourself, or click **Open pull request**.

Stop the dashboard with **Ctrl + C** in the terminal.

### From the terminal

```bash
ghostpatch fix "The SAVE10 coupon charges $0.00 instead of 10% off" --approve safe
```

You see each step live, then the result, the proof and the score. `ghostpatch undo` puts
everything back.

## 8. The dashboard, screen by screen

The top bar has four tabs: **Dashboard**, **Runs**, **Insights** and **Setup**. On the right it
shows the status, the model in use, and the approval mode.

### Dashboard

The left side is where you tell the ghost what to do. There are three modes:

- **Fix:** describe a bug (or paste a stack trace or GitHub issue link) and click **Fix it**. Options:
  - **Proof** (on by default): after the fix, GhostPatch runs the new tests without the fix and
    with it, to prove the fix is what makes them pass. If the ghost wrote no test, it writes one first.
  - **Guard** (on by default): GhostPatch runs your whole test suite before the ghost starts and
    again after the fix. If the fix breaks a test that passed before, the ghost is told and fixes
    that too. Turn it off for very slow test suites.
  - **Tournament:** 2 or 3 independent fixes compete, each using a different approach. Every fix
    must also pass the other fixes' tests, and the best-proven one wins. Uses more of your quota.
  - **Poltergeist:** after the fix, a second AI that may only write tests tries to break it.
    If it succeeds, the ghost fixes the code again.
- **Haunt:** shows your riskiest functions (called from many places, not tested, changed often).
  Tick the ones to check and click **Haunt**. For each, the ghost writes tests of what the code
  is meant to do and runs them. A separate "skeptic" checks every failure before it counts as a bug.
  Real bugs come with a failing test as proof and a **Fix this bug** button.
- **Ask:** ask a question about your code, like *"How is the order total calculated?"* The answer
  names files and line numbers, and the **call flow** (which function calls which) lights up on
  the graph. Ask mode never changes files.

When nothing is running, the timeline shows **What the ghost can do**: click any card to jump
to that feature.

The right side is the **code graph**: each box is a file, each row a function. Colours: mint =
edited, red = could be affected by the change, amber = crash path or asked-about flow, a red
outline = no test reaches it. Use the zoom buttons, drag to move, and type in **Filter symbols**
to find something. Click a function for details.

### Runs

Every run is recorded. Pick one to **replay** it step by step (play, pause, speed up), see its
score and proof, **Share** it as a single web page anyone can open, or **Undo** it.

### Insights

- **Test gaps:** functions no test reaches. Tick some and click **Write tests for selected**.
- **Time-lapse:** your code graph at each of the last 20 commits. Drag the slider or press play.
- **Review:** the blast radius of your uncommitted changes or of a pull request (type its number).
  **Post as PR comment** posts the review on GitHub.
- **Night shift:** morning reports from night shifts, and a button to add the nightly GitHub workflow.

### Setup

- **Model & fallback:** the model in use, the backup chain, and which providers have a key (with
  links to get the missing ones).
- **Health check:** the same checks as `ghostpatch doctor`.
- **GitHub automation:** add ready-made GitHub workflows with one click (see [section 11](#11-github-issues-pull-requests-and-automation)).
- **Repo memory:** notes the ghost reads before every run. Add your own (e.g. *"Run the tests
  with `pytest -q tests/`"*), see what it learned by itself, or forget everything.

## 9. Every command

Run these inside your project's folder (or add `--repo path/to/project`).

| Command | What it does |
|---|---|
| `ghostpatch init` | Save your provider, API keys and approval mode (once per computer) |
| `ghostpatch doctor` | Check your setup and say what to fix |
| `ghostpatch serve` | Open the dashboard (`--port 9000` for another port, `--no-browser` to not open a browser) |
| `ghostpatch fix "description"` | Fix a bug described in words |
| `ghostpatch fix @ISSUE.md` | Fix a bug described in a file |
| `ghostpatch fix https://github.com/owner/repo/issues/42 --pr` | Fix a GitHub issue and open a pull request |
| `ghostpatch fix "…" --candidates 3` | Fix tournament: 3 fixes compete |
| `ghostpatch fix "…" --poltergeist` | Have a second AI attack the fix |
| `ghostpatch fix "…" --no-proof` | Skip the red-green proof |
| `ghostpatch fix "…" --no-regression` | Skip running the whole test suite before and after (for very slow suites) |
| `ghostpatch trace crash.txt --fix` | Map a stack trace onto the code, then fix the crash |
| `ghostpatch haunt` | Hunt for unreported bugs in the 3 riskiest functions |
| `ghostpatch haunt --list` | Just show the risk ranking (uses no AI, costs nothing) |
| `ghostpatch haunt --only apply_discount --fix` | Haunt one function and fix what it finds |
| `ghostpatch ask "question"` | Ask about the code; get an answer and the call flow |
| `ghostpatch review` | Blast-radius review of your uncommitted changes |
| `ghostpatch review 42 --post` | Review pull request #42 and post the review on GitHub |
| `ghostpatch gaps` | List functions no test reaches (`--write-tests 5` writes tests for 5) |
| `ghostpatch timelapse` | The code graph over the last 20 commits |
| `ghostpatch graph impact NAME` | What would a change to NAME affect? (also `map`, `callers`, `callees`, `tests`, `symbol`, `stats`) |
| `ghostpatch history` | List past runs |
| `ghostpatch undo` | Undo the latest run (`ghostpatch undo RUN_ID` for another) |
| `ghostpatch pr` | Open a pull request for the latest run |
| `ghostpatch share` | Save a run as a web page anyone can open |
| `ghostpatch memory` | Show what the ghost remembers (`--add "note"`, `--clear`) |
| `ghostpatch nightshift --approve safe` | Fix every open issue labelled `ghostpatch`, then write a morning report |
| `ghostpatch workflow` | List the GitHub workflows; `ghostpatch workflow nightshift --write` adds one |
| `ghostpatch ci-fix` | For CI: run the tests and, if they fail, fix them |

Options that work with fix, trace, haunt, gaps, serve, nightshift and ci-fix:

| Option | Meaning |
|---|---|
| `--approve ask\|safe\|all` | Which commands may run without asking ([section 10](#10-command-approvals-what-the-ghost-may-run)) |
| `--provider groq` / `--model NAME` | Use a different provider or model for this run |
| `--max-steps 40` | Allow more (or fewer) steps. Default 30 |
| `--no-fallback` | Don't switch provider when a daily quota runs out |
| `--no-graph` | Don't build or use the code graph |

`ghostpatch COMMAND --help` shows everything a command accepts.

## 10. Command approvals: what the ghost may run

To test a fix, the ghost runs commands on your computer, like `pytest`. You decide how much it
may do without asking:

| Mode | What happens | Use it when |
|---|---|---|
| `ask` (default) | Every command waits for you: click Allow or Deny in the dashboard; in the terminal, press Enter (or `y`) to allow, `n` to deny | You want to see everything |
| `safe` (recommended) | Known test commands (`pytest`, `npm test`, `node --test`, `go test`, `cargo test`, …) and read-only git commands (`git status`, `git log`, `git diff`, `git show`) run automatically. Anything else still asks. Commands that chain or redirect (`&&`, `;`, `>`, …) always ask | Everyday use |
| `all` | Everything runs without asking | Only in a throwaway environment, like CI |

Set it per run with `--approve safe`, or for good in `ghostpatch init`.

Whatever the mode: GhostPatch only edits files inside your project, never commits or pushes
unless you ask (`--pr`, **Open pull request**), and every run can be undone.

## 11. GitHub: issues, pull requests and automation

### Connect GitHub (once)

1. Install the GitHub CLI: https://cli.github.com
2. Log in: `gh auth login`, then follow the prompts (choose GitHub.com, HTTPS, log in with a browser).

GhostPatch uses `gh` for everything on GitHub, so it never sees or stores your GitHub password or token.

### Fix an issue and open a pull request

Your project must be a git repository whose `origin` is on GitHub:

```bash
ghostpatch fix https://github.com/you/your-repo/issues/42 --pr
```

It reads the issue, fixes it and, only if the fix is verified, pushes a new branch and opens a
pull request that says "Fixes #42". The description includes the red-green proof and the score.

### Let it work on GitHub by itself (free, with GitHub Actions)

There are three ready-made workflows:

| Name | What it does |
|---|---|
| `ci` | When your tests fail on a push or pull request, fix the code and open a pull request |
| `issues` | Add the label `ghostpatch` to an issue, and it fixes the issue and opens a pull request |
| `nightshift` | Every night, fix all issues labelled `ghostpatch`, hunt for new bugs, and write a morning report |

**Step by step:**

1. Add a workflow, either with the dashboard (**Setup → GitHub automation → Add to repository**)
   or with the terminal:
   ```bash
   ghostpatch workflow nightshift --write
   ```
   This creates a file in `.github/workflows/`.
2. Commit and push that file:
   ```bash
   git add .github/workflows
   git commit -m "Add GhostPatch night shift"
   git push
   ```
3. Give GitHub your API key, as a secret. On your repository's GitHub page:
   **Settings → Secrets and variables → Actions → New repository secret**.
   Name: `GROQ_API_KEY`. Value: your key. Click **Add secret**.
   (Optional backup: also add `OPENROUTER_API_KEY`.)
4. Allow GitHub Actions to open pull requests:
   **Settings → Actions → General → Workflow permissions** → select **Read and write permissions**
   and tick **Allow GitHub Actions to create and approve pull requests** → **Save**.
5. For `issues` and `nightshift`: create a label named `ghostpatch`
   (**Issues → Labels → New label**) and add it to the issues you want fixed.
6. If your project needs its own packages to run its tests, open the workflow file and fill in the
   line that says "Install your project's own dependencies here", e.g.
   `- run: pip install -e ".[dev]"`.

Secrets are encrypted by GitHub and never shown in logs. The night shift can also be started by
hand: **Actions → ghostpatch night shift → Run workflow**.

### Run the night shift on your own computer

```bash
ghostpatch nightshift --approve safe --haunt 3
```

It needs `gh` logged in and no uncommitted changes. It won't run with `--approve ask`, because
nobody would be there to click Allow. In `safe` mode anything that would need approval is
declined. The morning report appears in the terminal and in the dashboard's
**Insights → Night shift** tab.

## 12. Free quotas: what happens when they run out

Free keys have limits: a few requests per minute and an amount per day.

- **Per-minute limits:** GhostPatch waits a few seconds and tries again, automatically.
- **Daily limits:** if you saved a backup key, GhostPatch switches to the next provider and
  carries on with the same conversation. The switch is shown in the timeline, e.g.
  *"groq: daily token limit of 200,000 reached; try again in 4m35s. Switching to gemini…"*
- **Everything used up:** the run stops and says when each provider will be available again.
  Try later, or add another free key with `ghostpatch init`.

Every run shows how many tokens it used. Tips for making the free allowance go further:
start with a plain fix (no tournament or poltergeist), keep `--max-steps` modest, and use
`ghostpatch haunt --list`, which is free.

## 13. All settings

These can go in your user settings file, a project `.env`, or environment variables
([section 5](#5-save-your-key-ghostpatch-init)).

| Setting | What it does | Example |
|---|---|---|
| `GHOSTPATCH_PROVIDER` | Main provider: `groq`, `openrouter`, `gemini`, `ollama` or `openai` | `groq` |
| `GROQ_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `OPENAI_API_KEY` | The keys | `gsk_…` |
| `GHOSTPATCH_MODEL` | A different model for the main provider | `qwen/qwen3.8-27b` |
| `GHOSTPATCH_APPROVE` | Default approval mode | `safe` |
| `GHOSTPATCH_FALLBACK` | Backup order, or `none` to turn switching off | `openrouter,gemini` |
| `GHOSTPATCH_BASE_URL` | Send the main provider's requests to another OpenAI-compatible server (Ollama on another machine, LM Studio, vLLM) | `http://192.168.1.20:11434/v1` |

## 14. Where GhostPatch keeps things

| What | Where |
|---|---|
| Your keys and settings | The user settings file ([section 5](#5-save-your-key-ghostpatch-init)) |
| The code graph, run history, learned notes, night shift reports | A hidden `.ghostpatch/` folder inside each project. It contains its own `.gitignore`, so it is never committed |
| Team notes the ghost reads before every run | `GHOSTPATCH.md` in the project's root. Write it yourself and commit it |
| GitHub workflows | `.github/workflows/` in your repository, when you add them |

Deleting a project's `.ghostpatch/` folder is safe: GhostPatch rebuilds the graph next time, but
the run history (and with it, undo for old runs) is lost.

## 15. Privacy and safety

- **Your code goes to the AI provider you choose.** To find and fix bugs, GhostPatch sends the
  relevant parts of your code, test output and your bug description to the model. Read your
  provider's terms. For code that must never leave your computer, use **Ollama** (local).
- **Keep keys secret.** Never commit them, paste them in issues, chats or screenshots, or share
  your settings file. If a key leaks, delete it on the provider's website and create a new one.
- **The dashboard is private to your computer.** It only listens on your own machine
  (127.0.0.1) and rejects requests from other websites.
- **You stay in control.** Approvals decide what runs; GhostPatch only touches files inside your
  project; nothing is committed or pushed unless you ask; every run can be undone.
- **Running tests runs code.** Tests execute your project's code, including tests the ghost
  wrote. That's why `all` mode is only for throwaway environments.

## 16. Troubleshooting

**"ghostpatch is not recognised" / "command not found"**
Run `python -m pipx ensurepath` and open a new terminal. Or use `python -m ghostpatch …` instead.

**"This program is blocked by group policy" or "Application Control policy" (Windows)**
Your computer blocks unknown programs. Use `python -m ghostpatch …`, which runs through Python.

**"GROQ_API_KEY is not set"**
Run `ghostpatch init` and paste your key, or check the settings file from section 5.

**"groq rejected the API key"**
The key was mistyped, deleted or expired. Create a new one and run `ghostpatch init` again.

**"daily token limit reached" / "Every configured provider is unavailable right now"**
The free allowance is used up. Wait (the message says for how long), or add a backup key with
`ghostpatch init`. See section 12.

**"Could not connect to …"**
Check your internet connection. For Ollama, make sure it's running (`ollama serve`).

**"The tests couldn't run: the test runner isn't installed"**, or the proof says it couldn't run the tests
Install your project's test tools where your project runs, e.g. `pip install pytest` (inside the
project's virtual environment if it has one). GhostPatch uses the project's `.venv` or `venv`
automatically when there is one.

**"No Python, JavaScript or TypeScript files found"**
Run GhostPatch inside your project's folder, or pass `--repo path/to/project`.

**"Could not start the dashboard on port 8765"**
Something else uses that port. Try `ghostpatch serve --port 8766`.

**The fix isn't what I wanted**
Click **Undo changes** (or run `ghostpatch undo`), describe the bug in more detail (the exact
input, what you expected, what happened), and try again. A tournament (`--candidates 3`) often
finds a better fix.

**Anything else**
Run `ghostpatch doctor`: it usually says exactly what's wrong. You can also open an issue at
https://github.com/Nitin23123/GhostPatch/issues (never include your API key).
