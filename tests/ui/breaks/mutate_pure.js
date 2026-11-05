"use strict";
// Negative controls for the pure functions (Group A): break COPIES of app.js on purpose and confirm test_pure.js notices each break.
// Usage: [OPS_INDEX_HTML=path] node tests/ui/breaks/mutate_pure.js [PATH_TO_APP_JS]   (default: static/app.js)
const { once, runBreaks } = require("./harness.js");

const mutations = [
  ["M1 an innerHTML assignment is added", (s) => once(s, "var out = [];\n    var n = text.length;", "var out = [];\n    var n = text.length; document.body.innerHTML = text;")],
  ["M2 the parser is made slow (a real O(n^2) scan that cannot be optimised away)", (s) => once(s, "var ctx = { sources: toIdSet(sourceIds)", "var sink = 0; for (var a = 0; a < text.length; a++) { sink += text.slice(a).split('').length; }\n    if (sink === 12345.5) text = '';\n    var ctx = { sources: toIdSet(sourceIds)")],
  ["M3 surrogate-pair protection is removed", (s) => once(s, "return last >= 0xd800 && last <= 0xdbff ? cut.slice(0, -1) : cut;", "return cut;")],
  ["M4 an unverified id still becomes a chip if it is in the sources", (s) => once(s, 'if (unverified.has(norm)) return "unverified";\n    return sources.has(norm) ? "chip" : "not-in-sources";', 'if (sources.has(norm)) return "chip";\n    return unverified.has(norm) ? "unverified" : "not-in-sources";')],
  ["M5 the 422 message includes text from the server", (s) => once(s, 'else result.detail = "The server did not accept the request. Please shorten or fix the question.";', 'else result.detail = "The server did not accept the request. " + JSON.stringify(body);')],
  ["M6 the request body gets an extra field the API would reject", (s) => once(s, "var payload = { question: validateQuestion(src.question).trimmed, k: clampK(src.k) };", "var payload = { question: validateQuestion(src.question).trimmed, k: clampK(src.k), stream: false };")],
  ["M7 Retry-After accepts any number", (s) => once(s, "if (!/^\\d{1,7}$/.test(v)) return null;", "if (!/^\\d+$/.test(v)) return null;")],
  ["M8 an unterminated ** swallows the rest as bold", (s) => once(s, "nextBold = -1;\n          continue;\n        }", "close = n;\n        }")],
  ["M9 duration: the one-hour boundary is wrong", (s) => once(s, "if (s < 3600) {", "if (s < 3601) {")],
  ["M10 history keeps the first 6 instead of the last 6", (s) => once(s, "return clean.slice(-max);", "return clean.slice(0, max);")],
  ["M11 the 'javascript:' scheme appears in the source", (s) => once(s, "var BULLET =", "var UNUSED = 'javascript:void(0)';\n  var BULLET =")],
  ["M12 a bold node may contain a raw html field", (s) => once(s, 'out.push({ type: "bold", children: parseInline(inner, ctx, false) });', 'out.push({ type: "bold", children: parseInline(inner, ctx, false), html: inner });')],
];

runBreaks({ suite: "test_pure.js", mutations, timeoutMs: 120000 });
