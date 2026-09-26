"""A living code graph: symbols, calls and imports, kept in sync with the files on disk.

This is what lets GhostPatch ask "who calls this?", "which tests cover this?" and
"what breaks if I change this?" instead of grepping blindly. The graph is stored in
SQLite under `.ghostpatch/` in the repository, and only changed files are re-parsed.

Python, JavaScript and TypeScript are supported (see `parsers.py`). Calls are
resolved by name, so results can include unrelated functions that share a name.
"""

from __future__ import annotations

import os
import sqlite3
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from ghostpatch.parsers import ParseError, extract, is_test_path, language_of, module_name  # noqa: F401

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "coverage", ".next", ".nuxt", ".turbo", ".cache",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode", ".ghostpatch",
}
MAX_SOURCE_BYTES = 500_000  # bigger files are almost always generated or bundled
SCHEMA_VERSION = 2
SCHEMA = """
CREATE TABLE files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, error TEXT);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY, path TEXT, name TEXT, qualname TEXT, kind TEXT,
    line INTEGER, end_line INTEGER, signature TEXT, parent_id INTEGER
);
CREATE TABLE calls (path TEXT, caller_id INTEGER, callee TEXT, line INTEGER);
CREATE TABLE imports (path TEXT, module TEXT, name TEXT, line INTEGER);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_path ON symbols(path);
CREATE INDEX idx_calls_callee ON calls(callee);
CREATE INDEX idx_calls_caller ON calls(caller_id);
"""
MAX_RESULTS = 60
IMPACT_DEPTH = 3
COVERAGE_DEPTH = 4  # how many calls deep a test may be from a function and still count as covering it


# ---------------------------------------------------------------------------- graph


@dataclass
class RefreshStats:
    indexed: int = 0
    removed: int = 0
    unchanged: int = 0
    errors: int = 0


class CodeGraph:
    def __init__(self, root: str | Path, db_path: str | Path | None = None):
        self.root = Path(root).resolve()
        if db_path is None:
            cache = self.root / ".ghostpatch"
            cache.mkdir(exist_ok=True)
            ignore = cache / ".gitignore"
            if not ignore.exists():
                ignore.write_text("*\n", encoding="utf-8")  # keep the cache out of git automatically
            db_path = cache / "graph.db"
        self.db = sqlite3.connect(str(db_path))
        if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            for table in ("files", "symbols", "calls", "imports"):
                self.db.execute(f"DROP TABLE IF EXISTS {table}")
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ indexing

    def _source_files(self) -> dict[str, os.stat_result]:
        found = {}
        for dirpath, dirnames, filenames in os.walk(self.root):
            dirnames[:] = [d for d in dirnames if d not in IGNORED_DIRS]
            for name in filenames:
                path = Path(dirpath) / name
                rel = path.relative_to(self.root).as_posix()
                if language_of(rel) is None:
                    continue
                st = path.stat()
                if st.st_size <= MAX_SOURCE_BYTES:
                    found[rel] = st
        return found

    def refresh(self) -> RefreshStats:
        """Bring the graph up to date with the files on disk, re-parsing only what changed."""
        stats = RefreshStats()
        on_disk = self._source_files()
        known = {row[0]: (row[1], row[2]) for row in self.db.execute("SELECT path, mtime_ns, size FROM files")}

        for rel in known.keys() - on_disk.keys():
            self._forget(rel)
            stats.removed += 1
        for rel, st in on_disk.items():
            if known.get(rel) == (st.st_mtime_ns, st.st_size):
                stats.unchanged += 1
                continue
            self._forget(rel)
            error = self._index_file(rel)
            self.db.execute("INSERT INTO files VALUES (?, ?, ?, ?)", (rel, st.st_mtime_ns, st.st_size, error))
            stats.indexed += 1
            stats.errors += error is not None
        self.db.commit()
        return stats

    def _forget(self, rel: str) -> None:
        for table in ("files", "symbols", "calls", "imports"):
            self.db.execute(f"DELETE FROM {table} WHERE path = ?", (rel,))

    def _index_file(self, rel: str) -> str | None:
        try:
            source = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            facts = extract(source, rel)
        except ParseError as e:
            return str(e)

        ids: list[int] = []
        for sym in facts.symbols:
            parent_id = ids[sym.parent] if sym.parent is not None else None
            cur = self.db.execute(
                "INSERT INTO symbols (path, name, qualname, kind, line, end_line, signature, parent_id)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rel, sym.name, sym.qualname, sym.kind, sym.line, sym.end_line, sym.signature, parent_id),
            )
            ids.append(cur.lastrowid)
        self.db.executemany(
            "INSERT INTO calls VALUES (?, ?, ?, ?)",
            [(rel, ids[i] if i is not None else None, callee, line) for i, callee, line in facts.calls],
        )
        self.db.executemany("INSERT INTO imports VALUES (?, ?, ?, ?)", [(rel, *imp) for imp in facts.imports])
        return None

    # ------------------------------------------------------------------- queries

    def stats(self) -> dict[str, int]:
        count = lambda sql: self.db.execute(sql).fetchone()[0]  # noqa: E731
        return {
            "files": count("SELECT COUNT(*) FROM files"),
            "symbols": count("SELECT COUNT(*) FROM symbols"),
            "calls": count("SELECT COUNT(*) FROM calls"),
            "parse_errors": count("SELECT COUNT(*) FROM files WHERE error IS NOT NULL"),
        }

    def _match(self, name: str) -> list[tuple]:
        """Symbols matching a plain name ('average') or a dotted suffix ('Cart.total', 'calc.average')."""
        if "." in name:
            sql = "SELECT id, path, name, qualname, kind, line, end_line, signature FROM symbols " \
                  "WHERE qualname = ? OR qualname LIKE ? ORDER BY path, line"
            return self.db.execute(sql, (name, f"%.{name}")).fetchall()
        sql = "SELECT id, path, name, qualname, kind, line, end_line, signature FROM symbols " \
              "WHERE name = ? ORDER BY path, line"
        return self.db.execute(sql, (name,)).fetchall()

    def find_symbol(self, name: str) -> str:
        rows = self._match(name)
        if not rows:
            return f"No symbol named '{name}'. Try search_code for partial names or unsupported languages."
        return "\n".join(
            f"{path}:{line}-{end}  {kind} {qual}  |  {sig}" for _, path, _, qual, kind, line, end, sig in rows[:MAX_RESULTS]
        )

    def _callers(self, short_name: str) -> list[tuple]:
        sql = """SELECT c.path, c.line, s.id, s.name, s.qualname FROM calls c
                 LEFT JOIN symbols s ON s.id = c.caller_id WHERE c.callee = ? ORDER BY c.path, c.line"""
        return self.db.execute(sql, (short_name,)).fetchall()

    def find_callers(self, name: str) -> str:
        rows = self._callers(name.rsplit(".", 1)[-1])
        if not rows:
            return f"Nothing calls '{name}' (in the indexed source files)."
        lines = [f"{path}:{line}  in {qual or '<module level>'}" for path, line, _, _, qual in rows[:MAX_RESULTS]]
        return f"Calls to '{name}' (matched by name):\n" + "\n".join(lines)

    def find_callees(self, name: str) -> str:
        symbols = self._match(name)
        if not symbols:
            return f"No symbol named '{name}'."
        out = []
        for sym_id, path, _, qual, *_ in symbols[:10]:
            callees = self.db.execute(
                "SELECT callee, MIN(line) FROM calls WHERE caller_id = ? GROUP BY callee ORDER BY MIN(line)", (sym_id,)
            ).fetchall()
            out.append(f"{qual} ({path}) calls:")
            if not callees:
                out.append("  (nothing)")
            for callee, line in callees:
                where = self.db.execute(
                    "SELECT path, line FROM symbols WHERE name = ? ORDER BY path LIMIT 1", (callee,)
                ).fetchone()
                target = f"defined at {where[0]}:{where[1]}" if where else "external / builtin"
                out.append(f"  line {line}: {callee}  ({target})")
        return "\n".join(out)

    def _walk_callers(self, name: str, depth: int) -> dict[int, list[tuple[str, int, str, str]]]:
        """Breadth-first walk up the call graph. Returns {depth: [(path, line, qualname, name)]}."""
        levels: dict[int, list[tuple[str, int, str, str]]] = {}
        seen_names = {name.rsplit(".", 1)[-1]}
        seen_sites: set[tuple[str, int]] = set()
        queue = deque([(name.rsplit(".", 1)[-1], 1)])
        while queue:
            current, level = queue.popleft()
            if level > depth:
                continue
            for path, line, caller_id, caller_name, qual in self._callers(current):
                if (path, line) in seen_sites:
                    continue
                seen_sites.add((path, line))
                levels.setdefault(level, []).append((path, line, qual or "<module level>", caller_name or ""))
                if caller_id is not None and caller_name not in seen_names:
                    seen_names.add(caller_name)
                    queue.append((caller_name, level + 1))
        return levels

    def related_tests(self, name: str) -> str:
        tests = self._tests(self._walk_callers(name, IMPACT_DEPTH))
        if not tests:
            return f"No tests found that call '{name}' (directly or up to {IMPACT_DEPTH} levels away)."
        return f"Tests that exercise '{name}':\n" + "\n".join(f"{p}  {q}" for p, q, _ in tests)

    @staticmethod
    def _tests(levels: dict[int, list[tuple[str, int, str, str]]]) -> list[tuple[str, str, str]]:
        """The (path, qualname, name) of every test-file caller, skipping module-level code."""
        return sorted({(p, q, n) for sites in levels.values() for p, _, q, n in sites if n and is_test_path(p)})

    def impact_of_change(self, name: str) -> str:
        if not self._match(name):
            return f"No symbol named '{name}'."
        levels = self._walk_callers(name, IMPACT_DEPTH)
        if not levels:
            return f"Nothing in the indexed code calls '{name}', so a change is unlikely to break other code."
        out = [f"Changing '{name}' may affect:"]
        labels = {1: "direct callers", 2: "callers of those", 3: "three levels up"}
        for level in sorted(levels):
            code = [s for s in levels[level] if not is_test_path(s[0])]
            if code:
                out.append(f"  {labels.get(level, f'level {level}')}:")
                out += [f"    {p}:{line}  in {q}" for p, line, q, _ in code[:MAX_RESULTS]]
        tests = self._tests(levels)
        out.append("  tests to run: " + (", ".join(f"{p}::{n}" for p, _, n in tests) or "none found"))
        return "\n".join(out)

    # --------------------------------------------------------------- test coverage

    def names_reached_by_tests(self, depth: int = COVERAGE_DEPTH) -> set[str]:
        """Names of the functions that some test calls, directly or up to `depth` calls deep."""
        rows = self.db.execute("SELECT id, name, path FROM symbols").fetchall()
        test_ids = {i for i, _, p in rows if is_test_path(p)}
        ids_by_name: dict[str, list[int]] = {}
        for i, name, path in rows:
            if not is_test_path(path):
                ids_by_name.setdefault(name, []).append(i)
        calls_by_caller: dict[int, set[str]] = {}
        for caller_id, callee in self.db.execute("SELECT caller_id, callee FROM calls WHERE caller_id IS NOT NULL"):
            calls_by_caller.setdefault(caller_id, set()).add(callee)

        reached: set[str] = set()
        frontier = {c for i in test_ids for c in calls_by_caller.get(i, ())}
        for _ in range(depth):
            frontier = {n for n in frontier if n in ids_by_name} - reached
            if not frontier:
                break
            reached |= frontier
            frontier = {c for n in frontier for i in ids_by_name[n] for c in calls_by_caller.get(i, ())}
        return reached

    def untested(self, limit: int = 500) -> list[dict]:
        """Functions and methods outside test files that no test reaches through the call graph."""
        reached = self.names_reached_by_tests()
        rows = self.db.execute(
            "SELECT name, qualname, kind, path, line, signature FROM symbols "
            "WHERE kind IN ('function', 'method') ORDER BY path, line"
        ).fetchall()
        out = []
        for name, qualname, kind, path, line, signature in rows:
            if is_test_path(path) or name in reached or name.startswith("__"):
                continue
            out.append({"name": name, "qualname": qualname, "kind": kind, "path": path, "line": line,
                        "signature": signature})
            if len(out) >= limit:
                break
        return out

    def blast_radius(self, names: list[str]) -> dict[str, dict]:
        """Every non-test function a change to `names` could affect: {qualname: {name, path, tested}}."""
        reached = self.names_reached_by_tests()
        radius: dict[str, dict] = {}
        for name in names:
            short = name.rsplit(".", 1)[-1]
            for _, path, sym_name, qualname, *_ in self._match(name):
                if not is_test_path(path):
                    radius[qualname] = {"name": sym_name, "path": path, "tested": sym_name in reached}
            for sites in self._walk_callers(short, IMPACT_DEPTH).values():
                for path, _, qualname, caller_name in sites:
                    if caller_name and not is_test_path(path):
                        radius[qualname] = {"name": caller_name, "path": path, "tested": caller_name in reached}
        return radius

    def export(self, max_nodes: int = 400) -> dict:
        """The graph as plain data for visualisation: nodes are symbols, edges are calls."""
        rows = self.db.execute(
            "SELECT id, name, qualname, kind, path, line FROM symbols ORDER BY path, line LIMIT ?", (max_nodes,)
        ).fetchall()
        reached = self.names_reached_by_tests()
        nodes = [
            {"id": i, "name": n, "qualname": q, "kind": k, "path": p, "line": ln, "test": is_test_path(p),
             "tested": is_test_path(p) or n in reached or k not in ("function", "method") or n.startswith("__")}
            for i, n, q, k, p, ln in rows
        ]
        ids = {node["id"] for node in nodes}
        by_name: dict[str, list[int]] = {}
        for node in nodes:
            by_name.setdefault(node["name"], []).append(node["id"])
        edges = set()
        for caller_id, callee in self.db.execute("SELECT caller_id, callee FROM calls WHERE caller_id IS NOT NULL"):
            if caller_id in ids:
                edges.update((caller_id, target) for target in by_name.get(callee, []) if target != caller_id)
        return {
            "nodes": nodes,
            "edges": [{"source": s, "target": t} for s, t in sorted(edges)],
            "truncated": self.stats()["symbols"] > len(nodes),
        }

    def symbol_at(self, rel_path: str, line: int) -> tuple[str, str] | None:
        """The innermost function or class containing a line, as (name, qualname)."""
        return self.db.execute(
            "SELECT name, qualname FROM symbols WHERE path = ? AND line <= ? AND end_line >= ? "
            "ORDER BY line DESC LIMIT 1",
            (rel_path, line, line),
        ).fetchone()

    def repo_map(self, max_chars: int = 6000) -> str:
        """A compact outline of every file's classes, functions and methods."""
        rows = self.db.execute(
            "SELECT path, kind, signature, parent_id FROM symbols "
            "WHERE parent_id IS NULL OR parent_id IN (SELECT id FROM symbols WHERE kind IN ('class', 'suite')) "
            "ORDER BY path, line"
        ).fetchall()
        out: list[str] = []
        current = None
        for path, kind, signature, parent_id in rows:
            if path != current:
                out.append(f"{path}:")
                current = path
            indent = "    " if parent_id is not None else "  "
            out.append(f"{indent}{signature}")
        text = "\n".join(out)
        if len(text) > max_chars:
            text = text[:max_chars].rsplit("\n", 1)[0] + "\n  ... (map truncated; use find_symbol for more)"
        return text or "(no functions or classes found)"
