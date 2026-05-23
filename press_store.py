"""Persistence helpers for rules, templates, and the UI config."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path


TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
CONFIG_PATH = TEMPLATES_DIR / "config.json"
# Bundled defaults the app seeds on first launch — see defaults/README.md.
DEFAULTS_DIR = Path(__file__).resolve().parent / "defaults"
DEFAULTS_MANIFEST_PATH = DEFAULTS_DIR / "manifest.json"

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
        # Per-window scope. Empty list = rule applies everywhere
        # (current default). Non-empty = rule only fires on windows
        # whose ``name`` is in the list. Identifier is the window
        # name (after _short_label trim) because HWNDs change across
        # Cursor restarts but the project-derived name tends to
        # repeat. Renaming a window via the UI invalidates a scope
        # that references the old name — user re-picks in the editor.
        "window_scope": [],
        "template_path": None,
        # Scale factor the template was captured at (1.0, 1.25, …). New
        # captures generate scaled variants for every supported preset
        # and stash the original here so the matcher can pick the right
        # one per search_region's monitor. None = legacy capture (no DPI
        # awareness, matches only at the original capture scale).
        "template_source_dpi": None,
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


def default_bridge_window(name: str = "Cursor") -> dict:
    """A single Cursor window the bridge monitors.

    The window list is rebuilt by auto-detect — there's no manual
    "Add window" path anymore. Each entry is the HWND-keyed view of
    a Cursor window currently on the user's foreground workspace.

    hwnd            — Win32 HWND. Stable while Cursor stays open; the
                       HWND-keyed merge inside auto-detect preserves
                       user-edited fields (name, chat_target) for as
                       long as the HWND is alive.
    region          — whole-window bbox in physical pixels [x, y, w, h],
                       re-read from GetWindowRect on every auto-detect
    chat_target     — click point for the chat input [x, y]; defaults to
                       the centre of the bottom 20% of `region` if None
    read_region     — area to snapshot as a PNG so the phone can show
                       the agent's most recent reply; None disables read
    """
    return {
        "id": uuid.uuid4().hex[:8],
        "hwnd": None,
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
        # Scale factor (1.0 / 1.25 / 1.5 / 1.75 / 2.0) the idle template
        # was captured at. None for legacy captures with no DPI metadata
        # — they still match, but only on monitors at the same DPI as
        # the original capture. New captures generate scaled variants
        # for every supported preset and store the original here.
        "idle_template_source_dpi": None,
        "idle_threshold": 0.90,
        # Optional second template: a marker for Cursor's AskUserQuestion
        # multiple-choice prompt (e.g. the "Submit answers" pill at the
        # bottom of the question card). When this matches the window is
        # in "asking" state — a third option alongside idle / busy.
        # Captured the same way as the idle template via the Bridge tab.
        "askuser_template_path": None,
        "askuser_template_source_dpi": None,
        # Cursor windows the bridge watches. Rebuilt by auto-detect on
        # bridge start, manual button, and workspace change — there's
        # no manual add/remove path. The HWND-keyed merge inside
        # auto-detect preserves user-edited fields (renamed names,
        # chat_target overrides) across re-detects.
        "windows": [],
        # Web Push: VAPID keypair is generated on first launch via
        # press_push.ensure_vapid_keys; both halves live here so the
        # public key stays stable across restarts (otherwise every
        # phone subscription would silently break). push_subscriptions
        # is the list of registered phones, each entry shaped
        # ``{"id": str, "endpoint": str, "keys": {p256dh, auth},
        # "label": str|None, "created_at": iso}``.
        "vapid_public_key": None,
        "vapid_private_key": None,
        "push_subscriptions": [],
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
    # window_scope: list of window names the rule applies to. Trim
    # any non-string entries, strip whitespace, dedupe while keeping
    # order. Empty list (or anything that normalises to empty) means
    # "all windows" — the legacy behaviour.
    raw_scope = base.get("window_scope")
    if isinstance(raw_scope, list):
        cleaned: list[str] = []
        seen: set[str] = set()
        for item in raw_scope:
            if not isinstance(item, str):
                continue
            name = item.strip()
            if name and name not in seen:
                cleaned.append(name)
                seen.add(name)
        base["window_scope"] = cleaned
    else:
        base["window_scope"] = []
    if not _valid_region(base.get("search_region")):
        base["search_region"] = None
    tpl = base.get("template_path")
    if not isinstance(tpl, str) or not tpl.strip():
        base["template_path"] = None
    base["template_source_dpi"] = _normalize_dpi(base.get("template_source_dpi"))
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
    # HWND is opaque to us — just check it's an int. None when the
    # entry came from a legacy config that predates dynamic tracking.
    try:
        h = base.get("hwnd")
        base["hwnd"] = int(h) if h is not None else None
    except (TypeError, ValueError):
        base["hwnd"] = None
    return base


# Supported display-scaling presets — must match press_dpi.SUPPORTED_SCALES
# (kept here to avoid importing Win32-only code from the store at module load).
SUPPORTED_DPI_SCALES: tuple[float, ...] = (1.0, 1.25, 1.5, 1.75, 2.0)


def _normalize_dpi(value) -> float | None:
    """Coerce a source-DPI metadata field. Accepts the supported
    preset scales (1.0/1.25/1.5/1.75/2.0); blank/None/invalid →
    None (legacy template, no DPI awareness)."""
    if value is None:
        return None
    try:
        scale = float(value)
    except (TypeError, ValueError):
        return None
    if scale <= 0:
        return None
    # Snap to nearest preset within 5 % so a rounding drift doesn't
    # leave us with a scale that has no matching variant file.
    best = min(SUPPORTED_DPI_SCALES, key=lambda s: abs(s - scale))
    if abs(best - scale) <= 0.05:
        return best
    return float(scale)


def dpi_tag(scale: float) -> str:
    """Filename suffix for a scale factor — '100', '125', etc."""
    return f"{int(round(scale * 100))}"


def dpi_variant_path(base_path: Path | str, scale: float) -> Path:
    """Path to the DPI variant of ``base_path`` at the given scale.

    Naming convention: ``<base>.dpi<n>.png``. The base file itself is
    a duplicate of the source-DPI variant, so a non-DPI-aware caller
    can keep using ``base_path`` and get the original capture."""
    base = Path(base_path)
    return base.with_suffix(f".dpi{dpi_tag(scale)}{base.suffix}")


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
    base["idle_template_source_dpi"] = _normalize_dpi(
        bridge.get("idle_template_source_dpi")
    )
    base["idle_threshold"] = _clamp_float(bridge.get("idle_threshold"), 0.90, 0.0, 1.0)
    ask = bridge.get("askuser_template_path")
    if isinstance(ask, str) and ask.strip():
        base["askuser_template_path"] = ask.strip()
    else:
        base["askuser_template_path"] = None
    base["askuser_template_source_dpi"] = _normalize_dpi(
        bridge.get("askuser_template_source_dpi")
    )

    raw_windows = bridge.get("windows")
    if isinstance(raw_windows, list):
        base["windows"] = [_normalize_window(w) for w in raw_windows]
    # Legacy migration: if the loaded config has setups + active_setup_id
    # (pre-simplification schema), keep whichever windows the active
    # setup carried. ``bridge.windows`` already mirrored the active
    # setup on save, so usually it's a no-op — but if the live windows
    # are empty and we have setups, surface the first setup's windows
    # so the user doesn't lose what they had before the upgrade.
    if not base["windows"]:
        legacy_setups = bridge.get("setups") if isinstance(bridge.get("setups"), list) else []
        legacy_active = bridge.get("active_setup_id")
        chosen = next(
            (s for s in legacy_setups if isinstance(s, dict) and s.get("id") == legacy_active),
            None,
        ) or (legacy_setups[0] if legacy_setups else None)
        if chosen and isinstance(chosen.get("windows"), list):
            base["windows"] = [_normalize_window(w) for w in chosen["windows"]]
    vpub = bridge.get("vapid_public_key")
    vprv = bridge.get("vapid_private_key")
    if isinstance(vpub, str) and vpub.strip():
        base["vapid_public_key"] = vpub.strip()
    if isinstance(vprv, str) and vprv.strip():
        base["vapid_private_key"] = vprv.strip()
    raw_subs = bridge.get("push_subscriptions")
    if isinstance(raw_subs, list):
        clean: list[dict] = []
        for s in raw_subs:
            if not isinstance(s, dict):
                continue
            endpoint = s.get("endpoint")
            keys = s.get("keys")
            if not isinstance(endpoint, str) or not endpoint.strip():
                continue
            if not isinstance(keys, dict):
                continue
            p256 = keys.get("p256dh")
            auth = keys.get("auth")
            if not isinstance(p256, str) or not isinstance(auth, str):
                continue
            entry = {
                "id": (
                    s.get("id")
                    if isinstance(s.get("id"), str) and s.get("id").strip()
                    else uuid.uuid4().hex[:8]
                ),
                "endpoint": endpoint.strip(),
                "keys": {"p256dh": p256, "auth": auth},
                "label": s.get("label") if isinstance(s.get("label"), str) else None,
                "created_at": (
                    s.get("created_at")
                    if isinstance(s.get("created_at"), str)
                    else None
                ),
            }
            clean.append(entry)
        base["push_subscriptions"] = clean
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
        # Fresh install — populate from defaults/ if any are bundled,
        # then write the config so subsequent launches skip the seed
        # path. The seed is a no-op when defaults/manifest.json is
        # missing, which is the case in dev until pack_defaults runs.
        cfg = default_config()
        if seed_defaults_if_blank(cfg):
            save_config(cfg)
        cfg = normalize_config(cfg)
        _self_heal_template_bundles(cfg)
        return cfg
    try:
        loaded = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return default_config()
    cfg = normalize_config(loaded)
    _self_heal_template_bundles(cfg)
    return cfg


def _self_heal_template_bundles(cfg: dict) -> None:
    """Restore template base files that were orphaned by older
    Rename / Delete code paths. A missing base + surviving variants
    used to silently break detection (engine loaded an empty pack);
    now we copy a variant back over the base on load so matching
    works again without user intervention."""
    for rule in cfg.get("rules", []) or []:
        ref = rule.get("template_path")
        if not ref:
            continue
        p = resolve_template_path(ref)
        if p is None:
            continue
        restore_missing_template_base(p, rule.get("template_source_dpi"))
        regenerate_missing_variants(p, rule.get("template_source_dpi"))
    bridge = cfg.get("bridge") or {}
    for path_key, dpi_key in [
        ("idle_template_path", "idle_template_source_dpi"),
        ("askuser_template_path", "askuser_template_source_dpi"),
    ]:
        ref = bridge.get(path_key)
        if not ref:
            continue
        p = resolve_template_path(ref)
        if p is None:
            continue
        restore_missing_template_base(p, bridge.get(dpi_key))
        regenerate_missing_variants(p, bridge.get(dpi_key))


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


_DPI_VARIANT_RE = re.compile(r"\.dpi\d+$", re.IGNORECASE)


def is_dpi_variant_file(name: str) -> bool:
    """True if ``name`` looks like a DPI variant generated by
    ``write_template_with_dpi_variants`` (``foo.dpi100.png``,
    ``foo.dpi150.png``, …). Used to hide variants from the template
    picker — they're not canonical templates, they're scaled copies."""
    stem = Path(name).stem
    return bool(_DPI_VARIANT_RE.search(stem))


def list_template_files() -> list[str]:
    """Canonical template files in TEMPLATES_DIR. DPI variants
    (``<base>.dpi<n>.<ext>``) are hidden — they exist on disk so the
    matcher can pick the right one per monitor, but the user's
    template picker should only show the base files they actually
    captured."""
    ensure_templates_dir()
    exts = {".png", ".jpg", ".jpeg", ".bmp", ".webp"}
    files = [
        p.name
        for p in TEMPLATES_DIR.iterdir()
        if p.is_file()
        and p.suffix.lower() in exts
        and not is_dpi_variant_file(p.name)
    ]
    return sorted(files)


def write_template_with_dpi_variants(
    base_path: Path | str,
    rgb_array,
    source_scale: float,
    target_scales: tuple[float, ...] = SUPPORTED_DPI_SCALES,
) -> list[Path]:
    """Write ``rgb_array`` to ``base_path`` AND a sibling file per
    target DPI scale (``base.dpi<n>.png``). The base file is a
    bit-for-bit copy of the source variant so callers that don't
    know about DPI just see the original capture.

    Lazy-imports cv2/PIL so the press_store module stays usable in
    headless / test environments that don't have the imaging stack.
    Returns the list of files written, in the order they were
    written (base first, then variants ascending by scale)."""
    import numpy as np  # imported here so test envs without numpy still load this module
    from PIL import Image
    import cv2

    base = Path(base_path)
    base.parent.mkdir(parents=True, exist_ok=True)

    if rgb_array is None:
        raise ValueError("rgb_array required")
    arr = np.asarray(rgb_array)
    if arr.ndim != 3 or arr.shape[2] != 3:
        raise ValueError(f"expected HxWx3 RGB, got {arr.shape}")

    written: list[Path] = [base]
    Image.fromarray(arr, mode="RGB").save(base, format="PNG", optimize=False)

    src_h, src_w = arr.shape[:2]
    for target in sorted(target_scales):
        ratio = target / max(source_scale, 1e-6)
        new_w = max(1, int(round(src_w * ratio)))
        new_h = max(1, int(round(src_h * ratio)))
        if ratio < 1.0:
            interp = cv2.INTER_AREA  # downscale — averaging is sharpest
        elif abs(ratio - 1.0) < 1e-3:
            # Same size as source; skip the resize round-trip and
            # write the original bytes again so the variant exists.
            new_w, new_h = src_w, src_h
            interp = None
        else:
            interp = cv2.INTER_CUBIC  # upscale — cubic looks the cleanest
        if interp is None:
            resized = arr
        else:
            resized = cv2.resize(arr, (new_w, new_h), interpolation=interp)
        variant_path = dpi_variant_path(base, target)
        Image.fromarray(resized, mode="RGB").save(variant_path, format="PNG", optimize=False)
        written.append(variant_path)
    return written


# ---- Bundled defaults: first-run seed + pack round-trip -------------------


def _read_defaults_manifest() -> dict | None:
    """Load defaults/manifest.json if present. Returns None when the
    bundle is missing or unreadable — seed treats that as 'no defaults
    available' and silently no-ops."""
    if not DEFAULTS_MANIFEST_PATH.exists():
        return None
    try:
        return json.loads(DEFAULTS_MANIFEST_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def iter_template_bundle(base_path: Path) -> list[Path]:
    """Every file that belongs to a template's bundle: the base PNG
    plus any ``<base>.dpi<n>.<ext>`` variants sitting next to it.
    Order: base first, then variants in filesystem order. Files that
    don't exist are skipped — caller gets only what's actually on
    disk."""
    base = Path(base_path)
    out: list[Path] = []
    if base.exists():
        out.append(base)
    if not base.parent.exists():
        return out
    stem = base.stem
    suffix = base.suffix.lower()
    for sibling in base.parent.iterdir():
        if not sibling.is_file() or sibling == base:
            continue
        if not sibling.name.startswith(stem + ".dpi"):
            continue
        if sibling.suffix.lower() != suffix:
            continue
        out.append(sibling)
    return out


def rename_template_bundle(old_base: Path, new_base: Path) -> int:
    """Rename a template's full bundle — the base PNG plus every
    DPI variant — atomically (best-effort). Returns the count of
    files renamed. Used by the desktop Rename Template button so a
    rename doesn't orphan variants."""
    old_base = Path(old_base)
    new_base = Path(new_base)
    if not old_base.exists() and not any(
        f for f in iter_template_bundle(old_base) if f != old_base
    ):
        return 0
    new_base.parent.mkdir(parents=True, exist_ok=True)
    old_stem = old_base.stem
    new_stem = new_base.stem
    suffix = new_base.suffix or old_base.suffix
    renamed = 0
    # Snapshot the bundle BEFORE we start moving, so a rename that
    # would shadow another sibling can't race itself.
    bundle = iter_template_bundle(old_base)
    for src in bundle:
        # base file → new base
        if src == old_base:
            dst = new_base
        else:
            # variant: replace stem, keep .dpi<n>.<ext> tail
            tail = src.name[len(old_stem):]  # ".dpi150.png"
            dst = new_base.with_name(new_stem + tail)
        if dst.exists() and dst != src:
            continue  # don't clobber a same-named target
        src.rename(dst)
        renamed += 1
    return renamed


def delete_template_bundle(base_path: Path) -> int:
    """Delete a template's full bundle. Returns the count of files
    removed. Mirrors ``rename_template_bundle`` so the desktop
    Delete button doesn't leave orphans."""
    removed = 0
    for f in iter_template_bundle(Path(base_path)):
        try:
            f.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def regenerate_missing_variants(base_path: Path, source_dpi: float | None) -> int:
    """If the template's base PNG is present and ``source_dpi`` is
    known, generate any DPI variants that aren't already on disk by
    resizing the base. Used by the self-heal to recover from a
    rename that left variants orphaned under the old name. Returns
    the count of variants written (0 if nothing to do)."""
    if source_dpi is None:
        return 0
    base = Path(base_path)
    if not base.exists():
        return 0
    missing = [
        s for s in SUPPORTED_DPI_SCALES
        if not dpi_variant_path(base, s).exists()
    ]
    if not missing:
        return 0
    try:
        import numpy as np
        from PIL import Image
    except Exception:
        return 0
    try:
        arr = np.array(Image.open(base).convert("RGB"))
    except Exception:
        return 0
    try:
        # Reuse the same writer that capture uses — it handles the
        # base + every preset variant in one shot. Pass only the
        # missing scales as targets so we don't rewrite existing
        # variants and bump their mtimes.
        write_template_with_dpi_variants(
            base, arr, float(source_dpi), target_scales=tuple(missing)
        )
        return len(missing)
    except Exception:
        return 0


def restore_missing_template_base(base_path: Path, source_dpi: float | None) -> bool:
    """If a template's base PNG is missing but its source-DPI variant
    exists, copy the variant back as the base. Used as a self-heal on
    load_config: previous Rename / Delete bugs orphaned variants,
    leaving the base file gone — the matcher then loaded nothing and
    detection silently stopped working.

    Returns True if the base was restored, False if it was already
    present or no usable variant exists."""
    import shutil

    base = Path(base_path)
    if base.exists():
        return False
    if source_dpi is None:
        # No DPI metadata — pick any variant we can find as a last
        # resort, so the matcher at least has SOMETHING to compare
        # against until the user re-captures.
        candidates = iter_template_bundle(base)
        if not candidates:
            return False
        src = candidates[0]
    else:
        src = dpi_variant_path(base, float(source_dpi))
        if not src.exists():
            # source_dpi variant missing too — fall back to any
            # variant. Better matching at the wrong scale than no
            # matching at all.
            candidates = iter_template_bundle(base)
            if not candidates:
                return False
            src = candidates[0]
    try:
        shutil.copy2(src, base)
        return True
    except OSError:
        return False


def _copy_template_bundle(src_base: Path, dst_base: Path) -> int:
    """Copy ``src_base`` plus every DPI variant sitting next to it
    (``<base>.dpi<n>.<ext>``) into ``dst_base`` and its siblings.
    Used by the seed and the pack tool.

    Tolerates a missing base: if only variants exist, the first
    variant found is also copied as the destination base so the
    bundle stays usable (matching engine needs the base for the
    legacy / no-DPI-metadata path). Returns the file count copied;
    0 if neither base nor variants exist."""
    import shutil

    dst_base.parent.mkdir(parents=True, exist_ok=True)
    written = 0
    base_written = False
    src_stem = src_base.stem
    src_suffix = src_base.suffix
    if src_base.exists():
        shutil.copy2(src_base, dst_base)
        written += 1
        base_written = True
    if not src_base.parent.exists():
        return written
    variants_seen: list[Path] = []
    for sibling in sorted(src_base.parent.iterdir()):
        if not sibling.is_file() or sibling == src_base:
            continue
        if not sibling.name.startswith(src_stem + ".dpi"):
            continue
        if sibling.suffix.lower() != src_suffix.lower():
            continue
        variants_seen.append(sibling)
        variant_suffix = sibling.name[len(src_stem):]  # ".dpi100.png"
        shutil.copy2(sibling, dst_base.with_name(dst_base.stem + variant_suffix))
        written += 1
    # Heal: if the source base was missing but variants existed,
    # also drop a copy at the destination base so the engine's
    # bundle loader doesn't trip on the missing base in the dst.
    if not base_written and variants_seen:
        shutil.copy2(variants_seen[0], dst_base)
        written += 1
    return written


def seed_defaults_if_blank(cfg: dict) -> bool:
    """If ``cfg`` has empty rules / bridge templates AND defaults are
    bundled with the app, populate the empty slots from defaults/.

    Mutates ``cfg`` in place. Returns True if anything was seeded.
    Safe to call multiple times — every check is per-slot, so partial
    seeds (rules present but bridge templates blank) are filled too.
    Missing PNGs in defaults/ skip the corresponding entries silently
    so a partial bundle still produces a working subset."""
    manifest = _read_defaults_manifest()
    if not manifest:
        return False
    seeded = False
    ensure_templates_dir()
    bridge = cfg.setdefault("bridge", default_bridge_config())

    if not cfg.get("rules"):
        for entry in manifest.get("rules", []) or []:
            matcher = entry.get("matcher")
            rule = default_rule(entry.get("name") or "New Rule")
            rule["enabled"] = bool(entry.get("enabled", True))
            rule["action"] = (
                entry["action"] if entry.get("action") in ACTION_TYPES else ACTION_CLICK
            )
            if isinstance(entry.get("text"), str):
                rule["text"] = entry["text"]
            rule["threshold"] = _clamp_float(
                entry.get("threshold"), 0.90, 0.0, 1.0
            )
            if matcher == MATCHER_COLOR:
                # Color rules don't need a template file — just the
                # RGB + capture area carry forward from the manifest.
                rgb = entry.get("color_rgb")
                if not _valid_rgb(rgb):
                    continue
                rule["matcher"] = MATCHER_COLOR
                rule["color_rgb"] = [int(c) for c in rgb]
                rule["color_name"] = entry.get("color_name") or ""
                try:
                    rule["color_capture_area"] = max(
                        0, int(entry.get("color_capture_area") or 0)
                    )
                except (TypeError, ValueError):
                    rule["color_capture_area"] = 0
                cfg.setdefault("rules", []).append(rule)
                seeded = True
                continue
            # Template rule.
            filename = entry.get("template_filename")
            if not filename:
                continue
            src = DEFAULTS_DIR / filename
            dst = TEMPLATES_DIR / filename
            if _copy_template_bundle(src, dst) == 0:
                continue  # bundle missing the actual PNG — skip
            rule["matcher"] = MATCHER_TEMPLATE
            rule["template_path"] = filename
            rule["template_source_dpi"] = _normalize_dpi(
                entry.get("template_source_dpi")
            )
            cfg.setdefault("rules", []).append(rule)
            seeded = True

    bridge_manifest = manifest.get("bridge") or {}
    if not bridge.get("idle_template_path"):
        filename = bridge_manifest.get("idle_template_filename")
        if filename:
            src = DEFAULTS_DIR / filename
            dst = TEMPLATES_DIR / filename
            if _copy_template_bundle(src, dst) > 0:
                bridge["idle_template_path"] = filename
                bridge["idle_template_source_dpi"] = _normalize_dpi(
                    bridge_manifest.get("idle_template_source_dpi")
                )
                seeded = True
    if not bridge.get("askuser_template_path"):
        filename = bridge_manifest.get("askuser_template_filename")
        if filename:
            src = DEFAULTS_DIR / filename
            dst = TEMPLATES_DIR / filename
            if _copy_template_bundle(src, dst) > 0:
                bridge["askuser_template_path"] = filename
                bridge["askuser_template_source_dpi"] = _normalize_dpi(
                    bridge_manifest.get("askuser_template_source_dpi")
                )
                seeded = True
    return seeded


def make_rule_summary(rule: dict, last_score: float | None = None) -> str:
    enabled = "on" if rule.get("enabled") else "off"
    scope = "screen" if not rule.get("search_region") else "region"
    action = rule.get("action", ACTION_CLICK)
    score = "-" if last_score is None else f"{last_score:.3f}"
    return f"{rule.get('priority', '?')}. {rule.get('name', 'Rule')} [{enabled}] {action} {scope} score={score}"


# ---- HWND-keyed merge for dynamic auto-detect -----------------------------
#
# Auto-detect runs on bridge start, manual button, and workspace change. The
# merge below preserves user-edited fields (renamed names, chat_target
# overrides) for windows whose HWND is still alive, while pulling in fresh
# regions from GetWindowRect and dropping HWNDs that disappeared.


def merge_detected_windows(
    existing: list[dict],
    detected: list[dict],
) -> list[dict]:
    """Reconcile the current ``bridge.windows`` list with a fresh
    enumeration. ``detected`` is the output of
    ``press_windows.list_cursor_windows()`` — each entry has hwnd,
    region (physical pixels), and a short label.

    For each detected HWND:
      * If we already had a window with this hwnd, preserve it but
        update region. Keep the user's name + chat_target.
      * If new, create a fresh window dict from the detected info.
    Existing windows whose hwnd is no longer in ``detected`` are
    dropped — they're not on the foreground workspace anymore (or
    their Cursor instance closed).

    Returns a new list; doesn't mutate the inputs. Order follows the
    ``detected`` list so the table reflects the OS's z-order /
    enumeration order, not whatever stale order the config carried.
    """
    by_hwnd: dict[int, dict] = {}
    for w in existing or []:
        h = w.get("hwnd")
        try:
            h_int = int(h) if h is not None else None
        except (TypeError, ValueError):
            h_int = None
        if h_int is not None:
            by_hwnd[h_int] = w
    out: list[dict] = []
    for d in detected or []:
        try:
            h_int = int(d.get("hwnd"))
        except (TypeError, ValueError):
            continue
        region = d.get("region")
        if not _valid_region(list(region) if region else None):
            continue
        prior = by_hwnd.get(h_int)
        if prior is not None:
            # Preserve user-edited fields, refresh region from the
            # current enumeration.
            updated = dict(prior)
            updated["hwnd"] = h_int
            updated["region"] = [int(v) for v in region]
            # Re-derive name when the stored value still carries a
            # " - " separator: that's the signature of a stale
            # auto-derived title from an older trim (or a fresh
            # title that's grown since first detection). Names
            # without " - " are treated as manual renames and
            # preserved as-is.
            detected_short = d.get("name")
            if " - " in (updated.get("name") or "") and detected_short:
                updated["name"] = detected_short
            out.append(updated)
        else:
            entry = default_bridge_window(d.get("name", "Cursor"))
            entry["hwnd"] = h_int
            entry["region"] = [int(v) for v in region]
            out.append(entry)
    return out
