// CI bridge: run ESLint with the JSON formatter and translate every error into
// a GitHub workflow-command annotation (::error file=...,line=...), so lint
// failures land inline on the PR diff. Exits 1 when any error was found.
//
// Kept as a tiny script instead of an action because the JSON output format is
// a stable ESLint API and the mapping is ~30 lines.

import { execFileSync } from "node:child_process";

const root = new URL("../..", import.meta.url).pathname
  .replace(/^\/([A-Za-z]:)/, "$1")
  .replace(/\/$/, ""); // strip trailing slash so slice(root.length + 1) is exact

let result;
try {
  // On Windows, pnpm is a .cmd shim and must go through the shell.
  result = execFileSync(
    process.platform === "win32" ? "pnpm.cmd" : "pnpm",
    ["exec", "eslint", "--quiet", "-f", "json", "."],
    { cwd: root, encoding: "utf8", maxBuffer: 64 * 1024 * 1024, shell: process.platform === "win32" },
  );
} catch (error) {
  // ESLint exits non-zero both when errors exist and on crashes; the JSON
  // output distinguishes them (empty messages array = crash).
  result = error.stdout ?? "";
}

const files = JSON.parse(result);
let errorCount = 0;
for (const file of files) {
  const relPath = file.filePath.replace(/\\/g, "/");
  for (const message of file.messages) {
    if (message.severity < 2) continue; // --quiet already filters; belt and braces
    errorCount++;
    const title = message.ruleId ?? "eslint";
    // GitHub workflow commands require the file path relative to the repo root.
    const fileArg = relPath.startsWith(root) ? relPath.slice(root.length + 1) : relPath;
    process.stdout.write(
      `::error file=${fileArg},line=${message.line},col=${message.column},title=${title}::${message.message}\n`,
    );
  }
}

if (errorCount > 0) {
  process.stderr.write(`eslint found ${errorCount} error(s).\n`);
  process.exit(1);
}
