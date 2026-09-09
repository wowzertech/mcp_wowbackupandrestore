#!/usr/bin/env node
/**
 * WOW AISuite — MCP launcher.
 *
 * Clients like Claude Desktop only accept stdio servers in their config file: a
 * bare URL is dropped on the next save. This bridges the two, so a customer can
 * paste a config block that looks like every other MCP server and still reach a
 * remote, OAuth-protected endpoint.
 *
 * The endpoint lives here rather than in each customer's config on purpose. It
 * means the address is ours to change — publish a new version and everyone
 * follows — instead of being copied into thousands of files we cannot edit.
 *
 * No credentials pass through this file, and there is nowhere to put one. The
 * customer signs in through a browser and the token is held by the OAuth client,
 * which is the point of the remote design: a config file that is safe to share
 * or commit, unlike one carrying a connection string.
 */

"use strict";

const { spawn } = require("node:child_process");
const path = require("node:path");

const DEFAULT_URL = "https://mcp.wowbackupandrestore.com";

// Precedence: an explicit argument, then the environment, then the address this
// version shipped with. The overrides exist for staging and for support staff
// reproducing a customer's problem — neither should require a private build.
const args = process.argv.slice(2);
const passthrough = args.filter((a) => a.startsWith("-"));
const positional = args.filter((a) => !a.startsWith("-"));

const url = positional[0] || process.env.WOW_MCP_URL || DEFAULT_URL;

if (positional[0] === "--version" || args.includes("--version")) {
  console.log(require(path.join(__dirname, "..", "package.json")).version);
  process.exit(0);
}

// require.resolve rather than a bare "mcp-remote": it finds the copy installed
// beside this package, so the version is the one declared in package.json and
// not whatever a global install happens to have.
let remote;
try {
  remote = require.resolve("mcp-remote/dist/proxy.js");
} catch {
  console.error(
    "wow-aisuite-mcp: could not find its bridge dependency (mcp-remote).\n" +
      "Reinstall with:  npm install -g @wow-aisuite/mcp-server"
  );
  process.exit(1);
}

const child = spawn(process.execPath, [remote, url, ...passthrough], {
  // stdio is the protocol channel — the client talks to us over it, so it has
  // to be inherited rather than piped, or every message is lost.
  stdio: "inherit",
  env: process.env,
});

child.on("error", (err) => {
  console.error(`wow-aisuite-mcp: failed to start — ${err.message}`);
  process.exit(1);
});

// Pass the child's fate through, so a client that watches the exit code sees
// what actually happened instead of a blanket success.
child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
    return;
  }
  process.exit(code === null ? 1 : code);
});

for (const signal of ["SIGINT", "SIGTERM"]) {
  process.on(signal, () => child.kill(signal));
}
