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
from pathlib import Path, PurePosixPath

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
                  "command not found", "Missing script:", "JAVA_HOME", "could not choose a version of cargo",
                  "go: cannot find main module",
                  # the runner is there, but the OS refused to start the compiled tests
                  "could not execute process", "Application Control policy", "(os error 4551)")
GO_PACKAGE_LINE = re.compile(r"^(ok|FAIL|\?)\s+\S+.*$", re.MULTILINE)
CARGO_RESULT = re.compile(r"^test result: \w+\. (\d+) passed; (\d+) failed; (\d+) ignored", re.MULTILINE)
MAVEN_TESTS_RUN = re.compile(r"^(?:\[\w+\] )?Tests run: (\d+), Failures: (\d+), Errors: (\d+), Skipped: (\d+)\s*$",
                             re.MULTILINE)
GRADLE_COUNTS = re.compile(r"^(\d+) tests? completed, (\d+) failed(?:, (\d+) skipped)?", re.MULTILINE)


def runner_missing(output: str) -> bool:
    """True when the tests never ran because the test runner itself is missing."""
    return any(marker in output for marker in RUNNER_MISSING)


def no_tests_ran(output: str) -> bool:
    """True when the runner worked but found no tests."""
    if "no tests ran" in output or re.search(r"^\s*[ℹ#] tests 0\s*$", output, flags=re.MULTILINE):
        return True  # pytest / node --test
    go = [m.group(0) for m in GO_PACKAGE_LINE.finditer(output)]
    if go and "--- FAIL" not in output:
        return all("[no test files]" in line or "[no tests to run]" in line for line in go)
    cargo = CARGO_RESULT.findall(output)
    if cargo:
        return sum(int(p) + int(f) for p, f, _ in cargo) == 0
    if "BUILD SUCCESS" in output and "Tests run:" not in output:
        return True  # Maven: no test class matched
    return "No tests found for given includes" in output or "Tests run: 0," in output  # Gradle / Maven


def _npm_test_script(repo: Path) -> str:
    try:
        scripts = json.loads((repo / "package.json").read_text(encoding="utf-8")).get("scripts", {})
    except (ValueError, OSError):
        return ""
    test = scripts.get("test", "") if isinstance(scripts, dict) else ""
    return "" if "no test specified" in test else test


def _wrapper(repo: Path, name: str, windows_suffix: str, fallback: str) -> str:
    """A build tool's wrapper script if the project has one (mvnw, gradlew), else the tool itself."""
    if sys.platform == "win32" and (repo / f"{name}{windows_suffix}").is_file():
        return f"{name}{windows_suffix}"
    if sys.platform != "win32" and (repo / name).is_file():
        return f"./{name}"
    return fallback


def maven_command(repo: Path) -> str | None:
    return _wrapper(repo, "mvnw", ".cmd", "mvn") + " -B" if (repo / "pom.xml").is_file() else None


def gradle_command(repo: Path) -> str | None:
    markers = ("build.gradle", "build.gradle.kts", "settings.gradle", "settings.gradle.kts")
    return _wrapper(repo, "gradlew", ".bat", "gradle") if any((repo / m).is_file() for m in markers) else None


def detect_test_command(repo: Path) -> str | None:
    """The obvious way to run this project's tests, or None. Suites keep going after a failure,
    so one run lists every failing test."""
    if (repo / "package.json").is_file():
        return "npm test" if _npm_test_script(repo) else "node --test"
    if (repo / "go.mod").is_file():
        return "go test ./..."
    if (repo / "Cargo.toml").is_file():
        return "cargo test --no-fail-fast"
    if maven_command(repo):
        return f"{maven_command(repo)} -fae test"
    if gradle_command(repo):
        return f"{gradle_command(repo)} test --continue"
    markers = ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini", "conftest.py")
    if any((repo / m).exists() for m in markers) or any(repo.glob("test_*.py")) or (repo / "tests").is_dir():
        return f"{project_python(repo)} -m pytest -q"
    return None


def _nearest(repo: Path, rel: str, manifest: str) -> str | None:
    """The folder (relative to the repo) of the closest `manifest` above a file, e.g. its go.mod."""
    folder = PurePosixPath(rel).parent
    while True:
        if (repo / folder / manifest).is_file():
            return "" if str(folder) == "." else folder.as_posix()
        if str(folder) in (".", ""):
            return None
        folder = folder.parent


def _go_commands(repo: Path, files: list[str]) -> list[str]:
    """`go test ./cart ./pricing`: Go runs tests a package (a folder) at a time."""
    by_module: dict[str, set[str]] = {}
    for rel in files:
        module = _nearest(repo, rel, "go.mod") or ""
        package = PurePosixPath(rel).parent.as_posix()
        package = package[len(module):].lstrip("/") if module else package
        by_module.setdefault(module, set()).add("." if package in (".", "") else f"./{package}")
    return [("go test " if not module else f"go -C {module} test ") + " ".join(sorted(packages))
            for module, packages in sorted(by_module.items())]


def _cargo_commands(repo: Path, files: list[str]) -> list[str]:
    """Integration tests by name (`--test cart`); unit tests inside source files by module (`cart::`)."""
    from ghostpatch.parsers_typed import rust_module_path

    by_crate: dict[str, tuple[list[str], list[str]]] = {}
    for rel in files:
        crate = _nearest(repo, rel, "Cargo.toml") or ""
        inside = rel[len(crate):].lstrip("/") if crate else rel
        targets, filters = by_crate.setdefault(crate, ([], []))
        if inside.startswith("tests/") and inside.count("/") == 1:
            targets.append(PurePosixPath(inside).stem)
        else:
            module = rust_module_path(inside)
            filters.append("::".join(module) + "::" if module else "tests::")
    commands = []
    for crate, (targets, filters) in sorted(by_crate.items()):
        package = ""
        if crate:
            text = (repo / crate / "Cargo.toml").read_text(encoding="utf-8", errors="replace")
            found = re.search(r'^name\s*=\s*"([^"]+)"', text, flags=re.MULTILINE)
            package = f" -p {found.group(1)}" if found else ""
        if targets:
            commands.append(f"cargo test --no-fail-fast{package} " + " ".join(f"--test {t}" for t in sorted(set(targets))))
        if filters:
            commands.append(f"cargo test --no-fail-fast{package} -- " + " ".join(sorted(set(filters))))
    return commands


def test_files_command(repo: Path, test_files: list[str]) -> list[str]:
    """Commands that run just these test files (one per language), or [] if we can't tell how.

    A Rust source file counts as a test file here when its `#[cfg(test)]` module is what changed."""
    quote = lambda p: f'"{p}"' if " " in p else p  # noqa: E731
    python = [p for p in test_files if p.endswith(".py")]
    js = [p for p in test_files if p.endswith((".js", ".jsx", ".mjs", ".cjs", ".ts", ".mts", ".cts", ".tsx"))]
    go = [p for p in test_files if p.endswith("_test.go")]
    rust = [p for p in test_files if p.endswith(".rs")]
    java = sorted({PurePosixPath(p).stem for p in test_files if p.endswith(".java")})
    commands = []
    if python:
        commands.append(f"{project_python(repo)} -m pytest -q -p no:cacheprovider " + " ".join(map(quote, python)))
    if js:
        runner = "npm test --" if _npm_test_script(repo) else "node --test"
        commands.append(f"{runner} " + " ".join(map(quote, js)))
    if go:
        commands += _go_commands(repo, go)
    if rust:
        commands += _cargo_commands(repo, rust)
    if java and maven_command(repo):
        commands.append(f"{maven_command(repo)} test -Dtest={','.join(java)} -Dsurefire.failIfNoSpecifiedTests=false")
    elif java and gradle_command(repo):
        commands.append(f"{gradle_command(repo)} test " + " ".join(f"--tests {name}" for name in java))
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


def _typed_headline(output: str) -> str | None:
    """Go, cargo, Maven and Gradle summaries, e.g. '2 passed, 1 failed'."""
    cargo = CARGO_RESULT.findall(output)
    if cargo:
        passed, failed, ignored = (sum(int(row[i]) for row in cargo) for i in range(3))
        return f"{passed} passed, {failed} failed" + (f", {ignored} ignored" if ignored else "")
    if "could not compile" in output:
        return "the code does not compile"
    packages = [m.group(0) for m in GO_PACKAGE_LINE.finditer(output)]
    if packages:
        ok = sum(line.startswith("ok") for line in packages)
        failed = sum(line.startswith("FAIL") for line in packages)
        broken = sum("[build failed]" in line or "[setup failed]" in line for line in packages)
        tests = len(re.findall(r"^\s*--- FAIL: \S+", output, flags=re.MULTILINE))
        if not ok and not failed:
            return "no test files"
        parts = [f"{ok} package{'s' if ok != 1 else ''} ok"]
        if failed:
            parts.append(f"{failed} failed" + (f" ({tests} failing test{'s' if tests != 1 else ''})" if tests else "")
                         + (" - build failed" if broken else ""))
        return ", ".join(parts)
    maven = MAVEN_TESTS_RUN.findall(output)
    if maven:
        run, failures, errors, skipped = (sum(int(row[i]) for row in maven) for i in range(4))  # one per module
        return f"{run - failures - errors - skipped} passed, {failures + errors} failed" + (
            f", {skipped} skipped" if skipped else "")
    if "COMPILATION ERROR" in output or "Compilation failed" in output or "Compilation failure" in output:
        return "the code does not compile"
    gradle = GRADLE_COUNTS.findall(output)
    if gradle:
        done, failed, skipped = int(gradle[-1][0]), int(gradle[-1][1]), int(gradle[-1][2] or 0)
        return f"{done - failed - skipped} passed, {failed} failed" + (f", {skipped} skipped" if skipped else "")
    if "BUILD SUCCESS" in output:  # Gradle only counts tests when some fail
        return "build successful"
    if "BUILD FAILED" in output or "BUILD FAILURE" in output:
        return "build failed"
    return None


def headline(output: str) -> str:
    """The one line of test output that says how it went, e.g. '1 failed, 3 passed in 0.12s'."""
    typed = _typed_headline(output)
    if typed:
        return typed
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
