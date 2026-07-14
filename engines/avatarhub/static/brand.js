/* ==========================================================================
   无界科技 BOUNDLESS · 白标（White-label）品牌桥接 · 全站单一行为真相
   --------------------------------------------------------------------------
   一份品牌配置（localStorage['bd_brand_config'] = {color, name, logo}）驱动全站：
     · color  → brand.css 设计令牌 --bd-acc / --bd-acc-weak（--bd-grad 引用 --bd-acc 自动跟变）
     · name   → 所有带 [data-bd-name] 的元素文本（公司/产品名白标）
     · logo   → 所有带 [data-bd-logo] 的元素文本（图标/字符）
   兼容旧键 avatarhub_brand（控制台取色器历史只存主色）。
   首次替换文案前会把原文存进 data-bd-orig，便于「恢复默认」无需刷新即可还原。
   用法：加载 brand.css 的页面在其后 <script src="/static/brand.js"></script>；
   控制台白标面板通过 window.__brandConfig.set/exportJSON/importJSON/reset 实时改。
   ========================================================================== */
(function () {
  var COLOR_KEY = 'avatarhub_brand';   // 旧键：仅主色（与控制台取色器兼容）
  var CFG_KEY   = 'bd_brand_config';   // 白标配置 JSON

  function triple(rgb){ return String(rgb).replace(/,/g, ' ').replace(/\s+/g, ' ').trim(); }
  function applyColor(rgb){
    if (!rgb) return;
    var t = triple(rgb), r = document.documentElement;
    r.style.setProperty('--bd-acc', 'rgb(' + t + ')');
    r.style.setProperty('--bd-acc-rgb', t);   // 三元组：供客户端 rgb(var(--bd-acc-rgb) / α) 跨设备联动
    r.style.setProperty('--bd-acc-weak', 'rgb(' + t + ' / .16)');
  }
  function getCfg(){
    var c = {};
    try { c = JSON.parse(localStorage.getItem(CFG_KEY) || '{}') || {}; } catch (_) {}
    if (!c.color){ try { var lg = localStorage.getItem(COLOR_KEY); if (lg) c.color = lg; } catch (_) {} }
    return c;
  }
  function applyOne(sel, attr, val){
    var els = document.querySelectorAll(sel), i;
    for (i = 0; i < els.length; i++){
      if (els[i].getAttribute(attr) == null) els[i].setAttribute(attr, els[i].textContent);
      els[i].textContent = val || els[i].getAttribute(attr);
    }
  }
  function applyText(cfg){
    applyOne('[data-bd-name]',    'data-bd-orig',         cfg.name);
    applyOne('[data-bd-logo]',    'data-bd-logo-orig',    cfg.logo);
    applyOne('[data-bd-product]', 'data-bd-product-orig', cfg.product);
  }
  // 标题白标：把 <title> 里的默认公司名替换成白标名（不改产品线/页面身份词）。
  var TITLE_DEFAULTS = ['无界科技 BOUNDLESS', '无界 BOUNDLESS'];
  function applyTitle(cfg){
    if (!cfg.name) return;
    if (window.__bdTitleOrig == null) window.__bdTitleOrig = document.title;
    var t = window.__bdTitleOrig, k;
    for (k = 0; k < TITLE_DEFAULTS.length; k++) t = t.split(TITLE_DEFAULTS[k]).join(cfg.name);
    document.title = t;
  }
  function applyAll(cfg){
    cfg = cfg || getCfg(); if (cfg.color) applyColor(cfg.color); applyText(cfg); applyTitle(cfg);
    // P12 广播「品牌配置已生效」：依赖 contact 等字段的旁路 UI（授权横幅/卡）借此重画——
    // 首屏 pullServer 是异步的，不广播就要等下一轮 60s 轮询才能看到联系按钮。
    try { document.dispatchEvent(new CustomEvent('bd:brand-applied')); } catch (_) {}
  }

  // 服务端持久化：跨浏览器/跨终端的单一真相。set/import/reset 写回，首屏拉取覆盖本地。
  function pushServer(cfg){
    try {
      fetch('/api/brand', { method: 'POST', headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ config: cfg || {} }) }).catch(function(){});
    } catch (_) {}
  }
  function pullServer(){
    try {
      fetch('/api/brand', { cache: 'no-store' })
        .then(function (r){ return r.ok ? r.json() : null; })
        .then(function (j){
          if (!j || !j.config) return;
          var s = j.config, has = false, k;
          for (k in s){ if (s[k]) has = true; }
          if (!has) return;                       // 服务端空 → 保持本地（不回写）
          try {
            localStorage.setItem(CFG_KEY, JSON.stringify(s));
            if (s.color) localStorage.setItem(COLOR_KEY, s.color);
          } catch (_) {}
          applyAll(s);
        }).catch(function(){});
    } catch (_) {}
  }

  // 主色尽早应用（防闪烁）；文案待 DOM 就绪；随后异步用服务端配置覆盖。
  var _c = getCfg(); if (_c.color) applyColor(_c.color);
  if (document.readyState !== 'loading') { applyAll(getCfg()); pullServer(); }
  else document.addEventListener('DOMContentLoaded', function () { applyAll(getCfg()); pullServer(); });

  window.__applyBrand = applyColor;   // 兼容旧调用（仅主色）
  window.__brandConfig = {
    get: getCfg,
    set: function (partial){
      var c = getCfg(), k;
      for (k in partial){ if (partial[k] == null || partial[k] === '') delete c[k]; else c[k] = partial[k]; }
      try { localStorage.setItem(CFG_KEY, JSON.stringify(c)); } catch (_) {}
      if ('color' in partial){ try { if (partial.color) localStorage.setItem(COLOR_KEY, partial.color); else localStorage.removeItem(COLOR_KEY); } catch (_) {} }
      applyAll(c); pushServer(c); return c;
    },
    apply: function (){ applyAll(getCfg()); },
    exportJSON: function (){ return JSON.stringify(getCfg(), null, 2); },
    importJSON: function (str){
      var c = JSON.parse(str);
      try { localStorage.setItem(CFG_KEY, JSON.stringify(c)); } catch (_) {}
      if (c.color){ try { localStorage.setItem(COLOR_KEY, c.color); } catch (_) {} }
      applyAll(c); pushServer(c); return c;
    },
    reset: function (){
      try { localStorage.removeItem(CFG_KEY); localStorage.removeItem(COLOR_KEY); } catch (_) {}
      var r = document.documentElement;
      r.style.removeProperty('--bd-acc'); r.style.removeProperty('--bd-acc-rgb'); r.style.removeProperty('--bd-acc-weak');
      applyText({});   // name/logo/product 回退到原文（无需刷新）
      if (window.__bdTitleOrig != null) document.title = window.__bdTitleOrig;
      pushServer({});  // 同步清空服务端（恢复出厂·整机生效）
    }
  };
})();
