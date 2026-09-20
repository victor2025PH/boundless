/**
 * 「二维码已被扫描」判定——零依赖纯函数（与 close-policy.js 同哲学，node --test 可直接单测）。
 *
 * 背景（2026-08-30 P0）：WhatsApp/Baileys 没有原生的「已扫描」中间态，connection.update
 * 只有 pending → open。于是坐席扫完码到登录成功之间前端零反馈，超时还会静默换新码
 * （_connectScanSeen 恒 false → expired 走自动换码而非「扫码后中断」报错），主观表现为
 * 「扫了没反应，码还自己变」。本函数从两个信号推断「手机已扫码、配对握手已开始」，
 * server.js 据此把 entry.status 推进到 "scanned"，前端即显示「已检测到扫码，正在登录…」。
 *
 * 两个信号（任一成立即判定）：
 *  1. connection.update 携 isNewLogin=true —— 新配对握手开始的最可靠信号（随后 515 重启→open）。
 *  2. 兜底：凭据里 me.id 首次出现，且这是一个「展示过二维码」的扫码会话（entry.qrImage 非空）。
 *     用 qrImage 作闸门排除「磁盘恢复的老会话重连」——它无 QR、me 早已在凭据里，绝不能误判扫码。
 *
 * 仅在当前处于 "pending"（等待扫码）时才推进，避免覆盖 authorized/reconnecting 等既有态。
 *
 * @param {{status?: string, qrImage?: string}|null} entry 会话条目（只读 status/qrImage）
 * @param {{isNewLogin?: boolean, meId?: string}} [sig] 触发信号
 * @returns {boolean} true = 应把状态置为 "scanned"
 */
export function shouldMarkScanned(entry, sig) {
  if (!entry || entry.status !== "pending") return false;
  const s = sig || {};
  if (s.isNewLogin) return true;
  if (s.meId && entry.qrImage) return true;
  return false;
}

/** 配对期 DNS 提示阈值：配对已耗时 ≥60s 且期间至少 1 次 dns 类 close → 给坐席可见提示。 */
export const PAIRING_DNS_HINT_MS = 60 * 1000;

/**
 * 配对时延观测（J-6 B / #181，纯函数）：从 entry 上的配对起点与 DNS 失败计数推导
 * 「配对已耗时多少 / 要不要给坐席 DNS 提示」。
 *
 * 背景：#181 实录配对 4-5 分钟，日志里全是 `ENOTFOUND web.whatsapp.com` 短退避——不是
 * 用户扫码慢，是本机解析不到 WhatsApp 域名在反复重连。前端只看得到「正在登录…」，
 * 坐席以为码坏了反复换码。这里给出 hint_code="dns_retry"，前端按既有 hint_code 通道渲染。
 *
 * @param {{pairingStartedAt?: number, pairingDnsFails?: number, pairingMs?: number}|null} entry
 * @param {number} [now] 注入时钟（缺省 Date.now()）
 * @returns {{pairing_ms: number, pairing_dns_fails: number, hint_code: string}}
 *   pairing_ms：配对进行中＝已耗时；已完成（entry.pairingMs 已定格）＝最终值；从未配对＝0。
 *   hint_code：满足阈值为 "dns_retry"，否则 ""。
 */
export function pairingObservation(entry, now) {
  const e = entry || {};
  const t = Number.isFinite(now) ? now : Date.now();
  const fails = Math.max(0, Number(e.pairingDnsFails) || 0);
  let ms = 0;
  if (Number.isFinite(e.pairingMs) && e.pairingMs > 0) ms = Math.floor(e.pairingMs);
  else if (Number(e.pairingStartedAt) > 0) ms = Math.max(0, Math.floor(t - Number(e.pairingStartedAt)));
  const inProgress = !(Number.isFinite(e.pairingMs) && e.pairingMs > 0) && Number(e.pairingStartedAt) > 0;
  const hint = inProgress && fails > 0 && ms >= PAIRING_DNS_HINT_MS ? "dns_retry" : "";
  return { pairing_ms: ms, pairing_dns_fails: fails, hint_code: hint };
}
