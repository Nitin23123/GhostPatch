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
  mode: "fix",         // what the composer does: fix | haunt | ask
  runMode: "fix",      // what the current (or last) run did
  overlay: {},         // graph highlights from a result: ask's call flow, haunt's bugs
  targets: [],         // haunt mode's risk ranking
  picked: new Set(),   // the targets chosen for haunting
};

const MODES = {
  fix: { button: "Fix it", placeholder: "Describe a bug, paste a stack trace, or a GitHub issue link…",
    desc: "Fix a bug from a description, a stack trace or a GitHub issue link. Every fix is proven red→green." },
  haunt: { button: "Haunt", placeholder: "",
    desc: "Hunt for bugs nobody has reported. These are the riskiest functions; the haunter writes tests for what each is meant to do, GhostPatch runs them, and a skeptic checks every failure." },
  ask: { button: "Ask", placeholder: "Ask about the code, e.g. “How is the order total calculated?”",
    desc: "Ask anything about the code. The answer cites files and lines, and the real call flow lights up on the graph." },
};

// ================================================================== views & top bar

function showView(name) {
  const [view, sub] = name.split("/");  // e.g. "insights/night" opens an Insights tab
  name = ["dashboard", "runs", "insights", "setup"].includes(view) ? view : "dashboard";
  for (const v of document.querySelectorAll(".view")) v.classList.toggle("active", v.id === `view-${name}`);
  for (const a of document.querySelectorAll(".nav a")) a.classList.toggle("active", a.dataset.view === name);
  if (name === "runs") loadRuns();
  if (name === "insights") openInsight(sub || insights.current);
  if (name === "setup") loadSetup();
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
  if (!on) $("progress").style.width = "0";
  $("ready-hint").textContent = on ? "Ghost at work" : "Ready";
  updateRunButton();
}

function updateRunButton() {
  const btn = $("run");
  if (state.running) { btn.innerHTML = `${icon("spinner", "sm")} Working…`; return; }
  let label = MODES[state.mode].button;
  if (state.mode === "haunt") label = `Haunt ${state.picked.size} function${state.picked.size === 1 ? "" : "s"}`;
  if (state.mode === "fix" && +$("candidates").value > 1) label = `Run a ${$("candidates").value}-fix tournament`;
  btn.innerHTML = `${icon(state.mode === "ask" ? "chat" : state.mode === "haunt" ? "bug" : "play", "sm")} ${label}`;
  btn.disabled = state.mode === "haunt" && !state.picked.size;
}

// ===================================================================== composer modes

function setMode(mode) {
  if (!MODES[mode]) mode = "fix";
  state.mode = mode;
  try { localStorage.setItem("ghostpatch.mode", mode); } catch { /* private window: fine */ }
  for (const b of document.querySelectorAll(".mode")) {
    b.classList.toggle("active", b.dataset.mode === mode);
    b.setAttribute("aria-selected", b.dataset.mode === mode);
  }
  for (const pane of document.querySelectorAll("[data-pane]")) pane.hidden = !pane.dataset.pane.split(" ").includes(mode);
  if (mode === "fix" && !state.info.issue_template) $("load-issue").hidden = true;
  $("issue").placeholder = MODES[mode].placeholder;
  $("mode-desc").textContent = MODES[mode].desc;
  if (mode === "haunt" && !state.targets.length) loadTargets();
  updateRunButton();
}

async function loadTargets() {
  const box = $("haunt-targets");
  try { state.targets = await getJSON("/api/haunt/targets"); } catch (e) { box.replaceChildren(el("div", { class: "hint red", text: e.message })); return; }
  if (!state.picked.size) state.targets.slice(0, 3).forEach((t) => state.picked.add(t.qualname));
  box.replaceChildren();
  if (!state.targets.length) box.append(el("div", { class: "hint", style: "padding:10px", text: "No functions to haunt: the code graph found none." }));
  for (const t of state.targets) {
    const check = el("input", { type: "checkbox", "aria-label": `Haunt ${t.qualname}` });
    check.checked = state.picked.has(t.qualname);
    check.addEventListener("click", (e) => e.stopPropagation());
    check.addEventListener("change", () => { check.checked ? state.picked.add(t.qualname) : state.picked.delete(t.qualname); updateRunButton(); });
    box.append(el("div", { class: "target", title: `${t.path}:${t.line}`, onclick: () => mainGraph.focusOn(t.qualname) }, check,
      el("div", { style: "min-width:0" }, el("div", { class: "n", text: t.qualname }),
        el("div", { class: "why", text: t.reasons.join(" · ") || "high risk" })),
      el("span", { class: "risk", title: "risk score", text: t.risk.toFixed(1) })));
  }
  updateRunButton();
}

for (const b of document.querySelectorAll(".mode")) b.addEventListener("click", () => setMode(b.dataset.mode));
$("candidates").addEventListener("change", updateRunButton);

// ======================================================================= timeline

const TOOL_ICON = {
  read_file: "file", list_files: "folder", search_code: "search", edit_file: "edit", replace_lines: "edit",
  create_file: "file-plus", run_command: "terminal", find_symbol: "graph", find_callers: "graph", find_callees: "graph",
  related_tests: "flask", impact_of_change: "impact", read_symbol: "file", remember: "bookmark", finish: "flag",
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
  if (ev.proof) box.append(renderProof(ev.proof));
  if (ev.regression) box.append(renderRegression(ev.regression));
  if (ev.tournament && ev.tournament.candidates) box.append(renderTournament(ev.tournament));
  const conf = ev.confidence;
  if (conf && conf.level && conf.level !== "none") box.append(renderConfidence(conf));
  if (ev.poltergeist && ev.poltergeist.length) box.append(renderRounds(ev.poltergeist));
}

function renderProof(proof) {
  const proven = proof.status === "proven";
  const chip = proven ? ["mint", "proven"] : proof.status === "not_green" ? ["red", "fails"] : ["amber", proof.status.replace("_", " ")];
  const section = el("div", { class: "section proof" },
    el("div", { class: "section-head" }, el("span", { class: "caps", text: "🔴→🟢 Red-green proof" }), el("span", { class: `chip ${chip[0]}`, text: chip[1] })),
    el("div", { class: "hint", style: "margin-bottom:6px", text: proof.summary }));
  if (proof.red || proof.green) {
    section.append(
      el("div", { class: "proof-line" }, el("span", { class: "dot red" }), el("span", { class: "muted", text: "without the fix" }), el("b", { text: proof.red || "—", title: proof.red })),
      el("div", { class: "proof-line" }, el("span", { class: "dot green" }), el("span", { class: "muted", text: "with the fix" }), el("b", { text: proof.green || "—", title: proof.green })));
  }
  if (proof.tests && proof.tests.length) section.append(el("div", { class: "hint", style: "margin-top:6px", text: `tests: ${proof.tests.join(", ")}` }));
  return section;
}

function renderRegression(check) {
  const ok = check.status === "clean";
  const section = el("div", { class: "section proof" },
    el("div", { class: "section-head" }, el("span", { class: "caps", html: `${icon("shield", "xs")} Regression guard` }),
      el("span", { class: `chip ${ok ? "mint" : "red"}`, text: ok ? "nothing broke" : `${check.broken.length} broken` })),
    el("div", { class: "hint", style: "margin-bottom:6px", text: check.summary }),
    el("div", { class: "proof-line" }, el("span", { class: "dot muted" }), el("span", { class: "muted", text: "before the fix" }),
      el("b", { text: check.before || "—", title: check.before })),
    el("div", { class: "proof-line" }, el("span", { class: `dot ${ok ? "green" : "red"}` }), el("span", { class: "muted", text: "after the fix" }),
      el("b", { text: check.after || "—", title: check.after })));
  if (check.broken.length) {
    section.append(el("div", { class: "uncovered" }, check.broken.slice(0, 6).map((name) =>
      el("div", { class: "item" }, el("span", { class: "red", text: name }), el("span", { class: "hint", text: "passed before" })))));
  }
  if (check.repaired.length) section.append(el("div", { class: "hint", style: "margin-top:6px", text: `Now passing: ${check.repaired.join(", ")}` }));
  return section;
}

function renderTournament(t) {
  const list = el("div", { class: "tourney" });
  for (const c of t.candidates) {
    const facts = c.disqualified ? `out: ${c.disqualified}`
      : [`proof ${c.proof || "—"}`, `score ${c.score}`, c.rivals ? `rival tests ${c.rivals}` : null, `${c.changed_lines} lines`].filter(Boolean).join(" · ");
    list.append(el("div", { class: "cand" + (c.winner ? " win" : c.disqualified ? " out" : "") },
      el("span", { class: "who", html: `${c.winner ? icon("trophy", "xs mint") : ""}#${c.number} ${c.label}` }),
      el("span", { class: "pts", text: c.disqualified ? "" : `${Math.round(c.points)} pts` }),
      el("span", { class: "facts", text: facts })));
  }
  return el("div", { class: "section" },
    el("div", { class: "section-head" }, el("span", { class: "caps", html: `${icon("trophy", "xs")} Fix tournament` }),
      el("span", { class: "hint", text: t.winner ? `#${t.winner} won on evidence` : "no verified winner" })), list);
}

/** A tiny, safe Markdown renderer for answers and reports: escapes HTML first. */
function md(text) {
  const inline = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;")
    .replace(/`([^`]+)`/g, "<code>$1</code>").replace(/\*\*([^*]+)\*\*/g, "<b>$1</b>")
    .replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  const root = el("div", { class: "md" });
  let list = null, code = null;
  for (const line of String(text || "").split("\n")) {
    if (line.trim().startsWith("```")) { if (code) { root.append(code); code = null; } else code = el("pre"); continue; }
    if (code) { code.textContent += line + "\n"; continue; }
    const heading = line.match(/^(#{1,4})\s+(.*)/);
    const item = line.match(/^\s*(?:[-*]|\d+\.)\s+(.*)/);
    if (heading) { list = null; root.append(el(`h${Math.min(4, heading[1].length + 1)}`, { html: inline(heading[2]) })); }
    else if (item) { if (!list) { list = el("ul"); root.append(list); } list.append(el("li", { html: inline(item[1]) })); }
    else if (/^\s*---+\s*$/.test(line)) { list = null; root.append(el("hr")); }
    else if (line.trim()) { list = null; root.append(el("p", { html: inline(line) })); }
    else list = null;
  }
  if (code) root.append(code);
  return root;
}

function renderHaunt(ev) {
  const report = ev.report || {};
  const box = $("result");
  box.replaceChildren();
  box.hidden = false;
  const findings = report.findings || [];
  const bugs = findings.filter((f) => f.status === "confirmed" || f.status === "suspected");
  const card = el("div", { class: "section result-card" },
    el("div", { class: "row between" },
      el("span", { class: `chip ${bugs.length ? "red" : "mint"}`, html: `${icon(bugs.length ? "bug" : "check", "xs")} ${bugs.length ? `${bugs.length} bug${bugs.length === 1 ? "" : "s"} found` : "No bugs found"}` }),
      ev.run_id ? el("span", { class: "hint", text: `run ${ev.run_id}` }) : null),
    el("p", { text: report.summary || "" }),
    el("div", { class: "meta" }, el("span", { html: `${icon("cpu", "xs")} ${fmt.tokens((ev.prompt_tokens || 0) + (ev.completion_tokens || 0))} tokens` })));
  box.append(card);
  const list = el("div", { class: "findings" });
  const label = { confirmed: ["red", "confirmed bug"], suspected: ["amber", "suspected"], false_alarm: ["grey", "false alarm"],
    clean: ["mint", "clean"], inconclusive: ["grey", "inconclusive"], error: ["red", "error"] };
  for (const f of findings) {
    const [cls, text] = label[f.status] || ["grey", f.status];
    const item = el("div", { class: `finding ${f.status}` },
      el("div", { class: "row between" }, el("span", { class: "fq", text: f.target.qualname }), el("span", { class: `chip ${cls}`, text })),
      el("div", { class: "ev", text: `${f.target.path}:${f.target.line} · ${f.target.reasons.join(" · ")}` }));
    if (f.claim && f.status !== "clean") item.append(el("div", { class: "claim", text: f.claim }));
    if (f.tests.length && f.failure) {
      item.append(el("div", { class: "ev", text: `${f.issue ? "proof" : "checked"}: ${f.tests.join(", ")} → ${f.failure}` }));
    }
    if (f.verdict) item.append(el("div", { class: "ev", text: `skeptic: ${f.verdict}` }));
    if (f.issue) item.append(el("div", { class: "row" }, el("button", { class: "btn sm primary", html: `${icon("wrench", "xs")} Fix this bug`,
      onclick: () => { setMode("fix"); startRun(f.issue, { proof_tests: f.tests }); } })));
    list.append(item);
  }
  box.append(el("div", { class: "section" },
    el("div", { class: "section-head" }, el("span", { class: "caps", html: `${icon("ghost", "xs")} Haunt report` })), list));
  state.overlay = {
    bug: new Set(bugs.map((f) => f.target.qualname)),
    clean: new Set(findings.filter((f) => f.status === "clean").map((f) => f.target.qualname)),
  };
}

function renderAnswer(answer) {
  const box = $("result");
  box.replaceChildren();
  box.hidden = false;
  const card = el("div", { class: "section result-card" },
    el("div", { class: "row between" },
      el("span", { class: `chip ${answer.found ? "mint" : "amber"}`, html: `${icon("chat", "xs")} ${answer.found ? "Answer" : "No answer"}` }),
      el("span", { class: "hint", text: `${answer.steps} steps · ${fmt.tokens(answer.tokens)} tokens` })),
    el("div", { class: "hint", style: "margin:8px 0", text: answer.question }),
    md(answer.text || answer.error || "The ghost couldn't find an answer in the code."));
  box.append(card);
  if (answer.tree) {
    box.append(el("div", { class: "section" },
      el("div", { class: "section-head" }, el("span", { class: "caps", html: `${icon("graph", "xs")} Call flow` }),
        el("span", { class: "hint", text: "from the code graph" })),
      el("pre", { class: "flowtree", text: answer.tree })));
  }
  state.overlay = { flow: new Set((answer.flow.nodes || []).map((n) => n.qualname)) };
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
  const marks = { ...marksFromEvents(state.events, state.graph.nodes), ...state.overlay };
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
      state.runMode = ev.mode || "fix";
      state.overlay = {};
      $("timeline").replaceChildren();
      $("result").hidden = true;
      $("changes").hidden = true;
      setRunning(true);
      setStatus("working", "Starting…");
      const label = ev.issue_ref ? `#${ev.issue_ref.number} ${ev.issue_ref.title}` : fmt.firstLine(ev.issue);
      const verb = { fix: "Working on: ", haunt: "Haunting: ", ask: "Question: " }[state.runMode] || "Working on: ";
      addToTimeline(el("div", { class: "thought", style: "padding-left:0;font-style:normal;color:var(--text-2)" },
        verb, el("b", { text: label })));
      if (ev.candidates > 1) addToTimeline(el("div", { class: "auto", text: `fix tournament: ${ev.candidates} candidates compete` }));
      if (ev.poltergeist) addToTimeline(el("div", { class: "auto", text: `poltergeist mode: ${ev.poltergeist} round(s) after the fix` }));
      refreshMarks();
      break;
    }
    case "haunt_done": {
      setRunning(false);
      const bugs = (ev.report.findings || []).filter((f) => f.status === "confirmed" || f.status === "suspected").length;
      setStatus(bugs ? "error" : "fixed", bugs ? `${bugs} bug${bugs === 1 ? "" : "s"} found` : "No bugs found");
      state.run = ev.run_id ? { id: ev.run_id, fixed: false } : null;
      renderHaunt(ev);
      renderChanges(ev.diffs, state.run);
      document.querySelector("#view-dashboard .aside").scrollTop = 0;
      loadGraph(true);
      if (document.querySelector("#view-runs.active")) loadRuns();
      break;
    }
    case "ask_done":
      setRunning(false);
      setStatus(ev.answer.found ? "fixed" : "notfixed", ev.answer.found ? "Answered" : "No answer");
      renderAnswer(ev.answer);
      document.querySelector("#view-dashboard .aside").scrollTop = 0;
      refreshMarks();
      break;
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
    case "thought": {
      // Tournament candidates take turns; remember whose edits are whose (see winnerOnly).
      const text = String(ev.text || "");
      const turn = text.match(/🏆 Candidate (\d+) of \d+/);
      if (turn) state.events.push({ type: "candidate", number: +turn[1] });
      else if (/🏆 (Candidate \d+ \(.*\) wins|No candidate produced)/.test(text)) state.events.push({ type: "candidate", number: 0 });
      addToTimeline(renderEvent(ev, { live: true }));
      break;
    }
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
      if (ev.tournament) state.events = winnerOnly(state.events, ev.tournament.winner);
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

/** After a tournament, keep only the graph events of the winning candidate (and what came after). */
function winnerOnly(events, winner) {
  let turn = null;
  return events.filter((ev) => {
    if (ev.type === "candidate") { turn = ev.number; return false; }
    return turn === null || turn === 0 || turn === winner;
  });
}

/** Fix a bug. `extra` can carry proof_tests (a haunted bug's failing tests). */
async function startRun(issue, extra = {}) {
  issue = (issue || "").trim();
  if (!issue) { $("issue").focus(); return; }
  await launch({ mode: "fix", issue, poltergeist: Number($("poltergeist").value), candidates: Number($("candidates").value),
    prove: $("prove").checked, regression: $("guard").checked, ...extra });
}

async function launch(body) {
  if (state.running) { toast("The ghost is already working on something.", true); return; }
  location.hash = "dashboard";
  const { ok, data } = await post("/api/run", body);
  if (!ok) toast(data.error || "Could not start the run.", true);
}

function submit() {
  const text = $("issue").value.trim();
  if (state.mode === "haunt") launch({ mode: "haunt", targets: state.picked.size, only: [...state.picked] });
  else if (state.mode === "ask") { if (text) launch({ mode: "ask", question: text }); else $("issue").focus(); }
  else startRun(text);
}

function testIssueFor(nodes) {
  return "Improve test coverage. No test currently reaches these functions:\n"
    + nodes.map((n) => `- ${n.qualname} (${n.path}:${n.line}) ${n.signature || ""}`).join("\n")
    + "\n\nWrite focused tests for them in the project's existing test style, run them, and make sure they pass. "
    + "Do NOT change the functions themselves. If a test reveals a real bug, describe it in your summary instead.";
}

$("run").addEventListener("click", submit);
$("issue").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit(); });
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
  return run.undone ? "undone" : run.kind === "haunt" ? "haunt" : run.error ? "error" : run.fixed ? "fixed" : "notfixed";
}

async function loadRuns() {
  try { state.runs = await getJSON("/api/history"); } catch { return; }
  const counts = { all: state.runs.length, fixed: 0, notfixed: 0, undone: 0, haunt: 0 };
  for (const r of state.runs) {
    const s = runState(r);
    if (s in counts && s !== "notfixed") counts[s]++; else counts.notfixed++;
  }
  const filters = $("filters");
  filters.replaceChildren();
  for (const [key, label] of [["all", "All"], ["fixed", "Fixed"], ["notfixed", "Unresolved"], ["haunt", "Haunts"], ["undone", "Reverted"]]) {
    if (key === "haunt" && !counts.haunt) continue;
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
          run.proof && run.proof.status === "proven" ? el("span", { class: "chip mint", text: "proven" }) : null,
          run.regression ? el("span", { class: `chip ${run.regression.status === "clean" ? "grey" : "red"}`,
            text: run.regression.status === "clean" ? "no regressions" : `${run.regression.broken.length} broken` }) : null,
          run.tournament ? el("span", { class: "chip grey", text: `tournament ×${run.tournament.candidates.length}` }) : null,
          run.kind === "haunt" ? el("span", { class: "chip amber", text: "haunt" }) : null,
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
  const chip = { fixed: ["mint", "Fixed"], notfixed: ["amber", "Not fixed"], error: ["red", "Error"], undone: ["grey", "Undone"],
    haunt: ["amber", "Haunt"] }[s];
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
      + (run.confidence && run.confidence.level && run.confidence.level !== "none" ? ` · confidence ${run.confidence.score}/100` : "")
      + (run.proof ? ` · ${run.proof.status === "proven" ? "🔴→🟢 proven" : `proof: ${run.proof.status.replace("_", " ")}`}` : "")
      + (run.tournament ? ` · tournament won by #${run.tournament.winner ?? "—"}` : "") })));

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
  if (name === "night") loadNight();
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

async function loadNight() {
  let reports = [];
  try { reports = await getJSON("/api/nightshift"); } catch (e) { toast(e.message, true); }
  const list = $("night-list");
  list.replaceChildren();
  const show = (r, item) => {
    list.querySelectorAll(".night-item").forEach((n) => n.classList.toggle("active", n === item));
    $("night-report").replaceChildren(md(r.markdown));
  };
  reports.forEach((r, i) => {
    const count = (status) => r.items.filter((it) => it.status === status).length;
    const found = r.items.filter((it) => it.kind === "haunt").length;
    const item = el("div", { class: "night-item" },
      el("span", { class: "when", text: r.started }),
      el("div", { class: "counts" },
        el("span", { class: "chip mint", text: `${count("pr")} PR${count("pr") === 1 ? "" : "s"}` }),
        count("needs_you") ? el("span", { class: "chip amber", text: `${count("needs_you")} need you` }) : null,
        found ? el("span", { class: "chip red", text: `${found} found` }) : null,
        r.stopped ? el("span", { class: "chip grey", text: "stopped early" }) : null));
    item.addEventListener("click", () => show(r, item));
    list.append(item);
    if (i === 0) show(r, item);
  });
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

// ======================================================= what the ghost can do (welcome)

function chooseMode(mode, select) {
  location.hash = "dashboard";
  setMode(mode);
  if (select) { const s = $(select[0]); s.value = select[1]; s.dispatchEvent(new Event("change")); }
  if (mode !== "haunt") $("issue").focus();
}

const CAPABILITIES = [
  ["wrench", "Fix a bug", "From a description, a stack trace or an issue link, proven red→green.", () => chooseMode("fix")],
  ["trophy", "Fix tournament", "Several fixes compete; the best-proven one wins.", () => chooseMode("fix", ["candidates", "3"])],
  ["ghost", "Poltergeist", "A second agent tries to break every fix.", () => chooseMode("fix", ["poltergeist", "1"])],
  ["bug", "Haunt", "Find bugs nobody has reported yet.", () => chooseMode("haunt")],
  ["chat", "Ask", "Questions about the code, with the real call flow.", () => chooseMode("ask")],
  ["flask", "Test gaps", "Functions no test reaches; write tests in a click.", "#insights/gaps"],
  ["pr", "Review a change", "The blast radius of a pull request or your own edits.", "#insights/review"],
  ["clock", "Time-lapse", "Watch the architecture evolve, commit by commit.", "#insights/lapse"],
  ["moon", "Night shift", "Labelled issues fixed overnight, with a morning report.", "#insights/night"],
  ["steps", "Runs & replay", "Every run recorded: replay, share or undo it.", "#runs"],
  ["bookmark", "Repo memory", "Team conventions and what the ghost has learned.", "#setup"],
  ["shield", "Setup & GitHub", "Models, fallback, health and one-click workflows.", "#setup"],
];

function renderWelcome() {
  const grid = el("div", { class: "caps-grid" });
  for (const [ic, title, text, action] of CAPABILITIES) {
    const link = typeof action === "string";
    grid.append(el(link ? "a" : "button", { class: "cap", href: link ? action : null, onclick: link ? null : action },
      el("span", { class: "cap-t", html: `${icon(ic, "xs")} ${title}` }), el("span", { class: "cap-d", text })));
  }
  $("timeline").replaceChildren(el("div", { class: "welcome" },
    el("div", { class: "caps", text: "What the ghost can do" }), grid,
    el("div", { class: "hint", text: "Every step the ghost takes will appear here." })));
}

// =========================================================================== setup

function panelHead(ic, title) {
  return el("div", { class: "section-head" }, el("span", { class: "caps", html: `${icon(ic, "xs")} ${title}` }));
}

function kv(label, value) {
  return el("div", { class: "kv" }, el("span", { class: "muted", text: label }), el("b", { text: value }));
}

async function copyText(text) {
  try { await navigator.clipboard.writeText(text); toast("Copied to the clipboard."); }
  catch { toast("Couldn't copy: select the text and copy it yourself.", true); }
}

function loadSetup() {
  loadSetupModel();
  loadWorkflows();
  loadMemory();
}

async function loadSetupModel() {
  let data;
  try { data = await getJSON("/api/setup"); } catch (e) { $("setup-model").replaceChildren(el("div", { class: "red", text: e.message })); return; }
  const chain = el("div", { class: "chain" });
  data.chain.forEach((c, i) => {
    if (i) chain.append(el("span", { class: "muted", text: "→" }));
    chain.append(el("span", { class: `chip ${i ? "grey" : "mint"}`, text: c }));
  });
  const providers = el("div", { class: "prov-list" }, data.providers.map((p) => {
    const ready = p.configured && !p.local;  // a local model needs Ollama running, which we don't check here
    return el("div", { class: "prov" },
      el("span", { class: ready ? "mint" : "muted", html: icon(ready ? "check" : "dot", "sm") }),
      el("b", { text: p.name }),
      el("span", { class: "muted", text: p.local ? "local model: needs Ollama running" : p.configured ? `${p.key_env} is set` : `${p.key_env} not set` }),
      el("span", { class: `chip ${p.free ? "mint" : "grey"}`, text: p.free ? "free" : "paid" }),
      ready ? null : el("a", { href: p.signup_url, target: "_blank", rel: "noopener", class: "mint", text: p.local ? "get Ollama" : "get a key" }));
  }));
  $("setup-model").replaceChildren(panelHead("cpu", "Model & fallback"),
    kv("Model", `${data.model} (${data.provider})`), kv("Approvals", data.approval),
    el("div", { class: "hint", style: "margin:10px 0 6px", text: data.fallback ? "When a daily quota runs out, the run carries on down this chain:" : "Fallback is turned off." }),
    chain,
    el("div", { class: "caps", style: "margin-top:16px", text: "Providers" }), providers,
    el("div", { class: "hint", style: "margin-top:12px", html: "Add a backup key with <code>ghostpatch init</code>, then restart <code>ghostpatch serve</code>. "
      + 'Step-by-step help: the <a class="mint" href="https://github.com/Nitin23123/GhostPatch/blob/main/docs/USER_GUIDE.md" target="_blank" rel="noopener">user guide</a>.' }));
  const icons = { ok: "check", warn: "alert", fail: "x" };
  $("setup-health").replaceChildren(panelHead("shield", "Health check"),
    el("div", { class: "hint", style: "margin-bottom:8px", text: "The checks `ghostpatch doctor` runs, without contacting the model provider." }),
    el("div", { class: "checks" }, data.checks.map((c) => el("div", { class: `check-row ${c.status}` },
      el("span", { html: icon(icons[c.status] || "dot", "sm") }), el("b", { text: c.name }), el("span", { text: c.detail })))));
}

async function installWorkflow(name, overwrite = false) {
  const { ok, status, data } = await post("/api/workflows", { name, overwrite });
  if (ok) {
    toast(`Added ${data.path}. Commit and push it, then add a free API key as a repository secret.`);
    if (document.querySelector("#view-setup.active")) loadWorkflows();
    return;
  }
  if (status === 409 && confirm(`${data.error}\n\nReplace it with GhostPatch's version?`)) return installWorkflow(name, true);
  if (status !== 409) toast(data.error || "Couldn't add the workflow.", true);
}

async function loadWorkflows() {
  let flows = [];
  try { flows = await getJSON("/api/workflows"); } catch (e) { toast(e.message, true); }
  const cards = flows.map((w) => {
    const yaml = el("pre", { class: "yaml", text: w.yaml, hidden: true });
    return el("div", { class: "flow-card" },
      el("div", { class: "row between" }, el("b", { text: w.title }),
        el("span", { class: `chip ${w.installed ? "mint" : "grey"}`, text: w.installed ? "added" : "not added" })),
      el("div", { class: "hint", text: w.description }),
      el("div", { class: "path mono", text: w.path, title: `in ${w.root}` }),
      el("div", { class: "row" },
        el("button", { class: `btn sm ${w.installed ? "" : "primary"}`, html: `${icon("file-plus", "xs")} ${w.installed ? "Replace" : "Add to repository"}`,
          onclick: () => installWorkflow(w.name, false) }),
        el("button", { class: "btn sm", text: "Show YAML", onclick: (e) => { yaml.hidden = !yaml.hidden; e.target.textContent = yaml.hidden ? "Show YAML" : "Hide YAML"; } }),
        el("button", { class: "btn sm ghost", html: `${icon("share", "xs")} Copy`, onclick: () => copyText(w.yaml) })),
      yaml);
  });
  $("setup-automation").replaceChildren(panelHead("branch", "GitHub automation"),
    el("div", { class: "hint", style: "margin-bottom:10px", text: "Run GhostPatch on GitHub Actions, for free. Adding a workflow writes the file into the repository; then commit and push it, add a free API key (e.g. GROQ_API_KEY) as a repository secret, and allow Actions to create pull requests (Settings → Actions → General)." }),
    el("div", { class: "flows" }, cards));
}

async function loadMemory() {
  let mem = { team: "", learned: [] };
  try { mem = await getJSON("/api/memory"); } catch (e) { toast(e.message, true); }
  const input = el("input", { class: "field", placeholder: "e.g. Run the tests with: pytest -q tests/", "aria-label": "A note for the ghost" });
  const add = async () => {
    const note = input.value.trim();
    if (!note) { input.focus(); return; }
    const { ok, data } = await post("/api/memory", { note });
    if (ok) { toast("Remembered for every future run."); loadMemory(); } else toast(data.error || "Couldn't save the note.", true);
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") add(); });
  const forget = async () => {
    if (!confirm("Forget everything the ghost has learned? GHOSTPATCH.md is kept.")) return;
    await post("/api/memory", { clear: true });
    loadMemory();
  };
  $("setup-memory").replaceChildren(panelHead("bookmark", "Repo memory"),
    el("div", { class: "hint", style: "margin-bottom:10px", text: "Fed into every run. Team conventions live in GHOSTPATCH.md at the repository root; the ghost adds what it learns as it works." }),
    el("div", { class: "mem-cols" },
      el("div", {}, el("div", { class: "caps", text: "GHOSTPATCH.md · team" }),
        mem.team ? el("pre", { class: "yaml", text: mem.team })
          : el("div", { class: "hint", style: "margin-top:6px", text: "No GHOSTPATCH.md yet. Create one with your conventions: how to run the tests, code style, things to avoid." })),
      el("div", {},
        el("div", { class: "row between" }, el("span", { class: "caps", text: `Learned · ${mem.learned.length}` }),
          mem.learned.length ? el("button", { class: "btn sm ghost danger", text: "Forget all", onclick: forget }) : null),
        mem.learned.length ? el("ul", { class: "notes" }, mem.learned.map((n) => el("li", { text: n })))
          : el("div", { class: "hint", style: "margin-top:6px", text: "Nothing yet. The ghost saves facts here as it works, like how the tests run." }),
        el("div", { class: "row", style: "margin-top:10px;flex-wrap:nowrap" }, input, el("button", { class: "btn sm", text: "Remember", onclick: add })))));
}

$("night-install").addEventListener("click", () => installWorkflow("nightshift"));

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
  let mode = "fix";
  try { mode = localStorage.getItem("ghostpatch.mode") || "fix"; } catch { /* storage blocked: default */ }
  setMode(mode);
  renderWelcome();
  showView(location.hash.slice(1) || "dashboard");
  await loadGraph();
  const events = new EventSource("/api/events");
  events.onmessage = (e) => handle(JSON.parse(e.data));
}

start();
