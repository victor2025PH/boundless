/* AvatarHub Studio Service Worker (v3)
   教训(v2→v3)：旧版把「页面/JS」也写进 Cache Storage，SW 接管后会回放陈旧/半截的
   应用外壳，导致 Alpine 不初始化、页面卡死（命令面板+弹窗全部露出）。强刷也复现，
   只有全新窗口(SW 尚未接管首帧)才正常。

   新策略（彻底消除该故障类别）：
   - 应用外壳（导航 / HTML / JS / CSS / API）：一律直连网络，SW 完全不拦截 → 永不回放旧壳。
   - 仅缓存 PWA 安装所必需、且基本不变的资产（图标 / manifest），并且只缓存正常的 200 同源响应。
   - activate 时清空所有旧版本缓存（含被污染的 v2）。
*/
const CACHE = 'avatarhub-v3';
const PWA_ASSETS = ['/static/manifest.json', '/static/icon.svg'];
const CACHEABLE = new Set(PWA_ASSETS);

self.addEventListener('install', e => {
  self.skipWaiting();
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(PWA_ASSETS)).catch(() => {}));
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  // 导航请求(页面外壳)绝不拦截：直连网络，杜绝回放旧壳导致的卡死。
  if (req.mode === 'navigate') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;
  // 只有图标/manifest 这两个不变资产才走缓存；其余(HTML/JS/CSS/API/图片)全部直连网络。
  if (!CACHEABLE.has(url.pathname)) return;

  e.respondWith(
    caches.match(req).then(hit => hit || fetch(req).then(res => {
      if (res && res.ok && res.type === 'basic') {
        const copy = res.clone();
        caches.open(CACHE).then(c => c.put(req, copy)).catch(() => {});
      }
      return res;
    }).catch(() => hit))
  );
});
