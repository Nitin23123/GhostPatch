"""👻 Haunt mode: find the bugs nobody has reported yet.

Everything else in GhostPatch starts from a bug report. Haunting starts from nothing:

1. **Rank the code by risk**, from the code graph and git history: functions called from many
   places, functions no test reaches, files that changed often lately, and longer functions.
2. **Haunt the riskiest few.** For each, a haunter (like the poltergeist, it may only write
   tests) reads the function, its callers and its docs, then writes tests for what the code is
   clearly *meant* to do, hunting for inputs where it doesn't.
3. **GhostPatch runs those tests itself.** If they pass there is no bug, and the tests are
   removed (or kept as extra coverage with `keep_tests`).
4. **A failing test is only a suspicion.** A skeptic (one more model call that sees the function,
   the test and the failure, and has no stake in the claim) must agree that the test's
   expectation follows from the code's intent before a bug counts as confirmed. The failing
   tests of real bugs stay in the repository as proof, ready for `ghostpatch fix`.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from ghostpatch import history
from ghostpatch.agent import Agent
from ghostpatch.cifix import headline, run_tests, runner_missing, test_files_command
from ghostpatch.parsers import is_test_path
from ghostpatch.proof import _put, _read
from ghostpatch.replay import RecordingUI
from ghostpatch.tools import Workspace

DEFAULT_TARGETS = 3
MAX_SOURCE_LINES = 120
MAX_CONTEXT_CHARS = 3000
CHURN_COMMITS = 200

HAUNT_PROMPT = """You are the Haunter, GhostPatch's bug hunter. Nobody has reported a bug. Your job is to find
out whether one function has one.

1. Read the function, its docstring and comments, its callers and any existing tests, to learn what it is MEANT to do.
2. Think of valid inputs where it might not do that: boundaries (0, 1, -1, empty, None/null, very large),
   rounding, off-by-one, unusual but valid combinations, and the ways its callers actually use it.
3. Write ONE new test file (you may only create or edit test files) with a few small, focused tests of the
   intended behaviour. Put it where the project keeps its tests and match their style, e.g.
   tests/test_haunt_<function>.py or tests/<function>.haunt.test.ts.
4. Run it with the project's test command.
5. Finish:
   - fixed=true ONLY if a test fails because the code is really wrong. Summary: first line names the bug;
     then which input breaks it, what it should give and what it gives.
   - fixed=false if everything passes. Summarise what you checked.
{graph_guide}
Rules:
- Only test behaviour the code clearly intends: its name, docstring, comments, callers or existing tests must
  support every expected value. Never invent requirements. Validation that no caller needs, style and
  performance are not bugs.
- Never modify non-test files. Keep tests deterministic: no network, clock or randomness.
- Shell commands run on {os} with the repository root as the working directory.{shell_hint}
- Keep your thinking short. Always act through tool calls, and you MUST call the `finish` tool.
"""

SKEPTIC_PROMPT = """You are a skeptical senior engineer reviewing a claimed bug. A bug hunter wrote a test that fails
against a function. Tests can be wrong: decide whether the TEST's expectation really follows from what the
function is clearly meant to do (its name, docstring, comments, how its callers use it, existing tests), or
whether the hunter invented a requirement.

Answer with JSON only, no other text:
{"verdict": "real_bug" | "test_is_wrong" | "unsure", "reason": "one sentence"}"""


@dataclass
class Target:
    qualname: str
    name: str
    path: str
    line: int
    end_line: int
    signature: str
    callers: int
    tested: bool
    churn: int
    risk: float
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Finding:
    target: Target
    status: str  # confirmed | suspected | false_alarm | clean | inconclusive | error
    claim: str = ""
    tests: list[str] = field(default_factory=list)
    failure: str = ""  # headline of the failing run
    output: str = ""
    verdict: str = ""  # the skeptic's reason
    steps: int = 0

    @property
    def is_bug(self) -> bool:
        return self.status in ("confirmed", "suspected")

    def as_issue(self) -> str:
        """A bug report for `ghostpatch fix`."""
        t = self.target
        return (f"Bug found by GhostPatch haunt mode in `{t.qualname}` ({t.path}:{t.line}).\n\n{self.claim}\n\n"
                f"These tests fail and show the bug: {', '.join(self.tests)}\n"
                f"Test result: {self.failure}\n\n"
                "Fix the code so these tests pass. Only change a test if it is clearly wrong about the intended behaviour.")

    def as_dict(self) -> dict[str, Any]:
        return {"target": self.target.as_dict(), "status": self.status, "claim": self.claim, "tests": self.tests,
                "failure": self.failure, "output": self.output, "verdict": self.verdict, "steps": self.steps,
                "issue": self.as_issue() if self.is_bug else None}


@dataclass
class HauntReport:
    findings: list[Finding]
    run_id: str | None = None
    error: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    events: list[dict] = field(default_factory=list)
    kept: dict[str, str | None] = field(default_factory=dict)  # test files left in place -> original content

    @property
    def bugs(self) -> list[Finding]:
        return [f for f in self.findings if f.is_bug]

    @property
    def summary(self) -> str:
        counts = Counter(f.status for f in self.findings)
        plural = lambda n, word: f"{n} {word}{'' if n == 1 else 's'}"  # noqa: E731
        parts = [text for status, text in (
            ("confirmed", plural(counts["confirmed"], "confirmed bug")), ("suspected", f"{counts['suspected']} suspected"),
            ("false_alarm", plural(counts["false_alarm"], "false alarm")), ("clean", f"{counts['clean']} clean"),
            ("inconclusive", f"{counts['inconclusive']} inconclusive"), ("error", plural(counts["error"], "error")),
        ) if counts[status]]
        return f"Haunted {plural(len(self.findings), 'function')}: " + (", ".join(parts) or "nothing to report") + "."

    def as_dict(self) -> dict[str, Any]:
        return {"findings": [f.as_dict() for f in self.findings], "run_id": self.run_id, "error": self.error,
                "summary": self.summary}

    def markdown(self) -> str:
        lines = [f"### 👻 {self.summary}"]
        for f in self.findings:
            t = f.target
            lines.append(f"- **{f.status.replace('_', ' ')}** `{t.qualname}` ({t.path}:{t.line})"
                         + (f": {f.claim.splitlines()[0]}" if f.is_bug and f.claim else ""))
            if f.is_bug:
                lines.append(f"  - proof: {', '.join(f'`{x}`' for x in f.tests)} → {f.failure}")
        return "\n".join(lines)


# --------------------------------------------------------------------------- ranking


def _churn(repo: Path) -> Counter:
    """How many recent commits touched each file (paths relative to `repo`)."""
    from ghostpatch.gitutil import git, is_git_repo

    if not is_git_repo(repo):
        return Counter()
    out = git(repo, "log", f"-n{CHURN_COMMITS}", "--name-only", "--format=", "--relative", "--", ".", check=False)
    return Counter(line.strip() for line in out.splitlines() if line.strip())


def rank_targets(graph: Any, repo: Path, limit: int = 10) -> list[Target]:
    """The riskiest functions first: widely called, untested, often changed, long."""
    graph.refresh()
    reached = graph.names_reached_by_tests()
    callers: Counter = Counter()
    for path, callee in graph.db.execute("SELECT path, callee FROM calls"):
        if not is_test_path(path):
            callers[callee] += 1
    churn = _churn(repo)
    targets = []
    rows = graph.db.execute("SELECT name, qualname, path, line, end_line, signature FROM symbols "
                            "WHERE kind IN ('function', 'method')").fetchall()
    for name, qualname, path, line, end_line, signature in rows:
        lines = end_line - line + 1
        if is_test_path(path) or name.startswith("__") or lines < 2:
            continue
        tested = name in reached
        risk = (2.0 * math.log2(1 + callers[name]) + (0 if tested else 3.0)
                + 1.5 * math.log2(1 + churn[path]) + min(2.0, lines / 20))
        reasons = []
        if callers[name]:
            reasons.append(f"called from {callers[name]} place{'s' if callers[name] != 1 else ''}")
        if not tested:
            reasons.append("no test reaches it")
        if churn[path]:
            reasons.append(f"its file changed in {churn[path]} recent commit{'s' if churn[path] != 1 else ''}")
        if lines >= 30:
            reasons.append(f"{lines} lines long")
        targets.append(Target(qualname, name, path, line, end_line, signature, callers[name], tested,
                              churn[path], round(risk, 2), reasons))
    targets.sort(key=lambda t: (-t.risk, t.path, t.line))
    return targets[:limit]


# --------------------------------------------------------------------------- haunting


def only_tests(rel_path: str) -> str | None:
    if is_test_path(rel_path):
        return None
    return (f"The haunter may only write test files, not {rel_path}. "
            "Put tests in a tests/ folder or name them test_*.py / *.test.ts.")


def brief(target: Target, repo: Path, graph: Any) -> str:
    source = (_read(repo / target.path) or "").splitlines()
    end = min(target.end_line, target.line + MAX_SOURCE_LINES - 1)
    numbered = "\n".join(f"{n:>5} | {source[n - 1]}" for n in range(target.line, end + 1) if n <= len(source))
    parts = [
        f"Function to investigate: {target.qualname} in {target.path}:{target.line}-{target.end_line}",
        f"Signature: {target.signature}",
        "Why it was chosen: " + ("; ".join(target.reasons) or "it is one of the riskiest functions"),
        f"Its source:\n{numbered}",
    ]
    if graph is not None:
        parts.append("Where it is used (from the code graph):\n" + graph.find_callers(target.name)[:MAX_CONTEXT_CHARS])
        parts.append(graph.related_tests(target.name)[:MAX_CONTEXT_CHARS])
    return "\n\n".join(parts)


def skeptic(client: Any, model: str, target: Target, repo: Path, tests: list[str], output: str,
            claim: str) -> tuple[str, str, int, int]:
    """Ask for a second opinion on a failing test. Returns (verdict, reason, prompt_tokens, completion_tokens)."""
    source = "\n".join((_read(repo / target.path) or "").splitlines()[target.line - 1:target.end_line][:MAX_SOURCE_LINES])
    test_text = "\n\n".join(f"# {t}\n{(_read(repo / t) or '')[:MAX_CONTEXT_CHARS]}" for t in tests)
    user = (f"Function `{target.qualname}` ({target.path}):\n```\n{source}\n```\n\n"
            f"The hunter's claim:\n{claim or '(none)'}\n\nThe hunter's tests:\n```\n{test_text}\n```\n\n"
            f"The test run (end of the output):\n```\n{output[-1500:]}\n```")
    response = client.chat.completions.create(
        model=model, messages=[{"role": "system", "content": SKEPTIC_PROMPT}, {"role": "user", "content": user}])
    text = response.choices[0].message.content or ""
    usage = getattr(response, "usage", None)
    tokens = (usage.prompt_tokens, usage.completion_tokens) if usage else (0, 0)
    text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    try:
        data = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        data = {}
    verdict = data.get("verdict") if data.get("verdict") in ("real_bug", "test_is_wrong", "unsure") else "unsure"
    return verdict, str(data.get("reason") or text.strip()[:300]), *tokens


def haunt(
    repo: Path, config: Any, client: Any, ui: Any, *, graph: Any = None, targets: int = DEFAULT_TARGETS,
    only: list[str] | None = None, approve_command: Callable[[str], bool] | None = None, max_steps: int = 20,
    keep_tests: bool = False, save: bool = True, describe_error: Callable[[Exception], str] = str,
) -> HauntReport:
    """Haunt the riskiest functions and report the bugs found, each backed by a failing test."""
    import openai

    from ghostpatch.graph import CodeGraph

    approve = approve_command or ui.approve_command
    recorder = RecordingUI(ui)
    ranking_graph = graph or CodeGraph(repo)
    try:
        ranked = rank_targets(ranking_graph, repo, limit=10_000 if only else max(1, targets))
    finally:
        if graph is None:
            ranking_graph.close()
    if only:
        wanted = set(only)
        ranked = [t for t in ranked if t.qualname in wanted or t.name in wanted][: max(1, targets)]
    report = HauntReport([])
    kept: dict[str, str | None] = {}  # test files left in the repository -> their original content
    recorder.thought(f"_👻 Haunting {len(ranked)} function(s): " + ", ".join(t.qualname for t in ranked) + "_")

    for number, target in enumerate(ranked, 1):
        recorder.thought(f"_👻 {number}/{len(ranked)}: haunting `{target.qualname}` ("
                         + ("; ".join(target.reasons) or "high risk") + ")…_")
        ws = Workspace(repo, approve_command=approve, graph=graph)
        ws.write_guard = only_tests
        finding = Finding(target, "clean")
        try:
            agent = Agent(client, config.model, ws, recorder, max_steps=max_steps, system_prompt=HAUNT_PROMPT,
                          exclude_tools=frozenset({"remember"}), task_heading="Function to investigate")
            result = agent.run(brief(target, repo, graph))
            finding.claim, finding.steps = result.summary, result.steps
            report.prompt_tokens += result.prompt_tokens
            report.completion_tokens += result.completion_tokens
            finding.tests = sorted(p for p in ws.changed_files if is_test_path(p))
            if finding.tests:
                _judge(finding, repo, approve, client, config.model, report, claimed=result.fixed)
            else:
                finding.status = "inconclusive" if result.fixed else "clean"
        except openai.APIError as e:
            finding.status, finding.claim = "error", describe_error(e)
            report.error = finding.claim
        finally:
            ws.write_guard = None
        report.findings.append(finding)
        _announce(recorder, finding)

        keep = finding.is_bug or (keep_tests and finding.status == "clean")
        changed = {rel: ws.originals.get(rel) for rel in ws.changed_files}
        if keep:
            for rel, original in changed.items():
                kept.setdefault(rel, original)
        else:
            _put(repo, changed)  # remove this target's tests
        if report.error:
            break

    report.events, report.kept = recorder.events, kept
    if save and (kept or report.findings):
        bugs = report.bugs
        report.run_id = history.save_run(
            repo, issue=f"Haunt: {', '.join(f.target.qualname for f in report.findings)}",
            provider=config.provider.name, model=config.model, fixed=bool(bugs), summary=report.summary,
            steps=sum(f.steps for f in report.findings), prompt_tokens=report.prompt_tokens,
            completion_tokens=report.completion_tokens, changed_files=set(kept), originals=kept,
            error=report.error, events=report.events, kind="haunt", haunt=report.as_dict(),
        )
    return report


def _judge(finding: Finding, repo: Path, approve: Callable[[str], bool], client: Any, model: str,
           report: HauntReport, claimed: bool) -> None:
    """Run the haunter's tests ourselves and, if they fail, get the skeptic's verdict."""
    commands = test_files_command(repo, finding.tests)
    if not commands or not all(approve(c) for c in commands):
        finding.status = "inconclusive"
        return
    outputs, passed = [], True
    for command in commands:
        run = run_tests(repo, command, env={"PYTHONDONTWRITEBYTECODE": "1"})
        passed = passed and run.passed
        outputs.append(run.output)
    output = "\n".join(outputs)
    finding.output, finding.failure = output[-2000:], headline(output)
    if passed:
        finding.status = "clean"
        return
    if runner_missing(output):
        finding.status = "inconclusive"
        return
    verdict, reason, prompt_tokens, completion_tokens = skeptic(
        client, model, finding.target, repo, finding.tests, output, finding.claim)
    report.prompt_tokens += prompt_tokens
    report.completion_tokens += completion_tokens
    finding.verdict = reason
    if verdict == "real_bug":
        finding.status = "confirmed" if claimed else "suspected"
    elif verdict == "unsure":
        finding.status = "suspected"
    else:
        finding.status = "false_alarm"


def _announce(ui: Any, finding: Finding) -> None:
    t = finding.target
    text = {
        "confirmed": f"🐛 Confirmed bug in `{t.qualname}`: {finding.claim.splitlines()[0] if finding.claim else ''}",
        "suspected": f"🐛? Suspected bug in `{t.qualname}`: {finding.claim.splitlines()[0] if finding.claim else ''}",
        "false_alarm": f"The skeptic rejected the claim about `{t.qualname}`: {finding.verdict}",
        "clean": f"No bug found in `{t.qualname}`.",
        "inconclusive": f"Couldn't tell whether `{t.qualname}` has a bug.",
        "error": f"Stopped: {finding.claim}",
    }[finding.status]
    ui.thought(f"_{text}_")
