// Runs the page's script against a small stand-in for the browser and prints what it drew.
// Input on stdin: {html, language, storage, search, luck, systemDark, clicks: [[elementId, dataset], ...]}.
const vm = require("vm");

const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const script = /<script>([\s\S]*)<\/script>/.exec(input.html)[1];

function element() {
  return {
    hidden: false, innerHTML: "", textContent: "", clientWidth: 780, style: {}, dataset: {}, listeners: {},
    classes: new Set(),
    addEventListener(type, fn) { this.listeners[type] = fn; },
    attrs: {},
    setAttribute(name, value) { this.attrs[name] = String(value); },
    get classList() {
      const classes = this.classes;
      return { toggle: (name, on) => (on ? classes.add(name) : classes.delete(name)) };
    },
  };
}

const elements = {};
const byId = id => (elements[id] = elements[id] || element());
const root = element();
const store = new Map(Object.entries(input.storage || {}));
const pressed = {};
const addressBar = [];

// "#modes button" as stand-in elements read from the page's own markup.
function buttons(selector) {
  const box = /^#(\w+) button$/.exec(selector);
  if (!box) return [];
  const body = new RegExp(`id="${box[1]}"[^>]*>([\\s\\S]*?)</div>`).exec(input.html)[1];
  return [...body.matchAll(/data-(\w+)="([^"]+)"/g)].map(([, key, value]) => ({
    dataset: { [key]: value },
    setAttribute(name, on) { pressed[box[1]] = { ...pressed[box[1]], [value]: on }; },
  }));
}

const sandbox = {
  console,
  innerWidth: 1000,
  navigator: { language: input.language },
  location: { search: input.search || "", pathname: "/" },
  history: { replaceState: (state, title, url) => { addressBar.push(url); } },
  URLSearchParams,
  matchMedia: () => ({ matches: !!input.systemDark, addEventListener() {} }),
  addEventListener() {},
  setTimeout,
  clearTimeout,
  localStorage: { getItem: k => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, v) },
  document: {
    documentElement: root,
    addEventListener() {},
    getElementById: byId,
    querySelector: selector => {
      // A [data-x=y] element starts with the label written in the page's markup.
      const key = "selector:" + selector, fresh = !elements[key], el = byId(key);
      const attr = /^\[data-(\w+)=(\w+)\]$/.exec(selector);
      const label = fresh && attr && new RegExp(`data-${attr[1]}="${attr[2]}"[^>]*>([^<]*)<`).exec(input.html);
      if (label) el.textContent = label[1];
      return el;
    },
    querySelectorAll: selector => buttons(selector),
  },
};

vm.createContext(sandbox);
vm.runInContext(`Math.random = () => ${input.luck || 0};`, sandbox);
vm.runInContext(script, sandbox);

for (const [id, dataset] of input.clicks || []) {
  byId(id).listeners.click({ target: { closest: () => ({ dataset }) } });
}

const out = { classes: [...root.classes], lang: root.lang, title: sandbox.document.title, elements: {}, pressed, addressBar,
  storage: Object.fromEntries(store) };
for (const [id, el] of Object.entries(elements)) {
  out.elements[id] = { hidden: el.hidden, html: el.innerHTML, text: el.textContent, attrs: el.attrs };
}
console.log(JSON.stringify(out));
