"""How much should you trust a fix? A score from 0 to 100.

Two things make a fix trustworthy:
1. The tests were run **after** the last edit, and they passed.  (up to 50 points)
2. The code the change could affect (its blast radius, from the code graph) is actually
   reached by tests, so "the tests passed" means something.  (up to 50 points)

If the tests failed after the last edit, the score is low no matter what.
"""

from __future__ import annotations

from typing import Any

HIGH, MEDIUM = 80, 50
MAX_LISTED = 20


def assess(workspace: Any) -> dict[str, Any]:
    if not workspace.changed_files:
        return {"score": 0, "level": "none", "tests_after_edit": "no changes", "blast_radius": 0,
                "covered": 0, "uncovered": [], "summary": "No files were changed."}

    run = workspace.last_test_run
    if run is None or run[0] < workspace.last_edit_at:
        tests = "not run"
    else:
        tests = "passed" if run[1] else "failed"

    radius: dict[str, dict] = {}
    if workspace.graph is not None and workspace.edited_symbols:
        workspace.graph.refresh()
        radius = workspace.graph.blast_radius(list(workspace.edited_symbols.values()))
    covered = [q for q, info in radius.items() if info["tested"]]
    uncovered = sorted(q for q, info in radius.items() if not info["tested"])
    coverage = len(covered) / len(radius) if radius else None

    test_points = {"passed": 50, "not run": 0, "failed": 0}[tests]
    coverage_points = round(50 * coverage) if coverage is not None else 20  # unknown: partial credit
    score = 10 if tests == "failed" else test_points + coverage_points
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
    return {
        "score": score, "level": level, "tests_after_edit": tests, "blast_radius": len(radius),
        "covered": len(covered), "uncovered": uncovered[:MAX_LISTED],
        "summary": f"{score}/100 ({level}): {parts}.",
    }
