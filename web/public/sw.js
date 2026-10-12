// Enough of a service worker to make litwatch installable and a share target. It handles no
// fetches: the app is always read live, and a no-op fetch handler would only sit in the way.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
