import numpy as np
import pytest

from press_backend_codex import CodexDesktopBackend
from press_backends import (
    agent_window_configured,
    backend_click_target,
    backend_scroll_target,
    backend_send_target,
    normalize_surface_capture,
    resolve_surface_region,
)


def frame(width=1000, height=800):
    return np.full((height, width, 3), 24, dtype=np.uint8)


def test_detects_only_compact_blue_markers_in_sidebar_band():
    rgb = frame()
    rgb[300:307, 198:205] = (47, 129, 247)
    rgb[300:307, 30:37] = (47, 129, 247)
    rgb[40:47, 198:205] = (47, 129, 247)

    result = CodexDesktopBackend().evaluate(rgb)

    assert result.ready is True
    assert result.ready_count == 1
    assert result.marker_centers == ((201, 303),)


def test_reports_not_ready_without_blue_markers():
    result = CodexDesktopBackend().evaluate(frame())

    assert result.ready is False
    assert result.ready_count == 0


def test_marker_click_is_shifted_left():
    rgb = frame()
    rgb[300:307, 198:205] = (47, 129, 247)
    assert CodexDesktopBackend().transform_click(rgb, (202, 304)) == (176, 303)


def test_non_marker_click_is_unchanged():
    assert CodexDesktopBackend().transform_click(frame(), (700, 400)) == (700, 400)


def test_marker_band_miss_still_shifts_left():
    assert CodexDesktopBackend().transform_click(frame(), (200, 500)) == (175, 500)


def test_backend_action_targets_center_the_supplied_surface():
    backend = CodexDesktopBackend()
    assert backend.scroll_target([100, 200, 1000, 800]) == (600, 600)
    assert backend.send_target([100, 200, 1000, 800]) == (600, 600)


def test_backend_click_target_translates_codex_local_result_to_screen_pixels():
    rgb = frame()
    rgb[300:307, 198:205] = (47, 129, 247)
    window = {"backend": "codex_desktop", "region": [100, 200, 1000, 800]}
    assert backend_click_target(window, 0.202, 0.38, rgb) == (276, 503)


def test_backend_action_targets_return_absolute_codex_points():
    window = {"backend": "codex_desktop", "region": [100, 200, 1000, 800]}
    assert backend_scroll_target(window) == (710, 552)
    assert backend_send_target(window) == (700, 912)


def test_calibrated_surfaces_drive_capture_scroll_and_send_targets():
    window = {
        "backend": "codex_desktop",
        "region": [100, 200, 1000, 800],
        "agent_surfaces": {
            "sidebar": [0.0, 0.0, 0.2, 1.0],
            "conversation": [0.25, 0.1, 0.70, 0.65],
            "composer": [0.35, 0.82, 0.50, 0.12],
        },
    }

    assert agent_window_configured(window) is True
    assert resolve_surface_region(window, "sidebar") == [100, 200, 200, 800]
    assert resolve_surface_region(window, "conversation") == [350, 280, 700, 520]
    assert backend_scroll_target(window) == (700, 540)
    assert backend_send_target(window) == (700, 904)


def test_surface_click_maps_back_to_full_window_before_codex_safety_transform():
    rgb = frame()
    window = {
        "backend": "codex_desktop",
        "region": [100, 200, 1000, 800],
        "agent_surfaces": {
            "sidebar": [0.0, 0.0, 0.22, 1.0],
            "conversation": None,
            "composer": None,
        },
    }

    assert backend_click_target(
        window, 0.5, 0.25, rgb, surface="sidebar"
    ) == (210, 400)


def test_absolute_capture_normalizes_and_clips_to_window():
    window = {"region": [100, 200, 1000, 800]}

    assert normalize_surface_capture(window, [50, 100, 300, 400]) == [
        0.0,
        0.0,
        0.25,
        0.375,
    ]


def test_uncalibrated_codex_uses_v2_default_surfaces():
    window = {
        "backend": "codex_desktop",
        "region": [100, 200, 1000, 800],
        "agent_surfaces": {},
    }

    assert agent_window_configured(window) is False
    assert resolve_surface_region(window, "sidebar") == [100, 200, 220, 800]
    assert backend_scroll_target(window) == (710, 552)
    assert backend_send_target(window) == (700, 912)


def test_backend_action_targets_defer_legacy_windows():
    window = {"backend": "cursor", "region": [100, 200, 1000, 800]}
    assert backend_click_target(window, 0.5, 0.5, frame()) is None
    assert backend_scroll_target(window) is None
    assert backend_send_target(window) is None


@pytest.mark.parametrize(
    "region",
    ([100, 200, 0, 800], [100, 200, 1000, -1]),
)
def test_backend_action_targets_reject_non_positive_codex_extents(region):
    window = {"backend": "codex_desktop", "region": region}

    with pytest.raises(ValueError, match="positive width and height"):
        backend_click_target(window, 0.5, 0.5, None)
    with pytest.raises(ValueError, match="positive width and height"):
        backend_scroll_target(window)
    with pytest.raises(ValueError, match="positive width and height"):
        backend_send_target(window)


def test_backend_action_targets_reject_malformed_codex_regions():
    window = {"backend": "codex_desktop", "region": [100, 200, "wide", 800]}

    with pytest.raises(ValueError, match="four integer values"):
        backend_click_target(window, 0.5, 0.5, None)
    with pytest.raises(ValueError, match="four integer values"):
        backend_scroll_target(window)
    with pytest.raises(ValueError, match="four integer values"):
        backend_send_target(window)
