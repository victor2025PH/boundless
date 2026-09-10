/* 「这版改变了什么」升级首开弹窗（Q-4 #267 F，2026-09-10）
   - 由 workspace_base.html 尾部按 master/operator 装载；GET /api/release-notice 不 pending 就零痕迹。
   - 每行「保持 / 改回」二选一（switch 行）；班表类改回必须填 IANA 时区（红线③）。
   - 文案全部来自 API labels / rows（后端 tr() 已本地化），本文件不含任何语种字面量。
   - 关闭（右上 ×）/「全部保持新默认」= 全部 keep → 写显式新默认 + 记 seen；同一版只弹一次。 */
(function () {
  if (window.__rnBooted) return;
  window.__rnBooted = true;
  if (document.documentElement.getAttribute('data-ws-embed') === '1') return;

  var CSS = '' +
    '.rn-mask{position:fixed;inset:0;background:rgba(15,23,42,.45);z-index:9000;display:flex;align-items:center;justify-content:center;padding:1rem}' +
    '.rn-box{background:var(--bg,#fff);color:var(--t1,#0f172a);border-radius:14px;max-width:640px;width:100%;max-height:90vh;overflow:auto;box-shadow:0 18px 60px rgba(0,0,0,.25);font-size:.86rem;line-height:1.55}' +
    '.rn-hd{display:flex;align-items:flex-start;gap:.6rem;padding:1rem 1.1rem .4rem}' +
    '.rn-hd h3{margin:0;font-size:1.05rem;flex:1}' +
    '.rn-x{border:0;background:transparent;font-size:1.2rem;cursor:pointer;color:var(--t3,#64748b);line-height:1}' +
    '.rn-sub{padding:0 1.1rem .6rem;color:var(--t2,#334155);font-size:.8rem}' +
    '.rn-row{margin:0 1.1rem .6rem;padding:.65rem .8rem;border:1px solid var(--bd,#e2e8f0);border-radius:10px}' +
    '.rn-row .t{font-weight:600}' +
    '.rn-row .d{color:var(--t2,#334155);font-size:.78rem;margin:.2rem 0 .45rem}' +
    '.rn-row .st{display:inline-block;font-size:.7rem;color:var(--t3,#64748b);margin-right:.5rem}' +
    '.rn-opts{display:flex;gap:.9rem;flex-wrap:wrap;align-items:center}' +
    '.rn-opts label{display:inline-flex;align-items:center;gap:.3rem;cursor:pointer}' +
    '.rn-tz{margin-top:.45rem;display:none}' +
    '.rn-tz.on{display:block}' +
    '.rn-tz input{width:100%;box-sizing:border-box;padding:.35rem .5rem;border:1px solid var(--bd,#e2e8f0);border-radius:6px;font:inherit}' +
    '.rn-tz .h{font-size:.72rem;color:var(--t3,#64748b);margin-top:.2rem}' +
    '.rn-err{color:#b91c1c;font-size:.76rem;margin:0 1.1rem;min-height:1em}' +
    '.rn-ft{display:flex;gap:.5rem;justify-content:flex-end;padding:.6rem 1.1rem 1rem}' +
    '.rn-btn{padding:.45rem .9rem;border-radius:8px;border:1px solid var(--bd,#e2e8f0);background:var(--bg2,#f8fafc);cursor:pointer;font:inherit}' +
    '.rn-btn.p{background:var(--p,#1e8cf2);border-color:var(--p,#1e8cf2);color:#fff}' +
    '.rn-btn[disabled]{opacity:.6;cursor:default}';

  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }
  function fmt(s, m) {
    return String(s || '').replace(/\{(\w+)\}/g, function (_, k) { return m && m[k] != null ? m[k] : ''; });
  }
  function fetchJ(url, opt) {
    var f = window.apiFetch || window.fetch;
    return f(url, opt).then(function (r) { return r.json(); });
  }

  function build(d) {
    var L = d.labels || {};
    var st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    var mask = document.createElement('div'); mask.className = 'rn-mask'; mask.id = 'rn-mask';
    var h = '<div class="rn-box" role="dialog" aria-modal="true">' +
      '<div class="rn-hd"><h3>' + esc(L.rn_title) + '</h3><button type="button" class="rn-x" id="rn-x" aria-label="close">×</button></div>' +
      '<div class="rn-sub">' + esc(L.rn_sub) + '</div>';
    (d.rows || []).forEach(function (r, i) {
      var isSwitch = r.kind === 'switch';
      var newOn = r['new'] === true;
      h += '<div class="rn-row" data-key="' + esc(r.key) + '" data-needs-tz="' + (r.needs_timezone ? '1' : '0') + '">' +
        '<div class="t">' + esc(r.title) + '</div>' +
        '<div class="d">' + esc(r.desc) + '</div>';
      if (isSwitch) {
        h += '<span class="st">' + esc(r.old === true ? L.rn_was_on : L.rn_was_off) + '</span>' +
          '<span class="st">' + esc(newOn ? L.rn_now_on : L.rn_now_off) + '</span>' +
          '<div class="rn-opts">' +
          '<label><input type="radio" name="rn-c-' + i + '" value="keep" checked> ' + esc(newOn ? L.rn_keep_on : L.rn_keep_off) + '</label>' +
          '<label><input type="radio" name="rn-c-' + i + '" value="flip"> ' + esc(newOn ? L.rn_turn_off : L.rn_turn_on) + '</label>' +
          '</div>';
        if (r.needs_timezone) {
          h += '<div class="rn-tz"><label>' + esc(L.rn_tz_label) + '</label>' +
            '<input type="text" class="rn-tz-in" list="rn-tz-list" value="' + esc(r.current_timezone || '') + '" placeholder="' + esc(L.rn_tz_ph) + '">' +
            '<div class="h">' + esc(L.rn_tz_hint) + '</div></div>';
        }
      } else {
        h += '<span class="st">' + esc(fmt(L.rn_value_change, { old: r.old, 'new': r['new'] })) + '</span>';
      }
      h += '</div>';
    });
    h += '<datalist id="rn-tz-list">' +
      ['America/New_York', 'America/Los_Angeles', 'America/Chicago', 'Europe/London', 'Europe/Berlin', 'Asia/Shanghai', 'Asia/Tokyo', 'Asia/Singapore', 'Australia/Sydney']
        .map(function (z) { return '<option value="' + z + '">'; }).join('') + '</datalist>' +
      '<div class="rn-err" id="rn-err"></div>' +
      '<div class="rn-ft">' +
      '<button type="button" class="rn-btn" id="rn-later">' + esc(L.rn_later) + '</button>' +
      '<button type="button" class="rn-btn p" id="rn-apply">' + esc(L.rn_apply) + '</button>' +
      '</div></div>';
    mask.innerHTML = h;
    document.body.appendChild(mask);

    mask.addEventListener('change', function (e) {
      var t = e.target;
      if (!t || t.type !== 'radio') return;
      var row = t.closest('.rn-row'); if (!row) return;
      var tz = row.querySelector('.rn-tz'); if (!tz) return;
      tz.classList.toggle('on', t.value === 'flip');
    });

    function collect() {
      var choices = {}, tz = '';
      var bad = null;
      mask.querySelectorAll('.rn-row').forEach(function (row) {
        var key = row.getAttribute('data-key');
        var sel = row.querySelector('input[type=radio]:checked');
        if (!sel) return;
        choices[key] = sel.value;
        if (sel.value === 'flip' && row.getAttribute('data-needs-tz') === '1') {
          var inp = row.querySelector('.rn-tz-in');
          tz = (inp && inp.value || '').trim();
          if (!tz) bad = bad || { row: row, msg: L.rn_tz_required };
          else if (!validTz(tz)) bad = bad || { row: row, msg: L.rn_tz_bad };
        }
      });
      return { choices: choices, timezone: tz, bad: bad };
    }
    function validTz(z) {
      try { new Intl.DateTimeFormat('en-US', { timeZone: z }); return true; } catch (_) { return false; }
    }
    function done(msg) {
      var err = document.getElementById('rn-err');
      if (msg) { err.style.color = 'var(--green,#15803d)'; err.textContent = msg; }
      setTimeout(function () { try { mask.remove(); } catch (_) { } }, msg ? 600 : 0);
    }
    function submit(all) {
      var err = document.getElementById('rn-err'); err.style.color = ''; err.textContent = '';
      var body = all === 'keep' ? { version: d.version, choices: {} } : collect();
      if (body.bad) {
        err.textContent = body.bad.msg;
        var inp = body.bad.row.querySelector('.rn-tz-in'); if (inp) inp.focus();
        return;
      }
      body.version = d.version;
      var btn = document.getElementById('rn-apply'), later = document.getElementById('rn-later');
      btn.disabled = true; later.disabled = true; btn.textContent = L.rn_applying;
      fetchJ('/api/release-notice/apply', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })
        .then(function (r) {
          if (r && r.ok) { done(L.rn_done); return; }
          var why = (r && r.failed && r.failed[0] && (r.failed[0].message || r.failed[0].reason)) || (r && r.reason) || '';
          err.textContent = L.rn_fail + why;
          btn.disabled = false; later.disabled = false; btn.textContent = L.rn_apply;
        })
        .catch(function (e) {
          err.textContent = L.rn_fail + e;
          btn.disabled = false; later.disabled = false; btn.textContent = L.rn_apply;
        });
    }
    document.getElementById('rn-apply').addEventListener('click', function () { submit(); });
    document.getElementById('rn-later').addEventListener('click', function () { submit('keep'); });
    document.getElementById('rn-x').addEventListener('click', function () { submit('keep'); });
  }

  function boot() {
    fetchJ('/api/release-notice', { cache: 'no-store' }).then(function (d) {
      if (d && d.ok && d.pending && (d.rows || []).length) build(d);
    }).catch(function () { /* 弹窗是锦上添花：失败零痕迹 */ });
  }
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
