"use strict";

// 首启向导：仅在首次运行时弹一次（localStorage + config.onboarding 双权威）。
//
// 托管版（managed）：欢迎 → 领取 → 就绪三屏；永不出现令牌 / AI 配置。
// 自建/开发版：基础(+令牌) → [AI → 结果] → [旧 trial 收尾]。
// 流程编排/结果映射为纯函数（first-run-model.js）；本文件只做 DOM + IPC。
(function () {
  var FLAG = "aitr_firstrun_v1";
  var forced = false;
  try { forced = !!(window.shell && window.shell.forceFirstRun); } catch (e) {}
  var managed = false;
  try { managed = !!(window.shell && window.shell.managedEdition); } catch (e) {}
  var flagged = false;
  try { flagged = !!localStorage.getItem(FLAG); }
  catch (e) { /* localStorage 不可用：仍展示一次（不持久化） */ }
  // 本会话已完成（点了「进入」后 finish 会 location.reload）：本次会话不再弹。
  // 否则 --first-run 强制态下「完成→reload→env 仍在→又判定要弹」会死循环。
  var sessionDone = false;
  try { sessionDone = !!sessionStorage.getItem(FLAG); } catch (e) {}
  var show = sessionDone ? false
    : (window.frShouldShowWizard ? window.frShouldShowWizard(flagged, forced, false) : !flagged);
  if (!show) return;

  var FONT = "'Inter','PingFang SC','Microsoft YaHei',system-ui,sans-serif";
  var FG = "#e6edf3", FG_MUTED = "#9aa5c4", FG_DIM = "#7d88a8";
  // 品牌色走 brand.css 的 --bl-* token（index.html 已加载，platform/brand SSOT 同步件；
  // 白标客户改 brand.css 首启自动跟色），字面量兜底=陈旧包/token 缺席时的当前视觉。
  // OK/WARN/ERR 是语义状态色、PURPLE 刻意对齐 web 端额度徽章(ws-pill.quota #a78bfa)
  // 而非品牌 studio 紫——额度语义色跨端一致优先于品牌化，都不接 token。
  var LINE = "rgba(255,255,255,.10)", ACCENT = "var(--bl-growth-500,#3b82f6)";
  var OK = "#34d399", WARN = "#fbbf24", ERR = "#f87171", PURPLE = "#a78bfa";
  var GRAD_BRAND = "var(--bl-gradient-brand,linear-gradient(90deg,#00b0f0,#1e6bf0,#7a3bf5,#d030f0,#f0509a,#f07800,#f0a010))";
  var CARD_BG = "var(--bl-ink-700,#1b2038)";
  var INPUT_STYLE = "width:100%;box-sizing:border-box;padding:10px 12px;background:#0f1420;color:" + FG + ";border:1px solid " + LINE + ";border-radius:9px;font:inherit;font-size:14px;outline:none";
  var LABEL_STYLE = "display:block;font-size:13px;margin-bottom:6px;color:" + FG_MUTED;
  var BTN_PRIMARY = "padding:10px 22px;background:" + ACCENT + ";color:#fff;border:0;border-radius:9px;cursor:pointer;font:inherit;font-size:14px;font-weight:600;transition:filter .15s ease,transform .1s ease";
  var BTN_GHOST = "padding:10px 16px;background:transparent;color:" + FG_MUTED + ";border:1px solid " + LINE + ";border-radius:9px;cursor:pointer;font:inherit;font-size:14px";
  var MARK_SVG = '<svg width="56" height="56" viewBox="0 0 512 512" role="img" aria-label="mark">'
    + '<rect width="512" height="512" rx="112" fill="#1b2038"/>'
    + '<path d="M128 144h256a40 40 0 0 1 40 40v112a40 40 0 0 1-40 40H248l-72 60v-60h-48a40 40 0 0 1-40-40V184a40 40 0 0 1 40-40z" fill="#ffffff"/>'
    + '<circle cx="208" cy="240" r="18" fill="#1b2038"/><circle cx="272" cy="240" r="18" fill="#1b2038"/><circle cx="336" cy="240" r="18" fill="#1b2038"/>'
    + '<path d="M384 92l15 39 39 15-39 15-15 39-15-39-39-15 39-15z" fill="#3b82f6"/></svg>';

  function frEsc(s) {
    return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
      .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
  }

  function run() {
    var shell = window.shell || {};
    var cfgPromise = shell.getConfig ? shell.getConfig() : Promise.resolve({});
    var aiPromise = shell.setupAiStatus ? shell.setupAiStatus().catch(function () { return null; }) : Promise.resolve(null);
    var trialPromise = shell.trialStatus ? shell.trialStatus().catch(function () { return null; }) : Promise.resolve(null);
    Promise.all([Promise.resolve(cfgPromise), Promise.resolve(aiPromise),
      Promise.resolve(trialPromise)]).then(function (rs) {
      var cfg = rs[0] || {};
      var aiStatus = rs[1];
      var trialStatus = rs[2];

      // config.onboarding 是第二权威：清了 localStorage 也不该再弹；并回写 localStorage 自愈。
      var configCompleted = !!(cfg.onboarding && cfg.onboarding.completed);
      if (!forced && configCompleted) {
        try { localStorage.setItem(FLAG, "1"); } catch (e) {}
        return;
      }

      var curToken = (cfg.backend && cfg.backend.token) || "admin";
      var curLang = (cfg.unified_inbox && cfg.unified_inbox.lang) || "";
      var steps = window.frBuildSteps
        ? window.frBuildSteps(aiStatus, trialStatus, { managed: managed })
        : (managed ? ["welcome", "claim", "celebrate"] : ["basic"]);
      var prefill = window.frAiPrefill ? window.frAiPrefill(aiStatus) : { base_url: "", model: "" };
      var state = {
        lang: curLang,
        token: curToken,
        saveView: null,
        claimed: false,
        giftShown: false,
        stepIdx: 0,
      };

      // 动画样式表（P1-H，注入一次；CSP style-src 带 unsafe-inline 放行）。
      // 动画一律走 class + keyframes，reduced-motion 用 !important 整组关停——
      // 行内只写 animation-delay 之类的参数，保证无障碍开关能压过一切。
      if (!document.getElementById("fr-anim-style")) {
        var animCss = document.createElement("style");
        animCss.id = "fr-anim-style";
        animCss.textContent = [
          "@keyframes frCardUp{from{opacity:0;transform:translateY(16px) scale(.98)}to{opacity:1;transform:none}}",
          "@keyframes frPop{0%{transform:scale(.4);opacity:0}62%{transform:scale(1.12)}100%{transform:scale(1);opacity:1}}",
          "@keyframes frOrb{0%{transform:translate(0,0)}50%{transform:translate(22px,-16px)}100%{transform:translate(0,0)}}",
          ".fr-anim-up{animation:frCardUp .34s cubic-bezier(.4,0,.2,1) both}",
          ".fr-anim-pop{animation:frPop .5s cubic-bezier(.34,1.56,.64,1) both}",
          ".fr-orb{animation:frOrb 16s ease-in-out infinite}",
          "@media (prefers-reduced-motion:reduce){.fr-anim-up,.fr-anim-pop,.fr-orb{animation:none!important}}",
        ].join("\n");
        document.head.appendChild(animCss);
      }

      var mask = document.createElement("div");
      mask.setAttribute("style", [
        "position:fixed;inset:0;z-index:99999",
        "background:rgba(20,24,39,.88);backdrop-filter:blur(6px)",
        "display:flex;align-items:center;justify-content:center",
        "font-family:" + FONT
      ].join(";"));

      // 品牌光斑（P1-H）：与 web 登录页 orb 呼应的两枚模糊光球，垫在卡片之下。
      // 颜色取品牌 cyan/violet token；纯装饰 pointer-events:none，reduced-motion 静止。
      var orb1 = document.createElement("div");
      orb1.className = "fr-orb";
      orb1.setAttribute("style", "position:absolute;width:380px;height:380px;border-radius:50%;"
        + "left:-90px;top:-110px;pointer-events:none;filter:blur(70px);opacity:.32;"
        + "background:radial-gradient(circle,var(--bl-brand-cyan,#00b0f0),transparent 70%)");
      var orb2 = document.createElement("div");
      orb2.className = "fr-orb";
      orb2.setAttribute("style", "position:absolute;width:330px;height:330px;border-radius:50%;"
        + "right:-80px;bottom:-100px;pointer-events:none;filter:blur(70px);opacity:.26;"
        + "animation-delay:-8s;animation-direction:reverse;"
        + "background:radial-gradient(circle,var(--bl-brand-violet,#7a3bf5),transparent 70%)");
      mask.appendChild(orb1);
      mask.appendChild(orb2);

      var card = document.createElement("div");
      card.className = "fr-anim-up";
      card.setAttribute("style", [
        "width:440px;max-width:92vw",
        // 内容变多（能力三点式/三步引导）后小屏的溢出保护：卡片自身可滚
        "max-height:92vh;overflow:auto",
        "background:" + GRAD_BRAND + " left top / 100% 3px no-repeat, " + CARD_BG,
        "color:" + FG,
        "border:1px solid " + LINE + ";border-radius:16px;padding:30px 30px 26px",
        "box-shadow:0 20px 64px rgba(0,0,0,.5)",
        "position:relative"
      ].join(";"));
      mask.appendChild(card);
      document.body.appendChild(mask);

      // 向导显示语言：显式选择 > 系统语言（「跟随系统」真跟随）> 中文。
      // 只影响显示；state.lang 本值（含空=跟随）原样保存进 config。
      function effL() {
        if (state.lang) return state.lang;
        return (window.frSysLang ? window.frSysLang(navigator.language) : "") || "";
      }
      function t(key) { return window.frT ? window.frT(effL(), key) : key; }
      function num(n) {
        return window.frFormatChars ? window.frFormatChars(n, effL()) : String(Math.round(Number(n) || 0));
      }

      // 漏斗埋点：托管版才发（自建/开发态噪声无意义），每事件本次向导只记一次，
      // 纯 fire-and-forget——埋点任何失败都不能影响向导本身。
      var _beaconSent = {};
      function beacon(ev) {
        try {
          if (!managed || _beaconSent[ev]) return;
          var evs = window.FR_FUNNEL_EVENTS || [];
          if (evs.indexOf(ev) < 0) return;
          _beaconSent[ev] = true;
          if (shell.trialFunnel) shell.trialFunnel({ event: ev });
        } catch (e) { /* 埋点绝不阻断向导 */ }
      }

      function finish(save) {
        if (save) beacon("done");
        try { localStorage.setItem(FLAG, "1"); } catch (e) {}
        try { sessionStorage.setItem(FLAG, "1"); } catch (e) {}
        var fin = Promise.resolve();
        if (save && shell.saveConfig) {
          var patch = {
            unified_inbox: { lang: state.lang },
            onboarding: {
              completed: true,
              completed_at: new Date().toISOString(),
              edition: managed ? "managed" : "self_hosted",
            },
          };
          // 托管版客户永不可见令牌——不把令牌写进「向导可改字段」；仍保留现有值。
          if (!managed) patch.backend = { token: (state.token || "admin").trim() };
          fin = Promise.resolve(shell.saveConfig(patch)).catch(function () {});
        }
        fin.then(function () {
          if (save) { try { location.reload(); return; } catch (e) {} }
          if (mask.parentNode) mask.parentNode.removeChild(mask);
        });
      }

      // 步骤指示器：托管三屏用文案点；自建多步用圆点序号。
      // P1-I：done/current/todo 三态（frStepDotsView）——走过的步骤打 ✓ 绿显，
      // 步骤间细连接线给进度感；模型缺席回落旧「仅高亮当前」。
      function stepDots(activeId) {
        var ids = steps.slice();
        if (!ids.length) return "";
        var labels = {
          welcome: t("step_welcome"), claim: t("step_claim"), celebrate: t("step_celebrate"),
          basic: "1", ai: "2", result: "3", trial: "4",
        };
        var view = window.frStepDotsView ? window.frStepDotsView(ids, activeId) : null;
        var html = '<div style="display:flex;gap:6px;justify-content:center;align-items:center;margin-bottom:18px;flex-wrap:wrap">';
        for (var i = 0; i < ids.length; i++) {
          var id = ids[i];
          var st = view ? view[i].state : (id === activeId ? "current" : "todo");
          var lab = labels[id] || String(i + 1);
          if (i > 0) {
            html += '<span style="width:10px;height:1px;flex:0 0 auto;background:'
              + (st === "todo" ? LINE : "rgba(52,211,153,.45)") + '"></span>';
          }
          var pill;
          if (st === "done") {
            pill = "background:rgba(52,211,153,.14);color:" + OK + ";border:1px solid rgba(52,211,153,.4)";
            lab = "✓ " + lab;
          } else if (st === "current") {
            pill = "background:rgba(59,130,246,.22);color:#93c5fd;border:1px solid rgba(59,130,246,.45)";
          } else {
            pill = "background:transparent;color:" + FG_DIM + ";border:1px solid " + LINE;
          }
          html += '<span style="font-size:11px;letter-spacing:.3px;padding:3px 10px;border-radius:999px;'
            + pill + '">' + frEsc(lab) + "</span>";
        }
        return html + "</div>";
      }

      function langCorner() {
        return '<div style="position:absolute;top:14px;right:16px">'
          + '<select id="fr-lang" style="background:transparent;color:' + FG_MUTED
          + ";border:1px solid " + LINE
          + ';border-radius:8px;padding:4px 8px;font:inherit;font-size:12px;cursor:pointer">'
          + '<option value="">' + t("lang_follow") + "</option>"
          + '<option value="zh">中文</option>'
          + '<option value="zh_hant">\u7e41\u9ad4\u4e2d\u6587</option>'
          + '<option value="en">English</option>'
          + '<option value="vi">Ti\u1ebfng Vi\u1ec7t</option>'
          + '<option value="th">\u0e44\u0e17\u0e22 (\u03b2)</option>'
          + '<option value="id">Bahasa Indonesia (\u03b2)</option></select></div>';
      }

      function wireLang(rerender) {
        var langSel = card.querySelector("#fr-lang");
        if (!langSel) return;
        langSel.value = state.lang;
        langSel.addEventListener("change", function () {
          state.lang = langSel.value;
          rerender();
        });
      }

      // 能力三点式（P0-A）：新用户 5 秒内明白「这是什么」，再谈领额度。
      function featureRows() {
        var fl = window.frFeatureList ? window.frFeatureList(effL()) : [];
        if (!fl.length) return "";
        var html = '<div style="margin:16px 0 2px;text-align:left">';
        for (var i = 0; i < fl.length; i++) {
          html += '<div style="display:flex;gap:10px;align-items:flex-start;padding:6px 2px">'
            + '<span style="font-size:17px;line-height:1.3;flex:0 0 auto">' + fl[i].icon + "</span>"
            + "<span>"
            + '<span style="display:block;font-size:13.5px;font-weight:600;color:' + FG + '">'
            + frEsc(fl[i].t) + "</span>"
            + '<span style="display:block;font-size:12px;color:' + FG_MUTED + ';margin-top:1px">'
            + frEsc(fl[i].d) + "</span></span></div>";
        }
        return html + "</div>";
      }

      // 额度 10 倍对比（P0-B）：左=尝鲜真值（本机已激活），右=注册可领（高亮）。
      // 体验档不可见时整块隐藏（绝不摆一个凭空的数字）。
      function compareCells() {
        var cv = window.frQuotaCompareView
          ? window.frQuotaCompareView(trialStatus, effL()) : { show: false };
        if (!cv.show) return "";
        var cell = function (c, hi, numId) {
          return '<div style="flex:1;background:#0f1420;border:1px solid '
            + (hi ? "rgba(167,139,250,.55)" : "#2a3344")
            + ';border-radius:10px;padding:11px 8px 9px'
            + (hi ? ";box-shadow:0 0 18px rgba(167,139,250,.16)" : "") + '">'
            + '<div style="font-size:11px;color:' + FG_MUTED + ';margin-bottom:4px">'
            + frEsc(c.label) + "</div>"
            + '<div id="' + numId + '" style="font-size:' + (hi ? "21px" : "18px")
            + ';font-weight:700;color:'
            + (hi ? PURPLE : FG) + '">' + frEsc(c.chars) + "</div>"
            + '<div style="font-size:10.5px;color:' + (hi ? "#c4b5fd" : FG_DIM) + ';margin-top:3px">'
            + frEsc(c.note) + "</div></div>";
        };
        return '<div style="display:flex;gap:8px;margin:14px 0 4px;align-items:stretch">'
          + cell(cv.now, false, "fr-cmp-nnum")
          + '<div style="align-self:center;flex:0 0 auto;color:' + FG_DIM + ';font-size:15px">→</div>'
          + cell(cv.claim, true, "fr-cmp-cnum") + "</div>";
      }

      // 额度数字滚动入场（P1-H）：每次向导只放一次（语言切换重渲不重放），
      // reduced-motion 直接保留终值；动画中途各帧同样走 frFormatChars 保持格式。
      function countUpCompare() {
        if (state._cmpAnimated) return;
        var tv = window.frTrialView ? window.frTrialView(trialStatus, effL()) : { show: false };
        if (!tv.show) return;
        state._cmpAnimated = true;
        var reduce = false;
        try {
          reduce = !!(window.matchMedia
            && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
        } catch (e) {}
        if (reduce || typeof requestAnimationFrame !== "function") return;
        var jobs = [
          { el: card.querySelector("#fr-cmp-nnum"), to: Number(tv.chars) || 0 },
          { el: card.querySelector("#fr-cmp-cnum"),
            to: Number(window.FR_CLAIM_CHARS) || 1000000 },
        ];
        for (var i = 0; i < jobs.length; i++) {
          if (jobs[i].el) jobs[i].el.textContent = num(0);
        }
        var t0 = null, DUR = 560;
        function tick(ts) {
          if (t0 == null) t0 = ts;
          var p = Math.min(1, (ts - t0) / DUR);
          var e = 1 - Math.pow(1 - p, 3);
          for (var k = 0; k < jobs.length; k++) {
            if (jobs[k].el) jobs[k].el.textContent = num(Math.round(jobs[k].to * e));
          }
          if (p < 1) requestAnimationFrame(tick);
        }
        requestAnimationFrame(tick);
      }

      // ── 托管①：欢迎（价值钩子 + 体验额度 + 语言角切换）──
      function renderWelcome() {
        state.stepIdx = steps.indexOf("welcome");
        beacon("welcome");
        var brandName = (cfg.brand && cfg.brand.product) || "";
        var wv = window.frWelcomeView
          ? window.frWelcomeView(trialStatus, effL())
          : { title: t("welcome_title"), sub: t("welcome_value_hook"), managedNote: t("welcome_managed_note"),
              showQuota: false, cta: t("btn_claim_start") };
        var title = brandName
          ? t("welcome_to_brand").replace("{brand}", brandName)
          : wv.title;
        card.innerHTML = langCorner()
          + stepDots("welcome")
          + '<div style="text-align:center;margin-bottom:4px">'
          + '<div style="display:inline-flex;border-radius:16px;overflow:hidden;box-shadow:0 6px 22px rgba(0,0,0,.4)">' + MARK_SVG + "</div>"
          + '<div style="font-size:22px;font-weight:650;margin-top:12px;letter-spacing:.2px">' + frEsc(title) + "</div>"
          + '<div style="font-size:13.5px;color:' + FG + ';line-height:1.65;margin-top:8px;font-weight:500">'
          + frEsc(wv.sub) + "</div>"
          + "</div>"
          + featureRows()
          + compareCells()
          + '<div style="font-size:11.5px;color:' + FG_DIM + ';margin-top:8px;line-height:1.55;text-align:center">✓ '
          + frEsc(wv.managedNote) + "</div>"
          + '<button id="fr-ok" style="' + BTN_PRIMARY + ';width:100%;margin-top:14px">'
          + frEsc(wv.cta) + "</button>";
        wireLang(renderWelcome);
        card.querySelector("#fr-ok").addEventListener("click", renderClaim);
        countUpCompare();
      }

      // ── 托管② / 自建 trial：领取 7 天 + 可选赠量 ──
      function renderClaim() {
        state.stepIdx = Math.max(steps.indexOf("claim"), steps.indexOf("trial"));
        var activeId = steps.indexOf("claim") >= 0 ? "claim" : "trial";
        var tv = window.frTrialView ? window.frTrialView(trialStatus, effL())
          : { show: false, title: "", sub: "", chars: 0, hours: null };
        var TONE = { ok: OK, warn: WARN, err: ERR, info: FG_MUTED };
        var head = managed
          ? (stepDots("claim")
            + '<div style="font-size:17px;font-weight:600;margin-bottom:6px;color:' + PURPLE + '">'
            + t("claim_title") + "</div>"
            + '<div style="font-size:12.5px;color:' + FG_MUTED + ';line-height:1.7;margin-bottom:12px">'
            + t("claim_sub") + "</div>")
          : ('<div style="padding:2px 0">'
            + '<div style="text-align:center">'
            + '<div style="font-size:17px;font-weight:600;margin-bottom:6px;color:' + PURPLE + '">'
            + tv.title + "</div></div>"
            + '<div style="display:flex;gap:10px;margin:12px 0 14px">'
            + '<div style="flex:1;background:#0f1420;border:1px solid #2a3344;border-radius:10px;padding:12px 8px">'
            + '<div style="font-size:11px;color:' + FG_MUTED + ';margin-bottom:4px">' + t("trial_chars") + "</div>"
            + '<div style="font-size:20px;font-weight:700;color:' + PURPLE + '">' + num(tv.chars) + "</div></div>"
            + ((tv.hours == null) ? ""
              : ('<div style="flex:1;background:#0f1420;border:1px solid #2a3344;border-radius:10px;padding:12px 8px">'
                + '<div style="font-size:11px;color:' + FG_MUTED + ';margin-bottom:4px">' + t("trial_hours") + "</div>"
                + '<div style="font-size:20px;font-weight:700;color:' + PURPLE + '">'
                + Math.round(tv.hours) + " " + t("trial_hours_unit") + "</div></div>"))
            + "</div>"
            + '<div style="border-top:1px solid #2a3344;margin:4px 0 14px"></div>'
            + '<div id="fr-claim-pitch">'
            + '<div style="font-size:14px;font-weight:600;color:#e6ecf5;margin-bottom:6px">' + t("claim_title") + "</div>"
            + '<div style="font-size:12.5px;color:' + FG_MUTED + ';line-height:1.7;margin-bottom:12px">'
            + t("claim_sub") + "</div></div>");

        card.innerHTML = (managed ? langCorner() : "")
          + head
          // 字符换算锚点（P0-C）：把抽象单位翻成体感，紧贴宣传语之下
          + '<div style="font-size:11.5px;color:' + FG_DIM + ';line-height:1.55;margin:-4px 0 10px">'
          + frEsc(t("claim_calc")) + "</div>"
          + '<input id="fr-contact" type="text" placeholder="' + frEsc(t("claim_ph")) + '" '
          + 'style="' + INPUT_STYLE + ';margin-bottom:6px" />'
          // 信任说明（P0-C）：要联系方式必须给理由，降低「怕被骚扰」型跳过
          + '<div id="fr-claim-trust" style="font-size:11.5px;color:' + FG_DIM
          + ';line-height:1.55;margin-bottom:8px">🔒 ' + frEsc(t("claim_trust")) + "</div>"
          // 邀请码降噪（P0-C）：新装用户 95% 没有邀请码，收进折叠链接
          + '<a id="fr-invite-toggle" href="javascript:void 0" style="display:inline-block;'
          + 'font-size:12px;color:#93c5fd;text-decoration:none;margin-bottom:8px">'
          + frEsc(t("invite_toggle")) + "</a>"
          + '<input id="fr-invite" type="text" placeholder="' + frEsc(t("invite_ph")) + '" '
          + 'autocomplete="off" spellcheck="false" '
          + 'style="' + INPUT_STYLE + ';margin-bottom:10px;font-size:12.5px;display:none" />'
          + '<div id="fr-claim-msg" style="font-size:12.5px;line-height:1.6;margin-bottom:10px;min-height:0"></div>'
          + '<div id="fr-claim-acts" style="display:flex;gap:10px">'
          + '<button id="fr-claim" style="' + BTN_PRIMARY + ';flex:1">' + t("btn_claim") + "</button>"
          + '<button id="fr-trial-go" style="' + BTN_GHOST + '">' + t("btn_skip_claim") + "</button>"
          + "</div>"
          + (managed ? "" : "</div>");

        if (managed) wireLang(renderClaim);
        var msg = card.querySelector("#fr-claim-msg");
        var acts = card.querySelector("#fr-claim-acts");
        var input = card.querySelector("#fr-contact");
        var inviteInp = card.querySelector("#fr-invite");
        var inviteToggle = card.querySelector("#fr-invite-toggle");
        var claimBtn = card.querySelector("#fr-claim");
        inviteToggle.addEventListener("click", function () {
          inviteToggle.style.display = "none";
          inviteInp.style.display = "";
          try { inviteInp.focus(); } catch (e) {}
          beacon("invite_open");
        });
        card.querySelector("#fr-trial-go").addEventListener("click", function () {
          beacon("claim_skip");
          if (managed) renderCelebrate();
          else finish(true);
        });
        try { input.focus(); } catch (e) {}

        function say(cls, text) {
          msg.style.color = TONE[cls] || TONE.info;
          msg.textContent = text;
        }

        function settle(v) {
          var html = "";
          if (v.canGift) {
            html += '<button id="fr-gift" style="' + BTN_PRIMARY + ';flex:1">' + t("btn_gift") + "</button>";
          }
          html += '<button id="fr-done2" style="' + (v.canGift ? BTN_GHOST : BTN_PRIMARY + ";flex:1") + '">'
            + (managed ? t("btn_next") : t("btn_start")) + "</button>";
          acts.innerHTML = html;
          try { input.style.display = "none"; } catch (e) {}
          try { if (inviteInp) inviteInp.style.display = "none"; } catch (e) {}
          try { if (inviteToggle) inviteToggle.style.display = "none"; } catch (e) {}
          try {
            var trust = card.querySelector("#fr-claim-trust");
            if (trust) trust.style.display = "none";
          } catch (e) {}
          if (v.phase === "exhausted" || v.phase === "fail") {
            var pitch = card.querySelector("#fr-claim-pitch");
            if (pitch) pitch.style.display = "none";
          }
          acts.querySelector("#fr-done2").addEventListener("click", function () {
            if (managed) renderCelebrate();
            else finish(true);
          });
          var g = acts.querySelector("#fr-gift");
          if (g) g.addEventListener("click", function () { renderGift(); });
        }

        function poll(attempt) {
          var plan = window.frClaimPollPlan ? window.frClaimPollPlan(attempt)
            : { again: attempt < 6, delayMs: 5000, giveUpKey: "claim_slow" };
          if (!plan.again) {
            say("info", t(plan.giveUpKey));
            settle({ canGift: false });
            return;
          }
          setTimeout(function () {
            if (!shell.trialClaimStatus) { say("info", t("claim_slow")); settle({ canGift: false }); return; }
            shell.trialClaimStatus().then(function (r) {
              var v = window.frClaimResultView ? window.frClaimResultView(r, effL())
                : { phase: "waiting", cls: "info", text: "", done: false, canGift: false };
              say(v.cls, v.text);
              if (v.phase === "ok") { state.claimed = true; beacon("claim_ok"); }
              if (v.done) settle(v);
              else poll(attempt + 1);
            }).catch(function () { poll(attempt + 1); });
          }, plan.delayMs);
        }

        claimBtn.addEventListener("click", function () {
          var contact = input.value;
          var chk = window.frClaimValidate ? window.frClaimValidate(contact)
            : { ok: !!contact, err: "claim_need_contact" };
          if (!chk.ok) { say("err", t(chk.err)); try { input.focus(); } catch (e) {} return; }
          claimBtn.disabled = true;
          say("info", t("claim_sending"));
          beacon("claim_submit");
          var inviteCode = "";
          try { inviteCode = String((inviteInp && inviteInp.value) || "").trim(); } catch (e) {}
          var p = shell.trialClaim
            ? shell.trialClaim(inviteCode
              ? { contact: contact, invite_code: inviteCode }
              : { contact: contact })
            : Promise.resolve({ ok: false, error: "unsupported" });
          p.then(function (r) {
            var v = window.frClaimResultView ? window.frClaimResultView(r, effL())
              : { phase: "fail", cls: "err", text: t("claim_fail"), done: true, canGift: false };
            say(v.cls, v.text);
            if (v.phase === "ok") { state.claimed = true; beacon("claim_ok"); }
            if (v.done) { settle(v); return; }
            poll(0);
          }).catch(function () {
            say("err", t("claim_network"));
            settle({ canGift: false });
          });
        });
        void activeId;
      }

      // ── 托管③：庆祝就绪 ──
      function renderCelebrate() {
        var cv = window.frCelebrateView
          ? window.frCelebrateView({ claimed: state.claimed, giftShown: state.giftShown }, effL())
          : { title: t("celebrate_title"), sub: t("celebrate_sub_skipped"), cta: t("btn_enter") };
        // 「接下来三步」信息地图（P0-D）：带着地图进工作台，修落地断层
        var stepsHtml = "";
        if (cv.steps && cv.steps.length) {
          stepsHtml = '<div style="text-align:left;background:#0f1420;border:1px solid #2a3344;'
            + 'border-radius:12px;padding:12px 14px;margin-bottom:16px">'
            + '<div style="font-size:11px;letter-spacing:.5px;color:' + FG_DIM
            + ';font-weight:600;margin-bottom:7px">' + frEsc(cv.stepsTitle) + "</div>";
          for (var i = 0; i < cv.steps.length; i++) {
            var s = cv.steps[i];
            stepsHtml += '<div class="fr-anim-up" style="display:flex;gap:9px;padding:4px 0;'
              + "align-items:flex-start;animation-delay:" + (120 + i * 80) + 'ms">'
              + '<span style="flex:0 0 18px;height:18px;border-radius:50%;background:rgba(59,130,246,.2);'
              + 'color:#93c5fd;font-size:11px;font-weight:700;display:inline-flex;'
              + 'align-items:center;justify-content:center;margin-top:1px">' + frEsc(s.n) + "</span>"
              + "<span>"
              + '<span style="display:block;font-size:12.5px;font-weight:600;color:' + FG + '">'
              + frEsc(s.t) + "</span>"
              + '<span style="display:block;font-size:11.5px;color:' + FG_MUTED + ';margin-top:1px">'
              + frEsc(s.d) + "</span></span></div>";
          }
          stepsHtml += "</div>";
        }
        card.innerHTML = langCorner()
          + stepDots("celebrate")
          + '<div style="text-align:center;padding:8px 0 4px">'
          + '<div class="fr-anim-pop" style="width:60px;height:60px;margin:0 auto 12px;border-radius:50%;'
          + "background:rgba(52,211,153,.12);border:1px solid rgba(52,211,153,.35);"
          + 'display:flex;align-items:center;justify-content:center">'
          + '<svg width="30" height="30" viewBox="0 0 24 24" fill="none" stroke="' + OK
          + '" stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round">'
          + '<path d="M20 6L9 17l-5-5"/></svg></div>'
          + '<div style="font-size:20px;font-weight:650;margin-bottom:8px;color:' + OK + '">'
          + frEsc(cv.title) + "</div>"
          + '<div style="font-size:12.5px;color:' + FG_MUTED + ';line-height:1.7;margin-bottom:16px">'
          + frEsc(cv.sub) + "</div>"
          + stepsHtml
          + '<button id="fr-done" style="' + BTN_PRIMARY + ';width:100%">' + frEsc(cv.cta) + "</button>"
          // 未领取用户保留回门（bind-code 需先领取，绑定码入口对 skip 用户必然失败）
          + (cv.showClaimBack
            ? '<button id="fr-back-claim" style="' + BTN_GHOST + ';width:100%;margin-top:10px">'
              + frEsc(cv.backCta) + "</button>"
            : "")
          + "</div>";
        wireLang(renderCelebrate);
        card.querySelector("#fr-done").addEventListener("click", function () { finish(true); });
        var backBtn = card.querySelector("#fr-back-claim");
        if (backBtn) backBtn.addEventListener("click", function () {
          beacon("claim_back");
          renderClaim();
        });
      }

      function renderGift() {
        state.giftShown = true;
        beacon("gift_open");
        card.innerHTML =
          (managed ? (langCorner() + stepDots("claim")) : "")
          + '<div style="text-align:center;padding:6px 0 2px">'
          + '<div style="font-size:17px;font-weight:600;margin-bottom:8px;color:' + PURPLE + '">'
          + t("gift_title") + "</div>"
          + '<div id="fr-gift-body" style="font-size:12.5px;color:' + FG_MUTED + ';line-height:1.7;margin:12px 0">…</div>'
          + '<div id="fr-gift-acts" style="display:flex;gap:10px;margin-top:14px">'
          + '<button id="fr-gift-done" style="' + BTN_PRIMARY + ';width:100%">'
          + (managed ? t("btn_next") : t("btn_start")) + "</button>"
          + "</div></div>";
        if (managed) wireLang(renderGift);
        var body = card.querySelector("#fr-gift-body");
        var gacts = card.querySelector("#fr-gift-acts");
        card.querySelector("#fr-gift-done").addEventListener("click", function () {
          if (managed) renderCelebrate();
          else finish(true);
        });

        var p = shell.trialBindCode ? shell.trialBindCode() : Promise.resolve({ ok: false });
        p.then(function (r) {
          var v = window.frGiftView ? window.frGiftView(r, effL()) : { ok: false, text: t("gift_fail") };
          if (!v.ok) { body.style.color = WARN; body.textContent = v.text; return; }
          body.innerHTML = '<div style="text-align:left">' + frEsc(v.text) + "</div>"
            + '<div style="display:flex;align-items:center;gap:10px;margin-top:12px">'
            + '<code id="fr-code" style="flex:1;background:#0f1420;border:1px solid #2a3344;border-radius:8px;'
            + "padding:10px;font-size:16px;letter-spacing:1px;color:" + PURPLE + ';font-weight:700">'
            + frEsc(v.code) + "</code>"
            + '<button id="fr-copy" style="' + BTN_GHOST + '">' + t("btn_copy") + "</button></div>";
          var copyBtn = body.querySelector("#fr-copy");
          copyBtn.addEventListener("click", function () {
            try {
              navigator.clipboard.writeText(v.code);
              copyBtn.textContent = t("copied");
            } catch (e) { /* 剪贴板不可用：码就在屏幕上 */ }
          });
          var chans = [];
          if (v.tgUrl) chans.push(["btn_open_tg", v.tgUrl]);
          if (v.waUrl) chans.push(["btn_open_wa", v.waUrl]);
          if (chans.length) {
            gacts.setAttribute("style", "display:flex;flex-direction:column;gap:10px;margin-top:14px");
            var row = document.createElement("div");
            row.setAttribute("style", "display:flex;gap:10px");
            chans.forEach(function (ch, i) {
              var open = document.createElement("button");
              open.setAttribute("style", (i === 0 ? BTN_PRIMARY : BTN_GHOST) + ";flex:1");
              open.textContent = t(ch[0]);
              open.addEventListener("click", function () {
                if (shell.openExternal) shell.openExternal(ch[1]);
              });
              row.appendChild(open);
            });
            gacts.insertBefore(row, gacts.firstChild);
            gacts.querySelector("#fr-gift-done").setAttribute("style", BTN_GHOST + ";width:100%");
          }
        }).catch(function () {
          body.style.color = WARN;
          body.textContent = t("gift_fail");
        });
      }

      // ── 自建①：语言 + 令牌 ──
      function renderBasic() {
        var hasAiStep = steps.indexOf("ai") >= 0;
        var hasTrial = steps.indexOf("trial") >= 0;
        var brandName = (cfg.brand && cfg.brand.product) || "";
        var title = brandName
          ? t("welcome_to_brand").replace("{brand}", brandName)
          : t("welcome_title");
        card.innerHTML = stepDots("basic")
          + '<div style="text-align:center;margin-bottom:20px">'
          + '<div style="display:inline-flex;border-radius:16px;overflow:hidden;box-shadow:0 6px 22px rgba(0,0,0,.4)">' + MARK_SVG + "</div>"
          + '<div style="font-size:20px;font-weight:600;margin-top:14px;letter-spacing:.2px">' + frEsc(title) + "</div>"
          + '<div style="font-size:13px;color:' + FG_MUTED + ';line-height:1.75;margin-top:8px">' + t("welcome_sub") + "</div>"
          + "</div>"
          + '<label style="' + LABEL_STYLE + '">' + t("lang_label") + "</label>"
          + '<select id="fr-lang" style="' + INPUT_STYLE + ';margin-bottom:16px">'
          + '<option value="">' + t("lang_follow") + "</option>"
          + '<option value="zh">中文</option>'
          + '<option value="zh_hant">\u7e41\u9ad4\u4e2d\u6587</option>'
          + '<option value="en">English</option>'
          + '<option value="vi">Ti\u1ebfng Vi\u1ec7t</option>'
          + '<option value="th">\u0e44\u0e17\u0e22 (\u03b2)</option>'
          + '<option value="id">Bahasa Indonesia (\u03b2)</option></select>'
          + '<label style="' + LABEL_STYLE + '">' + t("token_label") + "</label>"
          + '<input id="fr-token" type="text" style="' + INPUT_STYLE + ';margin-bottom:22px" />'
          + '<div style="display:flex;gap:10px;justify-content:flex-end">'
          + '<button id="fr-skip" style="' + BTN_GHOST + '">' + t("btn_skip") + "</button>"
          + '<button id="fr-ok" style="' + BTN_PRIMARY + '">'
          + ((hasAiStep || hasTrial) ? t("btn_next") : t("btn_start")) + "</button></div>";
        var langSel = card.querySelector("#fr-lang");
        langSel.value = state.lang;
        var tokenInp = card.querySelector("#fr-token");
        tokenInp.value = state.token;
        langSel.addEventListener("change", function () { state.lang = langSel.value; renderBasic(); });
        card.querySelector("#fr-ok").addEventListener("click", function () {
          state.lang = langSel.value;
          state.token = tokenInp.value;
          if (hasAiStep) renderAi();
          else if (hasTrial) renderClaim();
          else finish(true);
        });
        card.querySelector("#fr-skip").addEventListener("click", function () { finish(false); });
      }

      function renderAi() {
        card.innerHTML = stepDots("ai")
          + '<div style="font-size:17px;font-weight:600;margin-bottom:6px">' + t("ai_title") + "</div>"
          + '<div style="font-size:12px;color:#97a3b6;line-height:1.6;margin-bottom:16px">' + t("ai_sub") + "</div>"
          + '<label style="' + LABEL_STYLE + '">' + t("ai_key_label") + "</label>"
          + '<input id="fr-ai-key" type="password" autocomplete="off" style="' + INPUT_STYLE + ';margin-bottom:12px" />'
          + '<label style="' + LABEL_STYLE + '">' + t("ai_base_label") + "</label>"
          + '<input id="fr-ai-base" type="text" style="' + INPUT_STYLE + ';margin-bottom:12px" />'
          + '<label style="' + LABEL_STYLE + '">' + t("ai_model_label") + "</label>"
          + '<input id="fr-ai-model" type="text" style="' + INPUT_STYLE + ';margin-bottom:10px" />'
          + '<div id="fr-ai-msg" style="min-height:18px;font-size:12px;margin-bottom:12px"></div>'
          + '<div style="display:flex;gap:10px;justify-content:flex-end;align-items:center">'
          + '<button id="fr-ai-back" style="' + BTN_GHOST + '">' + t("btn_back") + "</button>"
          + '<button id="fr-ai-skip" style="' + BTN_GHOST + '">' + t("btn_skip") + "</button>"
          + '<button id="fr-ai-test" style="' + BTN_GHOST + ";color:" + FG + '">' + t("btn_test") + "</button>"
          + '<button id="fr-ai-save" style="' + BTN_PRIMARY + '">' + t("btn_save") + "</button></div>";
        var keyInp = card.querySelector("#fr-ai-key");
        var baseInp = card.querySelector("#fr-ai-base");
        var modelInp = card.querySelector("#fr-ai-model");
        var msg = card.querySelector("#fr-ai-msg");
        baseInp.value = prefill.base_url || "";
        modelInp.value = prefill.model || "";
        function setMsg(cls, text) {
          msg.textContent = text || "";
          msg.style.color = cls === "ok" ? OK : (cls === "warn" ? WARN : (cls === "err" ? ERR : FG_MUTED));
        }
        function vals() {
          return {
            api_key: (keyInp.value || "").trim(),
            base_url: (baseInp.value || "").trim(),
            model: (modelInp.value || "").trim(),
          };
        }
        card.querySelector("#fr-ai-back").addEventListener("click", renderBasic);
        card.querySelector("#fr-ai-skip").addEventListener("click", function () { finish(true); });
        card.querySelector("#fr-ai-test").addEventListener("click", function () {
          var v = vals();
          var chk = window.frValidateAiInput ? window.frValidateAiInput(v) : { ok: true };
          if (!chk.ok) { setMsg("err", t(chk.err)); return; }
          setMsg("", t("testing"));
          var p = shell.setupTestAi ? shell.setupTestAi(v) : Promise.resolve(null);
          Promise.resolve(p).then(function (resp) {
            var view = window.frAiTestView ? window.frAiTestView(resp, effL())
              : { cls: "err", text: t("test_fail") };
            setMsg(view.cls, view.text);
          }).catch(function () { setMsg("err", t("backend_wait")); });
        });
        card.querySelector("#fr-ai-save").addEventListener("click", function () {
          var v = vals();
          var chk = window.frValidateAiInput ? window.frValidateAiInput(v) : { ok: true };
          if (!chk.ok) { setMsg("err", t(chk.err)); return; }
          setMsg("", t("saving"));
          var p = shell.setupSaveAiKey ? shell.setupSaveAiKey(v) : Promise.resolve(null);
          Promise.resolve(p).then(function (resp) {
            var view = window.frAiSaveView ? window.frAiSaveView(resp, effL())
              : { cls: "err", ready: false, text: t("save_fail") };
            if (view.cls === "err") { setMsg("err", view.text); return; }
            state.saveView = view;
            renderResult();
          }).catch(function () { setMsg("err", t("backend_wait")); });
        });
      }

      function renderResult() {
        var view = window.frResultView ? window.frResultView(state.saveView, effL())
          : { cls: "warn", title: "", sub: "" };
        var okGreen = view.cls === "ok";
        card.innerHTML = stepDots("result")
          + '<div style="text-align:center;padding:8px 0 4px">'
          + '<div style="font-size:18px;font-weight:600;margin-bottom:8px;color:'
          + (okGreen ? OK : WARN) + '">' + frEsc(view.title) + "</div>"
          + '<div style="font-size:13px;color:' + FG_MUTED + ';line-height:1.7;margin-bottom:20px">'
          + frEsc(view.sub) + "</div>"
          + '<button id="fr-done" style="' + BTN_PRIMARY + ';width:100%">' + t("btn_finish") + "</button></div>";
        card.querySelector("#fr-done").addEventListener("click", function () {
          if (steps.indexOf("trial") >= 0) renderClaim();
          else finish(true);
        });
      }

      if (managed) renderWelcome();
      else renderBasic();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();
})();
