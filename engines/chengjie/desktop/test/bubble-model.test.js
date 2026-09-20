"use strict";

// 双语气泡渲染模型纯函数单测（无框架,node 直跑）：node test/bubble-model.test.js
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const {
  bubbleRenderModel, stateFromResult, srcFingerprint, isStale, bubbleStyleCss,
} = require("../../shared/inject/bubble-model.js");

let pass = 0;
function ok(name, cond) { assert.ok(cond, name); pass++; }

// ── stateFromResult：翻译结果 → 下一状态 ─────────────────────────────────────
ok("state null→error", stateFromResult(null) === "error");
ok("state budget", stateFromResult({ reason: "budget" }) === "budget");
ok("state ok:false→error", stateFromResult({ ok: false }) === "error");
ok("state already_target→same", stateFromResult({ ok: true, reason: "already_target" }) === "same");
ok("state 不 meaningful→same", stateFromResult({ ok: true, meaningful: false }) === "same");
ok("state 有意义→done", stateFromResult({ ok: true, meaningful: true, translated: "x" }) === "done");

// ── loading ───────────────────────────────────────────────────────────────────
const load = bubbleRenderModel({ state: "loading", direction: "in", origText: "hi" });
ok("loading 有徽标", load.badge.indexOf("翻译中") >= 0 && load.show === true);
ok("首译 loading 无译文行", load.lines.length === 0);
// 重译：旧译文必须留着（弱化）,否则一点「重新翻译」整块消失再出现 = 闪烁 + 疑似点坏。
// P1.5：注入框译文单显——只留一行译文,不重复原文（官方气泡已原生显示）。
const reload = bubbleRenderModel({ state: "loading", direction: "in", origText: "hi", translated: "旧译" });
ok("重译 loading 保留旧译(单行译文)", reload.lines.length === 1 && reload.lines[0].text === "旧译");
ok("重译 loading 旧译弱化", reload.lines[0].muted === true && reload.lines[0].accent === false);
ok("重译 loading 仍有徽标", reload.badge.indexOf("翻译中") >= 0);
const reloadOut = bubbleRenderModel({ state: "loading", direction: "out", origText: "hi", translated: "旧译" });
ok("重译 loading 出站同口径单行译文", reloadOut.lines.length === 1 && reloadOut.lines[0].role === "trans");

// ── done（译文单显：只显译文,原文由官方气泡原生显示）──────────────────────────
const doneIn = bubbleRenderModel({ state: "done", direction: "in", origText: "hello", translated: "你好" });
ok("done 单行译文", doneIn.lines.length === 1);
ok("done 译文 accent", doneIn.lines[0].role === "trans" && doneIn.lines[0].accent === true && doneIn.lines[0].muted === false);
ok("done 可重译", doneIn.actionable === true && doneIn.action === "retranslate");
ok("done 展示", doneIn.show === true);

// ── done（出站:同口径,只显译文）──────────────────────────────────────────────
const doneOut = bubbleRenderModel({ state: "done", direction: "out", origText: "在吗", translated: "you there?" });
ok("done 出站单行译文", doneOut.lines.length === 1 && doneOut.lines[0].role === "trans");

// ── 译文单显不变量：任何状态都不得铺 orig 行（防重复原文回潮）────────────────────
["done", "stale", "loading"].forEach((s) => {
  const m = bubbleRenderModel({ state: s, direction: "in", origText: "hello world", translated: "你好世界" });
  ok(`${s} 注入框无 orig 行(不重复原文)`, m.lines.every((l) => l.role !== "orig"));
});
// out 方向同样不铺原文（本就如此,一并钉住）
ok("done 入站/出站行数一致(同口径)",
  bubbleRenderModel({ state: "done", direction: "in", origText: "a", translated: "b" }).lines.length
  === bubbleRenderModel({ state: "done", direction: "out", origText: "a", translated: "b" }).lines.length);

// ── same / error / stale / budget / idle ────────────────────────────────────
const same = bubbleRenderModel({ state: "same", direction: "in", origText: "你好" });
ok("same 只挂角标", same.badge.indexOf("原文") >= 0 && same.lines.length === 0 && same.show === true);

const err = bubbleRenderModel({ state: "error", direction: "in", origText: "x" });
ok("error 可重试", err.actionable === true && err.action === "retry" && err.badge.indexOf("失败") >= 0);

const stale = bubbleRenderModel({ state: "stale", direction: "in", origText: "new", translated: "旧译" });
ok("stale 保留旧译但标陈旧(单行译文)", stale.badge.indexOf("原文已更新") >= 0 && stale.lines.length === 1);
ok("stale 旧译 muted", stale.lines[0].muted === true && stale.action === "retranslate");

const bud = bubbleRenderModel({ state: "budget", direction: "in", origText: "x" });
ok("budget 提示充值", bud.badge.indexOf("额度") >= 0 && bud.action === "topup");

const idle = bubbleRenderModel({ state: "idle", direction: "in", origText: "x" });
ok("idle 不展示但可触发翻译", idle.show === false && idle.action === "translate");

ok("非法 state→idle", bubbleRenderModel({ state: "weird" }).state === "idle");
ok("null 输入安全", bubbleRenderModel(null).state === "idle");
ok("cls 前缀", bubbleRenderModel({ state: "done" }).cls === "aitr-done");

// ── 原文指纹 / 陈旧检测 ───────────────────────────────────────────────────────
ok("指纹忽略空白与大小写",
  srcFingerprint(" Hello  World \n") === srcFingerprint("helloworld"));
ok("指纹区分内容", srcFingerprint("abc") !== srcFingerprint("abd"));
ok("指纹 null 安全", srcFingerprint(null) === "" && srcFingerprint(undefined) === "");

ok("原文变了→陈旧", isStale(srcFingerprint("hello"), "hello world") === true);
ok("只差空白→不陈旧", isStale(srcFingerprint("hello world"), " hello   world ") === false);
// 两条「宁可漏报不误报」：没记过指纹、当前取不到原文,都不许判陈旧（否则整屏译文集体变黄）
ok("无指纹→不判陈旧", isStale("", "anything") === false);
ok("原文取空→不判陈旧", isStale(srcFingerprint("hello"), "") === false);
ok("原文 null→不判陈旧", isStale(srcFingerprint("hello"), null) === false);

// ── 样式表：明暗 token + 状态色 + 类名契约 ────────────────────────────────────
const css = bubbleStyleCss();
// 类名契约：profiles.js 取原文时按 `.aitr-box,.aitr-btn` 摘注入物,改名会静默让译文
// 被当成原文读回去 → 这两个类名是跨模块契约,不能只在 CSS 里改。
ok("类名契约 aitr-box", css.indexOf(".aitr-box{") >= 0);
ok("类名契约 aitr-btn", css.indexOf(".aitr-btn{") >= 0);
ok("有浅色 token", css.indexOf("--aitr-fg:#111827") >= 0);
ok("有深色 token", css.indexOf("--aitr-fg:#e8eaed") >= 0);
// 官方页的深色是应用内开关（html.night / body.dark）而非系统开关：宿主标记规则必须排在
// prefers-color-scheme 之后才能在「系统浅色 + 应用深色」下赢,否则译文块是白底糊深色流里。
const _mq = css.indexOf("prefers-color-scheme");
const _host = css.indexOf("html.night");
ok("宿主深色规则排在 media query 之后", _mq >= 0 && _host > _mq);
ok("深色宿主覆盖 whatsapp 口径", css.indexOf("body.dark .aitr-box") >= 0);
// P1 原生化：原文用 opacity 弱化（随气泡背景自适应）、译文继承气泡文字色仅加粗（非蓝色 callout）
ok("原文弱化(opacity)/译文原生加粗", css.indexOf(".aitr-orig{opacity") >= 0
  && css.indexOf(".aitr-trans{font-weight") >= 0);
// 问题态改用「发丝线上色」而非左边框（整体已无左边框）
ok("陈旧走警示色(发丝线)", css.indexOf(".aitr-box.aitr-stale{border-top-color:var(--aitr-warn)") >= 0);
ok("失败走错误色(发丝线)", css.indexOf(".aitr-box.aitr-error{border-top-color:var(--aitr-err)") >= 0);
ok("忙态按钮有视觉反馈", css.indexOf('.aitr-btn[data-aitr-busy]') >= 0);

// ── P1 原生化不变量（防「外挂蓝框」被改回来）─────────────────────────────────
// done 主体必须：无左边框、无背景块、文字继承气泡自身色（这三条才是「融入而非外挂」）。
ok("done 框去左边框(不再是外挂卡片)", css.indexOf("border-left") < 0);
ok("done 框无蓝底色块", css.indexOf("background:var(--aitr-accent-weak)") < 0);
ok("done 框继承气泡文字色", /\.aitr-box\{[^}]*color:inherit/.test(css));
ok("done 框以发丝线分隔", /\.aitr-box\{[^}]*border-top:1px solid var\(--aitr-hair\)/.test(css)
  && css.indexOf("--aitr-hair:rgba(0,0,0,.14)") >= 0
  && css.indexOf("--aitr-hair:rgba(255,255,255,.16)") >= 0);
// 翻译触发＝ghost 文字链（平时淡、hover 显现），不再是贴满每条的蓝胶囊
ok("翻译按钮 ghost 化(平时淡)", /\.aitr-btn\{[^}]*opacity:\.6/.test(css)
  && css.indexOf(".aitr-btn:hover{opacity:1") >= 0);
ok("翻译按钮不再是胶囊(无圆角背景块)", /\.aitr-btn\{[^}]*background:none/.test(css)
  && /\.aitr-btn\{(?![^}]*border-radius)/.test(css));
// 每个状态 cls 都得有落地样式,否则 model 出了状态而 UI 看不出区别
["loading", "same", "stale", "error", "budget"].forEach((s) => {
  ok(`状态 ${s} 有样式`, css.indexOf(".aitr-box.aitr-" + s) >= 0);
});

// ── XSS 硬不变量：译文渲染路径禁止拼 HTML ────────────────────────────────────
// 原文/译文来自对方消息,完全不可信。core.js 全程 textContent；谁哪天为了加个 <b> 改成
// innerHTML,这条就红。
const coreSrc = fs.readFileSync(
  path.join(__dirname, "..", "..", "shared", "inject", "core.js"), "utf8"
);
ok("core.js 零 innerHTML 赋值", /\.innerHTML\s*=/.test(coreSrc) === false);
ok("core.js 零 insertAdjacentHTML", coreSrc.indexOf("insertAdjacentHTML") < 0);

// P1：CSP 回落内联样式也须原生化（样式表被拦时不能露出旧的外挂蓝框）
ok("CSP 回落框内联去外挂蓝框", coreSrc.indexOf("border-left:2px solid #3aa0ff") < 0
  && /_BOX_INLINE\s*=[\s\S]*?border-top:1px solid rgba\(128,128,128/.test(coreSrc));

// ── 媒体翻译状态对齐（连点防护 + 加载/错误进盒子，与文本翻译同一套视觉）──────────
// 修真 bug：媒体路径此前无忙态闸门，点两下"翻译图片"＝两次后端 OCR；且失败只落按钮文字。
{
  const mt = coreSrc.slice(coreSrc.indexOf("function translateMediaBubble"),
    coreSrc.indexOf("function _aitrNorm"));
  ok("媒体连点防护(忙态闸门)", mt.indexOf('getAttribute("data-aitr-busy")') > 0
    && mt.indexOf('setAttribute("data-aitr-busy"') > 0 && mt.indexOf("removeAttribute") > 0);
  ok("媒体加载进盒子", /renderMediaBox\(bubble,\s*"loading"/.test(mt));
  ok("媒体失败进错误盒子(非仅按钮文字)", /renderMediaBox\(bubble,\s*"error"/.test(mt));
  ok("媒体完成进 done 盒子", /renderMediaBox\(bubble,\s*"done"/.test(mt));
  ok("媒体失败给重试出路", mt.indexOf('"重试"') > 0);
  // renderMediaBox 复用文本同一套盒子类（aitr-<state>），不另造样式
  ok("renderMediaBox 复用盒子状态类",
    /function renderMediaBox[\s\S]*?box\.className\s*=\s*"aitr-box aitr-"\s*\+\s*state/.test(coreSrc));
}

// ── P2：智能回复浮钮收编（去 emoji + 主题化 ghost chip + 连点防护）─────────────
// 样式在 bubble-model 的 #aitr-smart（走明暗 token,不再自带硬编码蓝实心 + 重阴影）
ok("FAB 样式进样式表(主题化)", css.indexOf("#aitr-smart{") >= 0
  && /#aitr-smart\{[^}]*color:var\(--aitr-accent\)/.test(css)
  && /#aitr-smart\{[^}]*background:var\(--aitr-fab-bg\)/.test(css));
ok("FAB 有 hover 与忙态样式", css.indexOf("#aitr-smart:hover{") >= 0
  && css.indexOf("#aitr-smart[data-aitr-busy]{") >= 0);
ok("FAB 去实心蓝(不再 background:#3aa0ff)", css.indexOf("background:#3aa0ff") < 0);
{
  // 切整个 FAB 区块（含 _fabSparkIcon 图标助手 + mountSmartReplyButton）
  const msb = coreSrc.slice(coreSrc.indexOf("const _FAB_INLINE"),
    coreSrc.indexOf("function autostart"));
  // 去 emoji：🤖 既是外挂大头,也向坐席暴露「机器人」
  ok("FAB 去 emoji(🤖)", msb.indexOf("\u{1F916}") < 0);
  // 图标用 SVG(createElementNS)，不用 innerHTML（core.js 全局禁 innerHTML）
  ok("FAB 图标走 createElementNS", msb.indexOf("createElementNS") > 0);
  // 忙态只换文字 span,不 fab.textContent=（那会连 SVG 图标一起抹掉）
  ok("FAB 忙态换文字 span 而非整体 textContent",
    /fab\.textContent\s*=/.test(msb) === false
    && msb.indexOf("aitr-fab-tx") > 0 && /tx\.textContent\s*=/.test(msb));
  // 连点防护:一次生成在途不重复触发（防重复烧 LLM / 重复填入）
  ok("FAB 连点防护", msb.indexOf('getAttribute("data-aitr-busy")') > 0
    && msb.indexOf('setAttribute("data-aitr-busy"') > 0);
  // CSP 回落:样式表被拦时用内联 ghost（仍去实心蓝/重阴影）
  ok("FAB 有 CSP 回落内联", coreSrc.indexOf("_FAB_INLINE") > 0
    && coreSrc.indexOf("background:#3aa0ff") < 0);
}

// ── 静态接线门禁：模块写好了但没接上，是本仓反复踩过的静默缺陷 ────────────────
// P0 起带 full 参（自愈全量轮不看脏标记）；钉的语义不变＝扫描循环里必须调 refreshStale
ok("陈旧刷新已接进扫描循环", /refreshStale\(b(,\s*full)?\)/.test(coreSrc));
// 指纹只在「已按当前原文出过结论」时落：error/budget 也落会把一次失败固化成永久错译
ok("指纹只在 done/same 落",
  /if \(state === "done" \|\| state === "same"\) setSrcFingerprint/.test(coreSrc));
// 自动档不铺 loading 框（首屏几十个虚线框的视觉噪音 + 两轮白重建）
ok("自动档 loading 框有 immediate 闸门", /if \(immediate\) \{\s*renderBubbleState/.test(coreSrc));
// 内联样式优先级高于样式表：样式表生效时不能再写内联，否则 hover/忙态/暗色全被压死
ok("按钮内联样式受样式表闸门约束", /if \(!ensureStyle\(\)\) b\.style\.cssText/.test(coreSrc));
ok("译文框内联样式受样式表闸门约束", /if \(!ensureStyle\(\)\) box\.style\.cssText/.test(coreSrc));

// refreshStale 跑在每轮扫描 × 每条已译气泡上（长会话几百条）。两条顺序不变量：
//   ① 便宜的短路（已标陈旧 / 翻译在途）必须排在贵的取文+指纹之前；
//   ② 翻译在途必须直接退出——此刻指纹还没落,必判"变了",标了又马上被 done 覆盖＝白闪。
{
  const fn = coreSrc.slice(coreSrc.indexOf("function refreshStale"));
  const body = fn.slice(0, fn.indexOf("\n  }\n"));
  const iStale = body.indexOf('indexOf("aitr-stale")');
  const iBusy = body.indexOf('data-aitr-busy');
  const iText = body.indexOf("bubbleVisibleText");
  ok("陈旧短路在取文之前", iStale > 0 && iText > 0 && iStale < iText);
  ok("在途短路在取文之前", iBusy > 0 && iBusy < iText);
  ok("在途直接退出而非仅跳过改字", /data-aitr-busy"\)\) return;/.test(body));
}

const tgSrc = fs.readFileSync(
  path.join(__dirname, "..", "inject", "tg-inject.js"), "utf8"
);
ok("桌面 preload 已把渲染模型接进 createInject", /createInject\([^)]*bubbleModel/.test(tgSrc));

console.log(`bubble-model.test.js: ${pass} passed`);
