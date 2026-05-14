"""Windows per-monitor DPI helpers.

Returns the *scale factor* (1.0 = 100%, 1.25 = 125%, 1.5 = 150%, …) for
the monitor containing any point or window region. Used by template
matching so we can pick a pre-scaled template variant that matches the
target window's actual pixel density — a Cursor idle indicator
rendered on a 150 % monitor is 1.5× the pixel size of the same
indicator on a 100 % monitor, and a single template can't match both.

Cross-platform: returns 1.0 (no scaling) on non-Windows so callers can
just multiply through and ignore platform branching.
"""

from __future__ import annotations

import logging
import sys
from typing import Optional, Tuple

LOG = logging.getLogger("press_dpi")

IS_WINDOWS = sys.platform.startswith("win")

# Common Windows display-scaling presets. These are the values the
# Windows "Scale and layout" dropdown exposes; any monitor in the
# wild will be set to one of these unless the user typed a custom
# value (rare). We pre-generate template variants for each so the
# matching loop can pick exactly the right one at runtime.
SUPPORTED_SCALES: tuple[float, ...] = (1.0, 1.25, 1.5, 1.75, 2.0)


if IS_WINDOWS:
    import ctypes
    from ctypes import byref, c_int, c_uint, c_void_p, c_wchar
    from ctypes.wintypes import BOOL, DWORD, HMONITOR, POINT, RECT

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shcore = ctypes.WinDLL("shcore", use_last_error=True)

    _MonitorFromPoint = _user32.MonitorFromPoint
    _MonitorFromPoint.argtypes = [POINT, c_uint]
    _MonitorFromPoint.restype = HMONITOR

    _MonitorFromRect = _user32.MonitorFromRect
    _MonitorFromRect.argtypes = [ctypes.POINTER(RECT), c_uint]
    _MonitorFromRect.restype = HMONITOR

    _GetDpiForMonitor = _shcore.GetDpiForMonitor
    _GetDpiForMonitor.argtypes = [HMONITOR, c_uint, ctypes.POINTER(c_uint), ctypes.POINTER(c_uint)]
    _GetDpiForMonitor.restype = c_int

    class _MONITORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", DWORD),
            ("rcMonitor", RECT),
            ("rcWork", RECT),
            ("dwFlags", DWORD),
        ]

    _GetMonitorInfoW = _user32.GetMonitorInfoW
    _GetMonitorInfoW.argtypes = [HMONITOR, ctypes.POINTER(_MONITORINFO)]
    _GetMonitorInfoW.restype = BOOL

    # MonitorFromPoint flag — "use the nearest monitor if the point is
    # outside any monitor's bounds". Saves an extra branch for
    # negative coords on the left of the primary.
    _MONITOR_DEFAULTTONEAREST = 0x00000002
    # GetDpiForMonitor mode — "effective DPI" includes the user's
    # scale-and-layout choice; "raw DPI" is the hardware DPI without
    # scaling. We want effective.
    _MDT_EFFECTIVE_DPI = 0


def _round_to_supported(scale: float) -> float:
    """Snap a measured scale to the nearest supported preset, so a
    rounding glitch (e.g. 1.499 instead of 1.5) doesn't push us to a
    nonexistent template variant. Falls through unmodified if the
    measurement is more than 5 % off any preset — that's a genuinely
    custom scale and we should treat it as such."""
    best = min(SUPPORTED_SCALES, key=lambda s: abs(s - scale))
    if abs(best - scale) <= 0.05:
        return best
    return scale


def scale_for_point(x: int, y: int) -> float:
    """Scale factor for the monitor containing (x, y) in physical
    screen coords. Returns 1.0 on non-Windows or if the API call
    fails (sane fallback — treats every monitor as 100 %)."""
    if not IS_WINDOWS:
        return 1.0
    try:
        pt = POINT(int(x), int(y))
        hmon = _MonitorFromPoint(pt, _MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return 1.0
        dpi_x = c_uint(96)
        dpi_y = c_uint(96)
        hr = _GetDpiForMonitor(hmon, _MDT_EFFECTIVE_DPI, byref(dpi_x), byref(dpi_y))
        if hr != 0:
            LOG.warning("GetDpiForMonitor failed: 0x%08x", hr & 0xFFFFFFFF)
            return 1.0
        return _round_to_supported(dpi_x.value / 96.0)
    except Exception as exc:
        LOG.warning("scale_for_point failed: %s", exc)
        return 1.0


def scale_for_region(region: Optional[list | tuple]) -> float:
    """Scale factor for the monitor containing the centre of
    ``region`` (a [x, y, w, h] bbox). Returns 1.0 on a missing /
    malformed region — the caller can treat that as "no scaling
    needed", which is what every legacy code path expected."""
    if not region or len(region) != 4:
        return 1.0
    try:
        x, y, w, h = (int(v) for v in region)
    except (TypeError, ValueError):
        return 1.0
    return scale_for_point(x + w // 2, y + h // 2)


def scale_tag(scale: float) -> str:
    """Stable filename suffix for a scale factor — '100', '125',
    '150', etc. Used by the template-variant naming convention
    so callers don't have to format floats consistently."""
    return f"{int(round(scale * 100))}"


def monitor_info_for_point(x: int, y: int) -> Optional[dict]:
    """Bounding rect + scale for the monitor containing (x, y).

    Returns ``{"rect": (x, y, w, h), "scale": float, "key": tuple}``
    where ``rect`` is the monitor's physical bounding box in screen
    coords (suitable for ``capture_screen_rgb``) and ``key`` is a
    hashable tuple usable as a dict key for grouping windows by
    monitor. ``None`` on non-Windows or API failure — caller should
    fall back to per-window capture in that case.
    """
    if not IS_WINDOWS:
        return None
    try:
        pt = POINT(int(x), int(y))
        hmon = _MonitorFromPoint(pt, _MONITOR_DEFAULTTONEAREST)
        if not hmon:
            return None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _GetMonitorInfoW(hmon, byref(info)):
            return None
        rc = info.rcMonitor
        rect = (
            int(rc.left),
            int(rc.top),
            int(rc.right - rc.left),
            int(rc.bottom - rc.top),
        )
        dpi_x = c_uint(96)
        dpi_y = c_uint(96)
        scale = 1.0
        if _GetDpiForMonitor(hmon, _MDT_EFFECTIVE_DPI, byref(dpi_x), byref(dpi_y)) == 0:
            scale = _round_to_supported(dpi_x.value / 96.0)
        return {"rect": rect, "scale": scale, "key": rect}
    except Exception as exc:
        LOG.warning("monitor_info_for_point failed: %s", exc)
        return None


def monitor_info_for_region(region: Optional[list | tuple]) -> Optional[dict]:
    """Same as ``monitor_info_for_point`` but takes a [x, y, w, h]
    bbox and uses its centre."""
    if not region or len(region) != 4:
        return None
    try:
        x, y, w, h = (int(v) for v in region)
    except (TypeError, ValueError):
        return None
    return monitor_info_for_point(x + w // 2, y + h // 2)


def all_monitor_scales() -> Tuple[float, ...]:
    """Distinct DPI scales across all attached monitors, deduped and
    sorted. Useful for the test/verify flow: "make sure variants
    exist for every scale the user actually has". On non-Windows
    returns ``(1.0,)``."""
    if not IS_WINDOWS:
        return (1.0,)
    try:
        # EnumDisplayMonitors callback fills a set; deduping happens
        # automatically.
        scales: set[float] = set()
        MONITORENUMPROC = ctypes.WINFUNCTYPE(
            c_int, HMONITOR, c_void_p, ctypes.POINTER(RECT), c_void_p
        )

        def _cb(hmon, hdc, lprc, lparam):
            try:
                dpi_x = c_uint(96)
                dpi_y = c_uint(96)
                if _GetDpiForMonitor(hmon, _MDT_EFFECTIVE_DPI, byref(dpi_x), byref(dpi_y)) == 0:
                    scales.add(_round_to_supported(dpi_x.value / 96.0))
            except Exception:
                pass
            return 1  # continue enumeration

        _user32.EnumDisplayMonitors(None, None, MONITORENUMPROC(_cb), 0)
        if not scales:
            return (1.0,)
        return tuple(sorted(scales))
    except Exception as exc:
        LOG.warning("all_monitor_scales failed: %s", exc)
        return (1.0,)
