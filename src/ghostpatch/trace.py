"""Crash-to-graph tracing: turn a stack trace into a path through the code graph.

Paste a Python traceback or a Node.js / TypeScript stack trace. Each frame that points into
the repository is mapped to the function it's in, which gives the crash path, from the entry
point to the line that failed. The agent gets that path as a head start, and the dashboard
can animate it on the graph.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

PY_FRAME = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+)(?:, in (?P<func>\S+))?', re.MULTILINE)
PYTEST_FRAME = re.compile(r"^(?P<path>[^\s:][^:\n]*\.py):(?P<line>\d+): (?:in (?P<func>\S+)|\w*Error)", re.MULTILINE)
NODE_FRAME = re.compile(
    r"^\s*at (?:(?P<func>[^\s(]+) )?\(?(?P<path>(?:file:///)?(?:[A-Za-z]:)?[^():\s]+):(?P<line>\d+):\d+\)?\s*$",
    re.MULTILINE,
)
PY_ERROR = re.compile(r"^(?:[\w.]+(?:Error|Exception|Exit|Interrupt|Warning)|AssertionError)\b.*$", re.MULTILINE)
NODE_ERROR = re.compile(r"^\s*(?:Uncaught )?(?:\w*Error|AssertionError)(?: \[\w+\])?:.*$", re.MULTILINE)


@dataclass
class Frame:
    path: str  # as written in the trace
    line: int
    function: str | None
    rel_path: str | None = None  # relative to the repository, if the file is in it
    qualname: str | None = None  # the graph symbol containing this line

    @property
    def in_repo(self) -> bool:
        return self.rel_path is not None


@dataclass
class Trace:
    language: str
    error: str
    frames: list[Frame]  # entry point first, crash site last

    @property
    def repo_frames(self) -> list[Frame]:
        return [f for f in self.frames if f.in_repo]

    def path_qualnames(self) -> list[str]:
        out: list[str] = []
        for frame in self.repo_frames:
            if frame.qualname and (not out or out[-1] != frame.qualname):
                out.append(frame.qualname)
        return out

    def as_dict(self) -> dict[str, Any]:
        return {"language": self.language, "error": self.error,
                "frames": [dict(asdict(f), in_repo=f.in_repo) for f in self.frames],
                "path": self.path_qualnames()}


def parse(text: str) -> Trace | None:
    """Find a stack trace in `text`. Returns None if there isn't one."""
    py = [Frame(m["path"], int(m["line"]), m["func"]) for m in PY_FRAME.finditer(text)]
    if not py:
        py = [Frame(m["path"], int(m["line"]), m["func"]) for m in PYTEST_FRAME.finditer(text)]
    node = [Frame(m["path"], int(m["line"]), m["func"]) for m in NODE_FRAME.finditer(text)]
    if not py and not node:
        return None
    if len(py) >= len(node):
        errors = PY_ERROR.findall(text)
        return Trace("python", errors[-1].strip() if errors else "", py)  # tracebacks list the entry first
    errors = NODE_ERROR.findall(text)
    return Trace("javascript", errors[0].strip() if errors else "", list(reversed(node)))  # node lists the crash first


def _relative(repo: Path, raw: str, known: set[str]) -> str | None:
    path = unquote(raw.removeprefix("file:///").removeprefix("file://"))
    if path.startswith(("node:", "internal/")) or "site-packages" in path or "node_modules" in path:
        return None
    candidate = Path(path)
    try:
        rel = candidate.resolve().relative_to(repo.resolve()).as_posix() if candidate.is_absolute() else None
    except (ValueError, OSError):
        rel = None
    if rel and rel in known:
        return rel
    # Traces from other machines or containers: match on the longest known path suffix.
    posix = PurePosixPath(path.replace("\\", "/")).as_posix().lstrip("./")
    matches = [k for k in known if posix == k or posix.endswith("/" + k)]
    return max(matches, key=len) if matches else None


def locate(trace: Trace, repo: Path, graph: Any) -> Trace:
    """Fill in each frame's repository path and graph symbol."""
    graph.refresh()
    known = {row[0] for row in graph.db.execute("SELECT path FROM files")}
    for frame in trace.frames:
        frame.rel_path = _relative(repo, frame.path, known)
        if frame.rel_path:
            symbol = graph.symbol_at(frame.rel_path, frame.line)
            frame.qualname = symbol[1] if symbol else None
    return trace


def describe(trace: Trace) -> str:
    """The crash path in words, for the agent."""
    lines = [f"Crash-to-graph trace ({trace.language}):"]
    if trace.error:
        lines.append(f"  error: {trace.error}")
    frames = trace.repo_frames
    if not frames:
        lines.append("  (no frames point into this repository)")
    for i, frame in enumerate(frames):
        marker = "💥" if i == len(frames) - 1 else "→ "
        where = frame.qualname or frame.function or "<module level>"
        lines.append(f"  {marker} {frame.rel_path}:{frame.line}  in {where}")
    lines.append("Start from the last frame (where it failed) and work back along this path.")
    return "\n".join(lines)


def enrich_issue(issue: str, repo: Path, graph: Any) -> tuple[str, Trace | None]:
    """If the issue contains a stack trace, add the crash path to it."""
    trace = parse(issue)
    if trace is None or graph is None:
        return issue, trace
    locate(trace, repo, graph)
    if not trace.repo_frames:
        return issue, trace
    return f"{issue}\n\n{describe(trace)}", trace
