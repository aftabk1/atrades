const CACHE = 'a1trades-v1';
const STATIC = [
  '/static/style.css',
  '/static/app.js',
  '/static/login.js',
  '/static/A1Trades.png',
  '/static/A1Trades1.png',
  '/static/manifest.json',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(STATIC)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Always go network-first for API calls — stale trading data is worse than no data
  if (url.pathname.startsWith('/api/') || url.pathname === '/login' || url.pathname === '/logout') {
    e.respondWith(
      fetch(e.request).catch(() => new Response('{"error":"offline"}', {
        headers: { 'Content-Type': 'application/json' }
      }))
    );
    return;
  }

  // Cache-first for static assets
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request).then(res => {
      if (res.ok && url.pathname.startsWith('/static/')) {
        caches.open(CACHE).then(c => c.put(e.request, res.clone()));
      }
      return res;
    }))
  );
});
