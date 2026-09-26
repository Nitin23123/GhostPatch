"""🔴→🟢 Proof: show that the fix is what makes the tests pass.

The ghost says it fixed the bug and that the tests pass. Proof checks that independently.
GhostPatch itself runs the tests the run added or changed, twice:

1. with the fix temporarily taken out of the code: at least one must FAIL (red), and
2. with the fix back in: they must all PASS (green).

Red then green means the new tests really catch the bug and the fix really removes it.
Tests that also pass without the fix prove nothing about it, and that is reported too.
The code is always put back, even if a test run crashes or is interrupted.

Rust keeps unit tests in a `#[cfg(test)]` module inside the source file they test. When such a
module changed, the file counts as a test too: "without the fix" is then the original code with
the new test module kept in.
"""

from __future__ import annotations

import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ghostpatch.cifix import headline, no_tests_ran, run_tests, runner_missing, test_files_command
from ghostpatch.parsers import is_test_path
from ghostpatch.parsers_typed import rust_split

TEST_TIMEOUT_SECONDS = 300
MAX_OUTPUT_CHARS = 2000

SUMMARIES = {
    "proven": "Proven: the new tests fail without the fix and pass with it.",
    "not_red": "The new tests pass even without the fix, so they don't prove it.",
    "not_green": "The new tests FAIL with the fix in place.",
    "no_test": "No test was added or changed, so no test proves the fix.",
    "no_code_change": "Only tests changed, so there is no fix to prove.",
    "no_runner": "The new tests couldn't run: GhostPatch couldn't tell how, or the test runner couldn't start.",
    "empty": "The changed test files contain no tests, so no test proves the fix.",
    "skipped": "The proof was skipped: running the tests wasn't approved.",
}


PROVER_PROMPT = """You are GhostPatch's test writer. A bug in this repository was just fixed, but no test shows it.
Write ONE small regression test that would have caught the bug: it must fail on the original code (before the
diff below) and pass on the fixed code.

1. Read the bug report, the fix (the diff) and the code it changed.
2. Create a new test file where the project keeps its tests, in the project's test style, with a test that
   exercises exactly the behaviour the report describes. (In Rust you may instead add the test to the
   `#[cfg(test)]` module of the file that was fixed.)
3. Run it with the project's test command: it must pass on the current (fixed) code.
4. Call `finish` with fixed=true and a one-line summary of what the test checks.
{graph_guide}
Rules:
- You may only create or edit tests. Never change the fix.
- Keep the test small and deterministic: no network, clock or randomness.
- Shell commands run on {os} with the repository root as the working directory.{shell_hint}
- Keep your thinking short. Always act through tool calls, and you MUST call the `finish` tool.
"""


TEST_NAMING = ("Put tests where the project keeps them: a tests/ folder, test_*.py, *.test.ts, *_test.go, "
               "*Test.java under src/test/java, or a Rust #[cfg(test)] module (or tests/*.rs).")


def _same(a: str, b: str) -> bool:
    return re.sub(r"\s+", " ", a).strip() == re.sub(r"\s+", " ", b).strip()


def rust_tests_changed(rel_path: str, before: str | None, after: str | None) -> bool:
    """A Rust source file whose `#[cfg(test)]` modules changed."""
    if not rel_path.endswith(".rs") or is_test_path(rel_path) or after is None:
        return False
    return not _same(rust_split(before or "")[1], rust_split(after)[1])


def rust_code_changed(before: str | None, after: str | None) -> bool:
    """Whether a change to a Rust file touches the code, not only its test modules."""
    return not _same(rust_split(before or "")[0], rust_split(after or "")[0])


def only_tests_changed(rel_path: str, before: str | None = None, after: str | None = None) -> bool:
    """True for a write that leaves the code alone: a test file, or a Rust file changed only in its tests."""
    if is_test_path(rel_path):
        return True
    return rel_path.endswith(".rs") and after is not None and not rust_code_changed(before, after)


def tests_only(rel_path: str, before: str | None = None, after: str | None = None) -> str | None:
    if only_tests_changed(rel_path, before, after):
        return None
    return f"Only tests may be written in this step, not {rel_path}. {TEST_NAMING}"


def changed_code(repo: Path, originals: dict[str, str | None], changed_files: set[str]) -> list[str]:
    """The changed files whose code changed (for Rust, not only the test modules)."""
    return sorted(p for p in changed_files if not is_test_path(p)
                  and (not p.endswith(".rs") or rust_code_changed(originals.get(p), _read(repo / p))))


def changed_tests(repo: Path, originals: dict[str, str | None], changed_files: set[str]) -> list[str]:
    """The changed files that carry tests: test files, and Rust files whose test modules changed."""
    return sorted(p for p in changed_files if is_test_path(p)
                  or rust_tests_changed(p, originals.get(p), _read(repo / p)))


def prover_brief(issue: str, diffs: list[dict]) -> str:
    diff = "\n".join(d["diff"] for d in diffs)[:6000]
    return f"The bug report:\n{issue}\n\nThe fix (diff):\n{diff}"


@dataclass
class Proof:
    status: str
    tests: list[str] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    red: str = ""  # headline of the run without the fix
    green: str = ""  # headline of the run with the fix
    red_output: str = ""
    green_output: str = ""
    seconds: float = 0.0

    @property
    def proven(self) -> bool:
        return self.status == "proven"

    @property
    def summary(self) -> str:
        return SUMMARIES.get(self.status, self.status)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "summary": self.summary}


def _read(path: Path) -> str | None:
    if not path.is_file():
        return None
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _put(repo: Path, contents: dict[str, str | None]) -> None:
    """Write each file's content, or delete it when the content is None."""
    for rel, text in contents.items():
        path = repo / rel
        if text is None:
            if path.is_file():
                path.unlink()
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "w", encoding="utf-8", newline="") as f:
                f.write(text)
        if path.suffix == ".py":
            # Python trusts cached bytecode whose source has the same size and mtime (in seconds).
            # A one-character fix swapped within a second matches both, so drop the cache.
            for cached in (path.parent / "__pycache__").glob(f"{path.stem}.*.pyc"):
                cached.unlink(missing_ok=True)


def _run_all(repo: Path, commands: list[str]) -> tuple[bool, str]:
    outputs, passed = [], True
    for command in commands:
        run = run_tests(repo, command, timeout=TEST_TIMEOUT_SECONDS, env={"PYTHONDONTWRITEBYTECODE": "1"})
        passed = passed and run.passed
        outputs.append(run.output)
    return passed, "\n".join(outputs)


def prove_fix(
    repo: Path, originals: dict[str, str | None], changed_files: set[str],
    approve: Callable[[str], bool] | None = None, ui: Any = None, extra_tests: list[str] | None = None,
) -> Proof:
    """Run the changed tests without and then with the fix. `originals` is the content before the run.

    `extra_tests` are existing test files that should also prove the fix, such as the failing
    tests haunt mode left behind for the bug being fixed.
    """
    extra = {p for p in (extra_tests or []) if is_test_path(p) and (repo / p).is_file()}
    current = {rel: _read(repo / rel) for rel in changed_files}
    mixed = {p for p in changed_files if rust_tests_changed(p, originals.get(p), current[p])}  # Rust: tests in the file
    tests = sorted({p for p in changed_files if is_test_path(p)} | mixed | extra)
    code = sorted(p for p in changed_files if not is_test_path(p)
                  and (p not in mixed or rust_code_changed(originals.get(p), current[p])))
    if not tests:
        return Proof("no_test")
    if not code:
        return Proof("no_code_change", tests=tests)
    commands = test_files_command(repo, tests)
    if not commands:
        return Proof("no_runner", tests=tests)
    if approve is not None and not all(approve(c) for c in commands):
        return Proof("skipped", tests=tests, commands=commands)
    if ui is not None:
        ui.thought(f"_🔴→🟢 Checking the proof: running {', '.join(tests)} without the fix, then with it…_")

    started = time.time()
    fixed = {rel: _read(repo / rel) for rel in code}
    without_fix = {rel: originals.get(rel) for rel in code}
    for rel in mixed & set(code):  # the original code, with the new test module kept in
        without_fix[rel] = rust_split(originals.get(rel) or "")[0].rstrip() + "\n\n" + rust_split(current[rel] or "")[1] + "\n"
    try:
        _put(repo, without_fix)  # take the fix out
        red_passed, red_output = _run_all(repo, commands)
    finally:
        _put(repo, fixed)  # always put it back
    green_passed, green_output = _run_all(repo, commands)

    if runner_missing(green_output) or runner_missing(red_output):
        status = "no_runner"  # the tests never ran, which says nothing about the fix
    elif no_tests_ran(green_output):
        status = "empty"
    else:
        status = "not_green" if not green_passed else "not_red" if red_passed else "proven"
    proof = Proof(status, tests, commands, headline(red_output), headline(green_output),
                  red_output[-MAX_OUTPUT_CHARS:], green_output[-MAX_OUTPUT_CHARS:], round(time.time() - started, 1))
    if ui is not None:
        detail = f" (without the fix: {proof.red}; with it: {proof.green})" if proof.red or proof.green else ""
        ui.thought(f"_{proof.summary}{detail}_")
    return proof
