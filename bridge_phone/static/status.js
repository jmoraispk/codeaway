function backendLabel(w) {
  return w && w.backend === "codex_desktop" ? "Codex" : "Cursor";
}

function stateLabel(w) {
  if (w && w.backend === "codex_desktop") {
    const count = Number(w.ready_count || 0);
    return count > 0 ? `ready · ${count} item${count === 1 ? "" : "s"}` : "working";
  }
  if (w && w.asking) return "asking";
  if (w && w.idle) return "idle";
  return "busy";
}

const statusHelpers = { backendLabel, stateLabel };
if (typeof module !== "undefined" && module.exports) module.exports = statusHelpers;
if (typeof globalThis !== "undefined") globalThis.AutoPressStatus = statusHelpers;
