"""Red→green proof and the fix tournament, with real test runs in a throwaway repository."""

import sys
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest

from ghostpatch import cifix, history
from ghostpatch.confidence import assess
from ghostpatch.proof import prove_fix
from ghostpatch.session import run_session
from ghostpatch.tools import Workspace
from ghostpatch.tournament import _rival_path
from test_agent import FakeClient, SilentUI, reply, tool_call

BUGGY = "def add(a, b):\n    return a - b\n"
FIXED = "def add(a, b):\n    return a + b\n"
TEST_2_2 = "from calc import add\n\n\ndef test_two_and_two():\n    assert add(2, 2) == 4\n"


@pytest.fixture(autouse=True)
def this_python(monkeypatch):
    """Run the throwaway repos' tests with the interpreter running this suite (it has pytest)."""
    monkeypatch.setattr(cifix, "project_python", lambda repo: f'"{sys.executable}"')


def make_repo(root: Path, calc: str = BUGGY) -> Path:
    (root / "calc.py").write_bytes(calc.encode("utf-8"))  # exact bytes: no CRLF translation on Windows
    (root / "conftest.py").write_text("", encoding="utf-8")  # puts the repo root on sys.path
    (root / "tests").mkdir()
    return root


def fixed_repo(root: Path, calc: str = FIXED, test: str = TEST_2_2) -> tuple[Path, dict, set]:
    """A repo as a run leaves it: calc.py edited, a new test written."""
    repo = make_repo(root, calc)
    (repo / "tests" / "test_calc.py").write_text(test, encoding="utf-8")
    return repo, {"calc.py": BUGGY, "tests/test_calc.py": None}, {"calc.py", "tests/test_calc.py"}


def test_a_real_fix_is_proven_red_then_green(tmp_path: Path):
    repo, originals, changed = fixed_repo(tmp_path)
    proof = prove_fix(repo, originals, changed)
    assert proof.status == "proven", proof.red_output + proof.green_output
    assert "1 failed" in proof.red and "1 passed" in proof.green
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED  # the fix is back in place


def test_tests_that_pass_without_the_fix_prove_nothing(tmp_path: Path):
    repo, originals, changed = fixed_repo(tmp_path, test="from calc import add\n\ndef test_zero():\n    assert add(0, 0) == 0\n")
    assert prove_fix(repo, originals, changed).status == "not_red"


def test_a_fix_that_fails_its_own_tests_is_caught(tmp_path: Path):
    repo, originals, changed = fixed_repo(tmp_path, calc="def add(a, b):\n    return a * b + 1\n")
    assert prove_fix(repo, originals, changed).status == "not_green"


def test_nothing_to_prove_without_a_test_or_a_code_change(tmp_path: Path):
    repo = make_repo(tmp_path, FIXED)
    assert prove_fix(repo, {"calc.py": BUGGY}, {"calc.py"}).status == "no_test"
    (tmp_path / "second").mkdir()
    repo2, _, _ = fixed_repo(tmp_path / "second")
    assert prove_fix(repo2, {"tests/test_calc.py": None}, {"tests/test_calc.py"}).status == "no_code_change"


def test_a_declined_proof_changes_nothing(tmp_path: Path):
    repo, originals, changed = fixed_repo(tmp_path)
    assert prove_fix(repo, originals, changed, approve=lambda command: False).status == "skipped"
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED


def test_a_missing_test_runner_is_not_blamed_on_the_fix(tmp_path: Path, monkeypatch):
    repo, originals, changed = fixed_repo(tmp_path)
    fake = f'"{sys.executable}" -c "print(\'No module named pytest\'); raise SystemExit(1)"'
    monkeypatch.setattr("ghostpatch.proof.test_files_command", lambda repo, files: [fake])
    assert prove_fix(repo, originals, changed).status == "no_runner"


def test_the_fix_is_restored_even_if_the_red_run_is_interrupted(tmp_path: Path, monkeypatch):
    repo, originals, changed = fixed_repo(tmp_path)

    def interrupted(repo, commands):
        assert (repo / "calc.py").read_text(encoding="utf-8") == BUGGY  # the fix is out during the red run
        raise KeyboardInterrupt

    monkeypatch.setattr("ghostpatch.proof._run_all", interrupted)
    with pytest.raises(KeyboardInterrupt):
        prove_fix(repo, originals, changed)
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED
    assert (repo / "tests" / "test_calc.py").read_text(encoding="utf-8") == TEST_2_2


def test_test_output_headlines():
    assert cifix.headline("....F\nFAILED t.py::x\n1 failed, 4 passed in 0.12s\n") == "1 failed, 4 passed in 0.12s"
    assert cifix.headline("▶ cart\nℹ tests 4\nℹ suites 1\nℹ pass 3\nℹ fail 1\n") == "3 passed, 1 failed"


def test_the_proof_moves_the_confidence_score(tmp_path: Path):
    ws = Workspace(tmp_path, approve_command=lambda c: True)
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    ws.call("edit_file", {"path": "a.py", "old_text": "x = 1", "new_text": "x = 2"})
    base = assess(ws)["score"]  # tests not run: 0 + 20 (unknown blast radius)
    assert assess(ws, SimpleNamespace(status="proven"))["score"] == 50 + 20 + 10
    assert assess(ws, SimpleNamespace(status="not_green"))["score"] == 10
    assert assess(ws, SimpleNamespace(status="not_red"))["score"] == 50 + 20 - 10
    assert base == 20
    assert "proven" in assess(ws, SimpleNamespace(status="proven"))["summary"]


def test_project_python_falls_back_to_our_own(tmp_path: Path, monkeypatch):
    monkeypatch.undo()  # use the real project_python
    monkeypatch.setattr(cifix, "_PYTHONS", {})
    monkeypatch.setattr(cifix.shutil, "which", lambda name: None)
    monkeypatch.delenv("VIRTUAL_ENV", raising=False)
    assert cifix.project_python(tmp_path) == f'"{sys.executable}"'


# ------------------------------------------------------------------------ tournament

def config():
    return SimpleNamespace(provider=SimpleNamespace(name="fake"), model="m")


def candidate(calc_fix: str, test: str, *, first_id: str) -> list:
    """One candidate's replies: read, fix calc.py, write a test, finish."""
    return [
        reply(None, [tool_call(f"{first_id}a", "read_file", path="calc.py")]),
        reply(None, [tool_call(f"{first_id}b", "edit_file", path="calc.py", old_text="return a - b", new_text=calc_fix)]),
        reply(None, [tool_call(f"{first_id}c", "create_file", path="tests/test_calc.py", content=test)]),
        reply(None, [tool_call(f"{first_id}d", "finish", summary=f"Changed add() to `{calc_fix}`.", fixed=True)]),
    ]


TEST_BOTH = TEST_2_2 + "\n\ndef test_two_and_three():\n    assert add(2, 3) == 5\n"


def test_the_tournament_picks_the_fix_that_survives_its_rivals_tests(tmp_path: Path):
    repo = make_repo(tmp_path)
    client = FakeClient([
        *candidate("return 4", TEST_2_2, first_id="1"),        # a hard-coded "fix" that passes its own test
        *candidate("return a + b", TEST_BOTH, first_id="2"),   # the real fix
    ])
    outcome = run_session(repo, config(), client, SilentUI(), "add(2, 2) returns 0", candidates=2,
                          approve_command=lambda command: True)

    t = outcome.tournament
    assert t.winner is not None and t.winner.number == 2
    first, second = t.candidates
    assert first.proof.status == "proven" and second.proof.status == "proven"
    assert (first.rivals_passed, first.rivals_total) == (0, 1)   # "return 4" fails add(2, 3) == 5
    assert (second.rivals_passed, second.rivals_total) == (1, 1)
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED
    assert (repo / "tests" / "test_calc.py").read_text(encoding="utf-8") == TEST_BOTH
    assert not (repo / "tests" / "test_calc_rival1.py").exists()  # cross-examination cleaned up
    assert outcome.fixed and outcome.proof.proven
    assert "Candidate 2 (Test first) won" in outcome.result.summary

    saved = history.load_run(repo, outcome.run_id)
    assert saved["tournament"]["winner"] == 2 and saved["proof"]["status"] == "proven"
    assert {f["path"] for f in saved["files"]} == {"calc.py", "tests/test_calc.py"}
    assert all(f["before"] in (BUGGY, None) for f in saved["files"])  # undo restores the true original


def test_candidates_that_change_no_code_are_disqualified(tmp_path: Path):
    repo = make_repo(tmp_path)
    client = FakeClient([
        reply(None, [tool_call("1", "finish", summary="Looks fine to me.", fixed=True)]),
        *candidate("return a + b", TEST_2_2, first_id="2"),
    ])
    outcome = run_session(repo, config(), client, SilentUI(), "bug", candidates=2, approve_command=lambda c: True)
    assert outcome.tournament.candidates[0].disqualified == "changed no code"
    assert outcome.tournament.winner.number == 2


def test_a_quota_error_mid_tournament_keeps_the_finished_candidates(tmp_path: Path):
    repo = make_repo(tmp_path)

    class QuotaAfterFirst(FakeClient):
        def _create(self, **kwargs):
            if not self.replies:
                error = openai.RateLimitError.__new__(openai.RateLimitError)
                Exception.__init__(error, "tokens per day (TPD): Limit 500000")
                raise error
            return super()._create(**kwargs)

    client = QuotaAfterFirst(candidate("return a + b", TEST_2_2, first_id="1"))
    outcome = run_session(repo, config(), client, SilentUI(), "bug", candidates=3, approve_command=lambda c: True)
    t = outcome.tournament
    assert len(t.candidates) == 2 and t.stopped and t.candidates[1].error
    assert t.winner.number == 1 and outcome.error is None and outcome.fixed
    assert (repo / "calc.py").read_text(encoding="utf-8") == FIXED


def test_rival_test_files_get_distinct_names():
    assert _rival_path("tests/test_cart.py", 2) == "tests/test_cart_rival2.py"
    assert _rival_path("tests/cart.test.ts", 3) == "tests/cart_rival3.test.ts"
    assert _rival_path("test_x.py", 1) == "test_x_rival1.py"


def test_a_test_file_without_tests_proves_nothing(tmp_path: Path):
    repo, originals, changed = fixed_repo(tmp_path, test="x = 1\n")
    assert prove_fix(repo, originals, changed).status == "empty"


def test_runs_with_no_tests_are_recognised():
    assert cifix.no_tests_ran("\nno tests ran in 0.01s\n")
    assert cifix.no_tests_ran("ℹ tests 0\nℹ pass 0\n")
    assert not cifix.no_tests_ran("ℹ tests 10\n1 failed, 4 passed in 0.12s")
