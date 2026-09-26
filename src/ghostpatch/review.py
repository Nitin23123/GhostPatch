"""Blast-radius review: what could a change break, and is that covered by tests?

Works on any change, including pull requests written by people:
- `ghostpatch review` reviews your uncommitted changes (against HEAD),
- `ghostpatch review 42` or a pull request link reviews a GitHub pull request, analysed at the
  pull request's own version of the code (fetched from GitHub, your working folder is untouched).

The review maps the changed lines to functions with the code graph, walks up the call graph
to everything those functions affect, and flags affected code that no test reaches.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from ghostpatch.gitutil import git, is_git_repo
from ghostpatch.parsers import is_test_path, language_of

HUNK_RE = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")
PR_URL_RE = re.compile(r"https?://github\.com/([\w.-]+)/([\w.-]+)/pull/(\d+)")
MAX_LISTED = 25


class ReviewError(Exception):
    pass


def changed_lines(diff: str) -> dict[str, set[int]]:
    """{path: new-file line numbers touched by the diff}. Deletions count at the line where they happened."""
    out: dict[str, set[int]] = {}
    path, line = None, 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            path = None if target == "/dev/null" else target.removeprefix("b/")
            if path is not None:
                out.setdefault(path, set())
            continue
        if raw.startswith("--- ") or path is None:
            continue
        match = HUNK_RE.match(raw)
        if match:
            line = int(match.group(1))
            continue
        if raw.startswith("+"):
            out[path].add(line)
            line += 1
        elif raw.startswith("-"):
            out[path].add(max(line, 1))
        elif raw.startswith(" "):
            line += 1
    return out


def analyse(folder: Path, diff: str, prefix: str = "") -> dict[str, Any]:
    """Review `diff` against the code in `folder`. `prefix` is folder's path inside the git root."""
    from ghostpatch.graph import CodeGraph

    lines = changed_lines(diff)
    graph = CodeGraph(folder, db_path=":memory:")
    try:
        graph.refresh()
        changed: dict[str, dict] = {}
        tests_changed, other_files = [], []
        for git_path, numbers in lines.items():
            if prefix and not git_path.startswith(prefix + "/"):
                continue
            rel = git_path[len(prefix) + 1:] if prefix else git_path
            if is_test_path(rel):
                tests_changed.append(rel)
                continue
            if language_of(rel) is None:
                other_files.append(rel)
                continue
            for number in sorted(numbers):
                symbol = graph.symbol_at(rel, number)
                if symbol:
                    changed[symbol[1]] = {"name": symbol[0], "path": rel}
        radius = graph.blast_radius(list(changed)) if changed else {}
    finally:
        graph.close()

    affected = {q: info for q, info in radius.items() if q not in changed}
    untested = sorted(q for q, info in radius.items() if not info["tested"])
    risk = ("high" if len(untested) >= 3 or (changed and all(q in untested for q in changed))
            else "medium" if untested else "low")
    return {
        "changed": [{"qualname": q, **info, "tested": radius.get(q, {}).get("tested", False)} for q, info in sorted(changed.items())],
        "affected": [{"qualname": q, **info} for q, info in sorted(affected.items())],
        "untested": untested,
        "tests_changed": sorted(tests_changed),
        "other_files": sorted(other_files),
        "risk": risk,
    }


def to_markdown(report: dict[str, Any], title: str) -> str:
    icon = {"low": "🟢", "medium": "🟡", "high": "🔴"}[report["risk"]]
    lines = [f"## 👻 GhostPatch blast-radius review: {title}", "",
             f"**Risk: {icon} {report['risk']}** · {len(report['changed'])} function(s) changed · "
             f"{len(report['affected'])} more affected · {len(report['untested'])} not reached by any test", ""]
    if report["changed"]:
        lines += ["### Changed", *[f"- `{c['qualname']}` ({c['path']}){'' if c['tested'] else ' ⚠ untested'}"
                                  for c in report["changed"][:MAX_LISTED]], ""]
    if report["affected"]:
        lines += ["### Could also be affected", *[f"- `{a['qualname']}` ({a['path']}){'' if a['tested'] else ' ⚠ untested'}"
                                                 for a in report["affected"][:MAX_LISTED]], ""]
    if report["untested"]:
        lines += ["### Suggested", "Add tests that exercise: " + ", ".join(f"`{q}`" for q in report["untested"][:10]) + ".", ""]
    if report["tests_changed"]:
        lines += ["Tests touched by this change: " + ", ".join(f"`{t}`" for t in report["tests_changed"]), ""]
    if not report["changed"]:
        lines += ["No changed functions in Python, JavaScript or TypeScript code were found.", ""]
    lines.append("<sub>Generated from the code graph by [GhostPatch](https://github.com/Nitin23123/GhostPatch). "
                 "Calls are matched by name, so treat this as a guide.</sub>")
    return "\n".join(lines)


def review_working_tree(repo: Path) -> dict[str, Any]:
    if not is_git_repo(repo):
        raise ReviewError("Reviewing local changes needs a git repository.")
    root = Path(git(repo, "rev-parse", "--show-toplevel"))
    prefix = repo.resolve().relative_to(root.resolve()).as_posix()
    prefix = "" if prefix == "." else prefix
    diff = git(root, "diff", "HEAD", "--", prefix or ".")
    untracked = git(root, "ls-files", "--others", "--exclude-standard", "--", prefix or ".").splitlines()
    for path in untracked:  # new files count as fully changed
        text = (root / path).read_text(encoding="utf-8", errors="replace")
        count = max(1, len(text.splitlines()))
        diff += f"\n--- /dev/null\n+++ b/{path}\n@@ -0,0 +1,{count} @@\n" + "".join(f"+{l}\n" for l in text.splitlines())
    report = analyse(repo, diff, prefix)
    report["markdown"] = to_markdown(report, "uncommitted changes")
    return report


def review_pull_request(repo: Path, pr: str) -> dict[str, Any]:
    from ghostpatch import github
    from ghostpatch.timelapse import extract_commit

    match = PR_URL_RE.match(pr.strip())
    if match:
        slug, number = f"{match.group(1)}/{match.group(2)}", match.group(3)
    else:
        number, slug = pr.strip().lstrip("#"), github.origin_slug(repo)
        if not number.isdigit() or slug is None:
            raise ReviewError("Give a pull request number (in a repository cloned from GitHub) or a pull request link.")
    diff = github.run_gh("pr", "diff", number, "--repo", slug)
    title = github.run_gh("pr", "view", number, "--repo", slug, "--json", "title", "--jq", ".title")

    # Analyse the pull request's own version of the code, without touching the working folder.
    root = Path(git(repo, "rev-parse", "--show-toplevel")) if is_git_repo(repo) else None
    if root is not None and github.origin_slug(repo) == slug:
        subprocess.run(["git", "fetch", "-q", "origin", f"pull/{number}/head"], cwd=root, capture_output=True)
        prefix = repo.resolve().relative_to(root.resolve()).as_posix()
        prefix = "" if prefix == "." else prefix
        with tempfile.TemporaryDirectory(prefix="ghostpatch-review-", ignore_cleanup_errors=True) as tmp:
            folder = extract_commit(root, "FETCH_HEAD", prefix, Path(tmp))
            report = analyse(folder, diff, prefix)
    else:
        report = analyse(repo, diff)  # best effort: the local checkout stands in for the PR's code
    report.update(pr=number, repo=slug, title=title, markdown=to_markdown(report, f"#{number} {title}"))
    return report


def post_review(report: dict[str, Any]) -> None:
    from ghostpatch import github

    github.run_gh("pr", "comment", report["pr"], "--repo", report["repo"], "--body", report["markdown"])
