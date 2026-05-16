"""Bridge config + idle-detection tests.

Scope of this slice: schema migration and the per-window idle detector.
The HTTP/SSE/send pipeline is being reshaped around the new "windows"
contract, so its tests will land with that next commit.
"""

from __future__ import annotations

import numpy as np
import pytest

import press_engine
import press_store


# ---- config schema migration -------------------------------------------


def test_old_config_without_bridge_block_loads_with_defaults():
    cfg = press_store.normalize_config({"rules": [{"name": "r", "matcher": "template"}]})
    assert "bridge" in cfg
    assert "enabled" not in cfg["bridge"]  # gated by --bridge flag, not config
    assert cfg["bridge"]["port"] == 8765
    assert cfg["bridge"]["windows"] == []
    assert cfg["bridge"]["idle_template_path"] is None
    assert cfg["bridge"]["idle_threshold"] == 0.90


def test_legacy_per_rule_bridge_fields_are_dropped():
    """Old rules carrying bridge_paste_offset / bridge_friendly_name etc.
    load cleanly — those fields are now dead and silently stripped."""
    cfg = press_store.normalize_config(
        {
            "rules": [
                {
                    "name": "r",
                    "bridge_paste_offset": [10, 20],
                    "bridge_friendly_name": "Cursor #1",
                    "bridge_read_strategy": "ocr",
                    "bridge_read_region": [0, 0, 100, 100],
                }
            ]
        }
    )
    rule = cfg["rules"][0]
    for key in (
        "bridge_paste_offset",
        "bridge_friendly_name",
        "bridge_read_strategy",
        "bridge_read_region",
    ):
        assert key not in rule


def test_window_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(press_store, "TEMPLATES_DIR", tmp_path)
    monkeypatch.setattr(press_store, "CONFIG_PATH", tmp_path / "config.json")

    cfg = press_store.default_config()
    cfg["bridge"]["idle_template_path"] = "idle.png"
    cfg["bridge"]["idle_threshold"] = 0.85
    win = press_store.default_bridge_window("Cursor #1")
    win["region"] = [100, 200, 800, 600]
    win["chat_target"] = [500, 750]
    win["read_region"] = [110, 210, 780, 400]
    cfg["bridge"]["windows"].append(win)

    press_store.save_config(cfg)
    loaded = press_store.load_config()

    assert loaded["bridge"]["idle_template_path"] == "idle.png"
    assert loaded["bridge"]["idle_threshold"] == 0.85
    [w] = loaded["bridge"]["windows"]
    assert w["name"] == "Cursor #1"
    assert w["region"] == [100, 200, 800, 600]
    assert w["chat_target"] == [500, 750]
    assert w["read_region"] == [110, 210, 780, 400]


def test_window_with_negative_origin_survives_round_trip():
    """Multi-monitor Windows: monitors placed left of/above the primary
    live at negative virtual-screen coordinates. Captures from those
    monitors must persist; an earlier validator rejected them outright
    and silently wiped the region on save."""
    cfg = press_store.normalize_config(
        {"bridge": {"windows": [{"name": "Left mon", "region": [-1920, 0, 800, 600]}]}}
    )
    assert cfg["bridge"]["windows"][0]["region"] == [-1920, 0, 800, 600]


def test_invalid_window_fields_normalize_to_none():
    cfg = press_store.normalize_config(
        {
            "bridge": {
                "windows": [
                    {"name": "X", "region": [10, 20, -5, 5], "chat_target": "nope"},
                    {"name": "", "region": "not a list"},
                ]
            }
        }
    )
    [w1, w2] = cfg["bridge"]["windows"]
    assert w1["region"] is None
    assert w1["chat_target"] is None
    assert w2["name"] == "Cursor"  # default fallback
    assert w2["region"] is None


# ---- idle detector ------------------------------------------------------


@pytest.fixture
def idle_template(tmp_path, monkeypatch):
    """Drop a recognisable 24×24 cross pattern as templates/idle.png.

    A uniform-grey template would correlate equally with any flat region
    (TM_CCOEFF_NORMED is undefined when both signals have zero variance),
    so we use a cross so detection is unambiguous.
    """
    monkeypatch.setattr(press_store, "TEMPLATES_DIR", tmp_path)
    cv2 = pytest.importorskip("cv2")
    template = np.zeros((24, 24), dtype=np.uint8)
    template[11:13, :] = 255   # horizontal bar
    template[:, 11:13] = 255   # vertical bar
    cv2.imwrite(str(tmp_path / "idle.png"), template)
    return "idle.png"


def test_evaluate_bridge_windows_returns_empty_when_unconfigured():
    assert press_engine.evaluate_bridge_windows({}) == []
    assert press_engine.evaluate_bridge_windows({"windows": [{"name": "X"}]}) == []


def test_evaluate_bridge_windows_skips_windows_without_region(idle_template, monkeypatch):
    bridge_cfg = {
        "idle_template_path": idle_template,
        "idle_threshold": 0.9,
        "windows": [{"id": "w1", "name": "Cursor #1", "region": None}],
    }
    states = press_engine.evaluate_bridge_windows(bridge_cfg)
    assert states == [
        {
            "id": "w1",
            "name": "Cursor #1",
            "idle": False,
            "asking": False,
            "score": 0.0,
            "configured": False,
        }
    ]


def _rgb_frame_with_cross_at(width: int, height: int, x: int, y: int):
    """Mid-grey RGB background with a white cross at (x, y). Detector
    converts RGB → gray internally, so the cross still matches the
    grayscale template."""
    frame = np.full((height, width, 3), 80, dtype=np.uint8)
    frame[y + 11:y + 13, x:x + 24, :] = 255
    frame[y:y + 24, x + 11:x + 13, :] = 255
    return frame


def test_evaluate_bridge_windows_detects_idle(idle_template, monkeypatch):
    """A frame containing the idle template anywhere in the region → idle."""
    frame = _rgb_frame_with_cross_at(200, 200, 50, 60)

    monkeypatch.setattr(press_engine, "capture_screen_rgb", lambda region: frame)

    bridge_cfg = {
        "idle_template_path": idle_template,
        "idle_threshold": 0.9,
        "windows": [{"id": "w1", "name": "Cursor #1", "region": [0, 0, 200, 200]}],
    }
    [state] = press_engine.evaluate_bridge_windows(bridge_cfg)
    assert state["idle"] is True
    assert state["score"] >= 0.9
    assert state["configured"] is True


def test_evaluate_bridge_windows_returns_rgb_when_requested(idle_template, monkeypatch):
    """capture_rgb=True attaches the captured ndarray for the snapshot path."""
    frame = _rgb_frame_with_cross_at(200, 200, 50, 60)
    monkeypatch.setattr(press_engine, "capture_screen_rgb", lambda region: frame)
    [state] = press_engine.evaluate_bridge_windows(
        {
            "idle_template_path": idle_template,
            "idle_threshold": 0.9,
            "windows": [{"id": "w1", "name": "Cursor #1", "region": [0, 0, 200, 200]}],
        },
        capture_rgb=True,
    )
    assert state["rgb"].shape == (200, 200, 3)


def test_evaluate_bridge_windows_busy_when_template_absent(idle_template, monkeypatch):
    # Diagonal stripe — non-uniform, but contains no cross.
    frame = np.zeros((200, 200, 3), dtype=np.uint8)
    for i in range(200):
        frame[i, i, :] = 255

    monkeypatch.setattr(press_engine, "capture_screen_rgb", lambda region: frame)

    bridge_cfg = {
        "idle_template_path": idle_template,
        "idle_threshold": 0.9,
        "windows": [{"id": "w1", "name": "Cursor #1", "region": [0, 0, 200, 200]}],
    }
    [state] = press_engine.evaluate_bridge_windows(bridge_cfg)
    assert state["idle"] is False
    assert state["configured"] is True


# ---- window store -------------------------------------------------------


def test_window_store_ring_buffer_caps_at_max():
    from press_bridge import WindowStore

    store = WindowStore(snapshots_per_window=3)
    states = [{"id": "w1", "name": "X", "idle": False, "score": 0.0, "configured": True}]
    for i in range(5):
        store.update(states, {"w1": f"png-{i}".encode()})
    [summary] = store.summaries()
    assert summary["snapshot_count"] == 3
    # Newest first.
    assert store.snapshot("w1", 0)[1] == b"png-4"
    assert store.snapshot("w1", 2)[1] == b"png-2"
    assert store.snapshot("w1", 3) is None  # past the buffer


def test_window_store_touch_last_snapshot_bumps_timestamp_only():
    """touch_last_snapshot updates the most recent snapshot's timestamp
    without replacing the PNG or hash, so a dedup-skipped scroll still
    fires a window_state event with a fresh snapshot_at."""
    from press_bridge import WindowStore

    store = WindowStore()
    base = {"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}
    store.update([base], {"w1": b"png-1"})
    store.set_snapshot("w1", b"png-2", "hash-2")
    [summary_before] = store.summaries()
    ts_before = summary_before["snapshot_at"]
    # Sleep a moment so the new ISO timestamp differs.
    import time
    time.sleep(0.01)
    assert store.touch_last_snapshot("w1") is True
    [summary_after] = store.summaries()
    assert summary_after["snapshot_at"] != ts_before
    # PNG and hash unchanged.
    assert store.snapshot("w1", 0)[1] == b"png-2"
    assert store.last_snapshot_hash("w1") == "hash-2"


def test_window_store_touch_last_snapshot_returns_false_when_empty():
    from press_bridge import WindowStore

    store = WindowStore()
    assert store.touch_last_snapshot("w1") is False
    base = {"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}
    store.update([base], {})  # state but no snapshot
    assert store.touch_last_snapshot("w1") is False


def test_window_store_last_snapshot_hash_round_trip():
    """set_snapshot stashes the hash; last_snapshot_hash returns it.
    Worker-tick captures pass no hash and last_snapshot_hash is None,
    so scroll-vs-scroll dedup doesn't accidentally suppress fresh
    busy→idle captures."""
    from press_bridge import WindowStore

    store = WindowStore(snapshots_per_window=3)
    base = {"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}
    # No snapshots yet → None.
    assert store.last_snapshot_hash("w1") is None
    # Worker-tick capture (no hash).
    store.update([base], {"w1": b"png-1"})
    assert store.last_snapshot_hash("w1") is None
    # Scroll capture with hash.
    store.set_snapshot("w1", b"png-2", "abc123")
    assert store.last_snapshot_hash("w1") == "abc123"
    # Another scroll with a different hash overwrites.
    store.set_snapshot("w1", b"png-3", "def456")
    assert store.last_snapshot_hash("w1") == "def456"


def test_window_store_clears_snapshots_on_busy_to_idle():
    """Snapshot lifecycle: scroll captures accumulate during idle, stay
    through the next busy spell, then a busy→idle transition wipes the
    deque and writes only the fresh capture. So the user always sees a
    coherent set of frames from the *current* idle session."""
    from press_bridge import WindowStore

    store = WindowStore(snapshots_per_window=10)
    base = {"id": "w1", "name": "X", "configured": True, "score": 0.5}
    # Initial busy observation — no image, no snapshot.
    store.update([{**base, "idle": False}], {})
    assert store.summaries()[0]["snapshot_count"] == 0
    # First idle observation. prev was False, so update treats this as a
    # busy→idle transition; clears (already empty) and stores the fresh
    # idle capture.
    store.update([{**base, "idle": True}], {"w1": b"png-idle-1"})
    assert store.summaries()[0]["snapshot_count"] == 1
    # User scrolls twice while idle — set_snapshot adds without wiping.
    assert store.set_snapshot("w1", b"png-scroll-1") is True
    assert store.set_snapshot("w1", b"png-scroll-2") is True
    assert store.summaries()[0]["snapshot_count"] == 3
    # Window goes busy. No image this tick → snapshots stay so the user
    # can keep reviewing them while the agent works.
    store.update([{**base, "idle": False}], {})
    assert store.summaries()[0]["snapshot_count"] == 3
    # busy → idle again. Wipe + new idle capture.
    store.update([{**base, "idle": True}], {"w1": b"png-idle-2"})
    summary = store.summaries()[0]
    assert summary["snapshot_count"] == 1
    assert store.snapshot("w1", 0)[1] == b"png-idle-2"


def test_window_store_propagates_asking_state():
    """The detector's 'asking' flag round-trips through the store and
    lands in summaries() so the phone can render the yellow indicator."""
    from press_bridge import WindowStore

    store = WindowStore(snapshots_per_window=4)
    base = {"id": "w1", "name": "X", "configured": True, "score": 0.9}
    # First observation: busy. No image; defaults populated.
    store.update([{**base, "idle": False, "asking": False}], {})
    [s] = store.summaries()
    assert s["idle"] is False
    assert s["asking"] is False
    # Transition straight into asking — same shape as busy→idle.
    store.update([{**base, "idle": False, "asking": True}], {"w1": b"png-ask"})
    [s] = store.summaries()
    assert s["idle"] is False
    assert s["asking"] is True
    assert s["snapshot_count"] == 1


def test_window_store_clears_snapshots_on_busy_to_asking():
    """Same lifecycle as busy→idle: entering 'asking' from busy should
    wipe the deque so the user sees a fresh capture of the question
    card, not the agent's previous chat scroll."""
    from press_bridge import WindowStore

    store = WindowStore(snapshots_per_window=10)
    base = {"id": "w1", "name": "X", "configured": True, "score": 0.5}
    # Go idle, accumulate a few snapshots.
    store.update([{**base, "idle": True, "asking": False}], {"w1": b"png-idle"})
    assert store.set_snapshot("w1", b"png-scroll") is True
    assert store.summaries()[0]["snapshot_count"] == 2
    # Drop back to busy.
    store.update([{**base, "idle": False, "asking": False}], {})
    assert store.summaries()[0]["snapshot_count"] == 2
    # busy → asking should wipe and write the fresh capture only.
    store.update(
        [{**base, "idle": False, "asking": True}],
        {"w1": b"png-question"},
    )
    summary = store.summaries()[0]
    assert summary["asking"] is True
    assert summary["snapshot_count"] == 1
    assert store.snapshot("w1", 0)[1] == b"png-question"


def test_window_store_summary_carries_latest_snapshot_timestamp():
    """Summaries include snapshot_at so the phone can show
    "captured Xs ago" without having to inspect the PNG headers."""
    from press_bridge import WindowStore

    store = WindowStore(snapshots_per_window=1)
    base = {"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}
    # First update with an image — snapshot_at populated.
    store.update([base], {"w1": b"png-1"})
    [s1] = store.summaries()
    assert s1["snapshot_at"] is not None
    first_ts = s1["snapshot_at"]
    # Update without an image — timestamp stays the same.
    store.update([base], {})
    [s2] = store.summaries()
    assert s2["snapshot_at"] == first_ts
    assert s2["snapshot_count"] == 1


def test_window_store_no_snapshot_yet_returns_none():
    from press_bridge import WindowStore

    store = WindowStore()
    store.update(
        [{"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}], {}
    )
    [summary] = store.summaries()
    assert summary["snapshot_at"] is None
    assert summary["snapshot_count"] == 0


def test_window_store_reports_only_idle_transitions():
    from press_bridge import WindowStore

    store = WindowStore()
    base = {"id": "w1", "name": "X", "configured": True}
    # First observation: no transition (we don't know the prior state).
    assert store.update([dict(base, idle=False, score=0.1)], {}) == []
    # Same state: still no transition.
    assert store.update([dict(base, idle=False, score=0.1)], {}) == []
    # Flip → reports.
    [tr] = store.update([dict(base, idle=True, score=0.95)], {})
    assert tr["idle"] is True


def test_window_store_drops_removed_windows():
    from press_bridge import WindowStore

    store = WindowStore()
    s1 = {"id": "w1", "name": "X", "idle": False, "score": 0.0, "configured": True}
    s2 = {"id": "w2", "name": "Y", "idle": False, "score": 0.0, "configured": True}
    store.update([s1, s2], {})
    assert len(store.summaries()) == 2
    store.update([s1], {})  # w2 removed from cfg
    [only] = store.summaries()
    assert only["id"] == "w1"


def test_window_store_prune_false_keeps_other_windows():
    """Partial updates (e.g. _post_send_recheck on one window) call
    update with prune=False so the other windows already in the store
    aren't dropped just because they aren't in this single-window
    states list."""
    from press_bridge import WindowStore

    store = WindowStore()
    s1 = {"id": "w1", "name": "X", "idle": False, "score": 0.0, "configured": True}
    s2 = {"id": "w2", "name": "Y", "idle": False, "score": 0.0, "configured": True}
    store.update([s1, s2], {})
    assert len(store.summaries()) == 2
    # Partial update: only w1's new state, prune=False.
    s1_busy = {"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}
    store.update([s1_busy], {}, prune=False)
    summaries = {s["id"]: s for s in store.summaries()}
    assert set(summaries) == {"w1", "w2"}
    assert summaries["w1"]["idle"] is True
    # Default prune=True still removes missing.
    store.update([s1_busy], {})
    [only] = store.summaries()
    assert only["id"] == "w1"


def test_window_store_pop_at_removes_specific_message():
    from press_bridge import WindowStore

    store = WindowStore()
    store.enqueue("w1", "first")
    store.enqueue("w1", "second")
    store.enqueue("w1", "third")
    assert store.pop_at("w1", 1) == "second"
    assert store.pending("w1") == ["first", "third"]
    # Out-of-range returns None and leaves the queue alone.
    assert store.pop_at("w1", 5) is None
    assert store.pending("w1") == ["first", "third"]
    # Popping the last item clears the per-window deque entry.
    store.pop_at("w1", 0)
    store.pop_at("w1", 0)
    assert store.pending("w1") == []


def test_window_store_queue_enqueue_dequeue():
    from press_bridge import WindowStore

    store = WindowStore()
    accepted, pos = store.enqueue("w1", "hello")
    assert accepted is True
    assert pos == 1
    accepted, pos = store.enqueue("w1", "world")
    assert pos == 2
    # Empty / whitespace strings are allowed: the send pipeline interprets
    # them as "click + Enter, no paste". Only None is rejected.
    accepted, pos = store.enqueue("w1", "")
    assert accepted is True
    assert pos == 3
    accepted, _ = store.enqueue("w1", None)
    assert accepted is False
    assert store.pending("w1") == ["hello", "world", ""]
    assert store.dequeue("w1") == "hello"
    assert store.pending("w1") == ["world", ""]
    assert store.dequeue("w1") == "world"
    assert store.dequeue("w1") == ""
    assert store.dequeue("w1") is None  # empty queue
    assert store.pending("w1") == []


def test_window_store_summary_includes_pending():
    from press_bridge import WindowStore

    store = WindowStore()
    store.update([{"id": "w1", "name": "X", "idle": False, "score": 0.0, "configured": True}], {})
    store.enqueue("w1", "ship it")
    [summary] = store.summaries()
    assert summary["pending"] == ["ship it"]


@pytest.fixture
def fastapi_client():
    """Boots a real BridgeService + FastAPI app behind TestClient. Tests
    that need a custom config mutate ``calls['cfg']`` directly; the
    cfg_snapshot callback returns a shallow copy so mutations don't bleed
    between requests but can still be set up with a single dict."""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient
    from press_bridge import BridgeCallbacks, BridgeService, build_app

    calls: dict = {
        "send": [],
        "match": [],
        "window_send": [],
        "window_scroll": [],
        "rules_running": False,
        "rules_set": [],
        "renames": [],
        "cfg": {
            "interval_seconds": 10.0,
            "bridge": {"pre_paste_delay_ms": 5, "clipboard_restore_delay_ms": 5},
            "rules": [],
        },
    }

    def cfg_snapshot():
        snap = dict(calls["cfg"])
        snap["rules"] = [dict(r) for r in calls["cfg"].get("rules", [])]
        snap["bridge"] = dict(calls["cfg"].get("bridge", {}))
        return snap

    def re_match_rule(rule_id):
        calls["match"].append(rule_id)
        return calls.get("match_result", [(0.99, (100, 200))])

    def perform_send(point, text, bridge_cfg):
        calls["send"].append((point, text, dict(bridge_cfg)))

    def perform_window_send(window, text, bridge_cfg):
        calls["window_send"].append((dict(window), text))

    def perform_window_scroll(window, amount, bridge_cfg):
        calls["window_scroll"].append((dict(window), int(amount)))

    def perform_window_click_at(window, x_frac, y_frac, bridge_cfg):
        region = window["region"]
        target_x = int(region[0] + x_frac * region[2])
        target_y = int(region[1] + y_frac * region[3])
        calls.setdefault("window_clicks", []).append(
            (dict(window), float(x_frac), float(y_frac), target_x, target_y)
        )
        return (target_x, target_y)

    def is_rules_running():
        return bool(calls["rules_running"])

    def set_rules_running(running):
        calls["rules_running"] = bool(running)
        calls["rules_set"].append(bool(running))

    def rename_window(window_id, new_name):
        calls["renames"].append((window_id, new_name))
        for w in calls["cfg"].get("bridge", {}).get("windows", []):
            if w.get("id") == window_id:
                w["name"] = new_name
                return True
        return False

    def auto_detect_windows(mode):
        calls["auto_detect"].append(mode)
        # If a test pre-seeds calls["auto_detect_result"], use it;
        # otherwise the default behaviour is "found 0 windows".
        return int(calls.get("auto_detect_result", -1))

    calls["auto_detect"] = []

    def new_bridge_setup(name):
        # Real helper does the heavy lifting; we wire it through the
        # cfg dict so endpoint tests can verify the bridge actually
        # mutates state, not just calls back.
        from press_store import new_setup

        sid = new_setup(calls["cfg"], name)
        calls.setdefault("setup_news", []).append((sid, name))
        return sid

    def activate_bridge_setup(setup_id):
        from press_store import activate_setup

        ok = activate_setup(calls["cfg"], setup_id)
        calls.setdefault("setup_activates", []).append((setup_id, ok))
        return ok

    def delete_bridge_setup(setup_id):
        from press_store import delete_setup

        ok = delete_setup(calls["cfg"], setup_id)
        calls.setdefault("setup_deletes", []).append((setup_id, ok))
        return ok

    def ensure_vapid_keys():
        # Stable fake key — tests only check that the endpoint
        # returns *some* string, not that it's a real ECDH point.
        calls.setdefault("vapid_called", 0)
        calls["vapid_called"] += 1
        return "BMockPublicKey"

    def add_push_subscription(raw, label):
        from press_push import normalize_subscription
        clean = normalize_subscription(raw)
        if clean is None:
            return None
        entry = {
            "id": f"sub{len(calls.setdefault('push_subs', []))}",
            "endpoint": clean["endpoint"],
            "keys": clean["keys"],
            "label": label,
        }
        # Idempotent: replace existing entry for same endpoint.
        calls["push_subs"] = [
            s for s in calls["push_subs"] if s["endpoint"] != clean["endpoint"]
        ] + [entry]
        return entry

    def remove_push_subscription(endpoint):
        subs = calls.setdefault("push_subs", [])
        before = len(subs)
        calls["push_subs"] = [s for s in subs if s["endpoint"] != endpoint]
        return before - len(calls["push_subs"])

    def send_push_to_all(payload):
        # Test stub records the payload and reports "all sent" so
        # the test endpoint round-trip is exercised without doing
        # real Web Push.
        calls.setdefault("push_payloads", []).append(payload)
        sent = len(calls.get("push_subs", []))
        return {"sent": sent, "failed": 0, "pruned": 0}

    callbacks = BridgeCallbacks(
        cfg_snapshot=cfg_snapshot,
        re_match_rule=re_match_rule,
        perform_send=perform_send,
        perform_window_send=perform_window_send,
        perform_window_scroll=perform_window_scroll,
        perform_window_click_at=perform_window_click_at,
        is_rules_running=is_rules_running,
        set_rules_running=set_rules_running,
        rename_window=rename_window,
        auto_detect_windows=auto_detect_windows,
        new_bridge_setup=new_bridge_setup,
        activate_bridge_setup=activate_bridge_setup,
        delete_bridge_setup=delete_bridge_setup,
        ensure_vapid_keys=ensure_vapid_keys,
        add_push_subscription=add_push_subscription,
        remove_push_subscription=remove_push_subscription,
        send_push_to_all=send_push_to_all,
    )
    service = BridgeService(callbacks)
    app = build_app(service)
    client = TestClient(app)
    yield client, service, calls


def test_window_send_endpoint_sends_immediately_when_idle(fastapi_client):
    """When the live state for a window is idle, /api/windows/{id}/send
    fires the callback synchronously and reports sent=True."""
    client, service, calls = fastapi_client
    # Configure a window in cfg + mark it idle in the store.
    calls["cfg"]["bridge"] = {
        "windows": [
            {
                "id": "w1",
                "name": "Cursor",
                "region": [0, 0, 800, 600],
                "chat_target": [400, 510],
            }
        ]
    }
    service.windows.update(
        [{"id": "w1", "name": "Cursor", "idle": True, "score": 0.95, "configured": True}],
        {},
    )

    res = client.post("/api/windows/w1/send", json={"text": "ship it"})
    assert res.status_code == 200
    body = res.json()
    assert body["sent"] is True
    assert body["queued"] is False
    assert calls["window_send"] == [
        (
            {
                "id": "w1",
                "name": "Cursor",
                "region": [0, 0, 800, 600],
                "chat_target": [400, 510],
            },
            "ship it",
        )
    ]


def test_window_send_endpoint_queues_when_busy(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    service.windows.update(
        [{"id": "w1", "name": "X", "idle": False, "score": 0.1, "configured": True}],
        {},
    )
    res = client.post("/api/windows/w1/send", json={"text": "hold"})
    assert res.status_code == 202
    assert res.json()["queued"] is True
    assert service.windows.pending("w1") == ["hold"]
    assert calls["window_send"] == []  # not fired yet


def test_window_send_endpoint_accepts_empty_text_when_idle(fastapi_client):
    """Empty payload = "just click + Enter" — useful when the user has
    already typed the message in the target window and only needs the
    submit keystroke from the bridge."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    service.windows.update(
        [{"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}], {}
    )
    res = client.post("/api/windows/w1/send", json={"text": ""})
    assert res.status_code == 200
    assert calls["window_send"][0][1] == ""


def test_window_send_endpoint_accepts_whitespace_text(fastapi_client):
    """A single space / dot is a real message — pass it through verbatim."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    service.windows.update(
        [{"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}], {}
    )
    res = client.post("/api/windows/w1/send", json={"text": " "})
    assert res.status_code == 200
    assert calls["window_send"][0][1] == " "


def test_window_send_endpoint_400_when_text_field_missing(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    service.windows.update(
        [{"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}], {}
    )
    res = client.post("/api/windows/w1/send", json={})
    assert res.status_code == 400


def test_window_send_endpoint_404_for_unknown_window(fastapi_client):
    client, *_ = fastapi_client
    res = client.post("/api/windows/nope/send", json={"text": "x"})
    assert res.status_code == 404


def test_window_queue_drains_one_per_idle_transition(fastapi_client):
    """Going busy → idle pops one queued message and dispatches it. A
    second message stays queued until the next idle transition."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    # Start busy.
    service.update_window_states(
        [{"id": "w1", "name": "X", "idle": False, "score": 0.1, "configured": True}], {}
    )
    # Queue two messages while busy.
    service.windows.enqueue("w1", "first")
    service.windows.enqueue("w1", "second")
    # Flip idle: should drain one.
    service.update_window_states(
        [{"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}], {}
    )
    # Drain happens in a daemon thread; give it a moment.
    import time
    for _ in range(50):
        if calls["window_send"]:
            break
        time.sleep(0.02)
    assert len(calls["window_send"]) == 1
    assert calls["window_send"][0][1] == "first"
    assert service.windows.pending("w1") == ["second"]


def test_window_queue_send_now_pops_specific_index_and_fires(fastapi_client):
    """The 'Send now' button on a queued message should pop *that*
    message from the queue and dispatch it via perform_window_send,
    even if the window is currently busy."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    service.windows.update(
        [{"id": "w1", "name": "X", "idle": False, "score": 0.1, "configured": True}], {}
    )
    service.windows.enqueue("w1", "first")
    service.windows.enqueue("w1", "second")
    service.windows.enqueue("w1", "third")

    res = client.post("/api/windows/w1/queue/1/send_now")
    assert res.status_code == 200
    assert res.json() == {"sent": True, "text": "second"}
    assert calls["window_send"][0][1] == "second"
    assert service.windows.pending("w1") == ["first", "third"]


def test_window_store_update_at_replaces_text():
    from press_bridge import WindowStore

    store = WindowStore()
    store.enqueue("w1", "first")
    store.enqueue("w1", "second")
    store.enqueue("w1", "third")
    assert store.update_at("w1", 1, "second-edited") is True
    assert store.pending("w1") == ["first", "second-edited", "third"]
    # Empty / whitespace allowed.
    assert store.update_at("w1", 0, "") is True
    assert store.pending("w1") == ["", "second-edited", "third"]
    # None rejected.
    assert store.update_at("w1", 0, None) is False
    # Out-of-range rejected.
    assert store.update_at("w1", 9, "nope") is False


def test_window_queue_update_one_endpoint(fastapi_client):
    """PUT swaps the text at the given index, returns the new value, and
    fires an SSE so other phones see the change."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    service.windows.enqueue("w1", "first")
    service.windows.enqueue("w1", "second")
    res = client.put("/api/windows/w1/queue/0", json={"text": "first-edited"})
    assert res.status_code == 200
    assert res.json() == {"updated": True, "text": "first-edited"}
    assert service.windows.pending("w1") == ["first-edited", "second"]


def test_window_queue_update_one_400_when_text_missing(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    service.windows.enqueue("w1", "first")
    res = client.put("/api/windows/w1/queue/0", json={})
    assert res.status_code == 400


def test_window_queue_update_one_404_for_bad_index(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    res = client.put("/api/windows/w1/queue/0", json={"text": "x"})
    assert res.status_code == 404


def test_window_queue_delete_one_pops_specific_index(fastapi_client):
    """The trash button on a queued row should drop *that* message
    without firing it. Adjacent items keep their order."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    service.windows.enqueue("w1", "first")
    service.windows.enqueue("w1", "second")
    service.windows.enqueue("w1", "third")

    res = client.delete("/api/windows/w1/queue/1")
    assert res.status_code == 200
    assert res.json() == {"deleted": True, "text": "second"}
    assert service.windows.pending("w1") == ["first", "third"]
    # No perform_window_send fired — delete is silent.
    assert calls["window_send"] == []


def test_window_queue_delete_one_404_for_bad_index(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    res = client.delete("/api/windows/w1/queue/0")
    assert res.status_code == 404


def test_window_queue_send_now_404_for_bad_index(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    res = client.post("/api/windows/w1/queue/0/send_now")
    assert res.status_code == 404


def test_window_store_update_window_name_in_place():
    """update_window_name flips the cached display name without waiting
    for the next worker tick to repopulate state."""
    from press_bridge import WindowStore

    store = WindowStore()
    # No state yet → refused.
    assert store.update_window_name("w1", "X") is False
    store.update(
        [{"id": "w1", "name": "Old", "idle": True, "score": 0.95, "configured": True}],
        {},
    )
    assert store.update_window_name("w1", "New") is True
    [s] = store.summaries()
    assert s["name"] == "New"
    # Empty / whitespace rejected.
    assert store.update_window_name("w1", "") is False
    assert store.update_window_name("w1", "   ") is False


def test_window_rename_endpoint_persists_and_pushes(fastapi_client):
    """PUT /name calls the rename callback (which persists to config),
    updates the cached display name, and fires SSE so phones see the
    new label without waiting for a tick."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "Old", "region": [0, 0, 100, 100]}]
    }
    # Seed live state so update_window_name can mutate it.
    service.windows.update(
        [{"id": "w1", "name": "Old", "idle": True, "score": 0.95, "configured": True}],
        {},
    )
    res = client.put("/api/windows/w1/name", json={"name": "Cursor — billing rewrite"})
    assert res.status_code == 200
    assert res.json() == {"renamed": True, "name": "Cursor — billing rewrite"}
    assert calls["renames"] == [("w1", "Cursor — billing rewrite")]
    [s] = service.windows.summaries()
    assert s["name"] == "Cursor — billing rewrite"


def test_window_rename_endpoint_400_on_empty_name(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "Old", "region": [0, 0, 100, 100]}]
    }
    res = client.put("/api/windows/w1/name", json={"name": "   "})
    assert res.status_code == 400


def test_window_rename_endpoint_404_for_unknown_window(fastapi_client):
    client, service, calls = fastapi_client
    res = client.put("/api/windows/missing/name", json={"name": "X"})
    assert res.status_code == 404


def test_window_scroll_endpoint_calls_callback_with_centered_point(fastapi_client):
    """The bridge endpoint forwards the configured window dict + amount
    to perform_window_scroll. The callback is responsible for clicking
    the centre and sending wheel events."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [100, 200, 800, 600]}]
    }
    res = client.post("/api/windows/w1/scroll", json={"amount": 3})
    assert res.status_code == 200
    assert res.json() == {"scrolled": 3}
    assert len(calls["window_scroll"]) == 1
    win, amount = calls["window_scroll"][0]
    assert win["region"] == [100, 200, 800, 600]
    assert amount == 3


def test_window_scroll_endpoint_accepts_negative_amount(fastapi_client):
    """Negative amount = scroll down (newer messages); the endpoint
    forwards it as-is and the desktop callback interprets the sign."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [100, 200, 800, 600]}]
    }
    res = client.post("/api/windows/w1/scroll", json={"amount": -8})
    assert res.status_code == 200
    assert res.json() == {"scrolled": -8}
    assert len(calls["window_scroll"]) == 1
    _, amount = calls["window_scroll"][0]
    assert amount == -8


def test_window_scroll_endpoint_400_for_zero_amount(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}]
    }
    res = client.post("/api/windows/w1/scroll", json={"amount": 0})
    assert res.status_code == 400


def test_window_scroll_endpoint_400_when_window_has_no_region(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {"windows": [{"id": "w1", "name": "X", "region": None}]}
    res = client.post("/api/windows/w1/scroll", json={"amount": 1})
    assert res.status_code == 400


def test_window_scroll_endpoint_404_for_unknown_window(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post("/api/windows/missing/scroll", json={"amount": 1})
    assert res.status_code == 404


def test_window_snapshot_endpoint_returns_captured(fastapi_client):
    """The recapture endpoint accepts no body and reports {captured: true}.
    The actual capture runs on a daemon thread; we don't assert the side
    effect here — _post_scroll_recheck has its own coverage. The contract
    we care about is: window must exist, must have a region, must be
    configured."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {
        "windows": [{"id": "w1", "name": "X", "region": [10, 20, 300, 400]}]
    }
    res = client.post("/api/windows/w1/snapshot")
    assert res.status_code == 200
    assert res.json() == {"captured": True}


def test_window_snapshot_endpoint_404_for_unknown_window(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post("/api/windows/missing/snapshot")
    assert res.status_code == 404


def test_window_snapshot_endpoint_400_when_window_has_no_region(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"] = {"windows": [{"id": "w1", "name": "X", "region": None}]}
    res = client.post("/api/windows/w1/snapshot")
    assert res.status_code == 400


def test_admin_rules_get_reflects_callback(fastapi_client):
    client, service, calls = fastapi_client
    calls["rules_running"] = True
    res = client.get("/api/admin/rules")
    assert res.status_code == 200
    assert res.json() == {"running": True}


def test_admin_rules_post_calls_setter(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post("/api/admin/rules", json={"running": True})
    assert res.status_code == 200
    assert calls["rules_set"] == [True]
    assert calls["rules_running"] is True
    res = client.post("/api/admin/rules", json={"running": False})
    assert res.status_code == 200
    assert calls["rules_set"] == [True, False]


def test_admin_rules_post_400_when_payload_malformed(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post("/api/admin/rules", json={"running": "yes"})
    assert res.status_code == 400


def test_state_endpoint_reports_rules_running(fastapi_client):
    client, service, calls = fastapi_client
    calls["rules_running"] = True
    res = client.get("/api/state")
    assert res.status_code == 200
    assert res.json()["rules_running"] is True


def test_window_store_set_snapshot_force_inserts_when_state_exists(fastapi_client):
    """set_snapshot bypasses the busy↔idle gate that update() applies —
    used by the scroll path so the user sees the new visible region
    immediately, regardless of whether the window flipped state."""
    from press_bridge import WindowStore

    store = WindowStore(snapshots_per_window=2)
    # No state yet → set_snapshot should refuse.
    assert store.set_snapshot("w1", b"png-x") is False
    # Seed state via update (no image), then force-insert.
    store.update(
        [{"id": "w1", "name": "X", "idle": True, "score": 0.95, "configured": True}], {}
    )
    assert store.set_snapshot("w1", b"png-1") is True
    assert store.snapshot("w1", 0)[1] == b"png-1"
    # A second forced snapshot pushes the first along the ring.
    store.set_snapshot("w1", b"png-2")
    assert store.snapshot("w1", 0)[1] == b"png-2"
    assert store.snapshot("w1", 1)[1] == b"png-1"


def test_window_clear_queue(fastapi_client):
    client, service, _ = fastapi_client
    service.windows.enqueue("w1", "a")
    service.windows.enqueue("w1", "b")
    res = client.delete("/api/windows/w1/queue")
    assert res.status_code == 200
    assert res.json()["cleared"] == 2
    assert service.windows.pending("w1") == []


def test_evaluate_bridge_windows_returns_empty_if_template_file_missing():
    bridge_cfg = {
        "idle_template_path": "does_not_exist.png",
        "windows": [{"id": "w1", "name": "X", "region": [0, 0, 100, 100]}],
    }
    assert press_engine.evaluate_bridge_windows(bridge_cfg) == []


# ---- Auto-detect ----------------------------------------------------------

def test_auto_detect_endpoint_returns_count(fastapi_client):
    """When the desktop callback reports a positive window count, the
    endpoint echoes mode + count back as JSON."""
    client, service, calls = fastapi_client
    calls["auto_detect_result"] = 3
    res = client.post("/api/admin/auto_detect", json={"mode": "add"})
    assert res.status_code == 200
    assert res.json() == {"mode": "add", "window_count": 3}
    assert calls["auto_detect"] == ["add"]


def test_auto_detect_endpoint_400_when_callback_reports_nothing(fastapi_client):
    """callback returning -1 means 'nothing detected on this platform' —
    the endpoint surfaces that as a 400 so the phone can show a sane
    error instead of silently appending zero windows."""
    client, service, calls = fastapi_client
    calls["auto_detect_result"] = -1
    res = client.post("/api/admin/auto_detect", json={"mode": "add"})
    assert res.status_code == 400


def test_auto_detect_endpoint_defaults_to_add_mode(fastapi_client):
    """Empty body / missing mode key falls back to the safe choice."""
    client, service, calls = fastapi_client
    calls["auto_detect_result"] = 1
    res = client.post("/api/admin/auto_detect", json={})
    assert res.status_code == 200
    assert calls["auto_detect"] == ["add"]


def test_auto_detect_endpoint_clamps_invalid_mode(fastapi_client):
    """Anything other than 'add' / 'replace' is treated as 'add' — the
    endpoint is a small public surface, don't trust the input."""
    client, service, calls = fastapi_client
    calls["auto_detect_result"] = 2
    res = client.post("/api/admin/auto_detect", json={"mode": "nuke"})
    assert res.status_code == 200
    assert calls["auto_detect"] == ["add"]


def test_auto_detect_endpoint_501_when_callback_missing(fastapi_client):
    """If the desktop side didn't wire auto_detect_windows (e.g. older
    build), the endpoint reports 501 rather than crashing."""
    client, service, calls = fastapi_client
    service.callbacks.auto_detect_windows = None
    res = client.post("/api/admin/auto_detect", json={"mode": "add"})
    assert res.status_code == 501


# ---- press_windows module --------------------------------------------------

def test_list_cursor_windows_returns_list_off_windows(monkeypatch):
    """On non-Windows platforms the function is a quiet no-op returning
    an empty list. Test runs on whatever the CI host is, so we patch
    the IS_WINDOWS flag to verify the early-return path."""
    import press_windows
    monkeypatch.setattr(press_windows, "IS_WINDOWS", False)
    assert press_windows.list_cursor_windows() == []


def test_short_label_trims_cursor_suffix():
    from press_windows import _short_label
    assert _short_label("README.md - auto-press - Cursor") == "README.md - auto-press"
    assert _short_label("Foo - Cursor") == "Foo"
    assert _short_label("Standalone") == "Standalone"
    assert _short_label(" - Cursor") == "Cursor"  # empty → fallback


# ---- Setups: data-layer helpers -------------------------------------------

def test_normalize_creates_default_setup_from_live_windows():
    """A config with windows but no setups gets a 'Default' setup
    auto-created from those windows on normalize. The active id
    points at it."""
    cfg = press_store.normalize_config(
        {
            "bridge": {
                "windows": [
                    {"id": "w1", "name": "Existing", "region": [0, 0, 100, 100]},
                ],
            }
        }
    )
    bridge = cfg["bridge"]
    assert len(bridge["setups"]) == 1
    [setup] = bridge["setups"]
    assert setup["name"] == "Default"
    assert setup["windows"][0]["name"] == "Existing"
    assert bridge["active_setup_id"] == setup["id"]


def test_normalize_wraps_live_as_current_when_setups_exist_but_no_active():
    """If a config has existing setups but no active_setup_id (e.g.
    migrating from a previous schema), the live windows become a
    new 'Current' setup so the user's view is preserved alongside
    pre-existing saved layouts."""
    cfg = press_store.normalize_config(
        {
            "bridge": {
                "windows": [
                    {"id": "live", "name": "Live", "region": [0, 0, 50, 50]},
                ],
                "setups": [
                    {"id": "old", "name": "Old", "windows": []},
                ],
            }
        }
    )
    bridge = cfg["bridge"]
    assert len(bridge["setups"]) == 2
    # 'Current' is prepended so it's the active one.
    assert bridge["setups"][0]["name"] == "Current"
    assert bridge["active_setup_id"] == bridge["setups"][0]["id"]
    # And the old setup is still there.
    assert bridge["setups"][1]["name"] == "Old"


def test_new_setup_creates_empty_active_and_mirrors_outgoing():
    """new_setup mirrors the outgoing setup's windows first so its
    state is preserved, then creates an empty setup and activates it."""
    from press_store import new_setup

    cfg = press_store.normalize_config(
        {
            "bridge": {
                "windows": [
                    {"id": "w1", "name": "Main", "region": [0, 0, 800, 600]},
                ],
            }
        }
    )
    old_active = cfg["bridge"]["active_setup_id"]

    new_id = new_setup(cfg, "Blank slate")
    bridge = cfg["bridge"]
    # Two setups now: the old one (with its 1 window mirrored) and
    # the new empty active one.
    assert len(bridge["setups"]) == 2
    assert bridge["active_setup_id"] == new_id
    assert bridge["windows"] == []
    # Old setup retained its windows.
    old = next(s for s in bridge["setups"] if s["id"] == old_active)
    assert len(old["windows"]) == 1
    assert old["windows"][0]["name"] == "Main"


def test_activate_setup_swaps_live_windows_and_mirrors_outgoing():
    """Activating a different setup mirrors the outgoing setup's
    windows first (so edits aren't lost), then loads the incoming
    setup's snapshot into bridge.windows."""
    from press_store import activate_setup, new_setup

    cfg = press_store.normalize_config(
        {
            "bridge": {
                "windows": [
                    {"id": "w1", "name": "Main", "region": [0, 0, 100, 100]},
                ],
            }
        }
    )
    setup_a = cfg["bridge"]["active_setup_id"]
    setup_b = new_setup(cfg, "Other")  # switches to empty B
    # Edit B's windows.
    cfg["bridge"]["windows"] = [
        {"id": "wB", "name": "InB", "region": [0, 0, 50, 50]},
    ]

    # Switch back to A — B's edit should be preserved (mirrored on switch).
    assert activate_setup(cfg, setup_a) is True
    assert cfg["bridge"]["active_setup_id"] == setup_a
    [w] = cfg["bridge"]["windows"]
    assert w["name"] == "Main"

    # Switching to B again brings back the InB window.
    assert activate_setup(cfg, setup_b) is True
    [w] = cfg["bridge"]["windows"]
    assert w["name"] == "InB"


def test_activate_setup_unknown_id_returns_false_and_no_op():
    from press_store import activate_setup

    cfg = press_store.normalize_config({"bridge": {}})
    before = list(cfg["bridge"]["windows"])
    active = cfg["bridge"]["active_setup_id"]
    assert activate_setup(cfg, "no-such-id") is False
    # Live state untouched.
    assert cfg["bridge"]["windows"] == before
    assert cfg["bridge"]["active_setup_id"] == active


def test_delete_setup_swaps_active_to_first_remaining():
    """Deleting the active setup activates the first remaining
    setup and loads its windows into bridge.windows."""
    from press_store import delete_setup, new_setup

    cfg = press_store.normalize_config(
        {
            "bridge": {
                "windows": [
                    {"id": "w1", "name": "InDefault", "region": [0, 0, 100, 100]},
                ],
            }
        }
    )
    default_id = cfg["bridge"]["active_setup_id"]
    new_id = new_setup(cfg, "New")
    # Edit New's windows so we can verify Default's windows come back.
    cfg["bridge"]["windows"] = [{"id": "wN", "name": "InNew"}]

    assert delete_setup(cfg, new_id) is True
    # Default is back as the only setup, active, with its windows.
    assert cfg["bridge"]["active_setup_id"] == default_id
    [w] = cfg["bridge"]["windows"]
    assert w["name"] == "InDefault"


def test_delete_last_setup_creates_fresh_default():
    """If the only remaining setup is deleted, a fresh empty Default
    is auto-created and activated — invariant: always one setup."""
    from press_store import delete_setup

    cfg = press_store.normalize_config({"bridge": {}})
    only_id = cfg["bridge"]["active_setup_id"]
    assert delete_setup(cfg, only_id) is True
    # A new Default replaced it.
    assert len(cfg["bridge"]["setups"]) == 1
    new_active = cfg["bridge"]["setups"][0]
    assert new_active["name"] == "Default"
    assert cfg["bridge"]["active_setup_id"] == new_active["id"]
    assert cfg["bridge"]["windows"] == []


def test_sync_active_setup_mirrors_live_windows():
    """sync_active_setup is the mirror step that save_config calls
    before persisting — it copies bridge.windows into the active
    setup's snapshot so the on-disk state has them paired."""
    from press_store import sync_active_setup

    cfg = press_store.normalize_config(
        {
            "bridge": {
                "windows": [
                    {"id": "w1", "name": "One", "region": [0, 0, 10, 10]},
                ],
            }
        }
    )
    active_id = cfg["bridge"]["active_setup_id"]
    # Mutate live windows without going through any helper.
    cfg["bridge"]["windows"].append(
        {"id": "w2", "name": "Two", "region": [0, 0, 20, 20]}
    )
    sync_active_setup(cfg)
    active = next(s for s in cfg["bridge"]["setups"] if s["id"] == active_id)
    assert [w["name"] for w in active["windows"]] == ["One", "Two"]


# ---- Setups: HTTP endpoints -----------------------------------------------

def test_list_setups_endpoint_includes_active_id(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"]["setups"] = [
        {"id": "a", "name": "First", "windows": [{"id": "w1", "name": "X"}]},
        {"id": "b", "name": "Second", "windows": []},
    ]
    calls["cfg"]["bridge"]["active_setup_id"] = "b"
    res = client.get("/api/bridge/setups")
    assert res.status_code == 200
    data = res.json()
    assert data == {
        "active_id": "b",
        "setups": [
            {"id": "a", "name": "First", "window_count": 1},
            {"id": "b", "name": "Second", "window_count": 0},
        ],
    }


def test_new_setup_endpoint_creates_and_activates(fastapi_client):
    client, service, calls = fastapi_client
    # Pre-seed an active setup with windows so we can verify the
    # outgoing setup's windows get mirrored when we create a new one.
    calls["cfg"] = press_store.normalize_config(
        {
            "bridge": {
                "windows": [
                    {"id": "w1", "name": "Live", "region": [0, 0, 100, 100]},
                ],
            }
        }
    )
    prev_active = calls["cfg"]["bridge"]["active_setup_id"]

    res = client.post("/api/bridge/setups", json={"name": "From phone"})
    assert res.status_code == 200
    body = res.json()
    assert body["name"] == "From phone"
    assert body["active_id"] == body["id"]
    # New setup is active and starts empty.
    bridge = calls["cfg"]["bridge"]
    assert bridge["active_setup_id"] == body["id"]
    assert bridge["windows"] == []
    # Previous active retained its mirrored windows.
    prev = next(s for s in bridge["setups"] if s["id"] == prev_active)
    assert prev["windows"][0]["name"] == "Live"


def test_new_setup_endpoint_400_for_blank_name(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post("/api/bridge/setups", json={"name": "   "})
    assert res.status_code == 400


def test_activate_setup_endpoint_swaps_live_windows(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"] = press_store.normalize_config(
        {
            "bridge": {
                "windows": [{"id": "w-live", "name": "Live"}],
                "setups": [
                    {
                        "id": "other",
                        "name": "Other",
                        "windows": [{"id": "w-other", "name": "FromOther"}],
                    }
                ],
            }
        }
    )
    # After normalize: live "w-live" became a 'Current' setup, and
    # 'other' is still in the list. Activate 'other'.
    res = client.post("/api/bridge/setups/other/activate")
    assert res.status_code == 200
    assert res.json()["active_id"] == "other"
    [w] = calls["cfg"]["bridge"]["windows"]
    assert w["name"] == "FromOther"


def test_activate_setup_endpoint_404_unknown(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post("/api/bridge/setups/no-such-id/activate")
    assert res.status_code == 404


def test_delete_setup_endpoint(fastapi_client):
    client, service, calls = fastapi_client
    # Two setups so we can delete one without hitting the
    # "auto-create new Default" path.
    calls["cfg"]["bridge"]["setups"] = [
        {"id": "doomed", "name": "Goes", "windows": []},
        {"id": "keeper", "name": "Stays", "windows": []},
    ]
    calls["cfg"]["bridge"]["active_setup_id"] = "keeper"
    res = client.delete("/api/bridge/setups/doomed")
    assert res.status_code == 200
    [remaining] = calls["cfg"]["bridge"]["setups"]
    assert remaining["id"] == "keeper"


# ---- Workspace binding ----------------------------------------------------

def test_setup_normalize_preserves_workspace_id():
    """A setup's workspace_id GUID survives a normalize round-trip;
    a blank one is coerced to None."""
    cfg = press_store.normalize_config(
        {
            "bridge": {
                "setups": [
                    {
                        "id": "a",
                        "name": "First",
                        "windows": [],
                        "workspace_id": "{aaaa1111-bbbb-2222-cccc-dddddddddddd}",
                    },
                    {"id": "b", "name": "Unbound", "windows": []},
                    {"id": "c", "name": "Blank", "windows": [], "workspace_id": "   "},
                ],
            }
        }
    )
    s_first, s_unbound, s_blank = cfg["bridge"]["setups"]
    assert s_first["workspace_id"] == "{aaaa1111-bbbb-2222-cccc-dddddddddddd}"
    assert s_unbound["workspace_id"] is None
    assert s_blank["workspace_id"] is None


def test_workspace_switch_endpoint_400_for_bad_direction(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post("/api/bridge/workspace/switch", json={"direction": "diagonal"})
    assert res.status_code == 400


def test_workspace_switch_endpoint_501_when_unwired(fastapi_client):
    client, service, calls = fastapi_client
    # Strip the callback off the live service.
    service.callbacks.workspace_switch = None
    res = client.post("/api/bridge/workspace/switch", json={"direction": "next"})
    assert res.status_code == 501


def test_workspace_switch_endpoint_calls_callback(fastapi_client):
    client, service, calls = fastapi_client

    def fake_switch(direction):
        calls.setdefault("workspace_switches", []).append(direction)
        return {
            "workspace_id": "{1111-2222-3333-4444-555555555555}",
            "active_setup_id": "abc",
        }

    service.callbacks.workspace_switch = fake_switch
    res = client.post("/api/bridge/workspace/switch", json={"direction": "next"})
    assert res.status_code == 200
    body = res.json()
    assert body["workspace_id"] == "{1111-2222-3333-4444-555555555555}"
    assert body["active_setup_id"] == "abc"
    assert calls["workspace_switches"] == ["next"]


def test_workspace_bind_endpoint_501_when_unwired(fastapi_client):
    client, service, calls = fastapi_client
    service.callbacks.workspace_bind_active = None
    res = client.post("/api/bridge/workspace/bind")
    assert res.status_code == 501


def test_workspace_bind_endpoint_400_when_id_unreadable(fastapi_client):
    client, service, calls = fastapi_client
    service.callbacks.workspace_bind_active = lambda: None
    res = client.post("/api/bridge/workspace/bind")
    assert res.status_code == 400


def test_workspace_bind_endpoint_returns_guid(fastapi_client):
    client, service, calls = fastapi_client
    service.callbacks.workspace_bind_active = lambda: "{abcd-1234}"
    res = client.post("/api/bridge/workspace/bind")
    assert res.status_code == 200
    assert res.json() == {"workspace_id": "{abcd-1234}"}


# ---- Click-at-fraction (phone tap-to-click on snapshot) -------------------

def test_click_at_translates_fractions_to_screen_coords(fastapi_client):
    """A tap at (0.5, 0.25) on a 800x600 window region at (100, 200)
    must land at (500, 350) in screen coords. DPI doesn't enter the
    math — the snapshot is captured at the window's physical pixels."""
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"]["windows"] = [
        {"id": "w1", "name": "Cursor", "region": [100, 200, 800, 600]},
    ]
    res = client.post(
        "/api/windows/w1/click_at",
        json={"x_frac": 0.5, "y_frac": 0.25},
    )
    assert res.status_code == 200
    body = res.json()
    assert body["clicked"] is True
    assert body["target"] == [500, 350]
    [(_, x_frac, y_frac, tx, ty)] = calls["window_clicks"]
    assert (x_frac, y_frac, tx, ty) == (0.5, 0.25, 500, 350)


def test_click_at_rejects_out_of_range_fractions(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"]["windows"] = [
        {"id": "w1", "name": "Cursor", "region": [0, 0, 100, 100]},
    ]
    res = client.post(
        "/api/windows/w1/click_at",
        json={"x_frac": 1.5, "y_frac": 0.5},
    )
    assert res.status_code == 400


def test_click_at_404_for_unknown_window(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post(
        "/api/windows/does-not-exist/click_at",
        json={"x_frac": 0.5, "y_frac": 0.5},
    )
    assert res.status_code == 404


def test_click_at_400_when_window_has_no_region(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"]["windows"] = [
        {"id": "w1", "name": "Cursor", "region": None},
    ]
    res = client.post(
        "/api/windows/w1/click_at",
        json={"x_frac": 0.5, "y_frac": 0.5},
    )
    assert res.status_code == 400


def test_click_at_501_when_callback_unwired(fastapi_client):
    client, service, calls = fastapi_client
    calls["cfg"]["bridge"]["windows"] = [
        {"id": "w1", "name": "Cursor", "region": [0, 0, 100, 100]},
    ]
    service.callbacks.perform_window_click_at = None
    res = client.post(
        "/api/windows/w1/click_at",
        json={"x_frac": 0.5, "y_frac": 0.5},
    )
    assert res.status_code == 501


# ---- Web Push notifications ----------------------------------------------

_SAMPLE_SUB = {
    "endpoint": "https://example.push.example/abc",
    "keys": {
        "p256dh": "BNcRdreALRFXTkOOUHK1EtK2wtaz5Ry4YfYCA_0QTpQtUbVlUls0VJXg7A8u-Ts1XbjhazAkj7I99e8QcYP7DkM",
        "auth": "tBHItJI5svbpez7KI4CCXg",
    },
}


def test_vapid_key_endpoint_returns_public_key(fastapi_client):
    client, service, calls = fastapi_client
    res = client.get("/api/notifications/vapid-key")
    assert res.status_code == 200
    body = res.json()
    assert isinstance(body.get("public_key"), str) and body["public_key"]
    assert calls["vapid_called"] == 1


def test_subscribe_endpoint_persists_subscription(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post(
        "/api/notifications/subscribe",
        json={"subscription": _SAMPLE_SUB, "label": "Pixel 8"},
    )
    assert res.status_code == 200
    body = res.json()
    assert "id" in body and "endpoint_hash" in body
    [stored] = calls["push_subs"]
    assert stored["endpoint"] == _SAMPLE_SUB["endpoint"]
    assert stored["label"] == "Pixel 8"


def test_subscribe_endpoint_idempotent_on_repeat(fastapi_client):
    client, service, calls = fastapi_client
    client.post("/api/notifications/subscribe", json={"subscription": _SAMPLE_SUB})
    res = client.post(
        "/api/notifications/subscribe", json={"subscription": _SAMPLE_SUB}
    )
    assert res.status_code == 200
    # Re-subscribing the same endpoint must replace, not duplicate.
    assert len(calls["push_subs"]) == 1


def test_subscribe_endpoint_400_on_malformed(fastapi_client):
    client, service, calls = fastapi_client
    res = client.post("/api/notifications/subscribe", json={"subscription": {}})
    assert res.status_code == 400


def test_subscribe_accepts_bare_subscription_dict(fastapi_client):
    """The phone may send the PushSubscription JSON directly without
    wrapping it under `subscription`."""
    client, service, calls = fastapi_client
    res = client.post("/api/notifications/subscribe", json=_SAMPLE_SUB)
    assert res.status_code == 200
    assert len(calls["push_subs"]) == 1


def test_unsubscribe_endpoint_drops_by_endpoint(fastapi_client):
    client, service, calls = fastapi_client
    client.post("/api/notifications/subscribe", json={"subscription": _SAMPLE_SUB})
    res = client.request(
        "DELETE",
        "/api/notifications/subscribe",
        json={"endpoint": _SAMPLE_SUB["endpoint"]},
    )
    assert res.status_code == 200
    assert res.json()["removed"] == 1
    assert calls["push_subs"] == []


def test_window_store_transition_carries_flipped_flags():
    """The transition entries returned by WindowStore.update must
    include flipped_to_idle / flipped_to_asking so the push fan-out
    only fires on actual edges, not on every state churn."""
    from press_bridge import WindowStore

    store = WindowStore()
    # First tick — no prior state, no transition emitted.
    assert store.update(
        [{"id": "w1", "name": "Cursor", "idle": False, "asking": False, "configured": True}],
        {},
    ) == []
    # Busy → idle.
    trs = store.update(
        [{"id": "w1", "name": "Cursor", "idle": True, "asking": False, "configured": True}],
        {},
    )
    assert len(trs) == 1
    assert trs[0]["flipped_to_idle"] is True
    assert trs[0]["flipped_to_asking"] is False
    # Idle → asking (while still idle).
    trs = store.update(
        [{"id": "w1", "name": "Cursor", "idle": True, "asking": True, "configured": True}],
        {},
    )
    assert len(trs) == 1
    assert trs[0]["flipped_to_idle"] is False  # was already idle
    assert trs[0]["flipped_to_asking"] is True


def test_press_push_normalize_subscription():
    from press_push import normalize_subscription

    assert normalize_subscription(None) is None
    assert normalize_subscription({}) is None
    # Missing keys block.
    assert normalize_subscription({"endpoint": "https://x"}) is None
    # Valid shape.
    sub = normalize_subscription(
        {
            "endpoint": "https://x.example/foo",
            "keys": {"p256dh": "pubkey", "auth": "secret"},
            "expirationTime": None,
            "extra": "dropped",
        }
    )
    assert sub == {
        "endpoint": "https://x.example/foo",
        "keys": {"p256dh": "pubkey", "auth": "secret"},
    }


def test_test_push_endpoint_returns_summary(fastapi_client):
    client, service, calls = fastapi_client
    client.post("/api/notifications/subscribe", json={"subscription": _SAMPLE_SUB})
    res = client.post("/api/notifications/test")
    assert res.status_code == 200
    body = res.json()
    assert body == {"sent": 1, "failed": 0, "pruned": 0}
    # Test payload propagated to the fan-out callback.
    [payload] = calls["push_payloads"]
    assert payload["title"] == "auto-press test"
    assert payload["tag"] == "test"
