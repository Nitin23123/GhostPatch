"""How much should you trust a fix? A score from 0 to 100.

Two things make a fix trustworthy:
1. The tests were run **after** the last edit, and they passed.  (up to 50 points)
2. The code the change could affect (its blast radius, from the code graph) is actually
   reached by tests, so "the tests passed" means something.  (up to 50 points)

If the tests failed after the last edit, the score is low no matter what.

GhostPatch's own red→green proof (see proof.py) outranks what the agent reported: a proven
fix gains 10 points, tests that pass even without the fix lose 10, and tests that fail with the
fix count as failed tests.
"""

from __future__ import annotations

from typing import Any

HIGH, MEDIUM = 80, 50
MAX_LISTED = 20
PROOF_POINTS = 10


def assess(workspace: Any, proof: Any = None, regression: Any = None) -> dict[str, Any]:
    """`regression` is GhostPatch's own before/after run of the whole suite (see regression.py)."""
    if not workspace.changed_files:
        return {"score": 0, "level": "none", "tests_after_edit": "no changes", "blast_radius": 0,
                "covered": 0, "uncovered": [], "summary": "No files were changed.", "proof": None}

    run = workspace.last_test_run
    if run is None or run[0] < workspace.last_edit_at:
        tests = "not run"
    else:
        tests = "passed" if run[1] else "failed"
    status = getattr(proof, "status", None)
    if status == "not_green":
        tests = "failed"  # GhostPatch saw the new tests fail with the fix in place
    elif status in ("proven", "not_red") and tests == "not run":
        tests = "passed"  # GhostPatch ran them after the last edit and they passed
    regressed = getattr(regression, "status", None) == "regressed"
    if regressed:
        tests = "failed"  # the fix broke tests that passed before
    elif regression is not None and getattr(regression, "passed_after", False) and status != "not_green":
        tests = "passed"  # GhostPatch ran the whole suite after the last edit and it passed

    radius: dict[str, dict] = {}
    if workspace.graph is not None and workspace.edited_symbols:
        workspace.graph.refresh()
        radius = workspace.graph.blast_radius(list(workspace.edited_symbols))
    covered = [q for q, info in radius.items() if info["tested"]]
    uncovered = sorted(q for q, info in radius.items() if not info["tested"])
    coverage = len(covered) / len(radius) if radius else None

    test_points = {"passed": 50, "not run": 0, "failed": 0}[tests]
    coverage_points = round(50 * coverage) if coverage is not None else 20  # unknown: partial credit
    score = 10 if tests == "failed" else test_points + coverage_points
    if tests != "failed" and status == "proven":
        score = min(100, score + PROOF_POINTS)
    elif tests != "failed" and status == "not_red":
        score = max(0, score - PROOF_POINTS)
    level = "high" if score >= HIGH else "medium" if score >= MEDIUM else "low"

    parts = {
        "passed": "tests passed after the last edit",
        "failed": "tests FAILED after the last edit",
        "not run": "tests were not run after the last edit",
    }[tests]
    if radius:
        parts += f"; {len(covered)} of {len(radius)} affected functions are covered by tests"
    else:
        parts += "; the change's blast radius is unknown (no code graph or no edited functions)"
    if status == "proven":
        parts += "; 🔴→🟢 proven: the new tests fail without the fix"
    elif status == "not_red":
        parts += "; the new tests pass even without the fix"
    if regressed:
        parts += f"; 🛡 the fix broke {len(regression.broken)} test(s) that passed before"
    elif regression is not None:
        parts += "; 🛡 the whole suite ran before and after: nothing broke"
    return {
        "score": score, "level": level, "tests_after_edit": tests, "blast_radius": len(radius),
        "covered": len(covered), "uncovered": uncovered[:MAX_LISTED],
        "summary": f"{score}/100 ({level}): {parts}.", "proof": status,
        "regressions": len(regression.broken) if regressed else (0 if regression is not None else None),
    }
