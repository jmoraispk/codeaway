"""Rule-based screen scanning engine."""

from __future__ import annotations

import sys
import time

from PIL import ImageGrab

from press_core import MODE_CLICK, MODE_CLICK_ENTER, do_action, load_template_gray, try_import_vision
from press_store import (
    ACTION_CLICK,
    ACTION_CLICK_TYPE_ENTER,
    MATCHER_COLOR,
    MATCHER_TEMPLATE,
    resolve_template_path,
)


ACTION_SETTLE_DELAY_SEC = 0.20

# A color rule clicks every contiguous patch of the captured RGB whose
# pixel area is at least this fraction of the originally dragged region.
# Self-calibrates: drag the whole button -> noisy small accents fail this.
COLOR_AREA_RATIO = 0.5
# Cap the number of clicks per tick so a freak case (target colour appears
# in dozens of places) doesn't lock the cursor for minutes.
COLOR_MAX_CLICKS = 5


def ensure_vision() -> tuple[object, object]:
    cv2, np, err = try_import_vision()
    if err:
        raise RuntimeError("Vision deps missing. Install with: uv sync")
    return cv2, np


def _pin_thread_v2_dpi() -> None:
    """Ensure the current thread is PER_MONITOR_AWARE_V2.

    Windows inherits thread DPI context from the process default at thread
    creation. If a background thread was started before Qt/main set V2,
    GetSystemMetrics and ImageGrab disagree with V2-aware callers. Pinning V2
    here is idempotent and cheap; it guarantees every capture path agrees on
    physical pixels.
    """
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        pass


def _virtual_screen_origin() -> tuple[int, int]:
    """Top-left screen coordinate of the virtual desktop (can be negative on Windows)."""
    if sys.platform.startswith("win"):
        import ctypes

        _pin_thread_v2_dpi()
        gm = ctypes.windll.user32.GetSystemMetrics
        return gm(76), gm(77)  # SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN
    return (0, 0)


def _grab_screen(region: tuple[int, int, int, int] | None):
    _pin_thread_v2_dpi()
    bbox = None
    if region:
        left, top, width, height = region
        bbox = (left, top, left + width, top + height)
    try:
        return ImageGrab.grab(bbox=bbox, all_screens=True)
    except TypeError:
        return ImageGrab.grab(bbox=bbox)


def capture_screen_rgb(region: tuple[int, int, int, int] | None = None):
    """Full screen / region as an HxWx3 RGB ndarray (uint8). Same coord rules as gray."""
    _cv2, np = ensure_vision()
    return np.array(_grab_screen(region).convert("RGB"))


def capture_screen_gray(region: tuple[int, int, int, int] | None = None):
    cv2, _np = ensure_vision()
    return cv2.cvtColor(capture_screen_rgb(region), cv2.COLOR_RGB2GRAY)


def dominant_rgb(rgb_array) -> tuple[int, int, int]:
    """Return the most-frequent (r, g, b) triple in an HxWx3 RGB array.

    Mode rather than mean: the centre of a button is one solid colour while
    anti-aliased edges smear into many unique colours. The mean of that mix
    is a colour that often does not exist anywhere, which makes an exact
    match scan pointless.
    """
    _cv2, np = ensure_vision()
    flat = rgb_array.reshape(-1, 3)
    if flat.size == 0:
        return (0, 0, 0)
    unique, counts = np.unique(flat, axis=0, return_counts=True)
    r, g, b = unique[counts.argmax()]
    return int(r), int(g), int(b)


def _load_dpi_variant_pack(base_path, source_dpi):
    """Load the base template plus every DPI variant sitting next to
    it on disk. Returns ``{"base": gray, "variants": {scale: gray},
    "source_dpi": float|None}``. The matcher picks a variant by the
    target screen's scale; the base is the fallback when no variant
    matches (or when source_dpi is None for legacy captures)."""
    from press_store import SUPPORTED_DPI_SCALES, dpi_variant_path

    base_gray = load_template_gray(str(base_path))
    pack = {"base": base_gray, "variants": {}, "source_dpi": None}
    if source_dpi is None:
        return pack
    try:
        src_scale = float(source_dpi)
    except (TypeError, ValueError):
        return pack
    pack["source_dpi"] = src_scale
    pack["variants"][src_scale] = base_gray
    for scale in SUPPORTED_DPI_SCALES:
        if scale in pack["variants"]:
            continue
        vpath = dpi_variant_path(base_path, scale)
        if not vpath.exists():
            continue
        try:
            pack["variants"][scale] = load_template_gray(str(vpath))
        except Exception:
            continue
    return pack


def _pick_template_from_pack(pack: dict, target_scale: float):
    """Closest-by-distance pick from a variant pack. Falls through
    to ``pack['base']`` when no variants are loaded (legacy capture)."""
    variants = pack.get("variants") or {}
    if not variants:
        return pack["base"]
    if target_scale in variants:
        return variants[target_scale]
    keys = list(variants.keys())
    best = min(keys, key=lambda s: abs(s - target_scale))
    return variants[best]


def build_runtime_rules(config: dict) -> list[dict]:
    runtime_rules: list[dict] = []
    for rule in sorted(config.get("rules", []), key=lambda item: int(item.get("priority", 9999))):
        if not rule.get("enabled"):
            continue
        matcher = rule.get("matcher", MATCHER_TEMPLATE)
        if matcher == MATCHER_COLOR:
            color = rule.get("color_rgb")
            area = int(rule.get("color_capture_area") or 0)
            if not color or area <= 0:
                continue
            runtime_rules.append({**rule})
            continue
        # template path
        template_path = resolve_template_path(rule.get("template_path"))
        if template_path is None or not template_path.exists():
            continue
        try:
            pack = _load_dpi_variant_pack(
                template_path, rule.get("template_source_dpi")
            )
        except Exception:
            continue
        runtime_rules.append(
            {
                **rule,
                # ``template_gray`` stays so existing call sites
                # (find_rule_matches default + legacy tests) keep
                # working unchanged; ``template_pack`` is the new
                # DPI-aware structure the matcher prefers when a
                # search_region is set.
                "template_gray": pack["base"],
                "template_pack": pack,
            }
        )
    return runtime_rules


def _find_matches_in(search_gray, template_gray, threshold: float, offset_x: int, offset_y: int) -> list[tuple[float, tuple[int, int]]]:
    cv2, np = ensure_vision()
    if search_gray is None or search_gray.size == 0:
        return []
    template_h, template_w = template_gray.shape[:2]
    if search_gray.shape[0] < template_h or search_gray.shape[1] < template_w:
        return []
    result = cv2.matchTemplate(search_gray, template_gray, cv2.TM_CCOEFF_NORMED)
    ys, xs = np.where(result >= threshold)
    if len(xs) == 0:
        return []

    candidates = sorted(
        [(float(result[y, x]), int(x), int(y)) for x, y in zip(xs.tolist(), ys.tolist())],
        key=lambda item: item[0],
        reverse=True,
    )
    matches: list[tuple[float, tuple[int, int]]] = []
    for score, x, y in candidates:
        abs_center = (offset_x + x + (template_w // 2), offset_y + y + (template_h // 2))
        if any(
            abs(abs_center[0] - chosen[1][0]) < template_w
            and abs(abs_center[1] - chosen[1][1]) < template_h
            for chosen in matches
        ):
            continue
        matches.append((score, abs_center))
    return matches


def _find_color_matches(
    search_rgb,
    target_rgb: tuple[int, int, int],
    capture_area: int,
    offset_x: int,
    offset_y: int,
) -> list[tuple[float, tuple[int, int]]]:
    """Click every contiguous run of pixels at exactly target_rgb that's at least
    half the size of the originally captured region. Returns up to N matches in
    descending area order; score is a synthetic 1.0 (color matches don't have a
    correlation score).
    """
    cv2, _np = ensure_vision()
    if search_rgb is None or search_rgb.size == 0:
        return []
    # cv2.inRange wants Scalar bounds (a 3-tuple), not a (3,) ndarray; passing
    # the latter trips a "sizes do not match" error on some OpenCV builds.
    bound = (int(target_rgb[0]), int(target_rgb[1]), int(target_rgb[2]))
    mask = cv2.inRange(search_rgb, bound, bound)  # 255 where exact match
    if not mask.any():
        return []
    min_area = max(1, int(capture_area * COLOR_AREA_RATIO))
    num_labels, _labels, stats, centroids = cv2.connectedComponentsWithStats(mask, connectivity=8)
    survivors: list[tuple[int, tuple[int, int]]] = []
    for i in range(1, num_labels):  # 0 is the background label
        area = int(stats[i, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        cx, cy = centroids[i]
        survivors.append((area, (offset_x + int(round(cx)), offset_y + int(round(cy)))))
    survivors.sort(key=lambda item: item[0], reverse=True)
    return [(1.0, center) for _, center in survivors[:COLOR_MAX_CLICKS]]


def _is_dpi_aware_pack(pack) -> bool:
    """A pack only deserves the per-monitor path if it actually has
    DPI metadata. Legacy captures (no source_dpi, empty variants)
    should keep matching against the cached virtual-screen frame."""
    return bool(pack) and pack.get("source_dpi") is not None


def _find_template_matches_per_monitor(
    runtime_rule: dict,
    monitor_cache: dict | None = None,
) -> list[tuple[float, tuple[int, int]]]:
    """DPI-aware template matching across all attached monitors.

    For each monitor: pick the variant matching that monitor's scale,
    capture the monitor's region, run matchTemplate. Aggregate
    matches across monitors and sort by score desc. ``monitor_cache``
    (when provided) is a dict ``{monitor_key: gray_frame}`` shared
    across rules in the same tick so we only capture each monitor
    once per evaluate_rules pass — same trick as the legacy
    ``shared_gray`` cache, just keyed per monitor."""
    import press_dpi as _dpi

    pack = runtime_rule["template_pack"]
    threshold = float(runtime_rule.get("threshold", 0.90))
    monitors = _dpi.all_monitor_infos()
    if not monitors:
        # No DPI service available — fall back to the legacy path so
        # callers still get some answer. _find_matches_in handles a
        # missing template gracefully via the size guard.
        return _find_matches_in(
            capture_screen_gray(),
            runtime_rule["template_gray"],
            threshold,
            *_virtual_screen_origin(),
        )
    all_matches: list[tuple[float, tuple[int, int]]] = []
    for info in monitors:
        rect = info["rect"]
        key = info["key"]
        gray = None
        if monitor_cache is not None and key in monitor_cache:
            gray = monitor_cache[key]
        if gray is None:
            try:
                gray = capture_screen_gray(rect)
            except Exception:
                continue
            if monitor_cache is not None:
                monitor_cache[key] = gray
        tpl = _pick_template_from_pack(pack, info["scale"])
        if tpl is None:
            continue
        all_matches.extend(
            _find_matches_in(gray, tpl, threshold, rect[0], rect[1])
        )
    all_matches.sort(key=lambda m: m[0], reverse=True)
    return all_matches


def find_rule_matches(frame, runtime_rule: dict) -> list[tuple[float, tuple[int, int]]]:
    """Evaluate one rule.

    `frame` is the cached full-virtual-screen capture: gray for template
    rules, RGB for color rules. If the rule carries its own ``search_region``
    that region is re-captured so screen-coord slicing stays correct on
    monitors at negative virtual-screen coordinates.
    """
    region = runtime_rule.get("search_region")
    matcher = runtime_rule.get("matcher", MATCHER_TEMPLATE)

    if matcher == MATCHER_COLOR:
        if region:
            search_rgb = capture_screen_rgb(region)
            offset_x, offset_y = int(region[0]), int(region[1])
        else:
            if frame is None:
                frame = capture_screen_rgb()
            search_rgb = frame
            offset_x, offset_y = _virtual_screen_origin()
        return _find_color_matches(
            search_rgb,
            tuple(runtime_rule["color_rgb"]),
            int(runtime_rule.get("color_capture_area") or 0),
            offset_x,
            offset_y,
        )

    # template
    pack = runtime_rule.get("template_pack")
    if region:
        search_gray = capture_screen_gray(region)
        offset_x, offset_y = int(region[0]), int(region[1])
        # Pick the variant matching the region's monitor — DPI is
        # unambiguous when the search is bounded to one monitor.
        if pack:
            import press_dpi as _dpi

            scale = _dpi.scale_for_region(region)
            template_gray = _pick_template_from_pack(pack, scale)
        else:
            template_gray = runtime_rule["template_gray"]
        return _find_matches_in(
            search_gray,
            template_gray,
            float(runtime_rule.get("threshold", 0.90)),
            offset_x,
            offset_y,
        )

    # No search_region — DPI-aware path iterates monitors so a button
    # captured on screen A at 150 % still matches on screen B at 100 %.
    # Legacy captures (no source_dpi) keep the single-virtual-screen
    # match against ``frame`` for back-compat.
    if _is_dpi_aware_pack(pack):
        return _find_template_matches_per_monitor(runtime_rule)
    if frame is None:
        frame = capture_screen_gray()
    return _find_matches_in(
        frame,
        runtime_rule["template_gray"],
        float(runtime_rule.get("threshold", 0.90)),
        *_virtual_screen_origin(),
    )


def evaluate_rule_on_frame(frame_gray, runtime_rule: dict) -> tuple[float, tuple[int, int] | None]:
    matches = find_rule_matches(frame_gray, runtime_rule)
    if not matches:
        return 0.0, None
    return matches[0]


def evaluate_rules(
    runtime_rules: list[dict],
    windows: list[dict] | None = None,
) -> tuple[list[dict], list[dict]]:
    """Run every enabled rule against the current screen, return
    (results, actions). ``windows`` is the bridge's tracked-window
    list (passed straight through from cfg); needed only when a rule
    has a non-empty ``window_scope``, in which case the rule fires
    once per matching window with that window's region used as
    search_region (the regular per-rule search_region is ignored when
    a scope is set)."""
    if not runtime_rules:
        return [], []
    # Only pay for the full-virtual-screen grab if at least one rule needs it.
    # Color rules want RGB, template rules (legacy) want gray; DPI-aware
    # template rules want one capture per monitor — that cache is keyed
    # by monitor rect and shared across rules in this tick.
    shared_rgb = None
    shared_gray = None
    shared_per_monitor: dict = {}
    results: list[dict] = []
    actions: list[dict] = []

    # Build a name→region lookup once so the per-rule scope loop is
    # cheap. Skips windows without a region (which can't be searched).
    windows_by_name: dict[str, list[list]] = {}
    if windows:
        for w in windows:
            name = w.get("name")
            region = w.get("region")
            if not isinstance(name, str) or not region:
                continue
            windows_by_name.setdefault(name, []).append(list(region))

    for rule in runtime_rules:
        matcher = rule.get("matcher", MATCHER_TEMPLATE)
        pack = rule.get("template_pack")
        scope = rule.get("window_scope") or []
        if scope:
            # Per-window scoping: rule fires once per tracked window
            # whose name is in the scope. The scoped window's region
            # becomes the search_region for that iteration. Windows
            # in the scope that aren't currently tracked are silently
            # skipped (matches the "Silently skip" choice — rule
            # reactivates when the named window reappears).
            scoped_matches: list = []
            for name in scope:
                for region in windows_by_name.get(name, []):
                    # Shallow copy of the rule with search_region
                    # overridden; preserves template_pack + threshold
                    # etc. Note: template_pack DPI variant lookup
                    # uses the search_region's monitor (see
                    # find_rule_matches), so multi-monitor scopes
                    # still pick the right variant.
                    scoped_rule = {**rule, "search_region": region}
                    scoped_matches.extend(find_rule_matches(None, scoped_rule))
            # Sort by score desc so the action loop fires the
            # strongest match first when there are several.
            scoped_matches.sort(key=lambda m: m[0], reverse=True)
            matches = scoped_matches
        elif rule.get("search_region"):
            # Rule has its own region — find_rule_matches captures it.
            matches = find_rule_matches(None, rule)
        elif matcher == MATCHER_COLOR:
            if shared_rgb is None:
                shared_rgb = capture_screen_rgb()
            matches = find_rule_matches(shared_rgb, rule)
        elif _is_dpi_aware_pack(pack):
            # DPI-aware template rules go through the per-monitor
            # pipeline. They re-use ``shared_per_monitor`` so multiple
            # such rules in a single tick only pay for K monitor
            # captures (not N rules × K monitors).
            matches = _find_template_matches_per_monitor(rule, shared_per_monitor)
        else:
            # Legacy template rule with no DPI metadata.
            if shared_gray is None:
                shared_gray = capture_screen_gray()
            matches = find_rule_matches(shared_gray, rule)
        best_score = matches[0][0] if matches else 0.0
        results.append(
            {
                "id": rule["id"],
                "name": rule["name"],
                "score": best_score,
                "matched": bool(matches),
                "match_count": len(matches),
                "centers": [center for _, center in matches],
                "action": rule.get("action", ACTION_CLICK),
                "text": rule.get("text", "continue"),
            }
        )
        for score, center in matches:
            actions.append(
                {
                    "id": rule["id"],
                    "name": rule["name"],
                    "score": score,
                    "center": center,
                    "action": rule.get("action", ACTION_CLICK),
                    "text": rule.get("text", "continue"),
                }
            )
    return results, actions


def execute_match(match: dict, refocus_mode: str | None = None) -> None:
    center = match.get("center")
    if center is None:
        return
    if match.get("action") == ACTION_CLICK_TYPE_ENTER:
        do_action(
            MODE_CLICK_ENTER,
            center,
            text_before_enter=match.get("text") or "continue",
            refocus_mode=refocus_mode,
        )
    else:
        do_action(MODE_CLICK, center, refocus_mode=refocus_mode)


def execute_matches(
    matches: list[dict],
    delay_seconds: float = ACTION_SETTLE_DELAY_SEC,
    refocus_mode: str | None = None,
) -> None:
    # If multiple matches fire in one tick, the user only wants ONE
    # refocus event at the very end — not one per match. Per-match
    # refocus would fire three times for three buttons, which both
    # wastes events and risks repeatedly punching focus into a
    # window that's now in a different state.
    last_idx = len(matches) - 1
    for idx, match in enumerate(matches):
        is_last = idx == last_idx
        execute_match(
            match,
            refocus_mode=refocus_mode if is_last else None,
        )
        if not is_last and delay_seconds > 0:
            time.sleep(delay_seconds)


# ---- bridge: per-window idle detection ---------------------------------


def bridge_has_runnable_targets(bridge_cfg: dict) -> bool:
    """Whether the bridge has windows that can be evaluated this tick."""
    windows = bridge_cfg.get("windows") or []
    return bool(windows) and (
        any(window.get("backend") == "codex_desktop" for window in windows)
        or bool(bridge_cfg.get("idle_template_path"))
    )


def evaluate_bridge_windows(bridge_cfg: dict, capture_rgb: bool = False) -> list[dict]:
    """Scan each configured Cursor window for the bridge's idle template
    and (optionally) the askuser template.

    Returns one dict per window:
        {id, name, idle, asking, score, configured}

    When ``capture_rgb`` is True an extra ``rgb`` key carries the captured
    HxWx3 numpy array for that window's region — the worker uses this to
    feed the bridge's snapshot ring buffer without a second screen grab.

    Semantics:
      - **idle**   = idle_template found in the region above idle_threshold.
      - **asking** = askuser_template found in the region above
                     idle_threshold (re-using the same threshold for now —
                     they're effectively the same kind of UI marker).
      - The two can coexist (the idle "ready for input" indicator may
        still be visible while a multiple-choice card is showing). Callers
        decide precedence — typically asking > idle.

    ``configured`` is False if the window has no region set yet.
    """
    windows = bridge_cfg.get("windows") or []
    if not windows:
        return []

    # Backends own their own readiness semantics and need no legacy idle
    # template. Evaluate them before entering the template pipeline, which
    # keeps a Codex-only bridge alive even before a Cursor template exists.
    from press_backends import backend_for_id

    codex_windows = [
        window for window in windows if window.get("backend") == "codex_desktop"
    ]
    legacy_windows = [
        window for window in windows if window.get("backend") != "codex_desktop"
    ]
    codex_results: list[dict] = []
    for window in codex_windows:
        backend = backend_for_id(window.get("backend"))
        if backend is None:
            continue
        state = {
            "id": window.get("id"),
            "name": window.get("name", backend.label),
            "idle": False,
            "asking": False,
            "score": 0.0,
            "configured": False,
            "detected_agent": backend.id,
            "backend": backend.id,
            "ready_count": 0,
        }
        region = window.get("region")
        if not region or len(region) != 4:
            codex_results.append(state)
            continue
        state["configured"] = True
        try:
            rgb = capture_screen_rgb(tuple(int(value) for value in region))
            evaluation = backend.evaluate(rgb)
        except Exception:
            codex_results.append(state)
            continue
        state.update(
            {
                "idle": evaluation.ready,
                "asking": evaluation.asking,
                "score": evaluation.score,
                "ready_count": max(0, int(evaluation.ready_count)),
            }
        )
        if capture_rgb:
            state["rgb"] = rgb
        codex_results.append(state)

    idle_ref = bridge_cfg.get("idle_template_path")
    if not legacy_windows or not idle_ref:
        return codex_results

    import press_dpi
    from press_store import SUPPORTED_DPI_SCALES, dpi_variant_path

    askuser_ref = bridge_cfg.get("askuser_template_path")
    idle_src_dpi = bridge_cfg.get("idle_template_source_dpi")
    askuser_src_dpi = bridge_cfg.get("askuser_template_source_dpi")
    threshold = float(bridge_cfg.get("idle_threshold", 0.90))
    windows = legacy_windows

    # ---- per-monitor matching pipeline ----
    # Old shape was N captures + N×M matches (N windows, M templates).
    # New shape: K captures + K×M matches (K = distinct monitors hosting
    # at least one configured window). For a typical 4-window setup on
    # one screen, that's 1 capture + 2 matches per tick instead of 4 + 8.
    # The trade-off is matchTemplate runs on a bigger search image
    # (whole monitor vs window region); cv2's matchTemplate is fast
    # enough on a 1080p screen that the saving on captures dominates.

    def _load_variants(base_ref, source_dpi):
        """Load every DPI variant available for a template. Returns
        ``{scale: gray_image}`` keyed by scale factor. Legacy
        captures (no source_dpi) just return ``{None: base_gray}``,
        which the picker treats as a one-size-fits-all fallback."""
        base = resolve_template_path(base_ref)
        if base is None or not base.exists():
            return {}
        try:
            base_gray = load_template_gray(str(base))
        except Exception:
            return {}
        variants: dict = {}
        if source_dpi is None:
            # No DPI metadata — keep one entry under the sentinel key so
            # `_pick_variant` falls back to it on every window.
            variants[None] = base_gray
            return variants
        try:
            variants[float(source_dpi)] = base_gray
        except (TypeError, ValueError):
            variants[None] = base_gray
            return variants
        for scale in SUPPORTED_DPI_SCALES:
            if scale in variants:
                continue
            vpath = dpi_variant_path(base, scale)
            if not vpath.exists():
                continue
            try:
                variants[scale] = load_template_gray(str(vpath))
            except Exception:
                continue
        return variants

    def _pick_variant(variants, target_scale):
        """Closest-match by absolute scale distance. Returns the
        legacy fallback (``variants[None]``) when no DPI metadata is
        available — preserves the original behaviour for users who
        haven't re-captured yet."""
        if not variants:
            return None
        if None in variants and len(variants) == 1:
            return variants[None]
        keys = [k for k in variants.keys() if k is not None]
        if target_scale in keys:
            return variants[target_scale]
        if not keys:
            return variants.get(None)
        best = min(keys, key=lambda s: abs(s - target_scale))
        return variants[best]

    idle_variants = _load_variants(idle_ref, idle_src_dpi)
    if not idle_variants:
        return codex_results

    # Askuser template is optional — if it's missing or unloadable, the
    # detector silently degrades to "asking is always False" and the
    # idle / busy behaviour is unchanged.
    askuser_variants = {}
    if isinstance(askuser_ref, str) and askuser_ref.strip():
        askuser_variants = _load_variants(askuser_ref, askuser_src_dpi)

    # Agent-pack detection templates. Iterating ``iter_packs()``
    # keeps the registry source-of-truth in press_packs; missing /
    # uncaptured slots silently degrade to "no detected agent".
    # Threshold reuses the bridge idle threshold for now — these
    # markers are visually similar in spirit (small, distinctive
    # UI cues), and a single knob keeps the configuration surface
    # tight while we validate the approach.
    from press_packs import PATH_KEY_FOR_ROLE, ROLE_DETECTION, iter_packs, DPI_KEY_FOR_ROLE

    packs_cfg = bridge_cfg.get("packs") or {}
    pack_variants: list[tuple[str, dict]] = []
    for pack in iter_packs():
        entry = packs_cfg.get(pack.id) or {}
        path = entry.get(PATH_KEY_FOR_ROLE[ROLE_DETECTION])
        if not path:
            continue
        variants = _load_variants(path, entry.get(DPI_KEY_FOR_ROLE[ROLE_DETECTION]))
        if variants:
            pack_variants.append((pack.id, variants))

    cv2, _np = ensure_vision()

    # Group configured windows by monitor. The monitor key is the
    # bounding rect tuple — same key, same capture. Windows with no
    # region (or unresolvable monitor) get handled in a separate
    # fallback pass that uses per-window capture.
    by_monitor: dict[tuple, dict] = {}
    monitor_for_window: dict[str, tuple] = {}
    unresolved_windows: list[dict] = []
    no_region_windows: list[dict] = []
    for window in windows:
        region = window.get("region")
        if not region or len(region) != 4:
            no_region_windows.append(window)
            continue
        info = press_dpi.monitor_info_for_region(region)
        if info is None:
            # No DPI service (non-Windows tests, or API hiccup) —
            # this window falls back to the legacy per-window path.
            unresolved_windows.append(window)
            continue
        key = info["key"]
        monitor_for_window[window["id"]] = key
        bucket = by_monitor.get(key)
        if bucket is None:
            by_monitor[key] = {
                "rect": info["rect"],
                "scale": info["scale"],
                "windows": [window],
                "rgb": None,
                "gray": None,
                "idle_matches": [],
                "askuser_matches": [],
                # pack_id -> [(score, (cx, cy)), ...], aggregated per
                # monitor so spatial filtering to each window only
                # walks lists at most once per pack per window.
                "pack_matches": {},
            }
        else:
            bucket["windows"].append(window)

    # Capture each unique monitor once + run all template matches.
    for key, bucket in by_monitor.items():
        try:
            bucket["rgb"] = capture_screen_rgb(bucket["rect"])
            bucket["gray"] = cv2.cvtColor(bucket["rgb"], cv2.COLOR_RGB2GRAY)
        except Exception:
            # Capture failed for this monitor — every window on it
            # gets a "configured but no signal" entry. Don't crash
            # the whole tick over one bad monitor.
            bucket["rgb"] = None
            bucket["gray"] = None
            continue
        mx, my = bucket["rect"][0], bucket["rect"][1]
        idle_gray = _pick_variant(idle_variants, bucket["scale"])
        if idle_gray is not None:
            bucket["idle_matches"] = _find_matches_in(
                bucket["gray"], idle_gray, threshold, mx, my
            )
        if askuser_variants:
            askuser_gray = _pick_variant(askuser_variants, bucket["scale"])
            if askuser_gray is not None:
                bucket["askuser_matches"] = _find_matches_in(
                    bucket["gray"], askuser_gray, threshold, mx, my
                )
        # Agent-pack detection pass — one matchTemplate per pack
        # per monitor. Cheap (the detection icons are tiny), but
        # gated on the pack list so users who haven't captured any
        # pack templates pay zero cost.
        for pack_id, variants in pack_variants:
            tpl = _pick_variant(variants, bucket["scale"])
            if tpl is None:
                continue
            bucket["pack_matches"][pack_id] = _find_matches_in(
                bucket["gray"], tpl, threshold, mx, my
            )

    def _matches_inside(matches, region):
        """Filter (score, (cx, cy)) entries to those whose centre is
        inside ``region`` [x, y, w, h]. Returns the original tuples,
        sorted by score desc (the input is already sorted)."""
        x, y, w, h = region
        x_end, y_end = x + w, y + h
        out = []
        for score, (cx, cy) in matches:
            if x <= cx < x_end and y <= cy < y_end:
                out.append((score, (cx, cy)))
        return out

    def _slice_window_rgb(bucket, region):
        """Crop the window's region out of the monitor capture.
        Returns None if the slice is empty (region entirely off-screen
        from the captured rect, which shouldn't happen in practice)."""
        if bucket.get("rgb") is None:
            return None
        mx, my, _mw, _mh = bucket["rect"]
        rx, ry, rw, rh = region
        x0 = max(0, rx - mx)
        y0 = max(0, ry - my)
        x1 = min(bucket["rgb"].shape[1], rx - mx + rw)
        y1 = min(bucket["rgb"].shape[0], ry - my + rh)
        if x1 <= x0 or y1 <= y0:
            return None
        return bucket["rgb"][y0:y1, x0:x1]

    results: list[dict] = []
    # Emit "no region configured" entries first so the order in
    # ``results`` roughly mirrors the order in cfg.windows (callers
    # like the worker don't rely on order, but consistency makes
    # debugging easier).
    for window in no_region_windows:
        results.append(
            {
                "id": window.get("id"),
                "name": window.get("name", "Cursor"),
                "idle": False,
                "asking": False,
                "score": 0.0,
                "configured": False,
                "detected_agent": None,
            }
        )

    def _best_pack_for_region(pack_matches: dict, region_tuple) -> str | None:
        """Pick the pack whose detection icon scored highest inside
        ``region_tuple``. Returns None if no pack matched anywhere
        in the window. With one tab per project (the user's stated
        assumption) this collapses to "the one pack that matched";
        the highest-score tiebreak keeps the result sane when
        multiple icons happen to share the window."""
        best_id: str | None = None
        best_score = 0.0
        for pid, matches in pack_matches.items():
            inside = _matches_inside(matches, region_tuple)
            if not inside:
                continue
            score = float(inside[0][0])
            if score > best_score:
                best_id = pid
                best_score = score
        return best_id

    # Windows whose monitor we could resolve — read off the cached
    # per-monitor match lists and spatially filter.
    for window in windows:
        if window in no_region_windows or window in unresolved_windows:
            continue
        wid = window["id"]
        key = monitor_for_window.get(wid)
        bucket = by_monitor.get(key) if key else None
        if bucket is None or bucket.get("gray") is None:
            results.append(
                {
                    "id": wid,
                    "name": window.get("name", "Cursor"),
                    "idle": False,
                    "asking": False,
                    "score": 0.0,
                    "configured": True,
                    "detected_agent": None,
                }
            )
            continue
        region = window["region"]
        region_tuple = (int(region[0]), int(region[1]), int(region[2]), int(region[3]))
        idle_inside = _matches_inside(bucket["idle_matches"], region_tuple)
        best_idle_score = idle_inside[0][0] if idle_inside else 0.0
        askuser_inside = (
            _matches_inside(bucket["askuser_matches"], region_tuple)
            if bucket["askuser_matches"]
            else []
        )
        entry = {
            "id": wid,
            "name": window.get("name", "Cursor"),
            "idle": bool(idle_inside),
            "asking": bool(askuser_inside),
            "score": float(best_idle_score),
            "configured": True,
            "detected_agent": _best_pack_for_region(
                bucket.get("pack_matches") or {}, region_tuple
            ),
        }
        if capture_rgb:
            entry["rgb"] = _slice_window_rgb(bucket, region_tuple)
        results.append(entry)

    # Fallback per-window path: only fires when monitor_info_for_region
    # returned None — typically a non-Windows test bench. Mirrors the
    # original pre-phase-2 capture + match loop exactly.
    for window in unresolved_windows:
        region = window["region"]
        region_tuple = (int(region[0]), int(region[1]), int(region[2]), int(region[3]))
        try:
            rgb = capture_screen_rgb(region_tuple)
            search_gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
        except Exception:
            results.append(
                {
                    "id": window.get("id"),
                    "name": window.get("name", "Cursor"),
                    "idle": False,
                    "asking": False,
                    "score": 0.0,
                    "configured": True,
                    "detected_agent": None,
                }
            )
            continue
        target_scale = press_dpi.scale_for_region(region)
        idle_gray = _pick_variant(idle_variants, target_scale)
        idle_matches = (
            _find_matches_in(
                search_gray, idle_gray, threshold, region_tuple[0], region_tuple[1]
            )
            if idle_gray is not None
            else []
        )
        is_asking = False
        if askuser_variants:
            askuser_gray = _pick_variant(askuser_variants, target_scale)
            if askuser_gray is not None:
                askuser_matches = _find_matches_in(
                    search_gray,
                    askuser_gray,
                    threshold,
                    region_tuple[0],
                    region_tuple[1],
                )
                is_asking = bool(askuser_matches)
        # Per-window agent-pack detection: same matcher, scoped to
        # the window's captured search image. Higher cost than the
        # per-monitor pass above (one matchTemplate per pack per
        # unresolved window) but this path only runs on test
        # benches without a DPI service.
        pack_matches_local: dict[str, list] = {}
        for pid, variants in pack_variants:
            tpl = _pick_variant(variants, target_scale)
            if tpl is None:
                continue
            pack_matches_local[pid] = _find_matches_in(
                search_gray,
                tpl,
                threshold,
                region_tuple[0],
                region_tuple[1],
            )
        detected_agent = _best_pack_for_region(pack_matches_local, region_tuple)
        entry = {
            "id": window.get("id"),
            "name": window.get("name", "Cursor"),
            "idle": bool(idle_matches),
            "asking": is_asking,
            "score": float(idle_matches[0][0]) if idle_matches else 0.0,
            "configured": True,
            "detected_agent": detected_agent,
        }
        if capture_rgb:
            entry["rgb"] = rgb
        results.append(entry)

    return codex_results + results
