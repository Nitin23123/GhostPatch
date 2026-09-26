"""Run replay and sharing.

`RecordingUI` wraps whatever UI the agent uses and keeps a timestamped copy of every step,
thought and tool call. The recording is saved with the run, so a run can be replayed later
in the dashboard, or exported by `ghostpatch share` as a single self-contained HTML file that
anyone can open in a browser, with no server and no GhostPatch install needed.
"""

from __future__ import annotations

import html
import json
import time
from typing import Any

MAX_TEXT = 4000


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + f"\n… [{len(value) - MAX_TEXT} more characters]"
    return value


class RecordingUI:
    def __init__(self, inner: Any):
        self.inner = inner
        self.events: list[dict] = []
        self._start = time.time()

    def _record(self, type_: str, **data: Any) -> None:
        self.events.append({"t": round(time.time() - self._start, 2), "type": type_, **data})

    def step(self, number: int, max_steps: int) -> None:
        self._record("step", number=number, max=max_steps)
        self.inner.step(number, max_steps)

    def thought(self, text: str) -> None:
        self._record("thought", text=_clip(text))
        self.inner.thought(text)

    def tool_call(self, name: str, args: dict, result: str) -> None:
        self._record("tool", name=name, args={k: _clip(v) for k, v in args.items()}, result=_clip(result))
        self.inner.tool_call(name, args, result)

    def __getattr__(self, name: str) -> Any:  # approve_command, banner, ... pass straight through
        return getattr(self.inner, name)


# ------------------------------------------------------------------------ sharing

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GhostPatch run {run_id}</title>
<style>
:root {{ --bg:#f6f5fb; --panel:#fff; --border:#e2dff0; --text:#1d1b2e; --muted:#6b6883; --accent:#6d5ce8;
        --ok:#13875a; --err:#c9314a; --code:#f4f3f9; color-scheme: light; }}
@media (prefers-color-scheme: dark) {{ :root {{ --bg:#0f0e17; --panel:#171624; --border:#2b2940; --text:#ecebf5;
        --muted:#9794b0; --accent:#9d8ff9; --ok:#3ccf8e; --err:#ff6b81; --code:#121120; color-scheme: dark; }} }}
* {{ box-sizing: border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text); font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif; }}
main {{ max-width: 860px; margin: 0 auto; padding: 28px 16px 60px; }}
h1 {{ font-size: 22px; margin: 0 0 4px; }} .sub {{ color: var(--muted); font-size: 13.5px; }}
.card {{ background:var(--panel); border:1px solid var(--border); border-radius:14px; padding:16px; margin-top:16px; }}
.controls {{ display:flex; gap:10px; align-items:center; flex-wrap:wrap; }}
button {{ font:inherit; border:0; border-radius:9px; padding:7px 14px; background:var(--accent); color:#fff; cursor:pointer; }}
input[type=range] {{ flex:1; min-width:160px; }}
.ev {{ border-top:1px solid var(--border); padding:10px 0; }} .ev:first-child {{ border-top:0; }}
.ev .head {{ font-weight:600; }} .ev .muted {{ color:var(--muted); font-weight:400; }}
pre {{ background:var(--code); border-radius:8px; padding:8px 10px; overflow:auto; max-height:280px; white-space:pre-wrap;
      word-break:break-word; font:12.5px/1.45 ui-monospace,Consolas,monospace; margin:6px 0 0; }}
.result {{ border-left:4px solid var(--ok); }} .result.bad {{ border-left-color: var(--err); }}
.hidden {{ display:none; }}
</style></head><body><main>
<h1>👻 {title}</h1>
<div class="sub">{meta}</div>
<div class="card result{bad}"><b>{verdict}</b><br>{summary}</div>
<div class="card"><div class="controls"><button id="play">▶ Play</button>
<input id="scrub" type="range" min="0" max="{last}" value="{last}" aria-label="Replay position"><span id="pos" class="sub"></span></div></div>
<div class="card" id="events"></div>
<p class="sub">Recorded by <a href="https://github.com/Nitin23123/GhostPatch">GhostPatch</a>.</p>
</main>
<script>
const EVENTS = {events_json};
const list = document.getElementById("events"), scrub = document.getElementById("scrub"), pos = document.getElementById("pos");
const ICON = {{ read_file:"📖", edit_file:"✏️", replace_lines:"✏️", create_file:"🆕", run_command:"⚡", search_code:"🔎",
  list_files:"📂", impact_of_change:"💥", related_tests:"🧪", remember:"🧠", finish:"🏁" }};
function node(ev) {{
  const d = document.createElement("div"); d.className = "ev";
  const head = document.createElement("div"); head.className = "head";
  const body = document.createElement("pre");
  if (ev.type === "step") {{ head.textContent = "Step " + ev.number; }}
  else if (ev.type === "thought") {{ head.textContent = "💭 " + ev.text; head.className = "sub"; }}
  else {{
    head.textContent = (ICON[ev.name] || "🔧") + " " + ev.name + " ";
    const m = document.createElement("span"); m.className = "muted";
    m.textContent = Object.entries(ev.args || {{}}).filter(([k]) => !["content","old_text","new_text"].includes(k))
      .map(([k, v]) => k + "=" + String(v).slice(0, 80)).join(", ");
    head.append(m);
    let text = "";
    if (ev.args && ev.args.old_text != null) text += ev.args.old_text.split("\\n").map(l => "- " + l).join("\\n") + "\\n";
    if (ev.args && ev.args.new_text != null) text += ev.args.new_text.split("\\n").map(l => "+ " + l).join("\\n") + "\\n\\n";
    body.textContent = text + (ev.result || "");
    d.append(head, body); return d;
  }}
  d.append(head); return d;
}}
const nodes = EVENTS.map(node); nodes.forEach(n => list.append(n));
function show(i) {{ nodes.forEach((n, k) => n.classList.toggle("hidden", k > i));
  pos.textContent = EVENTS.length ? `${{i + 1}} / ${{EVENTS.length}} · ${{EVENTS[i].t}}s` : "no steps recorded"; }}
scrub.oninput = () => show(+scrub.value);
let timer = null;
document.getElementById("play").onclick = () => {{
  if (timer) {{ clearInterval(timer); timer = null; return; }}
  let i = 0; show(0); scrub.value = 0;
  timer = setInterval(() => {{ i++; if (i >= EVENTS.length) {{ clearInterval(timer); timer = null; return; }}
    scrub.value = i; show(i); nodes[i].scrollIntoView({{ block: "nearest", behavior: "smooth" }}); }}, 700);
}};
show(EVENTS.length - 1);
</script></body></html>
"""


def export_html(run: dict) -> str:
    """A self-contained HTML page that replays a recorded run."""
    events = run.get("events") or []
    issue_ref = run.get("issue_ref")
    title = f"#{issue_ref['number']} {issue_ref['title']}" if issue_ref else (
        next((l.strip("# ").strip() for l in (run.get("issue") or "").splitlines() if l.strip()), "GhostPatch run"))
    confidence = run.get("confidence") or {}
    meta = " · ".join(filter(None, [
        run.get("created"), f"{run.get('steps', 0)} steps", run.get("model"),
        f"confidence {confidence['score']}/100" if confidence.get("score") is not None and confidence.get("level") != "none" else "",
    ]))
    fixed = run.get("fixed")
    # json.dumps output is safe inside <script> once "</" can't close the tag.
    events_json = json.dumps(events).replace("</", "<\\/")
    return _PAGE.format(
        run_id=html.escape(run.get("id", "")), title=html.escape(title), meta=html.escape(meta),
        bad="" if fixed else " bad", verdict="Fixed" if fixed else "Not fixed",
        summary=html.escape(run.get("summary") or run.get("error") or ""),
        last=max(len(events) - 1, 0), events_json=events_json,
    )
