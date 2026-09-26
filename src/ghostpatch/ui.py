"""Terminal output, so you can watch the ghost work."""

from __future__ import annotations

from rich.console import Console
from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm

from ghostpatch.policy import DEFAULT_APPROVAL, auto_approves

GHOST = r"""
   .-.
  (o o)   GhostPatch
  | O \   your bugs, fixed while you sleep
   \   \
    `~~~'
"""

TOOL_ICONS = {
    "list_files": "📂",
    "read_file": "📖",
    "search_code": "🔎",
    "edit_file": "✏️ ",
    "replace_lines": "✏️ ",
    "create_file": "🆕",
    "run_command": "⚡",
    "find_symbol": "🕸 ",
    "find_callers": "🕸 ",
    "find_callees": "🕸 ",
    "related_tests": "🧪",
    "impact_of_change": "💥",
    "finish": "🏁",
}
HIDDEN_ARGS = {"content", "old_text", "new_text"}  # too long for one line; shown separately


class ConsoleUI:
    def __init__(self, console: Console | None = None, approval: str = DEFAULT_APPROVAL):
        self.console = console or Console()
        self.approval = approval

    def banner(self, repo: str, model: str) -> None:
        self.console.print(f"[bold magenta]{escape(GHOST)}[/]")
        self.console.print(f"[dim]repo:[/] {escape(repo)}   [dim]model:[/] {escape(model)}\n")

    def step(self, number: int, max_steps: int) -> None:
        self.console.rule(f"[dim]step {number}/{max_steps}[/]", style="dim")

    def thought(self, text: str) -> None:
        self.console.print(Panel(Markdown(text), title="💭 thinking", border_style="dim", title_align="left"))

    def tool_call(self, name: str, args: dict, result: str) -> None:
        icon = TOOL_ICONS.get(name, "🔧")
        detail = ", ".join(f"{k}={_short(v)}" for k, v in args.items() if k not in HIDDEN_ARGS)
        self.console.print(f"{icon} [bold cyan]{name}[/]([dim]{escape(detail)}[/])")

        failed = result.startswith("Error")
        if name in ("edit_file", "replace_lines") and not failed:
            self.console.print(f"   [red]- {escape(_short(args.get('old_text', ''), 70))}[/]")
            self.console.print(f"   [green]+ {escape(_short(args.get('new_text', ''), 70))}[/]")
        preview = result if len(result) < 300 else result[:300] + " …"
        self.console.print(f"   [{'red' if failed else 'dim'}]{escape(preview)}[/]")

    def approve_command(self, command: str) -> bool:
        if auto_approves(self.approval, command):
            if self.approval == "safe":
                self.console.print(f"[dim]⚡ auto-approved (safe command): {escape(command)}[/]")
            return True
        self.console.print(f"\n⚡ The agent wants to run: [bold yellow]{escape(command)}[/]")
        return Confirm.ask("   Allow?", default=True, console=self.console)


def _short(value: object, limit: int = 60) -> str:
    text = str(value).replace("\n", "⏎ ")
    return text if len(text) <= limit else text[: limit - 1] + "…"
