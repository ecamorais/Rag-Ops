"use strict";
// Runs the five front-end suites in this folder against static/app.js and checks their counts.
// Usage: node tests/ui/run_all.js            (env OPS_APP_JS=path runs them against another copy of app.js; OPS_INDEX_HTML=path overrides the page)
// Exit code 1 if a suite fails, times out, or reports another number of tests than expected, or prints no "ALL PASSED: N passed" line.
const path = require("path");
const { spawnSync } = require("child_process");

const ROOT = path.join(__dirname, "..", "..");
const TIMEOUT_MS = 120000;
const SUITES = [["test_pure.js", 116], ["test_dom.js", 38], ["test_inert.js", 10], ["test_chat.js", 47], ["test_drawer.js", 27]];
const appArgs = process.env.OPS_APP_JS ? [process.env.OPS_APP_JS] : [];

let total = 0, expectedTotal = 0, problems = 0;
for (const [suite, expected] of SUITES) {
  expectedTotal += expected;
  const r = spawnSync(process.execPath, [path.join(__dirname, suite), ...appArgs], { cwd: ROOT, encoding: "utf8", timeout: TIMEOUT_MS, env: process.env, maxBuffer: 64 * 1024 * 1024 });
  const out = r.stdout || "";
  const summary = out.match(/^ALL PASSED: (\d+) passed, 0 failed$/m);
  const passLines = (out.match(/^ {2}PASS {2}/gm) || []).length;
  const failLines = out.match(/^ {2}FAIL {2}.*$/gm) || [];
  const faults = [];
  if (r.error || r.signal) faults.push("timed out or was killed (" + (r.error ? r.error.code : r.signal) + ")");
  else if (r.status !== 0) faults.push("exit code " + r.status);
  if (!summary) faults.push('no "ALL PASSED: N passed, 0 failed" line');
  else if (Number(summary[1]) !== expected) faults.push("reports " + summary[1] + " passed, expected " + expected);
  if (passLines !== expected) faults.push(passLines + " PASS lines, expected " + expected);
  if (failLines.length) faults.push(failLines.length + " FAIL line(s)");
  if (faults.length) {
    problems++;
    console.log("FAIL " + suite + ": " + faults.join("; "));
    failLines.forEach((l) => console.log("    " + l.trim()));
    if (r.stderr) console.log("    stderr: " + r.stderr.trim().split("\n").slice(0, 5).join(" | "));
  } else {
    total += expected;
    console.log("ok   " + suite + ": " + expected + " passed");
  }
}
console.log("\ntotal " + total + " of " + expectedTotal + " tests passed in " + (SUITES.length - problems) + " of " + SUITES.length + " suites");
process.exit(problems ? 1 : 0);
