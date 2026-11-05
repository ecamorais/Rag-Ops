"use strict";
// Negative controls for the ticket drawer (Group D): break COPIES of app.js on purpose and confirm test_drawer.js notices each break.
// Usage: [OPS_INDEX_HTML=path] node tests/ui/breaks/mutate_drawer.js [PATH_TO_APP_JS]   (default: static/app.js)
const { once, runBreaks } = require("./harness.js");

const mutations = [
  ["D01 the id is not re-validated", (s) => once(s, "      if (!TICKET_ID_PATTERN.test(id)) return false;\n", "")],
  ["D02 the id pattern is loosened to a prefix", (s) => once(s, "var TICKET_ID_PATTERN = /^OPS-[0-9]+$/;", "var TICKET_ID_PATTERN = /^OPS-/;")],
  ["D03 no stale-response guard", (s) => once(s, "if (token !== state.seq || !state.open) return;", "")],
  ["D04 an older request is not aborted", (s) => once(s, "      if (state.controller) state.controller.abort();\n", "")],
  ["D05 the scroll lock is never released", (s) => once(s, '      doc.body.classList.remove("drawer-open");\n', "")],
  ["D06 the scroll lock is never set", (s) => once(s, '        doc.body.classList.add("drawer-open");\n', "")],
  ["D07 focus does not move to the drawer title", (s) => once(s, "      els.drawerTitle.focus();\n      return true;", "      return true;")],
  ["D08 focus is not given back to the opener", (s) => once(s, "      if (opener && isInPage(doc, opener)) opener.focus();\n", "")],
  ["D09 no fallback to the question box", (s) => once(s, "      if (!opener || doc.activeElement !== opener) els.question.focus();\n", "")],
  ["D10 a removed opener is still used", (s) => once(s, "if (opener && isInPage(doc, opener)) opener.focus();", "if (opener) opener.focus();")],
  ["D11 the 404 wording is wrong", (s) => once(s, '"Ticket " + id + " was not found"', '"Ticket not found"')],
  ["D12 a 404 offers Retry", (s) => once(s, 'was not found", detail: "", retryable: false }', 'was not found", detail: "", retryable: true }')],
  ["D13 errors never offer Retry", (s) => once(s, "    if (desc.retryable) {\n      retry = doc.createElement", "    if (false) {\n      retry = doc.createElement")],
  ["D14 a reply for another ticket is accepted", (s) => once(s, "if (body.ticket_id !== expectedId || typeof body.title", "if (typeof body.title")],
  ["D15 'Not resolved yet' wording changed", (s) => once(s, 'var NOT_RESOLVED_TEXT = "Not resolved yet";', 'var NOT_RESOLVED_TEXT = "Open";')],
  ["D16 'No resolution recorded' wording changed", (s) => once(s, 'var NO_RESOLUTION_TEXT = "No resolution recorded";', 'var NO_RESOLUTION_TEXT = "";')],
  ["D17 the comment author is left out", (s) => once(s, '(c.author || "Unknown") + (c.timestamp', '"" + (c.timestamp')],
  ["D18 no loading state", (s) => once(s, "      setBody(renderDrawerLoading(doc));\n      setBusy(true);", "      setBusy(true);")],
  ["D19 aria-busy is never cleared after a ticket", (s) => once(s, "            setBody(renderTicketBody(doc, ticket));\n            setBusy(false);", "            setBody(renderTicketBody(doc, ticket));")],
  ["D20 the description is set through innerHTML", (s) => once(s, '      p.classList.add("prose");\n      p.textContent = text;', '      p.classList.add("prose");\n      p.innerHTML = text;')],
  ["D21 Esc is blocked", (s) => once(s, 'els.drawer.addEventListener("close", onClosed);', 'els.drawer.addEventListener("close", onClosed);\n    els.drawer.addEventListener("cancel", function (e) { e.preventDefault(); });')],
  ["D22 only the Close button cleans up, Esc does not", (s) => once(s, '      els.drawer.close();\n    });\n    els.drawer.addEventListener("close", onClosed);', "      els.drawer.close();\n      onClosed();\n    });")],
  ["D23 the body scroll is not reset", (s) => once(s, "      els.drawerBody.scrollTop = 0;\n", "")],
  ["D24 opening while open calls showModal again", (s) => once(s, "      if (!state.open) {\n        state.open = true;", "      if (true) {\n        state.open = true;")],
  ["D25 chips are not wired to the drawer", (s) => once(s, "    app.chat.setOpenTicketHandler(app.drawer.open);\n", "")],
  ["D26 the request has no timeout", (s) => once(s, "        timeoutMs: timeout,\n        signal: controller", "        signal: controller")],
  ["D27 the priority has no colour class", (s) => once(s, 'fieldRow(doc, list, "Priority", ticket.priority, priorityClass(ticket.priority));', 'fieldRow(doc, list, "Priority", ticket.priority);')],
  ["D28 Retry does not keep focus in the dialog", (s) => once(s, "          els.drawerTitle.focus(); // the Retry button is gone; keep focus inside the dialog\n", "")],
  ["D29 the drawer title shows only the id", (s) => once(s, "drawerTitleText(id, ticket.title)", "id")],
  ["D30 the id is not trimmed or upper-cased", (s) => once(s, "rawId.trim().toUpperCase()", "rawId")],
  ["D31 tabs are stripped from long text", (s) => once(s, "[\\u0000-\\u0008\\u000b-\\u001f\\u007f]", "[\\u0000-\\u001f\\u007f]")],
];

runBreaks({ suite: "test_drawer.js", mutations, timeoutMs: 120000 });
