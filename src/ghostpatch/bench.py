"""The benchmark: how often does GhostPatch really fix a bug?

Each case in `bench/cases/<name>/` has:
- `repo/`      the buggy project, as the agent sees it
- `hidden/`    tests the agent never sees, copied in afterwards to judge the fix
- `solution/`  a reference fix (used only to check that the case itself is valid)
- `case.json`  the bug report and which test runner to use

A run copies the repo to a temporary folder, lets the agent work, then adds the hidden tests
and runs the whole test suite. The case passes only if every test passes, visible and hidden.
Results are appended to a JSON file, so an interrupted benchmark (for example when a free
daily quota runs out) resumes where it stopped.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

RUNNERS = {
    "pytest": [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider"],
    "node": ["node", "--test"],
}
CHECK_TIMEOUT_SECONDS = 300


@dataclass
class Case:
    name: str
    path: Path
    title: str
    language: str
    runner: str
    issue: str
    trap: str | None = None


@dataclass
class CaseResult:
    case: str
    language: str
    graph: bool
    provider: str
    model: str
    passed: bool
    claimed_fixed: bool
    steps: int
    prompt_tokens: int
    completion_tokens: int
    seconds: float
    error: str | None = None
    check_output: str = ""


def load_cases(root: Path, only: list[str] | None = None) -> list[Case]:
    cases = []
    for folder in sorted(p for p in root.iterdir() if (p / "case.json").is_file()):
        if only and folder.name not in only:
            continue
        data = json.loads((folder / "case.json").read_text(encoding="utf-8"))
        cases.append(Case(name=folder.name, path=folder, title=data["title"], language=data["language"],
                          runner=data["runner"], issue=data["issue"], trap=data.get("trap")))
    return cases


def check(work: Path, runner: str) -> tuple[bool, str]:
    """Run the project's whole test suite. Returns (passed, output)."""
    try:
        proc = subprocess.run(RUNNERS[runner], cwd=work, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=CHECK_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        return False, "tests timed out"
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-3000:]


def copy_into(src: Path, dest: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dest, dirs_exist_ok=True)


def run_case(case: Case, config: Any, use_graph: bool, max_steps: int, ui: Any) -> CaseResult:
    """Let the agent fix one case in a throwaway copy, then judge it with the hidden tests."""
    import openai

    from ghostpatch.agent import Agent
    from ghostpatch.graph import CodeGraph
    from ghostpatch.providers import describe_api_error
    from ghostpatch.tools import Workspace

    with tempfile.TemporaryDirectory(prefix=f"ghostpatch-bench-{case.name}-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp) / case.name
        shutil.copytree(case.path / "repo", work)
        graph = CodeGraph(work) if use_graph else None
        workspace = Workspace(work, approve_command=lambda command: True, graph=graph)
        agent = Agent(config.client(), config.model, workspace, ui, max_steps=max_steps)
        started, error = time.time(), None
        try:
            agent.run(case.issue)
        except openai.APIError as e:
            error = describe_api_error(e, config.provider)
        finally:
            if graph is not None:
                graph.close()
        seconds = round(time.time() - started, 1)
        result = agent.result

        copy_into(case.path / "hidden", work)
        passed, output = check(work, case.runner)
        return CaseResult(
            case=case.name, language=case.language, graph=use_graph,
            provider=config.provider.name, model=config.model,
            passed=passed and error is None, claimed_fixed=bool(result and result.fixed),
            steps=result.steps if result else 0,
            prompt_tokens=result.prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            seconds=seconds, error=error, check_output=output,
        )


def validate_case(case: Case) -> tuple[bool, str]:
    """A case is valid if its hidden tests fail on the buggy code and pass with the reference fix."""
    with tempfile.TemporaryDirectory(prefix="ghostpatch-validate-", ignore_cleanup_errors=True) as tmp:
        work = Path(tmp) / case.name
        shutil.copytree(case.path / "repo", work)
        visible_ok, output = check(work, case.runner)
        if not visible_ok:
            return False, "the visible tests should pass on the buggy code:\n" + output
        copy_into(case.path / "hidden", work)
        buggy_ok, output = check(work, case.runner)
        if buggy_ok:
            return False, "the hidden tests should fail on the buggy code"
        copy_into(case.path / "solution", work)
        fixed_ok, output = check(work, case.runner)
        if not fixed_ok:
            return False, "the hidden tests should pass with the reference fix:\n" + output
    return True, "ok"


# ------------------------------------------------------------------------- results


def load_results(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    return json.loads(path.read_text(encoding="utf-8"))


def save_result(path: Path, result: CaseResult) -> None:
    results = [r for r in load_results(path)
               if not (r["case"] == result.case and r["graph"] == result.graph and r["model"] == result.model)]
    results.append(asdict(result))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(results, indent=1), encoding="utf-8")


def already_done(results: list[dict], case: str, graph: bool, model: str) -> bool:
    return any(r["case"] == case and r["graph"] == graph and r["model"] == model and not r.get("error")
               for r in results)


def summary_table(results: list[dict]) -> str:
    """A Markdown table of results: one row per case, one column per graph setting."""
    models = sorted({r["model"] for r in results})
    lines = []
    for model in models:
        rows = [r for r in results if r["model"] == model]
        settings = sorted({r["graph"] for r in rows}, reverse=True)
        header = "| Case | Language | " + " | ".join("with graph" if g else "without graph" for g in settings) + " |"
        lines += [f"**{model}**", "", header, "|---|---|" + "---|" * len(settings)]
        for case in sorted({r["case"] for r in rows}):
            cells = []
            for g in settings:
                match = next((r for r in rows if r["case"] == case and r["graph"] == g), None)
                if match is None:
                    cells.append("–")
                elif match.get("error"):
                    cells.append("⚠ error")
                else:
                    cells.append(("✅" if match["passed"] else "❌") + f" {match['steps']} steps")
            language = next(r["language"] for r in rows if r["case"] == case)
            lines.append(f"| `{case}` | {language} | " + " | ".join(cells) + " |")
        totals = []
        for g in settings:
            done = [r for r in rows if r["graph"] == g and not r.get("error")]
            passed = sum(r["passed"] for r in done)
            totals.append(f"**{passed}/{len(done)}**" if done else "–")
        lines += [f"| **Solved** | | " + " | ".join(totals) + " |", ""]
    return "\n".join(lines)


class QuietUI:
    """One short line per tool call, so a benchmark run stays readable."""

    def __init__(self, print_fn=print):
        self.print = print_fn

    def step(self, number: int, max_steps: int) -> None:
        pass

    def thought(self, text: str) -> None:
        pass

    def tool_call(self, name: str, args: dict, result: str) -> None:
        target = args.get("path") or args.get("name") or args.get("command") or args.get("pattern") or ""
        mark = "✗" if result.startswith("Error") else "·"
        self.print(f"      {mark} {name} {str(target)[:70]}")


def ensure_python_on_path() -> None:
    """Make `python` in the agent's shell commands mean this interpreter (which has pytest)."""
    folder = str(Path(sys.executable).parent)
    if not os.environ.get("PATH", "").startswith(folder):
        os.environ["PATH"] = folder + os.pathsep + os.environ.get("PATH", "")
