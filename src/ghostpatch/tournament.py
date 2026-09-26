"""🏆 Fix tournament: several independent fixes compete, and the best-proven one wins.

Small free models are inconsistent: the same bug can get a clean root-cause fix one time and
a symptom patch the next. So instead of trusting one attempt, the tournament runs several
candidates from the same starting point, each with its own strategy:

1. Direct: the normal workflow.
2. Test first: reproduce the bug with a failing test before touching the code.
3. Graph first: map every caller of the suspect code, then pick a fix that's right for all of them.

Candidates are judged on evidence GhostPatch gathers itself, not on their own claims:
- the red→green proof (their tests fail without the fix and pass with it),
- the confidence score (tests after the last edit, how much of the blast radius tests reach),
- cross-examination: a candidate's code must also pass its rivals' proven tests,
- the regression guard: a candidate that breaks tests which passed before is disqualified,
- and a small penalty for large diffs.

Candidates run one after another, reverting the files in between. Free tiers limit requests
per minute anyway, and this needs no second copy of the repository (or of node_modules).
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ghostpatch.agent import RunResult
from ghostpatch.confidence import assess
from ghostpatch.parsers import is_test_path
from ghostpatch.proof import Proof, _put, _read, _run_all, prove_fix
from ghostpatch.regression import RegressionCheck, SuiteRun, compare, run_suite
from ghostpatch.cifix import test_files_command
from ghostpatch.tools import Workspace

MAX_CANDIDATES = 5
CROSS_POINTS = 20  # for passing every rival's proven tests
MAX_SIZE_PENALTY = 10

STRATEGIES = [
    ("direct", "Direct", ""),
    ("test-first", "Test first",
     "Strategy for this attempt: before changing any code, write a small regression test that reproduces "
     "the bug and run it to watch it fail. Then fix the root cause and run it again."),
    ("graph-first", "Graph first",
     "Strategy for this attempt: before editing, use find_callers and impact_of_change on the code you "
     "suspect, and choose the fix that is correct for every caller, not only the one in the report."),
]


@dataclass
class Candidate:
    number: int
    strategy: str
    label: str
    fixed: bool = False
    summary: str = ""
    error: str | None = None
    files: dict[str, str | None] = field(default_factory=dict)  # path -> content after the attempt
    originals: dict[str, str | None] = field(default_factory=dict)
    edited_symbols: dict[str, str] = field(default_factory=dict)
    confidence: dict[str, Any] = field(default_factory=dict)
    proof: Proof | None = None
    rivals_passed: int = 0
    rivals_total: int = 0
    changed_lines: int = 0
    points: float = 0.0
    disqualified: str | None = None
    result: RunResult | None = None
    regression: RegressionCheck | None = None

    @property
    def code_files(self) -> list[str]:
        return sorted(p for p in self.files if not is_test_path(p))

    @property
    def test_files(self) -> list[str]:
        return sorted(p for p in self.files if is_test_path(p))

    def as_dict(self, winner: int | None) -> dict[str, Any]:
        return {
            "number": self.number, "strategy": self.strategy, "label": self.label, "fixed": self.fixed,
            "summary": self.summary, "error": self.error, "files": sorted(self.files),
            "score": self.confidence.get("score", 0), "proof": self.proof.status if self.proof else None,
            "rivals": f"{self.rivals_passed}/{self.rivals_total}" if self.rivals_total else None,
            "changed_lines": self.changed_lines, "points": round(self.points, 1),
            "disqualified": self.disqualified, "winner": self.number == winner,
            "broke": len(self.regression.broken) if self.regression else None,
        }


@dataclass
class Tournament:
    candidates: list[Candidate]
    winner: Candidate | None
    workspace: Workspace  # holds the winner's changes, ready to be saved
    result: RunResult
    stopped: str | None = None  # why the tournament ended early, if it did

    def as_dict(self) -> dict[str, Any]:
        number = self.winner.number if self.winner else None
        return {"winner": number, "stopped": self.stopped,
                "candidates": [c.as_dict(number) for c in self.candidates]}


def _changed_lines(candidate: Candidate) -> int:
    total = 0
    for rel in candidate.code_files:
        before = (candidate.originals.get(rel) or "").splitlines()
        after = (candidate.files.get(rel) or "").splitlines()
        total += sum(1 for line in difflib.unified_diff(before, after, lineterm="", n=0)
                     if line[:1] in "+-" and not line.startswith(("+++", "---")))
    return total


def _rival_path(rel: str, number: int) -> str:
    """tests/test_cart.py -> tests/test_cart_rival2.py ; tests/cart.test.ts -> tests/cart_rival2.test.ts"""
    folder, _, name = rel.rpartition("/")
    stem, dot, rest = name.partition(".")
    renamed = f"{stem}_rival{number}{dot}{rest}"
    return f"{folder}/{renamed}" if folder else renamed


def _disqualify(c: Candidate) -> str | None:
    if c.error:
        return f"stopped: {c.error}"
    if not c.fixed:
        return "did not claim a fix"
    if not c.code_files:
        return "changed no code"
    if c.proof is not None and c.proof.status == "not_green":
        return "its own tests fail with its fix"
    if c.regression is not None and c.regression.status == "regressed":
        return f"broke {len(c.regression.broken)} test(s) that passed before"
    if c.confidence.get("tests_after_edit") == "failed":
        return "tests failed after its last edit"
    return None


def cross_examine(repo: Path, candidates: list[Candidate], approve: Callable[[str], bool] | None, ui: Any) -> None:
    """Run each eligible candidate's code against every rival's proven tests."""
    eligible = [c for c in candidates if not c.disqualified]
    judges = [c for c in candidates if c.proof is not None and c.proof.proven and c.test_files]
    approved: dict[str, bool] = {}
    for c in eligible:
        rivals = [j for j in judges if j is not c]
        if not rivals:
            continue
        ui.thought(f"_🏆 Cross-examining candidate {c.number} with {len(rivals)} rival test suite(s)…_")
        _put(repo, c.files)
        try:
            for rival in rivals:
                tests = {(_rival_path(t, rival.number) if t in c.files else t): rival.files[t] for t in rival.test_files}
                saved = {rel: _read(repo / rel) for rel in tests}
                _put(repo, tests)
                try:
                    commands = test_files_command(repo, sorted(tests))
                    for command in commands:
                        if command not in approved:
                            approved[command] = approve(command) if approve else True
                    if not commands or not all(approved[cmd] for cmd in commands):
                        continue
                    passed, _ = _run_all(repo, commands)
                finally:
                    _put(repo, saved)
                c.rivals_total += 1
                c.rivals_passed += passed
        finally:
            _put(repo, {rel: c.originals.get(rel) for rel in c.files})


def score(c: Candidate) -> float:
    if c.disqualified:
        return -1.0
    points = float(c.confidence.get("score", 0))
    if c.rivals_total:
        points += CROSS_POINTS * c.rivals_passed / c.rivals_total
    return points - min(MAX_SIZE_PENALTY, c.changed_lines / 10)


def run_tournament(
    repo: Path, new_workspace: Callable[[], Workspace], make_agent: Callable[..., Any], ui: Any, issue: str,
    candidates: int = 3, approve: Callable[[str], bool] | None = None, prove: bool = True,
    describe_error: Callable[[Exception], str] = str, proof_tests: list[str] | None = None,
    baseline: SuiteRun | None = None,
) -> Tournament:
    """Run the candidates, judge them and leave the winner's changes in the repository.

    A model error (such as every quota being used up) ends the tournament early, but the
    candidates that finished are still judged.
    """
    import openai

    n = max(2, min(candidates, MAX_CANDIDATES))
    entries: list[Candidate] = []
    stopped: str | None = None
    for i in range(n):
        strategy, label, hint = STRATEGIES[i % len(STRATEGIES)]
        if i >= len(STRATEGIES):
            label = f"{label} #{i // len(STRATEGIES) + 1}"
        c = Candidate(i + 1, strategy, label)
        entries.append(c)
        ui.thought(f"_🏆 Candidate {c.number} of {n}: {label}…_")
        ws = new_workspace()
        try:
            try:
                result = make_agent(ws).run(f"{issue}\n\n{hint}".strip())
                c.result, c.fixed, c.summary = result, result.fixed, result.summary
            except openai.APIError as e:
                c.error = stopped = describe_error(e)
            c.files = {rel: _read(repo / rel) for rel in ws.changed_files}
            c.originals = {rel: ws.originals.get(rel) for rel in ws.changed_files}
            c.edited_symbols = dict(ws.edited_symbols)
            if prove and c.fixed and not c.error and c.code_files:
                c.proof = prove_fix(repo, ws.originals, ws.changed_files, approve=approve, ui=ui,
                                    extra_tests=proof_tests)
            if baseline is not None and c.fixed and not c.error and c.code_files:
                c.regression = compare(baseline, run_suite(repo, baseline.command))
            c.confidence = assess(ws, c.proof, c.regression)
            c.changed_lines = _changed_lines(c)
        finally:
            _put(repo, {rel: ws.originals.get(rel) for rel in ws.changed_files})  # back to the start
        if stopped:
            break

    for c in entries:
        c.disqualified = _disqualify(c)
    cross_examine(repo, entries, approve, ui)
    for c in entries:
        c.points = score(c)

    eligible = sorted((c for c in entries if not c.disqualified), key=lambda c: (-c.points, c.changed_lines, c.number))
    winner = eligible[0] if eligible else None
    # Nothing verified: keep the most promising attempt for inspection, like a single run would.
    kept = winner or max((c for c in entries if c.files), key=lambda c: (c.confidence.get("score", 0), -c.number),
                         default=None)

    workspace = new_workspace()
    total = RunResult(fixed=False, summary="", steps=0)
    for c in entries:
        if c.result is not None:
            total.steps += c.result.steps
            total.prompt_tokens += c.result.prompt_tokens
            total.completion_tokens += c.result.completion_tokens
    if kept is not None:
        _put(repo, kept.files)
        workspace.originals = dict(kept.originals)
        workspace.changed_files = set(kept.files)
        workspace.edited_symbols = dict(kept.edited_symbols)
    table = "; ".join(
        f"#{c.number} {c.label}: " + (c.disqualified or f"{c.points:.0f} points") for c in entries)
    if winner is not None:
        total.fixed = True
        others = len(entries) - 1
        total.summary = (f"{winner.summary}\n\n🏆 Candidate {winner.number} ({winner.label}) won the tournament "
                         f"against {others} other{'s' if others != 1 else ''}.")
        ui.thought(f"_🏆 Candidate {winner.number} ({winner.label}) wins. {table}._")
    else:
        total.summary = ("No candidate produced a verified fix. "
                         + (f"Kept candidate {kept.number}'s attempt for you to inspect. " if kept else "")
                         + f"{table}.")
        ui.thought(f"_🏆 {total.summary}_")
    return Tournament(entries, winner, workspace, total, stopped)
