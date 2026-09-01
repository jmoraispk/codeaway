const test = require("node:test");
const assert = require("node:assert/strict");
const { backendLabel, stateLabel } = require("./status.js");

test("Codex readiness includes the visible item count", () => {
  assert.equal(backendLabel({ backend: "codex_desktop" }), "Codex");
  assert.equal(stateLabel({ backend: "codex_desktop", ready_count: 2 }), "ready · 2 items");
});

test("Codex without markers is working", () => {
  assert.equal(stateLabel({ backend: "codex_desktop", ready_count: 0 }), "working");
});

test("legacy targets retain their bridge state", () => {
  assert.equal(stateLabel({ backend: "cursor", idle: true }), "idle");
});
