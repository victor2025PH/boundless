/**
 * Bad MAC（libsignal 解密对端消息失败）自愈——零依赖纯函数 + 进程内计数器（J-6 C）。
 *
 * 背景（J-3「D 观测」）：skuio 09-05 每份报告尾 5–6 条 `Session error:Error: Bad MAC`
 * （session_cipher.js doDecryptWhisperMessage），同一对端跨 3 小时不停；日志里紧跟的
 * `WA app-state resync requested` 其实是 open 时的**通讯录 app-state 补拉**（resyncContacts），
 * 与 Signal 会话无关——此前对 Bad MAC **没有任何自愈**。Baileys 对每条解密失败会发
 * retry receipt（对端重发），但对端会话记录本身已坏（ratchet 链错位）时重发照样 Bad MAC，
 * 只会无限刷日志。Baileys 常规处置＝删该对端 session 记录，让下一条消息以 prekey 重建会话。
 *
 * 不变量：
 *  - 计数按 (loginId, 对端 jid) 独立；同对端**连续** ≥ threshold（默认 3）次才删，任一条
 *    解密成功即清零（偶发一两条 Bad MAC 是正常的乱序/重放，删会话反而多余）。
 *  - 删过一次后进冷却（默认 30min）：冷却内再连击不重删（删了还坏＝对端设备侧问题，
 *    重删只是无谓丢会话），只计数。
 *  - 绝不触碰全部会话 / 绝不 resync 全部 app-state（会打断在线号）。
 *  - jid 打码输出（日志/health），不落明文号码。
 *
 * server.js 只做副作用（调 signalRepository.deleteSession），决策全部在这里可被 node --test。
 */

/** WebMessageInfo.StubType.CIPHERTEXT（Baileys 把解密失败的消息升为该 stub，params[0]=错误文案）。 */
export const STUB_CIPHERTEXT = 2;

export const BAD_MAC_DEFAULT = Object.freeze({ threshold: 3, cooldownMs: 30 * 60 * 1000 });

/** 读 env：WA_BAD_MAC_THRESHOLD（连击阈值，0=关闭自愈只计数）/ WA_BAD_MAC_COOLDOWN_MIN。 */
export function badMacConfig(env = {}) {
  const t = Number(env.WA_BAD_MAC_THRESHOLD);
  const c = Number(env.WA_BAD_MAC_COOLDOWN_MIN);
  return {
    threshold: Number.isFinite(t) && t >= 0 ? Math.floor(t) : BAD_MAC_DEFAULT.threshold,
    cooldownMs: Number.isFinite(c) && c >= 0 ? Math.floor(c) * 60 * 1000 : BAD_MAC_DEFAULT.cooldownMs,
  };
}

const _BAD_MAC_RE = /bad mac/i;

/** 是否是「解密失败」stub（CIPHERTEXT + 文案含 Bad MAC）。其他 CIPHERTEXT（缺 key / 无内容）不算。 */
export function isBadMacStub(msg) {
  if (!msg || Number(msg.messageStubType) !== STUB_CIPHERTEXT) return false;
  const p = Array.isArray(msg.messageStubParameters) ? msg.messageStubParameters : [];
  return _BAD_MAC_RE.test(String(p[0] || ""));
}

/** 是否是「解密成功」的对端消息（有 message 体、非 fromMe）——用于清零连击。 */
export function isPeerPlaintext(msg) {
  if (!msg || !msg.message || Number(msg.messageStubType) === STUB_CIPHERTEXT) return false;
  return !(msg.key && msg.key.fromMe);
}

/** 收件箱占位正文：CIPHERTEXT stub 没有 message 体，不能当客户原话喂自动回复。 */
export const DECRYPT_FAIL_PLACEHOLDER = "[无法解密的消息 · 会话将自动重建]";

/** Bad MAC stub → 落库字段。非 stub 返 null。backfill_source=decrypt_fail 跳过自动起草。 */
export function decryptFailIngestFields(msg) {
  if (!isBadMacStub(msg)) return null;
  return {
    text: DECRYPT_FAIL_PLACEHOLDER,
    decrypt_fail: true,
  };
}

const _USER_JID_RE = /^[^@\s]+@(s\.whatsapp\.net|lid|hosted)$/;

/**
 * 对端 Signal 会话候选 jid：群消息取 participant（发送者），私聊取 remoteJid；
 * LID 迁移期两套地址（*Alt）并存，一并给出——deleteSession 对不存在的地址是 no-op。
 * 只回用户 jid（@s.whatsapp.net / @lid / @hosted），群 jid（@g.us）与广播不回。
 */
export function peerSessionJids(msg) {
  const k = (msg && msg.key) || {};
  const cands = [
    k.participant, k.participantAlt, msg && msg.participant,
    k.remoteJid, k.remoteJidAlt,
  ];
  const out = [];
  for (const c of cands) {
    const s = String(c || "").trim();
    if (s && _USER_JID_RE.test(s) && !out.includes(s)) out.push(s);
  }
  // 群消息 remoteJid 是 @g.us 天然被正则剔除，只剩 participant；私聊 participant 为空，
  // 只剩 remoteJid（及其 LID/PN alt）。计数键取 out[0]（首个候选＝发送者主地址）。
  return out;
}

/** 把登录快照编成 detail 标签 `[bm:total=T,peers=P,heals=H,active=A]`（Python 侧 parse_bad_mac 解）。 */
export function badMacDetailTag(snap) {
  const s = snap || {};
  const n = (k) => Math.max(0, Number(s[k]) || 0);
  return `[bm:total=${n("total")},peers=${n("peers")},heals=${n("heals")},active=${n("active")}]`;
}

/** 对端 jid 打码：保留域与末 4 位（日志/health 可关联同一对端，不落整号）。 */
export function maskJid(jid) {
  const s = String(jid || "");
  const at = s.indexOf("@");
  const user = at >= 0 ? s.slice(0, at) : s;
  const dom = at >= 0 ? s.slice(at) : "";
  if (user.length <= 4) return "*".repeat(user.length) + dom;
  return "*".repeat(Math.max(0, user.length - 4)) + user.slice(-4) + dom;
}

/** 按 (loginId, jid) 的连击计数器。record() 返回本次是否应删会话。 */
export class BadMacTracker {
  constructor(cfg = BAD_MAC_DEFAULT) {
    this.threshold = Math.max(0, Number(cfg.threshold) || 0);
    this.cooldownMs = Math.max(0, Number(cfg.cooldownMs) || 0);
    this._peers = new Map(); // `${loginId}|${jid}` → {loginId, jid, streak, total, lastTs, healedTs, heals}
    this.total = 0;      // 进程累计 Bad MAC 条数
    this.healTotal = 0;  // 进程累计删会话次数
  }

  _key(loginId, jid) { return `${loginId}|${jid}`; }

  /**
   * 登记一次 Bad MAC。返回 {streak, heal}：heal=true 表示连击达阈且不在冷却 → 调用方应删会话
   * 并随后调 markHealed()。threshold=0 永不 heal（只观测）。
   */
  record(loginId, jid, now = Date.now()) {
    const key = this._key(loginId, jid);
    let p = this._peers.get(key);
    if (!p) {
      p = { loginId, jid, streak: 0, total: 0, lastTs: 0, healedTs: 0, heals: 0 };
      this._peers.set(key, p);
    }
    p.streak += 1;
    p.total += 1;
    p.lastTs = now;
    this.total += 1;
    const inCooldown = p.healedTs > 0 && (now - p.healedTs) < this.cooldownMs;
    const heal = this.threshold > 0 && p.streak >= this.threshold && !inCooldown;
    return { streak: p.streak, total: p.total, heal, inCooldown };
  }

  /** 删会话后登记：连击清零、进冷却。 */
  markHealed(loginId, jid, now = Date.now()) {
    const p = this._peers.get(this._key(loginId, jid));
    if (!p) return;
    p.healedTs = now;
    p.heals += 1;
    p.streak = 0;
    this.healTotal += 1;
  }

  /** 该对端来了一条解密成功的消息 → 连击清零（保留 total/heals 供观测）。 */
  noteOk(loginId, jid) {
    const p = this._peers.get(this._key(loginId, jid));
    if (p) p.streak = 0;
  }

  /** 登出/登录换代：该 loginId 的全部对端状态清掉（会话文件已随之作废）。 */
  clear(loginId) {
    for (const [k, p] of this._peers.entries()) if (p.loginId === loginId) this._peers.delete(k);
  }

  /** 单个登录的累计快照（给 postStatus 带给 Python：total/peers/heals/active）。 */
  snapshotFor(loginId) {
    let total = 0, peers = 0, heals = 0, active = 0;
    for (const p of this._peers.values()) {
      if (p.loginId !== loginId) continue;
      peers += 1; total += p.total; heals += p.heals;
      if (p.streak > 0) active += 1;
    }
    return { total, peers, heals, active };
  }

  /** /health 读数：bad_mac_total / bad_mac_peers（有过 Bad MAC 的对端数）/ 当前连击中的对端 Top。 */
  snapshot(limit = 5) {
    const active = [];
    for (const p of this._peers.values()) {
      if (p.streak > 0) active.push(p);
    }
    active.sort((a, b) => b.streak - a.streak || b.lastTs - a.lastTs);
    return {
      bad_mac_total: this.total,
      bad_mac_peers: this._peers.size,
      bad_mac_active: active.length,
      heals: this.healTotal,
      threshold: this.threshold,
      cooldown_min: Math.round(this.cooldownMs / 60000),
      top: active.slice(0, limit).map((p) => ({
        login: p.loginId, peer: maskJid(p.jid), streak: p.streak, total: p.total, heals: p.heals,
      })),
    };
  }
}
