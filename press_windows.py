"""Win32 enumeration for auto-detecting Cursor windows.

The bridge configures each Cursor window manually today: drag-capture a
bounding box per window. With many monitors and frequent re-tiling that
gets old fast. Windows already knows where every visible top-level
window is — calling EnumWindows + GetWindowRect skips the manual step
entirely, and the rects come back in physical pixels when the process
is pinned to PER_MONITOR_AWARE_V2 (which we already do at startup).

This module is Windows-only. On any other platform the public function
returns an empty list, so callers can ship the feature without
guarding the import.
"""

from __future__ import annotations

import ctypes
import logging
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform.startswith("win")
LOG = logging.getLogger("press_windows")


def _pin_thread_v2_dpi() -> None:
    """Match the rest of the engine: tell Windows this thread renders /
    queries coordinates in physical pixels, regardless of system DPI
    scaling. Idempotent; cheap to call on every detection pass."""
    if not IS_WINDOWS:
        return
    try:
        # PER_MONITOR_AWARE_V2 = -4 per SetThreadDpiAwarenessContext docs
        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def _short_label(title: str) -> str:
    """Keep only the part before the first " - " — Cursor's title
    template is ``<tab name> - <project> - Cursor`` (and similar
    variations for SSH sessions). For glanceable tracking the tab
    name is the only part the user actually recognises; the project
    + host suffix is noise that pushes the useful bit off the row.

    Examples:
        "Polish README - auto-press - Cursor"   → "Polish README"
        "Ch.EST (504) - aerial-framework-3 [SSH: dgx]" → "Ch.EST (504)"
        "aerial-framework-4 [SSH: dgx]"         → kept whole (no " - ")
    """
    parts = title.split(" - ", 1)
    head = parts[0].strip()
    if not head and len(parts) > 1:
        head = parts[1].strip()
    return head or "Cursor"


def _configure_process_api(user32, kernel32) -> None:
    """Declare pointer-safe ctypes signatures for process-path lookup."""
    user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)
    ]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    kernel32.OpenProcess.argtypes = [
        wintypes.DWORD, wintypes.BOOL, wintypes.DWORD
    ]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def _window_process_path(user32, kernel32, hwnd) -> str:
    """Return an HWND's executable path, or an empty string on failure."""
    try:
        process_id = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
        if not process_id.value:
            return ""
        process = kernel32.OpenProcess(0x1000, False, process_id.value)
        if not process:
            return ""
        try:
            buffer = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buffer))
            if kernel32.QueryFullProcessImageNameW(
                process, 0, buffer, ctypes.byref(size)
            ):
                return buffer.value
        finally:
            kernel32.CloseHandle(process)
    except Exception:
        pass
    return ""


def _list_visible_windows(
    current_workspace_only: bool = True,
    *,
    fail_closed_when_workspace_unknown: bool = False,
    target_label: str = "window",
) -> list[dict]:
    """Enumerate visible, usable top-level windows with process paths.

    Each entry:
        {
          "title": <full window title>,
          "name": <short human label derived from title>,
          "region": [x, y, w, h]   # physical px
          "hwnd": <Win32 handle, int>
        }

    Filters out hidden windows, minimised windows, ones without a title,
    and ones with degenerate (<200 px) width or height. Returns [] on
    non-Windows platforms.

    ``current_workspace_only`` (default True) restricts the result to
    windows on the foreground virtual desktop via
    IVirtualDesktopManager. Pass False to see windows on every
    desktop — used by the rule's window-scope picker so the user
    can select windows that aren't currently in view but will be
    when they switch workspaces. ``fail_closed_when_workspace_unknown``
    returns no candidates (and logs ``target_label``) if membership cannot
    be verified; the default preserves Cursor's legacy fail-open behavior.
    """
    if not IS_WINDOWS:
        return []

    _pin_thread_v2_dpi()
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    _configure_process_api(user32, kernel32)
    out: list[dict] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_bool, wintypes.HWND, wintypes.LPARAM
    )

    def cb(hwnd, _lparam):
        # Each EnumWindows callback runs synchronously on this thread,
        # so it's safe to mutate `out` directly. The closure also keeps
        # the WNDENUMPROC reference alive — without that, ctypes would
        # GC it mid-iteration on some Pythons.
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            if user32.IsIconic(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if length == 0:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            w = int(rect.right) - int(rect.left)
            h = int(rect.bottom) - int(rect.top)
            if w < 200 or h < 200:
                return True
            out.append(
                {
                    "title": title,
                    "name": _short_label(title),
                    "region": [int(rect.left), int(rect.top), int(w), int(h)],
                    "hwnd": int(hwnd),
                    "process_path": _window_process_path(user32, kernel32, hwnd),
                }
            )
        except Exception:
            # One pathological HWND shouldn't kill the whole sweep.
            pass
        return True

    proc = WNDENUMPROC(cb)
    try:
        user32.EnumWindows(proc, 0)
    except Exception:
        # If EnumWindows itself blows up, return whatever we got so far
        # rather than crashing the bridge / UI.
        pass
    # Filter to the foreground virtual desktop unless the caller
    # asked for everything. IsWindowVisible doesn't distinguish
    # "rendered here" from "rendered on another desktop" — both
    # return True. Without this filter, a user with 3 Cursors on
    # desktop A and 3 on desktop B sees all 6 listed as if they
    # shared the current workspace. A None return means the filter is
    # unavailable: legacy Cursor discovery keeps its fail-open fallback,
    # while Codex opts into fail-closed because an unverified HWND may
    # belong to another desktop.
    if current_workspace_only:
        workspace_error = None
        try:
            from press_workspace import filter_to_current_workspace

            keep = filter_to_current_workspace([w["hwnd"] for w in out])
        except Exception as exc:
            keep = None
            workspace_error = exc
        if keep is not None:
            out = [w for w in out if w["hwnd"] in keep]
        elif fail_closed_when_workspace_unknown:
            detail = f": {workspace_error}" if workspace_error else ""
            LOG.warning(
                "%s discovery failed closed because current virtual desktop "
                "membership is unavailable%s",
                target_label,
                detail,
            )
            return []
    # Sort left-to-right, then top-to-bottom — matches how the user
    # would scan a tiled monitor and makes auto-generated #1, #2, etc.
    # names line up with what they see.
    out.sort(key=lambda w: (w["region"][0], w["region"][1]))
    return out


def list_cursor_windows(current_workspace_only: bool = True) -> list[dict]:
    """Enumerate visible Cursor windows, preserving legacy title matching."""
    return [
        {**candidate, "backend": "cursor"}
        for candidate in _list_visible_windows(current_workspace_only)
        if "Cursor" in candidate["title"]
    ]


def _is_codex_process_path(process_path):
    normalized = (process_path or "").replace("/", "\\").lower()
    return "openai.codex_" in normalized or "\\openai\\codex\\" in normalized


def list_codex_windows(current_workspace_only: bool = True) -> list[dict]:
    return [
        {**candidate, "name": "Codex", "backend": "codex_desktop"}
        for candidate in _list_visible_windows(
            current_workspace_only,
            fail_closed_when_workspace_unknown=True,
            target_label="Codex",
        )
        if _is_codex_process_path(candidate.get("process_path"))
    ]


def list_bridge_windows(current_workspace_only: bool = True) -> list[dict]:
    combined = list_cursor_windows(current_workspace_only) + list_codex_windows(
        current_workspace_only
    )
    by_hwnd = {}
    for window in combined:
        hwnd = int(window["hwnd"])
        if hwnd not in by_hwnd or window.get("backend") == "codex_desktop":
            by_hwnd[hwnd] = window
    return sorted(
        by_hwnd.values(), key=lambda window: (window["region"][0], window["region"][1])
    )
