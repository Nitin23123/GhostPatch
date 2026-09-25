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
