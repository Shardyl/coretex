// YOLO boat book service worker. Bump CACHE on every ship or the phone keeps the cached index.html.
const CACHE = 'yolo-v1';
const ASSETS = ['./', './index.html', './manifest.json'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  // NEVER cache the API: a cached /api/yolo/state would serve stale rows, and a cached 401 would
  // look like a broken login forever.
  if (url.pathname.startsWith('/api/')) return;
  // App shell is network-first so a deploy lands on the next reload; cache is the offline fallback.
  if (e.request.mode === 'navigate' || url.pathname.endsWith('/index.html')) {
    e.respondWith(fetch(e.request.url, {cache: 'no-cache', credentials: 'same-origin'}).then(res => {
      if (res.ok) caches.open(CACHE).then(c => c.put(e.request, res.clone()));
      return res;
    }).catch(() => caches.match(e.request).then(c => c || caches.match('./index.html'))));
    return;
  }
  e.respondWith(caches.match(e.request).then(cached => cached || fetch(e.request).then(res => {
    if (res.ok) caches.open(CACHE).then(c => c.put(e.request, res.clone()));
    return res;
  }).catch(() => cached)));
});
