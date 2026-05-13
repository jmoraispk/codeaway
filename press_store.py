"""Persistence helpers for rules, templates, and the UI config."""

from __future__ import annotations

import json
import uuid
from pathlib import Path


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
CONFIG_PATH = TEMPLATES_DIR / "config.json"

ACTION_CLICK = "click"
ACTION_CLICK_TYPE_ENTER = "click+type+enter"
ACTION_TYPES = [ACTION_CLICK, ACTION_CLICK_TYPE_ENTER]

MATCHER_TEMPLATE = "template"
MATCHER_COLOR = "color"
MATCHER_TYPES = [MATCHER_TEMPLATE, MATCHER_COLOR]


def default_rule(name: str = "New Rule") -> dict:
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "enabled": True,
        "matcher": MATCHER_TEMPLATE,
        "template_path": None,
        # Color-matcher fields. Empty until a color is captured.
        "color_rgb": None,
        "color_name": "",
        "color_capture_area": 0,
        "search_region": None,
        "threshold": 0.90,
        "action": ACTION_CLICK,
        "text": "continue",
        "priority": 1,
    }


def default_setup(name: str = "Default") -> dict:
    """A named snapshot of bridge.windows the user can switch back to.

    The user's live windows live in cfg['bridge']['windows']. Setups
    are saved copies — explicit named backups that the user can switch
    to at any time. Auto-detect / Replace flows stash a backup setup
    automatically so the previous layout is one click away from being
    restored.
    """
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "windows": [],
    }


def default_bridge_window(name: str = "Cursor") -> dict:
    """A single Cursor window the bridge should monitor.

    region          — whole-window bbox in physical pixels [x, y, w, h]
    chat_target     — click point for the chat input [x, y]; defaults to
                       the centre of the bottom 20% of `region` if None
    read_region     — area to snapshot as a PNG so the phone can show
                       the agent's most recent reply; None disables read
    """
    return {
        "id": uuid.uuid4().hex[:8],
        "name": name,
        "region": None,
        "chat_target": None,
        "read_region": None,
    }


# Windows Virtual-Key code for the default global start/stop hotkey.
# 0x22 is VK_PAGEDOWN; modifiers bitmask matches RegisterHotKey MOD_* flags.
DEFAULT_HOTKEY_VK = 0x22
DEFAULT_HOTKEY_MODS = 0


def default_bridge_config() -> dict:
    # The bridge is gated by the --bridge CLI flag, not by config. The keys
    # here describe what the bridge monitors (windows + idle template) plus
    # bind/notification/timing knobs.
    return {
        "host": "0.0.0.0",
        "port": 8765,
        "ntfy_topic": "",
        "ntfy_server": "https://ntfy.sh",
        "pre_paste_delay_ms": 150,
        "clipboard_restore_delay_ms": 500,
        "tailnet_only": False,
        # The single template that, when found inside a window's region,
        # means that window is idle and ready for input.
        "idle_template_path": None,
        "idle_threshold": 0.90,
        # Optional second template: a marker for Cursor's AskUserQuestion
        # multiple-choice prompt (e.g. the "Submit answers" pill at the
        # bottom of the question card). When this matches the window is
        # in "asking" state — a third option alongside idle / busy.
        # Captured the same way as the idle template via the Bridge tab.
        "askuser_template_path": None,
        # Cursor windows the bridge watches; empty list = bridge has nothing
        # useful to do. Each entry is a default_bridge_window() dict.
        "windows": [],
        # Saved layouts the user can switch between. Each entry is a
        # default_setup() dict — id, name, and a snapshot of the
        # windows list at save time. The live `windows` field above is
        # the active layout; setups are explicit backups.
        "setups": [],
    }


def default_config() -> dict:
    return {
        "interval_seconds": 10.0,
        "hotkey_vk": DEFAULT_HOTKEY_VK,
        "hotkey_mods": DEFAULT_HOTKEY_MODS,
        "rules": [],
        "bridge": default_bridge_config(),
    }


def ensure_templates_dir() -> None:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)


def _clamp_float(value, default: float, min_value: float, max_value: float) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return max(min_value, min(max_value, parsed))


def _valid_region(region) -> bool:
    if region is None:
        return True
    if not isinstance(region, list) or len(region) != 4:
        return False
    try:
        # Only width/height must be positive. left/top can be negative on
        # Windows when a monitor is placed to the left of (or above) the
        # primary — physical coords there are negative. Stripping such
        # regions wiped persisted bridge windows on multi-monitor setups.
        _left, _top, width, height = [int(v) for v in region]
    except (TypeError, ValueError):
        return False
    return width > 0 and height > 0


def _valid_rgb(value) -> bool:
    if not isinstance(value, (list, tuple)) or len(value) != 3:
        return False
    try:
        return all(0 <= int(c) <= 255 for c in value)
    except (TypeError, ValueError):
        return False


def _valid_point(value) -> bool:
    if value is None:
        return True
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return False
    try:
        int(value[0]); int(value[1])
    except (TypeError, ValueError):
        return False
    return True


def _normalize_rule(rule: dict, priority: int) -> dict:
    base = default_rule()
    if isinstance(rule, dict):
        for key in base:
            if key in rule:
                base[key] = rule[key]
    if not isinstance(base.get("id"), str) or not base["id"].strip():
        base["id"] = uuid.uuid4().hex[:8]
    if not isinstance(base.get("name"), str) or not base["name"].strip():
        base["name"] = f"Rule {priority}"
    if base.get("action") not in ACTION_TYPES:
        base["action"] = ACTION_CLICK
    if base.get("matcher") not in MATCHER_TYPES:
        base["matcher"] = MATCHER_TEMPLATE
    base["enabled"] = bool(base.get("enabled", True))
    base["threshold"] = _clamp_float(base.get("threshold"), 0.90, 0.0, 1.0)
    base["priority"] = priority
    if not isinstance(base.get("text"), str):
        base["text"] = "continue"
    if not _valid_region(base.get("search_region")):
        base["search_region"] = None
    tpl = base.get("template_path")
    if not isinstance(tpl, str) or not tpl.strip():
        base["template_path"] = None
    if _valid_rgb(base.get("color_rgb")):
        base["color_rgb"] = [int(c) for c in base["color_rgb"]]
    else:
        base["color_rgb"] = None
    if not isinstance(base.get("color_name"), str):
        base["color_name"] = ""
    try:
        area = int(base.get("color_capture_area") or 0)
    except (TypeError, ValueError):
        area = 0
    base["color_capture_area"] = max(0, area)
    return base


def _normalize_setup(setup: dict | None) -> dict:
    """Coerce an arbitrary dict into a valid setup. Missing / blank
    fields fall back to defaults; the windows list is run through the
    standard window normaliser so legacy saved setups load cleanly."""
    base = default_setup()
    if isinstance(setup, dict):
        sid = setup.get("id")
        if isinstance(sid, str) and sid.strip():
            base["id"] = sid.strip()
        name = setup.get("name")
        if isinstance(name, str) and name.strip():
            base["name"] = name.strip()
        raw = setup.get("windows")
        if isinstance(raw, list):
            base["windows"] = [_normalize_window(w) for w in raw]
    return base


def _normalize_window(window: dict | None) -> dict:
    base = default_bridge_window()
    if isinstance(window, dict):
        for key in base:
            if key in window:
                base[key] = window[key]
    if not isinstance(base.get("id"), str) or not base["id"].strip():
        base["id"] = uuid.uuid4().hex[:8]
    if not isinstance(base.get("name"), str) or not base["name"].strip():
        base["name"] = "Cursor"
    if not _valid_region(base.get("region")):
        base["region"] = None
    if _valid_point(base.get("chat_target")) and base.get("chat_target") is not None:
        base["chat_target"] = [int(base["chat_target"][0]), int(base["chat_target"][1])]
    else:
        base["chat_target"] = None
    if not _valid_region(base.get("read_region")):
        base["read_region"] = None
    return base


def _valid_vk(value) -> bool:
    try:
        return 0 <= int(value) <= 0xFFFF
    except (TypeError, ValueError):
        return False


def _normalize_bridge(bridge: dict | None) -> dict:
    base = default_bridge_config()
    if not isinstance(bridge, dict):
        return base
    if isinstance(bridge.get("host"), str) and bridge["host"].strip():
        base["host"] = bridge["host"].strip()
    try:
        port = int(bridge.get("port", base["port"]))
        if 1 <= port <= 65535:
            base["port"] = port
    except (TypeError, ValueError):
        pass
    if isinstance(bridge.get("ntfy_topic"), str):
        base["ntfy_topic"] = bridge["ntfy_topic"].strip()
    if isinstance(bridge.get("ntfy_server"), str) and bridge["ntfy_server"].strip():
        base["ntfy_server"] = bridge["ntfy_server"].strip()
    base["pre_paste_delay_ms"] = int(_clamp_float(bridge.get("pre_paste_delay_ms"), 150, 0, 10000))
    base["clipboard_restore_delay_ms"] = int(
        _clamp_float(bridge.get("clipboard_restore_delay_ms"), 500, 0, 10000)
    )
    base["tailnet_only"] = bool(bridge.get("tailnet_only", False))

    tpl = bridge.get("idle_template_path")
    if isinstance(tpl, str) and tpl.strip():
        base["idle_template_path"] = tpl.strip()
    else:
        base["idle_template_path"] = None
    base["idle_threshold"] = _clamp_float(bridge.get("idle_threshold"), 0.90, 0.0, 1.0)
    ask = bridge.get("askuser_template_path")
    if isinstance(ask, str) and ask.strip():
        base["askuser_template_path"] = ask.strip()
    else:
        base["askuser_template_path"] = None

    raw_windows = bridge.get("windows")
    if isinstance(raw_windows, list):
        base["windows"] = [_normalize_window(w) for w in raw_windows]
    raw_setups = bridge.get("setups")
    if isinstance(raw_setups, list):
        base["setups"] = [_normalize_setup(s) for s in raw_setups]
    return base


def normalize_config(config: dict | None) -> dict:
    base = default_config()
    if isinstance(config, dict):
        if "interval_seconds" in config:
            base["interval_seconds"] = _clamp_float(config["interval_seconds"], 10.0, 0.1, 86400.0)
        if _valid_vk(config.get("hotkey_vk")):
            base["hotkey_vk"] = int(config["hotkey_vk"])
        if _valid_vk(config.get("hotkey_mods")):
            base["hotkey_mods"] = int(config["hotkey_mods"])
        raw_rules = config.get("rules")
        if isinstance(raw_rules, list):
            base["rules"] = [_normalize_rule(rule, idx + 1) for idx, rule in enumerate(raw_rules)]
        base["bridge"] = _normalize_bridge(config.get("bridge"))
    return base


def load_config() -> dict:
    ensure_templates_dir()
    if not CONFIG_PATH.exists():
        return default_config()
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return default_config()
    return normalize_config(loaded)


def save_config(config: dict) -> None:
    ensure_templates_dir()
    normalized = normalize_config(config)
    CONFIG_PATH.write_text(json.dumps(normalized, indent=2), encoding="utf-8")


def template_asset_path(name: str) -> Path:
    ensure_templates_dir()
    return TEMPLATES_DIR / name


def serialize_template_path(path: str | Path) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(TEMPLATES_DIR.resolve()))
    except ValueError:
        return str(p.resolve())


def resolve_template_path(template_ref: str | None) -> Path | None:
    if not template_ref:
        return None
    p = Path(template_ref)
    if p.is_absolute():
        return p
    return TEMPLATES_DIR / p


def relativize_template_path(path: str | Path) -> str:
    p = Path(path)
    try:
        return str(p.resolve().relative_to(TEMPLATES_DIR.resolve()))
    except ValueError:
        return p.name


def list_template_files() -> list[str]:
    ensure_templates_dir()
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    files = [p.name for p in TEMPLATES_DIR.iterdir() if p.is_file() and p.suffix.lower() in exts]
    return sorted(files)


def make_rule_summary(rule: dict, last_score: float | None = None) -> str:
    enabled = "on" if rule.get("enabled") else "off"
    scope = "screen" if not rule.get("search_region") else "region"
    action = rule.get("action", ACTION_CLICK)
    score = "-" if last_score is None else f"{last_score:.3f}"
    return f"{rule.get('priority', '?')}. {rule.get('name', 'Rule')} [{enabled}] {action} {scope} score={score}"


# ---- Setups: named snapshots of the bridge's live windows list ------------

def save_setup_from_current(cfg: dict, name: str) -> str:
    """Snapshot the live bridge.windows as a new named setup. Returns
    the new setup's id.

    Deep-copies the windows so subsequent edits to bridge.windows don't
    leak into the saved snapshot.
    """
    bridge = cfg.setdefault("bridge", default_bridge_config())
    bridge.setdefault("setups", [])
    setup = default_setup(name)
    setup["windows"] = [dict(w) for w in (bridge.get("windows") or [])]
    bridge["setups"].append(setup)
    return setup["id"]


def load_setup(cfg: dict, setup_id: str) -> bool:
    """Copy the named setup's windows into the live bridge.windows
    list. Returns True if the setup was found, False otherwise. The
    setup itself stays in the saved list — load is non-destructive
    of the snapshot."""
    bridge = cfg.get("bridge", {}) or {}
    for s in bridge.get("setups", []) or []:
        if s.get("id") == setup_id:
            cfg["bridge"]["windows"] = [dict(w) for w in s.get("windows", [])]
            return True
    return False


def delete_setup(cfg: dict, setup_id: str) -> bool:
    """Remove the named setup. Returns True if removed, False if not
    found. Doesn't touch the live windows list."""
    bridge = cfg.get("bridge", {}) or {}
    setups = bridge.get("setups", []) or []
    for i, s in enumerate(setups):
        if s.get("id") == setup_id:
            setups.pop(i)
            return True
    return False
