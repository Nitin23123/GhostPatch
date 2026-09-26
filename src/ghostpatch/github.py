"""GitHub integration: fix an issue straight from its link, then open a pull request.

Everything goes through the GitHub CLI (`gh`), so GhostPatch never handles a GitHub token
itself: it uses whatever account `gh auth login` set up. Issues of public repositories can
also be read without `gh`, through the public API.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

from ghostpatch.gitutil import git, is_git_repo

ISSUE_URL_RE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/issues/(\d+)")
REMOTE_RE = re.compile(r"github\.com[:/]([\w.-]+)/([\w.-]+?)(?:\.git)?/?$")
MAX_COMMENTS = 8
MAX_ISSUE_CHARS = 12_000


class GitHubError(Exception):
    """Something the user can fix (gh not installed, not logged in, no remote, ...)."""


@dataclass
class Issue:
    owner: str
    repo: str
    number: int
    title: str
    body: str
    url: str
    comments: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return f"{self.owner}/{self.repo}"

    def as_prompt(self) -> str:
        text = f"GitHub issue #{self.number} in {self.slug}: {self.title}\n\n{self.body.strip() or '(no description)'}"
        if self.comments:
            text += "\n\nComments on the issue:\n" + "\n---\n".join(c.strip() for c in self.comments[-MAX_COMMENTS:])
        return text[:MAX_ISSUE_CHARS]

    def as_record(self) -> dict:
        return {"repo": self.slug, "number": self.number, "title": self.title, "url": self.url}


# ------------------------------------------------------------------------------ gh


def run_gh(*args: str, cwd: Path | None = None) -> str:
    """Run the GitHub CLI. Tests replace this function."""
    if not shutil.which("gh"):
        raise GitHubError("The GitHub CLI (gh) is not installed. Get it from https://cli.github.com, "
                          "then run `gh auth login`.")
    proc = subprocess.run(["gh", *args], cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        message = (proc.stderr or proc.stdout).strip()
        if "auth login" in message or "not logged" in message.lower():
            message = "You're not logged in to GitHub. Run `gh auth login` first."
        raise GitHubError(message)
    return proc.stdout.strip()


# -------------------------------------------------------------------------- issues


def parse_issue_url(text: str) -> tuple[str, str, int] | None:
    """('owner', 'repo', 42) if the text is (or starts with) a GitHub issue link."""
    match = ISSUE_URL_RE.match(text.strip())
    return (match.group(1), match.group(2), int(match.group(3))) if match else None


def origin_slug(repo: Path) -> str | None:
    """'owner/repo' of the repository's GitHub `origin` remote, if there is one."""
    if not is_git_repo(repo):
        return None
    url = git(repo, "remote", "get-url", "origin", check=False)
    match = REMOTE_RE.search(url)
    return f"{match.group(1)}/{match.group(2)}" if match else None


def fetch_issue(owner: str, repo: str, number: int) -> Issue:
    url = f"https://github.com/{owner}/{repo}/issues/{number}"
    try:
        data = json.loads(run_gh("issue", "view", str(number), "--repo", f"{owner}/{repo}",
                                 "--json", "title,body,url,comments"))
        comments = [c.get("body", "") for c in data.get("comments", [])]
        return Issue(owner, repo, number, data["title"], data.get("body") or "", data.get("url", url), comments)
    except GitHubError as gh_error:
        if "not installed" not in str(gh_error):
            raise
    # No gh: public repositories can still be read through the public API.
    try:
        api = f"https://api.github.com/repos/{owner}/{repo}/issues/{number}"
        request = urllib.request.Request(api, headers={"Accept": "application/vnd.github+json",
                                                       "User-Agent": "ghostpatch"})
        with urllib.request.urlopen(request, timeout=15) as response:
            data = json.loads(response.read())
    except urllib.error.HTTPError as e:
        raise GitHubError(f"Could not read {url} (HTTP {e.code}). Is the repository private? "
                          "Install the GitHub CLI and run `gh auth login`.") from e
    except urllib.error.URLError as e:
        raise GitHubError(f"Could not reach GitHub: {e.reason}") from e
    return Issue(owner, repo, number, data["title"], data.get("body") or "", data.get("html_url", url))


def resolve_issue(text: str) -> Issue | None:
    """Fetch the issue if `text` is a GitHub issue link, otherwise return None."""
    ref = parse_issue_url(text)
    return fetch_issue(*ref) if ref else None


def list_issues(slug: str, label: str, limit: int = 10) -> list[dict]:
    """Open issues with a label, oldest first: [{number, title, url}]."""
    data = json.loads(run_gh("issue", "list", "--repo", slug, "--label", label, "--state", "open",
                             "--json", "number,title,url", "--limit", str(limit)) or "[]")
    return sorted(data, key=lambda issue: issue["number"])


def issues_with_open_prs(slug: str) -> set[int]:
    """Issue numbers that already have an open GhostPatch pull request (from its branch names)."""
    data = json.loads(run_gh("pr", "list", "--repo", slug, "--state", "open", "--json", "headRefName",
                             "--limit", "200") or "[]")
    numbers = set()
    for pr in data:
        match = re.match(r"ghostpatch/issue-(\d+)-", pr.get("headRefName", ""))
        if match:
            numbers.add(int(match.group(1)))
    return numbers


def comment_on_issue(slug: str, number: int, body: str) -> None:
    run_gh("issue", "comment", str(number), "--repo", slug, "--body", body)


# --------------------------------------------------------------------- pull requests


def _slugify(text: str, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return words[:limit].rstrip("-") or "fix"


def pr_title(run: dict) -> str:
    issue = run.get("issue_ref")
    if issue:
        return f"Fix #{issue['number']}: {issue['title']}"[:100]
    first = next((line.strip() for line in (run.get("summary") or run.get("issue") or "").splitlines() if line.strip()), "")
    return f"GhostPatch: {first.lstrip('#').strip()}"[:100] if first else "GhostPatch fix"


def pr_body(run: dict) -> str:
    issue = run.get("issue_ref")
    files = "\n".join(f"- `{f['path']}`" + (" (new file)" if f["before"] is None else "") for f in run["files"])
    parts = [
        "## Summary", run.get("summary") or "(no summary)",
        "## Changed files", files,
    ]
    proof = run.get("proof") or {}
    if proof.get("status") == "proven":
        parts += ["## 🔴→🟢 Proof",
                  "GhostPatch ran the new tests itself, with the fix taken out and then put back:\n\n"
                  f"- 🔴 without the fix: `{proof.get('red') or 'failed'}`\n"
                  f"- 🟢 with the fix: `{proof.get('green') or 'passed'}`\n\n"
                  "Tests: " + ", ".join(f"`{t}`" for t in proof.get("tests", []))]
    elif proof.get("summary"):
        parts += ["## Proof", proof["summary"]]
    confidence = (run.get("confidence") or {}).get("summary")
    if confidence:
        parts += ["## Confidence", confidence]
    tournament = run.get("tournament") or {}
    if tournament.get("candidates"):
        rows = "\n".join(
            f"| {'🏆 ' if c['winner'] else ''}#{c['number']} {c['label']} | {c.get('proof') or '–'} | "
            f"{c.get('rivals') or '–'} | {c['changed_lines']} | {c['disqualified'] or c['points']} |"
            for c in tournament["candidates"])
        parts += ["## Tournament", "Several independent fixes competed; this one won on evidence.\n\n"
                  "| Candidate | Proof | Rival tests passed | Lines changed | Points |\n|---|---|---|---|---|\n" + rows]
    parts += [
        "## How this was made",
        f"GhostPatch fixed this autonomously in {run.get('steps', '?')} steps with `{run.get('model')}` "
        f"({run.get('provider')}), and verified it with the project's tests. Please review before merging.",
    ]
    if issue:
        parts.append(f"Fixes #{issue['number']}")
    parts.append("---\n👻 Opened by [GhostPatch](https://github.com/Nitin23123/GhostPatch)")
    return "\n\n".join(parts)


def open_pull_request(repo: Path, run: dict, draft: bool = False) -> str:
    """Put a run's changes on a new branch, push it and open a pull request. Returns the PR's URL."""
    if not is_git_repo(repo):
        raise GitHubError("This folder is not a git repository.")
    if run.get("undone"):
        raise GitHubError(f"Run {run['id']} was undone, so there is nothing to put in a pull request.")
    if run.get("pr_url"):
        raise GitHubError(f"Run {run['id']} already has a pull request: {run['pr_url']}")
    if not run["files"]:
        raise GitHubError(f"Run {run['id']} didn't change any files.")
    slug = origin_slug(repo)
    if slug is None:
        raise GitHubError("This repository has no GitHub `origin` remote to push to.")

    from ghostpatch.history import _read  # same newline-preserving reader undo uses

    changed_since = [f["path"] for f in run["files"] if _read(repo / f["path"]) != f["after"]]
    if changed_since:
        raise GitHubError("These files changed after the run, so the pull request wouldn't match it: "
                          + ", ".join(changed_since))

    root = Path(git(repo, "rev-parse", "--show-toplevel"))
    paths = [str((repo / f["path"]).resolve().relative_to(root.resolve()).as_posix()) for f in run["files"]]
    staged_elsewhere = [p for p in git(root, "diff", "--cached", "--name-only").splitlines() if p not in paths]
    if staged_elsewhere:
        raise GitHubError("Other changes are staged for commit (" + ", ".join(staged_elsewhere[:5])
                          + "). Commit or unstage them first, so they don't end up in the pull request.")

    base = git(root, "rev-parse", "--abbrev-ref", "HEAD")
    issue = run.get("issue_ref")
    branch = "ghostpatch/" + (f"issue-{issue['number']}-" if issue else "") + _slugify(issue["title"] if issue else pr_title(run))
    candidate, n = branch, 2
    while git(root, "rev-parse", "--verify", "--quiet", f"refs/heads/{candidate}", check=False):
        candidate, n = f"{branch}-{n}", n + 1
    branch = candidate

    git(root, "switch", "-c", branch)
    try:
        git(root, "add", "--", *paths)
        title = pr_title(run)
        git(root, "commit", "-m", title, "-m", (run.get("summary") or "") + (f"\n\nFixes #{issue['number']}" if issue else ""),
            "--", *paths)
        git(root, "push", "-u", "origin", branch)
        args = ["pr", "create", "--repo", slug, "--head", branch, "--title", title, "--body", pr_body(run)]
        if base and base != "HEAD":
            args += ["--base", base]
        if draft:
            args.append("--draft")
        output = run_gh(*args, cwd=root)
    except (RuntimeError, GitHubError) as e:
        raise GitHubError(f"{e}\nYour changes are committed on branch '{branch}'.") from e
    urls = re.findall(r"https://github\.com/\S+/pull/\d+", output)
    return urls[-1] if urls else output.splitlines()[-1]
