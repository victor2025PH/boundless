# -*- coding: utf-8 -*-
"""PWA (Progressive Web App) 支持端点。"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

router = APIRouter(tags=["pwa"])


@router.get("/manifest.json")
def pwa_manifest():
    return JSONResponse(content={
        "name": "OpenClaw 群控管理系统",
        "short_name": "OpenClaw",
        "start_url": "/dashboard",
        "display": "standalone",
        "background_color": "#0b1120",
        "theme_color": "#3b82f6",
        "icons": [
            {"src": "/icon-192.svg", "sizes": "192x192", "type": "image/svg+xml"},
            {"src": "/icon-512.svg", "sizes": "512x512", "type": "image/svg+xml"},
        ],
    }, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/icon-192.svg")
@router.get("/icon-512.svg")
def pwa_icon():
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="80" fill="#1e293b"/>
<text x="256" y="300" text-anchor="middle" font-size="240" font-weight="700"
  font-family="system-ui" fill="#3b82f6">OC</text>
</svg>"""
    return Response(content=svg, media_type="image/svg+xml",
                    headers={"Cache-Control": "public, max-age=604800"})


@router.get("/sw.js")
def service_worker():
    # Phase-5: 缓存策略升级
    # - 静态资源 (icon / manifest / css / js): cache-first, 后台更新
    # - 数据 API (/cluster/, /lead-mesh/, /auth/): network-first, fallback offline 提示
    # - dashboard / login HTML: 永远 network (不要 cache 旧版界面)
    sw_code = """
const CACHE_NAME = 'openclaw-v5-phase5';
const STATIC_ASSETS = ['/manifest.json', '/icon-192.svg', '/icon-512.svg'];
const STATIC_PREFIXES = ['/static/css/', '/static/js/'];
const NETWORK_FIRST_PREFIXES = ['/cluster/', '/lead-mesh/', '/auth/'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE_NAME).then(c => c.addAll(STATIC_ASSETS)));
  self.skipWaiting();
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys().then(keys =>
    Promise.all(keys.filter(k => k !== CACHE_NAME).map(k => caches.delete(k)))
  ));
  self.clients.claim();
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);

  // dashboard / login HTML: 永远拉新, 不缓存避免老 UI 残留
  if (url.pathname === '/dashboard' || url.pathname === '/login'
      || url.pathname === '/' || url.pathname.startsWith('/static/l2-dashboard')) {
    e.respondWith(fetch(e.request).catch(() =>
      new Response('Offline', { status: 503, statusText: 'Offline' })
    ));
    return;
  }

  // 数据 API: network-first, offline 时返 503
  if (NETWORK_FIRST_PREFIXES.some(p => url.pathname.startsWith(p))) {
    e.respondWith(
      fetch(e.request).catch(() =>
        new Response(JSON.stringify({error: 'offline'}),
          { status: 503, headers: {'Content-Type': 'application/json'} })
      )
    );
    return;
  }

  // 静态资源 (icon / css / js / manifest): cache-first, 后台 revalidate
  const isStatic = url.pathname.startsWith('/icon')
    || url.pathname === '/manifest.json'
    || STATIC_PREFIXES.some(p => url.pathname.startsWith(p));
  if (isStatic) {
    e.respondWith(
      caches.match(e.request).then(cached => {
        const networkFetch = fetch(e.request).then(resp => {
          if (resp && resp.ok) {
            const clone = resp.clone();
            caches.open(CACHE_NAME).then(c => c.put(e.request, clone));
          }
          return resp;
        }).catch(() => cached);  // offline → 用 cache
        return cached || networkFetch;
      })
    );
  }
});
"""
    return Response(content=sw_code, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})
