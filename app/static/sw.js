/* Gist service worker.
 *
 * Three jobs, in order of how much they matter on a phone:
 *
 *  1. Make the app launchable offline. The shell is precached, so tapping the
 *     home-screen icon on a bad train connection opens the UI rather than a
 *     Safari error page.
 *  2. Keep the saved library readable offline. GET /api/saved* is network
 *     first with a cache fallback — never the other way round, or you would
 *     read yesterday's notes while online and not know it.
 *  3. Never touch anything else under /api/. Generating notes, chatting and
 *     research all cost money and have side effects; a stale answer there is
 *     worse than no answer.
 *
 * Bump VERSION on every change to the shell or this file. Old caches are
 * deleted on activate, so a bump is also how a bad cache gets cleared.
 */

const VERSION = "gist-v2";
const SHELL = `${VERSION}-shell`;
const DATA = `${VERSION}-data`;
const VENDOR = `${VERSION}-vendor`;

/* A stalled mobile connection does not fail fast — WebKit will hold it for
 * tens of seconds. Waiting that long in a standalone window means staring at
 * blank white with no browser chrome and no reload button, which is worse
 * than being offline. Show the cached shell instead and let the network land
 * whenever it lands. */
const NAVIGATE_TIMEOUT_MS = 3000;

const SHELL_URLS = [
  "/",
  "/manifest.webmanifest",
  "/icons/icon-192.png",
  "/icons/icon-512.png",
  "/apple-touch-icon.png",
];

const OFFLINE_HTML = `<!doctype html><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Gist is offline</title>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;
background:#f3f2f9;color:#16162a;font:400 15px/1.6 system-ui,-apple-system,sans-serif}
div{text-align:center;padding:24px}h1{font-size:19px;margin:0 0 6px}
p{margin:0;color:#55527a;font-size:14px}
@media(prefers-color-scheme:dark){body{background:#0b0b14;color:#edecf7}p{color:#a9a6c4}}</style>
<div><h1>Gist is offline</h1><p>Reconnect and open it again.</p></div>`;

const offlinePage = () =>
  new Response(OFFLINE_HTML, {
    status: 503,
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });

self.addEventListener("install", (e) => {
  // addAll fails the whole install if one URL 404s, which is the behaviour we
  // want: a half-populated shell cache is worse than none.
  e.waitUntil(caches.open(SHELL).then((c) => c.addAll(SHELL_URLS)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((k) => !k.startsWith(VERSION)).map((k) => caches.delete(k)),
      ))
      .then(() => self.clients.claim()),
  );
});

/* Signing out has to empty the API cache. Without this the next person to use
 * the phone gets the previous user's library the moment they go offline —
 * cache.match does not look at cookies. */
self.addEventListener("message", (e) => {
  if (e.data && e.data.type === "wipe") {
    e.waitUntil(caches.delete(DATA));
  }
});

/** Network first, falling back to whatever we cached last time. */
async function networkFirst(event, req, cacheName) {
  const cache = await caches.open(cacheName);
  try {
    const res = await fetch(req);
    // Keep the SW alive long enough to finish the write; iOS is aggressive
    // about terminating workers the moment respondWith settles.
    if (res.ok) event.waitUntil(cache.put(req, res.clone()));
    return res;
  } catch (err) {
    const hit = await cache.match(req);
    if (hit) return hit;
    throw err;
  }
}

/** Serve from cache at once; refresh in the background for next time. */
async function staleWhileRevalidate(event, req, cacheName) {
  const cache = await caches.open(cacheName);
  const hit = await cache.match(req);
  const fresh = fetch(req)
    .then((res) => {
      if (res.ok) event.waitUntil(cache.put(req, res.clone()));
      return res;
    })
    .catch(() => null);
  if (hit) {
    event.waitUntil(fresh);
    return hit;
  }
  const res = await fresh;
  return res || Response.error();
}

/**
 * The page itself: whichever of network or cache answers first, with the
 * network still allowed to refresh the cache afterwards.
 */
async function navigateWithTimeout(event, req) {
  const cache = await caches.open(SHELL);
  const net = fetch(req)
    .then((res) => {
      if (res.ok) event.waitUntil(cache.put("/", res.clone()));
      return res;
    })
    .catch(() => null);

  const timer = new Promise((r) => setTimeout(() => r(null), NAVIGATE_TIMEOUT_MS));
  const winner = await Promise.race([net, timer]);
  if (winner) return winner;

  // Either the network lost the race or it failed outright.
  const hit = (await cache.match(req)) || (await cache.match("/"));
  if (hit) {
    event.waitUntil(net);
    return hit;
  }
  // The cache can be evicted after a long idle period. Without this the
  // standalone window shows "Safari cannot open the page" and there is no
  // browser chrome to recover from.
  return (await net) || offlinePage();
}

const VENDOR_HOSTS = /^(fonts\.(googleapis|gstatic)\.com|cdnjs\.cloudflare\.com)$/;
const OFFLINE_READABLE = /^\/api\/(saved|categories|auth\/me)(\/|$)/;

self.addEventListener("fetch", (e) => {
  const req = e.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  // Fonts and the markdown library: versioned URLs, safe to keep.
  if (url.origin !== self.location.origin) {
    if (VENDOR_HOSTS.test(url.host)) {
      e.respondWith(staleWhileRevalidate(e, req, VENDOR));
    }
    return;
  }

  if (req.mode === "navigate") {
    e.respondWith(navigateWithTimeout(e, req));
    return;
  }

  if (url.pathname.startsWith("/api/")) {
    // Only the library and the current identity are readable offline.
    // Everything else must be live. /api/auth/me is here because without it
    // the page cannot tell it is already signed in and shows a sign-in wall
    // that no offline user can get past.
    if (OFFLINE_READABLE.test(url.pathname)) {
      e.respondWith(networkFirst(e, req, DATA));
    }
    return;
  }

  // Icons, manifest and any other static file.
  e.respondWith(staleWhileRevalidate(e, req, SHELL));
});
