/**
 * 上游协议查询的超时包裹（2026-08-05 头像挂死事故的服务端纵深）。
 *
 * WA socket 半死时 `sock.profilePictureUrl` 这类协议 query 会**无限挂起**（Baileys
 * 无内建超时、无取消 API）——Express 响应随之挂死：Python 侧每个头像吃满客户端
 * 超时，页面上几行 WhatsApp 会话就占满浏览器同源 6 连接，整页请求饿死。
 *
 * 分层语义（勿把两层调成一样的值）：
 * - Python 层（unified_inbox_account_routes）：4s 客户端超时 + 账号级熔断——先到点，
 *   负责把坐席侧等待止血在 4s 并打开 60s 熔断窗；
 * - 本层：8s **兜底自保**——即使调用方不设超时，挂起的 query 也到点即弃，
 *   Express 连接/内存不被无限占用。8s 刻意 > 4s：熔断判定权留在 Python 层
 *   （若本层先返回，Python 收到的是 HTTP 错误而非超时，熔断不会打开）。
 *
 * 注意：Promise.race 只是放弃等待，不能取消底层 query（Baileys 不提供）；
 * 泄漏的 pending promise 随 socket 重建被释放，可接受。
 */

export const AVATAR_QUERY_TIMEOUT_MS = 8000;

export class UpstreamTimeoutError extends Error {
  constructor(ms) {
    super(`upstream query timeout after ${ms}ms`);
    this.name = "UpstreamTimeoutError";
  }
}

/** 给无超时的上游 promise 加硬超时：正常透传值/错误，超时 reject UpstreamTimeoutError。 */
export function withTimeout(promise, ms) {
  let timer = null;
  return Promise.race([
    promise,
    new Promise((_resolve, reject) => {
      timer = setTimeout(() => reject(new UpstreamTimeoutError(ms)), ms);
    }),
  ]).finally(() => clearTimeout(timer));
}
