"use strict";

/* 双语气泡渲染模型（单一事实来源,桌面 preload 与浏览器扩展共用）。
 *
 * 现有 core.js 的译文展示是一块固定样式的 div,只有「有译文 / 无译文」两态,坐席看不出
 * 「正在翻 / 翻过了 / 已是原文 / 翻译失败 / 原文已变(陈旧)」。这既是体验问题（用户不知道
 * 哪条译过了）也是美化点（原文该弱化、译文该被强调）。
 *
 * 本模块把「一个气泡此刻该显示成什么样」收成纯函数 bubbleRenderModel(input)→model,
 * 渲染层（DOM / React / 桌面 renderer）只消费 model,零业务逻辑,天然可单测,也让明暗主题
 * 只需换 token 不改逻辑。
 *
 * 【译文单显不变量（P1.5, 2026-08-10）】注入框**只放译文,永不重复原文**——原文由官方
 * 气泡自身原生显示,我们的框追加在其下方,再放一遍原文既冗余又显「外挂」（实测入站气泡
 * 原文出现两遍）。故各状态的 lines 只含 role:"trans"；direction 仍收但不再据此铺原文行
 * （in/out 现在同口径）。媒体译文(OCR/转写)走 core.js::renderTranslation 另一条整块路径,
 * 「[图片原文]…」是它自带的,不受本不变量约束。
 *
 * 状态机 state：
 *   idle    未触发（手动翻译模式的初始态）
 *   loading 翻译在途
 *   done    有有意义译文（正常展示原文+译文）
 *   same    译文≈原文（已是目标语/回译雷同）→ 只标一枚「≈原文」,不铺整块译文
 *   error   翻译失败 → 可重试
 *   stale   原文已变化但译文还是旧的 → 提示「原文已更新,点重译」
 *   budget  额度不足被预算护栏拦下 → 提示充值/等待窗
 */

const _LABELS = {
  loading: "翻译中…",
  same: "≈ 原文",
  error: "翻译失败,点重试",
  stale: "原文已更新,点重译",
  budget: "翻译额度不足",
};

function _clampState(s) {
  const ok = ["idle", "loading", "done", "same", "error", "stale", "budget"];
  return ok.indexOf(s) >= 0 ? s : "idle";
}

// input: {
//   origText, direction("in"|"out"), state, translated, target_lang,
//   autoMode(bool 全局自动翻译档), reused(bool 命中缓存)
// }
// 返回渲染模型：
//   { show, state, cls, direction, badge, lines:[{role,text,muted,accent}], actionable, action }
function bubbleRenderModel(input) {
  const i = input || {};
  const state = _clampState(i.state);
  // origText 仍收（调用方按旧签名传）,但注入框不再铺原文行（译文单显不变量,见文件头）。
  const translated = String(i.translated == null ? "" : i.translated);
  const direction = i.direction === "out" ? "out" : "in";
  const model = {
    state,
    cls: "aitr-" + state,
    direction,
    badge: "",
    lines: [],
    show: false,
    actionable: false,
    action: "",
  };

  switch (state) {
    case "loading":
      // 重译场景先把旧译文留着（弱化）：一点「重新翻译」整块就消失再出现,既闪烁又让人
      // 以为点坏了。首译时没有旧译文,自然退化成纯徽标。原文由官方气泡原生显示,不重复。
      if (translated) {
        model.lines.push({ role: "trans", text: translated, muted: true, accent: false });
      }
      model.badge = _LABELS.loading;
      model.show = true;
      break;
    case "done":
      // 译文单显：只放译文（强调）,原文官方气泡已原生显示。in/out 现在同口径。
      if (translated) {
        model.lines.push({ role: "trans", text: translated, muted: false, accent: true });
      }
      model.show = model.lines.length > 0;
      model.actionable = true;
      model.action = "retranslate";
      break;
    case "same":
      // 已是目标语/雷同:不铺整块译文,仅挂一枚安静的角标,避免噪音
      model.badge = _LABELS.same;
      model.show = true;
      break;
    case "error":
      model.badge = _LABELS.error;
      model.show = true;
      model.actionable = true;
      model.action = "retry";
      break;
    case "stale":
      // 保留旧译文但标记陈旧 + 提供重译；原文（已更新）由官方气泡原生显示,不重复。
      if (translated) model.lines.push({ role: "trans", text: translated, muted: true, accent: false });
      model.badge = _LABELS.stale;
      model.show = true;
      model.actionable = true;
      model.action = "retranslate";
      break;
    case "budget":
      model.badge = _LABELS.budget;
      model.show = true;
      model.actionable = true;
      model.action = "topup";
      break;
    case "idle":
    default:
      // 手动档:只在需要时挂「点击翻译」按钮(由渲染层决定),模型本身不展示
      model.show = false;
      model.actionable = true;
      model.action = "translate";
      break;
  }
  return model;
}

// 从一次翻译结果（translate-scheduler.request 的返回）推导下一个 state。
// res: {ok, translated, meaningful, reason}
function stateFromResult(res) {
  if (!res) return "error";
  if (res.reason === "budget") return "budget";
  if (!res.ok) return "error";
  if (res.reason === "already_target" || res.reason === "same" || res.meaningful === false) return "same";
  return "done";
}

// ── 原文指纹（陈旧检测的判据）────────────────────────────────────────────────
// 译文渲染完就把「当时的原文」按指纹记在气泡上；下一轮扫描发现原文变了（对方编辑消息、
// 虚拟列表复用同一个 DOM 节点换了内容）就把旧译文标陈旧——否则旧译文会静默张冠李戴，
// 是比「没译」更糟的错误。写入与比较必须同一实现，否则原文没变也会被判陈旧。
function srcFingerprint(s) {
  return String(s == null ? "" : s).replace(/\s+/g, "").toLowerCase();
}

// 两个「宁可漏报不误报」的短路：没记过指纹（旧版本渲染过的气泡 / 媒体译文）不判陈旧；
// 当前取不到原文（DOM 抖动、懒卸载、选择器暂时失配）也不判——否则整屏译文会集体变黄。
function isStale(prevFingerprint, currentText) {
  const prev = String(prevFingerprint == null ? "" : prevFingerprint);
  if (!prev) return false;
  const cur = srcFingerprint(currentText);
  if (!cur) return false;
  return cur !== prev;
}

// ── 明暗主题 token + 样式表 ──────────────────────────────────────────────────
// P1 视觉「融入原生」（2026-08-10）：旧版把译文包在一个「左蓝条 + 浅蓝底」的外挂卡片里，
// 贴在官方气泡下方＝两套视觉语言，一眼就是「插件加的」。改法＝让译文读起来像气泡自带的
// 第二行：**去掉左边框/背景色块**，正文用 `color:inherit`（继承官方气泡自身文字色，出站
// 彩色气泡上也天然正确）、译文同色仅加粗、原文降为 opacity 弱化，中间只留一条极浅发丝分隔线。
// 附带收益：旧版「白底块糊在深色流里」的暗色顽疾，因背景透明 + 文字继承而基本消失
// （token 仅剩状态色/发丝线需要明暗切换）。--aitr-hair＝发丝线（明暗各一，中性低透明度，
// 不依赖 color-mix，任意内嵌 Chromium 版本都稳）。
const _TOK_LIGHT =
  "--aitr-fg:#111827;--aitr-muted:#6b7280;--aitr-accent:#1c7ed6;" +
  "--aitr-hair:rgba(0,0,0,.14);" +
  "--aitr-fab-bg:rgba(255,255,255,.92);--aitr-fab-bd:rgba(28,126,214,.35);" +
  "--aitr-fab-bg-h:#ffffff;" +
  "--aitr-warn:#b45309;--aitr-err:#c92a2a";
const _TOK_DARK =
  "--aitr-fg:#e8eaed;--aitr-muted:#9aa0a6;--aitr-accent:#6cb6ff;" +
  "--aitr-hair:rgba(255,255,255,.16);" +
  "--aitr-fab-bg:rgba(32,38,46,.94);--aitr-fab-bd:rgba(108,182,255,.42);" +
  "--aitr-fab-bg-h:rgba(42,50,60,.98);" +
  "--aitr-warn:#f0b429;--aitr-err:#ff8787";

// 宿主自带的深色标记（Telegram Web K：html.night / .theme-dark；WhatsApp：body.dark /
// [data-theme=dark]）。官方页的深色是**应用内**开关而不是系统开关，只跟
// prefers-color-scheme 会在「系统浅色 + 应用深色」这个最常见组合上,把译文块渲染成
// 一条白底色块糊在深色聊天流里。故两套判据都要覆盖,且宿主标记必须排在 media query
// 之后（同特异性下后者胜）才能赢。
const _DARK_HOSTS = [
  "html.night", "html.theme-dark", "html[data-theme='dark']",
  "body.dark", "body[data-theme='dark']", "[data-theme='dark']",
];

// 返回注入用样式表文本。逻辑全在 model 里,这里只有 token 与排版 → 换肤不改代码。
function bubbleStyleCss() {
  // #aitr-smart（智能回复浮钮）一并纳入 token 作用域,与气泡同一套明暗色,不再自带硬编码蓝。
  const scope = ".aitr-box,.aitr-btn,#aitr-smart";
  const darkSel = _DARK_HOSTS
    .map((h) => h + " .aitr-box," + h + " .aitr-btn," + h + " #aitr-smart").join(",");
  return [
    scope + "{" + _TOK_LIGHT + "}",
    "@media (prefers-color-scheme:dark){" + scope + "{" + _TOK_DARK + "}}",
    darkSel + "{" + _TOK_DARK + "}",
    // done 主体：原生化——无左边框、无背景块，仅一条发丝线把译文与原文分开；文字继承气泡自身色。
    ".aitr-box{display:block;margin-top:3px;padding-top:4px;" +
      "border-top:1px solid var(--aitr-hair);" +
      "font-size:13px;line-height:1.45;white-space:pre-wrap;word-break:break-word;" +
      "color:inherit}",
    ".aitr-line{display:block}",
    ".aitr-line+.aitr-line{margin-top:2px}",
    ".aitr-orig{opacity:.58;font-size:12px}",       // 原文降噪：同色弱化,随气泡背景自适应
    ".aitr-trans{font-weight:500}",                  // 译文＝气泡原生文字色,仅加粗
    ".aitr-badge{display:inline-block;margin-top:2px;font-size:11px;opacity:.6}",
    ".aitr-box.aitr-loading{border-top-style:dashed}",
    // same：只是一枚「≈原文」小角标,连发丝线都不要,零存在感
    ".aitr-box.aitr-same{border-top:none;padding-top:0;margin-top:2px}",
    // 问题态（陈旧/失败/额度）＝罕见且需坐席注意：给发丝线 + 角标上色,仍不铺整块底色
    ".aitr-box.aitr-stale{border-top-color:var(--aitr-warn)}",
    ".aitr-box.aitr-stale .aitr-badge{color:var(--aitr-warn);opacity:1}",
    ".aitr-box.aitr-error{border-top-color:var(--aitr-err)}",
    ".aitr-box.aitr-error .aitr-badge{color:var(--aitr-err);opacity:1}",
    ".aitr-box.aitr-budget{border-top-color:var(--aitr-warn)}",
    ".aitr-box.aitr-budget .aitr-badge{color:var(--aitr-warn);opacity:1}",
    // 翻译触发＝ghost 文字链：平时淡（opacity .6）,hover 才显现,不再是贴满每条的蓝胶囊
    ".aitr-btn{display:inline-block;margin-top:4px;font-size:11px;cursor:pointer;" +
      "color:var(--aitr-accent);opacity:.6;user-select:none;" +
      "background:none;border:0;padding:0}",
    ".aitr-btn:hover{opacity:1;text-decoration:underline}",
    ".aitr-btn[data-aitr-busy]{opacity:.45;cursor:default;text-decoration:none}",
    // 智能回复浮钮：从「实心蓝 + 🤖 + 重阴影」的外挂胶囊,改成安静的主题化 ghost chip
    // ——近实底(保证浮在任意聊天内容上仍可读) + 发丝描边 + 强调色文字/图标 + 极轻阴影。
    "#aitr-smart{position:fixed;right:16px;bottom:88px;z-index:99999;" +
      "display:inline-flex;align-items:center;gap:5px;padding:6px 12px;" +
      "border-radius:16px;font-size:12px;cursor:pointer;user-select:none;" +
      "color:var(--aitr-accent);background:var(--aitr-fab-bg);" +
      "border:1px solid var(--aitr-fab-bd);box-shadow:0 1px 3px rgba(0,0,0,.14)}",
    "#aitr-smart:hover{background:var(--aitr-fab-bg-h)}",
    "#aitr-smart[data-aitr-busy]{opacity:.6;cursor:default}",
    ".aitr-fab-ic{width:13px;height:13px;flex:none;display:block}",
  ].join("\n");
}

const _api = { bubbleRenderModel, stateFromResult, srcFingerprint, isStale, bubbleStyleCss };
if (typeof module !== "undefined" && module.exports) module.exports = _api;
if (typeof globalThis !== "undefined") globalThis.ABubbleModel = _api;
