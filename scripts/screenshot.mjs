// Screenshot the GhostPatch dashboard (used for the README images).
//
//   node scripts/screenshot.mjs [url] [out.png] [width] [height] [waitMs]
//   node scripts/screenshot.mjs http://localhost:8765 docs/dashboard.png 1440 1000 5000
//
// Drives headless Chrome or Edge over the DevTools protocol, so it works with the dashboard's
// live event stream (plain `--screenshot` never finishes loading such pages). It also prints
// any JavaScript errors and a few facts about what the page rendered. Needs Node.js 22+.
import { spawn } from "node:child_process";
import { existsSync, mkdtempSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const [url = "http://localhost:8765/", out = "dashboard.png", width = "1440", height = "1000", waitMs = "4000"] =
  process.argv.slice(2);
const browsers = [
  process.env.CHROME,
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
  "C:\\Program Files (x86)\\Microsoft\\Edge\\Application\\msedge.exe",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
  "/usr/bin/google-chrome", "/usr/bin/chromium", "/usr/bin/chromium-browser",
].filter(Boolean);
const browser = browsers.find((b) => existsSync(b));
if (!browser) throw new Error("No Chrome/Edge found. Set CHROME=/path/to/chrome.");

const port = 9333;
const profile = mkdtempSync(join(tmpdir(), "ghostpatch-shot-"));
const proc = spawn(browser, ["--headless=new", "--disable-gpu", "--hide-scrollbars",
  `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, "about:blank"], { stdio: "ignore" });
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let page;
for (let i = 0; i < 50 && !page; i++) {
  try { page = (await (await fetch(`http://127.0.0.1:${port}/json/list`)).json()).find((t) => t.type === "page"); } catch {}
  if (!page) await sleep(200);
}
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve) => (ws.onopen = resolve));
let nextId = 1;
const pending = new Map();
const logs = [];
ws.onmessage = (message) => {
  const msg = JSON.parse(message.data);
  if (msg.id && pending.has(msg.id)) { pending.get(msg.id)(msg); pending.delete(msg.id); }
  if (msg.method === "Runtime.exceptionThrown") {
    logs.push("EXCEPTION: " + (msg.params.exceptionDetails.exception?.description || msg.params.exceptionDetails.text));
  }
  if (msg.method === "Runtime.consoleAPICalled" && msg.params.type === "error") {
    logs.push("console.error: " + msg.params.args.map((a) => a.value ?? a.description).join(" "));
  }
};
const send = (method, params = {}) => new Promise((resolve) => {
  const id = nextId++;
  pending.set(id, resolve);
  ws.send(JSON.stringify({ id, method, params }));
});

await send("Runtime.enable");
await send("Page.enable");
await send("Emulation.setDeviceMetricsOverride", { width: +width, height: +height, deviceScaleFactor: 1, mobile: +width < 600 });
await send("Page.navigate", { url });
await sleep(+waitMs);
const facts = await send("Runtime.evaluate", { returnByValue: true, expression: `JSON.stringify({
  status: document.getElementById('status')?.textContent,
  steps: document.querySelectorAll('.step-divider').length,
  graphNodes: document.querySelectorAll(".srow[data-q]").length,
  edited: document.querySelectorAll(".srow.edited").length,
  diffs: document.querySelectorAll(".difffile").length,
  runs: document.querySelectorAll(".trow").length,
  horizontalScroll: document.documentElement.scrollWidth > document.documentElement.clientWidth,
})` });
console.log(facts.result.result.value);
for (const line of logs) console.log(line);
const shot = await send("Page.captureScreenshot", { format: "png" });
writeFileSync(out, Buffer.from(shot.result.data, "base64"));
console.log(`saved ${out}`);
ws.close();
proc.kill();
process.exit(0);
