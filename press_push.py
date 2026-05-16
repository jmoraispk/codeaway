"""Web Push helpers for the bridge.

Two responsibilities:

1. ``ensure_vapid_keys(bridge_cfg)`` — first-run keypair generation.
   Web Push uses VAPID (RFC 8292): the server signs each push request
   with a private key the push services (Apple, Google, Mozilla) can
   verify against the public key the phone passed when it subscribed.
   We generate the keypair once on first launch and stash both halves
   in ``bridge.vapid_*`` so the public key stays stable across
   sessions — otherwise every restart would invalidate every existing
   subscription.

2. ``send_push(subscription, payload, bridge_cfg)`` — fire-and-forget
   delivery of one notification to one subscription endpoint. Returns
   a (ok, gone) pair: ``gone`` is True for 404 / 410 responses,
   meaning the user uninstalled the PWA or revoked permission and the
   subscription should be dropped from our list. Other errors are
   logged but not re-raised — pushes are best-effort, the bridge
   keeps running.
"""

from __future__ import annotations

import base64
import json
import logging
from typing import Optional

LOG = logging.getLogger("press_push")


def _b64url_no_pad(data: bytes) -> str:
    """URL-safe base64 without padding — the encoding Web Push uses
    for VAPID public keys and subscription auth secrets."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def generate_vapid_keypair() -> tuple[str, str]:
    """Returns ``(public_key_b64, private_key_pem)``.

    Public key: 65-byte uncompressed P-256 point, base64-url-no-pad.
    That's the format ``pushManager.subscribe`` wants for
    ``applicationServerKey``.

    Private key: PEM-encoded PKCS#8. ``pywebpush`` accepts either PEM
    or raw — PEM is the format ``py-vapid``'s ``Vapid.from_pem``
    eats, which is what's under the hood. PEM also survives a JSON
    config round-trip without surprises.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ec

    private_key = ec.generate_private_key(ec.SECP256R1())
    public_numbers = private_key.public_key().public_numbers()
    # Uncompressed point: 0x04 || X(32) || Y(32) — what the Web Push
    # spec mandates for the applicationServerKey.
    raw_public = (
        b"\x04"
        + public_numbers.x.to_bytes(32, "big")
        + public_numbers.y.to_bytes(32, "big")
    )
    private_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return _b64url_no_pad(raw_public), private_pem


def ensure_vapid_keys(bridge_cfg: dict) -> tuple[str, str]:
    """Return the bridge's VAPID keypair, generating it on first
    call. Mutates ``bridge_cfg`` so the caller can persist via
    ``save_config``."""
    public = bridge_cfg.get("vapid_public_key")
    private = bridge_cfg.get("vapid_private_key")
    if (
        isinstance(public, str)
        and public.strip()
        and isinstance(private, str)
        and private.strip()
    ):
        return public, private
    public, private = generate_vapid_keypair()
    bridge_cfg["vapid_public_key"] = public
    bridge_cfg["vapid_private_key"] = private
    return public, private


def send_push(
    subscription: dict,
    payload: dict,
    bridge_cfg: dict,
    ttl_seconds: int = 60,
) -> tuple[bool, bool]:
    """Sign + POST one push to ``subscription['endpoint']`` carrying
    ``payload`` (JSON-serialised in the encrypted body the browser
    sees). Returns ``(ok, gone)``:

    * ``ok``: True if the push service accepted the request.
    * ``gone``: True if the endpoint returned 404 / 410 — the
      subscription is dead and the caller should remove it.

    Errors that aren't ``gone`` are logged at warning level; we never
    re-raise, since a single phone going offline shouldn't take down
    the bridge thread.
    """
    from pywebpush import WebPushException, webpush

    public, private = ensure_vapid_keys(bridge_cfg)
    sub = subscription.get("subscription") if "subscription" in subscription else subscription
    if not isinstance(sub, dict) or not sub.get("endpoint"):
        return (False, False)

    # VAPID claims. ``sub`` is the subject identifying the
    # application server — push services require either a mailto: or
    # https: URL. We just pin to the auto-press project URL since
    # that's stable; this isn't a contact channel, it's an identity.
    vapid_claims = {
        "sub": (bridge_cfg.get("vapid_subject") or "mailto:bridge@auto-press.local"),
    }

    try:
        webpush(
            subscription_info=sub,
            data=json.dumps(payload),
            vapid_private_key=private,
            vapid_claims=vapid_claims,
            ttl=int(ttl_seconds),
        )
        return (True, False)
    except WebPushException as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        if status in (404, 410):
            return (False, True)
        LOG.warning(
            "web-push delivery failed (status=%s): %s",
            status,
            exc,
        )
        return (False, False)
    except Exception as exc:
        LOG.warning("web-push unexpected error: %s", exc)
        return (False, False)


def normalize_subscription(raw) -> Optional[dict]:
    """Accept the JSON shape the browser produces from
    ``PushManager.subscribe``. Returns a dict suitable for
    ``send_push`` (with ``endpoint``, ``keys.p256dh``, ``keys.auth``),
    or None if it's malformed.

    We trim to only the fields we need so we don't end up persisting
    the full PushSubscription wrapper into config.json."""
    if not isinstance(raw, dict):
        return None
    endpoint = raw.get("endpoint")
    keys = raw.get("keys")
    if not isinstance(endpoint, str) or not endpoint.strip():
        return None
    if not isinstance(keys, dict):
        return None
    p256dh = keys.get("p256dh")
    auth = keys.get("auth")
    if not isinstance(p256dh, str) or not isinstance(auth, str):
        return None
    return {
        "endpoint": endpoint.strip(),
        "keys": {"p256dh": p256dh, "auth": auth},
    }
