"""GitHub Actions workflows that put GhostPatch to work on a repository.

`ghostpatch workflow NAME --write` (or one click in the dashboard's Setup view) adds one to
`.github/workflows/`. The same files are in docs/ for people reading the repository; a test
keeps the two identical.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Workflow:
    name: str
    filename: str
    title: str
    description: str
    docs_file: str
    yaml: str


CI_YAML = """\
# Copy this file to .github/workflows/ghostpatch.yml in your own repository.
# When the tests fail on a push or pull request, GhostPatch fixes the code and opens a pull request.
#
# Setup: add a free API key as a repository secret (Settings → Secrets and variables → Actions),
# e.g. GROQ_API_KEY from https://console.groq.com/keys, and allow Actions to create pull requests
# (Settings → Actions → General → Workflow permissions). A second free key, such as
# OPENROUTER_API_KEY from https://openrouter.ai/keys, is used when the first one's daily quota runs out.
name: ghostpatch

on:
  push:
    branches: [main]
  pull_request:

permissions:
  contents: write
  pull-requests: write

jobs:
  autofix:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
      # Install your project's own dependencies here, e.g.:
      # - run: pip install -e ".[dev]"
      - uses: Nitin23123/GhostPatch@main
        with:
          provider: groq
          mode: pr          # or: push, report
          poltergeist: "1"  # rounds of adversarial testing on the fix
        env:
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}  # optional backup
          GH_TOKEN: ${{ github.token }}
"""

ISSUES_YAML = """\
# Copy this file to .github/workflows/ghostpatch-issues.yml in your own repository.
# Add the label `ghostpatch` to an issue, and GhostPatch fixes it and opens a pull request that
# says "Fixes #N", with the red→green proof in its description. Nothing happens for other labels.
#
# Setup: add a free API key as a repository secret, e.g. GROQ_API_KEY from
# https://console.groq.com/keys, and allow Actions to create pull requests
# (Settings → Actions → General → Workflow permissions).
name: ghostpatch issues

on:
  issues:
    types: [labeled]

permissions:
  contents: write
  pull-requests: write
  issues: read

jobs:
  fix:
    if: github.event.label.name == 'ghostpatch'
    runs-on: ubuntu-latest
    timeout-minutes: 45
    steps:
      - uses: actions/checkout@v5
      # Install your project's own dependencies here, e.g.:
      # - run: pip install -e ".[dev]"
      - uses: Nitin23123/GhostPatch@main
        with:
          task: fix-issue
          issue: ${{ github.event.issue.html_url }}
          candidates: "2"
        env:
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}  # optional backup
          GH_TOKEN: ${{ github.token }}
"""

NIGHTSHIFT_YAML = """\
# Copy this file to .github/workflows/ghostpatch-nightshift.yml in your own repository.
# Every night GhostPatch works through the open issues labelled `ghostpatch`, opens a pull request
# for every fix it can prove, haunts a few risky functions for unreported bugs, and leaves a
# morning report on the workflow run's summary page.
#
# Setup: add a free API key as a repository secret (Settings → Secrets and variables → Actions),
# e.g. GROQ_API_KEY from https://console.groq.com/keys (a second key such as OPENROUTER_API_KEY
# is used when the first one's daily quota runs out), and allow Actions to create pull requests
# (Settings → Actions → General → Workflow permissions).
name: ghostpatch night shift

on:
  schedule:
    - cron: "0 2 * * *"  # 02:00 UTC every night
  workflow_dispatch:       # and a "Run workflow" button

permissions:
  contents: write
  pull-requests: write
  issues: write

jobs:
  nightshift:
    runs-on: ubuntu-latest
    timeout-minutes: 120
    steps:
      - uses: actions/checkout@v5
      # Install your project's own dependencies here, e.g.:
      # - run: pip install -e ".[dev]"
      - uses: Nitin23123/GhostPatch@main
        with:
          task: nightshift
          label: ghostpatch
          limit: "5"        # issues per night
          haunt: "3"        # risky functions to haunt afterwards (0 to skip)
          candidates: "2"   # a fix tournament for every issue
        env:
          GROQ_API_KEY: ${{ secrets.GROQ_API_KEY }}
          OPENROUTER_API_KEY: ${{ secrets.OPENROUTER_API_KEY }}  # optional backup
          GH_TOKEN: ${{ github.token }}
"""

WORKFLOWS = {
    "ci": Workflow("ci", "ghostpatch.yml", "Fix failing CI",
                       "When the tests fail on a push or pull request, GhostPatch fixes the code and opens a pull request.",
                       "docs/ci-autofix-example.yml", CI_YAML),
    "issues": Workflow("issues", "ghostpatch-issues.yml", "Label an issue, get a pull request",
                       "Add the `ghostpatch` label to an issue and GhostPatch fixes it and opens a pull request with the proof.",
                       "docs/issue-label-example.yml", ISSUES_YAML),
    "nightshift": Workflow("nightshift", "ghostpatch-nightshift.yml", "Night shift",
                       "Every night: fix every issue labelled `ghostpatch`, haunt for new bugs, and leave a morning report.",
                       "docs/nightshift-example.yml", NIGHTSHIFT_YAML),
}


def workflows_dir(repo: Path) -> Path:
    """Where GitHub looks for workflows: `.github/workflows` at the root of the git repository."""
    from ghostpatch.gitutil import git, is_git_repo

    root = Path(git(repo, "rev-parse", "--show-toplevel", check=False) or repo) if is_git_repo(repo) else repo
    return root / ".github" / "workflows"


def target(repo: Path, name: str) -> Path:
    return workflows_dir(repo) / WORKFLOWS[name].filename


def install(repo: Path, name: str, overwrite: bool = False) -> Path:
    """Write a workflow into the repository. It still has to be committed and pushed to take effect."""
    path = target(repo, name)
    if path.exists() and not overwrite:
        raise FileExistsError(f"{path} already exists.")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(WORKFLOWS[name].yaml, encoding="utf-8", newline="\n")
    return path
