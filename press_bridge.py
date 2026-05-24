"""Optional remote bridge for auto-press.

Exposes a tiny FastAPI app over Tailscale that lets a phone:
  1. see which Cursor windows are idle vs busy
  2. browse the last N PNG snapshots of each window
  3. receive ntfy notifications when a window flips to idle
  4. (planned) send a reply into a specific window

Designed to add zero overhead when the bridge service is off — the
server thread is only started when the user flips the toggle on.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional


LOG = logging.getLogger("press_bridge")

EVENT_BUFFER_MAX = 100
SSE_KEEPALIVE_SECS = 15.0
SNAPSHOTS_PER_WINDOW = 10

PHONE_DIR = Path(__file__).resolve().parent / "bridge_phone"


# ---- pluggable callbacks supplied by the host (press_ui.MainWindow) -----


@dataclass
class BridgeCallbacks:
    """Surface area the bridge needs from the rest of the app.

    Keeping this as a dataclass of callables (rather than importing press_ui)
    lets press_bridge run headless in tests and avoids a Qt import cycle.
    """

    cfg_snapshot: Callable[[], dict]
    re_match_rule: Callable[[str], list[tuple[float, tuple[int, int]]]]
    perform_send: Callable[[tuple[int, int], str, dict], None]
    # Window-aware send: called with the full window dict from config (so the
    # callback can pick chat_target / region / name) plus the text and the
    # bridge config (timing knobs). Used by /api/windows/{id}/send and by
    # the queue-drain path on busy → idle transitions.
    perform_window_send: Optional[Callable[[dict, str, dict], None]] = None
    # Move cursor to the centre of the window region, click to take focus,
    # and scroll by ``amount`` wheel notches (positive = up). Used by
    # /api/windows/{id}/scroll so the phone can scroll older messages into
    # view without leaving the bridge UI.
    perform_window_scroll: Optional[Callable[[dict, int, dict], None]] = None
    # Click an arbitrary point inside the window expressed as fractional
    # coords (x_frac, y_frac in [0, 1]) of the window's region. Used by
    # /api/windows/{id}/click_at — phone taps on a snapshot, fractions
    # round-trip to screen coords via region.x + x_frac * region.w.
    # DPI doesn't enter the math because the snapshot is captured at
    # the same physical pixels we're clicking into.
    perform_window_click_at: Optional[
        Callable[[dict, float, float, dict], tuple[int, int]]
    ] = None
    perform_read: Optional[Callable[[str, dict], Optional[str]]] = None
    # Hot-reload hook for /api/admin/reload. The callback is expected to
    # importlib.reload(press_bridge) and restart the FastAPI service so
    # the user can ship code changes from a phone over Tailscale.
    request_reload: Optional[Callable[[], None]] = None
    # Reflect / mutate the engine's "rules running" flag from the phone,
    # so the user can pause auto-clicking while reading the agent's
    # output and resume when done.
    is_rules_running: Optional[Callable[[], bool]] = None
    set_rules_running: Optional[Callable[[bool], None]] = None
    # Phone-side rename: takes (window_id, new_name) and returns True if
    # the window was found + persisted, False otherwise. The desktop
    # side mutates templates/config.json so the name survives restarts.
    rename_window: Optional[Callable[[str, str], bool]] = None
    # Windows virtual-desktop (workspace) switch keystroke. Returns
    # a dict ``{workspace_id}`` after firing Ctrl+Win+Right/Left and
    # waiting for the desktop transition to settle. The host side
    # also re-runs auto-detect on workspace change automatically; the
    # endpoint just exposes the keystroke so the phone can drive it.
    workspace_switch: Optional[Callable[[str], dict]] = None
    # Web Push hooks. Each runs on the host side (desktop process)
    # under the cfg lock so subscription mutations and VAPID key
    # generation persist via the same save_config path everything
    # else uses.
    ensure_vapid_keys: Optional[Callable[[], str]] = None
    add_push_subscription: Optional[
        Callable[[dict, Optional[str]], Optional[dict]]
    ] = None
    remove_push_subscription: Optional[Callable[[str], int]] = None
    # Fan-out helper: send the same payload to every registered
    # subscription. Returns ``{"sent": int, "failed": int,
    # "pruned": int}`` so the test endpoint can echo a summary.
    send_push_to_all: Optional[Callable[[dict], dict]] = None
    # Per-rule editing surface for the phone's "Window-specific rules"
    # settings section. ``rules_snapshot`` returns a small projection of
    # the cfg shaped ``{"rules": [{id, name, enabled, window_scope},
    # ...], "available_windows": [{name, visible}, ...]}`` — only the
    # fields the phone needs (no template paths, thresholds, etc.).
    # The set_* callbacks return True iff the rule id was found; the
    # host side is responsible for persisting and refreshing its own
    # UI. Both reuse rule.window_scope / rule.enabled as-is, no
    # schema change.
    rules_snapshot: Optional[Callable[[], dict]] = None
    set_rule_enabled: Optional[Callable[[str, bool], bool]] = None
    set_rule_window_scope: Optional[Callable[[str, list[str]], bool]] = None


# ---- per-window state + snapshot ring buffer ----------------------------


class WindowStore:
    """Thread-safe store of latest window states + a small ring of PNG
    snapshots per window. Bounded to ``snapshots_per_window`` so memory
    stays predictable; older snapshots fall off when new ones land."""

    def __init__(self, snapshots_per_window: int = SNAPSHOTS_PER_WINDOW) -> None:
        self._max = max(1, int(snapshots_per_window))
        self._max_queue = 10  # cap so a stuck window can't grow unbounded
        self._lock = threading.Lock()
        # window_id -> {"state": dict, "snapshots": deque[(iso_ts, png_bytes)]}
        self._windows: dict[str, dict] = {}
        # window_id -> list[str] of messages queued while the window is busy.
        # Drained one-per-transition when the window flips back to idle.
        self._queues: dict[str, list[str]] = {}

    def update(
        self,
        states: list[dict],
        images: dict[str, bytes],
        prune: bool = True,
    ) -> list[dict]:
        """Apply a fresh detector tick. Returns the list of windows whose
        idle/busy state changed since the previous tick — callers can use
        this to decide which transitions to surface (SSE, ntfy).

        Snapshot lifecycle: a busy → idle transition clears the existing
        snapshot deque before appending the fresh capture. This is the
        cycle the user wants — old scroll history from the *previous*
        idle session shouldn't shadow the new "agent just finished"
        moment. Snapshots accumulate during a single idle session via
        set_snapshot() (called by /api/windows/{id}/scroll); they
        persist through the following busy spell so the user can keep
        reviewing them; then the next busy → idle wipes and starts
        fresh.

        ``prune`` (default True) drops any windows currently in the
        store that aren't in the incoming states list — used by the
        worker tick which always sends the *complete* set so a removed
        window is purged. Partial updates (e.g. _post_send_recheck on
        one window after a phone-driven send) pass prune=False so they
        don't accidentally wipe the others.
        """
        now = datetime.now(timezone.utc).isoformat()
        transitions: list[dict] = []
        with self._lock:
            seen_ids: set[str] = set()
            for state in states:
                wid = state.get("id")
                if not wid:
                    continue
                seen_ids.add(wid)
                entry = self._windows.setdefault(
                    wid, {"state": {}, "snapshots": deque(maxlen=self._max)}
                )
                prev = entry["state"] or None
                prev_idle = bool(prev["idle"]) if prev else None
                prev_asking = bool(prev.get("asking")) if prev else None
                stored = {
                    "id": wid,
                    "name": state.get("name", "Cursor"),
                    "idle": bool(state.get("idle")),
                    "asking": bool(state.get("asking")),
                    "score": float(state.get("score", 0.0)),
                    "configured": bool(state.get("configured", False)),
                    "last_update": now,
                }
                entry["state"] = stored
                # Treat idle and asking as the "user can act now" states.
                # Any transition INTO that family (from busy) gets a
                # fresh snapshot session so the new visuals aren't mixed
                # with stale scroll captures from the previous one.
                prev_actionable = (prev_idle is True) or (prev_asking is True)
                curr_actionable = stored["idle"] or stored["asking"]
                if prev_actionable is False and curr_actionable:
                    entry["snapshots"].clear()
                png = images.get(wid)
                if png is not None:
                    # Store as a 3-tuple (timestamp, png, hash). Worker-
                    # side captures don't compute a hash; dedup only
                    # matters for scroll captures where the user can
                    # accidentally fire the same view twice.
                    entry["snapshots"].append((now, png, None))
                # Any change in idle OR asking counts as a transition for
                # SSE / ntfy fan-out — the phone needs to redraw on both.
                # ``flipped_to_idle`` and ``flipped_to_asking`` carry the
                # direction so push-notification callers can fire only on
                # the "X just became true" edges without re-deriving prev
                # state themselves.
                if prev is not None and (
                    prev_idle != stored["idle"] or prev_asking != stored["asking"]
                ):
                    entry_tr = dict(stored)
                    entry_tr["flipped_to_idle"] = (
                        prev_idle is False and stored["idle"]
                    )
                    entry_tr["flipped_to_asking"] = (
                        prev_asking is False and stored["asking"]
                    )
                    transitions.append(entry_tr)
            if prune:
                # Drop windows that disappeared from config (e.g. user removed).
                for wid in list(self._windows):
                    if wid not in seen_ids:
                        self._windows.pop(wid, None)
        return transitions

    def summaries(self) -> list[dict]:
        with self._lock:
            out: list[dict] = []
            for wid, entry in self._windows.items():
                if not entry["state"]:
                    continue
                summary = dict(entry["state"])
                summary["snapshot_count"] = len(entry["snapshots"])
                # Latest snapshot's ISO timestamp (None if no snapshot yet).
                # The phone uses this to render "captured Xs ago" instead of
                # the meaningless "newest / N ticks ago" label that made
                # sense only with a multi-frame ring buffer.
                # Snapshots are 3-tuples (ts, png, hash); first element
                # is always the timestamp regardless of length.
                summary["snapshot_at"] = (
                    entry["snapshots"][-1][0] if entry["snapshots"] else None
                )
                summary["pending"] = list(self._queues.get(wid, []))
                out.append(summary)
            return out

    def snapshot(self, window_id: str, idx: int) -> Optional[tuple[str, bytes]]:
        """Snapshot at ``idx`` (0 = newest). Returns (iso_ts, png_bytes).

        Internal storage is a 3-tuple (ts, png, hash); we strip the hash
        from the public return value so callers don't need to know.
        """
        with self._lock:
            entry = self._windows.get(window_id)
            if entry is None:
                return None
            snaps = list(entry["snapshots"])
        if not (0 <= idx < len(snaps)):
            return None
        # idx 0 is newest, deque appends new on the right.
        snap = snaps[len(snaps) - 1 - idx]
        return (snap[0], snap[1])

    def clear_window(self, window_id: str) -> None:
        with self._lock:
            self._windows.pop(window_id, None)
            self._queues.pop(window_id, None)

    def state_of(self, window_id: str) -> Optional[dict]:
        with self._lock:
            entry = self._windows.get(window_id)
            return dict(entry["state"]) if entry and entry["state"] else None

    def update_window_name(self, window_id: str, name: str) -> bool:
        """Mutate the cached display name of a window in place. Used by
        the rename endpoint so the new name appears on phone clients
        immediately rather than after the next worker tick."""
        if not isinstance(name, str) or not name.strip():
            return False
        with self._lock:
            entry = self._windows.get(window_id)
            if entry is None or not entry["state"]:
                return False
            entry["state"]["name"] = name
            return True

    def set_snapshot(
        self,
        window_id: str,
        png_bytes: bytes,
        rgb_hash: Optional[str] = None,
    ) -> bool:
        """Insert a snapshot for the window, bypassing the busy↔idle
        transition gate that update() applies. Used by paths like the
        scroll endpoint where we want a fresh frame regardless of state.
        ``rgb_hash`` (optional) lets callers stash a hash of the source
        bitmap for downstream dedup via last_snapshot_hash().
        Returns False if the window has no state yet (haven't seen a
        tick for it)."""
        if not png_bytes:
            return False
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            entry = self._windows.get(window_id)
            if entry is None or not entry["state"]:
                return False
            entry["snapshots"].append((now, png_bytes, rgb_hash))
            return True

    def last_snapshot_hash(self, window_id: str) -> Optional[str]:
        """Hash stashed alongside the most recent snapshot, if any. Used
        by the scroll path to skip identical captures (e.g. user scrolls
        when already at the top — same frame, no point storing it again)."""
        with self._lock:
            entry = self._windows.get(window_id)
            if not entry or not entry["snapshots"]:
                return None
            snap = entry["snapshots"][-1]
            return snap[2] if len(snap) >= 3 else None

    def touch_last_snapshot(self, window_id: str) -> bool:
        """Refresh the timestamp on the most recent snapshot without
        replacing its bytes. Used by the scroll path so the user sees
        feedback (the "captured Xs ago" label resets, an SSE window_state
        event fires) when the dedup hash matches and we'd otherwise skip
        the capture entirely."""
        now = datetime.now(timezone.utc).isoformat()
        with self._lock:
            entry = self._windows.get(window_id)
            if not entry or not entry["snapshots"]:
                return False
            last = entry["snapshots"][-1]
            png = last[1] if len(last) >= 2 else None
            rgb_hash = last[2] if len(last) >= 3 else None
            entry["snapshots"][-1] = (now, png, rgb_hash)
            return True

    # ---- pending-message queue ----

    def enqueue(self, window_id: str, text: str) -> tuple[bool, int]:
        """Append a queued message; returns (accepted, new_position).
        Caps at ``self._max_queue``. Empty / whitespace-only text is OK
        — the send pipeline interprets that as "click + Enter, no paste"."""
        if text is None:
            return False, 0
        with self._lock:
            q = self._queues.setdefault(window_id, [])
            if len(q) >= self._max_queue:
                return False, len(q)
            q.append(text)
            return True, len(q)

    def dequeue(self, window_id: str) -> Optional[str]:
        with self._lock:
            q = self._queues.get(window_id)
            if not q:
                return None
            text = q.pop(0)
            if not q:
                self._queues.pop(window_id, None)
            return text

    def pop_at(self, window_id: str, idx: int) -> Optional[str]:
        """Remove the queued message at ``idx`` (0 = oldest) and return it.
        Used by /queue/{idx}/send_now so the user can fire one specific
        queued message without waiting for an idle transition."""
        with self._lock:
            q = self._queues.get(window_id)
            if not q or not (0 <= idx < len(q)):
                return None
            text = q.pop(idx)
            if not q:
                self._queues.pop(window_id, None)
            return text

    def update_at(self, window_id: str, idx: int, text: str) -> bool:
        """Replace the queued message at ``idx`` with ``text``. Returns
        False if the index is out of range. Empty / whitespace text is
        accepted (same semantics as enqueue)."""
        if text is None:
            return False
        with self._lock:
            q = self._queues.get(window_id)
            if not q or not (0 <= idx < len(q)):
                return False
            q[idx] = text
            return True

    def pending(self, window_id: str) -> list[str]:
        with self._lock:
            return list(self._queues.get(window_id, []))

    def clear_queue(self, window_id: str) -> int:
        with self._lock:
            removed = len(self._queues.get(window_id, []))
            self._queues.pop(window_id, None)
            return removed


# ---- in-process event hub -----------------------------------------------


class EventHub:
    """Thread-safe ring buffer + asyncio fan-out for rule_matched events.

    The Qt worker emits via ``publish`` (any thread). SSE clients in the
    asyncio loop each get their own ``asyncio.Queue`` registered through
    ``subscribe`` and torn down via ``unsubscribe``. ``dismiss`` flips a
    flag on a buffered event so the phone UI can hide it.
    """

    def __init__(self, maxlen: int = EVENT_BUFFER_MAX) -> None:
        self._buffer: deque[dict] = deque(maxlen=maxlen)
        self._dismissed: set[str] = set()
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._subscribers: list[asyncio.Queue] = []

    def attach_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def publish(self, event: dict) -> None:
        """Publish a rule_matched event (back-compat name for the existing
        SSE channel). Equivalent to publish_typed("rule_matched", event)."""
        self.publish_typed("rule_matched", event)

    def publish_typed(self, event_type: str, event: dict) -> None:
        """Publish an event tagged with an SSE event-name. Subscribers
        receive a tuple (event_type, payload) and the SSE handler routes
        it to the matching named channel (event: <event_type>)."""
        payload = (event_type, event)
        with self._lock:
            # Keep only rule_matched in the persistent buffer; window_state
            # events are ephemeral (the WindowStore is the source of truth).
            if event_type == "rule_matched":
                self._buffer.append(event)
            subs = list(self._subscribers)
        loop = self._loop
        if loop is None:
            return
        for queue in subs:
            with suppress(Exception):
                loop.call_soon_threadsafe(queue.put_nowait, payload)

    def recent(self) -> list[dict]:
        with self._lock:
            return [dict(e, dismissed=(e["event_id"] in self._dismissed)) for e in self._buffer]

    def find(self, event_id: str) -> Optional[dict]:
        with self._lock:
            for event in self._buffer:
                if event.get("event_id") == event_id:
                    return dict(event)
        return None

    def dismiss(self, event_id: str) -> bool:
        with self._lock:
            for event in self._buffer:
                if event.get("event_id") == event_id:
                    self._dismissed.add(event_id)
                    return True
        return False

    def subscribe(self) -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue()
        with self._lock:
            self._subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        with self._lock:
            with suppress(ValueError):
                self._subscribers.remove(queue)


# ---- ntfy notifier (best-effort) ----------------------------------------


def send_ntfy(bridge_cfg: dict, event: dict) -> None:
    """Fire-and-forget notification. Failures only log; never raise."""
    topic = (bridge_cfg.get("ntfy_topic") or "").strip()
    if not topic:
        return
    server = (bridge_cfg.get("ntfy_server") or "https://ntfy.sh").rstrip("/")
    rule_name = event.get("rule_name") or event.get("rule_id") or "Rule"
    monitor = event.get("monitor_index", -1)
    title = str(rule_name)
    body = f"{event.get('timestamp_iso','?')} · monitor {monitor}"
    url = f"{server}/{topic}"
    try:
        import httpx  # type: ignore

        with httpx.Client(timeout=5.0) as client:
            client.post(
                url,
                content=body.encode("utf-8"),
                headers={
                    "Title": title,
                    "Tags": "computer",
                    # ntfy custom click-action: open the phone UI focused on
                    # this rule. Hosting URL is unknown here, so we just use
                    # the relative path; ntfy lets the user override.
                    "Click": f"/?focus={event.get('rule_id','')}",
                },
            )
    except Exception as exc:
        LOG.warning("ntfy publish failed: %s", exc)


# ---- bridge service (started/stopped by MainWindow) ---------------------


def _safe_perform_window_send(send_fn, win_cfg, text, bridge_cfg) -> None:
    """Run a window-send callback, swallowing exceptions to a log line so
    a flaky click can't blow up the daemon thread the bridge fired."""
    try:
        send_fn(win_cfg, text, bridge_cfg)
    except Exception as exc:
        LOG.warning("window send failed: %s", exc)


def _png_from_rgb(rgb) -> Optional[bytes]:
    """RGB ndarray → PNG bytes; None on failure. Local copy because press_ui
    is the other call site and we don't want to import Qt here."""
    try:
        from io import BytesIO

        from PIL import Image
    except Exception:
        return None
    try:
        buf = BytesIO()
        Image.fromarray(rgb, mode="RGB").save(buf, format="PNG", optimize=False)
        return buf.getvalue()
    except Exception:
        return None


def _post_scroll_recheck(service: "BridgeService", window_id: str, delay_s: float = 0.6) -> None:
    """After scrolling, capture a fresh snapshot of the window region and
    push it into the store via set_snapshot, bypassing the busy↔idle
    transition gate. Cursor's scroll animation needs ~0.5 s to settle on
    most machines; we wait a hair more before capturing.

    Doesn't run idle detection — scrolling doesn't change idle/busy
    state, so the existing detector tick is the right place to track
    that. We only care about the new snapshot here.
    """
    import sys as _sys
    if _sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
    time.sleep(max(0.1, float(delay_s)))
    try:
        cfg = service.callbacks.cfg_snapshot() or {}
    except Exception:
        return
    bridge_cfg = cfg.get("bridge") or {}
    win_cfg = next(
        (w for w in bridge_cfg.get("windows", []) if w.get("id") == window_id),
        None,
    )
    if not win_cfg:
        return
    region = win_cfg.get("region")
    if not region or len(region) != 4:
        return
    try:
        from press_engine import capture_screen_rgb
    except Exception:
        return
    try:
        rgb = capture_screen_rgb(
            (int(region[0]), int(region[1]), int(region[2]), int(region[3]))
        )
    except Exception as exc:
        LOG.warning("post-scroll capture failed: %s", exc)
        return
    # Dedup: if the user fires the scroll button when the chat is
    # already at the top, the new capture is byte-identical to the
    # previous one. Hash the raw RGB bytes (cheap, deterministic) and
    # skip storing a duplicate. Worker-tick captures don't compute a
    # hash so this only catches scroll-vs-scroll dupes, which is fine —
    # the busy→idle transition always replaces anyway.
    import hashlib
    rgb_hash = hashlib.md5(rgb.tobytes()).hexdigest()
    if service.windows.last_snapshot_hash(window_id) == rgb_hash:
        # Same frame as before — but still bump the timestamp on the
        # existing snapshot and fan an SSE event so the user gets
        # visible feedback that scroll fired (freshness pill resets,
        # "captured Xs ago" label restarts at 0). Without this the UI
        # looks frozen when scrolling at the top of the chat.
        if service.windows.touch_last_snapshot(window_id):
            for s in service.windows.summaries():
                service.hub.publish_typed("window_state", s)
        return
    png = _png_from_rgb(rgb)
    if not png:
        return
    if service.windows.set_snapshot(window_id, png, rgb_hash):
        # Push state so connected phones refetch the snapshot URL.
        for s in service.windows.summaries():
            service.hub.publish_typed("window_state", s)


def _post_send_recheck(service: "BridgeService", window_id: str, delay_s: float = 2.0) -> None:
    """Re-evaluate one window after a paste so the phone sees the state
    flip without waiting for the next engine tick. Cursor needs a moment
    to register the keystroke, so a short delay before the recapture
    catches the busy state much more reliably."""
    import sys as _sys
    if _sys.platform.startswith("win"):
        try:
            import ctypes
            ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass
    time.sleep(max(0.1, float(delay_s)))
    try:
        cfg = service.callbacks.cfg_snapshot() or {}
    except Exception:
        return
    bridge_cfg = cfg.get("bridge") or {}
    if not bridge_cfg.get("idle_template_path"):
        return
    win_cfg = next(
        (w for w in bridge_cfg.get("windows", []) if w.get("id") == window_id),
        None,
    )
    if not win_cfg:
        return
    try:
        from press_engine import evaluate_bridge_windows
    except Exception:
        return
    single_cfg = dict(bridge_cfg)
    single_cfg["windows"] = [win_cfg]
    try:
        states = evaluate_bridge_windows(single_cfg, capture_rgb=True)
    except Exception as exc:
        LOG.warning("post-send recheck failed: %s", exc)
        return
    if not states:
        return
    images: dict[str, bytes] = {}
    slim: list[dict] = []
    for s in states:
        rgb = s.pop("rgb", None)
        slim.append(s)
        if rgb is None or not s.get("id"):
            continue
        png = _png_from_rgb(rgb)
        if png is not None:
            images[s["id"]] = png
    # Partial update — only this one window's state. prune=False so
    # the other windows currently tracked in the store aren't dropped
    # from the phone view by what's effectively a single-window tick.
    service.update_window_states(slim, images, prune=False)


class BridgeService:
    def __init__(self, callbacks: BridgeCallbacks) -> None:
        self.callbacks = callbacks
        self.hub = EventHub()
        self.windows = WindowStore()
        self._thread: Optional[threading.Thread] = None
        self._server: Any = None  # uvicorn.Server
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cfg_snapshot_at_start: dict = {}

    # --- lifecycle ---

    def start(self, bridge_cfg: dict) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._cfg_snapshot_at_start = dict(bridge_cfg)
        self._thread = threading.Thread(target=self._run, daemon=True, name="bridge")
        self._thread.start()

    def stop(self, timeout: float = 3.0) -> None:
        server = self._server
        if server is not None:
            server.should_exit = True
        thread = self._thread
        if thread is not None:
            thread.join(timeout=timeout)
        self._thread = None
        self._server = None
        self._loop = None

    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def publish_event(self, event: dict) -> None:
        """Called from any thread when a rule matches."""
        self.hub.publish(event)
        # Notify in a worker thread so a slow ntfy server can't stall the
        # Qt event loop.
        bridge_cfg = self._current_bridge_cfg()
        if bridge_cfg.get("ntfy_topic"):
            threading.Thread(
                target=send_ntfy, args=(bridge_cfg, event), daemon=True
            ).start()

    def update_window_states(
        self,
        states: list[dict],
        images: dict[str, bytes],
        prune: bool = True,
    ) -> None:
        """Called from the engine worker every detection tick. Updates the
        ring buffer, fans state out over SSE, fires ntfy on busy → idle,
        and drains one queued message per window that just flipped idle.

        ``prune`` is forwarded to WindowStore.update — the worker tick
        sends a complete states list (prune=True), but partial-recheck
        paths (e.g. _post_send_recheck on one window) call with
        prune=False so the other windows aren't dropped from the store.
        """
        transitions = self.windows.update(states, images, prune=prune)
        # Always fan a "window_state" event so phone clients can stay in
        # sync without polling /api/state.
        for state in self.windows.summaries():
            self.hub.publish_typed("window_state", state)
        bridge_cfg = self._current_bridge_cfg()
        # Notify only on busy → idle (more interesting than the reverse).
        if bridge_cfg.get("ntfy_topic"):
            for tr in transitions:
                if tr.get("idle"):
                    threading.Thread(
                        target=send_ntfy,
                        args=(bridge_cfg, {
                            "rule_name": tr.get("name"),
                            "rule_id": tr.get("id"),
                            "monitor_index": -1,
                            "timestamp_iso": tr.get("last_update", ""),
                        }),
                        daemon=True,
                    ).start()

        # Web Push fan-out. Triggers: window flipped TO asking (multi-
        # choice prompt), or window flipped TO idle (and isn't asking,
        # since asking already covered the actionable case). Asking
        # takes priority over idle in the same tick to avoid double-
        # notifying the same window.
        if self.callbacks.send_push_to_all is not None:
            for tr in transitions:
                if tr.get("flipped_to_asking"):
                    payload = {
                        "title": tr.get("name") or "Cursor",
                        "body": "Asking — needs your pick.",
                        "tag": f"asking:{tr.get('id', '')}",
                        "window_id": tr.get("id"),
                        "kind": "asking",
                    }
                elif tr.get("flipped_to_idle") and not tr.get("asking"):
                    payload = {
                        "title": tr.get("name") or "Cursor",
                        "body": "Idle — ready for input.",
                        "tag": f"idle:{tr.get('id', '')}",
                        "window_id": tr.get("id"),
                        "kind": "idle",
                    }
                else:
                    continue
                threading.Thread(
                    target=self._safe_push_fanout,
                    args=(payload,),
                    daemon=True,
                ).start()

        # Drain one queued message per just-idle window. We only send one
        # per idle transition because firing it will likely flip the
        # window back to busy; remaining queued messages wait for the
        # next tick.
        if self.callbacks.perform_window_send is None:
            return
        cfg = {}
        try:
            cfg = self.callbacks.cfg_snapshot() or {}
        except Exception:
            return
        cfg_windows = {
            w.get("id"): w for w in (cfg.get("bridge") or {}).get("windows", [])
        }
        for tr in transitions:
            if not tr.get("idle"):
                continue
            wid = tr.get("id")
            win_cfg = cfg_windows.get(wid)
            if not win_cfg:
                continue
            text = self.windows.dequeue(wid)
            if text is None:
                continue
            # Run send off the engine tick so a 2-second click+paste
            # doesn't block the next detection. Chain a delayed recheck
            # so the phone sees the busy flip without waiting for the
            # next engine tick.
            def _drain(send_fn=self.callbacks.perform_window_send,
                       wc=win_cfg, t=text, bc=bridge_cfg, wid=wid):
                _safe_perform_window_send(send_fn, wc, t, bc)
                _post_send_recheck(self, wid)

            threading.Thread(target=_drain, daemon=True).start()

    def _safe_push_fanout(self, payload: dict) -> None:
        """Run ``send_push_to_all`` from a daemon thread without
        letting any push-service hiccup blow up the worker. Errors
        are swallowed to a log line — every individual subscription's
        delivery is best-effort anyway."""
        if self.callbacks.send_push_to_all is None:
            return
        try:
            self.callbacks.send_push_to_all(payload)
        except Exception as exc:
            LOG.warning("web-push fan-out failed: %s", exc)

    def _current_bridge_cfg(self) -> dict:
        try:
            cfg = self.callbacks.cfg_snapshot() or {}
        except Exception:
            cfg = {}
        return dict(cfg.get("bridge") or self._cfg_snapshot_at_start or {})

    # --- thread entrypoint ---

    def _run(self) -> None:
        try:
            import uvicorn  # type: ignore
        except Exception as exc:
            LOG.error("bridge: uvicorn not installed (%s); install with: uv sync", exc)
            return
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        self.hub.attach_loop(loop)

        app = build_app(self)

        cfg = self._cfg_snapshot_at_start
        host = cfg.get("host", "0.0.0.0")
        port = int(cfg.get("port", 8765))
        config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off")
        server = uvicorn.Server(config)
        self._server = server
        try:
            loop.run_until_complete(server.serve())
        except Exception as exc:
            LOG.error("bridge server crashed: %s", exc)
        finally:
            with suppress(Exception):
                loop.close()


# ---- FastAPI app ---------------------------------------------------------


def build_app(service: BridgeService):
    """Construct the FastAPI app. Imported lazily so the rest of the file
    is testable without FastAPI installed."""
    from fastapi import FastAPI, HTTPException, Request
    from fastapi.responses import (
        FileResponse,
        JSONResponse,
        PlainTextResponse,
        Response,
        StreamingResponse,
    )
    from fastapi.staticfiles import StaticFiles

    app = FastAPI(title="auto-press bridge", docs_url=None, redoc_url=None)
    started_at = time.time()

    # Cache-Control headers are added per-route below (and via a
    # StaticFiles subclass for /static/*). Doing this with
    # @app.middleware("http") looks tempting but Starlette's
    # BaseHTTPMiddleware buffers the full response body before passing
    # it on, which silently breaks SSE — the /api/events stream never
    # flushes to the client.
    NO_CACHE = {"Cache-Control": "no-cache, must-revalidate"}

    @app.get("/api/health")
    async def health() -> JSONResponse:
        return JSONResponse({"ok": True, "uptime_s": int(time.time() - started_at)})

    @app.get("/api/admin/rules")
    async def admin_rules_get() -> JSONResponse:
        running = False
        if service.callbacks.is_rules_running is not None:
            try:
                running = bool(service.callbacks.is_rules_running())
            except Exception:
                running = False
        return JSONResponse({"running": running})

    @app.post("/api/admin/rules")
    async def admin_rules_set(payload: dict) -> JSONResponse:
        if service.callbacks.set_rules_running is None:
            raise HTTPException(status_code=501, detail="rules toggle not wired")
        if not isinstance(payload, dict) or not isinstance(payload.get("running"), bool):
            raise HTTPException(status_code=400, detail="running (bool) required")
        running = payload["running"]
        try:
            service.callbacks.set_rules_running(running)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse({"running": running})

    @app.get("/api/rules")
    async def rules_get() -> JSONResponse:
        """Snapshot for the phone's 'Window-specific rules' settings
        section. Returns the minimal rule projection (id, name,
        enabled, window_scope) plus the cross-workspace list of
        Cursor windows the picker can choose from, each tagged
        ``visible: bool`` (False = referenced by a scope but not
        currently detected, surfaced so stale entries can still be
        unticked)."""
        if service.callbacks.rules_snapshot is None:
            raise HTTPException(status_code=501, detail="rules snapshot not wired")
        try:
            return JSONResponse(service.callbacks.rules_snapshot())
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc

    @app.put("/api/rules/{rule_id}/enabled")
    async def rule_set_enabled(rule_id: str, payload: dict) -> JSONResponse:
        """Toggle rule.enabled from the phone. Mirrors the rule
        checkbox in the desktop UI: persists immediately and lands
        on the engine within one tick."""
        if service.callbacks.set_rule_enabled is None:
            raise HTTPException(status_code=501, detail="rule edit not wired")
        if not isinstance(payload, dict) or not isinstance(payload.get("enabled"), bool):
            raise HTTPException(status_code=400, detail="enabled (bool) required")
        try:
            found = service.callbacks.set_rule_enabled(rule_id, payload["enabled"])
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not found:
            raise HTTPException(status_code=404, detail="rule not found")
        return JSONResponse({"enabled": payload["enabled"]})

    @app.put("/api/rules/{rule_id}/scope")
    async def rule_set_scope(rule_id: str, payload: dict) -> JSONResponse:
        """Replace a rule's window_scope wholesale. Empty list = the
        rule applies to all windows (same convention as the desktop
        picker). The host side normalises entries (strip, dedupe)
        before persisting, so the phone can PUT whatever it has and
        the server resolves the canonical form."""
        if service.callbacks.set_rule_window_scope is None:
            raise HTTPException(status_code=501, detail="rule edit not wired")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="window_scope (list) required")
        scope = payload.get("window_scope")
        if not isinstance(scope, list) or any(not isinstance(s, str) for s in scope):
            raise HTTPException(status_code=400, detail="window_scope must be a list of strings")
        try:
            found = service.callbacks.set_rule_window_scope(rule_id, scope)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if not found:
            raise HTTPException(status_code=404, detail="rule not found")
        return JSONResponse({"window_scope": scope})

    @app.post("/api/admin/reload")
    async def admin_reload() -> JSONResponse:
        """Hot-reload the press_bridge module + restart the FastAPI
        listener so code edits take effect without killing the desktop
        process. Risky: if the new code is broken the bridge can come
        back up dead. The MainWindow side does an import test first so
        a syntax error keeps the old service running."""
        if service.callbacks.request_reload is None:
            raise HTTPException(status_code=501, detail="reload not wired")
        try:
            service.callbacks.request_reload()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse({"reload_scheduled": True})

    @app.get("/api/notifications/vapid-key")
    async def push_vapid_key() -> JSONResponse:
        """Public VAPID key the phone passes to PushManager.subscribe.
        Generated on first call and persisted in the bridge config so
        it stays stable across restarts — rotating it would silently
        invalidate every existing subscription."""
        if service.callbacks.ensure_vapid_keys is None:
            raise HTTPException(status_code=501, detail="push not wired")
        try:
            public_key = service.callbacks.ensure_vapid_keys()
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse({"public_key": public_key})

    @app.post("/api/notifications/subscribe")
    async def push_subscribe(payload: dict) -> JSONResponse:
        """Register the phone's PushSubscription JSON. Body matches
        the shape ``PushSubscription.toJSON()`` returns (endpoint +
        keys.{p256dh, auth}); we trim to those fields. Idempotent —
        re-subscribing with the same endpoint updates instead of
        duplicating."""
        if service.callbacks.add_push_subscription is None:
            raise HTTPException(status_code=501, detail="push not wired")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="subscription JSON required")
        # Caller may wrap as {"subscription": {...}, "label": "..."}
        # or pass the bare PushSubscription dict — accept both.
        sub_data = payload.get("subscription") if "subscription" in payload else payload
        label = payload.get("label") if isinstance(payload.get("label"), str) else None
        try:
            entry = service.callbacks.add_push_subscription(sub_data, label)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        if entry is None:
            raise HTTPException(status_code=400, detail="malformed subscription")
        return JSONResponse(
            {"id": entry["id"], "endpoint_hash": entry["endpoint"][-12:]}
        )

    @app.delete("/api/notifications/subscribe")
    async def push_unsubscribe(payload: dict) -> JSONResponse:
        """Drop a subscription. Body: ``{"endpoint": "https://..."}``
        — phone passes the endpoint it owns so it doesn't need to
        remember the subscription id we generated."""
        if service.callbacks.remove_push_subscription is None:
            raise HTTPException(status_code=501, detail="push not wired")
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="endpoint required")
        endpoint = payload.get("endpoint")
        if not isinstance(endpoint, str) or not endpoint.strip():
            raise HTTPException(status_code=400, detail="endpoint required")
        try:
            removed = service.callbacks.remove_push_subscription(endpoint.strip())
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse({"removed": int(removed)})

    @app.post("/api/notifications/test")
    async def push_test() -> JSONResponse:
        """Send a smoke-test notification to every registered phone.
        Useful for verifying the round-trip works after subscribing
        without waiting for a real busy→idle transition."""
        if service.callbacks.send_push_to_all is None:
            raise HTTPException(status_code=501, detail="push not wired")
        try:
            stats = service.callbacks.send_push_to_all(
                {
                    "title": "auto-press test",
                    "body": "Web Push is working.",
                    "tag": "test",
                }
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        return JSONResponse(stats)

    @app.post("/api/bridge/workspace/switch")
    async def workspace_switch_endpoint(payload: dict) -> JSONResponse:
        """Switch the Windows virtual desktop. Body: ``{"direction":
        "next" | "prev"}``. Fires Ctrl+Win+Right or Ctrl+Win+Left,
        waits for the desktop transition animation, then activates
        the setup bound to the new workspace (if any).

        Returns ``{"workspace_id": str, "active_setup_id": str|None}``."""
        direction = (payload or {}).get("direction")
        if direction not in ("next", "prev"):
            raise HTTPException(
                status_code=400, detail="direction must be 'next' or 'prev'"
            )
        if service.callbacks.workspace_switch is None:
            raise HTTPException(status_code=501, detail="workspace switch not wired")
        try:
            result = service.callbacks.workspace_switch(direction)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        for s in service.windows.summaries():
            service.hub.publish_typed("window_state", s)
        return JSONResponse(result or {})

    @app.get("/api/state")
    async def state() -> JSONResponse:
        cfg = service.callbacks.cfg_snapshot()
        rules_running = False
        if service.callbacks.is_rules_running is not None:
            try:
                rules_running = bool(service.callbacks.is_rules_running())
            except Exception:
                rules_running = False
        return JSONResponse(
            {
                "windows": service.windows.summaries(),
                "events": service.hub.recent(),
                "interval_seconds": cfg.get("interval_seconds", 10.0),
                "snapshots_per_window": SNAPSHOTS_PER_WINDOW,
                "rules_running": rules_running,
            }
        )

    @app.get("/api/windows")
    async def windows_list() -> JSONResponse:
        return JSONResponse(service.windows.summaries())

    @app.post("/api/windows/{window_id}/send")
    async def window_send(window_id: str, payload: dict) -> JSONResponse:
        # Allow empty / whitespace-only text. Empty means "click + Enter
        # without pasting" — used when the user already typed the message
        # in the target window from their laptop and just wants to fire
        # Enter from the phone. Strict validation: text key must exist and
        # be a string, but the string itself can be anything.
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise HTTPException(status_code=400, detail="text (string) required")
        text = payload["text"]
        cfg = service.callbacks.cfg_snapshot()
        win_cfg = next(
            (w for w in (cfg.get("bridge") or {}).get("windows", [])
             if w.get("id") == window_id),
            None,
        )
        if win_cfg is None:
            raise HTTPException(status_code=404, detail="window not found")
        if not win_cfg.get("region"):
            raise HTTPException(
                status_code=400,
                detail="window has no region configured; set one in the desktop UI",
            )
        if service.callbacks.perform_window_send is None:
            raise HTTPException(status_code=501, detail="window send not wired")

        # If the latest tick said the window is idle, send right now.
        # Otherwise queue the message and let the next idle transition
        # drain it. Either way the phone gets a clear answer.
        live = service.windows.state_of(window_id)
        is_idle = bool(live and live.get("idle"))
        bridge_cfg = cfg.get("bridge") or {}

        if is_idle:
            loop = asyncio.get_running_loop()
            try:
                await loop.run_in_executor(
                    None,
                    service.callbacks.perform_window_send,
                    win_cfg,
                    text,
                    bridge_cfg,
                )
            except Exception as exc:
                raise HTTPException(status_code=500, detail=f"send failed: {exc}") from exc
            # Refresh SSE so phones see pending count drop / state change.
            for s in service.windows.summaries():
                service.hub.publish_typed("window_state", s)
            # Kick off a delayed re-detect so the phone sees the busy
            # flip well before the next engine tick (could be ~10 s away).
            threading.Thread(
                target=_post_send_recheck,
                args=(service, window_id),
                daemon=True,
            ).start()
            return JSONResponse({"sent": True, "queued": False})

        accepted, position = service.windows.enqueue(window_id, text)
        if not accepted:
            raise HTTPException(status_code=429, detail="queue is full")
        # Push state so pending count updates on every connected phone.
        for s in service.windows.summaries():
            service.hub.publish_typed("window_state", s)
        return JSONResponse(
            {"sent": False, "queued": True, "position": position},
            status_code=202,
        )

    @app.get("/api/windows/{window_id}/queue")
    async def window_queue(window_id: str) -> JSONResponse:
        return JSONResponse({"pending": service.windows.pending(window_id)})

    @app.delete("/api/windows/{window_id}/queue")
    async def window_queue_clear(window_id: str) -> JSONResponse:
        removed = service.windows.clear_queue(window_id)
        for s in service.windows.summaries():
            service.hub.publish_typed("window_state", s)
        return JSONResponse({"cleared": removed})

    @app.put("/api/windows/{window_id}/name")
    async def window_rename(window_id: str, payload: dict) -> JSONResponse:
        """Rename a window from the phone. The new name is written to
        templates/config.json so it persists across restarts, and the
        cached display name in WindowStore is updated immediately so
        connected phones see the new label without waiting for a tick."""
        if not isinstance(payload, dict) or not isinstance(payload.get("name"), str):
            raise HTTPException(status_code=400, detail="name (string) required")
        new_name = payload["name"].strip()
        if not new_name:
            raise HTTPException(status_code=400, detail="name cannot be empty")
        if service.callbacks.rename_window is None:
            raise HTTPException(status_code=501, detail="rename not wired")
        if not service.callbacks.rename_window(window_id, new_name):
            raise HTTPException(status_code=404, detail="window not found")
        # Reflect in the live store + fan SSE so phones update without
        # waiting for the next tick to come around.
        service.windows.update_window_name(window_id, new_name)
        for s in service.windows.summaries():
            service.hub.publish_typed("window_state", s)
        return JSONResponse({"renamed": True, "name": new_name})

    @app.post("/api/windows/{window_id}/scroll")
    async def window_scroll(window_id: str, payload: dict) -> JSONResponse:
        """Scroll the window's chat panel by ``amount`` arrow-key
        presses. Positive = scroll up (older messages into view),
        negative = scroll down (newer messages). Captures a fresh
        snapshot a moment later so the phone shows the new visible
        region without waiting for the next engine tick."""
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="amount (int) required")
        try:
            amount = int(payload.get("amount", 0))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="amount must be an int")
        if amount == 0:
            raise HTTPException(status_code=400, detail="amount must be non-zero")
        cfg = service.callbacks.cfg_snapshot()
        win_cfg = next(
            (w for w in (cfg.get("bridge") or {}).get("windows", [])
             if w.get("id") == window_id),
            None,
        )
        if win_cfg is None:
            raise HTTPException(status_code=404, detail="window not found")
        if not win_cfg.get("region"):
            raise HTTPException(status_code=400, detail="window has no region")
        if service.callbacks.perform_window_scroll is None:
            raise HTTPException(status_code=501, detail="window scroll not wired")
        bridge_cfg = cfg.get("bridge") or {}
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                service.callbacks.perform_window_scroll,
                win_cfg,
                amount,
                bridge_cfg,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"scroll failed: {exc}") from exc
        # Background recheck: wait briefly so Cursor finishes the scroll
        # animation, then capture a fresh snapshot for this window.
        threading.Thread(
            target=_post_scroll_recheck,
            args=(service, window_id),
            daemon=True,
        ).start()
        return JSONResponse({"scrolled": amount})

    @app.post("/api/windows/{window_id}/click_at")
    async def window_click_at(window_id: str, payload: dict) -> JSONResponse:
        """Click an arbitrary point in the window expressed as
        fractional coords of the window's region. Body:
        ``{"x_frac": 0.45, "y_frac": 0.78}``.

        The phone uses this to fire a tap on a multi-choice option
        (or anywhere else): the user taps on the latest snapshot,
        the phone computes x_frac / y_frac against the displayed
        image, and the bridge translates back to physical screen
        coords via ``region.x + x_frac * region.w`` and the analogous
        Y. DPI doesn't enter because the snapshot itself was captured
        at the same physical pixels we're clicking into.

        Triggers a delayed re-detect so the phone sees the resulting
        busy / state flip without waiting for the next engine tick."""
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="x_frac, y_frac required")
        try:
            x_frac = float(payload.get("x_frac"))
            y_frac = float(payload.get("y_frac"))
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400, detail="x_frac, y_frac must be numbers in [0, 1]"
            )
        if not (0.0 <= x_frac <= 1.0 and 0.0 <= y_frac <= 1.0):
            raise HTTPException(status_code=400, detail="x_frac, y_frac out of range")
        cfg = service.callbacks.cfg_snapshot()
        win_cfg = next(
            (
                w
                for w in (cfg.get("bridge") or {}).get("windows", [])
                if w.get("id") == window_id
            ),
            None,
        )
        if win_cfg is None:
            raise HTTPException(status_code=404, detail="window not found")
        if not win_cfg.get("region"):
            raise HTTPException(status_code=400, detail="window has no region")
        if service.callbacks.perform_window_click_at is None:
            raise HTTPException(status_code=501, detail="click_at not wired")
        bridge_cfg = cfg.get("bridge") or {}
        loop = asyncio.get_running_loop()
        try:
            target = await loop.run_in_executor(
                None,
                service.callbacks.perform_window_click_at,
                win_cfg,
                x_frac,
                y_frac,
                bridge_cfg,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"click failed: {exc}") from exc
        # Kick off a delayed snapshot so the phone sees the post-click
        # state (multi-choice card collapses, agent starts processing)
        # without waiting for the next engine tick.
        threading.Thread(
            target=_post_send_recheck,
            args=(service, window_id),
            daemon=True,
        ).start()
        return JSONResponse({"clicked": True, "target": list(target) if target else None})

    @app.post("/api/windows/{window_id}/snapshot")
    async def window_snapshot(window_id: str) -> JSONResponse:
        """Capture a fresh snapshot of the window region, right now, no
        scroll and no clicks. Used when the user has rearranged windows
        on the desktop and the stored snapshot has gone stale.

        Same path as the post-scroll recheck — just with zero settle
        delay. The dedup hash still applies, so if nothing actually
        changed on screen, the freshness pill resets but no duplicate
        tile gets stored.
        """
        cfg = service.callbacks.cfg_snapshot()
        win_cfg = next(
            (w for w in (cfg.get("bridge") or {}).get("windows", [])
             if w.get("id") == window_id),
            None,
        )
        if win_cfg is None:
            raise HTTPException(status_code=404, detail="window not found")
        if not win_cfg.get("region"):
            raise HTTPException(status_code=400, detail="window has no region")
        threading.Thread(
            target=_post_scroll_recheck,
            args=(service, window_id),
            kwargs={"delay_s": 0.0},
            daemon=True,
        ).start()
        return JSONResponse({"captured": True})

    @app.put("/api/windows/{window_id}/queue/{idx}")
    async def queue_update_one(window_id: str, idx: int, payload: dict) -> JSONResponse:
        """Edit a queued message in place — same accept-anything-string
        rule as POST /send (empty / whitespace are valid)."""
        if not isinstance(payload, dict) or not isinstance(payload.get("text"), str):
            raise HTTPException(status_code=400, detail="text (string) required")
        text = payload["text"]
        if not service.windows.update_at(window_id, idx, text):
            raise HTTPException(status_code=404, detail="queue index out of range")
        for s in service.windows.summaries():
            service.hub.publish_typed("window_state", s)
        return JSONResponse({"updated": True, "text": text})

    @app.delete("/api/windows/{window_id}/queue/{idx}")
    async def queue_delete_one(window_id: str, idx: int) -> JSONResponse:
        """Drop a specific queued message (by 0-based index) without
        firing it. Useful when the user changes their mind about one
        message but wants to keep the rest of the queue."""
        text = service.windows.pop_at(window_id, idx)
        if text is None:
            raise HTTPException(status_code=404, detail="queue index out of range")
        for s in service.windows.summaries():
            service.hub.publish_typed("window_state", s)
        return JSONResponse({"deleted": True, "text": text})

    @app.post("/api/windows/{window_id}/queue/{idx}/send_now")
    async def queue_send_now(window_id: str, idx: int) -> JSONResponse:
        """Pop one queued message and send it immediately, regardless of
        whether the window is currently idle. Useful when the user has
        composed a follow-up while the agent's still busy and decides
        they want to interrupt anyway."""
        cfg = service.callbacks.cfg_snapshot()
        win_cfg = next(
            (w for w in (cfg.get("bridge") or {}).get("windows", [])
             if w.get("id") == window_id),
            None,
        )
        if win_cfg is None:
            raise HTTPException(status_code=404, detail="window not found")
        if service.callbacks.perform_window_send is None:
            raise HTTPException(status_code=501, detail="window send not wired")
        text = service.windows.pop_at(window_id, idx)
        if text is None:
            raise HTTPException(status_code=404, detail="queue index out of range")
        bridge_cfg = cfg.get("bridge") or {}
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                service.callbacks.perform_window_send,
                win_cfg,
                text,
                bridge_cfg,
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"send failed: {exc}") from exc
        for s in service.windows.summaries():
            service.hub.publish_typed("window_state", s)
        return JSONResponse({"sent": True, "text": text})

    @app.get("/api/windows/{window_id}/snapshot/{idx}")
    async def window_snapshot(window_id: str, idx: int) -> Response:
        snap = service.windows.snapshot(window_id, idx)
        if snap is None:
            raise HTTPException(status_code=404, detail="snapshot not available")
        ts, png = snap
        return Response(
            content=png,
            media_type="image/png",
            headers={
                "X-Snapshot-Timestamp": ts,
                # Each snapshot at a given (window, idx) shifts every tick,
                # so the phone shouldn't cache them — bypass.
                "Cache-Control": "no-store",
            },
        )

    @app.get("/api/events")
    async def events(request: Request) -> StreamingResponse:
        queue = service.hub.subscribe()

        async def stream():
            # Replay buffered rule events first so a freshly-connected
            # phone doesn't have to /api/state to backfill rule history.
            for event in service.hub.recent():
                yield f"event: rule_matched\ndata: {json.dumps(event)}\n\n"
            # And current window summaries, as window_state events.
            for w in service.windows.summaries():
                yield f"event: window_state\ndata: {json.dumps(w)}\n\n"
            try:
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        payload = await asyncio.wait_for(
                            queue.get(), timeout=SSE_KEEPALIVE_SECS
                        )
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
                        continue
                    if isinstance(payload, tuple):
                        ev_type, ev_data = payload
                    else:  # legacy: untagged events default to rule_matched
                        ev_type, ev_data = "rule_matched", payload
                    yield f"event: {ev_type}\ndata: {json.dumps(ev_data)}\n\n"
            finally:
                service.hub.unsubscribe(queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/send")
    async def send(payload: dict) -> JSONResponse:
        rule_id = payload.get("rule_id")
        text = payload.get("text")
        match_index = payload.get("match_index")
        if not isinstance(rule_id, str) or not rule_id:
            raise HTTPException(status_code=400, detail="rule_id required")
        if not isinstance(text, str) or not text:
            raise HTTPException(status_code=400, detail="text required")

        cfg = service.callbacks.cfg_snapshot()
        rule = next((r for r in cfg.get("rules", []) if r.get("id") == rule_id), None)
        if rule is None:
            raise HTTPException(status_code=404, detail="rule not found")

        # Re-run matching now. Cached coords lie when windows have moved.
        loop = asyncio.get_running_loop()
        try:
            matches = await loop.run_in_executor(None, service.callbacks.re_match_rule, rule_id)
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"match failed: {exc}") from exc
        if not matches:
            raise HTTPException(status_code=404, detail="no matches for rule right now")

        if match_index is None:
            if len(matches) > 1:
                return JSONResponse(
                    status_code=409,
                    content={
                        "count": len(matches),
                        "matches": [
                            {"index": i, "score": float(s), "center": [int(c[0]), int(c[1])]}
                            for i, (s, c) in enumerate(matches)
                        ],
                    },
                )
            chosen = matches[0]
        else:
            try:
                idx = int(match_index)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="match_index must be int")
            if not 0 <= idx < len(matches):
                raise HTTPException(status_code=400, detail="match_index out of range")
            chosen = matches[idx]

        score, center = chosen
        offset = rule.get("bridge_paste_offset") or [0, 0]
        try:
            paste_point = (int(center[0]) + int(offset[0]), int(center[1]) + int(offset[1]))
        except (TypeError, ValueError):
            paste_point = (int(center[0]), int(center[1]))

        bridge_cfg = cfg.get("bridge") or {}
        t0 = time.monotonic()
        try:
            await loop.run_in_executor(
                None, service.callbacks.perform_send, paste_point, text, bridge_cfg
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"send failed: {exc}") from exc
        dur_ms = int((time.monotonic() - t0) * 1000)
        return JSONResponse(
            {
                "success": True,
                "matched_at": [int(paste_point[0]), int(paste_point[1])],
                "score": float(score),
                "duration_ms": dur_ms,
            }
        )

    @app.post("/api/dismiss/{event_id}")
    async def dismiss(event_id: str) -> JSONResponse:
        ok = service.hub.dismiss(event_id)
        if not ok:
            raise HTTPException(status_code=404, detail="event not found")
        return JSONResponse({"success": True})

    @app.post("/api/refresh_targets")
    async def refresh_targets() -> JSONResponse:
        # Same body as /api/state; phone uses this as a "force re-scan" pull.
        cfg = service.callbacks.cfg_snapshot()
        return JSONResponse(
            {
                "rules": [
                    {
                        "id": r.get("id"),
                        "name": r.get("name"),
                        "friendly_name": r.get("bridge_friendly_name") or r.get("name"),
                    }
                    for r in cfg.get("rules", [])
                ]
            }
        )

    @app.post("/api/read")
    async def read(payload: dict) -> JSONResponse:
        rule_id = payload.get("rule_id")
        if not isinstance(rule_id, str) or not rule_id:
            raise HTTPException(status_code=400, detail="rule_id required")
        if service.callbacks.perform_read is None:
            raise HTTPException(status_code=501, detail="read not implemented")
        cfg = service.callbacks.cfg_snapshot()
        rule = next((r for r in cfg.get("rules", []) if r.get("id") == rule_id), None)
        if rule is None:
            raise HTTPException(status_code=404, detail="rule not found")
        loop = asyncio.get_running_loop()
        try:
            text = await loop.run_in_executor(
                None, service.callbacks.perform_read, rule_id, cfg.get("bridge") or {}
            )
        except Exception as exc:
            raise HTTPException(status_code=500, detail=f"read failed: {exc}") from exc
        return JSONResponse({"text": text or ""})

    # --- phone UI ---

    @app.get("/")
    async def root() -> Response:
        index = PHONE_DIR / "index.html"
        if not index.exists():
            return PlainTextResponse("bridge_phone/ missing", status_code=500)
        return FileResponse(index, headers=NO_CACHE)

    static_dir = PHONE_DIR / "static"
    if static_dir.exists():
        class _NoCacheStatic(StaticFiles):
            """StaticFiles with Cache-Control on every served file. Doing
            this here (instead of in @app.middleware) means we don't go
            through BaseHTTPMiddleware and SSE keeps streaming."""

            async def get_response(self, path, scope):
                response = await super().get_response(path, scope)
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
                return response

        app.mount("/static", _NoCacheStatic(directory=str(static_dir)), name="static")

    @app.get("/sw.js")
    async def service_worker() -> Response:
        """Serve the service worker from the root so its scope covers
        the whole PWA. Browsers cap a SW's scope to the directory of
        the file that served it, so this can't live under /static/."""
        sw = PHONE_DIR / "sw.js"
        if not sw.exists():
            return PlainTextResponse("sw missing", status_code=404)
        return FileResponse(
            sw,
            media_type="application/javascript",
            headers={
                # SWs are aggressively cached by browsers. no-cache
                # forces revalidation each load so a deploy lands
                # without users having to manually unregister.
                "Cache-Control": "no-cache, must-revalidate",
                "Service-Worker-Allowed": "/",
            },
        )

    @app.get("/manifest.webmanifest")
    async def manifest() -> Response:
        path = static_dir / "manifest.webmanifest"
        if not path.exists():
            return PlainTextResponse("manifest missing", status_code=404)
        return FileResponse(
            path, media_type="application/manifest+json", headers=NO_CACHE,
        )

    return app
