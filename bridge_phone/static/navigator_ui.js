function setProjectExpanded(section, button, projectName, expanded) {
  section.classList.toggle("collapsed", !expanded);
  button.setAttribute("aria-expanded", String(expanded));
  button.setAttribute(
    "aria-label",
    `${expanded ? "Collapse" : "Expand"} ${projectName}`
  );
}

function beginProjectExpansion(button, projectName, expanded) {
  const section = button.closest(".navigator-project");
  if (!section) return () => {};
  const previousExpanded = button.getAttribute("aria-expanded") === "true";

  setProjectExpanded(section, button, projectName, expanded);
  return () => setProjectExpanded(
    section,
    button,
    projectName,
    previousExpanded
  );
}

function shouldShowNavigatorSync({ announce = false, hasSnapshot = false } = {}) {
  return Boolean(announce || !hasSnapshot);
}

function placeComposerForWindow({
  composer,
  agentSlot,
  legacySlot,
  isAgentWindow,
}) {
  const target = isAgentWindow ? agentSlot : legacySlot;
  if (composer && target && composer.parentElement !== target) {
    target.appendChild(composer);
  }
}

const navigatorUiHelpers = {
  beginProjectExpansion,
  placeComposerForWindow,
  shouldShowNavigatorSync,
};
if (typeof module !== "undefined" && module.exports) {
  module.exports = navigatorUiHelpers;
}
if (typeof globalThis !== "undefined") {
  globalThis.AutoPressNavigatorUI = navigatorUiHelpers;
}
