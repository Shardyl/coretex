// Cortex service worker — makes the cockpit installable + keeps the shell working offline.
// Network-first for everything; the API is never cached; the app shell falls back to cache.
const CACHE = 'cortex-v5';
const SHELL = ['/', '/index.html', '/manifest.webmanifest', '/icon-192.png', '/icon-512.png'];

self.addEventListener('install', (e) => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/')) return;  // never cache the API
  // Network-first WITH A TIMEOUT: a slow/hung connection must never leave the app loading forever.
  // If the network doesn't answer in 3.5s, serve the cached shell so the PIN screen always appears.
  e.respondWith((async () => {
    const net = fetch(e.request).then((r) => {
      const cp = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, cp)).catch(() => {}); return r;
    });
    let r;
    try { r = await Promise.race([net, new Promise((res) => setTimeout(() => res('TIMEOUT'), 3500))]); }
    catch (x) { r = 'TIMEOUT'; }
    if (r && r !== 'TIMEOUT') return r;
    const cached = (await caches.match(e.request)) || (await caches.match('/'));
    if (cached) return cached;
    try { return await net; } catch (x) { return new Response('offline', { status: 503 }); }
  })());
});

// ---- Web Push: show the notification, deep-link on tap ----
self.addEventListener('push', (e) => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch (x) {}
  e.waitUntil((async () => {
    await self.registration.showNotification(d.title || 'Cortex', {
      body: d.body || '', tag: d.tag, data: { url: d.url || '/' },
      icon: '/icon-192.png', badge: '/icon-192.png', renotify: true
    });
    // nudge any open app window to refresh its Inbox immediately (no need to tap the notification)
    const cs = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
    cs.forEach((c) => c.postMessage({ type: 'cortex-refresh' }));
  })());
});
self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || '/';
  e.waitUntil(clients.matchAll({ type: 'window', includeUncontrolled: true }).then((cs) => {
    for (const c of cs) { if ('focus' in c) return c.focus(); }
    if (clients.openWindow) return clients.openWindow(url);
  }));
});
