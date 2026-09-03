from dataclasses import dataclass
from typing import Protocol


AGENT_SURFACE_NAMES = ("sidebar", "conversation", "composer")

# Useful immediately after upgrading, while still making calibration the
# authoritative path. These match the standard Codex desktop layout and are
# deliberately backend-specific rather than leaking percentages into UI code.
DEFAULT_CODEX_SURFACES = {
    "sidebar": [0.0, 0.0, 0.22, 1.0],
    "conversation": [0.22, 0.04, 0.78, 0.80],
    "composer": [0.30, 0.82, 0.60, 0.14],
}


@dataclass(frozen=True)
class BackendEvaluation:
    ready: bool
    asking: bool = False
    score: float = 0.0
    ready_count: int = 0
    marker_centers: tuple[tuple[int, int], ...] = ()


class DesktopBackend(Protocol):
    id: str
    label: str

    def evaluate(self, rgb) -> BackendEvaluation: ...
    def transform_click(self, rgb, requested_xy: tuple[int, int]) -> tuple[int, int]: ...
    def send_target(self, region: list[int]) -> tuple[int, int]: ...
    def scroll_target(self, region: list[int]) -> tuple[int, int]: ...


def backend_for_id(backend_id):
    if backend_id == "codex_desktop":
        from press_backend_codex import CodexDesktopBackend
        return CodexDesktopBackend()
    return None


def _normalized_window_region(window: dict):
    region = window.get("region")
    if not isinstance(region, (list, tuple)) or len(region) != 4:
        raise ValueError("backend region must have four integer values")
    try:
        normalized = [int(value) for value in region]
    except (TypeError, ValueError):
        raise ValueError("backend region must have four integer values") from None
    if normalized[2] <= 0 or normalized[3] <= 0:
        raise ValueError("backend region must have positive width and height")
    return normalized


def _backend_with_region(window: dict):
    backend = backend_for_id(window.get("backend"))
    if backend is None:
        return None, None
    normalized = _normalized_window_region(window)
    return backend, normalized


def _coerce_fractional_region(value):
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x, y, width, height = [float(part) for part in value]
    except (TypeError, ValueError):
        return None
    if x < 0 or y < 0 or width <= 0 or height <= 0:
        return None
    if x + width > 1.000001 or y + height > 1.000001:
        return None
    return x, y, width, height


def _fractional_surface(window: dict, surface: str):
    if surface not in AGENT_SURFACE_NAMES:
        raise ValueError(f"unknown agent surface: {surface}")
    configured = window.get("agent_surfaces")
    value = configured.get(surface) if isinstance(configured, dict) else None
    if value is None and window.get("backend") == "codex_desktop":
        value = DEFAULT_CODEX_SURFACES[surface]
    return _coerce_fractional_region(value)


def agent_window_configured(window: dict) -> bool:
    configured = window.get("agent_surfaces")
    return isinstance(configured, dict) and all(
        _coerce_fractional_region(configured.get(name)) is not None
        for name in AGENT_SURFACE_NAMES
    )


def resolve_surface_region(window: dict, surface: str) -> list[int] | None:
    """Resolve a normalized agent surface to an absolute screen rectangle."""
    _backend, region = _backend_with_region(window)
    if region is None:
        return None
    fractional = _fractional_surface(window, surface)
    if fractional is None:
        return None
    x, y, width, height = region
    sx, sy, sw, sh = fractional
    return [
        x + int(round(sx * width)),
        y + int(round(sy * height)),
        max(1, int(round(sw * width))),
        max(1, int(round(sh * height))),
    ]


def normalize_surface_capture(window: dict, capture: list[int]) -> list[float]:
    """Clip an absolute drag capture to a window and normalize it."""
    region = _normalized_window_region(window)
    if not isinstance(capture, (list, tuple)) or len(capture) != 4:
        raise ValueError("surface capture must have four integer values")
    try:
        cx, cy, cw, ch = [int(value) for value in capture]
    except (TypeError, ValueError):
        raise ValueError("surface capture must have four integer values") from None
    if cw <= 0 or ch <= 0:
        raise ValueError("surface capture must have positive width and height")
    wx, wy, ww, wh = region
    left = max(wx, cx)
    top = max(wy, cy)
    right = min(wx + ww, cx + cw)
    bottom = min(wy + wh, cy + ch)
    if right <= left or bottom <= top:
        raise ValueError("surface capture must overlap the target window")
    return [
        round((left - wx) / ww, 6),
        round((top - wy) / wh, 6),
        round((right - left) / ww, 6),
        round((bottom - top) / wh, 6),
    ]


def backend_click_target(
    window: dict,
    x_frac: float,
    y_frac: float,
    rgb,
    surface: str | None = None,
):
    """Return a backend-adjusted click point in absolute screen pixels."""
    backend, region = _backend_with_region(window)
    if backend is None:
        return None
    x, y, width, height = region
    base = resolve_surface_region(window, surface) if surface else region
    if base is None:
        raise ValueError(f"agent surface is not available: {surface}")
    bx, by, bw, bh = base
    local = (
        bx - x + int(round(x_frac * bw)),
        by - y + int(round(y_frac * bh)),
    )
    target_x, target_y = backend.transform_click(rgb, local)
    return x + target_x, y + target_y


def backend_scroll_target(window: dict):
    """Return the backend's absolute scroll focus point, if it owns one."""
    backend, region = _backend_with_region(window)
    if backend is None:
        return None
    surface = resolve_surface_region(window, "conversation")
    return backend.scroll_target(surface or region)


def backend_send_target(window: dict):
    """Return the backend's absolute composer point, if it owns one."""
    backend, region = _backend_with_region(window)
    if backend is None:
        return None
    surface = resolve_surface_region(window, "composer")
    return backend.send_target(surface or region)
