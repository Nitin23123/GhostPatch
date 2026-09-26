"""Haunt mode: risk ranking, bug hunting with real test runs, the skeptic, and fixing what it finds."""

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghostpatch import cifix, history
from ghostpatch.graph import CodeGraph
from ghostpatch.haunt import haunt, rank_targets
from ghostpatch.session import run_session
from test_agent import FakeClient, SilentUI, reply, tool_call

CALC = (
    "def add(a, b):\n    \"\"\"Return the sum of a and b.\"\"\"\n    return a - b\n\n\n"
    "def double(x):\n    \"\"\"Twice x.\"\"\"\n    return add(x, x)\n\n\n"
    "def total(items):\n    return sum(add(i, 0) for i in items)\n"
)
BUG_TEST = "from calc import add\n\n\ndef test_add_two_numbers():\n    assert add(2, 3) == 5\n"
FINE_TEST = "from calc import add\n\n\ndef test_add_zero():\n    assert add(4, 0) == 4\n"


@pytest.fixture(autouse=True)
def this_python(monkeypatch):
    monkeypatch.setattr(cifix, "project_python", lambda repo: f'"{sys.executable}"')


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "calc.py").write_bytes(CALC.encode())
    (tmp_path / "conftest.py").write_text("", encoding="utf-8")
    (tmp_path / "tests").mkdir()
    return tmp_path


def config():
    return SimpleNamespace(provider=SimpleNamespace(name="fake"), model="m")


def hunt(test: str, claim: str, found: bool) -> list:
    """The haunter's replies: write one test file, run nothing, finish."""
    return [
        reply(None, [tool_call("h1", "create_file", path="tests/test_haunt_add.py", content=test)]),
        reply(None, [tool_call("h2", "finish", summary=claim, fixed=found)]),
    ]


def verdict(kind: str, reason: str):
    return reply(json.dumps({"verdict": kind, "reason": reason}))


def test_the_riskiest_functions_come_first(repo: Path):
    graph = CodeGraph(repo)
    try:
        ranked = rank_targets(graph, repo)
    finally:
        graph.close()
    assert ranked[0].qualname == "calc.add"  # called from two places and untested
    assert "called from 2 places" in ranked[0].reasons and "no test reaches it" in ranked[0].reasons


def test_a_confirmed_bug_keeps_its_failing_test_as_proof(repo: Path):
    client = FakeClient([*hunt(BUG_TEST, "add() subtracts: add(2, 3) gives -1, not 5.", True),
                         verdict("real_bug", "The docstring says it returns the sum.")])
    graph = CodeGraph(repo)
    try:
        report = haunt(repo, config(), client, SilentUI(), graph=graph, targets=1, approve_command=lambda c: True)
    finally:
        graph.close()
    [finding] = report.findings
    assert finding.status == "confirmed" and finding.target.qualname == "calc.add"
    assert finding.tests == ["tests/test_haunt_add.py"] and "1 failed" in finding.failure
    assert (repo / "tests" / "test_haunt_add.py").exists()  # kept as proof
    assert "calc.add" in finding.as_issue() and "tests/test_haunt_add.py" in finding.as_issue()
    # The skeptic saw the function and the failing test.
    skeptic_prompt = client.requests[-1]["messages"][-1]["content"]
    assert "return a - b" in skeptic_prompt and "assert add(2, 3) == 5" in skeptic_prompt

    run = history.load_run(repo, report.run_id)
    assert run["kind"] == "haunt" and run["haunt"]["findings"][0]["status"] == "confirmed"
    history.undo_run(repo, report.run_id)
    assert not (repo / "tests" / "test_haunt_add.py").exists()


def test_the_skeptic_can_reject_an_invented_requirement(repo: Path):
    wrong = "from calc import add\n\n\ndef test_add_rejects_negatives():\n    assert add(-1, 1) is None\n"
    client = FakeClient([*hunt(wrong, "add() accepts negative numbers.", True),
                         verdict("test_is_wrong", "Nothing says negatives are invalid.")])
    report = haunt(repo, config(), client, SilentUI(), targets=1, approve_command=lambda c: True)
    assert report.findings[0].status == "false_alarm"
    assert not (repo / "tests" / "test_haunt_add.py").exists()  # its test was removed
    assert not report.bugs


def test_passing_tests_mean_no_bug_and_are_cleaned_up(repo: Path):
    client = FakeClient(hunt(FINE_TEST, "Checked add with zero.", False))
    report = haunt(repo, config(), client, SilentUI(), targets=1, approve_command=lambda c: True)
    assert report.findings[0].status == "clean"
    assert not (repo / "tests" / "test_haunt_add.py").exists()
    assert len(client.requests) == 2  # no skeptic call was needed


def test_a_haunt_stopped_by_quota_reports_it_and_saves_nothing(repo: Path):
    import openai

    class OutOfQuota(FakeClient):
        def _create(self, **kwargs):
            error = openai.RateLimitError.__new__(openai.RateLimitError)
            Exception.__init__(error, "tokens per day (TPD): Limit 200000")
            raise error

    report = haunt(repo, config(), OutOfQuota([]), SilentUI(), targets=1, approve_command=lambda c: True,
                   describe_error=lambda e: "quota gone")
    assert report.error == "quota gone" and report.findings[0].status == "error"
    assert report.run_id is None and not history.list_runs(repo)


def test_the_haunter_cannot_touch_the_code(repo: Path):
    client = FakeClient([
        reply(None, [tool_call("1", "edit_file", path="calc.py", old_text="a - b", new_text="a + b")]),
        reply(None, [tool_call("2", "finish", summary="Nothing found.", fixed=False)]),
    ])
    haunt(repo, config(), client, SilentUI(), targets=1, approve_command=lambda c: True)
    assert "return a - b" in (repo / "calc.py").read_text(encoding="utf-8")
    refusal = [m for m in client.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "may only write test files" in refusal


def test_fixing_a_haunted_bug_is_proven_by_the_haunters_test(repo: Path):
    client = FakeClient([*hunt(BUG_TEST, "add() subtracts.", True), verdict("real_bug", "Docstring says sum.")])
    report = haunt(repo, config(), client, SilentUI(), targets=1, approve_command=lambda c: True)
    [bug] = report.bugs

    fixer = FakeClient([
        reply(None, [tool_call("1", "edit_file", path="calc.py", old_text="return a - b", new_text="return a + b")]),
        reply(None, [tool_call("2", "finish", summary="add() now adds.", fixed=True)]),
    ])
    outcome = run_session(repo, config(), fixer, SilentUI(), bug.as_issue(), proof_tests=bug.tests,
                          approve_command=lambda c: True)
    assert outcome.fixed and outcome.proof.status == "proven"
    assert outcome.proof.tests == ["tests/test_haunt_add.py"]
