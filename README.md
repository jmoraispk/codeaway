<p align="center">
  <img src="imgs/ui.jpg" alt="auto-press UI" width="760">
</p>

<h1 align="center">🖱️ CodeAway</h1>

<p align="center">
  <a href="https://codeaway.dev"><img src="https://img.shields.io/badge/site-codeaway.dev-7da7e8.svg" alt="Site"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.14%20preferred-blue.svg" alt="Python"></a>
  <a href="#-faq"><img src="https://img.shields.io/badge/platform-Windows-0078D6.svg" alt="Platform"></a>
  <a href="https://github.com/astral-sh/uv"><img src="https://img.shields.io/badge/packaged%20with-uv-261230.svg" alt="uv"></a>
  <a href="#"><img src="https://img.shields.io/badge/status-active-2e7d32.svg" alt="Status"></a>
</p>

<p align="center"><strong>Code away from your desk — open-source automation for IDEs and AI agents.</strong></p>

CodeAway keeps [Cursor](https://cursor.com/) agents moving without babysitting. Sandboxes take time to spin up and don't work for every workflow; if you already have a Cursor window open, this tool watches it, clicks the right button, and keeps the agent running on its own. It's simple, fast and local.

> **Free forever. MIT licensed. No accounts, no telemetry, no pricing.**<br>
> Landing page lives in [`site/`](site/) and is deployed at [codeaway.dev](https://codeaway.dev).

## ☕ Why this exists

Coding is supposed to be a calm activity, not a chained-to-the-desk one. CodeAway exists for three reasons:

1. **Take a real walk in the middle of the day — and stay productive.** Your agents keep working while you're outside. Lunch, a coffee, a half-hour around the block: none of it costs you progress.

2. **Stop feeling bad about stepping away.** No more "I should probably stay at my desk in case the agent stalls." It won't, and if it does, your phone is the leash.

3. **Travel without a laptop.** Long meetings, doctor's appointments, the in-between hours — your phone is enough, and often faster than the IDE itself, since the bridge cuts straight to the chat without IDE chrome.

A normal workday has a few hours of dead time — agents thinking, tests running, you waiting. CodeAway turns those hours into ground covered.

## 🚀 Quickstart

<a href="https://youtu.be/V4NTQVTd4Rs" target="_blank" rel="noopener"><img align="right" width="380" src="imgs/demo-thumbnail.png" alt="Watch the CodeAway demo on YouTube"></a>

Install [uv](https://github.com/astral-sh/uv), then:

```bash
uv sync
uv run main.py
```

That's it — the UI opens and you're ready to add your first rule. Watch the video walkthrough 👉

## 🧭 The workflow

Each rule is a few clicks:

1. **Add a rule.** Name it and toggle it on.
2. **Pick an action.** Click, click + Enter, or click + send (send = type a word, then Enter).
3. **Capture the target.** A crop of the button — triggers the rule; the cursor clicks its center.
4. **Test & Save.** Run a single match to confirm, then save.
5. **Press Page Down** to start / stop — rebind via the **Hotkey** button. The window can hide to the tray.

## 🟢🔴 Tray indicator

auto-press sits in the Windows system tray. The dot color tells you what it's doing:

<table>
  <tr>
    <td align="center"><img src="imgs/tray_off.png" alt="Stopped" width="110"><br><strong>Stopped</strong><br><sub>not scanning</sub></td>
    <td align="center"><img src="imgs/tray_on.png" alt="Running" width="110"><br><strong>Running</strong><br><sub>scanning &amp; firing rules</sub></td>
  </tr>
</table>

Left-click the icon to show/hide the window. Right-click for Start/Stop and Quit.

## 📱 Remote Bridge

A tiny FastAPI server that turns your phone over Tailscale into a remote control for your Cursor windows:

- 🟢🔴 **Live status per window** — idle / busy dots over Server-Sent Events; no polling, no flicker.
- ✍️ **Send / queue replies** — type on the phone; sends if window is idle, else queues until available.
- 📜 **Tap to scroll + screenshot** — read all the context without touching the laptop.
- 📲 **PWA install** — full-screen app on your home screen, no browser chrome.

The bridge is **on by default**. Pass `--no-bridge` if you want a rules-only run.

### Flags

```bash
uv run main.py --no-bridge              # rules-only, no FastAPI listener
uv run main.py --no-rules               # bridge-only, toggle rules on later
uv run main.py --bridge-host 127.0.0.1  # bind to loopback only
uv run main.py --bridge-port 8765       # override default port
```

Once running, open `http://<laptop-tailscale-id>:8765` from the phone.

### Codex Agent Window v2

The v2 branch keeps the same phone URL but separates Codex into three
calibrated interaction surfaces instead of guessing points inside the full
window:

1. Keep Codex visible and choose **Auto-detect** on the Bridge tab.
2. In **Codex Agent Window v2**, select the Codex window.
3. Choose **Set sidebar** and drag around the project/task navigator.
4. Choose **Set conversation** and drag around the scrollable output pane.
5. Choose **Set composer** and drag around the message input box.

The phone then shows a persistent Navigator above the current Conversation.
Taps activate the exact Codex HWND before clicking, scrolling uses native
wheel input inside the calibrated conversation, and Send targets the
calibrated composer. Calibration is stored as window-relative coordinates, so
moving or resizing Codex does not require repeating it.

## ❓ FAQ

<details>
<summary><strong>How do I keep the tray icon always visible on Windows?</strong></summary>

By default Windows hides new tray icons inside the `^` overflow flyout. Windows doesn't let apps force the icon to be pinned — it's a per-user setting you toggle once:

- **Windows 11**: Settings → Personalization → Taskbar → *Other system tray icons* → turn on `Auto Press` (or `python.exe` while the app is running).
- **Windows 10**: Settings → Personalization → Taskbar → *Select which icons appear on the taskbar* → turn on `Auto Press`.

After that, the red/green dot stays next to the clock whenever auto-press is running.
</details>

<details>
<summary><strong>Does this work on macOS or Linux?</strong></summary>

Today auto-press is **Windows-only in practice**. The engine, UI, and template matching are cross-platform (PySide6 + Pillow + OpenCV run everywhere), but three pieces lean on Win32 APIs:

- Per-monitor-v2 DPI awareness for reliable capture across mixed-DPI monitors.
- Physical-pixel cursor and monitor enumeration (`GetCursorPos`, `EnumDisplayMonitors`).
- Global **Page Down** hotkey (`RegisterHotKey`).

macOS / Linux parity is on the roadmap but **low priority** — happy to pick it up if someone finds it useful. Contributions welcome; the three items above are all that'd need writing, the rest already works.

</details>

<details>
<summary><strong>Why does my template match on one monitor but not another?</strong></summary>

Different DPI scalings. Template matching isn't scale-invariant — capture the template on the monitor you want it to match on, or set both monitors to the same Windows display scaling.
</details>

<details>
<summary><strong>How do I set a custom Start/Stop shortcut?</strong></summary>

Click the **Hotkey** button in the toolbar (next to Start), then press the key combination you want — modifiers included, so `Ctrl+Alt+F9` works just as well as plain `F12`. Your choice is saved to `templates/config.json` and re-registered on the spot. Default is **Page Down**; press **Esc** while capturing to cancel.
</details>

<details>
<summary><strong>What does each file do?</strong></summary>

- [main.py](main.py) — entrypoint; forces per-monitor-v2 DPI awareness and installs a SIGINT handler.
- [press_ui.py](press_ui.py) — Fluent-Design UI, engine worker thread, per-monitor drag-capture overlays, hotkey picker, tray icon.
- [press_engine.py](press_engine.py) — screen capture + template matching + action dispatch.
- [press_store.py](press_store.py) — config persistence (rules + hotkey + interval) and template-file helpers.
- [press_core.py](press_core.py) — click / type / vision primitives used by the engine.
- [press_bridge.py](press_bridge.py) — FastAPI server, SSE event hub, send pipeline.
- [bridge_phone/](bridge_phone/) — vanilla HTML/CSS/JS PWA served by the bridge.
- [templates/](templates/) — captured template images and `config.json`. User-local; gitignored.
</details>
