import { NextRequest, NextResponse } from "next/server";
import { R2_PUBLIC_ROOT, DL_PREFIXES } from "@/lib/mirror";

/**
 * /dl/<path> 安装包智能分流（下载提速 P0 · 2026-08-08）。
 *
 * 逻辑：HEAD 探测 R2 镜像上的同名文件（结果按路径缓存，正向 5 分钟 / 负向 1 分钟）
 *   → 镜像健康：302 到 R2（下载流量走 Cloudflare 边缘，$0 出流，不占主站 15Mbps 管道）
 *   → 镜像缺文件/超时：302 回本站同路径（nginx /releases 别名或 Next public/downloads）。
 * 未来发新版即使忘了同步镜像，该文件也只是自动回落主站，绝不 404。
 *
 * 安全：路径白名单前缀 + 拒绝 ".."/协议注入；只处理 GET/HEAD。
 */
export const dynamic = "force-dynamic";

const OK_TTL_MS = 5 * 60_000;
const BAD_TTL_MS = 60_000;
const PROBE_TIMEOUT_MS = 3500;
// r2.dev 会 403 部分非浏览器 UA（Python-urllib 实锤）——探测必须带自定义 UA。
const PROBE_UA = "BD-dl-router/1.0";

const health = new Map<string, { ok: boolean; ts: number }>();

function validPath(p: string): boolean {
  if (!DL_PREFIXES.some((pre) => p.startsWith(pre))) return false;
  if (p.includes("..") || p.includes("//") || p.includes("\\") || p.includes(":")) return false;
  return /^[\w.\-/%+ ]+$/.test(p) && !p.endsWith("/");
}

async function mirrorOk(path: string): Promise<boolean> {
  const hit = health.get(path);
  const now = Date.now();
  if (hit && now - hit.ts < (hit.ok ? OK_TTL_MS : BAD_TTL_MS)) return hit.ok;
  let ok = false;
  try {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), PROBE_TIMEOUT_MS);
    const r = await fetch(`${R2_PUBLIC_ROOT}/${path}`, {
      method: "HEAD",
      headers: { "User-Agent": PROBE_UA },
      signal: ctl.signal,
      cache: "no-store",
    });
    clearTimeout(timer);
    ok = r.ok;
  } catch {
    ok = false;
  }
  health.set(path, { ok, ts: now });
  return ok;
}

export async function GET(req: NextRequest, { params }: { params: { path: string[] } }) {
  const path = (params.path || []).map(decodeURIComponent).join("/");
  if (!validPath(path)) {
    return NextResponse.json({ error: "bad path" }, { status: 400 });
  }
  // 回落地址不能用 req.nextUrl.origin：nginx 反代后它是 localhost:3000，
  // 要从转发头还原公网域名（nginx 已设 Host / X-Forwarded-Proto）。
  const proto = req.headers.get("x-forwarded-proto") || "https";
  const host = req.headers.get("host") || "bd2026.cc";
  const target = (await mirrorOk(path))
    ? `${R2_PUBLIC_ROOT}/${path}`
    : `${proto}://${host}/${path}`;
  // 302（非 301）：镜像健康是动态判定，绝不让浏览器缓存住某一次的选路。
  return NextResponse.redirect(target, { status: 302, headers: { "Cache-Control": "no-store" } });
}

export const HEAD = GET;
