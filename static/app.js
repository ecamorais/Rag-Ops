"use strict";

/*
  Ops Ticket Assistant: browser script.

  Group A: pure functions. They take plain data and return plain data and never touch the page.
  Group B: the API layer, page state, header, theme, filters and sidebar.
  Group C: the chat thread, composer, answers with sources, errors with Retry and a countdown.
  Group D: the ticket drawer.
  The helpers of groups B, C and D take the document or elements as arguments, so the Node tests run them
  with a fake DOM; the start-up code only runs in a browser. Everything is exported for the Node tests
  when `module` exists, so this file also loads in Node with no DOM.

  Text from the API, from tickets and from people is untrusted. The parser below only produces a
  small tree of plain nodes (text, br, bold, chip, idtext). Turning that tree into page elements
  happens later with createElement and textContent only.
*/
(function () {
  // ---------- limits that mirror the API (app/schemas.py) ----------
  var MAX_QUESTION = 1000;
  var MAX_HISTORY_CONTENT = 4000;
  var HISTORY_TURNS = 6;
  var K_MIN = 3;
  var K_MAX = 10;
  var K_DEFAULT = 6;
  var REQUEST_TIMEOUT_MS = 150000;
  var MAX_RETRY_AFTER = 86400;
  var MAX_ERROR_TEXT = 500;
  var THEME_KEY = "ops-theme";
  var FILTER_KEYS = ["category", "priority", "status", "team", "date_from", "date_to"];

  // ---------- ticket ids ----------
  // A bracketed id in an answer. Sticky, so each candidate costs only the length of its own digits.
  var BRACKET_ID = /\[(OPS-\d{1,15})\]/iy;
  var PLAIN_ID = /^OPS-\d{1,15}$/;

  function normaliseId(value) {
    if (typeof value !== "string") return null;
    var id = value.trim().toUpperCase();
    return PLAIN_ID.test(id) ? id : null;
  }

  function toIdSet(list) {
    var set = new Set();
    if (list == null) return set;
    var items = typeof list[Symbol.iterator] === "function" && typeof list !== "string" ? Array.from(list) : [];
    for (var i = 0; i < items.length; i++) {
      var item = items[i];
      var id = normaliseId(item && typeof item === "object" ? item.ticket_id : item);
      if (id) set.add(id);
    }
    return set;
  }

  // "chip": clickable (in the retrieved sources and not reported as unverified).
  // "unverified": the server reported it as not in the context. "not-in-sources": cited but not retrieved.
  function classifyCitation(id, sourceIds, unverifiedIds) {
    var norm = normaliseId(id);
    if (!norm) return "invalid";
    var sources = sourceIds instanceof Set ? sourceIds : toIdSet(sourceIds);
    var unverified = unverifiedIds instanceof Set ? unverifiedIds : toIdSet(unverifiedIds);
    if (unverified.has(norm)) return "unverified";
    return sources.has(norm) ? "chip" : "not-in-sources";
  }

  // ---------- markdown-lite ----------
  // Supported: paragraphs, line breaks, "- " or "* " bullet lines, **bold**, [OPS-1234] ids.
  // Everything else (headings, italics, code, links, images, tables, HTML) stays literal text.

  function pushText(out, str) {
    if (str === "") return;
    var parts = str.split("\n");
    for (var i = 0; i < parts.length; i++) {
      if (i > 0) out.push({ type: "br" });
      if (parts[i] !== "") out.push({ type: "text", text: parts[i] });
    }
  }

  function parseInline(text, ctx, allowBold) {
    var out = [];
    var n = text.length;
    var start = 0; // beginning of the text not yet emitted
    var pos = 0; // where scanning continues
    // Next "**" and next "[" at or after pos, or -1 when there is none. A -1 is never searched again,
    // so the whole scan stays linear however many candidates the text has.
    var nextBold = allowBold ? text.indexOf("**") : -1;
    var nextBracket = text.indexOf("[");
    for (;;) {
      if (nextBold !== -1 && nextBold < pos) nextBold = text.indexOf("**", pos);
      if (nextBracket !== -1 && nextBracket < pos) nextBracket = text.indexOf("[", pos);
      if (nextBold === -1 && nextBracket === -1) break;

      if (nextBold !== -1 && (nextBracket === -1 || nextBold <= nextBracket)) {
        var close = text.indexOf("**", nextBold + 2);
        if (close === -1) {
          // An opener with no closer. No later "**" can close either, so stop looking for bold.
          nextBold = -1;
          continue;
        }
        var inner = text.slice(nextBold + 2, close);
        if (inner.trim() === "") {
          pos = nextBold + 2; // "****" or "** **": keep as plain text
          continue;
        }
        pushText(out, text.slice(start, nextBold));
        out.push({ type: "bold", children: parseInline(inner, ctx, false) });
        start = pos = close + 2;
      } else {
        BRACKET_ID.lastIndex = nextBracket;
        var m = BRACKET_ID.exec(text);
        if (!m) {
          pos = nextBracket + 1; // "[OPS-1" without the closing bracket, or any other "[": plain text
          continue;
        }
        pushText(out, text.slice(start, nextBracket));
        var id = m[1].toUpperCase();
        var kind = classifyCitation(id, ctx.sources, ctx.unverified);
        out.push(kind === "chip" ? { type: "chip", id: id } : { type: "idtext", id: id, reason: kind });
        start = pos = nextBracket + m[0].length;
      }
    }
    pushText(out, text.slice(start, n));
    return out;
  }

  var BULLET = /^ {0,3}[-*] +(\S.*)$/;

  // Returns an array of blocks: {type:"p", inline:[...]} or {type:"ul", items:[[...inline], ...]}.
  function parseAnswer(text, sourceIds, unverifiedIds) {
    if (typeof text !== "string" || text === "") return [];
    var ctx = { sources: toIdSet(sourceIds), unverified: toIdSet(unverifiedIds) };
    var lines = text.replace(/\r\n?/g, "\n").split("\n");
    var blocks = [];
    var para = [];
    var list = null;

    function flushParagraph() {
      if (para.length === 0) return;
      blocks.push({ type: "p", inline: parseInline(para.join("\n"), ctx, true) });
      para = [];
    }

    for (var i = 0; i < lines.length; i++) {
      var line = lines[i];
      var m = BULLET.exec(line);
      if (m) {
        flushParagraph();
        if (!list) {
          list = { type: "ul", items: [] };
          blocks.push(list);
        }
        list.items.push(parseInline(m[1], ctx, true));
        continue;
      }
      list = null;
      if (line.trim() === "") {
        flushParagraph();
        continue;
      }
      para.push(line);
    }
    flushParagraph();
    return blocks;
  }

  // ---------- durations ----------
  // Whole seconds, rounded up. Under a minute "S s"; under an hour "M min S s"; from an hour "H h M min".
  function formatDuration(seconds) {
    if (typeof seconds !== "number" || !isFinite(seconds)) return "";
    var s = Math.max(0, Math.ceil(seconds));
    if (s < 60) return s + " s";
    if (s < 3600) {
      var m = Math.floor(s / 60);
      var r = s % 60;
      return r ? m + " min " + r + " s" : m + " min";
    }
    var h = Math.floor(s / 3600);
    var mm = Math.floor((s % 3600) / 60);
    return mm ? h + " h " + mm + " min" : h + " h";
  }

  // The Retry-After header as whole seconds, or null. The server sends integers, capped at one day.
  function parseRetryAfter(value) {
    if (typeof value !== "string") return null;
    var v = value.trim();
    if (!/^\d{1,7}$/.test(v)) return null;
    var n = parseInt(v, 10);
    if (n <= 0) return null;
    return Math.min(n, MAX_RETRY_AFTER);
  }

  // ---------- errors ----------
  function cleanServerText(value) {
    if (typeof value !== "string") return "";
    var text = value.replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
    return text.length > MAX_ERROR_TEXT ? text.slice(0, MAX_ERROR_TEXT - 1) + "…" : text;
  }

  function areaOf422(body) {
    var detail = body && typeof body === "object" && Array.isArray(body.detail) ? body.detail : [];
    var found = { question: null, filters: false, history: false, k: false };
    for (var i = 0; i < detail.length; i++) {
      var item = detail[i];
      var loc = item && typeof item === "object" && Array.isArray(item.loc) ? item.loc : [];
      var type = item && typeof item.type === "string" ? item.type : "";
      for (var j = 0; j < loc.length; j++) {
        var part = loc[j];
        if (part === "question") found.question = type === "string_too_long" ? "long" : type === "string_too_short" ? "short" : "other";
        else if (part === "filters") found.filters = true;
        else if (part === "history") found.history = true;
        else if (part === "k") found.k = true;
      }
    }
    return found;
  }

  // Input: {kind: "http"|"network"|"timeout"|"abort"|"invalid", status, body, retryAfter}
  // Output: {title, detail, retryable, retryAfterSeconds, silent, code}. Only fixed wording is used for 422;
  // for other server errors the server's own short message is shown (as text, never as markup).
  function describeError(input) {
    var e = input && typeof input === "object" ? input : {};
    var body = e.body && typeof e.body === "object" ? e.body : null;
    var message = body ? cleanServerText(body.error) : "";
    var code = body && typeof body.code === "string" ? body.code : "";
    var result = { title: "", detail: "", retryable: true, retryAfterSeconds: null, silent: false, code: code };

    if (e.kind === "abort") {
      result.silent = true;
      return result;
    }
    if (e.kind === "network") {
      result.title = "Could not reach the server";
      result.detail = "Check that the server is running and your connection works, then try again.";
      return result;
    }
    if (e.kind === "timeout") {
      result.title = "Could not reach the server";
      result.detail = "No answer arrived within " + Math.round(REQUEST_TIMEOUT_MS / 1000) +
        " seconds, so the request was stopped. The server may still be working on it. Try again in a moment.";
      return result;
    }
    if (e.kind !== "http" || typeof e.status !== "number") {
      result.title = "Unexpected response";
      result.detail = "The server sent a response this page could not read.";
      return result;
    }

    var status = e.status;
    if (status === 422) {
      var area = areaOf422(body);
      result.title = "Please shorten or fix your question";
      result.retryable = false;
      if (area.question === "long") result.detail = "Questions can be up to " + MAX_QUESTION + " characters. Please shorten it.";
      else if (area.question === "short") result.detail = "The question is empty. Please type a question.";
      else if (area.question) result.detail = "The server did not accept the question. Please change it and try again.";
      else if (area.filters) result.detail = "Please check the filters, especially the dates: they must be real dates, with the start not after the end.";
      else if (area.k) result.detail = "The number of results per question must be between " + K_MIN + " and " + K_MAX + ".";
      else if (area.history) result.detail = "The conversation could not be sent. Start a new chat and try again.";
      else result.detail = "The server did not accept the request. Please shorten or fix the question.";
      return result;
    }
    if (status === 429) {
      result.title = "Request limit reached";
      result.detail = message || "The model is rate limited right now. Please try again later.";
      result.retryAfterSeconds = parseRetryAfter(e.retryAfter);
      return result;
    }
    if (status >= 500) {
      if (code === "server_misconfigured") result.title = "The server is not set up for this";
      else if (status === 502 || code === "bad_upstream_response") result.title = "The model service sent a bad response";
      else if (status === 503 || code === "upstream_unavailable") result.title = "The model service is unavailable";
      else result.title = "Server error";
      result.detail = message || "The server returned an error (HTTP " + status + ").";
      return result;
    }
    result.title = "The server could not process the request";
    result.detail = "HTTP " + status + ".";
    return result;
  }

  // ---------- history and the request body ----------
  // Cut to at most `max` UTF-16 units without leaving half of an emoji (a lone high surrogate) at the end.
  function truncateForHistory(text, max) {
    var limit = typeof max === "number" ? max : MAX_HISTORY_CONTENT;
    if (typeof text !== "string") return "";
    if (text.length <= limit) return text;
    var cut = text.slice(0, limit);
    var last = cut.charCodeAt(cut.length - 1);
    return last >= 0xd800 && last <= 0xdbff ? cut.slice(0, -1) : cut;
  }

  // The last `limit` turns as {role, content}, each content cut to 4000. Invalid or empty turns are dropped.
  function buildHistory(turns, limit) {
    if (!Array.isArray(turns)) return [];
    var max = typeof limit === "number" ? limit : HISTORY_TURNS;
    var clean = [];
    for (var i = 0; i < turns.length; i++) {
      var t = turns[i];
      if (!t || (t.role !== "user" && t.role !== "assistant")) continue;
      var content = truncateForHistory(typeof t.content === "string" ? t.content : "");
      if (content.trim() === "") continue;
      clean.push({ role: t.role, content: content });
    }
    return clean.slice(-max);
  }

  function validateQuestion(text) {
    var trimmed = typeof text === "string" ? text.trim() : "";
    if (trimmed === "") return { ok: false, reason: "empty", trimmed: "" };
    if (trimmed.length > MAX_QUESTION) return { ok: false, reason: "too_long", trimmed: trimmed };
    return { ok: true, reason: "", trimmed: trimmed };
  }

  function clampK(value) {
    var n = typeof value === "number" ? value : parseFloat(value);
    if (!isFinite(n)) return K_DEFAULT;
    return Math.min(K_MAX, Math.max(K_MIN, Math.round(n)));
  }

  function cleanFilters(filters) {
    var out = {};
    if (!filters || typeof filters !== "object") return out;
    for (var i = 0; i < FILTER_KEYS.length; i++) {
      var v = filters[FILTER_KEYS[i]];
      if (typeof v === "string" && v.trim() !== "") out[FILTER_KEYS[i]] = v.trim();
    }
    return out;
  }

  // Exactly the fields the API accepts (it rejects unknown ones). Empty filters and history are left out.
  function buildChatPayload(input) {
    var src = input && typeof input === "object" ? input : {};
    var payload = { question: validateQuestion(src.question).trimmed, k: clampK(src.k) };
    var filters = cleanFilters(src.filters);
    if (Object.keys(filters).length > 0) payload.filters = filters;
    var history = buildHistory(src.history);
    if (history.length > 0) payload.history = history;
    return payload;
  }

  // ---------- theme ----------
  function isTheme(value) {
    return value === "light" || value === "dark";
  }

  function resolveTheme(stored, systemPrefersDark) {
    return isTheme(stored) ? stored : systemPrefersDark ? "dark" : "light";
  }

  function nextTheme(current) {
    return current === "dark" ? "light" : "dark";
  }

  // The label of the toggle names the theme it switches TO.
  function themeToggleLabel(current) {
    return current === "dark" ? "Light mode" : "Dark mode";
  }

  // Storage can be missing or throw (private windows, blocked site data): never let that break the page.
  function readStoredTheme(storage) {
    try {
      var value = storage.getItem(THEME_KEY);
      return isTheme(value) ? value : null;
    } catch (err) {
      return null;
    }
  }

  function writeStoredTheme(storage, theme) {
    try {
      storage.setItem(THEME_KEY, theme);
      return true;
    } catch (err) {
      return false;
    }
  }

  // =====================================================================================================
  // Group B: API layer, page state, header, theme, filters, active-filter chips, k control, sidebar.
  // The rendering helpers take the document (or elements) as arguments, so the Node tests can run them with a
  // small fake DOM. They build page elements only with createElement, textContent and setAttribute on fixed
  // names, and they change how things look only through classes and the hidden attribute.
  // =====================================================================================================

  var HEALTH_TIMEOUT_MS = 10000;
  var FILTERS_TIMEOUT_MS = 10000;
  var NARROW_QUERY = "(max-width: 800px)";
  var DARK_QUERY = "(prefers-color-scheme: dark)";
  var MAX_FILTER_VALUE = 100; // the API rejects longer filter values
  var MAX_FILTER_VALUES = 200;

  var FILTER_FIELDS = [
    { key: "category", label: "Category" },
    { key: "priority", label: "Priority" },
    { key: "status", label: "Status" },
    { key: "team", label: "Team" }
  ];
  var DATE_FIELDS = [
    { key: "date_from", label: "Created from" },
    { key: "date_to", label: "Created to" }
  ];

  // name used in this script -> id in index.html
  var ELEMENT_IDS = {
    sidebarToggle: "sidebar-toggle",
    indexStatus: "index-status",
    themeToggle: "theme-toggle",
    sidebar: "sidebar",
    sidebarClose: "sidebar-close",
    sidebarBackdrop: "sidebar-backdrop",
    filtersNotice: "filters-notice",
    filtersReload: "filters-reload",
    category: "filter-category",
    priority: "filter-priority",
    status: "filter-status",
    team: "filter-team",
    date_from: "filter-date-from",
    date_to: "filter-date-to",
    dateError: "date-error",
    kInput: "k-input",
    clearFilters: "clear-filters",
    activeFilters: "active-filters",
    appHeader: "app-header",
    mainArea: "main-area",
    thread: "thread",
    emptyState: "empty-state",
    jumpLatest: "jump-latest",
    composer: "composer",
    question: "question",
    send: "send",
    charCount: "char-count",
    newChat: "new-chat",
    drawer: "drawer",
    drawerTitle: "drawer-title",
    drawerClose: "drawer-close",
    drawerBody: "drawer-body"
  };

  // ---------- HTTP ----------
  // One JSON request. It never throws; it resolves to one of
  //   {kind: "http", status, body, retryAfter}   a response arrived (body is null when it was not JSON)
  //   {kind: "invalid", status}                  a 2xx response that was not JSON
  //   {kind: "timeout"} | {kind: "abort"} | {kind: "network"}
  // These are the inputs describeError() takes. options: {method, body, timeoutMs, signal}.
  async function requestJson(fetchImpl, url, options) {
    var opts = options || {};
    var external = opts.signal || null;
    if (external && external.aborted) return { kind: "abort" };
    var controller = typeof AbortController === "function" ? new AbortController() : null;
    var timedOut = false;
    var timer = null;
    var onExternalAbort = function () {
      if (controller) controller.abort();
    };
    if (external) external.addEventListener("abort", onExternalAbort);
    if (controller && opts.timeoutMs > 0) {
      timer = setTimeout(function () {
        timedOut = true;
        controller.abort();
      }, opts.timeoutMs);
    }
    var init = { method: opts.method || "GET", headers: { Accept: "application/json" } };
    if (controller) init.signal = controller.signal;
    if (opts.body !== undefined) {
      init.body = JSON.stringify(opts.body);
      init.headers["Content-Type"] = "application/json";
    }
    try {
      var response;
      try {
        response = await fetchImpl(url, init);
      } catch (err) {
        if (timedOut) return { kind: "timeout" };
        if (external && external.aborted) return { kind: "abort" };
        return { kind: "network" };
      }
      var text;
      try {
        text = await response.text();
      } catch (err) {
        if (timedOut) return { kind: "timeout" };
        if (external && external.aborted) return { kind: "abort" };
        return { kind: "invalid", status: response.status };
      }
      var body = null;
      try {
        body = JSON.parse(text);
      } catch (err) {
        body = null;
      }
      var retryAfter = response.headers && typeof response.headers.get === "function" ? response.headers.get("retry-after") : null;
      if (response.ok && body === null) return { kind: "invalid", status: response.status };
      return { kind: "http", status: response.status, body: body, retryAfter: retryAfter };
    } finally {
      if (timer !== null) clearTimeout(timer);
      if (external) external.removeEventListener("abort", onExternalAbort);
    }
  }

  // ---------- health ----------
  function normaliseHealth(body) {
    if (!body || typeof body !== "object") return null;
    if (body.status !== "ok" && body.status !== "degraded") return null;
    if (!Number.isInteger(body.indexed_tickets) || body.indexed_tickets < 0) return null;
    return { status: body.status, indexed: body.indexed_tickets };
  }

  function formatCount(n) {
    return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
  }

  // The text in the header and whether to mark it as a problem. `info` is null when /health could not be read.
  function describeHealth(info) {
    if (!info) return { text: "Status unavailable", degraded: true };
    if (info.status === "ok" && info.indexed > 0) {
      return { text: formatCount(info.indexed) + (info.indexed === 1 ? " ticket indexed" : " tickets indexed"), degraded: false };
    }
    return { text: info.indexed === 0 ? "Degraded: no tickets are indexed" : "Degraded: the server reports a problem", degraded: true };
  }

  // ---------- filter options and the date rules ----------
  function emptyFilterOptions() {
    return { category: [], priority: [], status: [], team: [], createdMin: "", createdMax: "" };
  }

  function cleanValueList(list) {
    var out = [];
    if (!Array.isArray(list)) return out;
    for (var i = 0; i < list.length && out.length < MAX_FILTER_VALUES; i++) {
      var v = list[i];
      if (typeof v !== "string") continue;
      v = v.trim();
      if (v === "" || v.length > MAX_FILTER_VALUE || out.indexOf(v) !== -1) continue;
      out.push(v);
    }
    return out;
  }

  function isRealDate(value) {
    if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
    var d = new Date(value + "T00:00:00Z");
    return !isNaN(d.getTime()) && d.toISOString().slice(0, 10) === value;
  }

  // The body of GET /filters, checked and cleaned. null when it is not an object at all.
  function normaliseFilterOptions(body) {
    if (!body || typeof body !== "object" || Array.isArray(body)) return null;
    var out = emptyFilterOptions();
    for (var i = 0; i < FILTER_FIELDS.length; i++) out[FILTER_FIELDS[i].key] = cleanValueList(body[FILTER_FIELDS[i].key]);
    out.createdMin = isRealDate(body.created_min) ? body.created_min : "";
    out.createdMax = isRealDate(body.created_max) ? body.created_max : "";
    return out;
  }

  // null when the range is usable (either end may be empty), otherwise a sentence for the person.
  function dateRangeProblem(from, to) {
    var f = typeof from === "string" ? from : "";
    var t = typeof to === "string" ? to : "";
    if ((f !== "" && !isRealDate(f)) || (t !== "" && !isRealDate(t))) return "Please use real dates.";
    if (f !== "" && t !== "" && f > t) return "The start date is after the end date.";
    return null;
  }

  // min and max for the two date inputs. The data range comes from /filters; each input is also limited by the
  // other one, so the picker cannot offer an inverted range. "" means no limit.
  function dateInputLimits(createdMin, createdMax, from, to) {
    var lo = isRealDate(createdMin) ? createdMin : "";
    var hi = isRealDate(createdMax) ? createdMax : "";
    return { fromMin: lo, fromMax: isRealDate(to) ? to : hi, toMin: isRealDate(from) ? from : lo, toMax: hi };
  }

  function emptySelection() {
    var selection = {};
    for (var i = 0; i < FILTER_KEYS.length; i++) selection[FILTER_KEYS[i]] = "";
    return selection;
  }

  // The chips above the question box: one per active filter, in a fixed order.
  function activeFilterList(selection) {
    var items = [];
    var fields = FILTER_FIELDS.concat(DATE_FIELDS);
    for (var i = 0; i < fields.length; i++) {
      var value = selection && typeof selection[fields[i].key] === "string" ? selection[fields[i].key].trim() : "";
      if (value !== "") items.push({ key: fields[i].key, label: fields[i].label, value: value, text: fields[i].label + ": " + value });
    }
    return items;
  }

  // ---------- page helpers (take the document or elements as arguments) ----------
  function clearChildren(element) {
    while (element.firstChild) element.removeChild(element.firstChild);
  }

  function setOrRemoveAttribute(element, name, value) {
    if (value === "" || value == null) element.removeAttribute(name);
    else element.setAttribute(name, value);
  }

  // inert: the browser skips the element for focus, clicks and screen readers
  function setInert(element, on) {
    if (on) element.setAttribute("inert", "");
    else element.removeAttribute("inert");
  }

  // "Any" plus one option per value. Returns the value that ends up selected ("" when `current` is gone).
  function renderSelectOptions(doc, select, values, current) {
    clearChildren(select);
    var any = doc.createElement("option");
    any.setAttribute("value", "");
    any.textContent = "Any";
    select.appendChild(any);
    for (var i = 0; i < values.length; i++) {
      var option = doc.createElement("option");
      option.setAttribute("value", values[i]);
      option.textContent = values[i];
      select.appendChild(option);
    }
    select.value = values.indexOf(current) !== -1 ? current : "";
    return select.value;
  }

  function renderActiveFilters(doc, list, items, onRemove) {
    clearChildren(list);
    items.forEach(function (item) {
      var chip = doc.createElement("li");
      chip.classList.add("filter-chip");
      var text = doc.createElement("span");
      text.textContent = item.text;
      chip.appendChild(text);
      var remove = doc.createElement("button");
      remove.setAttribute("type", "button");
      remove.classList.add("filter-chip-remove");
      remove.setAttribute("aria-label", "Remove filter " + item.text);
      remove.textContent = "×";
      remove.addEventListener("click", function () {
        onRemove(item.key);
      });
      chip.appendChild(remove);
      list.appendChild(chip);
    });
    list.hidden = items.length === 0;
  }

  function renderHealth(els, description) {
    els.indexStatus.textContent = description.text;
    els.indexStatus.classList.toggle("is-degraded", description.degraded);
  }

  function renderFiltersNotice(els, message) {
    els.filtersNotice.textContent = message || "";
    els.filtersNotice.hidden = !message;
    els.filtersReload.hidden = !message;
  }

  function renderDateProblem(els, problem) {
    els.dateError.textContent = problem || "";
    els.dateError.hidden = !problem;
    for (var i = 0; i < DATE_FIELDS.length; i++) {
      var input = els[DATE_FIELDS[i].key];
      if (problem) input.setAttribute("aria-invalid", "true");
      else input.removeAttribute("aria-invalid");
    }
  }

  function renderDateLimits(els, limits) {
    setOrRemoveAttribute(els.date_from, "min", limits.fromMin);
    setOrRemoveAttribute(els.date_from, "max", limits.fromMax);
    setOrRemoveAttribute(els.date_to, "min", limits.toMin);
    setOrRemoveAttribute(els.date_to, "max", limits.toMax);
  }

  // The label names the theme the button switches TO, so it carries the state itself (no aria-pressed).
  function renderThemeButton(button, effectiveTheme) {
    button.textContent = themeToggleLabel(effectiveTheme);
    button.removeAttribute("aria-pressed");
  }

  // While the panel covers the page, the header and the main area are inert (no keyboard focus, no clicks, hidden
  // from screen readers), so the panel behaves like a dialog. The panel and its backdrop are not inside them.
  function renderSidebar(els, open) {
    els.sidebar.classList.toggle("is-open", open);
    els.sidebarBackdrop.hidden = !open;
    els.sidebarToggle.setAttribute("aria-expanded", open ? "true" : "false");
    setInert(els.appHeader, open);
    setInert(els.mainArea, open);
  }

  function systemPrefersDark(win) {
    try {
      return !!(win.matchMedia && win.matchMedia(DARK_QUERY).matches);
    } catch (err) {
      return false;
    }
  }

  function safeStorage(win) {
    try {
      return win.localStorage || null;
    } catch (err) {
      return null;
    }
  }

  // Runs while the page is still loading (the script is in the head), so a saved theme shows from the first paint.
  // With no saved choice nothing is set, and the stylesheet follows the system preference by itself.
  function applyEarlyTheme(doc, win) {
    var stored = readStoredTheme(safeStorage(win));
    if (stored) doc.documentElement.setAttribute("data-theme", stored);
    return stored;
  }

  function collectElements(doc) {
    var els = {};
    var missing = [];
    for (var name in ELEMENT_IDS) {
      if (!Object.prototype.hasOwnProperty.call(ELEMENT_IDS, name)) continue;
      var element = doc.getElementById(ELEMENT_IDS[name]);
      if (element) els[name] = element;
      else missing.push(ELEMENT_IDS[name]);
    }
    return { els: els, missing: missing };
  }

  // ---------- wiring ----------
  // Sets the page up. `deps` is optional and only used by the tests: fetch (instead of window.fetch) and the two
  // request timeouts. Returns the app object; `ready` resolves when the first /health and /filters requests are
  // done (they never reject).
  function init(doc, win, deps) {
    var collected = collectElements(doc);
    if (collected.missing.length > 0) throw new Error("The page is missing elements: " + collected.missing.join(", "));
    var els = collected.els;
    var fetchImpl = (deps && deps.fetch) || win.fetch.bind(win);
    var healthTimeout = (deps && deps.healthTimeoutMs) || HEALTH_TIMEOUT_MS;
    var filtersTimeout = (deps && deps.filtersTimeoutMs) || FILTERS_TIMEOUT_MS;
    var storage = safeStorage(win);
    var subscribers = [];
    var state = {
      health: null,
      filters: emptyFilterOptions(),
      filtersStatus: "loading",
      selected: emptySelection(),
      k: K_DEFAULT,
      problem: null,
      sidebarOpen: false
    };

    function snapshot() {
      return { filters: cleanFilters(state.selected), k: state.k, problem: state.problem };
    }

    function notify() {
      var shot = snapshot();
      for (var i = 0; i < subscribers.length; i++) subscribers[i](shot);
    }

    function syncControls() {
      for (var i = 0; i < FILTER_KEYS.length; i++) els[FILTER_KEYS[i]].value = state.selected[FILTER_KEYS[i]];
    }

    function renderFilterState() {
      var s = state.selected;
      state.problem = dateRangeProblem(s.date_from, s.date_to);
      renderDateProblem(els, state.problem);
      renderDateLimits(els, dateInputLimits(state.filters.createdMin, state.filters.createdMax, s.date_from, s.date_to));
      renderActiveFilters(doc, els.activeFilters, activeFilterList(s), function (key) {
        setSelection(key, "");
        syncControls();
      });
      notify();
    }

    function setSelection(key, value) {
      state.selected[key] = typeof value === "string" ? value.trim() : "";
      renderFilterState();
    }

    function clearFilters() {
      state.selected = emptySelection();
      syncControls();
      renderFilterState();
    }

    function setK(raw) {
      state.k = clampK(raw);
      els.kInput.value = String(state.k);
      notify();
    }

    // ----- theme
    function effectiveTheme() {
      var explicit = doc.documentElement.getAttribute("data-theme");
      return resolveTheme(isTheme(explicit) ? explicit : null, systemPrefersDark(win));
    }

    function refreshThemeButton() {
      renderThemeButton(els.themeToggle, effectiveTheme());
    }

    els.themeToggle.addEventListener("click", function () {
      var next = nextTheme(effectiveTheme());
      doc.documentElement.setAttribute("data-theme", next);
      writeStoredTheme(storage, next);
      refreshThemeButton();
    });
    try {
      var dark = win.matchMedia(DARK_QUERY);
      if (dark && typeof dark.addEventListener === "function") dark.addEventListener("change", refreshThemeButton);
    } catch (err) {
      // no media query support: the label just does not follow the system
    }
    refreshThemeButton();

    // ----- sidebar (a slide-over panel on narrow screens; always visible on wide ones)
    function openSidebar() {
      state.sidebarOpen = true;
      renderSidebar(els, true);
      els.sidebarClose.focus();
    }

    function closeSidebar(returnFocus) {
      var wasOpen = state.sidebarOpen;
      state.sidebarOpen = false;
      renderSidebar(els, false);
      if (wasOpen && returnFocus) els.sidebarToggle.focus();
    }

    els.sidebarToggle.addEventListener("click", function () {
      if (state.sidebarOpen) closeSidebar(true);
      else openSidebar();
    });
    els.sidebarClose.addEventListener("click", function () {
      closeSidebar(true);
    });
    els.sidebarBackdrop.addEventListener("click", function () {
      closeSidebar(true);
    });
    doc.addEventListener("keydown", function (event) {
      if (event.key === "Escape" && state.sidebarOpen) closeSidebar(true);
    });
    try {
      var narrow = win.matchMedia(NARROW_QUERY);
      if (narrow && typeof narrow.addEventListener === "function") {
        narrow.addEventListener("change", function (event) {
          if (!event.matches) closeSidebar(false); // back to a wide screen: no overlay left behind
        });
      }
    } catch (err) {
      // no media query support: the panel simply stays as the person left it
    }
    renderSidebar(els, false);

    // ----- filters and k
    FILTER_FIELDS.forEach(function (field) {
      els[field.key].addEventListener("change", function () {
        setSelection(field.key, els[field.key].value);
      });
    });
    DATE_FIELDS.forEach(function (field) {
      var onChange = function () {
        setSelection(field.key, els[field.key].value);
      };
      els[field.key].addEventListener("change", onChange);
      els[field.key].addEventListener("input", onChange);
    });
    els.clearFilters.addEventListener("click", clearFilters);
    els.kInput.addEventListener("change", function () {
      setK(els.kInput.value);
    });
    els.kInput.value = String(state.k);

    // ----- the two first requests; each one fails on its own without breaking the page
    async function loadHealth() {
      var result = await requestJson(fetchImpl, "/health", { timeoutMs: healthTimeout });
      state.health = result.kind === "http" && result.status === 200 ? normaliseHealth(result.body) : null;
      renderHealth(els, describeHealth(state.health));
    }

    async function loadFilters() {
      els.filtersReload.disabled = true;
      var result = await requestJson(fetchImpl, "/filters", { timeoutMs: filtersTimeout });
      var options = result.kind === "http" && result.status === 200 ? normaliseFilterOptions(result.body) : null;
      els.filtersReload.disabled = false;
      if (options === null) {
        state.filters = emptyFilterOptions();
        state.filtersStatus = "failed";
        renderFiltersNotice(els, "Filters are unavailable right now. You can still ask questions.");
      } else {
        state.filters = options;
        state.filtersStatus = "ok";
        var anyValues = FILTER_FIELDS.some(function (f) {
          return options[f.key].length > 0;
        });
        renderFiltersNotice(els, anyValues ? "" : "The server has no filter values to offer.");
      }
      FILTER_FIELDS.forEach(function (field) {
        state.selected[field.key] = renderSelectOptions(doc, els[field.key], state.filters[field.key], state.selected[field.key]);
      });
      renderFilterState();
    }

    els.filtersReload.addEventListener("click", loadFilters);

    renderFilterState();
    var ready = Promise.allSettled([loadHealth(), loadFilters()]);

    var app = {
      els: els,
      state: state,
      ready: ready,
      subscribe: function (listener) {
        subscribers.push(listener);
      },
      snapshot: snapshot,
      setSelection: setSelection,
      clearFilters: clearFilters,
      setK: setK,
      openSidebar: openSidebar,
      closeSidebar: closeSidebar,
      reloadFilters: loadFilters
    };
    app.chat = initChat(app, doc, win, fetchImpl, deps);
    app.drawer = initDrawer(app, doc, win, fetchImpl, deps);
    app.chat.setOpenTicketHandler(app.drawer.open);
    return app;
  }

  // =====================================================================================================
  // Group C: the chat thread, the composer, answers with sources, errors with Retry and a countdown, history,
  // New chat. Same rules as Group B: helpers take the document as an argument, page elements are built only with
  // createElement, createTextNode and textContent, and looks change only through classes and the hidden attribute.
  // Everything from the API (answers, titles, snippets, errors) is shown as plain text.
  // =====================================================================================================

  var CHAT_TIMEOUT_MS = REQUEST_TIMEOUT_MS;
  var COUNTER_FROM = 900;
  var NEAR_BOTTOM_PX = 80;
  var HISTORY_KEEP = 12;
  var MAX_SHOWN_TEXT = 300;
  var REDUCED_MOTION_QUERY = "(prefers-reduced-motion: reduce)";
  var NOT_FOUND_LABEL = "Not found in the tickets";
  var NOT_RETRIEVED_MARKER = "not in retrieved tickets";
  var LOADING_TEXT = "Looking through the tickets…";

  // ---------- the /chat response, checked and cleaned ----------
  function shownText(value) {
    if (typeof value !== "string") return "";
    var text = value.replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
    return text.length > MAX_SHOWN_TEXT ? text.slice(0, MAX_SHOWN_TEXT - 1) + "…" : text;
  }

  function referenceId(value) {
    if (typeof value !== "string") return null;
    var id = value.trim().toUpperCase();
    return /^(OPS|PO|INV)-\d{1,15}$/.test(id) ? id : null;
  }

  function idList(list, clean) {
    var out = [];
    if (!Array.isArray(list)) return out;
    for (var i = 0; i < list.length; i++) {
      var id = clean(list[i]);
      if (id && out.indexOf(id) === -1) out.push(id);
    }
    return out;
  }

  // null when the body is not a usable answer (the caller then shows "Unexpected response").
  function validateChatResponse(body) {
    if (!body || typeof body !== "object" || Array.isArray(body)) return null;
    if (typeof body.answer !== "string" || typeof body.found !== "boolean") return null;
    var sources = [];
    var raw = Array.isArray(body.sources) ? body.sources : [];
    for (var i = 0; i < raw.length; i++) {
      var s = raw[i];
      var id = s && typeof s === "object" ? normaliseId(s.ticket_id) : null;
      if (!id) continue;
      sources.push({
        ticket_id: id,
        title: shownText(s.title),
        category: shownText(s.category),
        priority: shownText(s.priority),
        status: shownText(s.status),
        score: typeof s.score === "number" && isFinite(s.score) ? s.score : null
      });
    }
    return {
      answer: body.answer,
      found: body.found,
      citations: idList(body.citations, normaliseId),
      unverified: idList(body.unverified_citations, normaliseId),
      missing: idList(body.missing_references, referenceId),
      sources: sources
    };
  }

  // The class for the priority badge; the badge always shows the priority as text as well.
  function priorityClass(priority) {
    var match = /^P([1-4])\b/.exec(typeof priority === "string" ? priority : "");
    return match ? "badge-p" + match[1] : "badge-unknown";
  }

  function scoreText(score) {
    return typeof score === "number" && isFinite(score) ? "similarity " + score.toFixed(2) : "exact match";
  }

  // ---------- scrolling ----------
  function nearBottom(thread) {
    return thread.scrollHeight - thread.scrollTop - thread.clientHeight <= NEAR_BOTTOM_PX;
  }

  function prefersReducedMotion(win) {
    try {
      return !!(win.matchMedia && win.matchMedia(REDUCED_MOTION_QUERY).matches);
    } catch (err) {
      return false;
    }
  }

  function scrollToBottom(win, thread) {
    if (typeof thread.scrollTo === "function") {
      thread.scrollTo({ top: thread.scrollHeight, behavior: prefersReducedMotion(win) ? "auto" : "smooth" });
    } else {
      thread.scrollTop = thread.scrollHeight;
    }
  }

  // ---------- countdowns ----------
  // One shared timer for all running countdowns. Each entry has an absolute end time (endsAt, in ms), so a tab that
  // was asleep shows the right time when it wakes. entry: {endsAt, onTick(secondsLeft), onDone()}.
  function createCountdowns(now, setIntervalFn, clearIntervalFn) {
    var entries = [];
    var timer = null;
    var start = setIntervalFn || function (fn, ms) { return setInterval(fn, ms); };
    var stop = clearIntervalFn || function (handle) { clearInterval(handle); };

    function stopTimer() {
      if (timer !== null) {
        stop(timer);
        timer = null;
      }
    }

    function remove(entry) {
      var i = entries.indexOf(entry);
      if (i !== -1) entries.splice(i, 1);
      if (entries.length === 0) stopTimer();
    }

    function tick() {
      var running = entries.slice();
      for (var i = 0; i < running.length; i++) {
        var entry = running[i];
        if (entries.indexOf(entry) === -1) continue;
        var seconds = Math.ceil((entry.endsAt - now()) / 1000);
        if (seconds <= 0) {
          remove(entry);
          entry.onDone();
        } else {
          entry.onTick(seconds);
        }
      }
      if (entries.length === 0) stopTimer();
    }

    return {
      add: function (entry) {
        entries.push(entry);
        if (timer === null) timer = start(tick, 1000);
        return entry;
      },
      remove: remove,
      clear: function () {
        entries = [];
        stopTimer();
      },
      tick: tick,
      size: function () {
        return entries.length;
      }
    };
  }

  // ---------- rendering ----------
  function textSpan(doc, className, text) {
    var span = doc.createElement("span");
    if (className) span.classList.add(className);
    span.textContent = text;
    return span;
  }

  function messageBox(doc, kind) {
    var box = doc.createElement("div");
    box.classList.add("message");
    box.classList.add(kind);
    return box;
  }

  function renderUserMessage(doc, text) {
    var box = messageBox(doc, "message-user");
    var bubble = doc.createElement("div");
    bubble.classList.add("bubble");
    bubble.textContent = text;
    box.appendChild(bubble);
    return box;
  }

  // A cited ticket that was retrieved: a button that opens the ticket.
  function renderChip(doc, id, onOpen) {
    var chip = doc.createElement("button");
    chip.setAttribute("type", "button");
    chip.classList.add("chip");
    chip.setAttribute("aria-label", "Open ticket " + id);
    chip.textContent = "[" + id + "]";
    chip.dataset.ticket = id;
    chip.addEventListener("click", function () {
      onOpen(id, chip);
    });
    return chip;
  }

  // An id the server did not retrieve (or reported as unverified): muted text, not clickable, with a visible marker.
  function renderMutedId(doc, id) {
    var muted = textSpan(doc, "chip-muted", "[" + id + "]");
    muted.appendChild(textSpan(doc, "chip-flag", NOT_RETRIEVED_MARKER));
    return muted;
  }

  function appendInline(doc, parent, nodes, onOpen) {
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.type === "text") {
        parent.appendChild(doc.createTextNode(node.text));
      } else if (node.type === "br") {
        parent.appendChild(doc.createElement("br"));
      } else if (node.type === "bold") {
        var strong = doc.createElement("strong");
        appendInline(doc, strong, node.children, onOpen);
        parent.appendChild(strong);
      } else if (node.type === "chip") {
        parent.appendChild(renderChip(doc, node.id, onOpen));
      } else if (node.type === "idtext") {
        parent.appendChild(renderMutedId(doc, node.id));
      }
    }
  }

  // The tree from parseAnswer() as page elements.
  function renderAnswerBody(doc, blocks, onOpen) {
    var body = doc.createElement("div");
    body.classList.add("answer");
    for (var i = 0; i < blocks.length; i++) {
      var block = blocks[i];
      if (block.type === "p") {
        var p = doc.createElement("p");
        appendInline(doc, p, block.inline, onOpen);
        body.appendChild(p);
      } else {
        var ul = doc.createElement("ul");
        for (var j = 0; j < block.items.length; j++) {
          var li = doc.createElement("li");
          appendInline(doc, li, block.items[j], onOpen);
          ul.appendChild(li);
        }
        body.appendChild(ul);
      }
    }
    return body;
  }

  function renderSourceCard(doc, source, cited, onOpen) {
    var card = doc.createElement("button");
    card.setAttribute("type", "button");
    card.classList.add("source-card");
    card.setAttribute("aria-label", "Open ticket " + source.ticket_id + (source.title ? ": " + source.title : ""));
    card.dataset.ticket = source.ticket_id;
    var top = doc.createElement("div");
    top.classList.add("card-top");
    top.appendChild(textSpan(doc, "card-id", source.ticket_id));
    if (cited) top.appendChild(textSpan(doc, "cited-marker", "cited"));
    var badge = textSpan(doc, "badge", source.priority || "No priority");
    badge.classList.add(priorityClass(source.priority));
    top.appendChild(badge);
    card.appendChild(top);
    var title = doc.createElement("div");
    title.classList.add("card-title");
    title.textContent = source.title || "(no title)";
    card.appendChild(title);
    var meta = doc.createElement("div");
    meta.classList.add("card-meta");
    if (source.status) meta.appendChild(textSpan(doc, "", source.status));
    if (source.category) meta.appendChild(textSpan(doc, "", source.category));
    meta.appendChild(textSpan(doc, "score", scoreText(source.score)));
    card.appendChild(meta);
    card.addEventListener("click", function () {
      onOpen(source.ticket_id, card);
    });
    return card;
  }

  // "Sources (N)": collapsed by default, one card per retrieved ticket in the order the server gave (cited first).
  function renderSources(doc, sources, citations, onOpen) {
    var details = doc.createElement("details");
    details.classList.add("sources");
    var summary = doc.createElement("summary");
    summary.classList.add("sources-summary");
    summary.textContent = "Sources (" + sources.length + ")";
    details.appendChild(summary);
    var list = doc.createElement("ul");
    list.classList.add("source-list");
    for (var i = 0; i < sources.length; i++) {
      var item = doc.createElement("li");
      item.appendChild(renderSourceCard(doc, sources[i], citations.indexOf(sources[i].ticket_id) !== -1, onOpen));
      list.appendChild(item);
    }
    details.appendChild(list);
    return details;
  }

  // data: the result of validateChatResponse().
  function renderAssistantMessage(doc, data, onOpen) {
    var box = messageBox(doc, "message-assistant");
    var bubble = doc.createElement("div");
    bubble.classList.add("bubble");
    if (!data.found) {
      bubble.classList.add("bubble-notfound");
      bubble.appendChild(textSpan(doc, "label-notfound", NOT_FOUND_LABEL));
    }
    var sourceIds = data.sources.map(function (s) {
      return s.ticket_id;
    });
    bubble.appendChild(renderAnswerBody(doc, parseAnswer(data.answer, sourceIds, data.unverified), onOpen));
    if (data.missing.length > 0) {
      var callout = doc.createElement("div");
      callout.classList.add("callout");
      callout.setAttribute("role", "note");
      callout.textContent = "Not found in the ticket data: " + data.missing.join(", ");
      bubble.appendChild(callout);
    }
    box.appendChild(bubble);
    if (data.sources.length > 0) box.appendChild(renderSources(doc, data.sources, data.citations, onOpen));
    return box;
  }

  function renderLoading(doc) {
    var box = messageBox(doc, "message-assistant");
    var loading = doc.createElement("div");
    loading.classList.add("loading");
    loading.setAttribute("role", "status");
    for (var i = 0; i < 3; i++) loading.appendChild(textSpan(doc, "dot", ""));
    loading.appendChild(textSpan(doc, "", LOADING_TEXT));
    box.appendChild(loading);
    return box;
  }

  function errorLine(doc, className, text) {
    var p = doc.createElement("p");
    p.classList.add(className);
    p.textContent = text;
    return p;
  }

  // desc: the result of describeError(). Returns the node and the parts the controller needs to update.
  function renderErrorBubble(doc, desc) {
    var box = messageBox(doc, "message-assistant");
    var bubble = doc.createElement("div");
    bubble.classList.add("error-bubble");
    bubble.setAttribute("role", "alert");
    bubble.appendChild(errorLine(doc, "error-title", desc.title));
    bubble.appendChild(errorLine(doc, "error-detail", desc.detail));
    var parts = { node: box, countdownLine: null, countdownText: null, doneNote: null, retry: null };
    if (desc.retryable && desc.retryAfterSeconds) {
      var line = errorLine(doc, "error-detail", "Try again in ");
      var timer = textSpan(doc, "countdown", formatDuration(desc.retryAfterSeconds));
      timer.setAttribute("role", "timer");
      timer.setAttribute("aria-live", "off"); // ticking every second must not be read out
      line.appendChild(timer);
      bubble.appendChild(line);
      var done = errorLine(doc, "error-detail", "");
      done.setAttribute("role", "status");
      done.hidden = true;
      bubble.appendChild(done);
      parts.countdownLine = line;
      parts.countdownText = timer;
      parts.doneNote = done;
    }
    if (desc.retryable) {
      var retry = doc.createElement("button");
      retry.setAttribute("type", "button");
      retry.classList.add("button");
      retry.textContent = "Retry";
      bubble.appendChild(retry);
      parts.retry = retry;
    }
    box.appendChild(bubble);
    return parts;
  }

  // ---------- the controller ----------
  // `deps` (tests only): now, setInterval, clearInterval, chatTimeoutMs.
  function initChat(app, doc, win, fetchImpl, deps) {
    var els = app.els;
    var options = deps || {};
    var now = options.now || function () { return Date.now(); };
    var chatTimeout = options.chatTimeoutMs || CHAT_TIMEOUT_MS;
    var countdowns = createCountdowns(now, options.setInterval, options.clearInterval);
    var state = { history: [], pending: null, epoch: 0, lastError: null, openHandler: null };

    function openTicket(id, element) {
      if (typeof state.openHandler === "function") state.openHandler(id, element);
    }

    // ----- the composer
    function updateSendState() {
      var question = validateQuestion(els.question.value);
      els.send.disabled = !!state.pending || !question.ok || !!app.state.problem;
    }

    // The line under the box: a reason why Send is off (bad date range), or the character counter from 900 on.
    function updateCounter() {
      var length = els.question.value.length;
      var text = "";
      var warn = false;
      var limit = false;
      if (app.state.problem) {
        text = "Fix the date range in the filters to send.";
        warn = true;
      } else if (length >= COUNTER_FROM) {
        text = length + " / " + MAX_QUESTION;
        limit = length >= MAX_QUESTION;
        warn = !limit;
      }
      els.charCount.textContent = text;
      els.charCount.hidden = text === "";
      els.charCount.classList.toggle("is-near", warn);
      els.charCount.classList.toggle("is-limit", limit);
    }

    function refreshComposer() {
      updateSendState();
      updateCounter();
    }

    // ----- the thread
    function appendToThread(node, forceScroll) {
      var wasNear = nearBottom(els.thread);
      if (els.emptyState.parentNode === els.thread) els.thread.removeChild(els.emptyState);
      els.thread.appendChild(node);
      followThread(wasNear || forceScroll);
    }

    function followThread(scroll) {
      if (scroll) {
        scrollToBottom(win, els.thread);
        els.jumpLatest.hidden = true;
      } else {
        els.jumpLatest.hidden = false; // something new arrived below; do not pull the reader away from what they read
      }
    }

    function removeFromThread(node) {
      if (node && node.parentNode === els.thread) els.thread.removeChild(node);
    }

    function replaceInThread(oldNode, newNode) {
      var wasNear = nearBottom(els.thread);
      removeFromThread(oldNode);
      els.thread.appendChild(newNode);
      followThread(wasNear);
    }

    // ----- errors
    function retireError() {
      var last = state.lastError;
      if (!last) return;
      state.lastError = null;
      if (last.countdownEntry) countdowns.remove(last.countdownEntry);
      [last.parts.retry, last.parts.countdownLine, last.parts.doneNote].forEach(function (part) {
        if (part && part.parentNode) part.parentNode.removeChild(part);
      });
    }

    function showError(job, loadingNode, desc) {
      var parts = renderErrorBubble(doc, desc);
      replaceInThread(loadingNode, parts.node);
      var record = { parts: parts, countdownEntry: null };
      state.lastError = record;
      if (parts.retry) {
        parts.retry.addEventListener("click", function () {
          if (state.pending) return;
          if (state.lastError === record) state.lastError = null;
          if (record.countdownEntry) countdowns.remove(record.countdownEntry);
          removeFromThread(parts.node);
          startRequest(job);
          els.question.focus();
        });
      }
      if (parts.retry && parts.countdownText) {
        parts.retry.disabled = true;
        record.countdownEntry = countdowns.add({
          endsAt: now() + desc.retryAfterSeconds * 1000,
          onTick: function (seconds) {
            parts.countdownText.textContent = formatDuration(seconds);
          },
          onDone: function () {
            parts.retry.disabled = false;
            parts.countdownLine.hidden = true;
            parts.doneNote.textContent = "You can retry now.";
            parts.doneNote.hidden = false;
          }
        });
      }
    }

    // ----- one request at a time
    function finishRequest(job, loadingNode, result) {
      state.pending = null;
      var data = null;
      var desc = null;
      if (result.kind === "http" && result.status === 200) {
        data = validateChatResponse(result.body);
        if (!data) desc = describeError({ kind: "invalid" });
      } else {
        desc = describeError(result);
      }
      if (desc && desc.silent) {
        removeFromThread(loadingNode);
      } else if (data) {
        replaceInThread(loadingNode, renderAssistantMessage(doc, data, openTicket));
        // History grows only after an answer arrived: the question and the answer text, never the sources.
        state.history.push({ role: "user", content: job.question }, { role: "assistant", content: data.answer });
        if (state.history.length > HISTORY_KEEP) state.history = state.history.slice(-HISTORY_KEEP);
      } else {
        showError(job, loadingNode, desc);
      }
      updateSendState();
    }

    // job: {question, k, filters}. The history is read now, so a Retry sends the current history.
    function startRequest(job) {
      var controller = typeof AbortController === "function" ? new AbortController() : null;
      var epoch = state.epoch;
      var loading = renderLoading(doc);
      state.pending = { controller: controller, loading: loading };
      appendToThread(loading, true);
      updateSendState();
      var payload = buildChatPayload({ question: job.question, k: job.k, filters: job.filters, history: state.history });
      requestJson(fetchImpl, "/chat", {
        method: "POST",
        body: payload,
        timeoutMs: chatTimeout,
        signal: controller ? controller.signal : undefined
      }).then(function (result) {
        if (epoch !== state.epoch) return; // "New chat" happened meanwhile: this reply belongs to a conversation that is gone
        finishRequest(job, loading, result);
      });
    }

    function submit() {
      if (state.pending) return false;
      var question = validateQuestion(els.question.value);
      if (!question.ok || app.state.problem) return false;
      var shot = app.snapshot();
      retireError();
      appendToThread(renderUserMessage(doc, question.trimmed), true);
      els.question.value = "";
      refreshComposer();
      startRequest({ question: question.trimmed, k: shot.k, filters: shot.filters });
      return true;
    }

    function newChat() {
      state.epoch += 1;
      if (state.pending && state.pending.controller) state.pending.controller.abort();
      state.pending = null;
      state.history = [];
      state.lastError = null;
      countdowns.clear();
      clearChildren(els.thread);
      els.thread.appendChild(els.emptyState);
      els.jumpLatest.hidden = true;
      if (app.state.sidebarOpen) app.closeSidebar(false);
      refreshComposer();
      els.question.focus();
    }

    // ----- events
    els.question.addEventListener("input", refreshComposer);
    els.question.addEventListener("keydown", function (event) {
      // Enter sends, Shift+Enter is a new line, and Enter while an input method is composing text never sends.
      if (event.key !== "Enter" || event.shiftKey || event.isComposing || event.keyCode === 229) return;
      event.preventDefault();
      submit();
    });
    els.composer.addEventListener("submit", function (event) {
      event.preventDefault();
      submit();
    });
    els.newChat.addEventListener("click", newChat);
    els.jumpLatest.addEventListener("click", function () {
      scrollToBottom(win, els.thread);
      els.jumpLatest.hidden = true;
    });
    els.thread.addEventListener("scroll", function () {
      if (nearBottom(els.thread)) els.jumpLatest.hidden = true;
    });
    var examples = Array.prototype.slice.call(els.emptyState.getElementsByClassName("example"));
    examples.forEach(function (button) {
      button.addEventListener("click", function () {
        els.question.value = button.textContent;
        refreshComposer();
        submit();
      });
    });
    app.subscribe(refreshComposer);
    refreshComposer();

    return {
      state: state,
      submit: submit,
      newChat: newChat,
      refresh: refreshComposer,
      tickCountdowns: countdowns.tick,
      countdownCount: countdowns.size,
      setOpenTicketHandler: function (handler) {
        state.openHandler = handler;
      }
    };
  }

  // =====================================================================================================
  // Group D: the ticket drawer. A native modal <dialog> opened from chips and source cards. It fetches
  // GET /tickets/{id} and shows every field as text. The browser gives us the focus trap, the Esc key and the
  // inert page behind it; this code adds loading, error and 404 states, a stale-response guard, focus handling
  // and the scroll lock.
  // =====================================================================================================

  var TICKET_TIMEOUT_MS = HEALTH_TIMEOUT_MS;
  var TICKET_ID_PATTERN = /^OPS-[0-9]+$/;
  var MAX_LONG_TEXT = 20000;
  var MAX_COMMENTS = 200;
  var NOT_RESOLVED_TEXT = "Not resolved yet";
  var NO_RESOLUTION_TEXT = "No resolution recorded";
  var NO_COMMENTS_TEXT = "No comments";

  // ---------- the ticket, checked and cleaned ----------
  // Short fields lose control characters and are cut like the chat sources. Long text keeps its line breaks.
  function longText(value) {
    if (typeof value !== "string") return "";
    var text = value.replace(/\r\n?/g, "\n").replace(/[\u0000-\u0008\u000b-\u001f\u007f]+/g, " ").trim();
    return text.length > MAX_LONG_TEXT ? text.slice(0, MAX_LONG_TEXT - 1) + "…" : text;
  }

  // "2025-10-26T06:29:40" -> "2025-10-26 06:29". Timestamps have no zone, so nothing is converted.
  function formatTimestamp(value) {
    var text = shownText(value);
    var match = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})/.exec(text);
    return match ? match[1] + " " + match[2] : text;
  }

  // null when the body is not the ticket that was asked for.
  function validateTicket(body, expectedId) {
    if (!body || typeof body !== "object" || Array.isArray(body)) return null;
    if (body.ticket_id !== expectedId || typeof body.title !== "string") return null;
    var comments = [];
    var raw = Array.isArray(body.comments) ? body.comments : [];
    for (var i = 0; i < raw.length && comments.length < MAX_COMMENTS; i++) {
      var c = raw[i];
      if (!c || typeof c !== "object") continue;
      comments.push({ author: shownText(c.author), timestamp: formatTimestamp(c.timestamp), text: longText(c.text) });
    }
    var resolution = longText(body.resolution_notes);
    return {
      id: expectedId,
      title: shownText(body.title),
      description: longText(body.description),
      category: shownText(body.category),
      priority: shownText(body.priority),
      status: shownText(body.status),
      team: shownText(body.team),
      reporter: shownText(body.reporter),
      assignee: shownText(body.assignee),
      created: formatTimestamp(body.created_at),
      resolved: typeof body.resolved_at === "string" && body.resolved_at.trim() !== "" ? formatTimestamp(body.resolved_at) : null,
      resolution: resolution === "" ? null : resolution,
      comments: comments
    };
  }

  // ---------- rendering ----------
  function drawerTitleText(id, title) {
    return title ? id + ": " + title : id;
  }

  function fieldRow(doc, list, label, value, extraClass) {
    var dt = doc.createElement("dt");
    dt.textContent = label;
    var dd = doc.createElement("dd");
    if (extraClass) {
      var badge = textSpan(doc, "badge", value || "Not set");
      badge.classList.add(extraClass);
      dd.appendChild(badge);
    } else {
      dd.textContent = value || "Not set";
    }
    list.appendChild(dt);
    list.appendChild(dd);
  }

  function proseSection(doc, heading, text, emptyText) {
    var section = doc.createElement("div");
    section.classList.add("drawer-section");
    var h = doc.createElement("h3");
    h.textContent = heading;
    section.appendChild(h);
    var p = doc.createElement("p");
    if (text) {
      p.classList.add("prose");
      p.textContent = text;
    } else {
      p.classList.add("drawer-state");
      p.textContent = emptyText;
    }
    section.appendChild(p);
    return section;
  }

  // ticket: the result of validateTicket(). Returns a fragment-like <div> to put in the drawer body.
  function renderTicketBody(doc, ticket) {
    var root = doc.createElement("div");
    var list = doc.createElement("dl");
    list.classList.add("field-list");
    fieldRow(doc, list, "Priority", ticket.priority, priorityClass(ticket.priority));
    fieldRow(doc, list, "Status", ticket.status);
    fieldRow(doc, list, "Category", ticket.category);
    fieldRow(doc, list, "Team", ticket.team);
    fieldRow(doc, list, "Reporter", ticket.reporter);
    fieldRow(doc, list, "Assignee", ticket.assignee);
    fieldRow(doc, list, "Created", ticket.created);
    fieldRow(doc, list, "Resolved", ticket.resolved === null ? NOT_RESOLVED_TEXT : ticket.resolved);
    root.appendChild(list);
    root.appendChild(proseSection(doc, "Description", ticket.description, "No description"));
    root.appendChild(proseSection(doc, "Resolution notes", ticket.resolution, NO_RESOLUTION_TEXT));
    var comments = doc.createElement("div");
    comments.classList.add("drawer-section");
    var h = doc.createElement("h3");
    h.textContent = "Comments (" + ticket.comments.length + ")";
    comments.appendChild(h);
    if (ticket.comments.length === 0) {
      var none = doc.createElement("p");
      none.classList.add("drawer-state");
      none.textContent = NO_COMMENTS_TEXT;
      comments.appendChild(none);
    }
    for (var i = 0; i < ticket.comments.length; i++) {
      var c = ticket.comments[i];
      var box = doc.createElement("div");
      box.classList.add("comment");
      var head = doc.createElement("div");
      head.classList.add("comment-head");
      head.textContent = (c.author || "Unknown") + (c.timestamp ? " · " + c.timestamp : "");
      box.appendChild(head);
      var text = doc.createElement("p");
      text.classList.add("prose");
      text.textContent = c.text;
      box.appendChild(text);
      comments.appendChild(box);
    }
    root.appendChild(comments);
    return root;
  }

  function renderDrawerLoading(doc) {
    var box = doc.createElement("p");
    box.classList.add("drawer-state");
    box.setAttribute("role", "status");
    box.textContent = "Loading ticket…";
    return box;
  }

  // desc: {title, detail, retryable}. Returns {node, retry}.
  function renderDrawerError(doc, desc) {
    var box = doc.createElement("div");
    box.classList.add("drawer-state");
    box.setAttribute("role", "alert");
    box.appendChild(errorLine(doc, "error-title", desc.title));
    if (desc.detail) box.appendChild(errorLine(doc, "error-detail", desc.detail));
    var retry = null;
    if (desc.retryable) {
      retry = doc.createElement("button");
      retry.setAttribute("type", "button");
      retry.classList.add("button");
      retry.textContent = "Retry";
      box.appendChild(retry);
    }
    return { node: box, retry: retry };
  }

  function isInPage(doc, element) {
    for (var p = element; p; p = p.parentNode) {
      if (p === doc.body) return true;
    }
    return false;
  }

  // ---------- the controller ----------
  // `deps` (tests only): ticketTimeoutMs.
  function initDrawer(app, doc, win, fetchImpl, deps) {
    var els = app.els;
    var timeout = (deps && deps.ticketTimeoutMs) || TICKET_TIMEOUT_MS;
    var state = { open: false, opener: null, seq: 0, id: null, controller: null };

    function setBody(node) {
      clearChildren(els.drawerBody);
      els.drawerBody.appendChild(node);
      els.drawerBody.scrollTop = 0;
    }

    function setBusy(busy) {
      els.drawerBody.setAttribute("aria-busy", busy ? "true" : "false");
    }

    function cancelPending() {
      if (state.controller) state.controller.abort();
      state.controller = null;
    }

    function showError(id, desc) {
      var parts = renderDrawerError(doc, desc);
      setBody(parts.node);
      setBusy(false);
      if (parts.retry) {
        parts.retry.addEventListener("click", function () {
          load(id);
          els.drawerTitle.focus(); // the Retry button is gone; keep focus inside the dialog
        });
      }
    }

    function load(id) {
      cancelPending();
      var token = ++state.seq;
      var controller = typeof AbortController === "function" ? new AbortController() : null;
      state.controller = controller;
      state.id = id;
      els.drawerTitle.textContent = id;
      setBody(renderDrawerLoading(doc));
      setBusy(true);
      requestJson(fetchImpl, "/tickets/" + id, {
        timeoutMs: timeout,
        signal: controller ? controller.signal : undefined
      }).then(function (result) {
        // A newer load, or closing the drawer, makes this reply stale.
        if (token !== state.seq || !state.open) return;
        state.controller = null;
        if (result.kind === "http" && result.status === 200) {
          var ticket = validateTicket(result.body, id);
          if (ticket) {
            els.drawerTitle.textContent = drawerTitleText(id, ticket.title);
            setBody(renderTicketBody(doc, ticket));
            setBusy(false);
            return;
          }
          showError(id, describeError({ kind: "invalid" }));
        } else if (result.kind === "http" && result.status === 404) {
          showError(id, { title: "Ticket " + id + " was not found", detail: "", retryable: false });
        } else {
          showError(id, describeError(result));
        }
      });
    }

    // Returns false when the id is not a ticket id (nothing is fetched and nothing opens).
    function open(rawId, opener) {
      var id = typeof rawId === "string" ? rawId.trim().toUpperCase() : "";
      if (!TICKET_ID_PATTERN.test(id)) return false;
      if (!state.open) {
        state.open = true;
        state.opener = opener || null;
        doc.body.classList.add("drawer-open");
        els.drawer.showModal();
      }
      load(id);
      els.drawerTitle.focus();
      return true;
    }

    // Runs for every way of closing: the Close button, Esc, or code. The browser fires "close" after the dialog closed.
    function onClosed() {
      if (!state.open) return;
      state.open = false;
      state.seq += 1; // any reply still on its way is now stale
      cancelPending();
      doc.body.classList.remove("drawer-open");
      var opener = state.opener;
      state.opener = null;
      if (opener && isInPage(doc, opener)) opener.focus();
      if (!opener || doc.activeElement !== opener) els.question.focus();
    }

    els.drawerClose.addEventListener("click", function () {
      els.drawer.close();
    });
    els.drawer.addEventListener("close", onClosed);

    return {
      state: state,
      open: open,
      close: function () {
        els.drawer.close();
      }
    };
  }

  // ---------- start-up in a browser (nothing runs under Node) ----------
  if (typeof document !== "undefined" && typeof window !== "undefined") {
    try {
      applyEarlyTheme(document, window);
    } catch (err) {
      // the page works without a saved theme
    }
    var start = function () {
      try {
        init(document, window, null);
      } catch (err) {
        if (typeof console !== "undefined") console.error(String(err && err.message ? err.message : err));
      }
    };
    if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", start);
    else start();
  }

  // ---------- exports for the Node tests (nothing is added to the page) ----------
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      constants: {
        MAX_QUESTION: MAX_QUESTION, MAX_HISTORY_CONTENT: MAX_HISTORY_CONTENT, HISTORY_TURNS: HISTORY_TURNS,
        K_MIN: K_MIN, K_MAX: K_MAX, K_DEFAULT: K_DEFAULT, REQUEST_TIMEOUT_MS: REQUEST_TIMEOUT_MS,
        MAX_RETRY_AFTER: MAX_RETRY_AFTER, THEME_KEY: THEME_KEY
      },
      normaliseId: normaliseId,
      toIdSet: toIdSet,
      classifyCitation: classifyCitation,
      parseAnswer: parseAnswer,
      formatDuration: formatDuration,
      parseRetryAfter: parseRetryAfter,
      describeError: describeError,
      truncateForHistory: truncateForHistory,
      buildHistory: buildHistory,
      validateQuestion: validateQuestion,
      clampK: clampK,
      buildChatPayload: buildChatPayload,
      isTheme: isTheme,
      resolveTheme: resolveTheme,
      nextTheme: nextTheme,
      themeToggleLabel: themeToggleLabel,
      readStoredTheme: readStoredTheme,
      writeStoredTheme: writeStoredTheme,
      // group B
      FILTER_FIELDS: FILTER_FIELDS,
      DATE_FIELDS: DATE_FIELDS,
      ELEMENT_IDS: ELEMENT_IDS,
      requestJson: requestJson,
      normaliseHealth: normaliseHealth,
      formatCount: formatCount,
      describeHealth: describeHealth,
      emptyFilterOptions: emptyFilterOptions,
      isRealDate: isRealDate,
      normaliseFilterOptions: normaliseFilterOptions,
      dateRangeProblem: dateRangeProblem,
      dateInputLimits: dateInputLimits,
      emptySelection: emptySelection,
      activeFilterList: activeFilterList,
      clearChildren: clearChildren,
      renderSelectOptions: renderSelectOptions,
      renderActiveFilters: renderActiveFilters,
      renderHealth: renderHealth,
      renderFiltersNotice: renderFiltersNotice,
      renderDateProblem: renderDateProblem,
      renderDateLimits: renderDateLimits,
      renderThemeButton: renderThemeButton,
      renderSidebar: renderSidebar,
      applyEarlyTheme: applyEarlyTheme,
      collectElements: collectElements,
      init: init,
      // group C
      shownText: shownText,
      referenceId: referenceId,
      validateChatResponse: validateChatResponse,
      priorityClass: priorityClass,
      scoreText: scoreText,
      nearBottom: nearBottom,
      scrollToBottom: scrollToBottom,
      createCountdowns: createCountdowns,
      renderUserMessage: renderUserMessage,
      renderChip: renderChip,
      renderMutedId: renderMutedId,
      renderAnswerBody: renderAnswerBody,
      renderSourceCard: renderSourceCard,
      renderSources: renderSources,
      renderAssistantMessage: renderAssistantMessage,
      renderLoading: renderLoading,
      renderErrorBubble: renderErrorBubble,
      initChat: initChat,
      // group D
      longText: longText,
      formatTimestamp: formatTimestamp,
      validateTicket: validateTicket,
      renderTicketBody: renderTicketBody,
      renderDrawerLoading: renderDrawerLoading,
      renderDrawerError: renderDrawerError,
      initDrawer: initDrawer
    };
  }
})();
