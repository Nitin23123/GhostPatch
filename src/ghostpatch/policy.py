"""When may the agent run a shell command without asking?

Three approval modes:
- ask:  every command needs the user's approval (the default).
- safe: well-known test runners and read-only git commands run automatically;
        everything else still asks.
- all:  everything runs without asking. Only for sandboxes.

"safe" removes friction, but it is not a sandbox: a test run executes the project's
own code, including any test file the agent wrote.
"""

from __future__ import annotations

import re

APPROVAL_MODES = ("ask", "safe", "all")
DEFAULT_APPROVAL = "ask"

# Any of these characters could chain, redirect or substitute commands.
_SHELL_METACHARACTERS = set("&|;<>`$()\n\r%^!")
_ARGS = r"(?:\s+[\w./\\:=,@+*'\"\[\]{}#~-]+)*"
_SAFE_PATTERNS = [
    rf"(?:python3?|py)(?:\s+-\d(?:\.\d+)?)?\s+-m\s+(?:pytest|unittest){_ARGS}",
    rf"pytest{_ARGS}",
    rf"node\s+--test{_ARGS}",
    rf"(?:npm|pnpm|yarn)\s+(?:test|run\s+test){_ARGS}",
    rf"npx\s+(?:jest|vitest|mocha){_ARGS}",
    rf"go\s+test{_ARGS}",
    rf"cargo\s+test{_ARGS}",
    rf"dotnet\s+test{_ARGS}",
    rf"git\s+(?:status|diff|log|show){_ARGS}",
]
_SAFE_RE = re.compile("|".join(f"(?:{p})" for p in _SAFE_PATTERNS), re.IGNORECASE)


def is_safe_command(command: str) -> bool:
    """True for a single, recognised test or read-only git command."""
    command = command.strip()
    if not command or any(ch in _SHELL_METACHARACTERS for ch in command):
        return False
    return _SAFE_RE.fullmatch(command) is not None


def auto_approves(mode: str, command: str) -> bool:
    """Whether a command may run without asking under this approval mode."""
    if mode == "all":
        return True
    if mode == "safe":
        return is_safe_command(command)
    return False
