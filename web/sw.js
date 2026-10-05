// 離線快取：有網路時一律先抓最新檔案（並更新快取），沒網路才用快取，
// 避免新舊版本的 app.js／core.js 混在一起。股價檔與跨網域請求不經過快取。
const CACHE = "asset-tracker-v4";
const SHELL = ["./", "index.html", "core.js", "app.js", "manifest.webmanifest", "icons/icon-192.png", "icons/icon-512.png", "icons/apple-touch-icon.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches
      .open(CACHE)
      .then((c) => c.addAll(SHELL.map((u) => new Request(u, { cache: "reload" }))))
      .then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== self.location.origin) return;
  if (url.pathname.endsWith("/prices.json")) return; // 股價每天都會變，一律走網路
  e.respondWith(
    (async () => {
      const cache = await caches.open(CACHE);
      try {
        // no-cache：向伺服器確認是否有新版（GitHub Pages 預設會讓瀏覽器快取 10 分鐘）
        const res = await fetch(e.request, { cache: "no-cache" });
        if (res.ok) cache.put(e.request, res.clone());
        return res;
      } catch (err) {
        const cached = await cache.match(e.request, { ignoreSearch: true });
        if (cached) return cached;
        throw err;
      }
    })()
  );
});
