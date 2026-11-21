// Runs the sidebar card's loader script against a stand-in for the page and the Claude app,
// and prints what it showed. Input on stdin: {html, mcp: "none" | {events: [...]}}.
// Each event is {data: payload} or {error: {code, message}}, delivered to the watch in order;
// {click: label} presses that button on the note, and {answer: payload} completes the oldest
// call the card made with callTool.
const vm = require("vm");

const input = JSON.parse(require("fs").readFileSync(0, "utf8"));
const script = /<script>([\s\S]*)<\/script>/.exec(input.html)[1];

function element() {
  let doc = "";
  return {
    hidden: false, textContent: "", innerHTML: "", children: [], listeners: {}, loads: 0,
    get srcdoc() { return doc; },
    set srcdoc(v) { doc = v; if (v) this.loads += 1; },  // each non-empty assignment reloads the page
    append(...items) { for (const i of items) { if (typeof i === "string") this.textContent += i; else this.children.push(i); } },
    addEventListener(type, fn) { this.listeners[type] = fn; },
  };
}
const elements = { note: element(), page: element() };
const watches = [], calls = [];
let stopped = 0;
const mcp = input.mcp === "none" ? null : {
  watchTool(server, tool, args, handler, options) {
    const w = { server, tool, args, handler, options, live: true };
    watches.push(w);
    return () => { stopped += 1; w.live = false; };  // an unsubscribed watch hears nothing more
  },
  callTool: () => new Promise(resolve => calls.push(resolve)),
};
const sandbox = {
  document: { getElementById: id => elements[id], createElement: () => element() },
  window: { claude: { use: async name => (name === "mcp" ? mcp : null) } },
  location: { reload() {} },
  JSON, console,
};
vm.createContext(sandbox);
vm.runInContext(script, sandbox);

setTimeout(async () => {
  for (const ev of (input.mcp && input.mcp.events) || []) {
    if (ev.click) elements.note.children.find(c => c.textContent === ev.click).listeners.click();
    else if (ev.answer) calls.shift()({ payload: ev.answer });
    else for (const w of watches.filter(w => w.live)) w.handler(ev.data ? { type: "data", result: { payload: ev.data } } : { type: "error", error: ev.error });
    await new Promise(r => setTimeout(r, 0));
  }
  const { note, page } = elements;
  console.log(JSON.stringify({
    note: { hidden: note.hidden, text: note.textContent, buttons: note.children.map(c => c.textContent).filter(Boolean) },
    page: { hidden: page.hidden, srcdoc: page.srcdoc, loads: page.loads },
    watches: watches.map(({ server, tool, args, options }) => ({ server, tool, args, options })),
    stopped,
  }));
}, 20);
