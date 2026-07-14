/* 管理面令牌打通（与 avatar_hub _auth_middleware 配套）
 * - 把 localStorage.ah_token 同步到同源 cookie ah_token：使 fetch / WebSocket / SSE /
 *   <audio><video> 等所有同源请求自动携带令牌，无需逐处改调用点。
 * - 若服务端启用了鉴权且当前未授权，顶部弹出输入条引导设置令牌。
 * localStorage 按 origin 共享：任一页面设过一次，其余页面(ops/ui/phone)自动生效。 */
(function () {
  function syncCookie() {
    try {
      var t = localStorage.getItem('ah_token');
      if (t) document.cookie = 'ah_token=' + encodeURIComponent(t) +
        ';path=/;max-age=31536000;samesite=lax';
    } catch (e) {}
  }
  syncCookie();

  window.ahSetToken = function (t) {
    try {
      if (t) localStorage.setItem('ah_token', t); else localStorage.removeItem('ah_token');
    } catch (e) {}
    syncCookie();
    location.reload();
  };

  function banner(msg, authed) {
    if (document.getElementById('_ahbar')) return;
    var bar = document.createElement('div');
    bar.id = '_ahbar';
    bar.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:99999;padding:8px 12px;' +
      'font:13px system-ui,sans-serif;display:flex;gap:8px;align-items:center;justify-content:center;' +
      'background:' + (authed ? 'rgba(34,197,94,.15)' : 'rgba(239,68,68,.15)') +
      ';color:' + (authed ? '#bbf7d0' : '#fecaca') +
      ';border-bottom:1px solid ' + (authed ? 'rgba(34,197,94,.4)' : 'rgba(239,68,68,.45)') + ';';
    bar.innerHTML = '<span>' + msg + '</span>';
    if (!authed) {
      var inp = document.createElement('input');
      inp.type = 'password'; inp.placeholder = '运维令牌';
      inp.style.cssText = 'padding:3px 8px;border-radius:6px;border:1px solid #475569;background:#0a0e16;color:#e5edf7;';
      var btn = document.createElement('button');
      btn.textContent = '保存'; btn.style.cssText = 'padding:3px 12px;border-radius:6px;border:0;background:#7cc4ff;color:#04101f;font-weight:700;cursor:pointer;';
      btn.onclick = function () { if (inp.value.trim()) window.ahSetToken(inp.value.trim()); };
      inp.onkeydown = function (e) { if (e.key === 'Enter') btn.onclick(); };
      bar.appendChild(inp); bar.appendChild(btn);
    }
    document.body.appendChild(bar);
    document.body.style.paddingTop = '40px';
  }

  // 探测鉴权状态：启用且未授权 → 弹输入条；启用且已授权 → 短暂提示已锁定
  try {
    fetch('/api/auth/status', { cache: 'no-store' }).then(function (r) { return r.json(); })
      .then(function (d) {
        if (!d || !d.auth_enabled) return;
        if (d.authed) {
          banner('🔒 管理面已启用令牌（本会话已授权）', true);
          setTimeout(function () { var b = document.getElementById('_ahbar'); if (b) { b.remove(); document.body.style.paddingTop = ''; } }, 2500);
        } else {
          banner('🔒 管理面已启用令牌：请输入运维令牌以执行写操作', false);
        }
      }).catch(function () {});
  } catch (e) {}
})();
