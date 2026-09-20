"use strict";
/* splash.js — 开机动画驱动层（赛博朋克首启序列，P0 2026-08-22）
   ───────────────────────────────────────────────────────────────────────────
   分工铁律：
   · 纯函数（阶段推导/进度映射/文案轮换/ETA/模式判定）全在 splash-model.js
     （Node 门禁直测）；本文件只做 DOM/计时/IPC 读取，**零业务判定复制**。
   · 连接状态机唯一事实源＝renderer.js buildInboxTab 的 setPhase()：经
     aitr:inbox-phase 事件镜像过来（error 文案 verbatim 直显、ready 收幕）；
     「重试」回派 aitr:splash-retry 复用 renderer 的 reload() 闭环——绝不在
     这里长出第二套重试/自动重连逻辑（双源迟早分叉）。
   · 自身只读三个真实信号驱动进度：spawn 状态机（backendSpawnStatus）、
     /login 探活（backendHealth，命中即停）、工作台 webview 首次真实导航。
   模式（老板拍板 2026-08-22）：冷启动播三幕全版；暖启动（首探活即健康）只播
   ≈1.2s 品牌闪场；白标（config.brand 非空）整层让位；Esc/「跳过动画」/系统
   reduced-motion → 极简态。
   兜底：init 整体 try/catch，任何异常立即拆层——动画绝不许把人困在后面；
   CSS 侧另有 150s spAutoHide 保险（本脚本压根没跑时生效），跑起来即关。 */
(function () {
  var rootEl = document.getElementById("bl-splash");
  if (!rootEl) return;
  var M = window.splashModel;
  var SH = (typeof window.SH === "function") ? window.SH : function (k) { return String(k); };

  var killed = false;
  var timers = [];
  function later(fn, ms) { var t = setTimeout(fn, ms); timers.push(t); return t; }
  function every(fn, ms) { var t = setInterval(fn, ms); timers.push(t); return t; }

  function kill(reason) {
    if (killed) return;
    killed = true;
    timers.forEach(function (t) { try { clearTimeout(t); clearInterval(t); } catch (e) { /* ignore */ } });
    try { rootEl.remove(); } catch (e) { try { rootEl.style.display = "none"; } catch (e2) { /* ignore */ } }
    try {
      window.dispatchEvent(new CustomEvent("aitr:splash-done", { detail: { reason: String(reason || "") } }));
    } catch (e) { /* ignore */ }
  }

  function beacon(action) {
    try {
      if (window.shell && typeof window.shell.uiEvent === "function") {
        window.shell.uiEvent({ page: "desktop-shell", action: String(action || "") });
      }
    } catch (e) { /* 埋点绝不影响开机 */ }
  }

  function lsGet(k) { try { return localStorage.getItem(k) || ""; } catch (e) { return ""; } }
  function lsSet(k, v) { try { localStorage.setItem(k, String(v)); } catch (e) { /* ignore */ } }

  // 任何异常都不许把用户困在动画后面。
  try { init(); } catch (e) { kill("init-error"); }

  function init() {
    if (!M) { kill("no-model"); return; }
    // JS 已接管生命周期 → 关掉 CSS 的 150s 自动隐藏保险（那是给「本脚本没跑」兜底的）。
    rootEl.style.animation = "none";

    var el = {
      amb: document.getElementById("sp-amb"),
      fill: document.getElementById("sp-fill"),
      pct: document.getElementById("sp-pct"),
      real: document.getElementById("sp-real"),
      term: document.getElementById("sp-term"),
      err: document.getElementById("sp-err"),
      errMsg: document.getElementById("sp-err-msg"),
      retry: document.getElementById("sp-retry"),
      diag: document.getElementById("sp-diag"),
      skip: document.getElementById("sp-skip"),
      nodes: Array.prototype.slice.call(rootEl.querySelectorAll(".sp-node")),
    };

    var t0 = Date.now();
    var st = {
      mode: "", warmT0: 0,
      phase: "shell", phaseT0: t0,
      progress: 0, shown: 0,
      spawn: "", spawnErr: "",
      healthOk: false, wsNav: false, wsReady: false,
      ambKey: "", errOn: false, errBeaconed: false,
      doneStarted: false, minOn: false,
    };
    var etaMs = M.spEtaMs(parseInt(lsGet("aitr.splash.last_cold_ms"), 10));

    // ── 状态镜像（同步注册：renderer 的首次 setPhase 在 getConfig IPC 回包之后，
    //    本脚本的同步求值必然先完成，不存在丢首发事件的窗口）────────────────
    window.addEventListener("aitr:inbox-phase", function (ev) {
      if (killed) return;
      var d = (ev && ev.detail) || {};
      if (d.phase === "ready") { st.wsReady = true; finish(); return; }
      if (d.phase === "error") { showError(String(d.msg || "")); return; }
      if (st.errOn) hideError(); // loading 等其余相位＝错误已被 renderer 接管恢复
    });

    el.retry.addEventListener("click", function () {
      try { window.dispatchEvent(new CustomEvent("aitr:splash-retry")); } catch (e) { /* ignore */ }
    });
    el.diag.addEventListener("click", function () {
      var payload = {
        ts: new Date().toISOString(),
        mode: st.mode, phase: st.phase,
        progress: Math.round(st.progress),
        elapsed_s: Math.round((Date.now() - t0) / 1000),
        spawn: st.spawn, spawn_err: st.spawnErr,
        health_ok: st.healthOk, ws_nav: st.wsNav, ws_ready: st.wsReady,
        error_shown: st.errOn,
        log: "userData/logs/backend.log",
      };
      try {
        if (window.shell && typeof window.shell.copy === "function") {
          window.shell.copy(JSON.stringify(payload, null, 2));
        }
      } catch (e) { /* ignore */ }
      el.diag.textContent = SH("splash.err.copied");
      later(function () { el.diag.textContent = SH("splash.err.copy"); }, 1400);
    });

    function toMin(source) {
      if (st.minOn || killed) return;
      st.minOn = true;
      rootEl.classList.add("sp-min", "sp-stage2");
      el.skip.hidden = true;
      if (source === "user") beacon("cpshell_splash_min");
    }
    el.skip.addEventListener("click", function () { toMin("user"); });
    window.addEventListener("keydown", function (ev) {
      if (!killed && ev.key === "Escape" && st.mode !== "warm") toMin("user");
    });

    // ── 模式判定：config（白标检测）与首次探活并行，谁都不许拖住首幕 ───────
    var cfgP = Promise.resolve(null);
    var healthP = Promise.resolve(false);
    if (window.shell && typeof window.shell.getConfig === "function") {
      cfgP = window.shell.getConfig().catch(function () { return null; });
    }
    if (window.shell && typeof window.shell.backendHealth === "function") {
      healthP = Promise.race([
        window.shell.backendHealth().then(function (h) { return !!(h && h.ok); }, function () { return false; }),
        new Promise(function (r) { later(function () { r(false); }, 1300); }),
      ]);
    }
    Promise.all([cfgP, healthP]).then(function (r) {
      if (!killed) decideMode(r[0], r[1]);
    }, function () {
      if (!killed) decideMode(null, false);
    });

    function decideMode(cfg, healthOk) {
      // ready 事件可能先于模式判定到达（收件箱未启用时 renderer 立即通知收幕）：
      // 已在谢幕就别再套模式类/发模式埋点了。
      if (st.doneStarted) return;
      var brand = cfg && cfg.brand;
      var whiteLabel = !!(brand && typeof brand === "object" && Object.keys(brand).length > 0);
      var reduced = false;
      try { reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches; } catch (e) { /* ignore */ }
      st.mode = M.spInitMode({ whiteLabel: whiteLabel, healthOk: healthOk, reducedMotion: reduced, pref: "" });

      if (st.mode === "skip") { beacon("cpshell_splash_skip_wl"); kill("white-label"); return; }
      if (healthOk) st.healthOk = true;

      if (st.mode === "warm") {
        st.warmT0 = Date.now();
        rootEl.classList.add("sp-warm", "sp-stage2");
        // 暖启动=回访：用「欢迎回来，指挥官」问候（冷启动收幕才用「已连接」宣告）。
        setAmb(M.spAmbientKey("done", 1), true);
        beacon("cpshell_splash_warm");
      } else if (st.mode === "cold-min") {
        toMin("auto");
        beacon("cpshell_splash_cold");
      } else {
        // cold-full：第一幕（无界母品牌）2.2s 后进第二幕；3s 后亮出「跳过动画」。
        later(function () { if (!killed && !st.minOn) rootEl.classList.add("sp-stage2"); }, 2200);
        later(function () { if (!killed && !st.minOn && !st.errOn) el.skip.hidden = false; }, 3000);
        beacon("cpshell_splash_cold");
      }
      startEngines();
    }

    // ── 三台引擎：真实信号轮询 / 工作台 webview 导航钩子 / 渲染循环 ────────
    function startEngines() {
      hookWebview();
      every(pollTick, 700);
      pollTick();
      requestAnimationFrame(renderTick);
    }

    function hookWebview() {
      var hooked = false;
      var probe = every(function () {
        if (killed || hooked) { try { clearInterval(probe); } catch (e) { /* ignore */ } return; }
        var wv = document.querySelector('webview[data-kind="backend"]');
        if (!wv) return;
        hooked = true;
        try { clearInterval(probe); } catch (e) { /* ignore */ }
        var mark = function () {
          try {
            var u = typeof wv.getURL === "function" ? wv.getURL() : "";
            if (/^https?:/i.test(String(u || ""))) st.wsNav = true;
          } catch (e) { /* ignore */ }
        };
        wv.addEventListener("dom-ready", mark);
        wv.addEventListener("did-navigate", mark);
      }, 500);
    }

    function pollTick() {
      if (killed || st.doneStarted) return;
      var sh = window.shell || {};
      if (typeof sh.backendSpawnStatus === "function") {
        sh.backendSpawnStatus().then(function (s) {
          if (s) { st.spawn = String(s.status || ""); st.spawnErr = String(s.lastError || ""); }
        }, function () { /* ignore */ });
      }
      if (!st.healthOk && typeof sh.backendHealth === "function") {
        sh.backendHealth().then(function (h) {
          if (h && h.ok && !st.healthOk) {
            st.healthOk = true;
            // 自适应 ETA：记下这台机器真实的「冷启动→服务就绪」耗时，下次进度节奏贴身。
            if (st.mode !== "warm") lsSet("aitr.splash.last_cold_ms", Date.now() - t0);
          }
        }, function () { /* ignore */ });
      }

      var now = Date.now();
      var next = M.spPhase({ spawnStatus: st.spawn, healthOk: st.healthOk, wsNav: st.wsNav, wsReady: st.wsReady }, st.phase);
      if (next !== st.phase) onPhaseChange(st.phase, next, now);

      var elapsedForSeg = st.phase === "engine" ? (now - t0) : (now - st.phaseT0);
      st.progress = M.spProgress(st.progress, M.spProgressTarget(st.phase, elapsedForSeg, etaMs));

      if (!st.errOn && st.mode !== "warm") {
        var key = M.spAmbientKey(st.phase, M.spAmbientIndex(st.phase, now - st.phaseT0));
        if (key !== st.ambKey) setAmb(key, false);
        el.real.textContent = realLine(now);
      }
    }

    function onPhaseChange(prev, next, now) {
      st.phase = next;
      st.phaseT0 = now;
      // 终端自检流：上一阶段收口打一行 ✓（done 由 finish() 收幕，不打行）。
      if (next !== "done" && prev !== "done") appendTerm(SH("splash.term." + prev));
    }

    function realLine(now) {
      var line = SH("splash.real", {
        n: M.spStageNum(st.phase),
        stage: SH("splash.stage." + st.phase),
        secs: Math.max(0, Math.round((now - t0) / 1000)),
      });
      if (st.phase === "engine") {
        var total = now - t0;
        if (M.spSlow(total, etaMs)) line += SH("splash.real_slow");
        else {
          var remain = M.spRemainSecs(total, etaMs);
          if (remain != null) line += SH("splash.real_eta", { secs: remain });
        }
      }
      return line;
    }

    function setAmb(key, instant) {
      st.ambKey = key;
      el.amb.textContent = SH(key);
      if (!instant && !st.minOn) {
        el.amb.classList.remove("glitch");
        void el.amb.offsetWidth; // 重排一次以重触发一次性故障闪动画
        el.amb.classList.add("glitch");
      }
    }

    function appendTerm(text) {
      if (st.minOn || st.mode === "warm") return;
      var div = document.createElement("div");
      div.textContent = text;
      el.term.appendChild(div);
      while (el.term.childNodes.length > 5) el.term.removeChild(el.term.firstChild);
    }

    function renderTick() {
      if (killed) return;
      var target = st.progress;
      var d = target - st.shown;
      st.shown = Math.abs(d) < 0.05 ? target : st.shown + d * 0.14;
      el.fill.style.width = st.shown.toFixed(2) + "%";
      el.pct.textContent = Math.floor(st.shown) + "%";
      for (var i = 0; i < el.nodes.length; i++) {
        var at = parseFloat(el.nodes[i].getAttribute("data-at") || "0");
        el.nodes[i].classList.toggle("lit", st.shown + 0.2 >= at);
      }
      requestAnimationFrame(renderTick);
    }

    function showError(msg) {
      st.errOn = true;
      rootEl.classList.add("sp-error");
      el.errMsg.textContent = msg;
      el.err.hidden = false;
      el.skip.hidden = true;
      if (!st.errBeaconed) { st.errBeaconed = true; beacon("cpshell_splash_error"); }
    }

    function hideError() {
      st.errOn = false;
      rootEl.classList.remove("sp-error");
      el.err.hidden = true;
    }

    function finish() {
      if (killed || st.doneStarted) return;
      st.doneStarted = true;
      hideError();
      st.phase = "done";
      st.progress = 100;
      rootEl.classList.add("sp-done");
      if (st.mode !== "warm") setAmb(M.spAmbientKey("done", 0), true); // 暖场保住问候语不被覆写
      el.real.textContent = SH("splash.real", { n: 6, stage: SH("splash.stage.done"), secs: Math.round((Date.now() - t0) / 1000) });
      beacon("cpshell_splash_done_" + M.spDurBucket(Date.now() - t0));
      // 暖场至少驻留 1s（快机上闪一下就没＝像故障）；冷启给充能+光波 0.75s。
      var hold = st.mode === "warm" ? Math.max(250, 1000 - (Date.now() - st.warmT0)) : 750;
      later(function () {
        rootEl.classList.add("sp-out");
        later(function () { kill("ready"); }, 560);
      }, hold);
    }
  }
})();
