"""Command-line entry point.

    ghostpatch init       set up a model provider and API key (once per computer)
    ghostpatch doctor     check the setup
    ghostpatch fix        fix a bug from the terminal
    ghostpatch serve      open the live dashboard
    ghostpatch graph      ask the code graph a question
    ghostpatch history    list past runs
    ghostpatch undo       roll back a run
    ghostpatch pr         open a GitHub pull request for a run
    ghostpatch bench      measure how often bugs really get fixed
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path

from ghostpatch import __version__
from ghostpatch.config import load_settings
from ghostpatch.policy import APPROVAL_MODES, DEFAULT_APPROVAL
from ghostpatch.providers import DEFAULT_PROVIDER, PROVIDERS

DEFAULT_MAX_STEPS = 30
DEFAULT_PORT = 8765
GRAPH_QUERIES = ["map", "stats", "symbol", "callers", "callees", "tests", "impact"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghostpatch",
        description="👻 GhostPatch: an AI software engineer that understands how your code connects.",
    )
    parser.add_argument("--version", action="version", version=f"ghostpatch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def add_repo(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=".", help="Path to the repository (default: current directory).")

    def add_model(p: argparse.ArgumentParser) -> None:
        p.add_argument(
            "--provider", choices=sorted(PROVIDERS), default=None,
            help=f"Model provider (default: $GHOSTPATCH_PROVIDER or {DEFAULT_PROVIDER}).",
        )
        p.add_argument("--model", default=None, help="Model name (default: $GHOSTPATCH_MODEL or the provider's default).")

    def add_agent_options(p: argparse.ArgumentParser) -> None:
        add_repo(p)
        add_model(p)
        p.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="Maximum agent steps.")
        p.add_argument(
            "--approve", choices=APPROVAL_MODES, default=None,
            help="When commands may run without asking: ask (always ask), safe (test and read-only git "
                 f"commands run automatically), all (never ask). Default: $GHOSTPATCH_APPROVE or {DEFAULT_APPROVAL}.",
        )
        p.add_argument("--yes", action="store_true", help="Same as --approve all. Only use in a sandbox!")
        p.add_argument("--no-graph", action="store_true", help="Don't build or use the code graph.")
        p.add_argument("--no-fallback", action="store_true",
                       help="Don't switch to another free provider when the quota runs out.")
        p.add_argument("--poltergeist", type=int, nargs="?", const=2, default=0, metavar="ROUNDS",
                       help="After fixing, let an adversarial agent try to break the fix (default 2 rounds).")
        p.add_argument("--candidates", type=int, default=1, metavar="N",
                       help="Fix tournament: try N independent fixes (2-5) and keep the best-proven one.")
        p.add_argument("--no-proof", action="store_true",
                       help="Skip the red→green proof (running the new tests without and with the fix).")
        p.add_argument("--no-regression", action="store_true",
                       help="Skip the regression guard (running the whole suite before and after the fix).")

    init = sub.add_parser("init", help="Set up a model provider and API key.")
    init.add_argument("--local", action="store_true", help="Save to ./.env instead of your user settings.")

    doctor = sub.add_parser("doctor", help="Check your setup and say what to fix.")
    add_repo(doctor)
    add_model(doctor)
    doctor.add_argument("--offline", action="store_true", help="Skip the check that contacts the model provider.")

    fix = sub.add_parser("fix", help="Fix a bug described in plain English, or a GitHub issue.")
    fix.add_argument("issue", help="The bug description, a GitHub issue link, or @path/to/issue.md.")
    add_agent_options(fix)
    fix.add_argument("--pr", action="store_true", help="If the fix is verified, open a GitHub pull request.")
    fix.add_argument("--draft", action="store_true", help="With --pr: open the pull request as a draft.")

    serve = sub.add_parser("serve", help="Open the web dashboard and watch the ghost work live.")
    add_agent_options(serve)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen on (default: {DEFAULT_PORT}).")
    serve.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically.")

    graph = sub.add_parser("graph", help="Ask the code graph a question.")
    graph.add_argument("query", choices=GRAPH_QUERIES, help="What to ask the graph.")
    graph.add_argument("name", nargs="?", help="Function, method or class name (not needed for map/stats).")
    add_repo(graph)

    hist = sub.add_parser("history", help="List past runs in this repository.")
    add_repo(hist)

    pr = sub.add_parser("pr", help="Open a GitHub pull request with a run's changes (the latest run by default).")
    pr.add_argument("run_id", nargs="?", help="Which run (see `ghostpatch history`).")
    pr.add_argument("--draft", action="store_true", help="Open it as a draft pull request.")
    add_repo(pr)

    bench = sub.add_parser("bench", help="Run the benchmark: real bug cases judged by hidden tests.")
    add_model(bench)
    bench.add_argument("--cases", default="bench/cases", help="Folder of benchmark cases (default: bench/cases).")
    bench.add_argument("--only", default=None, help="Comma-separated case names to run.")
    bench.add_argument("--out", default="bench/results.json", help="Where to store results (default: bench/results.json).")
    bench.add_argument("--max-steps", type=int, default=20, help="Maximum agent steps per case (default: 20).")
    graph_mode = bench.add_mutually_exclusive_group()
    graph_mode.add_argument("--compare", action="store_true", help="Run every case with and without the code graph.")
    graph_mode.add_argument("--no-graph", action="store_true", help="Run without the code graph.")
    graph_mode.add_argument("--full", action="store_true",
                            help="Run GhostPatch's whole fixing session (where-to-look, regression guard, proof).")
    bench.add_argument("--rerun", action="store_true", help="Run cases again even if results already exist.")
    bench.add_argument("--validate", action="store_true", help="Only check that every case is valid (no model needed).")
    bench.add_argument("--localize", action="store_true",
                       help="Only measure where-to-look: does the ranker find the buggy function? (no model needed)")
    bench.add_argument("--report", action="store_true", help="Only print the results table.")
    bench.add_argument("--repo", default=".", help=argparse.SUPPRESS)

    ci = sub.add_parser("ci-fix", help="Run the tests; if they fail, fix the code (for CI).")
    add_agent_options(ci)
    ci.add_argument("--test-command", default=None, help="How to run the tests (detected automatically by default).")
    ci.add_argument("--mode", choices=["report", "push", "pr"], default="report",
                    help="What to do with a verified fix: report only, push a commit, or open a pull request.")

    review = sub.add_parser("review", help="Blast-radius review of your changes or of a GitHub pull request.")
    review.add_argument("pr", nargs="?", help="Pull request number or link (default: your uncommitted changes).")
    review.add_argument("--post", action="store_true", help="Post the review as a comment on the pull request.")
    review.add_argument("--json", action="store_true", help="Print the review as JSON.")
    add_repo(review)

    trace = sub.add_parser("trace", help="Map a stack trace onto the code graph (and optionally fix it).")
    trace.add_argument("file", nargs="?", default="-", help="File containing the stack trace (default: read stdin).")
    trace.add_argument("--fix", action="store_true", help="Fix the crash.")
    trace.add_argument("--json", action="store_true", help="Print the trace as JSON.")
    add_agent_options(trace)

    gaps = sub.add_parser("gaps", help="List functions that no test reaches (and optionally write tests).")
    gaps.add_argument("--write-tests", type=int, default=0, metavar="N", help="Let the ghost write tests for N of them.")
    gaps.add_argument("--json", action="store_true", help="Print the list as JSON.")
    add_agent_options(gaps)

    haunt = sub.add_parser("haunt", help="Hunt for bugs nobody has reported: test the riskiest functions.")
    haunt.add_argument("--targets", type=int, default=3, metavar="N", help="How many functions to haunt (default: 3).")
    haunt.add_argument("--only", default=None, metavar="NAMES", help="Comma-separated functions to haunt instead.")
    haunt.add_argument("--list", action="store_true", help="Only show the risk ranking (no model needed).")
    haunt.add_argument("--fix", action="store_true", help="Fix every confirmed bug right away.")
    haunt.add_argument("--keep-tests", action="store_true", help="Keep the passing tests too, as extra coverage.")
    haunt.add_argument("--json", action="store_true", help="Print the report as JSON.")
    add_agent_options(haunt)
    haunt.set_defaults(max_steps=20)

    ask_ = sub.add_parser("ask", help="Ask a question about the code; get an answer and the real call flow.")
    ask_.add_argument("question", help="For example: \"How does checkout calculate the total?\"")
    ask_.add_argument("--json", action="store_true", help="Print the answer as JSON.")
    ask_.add_argument("--max-steps", type=int, default=15, help="Maximum agent steps (default: 15).")
    ask_.add_argument("--no-fallback", action="store_true", help="Don't switch provider when the quota runs out.")
    add_repo(ask_)
    add_model(ask_)

    night = sub.add_parser("nightshift", help="Fix every issue with a label, haunt for bugs, leave a morning report.")
    night.add_argument("--label", default="ghostpatch", help="Work on open issues with this label (default: ghostpatch).")
    night.add_argument("--limit", type=int, default=5, help="At most this many issues (default: 5).")
    night.add_argument("--haunt", type=int, default=0, metavar="N", help="Afterwards, haunt N risky functions.")
    night.add_argument("--draft", action="store_true", help="Open every pull request as a draft.")
    night.add_argument("--comment", action="store_true", help="Comment on issues the ghost couldn't fix.")
    night.add_argument("--reports", action="store_true", help="Only list earlier night shift reports.")
    add_agent_options(night)

    flow = sub.add_parser("workflow", help="Add a GitHub Actions workflow: fix failing CI, fix labelled issues, night shift.")
    flow.add_argument("name", nargs="?", choices=["ci", "issues", "nightshift"], help="Which workflow (default: list them).")
    flow.add_argument("--write", action="store_true", help="Write it into .github/workflows/ (otherwise print it).")
    flow.add_argument("--force", action="store_true", help="With --write: replace an existing file.")
    add_repo(flow)

    share = sub.add_parser("share", help="Export a run as a single HTML page anyone can open.")
    share.add_argument("run_id", nargs="?", help="Which run (default: the latest).")
    share.add_argument("--out", default=None, help="Output file (default: ghostpatch-run-<id>.html).")
    add_repo(share)

    mem = sub.add_parser("memory", help="Show or edit what GhostPatch remembers about this project.")
    mem.add_argument("--add", default=None, metavar="NOTE", help="Add a note.")
    mem.add_argument("--clear", action="store_true", help="Forget everything GhostPatch learned (keeps GHOSTPATCH.md).")
    add_repo(mem)

    lapse = sub.add_parser("timelapse", help="The code graph at each of the last N commits.")
    lapse.add_argument("--commits", type=int, default=20, help="How many commits (default: 20).")
    lapse.add_argument("--json", default=None, metavar="FILE", help="Save all frames as JSON for visualisation.")
    add_repo(lapse)

    undo = sub.add_parser("undo", help="Roll back the files a run changed (the latest run by default).")
    undo.add_argument("run_id", nargs="?", help="Which run to undo (see `ghostpatch history`).")
    undo.add_argument("--force", action="store_true", help="Undo even if the files were edited after the run.")
    add_repo(undo)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # emoji-safe output on Windows terminals
    args = build_parser().parse_args(argv)

    if args.command == "init":
        return run_init(args)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"Repository not found: {repo}")
        return 2
    load_settings(repo)

    commands = {
        "doctor": run_doctor, "fix": run_fix, "serve": run_serve,
        "graph": run_graph, "history": run_history, "undo": run_undo, "pr": run_pr, "bench": run_bench,
        "ci-fix": run_ci_fix, "review": run_review, "trace": run_trace, "gaps": run_gaps,
        "share": run_share, "memory": run_memory, "timelapse": run_timelapse, "haunt": run_haunt,
        "nightshift": run_nightshift_command, "ask": run_ask, "workflow": run_workflow,
    }
    return commands[args.command](args, repo)


def approval_mode(args: argparse.Namespace) -> str:
    if args.yes:
        return "all"
    mode = args.approve or os.environ.get("GHOSTPATCH_APPROVE") or DEFAULT_APPROVAL
    if mode not in APPROVAL_MODES:
        print(f"Unknown approval mode '{mode}' (from GHOSTPATCH_APPROVE); using '{DEFAULT_APPROVAL}'.")
        return DEFAULT_APPROVAL
    return mode


# ------------------------------------------------------------------------------ setup


def run_init(args: argparse.Namespace) -> int:
    from rich.console import Console
    from rich.prompt import Confirm, Prompt

    from ghostpatch.config import user_config_path, write_settings

    console = Console()
    target = Path(".env").resolve() if args.local else user_config_path()
    console.print("\n[bold magenta]👻 GhostPatch setup[/]\n")
    for name, p in PROVIDERS.items():
        cost = "[green]free[/]" if p.free else "[yellow]paid[/]"
        console.print(f"  [bold]{name:<11}[/] {cost}  {p.signup_url}")
    provider_name = Prompt.ask("\nWhich provider?", choices=list(PROVIDERS), default="groq", console=console)
    provider = PROVIDERS[provider_name]

    def ask_key(p) -> str:
        console.print(f"Get a key at [link={p.signup_url}]{p.signup_url}[/link]")
        return Prompt.ask(f"Paste your {p.key_env} (hidden)", password=True, console=console).strip()

    values: dict[str, str | None] = {"GHOSTPATCH_PROVIDER": provider_name}
    if provider.key_env:
        key = ask_key(provider)
        if not key:
            console.print("[red]No key entered; nothing was saved.[/]")
            return 2
        values[provider.key_env] = key
    model = Prompt.ask("Model", default=provider.default_model, console=console).strip()
    values["GHOSTPATCH_MODEL"] = None if model == provider.default_model else model
    values["GHOSTPATCH_APPROVE"] = Prompt.ask(
        "Command approvals (ask = always ask, safe = auto-run tests)",
        choices=list(APPROVAL_MODES[:2]), default="safe", console=console,
    )

    # Free quotas run out. With a second free provider, a run switches over instead of stopping.
    backups = [n for n, p in PROVIDERS.items()
               if p.free and p.key_env and n != provider_name and not os.environ.get(p.key_env)]
    question = f"\nAdd a free backup provider for when {provider_name}'s daily quota runs out?"
    while backups and Confirm.ask(question, default=True, console=console):
        name = Prompt.ask("Backup provider", choices=list(backups), default=backups[0], console=console)
        key = ask_key(PROVIDERS[name])
        if key:
            values[PROVIDERS[name].key_env] = key
        backups.remove(name)
        question = "Add another backup?"

    write_settings(target, values)
    console.print(f"\n[green]✓ Saved to {target}[/]")
    if args.local:
        gitignore = Path(".gitignore")
        if not gitignore.is_file() or ".env" not in gitignore.read_text(encoding="utf-8").split():
            console.print("[yellow]⚠ Add .env to .gitignore so your key is never committed.[/]")
    console.print("Next: run [bold]ghostpatch doctor[/] to check everything works.\n")
    return 0


def run_doctor(args: argparse.Namespace, repo: Path) -> int:
    from rich.console import Console

    from ghostpatch.doctor import run_checks

    console = Console()
    console.print(f"\n[bold magenta]👻 GhostPatch doctor[/]  [dim]{repo}[/]\n")
    icons = {"ok": "[green]✓[/]", "warn": "[yellow]![/]", "fail": "[red]✗[/]"}
    checks = run_checks(repo, args.provider, args.model, online=not args.offline)
    for check in checks:
        console.print(f" {icons[check.status]} [bold]{check.name:<15}[/] {check.detail}", highlight=False)
    failed = sum(c.status == "fail" for c in checks)
    console.print("\n[green]All good. Try `ghostpatch serve`.[/]\n" if not failed
                  else f"\n[red]{failed} problem(s) to fix.[/]\n")
    return 1 if failed else 0


# ------------------------------------------------------------------------- the agent


def run_serve(args: argparse.Namespace, repo: Path) -> int:
    from ghostpatch.providers import ProviderError, resolve
    from ghostpatch.server import serve

    try:
        config = resolve(args.provider, args.model)
    except ProviderError as e:
        print(e)
        return 2
    return serve(
        repo, config, port=args.port, max_steps=args.max_steps, approval=approval_mode(args),
        use_graph=not args.no_graph, open_browser=not args.no_browser, fallback=not args.no_fallback,
        poltergeist=args.poltergeist,
    )


def run_fix(args: argparse.Namespace, repo: Path) -> int:
    # Imported lazily so `ghostpatch --help` stays fast.
    from rich.console import Console

    from ghostpatch.fallback import FallbackClient, make_client
    from ghostpatch.gitutil import is_git_repo
    from ghostpatch.providers import ProviderError, resolve
    from ghostpatch.session import run_session
    from ghostpatch.ui import ConsoleUI

    console = Console()
    try:
        config = resolve(args.provider, args.model)
    except ProviderError as e:
        console.print(f"[red]{e}[/]")
        return 2

    from ghostpatch import github

    issue, issue_ref = args.issue, None
    if issue.startswith("@"):
        issue = Path(issue[1:]).read_text(encoding="utf-8")
    try:
        gh_issue = github.resolve_issue(issue)
        if getattr(args, "pr", False):
            github.run_gh("auth", "status")
            if github.origin_slug(repo) is None:
                raise github.GitHubError("--pr needs a git repository with a GitHub `origin` remote.")
    except github.GitHubError as e:
        console.print(f"[red]{e}[/]")
        return 2
    if gh_issue is not None:
        console.print(f"[bold]Issue #{gh_issue.number}:[/] {gh_issue.title}  [dim]{gh_issue.url}[/]")
        issue, issue_ref = gh_issue.as_prompt(), gh_issue.as_record()

    approval = approval_mode(args)
    ui = ConsoleUI(console, approval=approval)
    ui.banner(str(repo), f"{config.model} ({config.provider.name}) · approvals: {approval}")

    graph = None
    if not args.no_graph:
        from ghostpatch.graph import CodeGraph

        graph = CodeGraph(repo)
        stats = graph.refresh()
        totals = graph.stats()
        console.print(
            f"[dim]🕸  code graph: {totals['files']} files · {totals['symbols']} symbols · "
            f"{totals['calls']} calls (re-indexed {stats.indexed})[/]\n"
        )
    client = make_client(config, ui, fallback=not args.no_fallback)
    if len(client.configs) > 1:
        console.print("[dim]↪ fallback: " + " → ".join(FallbackClient.label(c) for c in client.configs) + "[/]")
    if args.poltergeist:
        console.print(f"[dim]👻 poltergeist mode: up to {args.poltergeist} round(s) of adversarial testing[/]")
    if args.candidates > 1:
        console.print(f"[dim]🏆 tournament: {min(args.candidates, 5)} candidate fixes will compete[/]")

    outcome = run_session(
        repo, config, client, ui, issue, graph=graph, max_steps=args.max_steps,
        poltergeist=args.poltergeist, issue_ref=issue_ref, candidates=args.candidates, prove=not args.no_proof,
        regression=not args.no_regression,
    )
    workspace, result, run_id = outcome.workspace, outcome.result, outcome.run_id
    if outcome.error:
        console.print(f"[red]{outcome.error}[/]", highlight=False)
        if run_id:
            console.print(f"[dim]Partial changes saved as run {run_id}. Undo with `ghostpatch undo`.[/]")
        return 130 if outcome.error == "stopped by user" else 2

    console.rule()
    headline = "[bold green]👻 Bug fixed![/]" if result.fixed else "[bold yellow]👻 Not fixed.[/]"
    console.print(headline)
    console.print(result.summary)
    if outcome.tournament is not None:
        print_tournament(console, outcome.tournament.as_dict())
    if outcome.proof is not None:
        proof = outcome.proof
        color = "green" if proof.proven else "yellow" if proof.status in ("no_test", "not_red") else "red"
        detail = f"  [dim](without the fix: {proof.red} · with it: {proof.green})[/]" if proof.red or proof.green else ""
        console.print(f"[{color}]{'🔴→🟢 ' if proof.proven else ''}{proof.summary}[/]{detail}", highlight=False)
    if outcome.regression is not None:
        check = outcome.regression
        detail = f"  [dim](before: {check.before} · after: {check.after})[/]" if check.before or check.after else ""
        color = "green" if check.status == "clean" else "red"
        console.print(f"[{color}]🛡 {check.summary}[/]{detail}", highlight=False)
    conf = outcome.confidence
    if conf.get("summary") and conf.get("level") != "none":
        color = {"high": "green", "medium": "yellow"}.get(conf.get("level"), "red")
        console.print(f"[{color}]📊 Confidence {conf['summary']}[/]")
    console.print(
        f"\n[dim]{result.steps} steps · {result.prompt_tokens:,} input tokens · "
        f"{result.completion_tokens:,} output tokens · run {run_id}[/]"
    )
    if workspace.changed_files:
        console.print("\n[bold]Changed files:[/] " + ", ".join(sorted(workspace.changed_files)))
        if is_git_repo(repo):
            diff = subprocess.run(["git", "diff", "--stat"], cwd=repo, capture_output=True, text=True)
            console.print(diff.stdout, highlight=False, markup=False)
        console.print("[dim]Don't like it? `ghostpatch undo` puts every file back.[/]")
    if getattr(args, "pr", False) and run_id and workspace.changed_files:
        if not result.fixed:
            console.print("[yellow]Not opening a pull request: the fix wasn't verified. "
                          f"Review it, then run `ghostpatch pr {run_id}` if you want one.[/]")
        else:
            return 0 if _open_pr(console, repo, run_id, args.draft) else 1
    return 0 if result.fixed else 1


def print_tournament(console, tournament: dict) -> None:
    from rich.table import Table

    table = Table(title="🏆 Fix tournament", box=None, header_style="bold", title_justify="left")
    for column in ("candidate", "proof", "rival tests", "lines", "points"):
        table.add_column(column)
    for c in tournament["candidates"]:
        name = f"{'🏆 ' if c['winner'] else '   '}#{c['number']} {c['label']}"
        points = f"[dim]{c['disqualified']}[/]" if c["disqualified"] else f"{c['points']:.0f}"
        table.add_row(name, c.get("proof") or "–", c.get("rivals") or "–", str(c["changed_lines"]), points)
    console.print(table)


def _open_pr(console, repo: Path, run_id: str | None, draft: bool) -> bool:
    from ghostpatch import github, history

    try:
        run = history.load_run(repo, run_id)
        console.print(f"Opening a pull request for run {run['id']}…")
        url = github.open_pull_request(repo, run, draft=draft)
    except (github.GitHubError, history.UndoError) as e:
        console.print(f"[red]{e}[/]")
        return False
    history.update_run(repo, run["id"], pr_url=url)
    console.print(f"[green]✓ Pull request:[/] {url}")
    return True


def run_pr(args: argparse.Namespace, repo: Path) -> int:
    from rich.console import Console

    return 0 if _open_pr(Console(), repo, args.run_id, args.draft) else 1


# ------------------------------------------------------------------ graph and history


def run_graph(args: argparse.Namespace, repo: Path) -> int:
    from ghostpatch.graph import CodeGraph

    if args.query not in ("map", "stats") and not args.name:
        print(f"`ghostpatch graph {args.query}` needs a name, e.g. `ghostpatch graph {args.query} my_function`.")
        return 2

    graph = CodeGraph(repo)
    try:
        refreshed = graph.refresh()
        if args.query == "stats":
            s = graph.stats()
            print(f"{s['files']} source files · {s['symbols']} symbols · {s['calls']} calls · "
                  f"{s['parse_errors']} parse errors  (re-indexed {refreshed.indexed} changed files)")
        else:
            query = {
                "map": lambda: graph.repo_map(max_chars=50_000),
                "symbol": lambda: graph.find_symbol(args.name),
                "callers": lambda: graph.find_callers(args.name),
                "callees": lambda: graph.find_callees(args.name),
                "tests": lambda: graph.related_tests(args.name),
                "impact": lambda: graph.impact_of_change(args.name),
            }[args.query]
            print(query())
    finally:
        graph.close()
    return 0


def run_history(args: argparse.Namespace, repo: Path) -> int:
    from rich.console import Console
    from rich.table import Table

    from ghostpatch import history

    runs = history.list_runs(repo)
    console = Console()
    if not runs:
        console.print("No runs yet. Start one with `ghostpatch fix` or `ghostpatch serve`.")
        return 0
    table = Table(box=None, header_style="bold")
    for column in ("run", "when", "result", "files", "summary"):
        table.add_column(column)
    for run in runs:
        result = ("[dim]undone[/]" if run["undone"] else "[green]fixed[/]" if run["fixed"]
                  else "[red]error[/]" if run.get("error") else "[yellow]not fixed[/]")
        if run.get("kind") == "haunt" and not run["undone"]:
            bugs = sum(f["status"] in ("confirmed", "suspected") for f in (run.get("haunt") or {}).get("findings", []))
            result = f"[magenta]haunt: {bugs} bug{'s' if bugs != 1 else ''}[/]"
        summary = (run["summary"] or run["issue"]).strip().splitlines()[0][:70] if (run["summary"] or run["issue"]) else ""
        table.add_row(run["id"], run["created"], result, str(len(run["files"])), summary)
    console.print(table)
    console.print("\n[dim]Undo a run with `ghostpatch undo RUN`.[/]")
    return 0


def run_undo(args: argparse.Namespace, repo: Path) -> int:
    from ghostpatch import history

    try:
        run = history.undo_run(repo, args.run_id, force=args.force)
    except history.UndoError as e:
        print(e)
        return 1
    print(f"↩ Undid run {run['id']}. Restored: {', '.join(f['path'] for f in run['files'])}")
    return 0


def run_bench(args: argparse.Namespace, repo: Path) -> int:
    from rich.console import Console

    from ghostpatch import bench

    console = Console()
    cases_dir, out = Path(args.cases), Path(args.out)
    if not cases_dir.is_dir():
        console.print(f"[red]No benchmark cases found in {cases_dir}.[/] Run this from the GhostPatch folder.")
        return 2
    cases = bench.load_cases(cases_dir, args.only.split(",") if args.only else None)
    if args.report:
        console.print(bench.summary_table(bench.load_results(out)), markup=False, highlight=False)
        return 0
    if args.localize:
        console.print(bench.localize_table([bench.localize_case(case) for case in cases]), markup=False, highlight=False)
        return 0
    if args.validate:
        bad = 0
        for case in cases:
            ok, why = bench.validate_case(case)
            bad += not ok
            console.print(f" {'[green]✓[/]' if ok else '[red]✗[/]'} {case.name:<20} {'' if ok else why}", highlight=False)
        return 1 if bad else 0

    from ghostpatch.providers import ProviderError, resolve

    try:
        config = resolve(args.provider, args.model)
    except ProviderError as e:
        console.print(f"[red]{e}[/]")
        return 2
    bench.ensure_python_on_path()
    settings = [True, False] if args.compare else [not args.no_graph]
    results = bench.load_results(out)
    console.print(f"[bold magenta]👻 GhostPatch benchmark[/]  {len(cases)} cases · {config.model} ({config.provider.name})\n")
    for case in cases:
        for use_graph in settings:
            label = f"{case.name} ({'graph' if use_graph else 'no graph'})"
            mode = "full" if args.full else "agent"
            if not args.rerun and bench.already_done(results, case.name, use_graph, config.model, mode):
                console.print(f"[dim]  skip {label}: already in {out}[/]")
                continue
            console.print(f"[bold]▶ {label}[/]  {case.title}")
            result = bench.run_case(case, config, use_graph, args.max_steps, bench.QuietUI(console.print),
                                    full=args.full)
            bench.save_result(out, result)
            verdict = ("[yellow]⚠ " + result.error + "[/]") if result.error else (
                "[green]✅ passed hidden tests[/]" if result.passed else "[red]❌ failed hidden tests[/]")
            console.print(f"    {verdict}  [dim]{result.steps} steps · {result.seconds}s · "
                          f"{result.prompt_tokens + result.completion_tokens:,} tokens[/]\n", highlight=False)
            if result.error and ("per day" in result.error or "quota" in result.error.lower()):
                console.print("[yellow]Stopping: the provider's quota is used up. Run the same command later to resume.[/]")
                console.print(bench.summary_table(bench.load_results(out)), markup=False, highlight=False)
                return 2
    console.print(bench.summary_table(bench.load_results(out)), markup=False, highlight=False)
    return 0


# ------------------------------------------------------------------- more commands


def run_ci_fix(args: argparse.Namespace, repo: Path) -> int:
    from rich.console import Console

    from ghostpatch import cifix, github, history
    from ghostpatch.fallback import make_client
    from ghostpatch.gitutil import git, is_git_repo
    from ghostpatch.providers import ProviderError, resolve
    from ghostpatch.session import run_session
    from ghostpatch.ui import ConsoleUI

    console = Console()
    command = args.test_command or cifix.detect_test_command(repo)
    if not command:
        console.print("[red]Couldn't tell how to run this project's tests. Pass --test-command.[/]")
        return 2
    console.print(f"[bold]🧪 Running the tests:[/] {command}")
    first = cifix.run_tests(repo, command)
    if cifix.runner_missing(first.output):
        console.print(f"[red]The tests couldn't run: the test runner isn't installed.[/]\n{first.output[-500:]}\n"
                      "Install the project's test dependencies (e.g. `pip install pytest`) before GhostPatch runs.",
                      highlight=False, markup=False)
        cifix.step_summary("### 👻 GhostPatch\n⚠ The tests couldn't run: install the project's test dependencies first.")
        return 2
    if first.passed:
        console.print("[green]✓ The tests pass. Nothing to fix.[/]")
        cifix.step_summary("### 👻 GhostPatch\n✅ The tests pass. Nothing to fix.")
        return 0
    console.print("[yellow]✗ The tests fail. Handing the failure to the ghost…[/]\n")

    try:
        config = resolve(args.provider, args.model)
    except ProviderError as e:
        console.print(f"[red]{e}[/]")
        return 2
    graph = None
    if not args.no_graph:
        from ghostpatch.graph import CodeGraph

        graph = CodeGraph(repo)
    ui = ConsoleUI(console, approval="all")  # CI machines are throwaway sandboxes
    outcome = run_session(repo, config, make_client(config, ui, fallback=not args.no_fallback), ui,
                          cifix.issue_from_failure(first), graph=graph, max_steps=args.max_steps,
                          poltergeist=args.poltergeist, candidates=args.candidates, prove=not args.no_proof,
                          regression=not args.no_regression)
    if outcome.error:
        console.print(f"[red]{outcome.error}[/]")
        cifix.step_summary(f"### 👻 GhostPatch\n⚠ Stopped: {outcome.error}")
        return 2

    verify = cifix.run_tests(repo, command)  # never trust the agent's own word
    confidence = outcome.confidence.get("summary", "")
    if not verify.passed:
        console.print("[red]✗ The tests still fail after the ghost's attempt.[/]")
        cifix.step_summary(f"### 👻 GhostPatch\n❌ Tried, but the tests still fail.\n\n{outcome.result.summary if outcome.result else ''}")
        return 1
    files = sorted(outcome.workspace.changed_files)
    console.print(f"[green]✓ The tests pass now.[/] Changed: {', '.join(files) or 'nothing'}\n📊 {confidence}")
    summary = (f"### 👻 GhostPatch fixed the failing tests\n\n{outcome.result.summary}\n\n"
               f"**Confidence:** {confidence}\n\n**Changed files:** {', '.join(f'`{f}`' for f in files)}")

    if args.mode != "report" and files:
        if os.environ.get("GITHUB_ACTIONS") and is_git_repo(repo) and not git(repo, "config", "user.email", check=False):
            git(repo, "config", "user.name", "GhostPatch")
            git(repo, "config", "user.email", "ghostpatch@users.noreply.github.com")
        try:
            if args.mode == "pr":
                url = github.open_pull_request(repo, history.load_run(repo, outcome.run_id))
                history.update_run(repo, outcome.run_id, pr_url=url)
                summary += f"\n\n**Pull request:** {url}"
                console.print(f"[green]⬆ Pull request:[/] {url}")
            else:
                git(repo, "add", "--", *files)
                git(repo, "commit", "-m", "GhostPatch: fix failing tests", "-m", outcome.result.summary, "--", *files)
                git(repo, "push")
                summary += "\n\nPushed a commit with the fix."
                console.print("[green]⬆ Pushed a commit with the fix.[/]")
        except (RuntimeError, github.GitHubError, history.UndoError) as e:
            console.print(f"[red]{e}[/]")
            cifix.step_summary(summary + f"\n\n⚠ Could not {args.mode}: {e}")
            return 1
    cifix.step_summary(summary)
    return 0


def run_review(args: argparse.Namespace, repo: Path) -> int:
    import json as jsonlib

    from rich.console import Console
    from rich.markdown import Markdown

    from ghostpatch import github, review

    try:
        report = review.review_pull_request(repo, args.pr) if args.pr else review.review_working_tree(repo)
        if args.post:
            if not args.pr:
                print("--post needs a pull request.")
                return 2
            review.post_review(report)
    except (review.ReviewError, github.GitHubError, RuntimeError) as e:
        print(e)
        return 2
    if args.json:
        print(jsonlib.dumps(report, indent=1))
    else:
        Console().print(Markdown(report["markdown"]))
        if args.post:
            print("Posted the review on the pull request.")
    return 0


def _read_input(file: str) -> str:
    return sys.stdin.read() if file == "-" else Path(file).read_text(encoding="utf-8", errors="replace")


def run_trace(args: argparse.Namespace, repo: Path) -> int:
    import json as jsonlib

    from ghostpatch.graph import CodeGraph
    from ghostpatch.trace import describe, locate, parse

    text = _read_input(args.file)
    found = parse(text)
    if found is None:
        print("No Python or JavaScript/TypeScript stack trace found in the input.")
        return 2
    graph = CodeGraph(repo)
    try:
        locate(found, repo, graph)
    finally:
        graph.close()
    print(jsonlib.dumps(found.as_dict(), indent=1) if args.json else describe(found))
    if args.fix:
        args.issue = text
        return run_fix(args, repo)
    return 0


def run_gaps(args: argparse.Namespace, repo: Path) -> int:
    import json as jsonlib

    from ghostpatch.graph import CodeGraph

    graph = CodeGraph(repo)
    try:
        graph.refresh()
        untested = graph.untested()
    finally:
        graph.close()
    if args.json:
        print(jsonlib.dumps(untested, indent=1))
    else:
        print(f"{len(untested)} function(s) that no test reaches:")
        for item in untested[:100]:
            print(f"  {item['path']}:{item['line']}  {item['qualname']}")
    if args.write_tests and untested:
        chosen = untested[: args.write_tests]
        args.issue = (
            "Improve test coverage. No test currently reaches these functions:\n"
            + "\n".join(f"- {c['qualname']} ({c['path']}:{c['line']}) {c['signature']}" for c in chosen)
            + "\n\nWrite focused tests for them in the project's existing test style, run them, and make sure "
              "they pass. Do NOT change the functions themselves. If a test reveals a real bug, describe it in "
              "your summary instead of changing the code."
        )
        return run_fix(args, repo)
    return 0


HAUNT_ICONS = {"confirmed": "🐛", "suspected": "🐛?", "false_alarm": "🙅", "clean": "✓", "inconclusive": "?",
               "error": "⚠"}


def run_haunt(args: argparse.Namespace, repo: Path) -> int:
    import json as jsonlib

    from rich.console import Console
    from rich.table import Table

    from ghostpatch.graph import CodeGraph
    from ghostpatch.haunt import haunt, rank_targets

    console = Console()
    graph = CodeGraph(repo)
    try:
        if args.list:
            table = Table(title="👻 Riskiest functions", box=None, header_style="bold", title_justify="left")
            for column in ("risk", "function", "where", "why"):
                table.add_column(column)
            for t in rank_targets(graph, repo, limit=max(args.targets, 15)):
                table.add_row(f"{t.risk:.1f}", t.qualname, f"{t.path}:{t.line}", "; ".join(t.reasons))
            console.print(table)
            console.print("\n[dim]Haunt the top ones with `ghostpatch haunt`, or pick some with --only.[/]")
            return 0

        from ghostpatch.fallback import make_client
        from ghostpatch.providers import ProviderError, resolve
        from ghostpatch.session import describe_model_error
        from ghostpatch.ui import ConsoleUI

        try:
            config = resolve(args.provider, args.model)
        except ProviderError as e:
            console.print(f"[red]{e}[/]")
            return 2
        approval = approval_mode(args)
        ui = ConsoleUI(console, approval=approval)
        ui.banner(str(repo), f"{config.model} ({config.provider.name}) · approvals: {approval} · 👻 haunt mode")
        client = make_client(config, ui, fallback=not args.no_fallback)
        report = haunt(repo, config, client, ui, graph=graph, targets=args.targets,
                       only=[n.strip() for n in args.only.split(",")] if args.only else None,
                       max_steps=args.max_steps, keep_tests=args.keep_tests,
                       describe_error=lambda e: describe_model_error(e, client, config))
    finally:
        graph.close()

    if args.json:
        print(jsonlib.dumps(report.as_dict(), indent=1))
    else:
        console.rule()
        console.print(f"[bold]👻 {report.summary}[/]")
        for f in report.findings:
            t = f.target
            color = {"confirmed": "red", "suspected": "yellow", "error": "red"}.get(f.status, "dim")
            console.print(f"\n[{color}]{HAUNT_ICONS[f.status]} {f.status.replace('_', ' ')}[/] [bold]{t.qualname}[/] "
                          f"[dim]{t.path}:{t.line}[/]", highlight=False)
            if f.is_bug:
                console.print(f"   {f.claim}", highlight=False, markup=False)
                console.print(f"   [dim]proof: {', '.join(f.tests)} → {f.failure}[/]", highlight=False)
                if f.verdict:
                    console.print(f"   [dim]skeptic: {f.verdict}[/]", highlight=False)
            elif f.status == "false_alarm":
                console.print(f"   [dim]{f.verdict}[/]", highlight=False)
        if report.run_id:
            console.print(f"\n[dim]{report.prompt_tokens + report.completion_tokens:,} tokens · run {report.run_id}"
                          + (" · the failing tests are kept as proof; `ghostpatch undo` removes them" if report.bugs else "")
                          + "[/]")
        if report.bugs and not args.fix:
            console.print("[bold]Fix them with[/] `ghostpatch haunt --fix`, or one at a time from the dashboard.")
    from ghostpatch import cifix

    cifix.step_summary(report.markdown())
    if report.error:
        console.print(f"[red]{report.error}[/]")
        return 2
    if args.fix and report.bugs:
        return _fix_haunted(args, repo, report)
    return 0


def _fix_haunted(args: argparse.Namespace, repo: Path, report) -> int:
    """Fix each bug haunt mode found, proving each fix with the haunter's failing tests."""
    from rich.console import Console

    from ghostpatch.fallback import make_client
    from ghostpatch.graph import CodeGraph
    from ghostpatch.providers import resolve
    from ghostpatch.session import run_session
    from ghostpatch.ui import ConsoleUI

    console = Console()
    config = resolve(args.provider, args.model)
    ui = ConsoleUI(console, approval=approval_mode(args))
    fixed = 0
    for n, bug in enumerate(report.bugs, 1):
        console.rule(f"🔧 Fixing bug {n}/{len(report.bugs)}: {bug.target.qualname}")
        graph = CodeGraph(repo)
        try:
            outcome = run_session(repo, config, make_client(config, ui, fallback=not args.no_fallback), ui,
                                  bug.as_issue(), graph=graph, max_steps=DEFAULT_MAX_STEPS,
                                  poltergeist=args.poltergeist, candidates=args.candidates,
                                  prove=not args.no_proof, regression=not args.no_regression, proof_tests=bug.tests)
        finally:
            graph.close()
        fixed += outcome.fixed
        proof = f" · {outcome.proof.summary}" if outcome.proof else ""
        console.print(("[green]✓ Fixed" if outcome.fixed else "[yellow]✗ Not fixed") + f"[/] (run {outcome.run_id}){proof}",
                      highlight=False)
        if outcome.error:
            console.print(f"[red]{outcome.error}[/]")
            break
    console.print(f"\n[bold]Fixed {fixed} of {len(report.bugs)} bug{'' if len(report.bugs) == 1 else 's'}.[/]")
    return 0 if fixed == len(report.bugs) else 1


class _SilentUI:
    """Shows nothing (for --json output). Ask mode never runs commands, so nothing needs approving."""

    def step(self, number: int, max_steps: int) -> None: ...
    def thought(self, text: str) -> None: ...
    def tool_call(self, name: str, args: dict, result: str) -> None: ...
    def approve_command(self, command: str) -> bool:
        return False


def run_ask(args: argparse.Namespace, repo: Path) -> int:
    import json as jsonlib

    from rich.console import Console
    from rich.markdown import Markdown

    from ghostpatch.ask import ask, render_flow
    from ghostpatch.fallback import make_client
    from ghostpatch.graph import CodeGraph
    from ghostpatch.providers import ProviderError, resolve
    from ghostpatch.session import describe_model_error
    from ghostpatch.ui import ConsoleUI

    console = Console()
    try:
        config = resolve(args.provider, args.model)
    except ProviderError as e:
        console.print(f"[red]{e}[/]")
        return 2
    ui = _SilentUI() if args.json else ConsoleUI(console, approval="ask")  # keep JSON output clean
    client = make_client(config, ui, fallback=not args.no_fallback)
    graph = CodeGraph(repo)
    try:
        answer = ask(repo, config, client, ui, args.question, graph=graph, max_steps=args.max_steps,
                     describe_error=lambda e: describe_model_error(e, client, config))
    finally:
        graph.close()
    if args.json:
        print(jsonlib.dumps(answer.as_dict(), indent=1))
        return 0 if answer.found else 1
    console.rule("💬 Answer")
    console.print(Markdown(answer.text or "_No answer._"))
    tree = render_flow(answer.flow)
    if tree:
        console.print("\n[bold]Call flow[/] [dim](from the code graph)[/]")
        console.print(tree, highlight=False, markup=False)
    console.print(f"\n[dim]{answer.steps} steps · {answer.prompt_tokens + answer.completion_tokens:,} tokens[/]")
    if answer.error:
        console.print(f"[red]{answer.error}[/]")
        return 2
    return 0 if answer.found else 1


def run_nightshift_command(args: argparse.Namespace, repo: Path) -> int:
    from rich.console import Console
    from rich.markdown import Markdown

    from ghostpatch import cifix, github, nightshift
    from ghostpatch.policy import auto_approves

    console = Console()
    if args.reports:
        reports = nightshift.list_reports(repo)
        for r in reports:
            prs = sum(i["status"] == "pr" for i in r["items"])
            console.print(f"{r['started']}  {prs} pull request(s)  {len(r['items'])} item(s)  [dim]{r.get('path')}[/]")
        if not reports:
            console.print("No night shift reports yet.")
        return 0

    approval = approval_mode(args)
    if approval == "ask":
        console.print("[red]The night shift runs unattended, so nobody can answer approval prompts.[/]\n"
                      "Use --approve safe (tests and read-only git commands run; anything else is declined) "
                      "or --approve all (only in a sandbox such as CI).")
        return 2

    from ghostpatch.fallback import make_client
    from ghostpatch.providers import ProviderError, resolve
    from ghostpatch.ui import ConsoleUI

    try:
        config = resolve(args.provider, args.model)
    except ProviderError as e:
        console.print(f"[red]{e}[/]")
        return 2
    ui = ConsoleUI(console, approval=approval)
    ui.banner(str(repo), f"{config.model} ({config.provider.name}) · approvals: {approval} · 🌙 night shift")
    client = make_client(config, ui, fallback=not args.no_fallback)
    try:
        report = nightshift.run_nightshift(
            repo, config, client, ui, approve=lambda command: auto_approves(approval, command),
            label=args.label, limit=args.limit, haunt_targets=args.haunt, poltergeist=args.poltergeist,
            candidates=args.candidates, draft=args.draft, comment=args.comment, max_steps=args.max_steps,
            say=lambda text: console.rule(text),
        )
    except (nightshift.NightShiftError, github.GitHubError) as e:
        console.print(f"[red]{e}[/]")
        return 2
    console.rule()
    console.print(Markdown(report.markdown()))
    console.print(f"\n[dim]Saved to {report.path}[/]")
    cifix.step_summary(report.markdown())
    return 2 if report.stopped and not report.of("pr") else 0


def run_workflow(args: argparse.Namespace, repo: Path) -> int:
    from ghostpatch import workflows

    if not args.name:
        for w in workflows.WORKFLOWS.values():
            state = "added" if workflows.target(repo, w.name).exists() else "not added"
            print(f"  {w.name:<11} {w.title}: {w.description}  [{state}]")
        print("\nAdd one with `ghostpatch workflow NAME --write`.")
        return 0
    if not args.write:
        print(workflows.WORKFLOWS[args.name].yaml)
        return 0
    try:
        path = workflows.install(repo, args.name, overwrite=args.force)
    except FileExistsError as e:
        print(f"{e} Use --force to replace it.")
        return 1
    print(f"Wrote {path}.\nCommit and push it, add a free API key (e.g. GROQ_API_KEY) as a repository secret, "
          "and allow Actions to create pull requests (Settings → Actions → General).")
    return 0


def run_share(args: argparse.Namespace, repo: Path) -> int:
    from ghostpatch import history
    from ghostpatch.replay import export_html

    try:
        run = history.load_run(repo, args.run_id) if args.run_id else history.list_runs(repo)[0]
    except (history.UndoError, IndexError):
        print("No runs recorded yet.")
        return 1
    out = Path(args.out or f"ghostpatch-run-{run['id']}.html")
    out.write_text(export_html(run), encoding="utf-8")
    print(f"Saved {out.resolve()}. Open it in a browser, or send it to anyone: it needs nothing else.")
    return 0


def run_memory(args: argparse.Namespace, repo: Path) -> int:
    from ghostpatch import memory

    if args.clear:
        print(f"Forgot {memory.forget_all(repo)} note(s).")
        return 0
    if args.add:
        print(f"Remembered: {memory.remember(repo, args.add)}")
        return 0
    text = memory.prompt_section(repo)
    print(text or f"Nothing yet. Add team conventions to {memory.TEAM_FILE}, or let the ghost learn as it works.")
    return 0


def run_timelapse(args: argparse.Namespace, repo: Path) -> int:
    import json as jsonlib

    from ghostpatch.timelapse import TimelapseError, timelapse

    try:
        data = timelapse(repo, commits=args.commits)
    except TimelapseError as e:
        print(e)
        return 2
    for frame in data["frames"]:
        s = frame["stats"]
        change = f"+{len(frame['added'])} -{len(frame['removed'])}"
        print(f"{frame['commit']}  {frame['date']}  {s['files']:>4} files  {s['symbols']:>5} symbols  "
              f"{s['calls']:>6} calls  {change:>9}  {frame['message'][:50]}")
    if args.json:
        Path(args.json).write_text(jsonlib.dumps(data), encoding="utf-8")
        print(f"Saved {len(data['frames'])} frames to {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
