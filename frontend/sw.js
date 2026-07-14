const CACHE = "transcript-desk-v1";
const SHELL = ["./index.html", "./manifest.json", "./icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// 只快取「頁面外殼」(HTML/CSS/JS/icon)，不快取 API 請求，
// 因為逐字稿/留言資料一定要即時連網才拿得到。
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // API 一律走網路
  e.respondWith(
    caches.match(e.request).then((cached) => cached || fetch(e.request))
  );
});
