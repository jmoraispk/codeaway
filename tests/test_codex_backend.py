import numpy as np
import pytest

from press_backend_codex import CodexDesktopBackend
from press_backends import backend_click_target, backend_scroll_target, backend_send_target


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


def test_action_targets_use_output_and_composer():
    backend = CodexDesktopBackend()
    assert backend.scroll_target([100, 200, 1000, 800]) == (700, 560)
    assert backend.send_target([100, 200, 1000, 800]) == (700, 936)


def test_backend_click_target_translates_codex_local_result_to_screen_pixels():
    rgb = frame()
    rgb[300:307, 198:205] = (47, 129, 247)
    window = {"backend": "codex_desktop", "region": [100, 200, 1000, 800]}
    assert backend_click_target(window, 0.202, 0.38, rgb) == (276, 503)


def test_backend_action_targets_return_absolute_codex_points():
    window = {"backend": "codex_desktop", "region": [100, 200, 1000, 800]}
    assert backend_scroll_target(window) == (700, 560)
    assert backend_send_target(window) == (700, 936)


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
