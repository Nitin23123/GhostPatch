"""Small helpers around the `git` command line."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path


def git(repo: Path, *args: str, check: bool = True, timeout: float | None = None) -> str:
    """Run a git command in `repo` and return its output. Raises RuntimeError if it fails."""
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout)
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def push(repo: Path, *args: str) -> str:
    """`git push`. In GitHub Actions, credentials come from the GitHub CLI and GhostPatch's own token,
    not from a token stored in .git/config, which the commands the ghost runs could read."""
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if os.environ.get("GITHUB_ACTIONS") and token and shutil.which("gh"):
        return git(repo, "-c", "credential.helper=", "-c", "credential.helper=!gh auth git-credential", "push", *args)
    return git(repo, "push", *args)


def is_git_repo(path: Path) -> bool:
    """True if `path` is inside a git working tree (not only when it contains `.git` itself)."""
    if not shutil.which("git"):
        return False
    try:
        return git(path, "rev-parse", "--is-inside-work-tree", check=False, timeout=15) == "true"
    except (OSError, subprocess.TimeoutExpired):
        return False
