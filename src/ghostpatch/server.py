"""The web dashboard: `ghostpatch serve`.

A small local web server (Python standard library only) that runs the agent in a
background thread and streams every step to the browser with Server-Sent Events.

Security: it listens on 127.0.0.1 only, rejects requests whose Host header isn't
localhost (DNS-rebinding protection), and requires a custom header on every POST,
which browsers won't let other websites send without a CORS preflight we never answer.
"""

from __future__ import annotations

import json
import threading
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from ghostpatch import __version__
from ghostpatch import history
from ghostpatch.gitutil import is_git_repo
from ghostpatch.policy import DEFAULT_APPROVAL, auto_approves
from ghostpatch.providers import ModelConfig, describe_api_error

WEB_DIR = Path(__file__).parent / "web"
MAX_EVENT_TEXT = 6000
APPROVAL_TIMEOUT_SECONDS = 600
CSRF_HEADER = "X-GhostPatch"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


class EventBus:
    """An append-only list of events that any number of browser tabs can follow."""

    def __init__(self) -> None:
        self.events: list[dict] = []
        self._cond = threading.Condition()

    def publish(self, type_: str, **data: Any) -> None:
        with self._cond:
            self.events.append({"id": len(self.events), "type": type_, **data})
            self._cond.notify_all()

    def wait_since(self, index: int, timeout: float = 15.0) -> list[dict]:
        with self._cond:
            if index >= len(self.events):
                self._cond.wait(timeout)
            return self.events[index:]


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_EVENT_TEXT:
        return value[:MAX_EVENT_TEXT] + f"\n… [{len(value) - MAX_EVENT_TEXT} more characters]"
    return value


class WebUI:
    """The agent's UI, implemented as events for the browser instead of terminal output."""

    def __init__(self, bus: EventBus, approval: str = DEFAULT_APPROVAL):
        self.bus = bus
        self.approval = approval
        self._pending: dict[str, dict] = {}
        self._lock = threading.Lock()

    def step(self, number: int, max_steps: int) -> None:
        self.bus.publish("step", number=number, max=max_steps)

    def thought(self, text: str) -> None:
        self.bus.publish("thought", text=_clip(text))

    def tool_call(self, name: str, args: dict, result: str) -> None:
        self.bus.publish("tool", name=name, args={k: _clip(v) for k, v in args.items()}, result=_clip(result))

    def approve_command(self, command: str) -> bool:
        if auto_approves(self.approval, command):
            self.bus.publish("approval_auto", command=command, mode=self.approval)
            return True
        request_id = uuid.uuid4().hex
        waiter = {"event": threading.Event(), "allow": False}
        with self._lock:
            self._pending[request_id] = waiter
        self.bus.publish("approval", request_id=request_id, command=command)
        answered = waiter["event"].wait(APPROVAL_TIMEOUT_SECONDS)
        with self._lock:
            self._pending.pop(request_id, None)
        allow = answered and waiter["allow"]
        self.bus.publish("approval_result", request_id=request_id, allow=allow)
        return allow

    def answer(self, request_id: str, allow: bool) -> bool:
        with self._lock:
            waiter = self._pending.get(request_id)
        if waiter is None:
            return False
        waiter["allow"] = allow
        waiter["event"].set()
        return True


class Dashboard:
    """Shared state behind the HTTP handler: settings, the event stream and the current run."""

    def __init__(self, repo: Path, config: ModelConfig, max_steps: int, approval: str, use_graph: bool):
        self.repo = repo
        self.config = config
        self.max_steps = max_steps
        self.use_graph = use_graph
        self.bus = EventBus()
        self.ui = WebUI(self.bus, approval=approval)
        self._run_lock = threading.Lock()
        self.running = False

    def info(self) -> dict:
        issue_file = self.repo / "ISSUE.md"
        return {
            "issue_template": issue_file.read_text(encoding="utf-8")[:5000] if issue_file.is_file() else "",
            "version": __version__,
            "repo": str(self.repo),
            "repo_name": self.repo.name,
            "provider": self.config.provider.name,
            "model": self.config.model,
            "free": self.config.provider.free,
            "graph": self.use_graph,
            "approval": self.ui.approval,
            "git": is_git_repo(self.repo),
            "running": self.running,
        }

    def graph_data(self) -> dict:
        if not self.use_graph:
            return {"nodes": [], "edges": [], "truncated": False, "stats": None}
        from ghostpatch.graph import CodeGraph

        graph = CodeGraph(self.repo)  # SQLite connections can't be shared across threads
        try:
            graph.refresh()
            return {**graph.export(), "stats": graph.stats()}
        finally:
            graph.close()

    def history(self) -> list[dict]:
        return [history.summarize(run) for run in history.list_runs(self.repo)]

    def undo(self, run_id: str | None, force: bool) -> dict:
        if self.running:
            raise history.UndoError("Wait for the current run to finish first.")
        run = history.undo_run(self.repo, run_id, force=force)
        self.bus.publish("undone", run_id=run["id"], files=[f["path"] for f in run["files"]])
        return history.summarize(run)

    def start(self, issue: str) -> bool:
        with self._run_lock:
            if self.running:
                return False
            self.running = True
        threading.Thread(target=self._run, args=(issue,), daemon=True).start()
        return True

    def _run(self, issue: str) -> None:
        import openai

        from ghostpatch.agent import Agent
        from ghostpatch.tools import Workspace

        graph = None
        try:
            if self.use_graph:
                from ghostpatch.graph import CodeGraph

                graph = CodeGraph(self.repo)
            workspace = Workspace(self.repo, approve_command=self.ui.approve_command, graph=graph)
            self.bus.publish("run_started", issue=issue, model=self.config.model, provider=self.config.provider.name)
            agent = Agent(self.config.client(), self.config.model, workspace, self.ui, max_steps=self.max_steps)
            try:
                result = agent.run(issue)
            except openai.APIError as e:
                message = describe_api_error(e, self.config.provider)
                run_id = self._save(issue, workspace, agent.result, error=message)
                self.bus.publish("error", message=message, diffs=workspace.diffs(), run_id=run_id)
                return
            run_id = self._save(issue, workspace, result)
            self.bus.publish(
                "done", fixed=result.fixed, summary=result.summary, steps=result.steps,
                prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                diffs=workspace.diffs(), run_id=run_id,
            )
        except Exception as e:  # never let the background thread die silently
            self.bus.publish("error", message=f"{type(e).__name__}: {e}")
        finally:
            if graph is not None:
                graph.close()
            self.running = False

    def _save(self, issue: str, workspace: Any, result: Any, error: str | None = None) -> str | None:
        if result is None and not workspace.changed_files:
            return None
        return history.save_run(
            self.repo, issue=issue, provider=self.config.provider.name, model=self.config.model,
            fixed=bool(result and result.fixed), summary=(result.summary if result else "") or (error or ""),
            steps=result.steps if result else 0,
            prompt_tokens=result.prompt_tokens if result else 0,
            completion_tokens=result.completion_tokens if result else 0,
            changed_files=workspace.changed_files, originals=workspace.originals, error=error,
        )


def make_handler(dashboard: Dashboard) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = f"GhostPatch/{__version__}"

        def log_message(self, format: str, *args: Any) -> None:  # keep the terminal quiet
            pass

        # ------------------------------------------------------------ helpers

        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
            return host in ALLOWED_HOSTS

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, data: Any) -> None:
            self._send(status, json.dumps(data).encode("utf-8"), "application/json")

        def _read_json(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}")
            except (ValueError, json.JSONDecodeError):
                return None
            return data if isinstance(data, dict) else None

        # ------------------------------------------------------------- routes

        def do_GET(self) -> None:
            if not self._host_ok():
                return self._json(403, {"error": "forbidden host"})
            path = self.path.split("?", 1)[0]
            if path == "/":
                return self._send(200, (WEB_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
            if path == "/api/info":
                return self._json(200, dashboard.info())
            if path == "/api/graph":
                return self._json(200, dashboard.graph_data())
            if path == "/api/history":
                return self._json(200, dashboard.history())
            if path == "/api/events":
                return self._stream_events()
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:
            body = self._read_json()  # always consume the body, or Windows may reset the connection
            if not self._host_ok() or self.headers.get(CSRF_HEADER) != "1":
                return self._json(403, {"error": "forbidden"})
            if body is None:
                return self._json(400, {"error": "expected a JSON object"})
            if self.path == "/api/run":
                issue = body.get("issue")
                issue = issue.strip() if isinstance(issue, str) else ""
                if not issue:
                    return self._json(400, {"error": "Describe the bug first."})
                if not dashboard.start(issue):
                    return self._json(409, {"error": "The ghost is already working on something."})
                return self._json(202, {"ok": True})
            if self.path == "/api/undo":
                try:
                    run = dashboard.undo(body.get("run_id") or None, force=bool(body.get("force")))
                except history.UndoError as e:
                    return self._json(409, {"error": str(e)})
                return self._json(200, {"ok": True, "run": run})
            if self.path == "/api/approve":
                ok = dashboard.ui.answer(str(body.get("request_id")), bool(body.get("allow")))
                return self._json(200 if ok else 404, {"ok": ok})
            self._json(404, {"error": "not found"})

        def _stream_events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            last = self.headers.get("Last-Event-ID")
            index = int(last) + 1 if last and last.isdigit() else 0
            try:
                while True:
                    events = dashboard.bus.wait_since(index)
                    if not events:
                        self.wfile.write(b": keep-alive\n\n")  # also detects closed tabs
                    for event in events:
                        payload = json.dumps(event)
                        self.wfile.write(f"id: {event['id']}\ndata: {payload}\n\n".encode("utf-8"))
                        index = event["id"] + 1
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass  # the browser tab was closed

    return Handler


def create_server(dashboard: Dashboard, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(dashboard))
    server.daemon_threads = True
    return server


def serve(
    repo: Path, config: ModelConfig, port: int, max_steps: int, approval: str,
    use_graph: bool, open_browser: bool,
) -> int:
    dashboard = Dashboard(repo, config, max_steps, approval, use_graph)
    try:
        server = create_server(dashboard, port)
    except OSError as e:
        print(f"Could not start the dashboard on port {port}: {e}\nTry another port with --port.")
        return 2
    url = f"http://localhost:{server.server_address[1]}"
    print(f"👻 GhostPatch dashboard running at {url}")
    print(f"   repo: {repo}   model: {config.model} ({config.provider.name})   approvals: {approval}")
    print("   Press Ctrl+C to stop.")
    if open_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0
