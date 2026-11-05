"use strict";
// Negative controls for the chat (Group C): break COPIES of app.js on purpose and confirm test_chat.js notices each break.
// Usage: [OPS_INDEX_HTML=path] node tests/ui/breaks/mutate_chat.js [PATH_TO_APP_JS]   (default: static/app.js)
const { once, runBreaks } = require("./harness.js");

const mutations = [
  ["C01 unretrieved ids become clickable chips", (s) => once(s, "parent.appendChild(renderMutedId(doc, node.id));", "parent.appendChild(renderChip(doc, node.id, onOpen));")],
  ["C02 the 'not in retrieved tickets' marker is missing", (s) => once(s, '    muted.appendChild(textSpan(doc, "chip-flag", NOT_RETRIEVED_MARKER));\n', "")],
  ["C03 the missing-references callout is never shown", (s) => once(s, "if (data.missing.length > 0) {", "if (false) {")],
  ["C04 the 'Not found' label is never shown", (s) => once(s, "    if (!data.found) {\n      bubble.classList.add", "    if (false) {\n      bubble.classList.add")],
  ["C05 a null score does not say 'exact match'", (s) => once(s, ': "exact match";', ': "similarity 0.00";')],
  ["C06 every badge gets the P1 colour", (s) => once(s, '"badge-p" + match[1]', '"badge-p1"')],
  ["C07 every card is marked 'cited'", (s) => once(s, "if (cited) top.appendChild", "if (true) top.appendChild")],
  ["C08 history grows BEFORE the answer arrives", (s) => once(s, "state.pending = { controller: controller, loading: loading };", 'state.pending = { controller: controller, loading: loading };\n      state.history.push({ role: "user", content: job.question });')],
  ["C09 a reply after New chat is not ignored (no epoch check)", (s) => once(s, "if (epoch !== state.epoch) return;", "")],
  ["C10 New chat does not abort the pending request", (s) => once(s, "if (state.pending && state.pending.controller) state.pending.controller.abort();", "")],
  ["C11 New chat keeps the history", (s) => once(s, "      state.history = [];\n      state.lastError = null;\n      countdowns.clear();", "      state.lastError = null;\n      countdowns.clear();")],
  ["C12 a second request may start while one is pending", (s) => once(s, "      if (state.pending) return false;\n      var question", "      var question")],
  ["C13 a date problem does not disable Send", (s) => once(s, "|| !!app.state.problem;\n    }", ";\n    }")],
  ["C14 a quota countdown disables Send", (s) => once(s, "els.send.disabled = !!state.pending ||", "els.send.disabled = countdowns.size() > 0 || !!state.pending ||")],
  ["C15 Retry uses the filters as they are NOW, not the snapshot", (s) => once(s, "filters: job.filters, history: state.history", "filters: app.snapshot().filters, history: state.history")],
  ["C16 a non-retryable error (422) still gets Retry", (s) => once(s, "    if (desc.retryable) {\n      var retry", "    if (true) {\n      var retry")],
  ["C17 the countdown counts ticks, not the absolute end time", (s) => once(s, "var seconds = Math.ceil((entry.endsAt - now()) / 1000);", "entry.n = (entry.n === undefined ? Math.ceil((entry.endsAt - now()) / 1000) : entry.n) - 1;\n        var seconds = entry.n;")],
  ["C18 Retry stays disabled after the countdown", (s) => once(s, "            parts.retry.disabled = false;\n", "")],
  ["C19 Shift+Enter sends too", (s) => once(s, "event.key !== \"Enter\" || event.shiftKey || event.isComposing", "event.key !== \"Enter\" || event.isComposing")],
  ["C20 Enter during IME composition sends", (s) => once(s, " || event.isComposing || event.keyCode === 229", "")],
  ["C21 the counter starts at 800", (s) => once(s, "var COUNTER_FROM = 900;", "var COUNTER_FROM = 800;")],
  ["C22 neither the user's message nor the loading bubble scrolls", (s) => once(once(s, "appendToThread(renderUserMessage(doc, question.trimmed), true);", "appendToThread(renderUserMessage(doc, question.trimmed), false);"), "appendToThread(loading, true);", "appendToThread(loading, false);")],
  ["C23 an answer always yanks the reader down", (s) => once(s, "      els.thread.appendChild(newNode);\n      followThread(wasNear);", "      els.thread.appendChild(newNode);\n      followThread(true);")],
  ["C24 example buttons only fill, they do not send", (s) => once(s, "        refreshComposer();\n        submit();\n      });\n    });", "        refreshComposer();\n      });\n    });")],
  ["C25 the user message is set through innerHTML", (s) => once(s, "    bubble.textContent = text;\n    box.appendChild(bubble);\n    return box;\n  }\n\n  // A cited", "    bubble.innerHTML = text;\n    box.appendChild(bubble);\n    return box;\n  }\n\n  // A cited")],
  ["C26 the sources section starts open", (s) => once(s, 'details.classList.add("sources");', 'details.classList.add("sources");\n    details.setAttribute("open", "");')],
  ["C27 the error bubble has no alert role", (s) => once(s, '    bubble.setAttribute("role", "alert");\n', "")],
  ["C28 the ticking countdown is announced", (s) => once(s, '      timer.setAttribute("aria-live", "off"); // ticking every second must not be read out\n', "")],
  ["C29 the chat timeout is not passed to the request", (s) => once(s, "        timeoutMs: chatTimeout,\n", "")],
  ["C30 the abort signal is not passed to the request", (s) => once(s, "timeoutMs: chatTimeout,\n        signal: controller ? controller.signal : undefined", "timeoutMs: chatTimeout,\n        signal: undefined")],
  ["C31 an old error keeps its Retry when a new question is sent", (s) => once(s, "      retireError();\n      appendToThread(renderUserMessage", "      appendToThread(renderUserMessage")],
  ["C32 history is not capped", (s) => once(s, "var HISTORY_KEEP = 12;", "var HISTORY_KEEP = 1000;")],
  ["C33 filters are not sent with the question", (s) => once(s, "startRequest({ question: question.trimmed, k: shot.k, filters: shot.filters });", "startRequest({ question: question.trimmed, k: shot.k, filters: {} });")],
  ["C34 the loading bubble is left behind after the answer", (s) => once(s, "      removeFromThread(oldNode);\n      els.thread.appendChild(newNode);", "      els.thread.appendChild(newNode);")],
  ["C35 Retry leaves the old error bubble in the thread", (s) => once(s, "          removeFromThread(parts.node);\n          startRequest(job);", "          startRequest(job);")],
  ["C36 the whole untrimmed text is sent", (s) => once(s, "      appendToThread(renderUserMessage(doc, question.trimmed), true);\n      els.question.value = \"\";", "      appendToThread(renderUserMessage(doc, question.trimmed), true);\n      var raw = els.question.value;\n      els.question.value = \"\";\n      question.trimmed = raw;")],
  ["C37 the empty state is not restored by New chat", (s) => once(s, "      els.thread.appendChild(els.emptyState);\n      els.jumpLatest.hidden = true;\n      if (app.state.sidebarOpen)", "      els.jumpLatest.hidden = true;\n      if (app.state.sidebarOpen)")],
  ["C38 New chat leaves running countdowns", (s) => once(s, "      state.lastError = null;\n      countdowns.clear();\n      clearChildren", "      state.lastError = null;\n      clearChildren")],
];

runBreaks({ suite: "test_chat.js", mutations, timeoutMs: 120000 });
