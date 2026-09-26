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
import re
import shutil
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


_PYTHONS: dict[str, str] = {}  # repo -> chosen interpreter, so we only probe once


def project_python(repo: Path) -> str:
    """The Python that runs this project's tests, quoted for a shell command.

    Not necessarily GhostPatch's own: installed with pipx, GhostPatch lives in a private
    virtualenv that lacks the project's packages. Candidates, in order: the project's
    virtualenv, an activated one, `python` on PATH, then our own interpreter. The first one
    that can import pytest wins.
    """
    key = str(repo.resolve())
    if key in _PYTHONS:
        return _PYTHONS[key]
    folders = [repo / ".venv", repo / "venv", repo / "env"]
    if os.environ.get("VIRTUAL_ENV"):
        folders.append(Path(os.environ["VIRTUAL_ENV"]))
    candidates: list[Path] = []
    for folder in folders:
        candidates += [folder / exe for exe in ("Scripts/python.exe", "bin/python") if (folder / exe).is_file()]
    on_path = shutil.which("python") or shutil.which("python3")
    if on_path and "WindowsApps" not in on_path:  # skip the Microsoft Store stub
        candidates.append(Path(on_path))
    candidates.append(Path(sys.executable))
    chosen = next((c for c in candidates if _can_import(c, "pytest")), candidates[0])
    _PYTHONS[key] = f'"{chosen}"'
    return _PYTHONS[key]


def _can_import(python: Path, module: str) -> bool:
    try:
        return subprocess.run([str(python), "-c", f"import {module}"], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


RUNNER_MISSING = ("No module named pytest", "is not recognized as an internal or external command",
                  "command not found", "Missing script:")


def runner_missing(output: str) -> bool:
    """True when the tests never ran because the test runner itself is missing."""
    return any(marker in output for marker in RUNNER_MISSING)


def no_tests_ran(output: str) -> bool:
    """True when the runner worked but found no tests (pytest: "no tests ran"; node: "tests 0")."""
    return "no tests ran" in output or bool(re.search(r"^\s*[ℹ#] tests 0\s*$", output, flags=re.MULTILINE))


def _npm_test_script(repo: Path) -> str:
    try:
        scripts = json.loads((repo / "package.json").read_text(encoding="utf-8")).get("scripts", {})
    except (ValueError, OSError):
        return ""
    test = scripts.get("test", "") if isinstance(scripts, dict) else ""
    return "" if "no test specified" in test else test


def detect_test_command(repo: Path) -> str | None:
    """The obvious way to run this project's tests, or None."""
    if (repo / "package.json").is_file():
        return "npm test" if _npm_test_script(repo) else "node --test"
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py")
    if any((repo / m).exists() for m in markers) or any(repo.glob("test_*.py")) or (repo / "tests").is_dir():
        return f"{project_python(repo)} -m pytest -q"
    return None


def test_files_command(repo: Path, test_files: list[str]) -> list[str]:
    """Commands that run just these test files (one per language), or [] if we can't tell how."""
    quote = lambda p: f'"{p}"' if " " in p else p  # noqa: E731
    python = [p for p in test_files if p.endswith(".py")]
    js = [p for p in test_files if p.endswith((".js", ".jsx", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx"))]
    commands = []
    if python:
        commands.append(f"{project_python(repo)} -m pytest -q -p no:cacheprovider " + " ".join(map(quote, python)))
    if js:
        runner = "npm test --" if _npm_test_script(repo) else "node --test"
        commands.append(f"{runner} " + " ".join(map(quote, js)))
    return commands


def run_tests(repo: Path, command: str, timeout: int = TEST_TIMEOUT_SECONDS, env: dict | None = None) -> TestRun:
    try:
        proc = subprocess.run(command, shell=True, cwd=repo, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout,
                              env={**os.environ, **env} if env else None)
    except subprocess.TimeoutExpired:
        return TestRun(command, False, f"The tests did not finish within {timeout} seconds.")
    output = (proc.stdout + "\n" + proc.stderr).strip()
    return TestRun(command, proc.returncode == 0, output[-MAX_OUTPUT_CHARS:])


def headline(output: str) -> str:
    """The one line of test output that says how it went, e.g. '1 failed, 3 passed in 0.12s'."""
    lines = [line.strip(" =") for line in output.splitlines() if line.strip()]
    for line in reversed(lines):  # pytest's summary line
        if any(w in line for w in (" passed", " failed", " error", "no tests ran")) and " in " in line:
            return line
    counts = {}  # node --test prints "ℹ pass 3" / "# fail 1"
    for line in lines:
        parts = line.lstrip("ℹ#").split()
        if len(parts) == 2 and parts[0] in ("tests", "pass", "fail") and parts[1].isdigit():
            counts[parts[0]] = int(parts[1])
    if counts:
        return ", ".join(f"{n} {'passed' if k == 'pass' else 'failed' if k == 'fail' else 'tests'}"
                         for k, n in counts.items() if k != "tests" or len(counts) == 1)
    return lines[-1][:160] if lines else ""


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
