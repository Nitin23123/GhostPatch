/* GhostPatch code graph: files as cards, functions as rows, calls as edges.
 *
 * Layout: files are placed in columns by dependency. Code that others call sits on the left and its
 * callers to the right, so an edit's blast radius flows left to right. Test files come last. Edges point
 * from a function to the code that calls it (the direction a change ripples).
 */
(() => {
"use strict";

const SVG_NS = "http://www.w3.org/2000/svg";
const CARD_W = 230, HEAD_H = 29, ROW_H = 24, GAP_X = 96, GAP_Y = 28, MAX_ROWS = 14;

function svgEl(tag, attrs = {}) {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

/** Normalise /api/graph data (numeric ids) or a time-lapse frame (qualnames) to one shape. */
function graphFromApi(data) {
  const byId = new Map((data.nodes || []).map((n) => [n.id, n.qualname]));
  const edges = (data.edges || []).map((e) => ({
    source: typeof e.source === "number" ? byId.get(e.source) : e.source,
    target: typeof e.target === "number" ? byId.get(e.target) : e.target,
  })).filter((e) => e.source && e.target);
  return { nodes: data.nodes || [], edges, stats: data.stats || null, truncated: !!data.truncated };
}

function emptyMarks() {
  return { read: new Set(), queried: new Set(), edited: new Set(), impacted: new Set(), added: new Set(),
           trace: [], current: null, showUntested: false, focus: null,
           flow: new Set(), bug: new Set(), clean: new Set() };  // ask mode's call flow; haunt mode's verdicts
}

/** Work out what to highlight from a list of agent events (live or recorded). */
function marksFromEvents(events, nodes) {
  const marks = emptyMarks();
  const byPath = (p) => nodes.filter((n) => n.path === p).map((n) => n.qualname);
  const byName = (name) => {
    name = String(name);
    const short = name.split(".").pop();
    return nodes.filter((n) => n.name === short && (n.qualname === name || n.qualname.endsWith("." + name) || !name.includes(".")))
      .map((n) => n.qualname);
  };
  const add = (set, keys) => { for (const k of keys) if (k) { set.add(k); marks.current = k; } };
  for (const ev of events) {
    if (ev.type === "trace") { marks.trace = ev.path || []; continue; }
    if (ev.type !== "tool") continue;
    const args = ev.args || {}, result = ev.result || "";
    if (["read_file", "create_file"].includes(ev.name) && args.path) add(marks.read, byPath(String(args.path).replace(/\\/g, "/").replace(/^\.\//, "")));
    if (["find_symbol", "find_callers", "find_callees", "related_tests", "impact_of_change"].includes(ev.name) && args.name) add(marks.queried, byName(args.name));
    if (ev.name === "search_code") {
      for (const m of result.matchAll(/^([^\s:]+\.(?:py|[cm]?[jt]sx?)):\d+:/gm)) add(marks.read, byPath(m[1]));
    }
    if (["impact_of_change", "edit_file", "replace_lines"].includes(ev.name)) {
      const changed = result.match(/you changed ([\w.]*\w)/);
      if (changed && !/you changed the test/.test(result)) add(marks.edited, [changed[1]]);
      for (const m of result.matchAll(/ in ([\w.]+)\s*$/gm)) marks.impacted.add(m[1]);
      const tests = result.match(/tests to run: (.*)$/m);
      for (const item of tests ? tests[1].split(", ") : []) {
        const [p, n] = item.split("::");
        if (n) for (const node of nodes) if (node.path === p && node.name === n) marks.impacted.add(node.qualname);
      }
    }
  }
  return marks;
}

class GraphView {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.opts = opts;
    this.layer = document.createElement("div");
    this.layer.className = "graph-layer";
    this.svg = svgEl("svg");
    this.svg.append(this._defs());
    this.edgeGroup = svgEl("g");
    this.svg.append(this.edgeGroup);
    this.layer.append(this.svg);
    canvas.append(this.layer);
    this.empty = document.createElement("div");
    this.empty.className = "canvas-empty";
    canvas.append(this.empty);
    this.view = { x: 0, y: 0, k: 1 };
    this.data = { nodes: [], edges: [] };
    this.marks = emptyMarks();
    this.rows = new Map();    // qualname -> {el, card, x, y}
    this.cards = new Map();   // path -> {el, x, y, h, rows: [qualname]}
    this.edges = [];          // {el, caller, callee}
    this.touched = false;     // has the user panned/zoomed? (then don't auto-fit)
    this._panZoom();
    new ResizeObserver(() => { if (!this.touched && this.data.nodes.length) this.fit(); }).observe(canvas);
  }

  _defs() {
    const defs = svgEl("defs");
    for (const [id, color] of [["muted", "#3e4943"], ["red", "#ffb4ab"], ["mint", "#7ee0b5"], ["amber", "#ffc433"]]) {
      const marker = svgEl("marker", { id: `gp-arrow-${id}-${this.canvas.id || "g"}`, viewBox: "0 0 10 10", refX: "9", refY: "5",
                                        markerWidth: "7", markerHeight: "7", orient: "auto-start-reverse" });
      marker.append(svgEl("path", { d: "M 0 1 L 9 5 L 0 9 z", fill: color }));
      defs.append(marker);
    }
    return defs;
  }

  _arrow(kind) { return `url(#gp-arrow-${kind}-${this.canvas.id || "g"})`; }

  setEmptyText(text) { this.emptyText = text; this._updateEmpty(); }

  _updateEmpty() {
    this.empty.hidden = this.data.nodes.length > 0;
    this.empty.textContent = this.emptyText || "No Python, JavaScript or TypeScript code found.";
  }

  setData(data, { keepView = false } = {}) {
    this.data = data;
    this._layout();
    this._render();
    this.applyMarks();
    this._updateEmpty();
    if (!keepView || !this.touched) this.fit();
  }

  setMarks(marks) {
    this.marks = { ...emptyMarks(), ...marks };
    this.applyMarks();
  }

  // ------------------------------------------------------------------ layout
  _layout() {
    const nodes = this.data.nodes;
    this.nodeByQ = new Map(nodes.map((n) => [n.qualname, n]));
    const byPath = new Map();
    for (const n of nodes) (byPath.get(n.path) || byPath.set(n.path, []).get(n.path)).push(n);
    for (const list of byPath.values()) list.sort((a, b) => a.line - b.line);
    const isTest = (p) => byPath.get(p).every((n) => n.test);
    const deps = new Map([...byPath.keys()].map((p) => [p, new Set()]));
    for (const e of this.data.edges) {
      const caller = this.nodeByQ.get(e.source), callee = this.nodeByQ.get(e.target);
      if (caller && callee && caller.path !== callee.path) deps.get(caller.path).add(callee.path);
    }
    const col = new Map([...byPath.keys()].map((p) => [p, 0]));
    const files = [...byPath.keys()].filter((p) => !isTest(p));
    for (let i = 0; i < files.length; i++) {
      let changed = false;
      for (const f of files) {
        let c = 0;
        for (const d of deps.get(f)) if (!isTest(d)) c = Math.max(c, col.get(d) + 1);
        c = Math.min(c, files.length);
        if (c !== col.get(f)) { col.set(f, c); changed = true; }
      }
      if (!changed) break;
    }
    const last = files.length ? Math.max(...files.map((f) => col.get(f))) + 1 : 0;
    for (const p of byPath.keys()) if (isTest(p)) col.set(p, last);

    // Compact empty columns away, then stack cards in each column.
    const used = [...new Set(col.values())].sort((a, b) => a - b);
    const columns = used.map((c) => [...byPath.keys()].filter((p) => col.get(p) === c).sort());
    this.layout = [];
    let maxH = 0;
    columns.forEach((paths, ci) => {
      let y = 0;
      for (const path of paths) {
        const all = byPath.get(path);
        const shown = all.slice(0, all.length > MAX_ROWS ? MAX_ROWS - 1 : MAX_ROWS);
        const hidden = all.slice(shown.length);
        const h = HEAD_H + (shown.length + (hidden.length ? 1 : 0)) * ROW_H + 4;
        this.layout.push({ path, x: ci * (CARD_W + GAP_X), y, h, shown, hidden, test: isTest(path) });
        y += h + GAP_Y;
      }
      maxH = Math.max(maxH, y);
    });
    this.bounds = { w: Math.max(columns.length * (CARD_W + GAP_X) - GAP_X, CARD_W), h: Math.max(maxH - GAP_Y, 60) };
  }

  // ------------------------------------------------------------------ render
  _render() {
    for (const card of this.cards.values()) card.el.remove();
    this.cards.clear();
    this.rows.clear();
    this.edgeGroup.replaceChildren();
    this.edges = [];
    this.layer.style.width = `${this.bounds.w}px`;
    this.layer.style.height = `${this.bounds.h}px`;
    this.svg.setAttribute("width", this.bounds.w);
    this.svg.setAttribute("height", this.bounds.h);

    for (const card of this.layout) {
      const el = document.createElement("div");
      el.className = "card" + (card.test ? " test" : "");
      el.style.left = `${card.x}px`;
      el.style.top = `${card.y}px`;
      const head = document.createElement("div");
      head.className = "card-head";
      const path = document.createElement("span");
      path.className = "path";
      path.textContent = card.path;
      path.title = card.path;
      const count = document.createElement("span");
      count.className = "count";
      count.textContent = `${card.shown.length + card.hidden.length} symbols`;
      head.append(path, count);
      el.append(head);
      card.shown.forEach((node, i) => {
        const row = document.createElement("div");
        const kind = node.kind === "method" ? "method" : node.kind === "class" ? "class"
          : node.kind === "test" || node.kind === "suite" ? "test" : "function";
        row.className = `srow k-${kind}`;
        row.dataset.q = node.qualname;
        row.innerHTML = '<span class="dot"></span><span class="nm"></span><span class="badge" hidden></span>';
        row.querySelector(".nm").textContent = node.kind === "method" ? node.name : node.name;
        row.title = `${node.qualname}\n${node.path}:${node.line}`;
        el.append(row);
        this.rows.set(node.qualname, { el: row, card, y: card.y + HEAD_H + i * ROW_H + ROW_H / 2 });
      });
      if (card.hidden.length) {
        const more = document.createElement("div");
        more.className = "srow more";
        more.innerHTML = '<span class="nm"></span><span class="badge" hidden></span>';
        more.querySelector(".nm").textContent = `+${card.hidden.length} more`;
        el.append(more);
        const y = card.y + HEAD_H + card.shown.length * ROW_H + ROW_H / 2;
        for (const node of card.hidden) this.rows.set(node.qualname, { el: more, card, y, hidden: true });
      }
      this.layer.append(el);
      this.cards.set(card.path, { el, card });
    }

    const seen = new Set();
    for (const e of this.data.edges) {
      const caller = this.rows.get(e.source), callee = this.rows.get(e.target);
      if (!caller || !callee || caller.el === callee.el) continue;
      const key = `${callee.el.dataset.q || callee.card.path + "+"}>${caller.el.dataset.q || caller.card.path + "+"}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const path = svgEl("path", { class: "edge", d: this._curve(callee, caller), "marker-end": this._arrow("muted") });
      this.edgeGroup.append(path);
      this.edges.push({ el: path, caller: e.source, callee: e.target });
    }
  }

  _curve(from, to) {
    const fc = from.card, tc = to.card;
    if (tc.x > fc.x) {  // caller to the right: leave from the right edge, arrive on the left edge
      const x1 = fc.x + CARD_W, x2 = tc.x, dx = Math.max(30, (x2 - x1) / 2);
      return `M ${x1} ${from.y} C ${x1 + dx} ${from.y}, ${x2 - dx} ${to.y}, ${x2 - 2} ${to.y}`;
    }
    if (tc.x === fc.x) {  // same column: loop out on the right
      const x = fc.x + CARD_W, bulge = 34 + Math.min(40, Math.abs(to.y - from.y) / 6);
      return `M ${x} ${from.y} C ${x + bulge} ${from.y}, ${x + bulge} ${to.y}, ${x + 2} ${to.y}`;
    }
    const x1 = fc.x, x2 = tc.x + CARD_W, dx = Math.max(30, (x1 - x2) / 2);  // caller to the left
    return `M ${x1} ${from.y} C ${x1 - dx} ${from.y}, ${x2 + dx} ${to.y}, ${x2 + 2} ${to.y}`;
  }

  // ------------------------------------------------------------------- marks
  applyMarks() {
    const m = this.marks;
    const tracePath = m.trace || [];
    const hot = new Set([...m.edited, ...m.impacted]);
    const focus = m.focus;
    const cardState = new Map();
    for (const [q, row] of this.rows) {
      if (row.hidden) continue;
      const node = this.nodeByQ.get(q);
      const el = row.el;
      const traceIndex = tracePath.indexOf(q);
      const bug = m.bug.has(q), flow = m.flow.has(q), clean = m.clean.has(q);
      const flags = {
        edited: m.edited.has(q) || clean, impacted: (m.impacted.has(q) || bug) && !m.edited.has(q),
        trace: traceIndex >= 0 || flow, read: m.read.has(q), queried: m.queried.has(q), added: m.added.has(q),
        untested: m.showUntested && node && !node.tested,
      };
      for (const [cls, on] of Object.entries(flags)) el.classList.toggle(cls, !!on);
      el.classList.toggle("current", q === m.current);
      el.classList.toggle("match", !!(focus && focus.has(q)));
      const badge = el.querySelector(".badge");
      const label = bug ? "BUG" : clean ? "CLEAN" : flow ? "FLOW" : m.edited.has(q) ? "EDITED"
        : traceIndex >= 0 ? (traceIndex === tracePath.length - 1 ? "CRASH" : "PATH")
        : flags.impacted ? "AFFECTED" : flags.added ? "NEW" : flags.untested ? "NO TESTS" : "";
      badge.hidden = !label;
      badge.textContent = label;
      const state = cardState.get(row.card.path) || {};
      if (flags.edited) state.edited = true;
      if (flags.impacted) state.impacted = true;
      if (flags.trace) state.trace = true;
      if (focus && focus.has(q)) state.focus = true;
      cardState.set(row.card.path, state);
    }
    for (const [path, { el }] of this.cards) {
      const s = cardState.get(path) || {};
      el.classList.toggle("edited", !!s.edited);
      el.classList.toggle("impacted", !!s.impacted && !s.edited);
      el.classList.toggle("trace", !!s.trace && !s.edited);
      el.classList.toggle("dim", !!(focus && focus.size && !s.focus));
    }
    const tracePairs = new Set();
    for (let i = 1; i < tracePath.length; i++) {
      tracePairs.add(`${tracePath[i]}>${tracePath[i - 1]}`);
      tracePairs.add(`${tracePath[i - 1]}>${tracePath[i]}`);
    }
    for (const e of this.edges) {
      const isHot = m.edited.size > 0 && hot.has(e.callee) && hot.has(e.caller);
      const isTrace = tracePairs.has(`${e.callee}>${e.caller}`) || (m.flow.has(e.callee) && m.flow.has(e.caller));
      const isMint = !isHot && m.edited.has(e.callee);
      e.el.setAttribute("class", "edge" + (isHot ? " hot" : isTrace ? " trace" : isMint ? " mint" : "")
        + (focus && focus.size && !focus.has(e.caller) && !focus.has(e.callee) ? " dim" : ""));
      e.el.setAttribute("marker-end", this._arrow(isHot ? "red" : isTrace ? "amber" : isMint ? "mint" : "muted"));
    }
  }

  /** Highlight symbols whose name contains `term`. Returns the matches. */
  search(term) {
    term = (term || "").trim().toLowerCase();
    if (!term) {
      this.marks.focus = null;
      this.applyMarks();
      return [];
    }
    const matches = this.data.nodes.filter((n) => n.qualname.toLowerCase().includes(term)).map((n) => n.qualname);
    this.marks.focus = new Set(matches);
    this.applyMarks();
    if (matches.length) this.focusOn(matches[0]);
    return matches;
  }

  // ---------------------------------------------------------------- viewport
  _apply() {
    this.layer.style.transform = `translate(${this.view.x}px, ${this.view.y}px) scale(${this.view.k})`;
  }

  fit() {
    const r = this.canvas.getBoundingClientRect();
    if (!r.width || !r.height) return;
    const pad = this.opts.mini ? 16 : 40;
    const k = Math.max(0.25, Math.min(this.opts.mini ? 0.9 : 1.15, (r.width - pad * 2) / this.bounds.w, (r.height - pad * 2) / this.bounds.h));
    this.view = { k, x: (r.width - this.bounds.w * k) / 2, y: Math.max(pad, (r.height - this.bounds.h * k) / 2) };
    this._apply();
  }

  zoomBy(factor, cx, cy) {
    const r = this.canvas.getBoundingClientRect();
    cx = cx ?? r.width / 2;
    cy = cy ?? r.height / 2;
    const k = Math.max(0.2, Math.min(3, this.view.k * factor));
    this.view.x = cx - ((cx - this.view.x) * k) / this.view.k;
    this.view.y = cy - ((cy - this.view.y) * k) / this.view.k;
    this.view.k = k;
    this.touched = true;
    this._apply();
  }

  focusOn(qualname) {
    const row = this.rows.get(qualname);
    if (!row) return;
    const r = this.canvas.getBoundingClientRect();
    const k = Math.max(this.view.k, 0.9);
    this.view = { k, x: r.width / 2 - (row.card.x + CARD_W / 2) * k, y: r.height / 2 - row.y * k };
    this._apply();
  }

  _panZoom() {
    const c = this.canvas;
    c.addEventListener("wheel", (e) => {
      e.preventDefault();
      const r = c.getBoundingClientRect();
      this.zoomBy(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX - r.left, e.clientY - r.top);
    }, { passive: false });
    let drag = null;
    c.addEventListener("pointerdown", (e) => {
      if (e.target.closest(".popover, .canvas-tools, .legend")) return;
      drag = { x: e.clientX, y: e.clientY, vx: this.view.x, vy: this.view.y, moved: false, target: e.target };
      c.setPointerCapture(e.pointerId);
    });
    c.addEventListener("pointermove", (e) => {
      if (!drag) return;
      const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
      if (!drag.moved && Math.hypot(dx, dy) < 4) return;
      drag.moved = true;
      this.touched = true;
      c.classList.add("dragging");
      this.view.x = drag.vx + dx;
      this.view.y = drag.vy + dy;
      this._apply();
    });
    const end = () => {
      if (drag && !drag.moved) {
        const row = drag.target.closest && drag.target.closest(".srow[data-q]");
        this._popover(row ? row.dataset.q : null);
      }
      drag = null;
      c.classList.remove("dragging");
    };
    c.addEventListener("pointerup", end);
    c.addEventListener("pointercancel", end);
  }

  _popover(qualname) {
    if (this.pop) this.pop.remove();
    this.pop = null;
    const node = qualname && this.nodeByQ.get(qualname);
    if (!node) return;
    const callers = this.data.edges.filter((e) => e.target === qualname).length;
    const calls = this.data.edges.filter((e) => e.source === qualname).length;
    const pop = document.createElement("div");
    pop.className = "popover";
    pop.innerHTML = '<div class="q"></div><div class="where"></div><div class="row"></div>';
    pop.querySelector(".q").textContent = node.qualname;
    pop.querySelector(".where").textContent = `${node.path}:${node.line} · ${node.kind} · ${callers} caller${callers === 1 ? "" : "s"} · calls ${calls}`;
    const chips = pop.querySelector(".row");
    const chip = (cls, text) => { const s = document.createElement("span"); s.className = `chip ${cls}`; s.textContent = text; chips.append(s); };
    if (node.test) chip("grey", "test");
    else chip(node.tested ? "mint" : "red", node.tested ? "reached by tests" : "no test reaches this");
    for (const action of (this.opts.actions ? this.opts.actions(node) : [])) {
      const b = document.createElement("button");
      b.className = "btn sm";
      b.textContent = action.label;
      b.onclick = () => { this._popover(null); action.run(); };
      chips.append(b);
    }
    const row = this.rows.get(qualname);
    const r = this.canvas.getBoundingClientRect();
    const x = this.view.x + (row.card.x + CARD_W) * this.view.k + 8;
    const y = this.view.y + row.y * this.view.k - 20;
    pop.style.left = `${Math.min(Math.max(8, x), r.width - 330)}px`;
    pop.style.top = `${Math.min(Math.max(8, y), r.height - 120)}px`;
    this.canvas.append(pop);
    this.pop = pop;
  }
}

window.GhostGraph = { GraphView, graphFromApi, marksFromEvents, emptyMarks };
})();
