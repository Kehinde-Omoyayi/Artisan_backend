'use strict';

// v4: request drafts and the offline request-assistant were removed entirely;
// request details are submitted once from a server-rendered form. This worker
// only keeps static assets fresh (stale-while-revalidate); pages require the
// network so booking/payment state is always read live from the server.
const SHELL_CACHE = 'artisan-market-shell-v5';
const PRECACHE_URLS = [
  '/static/css/site.css',
  '/static/css/conversations.css',
  '/static/js/api-errors.js',
  '/static/js/site.js',
  '/static/fonts/DMSans-Latin-Variable.woff2',
  '/static/fonts/DMSerifDisplay-Regular.woff2',
];

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then((cache) => cache.addAll(PRECACHE_URLS))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys
        // Drop every previous shell generation, including the retired
        // artisan-market-offline-* caches.
        .filter((key) => (key.startsWith('artisan-market-offline-') || key.startsWith('artisan-market-shell-')) && key !== SHELL_CACHE)
        .map((key) => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

// Static assets are served from the cache for speed but always refreshed in the
// background. A shipped stylesheet or script can therefore never be pinned to an
// old copy by a long-lived browser cache.
async function staleWhileRevalidateAsset(request) {
  const cache = await caches.open(SHELL_CACHE);
  const cached = await cache.match(request);
  const network = fetch(request)
    .then((response) => {
      if (response && response.ok) cache.put(request, response.clone());
      return response;
    })
    .catch(() => null);
  return cached || (await network) || Response.error();
}

self.addEventListener('fetch', (event) => {
  const request = event.request;
  if (request.method !== 'GET') return;
  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;

  if (url.pathname.startsWith('/static/')) {
    event.respondWith(staleWhileRevalidateAsset(request));
  }
});
