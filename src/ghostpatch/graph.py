"""A living code graph: symbols, calls and imports, kept in sync with the files on disk.

This is what lets GhostPatch ask "who calls this?", "which tests cover this?" and
"what breaks if I change this?" instead of grepping blindly. The graph is stored in
SQLite under `.ghostpatch/` in the repository, and only changed files are re-parsed.

Python, JavaScript, TypeScript, Go, Rust and Java are supported (see `parsers.py`).

Calls are linked to the function they really mean wherever the code says so:
- a function defined in the same module (for Go, the same package: the same folder),
- a name imported from another module (`from shop.pricing import apply_discount`,
  `use crate::pricing::apply`, `import static shop.Util.round`),
- a module or package alias (`import shop.pricing as p; p.apply_discount()`, `pricing.Apply()`),
- `self.method()` / `this.method()`: the method of the enclosing class (for Java also a bare `method()`),
- a value whose type the code states (`cart *Cart`, `Cart cart`, `let cart = Cart::new()`): that type's method.
Only calls on values of unknown type fall back to matching by name, and those links are marked
as name matches ("exact" = 0), so answers can say how sure they are.
"""

from __future__ import annotations

import os
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ghostpatch.parsers import TEST_KINDS, ParseError, extract, is_test_path, language_of, module_name  # noqa: F401

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "coverage", ".next", ".nuxt", ".turbo", ".cache",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode", ".ghostpatch",
    "target", ".gradle", "vendor",  # Rust / Maven / Gradle build output, vendored Go modules
}
FULL_RELINK_FILES = 50  # beyond this many changed files, relinking everything is simpler
MANIFESTS = ("go.mod", "Cargo.toml")  # say how Go import paths and Rust crate names map to folders
MAX_SOURCE_BYTES = 500_000  # bigger files are almost always generated or bundled
SCHEMA_VERSION = 5
SCHEMA = """
CREATE TABLE files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, error TEXT);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY, path TEXT, name TEXT, qualname TEXT, kind TEXT,
    line INTEGER, end_line INTEGER, signature TEXT, parent_id INTEGER
);
CREATE TABLE calls (path TEXT, caller_id INTEGER, callee TEXT, line INTEGER, receiver TEXT);
CREATE TABLE imports (path TEXT, module TEXT, name TEXT, line INTEGER, alias TEXT);
CREATE TABLE edges (caller_id INTEGER, callee_id INTEGER, path TEXT, line INTEGER, exact INTEGER, callee TEXT);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_path ON symbols(path);
CREATE INDEX idx_calls_callee ON calls(callee);
CREATE INDEX idx_calls_caller ON calls(caller_id);
CREATE INDEX idx_calls_path ON calls(path);
CREATE INDEX idx_imports_path ON imports(path);
CREATE INDEX idx_edges_callee ON edges(callee_id);
CREATE INDEX idx_edges_caller ON edges(caller_id);
CREATE INDEX idx_edges_path ON edges(path);
CREATE INDEX idx_edges_name ON edges(callee);
"""
TABLES = ("files", "symbols", "calls", "imports", "edges")
MAX_RESULTS = 60
IMPACT_DEPTH = 3
COVERAGE_DEPTH = 4  # how many calls deep a test may be from a function and still count as covering it


# ---------------------------------------------------------------------------- graph


def _is_junction(entry: os.DirEntry) -> bool:
    """A Windows junction: a folder that leads somewhere else, which os.scandir doesn't call a link."""
    is_junction = getattr(entry, "is_junction", None)  # Python 3.12+
    if is_junction is not None:
        return is_junction()
    if os.name != "nt" or not entry.is_dir():
        return False
    return os.path.normcase(os.path.realpath(entry.path)) != os.path.normcase(os.path.abspath(entry.path))


def _is_link(path: Path) -> bool:
    """A symlink, or on Windows a junction: either can point outside the repository."""
    try:
        return path.is_symlink() or bool(getattr(path, "is_junction", lambda: False)())
    except OSError:
        return True


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
        self.db = sqlite3.connect(str(db_path), timeout=30)  # the dashboard reads from several threads
        if str(db_path) != ":memory:":
            self.db.execute("PRAGMA journal_mode = WAL")  # readers don't block the writer, or each other
            self.db.execute("PRAGMA synchronous = NORMAL")
        if self.db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            for table in TABLES:
                self.db.execute(f"DROP TABLE IF EXISTS {table}")
            self.db.executescript(SCHEMA)
            self.db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------------ indexing

    def _source_files(self) -> dict[str, os.stat_result]:
        """Every source file under the root, with its size and mtime. This runs before every graph
        query, so it uses os.scandir, whose entries carry their stat (for free on Windows)."""
        found: dict[str, os.stat_result] = {}
        self.manifests: list[str] = []
        pending = [(str(self.root), "")]
        while pending:
            folder, prefix = pending.pop()
            try:
                entries = list(os.scandir(folder))
            except OSError:
                continue
            for entry in entries:
                try:
                    if entry.is_symlink() or _is_junction(entry):
                        continue  # it could point outside the repository
                    if entry.is_dir():
                        if entry.name not in IGNORED_DIRS:
                            pending.append((entry.path, f"{prefix}{entry.name}/"))
                        continue
                    rel = prefix + entry.name
                    if entry.name in MANIFESTS:
                        self.manifests.append(rel)
                    if language_of(entry.name) is None:
                        continue
                    st = entry.stat()
                except OSError:
                    continue
                if st.st_size <= MAX_SOURCE_BYTES:
                    found[rel] = st
        return found

    def refresh(self) -> RefreshStats:
        """Bring the graph up to date with the files on disk, re-parsing only what changed."""
        stats = RefreshStats()
        on_disk = self._source_files()
        known = {row[0]: (row[1], row[2]) for row in self.db.execute("SELECT path, mtime_ns, size FROM files")}

        changed: set[str] = set()
        names: set[str] = set()  # names defined in changed files, before and after the change
        for rel in known.keys() - on_disk.keys():
            names |= self._names_in(rel)
            self._forget(rel)
            changed.add(rel)
            stats.removed += 1
        for rel, st in on_disk.items():
            if known.get(rel) == (st.st_mtime_ns, st.st_size):
                stats.unchanged += 1
                continue
            if rel in known:  # a file seen before: drop what it said last time
                names |= self._names_in(rel)
                self._forget(rel)
            error = self._index_file(rel)
            self.db.execute("INSERT INTO files VALUES (?, ?, ?, ?)", (rel, st.st_mtime_ns, st.st_size, error))
            changed.add(rel)
            stats.indexed += 1
            stats.errors += error is not None
        if changed:
            full = not known or len(changed) > max(FULL_RELINK_FILES, len(on_disk) // 4)
            self._link(None if full else changed, names)
        self.db.commit()
        return stats

    def _names_in(self, rel: str) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT name FROM symbols WHERE path = ?", (rel,))}

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
            "INSERT INTO calls VALUES (?, ?, ?, ?, ?)",
            [(rel, ids[i] if i is not None else None, callee, line, receiver)
             for i, callee, line, receiver in facts.calls],
        )
        self.db.executemany("INSERT INTO imports VALUES (?, ?, ?, ?, ?)", [(rel, *imp) for imp in facts.imports])
        return None

    # ------------------------------------------------------------------- linking

    def _link(self, changed: set[str] | None = None, old_names: set[str] | None = None) -> None:
        """Resolve calls to the symbols they can refer to, and store them as edges.

        With `changed` files, only the calls a change can affect are resolved again: those made in
        the changed files, and those anywhere to a name the changed files define (before or after
        the change, including import aliases of those names). Otherwise every call is.
        """
        from ghostpatch.linker import Linker

        manifests = {}
        for rel in getattr(self, "manifests", []):
            try:
                manifests[rel] = (self.root / rel).read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
        linker = Linker(
            self.db.execute("SELECT id, path, name, qualname, kind, parent_id, signature FROM symbols").fetchall(),
            {r[0] for r in self.db.execute("SELECT path FROM files")},
            self.db.execute("SELECT path, module, name, alias FROM imports").fetchall(),
            manifests,
        )
        if changed is None:
            self.db.execute("DELETE FROM edges")
            calls = self.db.execute("SELECT path, caller_id, callee, line, receiver FROM calls").fetchall()
        else:
            self.db.execute("CREATE TEMP TABLE IF NOT EXISTS relink_paths (path TEXT PRIMARY KEY)")
            self.db.execute("CREATE TEMP TABLE IF NOT EXISTS relink_names (name TEXT PRIMARY KEY)")
            self.db.execute("DELETE FROM relink_paths")
            self.db.execute("DELETE FROM relink_names")
            self.db.executemany("INSERT INTO relink_paths VALUES (?)", [(p,) for p in changed])
            names = set(old_names or ()) | {r[0] for r in self.db.execute(
                "SELECT name FROM symbols WHERE path IN (SELECT path FROM relink_paths)")}
            names |= {r[0] for r in self.db.execute(  # `from x import f as g`: calls to g mean f
                f"SELECT alias FROM imports WHERE alias IS NOT NULL AND name IN ({','.join('?' * len(names))})",
                list(names))} if names and len(names) < 900 else set()
            self.db.executemany("INSERT OR IGNORE INTO relink_names VALUES (?)", [(n,) for n in names])
            where = "path IN (SELECT path FROM relink_paths) OR callee IN (SELECT name FROM relink_names)"
            self.db.execute(f"DELETE FROM edges WHERE {where}")
            self.db.execute("DELETE FROM edges WHERE callee_id NOT IN (SELECT id FROM symbols)")  # safety net
            calls = self.db.execute(f"SELECT path, caller_id, callee, line, receiver FROM calls WHERE {where}").fetchall()
        edges = []
        for path, caller, callee, line, receiver in calls:
            found, exact = linker.targets(path, caller, callee, receiver)
            edges += [(caller, t, path, line, exact, callee) for t in linker.one_per_method(found) if t != caller]
        self.db.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?, ?)", edges)

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
        """Symbols matching a plain name ('average') or a dotted suffix ('Cart.total', 'calc.average',
        'Cart::total')."""
        name = name.replace("::", ".").strip(".")
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

    def test_ids(self) -> set[int]:
        """Symbols that are test code: in a test file, or tests where they live (Rust's `#[cfg(test)]`)."""
        return {i for i, path, kind in self.db.execute("SELECT id, path, kind FROM symbols")
                if is_test_path(path) or kind in TEST_KINDS}

    def _callers_of(self, ids: list[int]) -> list[tuple]:
        """(path, line, caller_id, caller_name, caller_qualname, exact) of every call to these symbols."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        sql = f"""SELECT e.path, e.line, e.caller_id, s.name, s.qualname, MAX(e.exact) FROM edges e
                  LEFT JOIN symbols s ON s.id = e.caller_id WHERE e.callee_id IN ({marks})
                  GROUP BY e.path, e.line, e.caller_id ORDER BY e.path, e.line"""
        return self.db.execute(sql, ids).fetchall()

    def find_callers(self, name: str) -> str:
        ids = [row[0] for row in self._match(name)]
        if ids:
            rows = self._callers_of(ids)
            if not rows:
                return f"Nothing calls '{name}' (in the indexed source files)."
            lines = [f"{path}:{line}  in {qual or '<module level>'}" + ("" if exact else "  (matched by name)")
                     for path, line, _, _, qual, exact in rows[:MAX_RESULTS]]
            return f"Calls to '{name}':\n" + "\n".join(lines)
        # Not defined here (a library or builtin): list the call sites by name.
        rows = self.db.execute(
            "SELECT c.path, c.line, s.qualname FROM calls c LEFT JOIN symbols s ON s.id = c.caller_id "
            "WHERE c.callee = ? ORDER BY c.path, c.line", (name.rsplit(".", 1)[-1],)).fetchall()
        if not rows:
            return f"Nothing calls '{name}' (in the indexed source files)."
        return f"Calls to '{name}' (not defined in this repository):\n" + "\n".join(
            f"{path}:{line}  in {qual or '<module level>'}" for path, line, qual in rows[:MAX_RESULTS])

    def find_callees(self, name: str) -> str:
        symbols = self._match(name)
        if not symbols:
            return f"No symbol named '{name}'."
        out = []
        for sym_id, path, _, qual, *_ in symbols[:10]:
            resolved: dict[str, list[tuple[str, int]]] = defaultdict(list)
            for callee_name, callee_path, callee_line in self.db.execute(
                    "SELECT s.name, s.path, s.line FROM edges e JOIN symbols s ON s.id = e.callee_id "
                    "WHERE e.caller_id = ? ORDER BY s.path, s.line", (sym_id,)):
                resolved[callee_name].append((callee_path, callee_line))
            calls = self.db.execute(
                "SELECT callee, MIN(line) FROM calls WHERE caller_id = ? GROUP BY callee ORDER BY MIN(line)", (sym_id,)
            ).fetchall()
            out.append(f"{qual} ({path}) calls:")
            if not calls:
                out.append("  (nothing)")
            for callee, line in calls:
                where = resolved.get(callee)
                if where:
                    more = f" and {len(where) - 1} more" if len(where) > 1 else ""
                    target = f"defined at {where[0][0]}:{where[0][1]}{more}"
                else:
                    target = "external / builtin"
                out.append(f"  line {line}: {callee}  ({target})")
        return "\n".join(out)

    def _walk_callers(self, name: str, depth: int) -> dict[int, list[tuple[str, int, str, str, bool]]]:
        """Breadth-first walk up the call graph. Returns {depth: [(path, line, qualname, name, is_test)]}."""
        levels: dict[int, list[tuple[str, int, str, str, bool]]] = {}
        frontier = [row[0] for row in self._match(name)]
        seen_ids, seen_sites = set(frontier), set()
        tests = self.test_ids()
        for level in range(1, depth + 1):
            next_ids = []
            for path, line, caller_id, caller_name, qual, _ in self._callers_of(frontier):
                if (path, line) in seen_sites:
                    continue
                seen_sites.add((path, line))
                is_test = caller_id in tests if caller_id is not None else is_test_path(path)
                levels.setdefault(level, []).append((path, line, qual or "<module level>", caller_name or "", is_test))
                if caller_id is not None and caller_id not in seen_ids:
                    seen_ids.add(caller_id)
                    next_ids.append(caller_id)
            frontier = next_ids
            if not frontier:
                break
        return levels

    def related_tests(self, name: str) -> str:
        tests = self._tests(self._walk_callers(name, IMPACT_DEPTH))
        if not tests:
            return f"No tests found that call '{name}' (directly or up to {IMPACT_DEPTH} levels away)."
        return f"Tests that exercise '{name}':\n" + "\n".join(f"{p}  {q}" for p, q, _ in tests)

    @staticmethod
    def _tests(levels: dict[int, list[tuple[str, int, str, str, bool]]]) -> list[tuple[str, str, str]]:
        """The (path, qualname, name) of every test caller, skipping module-level code."""
        return sorted({(p, q, n) for sites in levels.values() for p, _, q, n, test in sites if n and test})

    def impact_of_change(self, name: str) -> str:
        if not self._match(name):
            return f"No symbol named '{name}'."
        levels = self._walk_callers(name, IMPACT_DEPTH)
        if not levels:
            return f"Nothing in the indexed code calls '{name}', so a change is unlikely to break other code."
        out = [f"Changing '{name}' may affect:"]
        labels = {1: "direct callers", 2: "callers of those", 3: "three levels up"}
        for level in sorted(levels):
            code = [s for s in levels[level] if not s[4]]
            if code:
                out.append(f"  {labels.get(level, f'level {level}')}:")
                out += [f"    {p}:{line}  in {q}" for p, line, q, _, _ in code[:MAX_RESULTS]]
        tests = self._tests(levels)
        out.append("  tests to run: " + (", ".join(f"{p}::{n}" for p, _, n in tests) or "none found"))
        return "\n".join(out)

    def edges_between(self, ids: list[int]) -> list[tuple[int, int]]:
        """The (caller_id, callee_id) links among these symbols."""
        if not ids:
            return []
        marks = ",".join("?" * len(ids))
        return self.db.execute(
            f"SELECT DISTINCT caller_id, callee_id FROM edges WHERE caller_id IN ({marks}) AND callee_id IN ({marks})",
            [*ids, *ids]).fetchall()

    def caller_counts(self) -> dict[int, int]:
        """How many call sites outside test files call each symbol."""
        counts: dict[int, int] = defaultdict(int)
        tests = self.test_ids()
        for caller_id, callee_id, path in self.db.execute("SELECT caller_id, callee_id, path FROM edges"):
            if not is_test_path(path) and caller_id not in tests:
                counts[callee_id] += 1
        return counts

    # --------------------------------------------------------------- test coverage

    def reached_ids(self, depth: int = COVERAGE_DEPTH) -> set[int]:
        """Symbols outside test files that some test calls, directly or up to `depth` calls deep."""
        test_ids = self.test_ids()
        out_edges: dict[int, set[int]] = defaultdict(set)
        frontier: set[int] = set()
        for caller, callee, path in self.db.execute("SELECT caller_id, callee_id, path FROM edges"):
            if caller is not None:
                out_edges[caller].add(callee)
            if caller in test_ids or (caller is None and is_test_path(path)):
                frontier.add(callee)
        reached: set[int] = set()
        for _ in range(depth):
            frontier = {i for i in frontier if i not in reached and i not in test_ids}
            if not frontier:
                break
            reached |= frontier
            frontier = {c for i in frontier for c in out_edges.get(i, ())}
        # A call links to one overload of a method; a test that reaches it reaches the method.
        methods = {(p, q) for i, p, q in self.db.execute("SELECT id, path, qualname FROM symbols") if i in reached}
        return reached | {i for i, p, q in self.db.execute("SELECT id, path, qualname FROM symbols") if (p, q) in methods}

    def names_reached_by_tests(self, depth: int = COVERAGE_DEPTH) -> set[str]:
        """Names of the functions some test reaches (kept for callers that work with names)."""
        reached = self.reached_ids(depth)
        return {name for i, name in self.db.execute("SELECT id, name FROM symbols") if i in reached}

    def untested(self, limit: int = 500) -> list[dict]:
        """Functions and methods outside test files that no test reaches through the call graph."""
        reached = self.reached_ids()
        rows = self.db.execute(
            "SELECT id, name, qualname, kind, path, line, signature FROM symbols "
            "WHERE kind IN ('function', 'method') ORDER BY path, line"
        ).fetchall()
        out = []
        for sym_id, name, qualname, kind, path, line, signature in rows:
            if is_test_path(path) or sym_id in reached or name.startswith("__") or name == "main":
                continue
            out.append({"name": name, "qualname": qualname, "kind": kind, "path": path, "line": line,
                        "signature": signature})
            if len(out) >= limit:
                break
        return out

    def blast_radius(self, names: list[str]) -> dict[str, dict]:
        """Every non-test function a change to `names` (names or qualnames) could affect:
        {qualname: {name, path, tested}}."""
        reached = self.reached_ids()
        tests = self.test_ids()
        radius: dict[str, dict] = {}
        for name in names:
            for sym_id, path, sym_name, qualname, *_ in self._match(name):
                if sym_id not in tests:
                    radius[qualname] = {"name": sym_name, "path": path, "tested": sym_id in reached}
        frontier = [row[0] for name in names for row in self._match(name)]
        seen = set(frontier)
        for _ in range(IMPACT_DEPTH):
            next_ids = []
            for path, _, caller_id, caller_name, qualname, _ in self._callers_of(frontier):
                if caller_id is None or caller_id in seen:
                    continue
                seen.add(caller_id)
                next_ids.append(caller_id)
                if caller_id not in tests:
                    radius[qualname] = {"name": caller_name, "path": path, "tested": caller_id in reached}
            frontier = next_ids
            if not frontier:
                break
        return radius

    def export(self, max_nodes: int = 400) -> dict:
        """The graph as plain data for visualisation: nodes are symbols, edges are calls."""
        rows = self.db.execute(
            "SELECT id, name, qualname, kind, path, line FROM symbols ORDER BY path, line LIMIT ?", (max_nodes,)
        ).fetchall()
        reached = self.reached_ids()
        tests = self.test_ids()
        nodes = [
            {"id": i, "name": n, "qualname": q, "kind": k, "path": p, "line": ln, "test": i in tests,
             "tested": i in tests or i in reached or k not in ("function", "method") or n.startswith("__")}
            for i, n, q, k, p, ln in rows
        ]
        ids = [node["id"] for node in nodes]
        edges = sorted(self.edges_between(ids)) if ids else []
        return {
            "nodes": nodes,
            "edges": [{"source": s, "target": t} for s, t in edges if s is not None and s != t],
            "truncated": self.stats()["symbols"] > len(nodes),
        }

    def symbol_at(self, rel_path: str, line: int) -> tuple[str, str] | None:
        """The innermost function or class containing a line, as (name, qualname)."""
        return self.db.execute(
            "SELECT name, qualname FROM symbols WHERE path = ? AND line <= ? AND end_line >= ? "
            "ORDER BY line DESC LIMIT 1",
            (rel_path, line, line),
        ).fetchone()

    def kind_at(self, rel_path: str, line: int) -> str | None:
        """The kind of the innermost symbol containing a line ('test' for a Rust #[test], ...)."""
        row = self.db.execute(
            "SELECT kind FROM symbols WHERE path = ? AND line <= ? AND end_line >= ? ORDER BY line DESC LIMIT 1",
            (rel_path, line, line)).fetchone()
        return row[0] if row else None

    def symbol_source(self, name: str, max_lines: int = 120) -> list[dict]:
        """The source of each symbol matching `name`: [{qualname, kind, path, line, end_line, text}]."""
        out = []
        for _, path, _, qualname, kind, line, end_line, _ in self._match(name)[:5]:
            try:
                lines = (self.root / path).read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            end = min(end_line, line + max_lines - 1)
            text = "\n".join(f"{n:>5} | {lines[n - 1]}" for n in range(line, end + 1) if n <= len(lines))
            if end < end_line:
                text += f"\n  ... ({end_line - end} more lines; use read_file with start_line={end + 1})"
            out.append({"qualname": qualname, "kind": kind, "path": path, "line": line, "end_line": end_line,
                        "text": text})
        return out

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
