const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");

const helperPath = path.join(__dirname, "navigator_ui.js");

function loadHelpers() {
  assert.equal(fs.existsSync(helperPath), true, "navigator_ui.js must exist");
  return require(helperPath);
}

function fakeProject(expanded = true) {
  const classes = new Set(expanded ? [] : ["collapsed"]);
  const attributes = new Map([["aria-expanded", String(expanded)]]);
  const section = {
    classList: {
      toggle(name, force) {
        if (force) classes.add(name);
        else classes.delete(name);
      },
      contains(name) { return classes.has(name); },
    },
  };
  const button = {
    closest(selector) { return selector === ".navigator-project" ? section : null; },
    getAttribute(name) { return attributes.get(name) ?? null; },
    setAttribute(name, value) { attributes.set(name, String(value)); },
  };
  return { section, button, attributes };
}

test("project expansion changes locally and can roll back", () => {
  const { beginProjectExpansion } = loadHelpers();
  const { section, button, attributes } = fakeProject(true);

  const rollback = beginProjectExpansion(button, "SummonLab", false);

  assert.equal(section.classList.contains("collapsed"), true);
  assert.equal(attributes.get("aria-expanded"), "false");
  assert.equal(attributes.get("aria-label"), "Expand SummonLab");

  rollback();
  assert.equal(section.classList.contains("collapsed"), false);
  assert.equal(attributes.get("aria-expanded"), "true");
  assert.equal(attributes.get("aria-label"), "Collapse SummonLab");
});

test("only initial and explicit refreshes show syncing", () => {
  const { shouldShowNavigatorSync } = loadHelpers();

  assert.equal(shouldShowNavigatorSync({ announce: false, hasSnapshot: false }), true);
  assert.equal(shouldShowNavigatorSync({ announce: true, hasSnapshot: true }), true);
  assert.equal(shouldShowNavigatorSync({ announce: false, hasSnapshot: true }), false);
});
