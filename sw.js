'use strict';

const CACHE_PREFIX = 'weather-pwa-';
const CACHE_VERSION = 'v1.0.2';
const SHELL_CACHE = CACHE_PREFIX + CACHE_VERSION;
const APP_SHELL = [
  './',
  './index.html',
  './manifest.json',
  './cities.js',
  './icon-192.png',
  './icon-512.png',
  './icon-512-maskable.png'
];

self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    await cache.addAll(APP_SHELL);
    await self.skipWaiting();
  })());
});

self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    const keys = await caches.keys();
    await Promise.all(keys
      .filter(key => key.startsWith(CACHE_PREFIX) && key !== SHELL_CACHE)
      .map(key => caches.delete(key)));
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', event => {
  const request = event.request;
  if (request.method !== 'GET') return;

  const url = new URL(request.url);

  // Never cache API calls or CDNs
  if (url.origin !== self.location.origin) return;
  if (url.pathname.endsWith('/sw.js')) return;

  // Navigation: network-first, then the last cached app shell for offline launch.
  if (request.mode === 'navigate') {
    event.respondWith((async () => {
      try {
        const response = await fetch(request);
        if (response && response.ok) {
          const cache = await caches.open(SHELL_CACHE);
          cache.put('./index.html', response.clone()).catch(() => {});
        }
        return response;
      } catch (error) {
        return (await caches.match('./index.html')) ||
               (await caches.match('./')) ||
               new Response('You are offline. Reconnect to load the app.', {
                 status: 503,
                 headers: { 'Content-Type': 'text/plain; charset=utf-8' }
               });
      }
    })());
    return;
  }

  // Same-origin static files: cache-first, refresh cache in the background.
  if (/\.(?:js|css|png|svg|webp|jpg|jpeg|json|woff2?)$/i.test(url.pathname)) {
    event.respondWith((async () => {
      const cached = await caches.match(request);
      const network = fetch(request).then(response => {
        if (response && response.ok) {
          const copy = response.clone();
          caches.open(SHELL_CACHE).then(cache => cache.put(request, copy)).catch(() => {});
        }
        return response;
      });
      return cached || network;
    })());
  }
});
