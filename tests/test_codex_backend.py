import numpy as np

from press_backend_codex import CodexDesktopBackend


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
