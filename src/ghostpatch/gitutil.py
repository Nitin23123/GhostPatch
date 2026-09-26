"""Small helpers around the `git` command line."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


def git(repo: Path, *args: str, check: bool = True) -> str:
    """Run a git command in `repo` and return its output. Raises RuntimeError if it fails."""
    proc = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if check and proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {(proc.stderr or proc.stdout).strip()}")
    return proc.stdout.strip()


def is_git_repo(path: Path) -> bool:
    """True if `path` is inside a git working tree (not only when it contains `.git` itself)."""
    if not shutil.which("git"):
        return False
    try:
        return git(path, "rev-parse", "--is-inside-work-tree", check=False) == "true"
    except OSError:
        return False
