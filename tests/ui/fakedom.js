"use strict";
// A small fake DOM for testing app.js without a browser. It is strict on purpose: it THROWS on everything the page
// must never do: innerHTML, outerHTML, insertAdjacentHTML, any use of .style, and on*/style/href/src attributes.
// It parses the real index.html into a tree, so ids, nesting, classes and starting attributes are the real ones.
// Like a browser, focus() does nothing for an element inside an `inert` subtree.
const fs = require("fs");

const FORBIDDEN_ATTRIBUTE = /^(on.*|style|href|src|srcdoc|action|formaction)$/i;
const forbid = (what) => () => {
  throw new Error("FORBIDDEN in page code: " + what);
};
const VOID_TAGS = new Set(["meta", "link", "input", "br", "img", "hr", "source", "area", "base", "col", "embed", "param", "track", "wbr"]);

class FakeText {
  constructor(doc, text) {
    this.ownerDocument = doc;
    this.nodeType = 3;
    this.textContent = String(text);
    this.parentNode = null;
  }
}

class FakeElement {
  constructor(doc, tag) {
    this.ownerDocument = doc;
    this.nodeType = 1;
    this.tagName = String(tag).toUpperCase();
    this.attrs = new Map();
    this.children = [];
    this.parentNode = null;
    this.listeners = {};
    this._text = "";
    this._value = "";
    this._classes = new Set();
    this.disabled = false;
    this.dataset = {};
    this.scrollTop = 0;
    this.scrollHeight = 0;
    this.clientHeight = 0;
    this.scrollCalls = [];
    const el = this;
    this.classList = {
      add: (...names) => names.forEach((n) => el._classes.add(n)),
      remove: (...names) => names.forEach((n) => el._classes.delete(n)),
      contains: (n) => el._classes.has(n),
      toggle: (n, force) => {
        const want = force === undefined ? !el._classes.has(n) : !!force;
        if (want) el._classes.add(n);
        else el._classes.delete(n);
        return want;
      },
    };
  }
  get id() { return this.attrs.get("id") || ""; }
  get className() { return [...this._classes].join(" "); }
  set className(v) { this._classes = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get hidden() { return this.attrs.has("hidden"); }
  set hidden(v) { if (v) this.attrs.set("hidden", ""); else this.attrs.delete("hidden"); }
  get firstChild() { return this.children[0] || null; }
  get textContent() { return this._text + this.children.map((c) => c.textContent).join(""); }
  set textContent(v) {
    this.children.forEach((c) => { c.parentNode = null; });
    this.children = [];
    this._text = String(v);
  }
  get value() {
    if (this.tagName === "OPTION") return this.attrs.has("value") ? this.attrs.get("value") : this.textContent;
    return this._value;
  }
  set value(v) {
    if (this.tagName === "SELECT") {
      const found = this.children.find((c) => c.tagName === "OPTION" && (c.attrs.has("value") ? c.attrs.get("value") : c.textContent) === String(v));
      this._value = found ? String(v) : "";
    } else {
      this._value = String(v);
    }
  }
  appendChild(child) {
    if (child.parentNode) child.parentNode.removeChild(child);
    child.parentNode = this;
    this.children.push(child);
    return child;
  }
  removeChild(child) {
    const i = this.children.indexOf(child);
    if (i === -1) throw new Error("removeChild: not a child");
    this.children.splice(i, 1);
    child.parentNode = null;
    return child;
  }
  setAttribute(name, value) {
    if (FORBIDDEN_ATTRIBUTE.test(name)) throw new Error("FORBIDDEN attribute in page code: " + name);
    this._setAttr(name, value);
  }
  _setAttr(name, value) { // used by the HTML parser, which may set the attributes the page code may not
    this.attrs.set(name, String(value));
    if (name === "class") this.className = value;
    if (name === "value" && this.tagName !== "OPTION") this._value = String(value);
  }
  getAttribute(name) { return this.attrs.has(name) ? this.attrs.get(name) : null; }
  hasAttribute(name) { return this.attrs.has(name); }
  removeAttribute(name) { this.attrs.delete(name); }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  fire(type, props) {
    const event = { type, target: this, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props };
    for (const fn of [...(this.listeners[type] || [])]) fn(event);
    return event;
  }
  focus() {
    for (let p = this; p; p = p.parentNode) {
      if (p.attrs && p.attrs.has("inert")) {
        this.ownerDocument.focusBlocked.push(this.id || this.tagName);
        return; // like a browser: an inert element cannot take focus
      }
    }
    this.ownerDocument.activeElement = this;
    this.ownerDocument.focusLog.push(this.id || this.tagName);
  }
  scrollTo(options) {
    this.scrollCalls.push(options);
    this.scrollTop = this.scrollHeight;
  }
  // <dialog>, as far as the page uses it: showModal throws when already open (like a browser), close fires "close".
  get open() { return this.attrs.has("open"); }
  showModal() {
    if (this.tagName !== "DIALOG") throw new Error("showModal on a non-dialog");
    if (this.attrs.has("open")) throw new Error("InvalidStateError: the dialog is already open");
    this.attrs.set("open", "");
    this.ownerDocument.modalLog.push("open");
  }
  close() {
    if (this.tagName !== "DIALOG") throw new Error("close on a non-dialog");
    if (!this.attrs.has("open")) return;
    this.attrs.delete("open");
    this.ownerDocument.modalLog.push("close");
    this.fire("close");
  }
  getElementsByClassName(name) {
    const out = [];
    this.walk((e) => { if (e !== this && e.classList && e.classList.contains(name)) out.push(e); });
    return out;
  }
  walk(visit) {
    visit(this);
    this.children.forEach((c) => { if (c.walk) c.walk(visit); });
  }
}
for (const name of ["innerHTML", "outerHTML", "style", "innerText", "href", "src", "srcdoc", "onclick"]) {
  Object.defineProperty(FakeElement.prototype, name, { get: forbid(name), set: forbid(name), configurable: true });
}
FakeElement.prototype.insertAdjacentHTML = forbid("insertAdjacentHTML");
FakeElement.prototype.insertAdjacentElement = forbid("insertAdjacentElement");

class FakeDocument {
  constructor(htmlPath) {
    this.activeElement = null;
    this.focusLog = [];
    this.focusBlocked = [];
    this.modalLog = [];
    this.created = [];
    this.byId = new Map();
    this.listeners = {};
    this.readyState = "complete";
    this.documentElement = new FakeElement(this, "html");
    this.body = new FakeElement(this, "body");
    if (htmlPath) this._parse(fs.readFileSync(htmlPath, "utf8"));
  }
  // Builds the tree of <body> from index.html. Only what that file uses: tags, quoted or bare attributes, text.
  _parse(html) {
    const start = html.indexOf("<body");
    const body = html.slice(html.indexOf(">", start) + 1, html.lastIndexOf("</body>"));
    const re = /<!--[\s\S]*?-->|<\/(\w+)\s*>|<(\w+)((?:\s+[\w-]+(?:="[^"]*")?)*)\s*\/?>|([^<]+)/g;
    const stack = [this.body];
    let m;
    while ((m = re.exec(body))) {
      const parent = stack[stack.length - 1];
      if (m[1]) { // closing tag
        if (stack.length > 1 && stack[stack.length - 1].tagName.toLowerCase() === m[1].toLowerCase()) stack.pop();
      } else if (m[2]) { // opening tag
        const el = new FakeElement(this, m[2]);
        for (const a of (m[3] || "").matchAll(/([\w-]+)(?:="([^"]*)")?/g)) el._setAttr(a[1], a[2] === undefined ? "" : a[2]);
        parent.appendChild(el);
        if (el.attrs.has("id")) this.byId.set(el.attrs.get("id"), el);
        if (!VOID_TAGS.has(m[2].toLowerCase())) stack.push(el);
      } else if (m[4] !== undefined && m[4].trim() !== "") { // text
        parent.appendChild(new FakeText(this, m[4].replace(/&amp;/g, "&").replace(/&lt;/g, "<").replace(/&gt;/g, ">").trim()));
      }
    }
  }
  createElement(tag) { this.created.push(String(tag).toLowerCase()); return new FakeElement(this, tag); }
  createTextNode(text) { return new FakeText(this, text); }
  getElementById(id) { return this.byId.get(id) || null; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  removeEventListener(type, fn) { this.listeners[type] = (this.listeners[type] || []).filter((f) => f !== fn); }
  fire(type, props) {
    const event = { type, ...props };
    for (const fn of [...(this.listeners[type] || [])]) fn(event);
    return event;
  }
}

// A window with matchMedia, localStorage and (optionally) hostile storage.
//   options: {dark, narrow, reducedMotion, storage: "ok" | "throws-on-use" | "throws-on-access" | "missing", stored}
function makeWindow(options = {}) {
  const media = {};
  const matches = (q) => (q.includes("prefers-color-scheme: dark") ? !!options.dark
    : q.includes("prefers-reduced-motion") ? !!options.reducedMotion
    : q.includes("max-width") ? options.narrow !== false : false);
  const store = new Map();
  if (options.stored) store.set("ops-theme", options.stored);
  const win = {
    matchMedia(query) {
      media[query] ||= { media: query, matches: matches(query), listeners: [], addEventListener(t, f) { this.listeners.push(f); }, removeEventListener() {} };
      return media[query];
    },
    setMedia(query, value) {
      const mql = win.matchMedia(query);
      mql.matches = value;
      mql.listeners.forEach((f) => f({ matches: value, media: query }));
    },
    store,
  };
  const mode = options.storage || "ok";
  if (mode === "ok") {
    win.localStorage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => { store.set(k, String(v)); } };
  } else if (mode === "throws-on-use") {
    win.localStorage = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("quota"); } };
  } else if (mode === "throws-on-access") {
    Object.defineProperty(win, "localStorage", { get() { throw new Error("SecurityError"); } });
  }
  return win;
}

// fetch fake: routes is {"/health": handler}; a handler returns a response, throws, or returns a promise.
function response(status, body, headers = {}) {
  const lower = Object.fromEntries(Object.entries(headers).map(([k, v]) => [k.toLowerCase(), v]));
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: (n) => (n.toLowerCase() in lower ? lower[n.toLowerCase()] : null) },
    text: () => Promise.resolve(typeof body === "string" ? body : JSON.stringify(body)),
  };
}
function hang(url, init) {
  return new Promise((_, reject) => {
    init.signal.addEventListener("abort", () => reject(Object.assign(new Error("The operation was aborted"), { name: "AbortError" })));
  });
}
function makeFetch(routes, log) {
  return async (url, init) => {
    (log || []).push({ url, method: init.method, headers: init.headers, body: init.body, hasSignal: !!init.signal, signal: init.signal });
    const handler = routes[url];
    if (!handler) throw new Error("no route for " + url);
    return typeof handler === "function" ? handler(url, init) : handler;
  };
}

module.exports = { FakeElement, FakeText, FakeDocument, makeWindow, makeFetch, response, hang };
