"""Command-line entry point.

    ghostpatch init       set up a model provider and API key (once per computer)
    ghostpatch doctor     check the setup
    ghostpatch fix        fix a bug from the terminal
    ghostpatch serve      open the live dashboard
    ghostpatch graph      ask the code graph a question
    ghostpatch history    list past runs
    ghostpatch undo       roll back a run
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

    init = sub.add_parser("init", help="Set up a model provider and API key.")
    init.add_argument("--local", action="store_true", help="Save to ./.env instead of your user settings.")

    doctor = sub.add_parser("doctor", help="Check your setup and say what to fix.")
    add_repo(doctor)
    add_model(doctor)
    doctor.add_argument("--offline", action="store_true", help="Skip the check that contacts the model provider.")

    fix = sub.add_parser("fix", help="Fix a bug described in plain English.")
    fix.add_argument("issue", help="The bug description, or @path/to/issue.md to read it from a file.")
    add_agent_options(fix)

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
        "graph": run_graph, "history": run_history, "undo": run_undo,
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
    from rich.prompt import Prompt

    from ghostpatch.config import user_config_path, write_settings

    console = Console()
    target = Path(".env").resolve() if args.local else user_config_path()
    console.print("\n[bold magenta]👻 GhostPatch setup[/]\n")
    for name, p in PROVIDERS.items():
        cost = "[green]free[/]" if p.free else "paid"
        console.print(f"  [bold]{name:<7}[/] {cost:<18} {p.signup_url}")
    provider_name = Prompt.ask("\nWhich provider?", choices=list(PROVIDERS), default="groq", console=console)
    provider = PROVIDERS[provider_name]

    values: dict[str, str | None] = {"GHOSTPATCH_PROVIDER": provider_name}
    if provider.key_env:
        console.print(f"Get a key at [link={provider.signup_url}]{provider.signup_url}[/link]")
        key = Prompt.ask(f"Paste your {provider.key_env} (hidden)", password=True, console=console).strip()
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
        print(f"{e}\nOr run `ghostpatch init` to set up a provider.")
        return 2
    return serve(
        repo, config, port=args.port, max_steps=args.max_steps, approval=approval_mode(args),
        use_graph=not args.no_graph, open_browser=not args.no_browser,
    )


def run_fix(args: argparse.Namespace, repo: Path) -> int:
    # Imported lazily so `ghostpatch --help` stays fast.
    import openai
    from rich.console import Console

    from ghostpatch import history
    from ghostpatch.agent import Agent
    from ghostpatch.gitutil import is_git_repo
    from ghostpatch.providers import ProviderError, describe_api_error, resolve
    from ghostpatch.tools import Workspace
    from ghostpatch.ui import ConsoleUI

    console = Console()
    try:
        config = resolve(args.provider, args.model)
    except ProviderError as e:
        console.print(f"[red]{e}[/]\nOr run [bold]ghostpatch init[/] to set up a provider.")
        return 2

    issue = args.issue
    if issue.startswith("@"):
        issue = Path(issue[1:]).read_text(encoding="utf-8")

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
    workspace = Workspace(repo, approve_command=ui.approve_command, graph=graph)
    agent = Agent(config.client(), config.model, workspace, ui, max_steps=args.max_steps)

    def save(error: str | None = None) -> str | None:
        result = agent.result
        if result is None or not workspace.changed_files and error:
            return None
        return history.save_run(
            repo, issue=issue, provider=config.provider.name, model=config.model,
            fixed=result.fixed, summary=result.summary or (error or ""), steps=result.steps,
            prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
            changed_files=workspace.changed_files, originals=workspace.originals, error=error,
        )

    try:
        result = agent.run(issue)
    except openai.APIError as e:
        message = describe_api_error(e, config.provider)
        console.print(f"[red]{message}[/]", highlight=False)
        run_id = save(error=message)
        if run_id:
            console.print(f"[dim]Partial changes saved as run {run_id}. Undo with `ghostpatch undo`.[/]")
        return 2
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user.[/]")
        run_id = save(error="stopped by user")
        if run_id:
            console.print(f"[dim]Partial changes saved as run {run_id}. Undo with `ghostpatch undo`.[/]")
        return 130

    run_id = save()
    console.rule()
    headline = "[bold green]👻 Bug fixed![/]" if result.fixed else "[bold yellow]👻 Not fixed.[/]"
    console.print(headline)
    console.print(result.summary)
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
    return 0 if result.fixed else 1


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


if __name__ == "__main__":
    raise SystemExit(main())
