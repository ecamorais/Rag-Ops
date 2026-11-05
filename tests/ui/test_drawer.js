"use strict";
// Tests for Group D (ticket drawer). Usage: [OPS_INDEX_HTML=path] node test_drawer.js [PATH_TO_APP_JS]   (defaults: static/app.js, static/index.html)
const fs = require("fs");
const path = require("path");
const assert = require("assert");
const { FakeDocument, makeWindow, makeFetch, response, hang } = require("./fakedom.js");

const appPath = path.resolve(process.argv[2] || path.join(__dirname, "..", "..", "static", "app.js"));
const HTML = process.env.OPS_INDEX_HTML || path.join(__dirname, "..", "..", "static", "index.html");
const app = require(appPath);
const tests = [];
const test = (name, fn) => tests.push([name, fn]);
console.log("Testing: " + appPath + "\nwith page: " + HTML);

// real tickets from the dataset (read only)
const DATA = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "..", "data", "tickets.json"), "utf8").replace(/^﻿/, ""));
const byId = (id) => DATA.find((t) => (t.ticket_id || t.id) === id);
const asApi = (t) => ({ ticket_id: t.ticket_id || t.id, title: t.title, description: t.description, category: t.category, priority: t.priority, status: t.status, team: t.team, reporter: t.reporter, assignee: t.assignee, created_at: t.created_at, resolved_at: t.resolved_at, resolution_notes: t.resolution_notes, comments: t.comments });

const HEALTH = { status: "ok", indexed_tickets: 800, model: "m", key_configured: true };
const FILTERS = { category: ["A"], priority: ["P1-Critical"], status: ["Open"], team: ["T"], created_min: "2025-10-26", created_max: "2026-10-24" };
const settle = () => new Promise((r) => setImmediate(r));
const CHAT = { answer: "See [OPS-1001].", found: true, citations: ["OPS-1001"], unverified_citations: [], missing_references: [], sources: [{ ticket_id: "OPS-1001", title: "T1", category: "C", priority: "P4-Low", status: "Closed", score: 0.5 }] };

async function boot(tickets) {
  const doc = new FakeDocument(HTML);
  const win = makeWindow();
  const log = [];
  const routes = { "/health": () => response(200, HEALTH), "/filters": () => response(200, FILTERS), "/chat": () => response(200, CHAT) };
  const fetch = async (url, init) => {
    if (url.startsWith("/tickets/")) {
      log.push({ url, signal: init.signal });
      const handler = tickets || (() => response(404, { error: "not found", code: "not_found" }));
      return handler(url, init);
    }
    return makeFetch(routes, [])(url, init);
  };
  const a = app.init(doc, win, { fetch, ticketTimeoutMs: 40 });
  await a.ready;
  const els = a.els;
  return {
    doc, a, els, log, drawer: a.drawer,
    text: () => els.drawerBody.textContent,
    byClass: (c) => els.drawerBody.getElementsByClassName(c),
    esc: () => { const e = els.drawer.fire("cancel"); if (!e.defaultPrevented) els.drawer.close(); },
    makeOpener: () => { const b = doc.createElement("button"); b.setAttribute("type", "button"); els.thread.appendChild(b); return b; },
  };
}
const ok = (id) => () => response(200, asApi(byId(id)));
const ALLOWED = new Set(["div", "p", "dl", "dt", "dd", "h3", "span", "button"]);

// ---------- pure helpers ----------
test("formatTimestamp and longText", () => {
  assert.strictEqual(app.formatTimestamp("2025-10-26T06:29:40"), "2025-10-26 06:29");
  assert.strictEqual(app.formatTimestamp("2025-10-26 06:29:40"), "2025-10-26 06:29");
  assert.strictEqual(app.formatTimestamp("yesterday"), "yesterday");
  assert.strictEqual(app.formatTimestamp(null), "");
  assert.strictEqual(app.longText("a\r\nb\u0000c\u0007d\te"), "a\nb c d\te");
  assert.strictEqual(app.longText(5), "");
  assert(app.longText("x".repeat(30000)).length <= 20000);
});
test("validateTicket accepts a real ticket and rejects wrong shapes or a different id", () => {
  const t = app.validateTicket(asApi(byId("OPS-1006")), "OPS-1006");
  assert.strictEqual(t.id, "OPS-1006");
  assert.strictEqual(t.comments.length, 2);
  assert.strictEqual(t.created, "2025-10-29 06:30");
  for (const bad of [null, undefined, "x", 3, [], {}, { ticket_id: "OPS-1", title: 5 }]) assert.strictEqual(app.validateTicket(bad, "OPS-1"), null);
  assert.strictEqual(app.validateTicket(asApi(byId("OPS-1006")), "OPS-1001"), null, "a reply for another ticket is not shown");
});
test("validateTicket: resolved null / blank -> null, resolution null / blank -> null, comments junk skipped and capped", () => {
  const base = asApi(byId("OPS-1001"));
  let t = app.validateTicket({ ...base, resolved_at: null, resolution_notes: null }, "OPS-1001");
  assert.strictEqual(t.resolved, null); assert.strictEqual(t.resolution, null);
  t = app.validateTicket({ ...base, resolved_at: "  ", resolution_notes: "  \n " }, "OPS-1001");
  assert.strictEqual(t.resolved, null); assert.strictEqual(t.resolution, null);
  t = app.validateTicket({ ...base, comments: [null, 4, "x", { author: "a", timestamp: "2025-01-01T01:02:03", text: "t" }] }, "OPS-1001");
  assert.strictEqual(t.comments.length, 1);
  t = app.validateTicket({ ...base, comments: Array.from({ length: 500 }, () => ({ author: "a", timestamp: "", text: "t" })) }, "OPS-1001");
  assert.strictEqual(t.comments.length, 200);
  t = app.validateTicket({ ...base, comments: "no" }, "OPS-1001");
  assert.deepStrictEqual(t.comments, []);
});

// ---------- rendering ----------
test("OPS-1001 (no comments): every field, the dates, resolution notes and 'No comments'", async () => {
  const t = await boot();
  const ticket = app.validateTicket(asApi(byId("OPS-1001")), "OPS-1001");
  const node = app.renderTicketBody(t.doc, ticket);
  const dts = node.getElementsByClassName("field-list")[0].children.filter((c) => c.tagName === "DT").map((c) => c.textContent);
  assert.deepStrictEqual(dts, ["Priority", "Status", "Category", "Team", "Reporter", "Assignee", "Created", "Resolved"]);
  const txt = node.textContent;
  for (const want of ["P4-Low", "Closed", "Vendor Management", "Procurement", "Aditya Naidu", "Rahul Gowda", "2025-10-26 06:29", "2025-11-08 22:54", "Orion Cold Systems applied a new price list", "Invoice INV-308701 from Orion Cold Systems", "Comments (0)", "No comments"]) assert(txt.includes(want), "missing: " + want);
  assert(!txt.includes("Not resolved yet") && !txt.includes("No resolution recorded"));
  assert.strictEqual(node.getElementsByClassName("comment").length, 0);
});
test("OPS-1006 (two comments): author, time and text of each, in order", async () => {
  const t = await boot();
  const node = app.renderTicketBody(t.doc, app.validateTicket(asApi(byId("OPS-1006")), "OPS-1006"));
  const comments = node.getElementsByClassName("comment");
  assert.strictEqual(comments.length, 2);
  assert.strictEqual(comments[0].getElementsByClassName("comment-head")[0].textContent, "Ravi Krishnan · 2025-10-29 12:36");
  assert.strictEqual(comments[0].getElementsByClassName("prose")[0].textContent, "Picked this up and starting the investigation.");
  assert.strictEqual(comments[1].getElementsByClassName("comment-head")[0].textContent, "Ravi Krishnan · 2025-10-30 07:52");
  assert.strictEqual(comments[1].getElementsByClassName("prose")[0].textContent, "Confirmed with the requester that the issue is resolved.");
  assert(node.textContent.includes("Comments (2)") && !node.textContent.includes("No comments"));
});
test("an unresolved ticket without resolution notes: 'Not resolved yet' and 'No resolution recorded'", async () => {
  const t = await boot();
  const open = DATA.find((x) => !x.resolved_at && !x.resolution_notes);
  assert(open, "the dataset has an unresolved ticket");
  const node = app.renderTicketBody(t.doc, app.validateTicket(asApi(open), open.ticket_id || open.id));
  assert(node.textContent.includes("Not resolved yet") && node.textContent.includes("No resolution recorded"));
});
test("priority badge: text plus colour class", async () => {
  const t = await boot();
  for (const [p, cls] of [["P1-Critical", "badge-p1"], ["P3-Medium", "badge-p3"], ["???", "badge-unknown"]]) {
    const node = app.renderTicketBody(t.doc, app.validateTicket({ ...asApi(byId("OPS-1001")), priority: p }, "OPS-1001"));
    const badge = node.getElementsByClassName("badge")[0];
    assert.strictEqual(badge.textContent, p); assert(badge.classList.contains(cls));
  }
});
test("a hostile ticket (title, description, resolution, comments, names) is shown as plain text", async () => {
  const evil = "<img src=x onerror=alert(1)><script>alert(2)</script><a href=\"javascript:alert(3)\">x</a>";
  const hostile = { ticket_id: "OPS-1", title: evil, description: evil + "\nline2", category: evil, priority: evil, status: evil, team: evil, reporter: evil, assignee: evil, created_at: evil, resolved_at: evil, resolution_notes: evil, comments: [{ author: evil, timestamp: evil, text: evil }] };
  const t = await boot(() => response(200, hostile));
  const before = t.doc.created.length;
  t.drawer.open("OPS-1", null);
  await settle(); await settle();
  assert(t.text().includes(evil));
  assert(t.els.drawerTitle.textContent.includes(evil));
  const created = new Set(t.doc.created.slice(before));
  for (const tag of created) assert(ALLOWED.has(tag), "unexpected tag " + tag);
  assert(!created.has("img") && !created.has("script") && !created.has("a"));
  assert.strictEqual(t.byClass("comment").length, 1);
});

// ---------- opening, loading, closing ----------
test("opening: modal shown, scroll lock on, loading state, aria-busy, focus on the title, fetch of the id", async () => {
  let release; const gate = new Promise((r) => { release = r; });
  const t = await boot(async () => { await gate; return response(200, asApi(byId("OPS-1001"))); });
  const opener = t.makeOpener(); opener.focus();
  assert.strictEqual(t.drawer.open("OPS-1001", opener), true);
  assert.strictEqual(t.els.drawer.open, true);
  assert.strictEqual(t.doc.body.classList.contains("drawer-open"), true);
  assert.strictEqual(t.els.drawerTitle.textContent, "OPS-1001");
  assert.strictEqual(t.doc.activeElement, t.els.drawerTitle);
  assert.strictEqual(t.els.drawerBody.getAttribute("aria-busy"), "true");
  assert.strictEqual(t.byClass("drawer-state")[0].getAttribute("role"), "status");
  assert(t.text().includes("Loading ticket"));
  assert.deepStrictEqual(t.log.map((l) => l.url), ["/tickets/OPS-1001"]);
  release(); await settle(); await settle();
  assert.strictEqual(t.els.drawerBody.getAttribute("aria-busy"), "false");
  assert.strictEqual(t.els.drawerTitle.textContent, "OPS-1001: Price mismatch between INV-308701 and PO-180649");
  assert(t.text().includes("Rahul Gowda") && !t.text().includes("Loading ticket"));
  assert.strictEqual(t.doc.activeElement, t.els.drawerTitle, "focus stays on the title");
});
test("the id is re-validated: anything but OPS-<digits> opens nothing and fetches nothing", async () => {
  const t = await boot(ok("OPS-1001"));
  for (const bad of ["OPS-", "OPS-12a", "../health", "OPS-1/../health", "OPS-1?x=1", "PO-5", "OPS-1\nOPS-2", "", null, undefined, 5, {}, "OPS-１２"]) {
    assert.strictEqual(t.drawer.open(bad, null), false, JSON.stringify(bad));
  }
  assert.strictEqual(t.log.length, 0);
  assert.strictEqual(t.els.drawer.open, false);
  assert.strictEqual(t.doc.body.classList.contains("drawer-open"), false);
});
test("a lower-case id with spaces is normalised and accepted", async () => {
  const t = await boot(ok("OPS-1001"));
  assert.strictEqual(t.drawer.open("  ops-1001 ", null), true);
  assert.deepStrictEqual(t.log.map((l) => l.url), ["/tickets/OPS-1001"]);
});
test("404: 'Ticket OPS-9999 was not found', an alert, and no Retry", async () => {
  const t = await boot();
  t.drawer.open("OPS-9999", null); await settle(); await settle();
  assert(t.text().includes("Ticket OPS-9999 was not found"));
  assert.strictEqual(t.byClass("drawer-state")[0].getAttribute("role"), "alert");
  assert.strictEqual(t.byClass("button").length, 0);
  assert.strictEqual(t.els.drawerBody.getAttribute("aria-busy"), "false");
});
test("a server error, a network failure and an unreadable body each give an error state with Retry", async () => {
  for (const handler of [() => response(500, { error: "boom", code: "internal" }), () => { throw new Error("down"); }, () => response(200, "not json"), () => response(200, { ticket_id: "OPS-2", title: "other" })]) {
    const t = await boot(handler);
    t.drawer.open("OPS-1001", null); await settle(); await settle();
    assert.strictEqual(t.byClass("drawer-state")[0].getAttribute("role"), "alert");
    assert(t.byClass("button").some((b) => b.textContent === "Retry"), "has Retry");
    assert.strictEqual(t.els.drawerBody.getAttribute("aria-busy"), "false");
  }
});
test("422 gives an error without Retry", async () => {
  const t = await boot(() => response(422, { detail: [] }));
  t.drawer.open("OPS-1", null); await settle(); await settle();
  assert.strictEqual(t.byClass("button").length, 0);
});
test("a hanging request ends in a timeout error with Retry", async () => {
  const t = await boot(hang);
  t.drawer.open("OPS-1001", null);
  await new Promise((r) => setTimeout(r, 120));
  assert(t.byClass("button").some((b) => b.textContent === "Retry"));
});
test("Retry fetches again, shows the ticket, and keeps focus inside the dialog (on the title)", async () => {
  let n = 0;
  const t = await boot(() => (++n === 1 ? response(503, { error: "busy" }) : response(200, asApi(byId("OPS-1006")))));
  t.drawer.open("OPS-1006", null); await settle(); await settle();
  const retry = t.byClass("button").find((b) => b.textContent === "Retry");
  retry.focus(); // clicking a button focuses it
  assert.strictEqual(t.doc.activeElement, retry);
  retry.fire("click");
  assert(t.text().includes("Loading ticket"));
  assert.strictEqual(t.doc.activeElement, t.els.drawerTitle);
  await settle(); await settle();
  assert.strictEqual(t.log.length, 2);
  assert.strictEqual(t.byClass("comment").length, 2);
});
test("stale guard: a late reply for an earlier ticket never replaces the newer one", async () => {
  const gates = {};
  const t = await boot((url) => new Promise((resolve) => { gates[url.split("/").pop()] = () => resolve(response(200, asApi(byId(url.split("/").pop())))); }));
  t.drawer.open("OPS-1001", null);
  t.drawer.open("OPS-1006", null);
  assert.strictEqual(t.log[0].signal.aborted, true, "the first request was aborted");
  gates["OPS-1006"](); await settle(); await settle();
  gates["OPS-1001"](); await settle(); await settle(); // the late one
  assert(t.els.drawerTitle.textContent.startsWith("OPS-1006"));
  assert(t.text().includes("Ravi Krishnan") && !t.text().includes("Rahul Gowda"));
});
test("stale guard: a reply that arrives after the drawer was closed is dropped", async () => {
  let release; const gate = new Promise((r) => { release = r; });
  const t = await boot(async () => { await gate; return response(200, asApi(byId("OPS-1001"))); });
  t.drawer.open("OPS-1001", null);
  t.esc();
  const before = t.text();
  release(); await settle(); await settle();
  assert.strictEqual(t.text(), before, "the body did not change after closing");
  assert.strictEqual(t.log[0].signal.aborted, true);
});
test("a reply for an earlier open does not leak into a later open", async () => {
  const gates = [];
  const t = await boot((url) => new Promise((resolve) => gates.push(() => resolve(response(200, asApi(byId(url.split("/").pop())))))));
  t.drawer.open("OPS-1001", null); t.esc();
  t.drawer.open("OPS-1006", null);
  gates[0](); await settle(); await settle();
  assert(t.text().includes("Loading ticket"), "still loading the second one");
  gates[1](); await settle(); await settle();
  assert(t.els.drawerTitle.textContent.startsWith("OPS-1006"));
});
test("Esc closes: the cancel event is not blocked, the dialog closes, the lock is released, focus returns to the opener", async () => {
  const t = await boot(ok("OPS-1001"));
  const opener = t.makeOpener(); opener.focus();
  t.drawer.open("OPS-1001", opener); await settle(); await settle();
  const e = t.els.drawer.fire("cancel");
  assert.strictEqual(e.defaultPrevented, false, "we never prevent the browser's own Esc handling");
  t.els.drawer.close();
  assert.strictEqual(t.els.drawer.open, false);
  assert.strictEqual(t.doc.body.classList.contains("drawer-open"), false);
  assert.strictEqual(t.doc.activeElement, opener);
});
test("the Close button closes and returns focus to the opener", async () => {
  const t = await boot(ok("OPS-1001"));
  const opener = t.makeOpener();
  t.drawer.open("OPS-1001", opener); await settle(); await settle();
  t.els.drawerClose.fire("click");
  assert.strictEqual(t.els.drawer.open, false);
  assert.strictEqual(t.doc.activeElement, opener);
  assert.deepStrictEqual(t.doc.modalLog, ["open", "close"]);
});
test("when the opener is gone, focus goes to the question box", async () => {
  const t = await boot(ok("OPS-1001"));
  const opener = t.makeOpener();
  t.drawer.open("OPS-1001", opener); await settle(); await settle();
  t.els.thread.removeChild(opener);
  t.els.drawerClose.fire("click");
  assert.strictEqual(t.doc.activeElement, t.els.question);
  const t2 = await boot(ok("OPS-1001"));
  t2.drawer.open("OPS-1001", null); await settle(); await settle();
  t2.esc();
  assert.strictEqual(t2.doc.activeElement, t2.els.question, "no opener at all");
});
test("opening while open does not throw, keeps the first opener and loads the new ticket", async () => {
  const t = await boot((url) => response(200, asApi(byId(url.split("/").pop()))));
  const first = t.makeOpener();
  t.drawer.open("OPS-1001", first); t.drawer.open("OPS-1006", t.makeOpener());
  await settle(); await settle();
  assert.deepStrictEqual(t.doc.modalLog, ["open"]);
  t.els.drawerClose.fire("click");
  assert.strictEqual(t.doc.activeElement, first);
});
test("closing twice or reopening later works and shows the new ticket from scratch", async () => {
  const t = await boot((url) => response(200, asApi(byId(url.split("/").pop()))));
  t.drawer.open("OPS-1001", null); await settle(); await settle();
  t.esc(); t.els.drawer.close();
  t.drawer.open("OPS-1006", null);
  assert(t.text().includes("Loading ticket") && !t.text().includes("Rahul Gowda"));
  await settle(); await settle();
  assert(t.text().includes("Ravi Krishnan"));
  assert.strictEqual(t.doc.body.classList.contains("drawer-open"), true);
  t.esc();
  assert.strictEqual(t.doc.body.classList.contains("drawer-open"), false);
});
test("the scroll position of the drawer body is reset for each ticket", async () => {
  const t = await boot((url) => response(200, asApi(byId(url.split("/").pop()))));
  t.drawer.open("OPS-1001", null); await settle(); await settle();
  t.els.drawerBody.scrollTop = 300;
  t.drawer.open("OPS-1006", null);
  assert.strictEqual(t.els.drawerBody.scrollTop, 0);
});

// ---------- wiring to the chat ----------
test("a chip and a source card in the chat open the drawer for their ticket, and focus returns to that element", async () => {
  const t = await boot(ok("OPS-1001"));
  t.els.question.value = "q"; t.els.question.fire("input"); t.a.chat.submit(); await settle(); await settle();
  const chip = t.els.thread.getElementsByClassName("chip").find((e) => e.tagName === "BUTTON");
  chip.focus(); chip.fire("click"); await settle(); await settle();
  assert.strictEqual(t.els.drawer.open, true);
  assert.deepStrictEqual(t.log.map((l) => l.url), ["/tickets/OPS-1001"]);
  t.els.drawerClose.fire("click");
  assert.strictEqual(t.doc.activeElement, chip);
  const card = t.els.thread.getElementsByClassName("source-card")[0];
  card.focus(); card.fire("click"); await settle(); await settle();
  t.esc();
  assert.strictEqual(t.doc.activeElement, card);
  assert.strictEqual(t.log.length, 2);
});
test("the source uses no innerHTML, outerHTML, insertAdjacentHTML or style", () => {
  const code = fs.readFileSync(appPath, "utf8");
  assert(!/innerHTML|outerHTML|insertAdjacentHTML|\.style\b/.test(code));
  assert(/TICKET_ID_PATTERN = \/\^OPS-\[0-9\]\+\$\//.test(code));
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
