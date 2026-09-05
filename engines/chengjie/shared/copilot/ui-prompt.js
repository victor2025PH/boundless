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
   · opts = { ok, cancel, placeholder, type }（type 默认 "text"，可传 "number"）；
     按钮文案未传时按页面语言给 zh/en 缺省（不依赖任何 i18n 系统，App 宿主
     与 Jinja 壳都能用）。
   · 样式只用 --cp-、--tk-、--p 主题变量 + 中性回落值，不引入新的品牌色字面量。
   静态门禁 tests/test_no_bare_prompt.py 禁止模板与共享组件再出现原生 prompt 调用。 */
(function () {
  "use strict";
  if (typeof window === "undefined" || typeof window.uiPrompt === "function") return;

  var STYLE_ID = "ui-prompt-style";
  var CSS =
    ".uip-backdrop{position:fixed;inset:0;z-index:2147483000;display:flex;align-items:center;" +
    "justify-content:center;background:rgba(15,23,42,.45);padding:16px;box-sizing:border-box;}" +
    ".uip-card{width:min(420px,100%);background:var(--cp-surface,var(--tk-bg-card,var(--bg-card,#fff)));" +
    "color:var(--cp-text,var(--tk-text,var(--text,#111827)));border:1px solid var(--cp-border,var(--tk-border,#e5e7eb));" +
    "border-radius:12px;box-shadow:0 18px 48px rgba(0,0,0,.28);padding:16px 16px 12px;box-sizing:border-box;" +
    "font:inherit;font-size:14px;line-height:1.5;}" +
    ".uip-title{margin:0 0 10px;font-size:14px;font-weight:600;white-space:pre-wrap;word-break:break-word;}" +
    ".uip-input{display:block;width:100%;box-sizing:border-box;font:inherit;font-size:14px;padding:8px 10px;" +
    "border:1px solid var(--cp-border,var(--tk-border,#d1d5db));border-radius:8px;" +
    "background:var(--cp-bg,var(--tk-bg,#fff));color:inherit;outline:none;}" +
    ".uip-input:focus{border-color:var(--p,var(--tk-brand,#1e8cf2));" +
    "box-shadow:0 0 0 3px color-mix(in srgb,var(--p,var(--tk-brand,#1e8cf2)) 22%,transparent);}" +
    ".uip-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:12px;}" +
    ".uip-btn{appearance:none;font:inherit;font-size:13px;padding:6px 14px;border-radius:8px;cursor:pointer;" +
    "border:1px solid var(--cp-border,var(--tk-border,#d1d5db));background:transparent;color:inherit;}" +
    ".uip-btn:hover{background:color-mix(in srgb,currentColor 8%,transparent);}" +
    ".uip-btn.ok{background:var(--p,var(--tk-brand,#1e8cf2));border-color:var(--p,var(--tk-brand,#1e8cf2));color:#fff;}" +
    ".uip-btn.ok:hover{filter:brightness(.92);}" +
    ".uip-btn:focus-visible{outline:2px solid var(--p,var(--tk-brand,#1e8cf2));outline-offset:2px;}";

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
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

  var _active = null; // { close: fn }

  function uiPrompt(title, def, opts) {
    opts = opts || {};
    if (_active) { try { _active.close(null); } catch (_e) {} }
    return new Promise(function (resolve) {
      ensureStyle();
      var zh = isZh();
      var okLabel = opts.ok || (zh ? "\u786e\u5b9a" : "OK");
      var cancelLabel = opts.cancel || (zh ? "\u53d6\u6d88" : "Cancel");

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

      var input = document.createElement("input");
      input.className = "uip-input";
      input.type = opts.type || "text";
      input.value = def == null ? "" : String(def);
      if (opts.placeholder) input.placeholder = String(opts.placeholder);
      input.setAttribute("autocomplete", "off");
      card.appendChild(input);

      var actions = document.createElement("div");
      actions.className = "uip-actions";
      var bCancel = document.createElement("button");
      bCancel.type = "button"; bCancel.className = "uip-btn"; bCancel.textContent = cancelLabel;
      var bOk = document.createElement("button");
      bOk.type = "button"; bOk.className = "uip-btn ok"; bOk.textContent = okLabel;
      actions.appendChild(bCancel); actions.appendChild(bOk);
      card.appendChild(actions);
      back.appendChild(card);

      var done = false;
      var prevFocus = document.activeElement;
      function close(val) {
        if (done) return;
        done = true;
        document.removeEventListener("keydown", onKey, true);
        if (back.parentNode) back.parentNode.removeChild(back);
        if (_active && _active.close === close) _active = null;
        try { if (prevFocus && typeof prevFocus.focus === "function") prevFocus.focus(); } catch (_e) {}
        resolve(val);
      }
      function onKey(ev) {
        if (ev.key === "Escape") { ev.preventDefault(); ev.stopPropagation(); close(null); }
        else if (ev.key === "Enter" && ev.target === input) { ev.preventDefault(); ev.stopPropagation(); close(input.value); }
      }
      bOk.addEventListener("click", function () { close(input.value); });
      bCancel.addEventListener("click", function () { close(null); });
      back.addEventListener("mousedown", function (ev) { if (ev.target === back) close(null); });
      card.addEventListener("mousedown", function (ev) { ev.stopPropagation(); });
      document.addEventListener("keydown", onKey, true);

      _active = { close: close };
      (document.body || document.documentElement).appendChild(back);
      try { input.focus(); input.select(); } catch (_e) {}
    });
  }

  window.uiPrompt = uiPrompt;
})();
