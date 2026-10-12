// Enough of a service worker to make litwatch installable and a share target. Nothing is
// cached: the app is always read live.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
