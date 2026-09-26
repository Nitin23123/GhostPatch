"""When may the agent run a shell command without asking?

Three approval modes:
- ask:  every command needs the user's approval (the default).
- safe: well-known test runners and read-only git commands run automatically;
        everything else still asks.
- all:  everything runs without asking. Only for sandboxes.

"safe" removes friction, but it is not a sandbox: a test run executes the project's
own code, including any test file the agent wrote. Options that reach outside the repository
(writing files elsewhere, other settings files, chosen code) always ask, and every command
runs without GhostPatch's API keys and GitHub tokens in its environment.
"""

from __future__ import annotations

import os
import re

APPROVAL_MODES = ("ask", "safe", "all")
DEFAULT_APPROVAL = "ask"

# Any of these characters could chain, redirect or substitute commands.
_SHELL_METACHARACTERS = set("&|;<>`$()\n\r%^!")
_ARGS = r"(?:\s+[\w./\\:=,@+*'\"\[\]{}#~-]+)*"
_MAVEN = r"(?:mvn|(?:\.[/\\])?mvnw(?:\.cmd)?)"  # Maven, or the project's own wrapper script
_GRADLE = r"(?:gradle|(?:\.[/\\])?gradlew(?:\.bat)?)"
# A Python interpreter: `python`, `python3.12`, `py`, or an absolute path to one (quoted or not).
# Not a relative path: `tools\python` would let cmd.exe run a `tools\python.bat` the agent wrote.
_PYTHON = (r"(?:\"(?:[A-Za-z]:\\[^\"]*\\python[\d.]*\.exe|/[^\"]*/python[\d.]*)\"|[A-Za-z]:\\\S*\\python[\d.]*\.exe"
           r"|/\S*/python[\d.]*|python[\d.]*(?:\.exe)?|py)")
_SAFE_PATTERNS = [
    # python / py / a full path to a Python executable, then -m pytest or unittest
    rf"{_PYTHON}(?:\s+-\d(?:\.\d+)?)?\s+-m\s+(?:pytest|unittest){_ARGS}",
    rf"pytest{_ARGS}",
    rf"node\s+--test{_ARGS}",
    rf"(?:npm|pnpm|yarn)\s+(?:test|run\s+test){_ARGS}",
    rf"npx\s+(?:jest|vitest|mocha){_ARGS}",
    rf"go(?:\s+-C\s+[\w./\\-]+)?\s+(?:test|vet){_ARGS}",
    rf"cargo\s+(?:test|check|build){_ARGS}",
    rf"{_MAVEN}(?:\s+-[\w.:=,@-]+)*\s+test{_ARGS}",
    rf"{_GRADLE}(?:\s+-[\w.:=,@-]+)*\s+(?:[\w-]*:)*test{_ARGS}",
    rf"dotnet\s+test{_ARGS}",
    rf"git\s+(?:status|diff|log|show){_ARGS}",
]
_SAFE_RE = re.compile("|".join(f"(?:{p})" for p in _SAFE_PATTERNS), re.IGNORECASE)


# Options that would write files elsewhere, read outside the repository, load other settings or run
# chosen code. A command using one isn't "safe", however it starts: it asks.
_RISKY_OPTIONS = {
    "git": {"--output", "--no-index", "--ext-diff", "--textconv", "-c", "--exec-path", "--git-dir", "--work-tree",
            "--config-env"},
    "pytest": {"--basetemp", "--rootdir", "--confcutdir", "-c", "--config-file", "-o", "--override-ini", "-p", "--junitxml",
               "--junit-xml", "--debug", "--log-file", "--report-log", "--html", "--resultlog"},
    "node": {"-e", "--eval", "-p", "--print", "-r", "--require", "--import", "--loader", "--experimental-loader",
             "--test-reporter-destination"},
    "npm": {"--prefix", "--script-shell", "--userconfig", "--globalconfig", "--cache"},
    "npx": {"-c", "--call", "--config", "--outputFile", "--output-file", "--coverageDirectory", "--rootDir"},
    "go": {"-exec", "-toolexec", "-o", "-coverprofile", "-cpuprofile", "-memprofile", "-blockprofile",
           "-mutexprofile", "-trace", "-outputdir", "-modfile", "-overlay", "-pkgdir"},
    "cargo": {"--config", "-Z", "--manifest-path", "--target-dir", "--artifact-dir", "--out-dir"},
    "maven": {"-f", "--file", "-s", "--settings", "-gs", "--global-settings", "-t", "--toolchains", "-l", "--log-file"},
    "gradle": {"-I", "--init-script", "-c", "--settings-file", "-b", "--build-file", "-p", "--project-dir", "-g",
               "--gradle-user-home", "--project-cache-dir"},
    "dotnet": {"--logger", "-l", "-o", "--output", "--results-directory", "-r", "--diag", "-d", "-s", "--settings"},
}
_FAMILIES = {"git": "git", "pytest": "pytest", "python": "pytest", "py": "pytest", "node": "node", "npm": "npm",
             "pnpm": "npm", "yarn": "npm", "npx": "npx", "go": "go", "cargo": "cargo", "mvn": "maven",
             "mvnw": "maven", "gradle": "gradle", "gradlew": "gradle", "dotnet": "dotnet"}
_OUTSIDE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/])|(?:^|[\\/])\.\.(?:[\\/]|$)")  # an absolute path, or one going up


def _risky(command: str) -> bool:
    """Whether a command that looks like a test run uses an option or a path that reaches further."""
    if command.startswith('"'):
        program, _, rest = command[1:].partition('"')
    else:
        program, _, rest = command.partition(" ")
    name = re.sub(r"\.(?:exe|cmd|bat)$", "", re.split(r"[\\/]", program)[-1].lower())
    family = _FAMILIES.get(re.sub(r"[\d.]+$", "", name), name)
    if family == "pytest" and re.search(r"-m\s+unittest\b", rest):
        family = "unittest"  # its -p is a file pattern
    tokens = [t.strip("'\"") for t in rest.split()]
    for i, token in enumerate(tokens):
        option = token.split("=", 1)[0]
        if option in _RISKY_OPTIONS.get(family, ()):
            if family == "pytest" and option == "-p" and i + 1 < len(tokens) and tokens[i + 1].startswith("no:"):
                continue  # -p no:cacheprovider turns a plugin off
            return True
        if family == "pytest" and re.match(r"-[co].", token):  # -oinifile=..., -cfile
            return True
        if family == "maven" and token.startswith("-D") and not token.startswith(("-Dtest=", "-Dsurefire.")):
            return True
        if family == "gradle" and token.startswith(("-D", "-P")):
            return True
        value = token.split("=", 1)[-1]
        if family != "git" and (_OUTSIDE.search(value) or _OUTSIDE.search(token)):
            return True  # git refuses paths outside the repository by itself; its ranges use ".."
    return False


def is_safe_command(command: str) -> bool:
    """True for a single, recognised test or read-only git command that stays inside the repository."""
    command = command.strip()
    if not command or any(ch in _SHELL_METACHARACTERS for ch in command):
        return False
    return _SAFE_RE.fullmatch(command) is not None and not _risky(command)


_TEST_RUNNER_RE = re.compile(
    # python, py -3.12, or a (quoted) full path to a Python executable, then -m pytest / unittest
    r"^\s*(?:(?:\"[^\"]*python[\d.]*(?:\.exe)?\"|\S*python[\d.]*(?:\.exe)?|py)(?:\s+-\d(?:\.\d+)?)?"
    r"\s+-m\s+(?:pytest|unittest)|pytest|node\s+--test|"
    r"(?:npm|pnpm|yarn)\s+(?:run\s+)?test|npx\s+(?:jest|vitest|mocha)|go(?:\s+-C\s+\S+)?\s+test|cargo\s+test|"
    rf"dotnet\s+test|{_MAVEN}(?:\s+-\S+)*\s+test|{_GRADLE}(?:\s+-\S+)*\s+(?:[\w-]*:)*test)\b",
    re.IGNORECASE,
)


def is_test_command(command: str) -> bool:
    """True if a command runs a test suite (used to know whether a fix was verified)."""
    return _TEST_RUNNER_RE.match(command) is not None


# ---------------------------------------------------------------- what the agent may touch

PROTECTED_DIRS = {".git", ".hg", ".svn", ".ghostpatch"}
SECRET_FILE = re.compile(r"\.env(?:\..+)?", re.IGNORECASE)
SHAREABLE_ENV = re.compile(r"\.env\.(?:example|sample|template|dist|defaults?)", re.IGNORECASE)


def protected_path(rel_path: str) -> str | None:
    """Why the agent may not read or write this path, or None if it may.

    `.git` and `.ghostpatch` hold hooks, git config and GhostPatch's own run history: writing
    there would let a model run code or rewrite what `undo` does without any approval. `.env`
    files hold secrets, which a prompt-injected issue could otherwise get into a pull request."""
    parts = [p for p in rel_path.replace("\\", "/").split("/") if p not in ("", ".")]
    if any(p.lower() in PROTECTED_DIRS for p in parts):
        return f"'{rel_path}' is inside {next(p for p in parts if p.lower() in PROTECTED_DIRS)}/, which the ghost may not touch."
    if parts and SECRET_FILE.fullmatch(parts[-1]) and not SHAREABLE_ENV.fullmatch(parts[-1]):
        return f"'{rel_path}' holds secrets, so the ghost may not read or change it."
    return None


# The credentials GhostPatch and GitHub Actions bring along: model API keys, GitHub tokens, the
# Actions OIDC / runtime tokens. (A project's own variables its tests may need are left alone.)
SECRET_ENV = re.compile(r".*_API_KEY$|^(?:GH|GITHUB|GH_ENTERPRISE)_TOKEN$|^ACTIONS_(?:ID_TOKEN_\w*|RUNTIME_TOKEN)$",
                        re.IGNORECASE)


def command_env() -> dict[str, str]:
    """The environment for commands the agent runs: without API keys and tokens, so a command
    (or test code the model wrote) can't send them anywhere."""
    env = {k: v for k, v in os.environ.items() if not SECRET_ENV.match(k)}
    env["NoDefaultCurrentDirectoryInExePath"] = "1"  # Windows: don't run a `python.bat` planted in the repo
    return env


def secret_values() -> list[str]:
    """The values of secret environment variables (for redaction), longest first."""
    return sorted({v for k, v in os.environ.items() if SECRET_ENV.match(k) and len(v) >= 8}, key=len, reverse=True)


def redact(text: str) -> str:
    """`text` with every secret environment variable's value replaced by [redacted]."""
    for value in secret_values():
        text = text.replace(value, "[redacted]")
    return text


def auto_approves(mode: str, command: str) -> bool:
    """Whether a command may run without asking under this approval mode."""
    if mode == "all":
        return True
    if mode == "safe":
        return is_safe_command(command)
    return False
