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
        lambda mode, center, text_before_enter=None: called.update(
            mode=mode, center=center, text=text_before_enter
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
    }


def test_execute_matches_waits_between_actions(monkeypatch):
    calls = []

    monkeypatch.setattr(
        press_engine,
        "execute_match",
        lambda match: calls.append(("exec", match["center"])),
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
