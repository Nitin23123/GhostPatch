"""The agent loop: the model decides, the workspace acts, repeat until done."""

from __future__ import annotations

import json
import platform
import time
from dataclasses import dataclass
from typing import Any, Protocol

import openai

from ghostpatch.memory import prompt_section
from ghostpatch.tools import GRAPH_TOOLS, TOOL_SCHEMAS, Workspace

RATE_LIMIT_RETRIES = 4

SYSTEM_PROMPT = """You are GhostPatch, an autonomous software engineer that fixes bugs in a code repository.

Work like a careful senior engineer:
1. Explore: find the code related to the issue (list_files, search_code, read_file).
2. Reproduce: if the project has tests or can be run, confirm the bug before changing anything.
   When practical, add a small regression test that fails because of the bug.
3. Fix: make the smallest change that fixes the root cause. Match the existing code style.
4. Verify: run the relevant tests again. If they fail, investigate and try again.
5. Finish: call `finish` with a short summary of the root cause and your change.
{graph_guide}
Rules:
- Read a file before you edit it, and copy old_text exactly, including indentation.
  If old_text is not unique, use replace_lines with the line numbers from read_file.
- Do not weaken or delete existing tests to make them pass unless the test itself is wrong.
- Never run destructive commands (deleting files, git push, git reset, global installs).
- Shell commands run on {os} with the repository root as the working directory.{shell_hint}
- Keep your thinking between tool calls short.
- Always act through tool calls. When you are done, you MUST call the `finish` tool.
- If you learn a lasting fact about the project (how its tests run, a convention), save it with `remember`.
"""
WINDOWS_HINT = " They run in cmd.exe: no heredocs or bash syntax; use `python -c \"...\"` for snippets."

GRAPH_GUIDE = """
You also have a code graph of the repository's Python, JavaScript and TypeScript code. Prefer it over text search for structure:
- find_symbol: where something is defined.  find_callers / find_callees: how code connects.
- related_tests: which tests to run.  impact_of_change: check this BEFORE editing a function
  that other code depends on, and run the tests it lists afterwards.
The user message includes a map of the repository to help you start.
"""

NUDGE = (
    "Reply with a tool call. If you have fixed and verified the bug, call `finish` now "
    "with a summary and fixed=true; otherwise continue working with the tools."
)
MAX_TEXT_REPLIES = 3  # consecutive replies without a tool call before we stop
BAD_JSON_HINT = "Your last tool call had invalid JSON arguments. Reply with one tool call whose arguments are valid JSON."


class UI(Protocol):
    def step(self, number: int, max_steps: int) -> None: ...
    def thought(self, text: str) -> None: ...
    def tool_call(self, name: str, args: dict, result: str) -> None: ...


@dataclass
class RunResult:
    fixed: bool
    summary: str
    steps: int
    prompt_tokens: int = 0
    completion_tokens: int = 0


class Agent:
    def __init__(self, client: Any, model: str, workspace: Workspace, ui: UI, max_steps: int = 30,
                 system_prompt: str | None = None, exclude_tools: frozenset[str] = frozenset()):
        self.system_prompt = system_prompt or SYSTEM_PROMPT  # a template with {os}, {shell_hint}, {graph_guide}
        self.client = client
        self.model = model
        self.workspace = workspace
        self.ui = ui
        self.max_steps = max_steps
        self.result: RunResult | None = None  # kept so a failed run can still be recorded
        has_graph = workspace.graph is not None
        self.tools = [t for t in TOOL_SCHEMAS
                      if (has_graph or t["function"]["name"] not in GRAPH_TOOLS)
                      and t["function"]["name"] not in exclude_tools]

    def run(self, issue: str) -> RunResult:
        graph = self.workspace.graph
        system = self.system_prompt.format(
            os=platform.system(),
            shell_hint=WINDOWS_HINT if platform.system() == "Windows" else "",
            graph_guide=GRAPH_GUIDE if graph else "",
        )
        intro = f"Repository root: {self.workspace.root}\n\n"
        notes = prompt_section(self.workspace.root)
        if notes:
            intro += f"{notes}\n\n"
        if graph:
            graph.refresh()
            intro += f"Repository map (classes, functions and tests):\n{graph.repo_map()}\n\n"
        messages: list[dict] = [
            {"role": "system", "content": system},
            {"role": "user", "content": f"{intro}Issue to fix:\n{issue}"},
        ]
        self.result = result = RunResult(fixed=False, summary="", steps=0)
        text_replies = 0

        for step in range(1, self.max_steps + 1):
            result.steps = step
            self.ui.step(step, self.max_steps)

            response = self._complete(messages)
            if response.usage:
                result.prompt_tokens += response.usage.prompt_tokens
                result.completion_tokens += response.usage.completion_tokens

            message = response.choices[0].message
            if message.content and _parse_finish(message.content) is None:
                self.ui.thought(message.content)

            tool_calls = message.tool_calls or []
            assistant: dict = {"role": "assistant", "content": message.content}
            if tool_calls:
                assistant["tool_calls"] = [_tool_call_dict(call) for call in tool_calls]
            messages.append(assistant)

            if not tool_calls:
                # Some models write the finish arguments as plain text instead of calling the tool.
                finish_args = _parse_finish(message.content)
                if finish_args is not None:
                    result.fixed = bool(finish_args.get("fixed", False))
                    result.summary = str(finish_args.get("summary", ""))
                    return result
                text_replies += 1
                if text_replies >= MAX_TEXT_REPLIES:
                    result.summary = (message.content or "") + "\n\n(The model stopped without confirming the fix.)"
                    return result
                messages.append({"role": "user", "content": NUDGE})
                continue
            text_replies = 0

            done = False
            for call in tool_calls:
                # Some models prefix tool names with a namespace, e.g. "repo_browser.list_files".
                name = call.function.name.rsplit(".", 1)[-1]
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args, output = {}, "Error: the arguments were not valid JSON."
                else:
                    if name == "finish":
                        done = True
                        result.fixed = bool(args.get("fixed", False))
                        result.summary = str(args.get("summary", ""))
                        output = "Finished."
                    else:
                        output = self.workspace.call(name, args)
                self.ui.tool_call(name, args, output)
                messages.append({"role": "tool", "tool_call_id": call.id, "content": output})

            if done:
                return result

        result.summary = f"Stopped after reaching the limit of {self.max_steps} steps."
        return result

    def _complete(self, messages: list[dict]) -> Any:
        """Call the model, waiting and retrying on rate limits and temporary overloads."""
        for attempt in range(RATE_LIMIT_RETRIES + 1):
            try:
                return self.client.chat.completions.create(model=self.model, messages=messages, tools=self.tools)
            except openai.BadRequestError as e:
                # Some providers (e.g. Groq) reject a reply when the model wrote malformed tool-call JSON.
                # Asking again usually works; if it repeats, tell the model what went wrong.
                if "tool_use_failed" not in str(e) or attempt == RATE_LIMIT_RETRIES:
                    raise
                if attempt == 1:
                    messages.append({"role": "user", "content": BAD_JSON_HINT})
                self.ui.thought("_The model produced a malformed tool call. Retrying…_")
            except (openai.RateLimitError, openai.InternalServerError) as e:
                text = str(e)
                # Waiting a few seconds won't help when credits or the daily quota are used up.
                out_of_quota = any(s in text for s in ("insufficient_quota", "credit_balance", "PerDay", "per day"))
                if out_of_quota or attempt == RATE_LIMIT_RETRIES:
                    raise
                wait = 10 * (attempt + 1)
                reason = "Rate limited" if isinstance(e, openai.RateLimitError) else "The provider is busy"
                self.ui.thought(f"_{reason}. Waiting {wait}s and retrying…_")
                time.sleep(wait)


def _parse_finish(content: str | None) -> dict | None:
    """Return finish arguments if the model wrote them as a JSON reply instead of a tool call."""
    if not content:
        return None
    text = content.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and "summary" in data and "fixed" in data:
        return data
    return None


def _tool_call_dict(call: Any) -> dict:
    """Echo a tool call back to the model exactly as it was sent.

    Some providers attach extra fields that must be returned unchanged, e.g. Gemini's
    `extra_content.google.thought_signature`, so we keep everything the SDK received.
    """
    data = call.model_dump(exclude_none=True) if hasattr(call, "model_dump") else {}
    data.update({"id": call.id, "type": "function"})
    data["function"] = {"name": call.function.name, "arguments": call.function.arguments}
    return data
