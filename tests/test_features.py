"""The ten 0.4 features: fallback, memory, confidence, test gaps, tracing, poltergeist,
replay/share, review, CI auto-fix and time-lapse."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import openai
import pytest

from ghostpatch import cifix, cli, history, memory, review
from ghostpatch.confidence import assess
from ghostpatch.fallback import GEMINI_PLACEHOLDER_SIGNATURE, FallbackClient, is_exhausted
from ghostpatch.graph import CodeGraph
from ghostpatch.poltergeist import fix_with_poltergeist, only_tests
from ghostpatch.agent import Agent
from ghostpatch.providers import PROVIDERS, fallback_chain, resolve
from ghostpatch.replay import RecordingUI, export_html
from ghostpatch.session import run_session
from ghostpatch.timelapse import timelapse
from ghostpatch.tools import Workspace
from ghostpatch.trace import enrich_issue, locate, parse
from test_agent import FakeClient, SilentUI, reply, tool_call

SHOP = {
    "shop/__init__.py": "",
    "shop/pricing.py": "def apply_discount(price, percent):\n    return round(price - price * percent / 10, 2)\n",
    "shop/cart.py": (
        "from shop.pricing import apply_discount\n\n"
        "def total(prices, percent=0):\n    return apply_discount(sum(prices), percent)\n"
    ),
    "shop/invoice.py": (
        "from shop.pricing import apply_discount\n\n"
        "def bulk_price(unit, qty):\n    return apply_discount(unit * qty, 5)\n"
    ),
    "tests/test_cart.py": "from shop.cart import total\n\ndef test_total():\n    assert total([10, 20]) == 30\n",
    "conftest.py": "",
}


def write(root: Path, files: dict) -> Path:
    for rel, text in files.items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(text, encoding="utf-8")
    return root


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def git_repo(root: Path, files: dict) -> Path:
    write(root, files)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")
    return root


def rate_limit(message: str):
    error = openai.RateLimitError.__new__(openai.RateLimitError)
    Exception.__init__(error, message)
    return error


def fake_config(name="groq", model="m", client=None):
    return SimpleNamespace(provider=SimpleNamespace(name=name), model=model, client=lambda: client)


# --------------------------------------------------------------------------- 8. fallback

class ScriptedCompletions:
    def __init__(self, *outcomes):
        self.outcomes, self.requests = list(outcomes), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_fallback_switches_when_the_daily_quota_is_gone():
    groq = ScriptedCompletions(rate_limit("tokens per day (TPD): Limit 200000"))
    gemini = ScriptedCompletions("ok")
    switches = []
    client = FallbackClient([fake_config("groq", "qwen", groq), fake_config("gemini", "flash", gemini)],
                            on_switch=lambda old, new, why: switches.append((old.model, new.model)))
    messages = [{"role": "assistant", "content": None,
                 "tool_calls": [{"id": "1", "type": "function", "function": {"name": "x", "arguments": "{}"}}]}]
    assert client.chat.completions.create(model="ignored", messages=messages, tools=[]) == "ok"
    assert switches == [("qwen", "flash")] and client.used == ["groq/qwen", "gemini/flash"]
    sent = gemini.requests[0]
    assert sent["model"] == "flash"
    assert sent["messages"][0]["tool_calls"][0]["extra_content"]["google"]["thought_signature"] == GEMINI_PLACEHOLDER_SIGNATURE
    assert "extra_content" not in messages[0]["tool_calls"][0]  # the caller's history is left alone


def test_a_provider_switch_is_saved_with_the_run(tmp_path: Path):
    repo = write(tmp_path, SHOP)
    finish = FakeClient([reply(None, [tool_call("1", "finish", fixed=False, summary="Nothing to do.")])])
    groq = ScriptedCompletions(rate_limit("tokens per day (TPD): Limit 200000"))
    client = FallbackClient([fake_config("groq", "qwen", groq), fake_config("gemini", "flash", finish)])
    outcome = run_session(repo, fake_config("groq", "qwen"), client, SilentUI(), "Nothing is wrong.",
                          approve_command=lambda command: False)
    thoughts = [e["text"] for e in outcome.events if e["type"] == "thought"]
    assert any("Switching to gemini/flash" in t for t in thoughts)
    assert history.load_run(repo, outcome.run_id)["model"] == "groq/qwen → gemini/flash"


GROQ_TPD = ("Error code: 429 - {'error': {'message': 'Rate limit reached for model `qwen/qwen3.8-27b` in organization "
            "`org_x` service tier `on_demand` on tokens per day (TPD): Limit 500000, Used 499800, Requested 1500. "
            "Please try again in 2m9.6s. Need more tokens? Upgrade to Dev Tier today', 'type': 'tokens'}}")
GEMINI_DAILY = ("Error code: 429 - [{'error': {'code': 429, 'message': 'You exceeded your current quota.\\n* Quota "
                "exceeded for metric: generativelanguage.googleapis.com/generate_content_free_tier_requests, limit: 20, "
                "model: gemini-3.8-flash\\nPlease retry in 10.6s.', 'details': [{'quotaId': "
                "'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}}]")


def test_rate_limit_errors_are_summarized():
    from ghostpatch.providers import summarize_rate_limit
    assert summarize_rate_limit(GROQ_TPD) == "daily token limit of 500,000 reached; try again in 2m9s"
    assert summarize_rate_limit(GEMINI_DAILY) == "daily request limit of 20 reached"
    assert summarize_rate_limit("tokens per minute (TPM): Limit 6000. Please try again in 3.2s.") \
        == "per-minute token limit of 6,000 reached; try again in 3s"
    assert summarize_rate_limit("insufficient_quota: You exceeded your current quota") == "out of credits"
    assert summarize_rate_limit("Too many requests") == "rate limited"


def test_when_every_provider_is_exhausted_the_error_names_them_all(tmp_path: Path):
    repo = write(tmp_path, SHOP)
    client = FallbackClient([
        fake_config("groq", "qwen", ScriptedCompletions(rate_limit(GROQ_TPD))),
        fake_config("gemini", "flash", ScriptedCompletions(rate_limit(GEMINI_DAILY))),
    ])
    outcome = run_session(repo, fake_config("groq", "qwen"), client, SilentUI(), "Bug.",
                          approve_command=lambda command: False)
    assert outcome.error == (
        "Every configured provider is unavailable right now. "
        "groq: daily token limit of 500,000 reached; try again in 2m9s. gemini: daily request limit of 20 reached."
    )


def test_short_rate_limits_are_left_to_the_agent():
    assert not is_exhausted(rate_limit("Too many requests, retry in 20s"))
    assert is_exhausted(rate_limit("GenerateRequestsPerDayPerProjectPerModel-FreeTier"))
    groq = ScriptedCompletions(rate_limit("Too many requests"))
    client = FallbackClient([fake_config("groq", "a", groq), fake_config("gemini", "b", ScriptedCompletions("ok"))])
    with pytest.raises(openai.RateLimitError):
        client.chat.completions.create(model="a", messages=[])
    assert client.used == ["groq/a"]


def test_fallback_chain(monkeypatch):
    for var in ("GROQ_API_KEY", "GEMINI_API_KEY", "OPENAI_API_KEY", "GHOSTPATCH_FALLBACK", "GHOSTPATCH_MODEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "g")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("GHOSTPATCH_MODEL", "only-for-groq")
    chain = fallback_chain(resolve("groq"))
    assert [(c.provider.name, c.model) for c in chain] == [("groq", "only-for-groq"),
                                                            ("gemini", PROVIDERS["gemini"].default_model)]
    monkeypatch.setenv("GHOSTPATCH_FALLBACK", "none")
    assert len(fallback_chain(resolve("groq"))) == 1


# ---------------------------------------------------------------------------- 9. memory

def test_memory_notes(tmp_path: Path):
    memory.remember(tmp_path, "Run tests with `node --test`.")
    memory.remember(tmp_path, "run tests with `node --test`.")  # duplicate, different case
    memory.remember(tmp_path, "Money is stored in cents.")
    assert memory.learned_notes(tmp_path) == ["run tests with `node --test`.", "Money is stored in cents."]
    (tmp_path / "GHOSTPATCH.md").write_text("Use pytest fixtures.", encoding="utf-8")
    section = memory.prompt_section(tmp_path)
    assert "Team notes from GHOSTPATCH.md:\nUse pytest fixtures." in section
    assert "- Money is stored in cents." in section
    assert memory.forget_all(tmp_path) == 2 and memory.learned_notes(tmp_path) == []


def test_the_agent_can_remember_and_starts_with_its_memory(tmp_path: Path):
    memory.remember(tmp_path, "Tests live in tests/.")
    client = FakeClient([
        reply(None, [tool_call("1", "remember", note="The API returns cents, not dollars.")]),
        reply(None, [tool_call("2", "finish", summary="done", fixed=False)]),
    ])
    Agent(client, "m", Workspace(tmp_path, lambda c: True), SilentUI()).run("bug")
    first_message = client.requests[0]["messages"][1]["content"]
    assert "- Tests live in tests/." in first_message
    assert "The API returns cents, not dollars." in memory.learned_notes(tmp_path)


# ------------------------------------------------------------- 10. confidence / 6. gaps

@pytest.fixture
def shop(tmp_path: Path) -> Path:
    return write(tmp_path, SHOP)


def test_names_reached_by_tests_and_untested(shop: Path):
    graph = CodeGraph(shop, db_path=":memory:")
    graph.refresh()
    assert {"total", "apply_discount"} <= graph.names_reached_by_tests()
    assert [u["qualname"] for u in graph.untested()] == ["shop.invoice.bulk_price"]
    radius = graph.blast_radius(["apply_discount"])
    assert radius["shop.pricing.apply_discount"]["tested"] is True
    assert radius["shop.invoice.bulk_price"]["tested"] is False
    nodes = {n["qualname"]: n for n in graph.export()["nodes"]}
    assert nodes["shop.invoice.bulk_price"]["tested"] is False and nodes["shop.cart.total"]["tested"] is True
    graph.close()


def _edit_pricing(ws: Workspace) -> None:
    ws.call("edit_file", {"path": "shop/pricing.py", "old_text": "/ 10,", "new_text": "/ 100,"})


def test_confidence_levels(shop: Path):
    graph = CodeGraph(shop, db_path=":memory:")
    ws = Workspace(shop, lambda c: True, graph=graph)
    assert assess(ws)["level"] == "none"

    _edit_pricing(ws)
    not_run = assess(ws)
    assert not_run["tests_after_edit"] == "not run" and not_run["blast_radius"] == 3
    assert not_run["uncovered"] == ["shop.invoice.bulk_price"]

    ws.call("run_command", {"command": f'"{sys.executable}" -m pytest -q -p no:cacheprovider'})
    passed = assess(ws)
    assert passed["tests_after_edit"] == "passed"
    assert passed["score"] == 50 + round(50 * 2 / 3) and passed["level"] == "high"
    assert "2 of 3 affected functions are covered" in passed["summary"]

    (shop / "tests" / "test_cart.py").write_text("def test_bad():\n    assert False\n", encoding="utf-8")
    ws.call("run_command", {"command": f'"{sys.executable}" -m pytest -q -p no:cacheprovider'})
    assert assess(ws)["tests_after_edit"] == "failed" and assess(ws)["level"] == "low"
    graph.close()


def test_cli_gaps(shop: Path, capsys):
    assert cli.main(["gaps", "--repo", str(shop), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["qualname"] == "shop.invoice.bulk_price"


# ----------------------------------------------------------------------------- 2. trace

PY_TRACE = """Traceback (most recent call last):
  File "/home/ci/work/app/main.py", line 3, in <module>
    run()
  File "/home/ci/work/app/shop/cart.py", line 4, in total
    return apply_discount(sum(prices), percent)
  File "/home/ci/work/app/shop/pricing.py", line 2, in apply_discount
    return round(price - price * percent / 10, 2)
  File "/usr/lib/python3.12/site-packages/lib.py", line 9, in helper
ZeroDivisionError: division by zero
"""

NODE_TRACE = """TypeError: Cannot read properties of undefined (reading 'price')
    at applyDiscount (file:///C:/work/app/src/pricing.ts:2:20)
    at total (C:\\work\\app\\src\\cart.ts:5:10)
    at node:internal/main/run_main_module:28:49
"""


def test_parse_python_and_node_traces():
    py = parse(PY_TRACE)
    assert py.language == "python" and py.error == "ZeroDivisionError: division by zero"
    assert [f.function for f in py.frames] == ["<module>", "total", "apply_discount", "helper"]
    node = parse(NODE_TRACE)
    assert node.language == "javascript" and node.error.startswith("TypeError: Cannot read properties")
    assert [f.function for f in node.frames] == ["total", "applyDiscount"]  # entry first; node internals skipped
    assert parse("no trace here") is None
    pytest_style = parse("shop/pricing.py:2: in apply_discount\n    return x\nE   ZeroDivisionError")
    assert pytest_style.frames[0].line == 2


def test_trace_maps_onto_the_graph_even_from_another_machine(shop: Path):
    graph = CodeGraph(shop, db_path=":memory:")
    trace = locate(parse(PY_TRACE), shop, graph)
    assert [f.rel_path for f in trace.repo_frames] == ["shop/cart.py", "shop/pricing.py"]
    assert trace.path_qualnames() == ["shop.cart.total", "shop.pricing.apply_discount"]
    issue, found = enrich_issue("It crashes:\n" + PY_TRACE, shop, graph)
    assert "💥 shop/pricing.py:2  in shop.pricing.apply_discount" in issue and found is not None
    graph.close()


# ------------------------------------------------------------------------ 1. poltergeist

def test_poltergeist_breaks_the_fix_and_the_ghost_fixes_it_again(shop: Path):
    graph = CodeGraph(shop, db_path=":memory:")
    ws = Workspace(shop, lambda c: True, graph=graph)
    client = FakeClient([
        # the ghost's first, incomplete "fix"
        reply(None, [tool_call("1", "edit_file", path="shop/pricing.py", old_text="/ 10,", new_text="/ 50,")]),
        reply(None, [tool_call("2", "finish", summary="Divided by 50.", fixed=True)]),
        # round 1: the poltergeist may not touch code...
        reply(None, [tool_call("3", "edit_file", path="shop/pricing.py", old_text="/ 50,", new_text="/ 1,")]),
        # ...so it writes a test and reports that it broke the fix
        reply(None, [tool_call("4", "create_file", path="tests/test_poltergeist.py",
                               content="from shop.pricing import apply_discount\n\ndef test_ten_percent():\n"
                                       "    assert apply_discount(50, 10) == 45\n")]),
        reply(None, [tool_call("5", "finish", summary="10% off 50 gives 40, not 45.", fixed=True)]),
        # the ghost fixes it properly
        reply(None, [tool_call("6", "edit_file", path="shop/pricing.py", old_text="/ 50,", new_text="/ 100,")]),
        reply(None, [tool_call("7", "finish", summary="Divide by 100.", fixed=True)]),
        # round 2: the poltergeist gives up
        reply(None, [tool_call("8", "finish", summary="All my tests pass.", fixed=False)]),
    ])
    def make_agent(**kwargs):
        return Agent(client, "m", ws, SilentUI(), max_steps=5, **kwargs)

    result, rounds = fix_with_poltergeist(make_agent, ws, SilentUI(), "10% coupon is wrong", rounds=2)
    assert result.fixed and "survived the poltergeist (2 rounds)" in result.summary
    assert [r.broke_it for r in rounds] == [True, False] and rounds[0].refixed is True
    assert rounds[0].tests == ["tests/test_poltergeist.py"]
    assert "/ 100," in (shop / "shop" / "pricing.py").read_text(encoding="utf-8")
    assert "only write test files" in only_tests("shop/pricing.py") and only_tests("tests/test_x.py") is None
    attack_messages = client.requests[2]["messages"]
    assert "You are the Poltergeist" in attack_messages[0]["content"]
    assert "shop.invoice.bulk_price" in attack_messages[1]["content"]  # it got the blast radius
    blocked = [m for m in client.requests[3]["messages"] if m["role"] == "tool"][-1]["content"]
    assert "may only write test files" in blocked
    graph.close()


# ------------------------------------------------------------- 5. replay, share, session

def test_session_records_traces_confidence_and_replays(shop: Path):
    client = FakeClient([
        reply(None, [tool_call("1", "edit_file", path="shop/pricing.py", old_text="/ 10,", new_text="/ 100,")]),
        reply(None, [tool_call("2", "finish", summary="Divide by 100.", fixed=True)]),
    ])
    graph = CodeGraph(shop, db_path=":memory:")
    ui = SilentUI()
    ui.approve_command = lambda c: True
    outcome = run_session(shop, fake_config(client=client), client, ui, "Crash!\n" + PY_TRACE, graph=graph)
    graph.close()
    assert outcome.fixed and outcome.trace.path_qualnames()[-1] == "shop.pricing.apply_discount"
    assert "💥 shop/pricing.py:2" in client.requests[0]["messages"][1]["content"]

    run = history.load_run(shop, outcome.run_id)
    assert run["trace"]["path"] == ["shop.cart.total", "shop.pricing.apply_discount"]
    assert run["confidence"]["tests_after_edit"] == "not run"
    assert [e["type"] for e in run["events"] if e["type"] == "tool"] == ["tool", "tool"]
    assert "events" not in history.summarize(run)

    page = export_html({**run, "summary": "Divide by 100 </script><b>"})
    assert page.startswith("<!doctype html>") and "&lt;/script&gt;&lt;b&gt;" in page
    assert page.count("</script>") == 1  # recorded text can't close the player's script tag


def test_recording_ui_passes_everything_through():
    seen = []
    inner = SimpleNamespace(step=lambda *a: seen.append("step"), thought=lambda t: seen.append("thought"),
                            tool_call=lambda *a: seen.append("tool"), approve_command=lambda c: "asked")
    ui = RecordingUI(inner)
    ui.step(1, 5)
    ui.thought("x" * 5000)
    ui.tool_call("read_file", {"path": "a.py"}, "ok")
    assert seen == ["step", "thought", "tool"] and ui.approve_command("ls") == "asked"
    assert [e["type"] for e in ui.events] == ["step", "thought", "tool"]
    assert len(ui.events[1]["text"]) < 4100


# ---------------------------------------------------------------------------- 3. review

def test_changed_lines_parser():
    diff = ("--- a/shop/cart.py\n+++ b/shop/cart.py\n@@ -3,2 +3,3 @@\n def total(prices, percent=0):\n"
            "-    return 1\n+    x = 2\n+    return x\n--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-gone\n")
    assert review.changed_lines(diff) == {"shop/cart.py": {4, 5}}


def test_review_of_uncommitted_changes(tmp_path: Path):
    repo = git_repo(tmp_path / "shop", SHOP)
    pricing = repo / "shop" / "pricing.py"
    pricing.write_text(pricing.read_text(encoding="utf-8").replace("/ 10,", "/ 100,"), encoding="utf-8")
    report = review.review_working_tree(repo)
    assert [c["qualname"] for c in report["changed"]] == ["shop.pricing.apply_discount"]
    assert {a["qualname"] for a in report["affected"]} == {"shop.cart.total", "shop.invoice.bulk_price"}
    assert report["untested"] == ["shop.invoice.bulk_price"] and report["risk"] == "medium"
    assert "⚠ untested" in report["markdown"]


# -------------------------------------------------------------------------- 4. CI fix

def test_detect_test_command(tmp_path: Path):
    assert cifix.detect_test_command(tmp_path) is None
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest"}}', encoding="utf-8")
    assert cifix.detect_test_command(tmp_path) == "npm test"
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    assert cifix.detect_test_command(tmp_path) == "node --test"
    (tmp_path / "package.json").unlink()
    (tmp_path / "tests").mkdir()
    assert "-m pytest" in cifix.detect_test_command(tmp_path)


def test_ci_fix_fixes_failing_tests_and_verifies_them_itself(tmp_path: Path, monkeypatch, capsys):
    files = dict(SHOP)
    files["tests/test_pricing.py"] = ("from shop.pricing import apply_discount\n\ndef test_ten_percent():\n"
                                      "    assert apply_discount(50, 10) == 45\n")
    repo = write(tmp_path / "shop", files)
    client = FakeClient([
        reply(None, [tool_call("1", "edit_file", path="shop/pricing.py", old_text="/ 10,", new_text="/ 100,")]),
        reply(None, [tool_call("2", "finish", summary="Divide by 100.", fixed=True)]),
    ])
    monkeypatch.setattr("ghostpatch.providers.resolve", lambda *a, **k: fake_config(client=client))
    monkeypatch.setattr("ghostpatch.fallback.make_client", lambda config, ui, fallback=True: client)
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    command = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'

    assert cli.main(["ci-fix", "--repo", str(repo), "--test-command", command, "--no-graph"]) == 0
    out = capsys.readouterr().out
    assert "The tests fail" in out and "The tests pass now" in out
    assert "GhostPatch fixed the failing tests" in summary.read_text(encoding="utf-8")
    assert cli.main(["ci-fix", "--repo", str(repo), "--test-command", command]) == 0  # nothing left to fix
    assert "Nothing to fix" in capsys.readouterr().out


# ------------------------------------------------------------------------- 7. time-lapse

def test_timelapse_frames(tmp_path: Path):
    repo = git_repo(tmp_path / "shop", SHOP)
    (repo / "shop" / "refunds.py").write_text("def refund(x):\n    return -x\n", encoding="utf-8")
    (repo / "shop" / "invoice.py").unlink()
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "refunds instead of invoices")
    data = timelapse(repo, commits=5)
    first, second = data["frames"]
    assert first["message"] == "initial" and "shop.invoice.bulk_price" in {n["qualname"] for n in first["nodes"]}
    assert second["added"] == ["shop.refunds.refund"] and second["removed"] == ["shop.invoice.bulk_price"]
    assert {"source": "shop.cart.total", "target": "shop.pricing.apply_discount"} in second["edges"]
    assert (repo / "shop" / "refunds.py").exists()  # the working folder was not touched
