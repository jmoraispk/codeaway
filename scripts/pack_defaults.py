"""Pack the current ``templates/config.json`` into ``defaults/`` so a
fresh install seeds the same rules + bridge templates.

Usage (from the repo root)::

    uv run python -m scripts.pack_defaults

What it does:

1. Reads ``templates/config.json`` (the current user's setup).
2. For every enabled template rule with a template_path: copies the
   base PNG + every ``.dpi<n>.png`` variant into ``defaults/`` under a
   sanitised filename derived from the rule's name (so the bundle
   isn't full of ``rule_81dbee27.png`` hashes), and records the rule's
   config in the manifest.
3. For the bridge idle / askuser templates: same thing, default names
   ``cursor_idle.png`` / ``cursor_askuser.png``.
4. Writes ``defaults/manifest.json``.

The script is idempotent: re-running overwrites the manifest and any
files it owns. It does NOT clean up stale PNGs left over from a
previous pack — drop those by hand before committing if the rule set
shrank.

Run this after capturing the templates you want to ship, then commit
``defaults/``. The next ``load_config()`` on a fresh install
(``templates/config.json`` missing) will copy the bundle back into
``templates/`` and create the rules + bridge config from the manifest.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Allow `uv run python -m scripts.pack_defaults` to import press_store
# from the repo root.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import press_store as ps  # noqa: E402


def _sanitise_filename(name: str, fallback: str = "rule") -> str:
    """Turn 'Click Yes' ->'click_yes', '"Continue when idle"' →
    'continue_when_idle'. Conservative: lowercases, replaces any run
    of non-alphanumerics with a single underscore, strips leading /
    trailing underscores. ``fallback`` is returned when the cleaned
    string is empty."""
    if not isinstance(name, str):
        return fallback
    cleaned = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")
    return cleaned or fallback


def _pack_template(
    base_src: Path, dst_dir: Path, dst_filename: str
) -> tuple[Path, int]:
    """Copy a template + its variants into ``dst_dir/<dst_filename>``
    using press_store's existing helper. Returns the destination base
    path and the number of files written."""
    dst = dst_dir / dst_filename
    written = ps._copy_template_bundle(base_src, dst)
    return dst, written


def pack(cfg: dict, defaults_dir: Path = ps.DEFAULTS_DIR) -> dict:
    """Build the manifest and copy files into ``defaults_dir``.
    Returns the manifest dict for inspection / further editing."""
    defaults_dir.mkdir(parents=True, exist_ok=True)
    used_names: set[str] = set()

    def _unique_filename(stem: str, ext: str) -> str:
        candidate = f"{stem}{ext}"
        if candidate not in used_names:
            used_names.add(candidate)
            return candidate
        # Disambiguate by appending a counter — happens if two rules
        # have names that sanitise to the same slug.
        n = 2
        while f"{stem}_{n}{ext}" in used_names:
            n += 1
        candidate = f"{stem}_{n}{ext}"
        used_names.add(candidate)
        return candidate

    manifest_rules: list[dict] = []
    for rule in cfg.get("rules", []) or []:
        if not rule.get("enabled"):
            continue
        if rule.get("matcher") != ps.MATCHER_TEMPLATE:
            continue
        template_ref = rule.get("template_path")
        if not template_ref:
            continue
        src = ps.resolve_template_path(template_ref)
        if src is None or not src.exists():
            print(f"  *skip rule '{rule.get('name')}': template file missing")
            continue
        stem = _sanitise_filename(rule.get("name", "rule"), fallback="rule")
        dst_filename = _unique_filename(stem, src.suffix)
        _, count = _pack_template(src, defaults_dir, dst_filename)
        entry = {
            "name": rule.get("name") or "Rule",
            "matcher": ps.MATCHER_TEMPLATE,
            "template_filename": dst_filename,
            "template_source_dpi": rule.get("template_source_dpi"),
            "action": rule.get("action", ps.ACTION_CLICK),
            "threshold": rule.get("threshold", 0.90),
        }
        if rule.get("action") == ps.ACTION_CLICK_TYPE_ENTER:
            entry["text"] = rule.get("text", "continue")
        manifest_rules.append(entry)
        print(f"  *packed rule '{entry['name']}' ->{dst_filename} ({count} files)")

    bridge_cfg = cfg.get("bridge", {}) or {}
    bridge_manifest: dict = {}
    for slot, default_name in [
        ("idle_template", "cursor_idle"),
        ("askuser_template", "cursor_askuser"),
    ]:
        ref = bridge_cfg.get(f"{slot}_path")
        if not ref:
            continue
        src = ps.resolve_template_path(ref)
        if src is None or not src.exists():
            print(f"  *skip bridge {slot}: file missing")
            continue
        filename = _unique_filename(default_name, src.suffix)
        _, count = _pack_template(src, defaults_dir, filename)
        bridge_manifest[f"{slot}_filename"] = filename
        bridge_manifest[f"{slot}_source_dpi"] = bridge_cfg.get(f"{slot}_source_dpi")
        print(f"  *packed bridge {slot} ->{filename} ({count} files)")

    manifest = {"rules": manifest_rules}
    if bridge_manifest:
        manifest["bridge"] = bridge_manifest
    (defaults_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="show what would be packed without writing files",
    )
    args = parser.parse_args()

    cfg = ps.load_config()
    print(f"reading {ps.CONFIG_PATH}")
    if args.dry_run:
        # Dump the would-be manifest without touching the filesystem.
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            manifest = pack(cfg, defaults_dir=Path(tmp))
        print("\nmanifest (dry-run, nothing written):")
        print(json.dumps(manifest, indent=2))
        return 0

    print(f"packing into {ps.DEFAULTS_DIR}")
    manifest = pack(cfg)
    print(f"\nwrote {ps.DEFAULTS_MANIFEST_PATH}")
    print(f"{len(manifest.get('rules', []))} rule(s), "
          f"{len(manifest.get('bridge') or {})} bridge template(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
