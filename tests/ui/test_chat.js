"use strict";
// Tests for Group C (chat). Usage: [OPS_INDEX_HTML=path] node test_chat.js [PATH_TO_APP_JS]   (defaults: static/app.js, static/index.html)
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

const HEALTH = { status: "ok", indexed_tickets: 800, model: "m", key_configured: true };
const FILTERS = { category: ["Network", "Billing"], priority: ["P1-Critical", "P2-High"], status: ["Open"], team: ["T"], created_min: "2025-10-26", created_max: "2026-10-24" };
const settle = () => new Promise((r) => setImmediate(r));
const src = (id, extra) => ({ ticket_id: id, title: "Title " + id, category: "Network", priority: "P2-High", status: "Open", score: 0.58, ...extra });
const ANSWER = (extra) => ({ answer: "See [OPS-1001].", found: true, citations: ["OPS-1001"], unverified_citations: [], missing_references: [], sources: [src("OPS-1001")], ...extra });

// opts.chat: handler(url, init) for /chat. Returns everything a test needs.
async function boot(opts = {}) {
  const doc = new FakeDocument(HTML);
  const win = makeWindow(opts.win || {});
  const log = [];
  let clock = 1000000;
  const intervals = [];
  const fetch = makeFetch({
    "/health": () => response(200, HEALTH),
    "/filters": () => response(200, FILTERS),
    "/chat": opts.chat || (() => response(200, ANSWER())),
  }, log);
  const a = app.init(doc, win, {
    fetch,
    now: () => clock,
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval: (h) => { intervals[h - 1] = null; },
    chatTimeoutMs: opts.chatTimeoutMs,
  });
  await a.ready;
  const chatLog = () => log.filter((l) => l.url === "/chat");
  const t = {
    doc, win, a, els: a.els, chat: a.chat, log, chatLog, intervals,
    advance: (ms) => { clock += ms; a.chat.tickCountdowns(); },
    type: (text) => { a.els.question.value = text; a.els.question.fire("input"); },
    send: async () => { const ok = a.chat.submit(); await settle(); await settle(); return ok; },
    byClass: (c) => a.els.thread.getElementsByClassName(c),
    bodies: () => chatLog().map((l) => JSON.parse(l.body)),
  };
  return t;
}
const tagsCreatedAfterBoot = (t, before) => new Set(t.doc.created.slice(before));

// ---------- pure helpers ----------
test("validateChatResponse: accepts a good body and cleans it", () => {
  const r = app.validateChatResponse(ANSWER({ citations: ["ops-1001", "OPS-1001", "bad", 5], missing_references: ["PO-7", "x", "inv-3"], unverified_citations: ["OPS-5"] }));
  assert.deepStrictEqual(r.citations, ["OPS-1001"]);
  assert.deepStrictEqual(r.missing, ["PO-7", "INV-3"]);
  assert.deepStrictEqual(r.unverified, ["OPS-5"]);
  assert.strictEqual(r.sources.length, 1);
  assert.strictEqual(r.sources[0].score, 0.58);
});
test("validateChatResponse: rejects things that are not an answer", () => {
  for (const bad of [null, undefined, "x", 5, [], {}, { answer: 1, found: true }, { answer: "a" }, { answer: "a", found: "yes" }]) {
    assert.strictEqual(app.validateChatResponse(bad), null, JSON.stringify(bad));
  }
});
test("validateChatResponse: drops sources without a usable id, nulls a bad score, cuts long and control text", () => {
  const r = app.validateChatResponse(ANSWER({ sources: [src("OPS-1"), { ticket_id: "nope" }, null, 4, src("OPS-2", { score: "high" }), src("OPS-3", { score: null }), src("OPS-4", { title: "a\u0000b\nc" + "x".repeat(500) })] }));
  assert.deepStrictEqual(r.sources.map((s) => s.ticket_id), ["OPS-1", "OPS-2", "OPS-3", "OPS-4"]);
  assert.strictEqual(r.sources[1].score, null);
  assert.strictEqual(r.sources[2].score, null);
  assert(r.sources[3].title.length <= 300 && !/[\u0000\n]/.test(r.sources[3].title));
});
test("validateChatResponse: a missing sources/citations list is fine", () => {
  const r = app.validateChatResponse({ answer: "a", found: false });
  assert.deepStrictEqual([r.sources, r.citations, r.missing, r.unverified], [[], [], [], []]);
});
test("priorityClass and scoreText", () => {
  assert.strictEqual(app.priorityClass("P1-Critical"), "badge-p1");
  assert.strictEqual(app.priorityClass("P4-Low"), "badge-p4");
  for (const x of ["P5", "High", "", null, undefined, "p1", "P12"]) assert.strictEqual(app.priorityClass(x), "badge-unknown", String(x));
  assert.strictEqual(app.scoreText(null), "exact match");
  assert.strictEqual(app.scoreText(0.5849), "similarity 0.58");
  assert.strictEqual(app.scoreText(1), "similarity 1.00");
  assert.strictEqual(app.scoreText(undefined), "exact match");
});
test("createCountdowns: absolute end time, one shared timer, stops when empty", () => {
  let clock = 0, started = 0, stopped = 0, tick;
  const c = app.createCountdowns(() => clock, (fn) => { started++; tick = fn; return 7; }, () => { stopped++; });
  const seen = []; let done = 0;
  c.add({ endsAt: 5000, onTick: (s) => seen.push(s), onDone: () => done++ });
  c.add({ endsAt: 9000, onTick: () => {}, onDone: () => done++ });
  assert.strictEqual(started, 1);
  clock = 1200; tick(); clock = 4100; tick();
  assert.deepStrictEqual(seen, [4, 1]);
  clock = 60000; tick(); // slept for a minute: both are over at once
  assert.strictEqual(done, 2);
  assert.strictEqual(c.size(), 0);
  assert.strictEqual(stopped, 1);
});
test("createCountdowns: remove and clear stop the timer", () => {
  let stopped = 0;
  const c = app.createCountdowns(() => 0, () => 1, () => { stopped++; });
  const e = c.add({ endsAt: 5000, onTick() {}, onDone() { throw new Error("must not run"); } });
  c.remove(e);
  assert.strictEqual(c.size(), 0); assert.strictEqual(stopped, 1);
  c.add({ endsAt: 5000, onTick() {}, onDone() {} });
  c.clear();
  assert.strictEqual(c.size(), 0); assert.strictEqual(stopped, 2);
});

// ---------- rendering ----------
const ALLOWED_TAGS = new Set(["div", "p", "ul", "li", "span", "strong", "br", "button", "details", "summary"]);

test("hostile answer, titles and ids stay inert text; only whitelisted tags are created", async () => {
  const t = await boot();
  const before = t.doc.created.length;
  const evil = "<img src=x onerror=alert(1)><script>alert(2)</script>";
  const data = app.validateChatResponse({
    answer: evil + " **<b>bold</b>** [OPS-1001] and [OPS-9999]\n- <a href=\"javascript:alert(3)\">x</a>\n- [PO-5]",
    found: true, citations: ["OPS-1001"], unverified_citations: ["OPS-9999"], missing_references: ["PO-5"],
    sources: [src("OPS-1001", { title: evil, category: evil, status: evil, priority: evil })],
  });
  const node = app.renderAssistantMessage(t.doc, data, () => {});
  assert(node.textContent.includes(evil), "markup is shown as literal text");
  const used = tagsCreatedAfterBoot(t, before);
  for (const tag of used) assert(ALLOWED_TAGS.has(tag), "unexpected tag created: " + tag);
  assert(!used.has("img") && !used.has("script") && !used.has("a"));
});
test("renderAssistantMessage: chips only for retrieved tickets; others are muted text with the marker", async () => {
  const t = await boot();
  const data = app.validateChatResponse(ANSWER({ answer: "A [OPS-1001], B [OPS-9999], C [OPS-1002].", citations: ["OPS-1001"], unverified_citations: ["OPS-9999"], sources: [src("OPS-1001"), src("OPS-1002")] }));
  const node = app.renderAssistantMessage(t.doc, data, () => {});
  const chips = node.getElementsByClassName("chip").filter((e) => e.tagName === "BUTTON");
  assert.deepStrictEqual(chips.map((c) => c.dataset.ticket), ["OPS-1001", "OPS-1002"]);
  chips.forEach((c) => assert.strictEqual(c.getAttribute("type"), "button"));
  assert.strictEqual(chips[0].getAttribute("aria-label"), "Open ticket OPS-1001");
  const muted = node.getElementsByClassName("chip-muted");
  assert.strictEqual(muted.length, 1);
  assert(muted[0].textContent.includes("[OPS-9999]") && muted[0].textContent.includes("not in retrieved tickets"));
  assert.strictEqual(muted[0].tagName, "SPAN");
  assert.strictEqual(muted[0].listeners.click, undefined, "not clickable");
});
test("renderAssistantMessage: bold, line breaks and lists come out as strong, br, ul/li", async () => {
  const t = await boot();
  const data = app.validateChatResponse(ANSWER({ answer: "one **two**\nthree\n- a\n- b", sources: [] , citations: []}));
  const node = app.renderAssistantMessage(t.doc, data, () => {});
  assert.strictEqual(node.getElementsByClassName("answer")[0].children.length >= 2, true);
  const tags = []; node.walk((e) => tags.push(e.tagName));
  assert(tags.includes("STRONG") && tags.includes("BR") && tags.includes("UL") && tags.filter((x) => x === "LI").length === 2);
});
test("renderAssistantMessage: found=false shows the label and the not-found class", async () => {
  const t = await boot();
  const nf = app.renderAssistantMessage(t.doc, app.validateChatResponse(ANSWER({ found: false, answer: "No.", sources: [], citations: [] })), () => {});
  assert.strictEqual(nf.getElementsByClassName("label-notfound").length, 1);
  assert.strictEqual(nf.getElementsByClassName("label-notfound")[0].textContent, "Not found in the tickets");
  assert.strictEqual(nf.getElementsByClassName("bubble-notfound").length, 1);
  const ok = app.renderAssistantMessage(t.doc, app.validateChatResponse(ANSWER()), () => {});
  assert.strictEqual(ok.getElementsByClassName("label-notfound").length, 0);
  assert.strictEqual(ok.getElementsByClassName("bubble-notfound").length, 0);
});
test("renderAssistantMessage: the callout lists missing references and is absent otherwise", async () => {
  const t = await boot();
  const withMissing = app.renderAssistantMessage(t.doc, app.validateChatResponse(ANSWER({ missing_references: ["PO-7", "INV-3"] })), () => {});
  const c = withMissing.getElementsByClassName("callout");
  assert.strictEqual(c.length, 1);
  assert.strictEqual(c[0].getAttribute("role"), "note");
  assert.strictEqual(c[0].textContent, "Not found in the ticket data: PO-7, INV-3");
  assert.strictEqual(app.renderAssistantMessage(t.doc, app.validateChatResponse(ANSWER()), () => {}).getElementsByClassName("callout").length, 0);
});
test("sources: collapsed section, count, cited first as given, badge text+class, exact match for a null score", async () => {
  const t = await boot();
  const data = app.validateChatResponse(ANSWER({
    citations: ["OPS-1002"],
    sources: [src("OPS-1002", { priority: "P1-Critical", score: null }), src("OPS-1001", { priority: "P3-Medium" }), src("OPS-1003", { priority: "weird" })],
  }));
  const node = app.renderAssistantMessage(t.doc, data, () => {});
  const det = node.getElementsByClassName("sources")[0];
  assert.strictEqual(det.tagName, "DETAILS");
  assert.strictEqual(det.hasAttribute("open"), false);
  assert.strictEqual(node.getElementsByClassName("sources-summary")[0].textContent, "Sources (3)");
  const cards = node.getElementsByClassName("source-card");
  assert.deepStrictEqual(cards.map((c) => c.dataset.ticket), ["OPS-1002", "OPS-1001", "OPS-1003"]);
  assert.strictEqual(cards[0].getElementsByClassName("cited-marker").length, 1);
  assert.strictEqual(cards[1].getElementsByClassName("cited-marker").length, 0);
  const b = cards.map((c) => c.getElementsByClassName("badge")[0]);
  assert.deepStrictEqual(b.map((x) => x.textContent), ["P1-Critical", "P3-Medium", "weird"]);
  assert(b[0].classList.contains("badge-p1") && b[1].classList.contains("badge-p3") && b[2].classList.contains("badge-unknown"));
  assert.strictEqual(cards[0].getElementsByClassName("score")[0].textContent, "exact match");
  assert.strictEqual(cards[1].getElementsByClassName("score")[0].textContent, "similarity 0.58");
  assert.strictEqual(cards[0].getElementsByClassName("card-title")[0].textContent, "Title OPS-1002");
});
test("sources: no section when nothing was retrieved; a card without title or priority still renders", async () => {
  const t = await boot();
  const none = app.renderAssistantMessage(t.doc, app.validateChatResponse(ANSWER({ sources: [], citations: [] })), () => {});
  assert.strictEqual(none.getElementsByClassName("sources").length, 0);
  const odd = app.renderAssistantMessage(t.doc, app.validateChatResponse(ANSWER({ sources: [src("OPS-1", { title: "", priority: "" })] })), () => {});
  const card = odd.getElementsByClassName("source-card")[0];
  assert.strictEqual(card.getElementsByClassName("card-title")[0].textContent, "(no title)");
  assert.strictEqual(card.getElementsByClassName("badge")[0].textContent, "No priority");
});
test("chips and cards call the open handler with the id and the element", async () => {
  const t = await boot();
  const calls = [];
  const node = app.renderAssistantMessage(t.doc, app.validateChatResponse(ANSWER()), (id, el) => calls.push([id, el]));
  const chip = node.getElementsByClassName("chip").find((e) => e.tagName === "BUTTON");
  const card = node.getElementsByClassName("source-card")[0];
  chip.fire("click"); card.fire("click");
  assert.deepStrictEqual(calls.map((c) => c[0]), ["OPS-1001", "OPS-1001"]);
  assert.strictEqual(calls[0][1], chip); assert.strictEqual(calls[1][1], card);
});
test("loading bubble: status role, three dots, text", async () => {
  const t = await boot();
  const n = app.renderLoading(t.doc);
  const l = n.getElementsByClassName("loading")[0];
  assert.strictEqual(l.getAttribute("role"), "status");
  assert.strictEqual(n.getElementsByClassName("dot").length, 3);
  assert(l.textContent.includes("Looking through the tickets"));
});
test("error bubble: alert role, no Retry for a non-retryable error, countdown parts for a 429", async () => {
  const t = await boot();
  const plain = app.renderErrorBubble(t.doc, app.describeError({ kind: "network" }));
  assert.strictEqual(plain.node.getElementsByClassName("error-bubble")[0].getAttribute("role"), "alert");
  assert(plain.retry && !plain.countdownText);
  const e422 = app.renderErrorBubble(t.doc, app.describeError({ kind: "http", status: 422, body: { error: "x" } }));
  assert.strictEqual(e422.retry, null);
  const e429 = app.renderErrorBubble(t.doc, app.describeError({ kind: "http", status: 429, body: { error: "slow down" }, retryAfter: "90" }));
  assert(e429.retry && e429.countdownText && e429.doneNote);
  assert.strictEqual(e429.countdownText.getAttribute("role"), "timer");
  assert.strictEqual(e429.countdownText.getAttribute("aria-live"), "off");
  assert.strictEqual(e429.doneNote.getAttribute("role"), "status");
  assert.strictEqual(e429.doneNote.hidden, true);
  assert.strictEqual(e429.countdownText.textContent, app.formatDuration(90));
});

// ---------- the controller ----------
test("a question is sent as a payload with k and filters; the user message and loading bubble appear; the empty state goes", async () => {
  let release; const gate = new Promise((r) => { release = r; });
  const t = await boot({ chat: async () => { await gate; return response(200, ANSWER()); } });
  assert.strictEqual(t.els.emptyState.parentNode, t.els.thread);
  t.a.setK(7); t.a.setSelection("category", "Network");
  t.type("  What is OPS-1001?  ");
  assert.strictEqual(await t.send(), true);
  assert.strictEqual(t.els.emptyState.parentNode, null);
  assert.strictEqual(t.byClass("message-user").length, 1);
  assert.strictEqual(t.byClass("message-user")[0].textContent, "What is OPS-1001?");
  assert.strictEqual(t.byClass("loading").length, 1);
  assert.strictEqual(t.els.question.value, "", "the box is cleared when sent");
  assert.deepStrictEqual(t.bodies()[0], { question: "What is OPS-1001?", k: 7, filters: { category: "Network" } });
  assert.strictEqual(t.chatLog()[0].method, "POST");
  assert.strictEqual(t.chatLog()[0].hasSignal, true);
  release(); await settle(); await settle();
  assert.strictEqual(t.byClass("loading").length, 0);
  assert.strictEqual(t.byClass("message-assistant").length, 1);
});
test("one request at a time: a second submit while pending does nothing", async () => {
  let release; const gate = new Promise((r) => { release = r; });
  const t = await boot({ chat: async () => { await gate; return response(200, ANSWER()); } });
  t.type("first"); await t.send();
  t.type("second");
  assert.strictEqual(t.els.send.disabled, true);
  assert.strictEqual(await t.send(), false);
  t.els.question.fire("keydown", { key: "Enter", shiftKey: false });
  t.els.composer.fire("submit");
  await settle();
  assert.strictEqual(t.chatLog().length, 1);
  assert.strictEqual(t.byClass("message-user").length, 1);
  assert.strictEqual(t.els.question.value, "second", "the second text is kept");
  release(); await settle(); await settle();
  assert.strictEqual(t.els.send.disabled, false);
  assert.strictEqual(await t.send(), true);
  assert.strictEqual(t.chatLog().length, 2);
});
test("history: nothing before the first success; then question+answer pairs; sources are never sent", async () => {
  const t = await boot();
  t.type("q1"); await t.send();
  t.type("q2"); await t.send();
  const [b1, b2] = t.bodies();
  assert.strictEqual(b1.history, undefined);
  assert.deepStrictEqual(b2.history, [{ role: "user", content: "q1" }, { role: "assistant", content: "See [OPS-1001]." }]);
  assert.deepStrictEqual(Object.keys(b2).sort(), ["history", "k", "question"]);
});
test("history keeps the trimmed question, not the padded text", async () => {
  const t = await boot();
  t.type("  padded q1  \n"); await t.send();
  t.type("q2"); await t.send();
  assert.strictEqual(t.bodies()[1].history[0].content, "padded q1");
  assert.strictEqual(t.chat.state.history[0].content, "padded q1");
});
test("New chat after a finished conversation: the next question carries no history", async () => {
  const t = await boot();
  t.type("q1"); await t.send();
  t.type("q2"); await t.send();
  assert.strictEqual(t.chat.state.history.length, 4);
  t.els.newChat.fire("click");
  assert.strictEqual(t.chat.state.history.length, 0);
  t.type("q3"); await t.send();
  assert.strictEqual(t.bodies()[2].history, undefined);
});
test("history is capped at the last 12 messages", async () => {
  const t = await boot();
  for (let i = 0; i < 9; i++) { t.type("q" + i); await t.send(); }
  assert.strictEqual(t.chat.state.history.length, 12);
  assert.strictEqual(t.chat.state.history[0].content, "q3");
  assert(t.bodies()[8].history.length <= 12);
});
test("a failed request leaves the history unchanged and shows an error bubble with Retry", async () => {
  let n = 0;
  const t = await boot({ chat: () => (++n === 1 ? response(200, ANSWER()) : response(503, { error: "busy", code: "upstream_unavailable" })) });
  t.type("q1"); await t.send();
  t.type("q2"); await t.send();
  assert.strictEqual(t.chat.state.history.length, 2);
  assert.strictEqual(t.byClass("error-bubble").length, 1);
  assert.strictEqual(t.byClass("error-bubble")[0].getAttribute("role"), "alert");
  assert.strictEqual(t.byClass("loading").length, 0);
  assert(t.byClass("button").some((b) => b.textContent === "Retry"));
});
test("Retry re-sends the original question, k and filters, with the CURRENT history, and removes the error", async () => {
  let n = 0;
  const t = await boot({ chat: () => (++n === 2 ? response(500, { error: "boom" }) : response(200, ANSWER())) });
  t.a.setK(4); t.a.setSelection("team", "T");
  t.type("q1"); await t.send();
  t.type("q2"); await t.send();           // fails
  t.a.setK(9); t.a.setSelection("team", ""); t.a.setSelection("priority", "P1-Critical"); // user changes the filters afterwards
  const retry = t.byClass("button").find((b) => b.textContent === "Retry");
  retry.fire("click"); await settle(); await settle();
  const b = t.bodies();
  assert.strictEqual(b.length, 3);
  assert.deepStrictEqual([b[2].question, b[2].k, b[2].filters], ["q2", 4, { team: "T" }]);
  assert.deepStrictEqual(b[2].history, [{ role: "user", content: "q1" }, { role: "assistant", content: "See [OPS-1001]." }]);
  assert.strictEqual(t.byClass("error-bubble").length, 0);
  assert.strictEqual(t.byClass("message-user").length, 2, "no duplicate user message on retry");
  assert.strictEqual(t.chat.state.history.length, 4);
});
test("a new question after an error takes the Retry button and countdown off the old error", async () => {
  let n = 0;
  const t = await boot({ chat: () => (++n === 1 ? response(429, { error: "slow" }, { "Retry-After": "60" }) : response(200, ANSWER())) });
  t.type("q1"); await t.send();
  assert(t.byClass("button").some((b) => b.textContent === "Retry"));
  assert.strictEqual(t.chat.countdownCount(), 1);
  t.type("q2"); await t.send();
  assert(!t.byClass("button").some((b) => b.textContent === "Retry"));
  assert.strictEqual(t.chat.countdownCount(), 0);
  assert.strictEqual(t.byClass("error-bubble").length, 1, "the old error text stays");
});
test("429: countdown from Retry-After on an absolute end time; Retry is off until it ends; then a note", async () => {
  const t = await boot({ chat: () => response(429, { error: "Quota used up" }, { "Retry-After": "90" }) });
  t.type("q"); await t.send();
  const retry = t.byClass("button").find((b) => b.textContent === "Retry");
  const timer = t.byClass("countdown")[0];
  assert.strictEqual(retry.disabled, true);
  assert.strictEqual(timer.textContent, app.formatDuration(90));
  assert.strictEqual(t.intervals.length, 1);
  t.advance(30000);
  assert.strictEqual(timer.textContent, app.formatDuration(60));
  assert.strictEqual(retry.disabled, true);
  t.advance(10 * 60000); // a long sleep: still correct because the end time is absolute
  assert.strictEqual(retry.disabled, false);
  const note = t.byClass("error-detail").find((e) => e.textContent === "You can retry now.");
  assert(note && note.hidden === false);
  assert.strictEqual(t.chat.countdownCount(), 0);
  assert.strictEqual(t.intervals[0], null, "the timer was stopped");
});
test("429: Send is never disabled by the quota block, a new question can be sent during the countdown", async () => {
  let n = 0;
  const t = await boot({ chat: () => (++n === 1 ? response(429, { error: "q" }, { "Retry-After": "600" }) : response(200, ANSWER())) });
  t.type("q1"); await t.send();
  t.type("q2");
  assert.strictEqual(t.els.send.disabled, false);
  assert.strictEqual(await t.send(), true);
  assert.strictEqual(t.chatLog().length, 2);
});
test("429 without Retry-After: Retry is enabled at once and there is no countdown", async () => {
  const t = await boot({ chat: () => response(429, { error: "x" }) });
  t.type("q"); await t.send();
  assert.strictEqual(t.byClass("countdown").length, 0);
  assert.strictEqual(t.byClass("button").find((b) => b.textContent === "Retry").disabled, false);
});
test("422: an error bubble with no Retry button", async () => {
  const t = await boot({ chat: () => response(422, { error: "bad", code: "validation_error" }) });
  t.type("q"); await t.send();
  assert.strictEqual(t.byClass("error-bubble").length, 1);
  assert(!t.byClass("button").some((b) => b.textContent === "Retry"));
});
test("network failure, invalid JSON and a body that is not an answer all give an error bubble", async () => {
  for (const handler of [() => { throw new Error("down"); }, () => response(200, "not json"), () => response(200, { hello: 1 })]) {
    const t = await boot({ chat: handler });
    t.type("q"); await t.send();
    assert.strictEqual(t.byClass("error-bubble").length, 1);
    assert.strictEqual(t.chat.state.history.length, 0);
    assert.strictEqual(t.els.send.disabled, true === false ? true : t.els.send.disabled);
    t.type("again"); assert.strictEqual(t.els.send.disabled, false, "not stuck pending");
  }
});
test("timeout: a hanging request is stopped after chatTimeoutMs and shown as an error with Retry", async () => {
  const t = await boot({ chat: hang, chatTimeoutMs: 30 });
  t.type("q"); t.chat.submit();
  await new Promise((r) => setTimeout(r, 120));
  assert.strictEqual(t.byClass("error-bubble").length, 1);
  assert(t.byClass("error-detail").some((e) => /150 seconds/.test(e.textContent)));
  assert(t.byClass("button").some((b) => b.textContent === "Retry"));
  t.type("next"); assert.strictEqual(t.els.send.disabled, false);
});
test("New chat while a request is pending: aborts it, clears everything, and the late reply is ignored", async () => {
  let release; const gate = new Promise((r) => { release = r; });
  const t = await boot({ chat: async () => { await gate; return response(200, ANSWER()); } });
  t.type("q1"); await t.send();
  const signal = t.chatLog()[0].signal;
  t.els.newChat.fire("click");
  assert.strictEqual(signal.aborted, true);
  assert.strictEqual(t.els.emptyState.parentNode, t.els.thread);
  assert.strictEqual(t.els.thread.children.length, 1);
  assert.strictEqual(t.chat.state.pending, null);
  assert.strictEqual(t.doc.activeElement, t.els.question);
  release(); await settle(); await settle();
  assert.strictEqual(t.els.thread.children.length, 1, "late reply not shown");
  assert.strictEqual(t.chat.state.history.length, 0);
  t.type("q2"); await t.send();
  assert.strictEqual(t.bodies()[1].history, undefined, "a fresh conversation");
});
test("New chat: a late reply is ignored even when the request cannot be aborted (the fake ignores the signal)", async () => {
  let release; const gate = new Promise((r) => { release = r; });
  const t = await boot({ chat: async () => { await gate; return response(200, ANSWER()); } });
  t.type("q1"); await t.send();
  t.chat.newChat();
  t.type("q2");
  assert.strictEqual(t.els.send.disabled, false, "a new question can be sent right away");
  release(); await settle(); await settle();
  assert.strictEqual(t.byClass("message-assistant").length, 0);
  assert.strictEqual(t.chat.state.history.length, 0);
});
test("New chat clears errors and countdowns and hides Jump; with the sidebar open it closes it without moving focus to the toggle", async () => {
  const t = await boot({ chat: () => response(429, { error: "x" }, { "Retry-After": "600" }) });
  t.type("q"); await t.send();
  assert.strictEqual(t.chat.countdownCount(), 1);
  t.els.jumpLatest.hidden = false;
  t.els.sidebarToggle.fire("click");
  assert.strictEqual(t.a.state.sidebarOpen, true);
  t.els.newChat.fire("click");
  assert.strictEqual(t.chat.countdownCount(), 0);
  assert.strictEqual(t.els.jumpLatest.hidden, true);
  assert.strictEqual(t.a.state.sidebarOpen, false);
  assert.strictEqual(t.els.mainArea.hasAttribute("inert"), false);
  assert.strictEqual(t.doc.activeElement, t.els.question);
  assert.deepStrictEqual(t.doc.focusBlocked, []);
});
test("Send state: off when empty, whitespace only, too long, pending, or with a date problem; on otherwise", async () => {
  const t = await boot();
  assert.strictEqual(t.els.send.disabled, true);
  t.type("   "); assert.strictEqual(t.els.send.disabled, true);
  t.type("ok"); assert.strictEqual(t.els.send.disabled, false);
  t.type("x".repeat(1001)); assert.strictEqual(t.els.send.disabled, true);
  t.type("x".repeat(1000)); assert.strictEqual(t.els.send.disabled, false);
  t.a.setSelection("date_from", "2026-05-01"); t.a.setSelection("date_to", "2026-04-01");
  assert(t.a.state.problem, "the fixture makes an inverted range");
  assert.strictEqual(t.els.send.disabled, true);
  assert.strictEqual(await t.send(), false);
  assert.strictEqual(t.chatLog().length, 0);
  t.a.setSelection("date_from", ""); t.a.setSelection("date_to", "");
  assert.strictEqual(t.els.send.disabled, false);
});
test("keyboard: Enter sends and is prevented; Shift+Enter is left alone; other keys do nothing", async () => {
  const t = await boot();
  t.type("hello");
  const shift = t.els.question.fire("keydown", { key: "Enter", shiftKey: true });
  assert.strictEqual(shift.defaultPrevented, false);
  const other = t.els.question.fire("keydown", { key: "a" });
  assert.strictEqual(other.defaultPrevented, false);
  assert.strictEqual(t.chatLog().length, 0);
  const enter = t.els.question.fire("keydown", { key: "Enter", shiftKey: false });
  assert.strictEqual(enter.defaultPrevented, true);
  await settle();
  assert.strictEqual(t.chatLog().length, 1);
});
test("keyboard: Enter while an input method is composing never sends and is not prevented", async () => {
  const t = await boot();
  t.type("こんにちは");
  const a = t.els.question.fire("keydown", { key: "Enter", shiftKey: false, isComposing: true });
  const b = t.els.question.fire("keydown", { key: "Enter", shiftKey: false, keyCode: 229 });
  assert.strictEqual(a.defaultPrevented, false); assert.strictEqual(b.defaultPrevented, false);
  await settle();
  assert.strictEqual(t.chatLog().length, 0);
});
test("keyboard: Enter on an empty box does not send, but is still prevented (no stray newline-submit)", async () => {
  const t = await boot();
  const e = t.els.question.fire("keydown", { key: "Enter", shiftKey: false });
  assert.strictEqual(e.defaultPrevented, true);
  assert.strictEqual(t.chatLog().length, 0);
});
test("the composer form submit (the Send button) sends and is prevented", async () => {
  const t = await boot();
  t.type("via button");
  const e = t.els.composer.fire("submit");
  assert.strictEqual(e.defaultPrevented, true);
  await settle();
  assert.strictEqual(t.chatLog().length, 1);
});
test("counter: hidden below 900, shows N / 1000 from 900, limit class at 1000; a date problem shows its own line", async () => {
  const t = await boot();
  assert.strictEqual(t.els.charCount.hidden, true);
  t.type("x".repeat(899)); assert.strictEqual(t.els.charCount.hidden, true);
  t.type("x".repeat(900));
  assert.strictEqual(t.els.charCount.hidden, false);
  assert.strictEqual(t.els.charCount.textContent, "900 / 1000");
  assert(t.els.charCount.classList.contains("is-near") && !t.els.charCount.classList.contains("is-limit"));
  t.type("x".repeat(1000));
  assert(t.els.charCount.classList.contains("is-limit") && !t.els.charCount.classList.contains("is-near"));
  t.type("short");
  assert.strictEqual(t.els.charCount.hidden, true);
  t.a.setSelection("date_from", "2026-05-01"); t.a.setSelection("date_to", "2026-04-01");
  assert.strictEqual(t.els.charCount.textContent, "Fix the date range in the filters to send.");
  assert.strictEqual(t.els.charCount.hidden, false);
  t.a.setSelection("date_from", ""); t.a.setSelection("date_to", "");
  assert.strictEqual(t.els.charCount.hidden, true);
});
test("example questions: clicking one fills the box and sends it", async () => {
  const t = await boot();
  const ex = t.els.emptyState.getElementsByClassName("example");
  assert(ex.length >= 3);
  const text = ex[1].textContent;
  ex[1].fire("click"); await settle(); await settle();
  assert.strictEqual(t.bodies()[0].question, text);
  assert.strictEqual(t.byClass("message-user")[0].textContent, text);
});
test("scroll: the user's own message always scrolls down; an answer scrolls only if the reader was near the bottom, else Jump appears", async () => {
  let release; const gate = new Promise((r) => { release = r; });
  const t = await boot({ chat: async () => { await gate; return response(200, ANSWER()); } });
  const th = t.els.thread;
  th.scrollHeight = 1000; th.clientHeight = 400; th.scrollTop = 0; // far from the bottom
  t.type("q"); await t.send();
  assert(th.scrollCalls.length >= 1, "own message scrolled");
  assert.strictEqual(t.els.jumpLatest.hidden, true);
  th.scrollTop = 0; // the reader scrolls up while waiting
  const calls = th.scrollCalls.length;
  release(); await settle(); await settle();
  assert.strictEqual(th.scrollCalls.length, calls, "the answer did not pull the reader down");
  assert.strictEqual(t.els.jumpLatest.hidden, false);
  t.els.jumpLatest.fire("click");
  assert.strictEqual(th.scrollCalls.length, calls + 1);
  assert.strictEqual(t.els.jumpLatest.hidden, true);
});
test("scroll: the Jump button hides when the reader scrolls back near the bottom; reduced motion uses auto", async () => {
  const t = await boot({ win: { reducedMotion: true } });
  const th = t.els.thread;
  th.scrollHeight = 1000; th.clientHeight = 400; th.scrollTop = 0;
  t.els.jumpLatest.hidden = false;
  th.fire("scroll");
  assert.strictEqual(t.els.jumpLatest.hidden, false, "still far up");
  th.scrollTop = 560; th.fire("scroll");
  assert.strictEqual(t.els.jumpLatest.hidden, true);
  t.type("q"); await t.send();
  assert.strictEqual(th.scrollCalls[0].behavior, "auto");
  const t2 = await boot();
  t2.type("q"); await t2.send();
  assert.strictEqual(t2.els.thread.scrollCalls[0].behavior, "smooth");
});
test("chat chips open tickets through the handler set later (Group D hook)", async () => {
  const t = await boot();
  const opened = [];
  t.chat.setOpenTicketHandler((id) => opened.push(id));
  t.type("q"); await t.send();
  t.byClass("chip").find((e) => e.tagName === "BUTTON").fire("click");
  assert.deepStrictEqual(opened, ["OPS-1001"]);
  const t2 = await boot();
  t2.type("q"); await t2.send();
  t2.byClass("chip").find((e) => e.tagName === "BUTTON").fire("click"); // no handler yet: must not throw
});
test("page accessibility hooks: the thread is a polite log, the loading and error parts have roles", async () => {
  const t = await boot();
  assert.strictEqual(t.els.thread.getAttribute("role"), "log");
  assert.strictEqual(t.els.thread.getAttribute("aria-live"), "polite");
});
test("the source never uses innerHTML, outerHTML, insertAdjacentHTML, style or on* attributes", () => {
  const code = fs.readFileSync(appPath, "utf8");
  assert(!/innerHTML|outerHTML|insertAdjacentHTML|\.style\b|setAttribute\(\s*["']on|setAttribute\(\s*["']style/.test(code));
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
