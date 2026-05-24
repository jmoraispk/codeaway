"""Agent packs — declarative registry of the AI agents we know how to
detect inside a Cursor window.

The "host" is always a Cursor IDE window. What varies is the *agent*
running inside it: the built-in Cursor agent, Claude Code in a
terminal pane, Codex CLI, and so on. Each agent has a small visual
fingerprint (an icon, a spinner, a button) that we template-match
against the window's region to tag the window with its agent type.

Two icon roles per pack today:

* ``detection`` — a marker that says "this agent is running here".
  Should be the smallest, most distinctive glyph: the Cursor chat
  panel's logo, the Claude Code prompt symbol, etc. Matching this
  template inside a window's region answers "what agent is in this
  window?".
* ``idle``      — a marker that says "this agent is idle / ready for
  input" (the orange burst for the Cursor agent, the dotted spinner
  for Claude Code). Captured here so a future engine pass can use
  per-pack idle templates instead of the single global one in
  ``bridge.idle_template_path``. Not yet wired into the runtime
  matcher — captures are stored for the upcoming switch.

The registry is **code-defined** (not user-config). Adding a pack =
add a constant + entry. The user's captured PNGs persist in
``templates/`` and survive app upgrades because the file names are
derived from the pack id, not the user's choice.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


# Icon role identifiers. Kept as plain strings so config storage and
# UI dispatch don't need an enum import — every consumer just imports
# the constants.
ROLE_DETECTION = "detection"
ROLE_IDLE = "idle"
ROLE_IDS: tuple[str, ...] = (ROLE_DETECTION, ROLE_IDLE)


@dataclass(frozen=True)
class AgentPack:
    """Static metadata about one agent type.

    Fields:
        id:           machine identifier — also the prefix for
                       template file names on disk. Stable across
                       releases.
        label:        human-readable name shown in the UI.
        capture_hint: one-sentence guidance shown in the UI capture
                       cards so the user knows what to drag a box
                       around (e.g. "the orange spinner in the
                       Cursor chat panel").
    """

    id: str
    label: str
    capture_hint: str


# Initial registry. Two packs cover the user's stated near-term use
# (Cursor agent + Claude Code in a terminal pane). Adding Codex /
# Copilot Chat / etc. later is a one-line append.
_PACKS: tuple[AgentPack, ...] = (
    AgentPack(
        id="cursor_agent",
        label="Cursor agent",
        capture_hint=(
            "the small orange burst that appears in the Cursor chat "
            "panel while the agent is thinking"
        ),
    ),
    AgentPack(
        id="claude_code",
        label="Claude Code",
        capture_hint=(
            "the small dotted spinner that appears at the Claude Code "
            "prompt while it's working"
        ),
    ),
)


def iter_packs() -> Iterable[AgentPack]:
    """Every registered pack in declaration order. Stable for callers
    that render a UI list."""
    return _PACKS


def pack_ids() -> tuple[str, ...]:
    return tuple(p.id for p in _PACKS)


def pack_by_id(pack_id: str | None) -> AgentPack | None:
    if not pack_id:
        return None
    for p in _PACKS:
        if p.id == pack_id:
            return p
    return None


def is_valid_role(role: str) -> bool:
    return role in ROLE_IDS


def template_filename(pack_id: str, role: str, ext: str = ".png") -> str:
    """Canonical filename for a pack template captured by the user.
    Naming convention: ``pack_<pack_id>_<role>.png``. Same template
    store as the existing idle / askuser bundles, so the DPI variant
    pipeline (write_template_with_dpi_variants etc.) applies without
    modification.

    Used by the capture UI to settle on a stable filename. Kept here
    so renaming a pack id later doesn't accidentally orphan the old
    PNGs — anyone touching the format reads this function first."""
    if not is_valid_role(role):
        raise ValueError(f"unknown pack template role: {role!r}")
    safe_id = pack_id.strip()
    if not safe_id:
        raise ValueError("pack_id required")
    return f"pack_{safe_id}_{role}{ext}"


def default_pack_entry() -> dict:
    """Empty-but-shaped per-pack config entry. Mirrors the shape used
    by the existing bridge idle / askuser slots so the store can
    treat them uniformly. ``*_template_path`` is the relative file
    name inside the templates/ directory; ``*_template_source_dpi``
    is the DPI scale captured at, or None for legacy / not-yet-
    captured slots."""
    return {
        "detection_template_path": None,
        "detection_template_source_dpi": None,
        "idle_template_path": None,
        "idle_template_source_dpi": None,
    }


# Field-name maps so other modules don't have to hard-code the
# four key strings every time they iterate roles. Keeps the schema
# definition in one place.
PATH_KEY_FOR_ROLE: dict[str, str] = {
    ROLE_DETECTION: "detection_template_path",
    ROLE_IDLE: "idle_template_path",
}
DPI_KEY_FOR_ROLE: dict[str, str] = {
    ROLE_DETECTION: "detection_template_source_dpi",
    ROLE_IDLE: "idle_template_source_dpi",
}
