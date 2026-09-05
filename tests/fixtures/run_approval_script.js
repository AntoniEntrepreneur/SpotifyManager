// Minimal DOM stub good for exactly what the approval page's inline scripts touch --
// getElementById lookups by a fixed element registry, classList/dataset/textContent,
// event listeners, and no-op querySelectorAll/closest for the group-less (empty
// plan) case. Not a browser: just enough to prove the click handler really wires up
// and really posts, which is the one thing string-matching the rendered HTML cannot
// prove for a JS event listener.
//
// Usage: node run_approval_script.js <script-file> <elements-json>
// Prints one JSON line: {"fetchCalls": [[url, body], ...]}

const fs = require("fs");

function makeElement(id, attrs) {
  attrs = attrs || {};
  return {
    id: id,
    hidden: !!attrs.hidden,
    textContent: attrs.textContent || "",
    innerHTML: "",
    disabled: false,
    dataset: {},
    _listeners: {},
    classList: {
      add() {},
      remove() {},
      toggle() {},
    },
    addEventListener(event, handler) {
      (this._listeners[event] = this._listeners[event] || []).push(handler);
    },
    dispatch(event, evt) {
      (this._listeners[event] || []).forEach((h) => h(evt || {}));
    },
    querySelectorAll() {
      return [];
    },
    closest() {
      return null;
    },
    appendChild() {},
    remove() {},
  };
}

const scriptFile = process.argv[2];
const elementsSpec = JSON.parse(process.argv[3]);

const registry = {};
for (const [id, attrs] of Object.entries(elementsSpec)) {
  registry[id] = makeElement(id, attrs);
}

const body = makeElement("body");
const fetchCalls = [];

global.document = {
  getElementById(id) {
    return registry[id] || null;
  },
  body: body,
  createElement() {
    return makeElement(null);
  },
};

global.window = {
  location: { href: null },
  confirm() {
    return true;
  },
};

global.fetch = function (url, opts) {
  fetchCalls.push([url, opts && opts.body]);
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ status: "received", message: "ok" }),
  });
};

const script = fs.readFileSync(scriptFile, "utf-8");
eval(script);

const approveButton = registry["approve"];
if (approveButton) {
  approveButton.dispatch("click");
}

// fetch() is async; give its microtasks a turn before we report.
setTimeout(() => {
  console.log(JSON.stringify({ fetchCalls: fetchCalls }));
}, 50);
