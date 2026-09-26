"""GitHub integration, tested against a real local git repository and a fake `gh`."""

import json
import subprocess
from pathlib import Path

import pytest

from ghostpatch import github, history


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


class FakeGh:
    """Stands in for the GitHub CLI and records every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, *args, cwd=None):
        self.calls.append(list(args))
        if args[:2] == ("issue", "view"):
            return json.dumps({
                "title": "Coupon charges $0.00", "url": "https://github.com/me/shop/issues/12",
                "body": "SAVE10 makes the total zero.", "comments": [{"body": "Still happens on v2."}],
            })
        if args[:2] == ("pr", "create"):
            return "Creating pull request...\nhttps://github.com/me/shop/pull/7"
        return ""


@pytest.fixture
def fake_gh(monkeypatch):
    gh = FakeGh()
    monkeypatch.setattr(github, "run_gh", gh)
    return gh


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo whose `origin` looks like GitHub but pushes to a local bare repository."""
    remote = tmp_path / "remote.git"
    work = tmp_path / "shop"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    work.mkdir()
    git(work, "init", "-q", "-b", "main")
    git(work, "config", "user.name", "Test")
    git(work, "config", "user.email", "test@example.com")
    (work / "pricing.py").write_text("def discount(p, pct):\n    return p - p * pct / 10\n", encoding="utf-8")
    (work / "notes.txt").write_text("todo\n", encoding="utf-8")
    git(work, "add", ".")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "remote", "add", "origin", "https://github.com/me/shop.git")
    git(work, "remote", "set-url", "--push", "origin", str(remote))
    return work


def record_fix(repo: Path, issue_ref=None) -> dict:
    before = (repo / "pricing.py").read_text(encoding="utf-8")
    (repo / "pricing.py").write_text(before.replace("/ 10", "/ 100"), encoding="utf-8")
    (repo / "test_pricing.py").write_text("def test_discount():\n    pass\n", encoding="utf-8")
    run_id = history.save_run(
        repo, issue="coupon bug", provider="groq", model="qwen", fixed=True, summary="Divided by 10 instead of 100.",
        steps=5, prompt_tokens=1, completion_tokens=1, changed_files={"pricing.py", "test_pricing.py"},
        originals={"pricing.py": before, "test_pricing.py": None}, issue_ref=issue_ref,
    )
    return history.load_run(repo, run_id)


def test_parse_issue_urls():
    assert github.parse_issue_url("https://github.com/me/shop/issues/12") == ("me", "shop", 12)
    assert github.parse_issue_url("  https://github.com/a-b/c.d/issues/3#issuecomment-1 ") == ("a-b", "c.d", 3)
    assert github.parse_issue_url("fix the coupon bug") is None
    assert github.parse_issue_url("https://github.com/me/shop/pull/12") is None


def test_origin_slug(repo: Path, tmp_path: Path):
    assert github.origin_slug(repo) == "me/shop"
    git(repo, "remote", "set-url", "origin", "git@github.com:someone/other-repo.git")
    assert github.origin_slug(repo) == "someone/other-repo"
    assert github.origin_slug(tmp_path) is None  # not a git repository


def test_fetch_issue_through_gh(fake_gh):
    issue = github.resolve_issue("https://github.com/me/shop/issues/12")
    assert fake_gh.calls[0][:3] == ["issue", "view", "12"]
    prompt = issue.as_prompt()
    assert "GitHub issue #12 in me/shop: Coupon charges $0.00" in prompt
    assert "SAVE10 makes the total zero." in prompt and "Still happens on v2." in prompt
    assert issue.as_record() == {"repo": "me/shop", "number": 12, "title": "Coupon charges $0.00",
                                 "url": "https://github.com/me/shop/issues/12"}


def test_open_pull_request_commits_only_the_runs_files(repo: Path, fake_gh):
    run = record_fix(repo, issue_ref={"repo": "me/shop", "number": 12, "title": "Coupon charges $0.00",
                                      "url": "https://github.com/me/shop/issues/12"})
    (repo / "notes.txt").write_text("my own unrelated edit\n", encoding="utf-8")

    url = github.open_pull_request(repo, run)

    assert url == "https://github.com/me/shop/pull/7"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "ghostpatch/issue-12-coupon-charges-0-00"
    committed = git(repo, "show", "--name-only", "--format=%s", "HEAD").splitlines()
    assert committed[0] == "Fix #12: Coupon charges $0.00"
    assert sorted(committed[2:]) == ["pricing.py", "test_pricing.py"]
    assert "notes.txt" in git(repo, "status", "--short")  # the user's own edit stays uncommitted
    pushed = git(repo.parent / "remote.git", "branch", "--list")  # origin pushes to this local bare repo
    assert "ghostpatch/issue-12-coupon-charges-0-00" in pushed

    create = next(c for c in fake_gh.calls if c[:2] == ["pr", "create"])
    body = create[create.index("--body") + 1]
    assert create[create.index("--base") + 1] == "main"
    assert "Fixes #12" in body and "`test_pricing.py` (new file)" in body


def test_open_pull_request_safety_checks(repo: Path, fake_gh, tmp_path: Path):
    run = record_fix(repo)
    (repo / "pricing.py").write_text("edited again\n", encoding="utf-8")
    with pytest.raises(github.GitHubError, match="changed after the run"):
        github.open_pull_request(repo, run)

    run = record_fix(repo)
    (repo / "notes.txt").write_text("staged\n", encoding="utf-8")
    git(repo, "add", "notes.txt")
    with pytest.raises(github.GitHubError, match="Other changes are staged"):
        github.open_pull_request(repo, run)

    with pytest.raises(github.GitHubError, match="undone"):
        github.open_pull_request(repo, {**run, "undone": True})
    with pytest.raises(github.GitHubError, match="not a git repository"):
        github.open_pull_request(tmp_path / "nowhere", run)


def test_pr_title_without_an_issue(repo: Path):
    run = record_fix(repo)
    assert github.pr_title(run) == "GhostPatch: Divided by 10 instead of 100."
    assert "Fixes #" not in github.pr_body(run)
