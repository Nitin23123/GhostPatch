"""Architecture time-lapse: the code graph at each of the last N commits.

Each commit's files are read straight from git (`git archive`), so the working folder is never
touched. Symbols are identified by qualified name, which stays stable across commits, so a UI
can animate what appeared, disappeared and grew between frames.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
import tempfile
from pathlib import Path

from ghostpatch.gitutil import git, is_git_repo

MAX_COMMITS = 60


class TimelapseError(Exception):
    pass


def extract_commit(root: Path, sha: str, subdir: str, dest: Path) -> Path:
    """Write the files of `subdir` at commit `sha` into `dest`. Returns the folder to analyse."""
    proc = subprocess.run(["git", "archive", "--format=tar", sha, subdir or "."], cwd=root, capture_output=True)
    if proc.returncode != 0:
        raise TimelapseError(proc.stderr.decode("utf-8", "replace").strip())
    with tarfile.open(fileobj=io.BytesIO(proc.stdout)) as tar:
        try:
            tar.extractall(dest, filter="data")  # Python 3.12+: refuses unsafe paths
        except TypeError:
            tar.extractall(dest)  # older Pythons; the archive comes from the user's own repository
    return dest / subdir if subdir else dest


def frame_for(folder: Path, max_nodes: int) -> dict:
    from ghostpatch.graph import CodeGraph

    graph = CodeGraph(folder, db_path=":memory:")
    try:
        graph.refresh()
        data = graph.export(max_nodes=max_nodes)
        stats = graph.stats()
    finally:
        graph.close()
    by_id = {n["id"]: n["qualname"] for n in data["nodes"]}
    return {
        "stats": stats,
        "nodes": [{k: n[k] for k in ("qualname", "name", "kind", "path", "test", "tested")} for n in data["nodes"]],
        "edges": [{"source": by_id[e["source"]], "target": by_id[e["target"]]} for e in data["edges"]],
    }


def timelapse(repo: Path, commits: int = 20, max_nodes: int = 300) -> dict:
    if not is_git_repo(repo):
        raise TimelapseError("The time-lapse needs a git repository.")
    root = Path(git(repo, "rev-parse", "--show-toplevel"))
    subdir = repo.resolve().relative_to(root.resolve()).as_posix()
    subdir = "" if subdir == "." else subdir
    log = git(root, "log", f"-n{max(1, min(commits, MAX_COMMITS))}", "--format=%H%x1f%h%x1f%ad%x1f%s",
              "--date=short", "--", subdir or ".")
    entries = [line.split("\x1f") for line in log.splitlines() if line]
    frames, previous = [], set()
    for sha, short, date, subject in reversed(entries):  # oldest first
        with tempfile.TemporaryDirectory(prefix="ghostpatch-timelapse-", ignore_cleanup_errors=True) as tmp:
            frame = frame_for(extract_commit(root, sha, subdir, Path(tmp)), max_nodes)
        current = {n["qualname"] for n in frame["nodes"]}
        frame.update(commit=short, date=date, message=subject,
                     added=sorted(current - previous), removed=sorted(previous - current))
        frames.append(frame)
        previous = current
    return {"repo": repo.name, "frames": frames}
