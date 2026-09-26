"""Night shift: a real git repository pushing to a local "GitHub", a fake `gh` and a scripted model."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghostpatch import cifix, github, nightshift
from test_agent import FakeClient, SilentUI, reply, tool_call

PRICING = "def discount(price, percent):\n    \"\"\"The price after taking `percent` percent off.\"\"\"\n    return price - price * percent / 10\n"
TEST = "from pricing import discount\n\n\ndef test_ten_percent_off():\n    assert discount(100, 10) == 90\n"


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


class FakeGh:
    def __init__(self, issues, open_pr_branches=()):
        self.issues, self.branches, self.calls = issues, open_pr_branches, []

    def __call__(self, *args, cwd=None):
        self.calls.append(list(args))
        if args[:2] == ("issue", "list"):
            return json.dumps([{"number": n, "title": t, "url": f"https://github.com/me/shop/issues/{n}"}
                               for n, (t, _) in self.issues.items()])
        if args[:2] == ("pr", "list"):
            return json.dumps([{"headRefName": b} for b in self.branches])
        if args[:2] == ("issue", "view"):
            title, body = self.issues[int(args[2])]
            return json.dumps({"title": title, "body": body, "url": f"https://github.com/me/shop/issues/{args[2]}",
                               "comments": []})
        if args[:2] == ("pr", "create"):
            return f"https://github.com/me/shop/pull/{len([c for c in self.calls if c[:2] == ['pr', 'create']])}"
        return ""


@pytest.fixture(autouse=True)
def this_python(monkeypatch):
    monkeypatch.setattr(cifix, "project_python", lambda repo: f'"{sys.executable}"')


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    remote, work = tmp_path / "remote.git", tmp_path / "shop"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.name", "Test")
    git(work, "config", "user.email", "test@example.com")
    (work / "pricing.py").write_bytes(PRICING.encode())
    (work / "conftest.py").write_text("", encoding="utf-8")
    (work / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "remote", "add", "origin", "https://github.com/me/shop.git")
    git(work, "remote", "set-url", "--push", "origin", str(remote))
    return work


def config():
    return SimpleNamespace(provider=SimpleNamespace(name="fake"), model="m")


def the_fix(first: str) -> list:
    return [
        reply(None, [tool_call(f"{first}1", "edit_file", path="pricing.py", old_text="/ 10", new_text="/ 100")]),
        reply(None, [tool_call(f"{first}2", "create_file", path="tests/test_pricing.py", content=TEST)]),
        reply(None, [tool_call(f"{first}3", "finish", summary="Percent was divided by 10, not 100.", fixed=True)]),
    ]


def test_the_night_shift_opens_prs_for_proven_fixes_and_rolls_back_the_rest(repo: Path, monkeypatch):
    gh = FakeGh({1: ("Coupons are 10x too big", "10% off takes $100 to $0."), 2: ("Make it faster", "Somehow."),
                 3: ("Already being fixed", "...")}, open_pr_branches=["ghostpatch/issue-3-already-being-fixed"])
    monkeypatch.setattr(github, "run_gh", gh)
    client = FakeClient([*the_fix("a"), reply(None, [tool_call("b1", "finish", summary="Not sure how.", fixed=False)])])

    report = nightshift.run_nightshift(repo, config(), client, SilentUI(), approve=lambda c: True)

    first, second = report.items
    assert (first.number, first.status, first.proof) == (1, "pr", "proven")
    assert first.pr_url == "https://github.com/me/shop/pull/1" and not first.draft
    assert (second.number, second.status) == (2, "needs_you") and "wasn't verified" in second.reason
    assert all(i.number != 3 for i in report.items)  # it already has an open GhostPatch pull request

    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "main"  # back where it started
    assert git(repo, "status", "--porcelain") == ""  # nothing left over
    create = next(c for c in gh.calls if c[:2] == ["pr", "create"])
    assert "--draft" not in create and "🔴→🟢 Proof" in create[create.index("--body") + 1]
    branch = git(repo, "branch", "--list", "ghostpatch/*")
    assert "ghostpatch/issue-1-coupons-are-10x-too-big" in branch

    markdown = Path(report.path).read_text(encoding="utf-8")
    assert "✅ 1 ready for review · ⚠ 1 need you" in markdown and "pull/1" in markdown


def test_the_night_shift_fixes_what_haunting_finds(repo: Path, monkeypatch):
    monkeypatch.setattr(github, "run_gh", FakeGh({}))
    haunt_test = "from pricing import discount\n\n\ndef test_half_off():\n    assert discount(80, 50) == 40\n"
    client = FakeClient([
        reply(None, [tool_call("h1", "create_file", path="tests/test_haunt_discount.py", content=haunt_test)]),
        reply(None, [tool_call("h2", "finish", summary="discount() takes 10x too much off.", fixed=True)]),
        reply(json.dumps({"verdict": "real_bug", "reason": "The docstring says percent."})),
        reply(None, [tool_call("f1", "edit_file", path="pricing.py", old_text="/ 10", new_text="/ 100")]),
        reply(None, [tool_call("f2", "finish", summary="Divide by 100.", fixed=True)]),
    ])

    report = nightshift.run_nightshift(repo, config(), client, SilentUI(), approve=lambda c: True, haunt_targets=1)

    [item] = report.items
    assert item.kind == "haunt" and item.status == "pr" and item.proof == "proven"
    committed = git(repo, "show", "--name-only", "--format=", "ghostpatch/" + git(
        repo, "branch", "--list", "ghostpatch/*", "--format=%(refname:short)").split("ghostpatch/")[1]).splitlines()
    assert sorted(committed) == ["pricing.py", "tests/test_haunt_discount.py"]  # the proof ships with the fix
    assert git(repo, "status", "--porcelain") == ""
    assert "Haunted 1 function: 1 confirmed bug" in report.markdown()


def test_the_night_shift_refuses_a_dirty_working_tree(repo: Path, monkeypatch):
    monkeypatch.setattr(github, "run_gh", FakeGh({}))
    (repo / "pricing.py").write_text("# my unfinished work\n", encoding="utf-8")
    with pytest.raises(nightshift.NightShiftError, match="uncommitted changes"):
        nightshift.run_nightshift(repo, config(), FakeClient([]), SilentUI(), approve=lambda c: True)


def test_the_cli_will_not_run_the_night_shift_in_ask_mode(repo: Path, capsys):
    from ghostpatch import cli

    assert cli.main(["nightshift", "--repo", str(repo), "--approve", "ask"]) == 2
    assert "runs unattended" in capsys.readouterr().out
