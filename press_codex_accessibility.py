"""Structured Codex desktop navigation through Windows UI Automation.

The Codex Windows app is Chromium-based, but it exposes a useful UIA tree:
project rows support ExpandCollapse, task rows support Invoke, and both expose
stable names and screen bounds.  This module turns that tree into a small JSON
model for the phone bridge.  A narrow pixel sample is used only for the blue
"done" marker because Chromium currently exposes that dot without a name.

No Codex service or SSH connection is involved.  Everything here talks to the
visible Codex window on the user's laptop.
"""

from __future__ import annotations

import ctypes
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import numpy as np


PROJECT_CLASS_MARKER = "group/folder-row"
TASK_CLASS_MARKER = "py-row-y"
WORKTREE_CLASS_MARKERS = ("icon-2xs", "text-codex-description", "no-drag", "shrink-0")
BUSY_CLASS_MARKERS = ("icon-xs", "shrink-0")
PROJECT_ACTION_PREFIX = "Project actions for "
PROJECT_NEW_CHAT_PREFIX = "Start new chat in "
_UIA_LOCK = threading.RLock()


class CodexAccessibilityError(RuntimeError):
    """The Codex accessibility tree could not be read or acted on."""


@dataclass
class _Node:
    control_type: str
    name: str
    class_name: str
    rect: tuple[int, int, int, int]
    depth: int = 0
    control: Any = None

    @property
    def x(self) -> int:
        return self.rect[0]

    @property
    def y(self) -> int:
        return self.rect[1]

    @property
    def width(self) -> int:
        return self.rect[2]

    @property
    def height(self) -> int:
        return self.rect[3]

    @property
    def center_y(self) -> float:
        return self.y + self.height / 2


def _require_windows_uia():
    if not sys.platform.startswith("win"):
        raise CodexAccessibilityError("Codex accessibility is available only on Windows")
    try:
        import uiautomation as auto  # type: ignore
    except Exception as exc:  # pragma: no cover - depends on host installation
        raise CodexAccessibilityError(
            "Windows UI Automation support is not installed; run `uv sync`"
        ) from exc
    return auto


def _rect_tuple(rect: Any) -> tuple[int, int, int, int]:
    left = int(round(float(rect.left)))
    top = int(round(float(rect.top)))
    right = int(round(float(rect.right)))
    bottom = int(round(float(rect.bottom)))
    return (left, top, max(0, right - left), max(0, bottom - top))


def _walk_nodes(hwnd: int) -> list[_Node]:
    with _UIA_LOCK:
        auto = _require_windows_uia()
        try:
            root = auto.ControlFromHandle(int(hwnd))
            if not root or not root.Exists(0):
                raise CodexAccessibilityError("Codex window is no longer available")
            walked = auto.WalkTree(
                root,
                getChildren=lambda control: control.GetChildren(),
                includeTop=False,
                maxDepth=40,
            )
            nodes: list[_Node] = []
            for item in walked:
                control, depth = item[0], int(item[1])
                try:
                    if control.IsOffscreen:
                        continue
                    rect = _rect_tuple(control.BoundingRectangle)
                    if rect[2] <= 0 or rect[3] <= 0:
                        continue
                    nodes.append(
                        _Node(
                            control_type=str(control.ControlTypeName or ""),
                            name=str(control.Name or ""),
                            class_name=str(control.ClassName or ""),
                            rect=rect,
                            depth=depth,
                            control=control,
                        )
                    )
                except Exception:
                    # Chromium can discard a row while the tree is being walked.
                    # A single stale element should not blank the whole navigator.
                    continue
            return nodes
        except CodexAccessibilityError:
            raise
        except Exception as exc:
            raise CodexAccessibilityError(f"could not inspect Codex: {exc}") from exc


def _inside_region(node: _Node, region: tuple[int, int, int, int]) -> bool:
    x, y, width, height = region
    center_x = node.x + node.width / 2
    center_y = node.center_y
    return x <= center_x <= x + width and y <= center_y <= y + height


def _row_nodes(nodes: Iterable[_Node], row: _Node) -> list[_Node]:
    top = row.y
    bottom = row.y + row.height
    return [node for node in nodes if top <= node.center_y <= bottom]


def _class_has(class_name: str, markers: Iterable[str]) -> bool:
    tokens = set(class_name.split())
    return all(marker in tokens for marker in markers)


def _project_identity(project: _Node, row_nodes: Iterable[_Node]) -> tuple[str, Optional[str]]:
    project_name = project.name.strip()
    for node in row_nodes:
        if node.control_type != "ButtonControl":
            continue
        prefix = next(
            (
                candidate_prefix
                for candidate_prefix in (PROJECT_ACTION_PREFIX, PROJECT_NEW_CHAT_PREFIX)
                if node.name.startswith(candidate_prefix)
            ),
            None,
        )
        if prefix:
            candidate = node.name[len(prefix) :].strip()
            if candidate:
                project_name = candidate
                break
    host = None
    folder_label = project.name.strip()
    if project_name and folder_label.startswith(project_name):
        suffix = folder_label[len(project_name) :].strip()
        host = suffix or None
    return project_name or folder_label, host


def _expand_state(node: _Node) -> bool:
    try:
        # UIA ExpandCollapseState: 0 collapsed, 1 expanded.
        return int(node.control.GetExpandCollapsePattern().ExpandCollapseState) == 1
    except Exception:
        return False


def _is_foreground(hwnd: int) -> bool:
    if not sys.platform.startswith("win"):
        return False
    try:
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        return int(user32.GetForegroundWindow() or 0) == int(hwnd)
    except Exception:
        return False


def _capture_sidebar(
    window: dict,
) -> tuple[Optional[np.ndarray], Optional[tuple[int, int, int, int]]]:
    """Capture pixels only while Codex is already foreground.

    Navigator polling must never steal focus from another desktop app.  UIA
    remains useful in the background; only the unnamed blue completion dot is
    reported as unknown until Codex is visible again.
    """
    hwnd = window.get("hwnd")
    if hwnd is None or not _is_foreground(int(hwnd)):
        return None, None
    try:
        from press_backends import resolve_surface_region
        from press_engine import capture_screen_rgb

        region = resolve_surface_region(window, "sidebar")
        if region is None:
            return None, None
        return capture_screen_rgb(tuple(region)), tuple(region)
    except Exception:
        return None, None


def _has_blue_done_marker(
    task: _Node,
    sidebar_rgb: Optional[np.ndarray],
    sidebar_region: Optional[tuple[int, int, int, int]],
) -> bool:
    if sidebar_rgb is None or sidebar_region is None or sidebar_rgb.ndim != 3:
        return False
    sx, sy, sw, sh = sidebar_region
    # Completion dots live in the final ~48 physical pixels of a task row.
    # Sample a little wider for DPI/theme variations, but never leave the row.
    x0 = max(task.x, sx + sw - 64) - sx
    x1 = min(task.x + task.width, sx + sw) - sx
    y0 = max(task.y, sy) - sy
    y1 = min(task.y + task.height, sy + sh) - sy
    x0, y0 = max(0, int(x0)), max(0, int(y0))
    x1 = min(sidebar_rgb.shape[1], int(x1))
    y1 = min(sidebar_rgb.shape[0], int(y1))
    if x1 <= x0 or y1 <= y0:
        return False
    crop = sidebar_rgb[y0:y1, x0:x1, :3].astype(np.int16, copy=False)
    red, green, blue = crop[..., 0], crop[..., 1], crop[..., 2]
    # Codex's blue marker is highly saturated.  The relaxed green bound keeps
    # anti-aliased edge pixels while excluding white/gray spinner pixels.
    mask = (blue >= 150) & (blue >= red + 45) & (blue >= green + 20)
    return int(np.count_nonzero(mask)) >= 6


def build_navigator_model(
    nodes: list[_Node],
    sidebar_region: tuple[int, int, int, int],
    sidebar_rgb: Optional[np.ndarray] = None,
) -> dict:
    """Pure-ish UIA-node to phone JSON transformation.

    ``nodes`` may contain live controls for actions, but the returned model is
    JSON-safe.  Geometry assigns each visible task to the closest project row
    above it, matching Codex's rendered sidebar order.
    """
    visible = [node for node in nodes if _inside_region(node, sidebar_region)]
    project_nodes = sorted(
        (
            node
            for node in visible
            if node.control_type == "ButtonControl"
            and PROJECT_CLASS_MARKER in node.class_name
        ),
        key=lambda node: (node.y, node.x),
    )
    task_nodes = sorted(
        (
            node
            for node in visible
            if node.control_type == "ButtonControl"
            and TASK_CLASS_MARKER in node.class_name
            and node.name not in {"Pin chat", "Archive chat"}
        ),
        key=lambda node: (node.y, node.x),
    )

    projects: list[dict] = []
    for index, project in enumerate(project_nodes):
        next_y = project_nodes[index + 1].y if index + 1 < len(project_nodes) else 10**9
        row = _row_nodes(visible, project)
        project_name, host = _project_identity(project, _row_nodes(nodes, project))
        connected = any(
            node.control_type == "ImageControl"
            and (node.name == "Connected" or "text-chart-green" in node.class_name)
            for node in row
        )
        project_busy = any(
            node.control_type == "ImageControl"
            and _class_has(node.class_name, BUSY_CLASS_MARKERS)
            for node in row
        )
        project_tasks: list[dict] = []
        for task in task_nodes:
            if not (project.y + project.height <= task.center_y < next_y):
                continue
            task_row = _row_nodes(visible, task)
            worktree = any(
                node.control_type == "ImageControl"
                and _class_has(node.class_name, WORKTREE_CLASS_MARKERS)
                for node in task_row
            )
            busy = any(
                node.control_type == "ImageControl"
                and _class_has(node.class_name, BUSY_CLASS_MARKERS)
                for node in task_row
            )
            done = _has_blue_done_marker(task, sidebar_rgb, sidebar_region)
            class_tokens = set(task.class_name.split())
            if done:
                task_state = "done"
            elif busy:
                task_state = "busy"
            elif sidebar_rgb is not None:
                task_state = "idle"
            else:
                task_state = "unknown"
            project_tasks.append(
                {
                    "title": task.name.strip(),
                    "state": task_state,
                    "worktree": worktree,
                    "selected": "bg-primary-ghost-hover" in class_tokens,
                }
            )
        projects.append(
            {
                "name": project_name,
                "host": host,
                "connected": connected,
                "state": "connected" if connected else "busy" if project_busy else "idle",
                "expanded": _expand_state(project),
                "tasks": project_tasks,
            }
        )
    return {
        "available": bool(projects),
        "source": "windows_uia",
        "projects": projects,
        "captured_at": datetime.now(timezone.utc).isoformat(),
    }


def read_codex_navigator(
    window: dict,
    *,
    nodes: Optional[list[_Node]] = None,
    sidebar_capture: Optional[
        tuple[Optional[np.ndarray], Optional[tuple[int, int, int, int]]]
    ] = None,
) -> dict:
    if window.get("backend") != "codex_desktop":
        raise CodexAccessibilityError("navigator is available only for Codex windows")
    hwnd = window.get("hwnd")
    if hwnd is None:
        raise CodexAccessibilityError("Codex target has no window handle")
    try:
        from press_backends import resolve_surface_region

        sidebar_region = resolve_surface_region(window, "sidebar")
    except Exception as exc:
        raise CodexAccessibilityError(f"sidebar region is unavailable: {exc}") from exc
    if sidebar_region is None:
        raise CodexAccessibilityError("sidebar region is unavailable")
    with _UIA_LOCK:
        live_nodes = nodes if nodes is not None else _walk_nodes(int(hwnd))
        pixels, pixel_region = sidebar_capture or _capture_sidebar(window)
        model = build_navigator_model(live_nodes, tuple(sidebar_region), pixels)
        model["pixel_states"] = pixels is not None and pixel_region == tuple(sidebar_region)
        return model


def _structured_projects(nodes: list[_Node], sidebar_region: tuple[int, int, int, int]):
    visible = [node for node in nodes if _inside_region(node, sidebar_region)]
    projects = sorted(
        [
            node
            for node in visible
            if node.control_type == "ButtonControl"
            and PROJECT_CLASS_MARKER in node.class_name
        ],
        key=lambda node: (node.y, node.x),
    )
    tasks = sorted(
        [
            node
            for node in visible
            if node.control_type == "ButtonControl"
            and TASK_CLASS_MARKER in node.class_name
            and node.name not in {"Pin chat", "Archive chat"}
        ],
        key=lambda node: (node.y, node.x),
    )
    result = []
    for index, project in enumerate(projects):
        name, _host = _project_identity(project, _row_nodes(nodes, project))
        next_y = projects[index + 1].y if index + 1 < len(projects) else 10**9
        result.append(
            (
                name,
                project,
                [
                    task
                    for task in tasks
                    if project.y + project.height <= task.center_y < next_y
                ],
            )
        )
    return result


def perform_codex_navigator_action(window: dict, action: dict) -> dict:
    """Expand/collapse a project or invoke a task without mouse coordinates."""
    if window.get("backend") != "codex_desktop":
        raise CodexAccessibilityError("navigator actions require a Codex window")
    hwnd = window.get("hwnd")
    if hwnd is None:
        raise CodexAccessibilityError("Codex target has no window handle")
    kind = action.get("kind")
    project_name = str(action.get("project") or "").strip()
    if kind not in {"project", "task"} or not project_name:
        raise CodexAccessibilityError("kind and project are required")
    try:
        from press_backends import resolve_surface_region

        sidebar_region = resolve_surface_region(window, "sidebar")
    except Exception as exc:
        raise CodexAccessibilityError(f"sidebar region is unavailable: {exc}") from exc
    if sidebar_region is None:
        raise CodexAccessibilityError("sidebar region is unavailable")
    with _UIA_LOCK:
        projects = _structured_projects(_walk_nodes(int(hwnd)), tuple(sidebar_region))
        match = next((item for item in projects if item[0] == project_name), None)
        if match is None:
            raise CodexAccessibilityError(f"project not found: {project_name}")
        _name, project, tasks = match
        if kind == "project":
            desired = action.get("expanded")
            if not isinstance(desired, bool):
                raise CodexAccessibilityError("expanded (bool) is required")
            try:
                pattern = project.control.GetExpandCollapsePattern()
                current = int(pattern.ExpandCollapseState) == 1
                if desired and not current:
                    pattern.Expand()
                elif not desired and current:
                    pattern.Collapse()
            except Exception as exc:
                raise CodexAccessibilityError(f"could not toggle project: {exc}") from exc
            return {"acted": True, "kind": "project", "project": project_name, "expanded": desired}

        title = str(action.get("title") or "").strip()
        if not title:
            raise CodexAccessibilityError("title is required")
        task = next((candidate for candidate in tasks if candidate.name.strip() == title), None)
        if task is None:
            raise CodexAccessibilityError(f"task not found in {project_name}: {title}")
        try:
            task.control.GetInvokePattern().Invoke()
        except Exception as exc:
            raise CodexAccessibilityError(f"could not open task: {exc}") from exc
        return {"acted": True, "kind": "task", "project": project_name, "title": title}


__all__ = [
    "CodexAccessibilityError",
    "build_navigator_model",
    "perform_codex_navigator_action",
    "read_codex_navigator",
]
