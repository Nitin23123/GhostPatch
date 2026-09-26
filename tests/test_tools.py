from pathlib import Path

import pytest

from ghostpatch.tools import Workspace


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "junk.js").write_text("def add", encoding="utf-8")
    return tmp_path


@pytest.fixture
def ws(repo: Path) -> Workspace:
    return Workspace(repo, approve_command=lambda cmd: True)


def test_paths_cannot_escape_the_repo(ws: Workspace):
    assert ws.call("read_file", {"path": "../secret.txt"}).startswith("Error: '../secret.txt' is outside")


def test_list_files_skips_ignored_dirs(ws: Workspace):
    listing = ws.call("list_files", {})
    assert "src/app.py" in listing
    assert "node_modules" not in listing


def test_read_file_shows_line_numbers(ws: Workspace):
    out = ws.call("read_file", {"path": "src/app.py"})
    assert "(lines 1-2 of 2)" in out
    assert "    2 |     return a - b" in out


def test_search_code_finds_matches(ws: Workspace):
    assert ws.call("search_code", {"pattern": r"return a"}) == "src/app.py:2: return a - b"
    assert ws.call("search_code", {"pattern": "nothing_here"}) == "No matches."
    assert ws.call("search_code", {"pattern": "("}).startswith("Error: Invalid regular expression")


def test_edit_file_replaces_unique_text(ws: Workspace, repo: Path):
    assert ws.call("edit_file", {"path": "src/app.py", "old_text": "a - b", "new_text": "a + b"}) == "Edited src/app.py."
    assert "return a + b" in (repo / "src" / "app.py").read_text(encoding="utf-8")
    assert ws.changed_files == {"src/app.py"}


def test_edit_file_rejects_missing_or_ambiguous_text(ws: Workspace, repo: Path):
    assert "was not found" in ws.call("edit_file", {"path": "src/app.py", "old_text": "zzz", "new_text": "y"})
    (repo / "dup.py").write_text("x = 1\nx = 1\n", encoding="utf-8")
    assert "appears 2 times" in ws.call("edit_file", {"path": "dup.py", "old_text": "x = 1", "new_text": "x = 2"})


def test_ambiguous_edit_lists_the_matching_lines(ws: Workspace, repo: Path):
    (repo / "dup.py").write_text("a = 1\nx = 1\nb = 2\nx = 1\n", encoding="utf-8")
    out = ws.call("edit_file", {"path": "dup.py", "old_text": "x = 1", "new_text": "x = 2"})
    assert "appears 2 times (starting on lines 2, 4)" in out and "replace_lines" in out


def test_replace_lines(ws: Workspace, repo: Path):
    (repo / "f.py").write_text("one\ntwo\nthree\n", encoding="utf-8")
    assert ws.call("replace_lines", {"path": "f.py", "start_line": 2, "end_line": 2, "new_text": "TWO\nTWO-B"}) \
        == "Replaced lines 2-2 of f.py."
    assert (repo / "f.py").read_text(encoding="utf-8") == "one\nTWO\nTWO-B\nthree\n"
    assert "out of range" in ws.call("replace_lines", {"path": "f.py", "start_line": 3, "end_line": 9, "new_text": ""})
    ws.call("replace_lines", {"path": "f.py", "start_line": "1", "end_line": "1", "new_text": ""})  # delete a line
    assert (repo / "f.py").read_text(encoding="utf-8") == "TWO\nTWO-B\nthree\n"


def test_replace_lines_restores_lost_indentation(ws: Workspace, repo: Path):
    # Seen live: the model sent a flush-left `return` for an indented line.
    out = ws.call("replace_lines", {"path": "src/app.py", "start_line": 2, "end_line": 2, "new_text": "return a + b"})
    assert "indented to match" in out
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "def add(a, b):\n    return a + b\n"


def test_flush_left_blocks_keep_their_relative_indentation(ws: Workspace, repo: Path):
    ws.call("edit_file", {"path": "src/app.py", "old_text": "    return a - b",
                          "new_text": "if a is None:\n    return b\nreturn a + b"})
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == \
        "def add(a, b):\n    if a is None:\n        return b\n    return a + b\n"


def test_only_a_stripped_first_line_is_fixed(ws: Workspace, repo: Path):
    (repo / "f.py").write_text("def f():\n    x = 1\n    y = 2\n", encoding="utf-8")
    ws.call("edit_file", {"path": "f.py", "old_text": "    x = 1\n    y = 2", "new_text": "x = 10\n    y = 2"})
    assert (repo / "f.py").read_text(encoding="utf-8") == "def f():\n    x = 10\n    y = 2\n"


def test_correct_indentation_is_left_alone(ws: Workspace, repo: Path):
    out = ws.call("edit_file", {"path": "src/app.py", "old_text": "    return a - b", "new_text": "    return a + b"})
    assert out == "Edited src/app.py."
    out = ws.call("edit_file", {"path": "src/app.py", "old_text": "a + b", "new_text": "b + a"})  # mid-line match
    assert out == "Edited src/app.py."
    assert (repo / "src" / "app.py").read_text(encoding="utf-8") == "def add(a, b):\n    return b + a\n"


def test_edits_that_break_the_syntax_are_flagged(ws: Workspace, repo: Path):
    out = ws.call("edit_file", {"path": "src/app.py", "old_text": "a - b", "new_text": "a + (b"})
    assert "Warning: src/app.py no longer parses" in out
    (repo / "cart.ts").write_text("export function f(a: number) {\n  return a;\n}\n", encoding="utf-8")
    out = ws.call("replace_lines", {"path": "cart.ts", "start_line": 3, "end_line": 3, "new_text": ""})
    assert "Warning: cart.ts no longer parses" in out
    out = ws.call("replace_lines", {"path": "cart.ts", "start_line": 2, "end_line": 2, "new_text": "  return a * 2;\n}"})
    assert out == "Replaced lines 2-2 of cart.ts."


def test_files_that_were_already_broken_are_not_blamed_on_the_edit(ws: Workspace, repo: Path):
    (repo / "broken.py").write_text("def f(:\n    return 1\n", encoding="utf-8")
    out = ws.call("edit_file", {"path": "broken.py", "old_text": "return 1", "new_text": "return 2"})
    assert out == "Edited broken.py."


def test_edit_file_preserves_windows_line_endings(ws: Workspace, repo: Path):
    (repo / "crlf.py").write_bytes(b"a = 1\r\nb = 2\r\n")
    ws.call("edit_file", {"path": "crlf.py", "old_text": "a = 1\nb = 2", "new_text": "a = 1\nb = 3"})
    assert (repo / "crlf.py").read_bytes() == b"a = 1\r\nb = 3\r\n"


def test_create_file_refuses_to_overwrite(ws: Workspace, repo: Path):
    assert ws.call("create_file", {"path": "tests/test_new.py", "content": "x"}) == "Created tests/test_new.py."
    assert "already exists" in ws.call("create_file", {"path": "src/app.py", "content": "x"})


def test_run_command_respects_user_decision(repo: Path):
    declined = Workspace(repo, approve_command=lambda cmd: False)
    assert "declined" in declined.call("run_command", {"command": "echo hi"})

    allowed = Workspace(repo, approve_command=lambda cmd: True)
    out = allowed.call("run_command", {"command": "echo hi"})
    assert "exit code: 0" in out and "hi" in out


def test_unknown_tool_and_bad_arguments(ws: Workspace):
    assert ws.call("delete_everything", {}).startswith("Error: unknown tool 'delete_everything'. Available tools:")
    assert ws.call("read_file", {"wrong": 1}).startswith("Error: bad arguments for read_file")


def test_common_argument_mistakes_are_tolerated(ws: Workspace):
    out = ws.call("read_file", {"file_path": "src/app.py", "line_start": 2, "line_end": 2, "verbose": True})
    assert out.startswith("(Note: ignored unknown arguments: verbose)")
    assert "(lines 2-2 of 2)" in out
