const CACHE_VERSION = "v1";
const SHELL_CACHE = "app-shell-" + CACHE_VERSION;
const API_CACHE = "api-" + CACHE_VERSION;

const SHELL_FILES = ["./", "./index.html", "./manifest.json"];

const API_HOSTS = [
  "api.open-meteo.com",
  "air-quality-api.open-meteo.com",
  "geocoding-api.open-meteo.com",
  "wttr.in",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(SHELL_CACHE)
      .then((cache) => cache.addAll(SHELL_FILES))
      .catch(() => {}),
  );
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(
          keys
            .filter((k) => k !== SHELL_CACHE && k !== API_CACHE)
            .map((k) => caches.delete(k)),
        ),
      ),
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;

  const url = new URL(req.url);

  if (API_HOSTS.some((h) => url.hostname === h)) {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const clone = res.clone();
          caches
            .open(API_CACHE)
            .then((c) => c.put(req, clone))
            .catch(() => {});
          return res;
        })
        .catch(() => caches.match(req)),
    );
    return;
  }

  if (url.origin === self.location.origin) {
    event.respondWith(
      caches.match(req).then((cached) => {
        return (
          cached ||
          fetch(req)
            .then((res) => {
              const clone = res.clone();
              caches
                .open(SHELL_CACHE)
                .then((c) => c.put(req, clone))
                .catch(() => {});
              return res;
            })
            .catch(() => caches.match("./index.html"))
        );
      }),
    );
  }
});
