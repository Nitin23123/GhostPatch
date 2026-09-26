"""One fixing session, from bug report to saved run. Shared by the CLI, the dashboard, CI and the benchmark.

A session:
1. adds the crash path to the issue if it contains a stack trace,
2. runs the ghost, or a tournament of several candidate fixes (and, if asked, the poltergeist),
3. records every step for replay,
4. proves the fix red→green and scores its confidence,
5. saves the run to history so it can be reviewed, shared, undone or turned into a pull request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ghostpatch import history
from ghostpatch.agent import Agent, RunResult
from ghostpatch.confidence import assess
from ghostpatch.fallback import FallbackClient, is_exhausted
from ghostpatch.poltergeist import Round, attack_fix, fix_with_poltergeist
from ghostpatch.proof import Proof, prove_fix
from ghostpatch.replay import RecordingUI
from ghostpatch.tools import Workspace
from ghostpatch.tournament import Tournament, run_tournament
from ghostpatch.trace import Trace, enrich_issue


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


def run_session(
    repo: Path, config: Any, client: Any, ui: Any, issue: str, *,
    graph: Any = None, approve_command: Callable[[str], bool] | None = None, max_steps: int = 30,
    poltergeist: int = 0, issue_ref: dict | None = None, save: bool = True,
    candidates: int = 1, prove: bool = True, kind: str = "fix", proof_tests: list[str] | None = None,
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

    agents: list[Agent] = []

    def make_agent(ws: Workspace | None = None, **kwargs: Any) -> Agent:
        agent = Agent(client, config.model, ws or workspace, recorder, max_steps=max_steps, **kwargs)
        agents.append(agent)
        return agent

    def describe(e: Exception) -> str:
        return describe_model_error(e, client, config)

    result: RunResult | None = None
    rounds: list[Round] = []
    tournament: Tournament | None = None
    proof: Proof | None = None
    error: str | None = None
    exhausted = False
    try:
        if candidates > 1:
            tournament = run_tournament(repo, new_workspace, make_agent, recorder, issue_text, candidates,
                                        approve=approve, prove=prove, describe_error=describe,
                                        proof_tests=proof_tests)
            workspace, result = tournament.workspace, tournament.result
            exhausted = tournament.stopped is not None
            if tournament.winner is None and tournament.stopped:
                error = tournament.stopped
            elif tournament.winner is not None:
                proof = tournament.winner.proof
                if poltergeist > 0:
                    rounds = attack_fix(lambda **kw: make_agent(workspace, **kw), workspace, recorder,
                                        issue_text, result, poltergeist)
        elif poltergeist > 0:
            result, rounds = fix_with_poltergeist(make_agent, workspace, recorder, issue_text, rounds=poltergeist)
        else:
            result = make_agent().run(issue_text)
    except openai.APIError as e:
        error = describe(e)
        exhausted = is_exhausted(e)
    except KeyboardInterrupt:
        error = "stopped by user"

    partial = result or (agents[-1].result if agents else None)
    needs_proof = proof is None or any(r.refixed for r in rounds)  # a re-fix changed the code since
    if prove and needs_proof and not error and partial is not None and partial.fixed and workspace.changed_files:
        try:
            proof = prove_fix(repo, workspace.originals, workspace.changed_files, approve=approve, ui=recorder,
                              extra_tests=proof_tests)
        except (OSError, KeyboardInterrupt) as e:
            proof = None
            recorder.thought(f"_The red→green proof could not run: {e}_")
    confidence = assess(workspace, proof)
    outcome = Outcome(workspace, partial, error, None, trace, rounds, confidence, recorder.events, proof, tournament,
                      exhausted)
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
        )
    return outcome
