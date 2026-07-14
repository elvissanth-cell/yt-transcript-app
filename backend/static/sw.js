const CACHE = "transcript-desk-v2"; // 版本號提升,配合下面的activate清理邏輯強制汰換所有裝置上的舊快取
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

// 改用「network-first」策略:每次都先嘗試連網抓最新版本,只有在真的連不到網路時
// 才退回本機快取當備援。之前用「cache-first」會導致快取住的舊版本永遠不會更新,
// 這也是先前手機上感覺「改了沒生效」的根本原因。
self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (url.pathname.startsWith("/api/")) return; // API 一律走網路,不快取

  e.respondWith(
    fetch(e.request)
      .then((res) => {
        const resClone = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, resClone));
        return res;
      })
      .catch(() => caches.match(e.request)) // 離線時才退回快取
  );
});
