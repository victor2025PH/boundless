/**
 * IP 归属地（下载台账控制台用）：ip-api.com 批量接口 + 本地 JSON 文件缓存（永不过期——
 * 归属地漂移对运营判断无关紧要，省下的是 45 次/分钟的免费配额）。
 * 只在控制台读页时惰性查询未缓存 IP，下载主链路绝不等它。失败静默返回空。
 */
import { mkdir, readFile, writeFile } from "fs/promises";
import path from "path";
import { DATA_DIR } from "./data-dir";

export interface IpGeo {
  cc: string;
  country: string;
  region: string;
  city: string;
  isp: string;
  mobile: boolean;
  proxy: boolean;
  hosting: boolean;
}

const CACHE_FILE = process.env.IP_GEO_CACHE || path.join(DATA_DIR, "ip-geo-cache.json");
const BATCH_URL = "http://ip-api.com/batch?fields=query,status,countryCode,country,regionName,city,isp,mobile,proxy,hosting";
const TIMEOUT_MS = 6000;
const MAX_LOOKUP_PER_CALL = 200; // 2 个批次，够一页台账

let mem: Record<string, IpGeo> | null = null;

function isPublicIp(ip: string): boolean {
  if (!ip || ip === "unknown") return false;
  if (/^(10\.|127\.|192\.168\.|169\.254\.|0\.)/.test(ip)) return false;
  if (/^172\.(1[6-9]|2\d|3[01])\./.test(ip)) return false;
  if (ip === "::1" || /^f[cd]/i.test(ip)) return false;
  return true;
}

async function load(): Promise<Record<string, IpGeo>> {
  if (mem) return mem;
  try {
    mem = JSON.parse(await readFile(CACHE_FILE, "utf8")) as Record<string, IpGeo>;
  } catch {
    mem = {};
  }
  return mem!;
}

async function persist(cache: Record<string, IpGeo>): Promise<void> {
  try {
    await mkdir(path.dirname(CACHE_FILE), { recursive: true });
    await writeFile(CACHE_FILE, JSON.stringify(cache));
  } catch {
    /* ignore */
  }
}

/** 批量取归属地；返回 ip → geo（查不到的键缺失）。 */
export async function geoLookup(ips: Iterable<string>): Promise<Record<string, IpGeo>> {
  const cache = await load();
  const want = [...new Set([...ips])].filter((ip) => isPublicIp(ip) && !cache[ip]).slice(0, MAX_LOOKUP_PER_CALL);
  let dirty = false;
  for (let i = 0; i < want.length; i += 100) {
    const chunk = want.slice(i, i + 100);
    try {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), TIMEOUT_MS);
      const r = await fetch(BATCH_URL, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(chunk),
        signal: ctl.signal,
        cache: "no-store",
      });
      clearTimeout(timer);
      if (!r.ok) break;
      const arr = (await r.json()) as Array<Record<string, unknown>>;
      for (const o of arr) {
        if (o.status !== "success" || typeof o.query !== "string") continue;
        cache[o.query] = {
          cc: String(o.countryCode || ""),
          country: String(o.country || ""),
          region: String(o.regionName || ""),
          city: String(o.city || ""),
          isp: String(o.isp || ""),
          mobile: !!o.mobile,
          proxy: !!o.proxy,
          hosting: !!o.hosting,
        };
        dirty = true;
      }
    } catch {
      break;
    }
  }
  if (dirty) await persist(cache);
  const out: Record<string, IpGeo> = {};
  for (const ip of new Set([...ips])) if (cache[ip]) out[ip] = cache[ip];
  return out;
}

/** 一行展示：「PH · Cebu City · PLDT」+ 网络性质角标。 */
export function geoLabel(g: IpGeo | undefined): string {
  if (!g) return "—";
  const parts = [g.cc, g.city || g.region, g.isp].filter(Boolean);
  const tags = [g.hosting && "机房", g.proxy && "代理/VPN", g.mobile && "移动网"].filter(Boolean);
  return parts.join(" · ") + (tags.length ? `（${tags.join("/")}）` : "");
}
