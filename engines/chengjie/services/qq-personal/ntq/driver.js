/**
 * QQ 驱动接口契约（冻结）——外壳 server.js 只依赖本接口，不感知底层是「注入真实 QQ」
 * 还是「离线 mock」。真实驱动 qqnt-driver.js（GPL-2.0 移植，注入本机 QQ 内核驱动收发）
 * 与 mock-driver.js 都实现同一套方法名与返回 shape。
 *
 * 选择：环境变量 QQ_DRIVER = "qqnt"（真实注入，默认）| "mock"（离线自测/CI）。
 * 真实驱动尚未接入（等去风险验证）时，qqnt 回退到 mock 并在 /health 里如实标注
 * driver="mock" + driver_reason，绝不假装能收发。
 *
 * ── 方法契约 ──────────────────────────────────────────────────────────────
 * 所有方法 async、绝不抛（失败返回 {ok:false, error, retcode?}）。message 段沿用
 * Milky IncomingSegment/OutgoingSegment 形状（text/mention/mention_all/reply/image/
 * record/video/file/forward）。
 *
 *   info()                         -> { qq_installed, qq_version, driver, driver_reason }
 *   loginState()                   -> "logged_out" | "qr_wait" | "scanned" | "logged_in"
 *   selfInfo()                     -> { uin, nickname } | null
 *   startLogin()                   -> { ok, qr_png_base64, qr_url, expire_sec }  拉一张新登录码
 *   quickLoginList()               -> [{ uin, nickname }]                       本地可免扫码重登的号
 *   quickLogin(uin)                -> { ok }
 *   logout()                       -> { ok }
 *   getImplInfo()                  -> { impl_name, impl_version, qq_version, milky_version }
 *   sendPrivate(userId, segs)      -> { ok, message_seq }
 *   sendGroup(groupId, segs)       -> { ok, message_seq }
 *   recallPrivate(userId, seq)     -> { ok }
 *   recallGroup(groupId, seq)      -> { ok }
 *   markRead(scene, peerId, seq)   -> { ok }
 *   uploadPrivateFile(userId, uri, name) -> { ok, file_id }
 *   uploadGroupFile(groupId, uri, name)  -> { ok, file_id }
 *   privateFileUrl(userId, fileId, hash) -> { ok, download_url }
 *   groupFileUrl(groupId, fileId)  -> { ok, download_url }
 *   kickGroupMember(groupId, userId, reject) -> { ok }
 *   setGroupName(groupId, name)    -> { ok }
 *   acceptFriend(initiatorUid, filtered) -> { ok }
 *
 * 事件：驱动通过 onEvent(cb) 注册回调，cb(ev) 收 Milky Event（{time, self_id,
 * event_type, data}）。外壳据此推 WS /event 与回推 Python ingest。
 */

export async function createDriver(opts = {}) {
  const kind = String(opts.kind || process.env.QQ_DRIVER || "qqnt").toLowerCase();
  if (kind === "mock") {
    const { createMockDriver } = await import("./mock-driver.js");
    return createMockDriver(opts);
  }
  // 真实注入驱动：接入前回退 mock（如实标注），去风险验证通过后 qqnt-driver.js 落地即自动生效
  try {
    const mod = await import("./qqnt-driver.js");
    return await mod.createQqntDriver(opts);
  } catch (e) {
    const { createMockDriver } = await import("./mock-driver.js");
    const d = await createMockDriver(opts);
    d.__driverReason = `qqnt-driver unavailable: ${String((e && e.message) || e).slice(0, 120)}`;
    return d;
  }
}
