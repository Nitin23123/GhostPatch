"""Run history and undo.

Every run is saved to `.ghostpatch/runs/<id>.json` with the content of each changed file
before and after the run. `ghostpatch undo` puts the "before" content back, but only if
nobody has edited those files since the run finished (unless forced).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

RUNS_DIR = Path(".ghostpatch") / "runs"


class UndoError(Exception):
    """A run that can't be undone as requested. The message says why and what to do."""


def _read(path: Path) -> str | None:
    if not path.is_file():
        return None
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(text)


def _runs_dir(repo: Path) -> Path:
    runs = repo / RUNS_DIR
    runs.mkdir(parents=True, exist_ok=True)
    ignore = repo / ".ghostpatch" / ".gitignore"
    if not ignore.exists():
        ignore.write_text("*\n", encoding="utf-8")  # keep GhostPatch's data out of git
    return runs


def save_run(
    repo: Path, *, issue: str, provider: str, model: str, fixed: bool, summary: str,
    steps: int, prompt_tokens: int, completion_tokens: int,
    changed_files: set[str], originals: dict[str, str | None], error: str | None = None,
    issue_ref: dict | None = None, confidence: dict | None = None, events: list[dict] | None = None,
    **extra: Any,
) -> str:
    """Record a finished run and return its id."""
    runs = _runs_dir(repo)
    run_id = time.strftime("%Y%m%d-%H%M%S")
    suffix = 1
    while (runs / f"{run_id}.json").exists():
        suffix += 1
        run_id = f"{time.strftime('%Y%m%d-%H%M%S')}-{suffix}"
    data = {
        "id": run_id,
        "timestamp": time.time(),  # precise, for ordering runs made within the same second
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "issue": issue,
        "provider": provider,
        "model": model,
        "fixed": fixed,
        "summary": summary,
        "error": error,
        "steps": steps,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "undone": False,
        "issue_ref": issue_ref,  # the GitHub issue this run fixed, if any
        "pr_url": None,
        "confidence": confidence,
        "events": events or [],  # every step, for replay and sharing
        **extra,  # e.g. poltergeist rounds, the crash trace
        "files": [
            {"path": rel, "before": originals.get(rel), "after": _read(repo / rel)}
            for rel in sorted(changed_files)
        ],
    }
    (runs / f"{run_id}.json").write_text(json.dumps(data, indent=1), encoding="utf-8")
    return run_id


def list_runs(repo: Path) -> list[dict[str, Any]]:
    """All recorded runs, newest first."""
    runs = repo / RUNS_DIR
    if not runs.is_dir():
        return []
    out = []
    for file in runs.glob("*.json"):
        try:
            out.append(json.loads(file.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return sorted(out, key=lambda run: run.get("timestamp", 0), reverse=True)


def summarize(run: dict[str, Any]) -> dict[str, Any]:
    """A run without the file contents, for listings."""
    return {
        **{k: v for k, v in run.items() if k not in ("files", "events")},
        "files": [f["path"] for f in run.get("files", [])],
    }


def load_run(repo: Path, run_id: str | None = None) -> dict[str, Any]:
    runs = list_runs(repo)
    if not runs:
        raise UndoError("No runs recorded in this repository yet.")
    if run_id is None:
        for run in runs:
            if run["files"] and not run["undone"]:
                return run
        raise UndoError("There is no run with changes left to undo.")
    for run in runs:
        if run["id"] == run_id:
            return run
    raise UndoError(f"No run with id '{run_id}'. See `ghostpatch history`.")


def update_run(repo: Path, run_id: str, **fields: Any) -> dict[str, Any]:
    """Change fields of a recorded run (e.g. store the pull request it became)."""
    run = load_run(repo, run_id)
    run.update(fields)
    (repo / RUNS_DIR / f"{run['id']}.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    return run


def adopt_files(repo: Path, run_id: str, originals: dict[str, str | None]) -> dict[str, Any]:
    """Add files changed outside a run to it (with their content before), so undo and pull
    requests include them. Used to ship haunt mode's failing tests together with the fix."""
    run = load_run(repo, run_id)
    have = {f["path"] for f in run["files"]}
    run["files"] += [{"path": rel, "before": before, "after": _read(repo / rel)}
                     for rel, before in sorted(originals.items()) if rel not in have]
    (repo / RUNS_DIR / f"{run['id']}.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    return run


def undo_run(repo: Path, run_id: str | None = None, force: bool = False) -> dict[str, Any]:
    """Restore the files a run changed. Returns the run, marked as undone."""
    run = load_run(repo, run_id)
    if run["undone"]:
        raise UndoError(f"Run {run['id']} was already undone.")
    if not run["files"]:
        raise UndoError(f"Run {run['id']} didn't change any files.")

    changed_since = [f["path"] for f in run["files"] if _read(repo / f["path"]) != f["after"]]
    if changed_since and not force:
        raise UndoError(
            "These files were changed after the run, so undoing would lose those edits: "
            + ", ".join(changed_since) + ". Use --force to undo anyway."
        )

    for f in run["files"]:
        path = repo / f["path"]
        if f["before"] is None:  # the run created this file
            if path.is_file():
                path.unlink()
        else:
            _write(path, f["before"])

    run["undone"] = True
    (repo / RUNS_DIR / f"{run['id']}.json").write_text(json.dumps(run, indent=1), encoding="utf-8")
    return run
