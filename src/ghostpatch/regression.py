"""🛡 Regression guard: a fix must not break what already worked.

The red→green proof shows the fix does what it should. This shows it doesn't break anything
else. GhostPatch runs the project's whole test suite before the ghost starts (the baseline) and
again after the fix. A test that passed before and fails now is a regression: the ghost gets it
back ("your fix broke test_invoice_total") and has another go, automatically.

Tests that were already failing before the ghost started don't count against the fix.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from ghostpatch.cifix import detect_test_command, headline, no_tests_ran, run_tests, runner_missing

SUITE_TIMEOUT_SECONDS = 900
MAX_OUTPUT_CHARS = 3000
WHOLE_SUITE = "(the test suite)"  # stands for a failure no single test name was found for

REFIX_TEMPLATE = """{issue}

Your fix made tests fail that passed before you started:
{broken}

The end of the test output:
{output}

Fix the code so the original bug stays fixed AND these tests pass again. Don't weaken or delete
tests to make them pass unless a test is clearly wrong about the intended behaviour."""


@dataclass
class SuiteRun:
    command: str
    passed: bool
    ran: bool  # False when the tests never ran: no runner, no tests, or a timeout
    failing: set[str] = field(default_factory=set)
    headline: str = ""
    output: str = ""


@dataclass
class RegressionCheck:
    status: str  # clean | regressed
    before: str
    after: str
    broken: list[str] = field(default_factory=list)  # passed before, fail now
    repaired: list[str] = field(default_factory=list)  # failed before, pass now
    already_failing: int = 0
    passed_after: bool = False  # the whole suite passes after the fix
    output: str = ""

    @property
    def summary(self) -> str:
        if self.status == "regressed":
            names = ", ".join(self.broken[:5]) + (f" and {len(self.broken) - 5} more" if len(self.broken) > 5 else "")
            return f"The fix breaks {len(self.broken)} test(s) that passed before: {names}."
        note = f" ({self.already_failing} were already failing before the fix.)" if self.already_failing else ""
        return f"No regressions: the whole test suite ran before and after the fix, and nothing broke.{note}"

    def as_dict(self) -> dict:
        return {**asdict(self), "summary": self.summary}


def suite_command(repo: Path) -> str | None:
    """How to run the whole suite, asking pytest to list every failure by name."""
    command = detect_test_command(repo)
    if command and "-m pytest" in command:
        command += " -rfE -p no:cacheprovider"
    return command


def failing_tests(output: str) -> set[str]:
    """Names of the failing tests in pytest's or node's test runner output."""
    names = set(re.findall(r"^(?:FAILED|ERROR) (\S+)", output, flags=re.MULTILINE))
    for match in re.finditer(r"^\s*✖ (.+?)(?: \([\d.]+m?s\))?\s*$", output, flags=re.MULTILINE):
        if match.group(1).rstrip(":") != "failing tests":
            names.add(match.group(1))
    return names


def run_suite(repo: Path, command: str) -> SuiteRun:
    run = run_tests(repo, command, timeout=SUITE_TIMEOUT_SECONDS, env={"PYTHONDONTWRITEBYTECODE": "1"})
    ran = not (runner_missing(run.output) or no_tests_ran(run.output) or "did not finish within" in run.output)
    failing = failing_tests(run.output) if not run.passed else set()
    if not run.passed and not failing:
        failing = {WHOLE_SUITE}  # e.g. an import error that stops the whole suite
    return SuiteRun(command, run.passed, ran, failing, headline(run.output), run.output[-MAX_OUTPUT_CHARS:])


def compare(before: SuiteRun, after: SuiteRun) -> RegressionCheck:
    broken = sorted(after.failing - before.failing)
    repaired = sorted(before.failing - after.failing)
    return RegressionCheck("regressed" if broken else "clean", before.headline, after.headline, broken, repaired,
                           len(before.failing - {WHOLE_SUITE}), after.passed, after.output)


def refix_brief(issue: str, check: RegressionCheck) -> str:
    return REFIX_TEMPLATE.format(issue=issue, broken="\n".join(f"- {name}" for name in check.broken),
                                 output=check.output[-2000:])
