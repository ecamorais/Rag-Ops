"use strict";
// Tests for the pure functions of app.js (Group A). Usage: node test_pure.js [PATH_TO_APP_JS]   (default: static/app.js)
// No DOM, no server, no network, no Gemini. Prints one line per test.
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const assert = require("assert");

const appPath = path.resolve(process.argv[2] || path.join(__dirname, "..", "..", "static", "app.js"));
const source = fs.readFileSync(appPath, "utf8");

// ---- load in Node without any browser objects, and prove loading adds nothing to the globals
const globalsBefore = new Set(Object.getOwnPropertyNames(globalThis));
const app = require(appPath);
const globalsAfter = Object.getOwnPropertyNames(globalThis).filter((k) => !globalsBefore.has(k));

let passed = 0;
let failed = 0;
const failures = [];
function test(name, fn) {
  try {
    fn();
    passed++;
    console.log("  PASS  " + name);
  } catch (err) {
    failed++;
    failures.push(name);
    console.log("  FAIL  " + name + "\n        " + String(err.message).split("\n").join("\n        "));
  }
}
function section(title) {
  console.log("\n== " + title);
}
function timed(fn) {
  const t0 = process.hrtime.bigint();
  const value = fn();
  return { ms: Number(process.hrtime.bigint() - t0) / 1e6, value };
}

// ---- helpers for looking at parse trees
const INLINE_TYPES = new Set(["text", "br", "bold", "chip", "idtext"]);
const ALLOWED_KEYS = {
  text: ["type", "text"], br: ["type"], bold: ["type", "children"], chip: ["type", "id"], idtext: ["type", "id", "reason"],
  p: ["type", "inline"], ul: ["type", "items"],
};
function checkTree(blocks) {
  assert(Array.isArray(blocks), "the tree is an array");
  const visitInline = (nodes, insideBold) => {
    assert(Array.isArray(nodes), "inline content is an array");
    for (const n of nodes) {
      assert(INLINE_TYPES.has(n.type), "unexpected inline node type " + n.type);
      assert.deepStrictEqual(Object.keys(n).sort(), ALLOWED_KEYS[n.type].slice().sort(), "unexpected keys on " + n.type);
      if (n.type === "text") assert(typeof n.text === "string" && n.text !== "", "text nodes hold non-empty strings");
      if (n.type === "bold") { assert(!insideBold, "bold is never nested"); visitInline(n.children, true); }
      if (n.type === "chip" || n.type === "idtext") assert(/^OPS-\d{1,15}$/.test(n.id), "ids are normalised OPS-n");
    }
  };
  for (const b of blocks) {
    assert(b.type === "p" || b.type === "ul", "unexpected block type " + b.type);
    assert.deepStrictEqual(Object.keys(b).sort(), ALLOWED_KEYS[b.type].slice().sort());
    if (b.type === "p") visitInline(b.inline, false);
    else for (const item of b.items) visitInline(item, false);
  }
  JSON.parse(JSON.stringify(blocks)); // plain data only
}
function flat(nodes) {
  return nodes.map((n) => (n.type === "text" ? n.text : n.type === "br" ? "\n" : n.type === "bold" ? "**" + flat(n.children) + "**" : n.type === "chip" ? "[" + n.id + "]" : "{" + n.id + "}")).join("");
}
function textOf(blocks) {
  return blocks.map((b) => (b.type === "p" ? flat(b.inline) : b.items.map((i) => "- " + flat(i)).join("\n"))).join("\n\n");
}
function inlineOf(text, sources = ["OPS-1001"], unverified = []) {
  const blocks = app.parseAnswer(text, sources, unverified);
  checkTree(blocks);
  assert.strictEqual(blocks.length, 1, "expected one block, got " + JSON.stringify(blocks));
  assert.strictEqual(blocks[0].type, "p");
  return blocks[0].inline;
}
function allNodes(blocks) {
  const out = [];
  const walk = (nodes) => nodes.forEach((n) => { out.push(n); if (n.type === "bold") walk(n.children); });
  blocks.forEach((b) => (b.type === "p" ? walk(b.inline) : b.items.forEach(walk)));
  return out;
}

console.log("Testing: " + appPath);

section("loading without a browser");
test("the file loads in Node with no window or document", () => {
  // (Node 26 has an experimental localStorage global that warns when touched, so it is not probed here.)
  assert.strictEqual(typeof window, "undefined");
  assert.strictEqual(typeof document, "undefined");
  assert.strictEqual(typeof app.parseAnswer, "function");
});
test("loading adds no global variables", () => assert.deepStrictEqual(globalsAfter, []));
test("it also runs in an empty VM context with only `module` (no globals to lean on)", () => {
  const mod = { exports: {} };
  vm.runInNewContext(source, { module: mod }, { timeout: 2000 });
  assert.strictEqual(typeof mod.exports.describeError, "function");
});
test("without `module` it exports nothing and does not throw", () => {
  const ctx = {};
  vm.runInNewContext(source, ctx, { timeout: 2000 });
  assert.deepStrictEqual(Object.keys(ctx), []);
});
test("all expected functions are exported", () => {
  for (const name of ["parseAnswer", "classifyCitation", "formatDuration", "parseRetryAfter", "describeError", "truncateForHistory",
    "buildHistory", "validateQuestion", "clampK", "buildChatPayload", "resolveTheme", "nextTheme", "themeToggleLabel", "readStoredTheme", "writeStoredTheme"]) {
    assert.strictEqual(typeof app[name], "function", name);
  }
});

section("security: the source must not contain risky constructs");
test("no innerHTML, outerHTML, insertAdjacentHTML, eval, new Function, document.write and friends", () => {
  const banned = [
    [/\binnerHTML\b/, "innerHTML"], [/\bouterHTML\b/, "outerHTML"], [/\binsertAdjacentHTML\b/, "insertAdjacentHTML"],
    [/\beval\s*\(/, "eval("], [/\bnew\s+Function\b/, "new Function"], [/(^|[^\w.])Function\s*\(/, "Function("],
    [/\bdocument\s*\.\s*write(ln)?\b/, "document.write"], [/javascript:/i, "javascript: URL"], [/\bsrcdoc\b/, "srcdoc"],
    [/\.\s*(href|src|action|formAction)\s*=[^=]/, "assignment to href/src/action"], [/setAttribute\(\s*["'`]\s*on/i, "setAttribute on*"],
    [/\bon[a-z]+\s*=\s*["'`]/i, "inline handler text"], [/set(Timeout|Interval)\(\s*["'`]/, "string timer"],
    [/\bdocument\s*\.\s*cookie\b/, "document.cookie"], [/\bXMLHttpRequest\b/, "XMLHttpRequest"], [/\bimportScripts\b/, "importScripts"],
    [/\bimport\s*\(/, "dynamic import"], [/\bsendBeacon\b/, "sendBeacon"], [/https?:\/\//i, "an external URL"], [/\bwith\s*\(/, "with statement"],
    [/\b(location|window\.location)\s*(\.\s*(href|assign|replace)\s*)?=[^=]/, "navigation by assignment"],
  ];
  const hits = banned.filter(([re]) => re.test(source)).map(([, label]) => label);
  assert.deepStrictEqual(hits, [], "found: " + hits.join(", "));
});
test("the file is LF only, ends with a newline and is not empty", () => {
  const raw = fs.readFileSync(appPath);
  assert(raw.length > 0);
  assert.strictEqual(raw.includes(13), false, "contains CR");
  assert.strictEqual(raw[raw.length - 1], 10);
});

section("markdown-lite: structure");
test("a plain paragraph", () => assert.deepStrictEqual(inlineOf("Hello world"), [{ type: "text", text: "Hello world" }]));
test("empty and non-string input give an empty tree", () => {
  for (const v of ["", null, undefined, 42, {}, []]) assert.deepStrictEqual(app.parseAnswer(v, [], []), []);
});
test("single newlines become line breaks inside one paragraph", () =>
  assert.deepStrictEqual(inlineOf("a\nb"), [{ type: "text", text: "a" }, { type: "br" }, { type: "text", text: "b" }]));
test("a blank line starts a new paragraph", () => {
  const blocks = app.parseAnswer("one\n\ntwo", [], []);
  checkTree(blocks);
  assert.deepStrictEqual(blocks.map((b) => b.type), ["p", "p"]);
});
test("CRLF and lone CR line endings behave like LF", () => {
  assert.deepStrictEqual(app.parseAnswer("a\r\n\r\nb", [], []), app.parseAnswer("a\n\nb", [], []));
  assert.deepStrictEqual(app.parseAnswer("a\rb", [], []), app.parseAnswer("a\nb", [], []));
});
test("'- ' and '* ' lines make one list; text after it starts a paragraph", () => {
  const blocks = app.parseAnswer("Intro\n- first\n* second\n   - third\nAfter", [], []);
  checkTree(blocks);
  assert.deepStrictEqual(blocks.map((b) => b.type), ["p", "ul", "p"]);
  assert.strictEqual(blocks[1].items.length, 3);
  assert.strictEqual(flat(blocks[1].items[2]), "third");
});
test("things that look like bullets but are not", () => {
  for (const line of ["-x", "*x", "**bold line**", "--- rule", "-", "* ", "    - four spaces", "1. numbered"]) {
    const blocks = app.parseAnswer(line, [], []);
    checkTree(blocks);
    assert(blocks.every((b) => b.type === "p"), JSON.stringify(line) + " should not be a list");
  }
});
test("**bold** works, also several in one line and across a line break", () => {
  assert.deepStrictEqual(inlineOf("a **b** c **d**"), [
    { type: "text", text: "a " }, { type: "bold", children: [{ type: "text", text: "b" }] }, { type: "text", text: " c " },
    { type: "bold", children: [{ type: "text", text: "d" }] }]);
  assert.strictEqual(flat(inlineOf("**x\ny**")), "**x\ny**");
});
test("an unterminated **bold stays literal text", () => assert.deepStrictEqual(inlineOf("a **b"), [{ type: "text", text: "a **b" }]));
test("empty bold markers stay literal", () => {
  assert.strictEqual(textOf(app.parseAnswer("****", [], [])), "****");
  assert.strictEqual(textOf(app.parseAnswer("a ** ** b", [], [])), "a ** ** b");
});
test("unsupported markup stays literal: headings, italics, code, links, images, tables, numbered lists", () => {
  const s = "# Title\n*italic* `code` [link](http://x) ![img](y)\n| a | b |\n1. one";
  assert.strictEqual(textOf(app.parseAnswer(s, [], [])).replace(/\n\n/g, "\n"), s);
});

section("markdown-lite: ticket ids and chips");
test("an id that is a retrieved source becomes a chip", () => {
  const n = inlineOf("See [OPS-1001].");
  assert.deepStrictEqual(n, [{ type: "text", text: "See " }, { type: "chip", id: "OPS-1001" }, { type: "text", text: "." }]);
});
test("lower case is accepted and normalised", () => assert.deepStrictEqual(inlineOf("[ops-1001]"), [{ type: "chip", id: "OPS-1001" }]));
test("[OPS-1001][OPS-1001] gives two chips", () =>
  assert.deepStrictEqual(inlineOf("[OPS-1001][OPS-1001]"), [{ type: "chip", id: "OPS-1001" }, { type: "chip", id: "OPS-1001" }]));
test("an id in unverified_citations is NOT a chip, even if it is also in the sources", () =>
  assert.deepStrictEqual(inlineOf("[OPS-0000]", ["OPS-1001"], ["OPS-0000"]), [{ type: "idtext", id: "OPS-0000", reason: "unverified" }]));
test("[OPS-0000] unverified, and an id that is simply not in the sources, are both muted text", () => {
  assert.deepStrictEqual(inlineOf("[OPS-0000]"), [{ type: "idtext", id: "OPS-0000", reason: "not-in-sources" }]);
  assert.deepStrictEqual(inlineOf("[OPS-1001]", ["OPS-1001"], ["OPS-1001"]), [{ type: "idtext", id: "OPS-1001", reason: "unverified" }]);
});
test("sources may be given as objects with ticket_id", () =>
  assert.deepStrictEqual(inlineOf("[OPS-1001]", [{ ticket_id: "OPS-1001", title: "x" }]), [{ type: "chip", id: "OPS-1001" }]));
test("an id inside bold is still classified", () => {
  const n = inlineOf("**see [OPS-1001] and [OPS-7]**");
  assert.strictEqual(flat(n), "**see [OPS-1001] and {OPS-7}**");
});
test("'[OPS-1001' without the closing bracket, bare ids and malformed ids stay plain text", () => {
  for (const s of ["[OPS-1001", "OPS-1001", "[OPS-]", "[OPS-12x]", "[OPS 1001]", "[OPS-1001 ]", "[[OPS-1001", "OPS-1001]", "[OPS-" + "1".repeat(16) + "]"]) {
    assert.deepStrictEqual(inlineOf(s), [{ type: "text", text: s }], s);
  }
});
test("a stray bracket before a real id does not hide the id", () => assert.strictEqual(flat(inlineOf("[x [OPS-1001]")), "[x [OPS-1001]"));
test("chips and muted ids keep their surrounding text in order", () =>
  assert.strictEqual(flat(inlineOf("a [OPS-1001] b [OPS-5] c")), "a [OPS-1001] b {OPS-5} c"));

section("markdown-lite: hostile input stays inert text");
const HOSTILE = [
  "<script>alert(1)</script>",
  "<img src=x onerror=alert(1)>",
  "<a href=\"javascript:alert(1)\">click</a>",
  "javascript:alert(1)",
  "[click me](javascript:alert(1))",
  "<iframe src=\"//evil.example\"></iframe>",
  "\"><svg onload=alert(1)>",
  "&lt;b&gt;already escaped&lt;/b&gt; &amp; <b>raw</b>",
  "</div></p><p>",
  "`<script>`",
];
for (const h of HOSTILE) {
  test("hostile text is kept as literal text: " + JSON.stringify(h).slice(0, 52), () => {
    for (const wrap of [h, "**" + h + "**", "- " + h, "[OPS-1001] " + h + " [OPS-0000]"]) {
      const blocks = app.parseAnswer(wrap, ["OPS-1001"], []);
      checkTree(blocks);
      const types = new Set(allNodes(blocks).map((n) => n.type));
      for (const t of types) assert(["text", "br", "bold", "chip", "idtext"].includes(t), t);
      assert(textOf(blocks).includes(h), "the hostile text must survive unchanged inside text nodes");
      // the only chip is the real one; nothing hostile produced a chip or an id
      assert.strictEqual(allNodes(blocks).filter((n) => n.type === "chip").length, wrap.startsWith("[OPS-1001]") ? 1 : 0);
    }
  });
}
test("hostile text containing ids and markers cannot forge a chip for an unretrieved ticket", () => {
  const blocks = app.parseAnswer("<img src=x onerror=1>[OPS-9999]**[OPS-8888]**", ["OPS-1001"], []);
  assert.strictEqual(allNodes(blocks).filter((n) => n.type === "chip").length, 0);
});
test("no tree node carries markup-like keys or values other than plain strings", () => {
  const blocks = app.parseAnswer("**<b>x</b>** [OPS-1001] <i>y</i>", ["OPS-1001"], []);
  assert(!JSON.stringify(Object.keys(JSON.parse(JSON.stringify(blocks)[0] === "[" ? JSON.stringify(blocks[0]) : "{}"))).includes("html"));
});

section("markdown-lite: speed on large input (50 KB)");
const KB50 = 50 * 1024;
const perf = [
  ["50 KB with one unterminated **bold", "**" + "a".repeat(KB50 - 2), 100],
  ["50 KB of '**a ' repeated (many bold pairs)", "**a ".repeat(KB50 / 4), 100],
  ["50 KB of '**' (asterisks only)", "*".repeat(KB50), 100],
  ["50 KB of '[OPS-' repeated", "[OPS-".repeat(KB50 / 5), 100],
  ["50 KB of '[OPS-1' repeated", "[OPS-1".repeat(Math.floor(KB50 / 6)), 100],
  ["50 KB of '[OPS-1001] ' repeated (10,000 ids)", "[OPS-1001] ".repeat(Math.floor(KB50 / 11)), 150],
  ["50 KB of '**[OPS-1' repeated", "**[OPS-1".repeat(Math.floor(KB50 / 8)), 100],
  ["one id with 50,000 digits and no closing bracket", "[OPS-" + "1".repeat(KB50), 100],
  ["50 KB of bullet lines", "- item\n".repeat(Math.floor(KB50 / 7)), 100],
  ["50 KB of single characters separated by blank lines", "x\n\n".repeat(Math.floor(KB50 / 3)), 150],
  ["50 KB of spaces after a dash", "-" + " ".repeat(KB50), 100],
  ["50 KB of '[' characters", "[".repeat(KB50), 100],
];
for (const [name, text, limit] of perf) {
  test(name + " finishes in under " + limit + " ms", () => {
    app.parseAnswer(text.slice(0, 2000), ["OPS-1001"], []); // warm up
    const best = Math.min(...[1, 2, 3].map(() => timed(() => app.parseAnswer(text, ["OPS-1001"], [])).ms));
    const { value } = timed(() => app.parseAnswer(text, ["OPS-1001"], []));
    checkTree(value);
    console.log("          (" + text.length + " chars, best of 3: " + best.toFixed(1) + " ms)");
    assert(best < limit, "took " + best.toFixed(1) + " ms");
  });
}
test("text is not lost or duplicated on large input (round trip of plain text)", () => {
  const plain = "word ".repeat(10000);
  assert.strictEqual(textOf(app.parseAnswer(plain, [], [])), plain);
});

section("citation classifier");
test("classifyCitation", () => {
  assert.strictEqual(app.classifyCitation("OPS-1001", ["OPS-1001"], []), "chip");
  assert.strictEqual(app.classifyCitation("ops-1001", [{ ticket_id: "OPS-1001" }], []), "chip");
  assert.strictEqual(app.classifyCitation("OPS-0000", ["OPS-1001"], []), "not-in-sources");
  assert.strictEqual(app.classifyCitation("OPS-0000", ["OPS-1001"], ["OPS-0000"]), "unverified");
  assert.strictEqual(app.classifyCitation("OPS-1001", ["OPS-1001"], ["OPS-1001"]), "unverified");
  for (const bad of ["", "abc", "OPS-", "OPS-1x", null, undefined, 5, {}, "OPS-" + "9".repeat(16)]) assert.strictEqual(app.classifyCitation(bad, ["OPS-1001"], []), "invalid");
  assert.strictEqual(app.classifyCitation("OPS-1001", null, undefined), "not-in-sources");
});
test("toIdSet ignores junk and normalises", () =>
  assert.deepStrictEqual([...app.toIdSet(["ops-1", " OPS-2 ", "x", 5, null, { ticket_id: "ops-3" }, "OPS-"])].sort(), ["OPS-1", "OPS-2", "OPS-3"]));

section("duration formatting");
const DUR = [[0, "0 s"], [1, "1 s"], [59, "59 s"], [60, "1 min"], [61, "1 min 1 s"], [3599, "59 min 59 s"], [3600, "1 h"], [3660, "1 h 1 min"],
  [41375, "11 h 29 min"], [42000, "11 h 40 min"], [86400, "24 h"], [0.2, "1 s"], [59.01, "1 min"], [-5, "0 s"], [90000, "25 h"]];
for (const [s, want] of DUR) test("formatDuration(" + s + ") = " + JSON.stringify(want), () => assert.strictEqual(app.formatDuration(s), want));
test("formatDuration of non-numbers is an empty string", () => {
  for (const v of [NaN, Infinity, -Infinity, "5", null, undefined, {}]) assert.strictEqual(app.formatDuration(v), "");
});

section("Retry-After parsing");
const RA = [["41375", 41375], ["0", null], [" 12 ", 12], ["abc", null], ["Wed, 21 Oct 2026 07:28:00 GMT", null], ["-5", null], ["1e3", null], ["86401", 86400],
  ["99999999999", null], ["", null], [null, null], [undefined, null], [34, null], ["3.5", null], ["86400", 86400]];
for (const [v, want] of RA) test("parseRetryAfter(" + JSON.stringify(v) + ") = " + want, () => assert.strictEqual(app.parseRetryAfter(v), want));

section("error describer: every status and code the API uses");
const ds = (input) => app.describeError(input);
test("422 for each part of the request has fixed, friendly wording and no Retry button", () => {
  const cases = [
    [{ detail: [{ type: "string_too_long", loc: ["body", "question"], msg: "<script>x</script>" }] }, /up to 1000 characters/],
    [{ detail: [{ type: "string_too_short", loc: ["body", "question"], msg: "x" }] }, /empty/],
    [{ detail: [{ type: "missing", loc: ["body", "question"], msg: "x" }] }, /did not accept the question/],
    [{ detail: [{ type: "value_error", loc: ["body", "filters"], msg: "x" }] }, /filters/],
    [{ detail: [{ type: "value_error", loc: ["body", "filters", "date_from"], msg: "x" }] }, /real dates/],
    [{ detail: [{ type: "less_than_equal", loc: ["body", "k"], msg: "x" }] }, /between 3 and 10/],
    [{ detail: [{ type: "too_long", loc: ["body", "history"], msg: "x" }] }, /new chat/],
    [{ detail: "not a list" }, /shorten or fix/],
    [null, /shorten or fix/],
    [{ detail: [null, 5, "x", { loc: "nope" }, { loc: [0, null, {}] }] }, /shorten or fix/],
  ];
  for (const [body, re] of cases) {
    const r = ds({ kind: "http", status: 422, body });
    assert.strictEqual(r.title, "Please shorten or fix your question");
    assert(re.test(r.detail), "detail was: " + r.detail);
    assert.strictEqual(r.retryable, false);
    assert.strictEqual(r.retryAfterSeconds, null);
    assert(!/script|<|>/.test(r.detail), "server text must not appear");
  }
});
test("429 with Retry-After: the server's message plus the wait in seconds", () => {
  const r = ds({ kind: "http", status: 429, body: { error: "Gemini's quota for this model is used up; it resets in about 11 hours (HTTP 429 RESOURCE_EXHAUSTED).", code: "rate_limited" }, retryAfter: "41375" });
  assert.strictEqual(r.title, "Request limit reached");
  assert(/resets in about 11 hours/.test(r.detail));
  assert.strictEqual(r.retryAfterSeconds, 41375);
  assert.strictEqual(r.retryable, true);
  assert.strictEqual(r.code, "rate_limited");
});
test("429 without Retry-After: no countdown, Retry stays available", () => {
  const r = ds({ kind: "http", status: 429, body: { error: "Gemini is rate limited; try again in a minute (HTTP 429 RESOURCE_EXHAUSTED).", code: "rate_limited" } });
  assert.strictEqual(r.retryAfterSeconds, null);
  assert.strictEqual(r.retryable, true);
  assert(/try again in a minute/.test(r.detail));
});
test("429 with a bad header or an empty body", () => {
  assert.strictEqual(ds({ kind: "http", status: 429, body: { error: "x", code: "rate_limited" }, retryAfter: "soon" }).retryAfterSeconds, null);
  const r = ds({ kind: "http", status: 429, body: null });
  assert(r.detail.length > 0 && r.retryAfterSeconds === null);
});
test("500 server_misconfigured, 500 internal_error, 502, 503 show the server's message", () => {
  const cases = [
    [500, "server_misconfigured", "GEMINI_API_KEY is not set. Add it to .env (see .env.example).", /not set up/],
    [500, "internal_error", "Internal error", /Server error/],
    [502, "bad_upstream_response", "Gemini returned an empty or blocked response.", /bad response/],
    [503, "upstream_unavailable", "Gemini is having server problems; try again shortly (HTTP 503 UNAVAILABLE).", /unavailable/],
    [503, "upstream_unavailable", "Could not reach Gemini (ConnectError). Check your internet connection.", /unavailable/],
  ];
  for (const [status, code, msg, title] of cases) {
    const r = ds({ kind: "http", status, body: { error: msg, code } });
    assert(title.test(r.title), r.title);
    assert.strictEqual(r.detail, msg);
    assert.strictEqual(r.retryable, true);
    assert.strictEqual(r.retryAfterSeconds, null);
  }
});
test("5xx without a readable body falls back to the status", () => {
  for (const body of [null, {}, { error: 5 }, { error: "" }, "text", []]) {
    const r = ds({ kind: "http", status: 503, body });
    assert.strictEqual(r.detail, "The server returned an error (HTTP 503).");
  }
});
test("other statuses (404, 405) get a generic message with only the number", () => {
  for (const status of [400, 404, 405]) {
    const r = ds({ kind: "http", status, body: { error: "<b>x</b>", code: "not_found" } });
    assert.strictEqual(r.title, "The server could not process the request");
    assert.strictEqual(r.detail, "HTTP " + status + ".");
  }
});
test("network failure, timeout and abort", () => {
  const net = ds({ kind: "network" });
  assert.strictEqual(net.title, "Could not reach the server");
  assert(net.retryable && !net.silent);
  const to = ds({ kind: "timeout" });
  assert.strictEqual(to.title, "Could not reach the server");
  assert(/150 seconds/.test(to.detail) && /may still be working/.test(to.detail));
  const ab = ds({ kind: "abort" });
  assert.strictEqual(ab.silent, true);
  assert.strictEqual(ab.title, "");
});
test("an unreadable response or unknown input", () => {
  for (const input of [{ kind: "invalid" }, { kind: "http" }, { kind: "http", status: "500" }, null, undefined, 5, "x", {}]) {
    const r = ds(input);
    assert.strictEqual(r.title, "Unexpected response");
    assert.strictEqual(r.silent, false);
  }
});
test("server text is cleaned: control characters removed, length capped, markup left as plain text", () => {
  const long = "x".repeat(5000);
  const r = ds({ kind: "http", status: 503, body: { error: "line1\n\u0000\u001bline2 <img src=x onerror=alert(1)>" + long, code: "upstream_unavailable" } });
  assert(r.detail.length <= 500);
  assert(!/[\u0000-\u001f]/.test(r.detail));
  assert(r.detail.startsWith("line1 line2 <img src=x onerror=alert(1)>"));
  assert.strictEqual(typeof r.title, "string");
});
test("every field of every result has the right type", () => {
  const inputs = [{ kind: "network" }, { kind: "timeout" }, { kind: "abort" }, { kind: "invalid" }, { kind: "http", status: 422, body: null },
    { kind: "http", status: 429, body: null, retryAfter: "5" }, { kind: "http", status: 500, body: null }, { kind: "http", status: 404, body: null }];
  for (const i of inputs) {
    const r = ds(i);
    assert(typeof r.title === "string" && typeof r.detail === "string" && typeof r.retryable === "boolean" && typeof r.silent === "boolean" && typeof r.code === "string");
    assert(r.retryAfterSeconds === null || Number.isInteger(r.retryAfterSeconds));
  }
});

section("history: truncation and building");
test("text up to 4000 is unchanged", () => {
  const s = "a".repeat(4000);
  assert.strictEqual(app.truncateForHistory(s), s);
  assert.strictEqual(app.truncateForHistory(""), "");
  assert.strictEqual(app.truncateForHistory(null), "");
});
test("longer text is cut to 4000", () => assert.strictEqual(app.truncateForHistory("a".repeat(9000)).length, 4000));
const lone = (s) => /[\uD800-\uDBFF](?![\uDC00-\uDFFF])|(?<![\uD800-\uDBFF])[\uDC00-\uDFFF]/.test(s);
test("an emoji (surrogate pair) straddling the limit is dropped whole, not split", () => {
  const s = "a".repeat(3999) + "\u{1F600}" + "tail"; // the pair occupies units 3999 and 4000
  const r = app.truncateForHistory(s);
  assert.strictEqual(r, "a".repeat(3999));
  assert(!lone(r));
});
test("an emoji that ends exactly at the limit is kept", () => {
  const s = "a".repeat(3998) + "\u{1F600}" + "tail";
  const r = app.truncateForHistory(s);
  assert.strictEqual(r.length, 4000);
  assert(r.endsWith("\u{1F600}") && !lone(r));
});
test("4000 units of emoji (2000 pairs) is unchanged; 2001 pairs is cut to 2000 pairs", () => {
  assert.strictEqual(app.truncateForHistory("\u{1F600}".repeat(2000)).length, 4000);
  const r = app.truncateForHistory("\u{1F600}".repeat(2001));
  assert.strictEqual(r.length, 4000);
  assert(!lone(r));
});
test("the cut never leaves a lone surrogate, for every position around the limit", () => {
  for (let pad = 3990; pad <= 4001; pad++) {
    const r = app.truncateForHistory("a".repeat(pad) + "\u{1F600}".repeat(5));
    assert(!lone(r), "pad " + pad);
    assert(r.length <= 4000);
  }
});
test("a custom limit works the same way", () => assert.strictEqual(app.truncateForHistory("ab\u{1F600}", 3), "ab"));
test("buildHistory keeps the last 6, in order, as plain {role, content}", () => {
  const turns = [];
  for (let i = 1; i <= 10; i++) turns.push({ role: i % 2 ? "user" : "assistant", content: "m" + i });
  const h = app.buildHistory(turns);
  assert.deepStrictEqual(h.map((t) => t.content), ["m5", "m6", "m7", "m8", "m9", "m10"]);
  assert.deepStrictEqual(Object.keys(h[0]).sort(), ["content", "role"]);
});
test("buildHistory drops invalid roles and empty content, truncates, and does not change its input", () => {
  const turns = [{ role: "system", content: "x" }, { role: "user", content: "   " }, { role: "user" }, null, 5, { role: "assistant", content: "b".repeat(5000), extra: 1 }, { role: "user", content: "ok" }];
  const copy = JSON.parse(JSON.stringify(turns));
  const h = app.buildHistory(turns);
  assert.strictEqual(h.length, 2);
  assert.strictEqual(h[0].content.length, 4000);
  assert.deepStrictEqual(Object.keys(h[0]).sort(), ["content", "role"]);
  assert.deepStrictEqual(turns, copy);
  assert.deepStrictEqual(app.buildHistory("nope"), []);
  assert.deepStrictEqual(app.buildHistory(undefined), []);
});

section("request body");
test("question validation", () => {
  assert.deepStrictEqual(app.validateQuestion("  hi  "), { ok: true, reason: "", trimmed: "hi" });
  for (const v of ["", "   ", "\n\t", null, undefined, 5]) assert.strictEqual(app.validateQuestion(v).reason, "empty");
  assert.strictEqual(app.validateQuestion("a".repeat(1000)).ok, true);
  assert.strictEqual(app.validateQuestion("a".repeat(1001)).reason, "too_long");
  assert.strictEqual(app.validateQuestion(" " + "a".repeat(1000) + " ").ok, true);
});
test("k is clamped to 3..10 and defaults to 6", () => {
  const cases = [[2, 3], [3, 3], [6, 6], [10, 10], [11, 10], ["7", 7], [6.4, 6], [6.6, 7], [NaN, 6], [undefined, 6], [null, 6], ["x", 6], [Infinity, 6], [-4, 3]];
  for (const [v, want] of cases) assert.strictEqual(app.clampK(v), want, String(v));
});
test("the body has only the fields the API accepts, nothing empty", () => {
  const p = app.buildChatPayload({ question: " What is OPS-1001? ", k: 6, filters: {}, history: [] });
  assert.deepStrictEqual(p, { question: "What is OPS-1001?", k: 6 });
  assert.deepStrictEqual(Object.keys(p).sort(), ["k", "question"]);
});
test("filters: only the six known keys, trimmed, empty ones left out", () => {
  const p = app.buildChatPayload({ question: "q", k: 5, filters: { category: " Facilities ", priority: "", status: "   ", team: "Platform Ops", date_from: "2026-01-01", date_to: "", bogus: "x", __proto__: { evil: 1 } } });
  assert.deepStrictEqual(p.filters, { category: "Facilities", team: "Platform Ops", date_from: "2026-01-01" });
});
test("history is the last 6 turns, with content cut to 4000", () => {
  const history = [];
  for (let i = 0; i < 9; i++) history.push({ role: i % 2 ? "assistant" : "user", content: "x".repeat(4100) });
  const p = app.buildChatPayload({ question: "q", k: 6, history });
  assert.strictEqual(p.history.length, 6);
  assert(p.history.every((t) => t.content.length === 4000 && (t.role === "user" || t.role === "assistant")));
});
test("the body satisfies the API's rules (question 1-1000, k 1-15, history up to 12, 1-4000 per turn, allowed keys)", () => {
  const allowed = new Set(["question", "k", "filters", "history"]);
  for (const k of [-1, 0, 3, 6, 10, 99, NaN]) {
    const p = app.buildChatPayload({ question: "  q  ", k, filters: { priority: "P1-Critical" }, history: [{ role: "user", content: "a" }, { role: "assistant", content: "b" }] });
    assert(Object.keys(p).every((key) => allowed.has(key)));
    assert(p.question.length >= 1 && p.question.length <= 1000);
    assert(Number.isInteger(p.k) && p.k >= 1 && p.k <= 15);
    assert(p.history.length <= 12 && p.history.every((t) => t.content.length >= 1 && t.content.length <= 4000));
  }
});
test("garbage input does not throw", () => {
  for (const v of [null, undefined, 5, "x", [], { question: 5, k: {}, filters: 5, history: 5 }]) assert.doesNotThrow(() => app.buildChatPayload(v));
});

section("theme");
test("resolveTheme: a saved choice wins, otherwise the system preference", () => {
  assert.strictEqual(app.resolveTheme("light", true), "light");
  assert.strictEqual(app.resolveTheme("dark", false), "dark");
  assert.strictEqual(app.resolveTheme(null, true), "dark");
  assert.strictEqual(app.resolveTheme(undefined, false), "light");
  for (const junk of ["blue", "", 5, {}, "DARK"]) assert.strictEqual(app.resolveTheme(junk, true), "dark");
});
test("nextTheme and the toggle label (which names the theme it switches to)", () => {
  assert.strictEqual(app.nextTheme("dark"), "light");
  assert.strictEqual(app.nextTheme("light"), "dark");
  assert.strictEqual(app.nextTheme("junk"), "dark");
  assert.strictEqual(app.themeToggleLabel("dark"), "Light mode");
  assert.strictEqual(app.themeToggleLabel("light"), "Dark mode");
});
test("reading and writing the saved theme never throws, even if storage does", () => {
  const mem = {}; const ok = { getItem: (k) => (k in mem ? mem[k] : null), setItem: (k, v) => { mem[k] = v; } };
  assert.strictEqual(app.writeStoredTheme(ok, "dark"), true);
  assert.strictEqual(mem[app.constants.THEME_KEY], "dark");
  assert.strictEqual(app.readStoredTheme(ok), "dark");
  mem[app.constants.THEME_KEY] = "purple";
  assert.strictEqual(app.readStoredTheme(ok), null);
  const bad = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("quota"); } };
  assert.strictEqual(app.readStoredTheme(bad), null);
  assert.strictEqual(app.writeStoredTheme(bad, "dark"), false);
  for (const missing of [undefined, null, {}, 5]) {
    assert.strictEqual(app.readStoredTheme(missing), null);
    assert.strictEqual(app.writeStoredTheme(missing, "dark"), false);
  }
});

console.log("\n" + (failed === 0 ? "ALL PASSED" : "FAILURES") + ": " + passed + " passed, " + failed + " failed");
if (failed) {
  console.log("failed tests:\n  - " + failures.join("\n  - "));
  process.exit(1);
}
