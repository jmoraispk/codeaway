// auto-press phone bridge — vanilla JS, no build step.
//
// Lists configured desktop agent targets with bridge status from SSE; tap in
// for snapshot thumbnails (click to expand) and a send composer.

// Resolve a window's tri-state from the bridge's flags. Asking
// (multiple-choice question pending) takes precedence over idle,
// otherwise it's busy.
function windowState(w) {
  if (!w) return "busy";
  if (w.asking) return "asking";
  if (w.idle) return "idle";
  return "busy";
}

const $ = (id) => document.getElementById(id);

const state = {
  windows: new Map(),       // id -> summary dict
  snapshotsPerWindow: 5,
  view: "list",             // "list" | "snapshots"
  current: null,            // window id when in "snapshots" view
  lastEventAt: 0,           // ms epoch of last SSE event for the freshness pill
  notifEnabled: localStorage.getItem("ap.notif") === "1",
  autoReloadEnabled: localStorage.getItem("ap.autoreload") === "1",
  intervalSeconds: 10,      // populated from /api/state
  rulesRunning: false,      // populated from /api/state; toggle in settings
  surfaceBust: 0,          // cache-buster for live Agent Window surfaces
  navigatorSignature: "", // semantic project/task tree, excluding timestamps
  navigatorConversationSignature: "", // selected task + task states only
  navigatorLoading: false,
  scrollPending: false,
};

const AUTO_RELOAD_DEFAULT_S = 10;

// ---- SSE ----------------------------------------------------------------

let sse;
let backoff = 1000;
function connectSSE() {
  if (sse) try { sse.close(); } catch {}
  sse = new EventSource("/api/events");
  sse.onopen = () => { backoff = 1000; markEvent(); };
  sse.addEventListener("window_state", (ev) => {
    markEvent();
    try {
      const data = JSON.parse(ev.data);
      if (!data || !data.id) return;
      const prev = state.windows.get(data.id);
      maybeNotify(prev, data);
      state.windows.set(data.id, data);
      renderWindows();
      if (state.view === "snapshots" && state.current === data.id) {
        const snapshotsGrew =
          !prev || (data.snapshot_count || 0) > (prev.snapshot_count || 0);
        renderWindowDetail(snapshotsGrew);
      }
    } catch {}
  });
  sse.addEventListener("rule_matched", () => markEvent());
  sse.onerror = () => {
    try { sse.close(); } catch {}
    setTimeout(connectSSE, backoff);
    backoff = Math.min(backoff * 2, 30000);
  };
}

function markEvent() {
  state.lastEventAt = Date.now();
  renderFreshness();
}

function renderFreshness() {
  const el = $("freshness");
  if (!state.lastEventAt) {
    el.textContent = "—";
    el.className = "freshness";
    return;
  }
  const sec = Math.floor((Date.now() - state.lastEventAt) / 1000);
  el.textContent = sec < 1 ? "just now" : `${sec}s ago`;
  el.className =
    "freshness " + (sec > 30 ? "stale" : sec > 15 ? "" : "live");
}
setInterval(renderFreshness, 1000);

// ---- initial paint ------------------------------------------------------

async function loadState() {
  try {
    const res = await fetch("/api/state");
    if (!res.ok) return;
    const data = await res.json();
    state.snapshotsPerWindow = data.snapshots_per_window || 5;
    state.intervalSeconds = data.interval_seconds || AUTO_RELOAD_DEFAULT_S;
    state.rulesRunning = Boolean(data.rules_running);
    state.windows.clear();
    for (const w of data.windows || []) state.windows.set(w.id, w);
    if ((data.windows || []).length) markEvent();
    renderWindows();
    renderRulesToggle();
    applyAutoReload();
    // After auto-refresh / page reload, restore the previously open
    // window if one was selected and still exists.
    let saved = null;
    try { saved = sessionStorage.getItem("ap.current"); } catch {}
    if (saved && state.windows.has(saved)) {
      openWindow(saved);
    }
  } catch {}
}

async function refreshState() {
  // Soft auto-refresh: same fetch as loadState, but doesn't touch the
  // current view (no openWindow re-call, no composer reset, no page
  // reload). Just merges new data into state and re-renders the bits
  // that actually changed.
  try {
    const res = await fetch("/api/state");
    if (!res.ok) return;
    const data = await res.json();
    state.snapshotsPerWindow = data.snapshots_per_window || 5;
    state.intervalSeconds = data.interval_seconds || AUTO_RELOAD_DEFAULT_S;
    state.rulesRunning = Boolean(data.rules_running);
    // Keep the prior count for the open window so we only re-fetch
    // snapshot images when they actually grew.
    const prevCount =
      state.current && state.windows.get(state.current)
        ? (state.windows.get(state.current).snapshot_count || 0)
        : 0;
    state.windows.clear();
    for (const w of data.windows || []) state.windows.set(w.id, w);
    if ((data.windows || []).length) markEvent();
    renderWindows();
    renderRulesToggle();
    // If the open window vanished (deleted on the desktop), drop back
    // to the overview so we don't leave a stale detail view around.
    if (state.current && !state.windows.has(state.current)) {
      closeSnapshots();
      return;
    }
    if (state.view === "snapshots" && state.current && state.windows.has(state.current)) {
      const newCount = state.windows.get(state.current).snapshot_count || 0;
      renderWindowDetail(newCount > prevCount);
    }
  } catch {}
}

// ---- views --------------------------------------------------------------

function renderWindows() {
  const container = $("windows");
  const empty = $("empty-hint");
  container.innerHTML = "";
  const all = [...state.windows.values()];
  empty.hidden = all.length > 0;
  for (const w of all) {
    const li = document.createElement("li");
    const isSelected = state.current === w.id;
    const stateName = windowState(w);
    const status = AutoPressStatus.stateLabel(w);
    const backend = AutoPressStatus.backendLabel(w);
    li.className =
      "window " + stateName +
      (isSelected ? " selected" : "");
    li.dataset.id = w.id;
    const pendingTag =
      (w.pending && w.pending.length)
        ? ` · ${w.pending.length} queued`
        : "";
    li.innerHTML = `
      <div class="window-head">
        <span class="dot" aria-hidden="true"></span>
        <div class="name"></div>
        <span class="backend-label"></span>
        <button class="window-rename" title="Rename" aria-label="Rename">✎</button>
      </div>
      <div class="sub">${status}${pendingTag}</div>
    `;
    li.querySelector(".name").textContent = w.name || w.id;
    li.querySelector(".backend-label").textContent = backend;
    li.querySelector(".window-rename").addEventListener("click", (e) => {
      e.stopPropagation();
      promptRenameWindow(w);
    });
    li.addEventListener("click", () => {
      // Toggle: tapping the selected row collapses the detail back to
      // the overview; tapping any other row switches.
      if (state.current === w.id) {
        closeSnapshots();
      } else {
        openWindow(w.id);
      }
    });
    container.appendChild(li);
  }
}

function openWindow(id) {
  stopCodexNavigatorPolling();
  state.view = "snapshots";
  state.current = id;
  state.navigatorSignature = "";
  state.navigatorConversationSignature = "";
  // Master-detail: keep the windows list visible above the detail
  // panel. The selected row is highlighted via renderWindows.
  $("snapshots-section").hidden = false;
  $("send-text").value = "";
  setSendStatus("");
  // Persist so an auto-refresh / accidental reload restores the view.
  try { sessionStorage.setItem("ap.current", id); } catch {}
  renderWindows();
  renderWindowDetail(true);
}

function closeSnapshots() {
  stopCodexNavigatorPolling();
  state.view = "list";
  state.current = null;
  $("snapshots-section").hidden = true;
  try { sessionStorage.removeItem("ap.current"); } catch {}
  renderWindows();
}

async function promptRenameWindow(w) {
  const current = w.name || w.id;
  const next = prompt("Rename window", current);
  if (next === null) return;
  const trimmed = next.trim();
  if (!trimmed || trimmed === current) return;
  // Optimistic local update so the rename is visible immediately.
  const stored = state.windows.get(w.id);
  if (stored) state.windows.set(w.id, { ...stored, name: trimmed });
  renderWindows();
  if (state.view === "snapshots" && state.current === w.id) {
    renderWindowDetail(false);
  }
  try {
    const res = await fetch(`/api/windows/${encodeURIComponent(w.id)}/name`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: trimmed }),
    });
    if (!res.ok) {
      const detail = await res.text();
      alert(`Rename failed: ${res.status} ${detail}`);
      // Roll back optimistic update.
      if (stored) state.windows.set(w.id, stored);
      renderWindows();
    }
  } catch (e) {
    alert(`Rename network error: ${e.message}`);
    if (stored) state.windows.set(w.id, stored);
    renderWindows();
  }
}

function renderWindowDetail(refetchSnapshots) {
  const id = state.current;
  if (!id) return;
  const w = state.windows.get(id);
  // The list card already shows the dot colour + human-readable state, so
  // the detail title used to repeat it on its own line. Fold it into
  // the heading instead — "Window Name (Busy)" — to spend the row on
  // the name and keep the status legible at a glance.
  const name = w ? (w.name || id) : id;
  const stateName = w ? AutoPressStatus.stateLabel(w) : "";
  const status = stateName
    ? stateName.charAt(0).toUpperCase() + stateName.slice(1)
    : "";
  $("snap-window-name").textContent = status ? `${name} (${status})` : name;
  renderQueue();
  const isAgentWindow = !!(w && w.backend === "codex_desktop");
  AutoPressNavigatorUI.placeComposerForWindow({
    composer: $("composer"),
    agentSlot: $("agent-composer-slot"),
    legacySlot: $("legacy-composer-slot"),
    isAgentWindow,
  });
  $("agent-window").hidden = !isAgentWindow;
  $("legacy-snapshots").hidden = isAgentWindow;
  if (isAgentWindow) {
    $("agent-calibration").textContent = w.agent_window_configured
      ? "calibrated"
      : "using defaults — calibrate on laptop";
    startCodexNavigatorPolling();
    if (refetchSnapshots) {
      refreshCodexNavigator({ conversationOnChange: false });
      refreshAgentSurfaces(["conversation"]);
      if ($("agent-sidebar-fallback").open) loadAgentSurface("sidebar");
    }
  } else if (refetchSnapshots) {
    stopCodexNavigatorPolling();
    renderSnapshots();
  }
}

function currentIsAgentWindow() {
  const w = state.current ? state.windows.get(state.current) : null;
  return !!(w && w.backend === "codex_desktop");
}

function navigatorSignature(data) {
  return JSON.stringify((data && data.projects) || []);
}

function navigatorConversationSignature(data) {
  return JSON.stringify(
    ((data && data.projects) || []).flatMap((project) =>
      (project.tasks || []).map((task) => [
        project.name,
        task.title,
        task.state,
        Boolean(task.selected),
      ])
    )
  );
}

function navigatorStateVisual(stateName) {
  if (stateName === "done") {
    const dot = document.createElement("i");
    dot.className = "state-dot done";
    dot.title = "Done";
    dot.setAttribute("aria-label", "Done");
    return dot;
  }
  if (stateName === "busy") {
    const spinner = document.createElement("i");
    spinner.className = "busy-spinner";
    spinner.title = "Busy";
    spinner.setAttribute("aria-label", "Busy");
    return spinner;
  }
  if (stateName === "connected") {
    const dot = document.createElement("i");
    dot.className = "state-dot connected";
    dot.title = "Connected";
    dot.setAttribute("aria-label", "Connected");
    return dot;
  }
  return null;
}

function renderCodexNavigator(data) {
  const root = $("agent-navigator");
  root.innerHTML = "";
  const projects = (data && data.projects) || [];
  if (!projects.length) {
    const message = document.createElement("p");
    message.className = "muted navigator-message";
    message.textContent = "No visible Codex projects. Open the sidebar or use the screenshot fallback.";
    root.appendChild(message);
    return;
  }

  for (const project of projects) {
    const section = document.createElement("section");
    section.className = `navigator-project${project.expanded ? "" : " collapsed"}`;

    const projectButton = document.createElement("button");
    projectButton.type = "button";
    projectButton.className = "navigator-project-row";
    projectButton.setAttribute("aria-expanded", String(Boolean(project.expanded)));
    projectButton.setAttribute("aria-label", `${project.expanded ? "Collapse" : "Expand"} ${project.name}`);

    const chevron = document.createElement("img");
    chevron.className = "navigator-chevron";
    chevron.src = "/static/project-chevron.svg";
    chevron.alt = "";
    chevron.setAttribute("aria-hidden", "true");
    projectButton.appendChild(chevron);

    const name = document.createElement("span");
    name.className = "navigator-name";
    name.textContent = project.name;
    projectButton.appendChild(name);

    const host = document.createElement("span");
    host.className = "navigator-host";
    host.textContent = project.host || "";
    projectButton.appendChild(host);

    const projectState = document.createElement("span");
    projectState.className = "navigator-project-state";
    const projectVisual = navigatorStateVisual(project.state);
    if (projectVisual) projectState.appendChild(projectVisual);
    projectButton.appendChild(projectState);
    projectButton.addEventListener("click", () =>
      runCodexNavigatorAction(
        { kind: "project", project: project.name, expanded: !project.expanded },
        projectButton
      )
    );
    section.appendChild(projectButton);

    const tasks = document.createElement("div");
    tasks.className = "navigator-tasks";
    for (const task of project.tasks || []) {
      const taskButton = document.createElement("button");
      taskButton.type = "button";
      taskButton.className = `navigator-task-row${task.selected ? " selected" : ""}`;
      taskButton.setAttribute("aria-label", `Open ${task.title}`);

      const title = document.createElement("span");
      title.className = "navigator-task-title";
      title.textContent = task.title;
      taskButton.appendChild(title);

      const meta = document.createElement("span");
      meta.className = "navigator-task-meta";
      if (task.worktree) {
        const worktree = document.createElement("img");
        worktree.className = "worktree-mark";
        worktree.src = "/static/worktree.svg";
        worktree.alt = "";
        worktree.title = "Separate worktree";
        worktree.setAttribute("aria-label", "Separate worktree");
        meta.appendChild(worktree);
      }
      const taskVisual = navigatorStateVisual(task.state);
      if (taskVisual) meta.appendChild(taskVisual);
      taskButton.appendChild(meta);
      taskButton.addEventListener("click", () =>
        runCodexNavigatorAction(
          { kind: "task", project: project.name, title: task.title },
          taskButton
        )
      );
      tasks.appendChild(taskButton);
    }
    if (project.expanded && !(project.tasks || []).length) {
      const empty = document.createElement("span");
      empty.className = "navigator-empty";
      empty.textContent = "No visible tasks";
      tasks.appendChild(empty);
    }
    section.appendChild(tasks);
    root.appendChild(section);
  }
}

async function refreshCodexNavigator({ announce = false, conversationOnChange = true } = {}) {
  const id = state.current;
  if (!id || !currentIsAgentWindow() || state.navigatorLoading) return;
  state.navigatorLoading = true;
  const live = $("agent-navigator-live");
  const showSync = AutoPressNavigatorUI.shouldShowNavigatorSync({
    announce,
    hasSnapshot: Boolean(state.navigatorSignature),
  });
  if (showSync) {
    live.textContent = "syncing…";
    live.classList.remove("live");
  }
  try {
    const res = await fetch(`/api/windows/${encodeURIComponent(id)}/navigator`, {
      cache: "no-store",
    });
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    const data = await res.json();
    if (state.current !== id) return;
    const nextSignature = navigatorSignature(data);
    const nextConversationSignature = navigatorConversationSignature(data);
    const changed = nextSignature !== state.navigatorSignature;
    const conversationChanged =
      Boolean(state.navigatorConversationSignature) &&
      nextConversationSignature !== state.navigatorConversationSignature;
    if (changed) renderCodexNavigator(data);
    state.navigatorSignature = nextSignature;
    state.navigatorConversationSignature = nextConversationSignature;
    live.textContent = data.pixel_states ? "live" : "live · visual states pending";
    live.classList.add("live");
    if (announce) setSendStatus("Navigator refreshed.", "success");
    if (conversationOnChange && conversationChanged) {
      setTimeout(() => refreshAgentSurfaces(["conversation"]), 300);
    }
  } catch (e) {
    if (state.current !== id) return;
    live.textContent = "retrying…";
    live.classList.remove("live");
    if (!state.navigatorSignature) {
      renderCodexNavigator({ projects: [] });
      $("agent-sidebar-fallback").open = true;
      loadAgentSurface("sidebar").catch(() => {});
    }
    if (announce) setSendStatus(`Navigator failed: ${e.message}`, "error");
  } finally {
    state.navigatorLoading = false;
  }
}

async function runCodexNavigatorAction(payload, button) {
  const id = state.current;
  if (!id || !currentIsAgentWindow()) return;
  const rollbackProjectExpansion = payload.kind === "project" && button
    ? AutoPressNavigatorUI.beginProjectExpansion(
        button,
        payload.project,
        payload.expanded
      )
    : null;
  if (button) button.disabled = true;
  const label = payload.kind === "task" ? payload.title : payload.project;
  setSendStatus(`${payload.kind === "task" ? "Opening" : "Updating"} ${label}…`);
  try {
    const res = await fetch(
      `/api/windows/${encodeURIComponent(id)}/navigator/action`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }
    );
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    setSendStatus(payload.kind === "task" ? `Opened ${label}.` : `Updated ${label}.`, "success");
    setTimeout(() => {
      refreshCodexNavigator({ conversationOnChange: false });
      if (payload.kind === "task") refreshAgentSurfaces(["conversation"]);
    }, 350);
  } catch (e) {
    if (rollbackProjectExpansion) rollbackProjectExpansion();
    setSendStatus(`Navigator action failed: ${e.message}`, "error");
  } finally {
    if (button) button.disabled = false;
  }
}

let codexNavigatorPollHandle = null;
function startCodexNavigatorPolling() {
  if (codexNavigatorPollHandle) return;
  codexNavigatorPollHandle = setInterval(() => {
    if (!document.hidden && currentIsAgentWindow()) refreshCodexNavigator();
  }, 1800);
}

function stopCodexNavigatorPolling() {
  if (codexNavigatorPollHandle) clearInterval(codexNavigatorPollHandle);
  codexNavigatorPollHandle = null;
}

function agentSurfaceImage(surface) {
  return surface === "sidebar"
    ? $("agent-sidebar-img")
    : $("agent-conversation-img");
}

async function loadAgentSurface(surface) {
  const id = state.current;
  if (!id) return;
  const img = agentSurfaceImage(surface);
  const card = img.closest(".agent-surface-card");
  card.classList.add("loading");
  card.classList.remove("error");
  try {
    const bust = `${Date.now()}-${++state.surfaceBust}`;
    const res = await fetch(
      `/api/windows/${encodeURIComponent(id)}/surface/${surface}?t=${bust}`,
      { cache: "no-store" }
    );
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    const blob = await res.blob();
    if (state.current !== id) return;
    if (img.dataset.objectUrl) URL.revokeObjectURL(img.dataset.objectUrl);
    const objectUrl = URL.createObjectURL(blob);
    img.dataset.objectUrl = objectUrl;
    img.src = objectUrl;
  } catch (e) {
    card.classList.add("error");
    throw new Error(`${surface}: ${e.message}`);
  } finally {
    card.classList.remove("loading");
  }
}

async function refreshAgentSurfaces(surfaces = ["conversation"], announce = false) {
  if (!currentIsAgentWindow()) return;
  try {
    await Promise.all(surfaces.map(loadAgentSurface));
    if (announce) setSendStatus("Agent workspace refreshed.", "success");
  } catch (e) {
    setSendStatus(`Workspace capture failed: ${e.message}`, "error");
  }
}

async function clickAgentSurface(surface, event) {
  if (!currentIsAgentWindow() || !state.current) return;
  const img = event.currentTarget;
  const rect = img.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const xFrac = Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width));
  const yFrac = Math.max(0, Math.min(1, (event.clientY - rect.top) / rect.height));
  const card = img.closest(".agent-surface-card");
  card.classList.add("loading");
  setSendStatus(`Clicking ${surface}…`);
  try {
    const res = await fetch(
      `/api/windows/${encodeURIComponent(state.current)}/click_at`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ surface, x_frac: xFrac, y_frac: yFrac }),
      }
    );
    if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
    const data = await res.json();
    const landed = Array.isArray(data.target) ? ` at ${data.target.join(", ")}` : "";
    setSendStatus(`Clicked ${surface}${landed}.`, "success");
    setTimeout(() => refreshAgentSurfaces(), 650);
  } catch (e) {
    setSendStatus(`Click failed: ${e.message}`, "error");
  } finally {
    card.classList.remove("loading");
  }
}

function renderQueue() {
  const id = state.current;
  const w = id ? state.windows.get(id) : null;
  const pending = (w && w.pending) || [];
  const section = $("queue-section");
  section.hidden = pending.length === 0;
  $("queue-count").textContent = pending.length ? `(${pending.length})` : "";
  const list = $("queue-list");
  list.innerHTML = "";
  for (let i = 0; i < pending.length; i++) {
    const li = document.createElement("li");
    list.appendChild(li);
    renderQueueRow(li, i, pending[i], false);
  }
}

function renderQueueRow(li, idx, text, editing) {
  li.innerHTML = "";
  li.classList.toggle("editing", editing);
  if (editing) {
    const ta = document.createElement("textarea");
    ta.className = "queue-edit-text";
    ta.rows = 2;
    ta.value = text;
    const saveBtn = document.createElement("button");
    saveBtn.className = "queue-save";
    saveBtn.textContent = "Save";
    const cancelBtn = document.createElement("button");
    cancelBtn.className = "queue-cancel ghost";
    cancelBtn.textContent = "Cancel";
    saveBtn.addEventListener("click", () => saveQueuedEdit(idx, ta.value, li, text));
    cancelBtn.addEventListener("click", () => renderQueueRow(li, idx, text, false));
    ta.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        saveQueuedEdit(idx, ta.value, li, text);
      } else if (e.key === "Escape") {
        e.preventDefault();
        renderQueueRow(li, idx, text, false);
      }
    });
    li.appendChild(ta);
    li.appendChild(saveBtn);
    li.appendChild(cancelBtn);
    setTimeout(() => ta.focus(), 0);
    return;
  }
  const span = document.createElement("span");
  span.className = "queue-text";
  span.textContent = text;
  span.title = "Double-click to edit";
  // Double-click (not single-click) opens the inline editor so an
  // accidental tap on a long message doesn't suddenly throw the user
  // into edit mode.
  span.addEventListener("dblclick", () =>
    renderQueueRow(li, idx, text, true)
  );
  const actions = document.createElement("div");
  actions.className = "queue-actions";
  const sendBtn = document.createElement("button");
  sendBtn.className = "queue-send-now";
  sendBtn.textContent = "Send now";
  const editBtn = document.createElement("button");
  editBtn.className = "queue-edit";
  editBtn.textContent = "✎";
  editBtn.title = "Edit";
  editBtn.setAttribute("aria-label", "Edit message");
  const delBtn = document.createElement("button");
  delBtn.className = "queue-delete";
  delBtn.textContent = "✕";
  delBtn.title = "Remove from queue";
  delBtn.setAttribute("aria-label", "Remove from queue");
  sendBtn.addEventListener("click", () => sendQueuedNow(idx, sendBtn));
  editBtn.addEventListener("click", () => renderQueueRow(li, idx, text, true));
  delBtn.addEventListener("click", () => deleteQueuedItem(idx, delBtn));
  // Send Now → Edit → Delete left-to-right, kept together as a group
  // so they wrap as a unit underneath the message text on narrow rows.
  actions.appendChild(sendBtn);
  actions.appendChild(editBtn);
  actions.appendChild(delBtn);
  li.appendChild(span);
  li.appendChild(actions);
}

async function saveQueuedEdit(idx, newText, li, prevText) {
  const id = state.current;
  if (!id) return;
  // Optimistic update: replace local pending entry, exit edit mode.
  patchPendingOptimistic((p) => p.map((t, i) => (i === idx ? newText : t)));
  try {
    const res = await fetch(
      `/api/windows/${encodeURIComponent(id)}/queue/${idx}`,
      {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text: newText }),
      }
    );
    if (!res.ok) {
      const detail = await res.text();
      setSendStatus(`Edit failed: ${res.status} ${detail}`, "error");
      // Roll back optimistic update on failure.
      patchPendingOptimistic((p) => p.map((t, i) => (i === idx ? prevText : t)));
    } else {
      setSendStatus("Edited.", "success");
    }
  } catch (e) {
    setSendStatus(`Network error: ${e.message}`, "error");
    patchPendingOptimistic((p) => p.map((t, i) => (i === idx ? prevText : t)));
  }
}

async function sendQueuedNow(idx, btn) {
  const id = state.current;
  if (!id) return;
  btn.disabled = true;
  btn.textContent = "Sending…";
  try {
    const res = await fetch(
      `/api/windows/${encodeURIComponent(id)}/queue/${idx}/send_now`,
      { method: "POST" }
    );
    if (!res.ok) {
      const detail = await res.text();
      setSendStatus(`Send-now failed: ${res.status} ${detail}`, "error");
      btn.disabled = false;
      btn.textContent = "Send now";
    } else {
      // Optimistic remove so the row disappears immediately. The
      // window_state SSE event lands shortly after and overwrites with
      // the authoritative server view (no-op if it matches).
      patchPendingOptimistic((p) => p.filter((_, i) => i !== idx));
      setSendStatus("Sent.", "success");
    }
  } catch (e) {
    setSendStatus(`Network error: ${e.message}`, "error");
    btn.disabled = false;
    btn.textContent = "Send now";
  }
}

function renderSnapshots() {
  const id = state.current;
  if (!id) return;
  const w = state.windows.get(id);
  const count = w ? Math.min(w.snapshot_count || 0, state.snapshotsPerWindow) : 0;
  const empty = $("snap-empty");
  const wrap = $("snapshots");
  empty.hidden = count > 0;
  wrap.innerHTML = "";
  if (count === 0) return;
  // Show all stored snapshots. Index 0 is newest; later indices step
  // back through previous scroll captures from this idle session.
  // Cache-bust so the same idx returns the latest bytes after a
  // re-capture; Cache-Control: no-store from the server too.
  state.lightboxBust = Date.now();
  for (let i = 0; i < count; i++) {
    const url = `/api/windows/${encodeURIComponent(id)}/snapshot/${i}?t=${state.lightboxBust}-${i}`;
    const card = document.createElement("div");
    card.className = "snapshot";
    const label =
      i === 0
        ? (w.snapshot_at ? `newest · ${formatRelativeTime(w.snapshot_at)}` : "newest")
        : `${i} scroll${i === 1 ? "" : "s"} ago`;
    card.innerHTML = `
      <img src="${url}" alt="snapshot ${i + 1}" loading="lazy">
      <div class="ts">${label}</div>
    `;
    card.addEventListener("click", () => openLightboxAt(i));
    wrap.appendChild(card);
  }
}

// Lightbox state lives on `state` so the prev/next handlers (and the
// browser-back popstate handler) can read it without a closure.
function snapshotUrl(idx) {
  const id = state.current;
  if (!id) return "";
  // Reuse the same cache-bust as the thumbnail grid so we don't re-
  // download bytes we already have.
  return `/api/windows/${encodeURIComponent(id)}/snapshot/${idx}?t=${state.lightboxBust}-${idx}`;
}

function formatRelativeTime(iso) {
  if (!iso) return "—";
  const t = Date.parse(iso);
  if (isNaN(t)) return "—";
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  const min = Math.floor(sec / 60);
  if (min < 60) return `${min}m ago`;
  const hr = Math.floor(min / 60);
  return `${hr}h ago`;
}

// Lightbox is index-driven (not URL-driven) so prev/next can step
// through the current window's snapshot deck without reaching back
// into render state. `_lightboxPushedHistory` tracks whether we own
// a history entry so the close path can decide between history.back()
// (consume our entry) and a passive close (popstate already fired).
function snapshotCount() {
  const w = state.current ? state.windows.get(state.current) : null;
  return w ? Math.min(w.snapshot_count || 0, state.snapshotsPerWindow) : 0;
}

function openLightboxAt(idx) {
  const count = snapshotCount();
  if (count === 0) return;
  state.lightboxIdx = Math.max(0, Math.min(idx, count - 1));
  $("lightbox-img").src = snapshotUrl(state.lightboxIdx);
  $("lightbox-prev").hidden = state.lightboxIdx <= 0;
  $("lightbox-next").hidden = state.lightboxIdx >= count - 1;
  const counter = $("lightbox-counter");
  counter.textContent = count > 1 ? `${state.lightboxIdx + 1} / ${count}` : "";
  // Tap-to-click is only meaningful on the newest snapshot. Older
  // snapshots are stale views — clicking based on them would land
  // on whatever's there NOW, which may have nothing to do with what
  // the user is looking at. Hint shows on idx 0, hides otherwise.
  hideClickCrosshair();
  $("lightbox-tap-hint").hidden = state.lightboxIdx !== 0;
  $("lightbox").hidden = false;
  // First open in a session pushes a history entry so the phone's back
  // button closes the lightbox instead of exiting the PWA. Subsequent
  // prev/next navigation reuses the same entry — we don't want every
  // arrow tap to grow the history stack.
  if (!state._lightboxPushedHistory) {
    try {
      history.pushState({ lightbox: true }, "");
      state._lightboxPushedHistory = true;
    } catch {}
  }
}

function lightboxStep(delta) {
  if ($("lightbox").hidden) return;
  const count = snapshotCount();
  if (count === 0) return;
  const next = state.lightboxIdx + delta;
  if (next < 0 || next >= count) return;
  openLightboxAt(next);
}

function closeLightbox() {
  if ($("lightbox").hidden) return;
  $("lightbox").hidden = true;
  $("lightbox-img").src = "";
  $("lightbox-counter").textContent = "";
  hideClickCrosshair();
  $("lightbox-tap-hint").hidden = true;
  // If we own a history entry, popping it keeps the URL bar in sync
  // and prevents a stale "lightbox" state from sitting on the stack.
  // The popstate handler clears the flag and skips the second close.
  if (state._lightboxPushedHistory) {
    state._lightboxPushedHistory = false;
    try { history.back(); } catch {}
  }
}

// ---- Tap-to-click on the newest snapshot --------------------------------
//
// User taps the image → crosshair lands at the tap position, Click/Cancel
// bar appears. Confirming fires POST /api/windows/{id}/click_at with the
// fractional coords (x / image.naturalWidth, y / image.naturalHeight),
// which the bridge translates back to physical screen pixels. Available
// only on the newest snapshot — older snapshots are stale and clicking
// on them would go to whatever is there NOW, not what the user sees.

state.pendingClick = null;  // {x_frac, y_frac} when crosshair is placed

function hideClickCrosshair() {
  $("lightbox-crosshair").hidden = true;
  $("lightbox-click-bar").hidden = true;
  state.pendingClick = null;
}

function showClickCrosshair(xFrac, yFrac) {
  const ch = $("lightbox-crosshair");
  // The crosshair is positioned via the img's currently-rendered rect,
  // not the wrap's rect, so letterboxing (object-fit: contain padding)
  // doesn't shift the cross off the actual pixel the user tapped.
  const img = $("lightbox-img");
  const wrap = $("lightbox-img-wrap");
  const imgRect = img.getBoundingClientRect();
  const wrapRect = wrap.getBoundingClientRect();
  const left = imgRect.left - wrapRect.left + xFrac * imgRect.width;
  const top = imgRect.top - wrapRect.top + yFrac * imgRect.height;
  ch.style.left = `${left}px`;
  ch.style.top = `${top}px`;
  ch.hidden = false;
  $("lightbox-click-bar").hidden = false;
  $("lightbox-tap-hint").hidden = true;
  state.pendingClick = { x_frac: xFrac, y_frac: yFrac };
}

function handleLightboxTap(clientX, clientY) {
  // Only the newest snapshot is a valid click target — see
  // openLightboxAt for the reasoning. Silently ignore taps on older
  // ones so a stray double-tap during scroll history doesn't fire.
  if (state.lightboxIdx !== 0) return;
  const img = $("lightbox-img");
  if (!img.naturalWidth) return;  // image still loading
  const rect = img.getBoundingClientRect();
  const xPx = clientX - rect.left;
  const yPx = clientY - rect.top;
  if (xPx < 0 || yPx < 0 || xPx > rect.width || yPx > rect.height) return;
  const xFrac = Math.max(0, Math.min(1, xPx / rect.width));
  const yFrac = Math.max(0, Math.min(1, yPx / rect.height));
  showClickCrosshair(xFrac, yFrac);
}

async function confirmClickAtCrosshair() {
  const pending = state.pendingClick;
  if (!pending || !state.current) return;
  const btn = $("lightbox-click-confirm");
  btn.disabled = true;
  const originalText = btn.textContent;
  btn.textContent = "Clicking…";
  try {
    const res = await fetch(
      `/api/windows/${encodeURIComponent(state.current)}/click_at`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(pending),
      }
    );
    if (!res.ok) {
      const detail = await res.text();
      alert(`Click failed: ${res.status} ${detail}`);
      return;
    }
    // Success — close the lightbox so the user sees the windows list
    // updating (busy badge will land via SSE shortly after the click).
    hideClickCrosshair();
    closeLightbox();
  } catch (e) {
    alert(`Click network error: ${e.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = originalText;
  }
}

function setSendStatus(msg, kind) {
  const el = $("send-status");
  el.textContent = msg || "";
  el.className = "status" + (kind ? " " + kind : "");
}

// Apply a function to the current window's pending list and re-render
// immediately, without waiting for the SSE round-trip. Whenever the
// window_state event eventually lands, it overwrites this with the
// authoritative server view; until then the user sees the result of
// their tap right away.
function patchPendingOptimistic(transform) {
  const id = state.current;
  if (!id) return;
  const w = state.windows.get(id);
  if (!w) return;
  const next = { ...w, pending: transform(w.pending || []) };
  state.windows.set(id, next);
  renderWindows();
  renderWindowDetail(false);
}

async function sendOrQueue() {
  const id = state.current;
  if (!id) return;
  // Allow empty / whitespace — that's the "just press Enter" case for
  // when the user already typed the message in the target window from
  // the laptop. No client-side .trim(); the bridge accepts any string.
  const text = $("send-text").value;
  setSendStatus("Sending…");
  try {
    const res = await fetch(
      `/api/windows/${encodeURIComponent(id)}/send`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text }),
      }
    );
    if (res.ok) {
      const data = await res.json();
      if (data.queued) {
        // Optimistic append so the queue list shows the new item without
        // waiting for the SSE event.
        patchPendingOptimistic((p) => [...p, text]);
        setSendStatus(`Queued (#${data.position}). Sends on next idle.`, "success");
      } else {
        setSendStatus("Sent.", "success");
        if (currentIsAgentWindow()) {
          setTimeout(() => refreshAgentSurfaces(), 700);
        }
      }
      $("send-text").value = "";
    } else if (res.status === 202) {
      const data = await res.json();
      patchPendingOptimistic((p) => [...p, text]);
      setSendStatus(`Queued (#${data.position}). Sends on next idle.`, "success");
      $("send-text").value = "";
    } else {
      const detail = await res.text();
      setSendStatus(`Error ${res.status}: ${detail}`, "error");
    }
  } catch (e) {
    setSendStatus(`Network error: ${e.message}`, "error");
  }
}

async function deleteQueuedItem(idx, btn) {
  const id = state.current;
  if (!id) return;
  if (btn) btn.disabled = true;
  // Optimistic local removal so the row disappears immediately. SSE
  // will reconcile if the server rejects (e.g. wrong index).
  patchPendingOptimistic((p) => p.filter((_, i) => i !== idx));
  try {
    const res = await fetch(
      `/api/windows/${encodeURIComponent(id)}/queue/${idx}`,
      { method: "DELETE" }
    );
    if (!res.ok) {
      const detail = await res.text();
      setSendStatus(`Delete failed: ${res.status} ${detail}`, "error");
    } else {
      setSendStatus("Removed from queue.", "success");
    }
  } catch (e) {
    setSendStatus(`Network error: ${e.message}`, "error");
  }
}

async function clearQueue() {
  const id = state.current;
  if (!id) return;
  // Optimistic empty so the list collapses immediately. If the server
  // 500s, the SSE-driven re-sync will put the items back.
  patchPendingOptimistic(() => []);
  try {
    const res = await fetch(`/api/windows/${encodeURIComponent(id)}/queue`, { method: "DELETE" });
    if (res.ok) setSendStatus("Queue cleared.", "success");
    else setSendStatus(`Clear failed: ${res.status}`, "error");
  } catch (e) {
    setSendStatus(`Could not clear queue: ${e.message}`, "error");
  }
}

// ---- helpers ------------------------------------------------------------

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function formatScore(s) {
  if (typeof s !== "number" || isNaN(s)) return "—";
  return `score ${s.toFixed(2)}`;
}

// ---- settings / notifications / admin reload ---------------------------

function renderNotifToggle() {
  const btn = $("notif-toggle");
  btn.textContent = state.notifEnabled ? "on" : "off";
  btn.classList.toggle("on", state.notifEnabled);
}

function maybeNotify(prev, next) {
  // No-op now: Web Push delivers transition notifications via the
  // service worker so they fire even when the PWA is closed. Kept
  // as a stub because the SSE handler still calls it — left as a
  // future hook if we ever want in-app toast banners while the PWA
  // is foreground.
}

// ---- Web Push subscription helpers --------------------------------------
//
// Subscribe: ask the OS push service for an endpoint (uses VAPID public
// key from the bridge), POST the subscription JSON back so the bridge
// can deliver pushes. Unsubscribe: ask the push service to drop the
// endpoint, then tell the bridge to forget it. The "notifications" toggle
// just calls these two depending on its current state.

function urlBase64ToUint8Array(b64) {
  // VAPID public keys come back as URL-safe base64 without padding.
  // PushManager.subscribe wants a Uint8Array.
  const padded = b64 + "==".slice((b64.length + 2) % 4);
  const ascii = atob(padded.replace(/-/g, "+").replace(/_/g, "/"));
  const arr = new Uint8Array(ascii.length);
  for (let i = 0; i < ascii.length; i++) arr[i] = ascii.charCodeAt(i);
  return arr;
}

async function ensureServiceWorker() {
  if (!("serviceWorker" in navigator)) {
    throw new Error("Service workers not supported in this browser.");
  }
  // Scope is root so the SW controls the whole PWA. /sw.js itself is
  // served with Service-Worker-Allowed: / by the bridge.
  return navigator.serviceWorker.register("/sw.js", { scope: "/" });
}

async function subscribePush() {
  if (typeof Notification === "undefined") {
    throw new Error("This browser doesn't expose the Notification API.");
  }
  if (Notification.permission === "denied") {
    throw new Error(
      "Notifications are blocked for this site. Enable them in browser settings, then try again."
    );
  }
  if (Notification.permission !== "granted") {
    const result = await Notification.requestPermission();
    if (result !== "granted") throw new Error("Permission not granted.");
  }
  const reg = await ensureServiceWorker();
  // Make sure the SW is fully active before we subscribe — calling
  // subscribe on a still-installing SW can produce a 'pending' state
  // on some browsers.
  await navigator.serviceWorker.ready;

  // Fetch the bridge's VAPID public key. Cached after first call —
  // it doesn't rotate.
  const keyRes = await fetch("/api/notifications/vapid-key");
  if (!keyRes.ok) {
    throw new Error(`Couldn't fetch VAPID key (${keyRes.status})`);
  }
  const { public_key } = await keyRes.json();
  if (!public_key) throw new Error("Bridge has no VAPID key.");

  const subscription = await reg.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey: urlBase64ToUint8Array(public_key),
  });
  // Persist server-side. Bridge dedupes by endpoint, so re-subscribing
  // is idempotent.
  const subRes = await fetch("/api/notifications/subscribe", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      subscription: subscription.toJSON(),
      label: navigator.userAgent.slice(0, 80),
    }),
  });
  if (!subRes.ok) {
    const detail = await subRes.text();
    throw new Error(`Subscribe failed: ${subRes.status} ${detail}`);
  }
  return subscription;
}

async function unsubscribePush() {
  if (!("serviceWorker" in navigator)) return;
  try {
    const reg = await navigator.serviceWorker.getRegistration("/");
    if (!reg) return;
    const sub = await reg.pushManager.getSubscription();
    if (!sub) return;
    const endpoint = sub.endpoint;
    // Tell the bridge to drop the entry first (so we don't keep
    // trying to deliver to a phone that no longer has the SW
    // subscribed), then unsubscribe locally.
    try {
      await fetch("/api/notifications/subscribe", {
        method: "DELETE",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ endpoint }),
      });
    } catch {}
    await sub.unsubscribe();
  } catch {}
}

async function toggleNotifications() {
  const btn = $("notif-toggle");
  btn.disabled = true;
  try {
    if (state.notifEnabled) {
      await unsubscribePush();
      state.notifEnabled = false;
      localStorage.setItem("ap.notif", "0");
    } else {
      try {
        await subscribePush();
      } catch (e) {
        alert(e.message || "Couldn't enable notifications.");
        return;
      }
      state.notifEnabled = true;
      localStorage.setItem("ap.notif", "1");
    }
    renderNotifToggle();
  } finally {
    btn.disabled = false;
  }
}

async function reloadBridge() {
  const ok = confirm(
    "Reload the bridge service? The connection will drop for ~1 second " +
    "and reconnect automatically. If the new code fails to import, the " +
    "current service stays up."
  );
  if (!ok) return;
  const btn = $("reload-btn");
  btn.disabled = true;
  btn.textContent = "Reloading…";
  try {
    const res = await fetch("/api/admin/reload", { method: "POST" });
    if (!res.ok) {
      const detail = await res.text();
      alert(`Reload failed: ${res.status} ${detail}`);
    }
  } catch (e) {
    // Expected: the request was in flight when the listener was torn down.
  } finally {
    setTimeout(() => {
      btn.disabled = false;
      btn.textContent = "Reload";
    }, 2000);
  }
}

// ---- auto-reload (page refresh on a fixed cadence) ---------------------

let autoReloadHandle = null;

function renderAutoReloadToggle() {
  const btn = $("autoreload-toggle");
  btn.textContent = state.autoReloadEnabled ? "on" : "off";
  btn.classList.toggle("on", state.autoReloadEnabled);
}

function applyAutoReload() {
  if (autoReloadHandle) {
    clearInterval(autoReloadHandle);
    autoReloadHandle = null;
  }
  if (!state.autoReloadEnabled) return;
  // Match the bridge tick cadence; clamp so a sub-second interval can't
  // turn this into a refresh-storm if someone mis-configures.
  const ms = Math.max(2000, Math.round(state.intervalSeconds * 1000));
  autoReloadHandle = setInterval(() => {
    // Don't refetch while typing into the composer or editing a queued
    // message — re-rendering would drop the user's draft / rebuild the
    // edit field.
    const active = document.activeElement;
    if (active && (active.tagName === "TEXTAREA" || active.tagName === "INPUT")) {
      return;
    }
    // Soft refresh: just re-fetch state and re-render the changed
    // bits. No location.reload, no full-page flash. Keeps the
    // composer text + scroll position intact.
    refreshState();
  }, ms);
}

function toggleAutoReload() {
  state.autoReloadEnabled = !state.autoReloadEnabled;
  localStorage.setItem("ap.autoreload", state.autoReloadEnabled ? "1" : "0");
  renderAutoReloadToggle();
  applyAutoReload();
}

// ---- rules automation toggle -------------------------------------------

function renderRulesToggle() {
  const btn = $("rules-toggle");
  const on = !!state.rulesRunning;
  btn.textContent = on ? "on" : "off";
  btn.classList.toggle("on", on);
}

async function toggleRules() {
  const next = !state.rulesRunning;
  // Optimistic flip so the toggle reacts on tap; the server response
  // confirms (and the next /api/state load is the authoritative read).
  state.rulesRunning = next;
  renderRulesToggle();
  try {
    const res = await fetch("/api/admin/rules", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ running: next }),
    });
    if (!res.ok) {
      const detail = await res.text();
      alert(`Rules toggle failed: ${res.status} ${detail}`);
      state.rulesRunning = !next;
      renderRulesToggle();
    }
  } catch (e) {
    alert(`Rules toggle network error: ${e.message}`);
    state.rulesRunning = !next;
    renderRulesToggle();
  }
}

// ---- per-rule window-scope editor (settings panel) ---------------------
//
// Mirror of the desktop's Window-scope card, projected per-rule onto the
// phone. The panel stays hidden behind the cog so the main flow isn't
// cluttered; opens lazily (first expand triggers loadRulesScope) and
// re-fetches on each mutation so the view tracks the authoritative
// server state without an SSE round-trip.

state.rulesScopeLoaded = false;
state.rulesScope = { rules: [], available_windows: [] };
// Per-rule UI state: {ruleId: bool} — whether the window picker is
// currently open. Defaults to closed; the user expands it explicitly
// via the "Scope: All windows / Specific" tap row so the panel stays
// compact when several rules are configured.
state.rulesScopeOpen = {};

async function loadRulesScope() {
  try {
    const res = await fetch("/api/rules");
    if (!res.ok) {
      state.rulesScope = { rules: [], available_windows: [] };
      renderRulesScope();
      return;
    }
    const data = await res.json();
    state.rulesScope = {
      rules: Array.isArray(data.rules) ? data.rules : [],
      available_windows: Array.isArray(data.available_windows)
        ? data.available_windows
        : [],
    };
    state.rulesScopeLoaded = true;
    renderRulesScope();
  } catch {
    state.rulesScope = { rules: [], available_windows: [] };
    renderRulesScope();
  }
}

function renderRulesScope() {
  const list = $("rules-scope-list");
  const empty = $("rules-scope-empty");
  list.innerHTML = "";
  const rules = state.rulesScope.rules || [];
  empty.hidden = rules.length > 0;
  if (rules.length === 0) return;

  // Pool every name we might want to show per rule: the cross-workspace
  // detected set plus any name referenced by a scope (so stale entries
  // are still tickable). The server already de-dupes both into
  // available_windows; we tag visibility on a name lookup.
  const visibilityByName = new Map();
  for (const w of state.rulesScope.available_windows || []) {
    if (w && typeof w.name === "string") {
      visibilityByName.set(w.name, w.visible !== false);
    }
  }

  for (const rule of rules) {
    const block = document.createElement("div");
    block.className = "rule-block";
    block.dataset.ruleId = rule.id;

    const head = document.createElement("div");
    head.className = "rule-head";
    const nameEl = document.createElement("span");
    nameEl.className = "rule-name";
    nameEl.textContent = rule.name || rule.id;
    const enableBtn = document.createElement("button");
    enableBtn.className = "ghost rule-enable";
    enableBtn.textContent = rule.enabled ? "on" : "off";
    enableBtn.classList.toggle("on", !!rule.enabled);
    enableBtn.addEventListener("click", () =>
      toggleRuleEnabled(rule.id, !rule.enabled)
    );
    head.appendChild(nameEl);
    head.appendChild(enableBtn);
    block.appendChild(head);

    const scope = Array.isArray(rule.window_scope) ? rule.window_scope : [];
    const isAll = scope.length === 0;
    const open = !!state.rulesScopeOpen[rule.id];

    // Scope row: single-tap surface that summarises the current state
    // (All windows / N windows) and toggles the picker open. Keeping
    // the window list collapsed by default is the whole point — a
    // user with five rules sees five tidy lines, not 25 checkboxes.
    const scopeRow = document.createElement("button");
    scopeRow.type = "button";
    scopeRow.className = "rule-scope-row";
    scopeRow.classList.toggle("open", open);
    scopeRow.setAttribute("aria-expanded", String(open));
    const label = document.createElement("span");
    label.className = "rule-scope-label";
    label.textContent = isAll
      ? "All windows"
      : `${scope.length} window${scope.length === 1 ? "" : "s"} selected`;
    const chevron = document.createElement("span");
    chevron.className = "rule-scope-chev";
    chevron.textContent = "▾";
    scopeRow.appendChild(label);
    scopeRow.appendChild(chevron);
    scopeRow.addEventListener("click", () => {
      state.rulesScopeOpen[rule.id] = !state.rulesScopeOpen[rule.id];
      renderRulesScope();
    });
    block.appendChild(scopeRow);

    if (!open) {
      list.appendChild(block);
      continue;
    }

    // Picker body — the "All windows" pill sits at the top as a quick
    // way to reset back to the default, then the per-window checkboxes.
    // Stale entries (referenced by the scope but not currently
    // detected) appear at the bottom tagged "(not visible)" so a user
    // can untick them without first switching workspaces on the
    // laptop.
    const body = document.createElement("div");
    body.className = "rule-scope-body";

    const allRow = document.createElement("button");
    allRow.type = "button";
    allRow.className = "rule-all-pill";
    allRow.classList.toggle("active", isAll);
    allRow.textContent = isAll ? "All windows ✓" : "Use all windows";
    allRow.addEventListener("click", () => clearRuleScope(rule.id));
    body.appendChild(allRow);

    const seen = new Set();
    const rowsWrap = document.createElement("div");
    rowsWrap.className = "rule-window-rows";
    for (const w of state.rulesScope.available_windows || []) {
      if (!w || typeof w.name !== "string") continue;
      const name = w.name;
      if (seen.has(name)) continue;
      seen.add(name);
      const inScope = scope.includes(name);
      rowsWrap.appendChild(
        buildScopeRow(rule.id, name, inScope, w.visible !== false)
      );
    }
    for (const name of scope) {
      if (seen.has(name)) continue;
      seen.add(name);
      rowsWrap.appendChild(
        buildScopeRow(rule.id, name, true, visibilityByName.get(name) ?? false)
      );
    }
    if (!rowsWrap.children.length) {
      const none = document.createElement("p");
      none.className = "muted rule-no-windows";
      none.textContent = "No Cursor windows detected yet.";
      body.appendChild(none);
    } else {
      body.appendChild(rowsWrap);
    }

    block.appendChild(body);
    list.appendChild(block);
  }
}

function buildScopeRow(ruleId, name, checked, visible) {
  const row = document.createElement("label");
  row.className = "rule-window-row";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.checked = checked;
  cb.addEventListener("change", () =>
    toggleRuleWindow(ruleId, name, cb.checked)
  );
  const nameEl = document.createElement("span");
  nameEl.className = "rule-window-name";
  nameEl.textContent = name;
  row.appendChild(cb);
  row.appendChild(nameEl);
  if (!visible) {
    const tag = document.createElement("span");
    tag.className = "rule-window-tag";
    tag.textContent = "(not visible)";
    row.appendChild(tag);
  }
  return row;
}

async function toggleRuleEnabled(ruleId, next) {
  // Optimistic flip on the local rule entry so the toggle reacts on
  // tap. Server PUT is the authoritative confirm; failure rolls back
  // and re-renders the original state.
  const rule = (state.rulesScope.rules || []).find((r) => r.id === ruleId);
  if (!rule) return;
  const prev = !!rule.enabled;
  rule.enabled = !!next;
  renderRulesScope();
  try {
    const res = await fetch(`/api/rules/${encodeURIComponent(ruleId)}/enabled`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ enabled: !!next }),
    });
    if (!res.ok) {
      const detail = await res.text();
      alert(`Rule toggle failed: ${res.status} ${detail}`);
      rule.enabled = prev;
      renderRulesScope();
    }
  } catch (e) {
    alert(`Rule toggle network error: ${e.message}`);
    rule.enabled = prev;
    renderRulesScope();
  }
}

async function toggleRuleWindow(ruleId, name, checked) {
  const rule = (state.rulesScope.rules || []).find((r) => r.id === ruleId);
  if (!rule) return;
  const prev = Array.isArray(rule.window_scope) ? [...rule.window_scope] : [];
  const nextScope = checked
    ? (prev.includes(name) ? prev : [...prev, name])
    : prev.filter((n) => n !== name);
  rule.window_scope = nextScope;
  renderRulesScope();
  await putRuleScope(rule, prev, nextScope);
}

async function clearRuleScope(ruleId) {
  const rule = (state.rulesScope.rules || []).find((r) => r.id === ruleId);
  if (!rule) return;
  const prev = Array.isArray(rule.window_scope) ? [...rule.window_scope] : [];
  if (prev.length === 0) return;
  rule.window_scope = [];
  renderRulesScope();
  await putRuleScope(rule, prev, []);
}

async function putRuleScope(rule, prevScope, nextScope) {
  try {
    const res = await fetch(`/api/rules/${encodeURIComponent(rule.id)}/scope`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ window_scope: nextScope }),
    });
    if (!res.ok) {
      const detail = await res.text();
      alert(`Rule scope update failed: ${res.status} ${detail}`);
      rule.window_scope = prevScope;
      renderRulesScope();
    }
  } catch (e) {
    alert(`Rule scope network error: ${e.message}`);
    rule.window_scope = prevScope;
    renderRulesScope();
  }
}

function toggleRulesScopePanel() {
  const panel = $("rules-scope-panel");
  const btn = $("rules-scope-toggle");
  const expanded = panel.hidden;
  panel.hidden = !expanded;
  btn.setAttribute("aria-expanded", String(expanded));
  // Chevron rotates via CSS; toggling a class is the cleanest way to
  // keep the arrow direction in sync without re-reading layout.
  btn.classList.toggle("expanded", expanded);
  if (expanded) {
    // Always re-fetch on expand so the picker reflects the desktop's
    // current rule set + cross-workspace window list. Cheap call;
    // running auto-detect on the desktop is already on the hot path.
    loadRulesScope();
  }
}

// ---- scroll / recapture the open window --------------------------------

async function recaptureWindow(btn) {
  // POST to /snapshot — captures a fresh PNG of the window's region with
  // no scroll and no clicks. Useful when the user has rearranged windows
  // on the desktop and the stored tile is showing the wrong content.
  const id = state.current;
  if (!id) return;
  if (currentIsAgentWindow()) {
    if (btn) {
      btn.disabled = true;
      btn.classList.add("loading");
    }
    await refreshCodexNavigator({ announce: true, conversationOnChange: false });
    const surfaces = ["conversation"];
    if ($("agent-sidebar-fallback").open) surfaces.push("sidebar");
    await refreshAgentSurfaces(surfaces, false);
    if (btn) {
      setTimeout(() => {
        btn.disabled = false;
        btn.classList.remove("loading");
      }, 400);
    }
    return;
  }
  if (btn) {
    btn.disabled = true;
    btn.classList.add("loading");
  }
  try {
    const res = await fetch(
      `/api/windows/${encodeURIComponent(id)}/snapshot`,
      { method: "POST" }
    );
    if (!res.ok) {
      const detail = await res.text();
      setSendStatus(`Recapture failed: ${res.status} ${detail}`, "error");
    } else {
      setSendStatus("New screenshot incoming.", "success");
    }
  } catch (e) {
    setSendStatus(`Recapture network error: ${e.message}`, "error");
  } finally {
    if (btn) {
      // Shorter lockout than scroll — there's no scroll-animation settle
      // delay, just the capture itself plus a short SSE roundtrip.
      setTimeout(() => {
        btn.disabled = false;
        btn.classList.remove("loading");
      }, 700);
    }
  }
}

async function scrollWindow(amount, btn) {
  const id = state.current;
  if (!id || state.scrollPending) return;
  state.scrollPending = true;
  if (btn) {
    btn.disabled = true;
    btn.classList.add("loading");
  }
  try {
    const res = await fetch(
      `/api/windows/${encodeURIComponent(id)}/scroll`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount }),
      }
    );
    if (!res.ok) {
      const detail = await res.text();
      setSendStatus(`Scroll failed: ${res.status} ${detail}`, "error");
    } else {
      setSendStatus("Scrolled. New screenshot incoming.", "success");
      if (currentIsAgentWindow()) {
        setTimeout(() => refreshAgentSurfaces(["conversation"]), 350);
      }
    }
  } catch (e) {
    setSendStatus(`Scroll network error: ${e.message}`, "error");
  } finally {
    state.scrollPending = false;
    if (btn) {
      // Lockout long enough for the post-scroll capture to settle and
      // the SSE event to land — otherwise a fast double-tap shoots
      // off two scrolls before the screenshot rolls in.
      setTimeout(() => {
        btn.disabled = false;
        btn.classList.remove("loading");
      }, 800);
    }
  }
}

// ---- Workspace switcher (Windows virtual desktops) ---------------------
//
// ◀ ▶ fire Ctrl+Win+Right / Left on the laptop; the desktop's COM
// poll picks up the new workspace GUID and auto-activates the setup
// bound to it (if any), and the SSE stream pushes us the new state.
// 📌 binds the currently active setup to whichever workspace is in
// front on the desktop right now.

async function switchWorkspace(direction, btn) {
  if (btn) {
    btn.disabled = true;
    btn.classList.add("loading");
  }
  try {
    const res = await fetch("/api/bridge/workspace/switch", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ direction }),
    });
    if (!res.ok) {
      const detail = await res.text();
      alert(`Workspace switch failed: ${res.status} ${detail}`);
      return;
    }
    const data = await res.json();
    updateWorkspaceLabel(data.workspace_id);
    // The desktop re-runs auto-detect synchronously on workspace
    // change, so the next /api/state fetch (which SSE will trigger
    // via window_state events) shows the new workspace's windows
    // automatically.
  } catch (e) {
    alert(`Workspace switch network error: ${e.message}`);
  } finally {
    if (btn) {
      // Give the desktop transition + state propagation ~500ms
      // before re-enabling, so a fast double-tap doesn't skip two
      // workspaces by accident.
      setTimeout(() => {
        btn.disabled = false;
        btn.classList.remove("loading");
      }, 500);
    }
  }
}

function updateWorkspaceLabel(wid) {
  const label = $("ws-label");
  if (!label) return;
  if (!wid) {
    label.textContent = "workspace";
    return;
  }
  // The GUID is too long to read — show a short stable hash of it
  // so the user can at least tell the workspace changed.
  const tag = wid.replace(/[{}-]/g, "").slice(0, 6);
  label.textContent = `workspace · ${tag}`;
}

renderNotifToggle();
renderAutoReloadToggle();
renderRulesToggle();

$("settings-btn").addEventListener("click", () => {
  const panel = $("settings-panel");
  panel.hidden = !panel.hidden;
  $("settings-btn").setAttribute("aria-expanded", String(!panel.hidden));
});
$("notif-toggle").addEventListener("click", toggleNotifications);
$("notif-test").addEventListener("click", async (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  const original = btn.textContent;
  btn.textContent = "Sending…";
  try {
    const res = await fetch("/api/notifications/test", { method: "POST" });
    if (!res.ok) {
      alert(`Test failed: ${res.status} ${await res.text()}`);
      return;
    }
    const data = await res.json();
    if (data.sent === 0 && data.failed === 0) {
      alert("No subscriptions registered yet — turn Notifications on first.");
    } else if (data.sent === 0) {
      alert(`Push failed for all ${data.failed} subscription(s). Check the bridge log.`);
    }
  } catch (err) {
    alert(`Test network error: ${err.message}`);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
});

// Reconcile the toggle with the actual SW subscription state on load.
// localStorage's flag can drift if the user toggled in another tab, or
// if the OS-level permission was revoked between sessions — in either
// case we want the visible toggle to match reality, not the stored
// guess.
(async () => {
  if (!("serviceWorker" in navigator)) return;
  try {
    // Register early so the SW is alive for any notification click
    // that happens later, even if the user hasn't toggled today.
    await navigator.serviceWorker.register("/sw.js", { scope: "/" });
    const reg = await navigator.serviceWorker.getRegistration("/");
    if (!reg) return;
    const sub = await reg.pushManager.getSubscription();
    const reallySubscribed = !!sub && Notification.permission === "granted";
    if (reallySubscribed !== state.notifEnabled) {
      state.notifEnabled = reallySubscribed;
      localStorage.setItem("ap.notif", reallySubscribed ? "1" : "0");
      renderNotifToggle();
    }
  } catch {}
})();
$("autoreload-toggle").addEventListener("click", toggleAutoReload);
$("reload-btn").addEventListener("click", reloadBridge);
$("ws-prev").addEventListener("click", (e) => switchWorkspace("prev", e.currentTarget));
$("ws-next").addEventListener("click", (e) => switchWorkspace("next", e.currentTarget));
$("rules-toggle").addEventListener("click", toggleRules);
$("rules-scope-toggle").addEventListener("click", toggleRulesScopePanel);

// Info buttons in the settings panel — each (i) reveals/hides the
// matching <p class="setting-info" data-info="..."> paragraph. One
// delegated handler so adding a new setting only needs the HTML
// pieces (button + paragraph with the same data-info key).
for (const btn of document.querySelectorAll(".info-btn[data-info]")) {
  btn.addEventListener("click", () => {
    const key = btn.dataset.info;
    const target = document.querySelector(`.setting-info[data-info="${key}"]`);
    if (!target) return;
    const show = target.hidden;
    target.hidden = !show;
    btn.setAttribute("aria-expanded", String(show));
    btn.classList.toggle("active", show);
  });
}
for (const btn of document.querySelectorAll(".scroll-btn[data-amount]")) {
  const amount = Number(btn.dataset.amount || "1");
  btn.addEventListener("click", () => scrollWindow(amount, btn));
}
$("recapture-btn").addEventListener("click", (e) =>
  recaptureWindow(e.currentTarget)
);
$("agent-refresh").addEventListener("click", (e) => {
  const btn = e.currentTarget;
  btn.disabled = true;
  const surfaces = ["conversation"];
  if ($("agent-sidebar-fallback").open) surfaces.push("sidebar");
  Promise.all([
    refreshCodexNavigator({ announce: true, conversationOnChange: false }),
    refreshAgentSurfaces(surfaces),
  ])
    .finally(() => { btn.disabled = false; });
});
$("agent-sidebar-img").addEventListener("click", (e) =>
  clickAgentSurface("sidebar", e)
);
$("agent-sidebar-fallback").addEventListener("toggle", (e) => {
  if (e.currentTarget.open && currentIsAgentWindow()) {
    loadAgentSurface("sidebar").catch(() => {});
  }
});

// Conversation gestures are phone-native: a tap remains an exact desktop
// click, while a vertical swipe becomes a distance-scaled wheel batch. The
// screenshot refreshes once the gesture settles rather than on every move.
let conversationGesture = null;
const conversationImage = $("agent-conversation-img");
conversationImage.addEventListener("pointerdown", (e) => {
  if (!currentIsAgentWindow() || (e.pointerType === "mouse" && e.button !== 0)) return;
  conversationGesture = {
    pointerId: e.pointerId,
    startY: e.clientY,
    lastY: e.clientY,
    startedAt: performance.now(),
    moved: false,
  };
  try { conversationImage.setPointerCapture(e.pointerId); } catch {}
  conversationImage.closest(".conversation-viewport").classList.add("gesture-active");
});
conversationImage.addEventListener("pointermove", (e) => {
  if (!conversationGesture || conversationGesture.pointerId !== e.pointerId) return;
  conversationGesture.lastY = e.clientY;
  if (Math.abs(e.clientY - conversationGesture.startY) > 10) {
    conversationGesture.moved = true;
    e.preventDefault();
  }
});
conversationImage.addEventListener("pointerup", (e) => {
  if (!conversationGesture || conversationGesture.pointerId !== e.pointerId) return;
  const gesture = conversationGesture;
  conversationGesture = null;
  conversationImage.closest(".conversation-viewport").classList.remove("gesture-active");
  try { conversationImage.releasePointerCapture(e.pointerId); } catch {}
  const deltaY = e.clientY - gesture.startY;
  if (!gesture.moved || Math.abs(deltaY) < 24) {
    clickAgentSurface("conversation", e);
    return;
  }
  e.preventDefault();
  const elapsed = Math.max(80, performance.now() - gesture.startedAt);
  const velocityBoost = Math.min(1.8, 1 + Math.abs(deltaY) / elapsed);
  const rawAmount = Math.round((deltaY / 13) * velocityBoost);
  const amount = Math.sign(rawAmount) * Math.max(4, Math.min(42, Math.abs(rawAmount)));
  scrollWindow(amount, null);
});
conversationImage.addEventListener("pointercancel", (e) => {
  if (!conversationGesture || conversationGesture.pointerId !== e.pointerId) return;
  conversationGesture = null;
  conversationImage.closest(".conversation-viewport").classList.remove("gesture-active");
});

// Desktop browsers don't have native pull-to-refresh, so we approximate
// it: when the page is already at the top and the user keeps scrolling
// up (two-finger trackpad gesture or mouse wheel), accumulate the
// upward delta. Once it crosses a threshold, fire the same scroll-and-
// screenshot the button does. Only active when a window is selected
// and the scroll button isn't already locked out, so a quick burst
// translates to one scroll, not many. Phones already behave well
// because of native overscroll → not jeopardised by this.
let _overscrollAccum = 0;
const _OVERSCROLL_TRIGGER_PX = 220;
window.addEventListener(
  "wheel",
  (e) => {
    if (state.view !== "snapshots" || !state.current) {
      _overscrollAccum = 0;
      return;
    }
    if (window.scrollY > 4) {
      _overscrollAccum = 0;
      return;
    }
    if (e.deltaY >= -1) {
      _overscrollAccum = 0;
      return;
    }
    _overscrollAccum += -e.deltaY;
    if (_overscrollAccum >= _OVERSCROLL_TRIGGER_PX) {
      _overscrollAccum = 0;
      // Target the positive-amount button specifically — the
      // overscroll pull-up gesture should always trigger an up-scroll
      // even if the DOM order of the scroll-down button ever changes.
      const btn = document.querySelector('.scroll-btn[data-amount="15"]');
      if (btn && !btn.disabled) scrollWindow(15, btn);
    }
  },
  { passive: true }
);

$("send-go").addEventListener("click", sendOrQueue);
$("queue-clear").addEventListener("click", clearQueue);
$("send-text").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
    e.preventDefault();
    sendOrQueue();
  }
});
$("lightbox").addEventListener("click", (e) => {
  // Click on backdrop closes; clicking the image, prev/next, or any
  // control inside the lightbox doesn't, so pinch-zoom and arrow taps
  // still work on mobile.
  if (e.target === $("lightbox")) closeLightbox();
});
$("lightbox-close").addEventListener("click", closeLightbox);
$("lightbox-prev").addEventListener("click", (e) => {
  e.stopPropagation();
  lightboxStep(-1);
});
$("lightbox-next").addEventListener("click", (e) => {
  e.stopPropagation();
  lightboxStep(1);
});

// Keyboard navigation — only meaningful on the desktop browser, but
// wiring it up costs nothing on mobile (no hardware keys → no events).
window.addEventListener("keydown", (e) => {
  if ($("lightbox").hidden) return;
  if (e.key === "ArrowLeft") { e.preventDefault(); lightboxStep(-1); }
  else if (e.key === "ArrowRight") { e.preventDefault(); lightboxStep(1); }
  else if (e.key === "Escape") { e.preventDefault(); closeLightbox(); }
});

// Touch swipe — record the start point on touchstart, compute the
// horizontal delta on touchend, and only treat it as a swipe if the
// gesture was mostly horizontal (filters out a vertical pan/scroll
// trying to look at a tall snapshot).
let _touchStartX = null;
let _touchStartY = null;
$("lightbox-img").addEventListener("touchstart", (e) => {
  if (e.touches.length !== 1) { _touchStartX = null; return; }
  _touchStartX = e.touches[0].clientX;
  _touchStartY = e.touches[0].clientY;
}, { passive: true });
$("lightbox-img").addEventListener("touchend", (e) => {
  if (_touchStartX === null) return;
  const t = e.changedTouches[0];
  const dx = t.clientX - _touchStartX;
  const dy = t.clientY - _touchStartY;
  const startX = _touchStartX;
  const startY = _touchStartY;
  _touchStartX = null;
  // Horizontal swipe → step through snapshots.
  if (Math.abs(dx) >= 50 && Math.abs(dx) > Math.abs(dy)) {
    lightboxStep(dx < 0 ? 1 : -1);
    return;
  }
  // Otherwise, if the gesture barely moved, treat it as a tap and
  // place the click crosshair (touchend coords; touchstart and
  // touchend are within ~10 px of each other for a tap).
  if (Math.abs(dx) < 12 && Math.abs(dy) < 12) {
    handleLightboxTap(t.clientX, t.clientY);
  }
}, { passive: true });
// Desktop browsers (and the dev box) get a plain click handler for
// the same tap-to-place behaviour — the touch path above doesn't fire
// on mouse events.
$("lightbox-img").addEventListener("click", (e) => {
  handleLightboxTap(e.clientX, e.clientY);
});
$("lightbox-click-cancel").addEventListener("click", hideClickCrosshair);
$("lightbox-click-confirm").addEventListener("click", confirmClickAtCrosshair);

// Browser/phone back: if the lightbox is open and the entry being
// popped is ours, swallow it as a close. Clearing the flag *before*
// the close call stops closeLightbox from calling history.back() a
// second time (which would actually exit the PWA).
window.addEventListener("popstate", () => {
  if (!$("lightbox").hidden) {
    state._lightboxPushedHistory = false;
    closeLightbox();
  }
});

loadState();
connectSSE();
