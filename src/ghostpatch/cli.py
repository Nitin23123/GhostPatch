"""Command-line entry point: `ghostpatch fix`, `ghostpatch graph` and `ghostpatch serve`."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from dotenv import find_dotenv, load_dotenv

from ghostpatch import __version__
from ghostpatch.providers import DEFAULT_PROVIDER, PROVIDERS

DEFAULT_MAX_STEPS = 30
DEFAULT_PORT = 8765
GRAPH_QUERIES = ["map", "stats", "symbol", "callers", "callees", "tests", "impact"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghostpatch",
        description="👻 GhostPatch: an open-source AI software engineer that fixes bugs.",
    )
    parser.add_argument("--version", action="version", version=f"ghostpatch {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_agent_options(p: argparse.ArgumentParser) -> None:
        p.add_argument("--repo", default=".", help="Path to the repository (default: current directory).")
        p.add_argument(
            "--provider", choices=sorted(PROVIDERS), default=None,
            help=f"Model provider (default: $GHOSTPATCH_PROVIDER or {DEFAULT_PROVIDER}).",
        )
        p.add_argument("--model", default=None, help="Model name (default: $GHOSTPATCH_MODEL or the provider's default).")
        p.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS, help="Maximum agent steps.")
        p.add_argument("--yes", action="store_true", help="Run shell commands without asking. Only use in a sandbox!")
        p.add_argument("--no-graph", action="store_true", help="Don't build or use the code graph.")

    fix = sub.add_parser("fix", help="Fix a bug described in plain English.")
    fix.add_argument("issue", help="The bug description, or @path/to/issue.md to read it from a file.")
    add_agent_options(fix)

    serve = sub.add_parser("serve", help="Open the web dashboard and watch the ghost work live.")
    add_agent_options(serve)
    serve.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port to listen on (default: {DEFAULT_PORT}).")
    serve.add_argument("--no-browser", action="store_true", help="Don't open the browser automatically.")

    graph = sub.add_parser("graph", help="Explore the code graph yourself.")
    graph.add_argument("query", choices=GRAPH_QUERIES, help="What to ask the graph.")
    graph.add_argument("name", nargs="?", help="Function, method or class name (not needed for map/stats).")
    graph.add_argument("--repo", default=".", help="Path to the repository (default: current directory).")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # emoji-safe output on Windows terminals
    args = build_parser().parse_args(argv)

    repo = Path(args.repo).resolve()
    if not repo.is_dir():
        print(f"Repository not found: {repo}")
        return 2
    load_dotenv(repo / ".env")
    load_dotenv(find_dotenv(usecwd=True))

    if args.command == "fix":
        return run_fix(args, repo)
    if args.command == "serve":
        return run_serve(args, repo)
    if args.command == "graph":
        return run_graph(args, repo)
    return 1


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


def run_serve(args: argparse.Namespace, repo: Path) -> int:
    from ghostpatch.providers import ProviderError, resolve
    from ghostpatch.server import serve

    try:
        config = resolve(args.provider, args.model)
    except ProviderError as e:
        print(e)
        return 2
    return serve(
        repo, config, port=args.port, max_steps=args.max_steps, auto_approve=args.yes,
        use_graph=not args.no_graph, open_browser=not args.no_browser,
    )


def run_fix(args: argparse.Namespace, repo: Path) -> int:
    # Imported lazily so `ghostpatch --help` stays fast.
    import openai
    from rich.console import Console

    from ghostpatch.agent import Agent
    from ghostpatch.providers import ProviderError, describe_api_error, resolve
    from ghostpatch.tools import Workspace
    from ghostpatch.ui import ConsoleUI

    console = Console()
    try:
        config = resolve(args.provider, args.model)
    except ProviderError as e:
        console.print(f"[red]{e}[/]")
        return 2

    issue = args.issue
    if issue.startswith("@"):
        issue = Path(issue[1:]).read_text(encoding="utf-8")

    ui = ConsoleUI(console, auto_approve=args.yes)
    ui.banner(str(repo), f"{config.model} ({config.provider.name})")

    if not (repo / ".git").exists():
        console.print("[yellow]⚠ This folder is not a git repository, so changes can't be undone easily.[/]\n")

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

    try:
        result = agent.run(issue)
    except openai.APIError as e:
        console.print(f"[red]{describe_api_error(e, config.provider)}[/]", highlight=False)
        return 2
    except KeyboardInterrupt:
        console.print("\n[yellow]Stopped by user.[/]")
        return 130

    console.rule()
    headline = "[bold green]👻 Bug fixed![/]" if result.fixed else "[bold yellow]👻 Not fixed.[/]"
    console.print(headline)
    console.print(result.summary)
    console.print(
        f"\n[dim]{result.steps} steps · {result.prompt_tokens:,} input tokens · "
        f"{result.completion_tokens:,} output tokens[/]"
    )
    if workspace.changed_files:
        console.print("\n[bold]Changed files:[/] " + ", ".join(sorted(workspace.changed_files)))
        if (repo / ".git").exists():
            diff = subprocess.run(["git", "diff", "--stat"], cwd=repo, capture_output=True, text=True)
            console.print(diff.stdout, highlight=False, markup=False)
            console.print("[dim]Review with `git diff`. Undo with `git checkout -- <file>`.[/]")
    return 0 if result.fixed else 1


if __name__ == "__main__":
    raise SystemExit(main())
