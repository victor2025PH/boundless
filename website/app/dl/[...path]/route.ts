import { NextRequest, NextResponse } from "next/server";
import { createReadStream } from "fs";
import { stat } from "fs/promises";
import path from "path";
import { Readable } from "stream";
import { R2_PUBLIC_ROOT, DL_PREFIXES } from "@/lib/mirror";
import { clientIp } from "@/lib/client-ip";
import { appendDownload, buildRecord, gate, isInstallerPath, type GateDecision } from "@/lib/download-ledger";
import { trackServer } from "@/lib/tg-events";

/**
 * /dl/<path> 安装包智能分流（下载提速 P0 · 2026-08-08）+ 下载台账与扒包防护（2026-09-10）。
 *
 * 逻辑：HEAD 探测 R2 镜像上的同名文件（结果按路径缓存，正向 5 分钟 / 负向 1 分钟）
 *   → 镜像健康：302 到 R2（下载流量走 Cloudflare 边缘，$0 出流，不占主站 15Mbps 管道）
 *   → 镜像缺文件/超时：文件在本站 public/ 下则**流式直出**（支持 Range，electron-updater 差量
 *     下载靠它）；不在 public/（如 nginx alias 的 /releases）才 302 回本站同路径。
 * 未来发新版即使忘了同步镜像，该文件也只是自动回落主站，绝不 404。
 *
 * 2026-09-10 起 middleware 把 GET /downloads/**.exe 改写到这里，所有安装包下载在此单点：
 *   台账 lib/download-ledger.ts（IP / UA / 产品 / 版本 / 渠道 / 走向），爬虫 403、
 *   按 IP 不同文件数限流 429、横扫多产品打 sweep 标。回落之所以改成直出而非 302，
 *   正是因为 302 回 /downloads 会再次进 middleware 形成循环。
 *
 * 安全：路径白名单前缀 + 拒绝 ".."/协议注入；只处理 GET/HEAD；直出前 resolve 后必须仍在 public/ 内。
 *
 * 归因：`?src=<广告来源码>`（@ChatX_bot 深链带到下载页、下载页带到这里）服务端落一条
 * `download_redirect` 事件（events.jsonl），与前端埋点互补：无 JS / 拦截器 / 直接拷链接也能计到安装包请求。
 */
export const dynamic = "force-dynamic";
export const runtime = "nodejs";

const OK_TTL_MS = 5 * 60_000;
const BAD_TTL_MS = 60_000;
const PROBE_TIMEOUT_MS = 3500;
// r2.dev 会 403 部分非浏览器 UA（Python-urllib 实锤）——探测必须带自定义 UA。
const PROBE_UA = "BD-dl-router/1.0";
const PUBLIC_DIR = path.resolve(process.env.DOWNLOADS_PUBLIC_DIR || path.join(process.cwd(), "public"));

const health = new Map<string, { ok: boolean; ts: number }>();
const SRC_RE = /^[A-Za-z0-9_-]{1,48}$/;

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

/** 本站 public/ 下的文件绝对路径；不存在或逃出 public/ 返回 null。 */
async function localFile(p: string): Promise<{ file: string; size: number; mtime: Date } | null> {
  const file = path.resolve(PUBLIC_DIR, p);
  if (!file.startsWith(PUBLIC_DIR + path.sep)) return null;
  try {
    const st = await stat(file);
    if (!st.isFile()) return null;
    return { file, size: st.size, mtime: st.mtime };
  } catch {
    return null;
  }
}

/** 解析单段 Range（bytes=a-b / a- / -n）；无 Range 返 null，非法返 "bad"。 */
function parseRange(h: string | null, size: number): { start: number; end: number } | null | "bad" {
  if (!h) return null;
  const m = /^bytes=(\d*)-(\d*)$/.exec(h.trim());
  if (!m) return "bad";
  let start: number, end: number;
  if (m[1] === "" && m[2] === "") return "bad";
  if (m[1] === "") {
    const n = Number(m[2]);
    if (!n) return "bad";
    start = Math.max(0, size - n);
    end = size - 1;
  } else {
    start = Number(m[1]);
    end = m[2] === "" ? size - 1 : Math.min(Number(m[2]), size - 1);
  }
  if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start >= size) return "bad";
  return { start, end };
}

function streamLocal(req: NextRequest, f: { file: string; size: number; mtime: Date }, head: boolean): NextResponse {
  const name = path.basename(f.file);
  const base: Record<string, string> = {
    "Content-Type": "application/octet-stream",
    "Content-Disposition": `attachment; filename="${name}"`,
    "Accept-Ranges": "bytes",
    "Last-Modified": f.mtime.toUTCString(),
    "Cache-Control": "public, max-age=3600",
    "X-Robots-Tag": "noindex, nofollow",
    "X-DL-Source": "local",
  };
  const range = parseRange(req.headers.get("range"), f.size);
  if (range === "bad") {
    return new NextResponse(null, { status: 416, headers: { ...base, "Content-Range": `bytes */${f.size}` } });
  }
  const start = range ? range.start : 0;
  const end = range ? range.end : f.size - 1;
  const headers = {
    ...base,
    "Content-Length": String(end - start + 1),
    ...(range ? { "Content-Range": `bytes ${start}-${end}/${f.size}` } : {}),
  };
  const status = range ? 206 : 200;
  if (head) return new NextResponse(null, { status, headers });
  const body = Readable.toWeb(createReadStream(f.file, { start, end })) as unknown as ReadableStream;
  return new NextResponse(body, { status, headers });
}

async function handle(req: NextRequest, params: { path: string[] }, head: boolean) {
  const p = (params.path || []).map(decodeURIComponent).join("/");
  if (!validPath(p)) {
    return NextResponse.json({ error: "bad path" }, { status: 400 });
  }
  const ip = clientIp(req);
  const ua = req.headers.get("user-agent") || "";
  const ref = req.headers.get("referer") || "";
  const installer = isInstallerPath(p);

  // HEAD 不算下载：不设闸、不记账（发版脚本/监控探测大量用 HEAD）。
  const decision: GateDecision = head
    ? { allow: true, status: 200, flags: [], first: false }
    : gate({ ip, ua, path: p, hasRange: !!req.headers.get("range") });

  if (!decision.allow) {
    void appendDownload(buildRecord({ ip, ua, ref, path: p }, "blocked", decision.status, decision.flags, decision.reason));
    return new NextResponse(decision.status === 429 ? "Too many downloads, try later." : "Forbidden", {
      status: decision.status,
      headers: { "Content-Type": "text/plain", "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow" },
    });
  }

  const src = req.nextUrl.searchParams.get("src") ?? "";
  if (!head && !req.headers.get("range") && SRC_RE.test(src)) {
    await trackServer("download_redirect", { src, file: p.split("/").pop() ?? p, installer }, "/dl");
  }

  // HEAD 且本地有文件：直接答 200 + Content-Length，不 302 去 R2。HEAD 不传字节，本地答最便宜；
  // release_watchdog / publish 脚本靠 HEAD 的状态码与 Content-Length 校验发布物，302 会让它们误报。
  if (head) {
    const localHead = await localFile(p);
    if (localHead) return streamLocal(req, localHead, true);
  }

  if (await mirrorOk(p)) {
    if (!head && installer && decision.first) {
      void appendDownload(buildRecord({ ip, ua, ref, path: p }, "r2", 302, decision.flags));
    }
    // 302（非 301）：镜像健康是动态判定，绝不让浏览器缓存住某一次的选路。
    return NextResponse.redirect(`${R2_PUBLIC_ROOT}/${p}`, {
      status: 302,
      headers: { "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow" },
    });
  }

  const local = await localFile(p);
  if (local) {
    if (!head && installer && decision.first) {
      void appendDownload(buildRecord({ ip, ua, ref, path: p }, "local", 200, decision.flags));
    }
    return streamLocal(req, local, head);
  }

  // 不在 public/（nginx alias 的 /releases 等）：回落地址不能用 req.nextUrl.origin——
  // nginx 反代后它是 localhost:3000，要从转发头还原公网域名（nginx 已设 Host / X-Forwarded-Proto）。
  const proto = req.headers.get("x-forwarded-proto") || "https";
  const host = req.headers.get("host") || "bd2026.cc";
  if (!head && installer && decision.first) {
    void appendDownload(buildRecord({ ip, ua, ref, path: p }, "local", 302, decision.flags));
  }
  return NextResponse.redirect(`${proto}://${host}/${p}`, {
    status: 302,
    headers: { "Cache-Control": "no-store", "X-Robots-Tag": "noindex, nofollow" },
  });
}

export async function GET(req: NextRequest, ctx: { params: { path: string[] } }) {
  return handle(req, ctx.params, false);
}

export async function HEAD(req: NextRequest, ctx: { params: { path: string[] } }) {
  return handle(req, ctx.params, true);
}
