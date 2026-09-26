"""The benchmark harness and the validity of every benchmark case."""

import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghostpatch import bench
from ghostpatch.providers import PROVIDERS
from test_agent import FakeClient, reply, tool_call

CASES_DIR = Path(__file__).resolve().parent.parent / "bench" / "cases"
CASES = bench.load_cases(CASES_DIR)


def test_there_are_cases_in_both_languages():
    assert len(CASES) >= 10
    assert {c.language for c in CASES} == {"python", "typescript"}


@pytest.mark.parametrize("case", CASES, ids=[c.name for c in CASES])
def test_every_case_is_valid(case):
    """Hidden tests must fail on the buggy code and pass with the reference fix."""
    if case.runner == "node" and shutil.which("node") is None:
        pytest.skip("Node.js is not installed")
    ok, why = bench.validate_case(case)
    assert ok, why


def fake_config(replies):
    return SimpleNamespace(provider=PROVIDERS["ollama"], model="fake-model", client=lambda: FakeClient(replies))


def weekend_case():
    return next(c for c in CASES if c.name == "py-weekend")


def test_a_correct_fix_passes_the_hidden_tests():
    config = fake_config([
        reply(None, [tool_call("1", "edit_file", path="calendar_utils/days.py",
                               old_text="weekday() > 5", new_text="weekday() >= 5")]),
        reply(None, [tool_call("2", "finish", summary="Saturday was not a weekend day.", fixed=True)]),
    ])
    result = bench.run_case(weekend_case(), config, use_graph=True, max_steps=5, ui=bench.QuietUI(lambda *a: None))
    assert result.passed and result.claimed_fixed and result.steps == 2


def test_claiming_a_fix_is_not_enough():
    config = fake_config([reply(None, [tool_call("1", "finish", summary="Fixed!", fixed=True)])])
    result = bench.run_case(weekend_case(), config, use_graph=False, max_steps=5, ui=bench.QuietUI(lambda *a: None))
    assert result.claimed_fixed and not result.passed
    assert "failed" in result.check_output


def test_results_are_saved_resumable_and_summarised(tmp_path: Path):
    out = tmp_path / "results.json"
    common = dict(language="python", provider="groq", model="m", claimed_fixed=True,
                  steps=4, prompt_tokens=1, completion_tokens=1, seconds=1.0)
    bench.save_result(out, bench.CaseResult(case="a", graph=True, passed=True, **common))
    bench.save_result(out, bench.CaseResult(case="a", graph=False, passed=False, **common))
    bench.save_result(out, bench.CaseResult(case="b", graph=True, passed=False, error="quota", **common))
    bench.save_result(out, bench.CaseResult(case="a", graph=True, passed=True, **common))  # replaces, not duplicates

    results = bench.load_results(out)
    assert len(results) == 3
    assert bench.already_done(results, "a", True, "m")
    assert not bench.already_done(results, "b", True, "m")  # errored runs are retried

    table = bench.summary_table(results)
    assert "| `a` | python | ✅ 4 steps | ❌ 4 steps |" in table
    assert "| `b` | python | ⚠ error | – |" in table
    assert "| **Solved** | | **1/1** | **0/1** |" in table
