"""Auto Press — Fluent Design UI.

QFluentWidgets on top of PySide6 for a Windows 11 Settings-app look.
Self-contained: engine worker, drag-capture overlays, monitor picker and
status widgets all live in this file.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (
    QEventLoop,
    QObject,
    QRectF,
    QSettings,
    QSize,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QCloseEvent,
    QColor,
    QGuiApplication,
    QIcon,
    QPainter,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# qfluentwidgets prints a "QFluentWidgets Pro is now released" banner from
# common/config.py at import time. Silence it by redirecting stdout only
# during the first qfluentwidgets import; subsequent imports hit the module
# cache and don't re-fire the print.
import contextlib as _contextlib
import io as _io

with _contextlib.redirect_stdout(_io.StringIO()):
    from qfluentwidgets import (
        BodyLabel,
        CaptionLabel,
        CheckBox,
        ComboBox,
        FluentIcon as FIF,
        HeaderCardWidget,
        LineEdit,
        PlainTextEdit as FluentPlainTextEdit,
        PrimaryPushButton,
        PushButton,
        SegmentedWidget,
        SimpleCardWidget,
        StrongBodyLabel,
        SubtitleLabel,
        SwitchButton,
        TableWidget,
        Theme,
        ToolButton,
        setTheme,
        setThemeColor,
    )
    from qfluentwidgets.components.widgets.spin_box import DoubleSpinBox
    from qfluentwidgets.components.widgets.menu import RoundMenu as _RoundMenu

# RoundMenu (parent of ComboBoxMenu) sets contentsMargins(12, 8, 12, 12) on
# its layout, which creates a gap between its painted outer rect and the
# inner item view — the "box inside a box" look that combo dropdowns get.
# Patching the margin to 0 once means every popup looks like a single
# unified surface instead.
_orig_round_menu_init = _RoundMenu.__init__


def _patched_round_menu_init(self, *args, **kwargs):
    _orig_round_menu_init(self, *args, **kwargs)
    layout = self.layout()
    if layout is not None:
        layout.setContentsMargins(0, 0, 0, 0)


_RoundMenu.__init__ = _patched_round_menu_init


from press_core import save_gray_image
from press_engine import (
    build_runtime_rules,
    capture_screen_gray,
    capture_screen_rgb,
    dominant_rgb,
    ensure_vision,
    evaluate_bridge_windows,
    evaluate_rule_on_frame,
    evaluate_rules,
    execute_matches,
)
from press_store import (
    ACTION_CLICK,
    ACTION_CLICK_TYPE_ENTER,
    ACTION_TYPES,
    CONFIG_PATH,
    MATCHER_COLOR,
    MATCHER_TEMPLATE,
    default_bridge_window,
    default_rule,
    list_template_files,
    load_config,
    resolve_template_path,
    save_config,
    serialize_template_path,
    template_asset_path,
)


IS_WINDOWS = sys.platform.startswith("win")

# ---- Win32 helpers (capture coordinates always in physical pixels) --

if IS_WINDOWS:
    from ctypes import wintypes as _wintypes

    _user32 = ctypes.windll.user32

    class _RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    _MONITORENUMPROC = ctypes.WINFUNCTYPE(
        ctypes.c_int,
        _wintypes.HMONITOR,
        _wintypes.HDC,
        ctypes.POINTER(_RECT),
        _wintypes.LPARAM,
    )

    def enumerate_physical_monitors() -> list[tuple[int, int, int, int]]:
        """Every connected monitor's rect in physical pixels (per-monitor-v2 context)."""
        monitors: list[tuple[int, int, int, int]] = []

        def _proc(_hm, _hdc, lprect, _lp):
            r = lprect.contents
            monitors.append((r.left, r.top, r.right - r.left, r.bottom - r.top))
            return 1

        _user32.EnumDisplayMonitors(None, None, _MONITORENUMPROC(_proc), 0)
        return monitors

    def physical_cursor_pos() -> tuple[int, int]:
        """Cursor position in physical screen pixels, consistent with ImageGrab."""
        p = _wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(p))
        return int(p.x), int(p.y)

else:

    def enumerate_physical_monitors() -> list[tuple[int, int, int, int]]:
        return [
            (s.geometry().left(), s.geometry().top(), s.geometry().width(), s.geometry().height())
            for s in QGuiApplication.screens()
        ]

    def physical_cursor_pos() -> tuple[int, int]:
        from PySide6.QtGui import QCursor

        pos = QCursor.pos()
        return pos.x(), pos.y()


# ---- shared colors / status widgets ---------------------------------

STATUS_RUNNING = "#22c55e"
STATUS_STOPPED = "#ef4444"
RECT_STROKE = "#22c55e"

WINDOWS_ACCENT = "#2b7de9"


class StatusDot(QWidget):
    """A small filled circle used as a status indicator."""

    def __init__(self, diameter: int = 10):
        super().__init__()
        self._diameter = diameter
        self._color = QColor(STATUS_STOPPED)
        self.setFixedSize(diameter + 2, diameter + 2)

    def set_color(self, color: str) -> None:
        self._color = QColor(color)
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(self._color)
        p.drawEllipse(1, 1, self._diameter, self._diameter)


def _make_dot_icon(color: QColor) -> QIcon:
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(color)
    p.setPen(QPen(QColor(20, 20, 22), 3))
    p.drawEllipse(6, 6, size - 12, size - 12)
    p.end()
    return QIcon(pm)


# ---- hotkey picker --------------------------------------------------

# Win32 RegisterHotKey modifier flags.
_MOD_ALT = 0x0001
_MOD_CONTROL = 0x0002
_MOD_SHIFT = 0x0004
_MOD_WIN = 0x0008

# Non-letter / non-digit Qt.Key -> Win32 VK mapping.
_QT_KEY_TO_VK = {
    Qt.Key_F1: 0x70, Qt.Key_F2: 0x71, Qt.Key_F3: 0x72, Qt.Key_F4: 0x73,
    Qt.Key_F5: 0x74, Qt.Key_F6: 0x75, Qt.Key_F7: 0x76, Qt.Key_F8: 0x77,
    Qt.Key_F9: 0x78, Qt.Key_F10: 0x79, Qt.Key_F11: 0x7A, Qt.Key_F12: 0x7B,
    Qt.Key_PageUp: 0x21, Qt.Key_PageDown: 0x22,
    Qt.Key_Home: 0x24, Qt.Key_End: 0x23,
    Qt.Key_Insert: 0x2D, Qt.Key_Delete: 0x2E,
    Qt.Key_Tab: 0x09, Qt.Key_Backtab: 0x09,
    Qt.Key_Space: 0x20, Qt.Key_Return: 0x0D, Qt.Key_Enter: 0x0D,
    Qt.Key_Left: 0x25, Qt.Key_Up: 0x26, Qt.Key_Right: 0x27, Qt.Key_Down: 0x28,
    Qt.Key_Escape: 0x1B, Qt.Key_Backspace: 0x08,
}

# VK -> short display name. A-Z / 0-9 / F1-F12 are generated on the fly.
_VK_DISPLAY = {
    0x21: "PgUp", 0x22: "PgDn",
    0x24: "Home", 0x23: "End",
    0x2D: "Ins", 0x2E: "Del",
    0x09: "Tab", 0x20: "Space", 0x0D: "Enter",
    0x25: "←", 0x26: "↑", 0x27: "→", 0x28: "↓",
    0x1B: "Esc", 0x08: "Backspace",
}


def _qt_key_to_vk(qt_key: int) -> int:
    if Qt.Key_A <= qt_key <= Qt.Key_Z:
        return int(qt_key)  # matches VK_A..VK_Z
    if Qt.Key_0 <= qt_key <= Qt.Key_9:
        return int(qt_key)  # matches VK_0..VK_9
    return _QT_KEY_TO_VK.get(qt_key, 0)


def _vk_name(vk: int) -> str:
    if 0x41 <= vk <= 0x5A:
        return chr(vk)
    if 0x30 <= vk <= 0x39:
        return chr(vk)
    if 0x70 <= vk <= 0x7B:
        return f"F{vk - 0x70 + 1}"
    return _VK_DISPLAY.get(vk, f"VK{vk:02X}")


def _format_hotkey(vk: int, mods: int) -> str:
    parts: list[str] = []
    if mods & _MOD_CONTROL:
        parts.append("Ctrl")
    if mods & _MOD_ALT:
        parts.append("Alt")
    if mods & _MOD_SHIFT:
        parts.append("Shift")
    if mods & _MOD_WIN:
        parts.append("Win")
    parts.append(_vk_name(vk))
    return "+".join(parts)


class HotkeyButton(PushButton):
    """Push-button that shows the current global hotkey and captures a new one on click."""

    hotkey_changed = Signal(int, int)  # (vk, mods) — Win32 codes.

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedWidth(132)
        self.setFocusPolicy(Qt.StrongFocus)
        self._capturing = False
        self._vk = 0x22
        self._mods = 0
        self._refresh()
        self.clicked.connect(self._begin_capture)

    def set_hotkey(self, vk: int, mods: int) -> None:
        self._vk = int(vk)
        self._mods = int(mods)
        self._refresh()

    def _begin_capture(self) -> None:
        self._capturing = True
        self.setText("Press keys…")
        self.setFocus()

    def _end_capture(self) -> None:
        self._capturing = False
        self._refresh()

    def _refresh(self) -> None:
        self.setText(_format_hotkey(self._vk, self._mods))

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._capturing:
            super().keyPressEvent(event)
            return
        key = event.key()
        # Ignore bare modifier presses — we're waiting for the payload key.
        if key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            return
        if key == Qt.Key_Escape:
            self._end_capture()
            return
        vk = _qt_key_to_vk(key)
        if vk == 0:
            # Can't express this key as a Win32 VK; keep waiting.
            return
        mods = event.modifiers()
        win_mods = 0
        if mods & Qt.ControlModifier:
            win_mods |= _MOD_CONTROL
        if mods & Qt.AltModifier:
            win_mods |= _MOD_ALT
        if mods & Qt.ShiftModifier:
            win_mods |= _MOD_SHIFT
        if mods & Qt.MetaModifier:
            win_mods |= _MOD_WIN
        self._vk = vk
        self._mods = win_mods
        self._end_capture()
        self.hotkey_changed.emit(vk, win_mods)


# ---- engine worker --------------------------------------------------


def _bridge_urls(host: str, port: int) -> list[str]:
    """URLs that should reach the bridge: localhost first, then LAN/Tailscale.

    When host is 0.0.0.0 we enumerate IPv4 addresses on the box so the user
    can copy whichever one matches the network they're testing from.
    """
    import socket

    urls = [f"http://localhost:{port}/"]
    if host in ("0.0.0.0", "::", ""):
        try:
            for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
                if ip.startswith("127.") or ip.startswith("169.254."):
                    continue
                urls.append(f"http://{ip}:{port}/")
        except Exception:
            pass
    elif host not in ("127.0.0.1", "localhost"):
        urls.insert(0, f"http://{host}:{port}/")
    # Stable de-dupe.
    seen: set[str] = set()
    unique: list[str] = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)
    return unique


def _encode_rgb_to_png(rgb) -> bytes | None:
    """HxWx3 RGB ndarray → PNG bytes for the bridge snapshot ring buffer.

    Returns None on encode failure rather than raising — the worker tick
    must keep going even if a single frame can't be encoded.
    """
    try:
        from io import BytesIO

        from PIL import Image  # already a project dependency
    except Exception:
        return None
    try:
        img = Image.fromarray(rgb, mode="RGB")
        buf = BytesIO()
        img.save(buf, format="PNG", optimize=False)
        return buf.getvalue()
    except Exception:
        return None


def _monitor_index_for_point(x: int, y: int) -> int:
    """Best-effort 0-based monitor index containing (x, y); -1 if none."""
    try:
        for idx, (mx, my, mw, mh) in enumerate(enumerate_physical_monitors()):
            if mx <= x < mx + mw and my <= y < my + mh:
                return idx
    except Exception:
        return -1
    return -1


class EngineWorker(QObject):
    tick_done = Signal(list, list, float)
    tick_error = Signal(str)
    running_changed = Signal(bool)
    needs_rules = Signal()
    # Bridge events: emitted whenever a rule action fires. Carries a JSON-
    # ready dict; bridge consumers push it onto an SSE/event ring buffer.
    rule_matched = Signal(dict)
    # Per-window idle/busy snapshot. First arg is the list of state dicts
    # (without the 'rgb' key); second is {window_id: png_bytes} so the
    # bridge can push them into its ring buffer without a second capture.
    bridge_window_states = Signal(list, dict)
    # Fires only on busy↔idle transition. Payload: a window state dict.
    bridge_window_transition = Signal(dict)

    def __init__(self, cfg_snapshot):
        super().__init__()
        self._cfg_snapshot = cfg_snapshot
        self._running = False
        self._stop = False
        self._interval = 10.0
        self._lock = threading.Lock()
        # Maps window id → last known idle bool, so we only emit transitions.
        # Maps window id → last (idle, asking) tuple, so we only emit
        # transitions when either flag flips.
        self._last_window_idle: dict[str, tuple[bool, bool]] = {}
        # Wake signal — anything that should trigger an immediate tick
        # (Start button, hotkey, bridge toggle, reset_window_tracking)
        # calls .set() on this; the post-tick sleep loop watches it and
        # breaks out early so the next observation happens within
        # ~50 ms instead of waiting up to a full interval.
        self._wake = threading.Event()

    def set_interval(self, seconds: float) -> None:
        with self._lock:
            self._interval = max(0.1, float(seconds))

    def set_running(self, on: bool) -> None:
        with self._lock:
            self._running = bool(on)
        # Trip the wake event so the worker doesn't sit out the rest
        # of its current sleep before noticing the user pressed Start.
        # Stop also benefits — the loop drops back to the idle-sleep
        # branch within 50 ms instead of finishing a stale tick.
        self._wake.set()
        self.running_changed.emit(bool(on))

    def request_stop(self) -> None:
        self._stop = True
        self._wake.set()

    def wake(self) -> None:
        """External hook to break the worker out of its post-tick sleep.
        Call after toggling bridge state, after rule edits that should
        take effect immediately, etc."""
        self._wake.set()

    def is_running(self) -> bool:
        with self._lock:
            return self._running

    def reset_window_tracking(self) -> None:
        """Forget every window's prior idle/busy observation.

        Called when the bridge service is started (or restarted) so the
        next tick treats each window as a first observation and
        captures a fresh snapshot even if it's already idle. Without
        this, a Reload that finds windows already idle would leave the
        WindowStore empty until the next busy→idle transition because
        the worker's tracking dict still says "prev=True" from before.

        Atomic via reference replacement — the running tick reads its
        own previous reference; this thread just rebinds the attribute.
        """
        self._last_window_idle = {}
        # Bridge just (re)started → don't make the user wait out a stale
        # post-tick sleep before the first capture lands on the phone.
        self._wake.set()

    def run(self) -> None:
        # Pin PER_MONITOR_AWARE_V2 on this worker thread once. Sticky for the
        # thread's lifetime, so every capture + click iteration agrees on
        # physical pixel coordinates.
        if IS_WINDOWS:
            try:
                ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
            except Exception:
                pass
        while not self._stop:
            cfg = self._cfg_snapshot()
            bridge_cfg = cfg.get("bridge") or {}
            # Bridge ticks whenever the service is on AND has windows +
            # an idle template configured — independent of the Start
            # button, so flipping the bridge switch is enough to start
            # populating the phone's view.
            bridge_should_tick = bool(
                cfg.get("bridge_active")
                and bridge_cfg.get("windows")
                and bridge_cfg.get("idle_template_path")
            )
            rules_should_tick = self.is_running()
            if not rules_should_tick and not bridge_should_tick:
                # Idle: wait on the event so toggling Start or the
                # bridge wakes us within ~50 ms instead of after a
                # 100 ms poll. The 0.5 s ceiling keeps cfg-snapshot
                # changes (e.g. bridge_active flipping) responsive
                # even if nobody calls .wake().
                if self._wake.wait(timeout=0.5):
                    self._wake.clear()
                continue

            # ---- rules ----
            results: list = []
            actions: list = []
            if rules_should_tick:
                try:
                    runtime_rules = build_runtime_rules(cfg)
                except Exception as exc:
                    self.tick_error.emit(f"runtime rules unavailable: {exc}")
                    self.set_running(False)
                    continue
                if not runtime_rules:
                    # No runnable rules — the user pressed Start but every
                    # rule is either disabled or has a missing template.
                    # Always log so the cause of the silent re-Stop is
                    # visible (the handler is log-only, not a popup).
                    self.needs_rules.emit()
                    self.set_running(False)
                    rules_should_tick = False
                else:
                    try:
                        results, actions = evaluate_rules(runtime_rules)
                        if actions:
                            execute_matches(actions)
                            for action in actions:
                                center = action.get("center")
                                if center is None:
                                    continue
                                cx, cy = int(center[0]), int(center[1])
                                event = {
                                    "event_id": uuid.uuid4().hex,
                                    "rule_id": action.get("id"),
                                    "rule_name": action.get("name"),
                                    "monitor_index": _monitor_index_for_point(cx, cy),
                                    "match_rect": None,
                                    "match_center": [cx, cy],
                                    "action_type": action.get("action"),
                                    "timestamp_iso": datetime.now(timezone.utc).isoformat(),
                                }
                                self.rule_matched.emit(event)
                    except Exception as exc:
                        self.tick_error.emit(f"tick failed: {exc}")

            # ---- bridge ----
            if bridge_should_tick:
                try:
                    self._tick_bridge_windows(cfg)
                except Exception as exc:
                    self.tick_error.emit(f"bridge tick failed: {exc}")

            self.tick_done.emit(results, actions, self._get_interval())

            # Post-tick wait. The Event lets Start / Stop / bridge-
            # toggle break the wait so the first tick under the new
            # state happens immediately. We don't pre-clear: if the
            # user pressed Stop while the tick was running, the set()
            # arrived mid-tick and would be consumed by a pre-clear.
            # A sticky wake makes the very first wait() return True
            # so Stop is responsive within one tick boundary.
            end = time.monotonic() + self._get_interval()
            while not self._stop:
                remaining = end - time.monotonic()
                if remaining <= 0:
                    break
                if self._wake.wait(timeout=remaining):
                    self._wake.clear()
                    break

    def _get_interval(self) -> float:
        with self._lock:
            return self._interval

    def _tick_bridge_windows(self, cfg: dict) -> None:
        """Run the per-window idle detector when the bridge service is
        active and has windows + a template configured. Emits a snapshot
        every tick and a transition only when a window flips between
        idle and busy."""
        if not cfg.get("bridge_active"):
            return
        bridge_cfg = cfg.get("bridge") or {}
        if not bridge_cfg.get("windows") or not bridge_cfg.get("idle_template_path"):
            return
        try:
            states = evaluate_bridge_windows(bridge_cfg, capture_rgb=True)
        except Exception as exc:
            # Surface detector failures instead of swallowing them — silent
            # bail-outs were exactly why the user couldn't tell why the
            # phone wasn't updating.
            self.tick_error.emit(f"bridge detect failed: {exc}")
            return
        if not states:
            return

        # Decide which windows deserve a fresh snapshot this tick.
        # Strategy:
        #   - First observation (prev=None): always capture. Otherwise a
        #     bridge restart that finds windows already idle would leave
        #     the WindowStore empty until the next busy→idle transition.
        #   - Subsequent ticks: capture only on busy→idle, so steady-
        #     idle and steady-busy don't churn through duplicates.
        images: dict[str, bytes] = {}
        slim_states: list[dict] = []
        for state in states:
            rgb = state.pop("rgb", None)
            slim_states.append(state)
            wid = state.get("id")
            if not wid or not state.get("configured"):
                continue
            is_idle = bool(state.get("idle"))
            is_asking = bool(state.get("asking"))
            actionable = is_idle or is_asking
            prev = self._last_window_idle.get(wid)
            prev_actionable = bool(prev[0] or prev[1]) if prev else None
            should_capture = prev is None or (actionable and prev_actionable is False)
            if should_capture and rgb is not None:
                png = _encode_rgb_to_png(rgb)
                if png is not None:
                    images[wid] = png

        self.bridge_window_states.emit(slim_states, images)

        for state in slim_states:
            if not state.get("configured"):
                continue
            wid = state.get("id")
            if not wid:
                continue
            now_idle = bool(state.get("idle"))
            now_asking = bool(state.get("asking"))
            curr = (now_idle, now_asking)
            prev = self._last_window_idle.get(wid)
            if prev is None:
                # First observation: record without emitting a transition,
                # otherwise we'd announce every window the moment the
                # worker starts.
                self._last_window_idle[wid] = curr
                continue
            if prev != curr:
                self._last_window_idle[wid] = curr
                self.bridge_window_transition.emit(state)


# ---- drag capture (per-monitor overlays, physical coords) -----------


class CaptureOverlay(QWidget):
    """One overlay per monitor. Controller stores start/current in PHYSICAL px."""

    def __init__(self, qt_screen, physical_rect: tuple[int, int, int, int], controller: "CaptureController"):
        super().__init__(None, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_NoSystemBackground, True)
        self.setCursor(Qt.CrossCursor)
        self._qt_screen = qt_screen
        self._physical_rect = physical_rect
        self._controller = controller
        self.setGeometry(qt_screen.geometry())

    def paintEvent(self, _event) -> None:  # noqa: N802
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(0, 0, 0, 100))
        if self._controller.start is None:
            return
        sx, sy = self._controller.start
        cx, cy = self._controller.current
        left = min(sx, cx); right = max(sx, cx)
        top = min(sy, cy); bottom = max(sy, cy)
        ml, mt, mw, mh = self._physical_rect
        mr, mb = ml + mw, mt + mh
        il = max(left, ml); it = max(top, mt)
        ir = min(right, mr); ib = min(bottom, mb)
        if ir <= il or ib <= it:
            return
        dpr = self._qt_screen.devicePixelRatio() or 1.0
        inner = QRectF(
            (il - ml) / dpr,
            (it - mt) / dpr,
            (ir - il) / dpr,
            (ib - it) / dpr,
        )
        p.setCompositionMode(QPainter.CompositionMode_Source)
        p.fillRect(inner, QColor(0, 0, 0, 0))
        p.setCompositionMode(QPainter.CompositionMode_SourceOver)
        p.setPen(QPen(QColor(RECT_STROKE), 2))
        p.drawRect(inner.adjusted(0, 0, -1, -1))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._controller.on_press(*physical_cursor_pos())

    def mouseMoveEvent(self, _event) -> None:  # noqa: N802
        if self._controller.start is not None:
            self._controller.on_motion(*physical_cursor_pos())

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self._controller.start is not None:
            self._controller.on_release(*physical_cursor_pos())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self._controller.cancel()


class CaptureController(QObject):
    done = Signal(object)

    def __init__(self, parent: Optional[QObject] = None):
        super().__init__(parent)
        self.start: Optional[tuple[int, int]] = None
        self.current: Optional[tuple[int, int]] = None
        self._overlays: list[CaptureOverlay] = []

    def begin(self) -> None:
        self.start = None
        self.current = None
        self._overlays = []
        qt_screens = QGuiApplication.screens()
        physical = enumerate_physical_monitors()
        for i, screen in enumerate(qt_screens):
            if i < len(physical):
                pr = physical[i]
            else:
                g = screen.geometry()
                d = screen.devicePixelRatio() or 1.0
                pr = (int(g.left() * d), int(g.top() * d), int(g.width() * d), int(g.height() * d))
            overlay = CaptureOverlay(screen, pr, self)
            overlay.show()
            self._overlays.append(overlay)
        if self._overlays:
            first = self._overlays[0]
            first.activateWindow(); first.raise_(); first.setFocus()

    def _redraw(self) -> None:
        for ov in self._overlays:
            ov.update()

    def on_press(self, x: int, y: int) -> None:
        self.start = (x, y); self.current = (x, y); self._redraw()

    def on_motion(self, x: int, y: int) -> None:
        self.current = (x, y); self._redraw()

    def on_release(self, x: int, y: int) -> None:
        if self.start is None:
            self._cleanup(); self.done.emit(None); return
        sx, sy = self.start
        left, right = min(sx, x), max(sx, x)
        top, bottom = min(sy, y), max(sy, y)
        w, h = right - left, bottom - top
        self._cleanup()
        self.done.emit([left, top, w, h] if w >= 5 and h >= 5 else None)

    def cancel(self) -> None:
        self._cleanup(); self.done.emit(None)

    def _cleanup(self) -> None:
        for ov in self._overlays:
            ov.close(); ov.deleteLater()
        self._overlays = []


def capture_drag_bbox(parent: QObject) -> Optional[list[int]]:
    loop = QEventLoop()
    result: dict = {"bbox": None}
    controller = CaptureController(parent)

    def on_done(bbox) -> None:
        result["bbox"] = bbox
        loop.quit()

    controller.done.connect(on_done)
    controller.begin()
    loop.exec()
    controller.deleteLater()
    return result["bbox"]


# ---- monitor picker -------------------------------------------------


class MonitorPickDialog(QDialog):
    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        self.setWindowTitle("Pick Monitor")
        self.setModal(True)
        self.selected: Optional[list[int]] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)
        title = QLabel("Restrict the scan to a single monitor:")
        layout.addWidget(title)

        for i, rect in enumerate(enumerate_physical_monitors(), start=1):
            left, top, width, height = rect
            bbox = [left, top, width, height]
            btn = QPushButton(
                f"Monitor {i}   ·   {width} × {height}   ·   ({left}, {top})"
            )
            btn.setMinimumWidth(340)
            btn.clicked.connect(lambda _c=False, b=bbox: self._choose(b))
            layout.addWidget(btn)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _choose(self, bbox: list[int]) -> None:
        self.selected = bbox
        self.accept()


class _VLine(QFrame):
    def __init__(self):
        super().__init__()
        self.setFrameShape(QFrame.VLine)
        self.setFixedWidth(1)
        self.setStyleSheet("background: rgba(255,255,255,24); border: none;")


class CollapsibleCard(HeaderCardWidget):
    """HeaderCardWidget with a chevron toggle that hides/shows the body."""

    expanded_changed = Signal(bool)

    def __init__(self, title: str = "", parent=None, expanded: bool = True):
        super().__init__(parent)
        self.setTitle(title)

        self._toggle_btn = ToolButton(FIF.UP, self)
        self._toggle_btn.setFixedSize(28, 28)
        self._toggle_btn.setToolTip("Collapse / expand")
        self._toggle_btn.clicked.connect(self._toggle)

        # HeaderCardWidget's headerLayout holds the title label; push our
        # chevron to the far right edge.
        self.headerLayout.addStretch(1)
        self.headerLayout.addWidget(self._toggle_btn)

        self._expanded = True
        if not expanded:
            self._toggle()

        # Let the header remain clickable for toggling too
        self.headerView.setCursor(Qt.PointingHandCursor)
        self.headerView.mousePressEvent = lambda _e: self._toggle()

    def _toggle(self) -> None:
        self._expanded = not self._expanded
        self.view.setVisible(self._expanded)
        self._toggle_btn.setIcon(FIF.UP if self._expanded else FIF.DOWN)
        self.expanded_changed.emit(self._expanded)

    def setExpanded(self, expanded: bool) -> None:
        if self._expanded != expanded:
            self._toggle()

    def isExpanded(self) -> bool:  # noqa: N802
        return self._expanded


class MainWindow(QMainWindow):
    hotkey_triggered = Signal()
    # Fires when the bridge HTTP /api/admin/reload endpoint is hit. Goes
    # through Qt so the actual reload runs on the main thread (the bridge
    # request handler is on its own asyncio thread).
    bridge_reload_requested = Signal()
    # Fires when /api/admin/rules is POSTed from the phone. Marshals to
    # the main thread so we touch the engine flag in the right context.
    bridge_set_rules_requested = Signal(bool)
    # Fires when a window is renamed via the phone's rename endpoint.
    # The cfg mutation happens synchronously on the bridge thread (so
    # the endpoint can return success/failure); this signal exists only
    # to refresh the desktop's bridge windows table on the main thread.
    bridge_window_renamed_remote = Signal(str, str)
    # Fires after a phone-triggered auto-detect mutates bridge.windows.
    # The slot redraws the windows table and resets worker tracking on
    # the Qt main thread.
    bridge_windows_auto_detected = Signal(int)
    # Fires after a phone-triggered setup new/activate/delete mutates
    # bridge.setups / bridge.windows / active_setup_id. Slot refreshes
    # the combo + the windows table on the main thread.
    bridge_setups_changed_remote = Signal(str)  # action: "new"/"activate"/"delete"

    CHROME_HEIGHT = 120

    def __init__(
        self,
        initial_seconds: float,
        bridge_enabled: bool = False,
        bridge_host: str | None = None,
        bridge_port: int | None = None,
        auto_activate: bool = False,
    ):
        super().__init__()
        self._bridge_enabled = bool(bridge_enabled)
        self._bridge_host_override = bridge_host
        self._bridge_port_override = bridge_port
        self._auto_activate = bool(auto_activate)
        self.hotkey_triggered.connect(self._toggle_running, Qt.QueuedConnection)
        self.bridge_reload_requested.connect(self._reload_bridge_service, Qt.QueuedConnection)
        self.bridge_set_rules_requested.connect(self._set_rules_running_remote, Qt.QueuedConnection)
        self.bridge_windows_auto_detected.connect(
            self._on_bridge_windows_auto_detected, Qt.QueuedConnection,
        )
        self.bridge_setups_changed_remote.connect(
            self._on_bridge_setups_changed_remote, Qt.QueuedConnection,
        )
        self.bridge_window_renamed_remote.connect(
            self._on_bridge_window_renamed_remote, Qt.QueuedConnection
        )

        setTheme(Theme.DARK)
        setThemeColor(WINDOWS_ACCENT)

        self.setWindowTitle("Auto Press")
        self.setMinimumSize(620, 240)
        self.resize(1120, 720)

        # Background matches Fluent dark surface. The rules here are scoped by
        # widget class; a blanket "QWidget { background: transparent }" would
        # bleed through into the combo popup and make it look like a box
        # inside a box.
        self.setStyleSheet(
            "QMainWindow { background: #1b1b1f; color: #e4e4e7; }"
            "QScrollArea { background: transparent; border: none; }"
            "QScrollArea > QWidget > QWidget { background: transparent; }"
            "QSplitter { background: transparent; }"
            "QScrollBar:vertical { background: transparent; width: 10px; margin: 0; }"
            "QScrollBar::handle:vertical { background: #3a3a42; border-radius: 5px; min-height: 30px; }"
            "QScrollBar::handle:vertical:hover { background: #4b4b54; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
        )

        # State
        self._cfg = load_config()
        self._cfg["interval_seconds"] = float(initial_seconds)
        save_config(self._cfg)
        self._cfg_lock = threading.Lock()
        self._last_scores: dict = {}
        self._next_tick_at: Optional[float] = None
        self._running = False
        self._quitting = False
        self._remembered_body_h = 600
        # Last expanded heights for the left splitter cards; restored on
        # collapse → expand round-trips.
        self._left_remembered = {"rules": 380, "log": 200}

        self._icon_running = _make_dot_icon(QColor(STATUS_RUNNING))
        self._icon_stopped = _make_dot_icon(QColor(STATUS_STOPPED))
        self.setWindowIcon(self._icon_stopped)

        self._build_central(initial_seconds)
        self._build_tray()

        # Engine worker
        self._worker = EngineWorker(self._snapshot_cfg)
        self._worker_thread = QThread(self)
        self._worker.moveToThread(self._worker_thread)
        self._worker_thread.started.connect(self._worker.run)
        self._worker.tick_done.connect(self._on_tick_done)
        self._worker.tick_error.connect(self._on_worker_error)
        self._worker.running_changed.connect(self._on_running_changed)
        self._worker.needs_rules.connect(self._on_needs_rules)
        self._worker.rule_matched.connect(self._on_rule_matched)
        self._worker.bridge_window_transition.connect(self._on_bridge_window_transition)
        self._worker.bridge_window_states.connect(self._on_bridge_window_states)
        self._worker_thread.start()

        # Optional remote bridge — controlled at runtime by the Bridge
        # tab's switch. When --bridge is passed we flip the switch on
        # programmatically so behaviour matches the old "auto-start" mode.
        self._bridge = None
        if self._bridge_enabled:
            self._bridge_switch.setChecked(True)

        # Countdown
        self._countdown_timer = QTimer(self)
        self._countdown_timer.setInterval(100)
        self._countdown_timer.timeout.connect(self._update_countdown)
        self._countdown_timer.start()

        # Workspace poll — checks the foreground virtual desktop GUID
        # every 1.5 s. If it changed and a setup is bound to the new
        # workspace, that setup is auto-activated. The COM call is
        # cheap (~few ms) and gracefully no-ops on non-Windows.
        self._last_seen_workspace_id: Optional[str] = None
        self._workspace_timer = QTimer(self)
        self._workspace_timer.setInterval(1500)
        self._workspace_timer.timeout.connect(self._poll_workspace)
        self._workspace_timer.start()

        # Hotkey
        self._hotkey_vk = int(self._cfg.get("hotkey_vk", 0x22))
        self._hotkey_mods = int(self._cfg.get("hotkey_mods", 0))
        self._hotkey_stop = threading.Event()
        self._hotkey_thread_id: dict[str, int | None] = {"tid": None}
        # Sync the picker with persisted config before we spawn the thread.
        self._hotkey_button.set_hotkey(self._hotkey_vk, self._hotkey_mods)
        if IS_WINDOWS:
            self._start_hotkey_thread()

        self._refresh_template_choices()
        self._refresh_rule_list(0 if self._cfg.get("rules") else None)
        self._refresh_bridge_template_view()
        self._refresh_bridge_windows_table()
        self._refresh_bridge_setup_combo()
        self._set_running_status(False)
        self._log(f"[ready] loaded {CONFIG_PATH}")
        # Restore window geometry + splitter sizes + collapse states from
        # the previous session. Anything not in QSettings keeps the
        # defaults set during the build phase above.
        self._restore_window_state()
        # --activate: same effect as clicking Start once the UI is up.
        # Defer to the next event-loop iteration so widgets are fully
        # constructed before the worker reads its config.
        if self._auto_activate:
            QTimer.singleShot(0, self._toggle_running)

    # ---------- layout ----------

    def _build_central(self, initial_seconds: float) -> None:
        central = QWidget()
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 14, 16, 14)
        root.setSpacing(12)

        # Build the tab pages and stack first so the command bar can
        # host the Rules/Bridge selector — that way the toolbar acts as
        # the page chrome and the body is just the selected page.
        rules_page = self._build_rules_page()
        bridge_page = self._build_bridge_page()
        self._tab_stack = QStackedWidget()
        self._tab_stack.addWidget(rules_page)
        self._tab_stack.addWidget(bridge_page)

        root.addWidget(self._build_command_bar(initial_seconds))

        self._body_container = QWidget()
        body_layout = QVBoxLayout(self._body_container)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.addWidget(self._tab_stack, 1)
        root.addWidget(self._body_container, 1)

        self.setCentralWidget(central)

    def _build_rules_page(self) -> QWidget:
        """Original rules + log + editor body, factored out so it can be
        a tab page rather than the central widget directly."""
        page = QWidget()
        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)

        self._body_splitter = QSplitter(Qt.Horizontal)
        self._body_splitter.setChildrenCollapsible(False)
        self._body_splitter.setHandleWidth(6)
        # Expand vertically regardless of children's sizeHints — without
        # this, collapsing the left-column log card shrinks the splitter
        # and the right-side editor scrolls instead of using the full
        # available height.
        self._body_splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._left_splitter = QSplitter(Qt.Vertical)
        self._left_splitter.setChildrenCollapsible(False)
        self._left_splitter.setHandleWidth(6)
        self._left_splitter.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        rules_card = self._build_rules_card()
        self._log_panel = self._build_log_panel()
        rules_card.setMinimumHeight(140)
        self._log_panel.setMinimumHeight(110)
        self._left_splitter.addWidget(rules_card)
        self._left_splitter.addWidget(self._log_panel)
        self._left_splitter.setStretchFactor(0, 2)
        self._left_splitter.setStretchFactor(1, 1)
        self._left_splitter.setSizes([380, 200])

        self._body_splitter.addWidget(self._left_splitter)
        self._body_splitter.addWidget(self._build_editor_scroll())
        self._body_splitter.setStretchFactor(0, 1)
        self._body_splitter.setStretchFactor(1, 2)
        self._body_splitter.setSizes([340, 740])

        layout.addWidget(self._body_splitter, 1)
        return page

    def _build_command_bar(self, initial_seconds: float) -> QWidget:
        bar = SimpleCardWidget()
        bar.setFixedHeight(62)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(14, 10, 14, 10)
        lay.setSpacing(12)

        self._start_btn = PrimaryPushButton("Start", self, FIF.PLAY)
        self._start_btn.setFixedWidth(108)
        self._start_btn.clicked.connect(self._toggle_running)
        lay.addWidget(self._start_btn)

        lay.addWidget(CaptionLabel("Hotkey"))
        self._hotkey_button = HotkeyButton()
        self._hotkey_button.hotkey_changed.connect(self._on_hotkey_changed)
        lay.addWidget(self._hotkey_button)

        lay.addWidget(_VLine())

        lay.addWidget(CaptionLabel("Interval"))
        self._interval_spin = DoubleSpinBox()
        self._interval_spin.setRange(0.1, 86400.0)
        self._interval_spin.setDecimals(1)
        self._interval_spin.setSingleStep(0.5)
        self._interval_spin.setValue(float(initial_seconds))
        self._interval_spin.setFixedWidth(130)
        self._interval_spin.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._interval_spin.valueChanged.connect(self._on_interval_changed)
        lay.addWidget(self._interval_spin)
        lay.addWidget(CaptionLabel("s"))

        lay.addWidget(_VLine())

        self._status_dot = StatusDot()
        lay.addWidget(self._status_dot)
        self._status_label = StrongBodyLabel("Stopped")
        self._status_label.setStyleSheet(f"color: {STATUS_STOPPED};")
        lay.addWidget(self._status_label)

        self._countdown_label = CaptionLabel("")
        self._countdown_label.setStyleSheet("color: #a1a1aa; font-family: Consolas; font-size: 11pt;")
        self._countdown_label.setFixedWidth(56)
        lay.addSpacing(4)
        lay.addWidget(self._countdown_label)

        self._action_status = CaptionLabel("")
        self._action_status.setStyleSheet("color: #a1a1aa; font-style: italic;")
        # When the toolbar is squeezed horizontally the action-status label
        # should collapse before any fixed-width control loses a pixel.
        # Ignored horizontal policy tells Qt this widget has no min width.
        self._action_status.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self._action_status.setMinimumWidth(0)
        lay.addWidget(self._action_status)

        lay.addStretch(1)

        # Rules / Bridge selector: lives in the toolbar so the body has
        # the full window height for actual content. Switches the
        # QStackedWidget that _build_central wires up.
        self._tab_strip = SegmentedWidget(self)
        self._tab_strip.addItem(
            routeKey="rules", text="Rules",
            onClick=lambda: self._tab_stack.setCurrentIndex(0),
        )
        self._tab_strip.addItem(
            routeKey="bridge", text="Bridge",
            onClick=lambda: self._tab_stack.setCurrentIndex(1),
        )
        self._tab_strip.setCurrentItem("rules")
        # SegmentedWidget defaults to a generous height that gets
        # awkwardly stretched inside the fixed-height toolbar (gap below
        # the buttons). Pin it to fit between the toolbar's vertical
        # margins.
        self._tab_strip.setFixedHeight(36)
        self._tab_strip.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        lay.addWidget(self._tab_strip)

        self._collapse_btn = ToolButton(FIF.UP)
        self._collapse_btn.setToolTip("Collapse to toolbar only")
        self._collapse_btn.setFixedSize(32, 28)
        self._collapse_btn.clicked.connect(self._toggle_body_collapsed)
        lay.addWidget(self._collapse_btn)

        return bar

    def _build_rules_card(self) -> QWidget:
        card = CollapsibleCard("Rules")
        card.expanded_changed.connect(self._on_left_card_toggled)
        self._rules_card = card
        card.setMinimumWidth(260)
        body = QVBoxLayout()
        body.setContentsMargins(2, 0, 2, 0)
        body.setSpacing(8)

        self._rules_list = TableWidget()
        self._rules_list.setColumnCount(3)
        self._rules_list.setHorizontalHeaderLabels(["Name", "On", "Action"])
        self._rules_list.verticalHeader().setVisible(False)
        self._rules_list.horizontalHeader().setVisible(True)
        self._rules_list.horizontalHeader().setHighlightSections(False)
        self._rules_list.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self._rules_list.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._rules_list.setSelectionMode(QAbstractItemView.SingleSelection)
        self._rules_list.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self._rules_list.setShowGrid(False)
        self._rules_list.setBorderVisible(True)
        self._rules_list.setBorderRadius(8)
        self._rules_list.setWordWrap(False)
        self._rules_list.verticalHeader().setDefaultSectionSize(32)

        header = self._rules_list.horizontalHeader()
        # Name and On both fit their content (On is just a small checkbox so
        # it sizes to the header label), and Action takes whatever's left —
        # so it's the first column to give up space when the window narrows.
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2, QHeaderView.Stretch)
        header.setMinimumSectionSize(24)

        self._rules_list.itemSelectionChanged.connect(
            lambda: self._load_selected_rule(self._rules_list.currentRow())
        )
        body.addWidget(self._rules_list, 1)

        buttons = QHBoxLayout()
        buttons.setSpacing(6)
        self._add_btn = PushButton(FIF.ADD, "Add")
        self._delete_btn = PushButton(FIF.DELETE, "Delete")
        self._up_btn = ToolButton(FIF.UP)
        self._down_btn = ToolButton(FIF.DOWN)
        self._add_btn.clicked.connect(self._add_rule)
        self._delete_btn.clicked.connect(self._delete_rule)
        self._up_btn.clicked.connect(lambda: self._move_rule(-1))
        self._down_btn.clicked.connect(lambda: self._move_rule(1))
        buttons.addWidget(self._add_btn)
        buttons.addWidget(self._delete_btn)
        buttons.addStretch(1)
        buttons.addWidget(self._up_btn)
        buttons.addWidget(self._down_btn)
        body.addLayout(buttons)

        card.viewLayout.addLayout(body)
        return card

    def _build_editor_scroll(self) -> QWidget:
        outer = QWidget()
        outer_lay = QVBoxLayout(outer)
        outer_lay.setContentsMargins(0, 0, 0, 0)
        outer_lay.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)

        content = QWidget()
        content.setMinimumWidth(340)
        stack = QVBoxLayout(content)
        # Symmetric margins so the editor cards line up with the bridge
        # tab's right column on the right edge. Earlier this was 2/0/6/0
        # which made the rules editor extend a few pixels past where the
        # bridge log card would.
        stack.setContentsMargins(2, 0, 2, 0)
        stack.setSpacing(12)

        stack.addWidget(self._build_basics_card())
        stack.addWidget(self._build_template_card())
        stack.addWidget(self._build_scope_card())
        stack.addWidget(self._build_editor_actions())
        stack.addStretch(1)

        scroll.setWidget(content)
        outer_lay.addWidget(scroll, 1)
        return outer

    def _build_basics_card(self) -> QWidget:
        card = CollapsibleCard("Basics")
        self._basics_card = card

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(6)

        # Enabled lives in the rules table now (one checkbox per row), so the
        # Basics row is just Name | Action | Text.
        self._name_label = CaptionLabel("Name")
        self._action_label = CaptionLabel("Action")
        self._text_label = CaptionLabel("Text")
        grid.addWidget(self._name_label, 0, 0)
        grid.addWidget(self._action_label, 0, 1)
        grid.addWidget(self._text_label, 0, 2)

        self._name_edit = LineEdit()
        self._action_combo = ComboBox()
        self._action_combo.addItems(ACTION_TYPES)
        self._action_combo.currentTextChanged.connect(self._update_action_fields)
        self._text_edit = LineEdit()
        self._text_edit.setPlaceholderText("typed before Enter")
        # Autosave: every input change writes back to the cfg under the
        # cfg lock and persists. Loading a rule sets _suppress_autosave
        # so we don't echo the just-loaded values back over the rule we
        # navigated away from.
        self._name_edit.editingFinished.connect(self._autosave_rule)
        self._text_edit.editingFinished.connect(self._autosave_rule)
        self._action_combo.currentTextChanged.connect(self._autosave_rule)

        grid.addWidget(self._name_edit, 1, 0)
        grid.addWidget(self._action_combo, 1, 1)
        grid.addWidget(self._text_edit, 1, 2)

        grid.setColumnStretch(0, 3)
        grid.setColumnStretch(1, 2)
        grid.setColumnStretch(2, 3)

        card.viewLayout.addLayout(grid)
        return card

    def _build_template_card(self) -> QWidget:
        card = CollapsibleCard("Match")
        self._match_card = card

        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(12)
        grid.setVerticalSpacing(10)

        # Matcher toggle: shows whether the active rule clicks on a template
        # (pattern) or on a color. Tapping a side switches the rule's matcher
        # and surfaces the right preview / inputs.
        self._matcher_seg = SegmentedWidget()
        self._matcher_seg.addItem(
            MATCHER_TEMPLATE, "Pattern", lambda: self._set_matcher(MATCHER_TEMPLATE)
        )
        self._matcher_seg.addItem(
            MATCHER_COLOR, "Color", lambda: self._set_matcher(MATCHER_COLOR)
        )
        self._matcher_seg.setCurrentItem(MATCHER_TEMPLATE)
        seg_row = QHBoxLayout()
        seg_row.setContentsMargins(0, 0, 0, 0)
        seg_row.addWidget(self._matcher_seg)
        seg_row.addStretch(1)
        grid.addLayout(seg_row, 0, 0, 1, 2)

        self._template_combo = ComboBox()
        self._template_combo.setMinimumWidth(180)
        self._template_combo.currentTextChanged.connect(self._on_template_selected)
        self._rename_template_btn = ToolButton(FIF.EDIT)
        self._rename_template_btn.setToolTip("Rename selected template")
        self._rename_template_btn.setFixedSize(32, 28)
        self._rename_template_btn.clicked.connect(self._rename_selected_template)
        self._delete_template_btn = ToolButton(FIF.DELETE)
        self._delete_template_btn.setToolTip("Delete selected template file")
        self._delete_template_btn.setFixedSize(32, 28)
        self._delete_template_btn.clicked.connect(self._delete_selected_template)
        # Color-matcher source: a dropdown listing every named colour from any
        # rule. Selecting one copies its RGB + name + capture area onto the
        # current rule. Hidden in template mode.
        self._color_library_combo = ComboBox()
        self._color_library_combo.setMinimumWidth(180)
        self._color_library_combo.currentIndexChanged.connect(self._on_color_library_selected)
        self._color_library_combo.setVisible(False)
        # Rename / delete tools for the colour library, mirroring the template
        # pair. Library-level: they act on every rule that shares the chosen
        # colour, so renaming "my red" updates every "my red" rule.
        self._rename_color_btn = ToolButton(FIF.EDIT)
        self._rename_color_btn.setToolTip("Rename selected color (all rules using it)")
        self._rename_color_btn.setFixedSize(32, 28)
        self._rename_color_btn.setVisible(False)
        self._rename_color_btn.clicked.connect(self._rename_selected_color)
        self._delete_color_btn = ToolButton(FIF.DELETE)
        self._delete_color_btn.setToolTip("Clear selected color from all rules using it")
        self._delete_color_btn.setFixedSize(32, 28)
        self._delete_color_btn.setVisible(False)
        self._delete_color_btn.clicked.connect(self._delete_selected_color)
        # Capture buttons follow the matcher selection — only the one that
        # makes sense for the current side is shown.
        self._capture_pattern_btn = PrimaryPushButton(FIF.CAMERA, "Capture pattern")
        self._capture_pattern_btn.clicked.connect(self._capture_template)
        self._capture_color_btn = PrimaryPushButton(FIF.PALETTE, "Capture color")
        self._capture_color_btn.clicked.connect(self._capture_color)

        top_row = QHBoxLayout()
        top_row.setSpacing(6)
        top_row.addWidget(self._template_combo, 1)
        top_row.addWidget(self._color_library_combo, 1)
        top_row.addWidget(self._rename_template_btn)
        top_row.addWidget(self._delete_template_btn)
        top_row.addWidget(self._rename_color_btn)
        top_row.addWidget(self._delete_color_btn)
        top_row.addSpacing(6)
        top_row.addWidget(self._capture_pattern_btn)
        top_row.addWidget(self._capture_color_btn)
        grid.addLayout(top_row, 1, 0, 1, 2)

        # Preview: either a template image (template matcher) or a flat color
        # swatch (color matcher). Same fixed footprint, only one is visible.
        self._preview_label = QLabel("(no template selected)")
        self._preview_label.setAlignment(Qt.AlignCenter)
        self._preview_label.setFixedSize(120, 64)
        self._preview_label.setStyleSheet(
            "QLabel { background: rgba(0,0,0,0.25); "
            "border: 1px dashed rgba(255,255,255,0.18); border-radius: 6px; "
            "color: #a1a1aa; }"
        )
        self._color_swatch = QLabel("")
        self._color_swatch.setFixedSize(120, 64)
        self._color_swatch.setVisible(False)

        preview_stack = QHBoxLayout()
        preview_stack.setContentsMargins(0, 0, 0, 0)
        preview_stack.setSpacing(0)
        preview_stack.addWidget(self._preview_label)
        preview_stack.addWidget(self._color_swatch)
        grid.addLayout(preview_stack, 2, 0, 2, 1)

        meta_box = QWidget()
        meta_lay = QVBoxLayout(meta_box)
        meta_lay.setContentsMargins(0, 2, 0, 0)
        meta_lay.setSpacing(6)

        self._threshold_row = QHBoxLayout()
        self._threshold_row.setSpacing(6)
        self._threshold_label = CaptionLabel("Threshold")
        self._threshold_row.addWidget(self._threshold_label)
        self._threshold_spin = DoubleSpinBox()
        self._threshold_spin.setRange(0.0, 1.0)
        self._threshold_spin.setDecimals(2)
        self._threshold_spin.setSingleStep(0.01)
        self._threshold_spin.setValue(0.90)
        self._threshold_spin.setFixedWidth(130)
        self._threshold_spin.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self._threshold_spin.valueChanged.connect(self._autosave_rule)
        self._threshold_row.addWidget(self._threshold_spin)
        self._threshold_row.addStretch(1)
        meta_lay.addLayout(self._threshold_row)

        # Color name (color matcher only) — friendly label that shows up in
        # the color library dropdown for re-use across rules.
        self._color_name_row = QHBoxLayout()
        self._color_name_row.setSpacing(6)
        self._color_name_label = CaptionLabel("Color name")
        self._color_name_row.addWidget(self._color_name_label)
        self._color_name_edit = LineEdit()
        self._color_name_edit.setPlaceholderText("e.g. RunBlue")
        self._color_name_edit.editingFinished.connect(self._on_color_name_edited)
        self._color_name_row.addWidget(self._color_name_edit, 1)
        meta_lay.addLayout(self._color_name_row)

        self._template_meta = BodyLabel("")
        self._template_meta.setStyleSheet("color: #9ca3af;")
        self._template_meta.setWordWrap(True)
        self._template_meta.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        meta_lay.addWidget(self._template_meta)
        meta_lay.addStretch(1)

        grid.addWidget(meta_box, 2, 1, 2, 1)
        grid.setColumnStretch(1, 1)

        card.viewLayout.addLayout(grid)
        return card

    def _build_scope_card(self) -> QWidget:
        card = CollapsibleCard("Search scope", expanded=False)
        self._scope_card = card

        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(10)

        self._region_label = BodyLabel("All monitors")
        self._region_label.setStyleSheet("color: #d4d4d8;")
        row.addWidget(self._region_label, 1)

        cap_btn = PushButton(FIF.VIEW, "Capture region")
        cap_btn.clicked.connect(self._capture_search_region)
        all_btn = PushButton(FIF.TILES, "All monitors")
        all_btn.clicked.connect(self._use_all_monitors)
        pick_btn = PushButton(FIF.ROBOT, "Pick monitor")
        pick_btn.clicked.connect(self._pick_monitor)

        row.addWidget(cap_btn)
        row.addWidget(all_btn)
        row.addWidget(pick_btn)

        card.viewLayout.addLayout(row)
        return card

    def _build_editor_actions(self) -> QWidget:
        wrap = QWidget()
        row = QHBoxLayout(wrap)
        row.setContentsMargins(2, 0, 2, 0)
        row.setSpacing(8)
        # Save is now automatic on every input change (see _autosave_rule).
        # The button is gone; Test match is centred on the row.
        row.addStretch(1)
        test_btn = PushButton(FIF.PLAY, "Test match")
        test_btn.clicked.connect(self._test_selected_rule)
        row.addWidget(test_btn)
        row.addStretch(1)
        return wrap

    def _build_log_panel(self) -> QWidget:
        card = CollapsibleCard("Log")
        card.expanded_changed.connect(self._on_left_card_toggled)
        self._log_card = card

        self._log_box = FluentPlainTextEdit()
        self._log_box.setReadOnly(True)
        self._log_box.setMaximumBlockCount(1000)

        card.viewLayout.addWidget(self._log_box)
        return card

    # ---------- bridge tab ----------

    def _build_bridge_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(12)

        # Inline service toggle row. Centered horizontally with stretch
        # spacers on both sides so the [label] [switch] [status] cluster
        # sits in the middle of the page rather than hugging the left
        # edge with a long status string trailing off to the right.
        svc_row = QHBoxLayout()
        svc_row.setContentsMargins(2, 4, 2, 4)
        svc_row.setSpacing(10)
        svc_row.setAlignment(Qt.AlignVCenter)
        svc_row.addStretch(1)
        svc_label = BodyLabel("Bridge service")
        svc_row.addWidget(svc_label, 0, Qt.AlignVCenter)
        self._bridge_switch = SwitchButton()
        self._bridge_switch.setOnText("On")
        self._bridge_switch.setOffText("Off")
        self._bridge_switch.checkedChanged.connect(self._on_bridge_switch_toggled)
        svc_row.addWidget(self._bridge_switch, 0, Qt.AlignVCenter)
        self._bridge_switch_status = BodyLabel(
            "Stopped — toggle on to start the FastAPI service."
        )
        self._bridge_switch_status.setTextFormat(Qt.RichText)
        self._bridge_switch_status.setOpenExternalLinks(True)
        svc_row.addWidget(self._bridge_switch_status, 0, Qt.AlignVCenter)
        svc_row.addStretch(1)
        outer.addLayout(svc_row)

        # Templates card — idle marker + optional askuser (multi-choice)
        # marker. The detector matches both inside each window region
        # and reports back a (idle, asking) pair for each window.
        tpl_card = CollapsibleCard("Detection templates")
        tpl_info = ToolButton(FIF.INFO)
        tpl_info.setToolTip(
            "Idle: a visual cue that means a Cursor window is ready for "
            "input (Continue button, send icon, etc.).\n"
            "Question: optional second template for Cursor's "
            "AskUserQuestion multi-choice prompt (e.g. the Submit answers "
            "pill at the bottom of the question card). Leave blank to "
            "skip asking-state detection."
        )
        tpl_info.setFixedSize(22, 22)
        tpl_card.headerLayout.insertWidget(1, tpl_info)

        tpl_body = QVBoxLayout()
        tpl_body.setContentsMargins(2, 0, 2, 0)
        tpl_body.setSpacing(8)

        # Idle template row.
        tpl_row = QHBoxLayout()
        tpl_row.setSpacing(8)
        idle_lbl = BodyLabel("Idle")
        idle_lbl.setMinimumWidth(56)
        tpl_row.addWidget(idle_lbl)
        self._bridge_template_thumb = QLabel()
        self._bridge_template_thumb.setFixedSize(120, 50)
        self._bridge_template_thumb.setAlignment(Qt.AlignCenter)
        self._bridge_template_thumb.setStyleSheet(
            "border: 1px dashed #555; border-radius: 6px; "
            "background: #1f2129; color: #6f7180; font-size: 11px;"
        )
        self._bridge_template_thumb.setText("(no template)")
        tpl_row.addWidget(self._bridge_template_thumb)
        capture_tpl_btn = PrimaryPushButton(FIF.CAMERA, "Capture")
        capture_tpl_btn.clicked.connect(self._capture_bridge_idle_template)
        tpl_row.addWidget(capture_tpl_btn)
        test_tpl_btn = PushButton(FIF.PLAY, "Test")
        test_tpl_btn.setToolTip("Run idle detection across all configured windows now")
        test_tpl_btn.clicked.connect(self._test_bridge_idle_match)
        tpl_row.addWidget(test_tpl_btn)
        tpl_row.addStretch(1)
        tpl_body.addLayout(tpl_row)

        # Askuser template row — same shape, no Test (we re-use the
        # idle Test runner since it now reports both states).
        ask_row = QHBoxLayout()
        ask_row.setSpacing(8)
        ask_lbl = BodyLabel("Question")
        ask_lbl.setMinimumWidth(56)
        ask_row.addWidget(ask_lbl)
        self._bridge_askuser_thumb = QLabel()
        self._bridge_askuser_thumb.setFixedSize(120, 50)
        self._bridge_askuser_thumb.setAlignment(Qt.AlignCenter)
        self._bridge_askuser_thumb.setStyleSheet(
            "border: 1px dashed #555; border-radius: 6px; "
            "background: #1f2129; color: #6f7180; font-size: 11px;"
        )
        self._bridge_askuser_thumb.setText("(optional)")
        ask_row.addWidget(self._bridge_askuser_thumb)
        capture_ask_btn = PushButton(FIF.CAMERA, "Capture")
        capture_ask_btn.setToolTip(
            "Capture a marker for the AskUserQuestion multi-choice prompt"
        )
        capture_ask_btn.clicked.connect(self._capture_bridge_askuser_template)
        ask_row.addWidget(capture_ask_btn)
        ask_row.addStretch(1)
        tpl_body.addLayout(ask_row)

        thr_row = QHBoxLayout()
        thr_row.setSpacing(8)
        thr_row.addWidget(BodyLabel("Match threshold:"))
        self._bridge_threshold_spin = DoubleSpinBox()
        self._bridge_threshold_spin.setRange(0.50, 1.00)
        self._bridge_threshold_spin.setSingleStep(0.05)
        self._bridge_threshold_spin.setDecimals(2)
        bridge_cfg = self._cfg.get("bridge", {})
        self._bridge_threshold_spin.setValue(float(bridge_cfg.get("idle_threshold", 0.90)))
        self._bridge_threshold_spin.valueChanged.connect(self._on_bridge_threshold_changed)
        thr_row.addWidget(self._bridge_threshold_spin)
        thr_row.addStretch(1)
        tpl_body.addLayout(thr_row)

        tpl_card.viewLayout.addLayout(tpl_body)
        outer.addWidget(tpl_card)

        # Cursor windows card.
        win_card = CollapsibleCard("Cursor windows")
        win_info = ToolButton(FIF.INFO)
        win_info.setToolTip(
            "Drag a box around each Cursor window. The bridge scans inside "
            "that box for the idle template. Click a name to rename."
        )
        win_info.setFixedSize(22, 22)
        win_card.headerLayout.insertWidget(1, win_info)

        win_body = QVBoxLayout()
        win_body.setContentsMargins(2, 0, 2, 0)
        win_body.setSpacing(8)

        self._bridge_windows_table = TableWidget()
        self._bridge_windows_table.setColumnCount(3)
        self._bridge_windows_table.setHorizontalHeaderLabels(["Name", "Region", ""])
        self._bridge_windows_table.verticalHeader().setVisible(False)
        self._bridge_windows_table.horizontalHeader().setHighlightSections(False)
        self._bridge_windows_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self._bridge_windows_table.setShowGrid(False)
        self._bridge_windows_table.setBorderVisible(True)
        self._bridge_windows_table.setBorderRadius(8)
        self._bridge_windows_table.verticalHeader().setDefaultSectionSize(40)
        header = self._bridge_windows_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        # Setup row — sits above the windows table. The combo lists
        # all setups; switching the combo activates that setup (its
        # windows replace the live ones). "New" creates a fresh empty
        # setup and activates it; the trash icon deletes the current
        # one. The active setup's windows ARE the live ``bridge.windows``:
        # every persist mirrors them back, so switching never loses
        # edits.
        setup_row = QHBoxLayout()
        setup_row.setSpacing(6)
        setup_row.addWidget(BodyLabel("Setup:"))
        self._bridge_setup_combo = ComboBox()
        self._bridge_setup_combo.setMinimumWidth(180)
        self._bridge_setup_combo.currentIndexChanged.connect(
            self._on_bridge_setup_combo_changed
        )
        setup_row.addWidget(self._bridge_setup_combo, 1)
        setup_new_btn = PushButton(FIF.ADD, "New setup")
        setup_new_btn.setToolTip(
            "Create a fresh empty setup and switch to it. The current "
            "setup is preserved — switch back via the dropdown."
        )
        setup_new_btn.clicked.connect(self._new_bridge_setup)
        setup_row.addWidget(setup_new_btn)
        setup_bind_btn = ToolButton(FIF.PIN)
        setup_bind_btn.setToolTip(
            "Bind the active setup to the current Windows virtual "
            "desktop. Switching to that workspace later (via "
            "Ctrl+Win+Right / Left, or the phone's ◀ ▶ buttons) "
            "auto-activates this setup."
        )
        setup_bind_btn.clicked.connect(self._bind_active_setup_to_workspace)
        setup_row.addWidget(setup_bind_btn)
        setup_del_btn = ToolButton(FIF.DELETE)
        setup_del_btn.setToolTip("Delete the current setup")
        setup_del_btn.clicked.connect(self._delete_bridge_setup)
        setup_row.addWidget(setup_del_btn)
        win_body.addLayout(setup_row)

        win_body.addWidget(self._bridge_windows_table)

        add_row = QHBoxLayout()
        add_btn = PushButton(FIF.ADD, "Add window")
        add_btn.clicked.connect(self._add_bridge_window)
        add_row.addWidget(add_btn)
        auto_btn = PushButton(FIF.SEARCH, "Auto-detect")
        auto_btn.setToolTip(
            "Scan visible Cursor windows on this desktop via Win32 and "
            "preview the result before adding them to the current setup. "
            "To keep the existing setup intact, click 'New setup' first."
        )
        auto_btn.clicked.connect(self._auto_detect_bridge_windows)
        add_row.addWidget(auto_btn)
        add_row.addStretch(1)
        win_body.addLayout(add_row)

        win_card.viewLayout.addLayout(win_body)

        # Bridge log card. Sized like the left-column cards: when
        # collapsed the card clamps to header height (44 px); when
        # expanded it stretches to fill the row. The Expanding vertical
        # policy is what keeps it tracking the body_split's height as
        # the user resizes the window — without it HeaderCardWidget
        # sticks to a smaller sizeHint and the log card stops following
        # the row height on resize. (No minHeight clamp — the toggle
        # handler manages collapse via maxHeight.)
        log_card = CollapsibleCard("Bridge log")
        log_card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        log_body = QVBoxLayout()
        log_body.setContentsMargins(2, 0, 2, 0)
        log_body.setSpacing(6)
        log_desc = CaptionLabel(
            "Bridge events that the phone UI would also see — busy↔idle transitions, "
            "captures, saves, errors."
        )
        log_desc.setWordWrap(True)
        log_body.addWidget(log_desc)
        self._bridge_log_view = FluentPlainTextEdit()
        self._bridge_log_view.setReadOnly(True)
        self._bridge_log_view.setMaximumBlockCount(500)
        self._bridge_log_view.setMinimumHeight(140)
        log_body.addWidget(self._bridge_log_view)
        log_card.viewLayout.addLayout(log_body)

        # Layout: left column [idle + windows] | right column [log].
        # This lets the cards use vertical space without each card
        # being a full-width letterbox; the log gets the right half.
        left_col = QSplitter(Qt.Vertical)
        left_col.setChildrenCollapsible(False)
        left_col.setHandleWidth(6)
        left_col.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        left_col.addWidget(tpl_card)
        left_col.addWidget(win_card)
        left_col.setStretchFactor(0, 0)
        left_col.setStretchFactor(1, 1)
        left_col.setSizes([260, 360])

        body_split = QSplitter(Qt.Horizontal)
        body_split.setChildrenCollapsible(False)
        body_split.setHandleWidth(6)
        # Same Expanding-on-both-axes treatment as the rules tab so
        # collapsing children doesn't shrink the splitter itself.
        body_split.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        body_split.addWidget(left_col)
        body_split.addWidget(log_card)
        body_split.setStretchFactor(0, 1)
        body_split.setStretchFactor(1, 1)
        body_split.setSizes([520, 460])

        # Stash everything the collapse handlers need.
        self._bridge_tpl_card = tpl_card
        self._bridge_win_card = win_card
        self._bridge_log_card = log_card
        self._bridge_left_splitter = left_col
        self._bridge_body_splitter = body_split
        self._bridge_left_remembered = {"tpl": 260, "win": 360}
        self._bridge_body_remembered = {"log": 460}
        tpl_card.expanded_changed.connect(self._on_bridge_left_card_toggled)
        win_card.expanded_changed.connect(self._on_bridge_left_card_toggled)
        log_card.expanded_changed.connect(self._on_bridge_log_card_toggled)

        outer.addWidget(body_split, 1)
        return page

    # ---- bridge collapse handlers ----

    def _on_bridge_left_card_toggled(self, _expanded: bool) -> None:
        """Match the rules-tab behaviour: when one of the two cards in
        the left vertical splitter collapses, clamp its max-height so the
        splitter actually shrinks the pane to the header bar instead of
        leaving a useless empty box. Remember the prior expanded size so
        a re-open restores it."""
        HEADER_H = 44
        sizes = self._bridge_left_splitter.sizes()
        sender = self.sender()
        if (
            sender is self._bridge_tpl_card
            and not self._bridge_tpl_card.isExpanded()
            and sizes[0] > HEADER_H
        ):
            self._bridge_left_remembered["tpl"] = sizes[0]
        elif (
            sender is self._bridge_win_card
            and not self._bridge_win_card.isExpanded()
            and sizes[1] > HEADER_H
        ):
            self._bridge_left_remembered["win"] = sizes[1]

        for card, min_open in (
            (self._bridge_tpl_card, 140),
            (self._bridge_win_card, 140),
        ):
            if card.isExpanded():
                card.setMinimumHeight(min_open)
                card.setMaximumHeight(16777215)
            else:
                card.setMinimumHeight(0)
                card.setMaximumHeight(HEADER_H)

        total = sum(sizes) or (self._bridge_left_splitter.height() or 600)
        tpl_exp = self._bridge_tpl_card.isExpanded()
        win_exp = self._bridge_win_card.isExpanded()
        if tpl_exp and win_exp:
            t = self._bridge_left_remembered.get("tpl", 260)
            self._bridge_left_splitter.setSizes([t, max(HEADER_H, total - t)])
        elif tpl_exp:
            self._bridge_left_splitter.setSizes([total - HEADER_H, HEADER_H])
        elif win_exp:
            self._bridge_left_splitter.setSizes([HEADER_H, total - HEADER_H])
        else:
            self._bridge_left_splitter.setSizes([HEADER_H, HEADER_H])

    def _on_bridge_log_card_toggled(self, _expanded: bool) -> None:
        """Mirror the rules-tab pattern: clamp maxHeight to the header
        bar (44 px) on collapse so the card actually shrinks instead of
        sitting around its previous height; lift the clamp on expand so
        the splitter is free to give it the full row height. We
        deliberately don't touch splitter widths here — the left column
        keeps its horizontal share of the row whatever the log is doing."""
        HEADER_H = 44
        if self._bridge_log_card.isExpanded():
            self._bridge_log_card.setMinimumHeight(0)
            self._bridge_log_card.setMaximumHeight(16777215)
        else:
            self._bridge_log_card.setMinimumHeight(0)
            self._bridge_log_card.setMaximumHeight(HEADER_H)

    # ---- bridge tab actions ----

    def _bridge_log(self, msg: str) -> None:
        from datetime import datetime as _dt

        view = getattr(self, "_bridge_log_view", None)
        if view is None:
            return
        view.appendPlainText(f"{_dt.now().strftime('%H:%M:%S')}  {msg}")

    def _refresh_bridge_template_view(self) -> None:
        bridge = self._cfg.get("bridge") or {}
        self._refresh_template_thumb(
            getattr(self, "_bridge_template_thumb", None),
            bridge.get("idle_template_path"),
            empty_label="(no template)",
        )
        self._refresh_template_thumb(
            getattr(self, "_bridge_askuser_thumb", None),
            bridge.get("askuser_template_path"),
            empty_label="(optional)",
        )

    def _refresh_template_thumb(self, thumb, path, empty_label="(no template)") -> None:
        """Render ``path`` into ``thumb`` (a QLabel), or the empty
        placeholder text if path is None/missing/unreadable. Factored
        so both detection templates use the same render path."""
        if thumb is None:
            return
        if not path:
            thumb.clear()
            thumb.setText(empty_label)
            return
        full = resolve_template_path(path)
        if not full or not full.exists():
            thumb.clear()
            thumb.setText("(missing)")
            return
        pix = QPixmap(str(full))
        if pix.isNull():
            thumb.clear()
            thumb.setText("(unreadable)")
            return
        thumb.setPixmap(
            pix.scaled(thumb.size(), Qt.KeepAspectRatio, Qt.SmoothTransformation)
        )

    def _test_bridge_idle_match(self) -> None:
        """Dry-run idle detection right now, regardless of whether the
        worker is running, so the user can verify the template + window
        regions without flipping the bridge service on."""
        bridge_cfg = self._cfg.get("bridge", {})
        if not bridge_cfg.get("idle_template_path"):
            self._bridge_log("test: capture an idle template first")
            return
        if not bridge_cfg.get("windows"):
            self._bridge_log("test: add at least one Cursor window first")
            return
        try:
            states = evaluate_bridge_windows(bridge_cfg)
        except Exception as exc:
            self._bridge_log(f"test failed: {exc}")
            return
        if not states:
            self._bridge_log("test: detector returned no states (template file missing?)")
            return
        self._bridge_log("test results:")
        for state in states:
            if not state.get("configured"):
                self._bridge_log(f"  • {state['name']}: not configured (no region)")
                continue
            if state.get("asking"):
                verdict = "asking"
            elif state["idle"]:
                verdict = "idle"
            else:
                verdict = "busy"
            self._bridge_log(
                f"  • {state['name']}: {verdict} (idle-score {state['score']:.3f})"
            )

    def _capture_bridge_template(
        self,
        filename: str,
        cfg_path_key: str,
        cfg_dpi_key: str,
        label: str,
    ) -> None:
        """Shared capture flow for the idle / askuser templates.

        Captures the drag bbox as RGB, detects the source monitor's
        DPI scale, then writes the base PNG plus one scaled variant
        per supported preset (100/125/150/175/200). Stores the source
        scale in config so the matcher can pick the right variant for
        each window's monitor at runtime."""
        import press_dpi as dpimod
        from press_store import write_template_with_dpi_variants

        try:
            ensure_vision()
        except Exception as exc:
            self._bridge_log(f"capture failed: {exc}")
            return
        bbox = capture_drag_bbox(self)
        if not bbox:
            self._bridge_log(f"{label} template capture cancelled")
            return
        try:
            rgb = capture_screen_rgb(tuple(bbox))
            source_scale = dpimod.scale_for_region(bbox)
            path = template_asset_path(filename)
            written = write_template_with_dpi_variants(path, rgb, source_scale)
            stored = serialize_template_path(path)
            with self._cfg_lock:
                self._cfg["bridge"][cfg_path_key] = stored
                self._cfg["bridge"][cfg_dpi_key] = source_scale
            self._persist()
            self._refresh_bridge_template_view()
            self._bridge_log(
                f"captured {label} template → {stored} "
                f"({bbox[2]}×{bbox[3]} px @ {int(round(source_scale * 100))}% DPI, "
                f"{len(written) - 1} scaled variants)"
            )
        except Exception as exc:
            self._bridge_log(f"capture failed: {exc}")

    def _capture_bridge_idle_template(self) -> None:
        self._capture_bridge_template(
            "bridge_idle.png",
            "idle_template_path",
            "idle_template_source_dpi",
            "idle",
        )

    def _capture_bridge_askuser_template(self) -> None:
        """Same flow as the idle template capture, but writes to the
        askuser slot. Optional template — leaving it empty means the
        bridge never reports "asking" state."""
        self._capture_bridge_template(
            "bridge_askuser.png",
            "askuser_template_path",
            "askuser_template_source_dpi",
            "askuser",
        )

    def _on_bridge_threshold_changed(self, value: float) -> None:
        with self._cfg_lock:
            self._cfg["bridge"]["idle_threshold"] = float(value)
        self._persist()

    def _add_bridge_window(self) -> None:
        existing = self._cfg.get("bridge", {}).get("windows", [])
        name = f"Cursor #{len(existing) + 1}"
        self._bridge_log(f"add window: drag a box around the new {name}…")
        bbox = capture_drag_bbox(self)
        if not bbox:
            self._bridge_log("add window cancelled")
            return
        win = default_bridge_window(name)
        win["region"] = [int(b) for b in bbox]
        with self._cfg_lock:
            self._cfg["bridge"]["windows"].append(win)
        self._persist()
        self._refresh_bridge_windows_table()
        self._bridge_log(
            f"added '{name}' at ({bbox[0]},{bbox[1]}) {bbox[2]}×{bbox[3]}"
        )

    def _refresh_bridge_setup_combo(self) -> None:
        """Populate the setup combo from the current config and select
        the active setup. Called on every mutation that touches
        bridge.setups / active_setup_id and on initial UI build."""
        combo = getattr(self, "_bridge_setup_combo", None)
        if combo is None:
            return
        # Block signals while we reset items / select index so the
        # currentIndexChanged handler (which activates a setup) doesn't
        # fire spuriously during a programmatic refresh.
        combo.blockSignals(True)
        combo.clear()
        bridge = self._cfg.get("bridge", {}) or {}
        setups = bridge.get("setups", []) or []
        active_id = bridge.get("active_setup_id")
        active_idx = 0
        # qfluentwidgets ComboBox.addItem signature is (text, icon=None,
        # userData=None). Passing the id positionally lands in `icon`,
        # not userData, so itemData() returns None and the change
        # handler bails out — keep the keyword explicit.
        for i, s in enumerate(setups):
            pin = " 📌" if s.get("workspace_id") else ""
            label = (
                f"{s.get('name', 'Setup')}{pin}  ·  "
                f"{len(s.get('windows', []))} win"
            )
            combo.addItem(label, userData=s.get("id"))
            if s.get("id") == active_id:
                active_idx = i
        if combo.count() > 0:
            combo.setCurrentIndex(active_idx)
        combo.blockSignals(False)

    def _on_bridge_setup_combo_changed(self, index: int) -> None:
        """Combo selection changed by the user → activate that setup.
        No confirm dialog; the outgoing setup's windows are auto-
        mirrored so switching is non-destructive."""
        combo = self._bridge_setup_combo
        if index < 0 or combo.count() == 0:
            return
        sid = combo.itemData(index)
        if not isinstance(sid, str):
            return
        bridge = self._cfg.get("bridge", {}) or {}
        if bridge.get("active_setup_id") == sid:
            return
        from press_store import activate_setup

        with self._cfg_lock:
            ok = activate_setup(self._cfg, sid)
        if not ok:
            return
        self._persist()
        self._refresh_bridge_windows_table()
        self._refresh_bridge_setup_combo()
        self._worker.reset_window_tracking()
        name = combo.itemText(index).split("·")[0].strip()
        self._bridge_log(f"switched to setup '{name}'")

    def _new_bridge_setup(self) -> None:
        """Create a fresh empty setup and switch to it. The previous
        setup is preserved automatically (its windows were already
        mirrored on the last persist)."""
        from PySide6.QtWidgets import QInputDialog

        existing = (self._cfg.get("bridge", {}) or {}).get("setups", []) or []
        suggested = f"Setup {len(existing) + 1}"
        name, ok = QInputDialog.getText(
            self,
            "New setup",
            "Name this setup — you'll see it in the dropdown:",
            text=suggested,
        )
        if not ok:
            return
        clean = (name or "").strip()
        if not clean:
            return
        from press_store import new_setup

        with self._cfg_lock:
            new_setup(self._cfg, clean)
        self._persist()
        self._refresh_bridge_setup_combo()
        self._refresh_bridge_windows_table()
        self._worker.reset_window_tracking()
        self._bridge_log(f"created setup '{clean}' (empty)")

    def _delete_bridge_setup(self) -> None:
        """Remove the currently active setup. If others exist, the
        first remaining becomes active; otherwise a fresh empty
        Default is created. Confirms first."""
        bridge = self._cfg.get("bridge", {}) or {}
        sid = bridge.get("active_setup_id")
        if not sid:
            return
        setups = bridge.get("setups", []) or []
        current = next((s for s in setups if s.get("id") == sid), None)
        name = current.get("name", "Setup") if current else "Setup"
        from PySide6.QtWidgets import QMessageBox

        confirmed = QMessageBox.question(
            self,
            "Delete setup",
            f"Delete the setup '{name}' and its windows?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirmed != QMessageBox.Yes:
            return
        from press_store import delete_setup

        with self._cfg_lock:
            removed = delete_setup(self._cfg, sid)
        if not removed:
            return
        self._persist()
        self._refresh_bridge_setup_combo()
        self._refresh_bridge_windows_table()
        self._worker.reset_window_tracking()
        self._bridge_log(f"deleted setup '{name}'")

    def _bind_active_setup_to_workspace(self) -> None:
        """Stamp the current Windows virtual-desktop GUID onto the
        active setup's ``workspace_id``. After binding, any future
        switch to this workspace auto-activates the setup."""
        import press_workspace as wsmod

        wid = wsmod.current_id()
        if not wid:
            self._bridge_log(
                "bind workspace: couldn't read current workspace GUID "
                "(non-Windows, or COM call failed)"
            )
            return
        bridge = self._cfg.get("bridge", {}) or {}
        active_id = bridge.get("active_setup_id")
        if not active_id:
            return
        with self._cfg_lock:
            for s in self._cfg["bridge"].get("setups", []) or []:
                if s.get("id") == active_id:
                    s["workspace_id"] = wid
                    name = s.get("name", "Setup")
                    break
            else:
                return
        self._persist()
        # Remember so the poll loop doesn't immediately re-activate
        # (this *is* the workspace the setup just got bound to).
        self._last_seen_workspace_id = wid
        self._refresh_bridge_setup_combo()
        self._bridge_log(f"bound '{name}' to current workspace")

    def _poll_workspace(self) -> None:
        """Detect virtual-desktop switches and auto-activate the
        setup bound to the new workspace. Fires every 1.5 s; cheap
        no-op when nothing changed or no setup is bound."""
        import press_workspace as wsmod

        wid = wsmod.current_id()
        if not wid or wid == self._last_seen_workspace_id:
            return
        self._last_seen_workspace_id = wid
        bridge = self._cfg.get("bridge", {}) or {}
        if bridge.get("active_setup_id") is None:
            return
        match = next(
            (
                s
                for s in (bridge.get("setups") or [])
                if s.get("workspace_id") == wid
            ),
            None,
        )
        if match is None:
            return
        if match.get("id") == bridge.get("active_setup_id"):
            return
        from press_store import activate_setup

        with self._cfg_lock:
            ok = activate_setup(self._cfg, match["id"])
        if not ok:
            return
        self._persist()
        self._refresh_bridge_setup_combo()
        self._refresh_bridge_windows_table()
        self._worker.reset_window_tracking()
        self._bridge_log(
            f"workspace changed → activated setup '{match.get('name', 'Setup')}'"
        )

    def _auto_detect_bridge_windows(self) -> None:
        """Run Win32 EnumWindows to find visible Cursor windows, show a
        small picker dialog, and either replace the current windows
        list or append to it based on the user's choice. The dialog
        is the safety net — never overwrites without a confirm-style
        button click."""
        from press_windows import list_cursor_windows

        try:
            detected = list_cursor_windows()
        except Exception as exc:
            self._bridge_log(f"auto-detect failed: {exc}")
            return
        if not detected:
            self._bridge_log(
                "auto-detect: no visible Cursor windows found "
                "(minimised / hidden / no 'Cursor' in title)"
            )
            return

        mode = self._auto_detect_dialog(detected)
        if mode is None:
            self._bridge_log("auto-detect: cancelled")
            return

        picked = mode["picked"]
        if not picked:
            self._bridge_log("auto-detect: nothing checked, nothing changed")
            return
        self._apply_auto_detected(picked, replace=mode["replace"])

    def _auto_detect_dialog(self, detected: list[dict]) -> Optional[dict]:
        """Modal dialog listing detected windows with checkboxes and
        Add / Replace / Cancel buttons. Returns
        ``{picked: list[dict], replace: bool}`` on accept, None on
        cancel."""
        dlg = QDialog(self)
        dlg.setWindowTitle("Auto-detect Cursor windows")
        dlg.setMinimumWidth(420)
        layout = QVBoxLayout(dlg)

        intro = BodyLabel(
            f"Found {len(detected)} visible Cursor window"
            f"{'s' if len(detected) != 1 else ''} on this desktop."
        )
        layout.addWidget(intro)

        listw = QListWidget()
        listw.setSelectionMode(QListWidget.NoSelection)
        for d in detected:
            item = QListWidgetItem(
                f"{d['name']}   —   "
                f"{d['region'][2]}×{d['region'][3]} px at "
                f"({d['region'][0]}, {d['region'][1]})"
            )
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Checked)
            item.setData(Qt.UserRole, d)
            listw.addItem(item)
        layout.addWidget(listw)

        hint = CaptionLabel(
            "Add appends checked windows to the current setup.\n"
            "Replace wipes the current setup's windows first, then adds checked.\n"
            "To keep the existing layout, click 'New setup' before running this."
        )
        hint.setWordWrap(True)
        layout.addWidget(hint)

        btn_row = QHBoxLayout()
        cancel_btn = PushButton("Cancel")
        add_btn = PrimaryPushButton(FIF.ADD, "Add")
        replace_btn = PushButton("Replace all")
        btn_row.addWidget(cancel_btn)
        btn_row.addStretch(1)
        btn_row.addWidget(replace_btn)
        btn_row.addWidget(add_btn)
        layout.addLayout(btn_row)

        result = {"replace": False, "accepted": False}
        cancel_btn.clicked.connect(dlg.reject)
        add_btn.clicked.connect(
            lambda: (result.update(accepted=True, replace=False), dlg.accept())
        )
        replace_btn.clicked.connect(
            lambda: (result.update(accepted=True, replace=True), dlg.accept())
        )

        if dlg.exec() != QDialog.Accepted:
            return None
        if not result["accepted"]:
            return None
        picked: list[dict] = []
        for i in range(listw.count()):
            item = listw.item(i)
            if item.checkState() == Qt.Checked:
                picked.append(item.data(Qt.UserRole))
        return {"picked": picked, "replace": result["replace"]}

    def _apply_auto_detected(self, detected: list[dict], replace: bool) -> None:
        """Persist the detected windows into the active setup. If
        ``replace`` is True the current setup's windows are wiped
        first. The previous setup is preserved as a setup the user
        can switch back to via the dropdown — no implicit backup
        layer."""
        with self._cfg_lock:
            if replace:
                self._cfg["bridge"]["windows"] = []
            existing = self._cfg["bridge"]["windows"]
            for d in detected:
                win = default_bridge_window(d.get("name", "Cursor"))
                win["region"] = [int(v) for v in d["region"]]
                existing.append(win)
        self._persist()
        self._refresh_bridge_windows_table()
        self._refresh_bridge_setup_combo()
        self._worker.reset_window_tracking()
        verb = "replaced with" if replace else "added"
        msg = f"auto-detect: {verb} {len(detected)} window"
        if len(detected) != 1:
            msg += "s"
        self._bridge_log(msg)

    def _find_window_index(self, window_id: str) -> Optional[int]:
        for idx, w in enumerate(self._cfg.get("bridge", {}).get("windows", [])):
            if w.get("id") == window_id:
                return idx
        return None

    def _recapture_bridge_window_region(self, window_id: str) -> None:
        idx = self._find_window_index(window_id)
        if idx is None:
            return
        name = self._cfg["bridge"]["windows"][idx].get("name", "Cursor")
        self._bridge_log(f"re-capture region for '{name}': drag the new box…")
        bbox = capture_drag_bbox(self)
        if not bbox:
            self._bridge_log("re-capture cancelled")
            return
        with self._cfg_lock:
            self._cfg["bridge"]["windows"][idx]["region"] = [int(b) for b in bbox]
        self._persist()
        self._refresh_bridge_windows_table()
        self._bridge_log(
            f"updated '{name}' region → ({bbox[0]},{bbox[1]}) {bbox[2]}×{bbox[3]}"
        )

    def _delete_bridge_window(self, window_id: str) -> None:
        idx = self._find_window_index(window_id)
        if idx is None:
            return
        name = self._cfg["bridge"]["windows"][idx].get("name", "Cursor")
        with self._cfg_lock:
            del self._cfg["bridge"]["windows"][idx]
        self._persist()
        self._refresh_bridge_windows_table()
        self._bridge_log(f"removed '{name}'")

    def _move_bridge_window(self, window_id: str, direction: int) -> None:
        """Swap a window with its neighbour. ``direction`` is -1 for up,
        +1 for down. Persisting the new order propagates to the phone
        UI on the next /api/state read so the cards rearrange to match
        the desktop list (which the user lays out left-to-right per
        monitor)."""
        idx = self._find_window_index(window_id)
        if idx is None:
            return
        with self._cfg_lock:
            windows = self._cfg.get("bridge", {}).get("windows", [])
            new_idx = idx + direction
            if not (0 <= new_idx < len(windows)):
                return
            windows[idx], windows[new_idx] = windows[new_idx], windows[idx]
            name = windows[new_idx].get("name", "Cursor")
        self._persist()
        self._refresh_bridge_windows_table()
        self._bridge_log(f"moved '{name}' {'up' if direction < 0 else 'down'}")

    def _refresh_bridge_windows_table(self) -> None:
        table = getattr(self, "_bridge_windows_table", None)
        if table is None:
            return
        windows = self._cfg.get("bridge", {}).get("windows", [])
        table.setRowCount(0)
        table.setRowCount(len(windows))
        for row, w in enumerate(windows):
            wid = w.get("id", "")

            # Name is a LineEdit cell widget rather than an editable
            # QTableWidgetItem — explicit input field, no double-click
            # ambiguity, no edit-mode overlap with adjacent cells.
            name_edit = LineEdit()
            name_edit.setText(w.get("name", "Cursor"))
            name_edit.setClearButtonEnabled(False)
            name_edit.editingFinished.connect(
                lambda _id=wid, _edit=name_edit: self._rename_bridge_window(
                    _id, _edit.text()
                )
            )
            table.setCellWidget(row, 0, name_edit)

            region = w.get("region")
            region_text = (
                f"{region[0]},{region[1]} {region[2]}×{region[3]}" if region else "—"
            )
            region_item = QTableWidgetItem(region_text)
            region_item.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            table.setItem(row, 1, region_item)

            cell = QWidget()
            cl = QHBoxLayout(cell)
            cl.setContentsMargins(2, 2, 2, 2)
            cl.setSpacing(4)

            # Up / down move the window in the list. Edges disable the
            # button that would walk off the list — saves a no-op log
            # entry plus the visual cue tells the user "you're already
            # at the top". The phone reads the list in this order, so
            # left-to-right monitor layout maps to top-to-bottom rows.
            up_btn = ToolButton(FIF.UP)
            up_btn.setToolTip("Move up")
            up_btn.setEnabled(row > 0)
            up_btn.clicked.connect(
                lambda _checked=False, _id=wid: self._move_bridge_window(_id, -1)
            )
            down_btn = ToolButton(FIF.DOWN)
            down_btn.setToolTip("Move down")
            down_btn.setEnabled(row < len(windows) - 1)
            down_btn.clicked.connect(
                lambda _checked=False, _id=wid: self._move_bridge_window(_id, 1)
            )

            region_btn = ToolButton(FIF.CAMERA)
            region_btn.setToolTip("Re-capture window region")
            region_btn.clicked.connect(
                lambda _checked=False, _id=wid: self._recapture_bridge_window_region(_id)
            )
            del_btn = ToolButton(FIF.DELETE)
            del_btn.setToolTip("Remove this window")
            del_btn.clicked.connect(
                lambda _checked=False, _id=wid: self._delete_bridge_window(_id)
            )
            cl.addWidget(up_btn)
            cl.addWidget(down_btn)
            cl.addWidget(region_btn)
            cl.addWidget(del_btn)
            table.setCellWidget(row, 2, cell)

    def _rename_bridge_window(self, window_id: str, new_text: str) -> None:
        new_name = new_text.strip() or "Cursor"
        with self._cfg_lock:
            for w in self._cfg.get("bridge", {}).get("windows", []):
                if w.get("id") == window_id:
                    if w.get("name") == new_name:
                        return
                    w["name"] = new_name
                    break
            else:
                return
        self._persist()
        self._bridge_log(f"renamed window → '{new_name}'")

    def _build_tray(self) -> None:
        self._tray = QSystemTrayIcon(self._icon_stopped, self)
        self._tray.setToolTip("Auto Press — Stopped")
        menu = QMenu()
        self._tray_show_action = QAction("Hide window", self)
        self._tray_toggle_action = QAction("Start", self)
        self._tray_quit_action = QAction("Quit Auto Press", self)
        self._tray_show_action.triggered.connect(self._toggle_window_visibility)
        self._tray_toggle_action.triggered.connect(self._toggle_running)
        self._tray_quit_action.triggered.connect(self._quit_app)
        menu.addAction(self._tray_show_action)
        menu.addAction(self._tray_toggle_action)
        menu.addSeparator()
        menu.addAction(self._tray_quit_action)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._on_tray_activated)
        self._tray.show()

    # ---------- body collapse ----------

    def _on_left_card_toggled(self, _expanded: bool) -> None:
        """Resize the left splitter when Rules or Log is collapsed/expanded.

        Remembers the last expanded heights for each panel so a collapse →
        expand round-trip restores the previous layout.
        """
        sender = self.sender()
        HEADER_H = 44
        sizes = self._left_splitter.sizes()
        # Snapshot whichever side is still expanded *before* we apply the
        # new min/max constraints, so re-expand later can restore it.
        if sender is self._rules_card and self._rules_card.isExpanded() is False and sizes[0] > HEADER_H:
            self._left_remembered["rules"] = sizes[0]
        elif sender is self._log_card and self._log_card.isExpanded() is False and sizes[1] > HEADER_H:
            self._left_remembered["log"] = sizes[1]

        for card, key, min_open in (
            (self._rules_card, "rules", 140),
            (self._log_card, "log", 110),
        ):
            if card.isExpanded():
                card.setMinimumHeight(min_open)
                card.setMaximumHeight(16777215)
            else:
                card.setMinimumHeight(0)
                card.setMaximumHeight(HEADER_H)

        total = sum(sizes) or (self._left_splitter.height() or 580)
        rules_exp = self._rules_card.isExpanded()
        log_exp = self._log_card.isExpanded()
        if rules_exp and log_exp:
            r = self._left_remembered.get("rules", 380)
            self._left_splitter.setSizes([r, max(HEADER_H, total - r)])
        elif rules_exp:
            self._left_splitter.setSizes([total - HEADER_H, HEADER_H])
        elif log_exp:
            self._left_splitter.setSizes([HEADER_H, total - HEADER_H])
        else:
            self._left_splitter.setSizes([HEADER_H, HEADER_H])

    def _toggle_body_collapsed(self) -> None:
        """Collapse the whole body (tabs + their contents) into toolbar-only mode."""
        if self._body_container.isVisible():
            self._remembered_body_h = self._body_container.height()
            self._body_container.setVisible(False)
            # Drop the min height so the window can shrink tight to the toolbar;
            # content margins (14 top + 14 bottom) + toolbar card (62) + chrome
            # fit into roughly 110 px.
            self._default_min_height = self.minimumHeight()
            self.setMinimumHeight(110)
            self.resize(self.width(), 110)
            self._collapse_btn.setIcon(FIF.DOWN)
            self._collapse_btn.setToolTip("Expand window")
        else:
            self.setMinimumHeight(getattr(self, "_default_min_height", 240))
            self._body_container.setVisible(True)
            self.resize(self.width(), 110 + self._remembered_body_h)
            self._collapse_btn.setIcon(FIF.UP)
            self._collapse_btn.setToolTip("Collapse to toolbar only")

    # ---------- cfg helpers ----------

    def _snapshot_cfg(self) -> dict:
        with self._cfg_lock:
            return {
                "interval_seconds": float(self._cfg.get("interval_seconds", 10.0)),
                "rules": [dict(rule) for rule in self._cfg.get("rules", [])],
                "bridge": dict(self._cfg.get("bridge", {})),
                # Worker reads this to decide whether to run idle detection.
                # Same boolean drives the FastAPI service, so detection and
                # service start/stop in lockstep.
                "bridge_active": self._bridge is not None,
            }

    def _persist(self) -> None:
        with self._cfg_lock:
            save_config(self._cfg)

    def _current_rule_index(self) -> Optional[int]:
        idx = self._rules_list.currentRow()
        return idx if idx is not None and idx >= 0 else None

    def _current_rule(self) -> Optional[dict]:
        idx = self._current_rule_index()
        if idx is None:
            return None
        rules = self._cfg.get("rules", [])
        return rules[idx] if 0 <= idx < len(rules) else None

    # ---------- log ----------

    def _log(self, message: str) -> None:
        self._log_box.appendPlainText(f"[{time.strftime('%H:%M:%S')}] {message}")

    # ---------- rule list / editor ----------

    def _refresh_rule_list(self, select_idx: Optional[int] = None) -> None:
        current = select_idx if select_idx is not None else self._current_rule_index()
        self._rules_list.blockSignals(True)
        rules = self._cfg.get("rules", [])
        self._rules_list.setRowCount(len(rules))
        for row, rule in enumerate(rules):
            name_item = QTableWidgetItem(rule.get("name", "(unnamed)"))
            name_item.setFlags(name_item.flags() & ~Qt.ItemIsEditable)
            self._rules_list.setItem(row, 0, name_item)

            # Enabled column hosts a real checkbox so users can toggle rules
            # on/off straight from the table without opening the editor.
            # Stretches on both sides centre the box regardless of column
            # width — setAlignment alone leaves it left-hugging in some Qt
            # styles.
            checkbox = CheckBox()
            checkbox.setChecked(bool(rule.get("enabled")))
            container = QWidget()
            cell_lay = QHBoxLayout(container)
            cell_lay.setContentsMargins(0, 0, 0, 0)
            cell_lay.addStretch(1)
            cell_lay.addWidget(checkbox)
            cell_lay.addStretch(1)
            self._rules_list.setCellWidget(row, 1, container)
            rule_id = rule["id"]
            checkbox.stateChanged.connect(
                lambda state, rid=rule_id: self._on_rule_enabled_toggled(rid, state)
            )

            action_item = QTableWidgetItem(rule.get("action", ACTION_CLICK))
            action_item.setForeground(QColor("#a1a1aa"))
            self._rules_list.setItem(row, 2, action_item)
        self._rules_list.blockSignals(False)

        if current is not None and self._rules_list.rowCount() > 0:
            bounded = max(0, min(self._rules_list.rowCount() - 1, current))
            self._rules_list.selectRow(bounded)
        else:
            self._clear_editor()

    def _on_rule_enabled_toggled(self, rule_id: str, state: int) -> None:
        from PySide6.QtCore import Qt as _Qt

        enabled = state == _Qt.Checked
        with self._cfg_lock:
            for r in self._cfg.get("rules", []):
                if r.get("id") == rule_id:
                    r["enabled"] = enabled
                    break
        self._persist()

    def _load_selected_rule(self, _row: int = -1) -> None:
        rule = self._current_rule()
        if rule is None:
            self._clear_editor(); return
        # Suppress autosave while we programmatically populate the editor
        # — otherwise switching rules would write the previous rule's
        # values into the newly-selected rule.
        self._suppress_autosave = True
        try:
            self._name_edit.setText(rule.get("name", ""))
            self._threshold_spin.setValue(float(rule.get("threshold", 0.90)))
            self._action_combo.setCurrentText(rule.get("action", ACTION_CLICK))
            self._text_edit.setText(rule.get("text", "continue"))
            # Block the combo's signal so the programmatic update doesn't fire
            # _on_template_selected and overwrite a colour rule's matcher.
            self._template_combo.blockSignals(True)
            self._template_combo.setCurrentText(rule.get("template_path") or "")
            self._template_combo.blockSignals(False)
            region = rule.get("search_region")
            self._region_label.setText(
                f"{region[2]} × {region[3]} @ ({region[0]}, {region[1]})" if region else "All monitors"
            )
            self._update_action_fields()
            self._update_match_preview()
        finally:
            self._suppress_autosave = False

    def _clear_editor(self) -> None:
        self._suppress_autosave = True
        try:
            self._name_edit.clear()
            self._threshold_spin.setValue(0.90)
            self._action_combo.setCurrentText(ACTION_CLICK)
            self._text_edit.setText("continue")
            self._template_combo.setCurrentText("")
        finally:
            self._suppress_autosave = False
        self._region_label.setText("All monitors")
        self._update_action_fields()
        self._update_match_preview()

    def _update_action_fields(self, *_args) -> None:
        wants_text = self._action_combo.currentText() == ACTION_CLICK_TYPE_ENTER
        self._text_label.setVisible(wants_text)
        self._text_edit.setVisible(wants_text)

    def _add_rule(self) -> None:
        with self._cfg_lock:
            rule = default_rule(name=f"Rule {len(self._cfg['rules']) + 1}")
            rule["priority"] = len(self._cfg["rules"]) + 1
            self._cfg["rules"].append(rule)
            idx = len(self._cfg["rules"]) - 1
        self._persist(); self._refresh_rule_list(idx)
        self._log(f"[rule] added {rule['name']}")

    def _delete_rule(self) -> None:
        idx = self._current_rule_index()
        if idx is None:
            self._log("[rule] select a rule to delete"); return
        with self._cfg_lock:
            removed = self._cfg["rules"].pop(idx)
            for pos, item in enumerate(self._cfg["rules"], start=1):
                item["priority"] = pos
            self._last_scores.pop(removed["id"], None)
        self._persist(); self._refresh_rule_list(max(0, idx - 1))
        self._log(f"[rule] deleted {removed['name']}")

    def _move_rule(self, direction: int) -> None:
        idx = self._current_rule_index()
        if idx is None:
            return
        new_idx = idx + direction
        with self._cfg_lock:
            if not (0 <= new_idx < len(self._cfg["rules"])):
                return
            self._cfg["rules"][idx], self._cfg["rules"][new_idx] = (
                self._cfg["rules"][new_idx],
                self._cfg["rules"][idx],
            )
            for pos, item in enumerate(self._cfg["rules"], start=1):
                item["priority"] = pos
        self._persist(); self._refresh_rule_list(new_idx)

    def _autosave_rule(self, *_args) -> None:
        """Quietly persist the editor inputs to the active rule. Connected
        to every editor field's change signal so the user never has to
        hit Save. Suppressed during _load_selected_rule / _clear_editor
        so programmatic input updates don't echo back into cfg."""
        if getattr(self, "_suppress_autosave", False):
            return
        idx = self._current_rule_index()
        if idx is None:
            return
        with self._cfg_lock:
            rule = self._cfg["rules"][idx]
            rule["name"] = self._name_edit.text().strip() or f"Rule {idx + 1}"
            rule["threshold"] = max(0.0, min(1.0, float(self._threshold_spin.value())))
            rule["action"] = (
                self._action_combo.currentText()
                if self._action_combo.currentText() in ACTION_TYPES
                else ACTION_CLICK
            )
            rule["text"] = self._text_edit.text().strip() or "continue"
        self._persist()

    def _save_selected_rule(self) -> bool:
        idx = self._current_rule_index()
        if idx is None:
            self._log("[rule] select a rule first"); return False
        with self._cfg_lock:
            rule = self._cfg["rules"][idx]
            rule["name"] = self._name_edit.text().strip() or f"Rule {idx + 1}"
            # `enabled` is owned by the rules-table checkbox column; preserve
            # whatever the user last set there.
            rule["threshold"] = max(0.0, min(1.0, float(self._threshold_spin.value())))
            rule["action"] = (
                self._action_combo.currentText()
                if self._action_combo.currentText() in ACTION_TYPES
                else ACTION_CLICK
            )
            rule["text"] = self._text_edit.text().strip() or "continue"
            for pos, item in enumerate(self._cfg["rules"], start=1):
                item["priority"] = pos
        self._persist(); self._refresh_rule_list(idx)
        self._log(f"[rule] saved {self._name_edit.text().strip() or f'Rule {idx + 1}'}")
        return True

    # ---------- templates ----------

    def _refresh_template_choices(self, selected: Optional[str] = None) -> None:
        current = selected if selected is not None else self._template_combo.currentText()
        items = [""] + list_template_files()
        self._template_combo.blockSignals(True)
        self._template_combo.clear()
        self._template_combo.addItems(items)
        self._template_combo.setCurrentText(current if current in items else "")
        self._template_combo.blockSignals(False)
        self._update_match_preview()

    def _set_matcher(self, matcher: str) -> None:
        """User clicked the segmented toggle. Persist on the active rule, refresh UI."""
        if getattr(self, "_suppress_matcher_signal", False):
            return
        idx = self._current_rule_index()
        if idx is None:
            self._update_match_preview()
            return
        with self._cfg_lock:
            current = self._cfg["rules"][idx].get("matcher", MATCHER_TEMPLATE)
        if current == matcher:
            self._update_match_preview()
            return
        with self._cfg_lock:
            self._cfg["rules"][idx]["matcher"] = matcher
        self._persist()
        self._refresh_rule_list(idx)
        self._update_match_preview()

    def _on_color_name_edited(self) -> None:
        idx = self._current_rule_index()
        if idx is None:
            return
        new_name = self._color_name_edit.text().strip()
        with self._cfg_lock:
            rule = self._cfg["rules"][idx]
            if rule.get("color_name", "") == new_name:
                return
            rule["color_name"] = new_name
        self._persist()
        self._refresh_color_library(remember_current=True)
        self._update_match_preview()

    def _refresh_color_library(self, remember_current: bool = False) -> None:
        """Populate the color library dropdown with every captured color across rules.

        The combo's selection is anchored to the active rule's own color, so
        after a refresh the user can still see which entry corresponds to the
        rule they're editing — picking a different entry won't visually snap
        back to "— choose a color —".
        """
        items: list[tuple[str, list[int]]] = []  # (label, rgb)
        seen: set[tuple[int, int, int]] = set()
        for r in self._cfg.get("rules", []):
            rgb = r.get("color_rgb")
            if not rgb or len(rgb) != 3:
                continue
            key = tuple(int(c) for c in rgb)
            if key in seen:
                continue
            seen.add(key)
            hex_label = f"#{key[0]:02X}{key[1]:02X}{key[2]:02X}"
            name = r.get("color_name") or ""
            label = f"{name}  ·  {hex_label}" if name else hex_label
            items.append((label, list(key)))

        active_rgb: Optional[tuple[int, int, int]] = None
        rule = self._current_rule()
        if rule and rule.get("color_rgb"):
            active_rgb = tuple(int(c) for c in rule["color_rgb"])

        # qfluentwidgets ComboBox.addItem signature is (text, icon=None,
        # userData=None) — the RGB list is user data, not an icon.
        self._color_library_combo.blockSignals(True)
        self._color_library_combo.clear()
        self._color_library_combo.addItem("— choose a color —", userData=None)
        selected_idx = 0
        for label, rgb in items:
            self._color_library_combo.addItem(label, userData=rgb)
            if active_rgb is not None and tuple(rgb) == active_rgb:
                selected_idx = self._color_library_combo.count() - 1
        self._color_library_combo.setCurrentIndex(selected_idx)
        self._color_library_combo.blockSignals(False)

    def _on_color_library_selected(self, idx: int) -> None:
        if idx <= 0:
            return  # placeholder
        rgb = self._color_library_combo.itemData(idx)
        if not rgb:
            return
        rule_idx = self._current_rule_index()
        if rule_idx is None:
            return
        # Pull name + captured area from the first rule that owns this colour.
        name = ""
        area = 0
        target = tuple(int(c) for c in rgb)
        for r in self._cfg.get("rules", []):
            r_rgb = r.get("color_rgb")
            if r_rgb and tuple(int(c) for c in r_rgb) == target:
                name = r.get("color_name") or ""
                area = int(r.get("color_capture_area") or 0)
                break
        with self._cfg_lock:
            rule = self._cfg["rules"][rule_idx]
            rule["matcher"] = MATCHER_COLOR
            rule["color_rgb"] = list(rgb)
            rule["color_name"] = name
            rule["color_capture_area"] = area
        self._persist()
        self._refresh_rule_list(rule_idx)
        self._update_match_preview()

    def _update_match_preview(self) -> None:
        """Render the preview area for whichever matcher the active rule uses."""
        rule = self._current_rule()
        matcher = (rule or {}).get("matcher", MATCHER_TEMPLATE)
        # Keep the segmented toggle in sync without re-firing onClick.
        self._suppress_matcher_signal = True
        try:
            self._matcher_seg.setCurrentItem(matcher)
        finally:
            self._suppress_matcher_signal = False

        is_color = matcher == MATCHER_COLOR
        # Source-row widgets: template combo + rename/delete vs colour library.
        self._template_combo.setVisible(not is_color)
        self._rename_template_btn.setVisible(not is_color)
        self._delete_template_btn.setVisible(not is_color)
        self._color_library_combo.setVisible(is_color)
        self._rename_color_btn.setVisible(is_color)
        self._delete_color_btn.setVisible(is_color)
        # Capture buttons follow the side too — no point offering "capture color"
        # while the user is on the Pattern tab and vice versa.
        self._capture_pattern_btn.setVisible(not is_color)
        self._capture_color_btn.setVisible(is_color)
        # Threshold only applies to template matching.
        self._threshold_label.setVisible(not is_color)
        self._threshold_spin.setVisible(not is_color)
        # Color name only applies to colour matching.
        self._color_name_label.setVisible(is_color)
        self._color_name_edit.setVisible(is_color)

        if is_color:
            self._refresh_color_library(remember_current=False)
            if rule and rule.get("color_rgb"):
                r, g, b = (int(c) for c in rule["color_rgb"])
                area = int(rule.get("color_capture_area") or 0)
                self._color_swatch.setStyleSheet(
                    f"QLabel {{ background: rgb({r},{g},{b}); "
                    f"border: 1px solid rgba(255,255,255,0.18); border-radius: 6px; }}"
                )
                self._template_meta.setText(f"#{r:02X}{g:02X}{b:02X}  ·  {area} px² captured")
                self._color_name_edit.blockSignals(True)
                self._color_name_edit.setText(rule.get("color_name") or "")
                self._color_name_edit.blockSignals(False)
            else:
                self._color_swatch.setStyleSheet(
                    "QLabel { background: rgba(0,0,0,0.25); "
                    "border: 1px dashed rgba(255,255,255,0.18); border-radius: 6px; }"
                )
                self._template_meta.setText("Click 'Capture color' to pick a color")
                self._color_name_edit.blockSignals(True)
                self._color_name_edit.clear()
                self._color_name_edit.blockSignals(False)
            self._preview_label.setVisible(False)
            self._color_swatch.setVisible(True)
            return

        # Template matcher path: show image preview + threshold.
        self._preview_label.setVisible(True)
        self._color_swatch.setVisible(False)
        name = (self._template_combo.currentText() or "").strip()
        if not name:
            self._preview_label.setPixmap(QPixmap())
            self._preview_label.setText("(no template)")
            self._template_meta.setText(""); return
        path = resolve_template_path(name)
        if path is None or not Path(path).exists():
            self._preview_label.setPixmap(QPixmap())
            self._preview_label.setText("(missing)")
            self._template_meta.setText("not found under templates/"); return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            self._preview_label.setPixmap(QPixmap())
            self._preview_label.setText("(error)")
            self._template_meta.setText(""); return
        nw, nh = pixmap.width(), pixmap.height()
        bw, bh = self._preview_label.width() - 8, self._preview_label.height() - 8
        if nw > bw or nh > bh:
            scaled = pixmap.scaled(bw, bh, Qt.KeepAspectRatio, Qt.SmoothTransformation)
            note = "fit"
        else:
            scaled = pixmap
            note = "actual"
        self._preview_label.setPixmap(scaled)
        self._preview_label.setText("")
        self._template_meta.setText(f"{nw} × {nh} px  ·  {note}")

    def _on_template_selected(self, name: str) -> None:
        """Dropdown-driven template assignment.

        Selecting a template flips the active rule's matcher back to template
        mode and persists the new path. No-op if no rule is active so
        programmatic reloads don't double-save.
        """
        idx = self._current_rule_index()
        if idx is None:
            self._update_match_preview()
            return
        choice = (name or "").strip()
        with self._cfg_lock:
            rule = self._cfg["rules"][idx]
            current = rule.get("template_path") or ""
            current_matcher = rule.get("matcher", MATCHER_TEMPLATE)
        if choice == current and current_matcher == MATCHER_TEMPLATE:
            self._update_match_preview()
            return
        with self._cfg_lock:
            rule = self._cfg["rules"][idx]
            rule["template_path"] = choice or None
            rule["matcher"] = MATCHER_TEMPLATE
        self._persist()
        self._refresh_rule_list(idx)
        self._update_match_preview()
        if choice:
            self._log(f"[template] {choice}")

    def _rename_selected_template(self) -> None:
        choice = (self._template_combo.currentText() or "").strip()
        if not choice:
            self._log("[template] no template selected to rename"); return
        from PySide6.QtWidgets import QInputDialog

        suffix = Path(choice).suffix or ".png"
        stem_default = Path(choice).stem
        new_stem, ok = QInputDialog.getText(
            self, "Rename template", "New filename (without extension):", text=stem_default
        )
        if not ok:
            return
        new_stem = (new_stem or "").strip()
        if not new_stem or any(ch in new_stem for ch in r"\/:*?\"<>|"):
            self._log("[template] rename aborted: empty or contains a path separator"); return
        new_name = f"{new_stem}{suffix}"
        if new_name == choice:
            return
        old_path = template_asset_path(choice)
        new_path = template_asset_path(new_name)
        if new_path.exists():
            self._log(f"[template] '{new_name}' already exists; pick another name"); return
        try:
            old_path.rename(new_path)
        except OSError as exc:
            self._log(f"[error] rename failed: {exc}"); return
        # Re-point every rule that referenced the old filename.
        with self._cfg_lock:
            for r in self._cfg.get("rules", []):
                if r.get("template_path") == choice:
                    r["template_path"] = new_name
        self._persist()
        self._refresh_template_choices(new_name)
        self._refresh_rule_list(self._current_rule_index())
        self._log(f"[template] renamed {choice} -> {new_name}")

    def _delete_selected_template(self) -> None:
        choice = (self._template_combo.currentText() or "").strip()
        if not choice:
            self._log("[template] no template selected to delete"); return
        from PySide6.QtWidgets import QMessageBox

        confirm = QMessageBox.question(
            self,
            "Delete template",
            f"Delete '{choice}' from disk?\nAny rule pointing to it will lose its template.",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        path = template_asset_path(choice)
        try:
            if path.exists():
                path.unlink()
        except OSError as exc:
            self._log(f"[error] delete failed: {exc}"); return
        # Detach the deleted file from every rule that referenced it.
        with self._cfg_lock:
            for r in self._cfg.get("rules", []):
                if r.get("template_path") == choice:
                    r["template_path"] = None
        self._persist()
        self._refresh_template_choices()
        self._refresh_rule_list(self._current_rule_index())
        self._log(f"[template] deleted {choice}")

    def _selected_library_color(self) -> Optional[tuple[int, int, int]]:
        """Return the RGB tuple currently picked in the color library combo."""
        idx = self._color_library_combo.currentIndex()
        if idx <= 0:
            return None
        rgb = self._color_library_combo.itemData(idx)
        if not rgb:
            return None
        return tuple(int(c) for c in rgb)

    def _rename_selected_color(self) -> None:
        """Rename a colour library entry. Updates color_name on every rule that
        shares the chosen RGB, so the friendly label stays consistent."""
        target = self._selected_library_color()
        if target is None:
            self._log("[color] pick a color from the library first"); return
        current_name = ""
        for r in self._cfg.get("rules", []):
            r_rgb = r.get("color_rgb")
            if r_rgb and tuple(int(c) for c in r_rgb) == target and r.get("color_name"):
                current_name = r.get("color_name") or ""
                break
        from PySide6.QtWidgets import QInputDialog

        new_name, ok = QInputDialog.getText(
            self, "Rename color", "New name:", text=current_name
        )
        if not ok:
            return
        new_name = (new_name or "").strip()
        with self._cfg_lock:
            for r in self._cfg.get("rules", []):
                r_rgb = r.get("color_rgb")
                if r_rgb and tuple(int(c) for c in r_rgb) == target:
                    r["color_name"] = new_name
        self._persist()
        self._refresh_rule_list(self._current_rule_index())
        self._update_match_preview()
        hex_label = f"#{target[0]:02X}{target[1]:02X}{target[2]:02X}"
        self._log(f"[color] {hex_label} renamed to '{new_name or '(unnamed)'}'")

    def _delete_selected_color(self) -> None:
        """Clear the chosen colour from every rule that uses it. Rules stay in
        colour matcher mode but lose their RGB data, ready for a re-capture."""
        target = self._selected_library_color()
        if target is None:
            self._log("[color] pick a color from the library first"); return
        from PySide6.QtWidgets import QMessageBox

        hex_label = f"#{target[0]:02X}{target[1]:02X}{target[2]:02X}"
        confirm = QMessageBox.question(
            self,
            "Delete color",
            f"Clear {hex_label} from every rule that uses it?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if confirm != QMessageBox.Yes:
            return
        cleared = 0
        with self._cfg_lock:
            for r in self._cfg.get("rules", []):
                r_rgb = r.get("color_rgb")
                if r_rgb and tuple(int(c) for c in r_rgb) == target:
                    r["color_rgb"] = None
                    r["color_name"] = ""
                    r["color_capture_area"] = 0
                    cleared += 1
        self._persist()
        self._refresh_rule_list(self._current_rule_index())
        self._update_match_preview()
        self._log(f"[color] {hex_label} cleared from {cleared} rule(s)")

    def _capture_template(self) -> None:
        import press_dpi as dpimod
        from press_store import write_template_with_dpi_variants

        idx = self._current_rule_index()
        if idx is None:
            self._log("[capture] add or select a rule first"); return
        try:
            ensure_vision()
        except Exception as exc:
            self._log(f"[error] {exc}"); return
        bbox = capture_drag_bbox(self)
        if not bbox:
            self._log("[capture] template capture cancelled"); return
        try:
            rgb = capture_screen_rgb(tuple(bbox))
            source_scale = dpimod.scale_for_region(bbox)
            file_name = f"rule_{self._cfg['rules'][idx]['id']}.png"
            path = template_asset_path(file_name)
            written = write_template_with_dpi_variants(path, rgb, source_scale)
            stored_path = serialize_template_path(path)
            with self._cfg_lock:
                rule = self._cfg["rules"][idx]
                rule["template_path"] = stored_path
                rule["template_source_dpi"] = source_scale
                rule["matcher"] = MATCHER_TEMPLATE
            self._persist(); self._refresh_rule_list(idx); self._refresh_template_choices(stored_path)
            self._log(
                f"[capture] {path.name}  bbox=({bbox[0]},{bbox[1]}) "
                f"size={bbox[2]}x{bbox[3]} @ {int(round(source_scale * 100))}% DPI, "
                f"{len(written) - 1} scaled variants"
            )
        except Exception as exc:
            self._log(f"[error] template capture failed: {exc}")

    def _capture_color(self) -> None:
        idx = self._current_rule_index()
        if idx is None:
            self._log("[capture] add or select a rule first"); return
        try:
            ensure_vision()
        except Exception as exc:
            self._log(f"[error] {exc}"); return
        bbox = capture_drag_bbox(self)
        if not bbox:
            self._log("[capture] color capture cancelled"); return
        try:
            rgb = capture_screen_rgb(tuple(bbox))
            r, g, b = dominant_rgb(rgb)
            area = int(bbox[2]) * int(bbox[3])
            with self._cfg_lock:
                rule = self._cfg["rules"][idx]
                rule["matcher"] = MATCHER_COLOR
                rule["color_rgb"] = [r, g, b]
                rule["color_capture_area"] = area
            self._persist(); self._refresh_rule_list(idx); self._update_match_preview()
            self._log(
                f"[capture] color #{r:02X}{g:02X}{b:02X} from {bbox[2]}x{bbox[3]} ({area} px²)"
            )
        except Exception as exc:
            self._log(f"[error] color capture failed: {exc}")

    def _capture_search_region(self) -> None:
        idx = self._current_rule_index()
        if idx is None:
            self._log("[capture] select a rule first"); return
        bbox = capture_drag_bbox(self)
        if not bbox:
            self._log("[capture] search region cancelled"); return
        with self._cfg_lock:
            self._cfg["rules"][idx]["search_region"] = bbox
        self._persist(); self._refresh_rule_list(idx)
        self._region_label.setText(f"{bbox[2]} × {bbox[3]} @ ({bbox[0]}, {bbox[1]})")
        self._log(f"[capture] region set: bbox=({bbox[0]},{bbox[1]}) size={bbox[2]}x{bbox[3]}")

    def _use_all_monitors(self) -> None:
        idx = self._current_rule_index()
        if idx is None:
            self._log("[capture] select a rule first"); return
        with self._cfg_lock:
            self._cfg["rules"][idx]["search_region"] = None
        self._persist(); self._refresh_rule_list(idx)
        self._region_label.setText("All monitors")
        self._log("[capture] rule now scans all monitors")

    def _pick_monitor(self) -> None:
        idx = self._current_rule_index()
        if idx is None:
            self._log("[monitor] select a rule first"); return
        dialog = MonitorPickDialog(self)
        if dialog.exec() and dialog.selected:
            bbox = dialog.selected
            with self._cfg_lock:
                self._cfg["rules"][idx]["search_region"] = bbox
            self._persist(); self._refresh_rule_list(idx)
            self._region_label.setText(f"Monitor  {bbox[2]} × {bbox[3]} @ ({bbox[0]}, {bbox[1]})")
            self._log(f"[monitor] region set to {bbox[2]}x{bbox[3]} @ ({bbox[0]},{bbox[1]})")

    def _test_selected_rule(self) -> None:
        idx = self._current_rule_index()
        if idx is None:
            self._log("[test] select a rule first"); return
        if not self._save_selected_rule():
            return
        try:
            with self._cfg_lock:
                # Make the rule enabled for the duration of the test so a
                # disabled rule still reports its score; we restore the
                # config's view through normalize on next persist.
                rule = dict(self._cfg["rules"][idx])
                rule["enabled"] = True
            matcher = rule.get("matcher", MATCHER_TEMPLATE)
            if matcher == MATCHER_TEMPLATE:
                tpl_path = resolve_template_path(rule.get("template_path"))
                if tpl_path is None or not Path(tpl_path).exists():
                    self._log("[test] capture a template first"); return
                frame = capture_screen_gray()
            else:  # MATCHER_COLOR
                if not rule.get("color_rgb") or int(rule.get("color_capture_area") or 0) <= 0:
                    self._log("[test] capture a color first"); return
                frame = capture_screen_rgb()

            runtime_rule = build_runtime_rules({"rules": [rule]})
            if not runtime_rule:
                self._log("[test] rule is not ready"); return
            score, center = evaluate_rule_on_frame(frame, runtime_rule[0])
            if matcher == MATCHER_COLOR:
                matched = center is not None
            else:
                matched = center is not None and score >= float(rule.get("threshold", 0.90))
            self._last_scores[rule["id"]] = score
            self._refresh_rule_list(idx)
            self._log(
                f"[test] {rule['name']}  {'match' if matched else 'no-match'}  "
                f"score={score:.3f}  center={center}"
            )
        except Exception as exc:
            self._log(f"[error] test failed: {exc}")

    # ---------- run control ----------

    def _on_interval_changed(self, value: float) -> None:
        with self._cfg_lock:
            self._cfg["interval_seconds"] = float(value)
        self._worker.set_interval(float(value))
        if self._running:
            self._next_tick_at = time.monotonic() + float(value)
        save_config(self._snapshot_cfg())

    def _toggle_running(self) -> None:
        if self._running:
            self._worker.set_running(False)
            self._next_tick_at = None
            self._log("[control] stopped")
            return
        with self._cfg_lock:
            self._cfg["interval_seconds"] = float(self._interval_spin.value())
            save_config(self._cfg)
        self._worker.set_interval(float(self._interval_spin.value()))
        self._worker.set_running(True)
        self._next_tick_at = time.monotonic() + float(self._interval_spin.value())
        self._log("[control] started")

    def _on_running_changed(self, running: bool) -> None:
        self._running = running
        self._set_running_status(running)
        if not running:
            self._next_tick_at = None
            self._action_status.setText("")

    def _on_needs_rules(self) -> None:
        self._log("[control] add at least one enabled rule with a template")

    def _set_running_status(self, running: bool) -> None:
        if running:
            self._status_label.setText("Running")
            self._status_label.setStyleSheet(f"color: {STATUS_RUNNING};")
            self._status_dot.set_color(STATUS_RUNNING)
            self._start_btn.setText("Stop")
            self._start_btn.setIcon(FIF.PAUSE)
            self._tray.setIcon(self._icon_running)
            self._tray.setToolTip("Auto Press — Running")
            self._tray_toggle_action.setText("Stop")
            self.setWindowIcon(self._icon_running)
        else:
            self._status_label.setText("Stopped")
            self._status_label.setStyleSheet(f"color: {STATUS_STOPPED};")
            self._status_dot.set_color(STATUS_STOPPED)
            self._start_btn.setText("Start")
            self._start_btn.setIcon(FIF.PLAY)
            self._tray.setIcon(self._icon_stopped)
            self._tray.setToolTip("Auto Press — Stopped")
            self._tray_toggle_action.setText("Start")
            self.setWindowIcon(self._icon_stopped)

    def _on_tick_done(self, results: list, actions: list, interval: float) -> None:
        for result in results:
            self._last_scores[result["id"]] = float(result["score"])
        self._refresh_rule_list()
        if actions:
            summaries: dict[str, int] = {}
            for action in actions:
                summaries[action["name"]] = summaries.get(action["name"], 0) + 1
            summary = ", ".join(f"{name} ×{c}" for name, c in summaries.items())
            self._action_status.setText(summary)
            self._log(f"[tick] {summary}")
        elif results:
            # Rules ran but didn't match. Show actual correlations so it's
            # obvious *why* — wrong threshold, dimmed Cursor, occluded
            # button, etc. all look identical without scores.
            scores = ", ".join(
                f"{r.get('name', '?')}={float(r.get('score', 0.0)):.3f}"
                for r in results
            )
            self._action_status.setText("no match")
            self._log(f"[tick] no match (scores: {scores})")
        # If results is empty too, rules either weren't attempted (bridge-
        # only tick) or runtime_rules was filtered to zero — needs_rules
        # already logged the explanation in the latter case, so stay quiet.
        self._next_tick_at = time.monotonic() + interval

    def _on_worker_error(self, message: str) -> None:
        self._log(f"[error] {message}")

    def _update_countdown(self) -> None:
        # Engine countdown lives in the toolbar and only counts when
        # rules are running.
        if self._running and self._next_tick_at is not None:
            remaining = max(0.0, float(self._next_tick_at) - time.monotonic())
            self._countdown_label.setText(f"{remaining:.1f}s")
        else:
            self._countdown_label.setText("")
        # Bridge countdown lives next to the bridge switch status. It
        # updates whenever the bridge is on, regardless of whether the
        # engine is running — bridge ticks fire either way.
        if (
            getattr(self, "_bridge", None) is not None
            and hasattr(self, "_bridge_switch_status")
        ):
            primary = getattr(self, "_bridge_primary_url", "") or ""
            if primary:
                # HTML anchor — the label is already in RichText mode and
                # opens external links via the OS, so a click on this URL
                # launches the user's default browser to the bridge page.
                head = f'Running — <a href="{primary}" style="color: #4f9eff; font-weight: 600;">{primary}</a>'
            else:
                head = "Running"
            if self._next_tick_at is not None:
                remaining = max(0.0, float(self._next_tick_at) - time.monotonic())
                head += f"  ·  next tick in {remaining:.0f}s"
            self._bridge_switch_status.setText(head)

    # ---------- tray / window ----------

    def _on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick):
            self._toggle_window_visibility()

    def _toggle_window_visibility(self) -> None:
        if self.isVisible():
            self.hide()
            self._tray_show_action.setText("Show window")
        else:
            self.show(); self.raise_(); self.activateWindow()
            self._tray_show_action.setText("Hide window")

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self._quitting or not self._tray.isVisible():
            self._shutdown(); event.accept(); return
        event.ignore()
        self.hide()
        self._tray_show_action.setText("Show window")
        self._log("[tray] window minimized to tray (right-click the tray icon to quit)")

    def _quit_app(self) -> None:
        self._quitting = True
        self._shutdown()
        QApplication.instance().quit()

    def _shutdown(self) -> None:
        self._worker.set_running(False)
        self._worker.request_stop()
        self._worker_thread.quit()
        self._worker_thread.wait(2000)
        self._hotkey_stop.set()
        if IS_WINDOWS:
            self._post_wm_quit()
        if self._bridge is not None:
            try:
                self._bridge.stop()
            except Exception:
                pass
        with self._cfg_lock:
            self._cfg["interval_seconds"] = float(self._interval_spin.value())
            save_config(self._cfg)
        self._save_window_state()
        self._tray.hide()

    # ---------- window state persistence (QSettings) ----------

    def _collapsible_cards(self) -> dict:
        """Map of stable key → CollapsibleCard for the cards we persist.
        Built lazily because some attributes don't exist until the bridge
        page or rule editor is built."""
        return {
            "rules_card": getattr(self, "_rules_card", None),
            "log_card": getattr(self, "_log_card", None),
            "basics_card": getattr(self, "_basics_card", None),
            "match_card": getattr(self, "_match_card", None),
            "scope_card": getattr(self, "_scope_card", None),
            "bridge_tpl": getattr(self, "_bridge_tpl_card", None),
            "bridge_win": getattr(self, "_bridge_win_card", None),
            "bridge_log": getattr(self, "_bridge_log_card", None),
        }

    def _save_window_state(self) -> None:
        """Persist window geometry + splitter sizes + each CollapsibleCard's
        expanded state + active tab so the next launch picks up where this
        one left off. Stored in QSettings (HKCU\\Software\\auto-press\\Auto
        Press on Windows)."""
        s = QSettings()
        s.setValue("ui/geometry", self.saveGeometry())
        if hasattr(self, "_body_splitter"):
            s.setValue("ui/body_splitter", self._body_splitter.saveState())
        if hasattr(self, "_left_splitter"):
            s.setValue("ui/left_splitter", self._left_splitter.saveState())
        if hasattr(self, "_bridge_left_splitter"):
            s.setValue("ui/bridge_left_splitter", self._bridge_left_splitter.saveState())
        if hasattr(self, "_bridge_body_splitter"):
            s.setValue("ui/bridge_body_splitter", self._bridge_body_splitter.saveState())
        if hasattr(self, "_tab_stack"):
            s.setValue("ui/tab_index", int(self._tab_stack.currentIndex()))
        for key, card in self._collapsible_cards().items():
            if card is not None:
                s.setValue(f"ui/{key}_expanded", bool(card.isExpanded()))

    def _restore_window_state(self) -> None:
        """Inverse of _save_window_state. Order matters:
          1. Geometry (window size + position).
          2. Card expanded states. Setting these fires expanded_changed,
             which the rules + bridge collapse handlers use to clamp
             max-height/width — that has to happen *before* splitter
             restore so the splitter state we restore wins, not the
             defaults the handlers reach for.
          3. Splitter states.
          4. Active tab.
        """
        s = QSettings()
        geo = s.value("ui/geometry")
        if geo is not None:
            self.restoreGeometry(geo)

        for key, card in self._collapsible_cards().items():
            if card is None:
                continue
            v = s.value(f"ui/{key}_expanded")
            if v is None:
                continue
            # On the registry backend QSettings returns native bools;
            # on .ini it returns "true"/"false" strings.
            expanded = v if isinstance(v, bool) else str(v).lower() == "true"
            if card.isExpanded() != expanded:
                card.setExpanded(expanded)

        for key, attr in (
            ("body_splitter", "_body_splitter"),
            ("left_splitter", "_left_splitter"),
            ("bridge_left_splitter", "_bridge_left_splitter"),
            ("bridge_body_splitter", "_bridge_body_splitter"),
        ):
            sp = getattr(self, attr, None)
            if sp is None:
                continue
            st = s.value(f"ui/{key}")
            if st is not None:
                sp.restoreState(st)

        if hasattr(self, "_tab_stack"):
            try:
                idx = int(s.value("ui/tab_index", 0))
            except (TypeError, ValueError):
                idx = 0
            idx = max(0, min(self._tab_stack.count() - 1, idx))
            self._tab_stack.setCurrentIndex(idx)
            if hasattr(self, "_tab_strip"):
                self._tab_strip.setCurrentItem("bridge" if idx == 1 else "rules")

    # ---------- bridge ----------

    def _on_bridge_switch_toggled(self, checked: bool) -> None:
        if checked:
            self._start_bridge_service()
        else:
            self._stop_bridge_service()

    def _start_bridge_service(self) -> None:
        if self._bridge is not None and self._bridge.is_running():
            return
        # Cache the primary URL so the countdown can append "next tick
        # in Xs" without losing the URL part of the status text.
        self._bridge_primary_url = ""
        try:
            from press_bridge import BridgeCallbacks, BridgeService
        except Exception as exc:
            self._bridge_switch_status.setText(
                "Stopped — install: uv sync --extra bridge"
            )
            self._bridge_log(f"cannot start: {exc}")
            self._log(f"[bridge] cannot start ({exc})")
            self._bridge_switch.blockSignals(True)
            self._bridge_switch.setChecked(False)
            self._bridge_switch.blockSignals(False)
            return
        bridge_cfg = dict(self._cfg.get("bridge") or {})
        if self._bridge_host_override:
            bridge_cfg["host"] = self._bridge_host_override
        if self._bridge_port_override:
            bridge_cfg["port"] = int(self._bridge_port_override)
        callbacks = BridgeCallbacks(
            cfg_snapshot=self._snapshot_cfg,
            re_match_rule=self._bridge_re_match_rule,
            perform_send=self._bridge_perform_send,
            perform_window_send=self._bridge_perform_window_send,
            perform_window_scroll=self._bridge_perform_window_scroll,
            perform_read=None,
            request_reload=self._bridge_request_reload,
            is_rules_running=self._bridge_is_rules_running,
            set_rules_running=self._bridge_set_rules_running,
            rename_window=self._bridge_rename_window,
            auto_detect_windows=self._bridge_auto_detect_windows,
            new_bridge_setup=self._bridge_new_setup,
            activate_bridge_setup=self._bridge_activate_setup,
            delete_bridge_setup=self._bridge_delete_setup,
            workspace_switch=self._bridge_workspace_switch,
            workspace_bind_active=self._bridge_workspace_bind_active,
        )
        self._bridge = BridgeService(callbacks)
        self._bridge.start(bridge_cfg)
        # Fresh service → fresh tracking. The next worker tick treats
        # every window as first-observed so we capture a snapshot even
        # for windows that are already idle.
        self._worker.reset_window_tracking()

        urls = _bridge_urls(
            str(bridge_cfg.get("host", "0.0.0.0")), int(bridge_cfg.get("port", 8765))
        )
        primary_url = urls[0] if urls else f"http://localhost:{bridge_cfg.get('port', 8765)}/"
        self._bridge_primary_url = primary_url
        self._bridge_switch_status.setText(
            f'Running — <a href="{primary_url}" style="color: #4f9eff; font-weight: 600;">{primary_url}</a>'
        )
        self._log("[bridge] listening — open one of:")
        print("[bridge] listening — open one of:", flush=True)
        for url in urls:
            self._log(f"  {url}")
            print(f"  {url}", flush=True)
        self._bridge_log(f"service started → {primary_url}")

    def _bridge_request_reload(self) -> None:
        """Called from the bridge's request handler thread when the phone
        hits POST /api/admin/reload. We just emit a Qt signal so the
        actual restart runs on the main thread."""
        self.bridge_reload_requested.emit()

    def _reload_bridge_service(self) -> None:
        """Hot-reload the press_bridge module and restart the listener.

        Strategy: try to importlib.reload() *before* tearing the running
        service down. If the new code has a syntax error or import fails,
        the existing service stays up — the user can keep the connection
        and ssh in to fix the breakage. Only after a successful re-import
        do we stop+start.
        """
        self._log("[bridge] reload requested")
        self._bridge_log("reload requested — re-importing press_bridge…")
        import importlib
        try:
            import press_bridge as _pb
            importlib.reload(_pb)
        except Exception as exc:
            msg = f"reload aborted (import failed): {exc}"
            self._log(f"[bridge] {msg}")
            self._bridge_log(msg)
            return
        self._bridge_log("module re-imported, restarting listener…")
        self._stop_bridge_service()
        # Brief pause so uvicorn fully unwinds before we bind again.
        QTimer.singleShot(500, self._start_bridge_service)

    def _stop_bridge_service(self) -> None:
        if self._bridge is None:
            self._bridge_switch_status.setText(
                "Stopped — toggle on to start the FastAPI service."
            )
            return
        try:
            self._bridge.stop()
        except Exception as exc:
            self._log(f"[bridge] stop failed: {exc}")
            self._bridge_log(f"stop failed: {exc}")
        finally:
            self._bridge = None
            self._bridge_primary_url = ""
        self._bridge_switch_status.setText(
            "Stopped — toggle on to start the FastAPI service."
        )
        self._log("[bridge] service stopped")
        self._bridge_log("service stopped")

    def _on_rule_matched(self, event: dict) -> None:
        if self._bridge is None:
            return
        try:
            self._bridge.publish_event(event)
        except Exception as exc:
            self._log(f"[bridge] publish failed: {exc}")

    def _on_bridge_window_transition(self, state: dict) -> None:
        name = state.get("name", "Cursor")
        is_idle = bool(state.get("idle"))
        score = float(state.get("score", 0.0))
        verb = "busy → idle" if is_idle else "idle → busy"
        self._log(f"[bridge] {name}: {verb} (score {score:.3f})")
        self._bridge_log(f"{name}: {verb} (score {score:.3f})")

    def _on_bridge_window_states(self, states: list, images: dict) -> None:
        if self._bridge is None:
            return
        try:
            self._bridge.update_window_states(states, images)
        except Exception as exc:
            self._log(f"[bridge] state push failed: {exc}")
        # Log a one-liner per tick so the user can see the bridge is alive
        # and watch idle/busy land on the desktop side too. Also confirms
        # the phone *should* be receiving an update right now.
        self._bridge_tick_count = getattr(self, "_bridge_tick_count", 0) + 1
        configured = [s for s in states if s.get("configured")]
        idle = sum(1 for s in configured if s.get("idle"))
        busy = len(configured) - idle
        self._bridge_log(
            f"tick #{self._bridge_tick_count} · "
            f"{len(states)} window{'' if len(states) == 1 else 's'} · "
            f"{idle} idle · {busy} busy · {len(images)} snap{'' if len(images) == 1 else 's'}"
        )

    def _bridge_re_match_rule(self, rule_id: str) -> list[tuple[float, tuple[int, int]]]:
        """Worker-thread callable: re-evaluate one rule and return raw matches."""
        from press_engine import find_rule_matches as _find

        cfg = self._snapshot_cfg()
        rule = next((r for r in cfg.get("rules", []) if r.get("id") == rule_id), None)
        if rule is None:
            return []
        runtime_rules = build_runtime_rules({"rules": [rule]})
        if not runtime_rules:
            return []
        return _find(None, runtime_rules[0])

    def _bridge_perform_send(
        self,
        paste_point: tuple[int, int],
        text: str,
        bridge_cfg: dict,
    ) -> None:
        """Worker-thread callable: click target, paste text, press Enter."""
        from press_core import click_point, paste_text_and_enter

        click_point(paste_point)
        paste_text_and_enter(
            text,
            pre_paste_delay_ms=int(bridge_cfg.get("pre_paste_delay_ms", 150)),
            clipboard_restore_delay_ms=int(bridge_cfg.get("clipboard_restore_delay_ms", 500)),
        )

    def _bridge_perform_window_scroll(
        self, window: dict, amount: int, bridge_cfg: dict
    ) -> None:
        """Focus Cursor's chat history with a slow double-click and press
        the arrow key ``|amount|`` times to scroll roughly one screen.
        Positive ``amount`` scrolls up (older messages into view),
        negative scrolls down (newer messages).

        Click target is 5% in from the left edge, 50% down — sits hard
        against the gutter, well outside the chat text where a double-
        click can highlight a word or follow a hyperlink. (10% used to
        catch text occasionally on narrow Cursor windows.) The bridge
        endpoint then schedules a snapshot recapture so the phone
        shows the scrolled view.
        """
        from press_core import focus_and_press_arrow

        region = window.get("region")
        if not region or len(region) != 4:
            return
        x, y, w, h = (int(region[0]), int(region[1]), int(region[2]), int(region[3]))
        target = (x + int(w * 0.05), y + h // 2)
        direction = "down" if int(amount) < 0 else "up"
        focus_and_press_arrow(target, direction, abs(int(amount)))

    def _bridge_is_rules_running(self) -> bool:
        """Read the engine running flag — called from the bridge's request
        handler thread. self._running is updated on the main thread via
        running_changed; reading a Python bool from another thread is
        atomic, no lock needed."""
        return bool(getattr(self, "_running", False))

    def _bridge_set_rules_running(self, running: bool) -> None:
        """Marshal the toggle request from the bridge thread to the main
        thread. The actual flip happens in _set_rules_running_remote
        because Qt UI mutations need to be on the GUI thread."""
        self.bridge_set_rules_requested.emit(bool(running))

    def _set_rules_running_remote(self, running: bool) -> None:
        if running == bool(getattr(self, "_running", False)):
            return  # already in the requested state — no-op
        self._toggle_running()

    def _bridge_rename_window(self, window_id: str, new_name: str) -> bool:
        """Phone hit PUT /api/windows/{id}/name. Mutate the config under
        the cfg lock, persist to disk, then queue a UI refresh on the
        main thread. Returns True if the window was found."""
        found = False
        with self._cfg_lock:
            for w in self._cfg.get("bridge", {}).get("windows", []):
                if w.get("id") == window_id:
                    w["name"] = new_name
                    found = True
                    break
        if found:
            self._persist()
            self.bridge_window_renamed_remote.emit(window_id, new_name)
        return found

    def _on_bridge_window_renamed_remote(self, window_id: str, new_name: str) -> None:
        self._refresh_bridge_windows_table()
        self._bridge_log(f"renamed via web → '{new_name}'")

    def _bridge_auto_detect_windows(self, mode: str) -> int:
        """Phone POSTed /api/admin/auto_detect. Runs the Win32
        enumeration here on the desktop side, mutates the active
        setup's windows under the cfg lock, and returns the new
        window count. Returns -1 when nothing was detected.

        The previous "stash a backup setup" safety net is gone — the
        user explicitly forks via 'New setup' before running this if
        they want to keep the existing layout. Qt widget refreshes
        are deferred to the main thread via the
        bridge_windows_auto_detected signal."""
        from press_windows import list_cursor_windows

        try:
            detected = list_cursor_windows()
        except Exception:
            return -1
        if not detected:
            return -1
        if mode not in ("add", "replace"):
            mode = "add"
        with self._cfg_lock:
            if mode == "replace":
                self._cfg["bridge"]["windows"] = []
            for d in detected:
                win = default_bridge_window(d.get("name", "Cursor"))
                win["region"] = [int(v) for v in d["region"]]
                self._cfg["bridge"]["windows"].append(win)
            new_count = len(self._cfg["bridge"]["windows"])
        self._persist()
        self.bridge_windows_auto_detected.emit(len(detected))
        return new_count

    def _on_bridge_windows_auto_detected(self, count: int) -> None:
        self._refresh_bridge_windows_table()
        self._refresh_bridge_setup_combo()
        self._worker.reset_window_tracking()
        self._bridge_log(
            f"auto-detect via web → added {count} window"
            f"{'s' if count != 1 else ''}"
        )

    def _bridge_new_setup(self, name: str) -> str:
        """POST /api/bridge/setups → create an empty setup, activate
        it, return the new id. Outgoing setup is auto-mirrored."""
        from press_store import new_setup

        with self._cfg_lock:
            sid = new_setup(self._cfg, name)
        self._persist()
        self.bridge_setups_changed_remote.emit("new")
        return sid

    def _bridge_activate_setup(self, setup_id: str) -> bool:
        """POST /api/bridge/setups/{id}/activate → switch active. Live
        windows are replaced with the target setup's snapshot;
        outgoing setup's windows are auto-mirrored first. Returns
        False if id unknown."""
        from press_store import activate_setup

        with self._cfg_lock:
            ok = activate_setup(self._cfg, setup_id)
        if not ok:
            return False
        self._persist()
        self.bridge_setups_changed_remote.emit("activate")
        return True

    def _bridge_delete_setup(self, setup_id: str) -> bool:
        from press_store import delete_setup

        with self._cfg_lock:
            removed = delete_setup(self._cfg, setup_id)
        if not removed:
            return False
        self._persist()
        self.bridge_setups_changed_remote.emit("delete")
        return True

    def _on_bridge_setups_changed_remote(self, action: str) -> None:
        # All three actions (new, activate, delete) can change the
        # active setup's windows, so the table needs refreshing too.
        self._refresh_bridge_setup_combo()
        self._refresh_bridge_windows_table()
        self._worker.reset_window_tracking()
        self._bridge_log(f"setup {action} via web")

    def _bridge_workspace_switch(self, direction: str) -> dict:
        """POST /api/bridge/workspace/switch — fires Ctrl+Win+Right
        (next) or Ctrl+Win+Left (prev), waits for the transition,
        then activates the setup bound to the new workspace if any.
        Returns a dict with the new workspace_id and active setup id."""
        import press_workspace as wsmod
        from press_store import activate_setup

        if direction == "next":
            wsmod.switch_next()
        else:
            wsmod.switch_prev()
        new_wid = wsmod.current_id()
        # Update the desktop's cached last-seen id so the polling
        # timer doesn't double-fire the activation log when it next
        # ticks. Atomic attribute write — safe from this thread.
        self._last_seen_workspace_id = new_wid
        activated_id: Optional[str] = None
        if new_wid:
            with self._cfg_lock:
                bridge = self._cfg.get("bridge", {}) or {}
                match = next(
                    (
                        s
                        for s in (bridge.get("setups") or [])
                        if s.get("workspace_id") == new_wid
                    ),
                    None,
                )
                if match and match.get("id") != bridge.get("active_setup_id"):
                    if activate_setup(self._cfg, match["id"]):
                        activated_id = match["id"]
            if activated_id is not None:
                self._persist()
                self.bridge_setups_changed_remote.emit("workspace-switch")
        return {"workspace_id": new_wid, "active_setup_id": activated_id}

    def _bridge_workspace_bind_active(self) -> Optional[str]:
        """POST /api/bridge/workspace/bind — stamp the current
        workspace's GUID onto the active setup. Returns the GUID."""
        import press_workspace as wsmod

        wid = wsmod.current_id()
        if not wid:
            return None
        with self._cfg_lock:
            active_id = self._cfg.get("bridge", {}).get("active_setup_id")
            if not active_id:
                return None
            for s in self._cfg["bridge"].get("setups", []) or []:
                if s.get("id") == active_id:
                    s["workspace_id"] = wid
                    break
            else:
                return None
        self._persist()
        self._last_seen_workspace_id = wid
        self.bridge_setups_changed_remote.emit("workspace-bind")
        return wid

    def _bridge_perform_window_send(
        self, window: dict, text: str, bridge_cfg: dict
    ) -> None:
        """Click into a Cursor window's chat input, paste text, press Enter.

        Click target is window['chat_target'] when set; otherwise we fall
        back to the centre of the bottom 15% of the region — a reasonable
        default for Cursor's chat input position.
        """
        from press_core import click_point, paste_text_and_enter

        target = window.get("chat_target")
        if not target:
            region = window.get("region")
            if not region or len(region) != 4:
                self._log(
                    f"[bridge] cannot send to '{window.get('name','?')}' — no region"
                )
                return
            x, y, w, h = (int(region[0]), int(region[1]), int(region[2]), int(region[3]))
            target = (x + w // 2, y + int(h * 0.85))
        click_point((int(target[0]), int(target[1])))
        paste_text_and_enter(
            text,
            pre_paste_delay_ms=int(bridge_cfg.get("pre_paste_delay_ms", 150)),
            clipboard_restore_delay_ms=int(bridge_cfg.get("clipboard_restore_delay_ms", 500)),
        )
        # Mirror to the bridge log so the user can confirm a send fired.
        QTimer.singleShot(
            0,
            lambda: self._bridge_log(
                f"sent to '{window.get('name','?')}': {text[:40]}{'…' if len(text) > 40 else ''}"
            ),
        )

    # ---------- global hotkey ----------

    def _start_hotkey_thread(self) -> None:
        if not IS_WINDOWS:
            return
        self._hotkey_stop = threading.Event()
        threading.Thread(target=self._hotkey_loop, daemon=True).start()

    def _stop_hotkey_thread(self, wait: bool = True) -> None:
        if not IS_WINDOWS:
            return
        self._hotkey_stop.set()
        self._post_wm_quit()
        # PostThreadMessage needs a short grace period for the loop to unwind.
        if wait:
            for _ in range(20):
                if self._hotkey_thread_id.get("tid") is None:
                    break
                time.sleep(0.02)

    def _hotkey_loop(self) -> None:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        from ctypes import wintypes

        WM_HOTKEY = 0x0312
        MOD_NOREPEAT = 0x4000
        HOTKEY_ID = 1

        class MSG(ctypes.Structure):
            _fields_ = [
                ("hwnd", wintypes.HWND),
                ("message", wintypes.UINT),
                ("wParam", wintypes.WPARAM),
                ("lParam", wintypes.LPARAM),
                ("time", wintypes.DWORD),
                ("pt", wintypes.POINT),
            ]

        vk = int(self._hotkey_vk)
        mods = int(self._hotkey_mods) | MOD_NOREPEAT
        self._hotkey_thread_id["tid"] = int(kernel32.GetCurrentThreadId())
        try:
            if not user32.RegisterHotKey(None, HOTKEY_ID, mods, vk):
                self._log("[hotkey] failed to register (already taken by another app?)")
                return
            msg = MSG()
            while not self._hotkey_stop.is_set():
                ok = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if ok <= 0:
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self.hotkey_triggered.emit()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
            user32.UnregisterHotKey(None, HOTKEY_ID)
        finally:
            self._hotkey_thread_id["tid"] = None

    def _post_wm_quit(self) -> None:
        tid = self._hotkey_thread_id.get("tid")
        if not tid:
            return
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            WM_QUIT = 0x0012
            user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
        except Exception:
            pass

    def _on_hotkey_changed(self, vk: int, mods: int) -> None:
        """Swap the global hotkey at runtime; persists to config."""
        self._stop_hotkey_thread(wait=True)
        self._hotkey_vk = int(vk)
        self._hotkey_mods = int(mods)
        with self._cfg_lock:
            self._cfg["hotkey_vk"] = self._hotkey_vk
            self._cfg["hotkey_mods"] = self._hotkey_mods
            save_config(self._cfg)
        self._start_hotkey_thread()
        self._log(f"[hotkey] rebound to {self._hotkey_button.text()}")
