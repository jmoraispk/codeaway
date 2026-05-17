"""Windows virtual-desktop (workspace) helpers.

Two capabilities:
  1. ``current_id()`` returns a stable GUID string for the current
     virtual desktop. Uses the documented ``IVirtualDesktopManager``
     COM interface (CLSID aa509086-...), called via bare ctypes so we
     don't pull in a comtypes dependency. The id is stable across
     sessions of a given desktop, so binding a setup to an id and
     reading it back later works.
  2. ``switch_next() / switch_prev()`` simulate Ctrl+Win+Right /
     Ctrl+Win+Left via pyautogui — the standard Windows shortcut for
     "next / previous virtual desktop". After firing the keystroke,
     callers should wait briefly before reading ``current_id()`` so
     the desktop transition animation has time to settle.

Cross-platform: every function is a no-op (returns None / does
nothing) when not on Windows, so the rest of the app degrades
gracefully on macOS / Linux dev boxes.
"""

from __future__ import annotations

import logging
import sys
import time
from typing import Optional

LOG = logging.getLogger("press_workspace")

IS_WINDOWS = sys.platform.startswith("win")


if IS_WINDOWS:
    import ctypes
    from ctypes import POINTER, byref, c_int, c_uint, c_void_p
    from ctypes.wintypes import BOOL, HWND

    # Documented Win32 / COM constants.
    CLSCTX_INPROC_SERVER = 1
    S_OK = 0
    # CLSID + IID for IVirtualDesktopManager. Both are documented in
    # the Windows SDK (ShObjIdl_core.h) and have been stable since
    # Windows 10 1607.
    _CLSID_VirtualDesktopManager = "{aa509086-5ca9-4c25-8f95-589d3c07b48a}"
    _IID_IVirtualDesktopManager = "{a5cd92ff-29be-454c-8d04-d82879fb3f1b}"

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_uint32),
            ("Data2", ctypes.c_uint16),
            ("Data3", ctypes.c_uint16),
            ("Data4", ctypes.c_uint8 * 8),
        ]

    def _guid_from_string(s: str) -> _GUID:
        parts = s.strip("{}").split("-")
        g = _GUID()
        g.Data1 = int(parts[0], 16)
        g.Data2 = int(parts[1], 16)
        g.Data3 = int(parts[2], 16)
        d4 = int(parts[3], 16)
        g.Data4[0] = (d4 >> 8) & 0xFF
        g.Data4[1] = d4 & 0xFF
        d5 = int(parts[4], 16)
        for i in range(6):
            g.Data4[2 + i] = (d5 >> (8 * (5 - i))) & 0xFF
        return g

    def _guid_to_string(g: _GUID) -> str:
        return (
            "{%08x-%04x-%04x-%02x%02x-%02x%02x%02x%02x%02x%02x}"
            % (
                g.Data1,
                g.Data2,
                g.Data3,
                g.Data4[0],
                g.Data4[1],
                g.Data4[2],
                g.Data4[3],
                g.Data4[4],
                g.Data4[5],
                g.Data4[6],
                g.Data4[7],
            )
        )

    _ole32 = ctypes.OleDLL("ole32")
    _user32 = ctypes.WinDLL("user32", use_last_error=True)

    _CoInitializeEx = _ole32.CoInitializeEx
    _CoInitializeEx.argtypes = [c_void_p, c_uint]
    _CoInitializeEx.restype = c_int

    _CoCreateInstance = _ole32.CoCreateInstance
    _CoCreateInstance.argtypes = [
        POINTER(_GUID),
        c_void_p,
        c_uint,
        POINTER(_GUID),
        POINTER(c_void_p),
    ]
    _CoCreateInstance.restype = c_int

    _GetForegroundWindow = _user32.GetForegroundWindow
    _GetForegroundWindow.argtypes = []
    _GetForegroundWindow.restype = HWND

    _GetDesktopWindow = _user32.GetDesktopWindow
    _GetDesktopWindow.argtypes = []
    _GetDesktopWindow.restype = HWND

    # COINIT_APARTMENTTHREADED = 0x2 — IVirtualDesktopManager is fine
    # in STA. CoInitializeEx returns S_FALSE (1) on subsequent calls
    # within the same thread; that's not an error, the apartment is
    # already initialised.
    _COM_INIT_FLAG = 0x2
    _com_initialised = False

    def _ensure_com_init() -> bool:
        global _com_initialised
        if _com_initialised:
            return True
        hr = _CoInitializeEx(None, _COM_INIT_FLAG)
        # S_OK (0) or S_FALSE (1) both mean "ready to go".
        if hr in (0, 1):
            _com_initialised = True
            return True
        LOG.warning("CoInitializeEx failed: 0x%08x", hr & 0xFFFFFFFF)
        return False

    # Vtable indices for IVirtualDesktopManager. IUnknown takes slots
    # 0–2 (QueryInterface, AddRef, Release); the interface's own
    # methods follow.
    _VTBL_QUERYINTERFACE = 0  # noqa: F841 — kept for documentation
    _VTBL_ADDREF = 1  # noqa: F841
    _VTBL_RELEASE = 2
    _VTBL_IS_ON_CURRENT = 3  # noqa: F841
    _VTBL_GET_WINDOW_DESKTOP_ID = 4

    # WINFUNCTYPE = stdcall. Each method's first arg is the `this`
    # pointer; the remaining args match the IDL signature.
    _Release_t = ctypes.WINFUNCTYPE(c_uint, c_void_p)
    _GetWindowDesktopId_t = ctypes.WINFUNCTYPE(
        c_int, c_void_p, HWND, POINTER(_GUID)
    )
    _IsWindowOnCurrentVirtualDesktop_t = ctypes.WINFUNCTYPE(
        c_int, c_void_p, HWND, POINTER(BOOL)
    )

    def _call_vtbl(this_ptr: c_void_p, slot: int, fn_type):
        """Resolve and bind a vtable slot. Returns a callable that
        takes ``this`` plus the method's args (per WINFUNCTYPE)."""
        vtbl_ptr = ctypes.cast(this_ptr, POINTER(c_void_p))
        vtbl = ctypes.cast(vtbl_ptr[0], POINTER(c_void_p))
        return fn_type(vtbl[slot])

    def _create_manager() -> Optional[c_void_p]:
        if not _ensure_com_init():
            return None
        clsid = _guid_from_string(_CLSID_VirtualDesktopManager)
        iid = _guid_from_string(_IID_IVirtualDesktopManager)
        out = c_void_p()
        hr = _CoCreateInstance(
            byref(clsid), None, CLSCTX_INPROC_SERVER, byref(iid), byref(out)
        )
        if hr != S_OK or not out.value:
            LOG.warning(
                "CoCreateInstance(VirtualDesktopManager) failed: 0x%08x",
                hr & 0xFFFFFFFF,
            )
            return None
        return out


def current_id() -> Optional[str]:
    """GUID string for the virtual desktop hosting the current
    foreground window. None if the API isn't available (non-Windows,
    or no foreground window, or COM call fails)."""
    if not IS_WINDOWS:
        return None
    mgr = _create_manager()
    if not mgr:
        return None
    try:
        hwnd = _GetForegroundWindow()
        if not hwnd:
            # No foreground window — fall back to the desktop window
            # itself, which is always present and on the current
            # virtual desktop.
            hwnd = _GetDesktopWindow()
            if not hwnd:
                return None
        get_id = _call_vtbl(mgr, _VTBL_GET_WINDOW_DESKTOP_ID, _GetWindowDesktopId_t)
        guid_out = _GUID()
        hr = get_id(mgr, hwnd, byref(guid_out))
        if hr != S_OK:
            LOG.warning("GetWindowDesktopId failed: 0x%08x", hr & 0xFFFFFFFF)
            return None
        return _guid_to_string(guid_out)
    finally:
        try:
            release = _call_vtbl(mgr, _VTBL_RELEASE, _Release_t)
            release(mgr)
        except Exception:
            pass


def filter_to_current_workspace(hwnds) -> Optional[set]:
    """Return the subset of ``hwnds`` that's on the foreground virtual
    desktop. ``None`` on non-Windows or if the COM service is
    unavailable — callers should fall back to keeping every HWND in
    that case (better to over-include than to silently drop the
    user's actual windows).

    Uses ``IVirtualDesktopManager.IsWindowOnCurrentVirtualDesktop``,
    which is what Windows itself uses to decide which windows render
    on the current desktop. ``IsWindowVisible`` doesn't help here —
    it returns True for windows on every desktop, since Windows
    treats them as "non-hidden", just rendered elsewhere.

    Creates ONE manager instance and reuses it across the queries
    so an N-window enumeration pays one CoCreateInstance, not N.
    """
    if not IS_WINDOWS:
        return None
    mgr = _create_manager()
    if not mgr:
        return None
    try:
        is_on = _call_vtbl(
            mgr, _VTBL_IS_ON_CURRENT, _IsWindowOnCurrentVirtualDesktop_t
        )
        keep: set[int] = set()
        for h in hwnds:
            try:
                hwnd_int = int(h)
            except (TypeError, ValueError):
                continue
            out = BOOL(False)
            try:
                hr = is_on(mgr, hwnd_int, byref(out))
            except Exception:
                continue
            if hr == 0 and out.value:
                keep.add(hwnd_int)
        return keep
    finally:
        try:
            release = _call_vtbl(mgr, _VTBL_RELEASE, _Release_t)
            release(mgr)
        except Exception:
            pass


def switch_next(settle_seconds: float = 0.35) -> bool:
    """Simulate Ctrl+Win+Right (Windows' next-virtual-desktop
    shortcut). Returns True if the keystroke was fired. The
    ``settle_seconds`` pause gives the desktop transition animation
    time to finish before the caller reads ``current_id()`` — Windows
    doesn't update the desktop manager state until the animation is
    well underway."""
    return _send_workspace_hotkey("right", settle_seconds)


def switch_prev(settle_seconds: float = 0.35) -> bool:
    """Simulate Ctrl+Win+Left (Windows' previous-virtual-desktop
    shortcut). See ``switch_next``."""
    return _send_workspace_hotkey("left", settle_seconds)


def _send_workspace_hotkey(direction: str, settle_seconds: float) -> bool:
    if not IS_WINDOWS:
        return False
    try:
        import pyautogui  # type: ignore
    except Exception as exc:
        LOG.warning("pyautogui import failed: %s", exc)
        return False
    try:
        # winleft + ctrl + arrow — pyautogui handles the modifier
        # press/release ordering. ctrl listed before winleft so the
        # combo registers cleanly on slower machines (Windows is
        # forgiving here either way).
        pyautogui.hotkey("ctrl", "winleft", direction)
    except Exception as exc:
        LOG.warning("workspace hotkey failed: %s", exc)
        return False
    if settle_seconds > 0:
        time.sleep(settle_seconds)
    return True
