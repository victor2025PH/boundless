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
 * 最终入站提示码（P0 2026-08-14，173 实测盲区修复）：把「PIN 缺失/未通过」从
 * classifyInboxHint 的泛化条件里解耦出来。
 *
 * 旧实现是 `if (hint && pinState…) hint = "e2ee_pin_required"` ——升格被泛化 hint
 * 闸住，而泛化三支全依赖 e2ee 占位比 ≥0.6 / 读取窗全败；173 实测占位比 0.39 时
 * PIN 浮层明明在场（worker 亲眼确认过 detectPinPrompt）却全程零提示，坐席只看到
 * 泛泛「通道离线」。pinState=missing/failed 本身就是确定性证据（只在浮层真实
 * 在场时置位），不需要旁证——单独命中即出精确码。泛化 hint 仍原样透传。
 */
export function resolveInboxHint({ baseHint = "", pinState = "" } = {}) {
  const pin = String(pinState || "");
  if (pin === "missing" || pin === "failed") return "e2ee_pin_required";
  return String(baseHint || "");
}

/**
 * PIN 自愈计数器推进（P1 2026-08-14 观测面，纯函数）：tryAutoE2eePin 的
 * 尝试/成败此前只进日志——「托管 PIN 后有没有真自愈」要翻 sidecar 日志才知道。
 * 返回**新对象**（不改入参，调用方整体替换 entry._pinHeal）；脏入参按零值重建。
 * outcome ∈ attempt | ok | fail（其他值原样返回=不误计）。
 */
export function pinHealBump(stats, outcome, now = Date.now()) {
  const s = {
    attempts: Math.max(0, Number((stats || {}).attempts) || 0),
    ok: Math.max(0, Number((stats || {}).ok) || 0),
    fail: Math.max(0, Number((stats || {}).fail) || 0),
    last_ts: Math.max(0, Number((stats || {}).last_ts) || 0),
    last_ok_ts: Math.max(0, Number((stats || {}).last_ok_ts) || 0),
  };
  const ts = Math.max(0, Number(now) || 0);
  if (outcome === "attempt") {
    s.attempts += 1;
    s.last_ts = ts;
  } else if (outcome === "ok") {
    s.ok += 1;
    s.last_ok_ts = ts;
  } else if (outcome === "fail") {
    s.fail += 1;
  }
  return s;
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
 * 首连/重启回填的「预热提速」：一个 poll tick 里**串行**读几条线程（不并发——不制造风控
 * 尖峰），把「重启后 E2EE 会话逐条解密」的稀疏窗口从 ~N×POLL 压到 ~N/batch×POLL。返回本
 * tick 应读的条数：夹在 [0, min(queueLen, cap)]。warmBatch 至少 1、封顶 cap（默认 5，防误配
 * 把单 tick 拉太长——tick 变长有 _polling 再入闸兜底，但过长仍不利于风控与响应）。纯函数。
 * @param {number} queueLen 回填队列剩余条数
 * @param {number} warmBatch 期望每 tick 条数（env MSG_BACKFILL_WARM_BATCH；1=旧逐条行为）
 * @param {number} [cap=5]   单 tick 硬上限
 * @returns {number} 本 tick 读取条数
 */
export function warmBackfillBatch(queueLen, warmBatch, cap = 5) {
  const q = Math.max(0, Math.floor(Number(queueLen) || 0));
  const c = Math.max(1, Math.floor(Number(cap) || 1));
  const b = Math.min(Math.max(1, Math.floor(Number(warmBatch) || 1)), c);
  return Math.min(q, b);
}

/**
 * 发送同线程快路判定：页面 URL 已停在目标线程（/t/<jid> 或 /e2ee/t/<jid>，段边界防
 * /t/123 误配 /t/1234）→ 发送可跳过整页重导航 + settle（实测省 ~3-5s；连续给同一
 * 客户发消息是坐席/AI 的常态形态）。纯函数。
 * @param {string} url 当前页面 URL
 * @param {string} jid 目标线程 key
 */
export function sendFastPathEligible(url, jid) {
  const u = String(url || "");
  const j = String(jid || "").replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  if (!u || !j) return false;
  return new RegExp(`/t/${j}(/|$|\\?)`).test(u);
}

/**
 * composer 内容与待发全文的一致性校验（长文本 insertText 快速输入的完整性闸门）：
 * 空白归一后逐字比对——不一致＝insertText 没进编辑器状态，调用方必须清空回退逐字输入。
 * 宁可慢也绝不让「内容截断」出站。纯函数。
 */
export function composerTextMatches(currentText, wantText) {
  const norm = (s) => String(s || "").replace(/\s+/g, " ").trim();
  const cur = norm(currentText);
  const want = norm(wantText);
  return !!want && cur === want;
}

/**
 * 引用回复目标定位（P2 双面板融合 2026-08-13）：在可见消息行文本里找「被引用的那条」。
 * Messenger web 无 wamid、synth id 在 DOM 里没有锚点 → 只能按**文本**匹配。
 * **宁缺勿滥**：歧义/找不到一律返回 -1，上层据此优雅降级为普通发送（绝不把引用挂到
 * 错误气泡上——挂错比不挂更糟）。保守三级：精确 > 唯一前缀（长文被截断成预览很常见）>
 * 唯一包含（仅当引用文足够长 ≥6，防短串「好」「在」误命中）；任一级命中多于一条＝歧义弃权。
 * 纯函数（零 DOM/Playwright，可单测）。
 * @param {string[]} rowTexts 最近可见消息行文本
 * @param {string} quotedText 被引用消息文本
 * @returns {number} 命中下标；-1=未命中/歧义
 */
export function matchQuotedTarget(rowTexts, quotedText) {
  const norm = (s) => String(s || "").replace(/\s+/g, " ").trim().toLowerCase();
  const want = norm(quotedText);
  if (!want || !Array.isArray(rowTexts)) return -1;
  const rows = rowTexts.map(norm);
  const uniq = (pred) => {
    const hits = [];
    for (let i = 0; i < rows.length; i++) if (rows[i] && pred(rows[i])) hits.push(i);
    return hits.length === 1 ? hits[0] : (hits.length > 1 ? -2 : -1);
  };
  // 1) 精确
  let r = uniq((t) => t === want);
  if (r >= 0) return r;
  if (r === -2) return -1;   // 多条完全相同→歧义弃权
  // 2) 唯一前缀（预览截断：一方是另一方的前缀，且公共长度 ≥4 防短串）
  r = uniq((t) => (t.startsWith(want) || want.startsWith(t)) && Math.min(t.length, want.length) >= 4);
  if (r >= 0) return r;
  if (r === -2) return -1;
  // 3) 唯一包含（仅长引用文，防短串误匹配）
  if (want.length >= 6) {
    r = uniq((t) => t.includes(want));
    if (r >= 0) return r;
  }
  return -1;
}

/**
 * 出站表情（P3 双面板融合 2026-08-13）：请求 emoji → Messenger 默认表情面板目标字符。
 * 面板六个：👍 ❤️ 😆 😮 😢 😠。归一化（剥 VS16 变体选择符）后映射；UI 侧的 😂 归 😆
 * （同为 laugh 语义，Messenger 面板没有 😂）；面板外（如 🙏）返回 ""＝诚实拒绝
 * （unsupported_emoji），绝不硬点「更多表情」网格（选择器深水区，宁缺勿滥）。纯函数。
 */
export function msgrPaletteTarget(emoji) {
  const e = String(emoji || "").replace(/\uFE0F/g, "").trim();
  const MAP = {
    "👍": "👍", "❤": "❤️", "😆": "😆", "😂": "😆",
    "😮": "😮", "😢": "😢", "😠": "😠",
  };
  return MAP[e] || "";
}

/**
 * 出站表情：面板字符 → picker 元素 aria-label 名称候选（中英/繁体，小写比对）。
 * 与入站 REACT_NAME（名称→emoji）互为反向：picker 元素常以名称而非字符作 aria，
 * 字符匹配不中时按名称兜底。纯函数。
 */
export function reactAriaCandidates(palette) {
  const REV = {
    "👍": ["赞", "like", "thumbs"],
    "❤️": ["大心", "爱心", "心", "love", "heart"],
    "😆": ["大笑", "笑", "laugh", "haha"],
    "😮": ["哇", "惊讶", "驚訝", "wow", "surprised"],
    "😢": ["哭", "难过", "難過", "sad", "cry"],
    "😠": ["怒", "生气", "生氣", "angry", "anger"],
  };
  return REV[String(palette || "")] || [];
}

/**
 * 发送快路径判定：当前页面 URL 是否已停在目标线程（/t/{jid} 或 /e2ee/t/{jid}，容忍尾斜杠/
 * 查询串）。命中＝同会话连发/刚发过，跳过重导航 + 2s settle（单发省 3-6s）。纯函数。
 * jid 做正则转义（线程 key 理论上纯数字，防御性处理）；空入参一律 false（走完整导航路径）。
 * @param {string} url  page.url()
 * @param {string} jid  目标线程 key
 */
export function isOnThreadUrl(url, jid) {
  const u = String(url || "");
  const j = String(jid || "");
  if (!u || !j) return false;
  const esc = j.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  return new RegExp(`/(e2ee/)?t/${esc}(/|$|\\?)`).test(u);
}

/**
 * 未读驱动的强制读取候选（P0 2026-08-15，「预览指纹永不变化 → 新消息隐形」盲区解药）。
 *
 * 变更检测唯一触发源是「左栏预览文字变化」。两类新消息不产生预览变化，静默丢失：
 *   ① E2EE 会话设备密钥未恢复 → 预览永远是加密占位（生产实锤 2026-08-15 00:47：
 *      侧栏未读=2 挂 30+ 分钟、read_attempts 纹丝不动、两条真实客户消息全丢）；
 *   ② 客户重发与上条**逐字相同**的文本 → sig 相同（哑巴盲区，顺带修）。
 * 本函数从会话行挑出「未进常规 sig 候选、但有未读证据」的线程作第二触发源。打开线程
 * 顺带给 readThreadTail 内的 E2EE PIN 就地自愈一次机会——读出正文即走常规 ingest
 * 去重管线，与常规候选完全同一条下游（lastInboundSig 防重复上报）。
 *
 * 两级证据（强 → 弱，弱级需全局未读旁证）：
 *   row_unread        — 行级「未读消息/Unread message」标记在场（最精确）；
 *   fresh_placeholder — 行预览是 E2EE 加密占位 且 行相对时间新鲜（≤freshMs）且
 *                       全局未读 >0。兜「行级标记抓取失效/改版」的后路——全局未读数
 *                       （Chats 徽标）与行级标记是两套独立抓取，历史上更稳。
 *
 * 护栏（全部在纯函数内可单测）：每线程冷却 cooldownMs（读不出的 E2EE 线程绝不能每
 * 4s 反复导航=风控敞口）；单轮上限 cap（叠加在调用方 MSG_MAX_OPENS 总预算**之内**，
 * 强制候选排在常规候选之后，总导航量不增）；已在常规候选的线程不重复；只读不写
 * attemptLog（推进时刻由调用方在真正打开时落账）。
 *
 * @param {Array<{key:string,preview:string,rel:string,unread:boolean}>} convs 左栏会话行
 * @param {object} o
 * @param {Set<string>} o.candidateKeys 本轮常规 sig 候选 key 集
 * @param {Map<string,number>} o.attemptLog 线程 key → 上次强制读取时刻 ms（调用方持有）
 * @param {number} o.now 当前时刻 ms
 * @param {number} o.globalUnread 侧栏全局未读数（readInboxUnread）
 * @param {number} [o.cooldownMs=600000] 同线程两次强制读取最小间隔
 * @param {number} [o.cap=2] 单轮最多强制读取条数
 * @param {number} [o.freshMs=1800000] fresh_placeholder 级的行相对时间新鲜窗
 * @param {RegExp} [o.placeholderRe] E2EE 占位预览正则（调用方注入，避免跨模块复制）
 * @param {(rel:string)=>number} [o.relToTs] 行相对时间 → epoch 秒（0=认不出）
 * @returns {Array<{key:string,why:"row_unread"|"fresh_placeholder"}>}
 */
export function pickUnreadForced(convs, {
  candidateKeys = new Set(),
  attemptLog = new Map(),
  now = Date.now(),
  globalUnread = 0,
  cooldownMs = 600000,
  cap = 2,
  freshMs = 1800000,
  placeholderRe = null,
  relToTs = null,
} = {}) {
  if (!Array.isArray(convs) || cap <= 0) return [];
  const ts = Math.max(0, Number(now) || 0);
  const cool = Math.max(0, Number(cooldownMs) || 0);
  const inCooldown = (key) => {
    const last = Number(attemptLog && attemptLog.get && attemptLog.get(key)) || 0;
    return last > 0 && (ts - last) < cool;
  };
  const rowUnread = [];
  const freshPh = [];
  const taken = new Set();
  for (const c of convs) {
    const key = String((c && c.key) || "");
    if (!key || taken.has(key)) continue;
    if (candidateKeys && candidateKeys.has && candidateKeys.has(key)) continue;
    if (inCooldown(key)) continue;
    if (c.unread === true) {
      rowUnread.push({ key, why: "row_unread" });
      taken.add(key);
      continue;
    }
    if ((Number(globalUnread) || 0) > 0 && placeholderRe && typeof relToTs === "function"
        && placeholderRe.test(String(c.preview || ""))) {
      const relSec = Math.max(0, Number(relToTs(String(c.rel || ""))) || 0);
      if (relSec > 0 && (ts - relSec * 1000) <= Math.max(0, Number(freshMs) || 0)) {
        freshPh.push({ key, why: "fresh_placeholder" });
        taken.add(key);
      }
    }
  }
  return rowUnread.concat(freshPh).slice(0, Math.max(1, Math.floor(cap)));
}

/**
 * 镜像自发环（P3 2026-08-15，「守卫窗被自己旧消息污染」根因修复）。
 *
 * 事故链（生产实锤 01:00:54）：服务自己 00:20 发出的三条消息，40 分钟后线程被
 * 首次读取时，因自发回声记忆（sentLog）只有 10 分钟 TTL 已过期 → 被
 * mirrorManualOutbound 误当「人工消息」以**当前时刻**重新入库 → Python 出站
 * 近重复守卫（窗口 180s）看到「17 秒前刚发过近似内容」→ 把 AI 对客户新问题的
 * 回复静默拦掉——客户体验=已读不回。TTL 对「发送后很久才被读到」的线程
 * （E2EE 占位线程正是常态）天然失效，故给镜像专用一个**按条数封顶、无 TTL**
 * 的自发文本环：restart 丢失无害（水位线基线机制本就防重启历史重灌）。
 *
 * ring = Map<chatKey, norm[]>（调用方持有）；纯函数可单测。
 * 命中语义与 isSelfEcho 对齐：全串相等，或双方 ≥16 字符且 24 字前缀互为前缀
 * （Messenger 预览/气泡截断容错）；短文本只认全等（防「好的」类误吞真人工消息）。
 */
/**
 * 回声文本比对核心（isSelfEcho / sentRingHit 唯一共用判据，2026-08-16 收口）。
 *
 * 语义：全串相等恒命中；「24 字前缀互为前缀」的截断容错只在**双方都 ≥16 字**时
 * 启用——短文本只认全等。生产实锤（2026-08-16 00:56）：本方刚发「在」，客户在
 * 10 分钟回声窗内回「在吗」，旧 isSelfEcho 的无条件前缀匹配把它判成自发回声 →
 * 预筛跳过并推进 seen 基线，客户消息永久隐形；且发送后线程停留在打开态（Messenger
 * 视为已读），行级未读兜底也永不触发。短消息的真回声不会被预览截断，前缀容错
 * 对它们只有误伤没有收益；漏判回声的代价仅是一次多余进线程读（方向权威在
 * readThreadTail 的 aria 层，绝不会自回复）。
 * 入参须已 normPreview 归一化；纯函数可单测。
 */
export function echoTextHit(aNorm, bNorm) {
  const a = String(aNorm || "");
  const b = String(bNorm || "");
  if (!a || !b) return false;
  if (a === b) return true;
  if (a.length >= 16 && b.length >= 16) {
    if (a.startsWith(b.slice(0, 24)) || b.startsWith(a.slice(0, 24))) return true;
  }
  return false;
}

export function sentRingPush(ring, key, norm, { perKey = 20, maxKeys = 64 } = {}) {
  if (!(ring instanceof Map)) return;
  const k = String(key || "");
  const n = String(norm || "");
  if (!k || !n) return;
  const rows = ring.get(k) || [];
  // 已在环内 → 挪到队尾（最近发送优先保留），不重复占位
  const idx = rows.indexOf(n);
  if (idx >= 0) rows.splice(idx, 1);
  rows.push(n);
  while (rows.length > Math.max(1, perKey)) rows.shift();
  // delete+set 让该 key 移到 Map 尾部（插入序=LRU 序，超限剪最旧 key）
  ring.delete(k);
  ring.set(k, rows);
  while (ring.size > Math.max(1, maxKeys)) {
    ring.delete(ring.keys().next().value);
  }
}

export function sentRingHit(ring, key, norm) {
  if (!(ring instanceof Map)) return false;
  const rows = ring.get(String(key || "")) || [];
  const n = String(norm || "");
  if (!n || !rows.length) return false;
  for (const r of rows) {
    if (echoTextHit(n, r)) return true;
  }
  return false;
}

/**
 * 人工出站镜像的取件决策（2026-08-16「手机手发在坐席隐形」修复的配套纯函数）。
 *
 * 背景：mirrorManualOutbound 的水位线机制里「首次观察只建水位不上报」是防
 * sidecar 重启后把编排器早已镜像过的历史出站重灌成重复行（P3 2026-08-15）。
 * 但「手机/原生页手发」触发的**首次**读取也会被同一条规则吞掉——手发的第一条
 * 永远不回流。本函数把两种「首次观察」区分开：
 *   - 普通读取（客户回复触发等）：维持旧行为只建水位（restart 重灌防线不动）；
 *   - manualTrigger（预览出现非自发的 "你:/You:" 出站、专门为镜像进的线程）：
 *     该出站必然发生在本进程两次活轮询之间（预览指纹变化才触发）＝绝非重启前
 *     历史 → 允许镜像**最新一条**，且文本须与触发预览吻合（echoTextHit 截断
 *     容错）；纯媒体（有 media_ref 无正文）无文可核对，凭触发本身放行。
 * 已有水位时与旧行为完全一致：找到水位切其后（burstMax 封顶）、水位滑出窗口
 * 保守只取最新。纯函数可单测；sigOf/normOf 由调用方注入（normPreview 在 server.js）。
 */
export function pickManualOutMirror(outs, {
  mark,
  sigOf,
  normOf = null,
  rowPreviewNorm = "",
  manualTrigger = false,
  burstMax = 6,
} = {}) {
  if (!Array.isArray(outs) || !outs.length || typeof sigOf !== "function") {
    return { fresh: [], newMark: undefined };
  }
  const newest = outs[outs.length - 1];
  const newMark = sigOf(newest);
  if (mark === undefined) {
    if (!manualTrigger) return { fresh: [], newMark };
    const text = String((newest && newest.text) || "").trim();
    const isPureMedia = !!(newest && newest.media_ref) && !text;
    let ok = false;
    if (isPureMedia) ok = true;
    else if (!rowPreviewNorm) ok = true;
    else if (typeof normOf === "function") ok = echoTextHit(normOf(newest), rowPreviewNorm);
    return { fresh: ok ? [newest] : [], newMark };
  }
  if (mark === newMark) return { fresh: [], newMark };
  let startIdx = -1;
  for (let i = outs.length - 1; i >= 0; i--) {
    if (sigOf(outs[i]) === mark) { startIdx = i; break; }
  }
  const fresh = (startIdx >= 0 ? outs.slice(startIdx + 1) : [newest])
    .slice(-Math.max(1, Math.floor(burstMax)));
  return { fresh, newMark };
}

/**
 * 消息请求页扫描结果分类（P2 2026-08-15，「219 轮连续空转」判别盲区解药）。
 *
 * rows=0 有三种完全不同的含义，旧实现一律计 emptyStreak（只用于自适应降频），
 * 「真没人来」与「FB 改版后根本读不到」在观测上不可区分——后者=陌生人新客户
 * 静默全丢，比 E2EE 盲区更隐蔽（连未读标记都看不见）：
 *   rows     — 抓到 ≥1 行请求（页面结构健康的正面证据）；
 *   blocked  — FB 风控封禁文案在场（既有退避逻辑处理）；
 *   empty_ok — 零行 **且** 页面有结构证据（子 tab「可能认识/垃圾信息」或空态文案
 *              渲染在场）＝页面正常渲染、文件夹真的空；
 *   suspect  — 零行且无任何结构证据＝选择器漂移/导航失败/改版，读数不可信。
 * 调用方对 suspect 计连续 streak（rows/empty_ok 清零），streak 高＝确定性漂移。
 * 纯函数零 DOM，可单测。
 */
export function classifyRequestsScan({ blocked = false, rowCount = 0, structSeen = false } = {}) {
  if (blocked) return "blocked";
  if ((Number(rowCount) || 0) > 0) return "rows";
  return structSeen ? "empty_ok" : "suspect";
}

/**
 * send 链「等不到输入框」的探针分类（2026-08-15 173 实锤：新建 E2EE 线程首开渲染
 * 超预算 → /send 命中唯一不打日志的 500 分支，边车零线索，排查只能靠后端截断的
 * httpx 文本绕一整圈）。输入＝server.js probeComposerBlockers 的页面快照，输出＝
 * 稳定 reason_code（进响应体 + 边车 error 日志）：
 *   login_page        页面是账密表单＝登录态丢了——需要重新登录
 *   e2ee_pin_prompt   E2EE 恢复 PIN 浮层挡住输入框——托管 PIN 或人工输入
 *   needs_accept      「接受」栏仍在场＝消息请求接受没点成（布局漂移？）
 *   composer_hidden   composer 在 DOM 里但不可见＝有浮层/遮罩，多为瞬态
 *   render_timeout    线程内容已渲染但 composer 缺席＝SPA 渲染慢/改版
 *   page_not_rendered 连会话内容都没渲染＝导航失败/白屏/网络
 * 判定顺序＝语义硬度（登录丢失最硬 → 渲染慢最软）。
 * ⚠ reason_code 会进 Python 投递失败文本：措辞必须避开 dead_peer_registry
 * 的永久错误标记词（USER_IS_BLOCKED 等 Telegram 大写词表）——错用词会把
 * 瞬态渲染慢误判成死 peer、封禁会话 6h。纯函数零 DOM，可单测。
 */
export function classifyComposerBlock(probe) {
  const p = probe || {};
  if (p.loginForm) return "login_page";
  if (p.pinPrompt) return "e2ee_pin_prompt";
  if (p.acceptSeen) return "needs_accept";
  if ((Number(p.composerCount) || 0) > 0) return "composer_hidden";
  if (p.hasLog) return "render_timeout";
  return "page_not_rendered";
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
