"use strict";
// Negative controls for the inert slide-over panel: break COPIES of app.js on purpose and confirm test_inert.js notices each break.
// Usage: [OPS_INDEX_HTML=path] node tests/ui/breaks/mutate_inert.js [PATH_TO_APP_JS]   (default: static/app.js)
const { once, runBreaks } = require("./harness.js");

const mutations = [
  ["I1 the header is not made inert", (s) => once(s, "    setInert(els.appHeader, open);\n", "")],
  ["I2 the main area is not made inert", (s) => once(s, "    setInert(els.mainArea, open);\n", "")],
  ["I3 inert is never removed", (s) => once(s, 'else element.removeAttribute("inert");', "")],
  ["I4 the panel itself is made inert too", (s) => once(s, "    setInert(els.mainArea, open);\n", "    setInert(els.mainArea, open);\n    setInert(els.sidebar, open);\n")],
  ["I5 focus goes back to the toggle BEFORE inert is removed", (s) => once(s, "      state.sidebarOpen = false;\n      renderSidebar(els, false);\n      if (wasOpen && returnFocus) els.sidebarToggle.focus();", "      state.sidebarOpen = false;\n      if (wasOpen && returnFocus) els.sidebarToggle.focus();\n      renderSidebar(els, false);")],
  ["I6 a wrong attribute name is used", (s) => once(s, 'if (on) element.setAttribute("inert", "");', 'if (on) element.setAttribute("aria-hidden", "true");')],
];

runBreaks({ suite: "test_inert.js", mutations, timeoutMs: 120000 });
