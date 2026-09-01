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
