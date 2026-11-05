"use strict";
// Tests for Group B of app.js. Usage: [OPS_INDEX_HTML=path] node test_dom.js [PATH_TO_APP_JS]   (defaults: static/app.js, static/index.html)
// No browser, no server, no network, no Gemini. Uses a strict fake DOM (fakedom.js) that throws on innerHTML etc.
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { FakeElement, FakeDocument, makeWindow, makeFetch, response, hang } = require("./fakedom.js");

const appPath = path.resolve(process.argv[2] || path.join(__dirname, "..", "..", "static", "app.js"));
const source = fs.readFileSync(appPath, "utf8");
const app = require(appPath);
const HTML = process.env.OPS_INDEX_HTML || path.join(__dirname, "..", "..", "static", "index.html");
const DATA = path.join(__dirname, "..", "..", "data", "tickets.json");

const tickets = JSON.parse(fs.readFileSync(DATA, "utf8"));
const uniq = (f) => [...new Set(tickets.map((t) => t[f]))].sort();
const days = tickets.map((t) => t.created_at.slice(0, 10)).sort();
const HEALTH_OK = { status: "ok", indexed_tickets: 800, model: "gemini-3.6-flash", key_configured: true };
const FILTERS_OK = { category: uniq("category"), priority: uniq("priority"), status: uniq("status"), team: uniq("team"), created_min: days[0], created_max: days[days.length - 1] };

// ---- tiny runner
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
const unhandled = [];
process.on("unhandledRejection", (e) => unhandled.push(String(e && e.message ? e.message : e)));

// ---- timers: remember which ones are still alive
const realSet = globalThis.setTimeout;
const realClear = globalThis.clearTimeout;
const liveTimers = new Set();
globalThis.setTimeout = (fn, ms, ...args) => {
  const h = realSet(() => { liveTimers.delete(h); fn(...args); }, ms);
  liveTimers.add(h);
  return h;
};
globalThis.clearTimeout = (h) => { liveTimers.delete(h); realClear(h); };
const sleep = (ms) => new Promise((res) => realSet(res, ms));

function boot(o = {}) {
  const doc = new FakeDocument(HTML);
  const win = makeWindow(o);
  if (o.early !== false) app.applyEarlyTheme(doc, win);
  const log = [];
  const routes = { "/health": o.health || (() => response(200, HEALTH_OK)), "/filters": o.filters || (() => response(200, FILTERS_OK)) };
  const a = app.init(doc, win, { fetch: makeFetch(routes, log), healthTimeoutMs: o.healthTimeoutMs, filtersTimeoutMs: o.filtersTimeoutMs });
  return { doc, win, a, log, routes, els: a.els };
}
const optionTexts = (select) => select.children.map((c) => c.textContent);
const optionValues = (select) => select.children.map((c) => c.attrs.get("value"));

console.log("Testing: " + appPath);

// =========================================================================================== the fake DOM itself
test("the fake DOM really throws on innerHTML, outerHTML, insertAdjacentHTML, style, on*/href/src (so a pass means something)", () => {
  const doc = new FakeDocument();
  const el = doc.createElement("div");
  for (const name of ["innerHTML", "outerHTML", "style", "innerText", "href", "src", "onclick"]) {
    assert.throws(() => { el[name] = "x"; }, /FORBIDDEN/, "set " + name);
    assert.throws(() => el[name], /FORBIDDEN/, "get " + name);
  }
  assert.throws(() => el.insertAdjacentHTML("beforeend", "<b>"), /FORBIDDEN/);
  for (const name of ["onclick", "onerror", "style", "href", "src", "srcdoc"]) assert.throws(() => el.setAttribute(name, "x"), /FORBIDDEN/, name);
  el.setAttribute("aria-label", "fine");
  assert.strictEqual(el.getAttribute("aria-label"), "fine");
});
test("the fake document knows every id the script needs, with the tags and starting attributes from index.html", () => {
  const doc = new FakeDocument(HTML);
  const { missing } = app.collectElements(doc);
  assert.deepStrictEqual(missing, []);
  assert.strictEqual(doc.getElementById("filters-notice").hidden, true);
  assert.strictEqual(doc.getElementById("k-input").value, "6");
  assert.strictEqual(doc.getElementById("filter-category").children.length, 1);
});

// =========================================================================================== source checks
test("source: no .style use, no innerText, no style attribute, no external URL", () => {
  assert(!/\.style\b/.test(source), ".style used");
  assert(!/\binnerText\b/.test(source));
  assert(!/setAttribute\(\s*["']style/.test(source));
  assert(!/https?:\/\//.test(source));
});
test("source: every id in ELEMENT_IDS exists in index.html, and nothing else is looked up by id", () => {
  const html = fs.readFileSync(HTML, "utf8");
  for (const id of Object.values(app.ELEMENT_IDS)) assert(html.includes(`id="${id}"`), id);
  const lookups = [...source.matchAll(/getElementById\(([^)]*)\)/g)].map((m) => m[1]);
  assert.deepStrictEqual(lookups, ["ELEMENT_IDS[name]"]);
});

// =========================================================================================== pure helpers
test("isRealDate", () => {
  for (const ok of ["2026-02-28", "2024-02-29", "2026-12-31", "0001-01-01"]) assert.strictEqual(app.isRealDate(ok), true, ok);
  for (const bad of ["2026-02-30", "2025-02-29", "2026-13-01", "2026-00-10", "26-01-01", "2026-1-1", "2026/01/01", "", " 2026-01-01", "2026-01-01T00:00", null, undefined, 20260101, {}]) assert.strictEqual(app.isRealDate(bad), false, String(bad));
});
test("dateRangeProblem: empty ends, equal dates and a correct range are fine; inverted or unreal ones are not", () => {
  for (const [f, t] of [["", ""], ["2026-01-01", ""], ["", "2026-01-01"], ["2026-01-01", "2026-01-01"], ["2026-01-01", "2026-12-31"], [undefined, undefined]]) assert.strictEqual(app.dateRangeProblem(f, t), null, `${f}..${t}`);
  assert.strictEqual(app.dateRangeProblem("2026-05-02", "2026-05-01"), "The start date is after the end date.");
  assert.strictEqual(app.dateRangeProblem("2026-02-30", ""), "Please use real dates.");
  assert.strictEqual(app.dateRangeProblem("", "nope"), "Please use real dates.");
});
test("dateInputLimits: the data range, tightened by the other end", () => {
  assert.deepStrictEqual(app.dateInputLimits("2025-10-26", "2026-10-24", "", ""), { fromMin: "2025-10-26", fromMax: "2026-10-24", toMin: "2025-10-26", toMax: "2026-10-24" });
  assert.deepStrictEqual(app.dateInputLimits("2025-10-26", "2026-10-24", "2026-03-01", ""), { fromMin: "2025-10-26", fromMax: "2026-10-24", toMin: "2026-03-01", toMax: "2026-10-24" });
  assert.deepStrictEqual(app.dateInputLimits("2025-10-26", "2026-10-24", "", "2026-04-30"), { fromMin: "2025-10-26", fromMax: "2026-04-30", toMin: "2025-10-26", toMax: "2026-10-24" });
  assert.deepStrictEqual(app.dateInputLimits("", "", "", ""), { fromMin: "", fromMax: "", toMin: "", toMax: "" });
  assert.deepStrictEqual(app.dateInputLimits("junk", null, "x", "y"), { fromMin: "", fromMax: "", toMin: "", toMax: "" });
});
test("normaliseFilterOptions: the real /filters body passes through", () => {
  const o = app.normaliseFilterOptions(FILTERS_OK);
  assert.deepStrictEqual([o.category.length, o.priority.length, o.status.length, o.team.length], [8, 4, 4, 7]);
  assert.strictEqual(o.createdMin, days[0]);
  assert.strictEqual(o.createdMax, days[days.length - 1]);
});
test("normaliseFilterOptions: junk is cleaned, never trusted", () => {
  assert.strictEqual(app.normaliseFilterOptions(null), null);
  assert.strictEqual(app.normaliseFilterOptions("x"), null);
  assert.strictEqual(app.normaliseFilterOptions([1]), null);
  const o = app.normaliseFilterOptions({
    category: ["A", " A ", "B", "", "   ", 5, null, { x: 1 }, ["z"], "x".repeat(101), "<img src=x onerror=alert(1)>"],
    priority: "P1", status: undefined, team: Array.from({ length: 500 }, (_, i) => "T" + i),
    created_min: "2026-02-30", created_max: "2026-03-01", __proto__: { polluted: true },
  });
  assert.deepStrictEqual(o.category, ["A", "B", "<img src=x onerror=alert(1)>"]);
  assert.deepStrictEqual(o.priority, []);
  assert.deepStrictEqual(o.status, []);
  assert.strictEqual(o.team.length, 200);
  assert.strictEqual(o.createdMin, "");
  assert.strictEqual(o.createdMax, "2026-03-01");
  assert.strictEqual(Object.prototype.polluted, undefined);
  assert.deepStrictEqual(Object.keys(o).sort(), ["category", "createdMax", "createdMin", "priority", "status", "team"]);
});
test("normaliseHealth and describeHealth", () => {
  assert.deepStrictEqual(app.normaliseHealth(HEALTH_OK), { status: "ok", indexed: 800 });
  for (const bad of [null, "x", {}, { status: "weird", indexed_tickets: 1 }, { status: "ok", indexed_tickets: -1 }, { status: "ok", indexed_tickets: 1.5 }, { status: "ok", indexed_tickets: "800" }, []]) assert.strictEqual(app.normaliseHealth(bad), null, JSON.stringify(bad));
  assert.deepStrictEqual(app.describeHealth({ status: "ok", indexed: 800 }), { text: "800 tickets indexed", degraded: false });
  assert.deepStrictEqual(app.describeHealth({ status: "ok", indexed: 1 }), { text: "1 ticket indexed", degraded: false });
  assert.deepStrictEqual(app.describeHealth({ status: "ok", indexed: 12345 }), { text: "12,345 tickets indexed", degraded: false });
  assert.deepStrictEqual(app.describeHealth({ status: "degraded", indexed: 0 }), { text: "Degraded: no tickets are indexed", degraded: true });
  assert.deepStrictEqual(app.describeHealth({ status: "degraded", indexed: 800 }), { text: "Degraded: the server reports a problem", degraded: true });
  assert.deepStrictEqual(app.describeHealth({ status: "ok", indexed: 0 }), { text: "Degraded: no tickets are indexed", degraded: true });
  assert.deepStrictEqual(app.describeHealth(null), { text: "Status unavailable", degraded: true });
  assert.strictEqual(app.formatCount(0), "0");
  assert.strictEqual(app.formatCount(1000000), "1,000,000");
});
test("activeFilterList: fixed order, trimmed, empty ones left out, text kept as plain text", () => {
  const sel = { ...app.emptySelection(), date_to: "2026-04-30", team: " Platform Ops ", category: "<b>x</b>", priority: "" };
  assert.deepStrictEqual(app.activeFilterList(sel).map((i) => i.text), ["Category: <b>x</b>", "Team: Platform Ops", "Created to: 2026-04-30"]);
  assert.deepStrictEqual(app.activeFilterList(app.emptySelection()), []);
  assert.deepStrictEqual(app.activeFilterList(null), []);
  assert.deepStrictEqual(Object.keys(app.emptySelection()), ["category", "priority", "status", "team", "date_from", "date_to"]);
});

// =========================================================================================== requestJson
test("requestJson: a GET sends the right request and returns status, body and the Retry-After header", async () => {
  const log = [];
  const f = makeFetch({ "/x": () => response(200, { a: 1 }) }, log);
  const res = await app.requestJson(f, "/x", { timeoutMs: 1000 });
  assert.deepStrictEqual(res, { kind: "http", status: 200, body: { a: 1 }, retryAfter: null });
  assert.strictEqual(log.length, 1);
  assert.strictEqual(log[0].method, "GET");
  assert.deepStrictEqual(log[0].headers, { Accept: "application/json" });
  assert.strictEqual(log[0].body, undefined);
  assert.strictEqual(log[0].hasSignal, true);
});
test("requestJson: a POST sends a JSON body with the content type", async () => {
  const log = [];
  await app.requestJson(makeFetch({ "/p": () => response(200, {}) }, log), "/p", { method: "POST", body: { question: "q", k: 6 } });
  assert.strictEqual(log[0].method, "POST");
  assert.strictEqual(log[0].body, '{"question":"q","k":6}');
  assert.strictEqual(log[0].headers["Content-Type"], "application/json");
});
test("requestJson: error responses keep status, body and Retry-After; non-JSON error bodies give body null", async () => {
  const f = makeFetch({
    "/429": () => response(429, { error: "used up", code: "rate_limited" }, { "Retry-After": "41375" }),
    "/422": () => response(422, { detail: [] }),
    "/502": () => response(502, "<html>Bad gateway</html>"),
    "/500": () => response(500, ""),
  });
  assert.deepStrictEqual(await app.requestJson(f, "/429"), { kind: "http", status: 429, body: { error: "used up", code: "rate_limited" }, retryAfter: "41375" });
  assert.strictEqual((await app.requestJson(f, "/422")).status, 422);
  assert.deepStrictEqual(await app.requestJson(f, "/502"), { kind: "http", status: 502, body: null, retryAfter: null });
  assert.deepStrictEqual(await app.requestJson(f, "/500"), { kind: "http", status: 500, body: null, retryAfter: null });
});
test("requestJson: a 2xx that is not JSON is 'invalid'; a body that cannot be read is 'invalid'", async () => {
  assert.deepStrictEqual(await app.requestJson(makeFetch({ "/x": () => response(200, "not json") }), "/x"), { kind: "invalid", status: 200 });
  assert.deepStrictEqual(await app.requestJson(makeFetch({ "/x": () => response(200, "null") }), "/x"), { kind: "invalid", status: 200 });
  const broken = { ok: true, status: 200, headers: { get: () => null }, text: () => Promise.reject(new Error("socket closed")) };
  assert.deepStrictEqual(await app.requestJson(makeFetch({ "/x": () => broken }), "/x"), { kind: "invalid", status: 200 });
});
test("requestJson: a failed connection is 'network'", async () => {
  const f = async () => { throw new TypeError("Failed to fetch"); };
  assert.deepStrictEqual(await app.requestJson(f, "/x", { timeoutMs: 1000 }), { kind: "network" });
});
test("requestJson: a server that never answers ends as 'timeout' after the limit, and the timer is gone", async () => {
  const t0 = Date.now();
  const res = await app.requestJson(makeFetch({ "/x": hang }), "/x", { timeoutMs: 40 });
  assert.deepStrictEqual(res, { kind: "timeout" });
  assert(Date.now() - t0 < 1000, "took " + (Date.now() - t0));
  assert.strictEqual(liveTimers.size, 0);
});
test("requestJson: an outside abort ends as 'abort' (not 'timeout'), also when already aborted before the call", async () => {
  const outside = new AbortController();
  const pending = app.requestJson(makeFetch({ "/x": hang }), "/x", { timeoutMs: 5000, signal: outside.signal });
  await sleep(10);
  outside.abort();
  assert.deepStrictEqual(await pending, { kind: "abort" });
  assert.strictEqual(liveTimers.size, 0);
  let called = false;
  const pre = new AbortController();
  pre.abort();
  assert.deepStrictEqual(await app.requestJson(async () => { called = true; }, "/x", { signal: pre.signal }), { kind: "abort" });
  assert.strictEqual(called, false);
});
test("requestJson: timers and abort listeners are cleaned up after a normal answer", async () => {
  let listeners = 0;
  const signal = { aborted: false, addEventListener: () => { listeners++; }, removeEventListener: () => { listeners--; } };
  await app.requestJson(makeFetch({ "/x": () => response(200, { ok: 1 }) }), "/x", { timeoutMs: 5000, signal });
  assert.strictEqual(liveTimers.size, 0);
  assert.strictEqual(listeners, 0);
});

// =========================================================================================== rendering helpers
test("renderSelectOptions: 'Any' first, one option per value, values and texts set safely, hostile text stays text", () => {
  const doc = new FakeDocument(HTML);
  const select = doc.getElementById("filter-category");
  const hostile = ["<img src=x onerror=alert(1)>", "</option><script>alert(1)</script>", "Plain", "a\nb"];
  const effective = app.renderSelectOptions(doc, select, hostile, "Plain");
  assert.strictEqual(effective, "Plain");
  assert.deepStrictEqual(optionTexts(select), ["Any", ...hostile]);
  assert.deepStrictEqual(optionValues(select), ["", ...hostile]);
  assert.deepStrictEqual([...new Set(doc.created)], ["option"]);
  assert.strictEqual(select.children.every((c) => c.children.length === 0), true);
});
test("renderSelectOptions: re-rendering replaces the options; a vanished selection falls back to Any", () => {
  const doc = new FakeDocument(HTML);
  const select = doc.getElementById("filter-team");
  app.renderSelectOptions(doc, select, ["A", "B", "C"], "");
  assert.strictEqual(select.children.length, 4);
  assert.strictEqual(app.renderSelectOptions(doc, select, ["A", "B"], "C"), "");
  assert.deepStrictEqual(optionTexts(select), ["Any", "A", "B"]);
  assert.strictEqual(app.renderSelectOptions(doc, select, [], "A"), "");
  assert.deepStrictEqual(optionTexts(select), ["Any"]);
});
test("renderActiveFilters: one chip per item, remove button with a clear label, hostile text stays text, list hidden when empty", () => {
  const doc = new FakeDocument(HTML);
  const list = doc.getElementById("active-filters");
  const removed = [];
  const items = [{ key: "category", text: "Category: <img src=x onerror=alert(1)>" }, { key: "date_to", text: "Created to: 2026-04-30" }];
  app.renderActiveFilters(doc, list, items, (k) => removed.push(k));
  assert.strictEqual(list.hidden, false);
  assert.strictEqual(list.children.length, 2);
  assert.strictEqual(list.children[0].children[0].textContent, items[0].text);
  const button = list.children[0].children[1];
  assert.strictEqual(button.getAttribute("aria-label"), "Remove filter " + items[0].text);
  assert.strictEqual(button.getAttribute("type"), "button");
  assert.strictEqual(button.classList.contains("filter-chip-remove"), true);
  button.fire("click");
  list.children[1].children[1].fire("click");
  assert.deepStrictEqual(removed, ["category", "date_to"]);
  assert.deepStrictEqual([...new Set(doc.created)].sort(), ["button", "li", "span"]);
  app.renderActiveFilters(doc, list, [], () => {});
  assert.strictEqual(list.hidden, true);
  assert.strictEqual(list.children.length, 0);
});
test("renderHealth, renderFiltersNotice, renderDateProblem, renderDateLimits, renderThemeButton, renderSidebar", () => {
  const doc = new FakeDocument(HTML);
  const { els } = app.collectElements(doc);
  app.renderHealth(els, { text: "<b>800 tickets indexed</b>", degraded: false });
  assert.strictEqual(els.indexStatus.textContent, "<b>800 tickets indexed</b>");
  assert.strictEqual(els.indexStatus.classList.contains("is-degraded"), false);
  app.renderHealth(els, { text: "Status unavailable", degraded: true });
  assert.strictEqual(els.indexStatus.classList.contains("is-degraded"), true);
  app.renderFiltersNotice(els, "Filters are unavailable");
  assert.deepStrictEqual([els.filtersNotice.hidden, els.filtersReload.hidden, els.filtersNotice.textContent], [false, false, "Filters are unavailable"]);
  app.renderFiltersNotice(els, "");
  assert.deepStrictEqual([els.filtersNotice.hidden, els.filtersReload.hidden], [true, true]);
  app.renderDateProblem(els, "The start date is after the end date.");
  assert.deepStrictEqual([els.dateError.hidden, els.date_from.getAttribute("aria-invalid"), els.date_to.getAttribute("aria-invalid")], [false, "true", "true"]);
  app.renderDateProblem(els, null);
  assert.deepStrictEqual([els.dateError.hidden, els.date_from.hasAttribute("aria-invalid")], [true, false]);
  app.renderDateLimits(els, { fromMin: "2025-10-26", fromMax: "2026-04-30", toMin: "", toMax: "2026-10-24" });
  assert.deepStrictEqual([els.date_from.getAttribute("min"), els.date_from.getAttribute("max"), els.date_to.hasAttribute("min"), els.date_to.getAttribute("max")], ["2025-10-26", "2026-04-30", false, "2026-10-24"]);
  app.renderThemeButton(els.themeToggle, "dark");
  assert.deepStrictEqual([els.themeToggle.textContent, els.themeToggle.hasAttribute("aria-pressed")], ["Light mode", false]);
  app.renderSidebar(els, true);
  assert.deepStrictEqual([els.sidebar.classList.contains("is-open"), els.sidebarBackdrop.hidden, els.sidebarToggle.getAttribute("aria-expanded")], [true, false, "true"]);
  app.renderSidebar(els, false);
  assert.deepStrictEqual([els.sidebar.classList.contains("is-open"), els.sidebarBackdrop.hidden, els.sidebarToggle.getAttribute("aria-expanded")], [false, true, "false"]);
});

// =========================================================================================== init: the whole page logic
test("init: the happy path fills the header, the four dropdowns and the date limits from the real data", async () => {
  const { a, els, log, doc } = boot();
  await a.ready;
  assert.strictEqual(els.indexStatus.textContent, "800 tickets indexed");
  assert.strictEqual(els.indexStatus.classList.contains("is-degraded"), false);
  assert.deepStrictEqual([els.category, els.priority, els.status, els.team].map((s) => s.children.length - 1), [8, 4, 4, 7]);
  assert.deepStrictEqual(optionTexts(els.priority), ["Any", "P1-Critical", "P2-High", "P3-Medium", "P4-Low"]);
  assert.deepStrictEqual([els.date_from.getAttribute("min"), els.date_from.getAttribute("max"), els.date_to.getAttribute("min"), els.date_to.getAttribute("max")], [days[0], days[days.length - 1], days[0], days[days.length - 1]]);
  assert.deepStrictEqual([els.filtersNotice.hidden, els.filtersReload.hidden, els.activeFilters.hidden], [true, true, true]);
  assert.deepStrictEqual(log.map((l) => [l.method, l.url, l.hasSignal]).sort(), [["GET", "/filters", true], ["GET", "/health", true]]);
  assert.strictEqual(a.state.filtersStatus, "ok");
  assert.strictEqual(els.kInput.value, "6");
  assert.strictEqual(unhandled.length, 0);
});
test("init: if /health fails (500, network, garbage), the header says so and everything else still works", async () => {
  for (const health of [() => response(500, { error: "x", code: "internal_error" }), async () => { throw new TypeError("Failed to fetch"); }, () => response(200, { nope: 1 }), () => response(200, "not json")]) {
    const { a, els } = boot({ health });
    await a.ready;
    assert.strictEqual(els.indexStatus.textContent, "Status unavailable");
    assert.strictEqual(els.indexStatus.classList.contains("is-degraded"), true);
    assert.strictEqual(els.category.children.length - 1, 8, "filters still loaded");
  }
});
test("init: a degraded /health (no tickets indexed) shows the notice but filters still work", async () => {
  const { a, els } = boot({ health: () => response(200, { status: "degraded", indexed_tickets: 0, model: "m", key_configured: true }) });
  await a.ready;
  assert.strictEqual(els.indexStatus.textContent, "Degraded: no tickets are indexed");
  assert.strictEqual(els.indexStatus.classList.contains("is-degraded"), true);
});
test("init: if /filters fails, the notice and Reload button appear, dropdowns have only 'Any', dates are unrestricted; Reload fixes it", async () => {
  const { a, els, routes } = boot({ filters: () => response(500, { error: "x", code: "internal_error" }) });
  await a.ready;
  assert.deepStrictEqual([els.filtersNotice.hidden, els.filtersReload.hidden], [false, false]);
  assert.strictEqual(els.filtersNotice.textContent, "Filters are unavailable right now. You can still ask questions.");
  assert.deepStrictEqual([els.category, els.priority, els.status, els.team].map((s) => optionTexts(s)), [["Any"], ["Any"], ["Any"], ["Any"]]);
  assert.deepStrictEqual([els.date_from.hasAttribute("min"), els.date_from.hasAttribute("max"), els.date_to.hasAttribute("min"), els.date_to.hasAttribute("max")], [false, false, false, false]);
  assert.strictEqual(els.indexStatus.textContent, "800 tickets indexed", "the header is independent of /filters");
  assert.strictEqual(a.state.filtersStatus, "failed");
  routes["/filters"] = () => response(200, FILTERS_OK);
  els.filtersReload.fire("click");
  await sleep(20);
  assert.deepStrictEqual([els.filtersNotice.hidden, els.filtersReload.hidden, els.filtersReload.disabled], [true, true, false]);
  assert.strictEqual(els.category.children.length - 1, 8);
  assert.strictEqual(a.state.filtersStatus, "ok");
});
test("init: /filters with no values says so; a hanging /health and /filters time out and the page stays usable", async () => {
  const empty = boot({ filters: () => response(200, { category: [], priority: [], status: [], team: [], created_min: null, created_max: null }) });
  await empty.a.ready;
  assert.strictEqual(empty.els.filtersNotice.textContent, "The server has no filter values to offer.");
  const t0 = Date.now();
  const slow = boot({ health: hang, filters: hang, healthTimeoutMs: 40, filtersTimeoutMs: 40 });
  await slow.a.ready;
  assert(Date.now() - t0 < 2000);
  assert.strictEqual(slow.els.indexStatus.textContent, "Status unavailable");
  assert.strictEqual(slow.els.filtersNotice.hidden, false);
  assert.strictEqual(liveTimers.size, 0);
  slow.els.kInput.value = "8";
  slow.els.kInput.fire("change");
  assert.strictEqual(slow.a.snapshot().k, 8, "the controls still work");
});
test("init: choosing a filter updates the selection, shows a chip and tells subscribers; the chip's button removes it", async () => {
  const { a, els } = boot();
  await a.ready;
  const seen = [];
  a.subscribe((s) => seen.push(s));
  els.priority.value = "P1-Critical";
  els.priority.fire("change");
  assert.deepStrictEqual(a.snapshot(), { filters: { priority: "P1-Critical" }, k: 6, problem: null });
  assert.strictEqual(els.activeFilters.hidden, false);
  assert.deepStrictEqual(els.activeFilters.children.map((c) => c.children[0].textContent), ["Priority: P1-Critical"]);
  assert.deepStrictEqual(seen[seen.length - 1], a.snapshot());
  els.activeFilters.children[0].children[1].fire("click");
  assert.deepStrictEqual(a.snapshot().filters, {});
  assert.strictEqual(els.priority.value, "");
  assert.strictEqual(els.activeFilters.hidden, true);
});
test("init: several filters give chips in a fixed order; Clear filters resets controls, chips and the snapshot", async () => {
  const { a, els } = boot();
  await a.ready;
  els.team.value = "Platform Ops"; els.team.fire("change");
  els.category.value = "Facilities"; els.category.fire("change");
  els.date_from.value = "2026-01-01"; els.date_from.fire("change");
  els.status.value = "Open"; els.status.fire("change");
  els.date_to.value = "2026-03-31"; els.date_to.fire("input");
  assert.deepStrictEqual(els.activeFilters.children.map((c) => c.children[0].textContent),
    ["Category: Facilities", "Status: Open", "Team: Platform Ops", "Created from: 2026-01-01", "Created to: 2026-03-31"]);
  assert.deepStrictEqual(a.snapshot().filters, { category: "Facilities", status: "Open", team: "Platform Ops", date_from: "2026-01-01", date_to: "2026-03-31" });
  els.clearFilters.fire("click");
  assert.deepStrictEqual(a.snapshot(), { filters: {}, k: 6, problem: null });
  assert.deepStrictEqual([els.category.value, els.status.value, els.team.value], ["", "", ""]);
  assert.strictEqual(els.activeFilters.children.length, 0);
});
test("init: the date rules - limits follow the other end, an inverted range shows a message and flags both inputs, fixing it clears that", async () => {
  const { a, els } = boot();
  await a.ready;
  els.date_from.value = "2026-03-01"; els.date_from.fire("change");
  assert.strictEqual(els.date_to.getAttribute("min"), "2026-03-01");
  els.date_to.value = "2026-04-30"; els.date_to.fire("change");
  assert.strictEqual(els.date_from.getAttribute("max"), "2026-04-30");
  assert.strictEqual(a.snapshot().problem, null);
  els.date_to.value = "2026-02-01"; els.date_to.fire("change");
  assert.strictEqual(a.snapshot().problem, "The start date is after the end date.");
  assert.strictEqual(els.dateError.hidden, false);
  assert.strictEqual(els.dateError.textContent, "The start date is after the end date.");
  assert.deepStrictEqual([els.date_from.getAttribute("aria-invalid"), els.date_to.getAttribute("aria-invalid")], ["true", "true"]);
  els.date_to.value = "2026-03-01"; els.date_to.fire("change");
  assert.deepStrictEqual([a.snapshot().problem, els.dateError.hidden, els.date_from.hasAttribute("aria-invalid")], [null, true, false], "same day is allowed");
  els.clearFilters.fire("click");
  assert.deepStrictEqual([els.date_from.getAttribute("min"), els.date_from.getAttribute("max"), els.date_to.getAttribute("min"), els.date_to.getAttribute("max")], [days[0], days[days.length - 1], days[0], days[days.length - 1]]);
});
test("init: the k control keeps 3..10 and falls back to 6 for junk, writing the corrected value back", async () => {
  const { a, els } = boot();
  await a.ready;
  for (const [typed, want] of [["2", 3], ["11", 10], ["abc", 6], ["", 6], ["7", 7], ["3", 3], ["10", 10], ["6.6", 7], ["-5", 3], ["1e9", 10]]) {
    els.kInput.value = typed;
    els.kInput.fire("change");
    assert.strictEqual(a.snapshot().k, want, `typed ${JSON.stringify(typed)}`);
    assert.strictEqual(els.kInput.value, String(want));
  }
});
test("init: hostile /filters values become inert option text; only option, li, span and button elements are ever created", async () => {
  const hostile = { category: ["<img src=x onerror=alert(1)>"], priority: ["</option><script>alert(1)</script>"], status: ["'\"><svg onload=1>"], team: ["javascript:alert(1)"], created_min: "2026-01-01", created_max: "2026-12-31" };
  const { a, els, doc } = boot({ filters: () => response(200, hostile) });
  await a.ready;
  els.category.value = hostile.category[0]; els.category.fire("change");
  els.team.value = hostile.team[0]; els.team.fire("change");
  assert.strictEqual(els.category.children[1].textContent, hostile.category[0]);
  assert.deepStrictEqual(els.activeFilters.children.map((c) => c.children[0].textContent), ["Category: <img src=x onerror=alert(1)>", "Team: javascript:alert(1)"]);
  assert.deepStrictEqual([...new Set(doc.created)].sort(), ["button", "li", "option", "span"]);
});
test("init: the theme - system default, explicit toggle with saved choice, saved choice at start, hostile storage", async () => {
  // no saved choice, light system: no data-theme set (the stylesheet follows the system), label offers dark
  let t = boot();
  assert.strictEqual(t.doc.documentElement.hasAttribute("data-theme"), false);
  assert.strictEqual(t.els.themeToggle.textContent, "Dark mode");
  assert.strictEqual(t.els.themeToggle.hasAttribute("aria-pressed"), false);
  // dark system: label offers light; the system changing later updates the label while there is no explicit choice
  t = boot({ dark: true });
  assert.strictEqual(t.els.themeToggle.textContent, "Light mode");
  t.win.setMedia("(prefers-color-scheme: dark)", false);
  assert.strictEqual(t.els.themeToggle.textContent, "Dark mode");
  // toggling sets and saves the explicit choice
  t = boot();
  t.els.themeToggle.fire("click");
  assert.deepStrictEqual([t.doc.documentElement.getAttribute("data-theme"), t.win.store.get("ops-theme"), t.els.themeToggle.textContent], ["dark", "dark", "Light mode"]);
  t.els.themeToggle.fire("click");
  assert.deepStrictEqual([t.doc.documentElement.getAttribute("data-theme"), t.win.store.get("ops-theme"), t.els.themeToggle.textContent], ["light", "light", "Dark mode"]);
  // a saved choice is applied at start (before init), and wins over the system
  t = boot({ stored: "dark", dark: false });
  assert.strictEqual(t.doc.documentElement.getAttribute("data-theme"), "dark");
  assert.strictEqual(t.els.themeToggle.textContent, "Light mode");
  t = boot({ stored: "purple" });
  assert.strictEqual(t.doc.documentElement.hasAttribute("data-theme"), false, "a junk saved value is ignored");
  // storage that throws on use, throws on access, or is missing: nothing breaks, the choice still applies for the session
  for (const storage of ["throws-on-use", "throws-on-access", "missing"]) {
    t = boot({ storage });
    t.els.themeToggle.fire("click");
    assert.strictEqual(t.doc.documentElement.getAttribute("data-theme"), "dark", storage);
    await t.a.ready;
  }
  assert.strictEqual(app.applyEarlyTheme(new FakeDocument(), makeWindow({ storage: "throws-on-access" })), null);
});
test("init: the sidebar - closed at start; toggle opens it and focuses Close; Escape, the backdrop and Close shut it and give focus back", async () => {
  const { a, els, doc, win } = boot();
  await a.ready;
  const isOpen = () => [els.sidebar.classList.contains("is-open"), els.sidebarBackdrop.hidden, els.sidebarToggle.getAttribute("aria-expanded")];
  assert.deepStrictEqual(isOpen(), [false, true, "false"]);
  els.sidebarToggle.fire("click");
  assert.deepStrictEqual(isOpen(), [true, false, "true"]);
  assert.strictEqual(doc.activeElement, els.sidebarClose);
  doc.fire("keydown", { key: "Escape" });
  assert.deepStrictEqual(isOpen(), [false, true, "false"]);
  assert.strictEqual(doc.activeElement, els.sidebarToggle);
  els.sidebarToggle.fire("click");
  els.sidebarBackdrop.fire("click");
  assert.deepStrictEqual(isOpen(), [false, true, "false"]);
  els.sidebarToggle.fire("click");
  els.sidebarClose.fire("click");
  assert.deepStrictEqual(isOpen(), [false, true, "false"]);
  els.sidebarToggle.fire("click");
  els.sidebarToggle.fire("click");
  assert.deepStrictEqual(isOpen(), [false, true, "false"], "the toggle button also closes it");
  const before = doc.focusLog.length;
  doc.fire("keydown", { key: "Escape" });
  doc.fire("keydown", { key: "a" });
  assert.strictEqual(doc.focusLog.length, before, "Escape while closed does nothing");
  els.sidebarToggle.fire("click");
  const focusAtOpen = doc.focusLog.length;
  win.setMedia("(max-width: 800px)", false);
  assert.deepStrictEqual(isOpen(), [false, true, "false"], "going back to a wide screen closes the panel");
  assert.strictEqual(doc.focusLog.length, focusAtOpen, "...without moving focus");
});
test("init: a page that lacks an element fails with a message naming it", () => {
  const doc = new FakeDocument(HTML);
  doc.byId.delete("k-input");
  doc.byId.delete("date-error");
  assert.throws(() => app.init(doc, makeWindow(), { fetch: makeFetch({}) }), /missing elements: date-error, k-input|missing elements: k-input, date-error/);
});
test("init: only /health and /filters are requested at start, nothing else, and nothing is left running", async () => {
  const { a, log } = boot();
  await a.ready;
  assert.deepStrictEqual(log.map((l) => l.url).sort(), ["/filters", "/health"]);
  assert(log.every((l) => l.method === "GET" && l.body === undefined));
  assert.strictEqual(liveTimers.size, 0);
  assert.strictEqual(unhandled.length, 0);
});
test("loading in Node starts nothing (no document, no start-up code ran)", () => {
  assert.strictEqual(typeof document, "undefined");
  assert.strictEqual(typeof app.init, "function");
});

// =========================================================================================== run
(async () => {
  let passed = 0, failed = 0;
  const failures = [];
  for (const [name, fn] of tests) {
    try {
      await Promise.race([Promise.resolve().then(fn), new Promise((_, reject) => realSet(() => reject(new Error("the test did not finish within 5 s (a hang)")), 5000))]);
      passed++;
      console.log("  PASS  " + name);
    } catch (err) {
      failed++;
      failures.push(name);
      console.log("  FAIL  " + name + "\n        " + String(err && err.stack ? err.stack.split("\n").slice(0, 4).join("\n        ") : err));
    }
  }
  console.log("\n" + (failed === 0 ? "ALL PASSED" : "FAILURES") + ": " + passed + " passed, " + failed + " failed");
  if (failed) console.log("failed tests:\n  - " + failures.join("\n  - "));
  process.exit(failed ? 1 : 0);
})();
