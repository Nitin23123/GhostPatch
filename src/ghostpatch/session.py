"""One fixing session, from bug report to saved run. Shared by the CLI, the dashboard, CI and the benchmark.

A session:
1. adds the crash path to the issue if it contains a stack trace,
2. runs the ghost (and, if asked, the poltergeist rounds),
3. records every step for replay,
4. scores its confidence in the fix,
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
from ghostpatch.poltergeist import Round, fix_with_poltergeist
from ghostpatch.replay import RecordingUI
from ghostpatch.tools import Workspace
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

    @property
    def fixed(self) -> bool:
        return bool(self.result and self.result.fixed and not self.error)


def run_session(
    repo: Path, config: Any, client: Any, ui: Any, issue: str, *,
    graph: Any = None, approve_command: Callable[[str], bool] | None = None, max_steps: int = 30,
    poltergeist: int = 0, issue_ref: dict | None = None, save: bool = True,
) -> Outcome:
    """Run one fixing session. API errors and Ctrl+C end the session but are reported, not raised."""
    import openai

    from ghostpatch.providers import describe_api_error

    workspace = Workspace(repo, approve_command=approve_command or ui.approve_command, graph=graph)
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

    def make_agent(**kwargs: Any) -> Agent:
        agent = Agent(client, config.model, workspace, recorder, max_steps=max_steps, **kwargs)
        agents.append(agent)
        return agent

    result: RunResult | None = None
    rounds: list[Round] = []
    error: str | None = None
    try:
        if poltergeist > 0:
            result, rounds = fix_with_poltergeist(make_agent, workspace, recorder, issue_text, rounds=poltergeist)
        else:
            result = make_agent().run(issue_text)
    except openai.APIError as e:
        error = describe_api_error(e, getattr(client, "current", config).provider)
        earlier = getattr(client, "failures", [])
        if earlier and is_exhausted(e):
            error = "Every configured provider is unavailable right now. " + " ".join([*earlier, error])
    except KeyboardInterrupt:
        error = "stopped by user"

    partial = result or (agents[-1].result if agents else None)
    confidence = assess(workspace)
    outcome = Outcome(workspace, partial, error, None, trace, rounds, confidence, recorder.events)
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
        )
    return outcome
