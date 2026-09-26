/* GhostPatch dashboard: views, live event stream, runs & replay, insights. */
"use strict";

const { icon, hydrateIcons } = window.GhostIcons;
const { GraphView, graphFromApi, marksFromEvents, emptyMarks } = window.GhostGraph;
const $ = (id) => document.getElementById(id);

function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) if (child != null && child !== false) node.append(child);
  return node;
}

async function getJSON(url) {
  const res = await fetch(url);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || `Request failed (${res.status})`);
  return data;
}

async function post(url, body) {
  const res = await fetch(url, {
    method: "POST", headers: { "Content-Type": "application/json", "X-GhostPatch": "1" }, body: JSON.stringify(body),
  });
  return { ok: res.ok, status: res.status, data: await res.json().catch(() => ({})) };
}

let toastTimer;
function toast(message, error = false) {
  document.querySelector(".toast")?.remove();
  const t = el("div", { class: "toast" + (error ? " err" : ""), text: message, role: "status" });
  document.body.append(t);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => t.remove(), 5000);
}

const fmt = {
  tokens: (n) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n || 0)),
  duration: (s) => (s < 60 ? `${Math.max(0, Math.round(s))}s` : `${Math.floor(s / 60)}m ${Math.round(s % 60)}s`),
  ago(created) {
    const t = Date.parse((created || "").replace(" ", "T"));
    if (!t) return created || "";
    const s = (Date.now() - t) / 1000;
    if (s < 60) return "just now";
    if (s < 3600) return `${Math.floor(s / 60)}m ago`;
    if (s < 86400) return `${Math.floor(s / 3600)}h ago`;
    return `${Math.floor(s / 86400)}d ago`;
  },
  firstLine: (text) => ((text || "").split("\n").find((l) => l.trim()) || "").replace(/^#+\s*/, "").trim(),
};

const state = {
  info: {},
  graph: { nodes: [], edges: [] },
  events: [],          // the current run's graph-relevant events (tool, trace)
  running: false,
  run: null,           // {id, fixed, ...} of the latest finished run
  runStart: null,
  lastTs: null,
  runs: [],
  filter: "all",
  selected: null,
};

// ================================================================== views & top bar

function showView(name) {
  if (!["dashboard", "runs", "insights"].includes(name)) name = "dashboard";
  for (const v of document.querySelectorAll(".view")) v.classList.toggle("active", v.id === `view-${name}`);
  for (const a of document.querySelectorAll(".nav a")) a.classList.toggle("active", a.dataset.view === name);
  if (name === "runs") loadRuns();
  if (name === "insights") openInsight(insights.current);
}
window.addEventListener("hashchange", () => showView(location.hash.slice(1)));

function setStatus(kind, text) {
  $("status").className = `pill status ${kind}`;
  $("status-text").textContent = text;
  const live = $("live");
  live.className = "live" + (kind === "working" ? " on" : "");
  live.textContent = kind === "working" ? "Agent working" : text;
}

function setRunning(on) {
  state.running = on;
  $("run").disabled = on;
  $("run").innerHTML = on ? `${icon("spinner", "sm")} Working…` : `${icon("play", "sm")} Fix it`;
  if (!on) $("progress").style.width = "0";
  $("ready-hint").textContent = on ? "Ghost at work" : "Ready";
}

// ======================================================================= timeline

const TOOL_ICON = {
  read_file: "file", list_files: "folder", search_code: "search", edit_file: "edit", replace_lines: "edit",
  create_file: "file-plus", run_command: "terminal", find_symbol: "graph", find_callers: "graph", find_callees: "graph",
  related_tests: "flask", impact_of_change: "impact", remember: "bookmark", finish: "flag",
};

function stepDetail(ev) {
  const a = ev.args || {};
  switch (ev.name) {
    case "read_file": return a.path + (a.start_line ? `:${a.start_line}${a.end_line ? "-" + a.end_line : ""}` : "");
    case "edit_file": case "replace_lines": case "create_file": return a.path || "";
    case "run_command": return a.command || "";
    case "search_code": return `/${a.pattern || ""}/` + (a.file_glob ? ` in ${a.file_glob}` : "");
    case "list_files": return a.path || ".";
    case "remember": return a.note || "";
    case "finish": return a.summary || "";
    default: return a.name || Object.values(a).join(" ");
  }
}

function toolFailed(ev) {
  if ((ev.result || "").startsWith("Error")) return true;
  const code = (ev.result || "").match(/^exit code: (\d+)/);
  return !!(code && code[1] !== "0");
}

/** One timeline entry for an event. Returns null for events that don't show. */
function renderEvent(ev, { live = false, prevTime = null } = {}) {
  const time = ev.ts ?? ev.t;
  const took = prevTime != null && time != null ? `${Math.max(0, time - prevTime).toFixed(1)}s` : "";
  if (ev.type === "step") return el("div", { class: "step-divider", text: `STEP ${ev.number}` });
  if (ev.type === "thought") return el("div", { class: "thought", text: String(ev.text || "").replace(/^_(.*)_$/s, "$1") });
  if (ev.type === "approval_auto") return el("div", { class: "auto" }, "auto-approved (safe command): ", el("code", { text: ev.command }));
  if (ev.type === "trace") {
    const body = el("div", { class: "step-body" },
      el("div", { class: "step-top" }, el("span", { class: "step-name", text: "crash trace" }), el("span", { class: "step-time", text: ev.language || "" })),
      el("div", { class: "step-detail", text: ev.error || "stack trace found" }),
      el("div", { class: "step-alert", style: "color:var(--amber);border-color:rgba(255,196,51,.3);background:rgba(255,196,51,.07)",
                  text: (ev.path || []).join(" → ") || "no frames in this repository" }));
    return el("div", { class: "step" }, el("span", { html: icon("compass") }).firstChild, body);
  }
  if (ev.type === "approval" && live) return renderApproval(ev);
  if (ev.type !== "tool") return null;

  const failed = toolFailed(ev);
  const body = el("div", { class: "step-body" });
  const name = el("span", { class: "step-name", text: ev.name, title: "Show output" });
  body.append(el("div", { class: "step-top" }, name, el("span", { class: "step-time", text: took })));
  body.append(el("div", { class: "step-detail", text: stepDetail(ev), title: stepDetail(ev) }));
  const a = ev.args || {};
  if ((ev.name === "edit_file" || ev.name === "replace_lines") && !failed) {
    const diff = el("div", { class: "minidiff" });
    if (a.old_text != null) for (const line of String(a.old_text).split("\n")) diff.append(el("div", { class: "del", text: "- " + line }));
    else diff.append(el("div", { class: "note", text: `lines ${a.start_line}–${a.end_line} replaced with:` }));
    for (const line of String(a.new_text || "").split("\n")) diff.append(el("div", { class: "add", text: "+ " + line }));
    body.append(diff);
  }
  const affected = (ev.result.match(/ in [\w.]+\s*$/gm) || []).length;
  if (affected && ["edit_file", "replace_lines", "impact_of_change"].includes(ev.name)) {
    body.append(el("div", { class: "step-alert", text: `${affected} dependent${affected === 1 ? "" : "s"} detected (blast radius)` }));
  }
  const out = el("div", { class: "step-output", text: ev.result, hidden: true });
  body.append(out);
  name.addEventListener("click", () => (out.hidden = !out.hidden));
  const iconName = failed ? "x" : TOOL_ICON[ev.name] === "flag" ? "flag" : "check";
  return el("div", { class: "step" + (failed ? " err" : "") }, el("span", { html: icon(iconName) }).firstChild, body);
}

function renderApproval(ev) {
  const buttons = el("div", { class: "row" });
  const answer = async (allow) => {
    buttons.querySelectorAll("button").forEach((b) => (b.disabled = true));
    await post("/api/approve", { request_id: ev.request_id, allow });
  };
  buttons.append(
    el("button", { class: "btn amber sm", text: "Allow", onclick: () => answer(true) }),
    el("button", { class: "btn sm", text: "Deny", onclick: () => answer(false) }),
  );
  return el("div", { class: "approval", id: `approval-${ev.request_id}` },
    el("div", { class: "row between" }, el("span", { class: "caps title", text: "Approval required · run_command" }),
      el("span", { class: "hint", text: "the ghost is waiting" })),
    el("pre", { text: ev.command }), buttons);
}

function addToTimeline(node) {
  if (!node) return;
  const tl = $("timeline");
  tl.querySelector(".tl-empty")?.remove();
  const scroller = document.querySelector("#view-dashboard .aside");
  const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
  tl.append(node);
  if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
}

// ========================================================================= results

function renderResult(ev) {
  const box = $("result");
  box.replaceChildren();
  box.hidden = false;
  const ok = ev.type === "done" && ev.fixed;
  const label = ev.type === "error" ? "Stopped" : ok ? "Bug fixed" : "Not fixed";
  const chipCls = ev.type === "error" ? "red" : ok ? "mint" : "amber";
  const seconds = state.runStart && ev.ts ? ev.ts - state.runStart : null;
  const card = el("div", { class: "section result-card" },
    el("div", { class: "row between" },
      el("span", { class: `chip ${chipCls}`, html: `${icon(ok ? "check" : ev.type === "error" ? "x" : "alert", "xs")} ${label}` }),
      ev.run_id ? el("span", { class: "hint", text: `run ${ev.run_id}` }) : null),
    el("p", { text: ev.summary || ev.message || "" }));
  if (ev.steps != null) {
    card.append(el("div", { class: "meta" },
      el("span", { html: `${icon("steps", "xs")} ${ev.steps} steps` }), "·",
      el("span", { html: `${icon("cpu", "xs")} ${fmt.tokens(ev.prompt_tokens + ev.completion_tokens)} tokens` }),
      seconds != null ? "·" : null, seconds != null ? el("span", { html: `${icon("clock", "xs")} ${fmt.duration(seconds)}` }) : null));
  }
  box.append(card);
  const conf = ev.confidence;
  if (conf && conf.level && conf.level !== "none") box.append(renderConfidence(conf));
  if (ev.poltergeist && ev.poltergeist.length) box.append(renderRounds(ev.poltergeist));
}

function bar(fraction, cls = "") {
  return el("div", { class: `bar ${cls}` }, el("i", { style: `width:${Math.round(Math.max(0, Math.min(1, fraction)) * 100)}%` }));
}

function renderConfidence(conf) {
  const levelText = { high: "High confidence", medium: "Medium confidence", low: "Low confidence" }[conf.level] || conf.level;
  const tests = conf.tests_after_edit;
  const testCls = tests === "passed" ? "" : tests === "failed" ? "red" : "amber";
  const section = el("div", { class: "section conf" },
    el("div", { class: "section-head" }, el("span", { class: "caps", text: "Verification quality" })),
    el("div", { class: `score ${conf.level}` }, el("b", { text: String(conf.score) }), el("small", { text: "/100" }),
      el("span", { class: `chip ${conf.level === "high" ? "mint" : conf.level === "medium" ? "amber" : "red"}`, text: levelText })),
    el("div", { class: "bar-row" },
      el("div", { class: "row" }, el("span", { class: "muted", text: "Tests after last edit" }), el("span", { class: "mono " + (testCls || "mint"), text: tests })),
      bar(tests === "passed" ? 1 : tests === "failed" ? 1 : 0.08, testCls)));
  if (conf.blast_radius) {
    section.append(el("div", { class: "bar-row" },
      el("div", { class: "row" }, el("span", { class: "muted", text: "Blast radius covered by tests" }),
        el("span", { class: "mono", text: `${conf.covered} of ${conf.blast_radius}` })),
      bar(conf.covered / conf.blast_radius, conf.covered === conf.blast_radius ? "" : "amber")));
  }
  if (conf.uncovered && conf.uncovered.length) {
    const list = el("div", { class: "uncovered" });
    for (const q of conf.uncovered.slice(0, 4)) {
      list.append(el("div", { class: "item" }, el("span", { class: "red", text: q }),
        el("button", { class: "btn sm", text: "Write a test", onclick: () => startRun(testIssueFor([nodeInfo(q)])) })));
    }
    section.append(list);
  }
  return section;
}

function renderRounds(rounds) {
  const list = el("div", { class: "rounds" });
  for (const r of rounds) {
    const broke = r.broke_it;
    const text = broke ? `Broke the fix. ${r.finding}${r.refixed ? " Re-fixed." : r.refixed === false ? " Not re-fixed." : ""}`
      : `Couldn't break the fix. ${r.finding}`;
    list.append(el("div", { class: `round ${broke ? "broke" : "held"}` },
      el("span", { html: icon(broke ? "alert" : "check", "sm " + (broke ? "amber" : "mint")) }).firstChild,
      el("div", {}, el("b", { text: `Round ${r.round}: ` }), text,
        r.tests && r.tests.length ? el("div", { class: "hint", text: r.tests.join(", ") }) : null)));
  }
  return el("div", { class: "section" },
    el("div", { class: "section-head" }, el("span", { class: "caps", html: `${icon("ghost", "xs")} Poltergeist · adversarial stress test` })), list);
}

// ========================================================================= changes

function parseDiff(text) {
  const rows = [];
  let oldN = 0, newN = 0, add = 0, del = 0;
  for (const line of text.split("\n")) {
    if (line.startsWith("---") || line.startsWith("+++")) continue;
    const hunk = line.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
    if (hunk) { oldN = +hunk[1]; newN = +hunk[2]; rows.push({ kind: "hunk", text: line }); continue; }
    if (line.startsWith("+")) { rows.push({ kind: "add", o: "", n: newN++, text: line }); add++; }
    else if (line.startsWith("-")) { rows.push({ kind: "del", o: oldN++, n: "", text: line }); del++; }
    else { rows.push({ kind: "ctx", o: oldN++, n: newN++, text: line }); }
  }
  return { rows, add, del };
}

function renderChanges(diffs, run) {
  const panel = $("changes"), body = $("changes-body");
  body.replaceChildren();
  if (!diffs || !diffs.length) { panel.hidden = true; return; }
  panel.hidden = false;
  panel.classList.remove("collapsed");
  let adds = 0, dels = 0;
  for (const d of diffs) {
    const parsed = parseDiff(d.diff);
    adds += parsed.add;
    dels += parsed.del;
    const file = el("div", { class: "difffile" },
      el("div", { class: "difffile-head" }, el("span", { text: d.path }), d.new ? el("span", { class: "chip mint", text: "new" }) : null,
        el("span", { class: "stat", text: `+${parsed.add} −${parsed.del}` })));
    for (const r of parsed.rows) {
      file.append(el("div", { class: `dl ${r.kind}` }, el("span", { class: "n", text: r.o ?? "" }), el("span", { class: "n", text: r.n ?? "" }),
        el("span", { text: r.kind === "hunk" ? r.text : r.text.slice(1) || " " })));
    }
    body.append(file);
  }
  $("changes-stat").textContent = `${diffs.length} file${diffs.length === 1 ? "" : "s"} changed (+${adds}, −${dels})`;
  const runId = run && run.id;
  $("share-btn").hidden = !runId;
  $("undo-btn").hidden = !runId || run.undone;
  $("pr-btn").hidden = !(runId && run.fixed && state.info.github) || run.pr_url;
  if (run && run.undone) body.prepend(el("div", { class: "note-bar", text: "These changes were undone: every file is back the way it was before the run." }));
  if (run && run.pr_url) body.prepend(el("div", { class: "note-bar" }, "Pull request opened: ", el("a", { href: run.pr_url, target: "_blank", rel: "noopener", text: run.pr_url })));
}

async function undoRun(runId, force = false) {
  const { ok, data } = await post("/api/undo", { run_id: runId, force });
  if (ok) { toast("Undone: every file is back the way it was."); return true; }
  if (/changed after the run/.test(data.error || "") && !force && confirm(`${data.error}\n\nUndo anyway and lose those edits?`)) return undoRun(runId, true);
  toast(data.error || "Could not undo this run.", true);
  return false;
}

// =========================================================================== graph

const mainGraph = new GraphView($("canvas-main"), {
  actions: (node) => (node.test ? [] : [{ label: "Write a test", run: () => startRun(testIssueFor([node])) }]),
});

function nodeInfo(qualname) {
  return state.graph.nodes.find((n) => n.qualname === qualname) || { qualname, path: "", line: "", signature: "" };
}

async function loadGraph(keepView = false) {
  try {
    const data = graphFromApi(await getJSON("/api/graph"));
    state.graph = data;
    mainGraph.setEmptyText(state.info.graph === false ? "The code graph is turned off (--no-graph)." : "No Python, JavaScript or TypeScript code found.");
    mainGraph.setData(data, { keepView });
    refreshMarks();
    const s = data.stats;
    $("graph-stats").textContent = s ? `graph: ${s.files} files · ${s.symbols} symbols · ${s.calls} calls` : "graph off";
    $("graph-state").textContent = s ? "active" : "off";
  } catch (e) {
    toast(`Could not load the code graph: ${e.message}`, true);
  }
}

function refreshMarks() {
  const marks = marksFromEvents(state.events, state.graph.nodes);
  if (!state.running) marks.current = null;
  mainGraph.setMarks(marks);
  const edited = [...marks.edited][0];
  $("crumbs").innerHTML = "";
  $("crumbs").append(el("span", { text: state.info.repo_name || "" }));
  if (edited) {
    const n = nodeInfo(edited);
    $("crumbs").append(el("span", { text: "›" }), el("span", { text: n.path }), el("span", { text: "›" }), el("b", { text: n.name || edited }));
  }
}

$("graph-search").addEventListener("input", (e) => mainGraph.search(e.target.value));
for (const b of document.querySelectorAll("[data-zoom]")) {
  b.addEventListener("click", () => {
    const g = mainGraph;
    if (b.dataset.zoom === "fit") { g.touched = false; g.fit(); } else g.zoomBy(b.dataset.zoom === "in" ? 1.25 : 0.8);
  });
}

// ===================================================================== live events

function handle(ev) {
  switch (ev.type) {
    case "run_started": {
      state.events = [];
      state.runStart = ev.ts;
      state.lastTs = ev.ts;
      state.run = null;
      $("timeline").replaceChildren();
      $("result").hidden = true;
      $("changes").hidden = true;
      setRunning(true);
      setStatus("working", "Starting…");
      const label = ev.issue_ref ? `#${ev.issue_ref.number} ${ev.issue_ref.title}` : fmt.firstLine(ev.issue);
      addToTimeline(el("div", { class: "thought", style: "padding-left:0;font-style:normal;color:var(--text-2)" },
        "Working on: ", el("b", { text: label })));
      if (ev.poltergeist) addToTimeline(el("div", { class: "auto", text: `poltergeist mode: ${ev.poltergeist} round(s) after the fix` }));
      refreshMarks();
      break;
    }
    case "step":
      setStatus("working", `Working · step ${ev.number} of ${ev.max}`);
      $("progress").style.width = `${Math.min(100, (ev.number / ev.max) * 100)}%`;
      $("step-count").textContent = `· ${ev.number} steps`;
      addToTimeline(renderEvent(ev));
      break;
    case "tool":
    case "trace":
      state.events.push(ev);
      addToTimeline(renderEvent(ev, { live: true, prevTime: state.lastTs }));
      state.lastTs = ev.ts;
      refreshMarks();
      break;
    case "thought":
    case "approval":
    case "approval_auto":
      addToTimeline(renderEvent(ev, { live: true }));
      break;
    case "approval_result": {
      const card = $(`approval-${ev.request_id}`);
      if (card) {
        card.classList.add("answered");
        card.querySelector(".row:last-child").replaceWith(el("div", { class: ev.allow ? "mint" : "red", text: ev.allow ? "Allowed" : "Denied" }));
      }
      state.lastTs = ev.ts;
      break;
    }
    case "done":
    case "error":
      setRunning(false);
      setStatus(ev.type === "error" ? "error" : ev.fixed ? "fixed" : "notfixed", ev.type === "error" ? "Error" : ev.fixed ? "Fixed" : "Not fixed");
      state.run = ev.run_id ? { id: ev.run_id, fixed: ev.type === "done" && ev.fixed } : null;
      renderResult(ev);
      renderChanges(ev.diffs, state.run);
      document.querySelector("#view-dashboard .aside").scrollTop = 0;  // show the result, not the log's end
      loadGraph(true);
      if (document.querySelector("#view-runs.active")) loadRuns();
      break;
    case "undone":
      if (state.run && ev.run_id === state.run.id) { state.run.undone = true; loadRunDetailsIntoChanges(); }
      loadGraph(true);
      loadRuns();
      break;
    case "pr_opened":
      if (state.run && ev.run_id === state.run.id) { state.run.pr_url = ev.url; loadRunDetailsIntoChanges(); }
      loadRuns();
      break;
  }
}

async function loadRunDetailsIntoChanges() {
  if (!state.run) return;
  const diffsNote = $("changes-body").querySelectorAll(".difffile");
  const diffs = [...diffsNote].map((f) => f); // keep the rendered diffs; only refresh buttons and notes
  $("changes-body").querySelectorAll(".note-bar").forEach((n) => n.remove());
  $("undo-btn").hidden = !!state.run.undone;
  $("pr-btn").hidden = !(state.run.fixed && state.info.github) || !!state.run.pr_url;
  if (state.run.undone) $("changes-body").prepend(el("div", { class: "note-bar", text: "These changes were undone: every file is back the way it was before the run." }));
  if (state.run.pr_url) $("changes-body").prepend(el("div", { class: "note-bar" }, "Pull request opened: ",
    el("a", { href: state.run.pr_url, target: "_blank", rel: "noopener", text: state.run.pr_url })));
  return diffs;
}

async function startRun(issue) {
  issue = (issue || "").trim();
  if (!issue) { $("issue").focus(); return; }
  if (state.running) { toast("The ghost is already working on something.", true); return; }
  location.hash = "dashboard";
  const { ok, data } = await post("/api/run", { issue, poltergeist: Number($("poltergeist").value) });
  if (!ok) toast(data.error || "Could not start the run.", true);
}

function testIssueFor(nodes) {
  return "Improve test coverage. No test currently reaches these functions:\n"
    + nodes.map((n) => `- ${n.qualname} (${n.path}:${n.line}) ${n.signature || ""}`).join("\n")
    + "\n\nWrite focused tests for them in the project's existing test style, run them, and make sure they pass. "
    + "Do NOT change the functions themselves. If a test reveals a real bug, describe it in your summary instead.";
}

$("run").addEventListener("click", () => startRun($("issue").value));
$("issue").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) startRun($("issue").value); });
$("changes-toggle").addEventListener("click", () => $("changes").classList.toggle("collapsed"));
$("share-btn").addEventListener("click", () => state.run && window.open(`/api/runs/${state.run.id}/share`, "_blank"));
$("undo-btn").addEventListener("click", () => state.run && undoRun(state.run.id));
$("pr-btn").addEventListener("click", async () => {
  if (!state.run) return;
  const btn = $("pr-btn");
  btn.disabled = true;
  btn.textContent = "Opening…";
  const { ok, data } = await post("/api/pr", { run_id: state.run.id });
  btn.disabled = false;
  btn.innerHTML = `${icon("pr", "xs")} Open pull request`;
  if (!ok) toast(data.error || "Could not open a pull request.", true);
});

// ============================================================================ runs

const replayGraph = { view: null };

function runState(run) {
  return run.undone ? "undone" : run.error ? "error" : run.fixed ? "fixed" : "notfixed";
}

async function loadRuns() {
  try { state.runs = await getJSON("/api/history"); } catch { return; }
  const counts = { all: state.runs.length, fixed: 0, notfixed: 0, undone: 0 };
  for (const r of state.runs) { const s = runState(r); if (s === "fixed") counts.fixed++; else if (s === "undone") counts.undone++; else counts.notfixed++; }
  const filters = $("filters");
  filters.replaceChildren();
  for (const [key, label] of [["all", "All"], ["fixed", "Fixed"], ["notfixed", "Unresolved"], ["undone", "Reverted"]]) {
    filters.append(el("button", { class: "filter" + (state.filter === key ? " active" : ""), text: `${label} (${counts[key]})`,
      onclick: () => { state.filter = key; loadRuns(); } }));
  }
  const rows = $("runs-rows");
  rows.replaceChildren();
  const shown = state.runs.filter((r) => state.filter === "all" || runState(r) === state.filter
    || (state.filter === "notfixed" && runState(r) === "error"));
  if (!shown.length) rows.append(el("div", { class: "tl-empty", text: state.runs.length ? "No runs match this filter." : "No runs yet. Fix a bug on the Dashboard." }));
  for (const run of shown) {
    const conf = run.confidence || {};
    const title = run.issue_ref ? `#${run.issue_ref.number} ${run.issue_ref.title}` : fmt.firstLine(run.summary) || fmt.firstLine(run.issue);
    const score = conf.level && conf.level !== "none"
      ? el("div", { class: "mini-score" }, el("span", { class: conf.level === "high" ? "mint" : conf.level === "medium" ? "amber" : "red", text: `${conf.score}/100` }),
          bar(conf.score / 100, conf.level === "high" ? "" : conf.level === "medium" ? "amber" : "red"))
      : el("span", { class: "muted mono", text: "—" });
    const row = el("div", { class: "trow" + (state.selected === run.id ? " active" : ""), onclick: () => selectRun(run.id) },
      el("span", { class: `sdot ${runState(run)}`, title: runState(run) }),
      el("div", { class: "sum" }, el("b", { text: title || "(no summary)", title }),
        el("small", {}, el("span", { text: `#${run.id}` }), el("span", { text: `· ${run.files.length} file${run.files.length === 1 ? "" : "s"}` }),
          run.pr_url ? el("span", { class: "chip mint", text: "PR" }) : null,
          run.poltergeist && run.poltergeist.length ? el("span", { class: "chip grey", text: "poltergeist" }) : null)),
      score,
      el("span", { class: "cell-mono", text: run.model, title: run.model }),
      el("span", { class: "cell-mono", text: fmt.ago(run.created), title: run.created }));
    rows.append(row);
  }
  if (!state.selected && shown.length) selectRun(shown[0].id);
}

async function selectRun(id) {
  state.selected = id;
  document.querySelectorAll(".trow").forEach((r) => r.classList.toggle("active", r.querySelector("small span")?.textContent === `#${id}`));
  let run;
  try { run = await getJSON(`/api/runs/${id}`); } catch (e) { toast(e.message, true); return; }
  if (!state.graph.nodes.length) await loadGraph(true);  // opened straight on #runs
  renderReplay(run);
}

function renderReplay(run) {
  const box = $("replay");
  box.replaceChildren();
  const events = (run.events || []).filter((e) => ["step", "thought", "tool"].includes(e.type));
  const s = runState(run);
  const chip = { fixed: ["mint", "Fixed"], notfixed: ["amber", "Not fixed"], error: ["red", "Error"], undone: ["grey", "Undone"] }[s];
  const actions = el("div", { class: "row" },
    run.pr_url ? el("a", { class: "btn sm", href: run.pr_url, target: "_blank", rel: "noopener", html: `${icon("pr", "xs")} View PR` }) : null,
    el("a", { class: "btn sm", href: `/api/runs/${run.id}/share`, html: `${icon("share", "xs")} Share` }),
    run.files.length && !run.undone ? el("button", { class: "btn sm danger", html: `${icon("undo", "xs")} Undo`,
      onclick: async () => { if (await undoRun(run.id)) loadRuns().then(() => selectRun(run.id)); } }) : null);
  const title = run.issue_ref ? `#${run.issue_ref.number} ${run.issue_ref.title}` : fmt.firstLine(run.issue);
  box.append(el("div", { class: "replay-head" },
    el("div", { class: "row between" }, el("div", { class: "row" }, el("span", { class: "caps", text: `Run ${run.id}` }),
      el("span", { class: `chip ${chip[0]}`, text: chip[1] })), actions),
    el("h2", { text: title }),
    el("div", { class: "hint", text: `${run.steps} steps · ${fmt.tokens((run.prompt_tokens || 0) + (run.completion_tokens || 0))} tokens · ${run.model}`
      + (run.confidence && run.confidence.level !== "none" ? ` · confidence ${run.confidence.score}/100` : "") })));

  const range = el("input", { type: "range", min: "0", max: String(Math.max(0, events.length - 1)), value: String(Math.max(0, events.length - 1)), "aria-label": "Replay position" });
  const label = el("span", { class: "hint", style: "min-width:120px;text-align:right" });
  const playBtn = el("button", { class: "iconbtn", title: "Play", html: icon("play", "sm") });
  let speed = 1, timer = null;
  const speedBtns = [1, 2, 4].map((n) => el("button", { class: "speed" + (n === 1 ? " active" : ""), text: `${n}x`,
    onclick: () => { speed = n; speedBtns.forEach((b) => b.classList.toggle("active", b.textContent === `${n}x`)); } }));
  box.append(el("div", { class: "scrubber" },
    el("button", { class: "iconbtn", title: "Previous step", html: icon("prev", "sm"), onclick: () => show(Math.max(0, +range.value - 1)) }),
    playBtn,
    el("button", { class: "iconbtn", title: "Next step", html: icon("next", "sm"), onclick: () => show(Math.min(events.length - 1, +range.value + 1)) }),
    range, label, ...speedBtns));

  const list = el("div", { class: "replay-steps" });
  const nodes = [];
  let prev = null;
  for (const ev of events) {
    const node = renderEvent(ev, { prevTime: prev }) || el("div");
    if (ev.type === "tool") prev = ev.t;
    nodes.push(node);
    list.append(node);
  }
  if (!events.length) list.append(el("div", { class: "tl-empty", text: "This run has no recorded steps (runs from before 0.4 weren't recorded)." }));
  box.append(list);

  const graphBox = el("div", { class: "replay-graph" },
    el("div", { class: "canvas-head" }, el("span", { class: "caps", text: "Live blast radius" }), el("span", { class: "hint", id: "replay-step-label" })),
    el("div", { class: "canvas", id: "canvas-replay" }));
  box.append(graphBox);
  replayGraph.view = new GraphView(graphBox.querySelector(".canvas"), { mini: true });
  replayGraph.view.setData(state.graph);

  const total = events.length ? events[events.length - 1].t : 0;
  function show(i) {
    range.value = i;
    nodes.forEach((n, k) => { n.classList.toggle("future", k > i); n.classList.toggle("now", k === i); });
    const upto = events.slice(0, i + 1);
    const marks = marksFromEvents(upto, state.graph.nodes);
    if (run.trace && run.trace.path) marks.trace = run.trace.path;
    replayGraph.view.setMarks(marks);
    const step = [...upto].reverse().find((e) => e.type === "step");
    label.textContent = events.length ? `${step ? `step ${step.number} · ` : ""}${(events[i].t || 0).toFixed(1)}s / ${total.toFixed(1)}s` : "";
    nodes[i]?.scrollIntoView({ block: "nearest" });
  }
  range.addEventListener("input", () => show(+range.value));
  playBtn.addEventListener("click", () => {
    if (timer) { clearInterval(timer); timer = null; playBtn.innerHTML = icon("play", "sm"); return; }
    if (+range.value >= events.length - 1) show(0);
    playBtn.innerHTML = icon("pause", "sm");
    timer = setInterval(() => {
      const next = +range.value + 1;
      if (next >= events.length) { clearInterval(timer); timer = null; playBtn.innerHTML = icon("play", "sm"); return; }
      show(next);
    }, 700 / speed);
  });
  if (events.length) show(events.length - 1);
}

// ======================================================================== insights

const insights = { current: "gaps", loaded: {}, gapsGraph: null, lapseGraph: null, reviewGraph: null, frames: [], selected: new Set() };

for (const b of document.querySelectorAll("[data-ins]")) b.addEventListener("click", () => openInsight(b.dataset.ins));

function openInsight(name) {
  insights.current = name;
  for (const b of document.querySelectorAll("[data-ins]")) b.classList.toggle("active", b.dataset.ins === name);
  for (const p of document.querySelectorAll(".ins")) p.classList.toggle("active", p.id === `ins-${name}`);
  if (name === "gaps") loadGaps();
  if (name === "lapse" && !insights.loaded.lapse) loadTimelapse();
  if (name === "review" && !insights.reviewGraph) {
    insights.reviewGraph = new GraphView($("canvas-review"));
    insights.reviewGraph.setEmptyText("Run a review to see its blast radius here.");
    insights.reviewGraph.setData({ nodes: [], edges: [] });
  }
}

async function loadGaps() {
  if (!insights.gapsGraph) insights.gapsGraph = new GraphView($("canvas-gaps"), {
    actions: (node) => (node.test ? [] : [{ label: "Write a test", run: () => startRun(testIssueFor([node])) }]),
  });
  let gaps = [];
  try { gaps = await getJSON("/api/gaps"); } catch (e) { toast(e.message, true); }
  if (!state.graph.nodes.length) await loadGraph(true);
  insights.gapsGraph.setData(state.graph, { keepView: true });
  insights.gapsGraph.setMarks({ ...emptyMarks(), showUntested: true });
  $("gaps-count").textContent = gaps.length || "";
  $("gaps-count").hidden = !gaps.length;
  $("gaps-title").textContent = `${gaps.length} function${gaps.length === 1 ? "" : "s"} no test reaches`;
  const list = $("gaps-list");
  list.replaceChildren();
  insights.selected = new Set([...insights.selected].filter((q) => gaps.some((g) => g.qualname === q)));
  if (!gaps.length) list.append(el("div", { class: "tl-empty", text: "Every function is reached by at least one test." }));
  for (const g of gaps) {
    const callers = state.graph.edges.filter((e) => e.target === g.qualname).length;
    const box = el("input", { type: "checkbox", "aria-label": `Select ${g.qualname}` });
    box.checked = insights.selected.has(g.qualname);
    box.addEventListener("click", (e) => e.stopPropagation());
    box.addEventListener("change", () => { box.checked ? insights.selected.add(g.qualname) : insights.selected.delete(g.qualname); updateGapButton(); });
    list.append(el("div", { class: "gap-item", onclick: () => insights.gapsGraph.focusOn(g.qualname) }, box,
      el("div", {}, el("div", { class: "p", text: `${g.path}:${g.line}` }), el("div", { class: "n", text: g.name }),
        el("div", { class: "c", text: `${callers} caller${callers === 1 ? "" : "s"} · 0 tests` })),
      el("span", { class: "chip red", text: "untested" })));
  }
  updateGapButton();
  $("gaps-write").onclick = () => startRun(testIssueFor(gaps.filter((g) => insights.selected.has(g.qualname))));
}

function updateGapButton() {
  const n = insights.selected.size;
  $("gaps-write").disabled = !n;
  $("gaps-write").innerHTML = `${icon("flask", "sm")} Write tests for selected${n ? ` (${n})` : ""}`;
}

async function loadTimelapse() {
  insights.loaded.lapse = true;
  if (!insights.lapseGraph) insights.lapseGraph = new GraphView($("canvas-lapse"));
  let data;
  try { data = await getJSON("/api/timelapse?commits=20"); } catch (e) {
    $("lapse-stats").textContent = e.message;
    insights.loaded.lapse = false;
    return;
  }
  insights.frames = data.frames || [];
  const range = $("lapse-range");
  range.max = String(Math.max(0, insights.frames.length - 1));
  range.value = range.max;
  const commits = $("lapse-commits");
  commits.replaceChildren();
  const picks = insights.frames.length > 6 ? [0, Math.floor(insights.frames.length / 3), Math.floor((2 * insights.frames.length) / 3), insights.frames.length - 1] : insights.frames.map((_, i) => i);
  for (const i of picks) commits.append(el("span", { text: `${insights.frames[i].commit} · ${insights.frames[i].date}` }));
  showFrame(insights.frames.length - 1, false);
}

function showFrame(i, keepView = true) {
  const frame = insights.frames[i];
  if (!frame) { $("lapse-stats").textContent = "No commits found."; return; }
  insights.lapseGraph.setData(graphFromApi(frame), { keepView });
  insights.lapseGraph.setMarks({ ...emptyMarks(), added: new Set(i === 0 ? [] : frame.added) });
  const s = frame.stats;
  const stats = $("lapse-stats");
  stats.replaceChildren(
    el("b", { class: "mono", text: frame.commit }), el("span", { text: frame.date }),
    el("span", { text: `${s.files} files · ${s.symbols} symbols · ${s.calls} calls` }),
    el("span", { class: "mint", text: `+${frame.added.length}` }), el("span", { class: "red", text: `−${frame.removed.length}` }),
    el("span", { class: "muted", text: frame.message }));
}

$("lapse-range").addEventListener("input", (e) => showFrame(+e.target.value));
let lapseTimer = null;
$("lapse-play").addEventListener("click", () => {
  const btn = $("lapse-play"), range = $("lapse-range");
  if (lapseTimer) { clearInterval(lapseTimer); lapseTimer = null; btn.innerHTML = icon("play", "sm"); return; }
  if (+range.value >= +range.max) { range.value = 0; showFrame(0); }
  btn.innerHTML = icon("pause", "sm");
  lapseTimer = setInterval(() => {
    const next = +range.value + 1;
    if (next > +range.max) { clearInterval(lapseTimer); lapseTimer = null; btn.innerHTML = icon("play", "sm"); return; }
    range.value = next;
    showFrame(next);
  }, 1100);
});

$("review-pr").addEventListener("input", (e) => ($("review-post").disabled = !e.target.value.trim()));
$("review-run").addEventListener("click", () => runReview(false));
$("review-post").addEventListener("click", () => runReview(true));

async function runReview(postIt) {
  const pr = $("review-pr").value.trim();
  const out = $("review-report");
  out.replaceChildren(el("div", { class: "hint", text: pr ? `Reviewing pull request ${pr}…` : "Reviewing your uncommitted changes…" }));
  const { ok, data } = await post("/api/review", { pr, post: postIt });
  if (!ok) { out.replaceChildren(el("div", { class: "red", text: data.error || "The review failed." })); return; }
  const riskCls = { low: "mint", medium: "amber", high: "red" }[data.risk];
  const list = (title, items, bad) => el("div", { style: "margin-top:12px" }, el("div", { class: "caps", text: `${title} (${items.length})` }),
    el("div", { class: "review-list" }, items.length ? items.map((it) => el("div", { class: "it" + (bad(it) ? " bad" : "") },
      el("span", { text: it.qualname || it }), el("span", { class: "muted", text: it.path || "" }))) : el("div", { class: "hint", text: "none" })));
  out.replaceChildren(...[
    el("div", { class: "row between" }, el("span", { class: "caps", text: data.title ? `#${data.pr} ${data.title}` : "Uncommitted changes" }),
      el("span", { class: `chip ${riskCls}`, text: `risk: ${data.risk}` })),
    list("Changed", data.changed, (c) => !c.tested),
    list("Could also be affected", data.affected, (a) => !a.tested),
    list("Not reached by any test", data.untested, () => true),
    data.posted ? el("div", { class: "note-bar", text: "Posted on the pull request." }) : null,
  ].filter(Boolean));
  if (!state.graph.nodes.length) await loadGraph(true);
  insights.reviewGraph.setData(state.graph, { keepView: false });
  insights.reviewGraph.setMarks({ ...emptyMarks(), edited: new Set(data.changed.map((c) => c.qualname)),
    impacted: new Set(data.affected.map((a) => a.qualname)), showUntested: true });
  if (postIt && data.posted) toast("Posted the review on the pull request.");
}

// ============================================================================ start

async function start() {
  hydrateIcons();
  try { state.info = await getJSON("/api/info"); } catch (e) { toast(`Could not reach the GhostPatch server: ${e.message}`, true); return; }
  const info = state.info;
  $("repo-name").textContent = info.repo_name;
  $("repo-branch").textContent = info.branch || "no git";
  $("repo-commit").textContent = info.commit ? `#${info.commit}` : "";
  $("model").innerHTML = "";
  $("model").append(el("span", { text: info.model }), el("span", { class: "k", text: " · " }), el("span", { text: info.provider }),
    info.free ? el("span", { class: "k", text: " · " }) : null, info.free ? el("span", { class: "free", text: "free" }) : null);
  $("approval").textContent = `Approvals: ${info.approval}`;
  $("version").textContent = `v${info.version}`;
  $("poltergeist").value = String(Math.min(3, info.poltergeist || 0));
  if (info.issue_template) {
    $("load-issue").hidden = false;
    $("load-issue").onclick = () => { $("issue").value = info.issue_template; $("issue").focus(); };
  }
  if (info.running) setRunning(true);
  showView(location.hash.slice(1) || "dashboard");
  await loadGraph();
  const events = new EventSource("/api/events");
  events.onmessage = (e) => handle(JSON.parse(e.data));
}

start();
