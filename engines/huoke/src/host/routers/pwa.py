# -*- coding: utf-8 -*-
"""PWA (Progressive Web App) 支持端点。"""
from fastapi import APIRouter
from fastapi.responses import JSONResponse, Response

from src.host import brand

router = APIRouter(tags=["pwa"])


@router.get("/manifest.json")
def pwa_manifest():
    return JSONResponse(content={
        "name": brand.app_title(),
        "short_name": brand.NAME_ZH,
        "start_url": "/dashboard",
        "display": "standalone",
        "background_color": "#080b10",   # AvatarHub VI 底色
        "theme_color": "#4f7aff",        # AvatarHub VI 强调色
        "icons": [
            {"src": "/static/brand/reachx-256.png", "sizes": "256x256",
             "type": "image/png"},
            {"src": "/static/brand/reachx-512.png", "sizes": "512x512",
             "type": "image/png", "purpose": "any maskable"},
        ],
    }, headers={"Cache-Control": "public, max-age=86400"})


@router.get("/icon-192.svg")
@router.get("/icon-512.svg")
def pwa_icon():
    # 智拓品牌图标：深底 + 强调色「智」字（替代旧 "OC"）
    svg = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512">
<rect width="512" height="512" rx="96" fill="#10161f"/>
<text x="256" y="336" text-anchor="middle" font-size="300" font-weight="700"
  font-family="'PingFang SC','Microsoft YaHei',system-ui" fill="#4f7aff">智</text>
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
const CACHE_NAME = 'reachx-v12-p3';
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
