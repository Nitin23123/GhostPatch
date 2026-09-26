"""Tests the agent loop with a scripted fake model, so no API key or network is needed."""

import json
from pathlib import Path
from types import SimpleNamespace

from ghostpatch.agent import Agent
from ghostpatch.tools import Workspace


def tool_call(call_id: str, name: str, **args):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    usage = SimpleNamespace(prompt_tokens=100, completion_tokens=10)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)], usage=usage)


class FakeClient:
    """Plays back a fixed list of model replies and records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.requests.append({**kwargs, "messages": list(kwargs["messages"])})  # snapshot
        return self.replies.pop(0)


class SilentUI:
    def step(self, number, max_steps): pass
    def thought(self, text): pass
    def tool_call(self, name, args, result): pass


def test_agent_explores_fixes_and_finishes(tmp_path: Path):
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
    client = FakeClient([
        reply("Let me look at the code.", [tool_call("1", "read_file", path="app.py")]),
        reply(None, [tool_call("2", "edit_file", path="app.py", old_text="a - b", new_text="a + b")]),
        reply(None, [tool_call("3", "finish", summary="add() subtracted instead of adding.", fixed=True)]),
    ])
    workspace = Workspace(tmp_path, approve_command=lambda cmd: True)

    result = Agent(client, "test-model", workspace, SilentUI()).run("add(2, 2) returns 0")

    assert result.fixed
    assert result.summary == "add() subtracted instead of adding."
    assert result.steps == 3
    assert result.prompt_tokens == 300
    assert "return a + b" in (tmp_path / "app.py").read_text(encoding="utf-8")
    # The tool result from step 1 was sent back to the model in step 2.
    tool_messages = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert "return a - b" in tool_messages[0]["content"]


def test_agent_nudges_when_model_stops_calling_tools(tmp_path: Path):
    client = FakeClient([
        reply("I think I know the answer."),
        reply(None, [tool_call("1", "finish", summary="Could not reproduce.", fixed=False)]),
    ])
    workspace = Workspace(tmp_path, approve_command=lambda cmd: True)

    result = Agent(client, "test-model", workspace, SilentUI()).run("bug")

    assert not result.fixed
    assert client.requests[1]["messages"][-1]["role"] == "user"


def test_agent_retries_when_rate_limited(tmp_path: Path, monkeypatch):
    import openai

    monkeypatch.setattr("ghostpatch.agent.time.sleep", lambda seconds: None)
    # Build the SDK's error without an HTTP response object (its HTTP library differs across SDK versions).
    rate_limited = openai.RateLimitError.__new__(openai.RateLimitError)
    Exception.__init__(rate_limited, "Too many requests")
    replies = [rate_limited, reply(None, [tool_call("1", "finish", summary="ok", fixed=True)])]

    class FlakyClient(FakeClient):
        def _create(self, **kwargs):
            next_reply = self.replies.pop(0)
            if isinstance(next_reply, Exception):
                raise next_reply
            return next_reply

    client = FlakyClient(replies)
    workspace = Workspace(tmp_path, approve_command=lambda cmd: True)

    assert Agent(client, "test-model", workspace, SilentUI()).run("bug").fixed


def test_agent_retries_malformed_tool_call(tmp_path: Path):
    import openai

    malformed = openai.BadRequestError.__new__(openai.BadRequestError)
    Exception.__init__(malformed, "Error code: 400 - {'code': 'tool_use_failed'}")
    replies = [malformed, reply(None, [tool_call("1", "finish", summary="ok", fixed=True)])]

    class FlakyClient(FakeClient):
        def _create(self, **kwargs):
            next_reply = self.replies.pop(0)
            if isinstance(next_reply, Exception):
                raise next_reply
            return next_reply

    workspace = Workspace(tmp_path, approve_command=lambda cmd: True)

    assert Agent(FlakyClient(replies), "test-model", workspace, SilentUI()).run("bug").fixed


def test_agent_accepts_finish_written_as_text_and_prefixed_tool_names(tmp_path: Path):
    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    client = FakeClient([
        reply(None, [tool_call("1", "repo_browser.read_file", path="app.py")]),
        reply('```json\n{"fixed": true, "summary": "Done."}\n```'),
    ])
    workspace = Workspace(tmp_path, approve_command=lambda cmd: True)

    result = Agent(client, "test-model", workspace, SilentUI()).run("bug")

    assert result.fixed and result.summary == "Done."
    tool_messages = [m for m in client.requests[1]["messages"] if m["role"] == "tool"]
    assert "x = 1" in tool_messages[0]["content"]


def test_agent_stops_at_step_limit(tmp_path: Path):
    client = FakeClient([reply(None, [tool_call(str(i), "list_files")]) for i in range(3)])
    workspace = Workspace(tmp_path, approve_command=lambda cmd: True)

    result = Agent(client, "test-model", workspace, SilentUI(), max_steps=3).run("bug")

    assert not result.fixed
    assert "limit of 3 steps" in result.summary


# ------------------------------------------------------------------ keeping requests small

def _conversation(*tool_outputs: str) -> list[dict]:
    messages = [{"role": "system", "content": "system"}, {"role": "user", "content": "issue"}]
    for i, output in enumerate(tool_outputs):
        call = {"id": str(i), "type": "function", "function": {"name": "run_command", "arguments": "{}"}}
        messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
        messages.append({"role": "tool", "tool_call_id": str(i), "content": output})
    return messages


def test_small_conversations_are_sent_unchanged():
    from ghostpatch.agent import compact

    messages = _conversation("short output", "x" * 2000)
    assert compact(messages) == 0
    assert messages[-1]["content"] == "x" * 2000


def test_old_long_results_are_shortened_but_keep_their_first_and_last_lines():
    from ghostpatch.agent import KEEP_RECENT, compact

    log = "\n".join(f"line {n}" for n in range(1, 2000)) + "\nFAILED tests/test_cart.py::test_total"
    messages = _conversation(log, "tiny", *[f"recent {n}\n" * 300 for n in range(KEEP_RECENT)])
    saved = compact(messages)
    tools = [m["content"] for m in messages if m["role"] == "tool"]
    assert saved > 15_000
    assert tools[0].startswith("line 1\n") and tools[0].endswith("FAILED tests/test_cart.py::test_total")
    assert "lines hidden to keep the conversation short" in tools[0]
    assert tools[1] == "tiny"                                                   # short results stay whole
    assert tools[2:] == [f"recent {n}\n" * 300 for n in range(KEEP_RECENT)]     # so do the newest ones
    assert compact(messages) == 0  # nothing left to shorten until the conversation grows again


def test_the_agent_trims_its_history_as_it_works(tmp_path: Path):
    (tmp_path / "big.py").write_text("".join(f"value_{n} = {n}\n" for n in range(1500)), encoding="utf-8")
    reads = [reply(None, [tool_call(str(n), "read_file", path="big.py")]) for n in range(8)]
    client = FakeClient([*reads, reply(None, [tool_call("9", "finish", summary="done", fixed=False)])])
    thoughts = []
    ui = SilentUI()
    ui.thought = thoughts.append
    Agent(client, "m", Workspace(tmp_path, approve_command=lambda c: True), ui).run("bug")

    sizes = [sum(len(m.get("content") or "") for m in r["messages"]) for r in client.requests]
    assert max(sizes) < 45_000  # untrimmed, the last request carries all 8 reads: ~98k characters
    assert any("Shortened old tool output" in t for t in thoughts)
