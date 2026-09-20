/**
 * 自身资料（昵称 / 头像）解析纯函数集。
 *
 * 为什么单独成模块：选头像是本 worker 里**唯一会把「别人的脸」写进自己账号卡**的地方，
 * 而它此前只有一句 `document.querySelector("img[alt]")`——DOM 顺序里第一个带 alt 的图
 * 多半是某个会话对端的头像。2026-08-15 实锤：Messenger 账号 `615***403`（Calixa Lopez）
 * 的 self_avatar 抓到的是会话对端 Micah Bindo 的头像（fid 与 inbox.db 里该会话的
 * avatar_url 同一张图），且因 `avatar_needs_refresh` 按 URL 指纹去重 + Messenger 侧
 * 没有周期重采，这张错脸被永久固化——运营在账号 rail 上看到的「本机头像」根本不是自己。
 *
 * 所以选头像必须是**可断言的纯函数**：给定候选集与自身身份，要么给出有据可依的一张，
 * 要么明确交空（fail-closed）。宁可账号卡暂时没有头像，也不能挂着别人的脸——
 * 前者是缺信息，后者是错信息（运营会据此判断「这号绑的是谁」）。
 */

/** 选中策略（也是可观测口径：miss 时看 reason 就知道该补哪一层）。 */
export const SELF_AVATAR_STRATEGY = {
  NAV_LABEL: "nav_label",        // 图挂在带本人名字的交互控件上（左栏「设置」按钮＝本人头像）
  NAME: "name",                 // alt 与自身昵称逐字相等（部分构建/语种下才有）
  PROFILE_LINK: "profile_link",  // 图挂在指向「自己主页」的链接容器内（/me/ 或自身 uid）
};

const _ZERO_WIDTH_RE = /[\u200b-\u200f\u2060\ufeff]/g;

/** 昵称归一：去零宽字符、折叠空白、去首尾空格。比较时再 casefold。 */
export function normalizeName(s) {
  return String(s == null ? "" : s).replace(_ZERO_WIDTH_RE, "").replace(/\s+/g, " ").trim();
}

function sameName(a, b) {
  const na = normalizeName(a);
  const nb = normalizeName(b);
  return !!na && !!nb && na.toLocaleLowerCase() === nb.toLocaleLowerCase();
}

/**
 * 从 `CurrentUserInitialData` 附近的脚本文本片段里取自身昵称与 uid。
 *
 * 该内嵌 JSON 是 FB 页面多年稳定的标配（含 NAME / USER_ID），是**唯一**权威的自身身份
 * 来源——DOM 上任何可见文字都可能是对端的。片段由调用方在页内按 marker 切好（有界），
 * 这里只做解析，便于离线回归。
 */
export function parseCurrentUserInitialData(slices) {
  const out = { name: "", userId: "" };
  const arr = Array.isArray(slices) ? slices : (slices ? [slices] : []);
  for (const raw of arr) {
    const t = String(raw || "");
    if (!out.name) {
      const m = t.match(/"NAME"\s*:\s*"((?:[^"\\]|\\.)*)"/);
      if (m) {
        let v = m[1];
        try { v = JSON.parse('"' + m[1] + '"'); } catch (_) { /* 保留原样 */ }
        out.name = normalizeName(v);
      }
    }
    if (!out.userId) {
      const m = t.match(/"USER_ID"\s*:\s*"(\d{3,})"/);
      if (m) out.userId = m[1];
    }
    if (out.name && out.userId) break;
  }
  return out;
}

/**
 * 头像直链是否可被 Python 侧本地化。
 *
 * `account_self_profile.enrich_from_fields` 只对 `http(s)` 直链走下载分支，其余一律
 * 原样存进 `self_avatar`——而 `blob:` / `data:` 存进去就是账号卡上一张永久裂图。
 * 故不可下载的候选在这里就淘汰，别让它进到「已选中」。
 */
export function isFetchableAvatarUrl(url) {
  return /^https?:\/\//i.test(String(url || "").trim());
}

function candidateArea(c) {
  const w = Number(c && c.w) || 0;
  const h = Number(c && c.h) || 0;
  return w > 0 && h > 0 ? w * h : 0;
}

/** 祖先链（页内收集器给出，形如 `[{tag, role, aria, href}, ...]`，自内向外有界）。 */
function chainOf(c) {
  return Array.isArray(c && c.chain) ? c.chain : [];
}

function hrefsOf(c) {
  if (Array.isArray(c && c.hrefs) && c.hrefs.length) return c.hrefs;
  return chainOf(c).map((h) => (h && h.href) || "").filter(Boolean);
}

/** 名字里可用于匹配的 token（去掉单字符碎片：单字母会把 "Settings" 之类误命中）。 */
function nameTokens(name) {
  return normalizeName(name).split(" ").filter((t) => t.length >= 2);
}

/** aria 文本里是否出现该名字 token。
 *
 *  拉丁 token 要求词边界（否则 "Ana" 会命中 "Manager"）；CJK 名字没有空格分词，
 *  整名当一个 token 走子串即可。 */
function containsNameToken(hay, token) {
  const h = normalizeName(hay).toLocaleLowerCase();
  const t = normalizeName(token).toLocaleLowerCase();
  if (!h || !t) return false;
  if (/^[a-z0-9'’.-]+$/i.test(t)) {
    try {
      return new RegExp(`(^|[^\\p{L}\\p{N}])${
        t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")}([^\\p{L}\\p{N}]|$)`, "u").test(h);
    } catch (_) { /* 老引擎无 \p{L} → 退子串 */ }
  }
  return h.includes(t);
}

const _INTERACTIVE_ROLES = new Set(["button", "link", "menuitem"]);

function isInteractive(hop) {
  const role = String((hop && hop.role) || "").toLowerCase();
  const tag = String((hop && hop.tag) || "").toUpperCase();
  return _INTERACTIVE_ROLES.has(role) || tag === "A" || tag === "BUTTON";
}

/**
 * 图是否挂在「带本人名字的交互控件」上。
 *
 * 2026-08-15 实测这版 messenger.com 上**所有头像的 alt 全为空**，自身身份的唯一 DOM
 * 痕迹是左栏那个账号菜单按钮：`div[role=button][aria-label="Calixa Settings, help and more"]`
 * 里嵌着本人头像（英文构建带本名 + 功能词，中文构建同理）。故按「祖先里有交互控件且其
 * aria-label 出现本人名字 token」判定——不依赖具体语种文案，也不依赖 DOM 顺序。
 */
function navLabelMatchesSelf(c, selfName) {
  const tokens = nameTokens(selfName);
  if (!tokens.length) return false;
  for (const hop of chainOf(c)) {
    if (!isInteractive(hop)) continue;
    const aria = (hop && hop.aria) || "";
    if (!aria) continue;
    if (tokens.some((t) => containsNameToken(aria, t))) return true;
  }
  return false;
}

/**
 * 这张图是否**被明确标成了别人**。
 *
 * 对端头像在会话列表/线程里带 `svg[role=img][aria-label="<对端名>"]`（连「已读」指示器
 * 也是 `role=img aria-label="Seen by Micah Bindo"`）。图自己被标成别人的名字，就一定不是
 * 自己的脸——这条排除直接钉死本次事故那张（Micah Bindo）。含本人名字的标签不算别人。
 */
function labeledAsSomeoneElse(c, selfName) {
  const self = normalizeName(selfName);
  for (const hop of chainOf(c)) {
    if (String((hop && hop.role) || "").toLowerCase() !== "img") continue;
    const aria = normalizeName(hop && hop.aria);
    if (!aria) continue;
    if (!self) return true;                       // 不知道自己是谁 → 有他人标签就别猜
    if (sameName(aria, self)) continue;
    if (nameTokens(self).some((t) => containsNameToken(aria, t))) continue;
    return true;
  }
  return false;
}

function linksToSelf(c, selfId) {
  const hrefs = hrefsOf(c);
  for (const raw of hrefs) {
    const href = String(raw || "");
    if (!href) continue;
    // `/me/` 只可能指向自己，无 uid 也可判。
    if (/(^|\/)me\/?($|[?#])/.test(href)) return true;
    if (!selfId) continue;
    if (href.includes(`profile.php?id=${selfId}`)) return true;
    if (new RegExp(`(^|/)${selfId}(/|$|\\?|#)`).test(href)) return true;
  }
  return false;
}

/**
 * 从候选图里选出「自己的头像」，选不出就交空。
 *
 * 候选形如 `{ src, alt, w, h, chain }`（由页内收集器给出，见 server.js::readSelfProfile；
 * `chain` 是自内向外的祖先属性链，legacy 的 `hrefs` 仍兼容）。判据按可信度排序，
 * **只认这三层**：
 *   1. `NAV_LABEL`——图挂在 aria-label 出现本人名字的交互控件上（左栏账号菜单按钮）；
 *   2. `NAME`——alt 与 CurrentUserInitialData 里的自身昵称逐字相等；
 *   3. `PROFILE_LINK`——图挂在指向自己主页（`/me/` 或自身 uid）的链接容器内。
 * 任何被明确标成**别人**的图先被剔除（`labeledAsSomeoneElse`）。
 * 同层多张取面积最大者，面积相同取先出现的。
 *
 * ⚠ 面积**不能**当独立判据：实测自身头像只有 32×32（左栏小圆头），而线程里对端的图是
 * 100×100——「挑最大的」必然抓错脸，这正是事故的形态。
 *
 * 刻意**不留**「任意 img / 任意 fbcdn 图」兜底：那正是抓到对端脸的根因，且错脸比无脸更
 * 有害。miss 时 `reason` 说明缺哪一层，调用方据此打日志/择机重采，而不是猜一张。
 */
export function pickSelfAvatar(candidates, opts = {}) {
  const selfName = normalizeName(opts.selfName);
  const selfId = String(opts.selfId || "").trim();
  const all = (Array.isArray(candidates) ? candidates : [])
    .filter((c) => c && isFetchableAvatarUrl(c.src));
  if (!all.length) return { url: "", alt: "", strategy: "", reason: "no_candidates" };
  const list = all.filter((c) => !labeledAsSomeoneElse(c, selfName));
  if (!list.length) return { url: "", alt: "", strategy: "", reason: "all_labeled_others" };

  for (const [strategy, match] of [
    [SELF_AVATAR_STRATEGY.NAV_LABEL, (c) => navLabelMatchesSelf(c, selfName)],
    [SELF_AVATAR_STRATEGY.NAME, (c) => selfName && sameName(c.alt, selfName)],
    [SELF_AVATAR_STRATEGY.PROFILE_LINK, (c) => linksToSelf(c, selfId)],
  ]) {
    let best = null;
    for (const c of list) {
      if (!match(c)) continue;
      if (!best || candidateArea(c) > candidateArea(best)) best = c;
    }
    if (best) {
      return {
        url: String(best.src),
        alt: normalizeName(best.alt),
        strategy,
        reason: "",
      };
    }
  }
  return {
    url: "",
    alt: "",
    strategy: "",
    reason: selfName ? "no_match" : "no_self_name",
  };
}

/** miss 时给日志用的紧凑候选摘要（有界，不打完整 URL——签名串又长又无信息量）。 */
export function summarizeCandidates(candidates, limit = 6) {
  return (Array.isArray(candidates) ? candidates : []).slice(0, limit).map((c) => ({
    alt: normalizeName(c && c.alt).slice(0, 40),
    host: (() => {
      try { return new URL(String((c && c.src) || "")).host; } catch (_) { return ""; }
    })(),
    w: Number(c && c.w) || 0,
    h: Number(c && c.h) || 0,
    hrefs: hrefsOf(c).slice(0, 2),
    // 祖先里的身份痕迹：miss 归因全靠它（本轮就是靠这个才发现 alt 全空、
    // 自身身份只在账号菜单按钮的 aria-label 上）。
    labels: chainOf(c)
      .filter((h) => h && (h.aria || h.role))
      .slice(0, 4)
      .map((h) => `${h.role || h.tag || "?"}:${normalizeName(h.aria).slice(0, 40)}`),
  }));
}

/**
 * 何时该重采自身资料。
 *
 * Messenger 侧此前**只在登录/恢复那一刻采一次**（WhatsApp 早有
 * `scheduleSelfProfileRecapture`），于是手机上换了头像本机永远不知道；叠加上面那个错脸
 * bug，一张错的脸能挂几个月。两档节奏：
 *   - 头像还没拿到（首采 fail-closed 空）→ 按 `retryMs` 短周期补采：登录瞬间页面常停在
 *     某个会话上，自身头像不在 DOM 里，属正常态，过一会儿就有；
 *   - 已拿到 → 按 `everyMs` 长周期对齐手机侧改动。
 * 返回 0 表示不排（未启用/缺前提），调用方据此不建定时器。
 */
export function nextSelfProfileRecaptureMs(state = {}, cfg = {}) {
  const everyMs = Number(cfg.everyMs) || 0;
  const retryMs = Number(cfg.retryMs) || 0;
  if (everyMs <= 0) return 0;                       // 总闸关
  if (!state.hasAvatar && retryMs > 0) return retryMs;
  return everyMs;
}
