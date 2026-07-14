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
