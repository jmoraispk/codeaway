import press_engine


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
        lambda mode, center, text_before_enter=None, refocus_after_click=False: called.update(
            mode=mode, center=center, text=text_before_enter, refocus=refocus_after_click
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
        "refocus": False,
    }


def test_execute_matches_waits_between_actions(monkeypatch):
    calls = []

    monkeypatch.setattr(
        press_engine,
        "execute_match",
        lambda match, refocus_after_click=False: calls.append(("exec", match["center"])),
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
