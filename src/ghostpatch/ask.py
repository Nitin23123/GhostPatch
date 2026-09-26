"""💬 Ask the graph: questions about the codebase, answered from the code itself.

`ghostpatch ask "How does checkout work?"` runs a read-only agent: it can list, read and search
files and query the code graph, but it can't edit anything or run commands. It answers in
Markdown, citing code as `path:line`.

Then GhostPatch draws the call flow between the functions the answer mentions, straight from
the code graph, so the diagram shows how the code really connects rather than what the model
remembers.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ghostpatch.agent import Agent
from ghostpatch.tools import Workspace

MAX_FLOW_NODES = 25
READ_ONLY_EXCLUDED = frozenset({"edit_file", "replace_lines", "create_file", "run_command", "remember"})

ASK_PROMPT = """You are GhostPatch's code guide. Answer the user's question about this repository, using the code
itself as your only source of truth.

1. Find the relevant code: prefer the code graph (find_symbol, find_callers, find_callees, impact_of_change),
   then read_file and search_code.
2. Answer clearly and briefly in Markdown: what happens, in order, and why. Cite code as `path:line`.
   Put every function or method you mention in backticks with its name, e.g. `Cart.total` or `apply_discount`.
3. If the code doesn't answer the question, say so. Never guess.
4. Call `finish` with fixed=true and your whole answer as the summary.
{graph_guide}
Rules:
- You can only read: there are no tools to change files or run commands.
- Keep your thinking short. Always act through tool calls, and you MUST call the `finish` tool.
"""


@dataclass
class Answer:
    question: str
    text: str = ""
    found: bool = False
    flow: dict[str, list] = field(default_factory=lambda: {"nodes": [], "edges": []})
    steps: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"question": self.question, "text": self.text, "found": self.found, "flow": self.flow,
                "tree": render_flow(self.flow), "mermaid": mermaid(self.flow), "steps": self.steps,
                "tokens": self.prompt_tokens + self.completion_tokens, "error": self.error}


def _read_only(rel_path: str) -> str:
    return "Ask mode is read-only: files can't be changed."


def mentioned_flow(graph: Any, text: str) -> dict[str, list]:
    """The functions a text mentions in backticks, and the calls between them, from the graph."""
    graph.refresh()
    names = []
    for raw in re.findall(r"`([A-Za-z_$][\w.$]*)(?:\(\))?`", text):
        if raw not in names:
            names.append(raw)
    nodes: dict[str, dict] = {}
    by_id: dict[int, str] = {}
    for name in names:
        for sym_id, path, sym_name, qualname, kind, line, *_ in graph._match(name)[:3]:
            if kind in ("function", "method", "class", "test") and len(nodes) < MAX_FLOW_NODES:
                nodes.setdefault(qualname, {"qualname": qualname, "name": sym_name, "path": path, "line": line})
                by_id[sym_id] = qualname
    edges = set()
    if by_id:
        marks = ",".join("?" * len(by_id))
        rows = graph.db.execute(f"SELECT caller_id, callee FROM calls WHERE caller_id IN ({marks})", list(by_id))
        for caller_id, callee in rows:
            for qualname, node in nodes.items():
                if node["name"] == callee and qualname != by_id[caller_id]:
                    edges.add((by_id[caller_id], qualname))
    return {"nodes": list(nodes.values()), "edges": [{"source": a, "target": b} for a, b in sorted(edges)]}


def render_flow(flow: dict[str, list]) -> str:
    """The call flow as an indented tree, roots first."""
    if not flow["nodes"]:
        return ""
    where = {n["qualname"]: f"{n['path']}:{n['line']}" for n in flow["nodes"]}
    children: dict[str, list[str]] = defaultdict(list)
    called = set()
    for e in flow["edges"]:
        children[e["source"]].append(e["target"])
        called.add(e["target"])
    order = [n["qualname"] for n in flow["nodes"]]
    roots = [q for q in order if q not in called and children.get(q)] or [q for q in order if children.get(q)][:1]
    lines: list[str] = []
    shown: set[str] = set()

    def walk(qualname: str, prefix: str, last: bool, depth: int) -> None:
        branch = "" if depth == 0 else ("└─ " if last else "├─ ")
        if qualname in shown:  # drawn already: point back instead of repeating its subtree
            lines.append(f"{prefix}{branch}{qualname}  (see above)")
            return
        lines.append(f"{prefix}{branch}{qualname}  ({where[qualname]})")
        shown.add(qualname)
        kids = children.get(qualname, [])
        extension = "" if depth == 0 else ("   " if last else "│  ")
        for i, kid in enumerate(kids):
            walk(kid, prefix + extension, i == len(kids) - 1, depth + 1)

    for root in roots:
        walk(root, "", True, 0)
    loose = [q for q in order if q not in shown]
    if loose:
        lines.append("also mentioned: " + ", ".join(loose))
    return "\n".join(lines)


def mermaid(flow: dict[str, list]) -> str:
    if not flow["edges"]:
        return ""
    ids = {n["qualname"]: f"n{i}" for i, n in enumerate(flow["nodes"])}
    out = ["flowchart LR"] + [f'  {ids[n["qualname"]]}["{n["qualname"]}"]' for n in flow["nodes"]]
    out += [f"  {ids[e['source']]} --> {ids[e['target']]}" for e in flow["edges"]]
    return "\n".join(out)


def ask(repo: Path, config: Any, client: Any, ui: Any, question: str, *, graph: Any = None,
        max_steps: int = 15, describe_error: Any = str) -> Answer:
    """Answer a question about the repository with a read-only agent, plus the call flow it describes."""
    import openai

    answer = Answer(question)
    workspace = Workspace(repo, approve_command=lambda command: False, graph=graph)
    workspace.write_guard = _read_only
    agent = Agent(client, config.model, workspace, ui, max_steps=max_steps, system_prompt=ASK_PROMPT,
                  exclude_tools=READ_ONLY_EXCLUDED, task_heading="Question")
    try:
        result = agent.run(question)
    except openai.APIError as e:
        answer.error = describe_error(e)
        result = agent.result
    if result is not None:
        answer.text = result.summary.strip()
        answer.found = bool(result.fixed and answer.text)
        answer.steps, answer.prompt_tokens, answer.completion_tokens = (
            result.steps, result.prompt_tokens, result.completion_tokens)
    if graph is not None and answer.text:
        answer.flow = mentioned_flow(graph, answer.text)
    return answer
