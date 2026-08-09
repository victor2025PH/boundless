// 进程内固定窗口限速：客户端授权面（activate/refresh/telemetry/revocations-push）无鉴权，
// 必须有基本防刷（《授权在线刷新_对接说明》§7 安全红线）。语义与厂商侧参考实现
// license_server.py _rate_check 一致：同 key 窗口内至多 limit 次，跨窗口自然重置，
// 表超量时清掉非当前窗口的键防长跑无界。nodejs runtime 常驻进程内存即够用。
const hits = new Map<string, number>();

export function rateOk(key: string, limit = 30, windowSec = 60): boolean {
  if (limit <= 0) return true;
  const win = Math.floor(Date.now() / 1000 / windowSec);
  const k = `${win}:${key}`;
  const n = (hits.get(k) || 0) + 1;
  hits.set(k, n);
  if (hits.size > 8192) {
    const pref = `${win}:`;
    for (const kk of hits.keys()) if (!kk.startsWith(pref)) hits.delete(kk);
  }
  return n <= limit;
}

export function clientIp(req: { headers: { get(n: string): string | null } }): string {
  const xf = req.headers.get("x-forwarded-for") || "";
  return (xf.split(",")[0] || "").trim() || req.headers.get("x-real-ip") || "unknown";
}
