"""Tools the agent can use to explore and change a repository.

The model never touches the machine directly: it only *asks* for a tool call,
and this module decides what actually runs. Every path is confined to the
repository root, and shell commands need the user's approval.
"""

from __future__ import annotations

import difflib
import fnmatch
import inspect
import os
import re
import subprocess
from pathlib import Path
from typing import Callable, Iterator

from ghostpatch.graph import IGNORED_DIRS, CodeGraph
from ghostpatch.parsers import is_test_path, language_of

MAX_OUTPUT_CHARS = 12_000
MAX_FILE_BYTES = 1_000_000
MAX_SEARCH_MATCHES = 100
MAX_LISTED_FILES = 500
MAX_COMMAND_SECONDS = 600


class ToolError(Exception):
    """An invalid tool call. The message is sent back to the model so it can correct itself."""


def truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    if len(text) <= limit:
        return text
    half = limit // 2
    return f"{text[:half]}\n\n... [{len(text) - limit} characters truncated] ...\n\n{text[-half:]}"


class Workspace:
    """A repository the agent is allowed to work in."""

    def __init__(self, root: str | Path, approve_command: Callable[[str], bool], graph: CodeGraph | None = None):
        self.root = Path(root).resolve()
        self.approve_command = approve_command
        self.graph = graph
        self.changed_files: set[str] = set()
        self.originals: dict[str, str | None] = {}  # content before the first edit; None = new file

    # ------------------------------------------------------------------ helpers

    def resolve(self, rel_path: str) -> Path:
        path = (self.root / rel_path).resolve()
        if path != self.root and self.root not in path.parents:
            raise ToolError(f"'{rel_path}' is outside the repository.")
        return path

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix() or "."

    def iter_files(self, start: Path, max_depth: int | None = None) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(start):
            depth = len(Path(dirpath).relative_to(start).parts)
            if max_depth is not None and depth + 1 >= max_depth:
                dirnames[:] = []
            else:
                dirnames[:] = sorted(d for d in dirnames if d not in IGNORED_DIRS)
            for name in sorted(filenames):
                yield Path(dirpath) / name

    def _read(self, file: Path) -> str:
        with open(file, encoding="utf-8", errors="replace", newline="") as f:
            return f.read()

    def _write(self, file: Path, text: str) -> None:
        rel = self.rel(file)
        if rel not in self.originals:
            self.originals[rel] = self._read(file) if file.exists() else None
        with open(file, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        self.changed_files.add(rel)

    def diffs(self) -> list[dict[str, str]]:
        """Unified diffs of every file changed in this session."""
        out = []
        for rel in sorted(self.changed_files):
            before = self.originals.get(rel) or ""
            file = self.root / rel
            after = self._read(file) if file.exists() else ""
            diff = difflib.unified_diff(
                before.splitlines(), after.splitlines(), lineterm="",
                fromfile="/dev/null" if self.originals.get(rel) is None else f"a/{rel}", tofile=f"b/{rel}",
            )
            out.append({"path": rel, "diff": "\n".join(diff), "new": self.originals.get(rel) is None})
        return out

    def _existing_file(self, path: str) -> Path:
        file = self.resolve(path)
        if not file.is_file():
            raise ToolError(f"'{path}' does not exist or is not a file.")
        if file.stat().st_size > MAX_FILE_BYTES:
            raise ToolError(f"'{path}' is too large to open (over {MAX_FILE_BYTES // 1000} KB).")
        return file

    # -------------------------------------------------------------------- tools

    def list_files(self, path: str = ".", max_depth: int = 3) -> str:
        base = self.resolve(path)
        if not base.is_dir():
            raise ToolError(f"'{path}' is not a directory.")
        lines = []
        for file in self.iter_files(base, max_depth=max_depth):
            if len(lines) >= MAX_LISTED_FILES:
                lines.append("... (more files not shown; list a subdirectory instead)")
                break
            lines.append(self.rel(file))
        return "\n".join(lines) or "(no files)"

    def read_file(self, path: str, start_line: int = 1, end_line: int | None = None) -> str:
        file = self._existing_file(path)
        lines = self._read(file).splitlines()
        total = len(lines)
        start = max(start_line, 1)
        end = total if end_line is None else min(end_line, total)
        if total and start > total:
            raise ToolError(f"start_line {start} is past the end of the file ({total} lines).")
        numbered = [f"{i:>5} | {lines[i - 1]}" for i in range(start, end + 1)]
        header = f"{self.rel(file)} (lines {start}-{end} of {total})"
        return truncate(header + "\n" + "\n".join(numbered))

    def search_code(self, pattern: str, file_glob: str | None = None) -> str:
        try:
            regex = re.compile(pattern)
        except re.error as e:
            raise ToolError(f"Invalid regular expression: {e}") from e

        matches: list[str] = []
        for file in self.iter_files(self.root):
            rel = self.rel(file)
            if file_glob and not (fnmatch.fnmatch(rel, file_glob) or fnmatch.fnmatch(file.name, file_glob)):
                continue
            try:
                if file.stat().st_size > MAX_FILE_BYTES:
                    continue
                text = file.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue  # binary or unreadable
            for number, line in enumerate(text.splitlines(), 1):
                if regex.search(line):
                    matches.append(f"{rel}:{number}: {line.strip()[:200]}")
                    if len(matches) >= MAX_SEARCH_MATCHES:
                        matches.append("... (too many matches; use a narrower pattern or file_glob)")
                        return "\n".join(matches)
        return "\n".join(matches) or "No matches."

    def edit_file(self, path: str, old_text: str, new_text: str) -> str:
        file = self._existing_file(path)
        text = self._read(file)
        if "\r\n" in text:  # keep Windows line endings intact
            old_text = old_text.replace("\r\n", "\n").replace("\n", "\r\n")
            new_text = new_text.replace("\r\n", "\n").replace("\n", "\r\n")
        count = text.count(old_text)
        if count == 0:
            raise ToolError(
                "old_text was not found. Re-read the file and copy the text exactly, including indentation."
            )
        if count > 1:
            starts, pos = [], text.find(old_text)
            while pos != -1:
                starts.append(str(text[:pos].count("\n") + 1))
                pos = text.find(old_text, pos + 1)
            raise ToolError(
                f"old_text appears {count} times (starting on lines {', '.join(starts)}). Include more "
                "surrounding lines so it is unique, or use replace_lines with exact line numbers."
            )
        edit_line = text[: text.index(old_text)].count("\n") + 1
        self._write(file, text.replace(old_text, new_text, 1))
        return f"Edited {self.rel(file)}." + self._impact_note(self.rel(file), edit_line)

    def replace_lines(self, path: str, start_line: int, end_line: int, new_text: str) -> str:
        file = self._existing_file(path)
        text = self._read(file)
        lines = text.splitlines(keepends=True)
        try:
            start, end = int(start_line), int(end_line)
        except (TypeError, ValueError) as e:
            raise ToolError("start_line and end_line must be whole numbers.") from e
        if not 1 <= start <= end <= len(lines):
            raise ToolError(f"Lines {start}-{end} are out of range; the file has {len(lines)} lines.")
        newline = "\r\n" if "\r\n" in text else "\n"
        replacement = new_text.replace("\r\n", "\n").replace("\n", newline)
        if replacement and not replacement.endswith(newline) and lines[end - 1].endswith(("\n", "\r")):
            replacement += newline
        self._write(file, "".join(lines[: start - 1]) + replacement + "".join(lines[end:]))
        return f"Replaced lines {start}-{end} of {self.rel(file)}." + self._impact_note(self.rel(file), start)

    def _impact_note(self, rel_path: str, line: int) -> str:
        """After an edit, tell the model what else the change could affect (the living code graph)."""
        if self.graph is None or language_of(rel_path) is None:
            return ""
        self.graph.refresh()
        symbol = self.graph.symbol_at(rel_path, line)
        if symbol is None:
            return ""
        name, qualname = symbol
        if is_test_path(rel_path):
            return f"\n\n🕸 Code graph: you changed the test {qualname}. Run it before you finish."
        impact = self.graph.impact_of_change(name)
        return f"\n\n🕸 Code graph: you changed {qualname}.\n{impact}\nRun those tests before you finish."

    def create_file(self, path: str, content: str) -> str:
        file = self.resolve(path)
        if file.exists():
            raise ToolError(f"'{path}' already exists. Use edit_file to change it.")
        file.parent.mkdir(parents=True, exist_ok=True)
        self._write(file, content)
        return f"Created {self.rel(file)}."

    def run_command(self, command: str, timeout: int = 120) -> str:
        timeout = max(1, min(timeout, MAX_COMMAND_SECONDS))
        if not self.approve_command(command):
            return "The user declined to run this command. Try a different approach or explain what you need."
        try:
            proc = subprocess.run(
                command, shell=True, cwd=self.root, capture_output=True,
                text=True, encoding="utf-8", errors="replace", timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return f"Command timed out after {timeout} seconds."
        return truncate(
            f"exit code: {proc.returncode}\n--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )

    # ---------------------------------------------------------- code graph tools

    def _graph(self) -> CodeGraph:
        if self.graph is None:
            raise ToolError("The code graph is not available. Use search_code instead.")
        self.graph.refresh()  # pick up any edits made since the last query
        return self.graph

    def find_symbol(self, name: str) -> str:
        return truncate(self._graph().find_symbol(name))

    def find_callers(self, name: str) -> str:
        return truncate(self._graph().find_callers(name))

    def find_callees(self, name: str) -> str:
        return truncate(self._graph().find_callees(name))

    def related_tests(self, name: str) -> str:
        return truncate(self._graph().related_tests(name))

    def impact_of_change(self, name: str) -> str:
        return truncate(self._graph().impact_of_change(name))

    # ----------------------------------------------------------------- dispatch

    def call(self, name: str, args: dict) -> str:
        handler = TOOL_HANDLERS.get(name)
        if handler is None:
            return f"Error: unknown tool '{name}'. Available tools: {', '.join(TOOL_HANDLERS)}, finish."

        # Models sometimes guess slightly wrong argument names; map common ones and drop the rest.
        accepted = set(inspect.signature(handler).parameters) - {"self"}
        cleaned, ignored = {}, []
        for key, value in args.items():
            key = ARG_ALIASES.get(key, key)
            if key in accepted:
                cleaned[key] = value
            else:
                ignored.append(key)
        note = f"(Note: ignored unknown arguments: {', '.join(ignored)})\n" if ignored else ""

        try:
            return note + handler(self, **cleaned)
        except ToolError as e:
            return f"Error: {e}"
        except TypeError as e:
            return f"Error: bad arguments for {name}: {e}"


ARG_ALIASES = {
    "line_start": "start_line", "line_end": "end_line",
    "file": "path", "file_path": "path", "filename": "path",
    "regex": "pattern", "query": "pattern", "glob": "file_glob",
    "cmd": "command", "depth": "max_depth",
    "old": "old_text", "new": "new_text", "old_string": "old_text", "new_string": "new_text",
    "symbol": "name", "symbol_name": "name", "function": "name", "function_name": "name",
}


TOOL_HANDLERS: dict[str, Callable[..., str]] = {
    "list_files": Workspace.list_files,
    "read_file": Workspace.read_file,
    "search_code": Workspace.search_code,
    "edit_file": Workspace.edit_file,
    "replace_lines": Workspace.replace_lines,
    "create_file": Workspace.create_file,
    "run_command": Workspace.run_command,
    "find_symbol": Workspace.find_symbol,
    "find_callers": Workspace.find_callers,
    "find_callees": Workspace.find_callees,
    "related_tests": Workspace.related_tests,
    "impact_of_change": Workspace.impact_of_change,
}
GRAPH_TOOLS = {"find_symbol", "find_callers", "find_callees", "related_tests", "impact_of_change"}

_NAME_ARG = {"name": {"type": "string", "description": "Function, method or class name, e.g. 'average' or 'Cart.total'."}}


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


TOOL_SCHEMAS = [
    _tool(
        "list_files",
        "List files in a directory of the repository (skips .git, node_modules, virtualenvs, etc.).",
        {
            "path": {"type": "string", "description": "Directory relative to the repo root. Default '.'."},
            "max_depth": {"type": "integer", "description": "How many directory levels deep to list. Default 3."},
        },
        [],
    ),
    _tool(
        "read_file",
        "Read a file with line numbers. Use start_line/end_line for large files.",
        {
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "start_line": {"type": "integer", "description": "First line to show (1-based)."},
            "end_line": {"type": "integer", "description": "Last line to show (inclusive)."},
        },
        ["path"],
    ),
    _tool(
        "search_code",
        "Search every text file in the repository for a regular expression. Returns file:line: text.",
        {
            "pattern": {"type": "string", "description": "Python regular expression."},
            "file_glob": {"type": "string", "description": "Optional filter such as '*.py' or 'src/**/*.ts'."},
        },
        ["pattern"],
    ),
    _tool(
        "edit_file",
        "Replace one exact, unique occurrence of old_text with new_text in a file.",
        {
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "old_text": {"type": "string", "description": "Exact text to replace, including indentation."},
            "new_text": {"type": "string", "description": "Replacement text."},
        },
        ["path", "old_text", "new_text"],
    ),
    _tool(
        "replace_lines",
        "Replace lines start_line..end_line (inclusive, 1-based, as shown by read_file) with new_text. "
        "Use this when edit_file's old_text is not unique.",
        {
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "start_line": {"type": "integer", "description": "First line to replace."},
            "end_line": {"type": "integer", "description": "Last line to replace (inclusive)."},
            "new_text": {"type": "string", "description": "Replacement text for those lines."},
        },
        ["path", "start_line", "end_line", "new_text"],
    ),
    _tool(
        "create_file",
        "Create a new file (for example a regression test). Fails if the file already exists.",
        {
            "path": {"type": "string", "description": "File path relative to the repo root."},
            "content": {"type": "string", "description": "Full file contents."},
        },
        ["path", "content"],
    ),
    _tool(
        "run_command",
        "Run a shell command in the repository root, e.g. to run tests. Stops after 120 seconds. "
        "The user may decline.",
        {"command": {"type": "string", "description": "The command to run."}},
        ["command"],
    ),
    _tool(
        "find_symbol",
        "Code graph: find where a function, method or class is defined (file, lines, signature).",
        _NAME_ARG, ["name"],
    ),
    _tool(
        "find_callers",
        "Code graph: list every place that calls a function or method.",
        _NAME_ARG, ["name"],
    ),
    _tool(
        "find_callees",
        "Code graph: list the functions a function calls, and where they are defined.",
        _NAME_ARG, ["name"],
    ),
    _tool(
        "related_tests",
        "Code graph: find the tests that exercise a function, directly or through other functions.",
        _NAME_ARG, ["name"],
    ),
    _tool(
        "impact_of_change",
        "Code graph: show everything that could break if a function changes, and which tests to run.",
        _NAME_ARG, ["name"],
    ),
    _tool(
        "finish",
        "Call this when you are done. Summarize the root cause and the change, or explain why you could not fix it.",
        {
            "summary": {"type": "string", "description": "Short summary for the pull request description."},
            "fixed": {"type": "boolean", "description": "True if the bug is fixed and verified."},
        },
        ["summary", "fixed"],
    ),
]
