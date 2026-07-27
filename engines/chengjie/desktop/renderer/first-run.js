"use strict";

// 首启向导（P0-1）：仅在首次运行（无 localStorage 标记）时弹一次。
// 步骤：① 界面语言 + 后台令牌 → ② 填 AI Key（测试 / 保存到后端 overlay）→ ③ 翻译就绪绿灯。
// AI 已配置（升级/重装保留数据目录）时只走 ①（与旧行为一致）。
// 流程编排/结果映射为纯函数（first-run-model.js，可 Node 单测）；本文件只做 DOM + IPC。
// 自包含、不依赖 renderer 内部；CSP: script-src 'self' 故为独立文件，样式走 inline（'unsafe-inline' 已许可）。
(function () {
  var FLAG = "aitr_firstrun_v1";
  var forced = false;
  try { forced = !!(window.shell && window.shell.forceFirstRun); } catch (e) {}
  var flagged = false;
  try { flagged = !!localStorage.getItem(FLAG); }
  catch (e) { /* localStorage 不可用：仍展示一次（不持久化） */ }
  var show = window.frShouldShowWizard ? window.frShouldShowWizard(flagged, forced) : !flagged;
  if (!show) return;

  var INPUT_STYLE = "width:100%;box-sizing:border-box;padding:8px 10px;background:#0f1420;color:#e6e9ef;border:1px solid #2a3344;border-radius:8px";
  var LABEL_STYLE = "display:block;font-size:13px;margin-bottom:6px;color:#c4ccd9";
  var BTN_PRIMARY = "padding:8px 18px;background:#2563eb;color:#fff;border:0;border-radius:8px;cursor:pointer;font-weight:600";
  var BTN_GHOST = "padding:8px 16px;background:transparent;color:#97a3b6;border:1px solid #2a3344;border-radius:8px;cursor:pointer";

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
      var curToken = (cfg.backend && cfg.backend.token) || "admin";
      var curLang = (cfg.unified_inbox && cfg.unified_inbox.lang) || "";

      var steps = window.frBuildSteps ? window.frBuildSteps(aiStatus, trialStatus) : ["basic"];
      var prefill = window.frAiPrefill ? window.frAiPrefill(aiStatus) : { base_url: "", model: "" };
      var state = { lang: curLang, token: curToken, saveView: null };

      var mask = document.createElement("div");
      mask.setAttribute("style", [
        "position:fixed;inset:0;z-index:99999",
        "background:rgba(8,12,20,.78);backdrop-filter:blur(2px)",
        "display:flex;align-items:center;justify-content:center",
        "font-family:system-ui,'Microsoft YaHei',sans-serif"
      ].join(";"));

      var card = document.createElement("div");
      card.setAttribute("style", [
        "width:440px;max-width:90vw;background:#161b26;color:#e6e9ef",
        "border:1px solid #2a3344;border-radius:12px;padding:24px 26px",
        "box-shadow:0 18px 60px rgba(0,0,0,.5)"
      ].join(";"));
      mask.appendChild(card);
      document.body.appendChild(mask);

      function t(key) { return window.frT ? window.frT(state.lang, key) : key; }

      function finish(save) {
        try { localStorage.setItem(FLAG, "1"); } catch (e) {}
        var fin = Promise.resolve();
        if (save && shell.saveConfig) {
          fin = Promise.resolve(shell.saveConfig({
            unified_inbox: { lang: state.lang },
            backend: { token: (state.token || "admin").trim() }
          })).catch(function () {});
        }
        fin.then(function () {
          if (save) { try { location.reload(); return; } catch (e) {} }
          if (mask.parentNode) mask.parentNode.removeChild(mask);
        });
      }

      // ── 步骤 ①：语言 + 令牌（与旧版一致；有 AI 步时按钮变「下一步」） ──
      function renderBasic() {
        var hasAiStep = steps.indexOf("ai") >= 0;
        card.innerHTML =
          '<div style="font-size:18px;font-weight:600;margin-bottom:6px">欢迎使用 AI 客服桌面端</div>' +
          '<div style="font-size:13px;color:#97a3b6;line-height:1.6;margin-bottom:18px">' +
          '首次启动会自动拉起本地后台服务（无需手动运行 Python），稍等片刻即可使用。<br>下面选项可先确认，之后也能在设置里改。</div>' +
          '<label style="' + LABEL_STYLE + '">界面语言</label>' +
          '<select id="fr-lang" style="' + INPUT_STYLE + ';margin-bottom:16px">' +
          '<option value="">跟随后台</option><option value="zh">中文</option><option value="en">English</option></select>' +
          '<label style="' + LABEL_STYLE + '">后台访问令牌（默认 admin）</label>' +
          '<input id="fr-token" type="text" style="' + INPUT_STYLE + ';margin-bottom:22px" />' +
          '<div style="display:flex;gap:10px;justify-content:flex-end">' +
          '<button id="fr-skip" style="' + BTN_GHOST + '">跳过</button>' +
          '<button id="fr-ok" style="' + BTN_PRIMARY + '">' + (hasAiStep ? "下一步" : "完成并进入") + '</button>' +
          '</div>';
        var langSel = card.querySelector("#fr-lang");
        var tokenInp = card.querySelector("#fr-token");
        langSel.value = state.lang;
        tokenInp.value = state.token;
        card.querySelector("#fr-ok").addEventListener("click", function () {
          state.lang = langSel.value;
          state.token = tokenInp.value;
          if (hasAiStep) renderAi();
          else if (steps.indexOf("trial") >= 0) renderTrial();
          else finish(true);
        });
        card.querySelector("#fr-skip").addEventListener("click", function () { finish(false); });
      }

      // ── 步骤 ②（A2）：AI Key 填写 → 测试（POST /api/setup/test-ai）→ 保存（POST /api/setup/ai-key，写 overlay） ──
      function renderAi() {
        card.innerHTML =
          '<div style="font-size:17px;font-weight:600;margin-bottom:6px">' + t("ai_title") + '</div>' +
          '<div style="font-size:12px;color:#97a3b6;line-height:1.6;margin-bottom:16px">' + t("ai_sub") + '</div>' +
          '<label style="' + LABEL_STYLE + '">' + t("ai_key_label") + '</label>' +
          '<input id="fr-ai-key" type="password" autocomplete="off" style="' + INPUT_STYLE + ';margin-bottom:12px" />' +
          '<label style="' + LABEL_STYLE + '">' + t("ai_base_label") + '</label>' +
          '<input id="fr-ai-base" type="text" style="' + INPUT_STYLE + ';margin-bottom:12px" />' +
          '<label style="' + LABEL_STYLE + '">' + t("ai_model_label") + '</label>' +
          '<input id="fr-ai-model" type="text" style="' + INPUT_STYLE + ';margin-bottom:10px" />' +
          '<div id="fr-ai-msg" style="min-height:18px;font-size:12px;margin-bottom:12px"></div>' +
          '<div style="display:flex;gap:10px;justify-content:flex-end;align-items:center">' +
          '<button id="fr-ai-back" style="' + BTN_GHOST + '">' + t("btn_back") + '</button>' +
          '<button id="fr-ai-skip" style="' + BTN_GHOST + '">' + t("btn_skip") + '</button>' +
          '<button id="fr-ai-test" style="' + BTN_GHOST + ';color:#e6e9ef">' + t("btn_test") + '</button>' +
          '<button id="fr-ai-save" style="' + BTN_PRIMARY + '">' + t("btn_save") + '</button>' +
          '</div>';
        var keyInp = card.querySelector("#fr-ai-key");
        var baseInp = card.querySelector("#fr-ai-base");
        var modelInp = card.querySelector("#fr-ai-model");
        var msg = card.querySelector("#fr-ai-msg");
        baseInp.value = prefill.base_url || "";
        modelInp.value = prefill.model || "";

        function setMsg(cls, text) {
          msg.textContent = text || "";
          msg.style.color = cls === "ok" ? "#34d399" : (cls === "warn" ? "#fbbf24" : (cls === "err" ? "#f87171" : "#97a3b6"));
        }
        function vals() {
          return {
            api_key: (keyInp.value || "").trim(),
            base_url: (baseInp.value || "").trim(),
            model: (modelInp.value || "").trim()
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
            var view = window.frAiTestView ? window.frAiTestView(resp, state.lang) : { cls: "err", text: t("test_fail") };
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
            var view = window.frAiSaveView ? window.frAiSaveView(resp, state.lang) : { cls: "err", ready: false, text: t("save_fail") };
            if (view.cls === "err") { setMsg("err", view.text); return; }
            state.saveView = view;
            renderResult();
          }).catch(function () { setMsg("err", t("backend_wait")); });
        });
      }

      // ── 步骤 ③（A5）：翻译就绪绿灯 / 失败下一步指引 ──
      function renderResult() {
        var view = window.frResultView ? window.frResultView(state.saveView, state.lang)
          : { cls: "warn", title: "", sub: "" };
        var okGreen = view.cls === "ok";
        card.innerHTML =
          '<div style="text-align:center;padding:8px 0 4px">' +
          '<div style="font-size:44px;line-height:1;margin-bottom:12px">' + (okGreen ? "🟢" : "🟡") + '</div>' +
          '<div style="font-size:18px;font-weight:600;margin-bottom:8px;color:' + (okGreen ? "#34d399" : "#fbbf24") + '">' + view.title + '</div>' +
          '<div style="font-size:13px;color:#97a3b6;line-height:1.7;margin-bottom:20px">' + view.sub + '</div>' +
          '<button id="fr-done" style="' + BTN_PRIMARY + ';width:100%">' + t("btn_finish") + '</button>' +
          '</div>';
        card.querySelector("#fr-done").addEventListener("click", function () {
          if (steps.indexOf("trial") >= 0) renderTrial();
          else finish(true);
        });
      }

      // ── 收尾步（P2）：告知体验额度 + 引导注册换 7 天完整版 ──
      // 体验额度是**默默生效**的：后端在无授权时自动开，用户压根不知道自己有；
      // 不说出来等于白送，也让后面「额度快用完了」的提示显得莫名其妙。
      // 说清楚之后顺势给出升级路径——注册链路（官网建单 → 厂商机签发 → 本机自动激活）
      // 现已闭合，可以承诺了。
      //
      // 铁律：**这一步随时可跳过**。体验档已在跑，用户此刻什么都不做也能干活；
      // 领取慢/失败都只是提示，绝不拦人。签发真到账由后端后台轮询兜底
      // （HealthWatchdog._check_trial_claim），不依赖这个窗口开着。
      function renderTrial() {
        var view = window.frTrialView ? window.frTrialView(trialStatus, state.lang)
          : { show: false, title: "", sub: "", chars: 0, hours: null };
        var num = function (n) {
          return window.frFormatChars ? window.frFormatChars(n, state.lang) : String(Math.round(Number(n) || 0));
        };
        var cell = function (label, value) {
          return '<div style="flex:1;background:#0f1420;border:1px solid #2a3344;border-radius:10px;padding:12px 8px">' +
            '<div style="font-size:11px;color:#97a3b6;margin-bottom:4px">' + label + '</div>' +
            '<div style="font-size:20px;font-weight:700;color:#a78bfa">' + value + '</div></div>';
        };
        var hoursCell = (view.hours == null) ? "" :
          cell(t("trial_hours"), Math.round(view.hours) + " " + t("trial_hours_unit"));
        var esc = function (s) {
          return String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;")
            .replace(/>/g, "&gt;").replace(/"/g, "&quot;");
        };
        var TONE = { ok: "#34d399", warn: "#fbbf24", err: "#f87171", info: "#97a3b6" };

        card.innerHTML =
          '<div style="padding:2px 0">' +
          '<div style="text-align:center">' +
          '<div style="font-size:36px;line-height:1;margin-bottom:10px">🎁</div>' +
          '<div style="font-size:17px;font-weight:600;margin-bottom:6px;color:#a78bfa">' + view.title + '</div>' +
          '</div>' +
          '<div style="display:flex;gap:10px;margin:12px 0 14px">' +
          cell(t("trial_chars"), num(view.chars)) + hoursCell +
          '</div>' +
          // 升级区：把「7 天完整版」放在体验额度正下方，对比即是理由。
          '<div style="border-top:1px solid #2a3344;margin:4px 0 14px"></div>' +
          '<div id="fr-claim-pitch">' +
          '<div style="font-size:14px;font-weight:600;color:#e6ecf5;margin-bottom:6px">' + t("claim_title") + '</div>' +
          '<div style="font-size:12.5px;color:#97a3b6;line-height:1.7;margin-bottom:12px">' + t("claim_sub") + '</div>' +
          '</div>' +
          '<input id="fr-contact" type="text" placeholder="' + esc(t("claim_ph")) + '" ' +
          'style="width:100%;box-sizing:border-box;padding:9px 12px;background:#0f1420;border:1px solid #2a3344;' +
          'border-radius:8px;color:#e6ecf5;font-size:13px;margin-bottom:10px" />' +
          '<div id="fr-claim-msg" style="font-size:12.5px;line-height:1.6;margin-bottom:10px;min-height:0"></div>' +
          '<div id="fr-claim-acts" style="display:flex;gap:10px">' +
          '<button id="fr-claim" style="' + BTN_PRIMARY + ';flex:1">' + t("btn_claim") + '</button>' +
          '<button id="fr-trial-go" style="' + BTN_GHOST + '">' + t("btn_skip_claim") + '</button>' +
          '</div></div>';

        var msg = card.querySelector("#fr-claim-msg");
        var acts = card.querySelector("#fr-claim-acts");
        var input = card.querySelector("#fr-contact");
        var claimBtn = card.querySelector("#fr-claim");
        card.querySelector("#fr-trial-go").addEventListener("click", function () { finish(true); });
        try { input.focus(); } catch (e) {}

        function say(cls, text) {
          msg.style.color = TONE[cls] || TONE.info;
          msg.textContent = text;
        }

        // 终态：换掉按钮组。已激活 → 继续领赠量；用尽/失败 → 只留「开始使用」。
        function settle(v) {
          var html = '';
          if (v.canGift) {
            html += '<button id="fr-gift" style="' + BTN_PRIMARY + ';flex:1">' + t("btn_gift") + '</button>';
          }
          html += '<button id="fr-done2" style="' + (v.canGift ? BTN_GHOST : BTN_PRIMARY + ';flex:1') + '">'
            + t("btn_start") + '</button>';
          acts.innerHTML = html;
          try { input.style.display = "none"; } catch (e) {}
          // 已用尽/领不到时收掉「免费领 7 天完整版」这段推介——它和紧接着的
          // 「这台机器的免费试用已经用过了」直接打脸（视觉验收所见）。
          // 成功态保留：那句推介正是对「已激活：7 天完整版」的说明。
          if (v.phase === "exhausted" || v.phase === "fail") {
            var pitch = card.querySelector("#fr-claim-pitch");
            if (pitch) pitch.style.display = "none";
          }
          acts.querySelector("#fr-done2").addEventListener("click", function () { finish(true); });
          var g = acts.querySelector("#fr-gift");
          if (g) g.addEventListener("click", function () { renderGift(); });
        }

        function poll(attempt) {
          var plan = window.frClaimPollPlan ? window.frClaimPollPlan(attempt)
            : { again: attempt < 6, delayMs: 5000, giveUpKey: "claim_slow" };
          if (!plan.again) {
            // 交给后端后台轮询：用户该走了，签发到账不需要这个窗口开着。
            say("info", t(plan.giveUpKey));
            settle({ canGift: false });
            return;
          }
          setTimeout(function () {
            if (!shell.trialClaimStatus) { say("info", t("claim_slow")); settle({ canGift: false }); return; }
            shell.trialClaimStatus().then(function (r) {
              var v = window.frClaimResultView ? window.frClaimResultView(r, state.lang)
                : { phase: "waiting", cls: "info", text: "", done: false, canGift: false };
              say(v.cls, v.text);
              if (v.done) settle(v);
              else poll(attempt + 1);
            }).catch(function () { poll(attempt + 1); });
          }, plan.delayMs);
        }

        claimBtn.addEventListener("click", function () {
          var contact = input.value;
          var chk = window.frClaimValidate ? window.frClaimValidate(contact) : { ok: !!contact, err: "claim_need_contact" };
          if (!chk.ok) { say("err", t(chk.err)); try { input.focus(); } catch (e) {} return; }
          claimBtn.disabled = true;
          say("info", t("claim_sending"));
          var p = shell.trialClaim ? shell.trialClaim({ contact: contact })
            : Promise.resolve({ ok: false, error: "unsupported" });
          p.then(function (r) {
            var v = window.frClaimResultView ? window.frClaimResultView(r, state.lang)
              : { phase: "fail", cls: "err", text: t("claim_fail"), done: true, canGift: false };
            say(v.cls, v.text);
            if (v.done) { settle(v); return; }
            poll(0);
          }).catch(function () {
            say("err", t("claim_network"));
            settle({ canGift: false });
          });
        });
      }

      // ── 加客服领 10 万字符 ──
      // 只负责「把码交到用户手上」；客服核销后的到账走后端后台轮询，
      // 所以这一屏永远可以直接关掉，不会丢东西。
      function renderGift() {
        card.innerHTML =
          '<div style="text-align:center;padding:6px 0 2px">' +
          '<div style="font-size:36px;line-height:1;margin-bottom:10px">💬</div>' +
          '<div style="font-size:17px;font-weight:600;margin-bottom:8px;color:#a78bfa">' + t("gift_title") + '</div>' +
          '<div id="fr-gift-body" style="font-size:12.5px;color:#97a3b6;line-height:1.7;margin:12px 0">…</div>' +
          '<div id="fr-gift-acts" style="display:flex;gap:10px;margin-top:14px">' +
          '<button id="fr-gift-done" style="' + BTN_PRIMARY + ';width:100%">' + t("btn_start") + '</button>' +
          '</div></div>';
        var body = card.querySelector("#fr-gift-body");
        var gacts = card.querySelector("#fr-gift-acts");
        card.querySelector("#fr-gift-done").addEventListener("click", function () { finish(true); });

        var p = shell.trialBindCode ? shell.trialBindCode() : Promise.resolve({ ok: false });
        p.then(function (r) {
          var v = window.frGiftView ? window.frGiftView(r, state.lang) : { ok: false, text: t("gift_fail") };
          if (!v.ok) { body.style.color = "#fbbf24"; body.textContent = v.text; return; }
          body.innerHTML = '<div style="text-align:left">' + v.text + '</div>' +
            '<div style="display:flex;align-items:center;gap:10px;margin-top:12px">' +
            '<code id="fr-code" style="flex:1;background:#0f1420;border:1px solid #2a3344;border-radius:8px;' +
            'padding:10px;font-size:16px;letter-spacing:1px;color:#a78bfa;font-weight:700">' + v.code + '</code>' +
            '<button id="fr-copy" style="' + BTN_GHOST + '">' + t("btn_copy") + '</button></div>';
          var copyBtn = body.querySelector("#fr-copy");
          copyBtn.addEventListener("click", function () {
            try {
              navigator.clipboard.writeText(v.code);
              copyBtn.textContent = t("copied");
            } catch (e) { /* 剪贴板不可用：码就在屏幕上，手抄也行 */ }
          });
          // 两个渠道都要给按钮。原来只渲染 Telegram，而 waUrl 明明已经算出来了——
          // 产品承诺的是「加客服 Telegram 或 WhatsApp」，只给一个等于让 WhatsApp
          // 用户自己去找人（视觉验收时抓到）。首个渠道主色，其余次要色。
          var chans = [];
          if (v.tgUrl) chans.push(["btn_open_tg", v.tgUrl]);
          if (v.waUrl) chans.push(["btn_open_wa", v.waUrl]);
          if (chans.length) {
            // 渠道各占一行内的等宽格，「开始使用」另起一行：三颗挤一行会把
            // 「打开 Telegram 发给客服」压到折行（视觉验收所见），且层次也不对——
            // 此刻的主动作是「选个渠道找客服」，开始使用是退路。
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
          body.style.color = "#fbbf24";
          body.textContent = t("gift_fail");
        });
      }

      renderBasic();
    });
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", run);
  else run();
})();
