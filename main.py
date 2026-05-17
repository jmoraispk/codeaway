"""Auto Press entrypoint."""

import argparse
import os
import signal
import sys

# Force PER_MONITOR_AWARE_V2 before any Qt / PIL import so every thread in
# the process agrees on physical pixel coordinates. Without this, Windows'
# app-compat shim leaves python.exe at V1, which gives ImageGrab and
# GetSystemMetrics an inconsistent virtual-screen origin on mixed-DPI setups.
if sys.platform.startswith("win"):
    try:
        import ctypes

        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass

os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.window=false")

from PySide6.QtCore import QTimer, QtMsgType, qInstallMessageHandler  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from press_ui import MainWindow  # noqa: E402


# qfluentwidgets occasionally creates QFonts with pointSize == -1 (pixel-
# sized) and Qt fires "QFont::setPointSize: Point size <= 0 (-1)" via
# qWarning without a category, so QT_LOGGING_RULES can't catch it. Filter
# those specific lines via a message handler — anything else still prints.
def _qt_message_filter(msg_type: QtMsgType, _context, message: str) -> None:
    text = message if isinstance(message, str) else str(message)
    if "QFont::setPointSize: Point size <= 0" in text:
        return
    sys.stderr.write(text + "\n")


qInstallMessageHandler(_qt_message_filter)


def _install_sigint(app: QApplication, window: MainWindow) -> QTimer:
    """Let Ctrl+C kill the app cleanly.

    Qt's C event loop doesn't yield to Python often enough for a SIGINT
    handler to run. A 100 ms no-op timer forces Python bytecode to execute,
    which is where signal handlers are dispatched.
    """

    def handler(*_):
        print("\n[ctrl+c] shutting down...", flush=True)
        try:
            window._quit_app()
        except Exception:
            app.quit()

    signal.signal(signal.SIGINT, handler)
    timer = QTimer()
    timer.setInterval(100)
    timer.timeout.connect(lambda: None)
    timer.start()
    return timer


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Auto Press — Fluent Design draft."
    )
    parser.add_argument(
        "seconds",
        nargs="?",
        type=float,
        default=None,
        help="Override the scan interval for this run. When omitted, "
             "the value persisted to templates/config.json is used "
             "(falls back to 10 s on first launch).",
    )
    # Both the bridge service and the rules engine are on by default —
    # they're the product. The --no-* flags are the launch-time
    # equivalents of toggling each tab off in the UI; the underlying
    # feature still exists, it just isn't running until you toggle
    # it back on.
    parser.add_argument(
        "--bridge",
        dest="bridge",
        action="store_true",
        default=True,
        help="Start the remote bridge (FastAPI + phone PWA). Default: on.",
    )
    parser.add_argument(
        "--no-bridge",
        dest="bridge",
        action="store_false",
        help="Skip the bridge service for this run.",
    )
    parser.add_argument(
        "--rules",
        dest="rules",
        action="store_true",
        default=True,
        help="Start the rules engine running on launch. Default: on.",
    )
    parser.add_argument(
        "--no-rules",
        dest="rules",
        action="store_false",
        help="Launch with the rules engine paused — toggle it on later from the UI or the phone.",
    )
    parser.add_argument(
        "--bridge-host",
        default=None,
        help="Override the bridge bind host (default from config: 0.0.0.0).",
    )
    parser.add_argument(
        "--bridge-port",
        type=int,
        default=None,
        help="Override the bridge port (default from config: 8765).",
    )
    args = parser.parse_args()
    # Resolve the scan interval: CLI override > persisted config >
    # hardcoded default. Importing press_store here (rather than at
    # the top of main.py) keeps `--help` fast on a cold cache.
    if args.seconds is None:
        from press_store import load_config

        args.seconds = float(load_config().get("interval_seconds", 10.0) or 10.0)
    if args.seconds <= 0:
        raise SystemExit("seconds must be > 0")

    app = QApplication(sys.argv)
    app.setApplicationName("Auto Press")
    # Organization name is required for QSettings to land in a stable
    # location (HKCU\Software\auto-press\Auto Press on Windows). Without
    # it, QSettings falls back to a "Unknown Organization" key per Qt
    # version and our window-state persistence wouldn't survive upgrades.
    app.setOrganizationName("auto-press")
    app.setQuitOnLastWindowClosed(False)

    window = MainWindow(
        initial_seconds=float(args.seconds),
        bridge_enabled=args.bridge,
        bridge_host=args.bridge_host,
        bridge_port=args.bridge_port,
        auto_activate=args.rules,
    )
    window.show()
    _keepalive = _install_sigint(app, window)
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
