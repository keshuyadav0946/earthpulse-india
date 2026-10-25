/* EarthPulse India service worker.
   Hard rule: pages are NETWORK-FIRST — always fetched fresh, with the
   cache used only as an offline fallback — because the live disaster
   data refreshes several times a day and must never be served stale.
   Cache-first is used ONLY for the app icons and the manifest. */
var CACHE = 'earthpulse-v1';
var STATIC_FILES = ['icon-192.png', 'icon-512.png', 'icon-maskable-512.png', 'manifest.webmanifest'];

self.addEventListener('install', function (event) {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE).then(function (cache) {
      return cache.addAll(STATIC_FILES);
    })
  );
});

self.addEventListener('activate', function (event) {
  event.waitUntil(
    caches.keys().then(function (names) {
      return Promise.all(names.map(function (name) {
        if (name !== CACHE) return caches.delete(name);
      }));
    }).then(function () {
      return self.clients.claim();
    })
  );
});

self.addEventListener('fetch', function (event) {
  var req = event.request;
  if (req.method !== 'GET') return;
  var url = new URL(req.url);
  if (url.origin !== self.location.origin) return; // Firebase, fonts, CDN: untouched

  var file = url.pathname.split('/').pop();

  if (STATIC_FILES.indexOf(file) !== -1) {
    // Icons + manifest: cache-first.
    event.respondWith(
      caches.match(req).then(function (hit) {
        return hit || fetch(req).then(function (res) {
          var copy = res.clone();
          caches.open(CACHE).then(function (cache) { cache.put(req, copy); });
          return res;
        });
      })
    );
    return;
  }

  // All pages/documents: network-first, cache only as offline fallback.
  event.respondWith(
    fetch(req).then(function (res) {
      var copy = res.clone();
      caches.open(CACHE).then(function (cache) { cache.put(req, copy); });
      return res;
    }).catch(function () {
      return caches.match(req);
    })
  );
});
