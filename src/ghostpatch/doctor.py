"""`ghostpatch doctor`: check the whole setup and say exactly what to fix."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ghostpatch.config import settings_files, user_config_path
from ghostpatch.policy import APPROVAL_MODES, DEFAULT_APPROVAL


@dataclass
class Check:
    status: str  # ok | warn | fail
    name: str
    detail: str


def run_checks(repo: Path, provider_name: str | None = None, model: str | None = None,
               online: bool = True, approval: str | None = None) -> list[Check]:
    """`approval` is the mode actually in use (the dashboard's), if it isn't the configured default."""
    checks = [_python(), _settings(repo)]
    checks += _model_checks(provider_name, model, online)
    checks += [_graph(repo), _git(repo), _node(repo), _approvals(approval)]
    return checks


def _python() -> Check:
    version = ".".join(map(str, sys.version_info[:3]))
    if sys.version_info >= (3, 10):
        return Check("ok", "Python", version)
    return Check("fail", "Python", f"{version}: GhostPatch needs Python 3.10 or newer")


def _settings(repo: Path) -> Check:
    files = settings_files(repo)
    if files:
        return Check("ok", "Settings", ", ".join(str(f) for f in files))
    return Check("warn", "Settings", f"no settings file found. Run `ghostpatch init` (saves to {user_config_path()})")


def _model_checks(provider_name: str | None, model: str | None, online: bool) -> list[Check]:
    from ghostpatch.providers import ProviderError, describe_api_error, resolve

    try:
        config = resolve(provider_name, model)
    except ProviderError as e:
        return [Check("fail", "Model provider", str(e).replace("\n", " "))]
    checks = [Check("ok", "Model provider", f"{config.provider.name} · {config.model}"
                    + (" (free tier)" if config.provider.free else "")), _fallback(config)]
    if not online:
        return checks

    import openai

    try:
        models = config.client().with_options(timeout=15, max_retries=0).models.list()
        ids = {m.id.removeprefix("models/") for m in models.data}
    except openai.APIError as e:
        checks.append(Check("fail", "Connection", describe_api_error(e, config.provider)))
        return checks
    checks.append(Check("ok", "Connection", f"reached {config.provider.name}, API key accepted"))
    if ids and config.model not in ids:
        close = sorted(i for i in ids if config.model.split("/")[-1].split("-")[0] in i)[:5]
        hint = f" Similar: {', '.join(close)}" if close else ""
        checks.append(Check("warn", "Model", f"'{config.model}' is not in {config.provider.name}'s model list.{hint}"))
    else:
        checks.append(Check("ok", "Model", f"{config.model} is available"))
    return checks


def _fallback(config) -> Check:
    from ghostpatch.providers import fallback_chain

    chain = fallback_chain(config)
    if len(chain) > 1:
        return Check("ok", "Fallback", " → ".join(c.provider.name for c in chain) + " when a daily quota runs out")
    if os.environ.get("GHOSTPATCH_FALLBACK", "").strip().lower() == "none":
        return Check("ok", "Fallback", "turned off (GHOSTPATCH_FALLBACK=none)")
    return Check("warn", "Fallback", f"only {config.provider.name} is set up, so a used-up free quota stops the run. "
                                     "Add a backup key with `ghostpatch init`.")


def _graph(repo: Path) -> Check:
    try:
        from ghostpatch.graph import CodeGraph
        from ghostpatch.parsers import _js_parser

        _js_parser("typescript")
    except Exception as e:
        return Check("fail", "Code graph", f"tree-sitter could not load ({e}). Reinstall GhostPatch.")
    graph = CodeGraph(repo)
    try:
        graph.refresh()
        s = graph.stats()
    finally:
        graph.close()
    if not s["files"]:
        return Check("warn", "Code graph", "no Python, JavaScript or TypeScript files found in this folder")
    errors = f", {s['parse_errors']} could not be parsed" if s["parse_errors"] else ""
    return Check("ok", "Code graph", f"{s['files']} files, {s['symbols']} symbols, {s['calls']} calls{errors}")


def _git(repo: Path) -> Check:
    from ghostpatch.gitutil import is_git_repo

    if not shutil.which("git"):
        return Check("warn", "git", "not installed; `ghostpatch undo` still works")
    if not is_git_repo(repo):
        return Check("warn", "git", "this folder is not a git repository; `ghostpatch undo` still works")
    return Check("ok", "git", "repository detected")


def _node(repo: Path) -> Check:
    node = shutil.which("node")
    needs_node = (repo / "package.json").is_file()
    if node:
        version = subprocess.run([node, "--version"], capture_output=True, text=True).stdout.strip()
        return Check("ok", "Node.js", version)
    if needs_node:
        return Check("warn", "Node.js", "not installed, but this is a JavaScript project: the agent can't run its tests")
    return Check("ok", "Node.js", "not installed (only needed for JavaScript projects)")


def _approvals(mode: str | None = None) -> Check:
    mode = mode or os.environ.get("GHOSTPATCH_APPROVE", DEFAULT_APPROVAL)
    if mode not in APPROVAL_MODES:
        return Check("fail", "Approvals", f"GHOSTPATCH_APPROVE='{mode}' is not one of {', '.join(APPROVAL_MODES)}")
    detail = {
        "ask": "every command asks first",
        "safe": "test and read-only git commands run automatically; others ask",
        "all": "every command runs without asking. Use only in a sandbox!",
    }[mode]
    return Check("warn" if mode == "all" else "ok", "Approvals", f"{mode}: {detail}")
