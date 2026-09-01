import press_engine


def test_codex_is_runnable_without_idle_template():
    from press_engine import bridge_has_runnable_targets

    assert bridge_has_runnable_targets({
        "windows": [{"backend": "codex_desktop", "region": [0, 0, 1000, 800]}]
    }) is True


def test_bridge_evaluation_dispatches_codex(monkeypatch):
    import numpy as np

    rgb = np.full((800, 1000, 3), 24, dtype=np.uint8)
    rgb[300:307, 198:205] = (47, 129, 247)
    monkeypatch.setattr(press_engine, "capture_screen_rgb", lambda region=None: rgb)

    states = press_engine.evaluate_bridge_windows({"windows": [{
        "id": "c1", "name": "Codex", "backend": "codex_desktop", "region": [0, 0, 1000, 800]
    }]}, capture_rgb=True)

    assert states[0]["idle"] is True
    assert states[0]["backend"] == "codex_desktop"
    assert states[0]["ready_count"] == 1


def test_codex_capture_failure_preserves_first_snapshot_opportunity(
    monkeypatch, caplog
):
    import numpy as np

    import press_ui

    rgb = np.full((800, 1000, 3), 24, dtype=np.uint8)
    captures = 0

    def capture_after_failure(_region=None):
        nonlocal captures
        captures += 1
        if captures == 1:
            raise RuntimeError("desktop capture unavailable")
        return rgb

    monkeypatch.setattr(press_engine, "capture_screen_rgb", capture_after_failure)
    caplog.set_level("WARNING", logger="press_engine")
    cfg = {
        "bridge_active": True,
        "bridge": {
            "windows": [{
                "id": "c1",
                "name": "Codex project alpha",
                "backend": "codex_desktop",
                "region": [0, 0, 1000, 800],
            }]
        },
    }
    worker = press_ui.EngineWorker(lambda: cfg)
    emissions = []
    worker.bridge_window_states.connect(
        lambda states, images: emissions.append((states, images))
    )

    worker._tick_bridge_windows(cfg)

    assert emissions[0][0][0]["idle"] is False
    assert emissions[0][0][0]["ready_count"] == 0
    assert emissions[0][1] == {}
    assert worker._last_window_idle == {}
    assert any(
        "capture" in record.getMessage().lower()
        and "c1" in record.getMessage()
        and "Codex project alpha" in record.getMessage()
        for record in caplog.records
    )

    worker._tick_bridge_windows(cfg)

    assert emissions[1][0][0]["idle"] is False
    assert emissions[1][0][0]["ready_count"] == 0
    assert emissions[1][1]["c1"].startswith(b"\x89PNG\r\n\x1a\n")
    assert worker._last_window_idle == {"c1": (False, False)}


def test_codex_evaluation_failure_retains_rgb_and_logs_target(
    monkeypatch, caplog
):
    import numpy as np

    from press_backend_codex import CodexDesktopBackend

    rgb = np.full((800, 1000, 3), 24, dtype=np.uint8)
    monkeypatch.setattr(press_engine, "capture_screen_rgb", lambda _region=None: rgb)

    def fail_evaluation(_self, _rgb):
        raise RuntimeError("marker evaluator unavailable")

    monkeypatch.setattr(CodexDesktopBackend, "evaluate", fail_evaluation)
    caplog.set_level("WARNING", logger="press_engine")

    [state] = press_engine.evaluate_bridge_windows({
        "windows": [{
            "id": "c-eval",
            "name": "Codex project beta",
            "backend": "codex_desktop",
            "region": [0, 0, 1000, 800],
        }]
    }, capture_rgb=True)

    assert state["configured"] is True
    assert state["idle"] is False
    assert state["asking"] is False
    assert state["ready_count"] == 0
    assert state["rgb"] is rgb
    assert any(
        "evaluation" in record.getMessage().lower()
        and "c-eval" in record.getMessage()
        and "Codex project beta" in record.getMessage()
        for record in caplog.records
    )


def test_cursor_missing_rgb_keeps_legacy_observation_tracking(monkeypatch):
    import press_ui

    cfg = {
        "bridge_active": True,
        "bridge": {
            "idle_template_path": "legacy-idle.png",
            "windows": [{
                "id": "w1",
                "name": "Cursor",
                "region": [0, 0, 1000, 800],
            }],
        },
    }
    monkeypatch.setattr(
        press_ui,
        "evaluate_bridge_windows",
        lambda _bridge_cfg, capture_rgb=False: [{
            "id": "w1",
            "name": "Cursor",
            "idle": False,
            "asking": False,
            "score": 0.0,
            "configured": True,
        }],
    )
    worker = press_ui.EngineWorker(lambda: cfg)

    worker._tick_bridge_windows(cfg)

    assert worker._last_window_idle == {"w1": (False, False)}


def test_pick_template_from_pack_picks_closest_scale():
    """Closest-by-distance pick — a target on the 150 % monitor
    should pull the 150 % variant when one exists, otherwise the
    nearest available scale."""
    base = object()
    v100 = object()
    v150 = object()
    pack = {
        "base": base,
        "variants": {1.0: v100, 1.5: v150},
        "source_dpi": 1.5,
    }
    assert press_engine._pick_template_from_pack(pack, 1.5) is v150
    assert press_engine._pick_template_from_pack(pack, 1.0) is v100
    # Equidistant tie-break (1.25 between 1.0 and 1.5) — min() picks
    # whichever comes first in the dict ordering; both are reasonable,
    # so just assert it's one of the two and not None.
    chosen = press_engine._pick_template_from_pack(pack, 1.25)
    assert chosen in (v100, v150)


def test_pick_template_from_pack_falls_through_to_base_for_legacy_capture():
    """A pack with no variants (legacy capture, no source_dpi)
    returns the base template — the original behaviour, no
    surprises for users who haven't re-captured yet."""
    base = object()
    pack = {"base": base, "variants": {}, "source_dpi": None}
    assert press_engine._pick_template_from_pack(pack, 1.5) is base


def test_is_dpi_aware_pack_distinguishes_legacy_from_fresh():
    """The per-monitor matching path only fires for packs with real
    DPI metadata. Legacy captures (no source_dpi) keep using the
    cheaper single-virtual-screen frame."""
    assert press_engine._is_dpi_aware_pack({"source_dpi": 1.5, "variants": {1.5: object()}}) is True
    assert press_engine._is_dpi_aware_pack({"source_dpi": None, "variants": {}}) is False
    assert press_engine._is_dpi_aware_pack(None) is False
    assert press_engine._is_dpi_aware_pack({}) is False


def test_find_rule_matches_uses_per_monitor_when_dpi_aware_and_unbounded(monkeypatch):
    """A template rule with a DPI-aware pack and no search_region
    must go through the per-monitor pipeline — that's the fix for
    'captured a button on screen A, moved window to screen B and
    matching stopped working'."""
    called: dict = {"per_monitor": False, "frame": None}

    def fake_per_monitor(rule, cache=None):
        called["per_monitor"] = True
        called["cache"] = cache
        return [(0.97, (1348, -266))]

    monkeypatch.setattr(
        press_engine, "_find_template_matches_per_monitor", fake_per_monitor
    )

    rule = {
        "matcher": "template",
        "search_region": None,
        "threshold": 0.9,
        "template_gray": object(),
        "template_pack": {
            "base": object(),
            "variants": {1.0: object(), 1.5: object()},
            "source_dpi": 1.5,
        },
    }
    matches = press_engine.find_rule_matches(object(), rule)
    assert called["per_monitor"] is True
    assert matches == [(0.97, (1348, -266))]


def test_find_rule_matches_skips_per_monitor_for_legacy_unbounded_rule(monkeypatch):
    """A legacy template (no source_dpi) keeps using the cached
    virtual-screen frame — no regression in CPU cost for users who
    haven't re-captured."""
    called = {"per_monitor": False}

    def fake_per_monitor(rule, cache=None):
        called["per_monitor"] = True
        return []

    monkeypatch.setattr(
        press_engine, "_find_template_matches_per_monitor", fake_per_monitor
    )
    monkeypatch.setattr(
        press_engine, "_find_matches_in", lambda *args, **kwargs: [(0.5, (1, 2))]
    )
    monkeypatch.setattr(press_engine, "_virtual_screen_origin", lambda: (0, 0))

    rule = {
        "matcher": "template",
        "search_region": None,
        "threshold": 0.9,
        "template_gray": object(),
        "template_pack": {"base": object(), "variants": {}, "source_dpi": None},
    }
    press_engine.find_rule_matches(object(), rule)
    assert called["per_monitor"] is False


def test_evaluate_rules_returns_all_matches(monkeypatch):
    runtime_rules = [
        {"id": "a", "name": "Rule A", "threshold": 0.9},
        {"id": "b", "name": "Rule B", "threshold": 0.9},
    ]

    monkeypatch.setattr(press_engine, "capture_screen_gray", lambda: object())

    def fake_find(_frame, rule):
        if rule["id"] == "a":
            return [(0.95, (10, 10)), (0.93, (20, 20))]
        return [(0.91, (30, 30))]

    monkeypatch.setattr(press_engine, "find_rule_matches", fake_find)

    results, actions = press_engine.evaluate_rules(runtime_rules)

    assert len(results) == 2
    assert results[0]["match_count"] == 2
    assert results[1]["match_count"] == 1
    assert [action["center"] for action in actions] == [(10, 10), (20, 20), (30, 30)]


def test_window_scope_iterates_only_matching_windows(monkeypatch):
    """A rule with non-empty window_scope fires once per tracked
    window whose name is in the scope. Unmatched names silently
    skip. find_rule_matches is called with each window's region as
    search_region."""
    captured_regions: list = []

    def fake_find(_frame, rule):
        captured_regions.append(rule.get("search_region"))
        # One match per call so we can count how many windows were hit.
        return [(0.95, (100, 100))]

    monkeypatch.setattr(press_engine, "find_rule_matches", fake_find)

    runtime_rules = [
        {
            "id": "r1",
            "name": "Scoped",
            "threshold": 0.9,
            "window_scope": ["alpha", "gamma"],
            "matcher": "template",
        }
    ]
    windows = [
        {"name": "alpha", "region": [0, 0, 100, 100]},
        {"name": "beta", "region": [200, 0, 100, 100]},
        {"name": "alpha", "region": [400, 0, 100, 100]},  # duplicate name → both fire
        {"name": "gamma", "region": [600, 0, 100, 100]},
    ]
    results, actions = press_engine.evaluate_rules(runtime_rules, windows)

    # alpha (×2) + gamma (×1) = 3 calls; beta skipped because not in scope.
    assert captured_regions == [[0, 0, 100, 100], [400, 0, 100, 100], [600, 0, 100, 100]]
    assert results[0]["match_count"] == 3


def test_window_scope_dormant_when_no_matching_window(monkeypatch):
    """Scope references a window name that isn't tracked → rule
    silently produces no matches. Matches the user's pick of
    'Silently skip' for the stale-scope case."""
    monkeypatch.setattr(
        press_engine,
        "find_rule_matches",
        lambda _frame, _rule: [(0.95, (100, 100))],
    )
    runtime_rules = [
        {
            "id": "r1",
            "name": "Scoped",
            "threshold": 0.9,
            "window_scope": ["nonexistent-project"],
            "matcher": "template",
        }
    ]
    windows = [{"name": "alpha", "region": [0, 0, 100, 100]}]
    results, actions = press_engine.evaluate_rules(runtime_rules, windows)
    assert results[0]["match_count"] == 0
    assert actions == []


def test_empty_window_scope_falls_through_to_legacy_path(monkeypatch):
    """A rule with empty window_scope behaves exactly like the
    pre-scope code path — must not regress."""
    monkeypatch.setattr(press_engine, "capture_screen_gray", lambda: object())
    monkeypatch.setattr(
        press_engine, "find_rule_matches", lambda _f, _r: [(0.95, (1, 1))]
    )
    runtime_rules = [
        {
            "id": "r1",
            "name": "Unscoped",
            "threshold": 0.9,
            "window_scope": [],
            "matcher": "template",
        }
    ]
    windows = [{"name": "alpha", "region": [0, 0, 100, 100]}]
    results, _ = press_engine.evaluate_rules(runtime_rules, windows)
    assert results[0]["match_count"] == 1


def test_evaluate_rule_on_frame_returns_best_match(monkeypatch):
    monkeypatch.setattr(
        press_engine,
        "find_rule_matches",
        lambda _frame, _rule: [(0.97, (40, 50)), (0.92, (60, 70))],
    )

    score, center = press_engine.evaluate_rule_on_frame(object(), {"id": "a"})

    assert score == 0.97
    assert center == (40, 50)


def test_execute_match_uses_click_enter(monkeypatch):
    called = {}

    monkeypatch.setattr(
        press_engine,
        "do_action",
        lambda mode, center, text_before_enter=None, refocus_mode=None: called.update(
            mode=mode, center=center, text=text_before_enter, refocus=refocus_mode
        ),
    )

    press_engine.execute_match(
        {
            "center": (40, 50),
            "action": "click+type+enter",
            "text": "continue",
        }
    )

    assert called == {
        "mode": "click+enter",
        "center": (40, 50),
        "text": "continue",
        "refocus": None,
    }


def test_execute_matches_waits_between_actions(monkeypatch):
    calls = []

    monkeypatch.setattr(
        press_engine,
        "execute_match",
        lambda match, refocus_mode=None: calls.append(("exec", match["center"])),
    )
    monkeypatch.setattr(
        press_engine.time,
        "sleep",
        lambda seconds: calls.append(("sleep", seconds)),
    )

    press_engine.execute_matches(
        [
            {"center": (10, 10)},
            {"center": (20, 20)},
            {"center": (30, 30)},
        ],
        delay_seconds=0.2,
    )

    assert calls == [
        ("exec", (10, 10)),
        ("sleep", 0.2),
        ("exec", (20, 20)),
        ("sleep", 0.2),
        ("exec", (30, 30)),
    ]


def test_execute_matches_only_refocuses_on_last_match(monkeypatch):
    """Regression: refocus_mode must fire ONCE at the end of the
    match sequence, not on every match. A user with three Yes-
    buttons cleared in one tick should see one final refocus event
    targeting their typing window, not three."""
    seen_modes: list = []

    def spy_execute_match(match, refocus_mode=None):
        seen_modes.append(refocus_mode)

    monkeypatch.setattr(press_engine, "execute_match", spy_execute_match)
    monkeypatch.setattr(press_engine.time, "sleep", lambda _: None)

    press_engine.execute_matches(
        [
            {"center": (10, 10)},
            {"center": (20, 20)},
            {"center": (30, 30)},
        ],
        delay_seconds=0.0,
        refocus_mode="ctrl_tab",
    )

    # Only the final iteration receives the active mode.
    assert seen_modes == [None, None, "ctrl_tab"]


def test_execute_matches_refocus_off_passes_none_throughout(monkeypatch):
    """With the dropdown set to "off" / None, no match should
    receive a non-None refocus_mode regardless of position."""
    seen_modes: list = []
    monkeypatch.setattr(
        press_engine,
        "execute_match",
        lambda match, refocus_mode=None: seen_modes.append(refocus_mode),
    )
    monkeypatch.setattr(press_engine.time, "sleep", lambda _: None)

    press_engine.execute_matches(
        [{"center": (10, 10)}, {"center": (20, 20)}],
        refocus_mode=None,
    )
    assert seen_modes == [None, None]


def test_legacy_refocus_after_click_bool_migrates_to_mode(tmp_path, monkeypatch):
    """A config carrying the legacy ``refocus_after_click: True``
    boolean (pre-enum era) must load as ``refocus_mode == "click"``
    so existing users keep their refocus behaviour on upgrade."""
    import press_store

    monkeypatch.setattr(press_store, "TEMPLATES_DIR", tmp_path)
    monkeypatch.setattr(press_store, "CONFIG_PATH", tmp_path / "config.json")
    cfg = press_store.normalize_config({"refocus_after_click": True})
    assert cfg["refocus_mode"] == "click"
    assert "refocus_after_click" not in cfg  # legacy key dropped

    cfg = press_store.normalize_config({"refocus_after_click": False})
    assert cfg["refocus_mode"] == "off"

    cfg = press_store.normalize_config({"refocus_mode": "ctrl_tab"})
    assert cfg["refocus_mode"] == "ctrl_tab"

    cfg = press_store.normalize_config({"refocus_mode": "garbage"})
    assert cfg["refocus_mode"] == "off"  # invalid → safe default
