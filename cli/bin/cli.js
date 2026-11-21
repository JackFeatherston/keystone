#!/usr/bin/env node
/**
 * Keystone CLI: `npx @keystone-dev/cli init`
 *
 * Adds the Keystone MCP server to the AI clients installed on this machine.
 * For now that means Claude Code and Cursor.
 *
 * How npx runs this file: npx fetches the named package from the npm registry
 * into a local cache, then runs the file listed under "bin" in its
 * package.json. Nothing is installed globally. The shebang on line 1 lets the
 * OS run this file with Node.
 *
 * Package name: "keystone" on npm is the old KeystoneJS CMS, and
 * "keystone-mcp" is a Keystone 6 plugin, so `npx keystone init` would run
 * someone else's code. This package uses the @keystone-dev scope instead,
 * which means creating the "keystone-dev" org on npmjs.com before the first
 * publish.
 *
 * Testing and publishing (from the cli/ folder):
 *   node bin/cli.js init                      run it directly
 *   npm link                                  then `keystone init` works the way it will for users
 *   npm login && npm publish --access public  scoped packages are private unless you pass --access public
 * Bump "version" in package.json before every later publish.
 *
 * What init does:
 *   Claude Code  If `claude` is on PATH, runs `claude mcp add` at user scope.
 *                Claude Code owns its config format, so we don't edit its files.
 *   Cursor       If ~/.cursor exists, adds a "keystone" entry to
 *                ~/.cursor/mcp.json and keeps every other server in the file.
 *                If the file isn't valid JSON, we leave it alone and say so.
 *
 * This depends on OAuth on /mcp (Distribution.MD, step 4). The CLI only writes
 * the server URL and each client handles login itself, which is why it can stay
 * this small. MCP_URL is the planned hosted endpoint. Until OAuth ships, the
 * backend expects the shared API key, so this CLI can't connect anyone yet.
 */
import { spawnSync } from "node:child_process";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

const MCP_URL = "https://mcp.keystone.dev/mcp";

function setupClaudeCode() {
  const probe = spawnSync("claude", ["--version"], { stdio: "ignore" });
  if (probe.error) return "Claude Code: not found, skipped";

  const result = spawnSync(
    "claude",
    ["mcp", "add", "--transport", "http", "--scope", "user", "keystone", MCP_URL],
    { encoding: "utf8" },
  );
  if (result.status === 0) return "Claude Code: added keystone";
  const output = (result.stderr || result.stdout).trim();
  if (/already exists/i.test(output)) return "Claude Code: keystone already configured";
  return `Claude Code: failed (${output})`;
}

function setupCursor() {
  const dir = join(homedir(), ".cursor");
  if (!existsSync(dir)) return "Cursor: not found, skipped";

  const file = join(dir, "mcp.json");
  let config = {};
  if (existsSync(file)) {
    try {
      config = JSON.parse(readFileSync(file, "utf8"));
    } catch {
      return `Cursor: ${file} is not valid JSON, left unchanged`;
    }
  }
  config.mcpServers = { ...config.mcpServers, keystone: { url: MCP_URL } };
  writeFileSync(file, JSON.stringify(config, null, 2) + "\n");
  return `Cursor: added keystone to ${file}`;
}

const [command] = process.argv.slice(2);
if (command === "init") {
  console.log(setupClaudeCode());
  console.log(setupCursor());
} else {
  console.log("Usage: npx @keystone-dev/cli init");
  process.exit(command ? 1 : 0);
}
