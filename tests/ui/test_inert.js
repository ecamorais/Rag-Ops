"use strict";
// Tests for the inert change. Usage: [OPS_INDEX_HTML=path] node test_inert.js [PATH_TO_APP_JS]   (defaults: static/app.js, static/index.html)
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { FakeDocument, makeWindow, makeFetch, response } = require("./fakedom.js");

const appPath = path.resolve(process.argv[2] || path.join(__dirname, "..", "..", "static", "app.js"));
const HTML = process.env.OPS_INDEX_HTML || path.join(__dirname, "..", "..", "static", "index.html");
const app = require(appPath);
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
console.log("Testing: " + appPath + "\nwith page: " + HTML);

const HEALTH = { status: "ok", indexed_tickets: 800, model: "m", key_configured: true };
const FILTERS = { category: ["A"], priority: ["P1-Critical"], status: ["Open"], team: ["T"], created_min: "2025-10-26", created_max: "2026-10-24" };
async function boot(o = {}) {
  const doc = new FakeDocument(HTML);
  const win = makeWindow(o);
  const a = app.init(doc, win, { fetch: makeFetch({ "/health": () => response(200, HEALTH), "/filters": () => response(200, FILTERS) }) });
  await a.ready;
  return { doc, win, a, els: a.els };
}
const inertAncestor = (el) => { for (let p = el; p; p = p.parentNode) if (p.attrs && p.attrs.has("inert")) return p; return null; };

test("the page has ids for the header and the main area, and the sidebar is inside neither", async () => {
  const { doc, els } = await boot();
  assert(els.appHeader && els.mainArea);
  assert.strictEqual(els.appHeader.tagName, "HEADER");
  assert.strictEqual(els.mainArea.tagName, "MAIN");
  const inside = (outer, inner) => { for (let p = inner; p; p = p.parentNode) if (p === outer) return true; return false; };
  for (const el of [els.sidebar, els.sidebarBackdrop, els.sidebarClose]) {
    assert(!inside(els.appHeader, el) && !inside(els.mainArea, el));
  }
  assert(inside(els.appHeader, els.sidebarToggle) && inside(els.appHeader, els.themeToggle));
  assert(inside(els.mainArea, doc.getElementById("thread")) && inside(els.mainArea, doc.getElementById("question")));
});
test("closed at start: nothing is inert", async () => {
  const { els } = await boot();
  assert.deepStrictEqual([els.appHeader.hasAttribute("inert"), els.mainArea.hasAttribute("inert")], [false, false]);
});
test("opening the panel makes the header and the main area inert, and the panel itself is not", async () => {
  const { els, doc } = await boot();
  els.sidebarToggle.fire("click");
  assert.deepStrictEqual([els.appHeader.hasAttribute("inert"), els.mainArea.hasAttribute("inert")], [true, true]);
  assert.strictEqual(inertAncestor(els.sidebar), null);
  assert.strictEqual(inertAncestor(els.sidebarClose), null);
  assert.strictEqual(inertAncestor(els.sidebarBackdrop), null);
  assert.strictEqual(doc.activeElement, els.sidebarClose, "focus went into the panel");
  assert.deepStrictEqual(doc.focusBlocked, []);
});
test("while the panel is open, focus cannot go to the header, the question box or the thread (the fake blocks it like a browser)", async () => {
  const { els, doc } = await boot();
  els.sidebarToggle.fire("click");
  for (const id of ["sidebar-toggle", "theme-toggle", "question", "thread", "send"]) doc.getElementById(id).focus();
  assert.deepStrictEqual(doc.focusBlocked, ["sidebar-toggle", "theme-toggle", "question", "thread", "send"]);
  assert.strictEqual(doc.activeElement, els.sidebarClose, "focus stayed in the panel");
  els.category.focus();
  assert.strictEqual(doc.activeElement, els.category, "controls inside the panel still take focus");
});
test("closing with Escape, the Close button, the backdrop or the toggle removes inert BEFORE focus goes back to the toggle", async () => {
  for (const how of ["escape", "close", "backdrop", "toggle"]) {
    const { els, doc } = await boot();
    els.sidebarToggle.fire("click");
    if (how === "escape") doc.fire("keydown", { key: "Escape" });
    if (how === "close") els.sidebarClose.fire("click");
    if (how === "backdrop") els.sidebarBackdrop.fire("click");
    if (how === "toggle") { doc.activeElement = els.sidebarToggle; els.sidebarToggle.fire("click"); }
    assert.deepStrictEqual([els.appHeader.hasAttribute("inert"), els.mainArea.hasAttribute("inert")], [false, false], how);
    assert.strictEqual(doc.activeElement, els.sidebarToggle, how + ": focus is back on the toggle");
    assert.deepStrictEqual(doc.focusBlocked, [], how + ": no focus attempt was blocked");
  }
});
test("going back to a wide screen while the panel is open removes inert and does not move focus", async () => {
  const { els, doc, win } = await boot();
  els.sidebarToggle.fire("click");
  const log = doc.focusLog.length;
  win.setMedia("(max-width: 800px)", false);
  assert.deepStrictEqual([els.appHeader.hasAttribute("inert"), els.mainArea.hasAttribute("inert")], [false, false]);
  assert.strictEqual(doc.focusLog.length, log);
});
test("opening and closing again repeatedly never leaves inert behind", async () => {
  const { els, doc } = await boot();
  for (let i = 0; i < 5; i++) {
    els.sidebarToggle.fire("click");
    assert.strictEqual(els.mainArea.hasAttribute("inert"), true);
    doc.fire("keydown", { key: "Escape" });
    assert.strictEqual(els.mainArea.hasAttribute("inert"), false);
    assert.strictEqual(els.appHeader.hasAttribute("inert"), false);
  }
});
test("a page missing the new ids fails with a message naming them", async () => {
  const doc = new FakeDocument(HTML);
  doc.byId.delete("app-header");
  doc.byId.delete("main-area");
  assert.throws(() => app.init(doc, makeWindow(), { fetch: makeFetch({}) }), /missing elements: (app-header, main-area|main-area, app-header)/);
});
test("the source sets inert only through setAttribute/removeAttribute (no style, no innerHTML)", () => {
  const src = fs.readFileSync(appPath, "utf8");
  assert(/setAttribute\("inert", ""\)/.test(src) && /removeAttribute\("inert"\)/.test(src));
  assert(!/\.inert\s*=/.test(src) && !/\.style\b/.test(src) && !/innerHTML/.test(src));
});

(async () => {
  let passed = 0, failed = 0;
  for (const [name, fn] of tests) {
    try { await fn(); passed++; console.log("  PASS  " + name); }
    catch (err) { failed++; console.log("  FAIL  " + name + "\n        " + String(err && err.stack ? err.stack.split("\n").slice(0, 4).join("\n        ") : err)); }
  }
  console.log("\n" + (failed === 0 ? "ALL PASSED" : "FAILURES") + ": " + passed + " passed, " + failed + " failed");
  process.exit(failed ? 1 : 0);
})();
