"use strict";
// Negative controls for the DOM logic (Group B): break COPIES of app.js on purpose and confirm test_dom.js notices each break.
// Usage: [OPS_INDEX_HTML=path] node tests/ui/breaks/mutate_dom.js [PATH_TO_APP_JS]   (default: static/app.js)
const { once, runBreaks } = require("./harness.js");

const mutations = [
  ["B1 a dropdown is emptied with innerHTML", (s) => once(s, "clearChildren(select);\n    var any", 'select.innerHTML = "";\n    var any')],
  ["B2 a value is put into an onclick attribute", (s) => once(s, "option.textContent = values[i];", 'option.setAttribute("onclick", values[i]);\n      option.textContent = values[i];')],
  ["B3 a one-day range (start = end) is called a problem", (s) => once(s, 'f !== "" && t !== "" && f > t', 'f !== "" && t !== "" && f >= t')],
  ["B4 the end date is not limited by the start date", (s) => once(s, "toMin: isRealDate(from) ? from : lo", "toMin: isRealDate(to) ? to : lo")],
  ["B5 Escape does not close the sidebar", (s) => once(s, 'if (event.key === "Escape" && state.sidebarOpen) closeSidebar(true);', "")],
  ["B6 the theme choice is not saved", (s) => once(s, "writeStoredTheme(storage, next);", "")],
  ["B7 the request timeout never fires", (s) => once(s, "timer = setTimeout(function () {\n        timedOut = true;\n        controller.abort();\n      }, opts.timeoutMs);", "timer = null;")],
  ["B8 the Retry-After header is dropped", (s) => once(s, 'var retryAfter = response.headers && typeof response.headers.get === "function" ? response.headers.get("retry-after") : null;', "var retryAfter = null;")],
  ["B9 the chip's remove button does nothing", (s) => once(s, "onRemove(item.key);", "")],
  ["B10 an unreadable /health is not marked as a problem", (s) => once(s, 'return { text: "Status unavailable", degraded: true };', 'return { text: "Status unavailable", degraded: false };')],
  ["B11 non-string filter values are kept", (s) => once(s, 'if (typeof v !== "string") continue;\n', "")],
  ["B12 the sidebar is moved with a .style assignment", (s) => once(s, 'els.sidebar.classList.toggle("is-open", open);', 'els.sidebar.classList.toggle("is-open", open);\n    els.sidebar.style.transform = open ? "none" : "";')],
  ["B13 opening the sidebar does not move focus into it", (s) => once(s, "renderSidebar(els, true);\n      els.sidebarClose.focus();", "renderSidebar(els, true);")],
  ["B14 'Clear filters' leaves the dropdowns as they were", (s) => once(s, "state.selected = emptySelection();\n      syncControls();", "state.selected = emptySelection();")],
  ["B15 the corrected k is not written back to the box", (s) => once(s, "state.k = clampK(raw);\n      els.kInput.value = String(state.k);", "state.k = clampK(raw);")],
  ["B16 an outside abort is reported as a network error", (s) => once(s, 'if (external && external.aborted) return { kind: "abort" };\n        return { kind: "network" };', 'return { kind: "network" };')],
  ["B17 the timer is not cleared after an answer", (s) => once(s, "if (timer !== null) clearTimeout(timer);", "")],
  ["B18 a saved theme is not applied at start", (s) => once(s, 'if (stored) doc.documentElement.setAttribute("data-theme", stored);', "")],
  ["B19 the date limits ignore the data range", (s) => once(s, 'var lo = isRealDate(createdMin) ? createdMin : "";', 'var lo = "";')],
  ["B20 a failed /filters leaves the notice hidden", (s) => once(s, 'renderFiltersNotice(els, "Filters are unavailable right now. You can still ask questions.");', 'renderFiltersNotice(els, "");')],
];

runBreaks({ suite: "test_dom.js", mutations, timeoutMs: 180000 });
