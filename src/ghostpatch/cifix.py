"""CI auto-fixer: when the test suite fails, fix it.

`ghostpatch ci-fix` runs the project's tests. If they fail, the failure output (stack traces
included) becomes the bug report for a normal fixing session. Afterwards GhostPatch runs the
tests again itself, and only if they now pass does it commit, push or open a pull request.
It never trusts the agent's own word that the tests pass.

Meant for CI, where the machine is a throwaway sandbox, so commands run without approval.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_CHARS = 6000
TEST_TIMEOUT_SECONDS = 900


@dataclass
class TestRun:
    command: str
    passed: bool
    output: str


def detect_test_command(repo: Path) -> str | None:
    """The obvious way to run this project's tests, or None."""
    package = repo / "package.json"
    if package.is_file():
        try:
            scripts = json.loads(package.read_text(encoding="utf-8")).get("scripts", {})
        except (ValueError, OSError):
            scripts = {}
        test = scripts.get("test", "")
        return "npm test" if test and "no test specified" not in test else "node --test"
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py")
    if any((repo / m).exists() for m in markers) or any(repo.glob("test_*.py")) or (repo / "tests").is_dir():
        return f'"{sys.executable}" -m pytest -q'
    return None


def run_tests(repo: Path, command: str) -> TestRun:
    try:
        proc = subprocess.run(command, shell=True, cwd=repo, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=TEST_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return TestRun(command, False, f"The tests did not finish within {TEST_TIMEOUT_SECONDS} seconds.")
    output = (proc.stdout + "\n" + proc.stderr).strip()
    return TestRun(command, proc.returncode == 0, output[-MAX_OUTPUT_CHARS:])


def issue_from_failure(run: TestRun) -> str:
    return (
        "The project's test suite is failing in CI. Make the tests pass by fixing the code. "
        "Only change a test if it is clearly wrong about the intended behaviour.\n\n"
        f"Test command: {run.command}\n\nOutput (the end of it):\n{run.output}"
    )


def step_summary(markdown: str) -> None:
    """Show a summary on the GitHub Actions run page, when running there."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a", encoding="utf-8") as f:
            f.write(markdown + "\n")
