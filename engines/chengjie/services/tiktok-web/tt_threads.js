/**
 * TikTok 网页收件箱行解析的纯函数核心（TK-3 ②-A，2026-09-11）。
 *
 * 与 services/instagram-web/ig_threads.js 同一哲学：只做「DOM 快照 → 结构化字段」，
 * 不碰 Playwright、不导航、不进线程（进线程会给对方留下已读回执——只读边车不该有副作用）。
 *
 * 两条硬不变量：
 * 1. **判不出就回落**：整行文本当正文、标题留空、chat_key 用行内可得的最稳定键；永不比现状差。
 * 2. **不猜关系态**：TikTok 私信「消息请求」（Message requests）在网页版是独立的过滤器/分区，
 *    行快照里给不出「对方是否回过」——这个判断留给智聊侧（会话有无对方入站，见
 *    tiktok_source_badge.peer_never_replied），这里只如实回落。
 *
 * ⚠ 选择器 / 字段名尚未用可用区真实账号采样核对（见 README「联调核对清单」）；
 *   本文件的输入只是「叶子文本片段 + 头像张数 + 行 href」，对 DOM 结构的依赖已经压到最薄。
 */

/** 相对时间标签（行尾那截「2h」/「刚刚」/「Yesterday」），既非标题也非正文。 */
const REL_TIME_BODY = "\\d+\\s*(?:m|min|h|hr|d|w|y|分钟|分|小时|时|天|周|年)|刚刚|现在|昨天|just\\s*now|yesterday|now|在线|active\\s*now";
const REL_TIME_RE = new RegExp(`^(?:${REL_TIME_BODY})$`, "i");
const TAIL_REL_TIME_RE = new RegExp(`\\s(?:${REL_TIME_BODY})$`, "i");

/** 自己发的那条预览前缀（界面语言相关；只用来标 direction 提示，判不出就 in）。 */
const SELF_PREFIX_RE = /^(?:you|你|您|我|me)\s*[:：]\s*/i;

/** 折叠空白 + 去首尾。非字符串 → 空串。 */
export function normRowText(s) {
  return String(s == null ? "" : s).replace(/\s+/g, " ").trim();
}

export function isRelativeTimeLabel(s) {
  const t = normRowText(s);
  return !!t && REL_TIME_RE.test(t);
}

/** 去掉行尾相对时间（预览变了没＝新消息，走时不该误触发）。 */
export function stripTailRelativeTime(s) {
  return normRowText(s).replace(TAIL_REL_TIME_RE, "").trim();
}

/**
 * 从行 href 提取会话键材料：``/messages?u=123`` → "123"；``/@shop_ph`` → "shop_ph"；无 → ""。
 * TikTok 网页收件箱行的 href 形态未核对，两种都认（联调后收窄）。
 */
export function threadIdFromHref(href) {
  const h = String(href || "");
  let m = /[?&]u=([^&#]+)/.exec(h);
  if (m) return decodeURIComponent(m[1]);
  m = /\/@([A-Za-z0-9._]{2,24})\b/.exec(h);
  if (m) return "@" + m[1];
  return "";
}

/**
 * 行快照 → 结构化：
 *   { tid, chatKey, title, text, directionHint, rawText }
 *
 * - chatKey：能拿到 ``@uniqueId`` → ``tiktok:user:<uniqueId>``（与官方 worker / 真机桥同键，三路合并成一条会话）；
 *   只有数字 u= → ``tiktok:web:<u>``；都没有 → ``tiktok:web:<title slug>``（最弱，联调后应消失）。
 * - directionHint：预览带 "You:" 类前缀 → "out"，否则 "in"（只是提示；智聊侧仍以自己的出站镜像为权威）。
 */
export function parseTtThreadRow(raw) {
  const spans = (raw && Array.isArray(raw.spans) ? raw.spans : []).map(normRowText).filter(Boolean);
  const rawText = normRowText(raw && raw.rowText);
  const tid = normRowText(raw && raw.tid) || threadIdFromHref(raw && raw.href);
  let title = "";
  let text = "";
  const body = spans.filter((s) => !isRelativeTimeLabel(s));
  if (body.length >= 2) {
    title = body[0];
    text = body.slice(1).join(" ");
  } else if (body.length === 1) {
    // 只有一段：没法分标题/正文 → 全当正文，标题留空（回落）
    text = body[0];
  } else {
    text = stripTailRelativeTime(rawText);
  }
  // 标题若被正文重复包含（行文本拼接产物），从正文里剥掉一次
  if (title && text.startsWith(title + " ")) text = text.slice(title.length + 1);
  let directionHint = "in";
  if (SELF_PREFIX_RE.test(text)) {
    directionHint = "out";
    text = text.replace(SELF_PREFIX_RE, "");
  }
  let chatKey = "";
  if (tid.startsWith("@")) chatKey = "tiktok:user:" + tid.slice(1);
  else if (tid) chatKey = "tiktok:web:" + tid;
  else if (title) chatKey = "tiktok:web:" + title.toLowerCase().replace(/[^a-z0-9\u4e00-\u9fff]+/g, "-").replace(/^-+|-+$/g, "");
  return { tid, chatKey, title, text, directionHint, rawText };
}

/** 语义预览键：只含 标题+正文+方向（不含相对时间），走时不触发「新消息」。 */
export function threadPreviewKey(row) {
  return [row.title || "", row.directionHint || "", row.text || ""].join("\u0001");
}
