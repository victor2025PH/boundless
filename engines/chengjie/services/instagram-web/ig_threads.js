/**
 * IG 收件箱行解析的纯函数核心（2026-08-20 群/频道 P1）。
 *
 * 背景：入站首版把整个 `<a>` 的 textContent 当消息正文上报（`chat_type` 恒空、
 * `name` 恒空）。于是坐席看到的正文是「群名 + 末条预览 + 相对时间」糊在一起的
 * 一坨，而**群 DM 全部混进私聊**——群会话拿不到群档位/误发闸，气泡也没有发言人。
 *
 * 本模块只做「DOM 快照 → 结构化字段」的判定，不碰 Playwright、不导航、不产生已读
 * 回执（IG 打开线程会给对方留下已读，入站探测绝不该有这种副作用）。
 *
 * 两条硬不变量：
 * 1. **判不出就回落今天的行为**（整行文本当正文、标题留空），永不比现状更差；
 * 2. **群判据只认头像张数**（群线程叠 ≥2 张头像）。刻意**不用**预览里的
 *    `"某人: "` 前缀当群判据——1:1 线程里自己发的那条预览是 `"You: "` /
 *    `"你: "`，随界面语言变；靠自我词表判定会在未覆盖语种上把私聊误判成群，
 *    而误判成群会牵动群档位与群误发闸，比「不判」后果更重。前缀只在**已判定
 *    为群之后**用来抽发言人。
 */

/** 相对时间标签（行尾那截「2h」/「刚刚」），既非标题也非正文。 */
const REL_TIME_BODY = "\\d+\\s*(?:m|min|h|hr|d|w|y|分钟|分|小时|时|天|周|年)|刚刚|现在|just\\s*now|active\\s*now|now|在线";
const REL_TIME_RE = new RegExp(`^(?:${REL_TIME_BODY})$`, "i");
/** 同一词表锚在行尾（整行文本里时间跟在正文后面，中间只隔空白）。 */
const TAIL_REL_TIME_RE = new RegExp(`\\s(?:${REL_TIME_BODY})$`, "i");

/** 发言人前缀：`Alice: 在吗` / `阿丽：在吗`（冒号前不超 40 字，避免把正文切腰）。 */
const SENDER_PREFIX_RE = /^([^:：\n]{1,40})[:：]\s*(.+)$/;

/** 折叠空白 + 去首尾。非字符串 → 空串。 */
export function normRowText(s) {
  return String(s == null ? "" : s).replace(/\s+/g, " ").trim();
}

/** 是否相对时间标签。 */
export function isRelativeTimeLabel(s) {
  const t = normRowText(s);
  return !!t && REL_TIME_RE.test(t);
}

/**
 * 拆发言人前缀 → `{ sender, text }`。无前缀 → sender 空、text 原样。
 *
 * 只在群会话上调用：私聊预览的 `"You: "` 前缀剥掉会把「谁说的」这个信息弄丢，
 * 而私聊本就不需要发言人。
 */
export function splitSenderPrefix(preview) {
  const t = normRowText(preview);
  if (!t) return { sender: "", text: "" };
  const m = SENDER_PREFIX_RE.exec(t);
  if (!m) return { sender: "", text: t };
  const sender = normRowText(m[1]);
  const text = normRowText(m[2]);
  if (!sender || !text) return { sender: "", text: t };
  return { sender, text };
}

/**
 * 去掉行尾的相对时间标签。
 *
 * 入站是靠「预览变了没」判新消息的，而 `<a>` 的整行文本里带着走时的相对时间
 * ——同一条旧消息在 `2h → 3h` 时行文本就变了，于是被当成新消息重报一遍
 * （msg_id 每次现取时间戳，去重也拦不住）。判定键必须把时间那截摘掉。
 */
export function stripTrailingRelTime(s) {
  let t = normRowText(s);
  if (!t) return "";
  // 逐次剥尾（"… 2h"、"… Active now"、"… 5 分钟" 可能叠着出现）；只剥到还剩内容为止。
  for (let i = 0; i < 4; i += 1) {
    const next = t.replace(TAIL_REL_TIME_RE, "").trim();
    if (!next || next === t) break;
    t = next;
  }
  return t;
}

/**
 * 「这一行的语义预览」——入站变更判定键。
 *
 * 结构化成功时用 标题/发言人/正文 三元组（天然不含时间）；回落分支用整行摘掉
 * 尾部时间。`\u0000` 分隔避免字段拼接产生歧义。
 */
export function threadPreviewKey(row) {
  const r = row || {};
  if (r.parsed) {
    return [r.title || "", r.senderName || "", r.text || ""].join("\u0000");
  }
  return stripTrailingRelTime(r.text);
}

/** 保序去重（IG 常把群名同时渲染在 alt/aria 与可见 span 上）。 */
function dedupeKeepOrder(list) {
  const seen = new Set();
  const out = [];
  for (const raw of list || []) {
    const t = normRowText(raw);
    if (!t || seen.has(t)) continue;
    seen.add(t);
    out.push(t);
  }
  return out;
}

/**
 * 解析一条收件箱行快照。
 *
 * 入参（`page.$$eval` 在浏览器里采到的**纯数据**）：
 *   `{ tid, rowText, spans, imgCount }`
 *   - `spans`：`<a>` 内叶子文本节点，DOM 序（典型 = [群名/人名, 末条预览, 相对时间]）
 *   - `imgCount`：`img[src]` 张数（群线程叠头像 → ≥2）
 *
 * 返回 `{ tid, title, chatType, senderName, text, parsed }`：
 *   - `chatType`：`"group"` 或 `""`（空＝私聊，与 Python 侧「缺省空＝非群」同口径）
 *   - `text`：群且抽出前缀时**已剥掉**发言人（`protocol_bridge` 会用 sender_name
 *     重新拼「某人：正文」当会话列表预览，不剥就会出现两遍名字）
 *   - `parsed`：false ＝ 片段不足以区分标题/正文，调用方按回落语义走
 */
export function parseIgThreadRow(raw) {
  const src = raw || {};
  const tid = normRowText(src.tid);
  const rowText = normRowText(src.rowText);
  const imgCount = Number.isFinite(Number(src.imgCount)) ? Number(src.imgCount) : 0;
  const isGroup = imgCount >= 2;
  const chatType = isGroup ? "group" : "";

  const parts = dedupeKeepOrder(src.spans).filter((s) => !isRelativeTimeLabel(s));
  // 少于两个片段＝无法区分「哪个是群名、哪个是正文」（纯媒体气泡、改版、
  // 采集失败都会落到这里）→ 如实回落：标题留空、正文用整行，与首版一致。
  if (parts.length < 2) {
    return { tid, title: "", chatType, senderName: "", text: rowText, parsed: false };
  }

  const title = parts[0];
  const preview = parts[parts.length - 1];
  if (!isGroup) {
    return { tid, title, chatType, senderName: "", text: preview, parsed: true };
  }
  const { sender, text } = splitSenderPrefix(preview);
  return {
    tid,
    title,
    chatType,
    senderName: sender,
    text: text || preview,
    parsed: true,
  };
}
