"""One fixing session, from bug report to saved run. Shared by the CLI, the dashboard, CI and the benchmark.

A session:
1. adds the crash path to the issue if it contains a stack trace,
2. ranks the code most likely at fault, so the ghost starts in the right place,
3. runs the whole test suite once as a baseline,
4. runs the ghost, or a tournament of several candidate fixes (and, if asked, the poltergeist),
5. re-runs the suite: tests the fix broke go back to the ghost to fix, automatically,
6. writes a regression test if the ghost didn't, then proves the fix red→green,
7. scores its confidence, records every step, and saves the run to history so it can be reviewed,
   shared, undone or turned into a pull request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ghostpatch import history
from ghostpatch.agent import Agent, RunResult
from ghostpatch.confidence import assess
from ghostpatch.fallback import FallbackClient, is_exhausted
from ghostpatch.parsers import is_test_path
from ghostpatch.poltergeist import Round, attack_fix, fix_with_poltergeist
from ghostpatch.proof import PROVER_PROMPT, Proof, prove_fix, prover_brief, tests_only
from ghostpatch.regression import (WHOLE_SUITE, RegressionCheck, SuiteRun, compare, refix_brief, run_suite,
                                  suite_command)
from ghostpatch.replay import RecordingUI
from ghostpatch.tools import Workspace
from ghostpatch.tournament import Tournament, run_tournament
from ghostpatch.trace import Trace, enrich_issue

REGRESSION_RETRIES = 2  # times the ghost gets tests it broke handed back


@dataclass
class Outcome:
    workspace: Workspace
    result: RunResult | None
    error: str | None = None
    run_id: str | None = None
    trace: Trace | None = None
    rounds: list[Round] = field(default_factory=list)
    confidence: dict[str, Any] = field(default_factory=dict)
    events: list[dict] = field(default_factory=list)
    proof: Proof | None = None
    tournament: Tournament | None = None
    quota_exhausted: bool = False  # every provider's quota is used up: stop starting new work
    regression: RegressionCheck | None = None
    suspects: list[str] = field(default_factory=list)  # where the ghost was pointed first

    @property
    def fixed(self) -> bool:
        return bool(self.result and self.result.fixed and not self.error)


def describe_model_error(error: Exception, client: Any, config: Any) -> str:
    """One readable line about a model error; names every provider tried if all are used up."""
    from ghostpatch.providers import describe_api_error

    message = describe_api_error(error, getattr(client, "current", config).provider)
    earlier = getattr(client, "failures", [])
    if earlier and is_exhausted(error):
        message = "Every configured provider is unavailable right now. " + " ".join([*earlier, message])
    return message


def _add(total: RunResult, part: RunResult | None) -> None:
    if part is not None:
        total.steps += part.steps
        total.prompt_tokens += part.prompt_tokens
        total.completion_tokens += part.completion_tokens


def baseline_run(repo: Path, approve: Callable[[str], bool], ui: Any) -> SuiteRun | None:
    """The whole test suite before any change, or None if it can't serve as a baseline."""
    command = suite_command(repo)
    if not command or not approve(command):
        return None
    ui.thought("_🛡 Running the whole test suite first, so any test the fix breaks can be caught…_")
    run = run_suite(repo, command)
    if not run.ran or WHOLE_SUITE in run.failing:
        ui.thought("_🛡 The test suite doesn't run cleanly yet, so the regression check is skipped._")
        return None
    ui.thought(f"_🛡 Baseline: {run.headline or ('passing' if run.passed else 'some tests failing')}_")
    return run


def run_session(
    repo: Path, config: Any, client: Any, ui: Any, issue: str, *,
    graph: Any = None, approve_command: Callable[[str], bool] | None = None, max_steps: int = 30,
    poltergeist: int = 0, issue_ref: dict | None = None, save: bool = True,
    candidates: int = 1, prove: bool = True, kind: str = "fix", proof_tests: list[str] | None = None,
    regression: bool = True, locate: bool = True,
) -> Outcome:
    """Run one fixing session. API errors and Ctrl+C end the session but are reported, not raised."""
    import openai

    approve = approve_command or ui.approve_command

    def new_workspace() -> Workspace:
        return Workspace(repo, approve_command=approve, graph=graph)

    workspace = new_workspace()
    recorder = RecordingUI(ui)
    if isinstance(client, FallbackClient):
        client.ui = recorder  # provider switches become part of the saved run
    issue_text, trace = enrich_issue(issue, repo, graph)
    if trace is not None and trace.repo_frames:
        recorder.thought(f"_🧭 Found a stack trace: the crash path runs through {len(trace.path_qualnames())} "
                         "function(s) in this repository._")
        if hasattr(ui, "trace"):
            ui.trace(trace.as_dict())

    hints, suspects = "", []
    if graph is not None and locate:
        from ghostpatch.locate import rank, where_to_look

        suspects = [s.qualname for s in rank(graph, issue_text)]
        if suspects:
            hints = where_to_look(graph, issue_text)
            recorder.thought("_🎯 Most likely places, from the report and the code graph: "
                             + ", ".join(suspects[:3]) + "_")

    agents: list[Agent] = []

    def make_agent(ws: Workspace | None = None, **kwargs: Any) -> Agent:
        kwargs.setdefault("context", "" if "system_prompt" in kwargs else hints)  # only fixers get the suspects
        agent = Agent(client, config.model, ws or workspace, recorder, max_steps=max_steps, **kwargs)
        agents.append(agent)
        return agent

    def describe(e: Exception) -> str:
        return describe_model_error(e, client, config)

    result: RunResult | None = None
    rounds: list[Round] = []
    tournament: Tournament | None = None
    proof: Proof | None = None
    check: RegressionCheck | None = None
    error: str | None = None
    exhausted = False
    baseline: SuiteRun | None = None
    try:
        if regression:
            baseline = baseline_run(repo, approve, recorder)
        if candidates > 1:
            tournament = run_tournament(repo, new_workspace, make_agent, recorder, issue_text, candidates,
                                        approve=approve, prove=prove, describe_error=describe,
                                        proof_tests=proof_tests, baseline=baseline)
            workspace, result = tournament.workspace, tournament.result
            exhausted = tournament.stopped is not None
            if tournament.winner is None and tournament.stopped:
                error = tournament.stopped
            elif tournament.winner is not None:
                proof = tournament.winner.proof
                check = tournament.winner.regression
                if poltergeist > 0:
                    rounds = attack_fix(lambda **kw: make_agent(workspace, **kw), workspace, recorder,
                                        issue_text, result, poltergeist)
        elif poltergeist > 0:
            result, rounds = fix_with_poltergeist(make_agent, workspace, recorder, issue_text, rounds=poltergeist)
        else:
            result = make_agent().run(issue_text)

        code_changed = bool(result and result.fixed and any(not is_test_path(p) for p in workspace.changed_files))
        refixed = any(r.refixed for r in rounds)

        # 🛡 The regression guard: what the fix broke goes back to the ghost.
        if baseline is not None and code_changed and (check is None or refixed):
            for attempt in range(REGRESSION_RETRIES + 1):
                check = compare(baseline, run_suite(repo, baseline.command))
                if check.status == "clean" or attempt == REGRESSION_RETRIES:
                    break
                recorder.thought(f"_🛡 {check.summary} Handing them back to the ghost…_")
                repair = make_agent(workspace).run(refix_brief(issue_text, check))
                _add(result, repair)
                refixed = True
                if repair.fixed:
                    result.summary = repair.summary
            recorder.thought(f"_🛡 {check.summary}_")
            if check.status == "regressed":
                result.fixed = False
                result.summary = f"{result.summary}\n\n🛡 {check.summary}"

        # 🧪 No test shows the fix? Write one, so the proof has something to prove with.
        wrote_tests = any(is_test_path(p) for p in workspace.changed_files) or bool(proof_tests)
        if prove and result is not None and result.fixed and code_changed and not wrote_tests:
            recorder.thought("_🧪 No test proves this fix yet: writing a regression test…_")
            workspace.write_guard = tests_only
            try:
                prover = make_agent(workspace, system_prompt=PROVER_PROMPT, exclude_tools=frozenset({"remember"}),
                                    task_heading="Write a regression test")
                _add(result, prover.run(prover_brief(issue_text, workspace.diffs())))
            finally:
                workspace.write_guard = None
            refixed = True  # new tests: prove again
        if proof is not None and refixed:
            proof = None
    except openai.APIError as e:
        error = describe(e)
        exhausted = is_exhausted(e)
    except KeyboardInterrupt:
        error = "stopped by user"

    partial = result or (agents[-1].result if agents else None)
    if prove and proof is None and not error and partial is not None and partial.fixed and workspace.changed_files:
        try:
            proof = prove_fix(repo, workspace.originals, workspace.changed_files, approve=approve, ui=recorder,
                              extra_tests=proof_tests)
        except (OSError, KeyboardInterrupt) as e:
            proof = None
            recorder.thought(f"_The red→green proof could not run: {e}_")
    confidence = assess(workspace, proof, check)
    outcome = Outcome(workspace, partial, error, None, trace, rounds, confidence, recorder.events, proof, tournament,
                      exhausted, check, suspects)
    if save and partial is not None and (workspace.changed_files or not error):
        used = getattr(client, "used", None)
        outcome.run_id = history.save_run(
            repo, issue=issue, provider=config.provider.name,
            model=" → ".join(used) if used and len(used) > 1 else config.model,
            fixed=outcome.fixed, summary=partial.summary or (error or ""), steps=partial.steps,
            prompt_tokens=partial.prompt_tokens, completion_tokens=partial.completion_tokens,
            changed_files=workspace.changed_files, originals=workspace.originals, error=error,
            issue_ref=issue_ref, confidence=confidence, events=recorder.events,
            poltergeist=[r.as_dict() for r in rounds], trace=trace.as_dict() if trace else None,
            proof=proof.as_dict() if proof else None,
            tournament=tournament.as_dict() if tournament else None, kind=kind,
            regression=check.as_dict() if check else None, suspects=suspects,
        )
    return outcome
