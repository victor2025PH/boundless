"use strict";

// 活动海报弹层（P0 2026-08-21 新人 6U；P0+P1 2026-08-22 可诊断化 + 美化批）。
// 分工：选品/频控/资格全在主进程（campaign-model.js 纯函数 + userData 状态），
// 本文件只做 DOM 渲染 + 倒计时 tick + 交互回执。
//
// 展示时序铁律：首启向导 > 强制升级横幅 > 本海报 > 普通公告——
//  · 向导未完成（onboarding 未写）时主进程 audience=new_user 判定天然返回 null；
//  · --first-run 演示会话默认退出；但**本会话向导已完成**（sessionStorage 标记，
//    与 first-run.js 同键）后放行——修「完成向导 reload 后 env 仍在 → 海报被压制
//    整个会话，验收永远看不到」；
//  · z-index 99980 < 向导的 99999，即便时序竞争也压不住向导。
// 倒计时 tick 只更新既有节点文本/样式，不整块重渲（cp-voice 撤销按钮被秒杀的教训）。
//
// P0 可诊断化：主进程现在回 {campaign:null, skip:{reason}}——本层把 reason 发成
// poster6u_skip_{reason} 埋点（暗面漏斗：多少台被哪道闸门拦住，ui-event-trend 可读）。
// P0 验收通道：--poster-preview（shell.posterPreview）跳资格/频控强制弹出，
// 埋点走独立 poster6u_preview、不落 shown 频控——不污染生产读数。
// P1 美化：入场动画 / CountUp 数字滚动 / 倒计时紧迫递进（<24h 转橙红）/ 光泽跟随 /
// 金色流光描边 / CTA 价值分层（按钮放价值、价格进 ctaSub 小字）/ 第 2/3 次曝光降级
// 右下角非模态角卡（减打扰）/ poster-hero 版式内置礼盒 SVG（文本仍全部来自 feed）。
// 全部动效尊重 prefers-reduced-motion（降级为静态呈现，功能不减）。
(function () {
  var shell = window.shell || {};
  var CM = window.CampaignModel || null;
  if (!CM) return;
  var PREVIEW = !!shell.posterPreview;
  if (shell.forceFirstRun && !PREVIEW) {
    // 客服在演示首启向导：营销弹窗别抢戏。例外＝本会话向导已完成（finish 会写
    // sessionStorage 再 reload），此时向导已谢幕、onboarding 刚刷新，正是首个展示时机。
    var wizardDoneThisSession = false;
    try { wizardDoneThisSession = !!sessionStorage.getItem("aitr_firstrun_v1"); } catch (e) { /* 不可用按未完成 */ }
    if (!wizardDoneThisSession) return;
  }
  if (!shell.campaignPoster) return; // 老 preload（未接活动桥）：静默无海报

  var FONT = "'Inter','PingFang SC','Microsoft YaHei',system-ui,sans-serif";
  var REDUCED = false;
  try {
    REDUCED = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  } catch (e) { /* 探测失败按有动效 */ }

  function esc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function beacon(action) {
    try {
      if (shell.uiEvent) shell.uiEvent({ page: "desktop-shell", action: action });
    } catch (e) { /* 埋点绝不阻断海报 */ }
  }
  /** 生产埋点：预览态一律不发（预览有独立的 poster6u_preview，不污染漏斗读数）。 */
  function beaconLive(action) { if (!PREVIEW) beacon(action); }

  // @keyframes 写不进内联 style 属性 → 注入一次 <style>（CSP style-src 'unsafe-inline' 放行）。
  function ensureStyle() {
    if (document.getElementById("camp-poster-style")) return;
    var st = document.createElement("style");
    st.id = "camp-poster-style";
    st.textContent = ""
      + "@keyframes campSpin{to{transform:rotate(360deg)}}"
      + "@keyframes campIn{from{opacity:0;transform:translateY(12px) scale(.96)}to{opacity:1;transform:none}}"
      + "@keyframes campCornerIn{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:none}}"
      + "@keyframes campMaskIn{from{opacity:0}to{opacity:1}}"
      + "#camp-cta{transition:filter .15s ease,transform .15s ease}"
      + "#camp-cta:hover{filter:brightness(1.07);transform:translateY(-1px)}";
    try { document.head.appendChild(st); } catch (e) { /* 缺 head 时动效降级，卡片仍渲染 */ }
  }

  // 礼盒视觉资产（poster-hero 版式，P1）：内置 SVG 随安装包走——feed 只下发文本的
  // 安全红线不变；老客户端未知版式整条丢弃（cmNormalizeFeed 白名单），天然向后兼容。
  var GIFT_SVG = '<svg width="92" height="92" viewBox="0 0 96 96" fill="none" aria-hidden="true">'
    + '<circle cx="48" cy="48" r="44" fill="rgba(252,211,77,.08)"/>'
    + '<rect x="22" y="42" width="52" height="38" rx="6" fill="#2a2140" stroke="#fcd34d" stroke-width="2.5"/>'
    + '<rect x="18" y="30" width="60" height="14" rx="5" fill="#342a4e" stroke="#fcd34d" stroke-width="2.5"/>'
    + '<path d="M48 30v50" stroke="#fcd34d" stroke-width="5"/>'
    + '<path d="M48 30c-12 0-16-12-8-15 6-2 9 7 8 15zm0 0c12 0 16-12 8-15-6-2-9 7-8 15z" fill="#f59e0b" stroke="#fcd34d" stroke-width="2"/>'
    + '<path d="M78 18l2.4 5.6L86 26l-5.6 2.4L78 34l-2.4-5.6L70 26l5.6-2.4z" fill="#fcd34d"/>'
    + '<path d="M16 58l1.8 4.2L22 64l-4.2 1.8L16 70l-1.8-4.2L10 64l4.2-1.8z" fill="#fcd34d" opacity=".7"/>'
    + '</svg>';

  /** headline 渲染：最大数字（≥1000）拆出来做金色渐变 + CountUp 滚动，其余原样。 */
  function headlineHtml(text) {
    var hp = CM.cmHeadlineParts(text);
    if (!hp) return esc(text);
    return esc(hp.before)
      + '<span id="camp-num" data-value="' + esc(String(hp.value)) + '" data-text="' + esc(hp.num) + '" '
      + 'style="background:linear-gradient(92deg,#fde68a,#f59e0b);-webkit-background-clip:text;'
      + 'background-clip:text;color:transparent;font-variant-numeric:tabular-nums">'
      + esc(hp.num) + "</span>" + esc(hp.after);
  }

  /** CountUp：0 → 目标值 ~800ms ease-out；只写 textContent；reduced-motion 直接终值。 */
  function runCountUp(root) {
    var el = root.querySelector("#camp-num");
    if (!el) return;
    var target = Number(el.getAttribute("data-value")) || 0;
    var finalText = el.getAttribute("data-text") || String(target);
    if (REDUCED || !target || typeof requestAnimationFrame !== "function") {
      el.textContent = finalText;
      return;
    }
    var grouped = finalText.indexOf(",") >= 0;
    var start = 0;
    var DUR = 800;
    function step(ts) {
      if (!start) start = ts;
      var p = Math.min(1, (ts - start) / DUR);
      var eased = 1 - Math.pow(1 - p, 3);
      if (p >= 1) { el.textContent = finalText; return; }
      var v = Math.round(target * eased);
      el.textContent = grouped ? v.toLocaleString("en-US") : String(v);
      requestAnimationFrame(step);
    }
    requestAnimationFrame(step);
  }

  // 倒计时 pill 两档样式（P1 紧迫递进）：常规金色 → 剩余 <24h 橙红加急。
  // 档位切换只 setAttribute 一次（urgentApplied 闸），tick 平时只写时间 span。
  var CD_STYLE_BASE = "display:inline-flex;align-items:center;gap:7px;margin-top:14px;"
    + "background:rgba(252,211,77,.12);border:1px solid rgba(252,211,77,.3);"
    + "border-radius:999px;padding:6px 14px;font-size:13px;color:#fcd34d";
  var CD_STYLE_URGENT = "display:inline-flex;align-items:center;gap:7px;margin-top:14px;"
    + "background:rgba(248,113,113,.14);border:1px solid rgba(248,113,113,.45);"
    + "border-radius:999px;padding:6px 14px;font-size:13px;color:#fca5a5";

  function countdownHtml(deadline, zh, compact) {
    if (!deadline) return "";
    var style = compact
      ? CD_STYLE_BASE.replace("margin-top:14px", "margin-top:10px").replace("font-size:13px", "font-size:12px")
      : CD_STYLE_BASE;
    return '<div id="camp-cd" style="' + style + '" data-compact="' + (compact ? "1" : "") + '">'
      + '⏳ <span id="camp-cd-label">' + esc(zh ? "限时剩余" : "Time left") + "</span>"
      + '<b id="camp-countdown" style="font-variant-numeric:tabular-nums;font-size:1.1em"></b></div>';
  }

  /** 启动倒计时 tick：只更新 span 文本；跨入紧迫带时一次性换 pill 样式+文案；到点 onExpire。 */
  function startCountdown(root, deadline, zh, onExpire) {
    if (!deadline) return 0;
    var pill = root.querySelector("#camp-cd");
    var span = root.querySelector("#camp-countdown");
    var label = root.querySelector("#camp-cd-label");
    var compact = !!(pill && pill.getAttribute("data-compact"));
    var urgentApplied = false;
    var tick = function () {
      var left = deadline - Date.now();
      if (left <= 0) { onExpire(); return; }
      if (span) span.textContent = CM.cmCountdownText(left);
      if (!urgentApplied && CM.cmCountdownUrgent(left)) {
        urgentApplied = true;
        if (pill) {
          var st = compact
            ? CD_STYLE_URGENT.replace("margin-top:14px", "margin-top:10px").replace("font-size:13px", "font-size:12px")
            : CD_STYLE_URGENT;
          pill.setAttribute("style", st);
          pill.setAttribute("data-compact", compact ? "1" : "");
        }
        if (label) label.textContent = zh ? "即将截止 · 仅剩" : "Ending soon · only";
      }
    };
    tick();
    return setInterval(tick, 1000);
  }

  /** 光泽跟随（ShineCard 简化版）：mousemove → 径向高光贴着指针走；reduced-motion 不装。 */
  function wireShine(card) {
    if (REDUCED) return;
    var shine = document.createElement("div");
    shine.setAttribute("style", "position:absolute;inset:0;pointer-events:none;opacity:0;"
      + "transition:opacity .25s ease;border-radius:inherit");
    card.appendChild(shine);
    var lastX = 0;
    var lastY = 0;
    var raf = 0;
    card.addEventListener("mousemove", function (ev) {
      lastX = ev.clientX;
      lastY = ev.clientY;
      if (raf) return;
      raf = requestAnimationFrame(function () {
        raf = 0;
        var r = card.getBoundingClientRect();
        shine.style.opacity = "1";
        shine.style.background = "radial-gradient(360px circle at " + (lastX - r.left) + "px "
          + (lastY - r.top) + "px, rgba(252,211,77,.10), transparent 62%)";
      });
    });
    card.addEventListener("mouseleave", function () { shine.style.opacity = "0"; });
  }

  function bulletsHtml(c, zh) {
    var bullets = (zh ? c.bullets.zh : (c.bullets.en.length ? c.bullets.en : c.bullets.zh)) || [];
    var html = "";
    for (var i = 0; i < bullets.length; i++) {
      html += "<li style='display:flex;gap:8px;align-items:flex-start;margin-top:7px;"
        + "font-size:12.5px;line-height:1.55;color:#c6cede'>"
        + "<span style='color:#fcd34d;flex:none'>✓</span><span>" + esc(bullets[i]) + "</span></li>";
    }
    return html;
  }

  function ctaBlockHtml(c, t, zh) {
    var sub = t(c.ctaSub);
    return "<div style='display:flex;gap:10px;align-items:center;margin-top:20px'>"
      + "<button id='camp-cta' style='flex:1;padding:12px 18px;border:0;border-radius:999px;cursor:pointer;"
      + "font:inherit;font-size:14px;font-weight:700;color:#141122;"
      + "background:linear-gradient(90deg,#fcd34d,#f59e0b);box-shadow:0 0 26px rgba(252,211,77,.25)'>"
      + esc(t(c.ctaLabel) || (zh ? "立即领取" : "Claim now")) + "</button>"
      + "<button id='camp-later' style='padding:12px 16px;background:transparent;color:#9aa5c4;"
      + "border:1px solid rgba(255,255,255,.14);border-radius:999px;cursor:pointer;font:inherit;font-size:13px'>"
      + esc(t(c.dismissLabel) || (zh ? "稍后再说" : "Maybe later")) + "</button></div>"
      + (sub ? "<div style='text-align:center;margin-top:9px;font-size:11.5px;color:#b8a468'>" + esc(sub) + "</div>" : "")
      + "<div style='text-align:center;margin-top:" + (sub ? "8px" : "12px") + "'>"
      + "<a id='camp-never' href='javascript:void(0)' style='font-size:11px;color:#5f6a8a;text-decoration:none'>"
      + esc(t(c.neverLabel) || (zh ? "不再提醒" : "Don't show again")) + "</a></div>";
  }

  /** 首曝光：全屏蒙层大卡（金色流光描边 + 光泽跟随 + 入场动画 + CountUp）。 */
  function showModal(payload, c, lang, deadline, zh) {
    var t = function (block) { return CM.cmLangText(block, lang); };
    var isHero = c.layout === "poster-hero";

    var mask = document.createElement("div");
    mask.id = "campaign-poster-mask";
    mask.setAttribute("style", [
      "position:fixed;inset:0;z-index:99980",
      "background:rgba(12,15,26,.78);backdrop-filter:blur(5px)",
      "display:flex;align-items:center;justify-content:center",
      "font-family:" + FONT,
      REDUCED ? "" : "animation:campMaskIn .2s ease",
    ].join(";"));

    // 流光描边结构：wrap（1px 内衬 + overflow:hidden）内衬旋转 conic 光带，
    // 卡片不透明盖在上面 → 只有 1px 环缝透出光带 = 金色流光描边（零依赖纯 CSS）。
    var wrap = document.createElement("div");
    wrap.setAttribute("style", [
      "position:relative;width:520px;max-width:92vw;border-radius:19px;padding:1px;overflow:hidden",
      REDUCED ? "background:rgba(252,211,77,.38)" : "background:rgba(252,211,77,.16)",
      REDUCED ? "" : "animation:campIn .28s cubic-bezier(.2,.8,.3,1)",
    ].join(";"));
    if (!REDUCED) {
      var beam = document.createElement("div");
      beam.setAttribute("style", "position:absolute;left:-60%;top:-60%;width:220%;height:220%;"
        + "pointer-events:none;animation:campSpin 6s linear infinite;"
        + "background:conic-gradient(rgba(252,211,77,0) 0deg,rgba(252,211,77,.7) 34deg,rgba(252,211,77,0) 76deg)");
      wrap.appendChild(beam);
    }

    var card = document.createElement("div");
    card.setAttribute("style", [
      "position:relative;overflow:hidden;border-radius:18px",
      "background:linear-gradient(135deg,#241a33 0%,#141122 55%,#12233a 100%)",
      "padding:30px 32px 26px;color:#e6edf3",
      "box-shadow:0 24px 72px rgba(0,0,0,.55)",
    ].join(";"));

    var glow = "<div style='position:absolute;right:-70px;top:-90px;width:260px;height:260px;"
      + "border-radius:50%;background:rgba(252,211,77,.12);filter:blur(70px);pointer-events:none'></div>";
    var heroArt = isHero
      ? "<div style='position:absolute;right:22px;top:20px;pointer-events:none;opacity:.95'>" + GIFT_SVG + "</div>"
      : "";
    var headPad = isHero ? "padding-right:104px" : "";

    card.innerHTML = glow + heroArt
      + "<div style='display:flex;align-items:center;gap:8px;font-size:12px;color:#fcd34d;" + headPad + "'>"
      + "🎁 <span>" + esc(t(c.title)) + "</span></div>"
      + "<div style='margin-top:14px;font-size:34px;font-weight:800;letter-spacing:.5px;color:#fff;" + headPad + "'>"
      + headlineHtml(t(c.headline)) + "</div>"
      + "<div style='margin-top:8px;font-size:13px;line-height:1.6;color:#9aa5c4'>" + esc(t(c.sub)) + "</div>"
      + countdownHtml(deadline, zh, false)
      + "<ul style='list-style:none;margin:14px 0 0;padding:0'>" + bulletsHtml(c, zh) + "</ul>"
      + ctaBlockHtml(c, t, zh)
      + (PREVIEW
        ? "<div style='position:absolute;left:0;top:0;background:rgba(155,140,255,.2);color:#c4b5fd;"
          + "font-size:10px;padding:3px 10px;border-radius:0 0 8px 0'>PREVIEW</div>"
        : "");

    wrap.appendChild(card);
    mask.appendChild(wrap);
    document.body.appendChild(mask);
    wireShine(card);
    runCountUp(card);
    beacon(PREVIEW ? "poster6u_preview" : "poster6u_view");
    if (!PREVIEW) {
      try { if (shell.campaignAct) shell.campaignAct({ id: c.id, action: "shown" }); } catch (e) { /* 回执失败不阻断 */ }
    }

    var timer = 0;
    function close() {
      if (timer) { clearInterval(timer); timer = 0; }
      try { document.removeEventListener("keydown", onKey, true); } catch (e) { /* 已移除 */ }
      try { mask.remove(); } catch (e) { /* 已移除 */ }
    }
    // 到点自动收起（绝不留一张过期海报）
    timer = startCountdown(card, deadline, zh, close);

    function act(action) {
      try { if (shell.campaignAct) shell.campaignAct({ id: c.id, action: action }); } catch (e) { /* 静默 */ }
    }
    var ctaBtn = card.querySelector("#camp-cta");
    if (ctaBtn) ctaBtn.addEventListener("click", function () {
      beaconLive("poster6u_click");
      act("click"); // 主进程侧 openExternal（https 白名单）+ 布防到账盯梢
      close();
    });
    var laterBtn = card.querySelector("#camp-later");
    if (laterBtn) laterBtn.addEventListener("click", function () {
      beaconLive("poster6u_dismiss");
      close(); // 展示已在 shown 回执里计数：稍后再说 = 静默收起，频控自然接管
    });
    var neverBtn = card.querySelector("#camp-never");
    if (neverBtn) neverBtn.addEventListener("click", function () {
      beaconLive("poster6u_never");
      if (!PREVIEW) act("never");
      close();
    });
    // 背板点击 / Esc = 稍后再说（营销弹窗必须给最低成本的退出，好感比曝光值钱）
    mask.addEventListener("click", function (ev) {
      if (ev.target === mask) {
        beaconLive("poster6u_dismiss");
        close();
      }
    });
    function onKey(ev) {
      if (ev.key === "Escape") {
        beaconLive("poster6u_dismiss");
        close();
      }
    }
    document.addEventListener("keydown", onKey, true);
  }

  /** 第 2/3 次曝光：右下角非模态角卡（P1 打扰递进降级）——同一张活动，但不再抢
   *  全屏：用户已经见过一次，重复曝光的正确姿势是「提醒」不是「拦路」。
   *  仍记 shown（频控照常消耗）、仍发 view 埋点（曝光口径不变）。 */
  function showCorner(payload, c, lang, deadline, zh) {
    var t = function (block) { return CM.cmLangText(block, lang); };
    var host = document.createElement("div");
    host.id = "campaign-poster-corner";
    host.setAttribute("style", [
      "position:fixed;right:18px;bottom:18px;z-index:99980",
      "width:340px;max-width:calc(100vw - 36px)",
      "background:linear-gradient(135deg,#241a33 0%,#141122 55%,#12233a 100%)",
      "border:1px solid rgba(252,211,77,.38);border-radius:14px",
      "padding:16px 18px 14px;color:#e6edf3",
      "box-shadow:0 12px 42px rgba(0,0,0,.5)",
      "font-family:" + FONT,
      REDUCED ? "" : "animation:campCornerIn .28s ease",
    ].join(";"));

    host.innerHTML = ""
      + "<div style='display:flex;align-items:center;gap:7px;font-size:11.5px;color:#fcd34d;padding-right:26px'>"
      + "🎁 <span style='overflow:hidden;text-overflow:ellipsis;white-space:nowrap'>" + esc(t(c.title)) + "</span></div>"
      + "<button id='camp-x' aria-label='" + esc(zh ? "关闭" : "Close") + "' style='position:absolute;right:6px;top:6px;"
      + "width:30px;height:30px;border:0;border-radius:8px;background:transparent;color:#7d88a8;"
      + "cursor:pointer;font-size:14px;line-height:1'>✕</button>"
      + "<div style='margin-top:9px;font-size:21px;font-weight:800;color:#fff'>" + headlineHtml(t(c.headline)) + "</div>"
      + countdownHtml(deadline, zh, true)
      + "<button id='camp-cta' style='display:block;width:100%;margin-top:12px;padding:10px 14px;border:0;"
      + "border-radius:999px;cursor:pointer;font:inherit;font-size:13px;font-weight:700;color:#141122;"
      + "background:linear-gradient(90deg,#fcd34d,#f59e0b)'>"
      + esc(t(c.ctaLabel) || (zh ? "立即领取" : "Claim now")) + "</button>"
      + (t(c.ctaSub) ? "<div style='text-align:center;margin-top:7px;font-size:11px;color:#b8a468'>" + esc(t(c.ctaSub)) + "</div>" : "")
      + "<div style='text-align:center;margin-top:7px'>"
      + "<a id='camp-never' href='javascript:void(0)' style='font-size:10.5px;color:#5f6a8a;text-decoration:none'>"
      + esc(t(c.neverLabel) || (zh ? "不再提醒" : "Don't show again")) + "</a></div>";

    document.body.appendChild(host);
    runCountUp(host);
    beacon("poster6u_view"); // 角卡只在非预览态出现（预览恒走大卡）
    try { if (shell.campaignAct) shell.campaignAct({ id: c.id, action: "shown" }); } catch (e) { /* 静默 */ }

    var timer = 0;
    function close() {
      if (timer) { clearInterval(timer); timer = 0; }
      try { host.remove(); } catch (e) { /* 已移除 */ }
    }
    timer = startCountdown(host, deadline, zh, close);

    function act(action) {
      try { if (shell.campaignAct) shell.campaignAct({ id: c.id, action: action }); } catch (e) { /* 静默 */ }
    }
    var ctaBtn = host.querySelector("#camp-cta");
    if (ctaBtn) ctaBtn.addEventListener("click", function () {
      beacon("poster6u_click");
      act("click");
      close();
    });
    var x = host.querySelector("#camp-x");
    if (x) x.addEventListener("click", function () {
      beacon("poster6u_dismiss");
      close();
    });
    var neverBtn = host.querySelector("#camp-never");
    if (neverBtn) neverBtn.addEventListener("click", function () {
      beacon("poster6u_never");
      act("never");
      close();
    });
  }

  function show(payload) {
    var c = payload && payload.campaign;
    if (!c) return;
    ensureStyle();
    var lang = String(payload.lang || "");
    var zh = String(lang).toLowerCase().indexOf("en") !== 0;
    var deadline = Number(payload.deadline) || 0;

    // 截止已过（拉取与展示之间跨过了窗口边缘）：绝不渲染死倒计时
    if (deadline && Date.now() >= deadline) return;

    var showCount = Number(payload.showCount) || 0;
    if (showCount >= 1 && !PREVIEW) {
      showCorner(payload, c, lang, deadline, zh);
    } else {
      showModal(payload, c, lang, deadline, zh);
    }
  }

  function boot() {
    // 3.5s 延迟：让工作台先完成首屏加载，海报不与启动闸门抢注意力（预览态缩短到 0.8s）
    setTimeout(function () {
      try {
        Promise.resolve(shell.campaignPoster()).then(function (payload) {
          try {
            if (payload && payload.campaign) { show(payload); return; }
            // 暗面漏斗（P0）：没弹时把「被哪道闸门拦住」发一发（每会话至多一条）——
            // poster6u_view=0 时运营终于能分清「没人合格」还是「链路断了」。
            var reason = payload && payload.skip && payload.skip.reason;
            if (reason && !PREVIEW) {
              beacon("poster6u_skip_" + String(reason).replace(/[^a-z0-9_]/gi, "").slice(0, 24));
            }
          } catch (e) { /* 渲染失败静默：营销层绝不伤壳 */ }
        }).catch(function () { /* 主进程未接/拉取失败：静默无海报 */ });
      } catch (e) { /* 同上 */ }
    }, PREVIEW ? 800 : 3500);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
})();
