/* ── window.uiPrompt(title, def, opts) → Promise<string|null> ──────────────────
   页内输入弹层，替代 window.prompt()。

   为什么要有它（2026-09-05 #173「批量挂链点了没反应」）：Electron 渲染进程里
   window.prompt() 直接抛「is and will not be supported」——
   `typeof prompt === "function"` 为真，调用即抛，按钮看起来「静默死」。alert/
   confirm 都正常，只有 prompt 是坑。本文件一次定义、全站可用：
   · 经 /copilot/ 静态前缀被 _win_unique.html（base + workspace_base 两壳）与
     shared/copilot/app.html（iframe App 宿主）三处引入；
   · 组件侧调用统一走 `window.uiPrompt`，本文件缺席时组件应自行降级（返回 null
     ＝视同取消），绝不回落原生 prompt 调用。

   契约：
   · resolve(string)＝用户确认（含空串）；resolve(null)＝取消 / Esc / 点背板；
   · 同时只允许一个弹层在场：第二次调用会先把前一个按取消收掉；
   · 打开即聚焦输入框并全选默认值；Enter 确认、Esc 取消；
   · opts = { ok, cancel, placeholder, type, hint, min, max, step, preview, danger, match }
     - type 默认 "text"，可传 "number"；number 且给了 min/max → 渲染 −/+ 步进器
       并把值夹在 [min,max]（越界回弹，不让「0 天」「999 天」直接过闸）；
     - hint：标题下方一行说明（次级色，不再和标题争同一行）；
     - danger：确认键走危险色（用户管理 P0-4 删除逐字确认，2026-09-11）——令牌链
       `--red → --tk-danger → --cp-danger → --danger → #dc2626`，同样不引入新字面量；
     - match：期望的逐字输入（如用户名）；给了它，输入与之不等时确认键禁用、Enter 不过闸
       ——把「输错就取消」提前到「输对才可点」，少一次失败 toast；
     - preview(value) → string | Promise<string>：值变化后（120ms 去抖）实时渲染预览行
       （#209「符合条件 N · 将挂 min(N,50) · 跳过 M」），抛错/返回空即隐藏；
     - 按钮文案未传时按页面语言给 zh/en 缺省（不依赖任何 i18n 系统，App 宿主
       与 Jinja 壳都能用）。
   · 主题（#209 YD2SMM：工作台壳白底压深色主题）：三宿主各自的令牌都读——
     copilot App `--cp-*`、工作台壳 `--tk-surface/--tk-text/--tk-border/--tk-brand`、
     经典后台壳 `--card/--t/--bd/--p`、收件箱 `--bg-panel/--text-main/--border/--accent`，
     最后才是中性回落值；不引入新的品牌色字面量。
   静态门禁 tests/test_no_bare_prompt.py 禁止模板与共享组件再出现原生 prompt 调用；
   tests/test_ui_prompt_theme_209.py 钉令牌链与步进/预览契约。 */
(function () {
  "use strict";
  if (typeof window === "undefined" || typeof window.uiPrompt === "function") return;

  var STYLE_ID = "ui-prompt-style";
  /* 令牌链：cp（App 宿主）→ tk（工作台壳）→ 经典壳 → 收件箱 → 中性回落 */
  var SURFACE = "var(--cp-surface,var(--tk-surface,var(--card,var(--bg-panel,#fff))))";
  var TEXT = "var(--cp-text,var(--tk-text,var(--t,var(--text-main,#111827))))";
  var MUTED = "var(--cp-text-dim,var(--tk-text-muted,var(--t2,var(--text-sub,#6b7280))))";
  var BORDER = "var(--cp-border,var(--tk-border,var(--bd,var(--border,#d1d5db))))";
  var INPUT_BG = "var(--cp-bg,var(--tk-surface-2,var(--input,var(--bg-soft,var(--tk-surface,var(--card,#fff))))))";
  var BRAND = "var(--p,var(--tk-brand,var(--cp-accent,var(--accent,#1e8cf2))))";
  var DANGER = "var(--red,var(--tk-danger,var(--cp-danger,var(--danger,#dc2626))))";
  var CSS =
    ".uip-backdrop{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;" +
    "justify-content:center;background:rgba(15,23,42,.45);padding:16px;box-sizing:border-box;}" +
    ".uip-card{width:min(440px,100%);background:" + SURFACE + ";color:" + TEXT + ";" +
    "border:1px solid " + BORDER + ";border-radius:12px;box-shadow:0 18px 48px rgba(0,0,0,.28);" +
    "padding:16px 16px 12px;box-sizing:border-box;font:inherit;font-size:14px;line-height:1.5;}" +
    ".uip-title{margin:0 0 6px;font-size:14px;font-weight:600;white-space:pre-wrap;word-break:break-word;}" +
    ".uip-hint{margin:0 0 10px;font-size:12.5px;line-height:1.5;color:" + MUTED + ";white-space:pre-wrap;word-break:break-word;}" +
    ".uip-row{display:flex;align-items:center;gap:6px;}" +
    ".uip-input{display:block;flex:1 1 auto;width:100%;min-width:0;box-sizing:border-box;font:inherit;font-size:14px;" +
    "padding:8px 10px;border:1px solid " + BORDER + ";border-radius:8px;background:" + INPUT_BG + ";color:" + TEXT + ";" +
    "outline:none;caret-color:" + BRAND + ";}" +
    ".uip-input::placeholder{color:" + MUTED + ";opacity:.85;}" +
    ".uip-input:focus{border-color:" + BRAND + ";box-shadow:0 0 0 3px color-mix(in srgb," + BRAND + " 22%,transparent);}" +
    ".uip-input[type=number]{text-align:center;-moz-appearance:textfield;}" +
    ".uip-input[type=number]::-webkit-outer-spin-button,.uip-input[type=number]::-webkit-inner-spin-button{-webkit-appearance:none;margin:0;}" +
    ".uip-step{appearance:none;flex:0 0 36px;height:36px;font:inherit;font-size:18px;line-height:1;border-radius:8px;cursor:pointer;" +
    "border:1px solid " + BORDER + ";background:" + INPUT_BG + ";color:" + TEXT + ";}" +
    ".uip-step:hover{border-color:" + BRAND + ";}" +
    ".uip-step:disabled{opacity:.4;cursor:default;}" +
    ".uip-unit{flex:0 0 auto;font-size:13px;color:" + MUTED + ";}" +
    ".uip-preview{margin:10px 0 0;min-height:18px;font-size:12.5px;line-height:1.5;color:" + MUTED + ";white-space:pre-wrap;}" +
    ".uip-preview:empty{display:none;}" +
    ".uip-preview.busy{opacity:.7;}" +
    ".uip-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px;}" +
    ".uip-btn{appearance:none;font:inherit;font-size:13px;padding:6px 14px;border-radius:8px;cursor:pointer;" +
    "border:1px solid " + BORDER + ";background:transparent;color:" + TEXT + ";opacity:1;}" +
    ".uip-btn:hover{background:color-mix(in srgb,currentColor 8%,transparent);}" +
    ".uip-btn.ok{background:" + BRAND + ";border-color:" + BRAND + ";color:#fff;}" +
    ".uip-btn.ok:hover{filter:brightness(.92);}" +
    ".uip-btn.ok:disabled{opacity:.5;cursor:default;filter:none;}" +
    ".uip-btn.ok.danger{background:" + DANGER + ";border-color:" + DANGER + ";}" +
    ".uip-input.danger:focus{border-color:" + DANGER + ";box-shadow:0 0 0 3px color-mix(in srgb," + DANGER + " 22%,transparent);caret-color:" + DANGER + ";}" +
    ".uip-btn:focus-visible{outline:2px solid " + BRAND + ";outline-offset:2px;}";

  function ensureStyle() {
    var old = document.getElementById(STYLE_ID);
    if (old && old.textContent === CSS) return;
    if (old && old.parentNode) old.parentNode.removeChild(old);
    var st = document.createElement("style");
    st.id = STYLE_ID;
    st.textContent = CSS;
    (document.head || document.documentElement).appendChild(st);
  }

  function isZh() {
    var lang = "";
    try { lang = (document.documentElement.getAttribute("lang") || navigator.language || ""); } catch (_e) {}
    return /^zh/i.test(String(lang));
  }

  function num(v, d) {
    var n = Number(v);
    return isFinite(n) ? n : d;
  }

  var _active = null; // { close: fn }

  function uiPrompt(title, def, opts) {
    opts = opts || {};
    if (_active) { try { _active.close(null); } catch (_e) {} }
    return new Promise(function (resolve) {
      ensureStyle();
      var zh = isZh();
      var okLabel = opts.ok || (zh ? "\u786e\u5b9a" : "OK");
      var cancelLabel = opts.cancel || (zh ? "\u53d6\u6d88" : "Cancel");
      var isNumber = (opts.type || "text") === "number";
      var hasRange = isNumber && (opts.min != null || opts.max != null);
      var lo = opts.min != null ? num(opts.min, -Infinity) : -Infinity;
      var hi = opts.max != null ? num(opts.max, Infinity) : Infinity;
      var step = Math.abs(num(opts.step, 1)) || 1;
      var isDanger = !!opts.danger;
      var match = (opts.match == null || isNumber) ? null : String(opts.match);

      var back = document.createElement("div");
      back.className = "uip-backdrop";
      back.setAttribute("role", "dialog");
      back.setAttribute("aria-modal", "true");

      var card = document.createElement("div");
      card.className = "uip-card";

      var h = document.createElement("div");
      h.className = "uip-title";
      h.textContent = title == null ? "" : String(title);
      card.appendChild(h);

      if (opts.hint) {
        var hint = document.createElement("div");
        hint.className = "uip-hint";
        hint.textContent = String(opts.hint);
        card.appendChild(hint);
      }

      var row = document.createElement("div");
      row.className = "uip-row";
      var input = document.createElement("input");
      input.className = "uip-input" + (isDanger ? " danger" : "");
      input.type = isNumber ? "number" : (opts.type || "text");
      input.value = def == null ? "" : String(def);
      if (opts.placeholder) input.placeholder = String(opts.placeholder);
      input.setAttribute("autocomplete", "off");
      if (isNumber) {
        if (opts.min != null) input.min = String(lo);
        if (opts.max != null) input.max = String(hi);
        input.step = String(step);
        input.inputMode = "numeric";
      }
      var bMinus = null, bPlus = null;
      if (hasRange) {
        bMinus = document.createElement("button");
        bMinus.type = "button"; bMinus.className = "uip-step"; bMinus.textContent = "\u2212";
        bMinus.setAttribute("aria-label", "-");
        bPlus = document.createElement("button");
        bPlus.type = "button"; bPlus.className = "uip-step"; bPlus.textContent = "+";
        bPlus.setAttribute("aria-label", "+");
        row.appendChild(bMinus);
        row.appendChild(input);
        row.appendChild(bPlus);
      } else {
        row.appendChild(input);
      }
      if (opts.unit) {
        var unit = document.createElement("span");
        unit.className = "uip-unit";
        unit.textContent = String(opts.unit);
        row.appendChild(unit);
      }
      card.appendChild(row);

      var preview = null;
      if (typeof opts.preview === "function") {
        preview = document.createElement("div");
        preview.className = "uip-preview";
        preview.setAttribute("aria-live", "polite");
        card.appendChild(preview);
      }

      var actions = document.createElement("div");
      actions.className = "uip-actions";
      var bCancel = document.createElement("button");
      bCancel.type = "button"; bCancel.className = "uip-btn"; bCancel.textContent = cancelLabel;
      var bOk = document.createElement("button");
      bOk.type = "button"; bOk.className = "uip-btn ok" + (isDanger ? " danger" : ""); bOk.textContent = okLabel;
      actions.appendChild(bCancel); actions.appendChild(bOk);
      card.appendChild(actions);
      back.appendChild(card);

      function clamp(v) {
        var n = num(v, NaN);
        if (!isFinite(n)) return null;
        if (n < lo) n = lo;
        if (n > hi) n = hi;
        return n;
      }
      function current() {
        if (!isNumber) return input.value;
        var n = clamp(input.value);
        return n == null ? "" : String(n);
      }
      function matchOk() {
        return match == null || String(input.value).trim() === match;
      }
      function syncSteppers() {
        if (match != null) { bOk.disabled = !matchOk(); return; }
        if (!hasRange) return;
        var n = clamp(input.value);
        bMinus.disabled = (n != null && n <= lo);
        bPlus.disabled = (n != null && n >= hi);
        bOk.disabled = (n == null);
      }
      var previewSeq = 0, previewTimer = null;
      function runPreview() {
        if (!preview) return;
        var v = current();
        var seq = ++previewSeq;
        preview.classList.add("busy");
        var out;
        try { out = opts.preview(v); } catch (_e) { out = ""; }
        Promise.resolve(out).then(function (txt) {
          if (seq !== previewSeq) return;
          preview.classList.remove("busy");
          preview.textContent = txt == null ? "" : String(txt);
        }, function () {
          if (seq !== previewSeq) return;
          preview.classList.remove("busy");
          preview.textContent = "";
        });
      }
      function schedulePreview() {
        if (!preview) return;
        if (previewTimer) clearTimeout(previewTimer);
        previewTimer = setTimeout(runPreview, 120);
      }
      function bump(d) {
        var n = clamp(input.value);
        if (n == null) n = isFinite(lo) ? lo : 0;
        n = clamp(n + d * step);
        input.value = String(n);
        syncSteppers();
        schedulePreview();
      }

      var done = false;
      var prevFocus = document.activeElement;
      function close(val) {
        if (done) return;
        done = true;
        if (previewTimer) clearTimeout(previewTimer);
        previewSeq++;
        document.removeEventListener("keydown", onKey, true);
        if (back.parentNode) back.parentNode.removeChild(back);
        if (_active && _active.close === close) _active = null;
        try { if (prevFocus && typeof prevFocus.focus === "function") prevFocus.focus(); } catch (_e) {}
        resolve(val);
      }
      function confirmValue() {
        if (!matchOk()) { try { input.focus(); } catch (_e) {} return; }
        if (isNumber) {
          var n = clamp(input.value);
          if (n == null) { try { input.focus(); } catch (_e) {} return; }
          close(String(n));
          return;
        }
        close(input.value);
      }
      function onKey(ev) {
        if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); close(null); }
        else if (ev.key === "Enter" && ev.target === input) { ev.preventDefault(); ev.stopPropagation(); confirmValue(); }
        else if (hasRange && ev.target === input && (ev.key === "ArrowUp" || ev.key === "ArrowDown")) {
          ev.preventDefault(); bump(ev.key === "ArrowUp" ? 1 : -1);
        }
      }
      bOk.addEventListener("click", confirmValue);
      bCancel.addEventListener("click", function () { close(null); });
      back.addEventListener("mousedown", function (ev) { if (ev.target === back) close(null); });
      card.addEventListener("mousedown", function (ev) { ev.stopPropagation(); });
      input.addEventListener("input", function () { syncSteppers(); schedulePreview(); });
      input.addEventListener("blur", function () {
        if (!isNumber) return;
        var n = clamp(input.value);
        if (n != null && String(n) !== input.value) { input.value = String(n); syncSteppers(); schedulePreview(); }
      });
      if (bMinus) bMinus.addEventListener("click", function () { bump(-1); });
      if (bPlus) bPlus.addEventListener("click", function () { bump(1); });
      document.addEventListener("keydown", onKey, true);

      _active = { close: close };
      (document.body || document.documentElement).appendChild(back);
      syncSteppers();
      runPreview();
      try { input.focus(); input.select(); } catch (_e) {}
    });
  }

  window.uiPrompt = uiPrompt;
})();
