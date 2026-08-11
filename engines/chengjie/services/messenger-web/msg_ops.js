/**
 * Messenger DOM 侧「表情 / 撤回 / 半死态提示 / 请求节奏」纯函数（零依赖、可单测）。
 *
 * 优化动机（相对「照搬 WA Baileys 事件模型」）：
 * Messenger 没有 protocolMessage / wamid——消息靠 aria-label 文本识别。
 * 若不上报稳定 msg_id，Python 的 /reaction /message-op 按 platform_msg_id 定位会
 * 永远静默失败。本模块先把「内容指纹 → 确定性 id」钉死，再谈表情与撤回。
 */

import crypto from "crypto";

/** 侧栏「你撤回了 / You unsent」类预览（本方主动撤回）。 */
export const UNSENT_PREVIEW_RE =
  /^(你撤回了|You unsent|撤回了一条消息|unsent a message)/i;

/** 线程内撤回墓碑文案（对端或本方撤回后的占位）。 */
export const UNSENT_TOMBSTONE_RE =
  /此消息已撤回|此訊息已收回|This message was unsent|You unsent a message|撤回了一条消息|unsent a message/i;

/** 反应名 → emoji（中英 UI 常见词表；未知名原样回落）。 */
const REACT_NAME = {
  // zh / zh-TW
  赞: "👍", 大心: "❤️", 爱心: "❤️", 心: "❤️",
  大笑: "😆", 笑脸: "😆", 笑: "😆",
  哇: "😮", 惊讶: "😮", 驚訝: "😮",
  哭: "😢", 难过: "😢", 難過: "😢",
  怒: "😠", 生气: "😠", 生氣: "😠",
  拥抱: "🤗", 擁抱: "🤗", 呵护: "🥰", 照顧: "🥰",
  // en（"You reacted with a laugh" 等）
  like: "👍", thumbsup: "👍", "thumbs up": "👍",
  love: "❤️", heart: "❤️",
  laugh: "😆", haha: "😆", lol: "😆",
  wow: "😮", surprised: "😮",
  sad: "😢", cry: "😢",
  angry: "😠", anger: "😠",
  care: "🥰", hug: "🤗", embrace: "🤗",
};

const EMOJI_RE = /\p{Extended_Pictographic}/u;

/**
 * 确定性平台消息 id（写入 ingest 的 msg_id → store.platform_msg_id）。
 * 不含墙钟：同一条 DOM 消息跨轮询重读必须得到同一 id，表情/撤回才能挂得上。
 */
export function synthMsgId({ chatKey, direction, tsLabel, text, mediaRef } = {}) {
  const parts = [
    String(chatKey || ""),
    String(direction || "in"),
    String(tsLabel || ""),
    String(text || "").trim(),
    String(mediaRef || ""),
  ].join("|");
  const digest = crypto.createHash("sha1").update(parts, "utf8").digest("hex").slice(0, 16);
  return `m_${digest}`;
}

/** 把反应名/emoji 归一成单个 emoji 或原串（空=撤销）。 */
export function normalizeReactionEmoji(raw) {
  const s = String(raw || "").trim();
  if (!s) return "";
  // 保留变体选择符（❤️ = ❤ + U+FE0F）；裸 \p{Extended_Pictographic} 会剥掉 FE0F
  const em = s.match(/\p{Extended_Pictographic}\uFE0F?/u);
  if (em) return em[0];
  const key = s.toLowerCase();
  if (REACT_NAME[s] || REACT_NAME[key]) return REACT_NAME[s] || REACT_NAME[key];
  // 「一个赞」「a like」类：剥冠词再查
  const bare = s.replace(/^(一个|一個|a|an)\s+/i, "").trim();
  const bareKey = bare.toLowerCase();
  if (REACT_NAME[bare] || REACT_NAME[bareKey]) {
    return REACT_NAME[bare] || REACT_NAME[bareKey];
  }
  return s.slice(0, 8);
}

/**
 * 从 aria-label 解析表情回应。
 * @returns {{ emoji: string, sender: "me"|"peer" } | null}
 */
export function parseReactionFromAria(aria) {
  const s = String(aria || "").trim();
  if (!s) return null;
  // 中文：你用👍回应了 / 你用大笑回应了
  let m = s.match(/^你用\s*(.+?)\s*回应了/);
  if (m) return { emoji: normalizeReactionEmoji(m[1]), sender: "me" };
  // 中文：对方用…回应了（「用X回应了」且不是「你」）
  m = s.match(/^(?!你)(.+?)\s*用\s*(.+?)\s*回应了/);
  if (m) return { emoji: normalizeReactionEmoji(m[2]), sender: "peer" };
  // 英文：You reacted with 👍 / You reacted with a laugh
  m = s.match(/^You reacted with(?: a)?\s+(.+)$/i);
  if (m) return { emoji: normalizeReactionEmoji(m[1]), sender: "me" };
  m = s.match(/^(.+?)\s+reacted with(?: a)?\s+(.+)$/i);
  if (m && !/^you$/i.test(m[1])) {
    return { emoji: normalizeReactionEmoji(m[2]), sender: "peer" };
  }
  return null;
}

export function isUnsentPreview(preview) {
  return UNSENT_PREVIEW_RE.test(String(preview || "").trim());
}

export function isUnsentTombstone(text) {
  return UNSENT_TOMBSTONE_RE.test(String(text || "").trim());
}

/**
 * 消息级 aria-label 锚点（识别「这是一条消息」的元素）。
 *
 * 词序断代史（2026-08-11 生产实锤，.403 账号 E2EE 线程渲染 19 行解析 0 条）：
 *   旧 en：Message sent by <sender> at <time>[: <text>]
 *   新 en：Message sent <datetime> by <sender>[: <text>]   ← FB 改版把时间挪到 sender 前
 * 「Message sent 」空格结尾同时覆盖两代；zh 旧词序保留。
 * 悬停时间戳变体「At <datetime>, <sender>: <text>」与消息元素**成对出现**，
 * 刻意不认——两个都抓会整线程双份。
 * ⚠ page.evaluate 内无法引用本常量（浏览器作用域），server.js 各 evaluate 里的
 * 字面量须与此保持同源同义（改这里必须同步那边）。
 */
export const MSG_ARIA_RE = /(消息由.*发送于|Message sent )/i;

/**
 * 解析消息 aria-label → {sender, direction, ts, text}；认不出返回 null。
 * 兼容三代词序（zh 旧 / en 旧 / en 新 2026-08），新代在后：旧串不含新词序特征，
 * 新串也不含旧词序特征（「Message sent by」子串在新格式中不出现），互不误吞。
 */
export function parseMsgAria(aria) {
  if (!aria) return null;
  const s = String(aria).replace(/^\s*Enter\s*[，,]\s*/i, "").trim();
  // 中文：消息由<sender>发送于<time>[：<text>]。注意时间内部用半角冒号(14:44)，正文分隔用
  // **全角冒号「：」** → 用第一个全角冒号切分 时间/正文（不能用半角冒号，否则会切碎 14:44）。
  // sender 用 (.*?) 允许**为空**：E2EE 私聊里对端消息常渲染成「消息由发送于<time>：<text>」
  // （发送者名缺失）——旧的 (.+?) 会整条匹配失败 → 静默丢弃所有对端消息！空发送者视为对端(in)。
  let m = s.match(/^消息由(.*?)发送于([\s\S]+)$/);
  if (m) {
    const sender = m[1].trim();
    const rest = m[2];
    const idx = rest.indexOf("：");
    const ts = (idx >= 0 ? rest.slice(0, idx) : rest).trim();
    const text = (idx >= 0 ? rest.slice(idx + 1) : "").trim();
    return { sender, direction: sender === "你" ? "out" : "in", ts, text };
  }
  // 英文旧词序：Message sent by <sender> at <time>[: <text>]（sender 同样允许空）。
  m = s.match(/^Message sent by (.*?) at (.+?):\s([\s\S]*)$/i);
  if (m) {
    const sender = m[1].trim();
    return { sender, direction: /^you$/i.test(sender) ? "out" : "in",
      ts: (m[2] || "").trim(), text: (m[3] || "").trim() };
  }
  m = s.match(/^Message sent by (.*?) at (.+)$/i); // 无正文（媒体）
  if (m) {
    const sender = m[1].trim();
    return { sender, direction: /^you$/i.test(sender) ? "out" : "in",
      ts: (m[2] || "").trim(), text: "" };
  }
  // 英文新词序（2026-08 改版）：Message sent <datetime> by <sender>[: <text>]。
  // datetime 含逗号与半角冒号（"July 24, 2026, 12:35 PM"）但不含 " by "；
  // sender 不含冒号（[^:：]）；正文用 sender 后第一个冒号切分，无冒号=无正文（媒体）。
  m = s.match(/^Message sent (.+?) by ([^:：]*?)(?:\s*[:：]\s?([\s\S]*))?$/i);
  if (m) {
    const sender = (m[2] || "").trim();
    return { sender, direction: /^(you|你)$/i.test(sender) ? "out" : "in",
      ts: (m[1] || "").trim(), text: (m[3] || "").trim() };
  }
  return null;
}

/**
 * 读线程结果 → 半死态滚动窗采样三态（2026-08-11 .403 实锤后收严语义）：
 *   "ok"   — 读到 ≥1 条真实消息＝管线健康的正面证据；
 *   "fail" — null（导航/渲染失败）或「侧栏预览是真实正文却读出空」＝管线故障证据；
 *   "skip" — 读出空数组且预览本就是加密占位＝锁死旧线程（密钥断档期积压，本设备
 *            永远解不出）或真空会话——既非健康也非故障证据，**不入窗**。
 * 不区分 skip 的教训：presence 抖动反复触发重读这类线程，8 格窗口被它们永久钉在
 * 全败 → 解析器早修好了、半死横幅还挂着（且回填的成功样本全被挤出）。
 */
export function threadReadSample(tail, { previewPlaceholder = true } = {}) {
  if (!Array.isArray(tail)) return "fail";
  if (tail.length > 0) return "ok";
  return previewPlaceholder ? "skip" : "fail";
}

/**
 * 消息请求（陌生人首讯）可操作按钮的文案词表（中英，供请求线程详情页定位）。
 * 语义：accept=接受（转正，非破坏）；decline=删除该请求（把请求移出请求箱，破坏性、
 * 对方以后消息不再自动进来直到再次发起）。**刻意不含「检举/举报 spam」**——那更危险
 * 且非坐席日常，需要时用桌面壳内嵌官方网页版原生处理。
 */
export const REQUEST_ACTION_LABELS = {
  accept: ["接受", "Accept"],
  decline: ["删除", "Delete", "拒绝", "Decline"],
};

/**
 * 归一坐席请求操作意图 → 'accept' | 'decline' | ''（非法/未知一律空串，上游据此 400）。
 * 收多种别名（approve/reject/delete）容错前端与脚本口径差异；纯函数、可单测。
 */
export function normalizeRequestAction(raw) {
  const s = String(raw || "").trim().toLowerCase();
  if (s === "accept" || s === "approve") return "accept";
  if (s === "decline" || s === "delete" || s === "reject") return "decline";
  return "";
}

/**
 * 已登录账号的「半死态」产品提示码（与 login hint_code 词汇对齐，供 /accounts 出网）。
 * e2ee_relogin = 需要在**服务器**完整重登以恢复设备密钥（cookie 快照不够）。
 */
export function classifyInboxHint({
  unread = 0, readAttempts = 0, readFails = 0,
  e2eeRatio = -1, convCount = 0,
} = {}) {
  const u = Math.max(0, Number(unread) || 0);
  const att = Math.max(0, Number(readAttempts) || 0);
  const fail = Math.max(0, Math.min(att, Number(readFails) || 0));
  const ratio = Number(e2eeRatio);
  const convs = Math.max(0, Number(convCount) || 0);
  const ratioHigh = convs >= 5 && Number.isFinite(ratio) && ratio >= 0 && ratio >= 0.6;
  const fullFail = u >= 1 && att >= 2 && fail >= att;
  const placeholderBlind = u >= 1 && att === 0 && ratioHigh;
  // 支三（P3 稳态盲区）：读取窗全败 + 大面积加密占位——失败的读取会把未读消费掉
  // （打开线程即 FB 侧标已读），稳态半死常呈现 unread=0（198 实测 4/4 全败 + 74% 占位
  // + hint 空）。占位占比作旁证：健康会话的预览是解密后正文，偶发导航超时不会命中。
  const steadyFail = att >= 2 && fail >= att && ratioHigh;
  if (fullFail || placeholderBlind || steadyFail) return "e2ee_relogin";
  return "";
}

/**
 * E2EE 恢复 PIN 归一化：只认 4-12 位纯数字（Messenger PIN 通常 6 位）。
 * 非法输入返回空串——上游据此拒收，绝不把脏值打进页面。
 */
export function normalizePin(raw) {
  const s = String(raw || "").replace(/\D+/g, "");
  return (s.length >= 4 && s.length <= 12) ? s : "";
}

/**
 * auto-PIN 尝试闸门（纯函数）：一个登录会话预算 maxTries 次，烧完进冷却窗；
 * 冷却窗过后允许重开预算（reset=true 由调用方清零计数）。两次尝试之间至少隔
 * minGapMs（打字 + 页面反应需要时间，连打=同一浮层重复输入）。
 * 判据只吃数据不碰 DOM——「会不会误触键盘」这条安全性质必须可单测。
 */
export function autoPinGate({
  pin = "", tries = 0, lastTryTs = 0, now = 0,
  maxTries = 2, cooldownMs = 600000, minGapMs = 5000,
} = {}) {
  if (!normalizePin(pin)) return { ok: false, reason: "no_pin", reset: false };
  const t = Math.max(0, Number(tries) || 0);
  const last = Math.max(0, Number(lastTryTs) || 0);
  const ts = Math.max(0, Number(now) || 0);
  if (t >= Math.max(1, maxTries)) {
    if (last && ts - last < cooldownMs) {
      return { ok: false, reason: "cooldown", reset: false };
    }
    return { ok: true, reason: "retry_window", reset: true };
  }
  if (last && ts - last < minGapMs) {
    return { ok: false, reason: "too_soon", reset: false };
  }
  return { ok: true, reason: "", reset: false };
}

/**
 * 消息请求文件夹自适应扫描间隔（单位：基础 poll tick 数）。
 *
 * 优化：旧实现只有「封禁后指数退避」；空转（连续扫到 0 请求）时仍按固定
 * MSG_REQ_EVERY 打 /requests/，白白积累风控分。空转拉长、有货收回——总量不增、
 * 有请求时灵敏度不降。
 *
 * @param {object} o
 * @param {number} o.baseEvery     配置底数（MSG_REQ_EVERY）
 * @param {number} o.emptyStreak   连续「扫到且 rows=0」次数
 * @param {boolean} o.blocked      当前是否在封禁冷却
 * @returns {number} 生效 every（≥ baseEvery）
 */
export function adaptiveReqEvery({ baseEvery = 15, emptyStreak = 0, blocked = false } = {}) {
  const base = Math.max(1, Number(baseEvery) || 15);
  if (blocked) return base; // 冷却窗内根本不扫，every 无意义；保持底数供恢复后用
  const streak = Math.max(0, Number(emptyStreak) || 0);
  // 空转 0→base；每 2 次空转 +base，封顶 4×base（≈ 默认 60s → 最长 ~4min）
  const steps = Math.min(3, Math.floor(streak / 2));
  return base * (1 + steps);
}

/**
 * 打开线程副作用清单（可执行决策，不只是注释）。
 *
 * Messenger 打开线程 = FB 侧标已读。任何「为探测/回填而打开」的路径必须先过这里。
 * @returns {{ ok: boolean, reason: string }}
 */
export function canOpenThread({ purpose = "", unread = false, isRequest = false } = {}) {
  const p = String(purpose || "");
  // 回填：绝不碰未读（生产实锤：首版开未读 → 未读清零 +「已读不回」）
  if (p === "backfill") {
    return unread
      ? { ok: false, reason: "backfill_skips_unread" }
      : { ok: true, reason: "" };
  }
  // 主动探针：会标已读，刻意禁用（半死态改走 e2ee_ratio 零副作用信号）
  if (p === "probe") {
    return { ok: false, reason: "probe_disabled_marks_read" };
  }
  // 实时入站 / 坐席拉更早 / 发送：允许（「正在处理」语义）
  if (p === "inbound" || p === "history_pull" || p === "send" || p === "debug") {
    return { ok: true, reason: "" };
  }
  // 请求区：打开≠接受（已联调），允许读全文；仍受上层 MSG_MAX_REQ_OPENS 限流
  if (p === "request_read") {
    return { ok: true, reason: isRequest ? "" : "not_a_request" };
  }
  return { ok: false, reason: "unknown_purpose" };
}
