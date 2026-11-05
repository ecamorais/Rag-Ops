"use strict";
// Shared by the five mutation scripts in this folder. Each script lists its mutations; this file runs them.
// A mutation breaks a COPY of app.js on purpose (in a temp folder, always removed) and a suite from tests/ui must notice.
// Usage of a script: [OPS_INDEX_HTML=path] node tests/ui/breaks/mutate_X.js [PATH_TO_APP_JS]   (default: static/app.js)
//
// Per mutation: CAUGHT / MISSED / INVALID, the number of failed tests and their names.
//   INVALID: the anchor did not match exactly once, or the mutated text equals the original. Recorded, the run goes on.
//   CAUGHT but WEAK: the suite exited non-zero without a failed test: it timed out, did not load, or crashed (not a real assertion).
// The unmutated copy must pass first, otherwise nothing is applied.
// Last line: "applied N, caught X (weak W), INVALID I, MISSED M". Exit code 1 if anything was MISSED or INVALID.
const fs = require("fs");
const os = require("os");
const path = require("path");
const { spawnSync } = require("child_process");

const UI_DIR = path.join(__dirname, "..");
const DEFAULT_APP = path.join(__dirname, "..", "..", "..", "static", "app.js");

function once(src, from, to) {
  const n = src.split(from).length - 1;
  if (n !== 1) throw new Error("the anchor matched " + n + " times (must be exactly once): " + from.slice(0, 70).replace(/\n/g, "\\n"));
  return src.replace(from, to);
}

function lines(stdout, label) {
  return (stdout.match(new RegExp("^ {2}" + label + " {2}.*$", "gm")) || []).map((l) => l.replace(new RegExp("^ {2}" + label + " {2}"), ""));
}

function runSuite(suite, file, timeoutMs) {
  const r = spawnSync(process.execPath, [path.join(UI_DIR, suite), file], { encoding: "utf8", timeout: timeoutMs, env: process.env, maxBuffer: 64 * 1024 * 1024 });
  return { r, failed: lines(r.stdout || "", "FAIL"), passed: lines(r.stdout || "", "PASS").length };
}

function runBreaks({ suite, mutations, timeoutMs = 120000 }) {
  const appPath = path.resolve(process.argv[2] || DEFAULT_APP);
  const original = fs.readFileSync(appPath, "utf8");
  const work = fs.mkdtempSync(path.join(os.tmpdir(), "ui-brk-"));
  let applied = 0, caught = 0, weak = 0, invalid = 0, missed = 0;
  try {
    console.log("breaking COPIES of: " + appPath + "\nsuite: " + suite + "\ntemp folder: " + work);
    const base = path.join(work, "unmutated.js");
    fs.writeFileSync(base, original);
    const first = runSuite(suite, base, timeoutMs);
    if (first.r.status !== 0 || first.failed.length > 0 || first.passed === 0 || !/ALL PASSED/.test(first.r.stdout || "")) {
      console.log("PRECONDITION FAILED: the unmutated copy does not pass " + suite + " (exit " + first.r.status + ", " + first.failed.length + " failed, " + first.passed + " passed). Nothing was applied.");
      process.exitCode = 2;
      return;
    }
    console.log("unmutated copy passes: " + first.passed + " tests\n");
    for (const [name, fn] of mutations) {
      let mutated;
      try {
        mutated = fn(original);
      } catch (error) {
        invalid++;
        console.log("INVALID " + name + "  -> " + error.message);
        continue;
      }
      if (mutated === original) {
        invalid++;
        console.log("INVALID " + name + "  -> the mutated text equals the original");
        continue;
      }
      const file = path.join(work, name.split(" ")[0] + ".js");
      fs.writeFileSync(file, mutated);
      if (fs.readFileSync(file, "utf8") !== mutated) {
        invalid++;
        console.log("INVALID " + name + "  -> the file on disk is not the mutated text");
        continue;
      }
      applied++;
      const { r, failed, passed } = runSuite(suite, file, timeoutMs);
      let state, reason = "";
      if (r.error || r.signal) {
        state = "CAUGHT"; reason = "WEAK: the run timed out or was killed (" + (r.error ? r.error.code : r.signal) + ")";
      } else if (r.status === 0) {
        state = "MISSED";
      } else if (failed.length > 0) {
        state = "CAUGHT";
      } else if (passed === 0) {
        state = "CAUGHT"; reason = "WEAK: the suite exited " + r.status + " before any test ran (the mutated file did not load)";
      } else {
        state = "CAUGHT"; reason = "WEAK: the suite exited " + r.status + " after " + passed + " passed tests with no failed test (it crashed)";
      }
      if (state === "CAUGHT") { caught++; if (reason) weak++; } else missed++;
      console.log(state + " " + name + "  -> exit " + r.status + ", " + failed.length + " test(s) failed" + (reason ? "  [" + reason + "]" : ""));
      failed.forEach((f) => console.log("          failed: " + f));
    }
  } finally {
    fs.rmSync(work, { recursive: true, force: true });
  }
  console.log("\napplied " + applied + ", caught " + caught + " (weak " + weak + "), INVALID " + invalid + ", MISSED " + missed);
  process.exitCode = missed || invalid ? 1 : 0;
}

module.exports = { once, runBreaks };
