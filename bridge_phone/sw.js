// auto-press service worker — Web Push delivery.
//
// Wakes up when the OS push service delivers an encrypted payload from
// the bridge, decrypts the body (handled transparently by the browser),
// and shows a native OS notification. Tapping the notification focuses
// (or opens) the PWA and, when the payload includes a window_id,
// navigates to that window's snapshot view.
//
// The SW also handles "skipWaiting" / "claim" on install + activate so a
// new SW version takes over immediately instead of waiting for every
// PWA tab to close — keeps the version bump cycle tight.

const SW_VERSION = "v1";

self.addEventListener("install", () => {
  // Activate the new SW right away — there's no cached content to
  // worry about clobbering, the PWA shell is served fresh every load.
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(self.clients.claim());
});

// Push events arrive with an encrypted payload that the browser
// already decrypted by the time we see it. event.data.json() returns
// whatever the server JSON-encoded.
self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (e) {
    // Fall back to the raw text if the server sent something that's
    // not JSON. Better to surface a generic notification than to
    // swallow the event silently.
    payload = { title: "auto-press", body: event.data && event.data.text() };
  }
  const title = payload.title || "auto-press";
  const options = {
    body: payload.body || "",
    // `tag` collapses replacement notifications for the same window —
    // a second "idle" push for the same window replaces the first
    // instead of stacking. That's what we want for an at-a-glance
    // status pill, not a log.
    tag: payload.tag || undefined,
    renotify: true,
    icon: "/static/icon-192.png",
    badge: "/static/icon-192.png",
    data: {
      window_id: payload.window_id || null,
      kind: payload.kind || null,
      ts: Date.now(),
    },
    // Vibration pattern: short-short-short for asking (more urgent),
    // single short for idle.
    vibrate: payload.kind === "asking" ? [80, 40, 80, 40, 80] : [100],
  };
  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const data = event.notification.data || {};
  // Build the URL to navigate to. When a window_id is attached we
  // route to the PWA root with a query string the app reads on load
  // to jump straight to that window's snapshot view.
  const url = data.window_id
    ? `/?focus_window=${encodeURIComponent(data.window_id)}`
    : "/";
  event.waitUntil(
    (async () => {
      // Re-use an existing tab if one's already open — calling focus()
      // brings it back from background. Falling through to openWindow
      // covers the "PWA fully closed" case (typical on mobile).
      const clientsList = await self.clients.matchAll({
        type: "window",
        includeUncontrolled: true,
      });
      for (const client of clientsList) {
        if ("focus" in client) {
          try {
            await client.focus();
            if ("navigate" in client && data.window_id) {
              try { await client.navigate(url); } catch {}
            }
            return;
          } catch {}
        }
      }
      if (self.clients.openWindow) {
        await self.clients.openWindow(url);
      }
    })()
  );
});
