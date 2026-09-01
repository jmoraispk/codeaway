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
