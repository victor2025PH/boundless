/** 秒 → 「12s / 3m / 2m5s」，日报和 admin 卡片共用（纯函数，客户端可引）。 */
export function fmtDur(sec: number): string {
  if (sec < 60) return `${sec}s`;
  const m = Math.floor(sec / 60);
  const s = sec % 60;
  return s ? `${m}m${s}s` : `${m}m`;
}
