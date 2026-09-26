"""👻 Poltergeist mode: a second agent tries to break every fix.

After the ghost fixes a bug, the poltergeist gets the issue, the diff and the fix's blast
radius from the code graph, and has one job: write tests that prove the fix is wrong or
incomplete. It may only create or edit test files. If one of its tests fails because the code
is really wrong, the ghost gets that failing test and fixes the code again. This repeats for
a few rounds, or until the poltergeist gives up.

The poltergeist's tests stay in the repository either way: they are extra coverage.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from ghostpatch.agent import Agent, RunResult
from ghostpatch.parsers import is_test_path
from ghostpatch.tools import Workspace

MAX_DIFF_CHARS = 6000

POLTERGEIST_PROMPT = """You are the Poltergeist, an adversarial tester. An AI engineer just claimed to fix a bug.
Your only goal is to prove the fix is wrong or incomplete.

1. Read the issue, the fix (the diff below) and the code the fix affects.
2. Think of inputs the fix may still get wrong: every example in the issue, edge cases (empty, zero,
   negative, None/null, boundaries, very large values, repeated calls), and other callers of the changed code.
3. Write NEW tests (you can only create or edit test files) that check the CORRECT behaviour described by
   the issue and the code's documented intent. Never encode the current output as the expectation.
4. Run the tests with the project's test command.
5. Finish:
   - fixed=true ONLY if at least one of your tests fails because the code is really wrong. In the summary,
     name the failing test and explain the bug in one or two sentences.
   - fixed=false if all your tests pass: you could not break the fix. Summarise what you tried.
{graph_guide}
Rules:
- Never modify non-test files and never weaken existing tests.
- Shell commands run on {os} with the repository root as the working directory.{shell_hint}
- Keep your thinking short. Always act through tool calls, and you MUST call the `finish` tool.
"""

REFIX_TEMPLATE = """{issue}

Your earlier fix is not complete. An adversarial tester found this problem:
{finding}

Its failing tests are in: {tests}
Fix the code so these tests pass too. Only change a test if it is clearly wrong about the correct behaviour."""


@dataclass
class Round:
    number: int
    broke_it: bool
    finding: str
    tests: list[str] = field(default_factory=list)
    refixed: bool | None = None

    def as_dict(self) -> dict:
        return {"round": self.number, "broke_it": self.broke_it, "finding": self.finding,
                "tests": self.tests, "refixed": self.refixed}


def only_tests(rel_path: str) -> str | None:
    if is_test_path(rel_path):
        return None
    return (f"The poltergeist may only write test files, not {rel_path}. "
            "Put tests in a tests/ folder or name them test_*.py / *.test.ts.")


def attack_brief(issue: str, workspace: Workspace) -> str:
    diff = "\n".join(d["diff"] for d in workspace.diffs())[:MAX_DIFF_CHARS]
    brief = f"The bug report the engineer worked on:\n{issue}\n\nTheir fix (diff):\n{diff}"
    if workspace.graph is not None and workspace.edited_symbols:
        radius = workspace.graph.blast_radius(list(workspace.edited_symbols.values()))
        if radius:
            brief += "\n\nCode the fix could affect (from the code graph):\n" + "\n".join(
                f"- {q} ({info['path']}){'' if info['tested'] else '  [no test reaches this]'}"
                for q, info in sorted(radius.items()))
    return brief


def _add(total: RunResult, part: RunResult | None) -> None:
    if part is not None:
        total.steps += part.steps
        total.prompt_tokens += part.prompt_tokens
        total.completion_tokens += part.completion_tokens


def fix_with_poltergeist(
    make_agent: Callable[..., Agent], workspace: Workspace, ui: Any, issue: str, rounds: int = 2,
    on_agent: Callable[[Agent], None] | None = None,
) -> tuple[RunResult, list[Round]]:
    """Fix, then let the poltergeist attack the fix, re-fixing whenever it succeeds."""
    ghost = make_agent()
    if on_agent:
        on_agent(ghost)
    result = ghost.run(issue)
    total = RunResult(fixed=result.fixed, summary=result.summary, steps=0)
    _add(total, result)
    report: list[Round] = []
    if not result.fixed or not workspace.changed_files:
        return total, report

    for number in range(1, rounds + 1):
        ui.thought(f"_👻 Poltergeist round {number}: trying to break the fix…_")
        before = set(workspace.changed_files)
        workspace.write_guard = only_tests
        try:
            poltergeist = make_agent(system_prompt=POLTERGEIST_PROMPT, exclude_tools=frozenset({"remember"}))
            if on_agent:
                on_agent(poltergeist)
            attack = poltergeist.run(attack_brief(issue, workspace))
        finally:
            workspace.write_guard = None
        _add(total, attack)
        tests = sorted(workspace.changed_files - before) or sorted(p for p in workspace.changed_files if is_test_path(p))
        round_ = Round(number, broke_it=attack.fixed, finding=attack.summary, tests=tests)
        report.append(round_)
        if not attack.fixed:
            ui.thought(f"_👻 The poltergeist couldn't break the fix: {attack.summary}_")
            break

        ui.thought(f"_👻 The poltergeist broke the fix: {attack.summary}. Fixing again…_")
        ghost = make_agent()
        if on_agent:
            on_agent(ghost)
        refix = ghost.run(REFIX_TEMPLATE.format(issue=issue, finding=attack.summary, tests=", ".join(tests) or "(see above)"))
        _add(total, refix)
        round_.refixed = refix.fixed
        total.fixed, total.summary = refix.fixed, refix.summary
        if not refix.fixed:
            break

    survived = report and not report[-1].broke_it
    verdict = ("The fix survived the poltergeist" if survived
               else "The poltergeist's last attack was answered with a re-fix" if report and report[-1].refixed
               else "The poltergeist found a problem that was not fixed")
    total.summary = f"{total.summary}\n\n👻 {verdict} ({len(report)} round{'s' if len(report) != 1 else ''})."
    if report and report[-1].broke_it and not report[-1].refixed:
        total.fixed = False
    return total, report
