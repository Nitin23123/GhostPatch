"""Tests for the web dashboard, using a real local HTTP server and a scripted fake model."""

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace

import pytest

from ghostpatch.providers import PROVIDERS
from ghostpatch.server import Dashboard, EventBus, WebUI, create_server
from ghostpatch.tools import Workspace
from test_agent import FakeClient, reply, tool_call


def fake_config(replies):
    return SimpleNamespace(provider=PROVIDERS["ollama"], model="fake-model", client=lambda: FakeClient(replies))


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    (tmp_path / "app.py").write_text("def add(a, b):\n    return a - b\n\ndef total(xs):\n    return add(xs[0], xs[1])\n",
                                     encoding="utf-8")
    (tmp_path / "ISSUE.md").write_text("add() subtracts", encoding="utf-8")
    return tmp_path


@pytest.fixture
def running_server(repo: Path):
    def start(replies=(), auto_approve=True):
        dashboard = Dashboard(repo, fake_config(list(replies)), max_steps=10, auto_approve=auto_approve, use_graph=True)
        server = create_server(dashboard, port=0)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        started.append(server)
        return dashboard, f"http://127.0.0.1:{server.server_address[1]}"

    started = []
    yield start
    for server in started:
        server.shutdown()
        server.server_close()


def get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=5) as res:
        return res.status, res.read()


def post(url, body, headers=None):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=5) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def wait_for(bus: EventBus, type_: str, timeout=5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for event in bus.events:
            if event["type"] == type_:
                return event
        time.sleep(0.02)
    raise AssertionError(f"no '{type_}' event; got {[e['type'] for e in bus.events]}")


def test_event_bus_replays_history():
    bus = EventBus()
    bus.publish("step", number=1)
    bus.publish("thought", text="hi")
    assert [e["type"] for e in bus.wait_since(0)] == ["step", "thought"]
    assert bus.wait_since(1)[0]["id"] == 1
    assert bus.wait_since(2, timeout=0.01) == []


def test_web_ui_waits_for_approval_from_the_browser():
    bus = EventBus()
    ui = WebUI(bus)
    answers = []
    worker = threading.Thread(target=lambda: answers.append(ui.approve_command("pytest")))
    worker.start()
    request = wait_for(bus, "approval")
    assert request["command"] == "pytest"
    assert ui.answer(request["request_id"], allow=True)
    worker.join(timeout=5)
    assert answers == [True]
    assert wait_for(bus, "approval_result")["allow"] is True
    assert not ui.answer("unknown-id", allow=True)


def test_workspace_diffs(repo: Path):
    ws = Workspace(repo, approve_command=lambda c: True)
    ws.call("edit_file", {"path": "app.py", "old_text": "a - b", "new_text": "a + b"})
    ws.call("create_file", {"path": "test_app.py", "content": "x = 1\n"})
    diffs = {d["path"]: d for d in ws.diffs()}
    assert "-    return a - b\n+    return a + b" in diffs["app.py"]["diff"]
    assert diffs["test_app.py"]["new"] and "+x = 1" in diffs["test_app.py"]["diff"]


def test_pages_and_api(running_server):
    _, url = running_server()
    status, body = get(url + "/")
    assert status == 200 and b"GhostPatch" in body

    info = json.loads(get(url + "/api/info")[1])
    assert info["model"] == "fake-model" and info["issue_template"] == "add() subtracts"

    graph = json.loads(get(url + "/api/graph")[1])
    names = {n["name"] for n in graph["nodes"]}
    assert names == {"add", "total"}
    ids = {n["name"]: n["id"] for n in graph["nodes"]}
    assert {"source": ids["total"], "target": ids["add"]} in graph["edges"]


def test_security_checks(running_server):
    _, url = running_server()
    assert post(url + "/api/run", {"issue": "x"})[0] == 403  # missing CSRF header
    with pytest.raises(urllib.error.HTTPError) as err:
        get(url + "/api/info", headers={"Host": "evil.example"})
    assert err.value.code == 403
    assert post(url + "/api/run", {"issue": " "}, {"X-GhostPatch": "1"})[0] == 400


def test_full_run_through_the_dashboard(running_server, repo: Path):
    dashboard, url = running_server([
        reply("Looking at add().", [tool_call("1", "read_file", path="app.py")]),
        reply(None, [tool_call("2", "edit_file", path="app.py", old_text="a - b", new_text="a + b")]),
        reply(None, [tool_call("3", "finish", summary="add() subtracted.", fixed=True)]),
    ])
    status, _ = post(url + "/api/run", {"issue": "add() subtracts"}, {"X-GhostPatch": "1"})
    assert status == 202

    done = wait_for(dashboard.bus, "done")
    assert done["fixed"] and done["summary"] == "add() subtracted."
    assert "+    return a + b" in done["diffs"][0]["diff"]
    edit = next(e for e in dashboard.bus.events if e["type"] == "tool" and e["name"] == "edit_file")
    assert "you changed app.add" in edit["result"] and "app.total" in edit["result"]
    assert "return a + b" in (repo / "app.py").read_text(encoding="utf-8")


def test_only_one_run_at_a_time(running_server):
    dashboard, url = running_server([reply(None, [tool_call("1", "run_command", command="echo hi")])],
                                    auto_approve=False)
    assert post(url + "/api/run", {"issue": "bug"}, {"X-GhostPatch": "1"})[0] == 202
    request = wait_for(dashboard.bus, "approval")  # the run is now waiting for the user
    assert post(url + "/api/run", {"issue": "another"}, {"X-GhostPatch": "1"})[0] == 409
    assert post(url + "/api/approve", {"request_id": request["request_id"], "allow": False},
                {"X-GhostPatch": "1"})[0] == 200
