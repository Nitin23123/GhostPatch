"""Crash-to-graph tracing: turn a stack trace into a path through the code graph.

Paste a Python traceback, a Node.js / TypeScript stack trace, a Go panic, a Rust panic (with or
without RUST_BACKTRACE=1) or a Java exception. Each frame that points into the repository is
mapped to the function it's in, which gives the crash path, from the entry point to the line
that failed. The agent gets that path as a head start, and the dashboard can animate it on the graph.
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
# Go: "example.com/app/shop.Pick(...)" then, indented, "C:/src/app/shop/shop.go:4 +0x1d"
GO_FRAME = re.compile(r"^(?P<func>[^\s(][^\n]*?)\([^\n]*\)\s*\n\s+(?P<path>(?:[A-Za-z]:)?[^\s:]+\.go):(?P<line>\d+)",
                      re.MULTILINE)
GO_TEST_FRAME = re.compile(r"^\s+(?P<path>[\w./\\-]+_test\.go):(?P<line>\d+): ", re.MULTILINE)  # t.Errorf sites
GO_ERROR = re.compile(r"^(?:panic|fatal error): .*$", re.MULTILINE)
# Rust: "panicked at src/cart.rs:2:5:" and, with a backtrace, "3: shop::cart::total / at ./src/cart.rs:2:5"
RUST_PANIC = re.compile(r"panicked at .*?(?P<path>(?:[A-Za-z]:)?[^\s:',]+\.rs):(?P<line>\d+):\d+:?[ \t]*\n?(?P<msg>[^\n]*)")
RUST_FRAME = re.compile(r"^\s*\d+: (?P<func>\S+)\s*\n\s+at (?P<path>[^\n]+?\.rs):(?P<line>\d+):\d+", re.MULTILINE)
# Java: "at com.shop.Cart.total(Cart.java:14)"
JAVA_FRAME = re.compile(r"^\s+at (?:[\w.$-]+/)?(?P<func>[\w$.<>]+)\((?P<file>[\w$]+\.(?:java|kt)):(?P<line>\d+)\)",
                        re.MULTILINE)
JAVA_ERROR = re.compile(r"^(?:Exception in thread \"[^\"]*\" |Caused by: )?(?P<error>(?:[\w$]+\.)+[\w$]*(?:Exception|Error)\b.*)$",
                        re.MULTILINE)


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


def _java_path(func: str, file: str) -> str:
    """'com.shop.Cart.total' + 'Cart.java' -> 'com/shop/Cart.java' (matched against the repo's paths)."""
    package = func.split(".")[:-2]
    return "/".join([*package, file])


def parse(text: str) -> Trace | None:
    """Find a stack trace in `text`. Returns None if there isn't one."""
    candidates: list[Trace] = []
    py = [Frame(m["path"], int(m["line"]), m["func"]) for m in PY_FRAME.finditer(text)]
    if not py:
        py = [Frame(m["path"], int(m["line"]), m["func"]) for m in PYTEST_FRAME.finditer(text)]
    if py:
        errors = PY_ERROR.findall(text)
        candidates.append(Trace("python", errors[-1].strip() if errors else "", py))  # tracebacks list the entry first

    node = [Frame(m["path"], int(m["line"]), m["func"]) for m in NODE_FRAME.finditer(text)
            if not m["path"].endswith((".rs", ".go", ".java", ".py"))]
    if node:
        errors = NODE_ERROR.findall(text)
        candidates.append(Trace("javascript", errors[0].strip() if errors else "", list(reversed(node))))  # crash first

    go = [Frame(m["path"], int(m["line"]), m["func"]) for m in GO_FRAME.finditer(text)]
    go = go or [Frame(m["path"], int(m["line"]), None) for m in GO_TEST_FRAME.finditer(text)]
    if go:
        errors = GO_ERROR.findall(text)
        candidates.append(Trace("go", errors[0].strip() if errors else "", list(reversed(go))))  # crash first

    panic = RUST_PANIC.search(text)
    rust = [Frame(m["path"], int(m["line"]), m["func"]) for m in RUST_FRAME.finditer(text)]
    if panic or rust:
        frames = list(reversed(rust))  # backtraces list the crash first
        if panic and not any(f.line == int(panic["line"]) and f.path.replace("\\", "/").endswith(
                panic["path"].replace("\\", "/").lstrip("./")) for f in frames):
            frames.append(Frame(panic["path"], int(panic["line"]), None))
        message = panic["msg"].strip() if panic and panic["msg"].strip() else ""
        candidates.append(Trace("rust", f"panicked: {message}" if message else "panicked", frames))

    java_text = text.rsplit("\nCaused by: ", 1)  # the root cause is the one to follow
    java_frames = JAVA_FRAME.finditer(java_text[-1] if len(java_text) > 1 else text)
    java = [Frame(_java_path(m["func"], m["file"]), int(m["line"]), m["func"]) for m in java_frames]
    if java:
        errors = JAVA_ERROR.findall(text)
        candidates.append(Trace("java", errors[-1].strip() if errors else "", list(reversed(java))))  # crash first

    if not candidates:
        return None
    return max(candidates, key=lambda t: len(t.frames))  # the first listed wins a tie


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
    if matches:
        return max(matches, key=len)
    # Java names a file by its package ("com/shop/Cart.java"), inside src/main/java/: the other way round.
    inside = [k for k in known if k.endswith("/" + posix)]
    return inside[0] if len(inside) == 1 else None


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
