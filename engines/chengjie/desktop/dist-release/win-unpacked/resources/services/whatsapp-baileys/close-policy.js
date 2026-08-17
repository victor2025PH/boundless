/**
 * WhatsApp 连接关闭（connection.update "close"）后的动作决策——零依赖纯函数。
 *
 * 为什么单独成文件：server.js 顶层会 app.listen（import 即拉起 HTTP 服务），测试没法安全
 * import；把「close 之后该干什么」的分支决策抽到这里，node --test 可直接单测，server.js
 * 在 close 处理器里只消费决策结果（执行副作用：改状态 / scheduleReconnect / postStatus）。
 *
 * 2026-07-22 事故背景（本决策表的直接依据）：断网 8 分钟，已授权会话 428 掉线进入重连；
 * 重连中 startLogin 建了新 entry（status="pending"），新 socket 因 DNS 失败再次 close，
 * 旧逻辑按「非 authorized → expired」终结会话——此时快重连计数才 1/5、慢重试也未武装，
 * 从此再无任何重试，也不上报 Python → 会话假死 2 小时。因此规则改为：**曾配对过的账号
 * （accountId 非空，来自持久化凭据）无论当前处于哪个中间态，close 后都必须继续重连**；
 * 只有从未配对成功的纯扫码流程失败才置 expired。
 */

// Baileys DisconnectReason 中本决策需要的两个码（WhatsApp 流协议常量，语义固定多年）。
// 刻意不 import @whiskeysockets/baileys：保持本模块零依赖，测试不必拉起重量级库；
// server.js 启动时会与权威枚举比对一次，上游罕见改值时大声告警（防静默漂移）。
export const CLOSE_CODES = Object.freeze({
  restartRequired: 515, // 配对完成后协议要求重启 socket（正常流程，非故障）
  loggedOut: 401, // 设备被解绑 / 手机端登出 → 必须人工重新配对，自动重连无意义
});

/**
 * 决定 close 事件后的动作。只读输入、无任何副作用。
 *
 * @param {object|null} entry  会话条目（只读 status / accountId 两个字段）
 * @param {number} code        lastDisconnect 的 statusCode（取不到时调用方传 0）
 * @param {boolean} isStale    事件是否来自已被替换的旧 socket（sessions 槽位已换代）
 * @returns {{action: "ignore"|"restart"|"logged_out"|"reconnect"|"expire"}}
 */
export function decideCloseAction(entry, code, isStale) {
  // 陈旧事件最高优先级：startLogin（重连/重启/手动 reconnect）会替换 sessions 里的 entry，
  // 旧 socket 迟到的 close 不得影响新会话——否则会把刚建的新连接状态改坏、触发幽灵重连，
  // 双 socket 抢同一 authDir → WhatsApp 440 connectionReplaced 冲突循环。
  if (isStale) return { action: "ignore" };
  // 配对后的协议性重启：立即重建 socket（无需退避，属正常流程）。
  if (code === CLOSE_CODES.restartRequired) return { action: "restart" };
  // 设备端解绑/登出：终态，重连只会再次被拒，必须人工重新扫码配对。
  if (code === CLOSE_CODES.loggedOut) return { action: "logged_out" };
  // 曾配对的账号（当前 authorized，或 accountId 非空＝持久化凭据里有 me）：无论此刻处于
  // pending/reconnecting 哪个中间态，掉线都继续重连。退避与放弃由 scheduleReconnect 统一
  // 负责（耗尽 → expired + postStatus + 慢重试兜底），这里绝不直接判死。
  if (entry && (entry.status === "authorized" || entry.accountId)) {
    return { action: "reconnect" };
  }
  // 纯扫码流程（从未配对成功、无凭据）失败 → expired，等用户重新发起扫码。
  return { action: "expire" };
}
