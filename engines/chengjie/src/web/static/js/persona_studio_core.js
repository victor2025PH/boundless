// ── 人设工作室核心模块(P2 工程底座:自 personas.html 迁出)────────────────────
// 资料完善度打分 / 上线就绪清单弹层 / 卡片语音试听。
// 依赖宿主页全局:_allProfiles、_faceRefs、_editingProfileId(getter 桥)、_fmt、_toast、
// editProfile、switchDTab、window.T;本文件先于其他 persona_*.js 加载。
// ── P12-3 → P0 改版: 资料完善度（绑定拆出为独立"服务状态"，头像计入 10%）──────
// 兼容两种入参：summary 行（has_voice/has_personality）与完整 persona（voice_profile/personality）
function _pHasVoice(p) {
  if (p.has_voice !== undefined) return !!p.has_voice;
  var vp = p.voice_profile || {};
  return !!(vp.enabled || vp.voice || vp.backend);
}
function _pHasPersonality(p) {
  if (p.has_personality !== undefined) return !!p.has_personality;
  var pers = p.personality;
  return !!(pers && (typeof pers === 'string' ? pers.trim() : Object.keys(pers || {}).length > 0));
}
function _pHasFaceRef(p) {
  var pid = p.id || _editingProfileId || '';
  return !!(_faceRefs[pid] && _faceRefs[pid].url);
}
function _profileCompleteness(p) {
  var s = 0;
  if (p.name && String(p.name).trim()) s += 25;
  if (p.role && String(p.role).trim()) s += 20;
  if (_pHasPersonality(p)) s += 20;
  if (p.tags && p.tags.length > 0) s += 15;
  if (_pHasVoice(p)) s += 10;
  if (_pHasFaceRef(p)) s += 10;
  return Math.min(100, s);
}
function _completenessColor(score) {
  if (score >= 90) return '#10b981';
  if (score >= 70) return '#84cc16';
  if (score >= 50) return '#f59e0b';
  return '#ef4444';
}
// ── P0 改版: 上线就绪清单弹层 ────────────────────────────────────────────────
// 每项: {label, done, pts, go: 点"去补全"后的动作}
function _readinessItems(p) {
  var pid = p.id;
  function _goField(fld) { return function() { editProfile(pid); setTimeout(function(){ var el = document.getElementById(fld); if (el) el.focus(); }, 420); }; }
  function _goTab(tab) { return function() { editProfile(pid); setTimeout(function(){ switchDTab(tab); }, 420); }; }
  return [
    {label: window.T('psn_ready_name', '名字'),          done: !!(p.name && String(p.name).trim()), pts: 25, go: _goField('pe-name')},
    {label: window.T('psn_ready_role', '角色定位'),      done: !!(p.role && String(p.role).trim()), pts: 20, go: _goField('pe-role')},
    {label: window.T('psn_ready_personality', '性格设定'), done: _pHasPersonality(p),               pts: 20, go: _goField('pe-personality')},
    {label: window.T('psn_ready_tags', '标签'),          done: !!(p.tags && p.tags.length),         pts: 15, go: _goField('pe-tags')},
    {label: window.T('psn_ready_voice', '声音'),         done: _pHasVoice(p),                       pts: 10, go: _goTab('voice')},
    {label: window.T('psn_ready_avatar', '头像（锁脸照）'), done: _pHasFaceRef(p),                  pts: 10, go: _goTab('album')},
  ];
}
var _readyPopEl = null;
function _closeReadyPop() {
  if (_readyPopEl) { _readyPopEl.remove(); _readyPopEl = null; }
  document.removeEventListener('mousedown', _readyPopOutside, true);
  document.removeEventListener('keydown', _readyPopEsc, true);
}
function _readyPopOutside(e) { if (_readyPopEl && !_readyPopEl.contains(e.target)) _closeReadyPop(); }
function _readyPopEsc(e) { if (e.key === 'Escape') _closeReadyPop(); }
function _openReadyPop(ev, pid) {
  _closeReadyPop();
  var p = _allProfiles.find(function(x){ return x.id === pid; });
  if (!p) return;
  var items = _readinessItems(p);
  var score = _profileCompleteness(p);
  var pop = document.createElement('div');
  pop.className = 'ready-pop';
  var rows = items.map(function(it, i) {
    return '<div class="ready-item ' + (it.done ? 'done' : 'miss') + '">'
      + '<span class="ri-ic">' + (it.done ? '✓' : '✕') + '</span>'
      + '<span>' + it.label + '</span>'
      + '<span style="font-size:.62rem;color:var(--t2)">' + it.pts + '%</span>'
      + (it.done ? '' : '<span class="ri-fix" data-ri="' + i + '">' + window.T('psn_ready_fix', '去补全') + '</span>')
      + '</div>';
  }).join('');
  // 服务状态独立一行（不计分）：未启用给"前往绑定"入口
  var liveRow = (p.binding_count || 0) > 0
    ? '<div class="ready-item done" style="border-top:1px solid var(--bd);margin-top:.3rem;padding-top:.34rem"><span class="ri-ic">✓</span><span>' + window.T('psn_ready_binding', '应用到会话 / 账号') + '</span><span style="font-size:.62rem;color:var(--green)">● ' + p.binding_count + '</span></div>'
    : '<div class="ready-item miss" style="border-top:1px solid var(--bd);margin-top:.3rem;padding-top:.34rem"><span class="ri-ic">○</span><span>' + window.T('psn_ready_binding', '应用到会话 / 账号') + '</span><span class="ri-fix" data-ri="bind">' + window.T('psn_ready_fix', '去补全') + '</span></div>';
  pop.innerHTML = '<div class="ready-pop-hd"><span>' + window.T('psn_ready_title', '上线就绪清单') + '</span>'
    + '<span class="ready-pop-pct" style="color:' + _completenessColor(score) + '">' + score + '%</span></div>'
    + (score >= 100 ? '<div class="ready-all-done">' + window.T('psn_ready_all_done', '资料齐全，可以上线 🎉') + '</div>' : '')
    + rows + liveRow;
  document.body.appendChild(pop);
  // 定位：优先显示在触发点右下，越界时收回视口内
  var x = ev.clientX + 8, y = ev.clientY + 8;
  pop.style.left = '0px'; pop.style.top = '0px';
  var rect = pop.getBoundingClientRect();
  if (x + rect.width > window.innerWidth - 12) x = window.innerWidth - rect.width - 12;
  if (y + rect.height > window.innerHeight - 12) y = window.innerHeight - rect.height - 12;
  pop.style.left = x + 'px'; pop.style.top = y + 'px';
  pop.addEventListener('click', function(e) {
    var fx = e.target.closest('.ri-fix');
    if (!fx) return;
    e.stopPropagation();
    _closeReadyPop();
    if (fx.dataset.ri === 'bind') {
      // P1：优先走统一「应用到…」弹窗;模块未加载回落抽屉预览页
      if (window.PSApply && PSApply.open) { PSApply.open(pid); }
      else { editProfile(pid); setTimeout(function(){ switchDTab('preview'); }, 420); }
      return;
    }
    var it = _readinessItems(p)[parseInt(fx.dataset.ri, 10)];
    if (it && it.go) it.go();
  });
  _readyPopEl = pop;
  setTimeout(function() {
    document.addEventListener('mousedown', _readyPopOutside, true);
    document.addEventListener('keydown', _readyPopEsc, true);
  }, 0);
}
// ── P0 改版: 卡片语音试听（复用 /api/voice/tts-test，同一时刻只播一段）─────────
var _vpAudio = null, _vpBtn = null;
function _vpReset() {
  if (_vpAudio) { try { _vpAudio.pause(); } catch(e) {} _vpAudio = null; }
  if (_vpBtn) { _vpBtn.classList.remove('playing', 'loading'); _vpBtn.textContent = '▶'; _vpBtn = null; }
}
async function _playVoicePreview(pid, btn) {
  if (_vpBtn === btn && _vpAudio) { _vpReset(); return; }   // 再点=停止
  _vpReset();
  _vpBtn = btn;
  var p = _allProfiles.find(function(x){ return x.id === pid; });
  var text = _fmt(window.T('psn_voice_preview_text', '你好呀，我是{name}，很高兴认识你！'), {name: (p && p.name) || pid});
  btn.classList.add('loading'); btn.textContent = '…';
  try {
    const r = await fetch('/api/voice/tts-test', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({text: text, persona_id: pid}),
    });
    const d = await r.json().catch(function(){ return {}; });
    if (!r.ok || !d.ok || !(d.url || d.audio_url)) throw new Error(d.error || d.detail || ('HTTP ' + r.status));
    if (_vpBtn !== btn) return;   // 期间用户点了别的卡片
    btn.classList.remove('loading'); btn.classList.add('playing'); btn.textContent = '■';
    _vpAudio = new Audio(d.url || d.audio_url);
    _vpAudio.onended = _vpAudio.onerror = function() { if (_vpBtn === btn) _vpReset(); };
    _vpAudio.play().catch(function() { if (_vpBtn === btn) _vpReset(); });
  } catch(e) {
    if (_vpBtn === btn) { btn.classList.remove('loading'); btn.textContent = '▶'; _vpBtn = null; }
    _toast(_fmt(window.T('psn_voice_preview_fail', '试听失败：{err}'), {err: (e && e.message) || e}), 'err');
  }
}

// ── P2 续迁(第五批):标签云 / 卡片渲染器 / 搜索筛选排序 ─────────────────────
// 自 personas.html 迁出;依赖宿主页全局(词法或 window):_allProfiles、_currentQ、
// _activeTag、_sfFilter、_healthTierFilter、_customOrder、_selectedProfiles、_bulkMode、
// _platBindMap、_filteredProfiles、_SRC、_hl、_fmt、_faceRefs、各 DnD/菜单/提示回调。
// 顶层 let/const 跨 <script> 词法共享;本文件在内联脚本之后、DOMContentLoaded 之前加载。
function renderTagCloud(profiles) {
  const cloud = document.getElementById('tag-cloud');
  if (!cloud) return;
  const tmap = {};
  profiles.forEach(function(p) { (p.tags || []).forEach(function(t) { tmap[t] = (tmap[t] || 0) + 1; }); });
  cloud.innerHTML = Object.entries(tmap).sort(function(a,b){ return b[1]-a[1]; }).map(function(e) {
    var t = e[0], c = e[1];
    var tSafe = t.replace(/'/g, "\\'");
    return '<span class="tc-chip' + (_activeTag === t ? ' active' : '') + '" data-tag="' + t + '" onclick="_clickTagText(\'' + tSafe + '\')">' + t + ' <span style="opacity:.65">' + c + '</span></span>';
  }).join('');
}

function _clickTagText(tag) {
  var inp = document.getElementById('profile-search');
  _activeTag = (_activeTag === tag) ? '' : tag;
  if (inp) inp.value = '';
  _applyFilter();
}

// ── Profile list renderer ─────────────────────────────────────────────────────
function renderProfileList(profiles) {
  var list = document.getElementById('profile-list');
  if (!list) return;
  var cnt = document.getElementById('profile-count');
  if (cnt) cnt.textContent = profiles.length + window.T('psn_js_102');
  var _tpc = document.getElementById('tb-profiles-cnt');
  if (_tpc) _tpc.textContent = _allProfiles.length || '';
  var hs = document.getElementById('hs-profiles');
  if (hs) hs.textContent = _allProfiles.length;
  var _q = _currentQ;
  // P0 改版：重名检测 — 全池同名计数（不受筛选影响，重名即警示）
  var _nameCnt = {};
  _allProfiles.forEach(function(pp) {
    var nm = String(pp.name || '').trim();
    if (nm) _nameCnt[nm] = (_nameCnt[nm] || 0) + 1;
  });
  list.innerHTML = profiles.map(function(p) {
    var color = _profileColor(p.id);
    var initial = _profileInitial(p.name);
    var srcInfo = _SRC[p.source] || ['src-config', p.source || '?', ''];
    var srcBadge = '<span class="src-badge ' + srcInfo[0] + '" title="' + (srcInfo[2] || '').replace(/"/g, '&quot;') + '">' + srcInfo[1] + '</span>';
    // 服务状态胶囊：绑定数>0 = 服务中，否则灰色未启用（不再计入完善度）
    var livePill = p.binding_count > 0
      ? '<span class="live-pill on">● ' + window.T('psn_serving') + ' ' + p.binding_count + '</span>'
      : '<span class="live-pill off" title="' + window.T('psn_not_live_t') + '">○ ' + window.T('psn_not_live') + '</span>';
    // P2：近7日活跃火苗(usage_7d 来自后端 summary,无数据不显示)
    var usagePill = (p.usage_7d || 0) > 0
      ? '<span class="usage-pill" title="' + window.T('psn_usage_pill_t') + '">🔥 ' + (p.usage_7d > 999 ? '999+' : p.usage_7d) + '</span>'
      : '';
    // 试听按钮：有声音配置可点播人设开场白
    var voiceBadge = p.has_voice
      ? '<button class="voice-play" data-pid="' + p.id + '" onclick="event.stopPropagation();_playVoicePreview(\'' + p.id.replace(/'/g, "\\'") + '\', this)" title="' + window.T('psn_voice_preview_t') + '">▶</button>'
      : '';
    var promoteBtn = p.is_mrpa_source
      ? '<button class="btn btn-sm" data-pid="' + p.id + window.T('psn_js_104') : '';
    var dName = _q ? _hl(p.name || p.id, _q) : (p.name || p.id);
    var dRole = _q ? _hl(p.role || '', _q) : (p.role || '');
    var pidSafe = p.id.replace(/'/g, "\\'");
    // 重名警示角标
    var nmKey = String(p.name || '').trim();
    var dupBadge = (nmKey && _nameCnt[nmKey] > 1)
      ? '<span class="dup-badge" title="' + _fmt(window.T('psn_dup_name_t'), {n: _nameCnt[nmKey], name: nmKey}).replace(/"/g, '&quot;') + '">⚠ ' + window.T('psn_dup_name') + '</span>'
      : '';
    var tagHtml = '';
    if (_q && p.tags && p.tags.length) {
      var matchedTags = p.tags.filter(function(t){ return t.toLowerCase().includes(_q); });
      if (matchedTags.length) {
        tagHtml = matchedTags.map(function(t){ return '<span class="tag" style="font-size:.61rem">' + _hl(t, _q) + '</span>'; }).join('');
      }
    }
    var tipText = '';
    if (p.personality) {
      var rawP = typeof p.personality === 'object' ? (p.personality.style || JSON.stringify(p.personality)) : String(p.personality);
      tipText = rawP.replace(/['"{}]/g,'').trim().slice(0, 80) + (rawP.length > 80 ? '…' : '');
    }
    var tipAttr = tipText ? ' data-tip="' + tipText.replace(/"/g,'&quot;').replace(/\n/g,' ') + '"' : '';
    // 头像：有锁脸基准照用照片，否则回落首字母色块
    var faceRef = _faceRefs[p.id];
    var avInner = faceRef && faceRef.url
      ? '<img src="' + faceRef.url + '" alt="' + window.T('psn_avatar_alt') + '" loading="lazy" onerror="this.parentNode.textContent=\'' + initial + '\'">'
      : initial;
    // 头像角落就绪环：按资料完善度着色，点击弹出就绪清单
    var compScore = _profileCompleteness(p);
    var ringColor = _completenessColor(compScore);
    var C = 2 * Math.PI * 7;   // r=7 圆周长
    var dash = (compScore / 100 * C).toFixed(1);
    var ring = '<span class="pcard-ring" onclick="event.stopPropagation();_openReadyPop(event, \'' + pidSafe + '\')"'
      + ' title="' + window.T('psn_ready_pct') + ' ' + compScore + '%">'
      + '<svg width="16" height="16" viewBox="0 0 18 18">'
      + '<circle cx="9" cy="9" r="7" fill="none" stroke="rgba(128,128,128,.22)" stroke-width="2.6"/>'
      + '<circle cx="9" cy="9" r="7" fill="none" stroke="' + ringColor + '" stroke-width="2.6" stroke-linecap="round"'
      + ' stroke-dasharray="' + dash + ' ' + C.toFixed(1) + '" transform="rotate(-90 9 9)"/>'
      + '</svg></span>';
    return '<div class="pcard' + (_q ? ' search-match' : '') + (_selectedProfiles && _selectedProfiles.has(p.id) ? ' selected' : '') + '"' + tipAttr
      + ' draggable="true" data-pid="' + pidSafe + '"'
      + ' ondragstart="_onCardDragStart(event,\'' + pidSafe + '\')"'
      + ' ondragover="_onCardDragOver(event)" ondragleave="_onCardDragLeave(event)"'
      + ' ondrop="_onCardDrop(event,\'' + pidSafe + '\')"'
      + ' oncontextmenu="_onPcardCtx(event,\'' + pidSafe + '\')" onmousedown="if(!_bulkMode)_setFocusCard(\'' + pidSafe + '\')"'
      + ' onmouseenter="_bulkMode ? undefined : _showPCardTip(event,this)"'
      + ' onmouseleave="_hidePCardTip()"'
      + ' onclick="_bulkMode ? _toggleCardSelect(\'' + pidSafe + '\', this) : editProfile(\'' + pidSafe + '\')">'
      + '<div class="pcard-check" onclick="event.stopPropagation();_toggleCardSelect(\'' + pidSafe + '\', this.closest(\'.pcard\'))"></div>'
      + '<div class="pcard-menu">'
      + '<button onclick="event.stopPropagation();editProfile(\'' + pidSafe + '\')">' + window.T('psn_js_355') + '</button>'
      + '<button class="btn-copy" onclick="event.stopPropagation();_copyProfileId(\'' + pidSafe + '\')" title="' + window.T('psn_js_356') + '">ID</button>'
      + '<button class="btn-clone" onclick="event.stopPropagation();_cloneProfile(\'' + pidSafe + '\')" title="' + window.T('psn_js_357') + '">' + window.T('psn_js_358') + '</button>'
      + promoteBtn + '</div>'
      + '<div class="pcard-top">'
      + '<div class="pcard-av-wrap"><div class="pcard-av" style="background:' + color + '">' + avInner + '</div>' + ring + '</div>'
      + '<div class="pcard-hd">'
      + '<div class="pcard-name"><span style="overflow:hidden;text-overflow:ellipsis">' + dName + '</span>' + dupBadge + '</div>'
      + '<div class="pcard-role">' + (dRole || '&nbsp;') + '</div>'
      + '</div></div>'
      + '<div class="pcard-foot">' + livePill + usagePill + srcBadge + voiceBadge + tagHtml
      + '<button class="pcard-tag-btn" onclick="event.stopPropagation();_openInlineTagEdit(\'' + pidSafe + '\',this)" title="\u5feb\u901f\u7f16\u8f91\u6807\u7b7e">\u270e</button></div>'
      + '</div>';
  }).join('') + '<button class="pcard-add" onclick="openCreateFlow()">'
    + window.T('psn_js_106');
}

// ── Search & filter ───────────────────────────────────────────────────────────
function _onSearchInput() {
  _activeTag = '';
  _healthTierFilter = null;
  _renderHealthOverview();
  _applyFilter();
}

function _applyFilter() {
  var q = (document.getElementById('profile-search') || {}).value || '';
  q = q.toLowerCase().trim();
  _currentQ = q;
  var cloud = document.getElementById('tag-cloud');
  if (cloud) cloud.querySelectorAll('.tc-chip').forEach(function(c) {
    c.classList.toggle('active', c.dataset.tag === _activeTag);
  });
  var filtered = _allProfiles.slice();
  if (q) {
    filtered = filtered.filter(function(p) {
      return (p.name || '').toLowerCase().includes(q) ||
             (p.role || '').toLowerCase().includes(q) ||
             (p.id || '').toLowerCase().includes(q) ||
             (p.tags || []).some(function(t){ return t.toLowerCase().includes(q); });
    });
  }
  if (_activeTag) filtered = filtered.filter(function(p){ return (p.tags || []).includes(_activeTag); });
  if (_healthTierFilter !== null) {
    var _tBounds = [[0,49],[50,74],[75,89],[90,100]][_healthTierFilter];
    filtered = filtered.filter(function(p){ var s=_profileCompleteness(p); return s>=_tBounds[0]&&s<=_tBounds[1]; });
  }
  // P19-4: source/platform filter
  if (_sfFilter) {
    if (_sfFilter === 'bound')   filtered = filtered.filter(function(p){ return (p.binding_count||0) > 0; });
    else if (_sfFilter === 'unbound') filtered = filtered.filter(function(p){ return (p.binding_count||0) === 0; });
    else if (_sfFilter === 'TG')    filtered = filtered.filter(function(p){ return _platBindMap.TG.has(p.id); });
    else if (_sfFilter === 'FB')    filtered = filtered.filter(function(p){ return _platBindMap.FB.has(p.id); });
    else if (_sfFilter === 'WA')    filtered = filtered.filter(function(p){ return _platBindMap.WA.has(p.id); });
    else if (_sfFilter === 'LINE')  filtered = filtered.filter(function(p){ return _platBindMap.LINE.has(p.id); });
    else if (_sfFilter === 'studio') filtered = filtered.filter(function(p){ return p.source === 'studio' || p.source === 'runtime'; });
    else if (_sfFilter === 'voice')  filtered = filtered.filter(function(p){ return !!p.has_voice; });
  }
  var sortEl = document.getElementById('profile-sort');
  var sort = sortEl ? sortEl.value : 'default';
  // 按人话化语义排序：已发布 → 已定制(studio/runtime) → 出厂 → 外部导入
  var _srcOrd = {canonical:0, studio:1, runtime:1, config:2, mrpa:3};
  if (sort === 'name') filtered.sort(function(a,b){ return (a.name||a.id).localeCompare(b.name||b.id); });
  else if (sort === 'bindings') filtered.sort(function(a,b){ return (b.binding_count||0)-(a.binding_count||0); });
  else if (sort === 'source') filtered.sort(function(a,b){ return ((_srcOrd[a.source]??5)-(_srcOrd[b.source]??5)); });
  else if ((sort === 'default' || sort === 'custom') && _customOrder.length) {
    // Sync order: remove stale, append new
    var knownSet = new Set(filtered.map(function(p){ return p.id; }));
    _customOrder = _customOrder.filter(function(id){ return knownSet.has(id); });
    filtered.forEach(function(p){ if (_customOrder.indexOf(p.id) === -1) _customOrder.push(p.id); });
    var _ordMap = {};
    _customOrder.forEach(function(id, i){ _ordMap[id] = i; });
    filtered.sort(function(a,b){ return (_ordMap[a.id]??99999) - (_ordMap[b.id]??99999); });
  }
  // Show/hide custom sort option
  var _custOpt = document.getElementById('sort-custom-opt');
  if (_custOpt) _custOpt.style.display = _customOrder.length ? '' : 'none';
  var _resetBtn = document.getElementById('sort-reset-btn');
  if (_resetBtn) _resetBtn.style.display = _customOrder.length ? '' : 'none';
  // P14-1: Health tier filter badge
  var _hfBadge = document.getElementById('health-filter-badge');
  if (_hfBadge) {
    if (_healthTierFilter !== null) {
      var _hfMeta = [{c:'#ef4444',l:window.T('psn_js_107')},{c:'#f59e0b',l:window.T('psn_js_108')},{c:'#84cc16',l:window.T('psn_js_109')},{c:'#10b981',l:window.T('psn_js_110')}][_healthTierFilter];
      _hfBadge.style.cssText = 'display:inline-flex;align-items:center;gap:.22rem;font-size:.7rem;padding:.14rem .42rem;border-radius:20px;background:'+_hfMeta.c+'1a;color:'+_hfMeta.c+';border:1px solid '+_hfMeta.c+'55;cursor:pointer;font-weight:600';
      _hfBadge.innerHTML = '&#9679;&nbsp;' + _hfMeta.l + '&nbsp;<b style="font-size:.85em">&#xD7;</b>';
      _hfBadge.onclick = function(){ _healthTierFilter=null; _renderHealthOverview(); _applyFilter(); };
    } else { _hfBadge.style.display='none'; }
  }
  _filteredProfiles = filtered.slice();
  renderProfileList(filtered);
}

function _clearSearch() {
  var inp = document.getElementById('profile-search');
  if (inp) inp.value = '';
  _activeTag = '';
  _applyFilter();
}

// ── P8 续迁(第八批):概览页渲染器 ─────────────────────────────────────────────
// _renderDashboard(hero 统计/语义色/未同步横幅 master 门控/平台绑定索引/活跃人设条/
// 账号卡片区)。依赖宿主页全局:__USER_ROLE、_platBindMap(赋值)、_updateSfChipCounts、
// _checkOnboarding、openPersonaPicker、window.T;由 refreshStatus/loadProfileList 在运行时调用。
// ── Dashboard renderer ────────────────────────────────────────────────────────
function _renderDashboard(d) {
  const pf = d.profiles || {};
  const bf = d.bindings || {};
  document.getElementById('hs-profiles').textContent  = pf.count != null ? pf.count : '—';
  document.getElementById('hs-bindings').textContent  = bf.count != null ? bf.count : (Object.keys(bf).length || '—');
  document.getElementById('hs-unsynced').textContent  = pf.unsynced != null ? pf.unsynced : '—';

  let platCount = 0;
  if (d.tg_accounts && d.tg_accounts.length)   platCount++;
  if (d.mrpa_accounts && d.mrpa_accounts.length) platCount++;
  if (d.wa_accounts && d.wa_accounts.length)   platCount++;
  if (d.line_rpa && d.line_rpa.length)         platCount++;
  var _allAccArr = [].concat(d.tg_accounts||[], d.mrpa_accounts||[], d.wa_accounts||[], d.line_rpa||[]);
  var _onlineAccCnt = _allAccArr.filter(function(a){ return a.status === 'active'; }).length;
  var _platEl = document.getElementById('hs-platforms');
  if (_platEl) {
    _platEl.innerHTML = (platCount || '—')
      + (_allAccArr.length ? '<span style="display:block;font-size:.56em;font-weight:400;margin-top:2px;opacity:.8">'
        + _onlineAccCnt + '/' + _allAccArr.length + window.T('psn_js_008') : '');
  }
  // Semantic color classes on hero-stat cards
  var _semMap = [
    {id:'hs-profiles', cls: (pf.count||0)>0 ? 's-ok' : ''},
    {id:'hs-bindings', cls: (bf.count||0)>0 ? 's-blue' : ''},
    {id:'hs-platforms', cls: platCount>0 ? '' : 's-warn'},
    {id:'hs-unsynced', cls: (pf.unsynced||0)>0 ? 's-warn' : 's-ok'},
  ];
  _semMap.forEach(function(m){
    var card = document.getElementById(m.id);
    if(!card) return;
    var hs = card.closest('.hero-stat');
    if(!hs) return;
    hs.className = 'hero-stat ' + m.cls;
  });

  _checkOnboarding(pf.count || 0);
  // P0 改版：未同步草稿是内部分层概念，只对 master 展示（横幅 + 概览格）
  const unsyncEl = document.getElementById('unsync-banner');
  const utextEl  = document.getElementById('unsync-text');
  const u = pf.unsynced || 0;
  const lastSync = d.last_sync_at ? window.wsFmtDateTime(d.last_sync_at) : window.T('psn_js_009');
  if (u > 0 && unsyncEl && __USER_ROLE === 'master') {
    unsyncEl.classList.add('show');
    if (utextEl) utextEl.textContent = u + window.T('psn_js_010') + lastSync + '）';
  } else if (unsyncEl) { unsyncEl.classList.remove('show'); }
  if (__USER_ROLE !== 'master') {
    var _uhs = document.getElementById('hs-unsynced');
    var _uhsCard = _uhs && _uhs.closest('.hero-stat');
    if (_uhsCard) _uhsCard.style.display = 'none';
  }

  // P20-1: Build platform binding index
  _platBindMap = {TG: new Set(), FB: new Set(), WA: new Set(), LINE: new Set()};
  (d.tg_accounts||[]).forEach(function(a){ (a.persona_ids||[]).forEach(function(pid){ _platBindMap.TG.add(pid); }); });
  (d.mrpa_accounts||[]).forEach(function(a){ (a.persona_ids||[]).forEach(function(pid){ _platBindMap.FB.add(pid); }); });
  (d.wa_accounts||[]).forEach(function(a){ (a.persona_ids||[]).forEach(function(pid){ _platBindMap.WA.add(pid); }); });
  (d.line_rpa||[]).forEach(function(a){ (a.persona_ids||[]).forEach(function(pid){ _platBindMap.LINE.add(pid); }); });
  _updateSfChipCounts();

  // P19-2: Active persona bar
  var _apsItems = [];
  function _apsCollect(accs, platCode) {
    if (!accs || !accs.length) return;
    accs.forEach(function(a) {
      var pid = (a.persona_ids && a.persona_ids[0]) || '';
      var pname = a.active_profile ? (a.active_profile.name || pid) : '';
      if (pname) _apsItems.push({plat: platCode, name: pname, pid: pid});
    });
  }
  _apsCollect(d.tg_accounts,   'TG');
  _apsCollect(d.mrpa_accounts, 'FB');
  _apsCollect(d.wa_accounts,   'WA');
  _apsCollect(d.line_rpa,      'LINE');
  var _apsBar = document.getElementById('aps-bar');
  if (_apsBar) {
    if (_apsItems.length) {
      _apsBar.className = 'aps-bar show';
      _apsBar.innerHTML = window.T('psn_js_011')
        + _apsItems.map(function(it) {
            return '<span class="aps-item" onclick="' + (it.pid ? 'editProfile(\'' + it.pid.replace(/'/g,"\\'") + '\')' : 'void 0') + '" title="' + it.name + window.T('psn_js_012')
              + '<span class="aps-plat">' + it.plat + '</span>'
              + '<span class="aps-name">' + it.name + '</span>'
              + '</span>';
          }).join('');
    } else {
      _apsBar.className = 'aps-bar';
    }
  }

  const wrap = document.getElementById('dashboard-platforms');
  if (!wrap) return;
  let html = '';

  function _renderAccSection(accounts, title, color, platform) {
    if (!accounts || !accounts.length) return '';
    var _sOnline = accounts.filter(function(a){ return a.status === 'active'; }).length;
    var _sClr = _sOnline === accounts.length ? '#10b981' : (_sOnline === 0 ? '#ef4444' : '#d97706');
    var _sHd = '<span style="font-size:.7rem;color:' + _sClr + ';font-weight:600;margin-left:.4rem">'
      + (_sOnline === accounts.length ? '✓' : '●') + '&nbsp;' + _sOnline + '/' + accounts.length + '</span>';
    var h = '<div class="plat-section"><div class="plat-hd">' + title + _sHd + '</div><div class="acc-grid">';
    accounts.forEach(function(a) {
      var hasP = !!a.active_profile;
      var pname = hasP ? (a.active_profile.name || a.active_profile.id) : null;
      var initials = (a.label || a.account_id || '?').slice(0,2).toUpperCase();
      var curPid = (a.persona_ids && a.persona_ids[0]) || '';
      var assignBtn = platform
        ? '<button class="btn btn-sm" style="font-size:.67rem;padding:.15rem .42rem' + (!hasP ? ';background:rgba(59,130,246,.1);border-color:rgba(59,130,246,.3);color:#3b82f6' : '') + '" onclick="openPersonaPicker(\'' + platform + '\',\'' + String(a.account_id).replace(/'/g,"\\'") + '\',\'' + String(curPid).replace(/'/g,"\\'") + '\')">' + (hasP ? window.T('psn_js_013') : window.T('psn_js_014')) + '</button>'
        : '';
      var statusClass = a.status === 'active' ? 'online' : (a.status === 'error' ? 'error' : 'offline');
      h += '<div class="acc-card' + (!hasP ? ' no-pf' : '') + '">';
      h += '<div class="acc-card-top">';
      h += '<div class="acc-avatar" style="background:' + color + '">' + initials + '</div>';
      h += '<div style="flex:1;min-width:0"><div class="acc-name">' + (a.label || a.account_id) + '</div><div class="acc-id">' + a.account_id + '</div></div>';
      h += '<span class="acc-sdot ' + statusClass + '" title="' + (a.status || 'unknown') + '"></span>';
      h += '</div>';
      h += '<div class="acc-bottom">';
      h += hasP ? '<span class="tag tag-green acc-p-name" style="font-size:.69rem">' + pname + '</span>' : window.T('psn_js_015');
      h += assignBtn;
      h += '</div></div>';
    });
    h += '</div></div>';
    return h;
  }

  html += _renderAccSection(d.tg_accounts,   window.T('psn_js_016'),     '#2CA5E0', 'tg');
  html += _renderAccSection(d.mrpa_accounts, window.T('psn_js_017'), '#0866FF', 'mrpa');
  html += _renderAccSection(d.wa_accounts,   window.T('psn_js_018'),  '#25D366', 'wa');
  html += _renderAccSection(d.line_rpa,      window.T('psn_js_019'),      '#00C300', '');

  if (!html) {
    html = window.T('psn_js_020');
  }
  wrap.innerHTML = html;
}
