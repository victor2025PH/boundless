// 获客归因：落地 URL 上的 utm_* 参数捕获与持久化。
// - 会话级（sessionStorage）：本次访问的来源，随埋点事件上报，用于「来源 → 留资」会话转化统计；
// - 首触级（localStorage，30 天）：访客第一次是从哪来的，留资时若本次会话无 UTM 则回退首触，
//   避免「频道点进来看过、隔天直达回来留资」把功劳错记给直达。

export interface Utm {
  s: string; // utm_source
  m: string; // utm_medium
  c: string; // utm_campaign
}

const SESSION_KEY = "ml_utm";
const FIRST_KEY = "ml_utm_first";
const FIRST_TTL_MS = 30 * 24 * 3600 * 1000;

let captured = false;

function sanitize(v: string | null, max: number): string {
  return (v ?? "").trim().slice(0, max);
}

/** 幂等：首次调用时从当前 URL 捕获 utm_*，写入会话与首触存储。 */
export function ensureCaptured(): void {
  if (captured || typeof window === "undefined") return;
  captured = true;
  try {
    const q = new URLSearchParams(window.location.search);
    const s = sanitize(q.get("utm_source"), 40);
    if (!s) return;
    const utm: Utm = {
      s,
      m: sanitize(q.get("utm_medium"), 40),
      c: sanitize(q.get("utm_campaign"), 60),
    };
    try {
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(utm));
    } catch {
      /* ignore */
    }
    try {
      const raw = localStorage.getItem(FIRST_KEY);
      const prev = raw ? (JSON.parse(raw) as { ts?: number }) : null;
      const fresh = prev?.ts && Date.now() - prev.ts < FIRST_TTL_MS;
      if (!fresh) localStorage.setItem(FIRST_KEY, JSON.stringify({ ...utm, ts: Date.now() }));
    } catch {
      /* ignore */
    }
  } catch {
    /* ignore */
  }
}

/** 本次会话的来源（无则 null）。 */
export function getSessionUtm(): Utm | null {
  if (typeof window === "undefined") return null;
  ensureCaptured();
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const u = JSON.parse(raw) as Utm;
    return u?.s ? { s: u.s, m: u.m ?? "", c: u.c ?? "" } : null;
  } catch {
    return null;
  }
}

/** 留资归因：优先本次会话，回退 30 天内首触。返回紧凑串 "source/medium/campaign"。 */
export function getLeadUtm(): string {
  const sess = getSessionUtm();
  if (sess) return compact(sess);
  if (typeof window === "undefined") return "";
  try {
    const raw = localStorage.getItem(FIRST_KEY);
    if (!raw) return "";
    const u = JSON.parse(raw) as Utm & { ts?: number };
    if (!u?.s || !u.ts || Date.now() - u.ts >= FIRST_TTL_MS) return "";
    return compact(u) + "(first)";
  } catch {
    return "";
  }
}

function compact(u: Utm): string {
  return [u.s, u.m || "-", u.c || "-"].join("/");
}

// ── 广告来源码 src（Telegram 广告 → @ChatX_bot /start=<src> → 下载页 ?src=<src>）──
// 与 utm 分开存：utm 走会话归因，src 是要原样跟到下载点击 / 安装包分流入口的短码。
// 30 天首触保留：广告点进来先看看、隔天直达回来下载，功劳仍记给那条广告。
const SRC_KEY = "ml_src";
const SRC_TTL_MS = 30 * 24 * 3600 * 1000;
const SRC_RE = /^[A-Za-z0-9_-]{1,48}$/;

export function isValidSrc(v: string | null | undefined): v is string {
  return typeof v === "string" && SRC_RE.test(v);
}

/** 把来源码落盘（小程序从 start_param / initData 回源拿到 src 时也用），非法值忽略。 */
export function rememberSrc(v: string): void {
  if (typeof window === "undefined" || !isValidSrc(v)) return;
  try {
    localStorage.setItem(SRC_KEY, JSON.stringify({ v, ts: Date.now() }));
  } catch {
    /* ignore */
  }
}

/** 当前访客的广告来源码（URL ?src= 优先并落盘；否则读 30 天内存储；无则 ""）。 */
export function getSrc(): string {
  return readParam("src", SRC_KEY, SRC_RE);
}

// ── bot 用户标识 tg（@ChatX_bot 深链 ?tg=<uid> → 下载页 → /dl 用户级回执）──
// 与 src 同样 30 天保留、同样只是原样带到安装包分流入口；服务端用它判断「这个 bot 用户下载了没」。
const TG_KEY = "ml_tg";
const TG_RE = /^\d{1,20}$/;

export function getTgUid(): string {
  return readParam("tg", TG_KEY, TG_RE);
}

function readParam(name: string, key: string, re: RegExp): string {
  if (typeof window === "undefined") return "";
  try {
    const fromUrl = (new URLSearchParams(window.location.search).get(name) ?? "").trim();
    if (re.test(fromUrl)) {
      try {
        localStorage.setItem(key, JSON.stringify({ v: fromUrl, ts: Date.now() }));
      } catch {
        /* ignore */
      }
      return fromUrl;
    }
    const raw = localStorage.getItem(key);
    if (!raw) return "";
    const rec = JSON.parse(raw) as { v?: string; ts?: number };
    if (!rec?.v || !rec.ts || Date.now() - rec.ts >= SRC_TTL_MS || !re.test(rec.v)) return "";
    return rec.v;
  } catch {
    return "";
  }
}
