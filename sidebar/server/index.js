#!/usr/bin/env node
/*
 * Claude Usage Page - sidebar extension for Claude Desktop.
 *
 * A tiny local MCP server with one read-only tool, get_usage. It hands the
 * sidebar artifact the data.json that the page's own build writes next to
 * index.html (~/.claude-usage-page). Before reading, it asks the page server
 * on 127.0.0.1 to rebuild if its last build is over a minute old, the same
 * thing reloading the browser page does. It reads only that folder's
 * data.json and port file, writes nothing, and connects only to that local
 * server. Whoever calls the tool gets the whole snapshot: if Claude calls it
 * in a conversation, it goes to Claude like any tool result.
 *
 * No dependencies; runs on the Node.js that ships with Claude Desktop.
 * Speaks MCP over stdio (newline-delimited JSON-RPC 2.0).
 *   node server/index.js --print   shows what the tool would return
 */
"use strict";

const fs = require("fs");
const http = require("http");
const os = require("os");
const path = require("path");

const NAME = "claude-usage-page";
const VERSION = "0.2.1";  // the same as manifest.json
const TOOL = "get_usage";

function home() {
  return process.env.CLAUDE_USAGE_PAGE_DIR || path.join(os.homedir(), ".claude-usage-page");
}

function port() {
  try {
    const n = parseInt(fs.readFileSync(path.join(home(), "port"), "utf8"), 10);
    if (n > 0 && n < 65536) return n;
  } catch {
    // installed before the port file existed: the default
  }
  return 8788;
}

// The page server rebuilds when its page is requested and the last build is over a minute
// old. Ask it, and carry on with whatever data.json holds if it isn't running.
function refresh(timeoutMs) {
  return new Promise((resolve) => {
    // A direct agent of its own, so a proxy configured for Node never sees this request.
    const agent = new http.Agent({ keepAlive: false });
    const req = http.get({ host: "127.0.0.1", port: port(), path: "/", timeout: timeoutMs, agent }, (res) => {
      res.resume();
      res.on("end", () => resolve(res.statusCode === 200));
      res.on("error", () => resolve(false));
    });
    req.on("timeout", () => { req.destroy(); resolve(false); });
    req.on("error", () => resolve(false));
  });
}

async function collect() {
  // Long enough for a first build after an upgrade, which reads every file again.
  const refreshed = await refresh(30000);
  const file = path.join(home(), "data.json");
  let text;
  try {
    text = fs.readFileSync(file, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") {
      throw new Error("No usage data yet. Install Claude Usage Page first (install.sh or install.ps1), " +
        "or reinstall it so it writes data.json.");
    }
    throw error;
  }
  // An older release builds index.html but not data.json: after going back to one, the snapshot
  // stops changing while the page moves on. Say so rather than serve old numbers as current.
  try {
    if (fs.statSync(path.join(home(), "index.html")).mtimeMs - fs.statSync(file).mtimeMs > 60000) {
      throw new Error("The usage page on this computer is now built by a version that doesn't feed this card. " +
        "Reinstall the current Claude Usage Page to bring the card back.");
    }
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  // The page the card draws: the template installed with the page, so updating the page updates
  // the card too. A card left from before this falls back to the copy built into it.
  let page = null;
  try {
    page = fs.readFileSync(path.join(home(), "template.html"), "utf8");
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
  }
  return { data: JSON.parse(text), refreshed, page };
}

const TOOL_INFO = {
  name: TOOL,
  title: "Claude Code and Codex usage",
  description: "The usage data Claude Usage Page built on this computer, for its sidebar card: the computer's " +
    "user name, daily and hourly activity, sessions, tokens per model and, when priced, the estimated cost at " +
    "API list prices, along with the page that draws it. Read-only.",
  inputSchema: { type: "object", properties: {}, additionalProperties: false },
  annotations: { readOnlyHint: true, openWorldHint: false },
};

class RpcError extends Error {
  constructor(code, message) {
    super(message);
    this.code = code;
  }
}

async function handle(request) {
  const params = request.params || {};
  switch (request.method) {
    case "initialize":
      return {
        protocolVersion: params.protocolVersion || "2025-06-18",
        capabilities: { tools: {} },
        serverInfo: { name: NAME, version: VERSION },
      };
    case "ping":
      return {};
    case "tools/list":
      return { tools: [TOOL_INFO] };
    case "tools/call": {
      if (params.name !== TOOL) throw new RpcError(-32602, `Unknown tool: ${params.name}`);
      try {
        // One copy, as JSON text: the card reads it back as the call's payload, and it keeps a
        // heavy user's snapshot well under the desktop app's limit on tool replies.
        return { content: [{ type: "text", text: JSON.stringify(await collect()) }] };
      } catch (error) {
        return { content: [{ type: "text", text: error.message }], isError: true };
      }
    }
    default:
      throw new RpcError(-32601, `Method not found: ${request.method}`);
  }
}

function serve() {
  let buffer = "";
  let pending = 0;
  let ended = false;
  const finish = () => { if (ended && !pending) process.stdout.write("", () => process.exit(0)); };
  process.stdin.setEncoding("utf8");
  process.stdin.on("data", (chunk) => {
    buffer += chunk;
    let newline;
    while ((newline = buffer.indexOf("\n")) !== -1) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (!line) continue;
      let request;
      try {
        request = JSON.parse(line);
      } catch {
        continue;
      }
      if (!request || typeof request !== "object" || !("id" in request)) continue; // notifications
      pending += 1;
      handle(request).then(
        (result) => ({ jsonrpc: "2.0", id: request.id, result }),
        (error) => ({ jsonrpc: "2.0", id: request.id, error: { code: error.code || -32603, message: error.message } }),
      ).then((reply) => {
        process.stdout.write(`${JSON.stringify(reply)}\n`);
        pending -= 1;
        finish();
      });
    }
  });
  // Exit once every reply is written: on macOS a pipe write is asynchronous, and exiting
  // early cuts a long reply at the 64 KB pipe buffer.
  process.stdin.on("end", () => { ended = true; finish(); });
}

if (process.argv.includes("--print")) {
  collect().then((p) => console.log(JSON.stringify({ refreshed: p.refreshed, generatedAt: p.data.generatedAt,
    days: Object.keys(p.data.days || {}).length, codex: !!p.data.codex }, null, 2)),
  (e) => { console.error(e.message); process.exit(1); });
} else {
  serve();
}
