/* ═══════════════════════════════════════════════════════════════════════════
 * persona_health_panel.js — 概览页「人设体检」面板（personas.html 拆分模块）
 *
 * 用途：
 *   汇总人设列表里"需要处理"的人设，按五类问题分组展示，每行给出跳转动作：
 *     1) 资料不全   —— 完善度 < 90，行说明列出缺失项（角色/性格/标签/声音/头像）
 *     2) 未启用     —— binding_count 为 0（零绑定），按钮跳「应用到…」弹窗
 *     3) 零回复     —— 已绑定但近 7 日 usage_7d === 0；仅当 summary 至少一行携带
 *                      usage_7d 字段且全池总和 > 0 时才启用本组，刚上线无数据
 *                      积累时整组不渲染，避免误报
 *     4) 超过 30 天未更新 —— last_edited_at 非空且距今超过 30 天，最老的排前面；
 *                      仅当 summary 至少一行携带 last_edited_at 字段时才启用本组，
 *                      后端未升级无该字段时整组不渲染，避免误报
 *     5) 重名冲突   —— trim 后同名人设 ≥ 2 个，一组一行，逐 id 提供编辑入口
 *
 * 实现约定：
 *   - IIFE 直挂 window，无 ES module、无构建；ES2017 以内写法（无可选链）。
 *   - 样式自注入 <style id="ps-health-style">，类名前缀 .psh- 避免冲突；颜色全部
 *     走页面 CSS 变量（--card/--bd/--t1/--t2/--green/--red 等），主题自适应。
 *   - 页面全局（T/_allProfiles/_faceRefs/_profileCompleteness/_profileColor/
 *     editProfile/PSApply）一律防御式引用，缺失时优雅降级：
 *       · _profileCompleteness 未定义或抛错（迁移期）→ 内置同权重兜底公式：
 *         名字25 + 角色20 + 性格20（优先 has_personality）+ 标签15
 *         + 声音10（has_voice）+ 头像10（_faceRefs 有 url）
 *       · PSApply 未加载 → 「去应用」回落 editProfile
 *   - 面板为 <details> 可折叠卡片：有问题项默认展开、全部健康默认折叠；
 *     用户手动开合后，后续幂等重渲染保留用户选择。
 *   - 所有插入 DOM 的动态文本一律经 _esc 转义。
 *
 * 对外 API：
 *   window.PSHealth.render(containerId)
 *     —— 渲染到宿主容器（幂等：重复调用整体重建，容器级事件只绑一次）
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  var STYLE_ID = 'ps-health-style';

  /* ── 折叠状态：用户手动开合后，优先于"有问题展开/健康折叠"的默认规则 ──── */
  var _userToggled = false;
  var _userOpen = false;

  /* ── 小工具 ─────────────────────────────────────────────────────────────── */
  function _t(key, fb) {
    if (typeof window.T === 'function') return window.T(key, fb);
    return fb;
  }

  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  // summary 池：过滤掉空洞元素，保证后续遍历安全
  function _profiles() {
    var list = window._allProfiles;
    if (!list || typeof list.length !== 'number') return [];
    var out = [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && typeof list[i] === 'object') out.push(list[i]);
    }
    return out;
  }

  function _faceUrl(pid) {
    var refs = window._faceRefs || {};
    var fr = refs[pid];
    return (fr && fr.url) ? String(fr.url) : '';
  }

  /* ── 完善度：单项判定（summary 行与完整 persona 双兼容）────────────────── */
  function _hasName(p) { return !!(p.name && String(p.name).trim()); }
  function _hasRole(p) { return !!(p.role && String(p.role).trim()); }
  function _hasPersonality(p) {
    if (p.has_personality !== undefined) return !!p.has_personality;
    var pers = p.personality;
    if (!pers) return false;
    if (typeof pers === 'string') return !!pers.trim();
    for (var k in pers) {
      if (Object.prototype.hasOwnProperty.call(pers, k)) return true;
    }
    return false;
  }
  function _hasTags(p) { return !!(p.tags && p.tags.length > 0); }
  function _hasVoice(p) {
    if (p.has_voice !== undefined) return !!p.has_voice;
    var vp = p.voice_profile || {};
    return !!(vp.enabled || vp.voice || vp.backend);
  }
  function _hasAvatar(p) { return !!_faceUrl(String(p.id == null ? '' : p.id)); }

  // 兜底公式（与页面 _profileCompleteness 同权重）
  function _fallbackScore(p) {
    var s = 0;
    if (_hasName(p)) s += 25;
    if (_hasRole(p)) s += 20;
    if (_hasPersonality(p)) s += 20;
    if (_hasTags(p)) s += 15;
    if (_hasVoice(p)) s += 10;
    if (_hasAvatar(p)) s += 10;
    return Math.min(100, s);
  }

  // 优先页面 _profileCompleteness（迁移期可能未定义/抛错/返回非数字 → 兜底）
  function _score(p) {
    if (typeof window._profileCompleteness === 'function') {
      try {
        var v = Number(window._profileCompleteness(p));
        if (isFinite(v)) return Math.max(0, Math.min(100, v));
      } catch (e) { /* 落兜底 */ }
    }
    return _fallbackScore(p);
  }

  // 缺失项短词列表（资料不全组的行说明）
  function _missing(p) {
    var out = [];
    if (!_hasRole(p)) out.push(_t('psn_hp_miss_role', '缺角色'));
    if (!_hasPersonality(p)) out.push(_t('psn_hp_miss_personality', '缺性格'));
    if (!_hasTags(p)) out.push(_t('psn_hp_miss_tags', '缺标签'));
    if (!_hasVoice(p)) out.push(_t('psn_hp_miss_voice', '缺声音'));
    if (!_hasAvatar(p)) out.push(_t('psn_hp_miss_avatar', '缺头像'));
    return out;
  }

  /* ── 「超过 30 天未更新」行判定（组启用条件见 _analyze）─────────────────── */
  var STALE_MS = 30 * 24 * 3600 * 1000;
  function _isStale(p, now) {
    var raw = p.last_edited_at;
    if (!raw) return false;          // 缺字段 / 空串（从未编辑）不算超期
    var ts = Date.parse(raw);
    if (isNaN(ts)) return false;     // 解析失败的行跳过，不误报
    return now - ts > STALE_MS;
  }

  /* ── 五组体检分析 ───────────────────────────────────────────────────────── */
  function _analyze() {
    var list = _profiles();
    var i, p;

    // 1) 资料不全：完善度 < 90，分数低的排前面
    var incomplete = [];
    for (i = 0; i < list.length; i++) {
      p = list[i];
      var sc = _score(p);
      if (sc < 90) incomplete.push({ p: p, score: sc, miss: _missing(p) });
    }
    incomplete.sort(function (a, b) { return a.score - b.score; });

    // 2) 未启用：零绑定（binding_count 缺失按 0 处理，同页面 unbound 过滤器）
    var unbound = [];
    for (i = 0; i < list.length; i++) {
      if ((Number(list[i].binding_count) || 0) === 0) unbound.push(list[i]);
    }

    // 3) 已绑定但近 7 日零回复：
    //    先判定 usage 数据是否真实存在——至少一行带 usage_7d 字段且全池总和 > 0；
    //    否则视为"统计未上线/无积累"，整组不渲染，避免全员误报。
    var hasUsageField = false, usageSum = 0;
    for (i = 0; i < list.length; i++) {
      var u = list[i].usage_7d;
      if (u !== undefined && u !== null) {
        hasUsageField = true;
        usageSum += (Number(u) || 0);
      }
    }
    var usageEnabled = hasUsageField && usageSum > 0;
    var idle = [];
    if (usageEnabled) {
      for (i = 0; i < list.length; i++) {
        p = list[i];
        if ((Number(p.binding_count) || 0) > 0 && p.usage_7d === 0) idle.push(p);
      }
    }

    // 4) 超过 30 天未更新：last_edited_at 非空且距今超过 30 天（解析失败跳过）；
    //    仅当至少一行携带 last_edited_at 字段时才启用本组，后端未升级时整组
    //    不渲染，避免误报；最老的排前面
    var hasEditedField = false;
    for (i = 0; i < list.length; i++) {
      if (list[i].last_edited_at !== undefined) { hasEditedField = true; break; }
    }
    var stale = [];
    if (hasEditedField) {
      var now = Date.now();
      for (i = 0; i < list.length; i++) {
        p = list[i];
        if (_isStale(p, now)) stale.push({ p: p, ts: Date.parse(p.last_edited_at) });
      }
      stale.sort(function (a, b) { return a.ts - b.ts; });
    }

    // 5) 重名冲突：trim 后同名 ≥ 2（空名不算重名，由"资料不全"覆盖）
    var byName = Object.create(null);
    for (i = 0; i < list.length; i++) {
      var nm = String(list[i].name == null ? '' : list[i].name).trim();
      if (!nm) continue;
      if (!byName[nm]) byName[nm] = [];
      byName[nm].push(list[i]);
    }
    var dups = [];
    for (var key in byName) {
      if (byName[key].length >= 2) dups.push({ name: key, rows: byName[key] });
    }
    dups.sort(function (a, b) { return b.rows.length - a.rows.length; });

    return { incomplete: incomplete, unbound: unbound, idle: idle, stale: stale, dups: dups };
  }

  /* ── 样式（一次注入，.psh- 前缀，颜色走页面 CSS 变量）───────────────────── */
  var CSS = ''
    + '.psh-card{display:block;background:var(--card);border:1px solid var(--bd);border-radius:12px;margin-bottom:1rem;overflow:hidden}'
    + '.psh-sum{display:flex;align-items:center;gap:.5rem;padding:.7rem .95rem;cursor:pointer;user-select:none;list-style:none;outline:none}'
    + '.psh-sum::-webkit-details-marker{display:none}'
    + '.psh-sum:hover{background:var(--bg2,var(--bg))}'
    + '.psh-chev{flex-shrink:0;font-size:.68rem;color:var(--t2);transition:transform .18s}'
    + '.psh-card[open] .psh-chev{transform:rotate(90deg)}'
    + '.psh-ic{font-size:1rem;flex-shrink:0;line-height:1}'
    + '.psh-ttl{flex-shrink:0;font-size:.88rem;font-weight:700;color:var(--t1)}'
    + '.psh-subttl{flex:1;min-width:0;font-size:.68rem;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.psh-badges{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:.3rem;flex-shrink:0}'
    + '.psh-badge{font-size:.64rem;font-weight:600;padding:.1rem .45rem;border-radius:99px;border:1px solid var(--bd);background:var(--bg2,var(--bg));color:var(--t2);white-space:nowrap}'
    + '.psh-badge b{margin-left:.18rem;font-weight:700;color:var(--red)}'
    + '.psh-badge.psh-ok{color:var(--green);border-color:rgba(16,185,129,.3);background:rgba(16,185,129,.08)}'
    + '.psh-body{border-top:1px solid var(--bd);padding:.3rem .95rem .75rem;max-height:340px;overflow-y:auto}'
    + '.psh-sec{margin-top:.6rem}'
    + '.psh-sec-hd{display:flex;align-items:center;gap:.4rem;font-size:.72rem;font-weight:700;color:var(--t2);margin-bottom:.1rem}'
    + '.psh-cnt{font-size:.64rem;font-weight:700;color:var(--red)}'
    + '.psh-row{display:flex;align-items:center;gap:.55rem;padding:.4rem 0;border-bottom:1px dashed var(--bd)}'
    + '.psh-row:last-child{border-bottom:none}'
    + '.psh-av{position:relative;width:26px;height:26px;border-radius:50%;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-size:.72rem;font-weight:700;color:#fff;overflow:hidden;background:#6366f1}'
    + '.psh-av img{position:absolute;top:0;left:0;width:100%;height:100%;object-fit:cover}'
    + '.psh-name{flex-shrink:0;max-width:9.5rem;font-size:.78rem;font-weight:600;color:var(--t1);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.psh-note{flex:1;min-width:0;font-size:.68rem;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}'
    + '.psh-ids{flex:1;min-width:0;display:flex;flex-wrap:wrap;gap:.25rem}'
    + '.psh-btn{flex-shrink:0;font-size:.68rem;font-weight:600;padding:.22rem .6rem;border-radius:7px;border:1px solid var(--bd);background:transparent;color:var(--t2);cursor:pointer;font-family:inherit;transition:all .15s}'
    + '.psh-btn:hover{color:var(--t1);border-color:var(--bd2,var(--bd));background:var(--bg2,var(--bg))}'
    + '.psh-idbtn{font-size:.64rem;font-family:ui-monospace,SFMono-Regular,Consolas,"Courier New",monospace;padding:.14rem .4rem;border-radius:5px;border:1px solid var(--bd);background:var(--bg2,var(--bg));color:var(--t2);cursor:pointer;max-width:11rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;transition:all .15s}'
    + '.psh-idbtn:hover{color:var(--t1);border-color:var(--bd2,var(--bd))}'
    + '.psh-good{padding:.55rem 0 .25rem;font-size:.78rem;font-weight:600;color:var(--green)}';

  function _injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var st = document.createElement('style');
    st.id = STYLE_ID;
    st.textContent = CSS;
    document.head.appendChild(st);
  }

  /* ── 行内 HTML 片段 ─────────────────────────────────────────────────────── */

  // 头像：优先 _faceRefs 基准照，回落首字母色块（img 加载失败自移除露出首字母）
  function _avatarHTML(p) {
    var pid = String(p.id == null ? '' : p.id);
    var color = '#6366f1';
    if (typeof window._profileColor === 'function') {
      try { color = window._profileColor(pid) || color; } catch (e) { /* 保底色 */ }
    }
    var initial = String((p.name && String(p.name).trim()) || pid || '?').charAt(0).toUpperCase();
    var url = _faceUrl(pid);
    var img = url
      ? '<img src="' + _esc(url) + '" alt="" loading="lazy" onerror="this.remove()">'
      : '';
    return '<div class="psh-av" style="background:' + _esc(color) + '">' + _esc(initial) + img + '</div>';
  }

  // 普通行：头像 + 名字 + 补充说明 + 单操作按钮
  function _rowHTML(p, note, btnLabel, act) {
    var pid = String(p.id == null ? '' : p.id);
    return '<div class="psh-row">'
      + _avatarHTML(p)
      + '<span class="psh-name" title="' + _esc(pid) + '">' + _esc(p.name || pid || '—') + '</span>'
      + '<span class="psh-note">' + _esc(note) + '</span>'
      + '<button type="button" class="psh-btn" data-act="' + act + '" data-pid="' + _esc(pid) + '">' + _esc(btnLabel) + '</button>'
      + '</div>';
  }

  // 重名组行：头像（取组内首个）+ 名字×N + 逐 id 等宽小按钮 → editProfile
  function _dupRowHTML(g) {
    var chips = '';
    for (var i = 0; i < g.rows.length; i++) {
      var pid = String(g.rows[i].id == null ? '' : g.rows[i].id);
      chips += '<button type="button" class="psh-idbtn" data-act="edit" data-pid="' + _esc(pid) + '"'
        + ' title="' + _esc(_t('psn_hp_fix', '处理') + ' · ' + pid) + '">' + _esc(pid) + '</button>';
    }
    return '<div class="psh-row">'
      + _avatarHTML(g.rows[0] || {})
      + '<span class="psh-name" title="' + _esc(g.name) + '">' + _esc(g.name) + ' × ' + g.rows.length + '</span>'
      + '<div class="psh-ids">' + chips + '</div>'
      + '</div>';
  }

  function _secHTML(title, count, rowsHtml) {
    return '<div class="psh-sec">'
      + '<div class="psh-sec-hd">' + _esc(title) + '<span class="psh-cnt">' + count + '</span></div>'
      + rowsHtml
      + '</div>';
  }

  /* ── 点击代理：处理 / 去应用（PSApply 缺失回落 editProfile）────────────── */
  function _edit(pid) {
    if (typeof window.editProfile === 'function') window.editProfile(pid);
  }

  function _onClick(e) {
    var btn = e.target && e.target.closest ? e.target.closest('[data-act]') : null;
    if (!btn) return;
    var act = btn.getAttribute('data-act');
    var pid = btn.getAttribute('data-pid') || '';
    if (!pid) return;
    if (act === 'edit') {
      _edit(pid);
    } else if (act === 'apply') {
      if (window.PSApply && typeof window.PSApply.open === 'function') window.PSApply.open(pid);
      else _edit(pid);
    }
  }

  /* ── 主渲染（幂等：整体重建容器内容；容器级点击代理只绑一次）──────────── */
  function render(containerId) {
    var box = document.getElementById(String(containerId == null ? '' : containerId));
    if (!box) return;
    _injectStyle();

    var r = _analyze();
    var issueCount = r.incomplete.length + r.unbound.length + r.idle.length + r.stale.length + r.dups.length;
    var open = _userToggled ? _userOpen : issueCount > 0;

    // 头部徽计数：组空不显示；全部健康 → 绿色徽（折叠时也能一眼看到结论）
    var badges = '';
    if (r.incomplete.length) {
      badges += '<span class="psh-badge">' + _esc(_t('psn_hp_incomplete', '资料不全')) + '<b>' + r.incomplete.length + '</b></span>';
    }
    if (r.unbound.length) {
      badges += '<span class="psh-badge">' + _esc(_t('psn_hp_unbound', '未启用（零绑定）')) + '<b>' + r.unbound.length + '</b></span>';
    }
    if (r.idle.length) {
      badges += '<span class="psh-badge">' + _esc(_t('psn_hp_idle', '已绑定但近 7 日零回复')) + '<b>' + r.idle.length + '</b></span>';
    }
    if (r.stale.length) {
      badges += '<span class="psh-badge">' + _esc(_t('psn_hp_stale', '超过 30 天未更新')) + '<b>' + r.stale.length + '</b></span>';
    }
    if (r.dups.length) {
      badges += '<span class="psh-badge">' + _esc(_t('psn_hp_dup', '重名冲突')) + '<b>' + r.dups.length + '</b></span>';
    }
    if (!issueCount) {
      badges = '<span class="psh-badge psh-ok">' + _esc(_t('psn_hp_all_good', '全部健康，无需处理 🎉')) + '</span>';
    }

    // 五个分组小节（组空整组隐藏）
    var body = '';
    var i;
    if (r.incomplete.length) {
      var rows1 = '';
      for (i = 0; i < r.incomplete.length; i++) {
        var it = r.incomplete[i];
        // 说明 = 缺失项短词逗号连接；极端情况（仅缺名字）无短词可列时退回显示分数
        var note1 = it.miss.length ? it.miss.join(', ') : (it.score + '%');
        rows1 += _rowHTML(it.p, note1, _t('psn_hp_fix', '处理'), 'edit');
      }
      body += _secHTML(_t('psn_hp_incomplete', '资料不全'), r.incomplete.length, rows1);
    }
    if (r.unbound.length) {
      var rows2 = '';
      for (i = 0; i < r.unbound.length; i++) {
        var p2 = r.unbound[i];
        rows2 += _rowHTML(p2, p2.role || p2.source || '', _t('psn_hp_apply', '去应用'), 'apply');
      }
      body += _secHTML(_t('psn_hp_unbound', '未启用（零绑定）'), r.unbound.length, rows2);
    }
    if (r.idle.length) {
      var rows3 = '';
      for (i = 0; i < r.idle.length; i++) {
        var p3 = r.idle[i];
        rows3 += _rowHTML(p3, '● ' + (Number(p3.binding_count) || 0) + ' · 7d 0', _t('psn_hp_fix', '处理'), 'edit');
      }
      body += _secHTML(_t('psn_hp_idle', '已绑定但近 7 日零回复'), r.idle.length, rows3);
    }
    if (r.stale.length) {
      var rows4 = '';
      for (i = 0; i < r.stale.length; i++) {
        var st = r.stale[i];
        var note4 = _t('psn_hp_last_edit', '最后编辑') + ' ' + String(st.p.last_edited_at).slice(0, 10);
        rows4 += _rowHTML(st.p, note4, _t('psn_hp_fix', '处理'), 'edit');
      }
      body += _secHTML(_t('psn_hp_stale', '超过 30 天未更新'), r.stale.length, rows4);
    }
    if (r.dups.length) {
      var rows5 = '';
      for (i = 0; i < r.dups.length; i++) rows5 += _dupRowHTML(r.dups[i]);
      body += _secHTML(_t('psn_hp_dup', '重名冲突'), r.dups.length, rows5);
    }
    if (!issueCount) {
      body = '<div class="psh-good">' + _esc(_t('psn_hp_all_good', '全部健康，无需处理 🎉')) + '</div>';
    }

    box.innerHTML = ''
      + '<details class="psh-card"' + (open ? ' open' : '') + '>'
      +   '<summary class="psh-sum">'
      +     '<span class="psh-chev">▸</span>'
      +     '<span class="psh-ic">🩺</span>'
      +     '<span class="psh-ttl">' + _esc(_t('psn_hp_title', '人设体检')) + '</span>'
      +     '<span class="psh-subttl">' + _esc(_t('psn_hp_sub', '需要处理的人设清单，按问题分组')) + '</span>'
      +     '<span class="psh-badges">' + badges + '</span>'
      +   '</summary>'
      +   '<div class="psh-body">' + body + '</div>'
      + '</details>';

    // 记住用户手动开合（click 触发时 open 尚未翻转，取反即点击后的状态）
    var det = box.querySelector('details.psh-card');
    var sum = det ? det.querySelector('summary.psh-sum') : null;
    if (det && sum) {
      sum.addEventListener('click', function () {
        _userToggled = true;
        _userOpen = !det.open;
      });
    }

    // 容器级点击代理只绑一次（容器由宿主长期持有；重渲染只换内部 DOM）
    if (!box._pshBound) {
      box._pshBound = true;
      box.addEventListener('click', _onClick);
    }
  }

  /* ── 对外 API ───────────────────────────────────────────────────────────── */
  window.PSHealth = { render: render };
})();
