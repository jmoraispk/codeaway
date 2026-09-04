"""Core click / type / vision helpers used by the engine and UI."""

import sys
import time

import pyautogui


MODE_CLICK = "click"
MODE_CLICK_ENTER = "click+enter"
CODEX_WHEEL_STEP = 40


def _pin_thread_v2_dpi() -> None:
    """Ensure current thread is PER_MONITOR_AWARE_V2 before any Win32 pixel call.

    pyautogui uses SetCursorPos / mouse_event under the hood, which both
    interpret coordinates in the CURRENT THREAD's DPI context. If the thread
    has drifted off V2 (stale inheritance, another library resetting it),
    physical pixel coords land on the wrong spot. Idempotent, nanoseconds.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


# ---- Win32 SendInput primitives ------------------------------------------
#
# Why we don't just use pyautogui's moveTo + click:
#   1. pyautogui calls moveTo and click as separate WinAPI invocations
#      (SetCursorPos, then mouse_event). Between those calls, physical
#      mouse-hardware events arriving on the user's input queue can shift
#      the cursor — so the click lands at "wherever the cursor is when
#      mouse_event fires", not at our intended target. That's the
#      "teleport, then click somewhere else" symptom when the user is
#      actively moving the mouse as a rule triggers.
#   2. If the user is mid-drag (holding a mouse button) when our action
#      fires, pyautogui's mouseDown is a no-op (button already down) and
#      its mouseUp releases the user's drag instead of completing a
#      clean click on the target.
#
# Both problems collapse with SendInput: one batched call queues every
# event together and MSDN guarantees that "all of the events from a
# single SendInput call are processed before any subsequent input is
# processed" — so physical events can't interleave between our MOVE,
# DOWN, UP, MOVE-back queue. And we prepend a "release any held mouse
# buttons" pass so a mid-drag user doesn't hijack the click sequence.

if sys.platform.startswith("win"):
    import ctypes as _ctypes
    from ctypes import wintypes as _wintypes

    _user32 = _ctypes.WinDLL("user32", use_last_error=True)

    _INPUT_MOUSE = 0

    _MOUSEEVENTF_MOVE = 0x0001
    _MOUSEEVENTF_LEFTDOWN = 0x0002
    _MOUSEEVENTF_LEFTUP = 0x0004
    _MOUSEEVENTF_RIGHTDOWN = 0x0008
    _MOUSEEVENTF_RIGHTUP = 0x0010
    _MOUSEEVENTF_MIDDLEDOWN = 0x0020
    _MOUSEEVENTF_MIDDLEUP = 0x0040
    _MOUSEEVENTF_ABSOLUTE = 0x8000
    _MOUSEEVENTF_VIRTUALDESK = 0x4000

    _VK_LBUTTON = 0x01
    _VK_RBUTTON = 0x02
    _VK_MBUTTON = 0x04
    _VK_TAB = 0x09
    _VK_MENU = 0x12     # Alt
    _VK_CONTROL = 0x11  # Ctrl

    _INPUT_KEYBOARD = 1
    _KEYEVENTF_KEYUP = 0x0002

    _SM_XVIRTUALSCREEN = 76
    _SM_YVIRTUALSCREEN = 77
    _SM_CXVIRTUALSCREEN = 78
    _SM_CYVIRTUALSCREEN = 79

    # ULONG_PTR is 8 bytes on 64-bit Python — c_void_p has the right size
    # on both 32 and 64 bit, which is what the docs recommend for this
    # opaque ptr-sized field.
    _ULONG_PTR = _ctypes.c_void_p

    class _MOUSEINPUT(_ctypes.Structure):
        _fields_ = [
            ("dx", _ctypes.c_long),
            ("dy", _ctypes.c_long),
            ("mouseData", _wintypes.DWORD),
            ("dwFlags", _wintypes.DWORD),
            ("time", _wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _KEYBDINPUT(_ctypes.Structure):
        _fields_ = [
            ("wVk", _wintypes.WORD),
            ("wScan", _wintypes.WORD),
            ("dwFlags", _wintypes.DWORD),
            ("time", _wintypes.DWORD),
            ("dwExtraInfo", _ULONG_PTR),
        ]

    class _INPUT_UNION(_ctypes.Union):
        # Union of every input variant so a single SendInput batch
        # can mix MOUSE and KEYBOARD events (refocus needs both:
        # restore-cursor mouse-move followed by Ctrl+Tab keystrokes).
        _fields_ = [("mi", _MOUSEINPUT), ("ki", _KEYBDINPUT)]

    class _INPUT(_ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [
            ("type", _wintypes.DWORD),
            ("u", _INPUT_UNION),
        ]

    _user32.SendInput.argtypes = [
        _wintypes.UINT,
        _ctypes.POINTER(_INPUT),
        _ctypes.c_int,
    ]
    _user32.SendInput.restype = _wintypes.UINT

    _user32.GetSystemMetrics.argtypes = [_ctypes.c_int]
    _user32.GetSystemMetrics.restype = _ctypes.c_int

    _user32.GetAsyncKeyState.argtypes = [_ctypes.c_int]
    _user32.GetAsyncKeyState.restype = _ctypes.c_short

    def _mouse_input(flags: int, dx: int = 0, dy: int = 0) -> _INPUT:
        ev = _INPUT()
        ev.type = _INPUT_MOUSE
        ev.mi = _MOUSEINPUT(dx, dy, 0, flags, 0, None)
        return ev

    def _key_input(vk: int, key_up: bool = False) -> _INPUT:
        ev = _INPUT()
        ev.type = _INPUT_KEYBOARD
        flags = _KEYEVENTF_KEYUP if key_up else 0
        ev.ki = _KEYBDINPUT(vk, 0, flags, 0, None)
        return ev

    def _refocus_events(refocus_mode: str | None) -> list:
        """Build the SendInput tail that re-routes focus back to
        whatever the user was working in. Returns an empty list
        when ``refocus_mode`` is "off" / unknown — callers append
        the result to the main batch unconditionally."""
        if refocus_mode == "click":
            return [
                _mouse_input(_MOUSEEVENTF_LEFTDOWN),
                _mouse_input(_MOUSEEVENTF_LEFTUP),
            ]
        if refocus_mode in ("ctrl_tab", "alt_tab"):
            mod = _VK_CONTROL if refocus_mode == "ctrl_tab" else _VK_MENU
            return [
                _key_input(mod),
                _key_input(_VK_TAB),
                _key_input(_VK_TAB, key_up=True),
                _key_input(mod, key_up=True),
            ]
        return []

    def _send_inputs(events: list) -> None:
        if not events:
            return
        arr_type = _INPUT * len(events)
        arr = arr_type(*events)
        _user32.SendInput(len(events), arr, _ctypes.sizeof(_INPUT))

    def _to_absolute_xy(x: int, y: int) -> tuple[int, int]:
        """Convert physical-pixel screen coords to the 0–65535 absolute
        range MOUSEEVENTF_ABSOLUTE + MOUSEEVENTF_VIRTUALDESK expects.
        Anchored to the virtual desktop so negative-coord secondary
        monitors map correctly."""
        vleft = _user32.GetSystemMetrics(_SM_XVIRTUALSCREEN)
        vtop = _user32.GetSystemMetrics(_SM_YVIRTUALSCREEN)
        vwidth = _user32.GetSystemMetrics(_SM_CXVIRTUALSCREEN) or 1
        vheight = _user32.GetSystemMetrics(_SM_CYVIRTUALSCREEN) or 1
        # The classic formula uses (range - 1) in the denominator so the
        # right / bottom edge maps to exactly 65535.
        dx = round((int(x) - vleft) * 65535 / max(1, vwidth - 1))
        dy = round((int(y) - vtop) * 65535 / max(1, vheight - 1))
        # Clamp — SendInput silently clips out-of-range absolutes which
        # tends to land clicks at (0, 0). Better to land at the nearest
        # edge of the virtual screen.
        dx = max(0, min(65535, dx))
        dy = max(0, min(65535, dy))
        return dx, dy

    def _release_held_mouse_buttons() -> tuple[bool, bool, bool]:
        """If the user is physically holding L/R/M when our action
        fires, send the matching UP events first so the in-flight drag
        terminates cleanly at the user's current position before we
        move the cursor to our target. Without this, our move would
        drag whatever they were dragging to the target and our click
        would be swallowed as the drag's release."""
        def _down(vk: int) -> bool:
            # High bit of GetAsyncKeyState's SHORT = currently pressed.
            return bool(_user32.GetAsyncKeyState(vk) & 0x8000)

        l = _down(_VK_LBUTTON)
        r = _down(_VK_RBUTTON)
        m = _down(_VK_MBUTTON)
        events = []
        if l:
            events.append(_mouse_input(_MOUSEEVENTF_LEFTUP))
        if r:
            events.append(_mouse_input(_MOUSEEVENTF_RIGHTUP))
        if m:
            events.append(_mouse_input(_MOUSEEVENTF_MIDDLEUP))
        if events:
            _send_inputs(events)
        return (l, r, m)

    def _click_at_target(
        x: int,
        y: int,
        restore_position: tuple[int, int] | None = None,
        refocus_mode: str | None = None,
    ) -> None:
        """Atomic move + left-click via a single SendInput batch.
        Optionally appends a final MOVE back to ``restore_position``
        so the cursor visits the target only briefly. Releases any
        currently-held mouse button first.

        ``refocus_mode`` (only meaningful when ``restore_position``
        is set): "off" / None = nothing extra; "click" = extra left
        click at restored origin (re-focuses by clicking); "ctrl_tab"
        or "alt_tab" = synthesise the keystroke at the restored
        position to re-focus WITHOUT clicking, so a text input's
        caret stays put. All events ride the same SendInput batch.
        """
        _release_held_mouse_buttons()
        dx, dy = _to_absolute_xy(x, y)
        events = [
            _mouse_input(
                _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                dx, dy,
            ),
            _mouse_input(_MOUSEEVENTF_LEFTDOWN),
            _mouse_input(_MOUSEEVENTF_LEFTUP),
        ]
        if restore_position is not None:
            rdx, rdy = _to_absolute_xy(*restore_position)
            events.append(
                _mouse_input(
                    _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                    rdx, rdy,
                )
            )
            events.extend(_refocus_events(refocus_mode))
        _send_inputs(events)

    def _restore_with_refocus(
        origin: tuple[int, int],
        refocus_mode: str | None,
    ) -> None:
        """Move cursor back to ``origin`` and (optionally) fire the
        refocus event sequence — all in one SendInput call. Used by
        do_action's MODE_CLICK_ENTER path where the click + typing
        happens first and the restore-and-refocus runs after the
        Enter keystroke (the click and the restore can't share a
        batch because the typing has to land between them)."""
        rdx, rdy = _to_absolute_xy(*origin)
        events = [
            _mouse_input(
                _MOUSEEVENTF_MOVE | _MOUSEEVENTF_ABSOLUTE | _MOUSEEVENTF_VIRTUALDESK,
                rdx, rdy,
            )
        ]
        events.extend(_refocus_events(refocus_mode))
        _send_inputs(events)
else:
    # Cross-platform stubs so tests / dev on macOS or Linux still work.
    def _release_held_mouse_buttons():  # type: ignore[no-redef]
        return (False, False, False)

    def _click_at_target(x, y, restore_position=None, refocus_mode=None):  # type: ignore[no-redef]
        pyautogui.moveTo(int(x), int(y), duration=0)
        pyautogui.click()
        if restore_position is not None:
            pyautogui.moveTo(int(restore_position[0]), int(restore_position[1]), duration=0)
            if refocus_mode == "click":
                pyautogui.click()
            elif refocus_mode in ("ctrl_tab", "alt_tab"):
                mod = "ctrl" if refocus_mode == "ctrl_tab" else "alt"
                pyautogui.hotkey(mod, "tab")

    def _restore_with_refocus(origin, refocus_mode):  # type: ignore[no-redef]
        pyautogui.moveTo(int(origin[0]), int(origin[1]), duration=0)
        if refocus_mode == "click":
            pyautogui.click()
        elif refocus_mode in ("ctrl_tab", "alt_tab"):
            mod = "ctrl" if refocus_mode == "ctrl_tab" else "alt"
            pyautogui.hotkey(mod, "tab")

WORD_PRE_DELAY_SEC = 0.30
WORD_RETRY_DELAY_SEC = 0.30
WORD_POST_DELAY_SEC = 0.30
ENTER_AFTER_WORD_DELAY_SEC = 0.15


def try_import_vision():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
        return cv2, np, None
    except ImportError as e:
        return None, None, str(e)


def _require_vision():
    cv2, np, err = try_import_vision()
    if err:
        raise RuntimeError("Vision deps missing. Install with: uv sync")
    return cv2, np


def load_template_gray(path: str):
    cv2, _ = _require_vision()
    tpl = cv2.imread(path, cv2.IMREAD_GRAYSCALE)
    if tpl is None:
        raise FileNotFoundError(f"Template unreadable: {path}")
    return tpl


def save_gray_image(path: str, gray_image) -> None:
    cv2, _ = _require_vision()
    ok = cv2.imwrite(path, gray_image)
    if not ok:
        raise RuntimeError(f"Failed to save template image: {path}")


def type_word_with_retry(word: str) -> None:
    sent = False
    last_err = None
    for _ in range(2):
        try:
            time.sleep(WORD_PRE_DELAY_SEC)
            pyautogui.typewrite(word)
            sent = True
            break
        except Exception as e:
            last_err = e
            time.sleep(WORD_RETRY_DELAY_SEC)
    if not sent and last_err is not None:
        raise last_err
    time.sleep(WORD_POST_DELAY_SEC)


def do_action(
    mode: str,
    click_target: tuple[int, int],
    text_before_enter: str | None = None,
    refocus_mode: str | None = None,
) -> None:
    """Rules-engine click action.

    ``refocus_mode`` (experimental): after the rule's click and
    cursor restore, fire one extra event to put focus back on the
    user's typing window.
      - None / "off": no extra event.
      - "click"     : extra LEFTDOWN+LEFTUP at the restored origin.
                      Re-focuses by clicking; can move the caret
                      inside text inputs.
      - "ctrl_tab"  : synthesise Ctrl+Tab. Caret stays put — useful
                      for tabbed apps (browsers, IDEs).
      - "alt_tab"   : synthesise Alt+Tab. Switches to the previous
                      top-level window — useful when the user's
                      typing happens in a different application.
    """
    _pin_thread_v2_dpi()
    x, y = click_target
    old = pyautogui.position()
    # SendInput batch: release-held + move + click + restore (and
    # optional refocus event) in one atomic kernel hop. See the long
    # comment by the SendInput primitives for why pyautogui's
    # moveTo+click pair couldn't survive concurrent physical mouse
    # activity.
    if mode == MODE_CLICK_ENTER:
        # No restore yet — text input wants focus on the target's
        # window. Restore + optional refocus happen after the keys
        # have been pressed (second SendInput batch).
        _click_at_target(x, y)
        if text_before_enter:
            type_word_with_retry(text_before_enter)
            time.sleep(ENTER_AFTER_WORD_DELAY_SEC)
        pyautogui.press("enter")
        if refocus_mode and refocus_mode != "off":
            _restore_with_refocus((old.x, old.y), refocus_mode)
        else:
            pyautogui.moveTo(old.x, old.y, duration=0)
    else:
        _click_at_target(
            x, y,
            restore_position=(old.x, old.y),
            refocus_mode=refocus_mode,
        )


# ---- bridge primitives ---------------------------------------------------

def click_point(point: tuple[int, int]) -> None:
    """Move the cursor and click — without restoring the previous cursor pos.

    do_action snaps back so unattended automation looks invisible. The bridge
    instead keeps the cursor at the click target so the pasted text lands in
    the right field, since some apps refuse focus until the next user input.
    """
    _pin_thread_v2_dpi()
    x, y = int(point[0]), int(point[1])
    _click_at_target(x, y)


def focus_and_press_arrow(
    point: tuple[int, int],
    direction: str = "up",
    presses: int = 15,
) -> None:
    """Focus the panel at ``point`` with two slow clicks, then press the
    Up or Down arrow ``presses`` times to scroll Cursor's chat history.

    ``direction`` is "up" or "down". Anything else is treated as "up".

    Why double-click with a delay:
      - A single click in Cursor often lands the caret in the chat
        *input* instead of giving the chat *history* focus. Two clicks
        reliably take focus away from the input.
      - 0.5 s between clicks avoids being interpreted as a fast double-
        click (which would select a word) and gives the UI time to
        settle between the two events.

    Arrow keys instead of mouse wheel: pyautogui.scroll's wheel events
    behave inconsistently across Cursor's nested electron views;
    arrow keystrokes are reliable and predictable. ~15 presses moves
    roughly one screenful in Cursor's chat at default zoom.
    """
    key = "down" if direction == "down" else "up"
    _pin_thread_v2_dpi()
    x, y = int(point[0]), int(point[1])
    # Two distinct clicks with a 0.5s gap (see docstring). Each one
    # goes through the atomic-batch primitive so a user mid-mouse-
    # move can't divert either click off-target.
    _click_at_target(x, y)
    time.sleep(0.5)
    _click_at_target(x, y)
    time.sleep(0.1)
    for _ in range(max(0, int(presses))):
        pyautogui.press(key)
        # Tiny gap so each keystroke is processed as a discrete event
        # — with no delay Cursor sometimes coalesces them.
        time.sleep(0.02)


def focus_and_press_up(point: tuple[int, int], presses: int = 15) -> None:
    """Back-compat shim — kept so existing callers don't break. New
    code should call focus_and_press_arrow(point, "up", presses)."""
    focus_and_press_arrow(point, "up", presses)


def focus_and_scroll(point: tuple[int, int], amount: int) -> None:
    """Focus a calibrated surface and send native mouse-wheel input.

    Codex's nested conversation viewport consumes wheel input at the pointer;
    unlike Cursor, it does not reliably scroll when arrow keys are sent after
    a generic focus click.
    """
    _pin_thread_v2_dpi()
    x, y = int(point[0]), int(point[1])
    _click_at_target(x, y)
    time.sleep(0.1)
    remaining = abs(int(amount))
    direction = 1 if int(amount) > 0 else -1
    # Chromium occasionally drops a single large wheel injection. Small,
    # paced chunks behave like a physical wheel and make phone swipes
    # proportional without flooding the desktop event queue. PyAutoGUI's
    # Windows adapter forwards ``clicks`` directly as Win32 ``mouseData``.
    # Windows defines one complete wheel detent as 120 units; Codex responds
    # best to a 40-unit third-detent per logical phone step. Sending bare values
    # such as 3 produces only a few pixels, while larger steps over-travel.
    while remaining:
        step = min(3, remaining)
        pyautogui.scroll(direction * step * CODEX_WHEEL_STEP, x=x, y=y)
        remaining -= step
        if remaining:
            time.sleep(0.025)


def get_clipboard_text() -> str:
    """Best-effort current clipboard text (empty string if unavailable)."""
    import pyperclip  # ships transitively with pyautogui
    try:
        return pyperclip.paste() or ""
    except Exception:
        return ""


def set_clipboard_text(text: str) -> None:
    import pyperclip
    try:
        pyperclip.copy(text)
    except Exception:
        pass


def paste_text_and_enter(
    text: str,
    pre_paste_delay_ms: int = 150,
    clipboard_restore_delay_ms: int = 500,
) -> None:
    """Paste ``text`` via Ctrl+V then press Enter, restoring the prior
    clipboard. Empty text is allowed: in that case the clipboard is left
    untouched and we just press Enter — useful when the user already
    composed the message in the target field and only needs the submit
    keystroke from the bridge.

    Pre-delay lets the focused field settle after the click; the restore
    delay covers slow-clipboard apps that otherwise read the new value back
    into themselves before we put the original contents back.
    """
    _pin_thread_v2_dpi()
    if pre_paste_delay_ms > 0:
        time.sleep(pre_paste_delay_ms / 1000.0)
    if text:
        saved = get_clipboard_text()
        set_clipboard_text(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)
        pyautogui.press("enter")
        if clipboard_restore_delay_ms > 0:
            time.sleep(clipboard_restore_delay_ms / 1000.0)
        set_clipboard_text(saved)
    else:
        pyautogui.press("enter")
