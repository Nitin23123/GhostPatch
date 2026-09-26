"""🌙 Night shift: work through the issue queue while you sleep, and leave a morning report.

`ghostpatch nightshift` is meant to run unattended, e.g. from the GitHub Action every night:

1. It collects the open issues with a label (`ghostpatch` by default) that don't already have a
   GhostPatch pull request.
2. It fixes them one at a time. Only verified fixes become pull requests: the fix must pass its
   own red→green proof and the tests after its last edit. A fix that isn't proven gets a *draft*
   pull request, and a failed attempt is rolled back so the next issue starts clean.
3. Optionally it haunts the riskiest code for bugs nobody has reported, fixes the confirmed ones,
   and opens pull requests that include the haunter's failing test as proof.
4. It writes a morning report: what's ready for review, what needs a human, and what haunting found.

It needs a git repository with a GitHub `origin`, the GitHub CLI (`gh`) and no uncommitted
changes to tracked files. After each pull request it goes back to the branch it started on.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ghostpatch import github, history
from ghostpatch.gitutil import git, is_git_repo
from ghostpatch.proof import _put

DEFAULT_LABEL = "ghostpatch"
REPORTS_DIR = Path(".ghostpatch") / "reports"


class NightShiftError(Exception):
    """A setup problem that stops the night shift before it starts."""


@dataclass
class Item:
    kind: str  # issue | haunt
    title: str
    status: str  # pr | needs_you | not_attempted | suspected
    number: int | None = None
    url: str | None = None
    pr_url: str | None = None
    draft: bool = False
    reason: str = ""
    run_id: str | None = None
    confidence: int | None = None
    proof: str | None = None


@dataclass
class NightReport:
    repo: str
    started: str
    items: list[Item] = field(default_factory=list)
    finished: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    stopped: str | None = None
    haunt: str | None = None  # the haunt summary, if haunting ran
    path: str | None = None  # where the report was saved

    def of(self, status: str) -> list[Item]:
        return [i for i in self.items if i.status == status]

    def markdown(self) -> str:
        prs, needs = self.of("pr"), self.of("needs_you")
        found = [i for i in self.items if i.kind == "haunt"]
        out = [f"# 🌙 Night shift report: {self.repo}", f"{self.started} → {self.finished}", "",
               f"**✅ {len(prs)} ready for review · ⚠ {len(needs)} need you · 🐛 {len(found)} found by haunting**"]

        def line(i: Item) -> str:
            ref = f"#{i.number} " if i.number else ""
            return f"- {ref}{i.title}"

        if prs:
            out += ["", "## Ready for review"]
            for i in prs:
                extras = [f"confidence {i.confidence}" if i.confidence is not None else "",
                          "🔴→🟢 proven" if i.proof == "proven" else "", "draft: not proven" if i.draft else ""]
                out.append(f"{line(i)} → [pull request]({i.pr_url})" + "".join(f" · {e}" for e in extras if e))
        if needs:
            out += ["", "## Needs you"] + [f"{line(i)}: {i.reason}" + (f" (run {i.run_id})" if i.run_id else "")
                                           for i in needs]
        suspected = self.of("suspected")
        if self.haunt:
            out += ["", "## Haunting", self.haunt]
            out += [f"{line(i)}: {i.reason}" for i in suspected]
        skipped = self.of("not_attempted")
        if skipped:
            out += ["", "## Not attempted"] + [f"{line(i)}: {i.reason}" for i in skipped]
        out += ["", "---", f"{self.prompt_tokens + self.completion_tokens:,} tokens · 👻 "
                "[GhostPatch](https://github.com/Nitin23123/GhostPatch) night shift"]
        return "\n".join(out)

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "markdown": self.markdown()}


def preflight(repo: Path) -> tuple[str, str]:
    """Check the night shift can run. Returns (owner/repo, the branch to come back to)."""
    if not is_git_repo(repo):
        raise NightShiftError("The night shift needs a git repository.")
    slug = github.origin_slug(repo)
    if slug is None:
        raise NightShiftError("The night shift needs a GitHub `origin` remote to open pull requests on.")
    github.run_gh("auth", "status")
    dirty = git(repo, "status", "--porcelain", "--untracked-files=no", check=False)
    if dirty:
        raise NightShiftError("There are uncommitted changes, which would get mixed into the fixes. "
                              "Commit or stash them first:\n" + dirty)
    base = git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    if not base or base == "HEAD":
        raise NightShiftError("Check out a branch first (the night shift opens pull requests against it).")
    if os.environ.get("GITHUB_ACTIONS") and not git(repo, "config", "user.email", check=False):
        git(repo, "config", "user.name", "GhostPatch")
        git(repo, "config", "user.email", "ghostpatch@users.noreply.github.com")
    return slug, base


def run_nightshift(
    repo: Path, config: Any, client: Any, ui: Any, *, approve: Callable[[str], bool],
    label: str = DEFAULT_LABEL, limit: int = 5, haunt_targets: int = 0, poltergeist: int = 0,
    candidates: int = 1, draft: bool = False, comment: bool = False, max_steps: int = 30,
    say: Callable[[str], None] | None = None,
) -> NightReport:
    from ghostpatch.graph import CodeGraph
    from ghostpatch.session import run_session

    say = say or ui.thought
    slug, base = preflight(repo)
    owner, name = slug.split("/", 1)
    report = NightReport(slug, time.strftime("%Y-%m-%d %H:%M"))
    queue = github.list_issues(slug, label, limit=max(1, limit) * 3)
    already = github.issues_with_open_prs(slug)
    queue = [i for i in queue if i["number"] not in already][: max(0, limit)]
    say(f"🌙 Night shift on {slug}: {len(queue)} issue(s) labelled '{label}'"
        + (f", then haunting {haunt_targets} function(s)" if haunt_targets else "") + ".")

    def fix(issue_text: str, **kwargs: Any) -> Any:
        graph = CodeGraph(repo)
        try:
            outcome = run_session(repo, config, client, ui, issue_text, graph=graph, approve_command=approve,
                                  max_steps=max_steps, poltergeist=poltergeist, candidates=candidates, **kwargs)
        finally:
            graph.close()
        if outcome.result is not None:
            report.prompt_tokens += outcome.result.prompt_tokens
            report.completion_tokens += outcome.result.completion_tokens
        if outcome.quota_exhausted:
            report.stopped = outcome.error or "the model provider's quota ran out"
        return outcome

    for issue in queue:
        item = Item("issue", issue["title"], "needs_you", number=issue["number"], url=issue.get("url"))
        report.items.append(item)
        if report.stopped:
            item.status, item.reason = "not_attempted", f"stopped: {report.stopped}"
            continue
        say(f"🌙 Issue #{issue['number']}: {issue['title']}")
        gh_issue = github.fetch_issue(owner, name, issue["number"])
        outcome = fix(gh_issue.as_prompt(), issue_ref=gh_issue.as_record())
        _deliver(repo, base, outcome, item, draft)
        if comment and item.status == "needs_you":
            github.comment_on_issue(slug, issue["number"], f"👻 GhostPatch tried this during its night shift but "
                                    f"couldn't verify a fix: {item.reason}")

    if haunt_targets and not report.stopped:
        from ghostpatch.haunt import haunt
        from ghostpatch.session import describe_model_error

        graph = CodeGraph(repo)
        try:
            found = haunt(repo, config, client, ui, graph=graph, targets=haunt_targets, approve_command=approve,
                          max_steps=20, describe_error=lambda e: describe_model_error(e, client, config))
        finally:
            graph.close()
        report.prompt_tokens += found.prompt_tokens
        report.completion_tokens += found.completion_tokens
        report.haunt = found.summary
        report.stopped = report.stopped or found.error
        for finding in found.findings:
            if not finding.is_bug:
                continue
            tests = {t: found.kept.get(t) for t in finding.tests}
            title = f"🐛 `{finding.target.qualname}`: {(finding.claim or 'a bug').splitlines()[0]}"
            item = Item("haunt", title, "suspected", reason=finding.verdict or finding.claim)
            report.items.append(item)
            if finding.status == "confirmed" and not report.stopped:
                outcome = fix(finding.as_issue(), proof_tests=finding.tests)
                _deliver(repo, base, outcome, item, draft, extra_files=tests)
            else:
                _put(repo, tests)  # a suspicion is reported, not left in the working tree
        if found.run_id:
            history.update_run(repo, found.run_id, undone=True)  # its tests were shipped or removed above

    report.finished = time.strftime("%Y-%m-%d %H:%M")
    _save(repo, report)
    return report


def _deliver(repo: Path, base: str, outcome: Any, item: Item, draft: bool,
             extra_files: dict[str, str | None] | None = None) -> None:
    """Open a pull request for a verified fix, or roll the attempt back. Always ends on `base`."""
    proof = outcome.proof.status if outcome.proof else None
    item.run_id, item.proof = outcome.run_id, proof
    item.confidence = outcome.confidence.get("score") if outcome.confidence else None
    verified = (outcome.fixed and proof != "not_green"
                and (outcome.confidence or {}).get("tests_after_edit") != "failed")
    if verified and outcome.run_id:
        try:
            if extra_files:
                history.adopt_files(repo, outcome.run_id, extra_files)
            item.draft = draft or proof != "proven"
            item.pr_url = github.open_pull_request(repo, history.load_run(repo, outcome.run_id), draft=item.draft)
            history.update_run(repo, outcome.run_id, pr_url=item.pr_url)
            item.status = "pr"
        except (github.GitHubError, history.UndoError, RuntimeError) as e:
            item.reason = f"the fix looked good, but the pull request failed: {str(e).splitlines()[0]}"
        finally:
            if git(repo, "rev-parse", "--abbrev-ref", "HEAD", check=False) != base:
                git(repo, "switch", base)
        if item.status == "pr":
            return
    else:
        first = ((outcome.result.summary if outcome.result else "") or "").strip().splitlines()
        item.reason = outcome.error or ("the fix wasn't verified" + (f": {first[0]}" if first else ""))
    if outcome.run_id:
        try:
            history.undo_run(repo, outcome.run_id, force=True)  # leave a clean tree for the next issue
        except history.UndoError:
            pass
    if extra_files:
        _put(repo, extra_files)


def _save(repo: Path, report: NightReport) -> None:
    folder = repo / REPORTS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    stem = folder / f"nightshift-{time.strftime('%Y%m%d-%H%M%S')}"
    stem.with_suffix(".md").write_text(report.markdown(), encoding="utf-8")
    report.path = str(stem.with_suffix(".md"))
    stem.with_suffix(".json").write_text(json.dumps(report.as_dict(), indent=1), encoding="utf-8")


def list_reports(repo: Path, limit: int = 20) -> list[dict]:
    """Saved night shift reports, newest first."""
    folder = repo / REPORTS_DIR
    if not folder.is_dir():
        return []
    reports = []
    for path in sorted(folder.glob("nightshift-*.json"), reverse=True)[:limit]:
        try:
            reports.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            continue
    return reports
