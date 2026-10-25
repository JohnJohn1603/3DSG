"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");

const source = fs.readFileSync("scripts/xyz_renderer.js", "utf8");
const context = { window: {} };
vm.runInNewContext(source, context, { filename: "xyz_renderer.js" });
const gate = context.window.createAtomicRevisionGate();

function deferred() {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

async function main() {
  // Delayed old revision resolves after the newer revision. It must not commit.
  let displayed = "rev-1";
  const oldLoad = deferred();
  const oldRequest = gate.request(() => oldLoad.promise, value => { displayed = value; });
  const newLoad = deferred();
  const newRequest = gate.request(() => newLoad.promise, value => { displayed = value; });
  assert.equal(displayed, "rev-1", "display remains on prior revision while requests are pending");
  newLoad.resolve("rev-3");
  assert.equal((await newRequest).status, "committed");
  assert.equal(displayed, "rev-3");
  oldLoad.resolve("rev-2");
  assert.equal((await oldRequest).status, "stale");
  assert.equal(displayed, "rev-3", "late stale result cannot overwrite the selected revision");

  // A failed asset load must leave the last committed display untouched.
  let reported = null;
  const failure = await gate.request(
    () => Promise.reject(new Error("fixture geometry load failed")),
    value => { displayed = value; },
    error => { reported = error.message; },
  );
  assert.equal(failure.status, "failed");
  assert.equal(reported, "fixture geometry load failed");
  assert.equal(displayed, "rev-3", "failed load retains prior revision");

  // A later request cancels a currently pending historical seek.
  const historical = deferred();
  const historicalRequest = gate.request(() => historical.promise, value => { displayed = value; });
  const follow = await gate.request(async () => "rev-4", value => { displayed = value; });
  historical.resolve("rev-2");
  assert.equal((await historicalRequest).status, "stale");
  assert.equal(follow.status, "committed");
  assert.equal(displayed, "rev-4");

  console.log("atomic revision fixture passed: delayed, out-of-order, failure retention, and cancellation");
}

main().catch(error => { console.error(error); process.exitCode = 1; });
