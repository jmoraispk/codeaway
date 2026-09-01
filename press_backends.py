from dataclasses import dataclass
from typing import Protocol


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


def _backend_with_region(window: dict):
    backend = backend_for_id(window.get("backend"))
    region = window.get("region")
    if backend is None or not isinstance(region, (list, tuple)) or len(region) != 4:
        return None, None
    try:
        return backend, [int(value) for value in region]
    except (TypeError, ValueError):
        return None, None


def backend_click_target(window: dict, x_frac: float, y_frac: float, rgb):
    """Return a backend-adjusted click point in absolute screen pixels."""
    backend, region = _backend_with_region(window)
    if backend is None:
        return None
    x, y, width, height = region
    local = (int(round(x_frac * width)), int(round(y_frac * height)))
    target_x, target_y = backend.transform_click(rgb, local)
    return x + target_x, y + target_y


def backend_scroll_target(window: dict):
    """Return the backend's absolute scroll focus point, if it owns one."""
    backend, region = _backend_with_region(window)
    if backend is None:
        return None
    return backend.scroll_target(region)


def backend_send_target(window: dict):
    """Return the backend's absolute composer point, if it owns one."""
    backend, region = _backend_with_region(window)
    if backend is None:
        return None
    return backend.send_target(region)
