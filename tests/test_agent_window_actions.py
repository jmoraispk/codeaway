from types import SimpleNamespace

import numpy as np
import pytest


def test_codex_scroll_activates_window_and_uses_wheel(monkeypatch):
    import press_core
    import press_ui

    events = []
    fake = SimpleNamespace(
        _bridge_activate_window=lambda window: events.append(
            ("activate", window["hwnd"])
        )
    )
    monkeypatch.setattr(
        press_core,
        "focus_and_scroll",
        lambda point, amount: events.append(("wheel", point, amount)),
    )

    press_ui.MainWindow._bridge_perform_window_scroll(
        fake,
        {
            "hwnd": 77,
            "backend": "codex_desktop",
            "region": [100, 200, 1000, 800],
            "agent_surfaces": {
                "sidebar": [0.0, 0.0, 0.2, 1.0],
                "conversation": [0.2, 0.1, 0.8, 0.6],
                "composer": [0.3, 0.8, 0.6, 0.2],
            },
        },
        6,
        {},
    )

    assert events == [("activate", 77), ("wheel", (700, 520), 6)]


def test_codex_click_activates_before_click_and_returns_target(monkeypatch):
    import press_core
    import press_ui

    events = []
    fake = SimpleNamespace(
        _bridge_activate_window=lambda window: events.append(
            ("activate", window["hwnd"])
        )
    )
    monkeypatch.setattr(
        press_ui,
        "capture_screen_rgb",
        lambda region: np.full((800, 1000, 3), 24, dtype=np.uint8),
    )
    monkeypatch.setattr(
        press_core, "click_point", lambda point: events.append(("click", point))
    )
    window = {
        "hwnd": 88,
        "backend": "codex_desktop",
        "region": [100, 200, 1000, 800],
    }

    target = press_ui.MainWindow._bridge_perform_window_click_at(
        fake, window, 0.5, 0.25, {}
    )

    assert target == (600, 400)
    assert events == [("activate", 88), ("click", (600, 400))]


def test_failed_codex_activation_stops_action(monkeypatch):
    import press_windows
    import press_ui

    monkeypatch.setattr(press_windows, "activate_window", lambda hwnd: False)
    fake = SimpleNamespace()

    with pytest.raises(RuntimeError, match="could not activate Codex"):
        press_ui.MainWindow._bridge_activate_window(
            fake,
            {"hwnd": 99, "backend": "codex_desktop", "name": "Codex"},
        )


def test_focus_and_scroll_paces_large_wheel_gestures(monkeypatch):
    import press_core

    clicks = []
    wheels = []
    monkeypatch.setattr(press_core, "_pin_thread_v2_dpi", lambda: None)
    monkeypatch.setattr(press_core, "_click_at_target", lambda x, y: clicks.append((x, y)))
    monkeypatch.setattr(press_core.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(
        press_core.pyautogui,
        "scroll",
        lambda amount, x, y: wheels.append((amount, x, y)),
    )

    press_core.focus_and_scroll((500, 600), -8)

    assert clicks == [(500, 600)]
    assert wheels == [(-3, 500, 600), (-3, 500, 600), (-2, 500, 600)]


def test_codex_navigator_action_activates_before_uia_invoke(monkeypatch):
    import press_codex_accessibility
    import press_ui

    events = []
    fake = SimpleNamespace(
        _bridge_activate_window=lambda window: events.append(("activate", window["hwnd"]))
    )
    monkeypatch.setattr(
        press_codex_accessibility,
        "perform_codex_navigator_action",
        lambda window, action: events.append(("invoke", action["title"]))
        or {"acted": True},
    )

    result = press_ui.MainWindow._bridge_codex_navigator_action(
        fake,
        {"hwnd": 77, "backend": "codex_desktop"},
        {"kind": "task", "project": "AutoPress", "title": "Ship it"},
    )

    assert result == {"acted": True}
    assert events == [("activate", 77), ("invoke", "Ship it")]
