"""A living code graph: symbols, calls and imports, kept in sync with the files on disk.

This is what lets GhostPatch ask "who calls this?", "which tests cover this?" and
"what breaks if I change this?" instead of grepping blindly. The graph is stored in
SQLite under `.ghostpatch/` in the repository, and only changed files are re-parsed.

Python, JavaScript and TypeScript are supported (see `parsers.py`).

Calls are linked to the function they really mean wherever the code says so:
- a function defined in the same module,
- a name imported from another module (`from shop.pricing import apply_discount`),
- a module alias (`import shop.pricing as p; p.apply_discount()`),
- `self.method()` / `this.method()`: the method of the enclosing class.
Only calls on other objects (`cart.total()`) fall back to matching by name, and those links are
marked as name matches ("exact" = 0), so answers can say how sure they are.
"""

from __future__ import annotations

import os
import posixpath
import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from ghostpatch.parsers import ParseError, extract, is_test_path, language_of, module_name  # noqa: F401

IGNORED_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", "env",
    "dist", "build", "out", "coverage", ".next", ".nuxt", ".turbo", ".cache",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".idea", ".vscode", ".ghostpatch",
}
MAX_SOURCE_BYTES = 500_000  # bigger files are almost always generated or bundled
SCHEMA_VERSION = 3
SCHEMA = """
CREATE TABLE files (path TEXT PRIMARY KEY, mtime_ns INTEGER, size INTEGER, error TEXT);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY, path TEXT, name TEXT, qualname TEXT, kind TEXT,
    line INTEGER, end_line INTEGER, signature TEXT, parent_id INTEGER
);
CREATE TABLE calls (path TEXT, caller_id INTEGER, callee TEXT, line INTEGER, receiver TEXT);
CREATE TABLE imports (path TEXT, module TEXT, name TEXT, line INTEGER, alias TEXT);
CREATE TABLE edges (caller_id INTEGER, callee_id INTEGER, path TEXT, line INTEGER, exact INTEGER);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_path ON symbols(path);
CREATE INDEX idx_calls_callee ON calls(callee);
CREATE INDEX idx_calls_caller ON calls(caller_id);
CREATE INDEX idx_edges_callee ON edges(callee_id);
CREATE INDEX idx_edges_caller ON edges(caller_id);
"""
TABLES = ("files", "symbols", "calls", "imports", "edges")
MAX_RESULTS = 60
IMPACT_DEPTH = 3
COVERAGE_DEPTH = 4  # how many calls deep a test may be from a function and still count as covering it
JS_EXTENSIONS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
SELF_RECEIVERS = {"self", "cls", "this"}
CALLABLE_KINDS = ("function", "method", "class")


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
            for table in TABLES:
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
        if stats.indexed or stats.removed:
            self._link()  # imports can point anywhere, so links are rebuilt whenever a file changes
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
            "INSERT INTO calls VALUES (?, ?, ?, ?, ?)",
            [(rel, ids[i] if i is not None else None, callee, line, receiver)
             for i, callee, line, receiver in facts.calls],
        )
        self.db.executemany("INSERT INTO imports VALUES (?, ?, ?, ?, ?)", [(rel, *imp) for imp in facts.imports])
        return None

    # ------------------------------------------------------------------- linking

    def _link(self) -> None:
        """Resolve every call to the symbols it can refer to, and store them as edges."""
        rows = self.db.execute("SELECT id, path, name, kind, parent_id FROM symbols").fetchall()
        kind_of = {i: k for i, _, _, k, _ in rows}
        parent_of = {i: p for i, _, _, _, p in rows}
        top: dict[tuple[str, str], list[int]] = defaultdict(list)  # (path, name) -> module-level symbols
        by_name: dict[str, list[int]] = defaultdict(list)
        methods: dict[int, dict[str, int]] = defaultdict(dict)  # class id -> {method name: id}
        for i, path, name, kind, parent in rows:
            by_name[name].append(i)
            if parent is None:
                top[(path, name)].append(i)
            elif kind_of.get(parent) == "class":
                methods[parent][name] = i
        files = {r[0] for r in self.db.execute("SELECT path FROM files")}
        modules = {module_name(p): p for p in files if p.endswith(".py")}
        imports: dict[str, dict[str, tuple[str, str]]] = defaultdict(dict)
        for path, module, name, alias in self.db.execute("SELECT path, module, name, alias FROM imports"):
            if alias:
                imports[path][alias] = (module, name)

        def module_file(path: str, module: str) -> str | None:
            if path.endswith(".py"):
                return _python_module_file(path, module, modules)
            return _js_module_file(path, module, files)

        def enclosing_class(symbol_id: int | None) -> int | None:
            while symbol_id is not None:
                if kind_of.get(symbol_id) == "class":
                    return symbol_id
                symbol_id = parent_of.get(symbol_id)
            return None

        def by_name_of(name: str, kinds: tuple[str, ...]) -> list[int]:
            return [i for i in by_name.get(name, []) if kind_of[i] in kinds]

        def targets(path: str, caller: int | None, callee: str, receiver: str | None) -> tuple[list[int], int]:
            if receiver is None:
                if top.get((path, callee)):
                    return top[(path, callee)], 1
                imported = imports[path].get(callee)
                if imported:
                    module, name = imported
                    target = module_file(path, module)
                    wanted = callee if name in ("*", "default") else name
                    if target and top.get((target, wanted)):
                        return top[(target, wanted)], 1
                return by_name_of(callee, CALLABLE_KINDS), 0
            if receiver in SELF_RECEIVERS:
                cls = enclosing_class(caller)
                if cls is not None and callee in methods.get(cls, {}):
                    return [methods[cls][callee]], 1
                return by_name_of(callee, ("method",)), 0
            imported = imports[path].get(receiver)
            if imported:  # a module alias: `p.apply_discount()` / `util.slugify()`
                module, name = imported
                target = module_file(path, module if name == "*" else _join_module(module, name))
                if target and top.get((target, callee)):
                    return top[(target, callee)], 1
            return by_name_of(callee, ("method",)) or by_name_of(callee, CALLABLE_KINDS), 0

        edges = []
        for path, caller, callee, line, receiver in self.db.execute(
                "SELECT path, caller_id, callee, line, receiver FROM calls"):
            found, exact = targets(path, caller, callee, receiver)
            edges += [(caller, t, path, line, exact) for t in found if t != caller]
        self.db.execute("DELETE FROM edges")
        self.db.executemany("INSERT INTO edges VALUES (?, ?, ?, ?, ?)", edges)

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

    def _walk_callers(self, name: str, depth: int) -> dict[int, list[tuple[str, int, str, str]]]:
        """Breadth-first walk up the call graph. Returns {depth: [(path, line, qualname, name)]}."""
        levels: dict[int, list[tuple[str, int, str, str]]] = {}
        frontier = [row[0] for row in self._match(name)]
        seen_ids, seen_sites = set(frontier), set()
        for level in range(1, depth + 1):
            next_ids = []
            for path, line, caller_id, caller_name, qual, _ in self._callers_of(frontier):
                if (path, line) in seen_sites:
                    continue
                seen_sites.add((path, line))
                levels.setdefault(level, []).append((path, line, qual or "<module level>", caller_name or ""))
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
        for callee_id, path in self.db.execute("SELECT callee_id, path FROM edges"):
            if not is_test_path(path):
                counts[callee_id] += 1
        return counts

    # --------------------------------------------------------------- test coverage

    def reached_ids(self, depth: int = COVERAGE_DEPTH) -> set[int]:
        """Symbols outside test files that some test calls, directly or up to `depth` calls deep."""
        test_ids = {i for i, p in self.db.execute("SELECT id, path FROM symbols") if is_test_path(p)}
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
        return reached

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
            if is_test_path(path) or sym_id in reached or name.startswith("__"):
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
        radius: dict[str, dict] = {}
        for name in names:
            for sym_id, path, sym_name, qualname, *_ in self._match(name):
                if not is_test_path(path):
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
                if not is_test_path(path):
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
        nodes = [
            {"id": i, "name": n, "qualname": q, "kind": k, "path": p, "line": ln, "test": is_test_path(p),
             "tested": is_test_path(p) or i in reached or k not in ("function", "method") or n.startswith("__")}
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


# ------------------------------------------------------------------ module resolution


def _join_module(module: str, name: str) -> str:
    return f"{module}.{name}" if module and not module.endswith(".") else f"{module}{name}"


def _python_module_file(path: str, module: str, modules: dict[str, str]) -> str | None:
    """The file a Python import refers to, from the importing file's point of view."""
    level = len(module) - len(module.lstrip("."))
    rest = module[level:]
    if level:
        package = module_name(path).split(".")
        if not path.endswith("__init__.py"):
            package = package[:-1]
        if level > 1:
            package = package[: -(level - 1)] if level - 1 <= len(package) else []
        full = ".".join([*package, rest] if rest else package)
    else:
        full = rest
    if full in modules:
        return modules[full]
    suffix = [m for m in modules if m.endswith("." + full)]  # e.g. a src/ layout: src.shop.cart for shop.cart
    return modules[suffix[0]] if len(suffix) == 1 else None


def _js_module_file(path: str, spec: str, files: set[str]) -> str | None:
    """The file a relative JS/TS import refers to. Packages (no leading dot) aren't in the graph."""
    if not spec.startswith("."):
        return None
    base = posixpath.normpath(posixpath.join(posixpath.dirname(path), spec))
    stem, ext = posixpath.splitext(base)
    candidates = [base, *(base + e for e in JS_EXTENSIONS), *(f"{base}/index{e}" for e in JS_EXTENSIONS)]
    if ext in (".js", ".jsx", ".mjs", ".cjs"):  # TypeScript projects import "./x.js" for x.ts
        candidates += [stem + e for e in (".ts", ".tsx", ".mts", ".cts")]
    return next((c for c in candidates if c in files), None)
