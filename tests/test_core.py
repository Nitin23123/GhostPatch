"""The core loop's safety nets: the regression guard, the regression-test writer, and where to look first."""

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghostpatch import cifix, history
from ghostpatch.graph import CodeGraph
from ghostpatch.locate import rank, report_terms, split_identifier, where_to_look
from ghostpatch.regression import compare, failing_tests, run_suite, suite_command
from ghostpatch.session import run_session
from test_agent import FakeClient, SilentUI, reply, tool_call

# total() is off by one, and average() quietly compensates for it. Fixing total() alone breaks
# average(), whose test passed before: exactly the kind of regression the guard must catch.
STATS = {
    "stats.py": ("def total(xs):\n    return sum(xs) - 1\n\n\n"
                 "def average(xs):\n    return (total(xs) + 1) / len(xs)\n"),
    "conftest.py": "",
    "tests/test_stats.py": ("from stats import average, total\n\n\n"
                            "def test_total():\n    assert total([1, 2, 3]) == 6\n\n\n"
                            "def test_average():\n    assert average([2, 4]) == 3\n"),
}


@pytest.fixture(autouse=True)
def this_python(monkeypatch):
    monkeypatch.setattr(cifix, "project_python", lambda repo: f'"{sys.executable}"')


def make(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_bytes(text.encode())
    return root


def config():
    return SimpleNamespace(provider=SimpleNamespace(name="fake"), model="m")


def edit(call_id, old, new, path="stats.py"):
    return reply(None, [tool_call(call_id, "edit_file", path=path, old_text=old, new_text=new)])


def finish(call_id, summary="Done.", fixed=True):
    return reply(None, [tool_call(call_id, "finish", summary=summary, fixed=fixed)])


def test_failing_test_names_are_read_from_pytest_and_node():
    pytest_out = "F.\nFAILED tests/test_a.py::test_x - assert 1 == 2\nERROR tests/test_b.py::test_y\n1 failed in 0.1s"
    assert failing_tests(pytest_out) == {"tests/test_a.py::test_x", "tests/test_b.py::test_y"}
    node_out = "▶ cart\n  ✖ adds tax (1.2ms)\n  ✔ counts items (0.3ms)\n✖ failing tests:\n\n✖ adds tax (1.2ms)\n"
    assert failing_tests(node_out) == {"adds tax"}


def test_the_baseline_and_comparison(tmp_path: Path):
    repo = make(tmp_path, STATS)
    command = suite_command(repo)
    assert "-m pytest" in command and "-rfE" in command
    before = run_suite(repo, command)
    assert before.ran and before.failing == {"tests/test_stats.py::test_total"}
    (repo / "stats.py").write_bytes(STATS["stats.py"].replace("sum(xs) - 1", "sum(xs)").encode())
    check = compare(before, run_suite(repo, command))
    assert check.status == "regressed" and check.broken == ["tests/test_stats.py::test_average"]
    assert check.repaired == ["tests/test_stats.py::test_total"] and check.already_failing == 1


def test_tests_the_fix_breaks_go_back_to_the_ghost(tmp_path: Path):
    repo = make(tmp_path, STATS)
    client = FakeClient([
        edit("1", "sum(xs) - 1", "sum(xs)"), finish("2", "total() was off by one."),       # breaks average()
        edit("3", "(total(xs) + 1) / len(xs)", "total(xs) / len(xs)"), finish("4", "Also fixed average()."),
    ])
    outcome = run_session(repo, config(), client, SilentUI(), "total([1, 2, 3]) returns 5, not 6",
                          approve_command=lambda c: True, prove=False)

    assert outcome.fixed and outcome.regression.status == "clean"
    refix_prompt = client.requests[2]["messages"][1]["content"]
    assert "tests/test_stats.py::test_average" in refix_prompt and "passed before" in refix_prompt
    assert "total(xs) / len(xs)" in (repo / "stats.py").read_text(encoding="utf-8")
    saved = history.load_run(repo, outcome.run_id)
    assert saved["regression"]["status"] == "clean" and "nothing broke" in saved["confidence"]["summary"]


def test_a_fix_that_keeps_breaking_tests_is_not_called_fixed(tmp_path: Path):
    repo = make(tmp_path, STATS)
    client = FakeClient([edit("1", "sum(xs) - 1", "sum(xs)"), finish("2")])  # re-fix attempts just give up
    outcome = run_session(repo, config(), client, SilentUI(), "total is off by one",
                          approve_command=lambda c: True, prove=False)
    assert not outcome.fixed and outcome.regression.status == "regressed"
    assert "breaks 1 test(s) that passed before" in outcome.result.summary
    assert outcome.confidence["tests_after_edit"] == "failed" and outcome.confidence["score"] == 10


def test_the_tournament_disqualifies_a_candidate_that_breaks_tests(tmp_path: Path):
    repo = make(tmp_path, STATS)
    client = FakeClient([
        edit("a", "sum(xs) - 1", "sum(xs)"), finish("b"),                                        # breaks average()
        edit("c", "sum(xs) - 1", "sum(xs)"), edit("d", "(total(xs) + 1) / len(xs)", "total(xs) / len(xs)"),
        finish("e"),
    ])
    outcome = run_session(repo, config(), client, SilentUI(), "total is off by one", candidates=2,
                          approve_command=lambda c: True, prove=False)
    first, second = outcome.tournament.candidates
    assert first.disqualified == "broke 1 test(s) that passed before" and outcome.tournament.winner is second
    assert outcome.fixed and outcome.regression.status == "clean"


def test_a_fix_without_a_test_gets_one_and_is_proven(tmp_path: Path):
    repo = make(tmp_path, {**STATS, "tests/test_stats.py": "from stats import total\n\n\ndef test_empty():\n    assert total([]) == -1\n"})
    (repo / "stats.py").write_bytes(b"def total(xs):\n    return sum(xs) - 1\n")
    new_test = "from stats import total\n\n\ndef test_three_numbers():\n    assert total([1, 2, 3]) == 6\n"
    client = FakeClient([
        edit("1", "sum(xs) - 1", "sum(xs)"), finish("2", "total() was off by one."),
        reply(None, [tool_call("3", "create_file", path="tests/test_total_regression.py", content=new_test)]),
        finish("4", "Checks total([1, 2, 3])."),
    ])
    outcome = run_session(repo, config(), client, SilentUI(), "total([1, 2, 3]) returns 5",
                          approve_command=lambda c: True, regression=False)
    assert "test writer" in client.requests[2]["messages"][0]["content"]
    assert outcome.proof.status == "proven" and outcome.proof.tests == ["tests/test_total_regression.py"]


# ------------------------------------------------------------------- where to look

SHOP = {
    "shop/__init__.py": "",
    "shop/pricing.py": "def apply_discount(price, percent):\n    return price - price * percent / 10\n\n\n"
                       "def add_tax(price):\n    return price * 1.08\n",
    "shop/cart.py": "from shop.pricing import add_tax, apply_discount\n\n\nclass Cart:\n"
                    "    def total(self, percent=0):\n        return add_tax(apply_discount(100, percent))\n",
    "shop/checkout.py": "def checkout(cart, code):\n    return f'Total charged: ${cart.total(10):.2f}'\n",
    "shop/users.py": "def rename_user(user, name):\n    user.name = name\n",
}


def test_identifiers_and_words_are_split_and_stemmed():
    assert split_identifier("applyDiscount") == ["apply", "discount"]
    assert split_identifier("HTTPError_code") == ["http", "error", "code"]
    weights, identifiers, literals = report_terms("`apply_discount` gives `Total charged: $0.00` with discounts")
    assert "apply_discount" in identifiers and weights["discount"] >= 2
    assert "Total charged:" in literals  # the fixed part of a quoted message


def test_the_ranker_starts_where_the_bug_is(tmp_path: Path):
    graph = CodeGraph(make(tmp_path, SHOP), db_path=":memory:")
    try:
        suspects = rank(graph, "The coupon receipt says `Total charged: $0.00`: the discount is way too big.")
        names = [s.qualname for s in suspects]
        assert "shop.pricing.apply_discount" in names[:3]
        assert "shop.users.rename_user" not in names  # unrelated code stays out
        hint = where_to_look(graph, "the discount is way too big")
        assert "shop.pricing.apply_discount" in hint and "price * percent / 10" in hint  # its source is included
    finally:
        graph.close()


def test_the_ghost_gets_the_suspects_in_its_first_message(tmp_path: Path):
    repo = make(tmp_path, SHOP)
    client = FakeClient([finish("1", "Not sure.", fixed=False)])
    graph = CodeGraph(repo, db_path=":memory:")
    try:
        run_session(repo, config(), client, SilentUI(), "The discount takes far too much off",
                    graph=graph, approve_command=lambda c: True)
    finally:
        graph.close()
    first_message = client.requests[0]["messages"][1]["content"]
    assert "Code that looks most related to the issue" in first_message and "apply_discount" in first_message
