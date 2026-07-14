/* platform-grid.js — 通用平台设备网格（复刻 TikTok 设备面板到所有平台）
   依赖: core.js (api, showToast, ALIAS, allDevices), platforms.js (PLAT_*, PLAT_PHASES)
*/

// ═══════════════════════════════════════════════════
// 平台专属配置：统计指标、操作按钮、卡片颜色
// ═══════════════════════════════════════════════════
const PLAT_GRID_CFG = {
  telegram: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['telegram_join_group','telegram_send_message','telegram_auto_reply'],
    accentColor: '#0088cc',
  },
  whatsapp: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['whatsapp_send_message','whatsapp_auto_reply','whatsapp_list_chats'],
    accentColor: '#25d366',
  },
  facebook: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['facebook_browse_feed','facebook_browse_feed_by_interest','facebook_search_leads','facebook_profile_hunt','facebook_add_friend','facebook_join_group','facebook_send_message'],
    accentColor: '#1877f2',
  },
  linkedin: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['linkedin_search_profile','linkedin_send_connection','linkedin_send_message'],
    accentColor: '#0a66c2',
  },
  instagram: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['instagram_browse_feed','instagram_search_leads','instagram_send_dm'],
    accentColor: '#e4405f',
  },
  twitter: {
    stats: [
      {icon:'📨',key:'today_tasks',label:'今日任务'},
      {icon:'✅',key:'today_completed',label:'完成'},
      {icon:'❌',key:'today_failed',label:'失败'},
    ],
    quickActions: ['twitter_browse_timeline','twitter_search_and_engage','twitter_send_dm'],
    accentColor: '#1d9bf0',
  },
};

// ═══════════════════════════════════════════════════
// 缓存 & 状态
// ═══════════════════════════════════════════════════
const _pgCache = {};   // platform -> {devices, summary, task_types}
let _pgLoading = {};
let _pgRefreshTimers = {};

// 暴露设备阶段查询给其他模块（如 facebook-ops.js 的 preset phase_gate 检查）
window._pgGetDevicePhase = function (platform, deviceId) {
  var devs = (_pgCache[platform] || {}).devices || [];
  var d = devs.find(function (x) { return x.device_id === deviceId; });
  return d ? (d.phase || '') : '';
};

// ═══════════════════════════════════════════════════
// 主入口：加载平台设备网格页面
// ═══════════════════════════════════════════════════
async function loadPlatGridPage(platform) {
  if (_pgLoading[platform]) return;
  _pgLoading[platform] = true;

  const container = document.querySelector(`#page-plat-${platform} .plat-page`);
  if (!container) { _pgLoading[platform] = false; return; }

  const color = PLAT_COLORS[platform] || '#60a5fa';
  const icon = PLAT_ICONS[platform] || '';
  const slogan = PLAT_SLOGANS[platform] || '';
  const cfg = PLAT_GRID_CFG[platform] || {};

  // 如果容器还没有网格结构，初始化骨架
  if (!container.querySelector('.pg-grid')) {
    container.innerHTML = _pgBuildSkeleton(platform, icon, slogan, color, cfg);
  }

  try {
    const [gridData] = await Promise.all([
      api('GET', `/platforms/${platform}/device-grid`),
      _fetchPlatTasks(platform),
    ]);
    _pgCache[platform] = gridData;

    // 更新汇总 pills
    const s = gridData.summary || {};
    _pgSetText(`pg-online-${platform}`, s.online || 0);
    _pgSetText(`pg-total-${platform}`, s.total || 0);
    _pgSetText(`pg-running-${platform}`, s.running_tasks || 0);

    // 渲染设备卡片
    _pgRenderGrid(platform, gridData.devices || []);

    // 渲染快捷操作（复用 platforms.js 的 PLAT_PHASES）
    _pgRenderQuickActions(platform, gridData.task_types || []);

    // 启动自动刷新
    _pgStartAutoRefresh(platform);

  } catch(e) {
    const grid = document.getElementById(`pg-device-grid-${platform}`);
    if (grid) grid.innerHTML = `<div style="grid-column:1/-1;text-align:center;color:#f87171;padding:24px;font-size:13px">${e.message||'加载失败'}</div>`;
  }
  _pgLoading[platform] = false;
}

// ═══════════════════════════════════════════════════
// 构建页面骨架 HTML
// ═══════════════════════════════════════════════════
function _pgBuildSkeleton(platform, icon, slogan, color, cfg) {
  const phases = PLAT_PHASES[platform] || [];
  return `
    <style>
      .pg-card{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;cursor:pointer;transition:all .15s;position:relative;border-left:3px solid transparent}
      .pg-card:hover{border-color:${color}44;transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.2)}
      .pg-card.state-online{border-left-color:#22c55e}
      .pg-card.state-busy{border-left-color:#3b82f6}
      .pg-card.state-idle{border-left-color:#94a3b8}
      .pg-card.state-offline{border-left-color:#ef4444;opacity:.65}
      .pg-card.failed-heavy{box-shadow:inset 0 0 0 1px #ef4444,0 0 14px rgba(239,68,68,.35)}
      .pg-stat-failed-clickable{cursor:pointer;transition:all .15s}
      .pg-stat-failed-clickable:hover{background:rgba(239,68,68,.12)!important;border-color:#ef4444!important;transform:translateY(-1px)}
      .pg-card-head{display:flex;align-items:center;gap:6px;margin-bottom:6px}
      .pg-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
      .pg-alias{font-size:12px;font-weight:600;color:var(--text)}
      .pg-node{font-size:9px;padding:1px 5px;border-radius:3px;font-weight:600}
      .pg-stat-row{display:flex;gap:8px;font-size:10px;color:var(--text-muted);margin-bottom:4px}
      .pg-stat-row span b{color:var(--text);margin-left:2px}
      .pg-task-tag{font-size:9px;padding:2px 6px;border-radius:4px;font-weight:600}
      .pg-panel{position:fixed;top:0;right:-420px;width:400px;height:100vh;background:var(--bg-card);border-left:1px solid var(--border);z-index:500;transition:right .3s cubic-bezier(.4,0,.2,1);overflow-y:auto;box-shadow:-8px 0 30px rgba(0,0,0,.4)}
      .pg-panel.open{right:0}
      .pg-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.35);backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);z-index:499;transition:opacity .3s}
    </style>
    <!-- 顶部指挥栏 -->
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px;flex-wrap:wrap;gap:8px">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:28px">${icon}</span>
        <div>
          <h3 style="font-size:18px;font-weight:700;margin:0">${(PLATFORMS_META||{})[platform]||platform}</h3>
          <div style="font-size:11px;color:var(--text-muted)">${slogan}</div>
        </div>
      </div>
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="font-size:11px;padding:4px 10px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px">
          📱 在线 <b id="pg-online-${platform}" style="color:#22c55e">-</b> / <b id="pg-total-${platform}">-</b>
        </span>
        <span style="font-size:11px;padding:4px 10px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px">
          🔄 执行中 <b id="pg-running-${platform}" style="color:#f59e0b">-</b>
        </span>
        <button class="qa-btn" onclick="loadPlatGridPage('${platform}')" style="font-size:11px;padding:4px 10px">⟳ 刷新</button>
      </div>
    </div>

    <!-- 设备卡片网格 -->
    <div id="pg-device-grid-${platform}" class="pg-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(185px,1fr));gap:10px;margin-bottom:14px">
      ${Array(6).fill(0).map(()=>'<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:12px;min-height:90px"><div style="height:12px;background:rgba(255,255,255,.06);border-radius:4px;margin-bottom:8px;width:60%"></div><div style="height:8px;background:rgba(255,255,255,.04);border-radius:4px;width:80%"></div></div>').join('')}
    </div>

    <!-- 快捷操作区 -->
    <div id="pg-quick-actions-${platform}" style="margin-bottom:14px"></div>

    <!-- 实战任务区 -->
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <div style="font-size:13px;font-weight:600">📋 实战任务</div>
        <div style="display:flex;gap:6px">
          <button class="qa-btn" onclick="loadPlatGridPage('${platform}')" style="padding:4px 10px;font-size:11px">↺ 刷新</button>
          <button class="qa-btn" onclick="navigateToPage('tasks')" style="padding:4px 10px;font-size:11px">全部 →</button>
        </div>
      </div>
      <div id="plat-recent-${platform}" style="font-size:12px;color:var(--text-muted)">加载中...</div>
    </div>

    <!-- 设备详情侧滑面板 -->
    <div id="pg-panel-${platform}" class="pg-panel">
      <div id="pg-panel-content-${platform}"></div>
    </div>
    <div id="pg-overlay-${platform}" class="pg-overlay" onclick="_pgClosePanel('${platform}')"></div>
  `;
}

// PLATFORMS_META 元数据（display name）
const PLATFORMS_META = {telegram:'Telegram',whatsapp:'WhatsApp',facebook:'Facebook',linkedin:'LinkedIn',instagram:'Instagram',twitter:'X (Twitter)'};

// ═══════════════════════════════════════════════════
// 渲染设备卡片网格
// ═══════════════════════════════════════════════════
function _pgRenderGrid(platform, devices) {
  const grid = document.getElementById(`pg-device-grid-${platform}`);
  if (!grid) return;

  if (!devices.length) {
    grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:var(--text-muted);padding:24px;font-size:13px">未发现设备 — 请检查 ADB 连接</div>';
    return;
  }

  const color = PLAT_COLORS[platform] || '#60a5fa';
  grid.innerHTML = devices.map(dev => _pgRenderCard(dev, platform, color)).join('');
}

function _pgRenderCard(dev, platform, color) {
  const alias = dev.alias || dev.device_id.substring(0, 8);
  const online = dev.online;
  const dotColor = online ? '#22c55e' : '#ef4444';
  const nodeLabel = dev.host || '主控';
  const nodeColor = _pgNodeColor(nodeLabel);

  let state = 'offline';
  if (online && dev.running_task) state = 'busy';
  else if (online && dev.today_tasks > 0) state = 'online';
  else if (online) state = 'idle';
  // P0.2: 失败率 > 30% 且至少有 3 个任务时，标记为高失败状态（红光警告）
  const _t = dev.today_tasks || 0, _f = dev.today_failed || 0;
  const failedHeavy = online && _t >= 3 && (_f / _t) > 0.3;
  const _failPct = _t > 0 ? Math.round((_f / _t) * 100) : 0;

  const taskTag = dev.running_task
    ? `<span class="pg-task-tag" style="background:${color}22;color:${color}">${TASK_NAMES[dev.running_task]||dev.running_task.replace(platform+'_','')}</span>`
    : (online ? '<span class="pg-task-tag" style="color:var(--text-dim)">😴 空闲</span>' : '<span class="pg-task-tag" style="color:#ef4444">离线</span>');

  const cfg = PLAT_GRID_CFG[platform] || {};
  // P0.4: 数字加 hover tooltip 显示完整 label；失败数字红色高亮
  const statItems = (cfg.stats||[]).map(s => {
    const v = dev[s.key] ?? 0;
    const isFailed = s.key === 'today_failed';
    const failColor = (isFailed && v > 0) ? ';color:#ef4444' : '';
    return `<span style="${v===0?'opacity:.4':''}${failColor}" title="${s.label}: ${v}">${s.icon}<b>${v}</b></span>`;
  }).join('');

  const _cardTitle = failedHeavy
    ? `⚠ 失败率 ${_failPct}% — 点击查看失败原因`
    : `${alias} · 点击查看详情`;
  return `<div class="pg-card state-${state}${failedHeavy?' failed-heavy':''}" data-did="${dev.device_id}" onclick="_pgOpenPanel('${platform}','${dev.device_id}')" title="${_cardTitle}">
    <div class="pg-card-head">
      <span class="pg-dot" style="background:${dotColor}"></span>
      <span class="pg-alias">${alias}</span>
      <span class="pg-node" style="color:${nodeColor};background:${nodeColor}18">${nodeLabel}</span>
      <span style="margin-left:auto">${taskTag}</span>
    </div>
    <div class="pg-stat-row">${statItems}</div>
    ${dev.algo_score > 0 ? `<div style="display:flex;align-items:center;gap:6px;font-size:9px;color:var(--text-muted)"><span>⚡ ${dev.algo_score}</span><div style="flex:1;height:3px;background:var(--border);border-radius:2px"><div style="height:100%;width:${Math.min(100,dev.algo_score)}%;background:${dev.algo_score>=60?'#22c55e':dev.algo_score>=30?'#f59e0b':'#94a3b8'};border-radius:2px"></div></div></div>` : ''}
  </div>`;
}

function _pgNodeColor(label) {
  if (label.startsWith('W')) return '#60a5fa';
  if (label === '主控') return '#94a3b8';
  return '#a78bfa';
}

// ═══════════════════════════════════════════════════
// 渲染快捷操作（复用 PLAT_PHASES）
// ═══════════════════════════════════════════════════
function _pgRenderQuickActions(platform, taskTypes) {
  const el = document.getElementById(`pg-quick-actions-${platform}`);
  if (!el) return;

  const phases = PLAT_PHASES[platform] || [];
  if (!phases.length) return;

  const icon = PLAT_ICONS[platform] || '';
  const color = PLAT_COLORS[platform] || '#60a5fa';

  // P0.1: 拆分双"快捷操作"语义 — 这里是【全局批量入口】
  const _onlineN = (_pgCache[platform]?.devices||[]).filter(d=>d.online).length;
  const _totalN = (_pgCache[platform]?.devices||[]).length;

  // 2026-05-07: Facebook 批量区 — 仅保留「配置执行流程」主按钮，避免和预设功能重复
  if (platform === 'facebook') {
    const hasFbPresets = typeof window.fbOpenPresetsModal === 'function';
    const totalRunning = Object.keys(_platRunningTasks).filter(k=>k.startsWith('facebook_')).reduce((a,k)=>a+(_platRunningTasks[k]||0),0);

    el.innerHTML = `
      <div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;padding:10px 12px;background:rgba(34,197,94,.06);border:1px dashed rgba(34,197,94,.25);border-radius:8px">
        <span style="font-size:14px;font-weight:700;color:var(--text)">🚀 批量任务下发</span>
        <span style="font-size:11px;color:var(--text-dim)">作用于全部 <b style="color:#22c55e">${_onlineN}</b>/${_totalN} 台在线设备</span>
        ${totalRunning>0?'<span style="margin-left:auto;font-size:11px;background:rgba(245,158,11,.12);color:#f59e0b;border-radius:5px;padding:2px 8px;font-weight:600">'+totalRunning+' 台执行中</span>':''}
      </div>
      ${hasFbPresets?`
      <button onclick="fbOpenPresetsModal()"
        style="width:100%;padding:14px 18px;font-size:15px;font-weight:700;
               background:linear-gradient(135deg,#1877f2,#0d6efd);
               border:none;color:#fff;border-radius:10px;cursor:pointer;
               display:flex;align-items:center;justify-content:center;gap:10px;
               box-shadow:0 4px 14px rgba(24,119,242,.35);transition:transform .15s,box-shadow .15s"
        onmouseover="this.style.transform='translateY(-1px)';this.style.boxShadow='0 6px 20px rgba(24,119,242,.45)'"
        onmouseout="this.style.transform='';this.style.boxShadow='0 4px 14px rgba(24,119,242,.35)'">
        <span style="font-size:20px">⚡</span>
        <div style="text-align:left">
          <div>配置执行流程</div>
          <div style="font-size:11px;opacity:.8;font-weight:400;margin-top:2px">选择方案 → 选择目标市场 → 一键批量启动</div>
        </div>
      </button>`:'<div style="text-align:center;padding:20px;color:var(--text-dim);font-size:12px">⚠ 执行方案未加载，请刷新页面</div>'}
    `;
    _loadPlatRecentTasks(platform);
    return;
  }

  // 其他平台保持原有大卡片布局
  el.innerHTML = `<div style="display:flex;align-items:baseline;gap:10px;margin-bottom:10px;padding:8px 10px;background:rgba(34,197,94,.06);border:1px dashed rgba(34,197,94,.25);border-radius:8px">
    <span style="font-size:13px;font-weight:600;color:var(--text)">🚀 批量任务下发</span>
    <span style="font-size:10px;color:var(--text-dim)">作用于全部 <b style="color:#22c55e">${_onlineN}</b>/${_totalN} 台在线设备 · 想给单台下发请点上方设备卡 →</span>
  </div>` +
    '<div style="display:flex;flex-direction:column;gap:10px">' +
    phases.map(phase => {
      const phaseTasks = taskTypes.filter(t => phase.types.includes(t.type));
      if (!phaseTasks.length) return '';
      const phaseRunning = phase.types.reduce((a,tp) => a + (_platRunningTasks[tp]||0), 0);
      return `<div style="background:var(--bg-card);border:1px solid ${phase.highlight?phase.color+'44':'var(--border)'};border-radius:10px;padding:10px 12px;${phase.highlight?'box-shadow:0 0 0 1px '+phase.color+'22':''}">
        <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
          <span style="font-size:11px;font-weight:700;color:${phase.color}">${phase.label}</span>
          <span style="font-size:10px;color:var(--text-dim)">${phase.desc}</span>
          ${phaseRunning>0?`<span style="margin-left:auto;font-size:10px;background:${phase.color}22;color:${phase.color};border-radius:4px;padding:1px 6px;font-weight:600">${phaseRunning}台运行中</span>`:''}
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(130px,1fr));gap:8px">
          ${phaseTasks.map(t => {
            const running = _platRunningTasks[t.type] || 0;
            const tIcon = PLAT_TASK_ICONS[t.type] || icon;
            const tGrad = PLAT_TASK_GRADIENTS[t.type] || ('linear-gradient(135deg,'+phase.color+','+phase.color+'dd)');
            const hint = PLAT_TASK_HINTS[t.type] || '';
            const isRunning = running > 0;
            return `<div class="action-card${isRunning?' action-card-running':''}" onclick="_tkQuickTaskWithModal('${platform}','${t.type}')" style="position:relative;${isRunning?'border-color:'+phase.color+';':''}" title="${hint}">
              <div class="action-icon" style="background:${tGrad}">${tIcon}</div>
              <div class="action-label">${t.label}</div>
              <div style="font-size:10px;color:var(--text-dim);margin-top:1px">${hint}</div>
              <div class="action-desc">${isRunning?'<span style="color:#f59e0b;font-weight:600">🔄 '+running+'台执行中</span>':'○ 空闲'}</div>
            </div>`;
          }).join('')}
        </div>
      </div>`;
    }).join('') +
    '</div>';

  // 加载任务列表
  _loadPlatRecentTasks(platform);
}

// ═══════════════════════════════════════════════════
// 设备详情侧滑面板
// ═══════════════════════════════════════════════════
function _pgOpenPanel(platform, deviceId) {
  const panel = document.getElementById(`pg-panel-${platform}`);
  const overlay = document.getElementById(`pg-overlay-${platform}`);
  const content = document.getElementById(`pg-panel-content-${platform}`);
  if (!panel || !content) return;

  const cached = _pgCache[platform];
  const dev = (cached?.devices || []).find(d => d.device_id === deviceId) || {device_id: deviceId};
  content.innerHTML = _pgBuildPanel(platform, dev);
  panel.classList.add('open');
  if (overlay) overlay.style.display = 'block';
  // P1.1: 异步加载账号画像（仅 Facebook，其他平台后续扩展）
  if (platform === 'facebook' && typeof _pgLoadAccountProfile === 'function') {
    _pgLoadAccountProfile(platform, dev);
  }
}

function _pgClosePanel(platform) {
  const panel = document.getElementById(`pg-panel-${platform}`);
  const overlay = document.getElementById(`pg-overlay-${platform}`);
  if (panel) panel.classList.remove('open');
  if (overlay) overlay.style.display = 'none';
}

function _pgBuildPanel(platform, dev) {
  const alias = dev.alias || dev.device_id.substring(0, 8);
  const online = dev.online;
  const dotColor = online ? '#22c55e' : '#ef4444';
  const color = PLAT_COLORS[platform] || '#60a5fa';
  const icon = PLAT_ICONS[platform] || '';
  const cfg = PLAT_GRID_CFG[platform] || {};

  // ── 风控前置警告（即时计算，不等 async 画像加载）──
  const _todayT = dev.today_tasks || 0;
  const _todayF = dev.today_failed || 0;
  const _failRate = _todayT > 0 ? _todayF / _todayT : 0;
  let topAlert = '';
  if (!online) {
    topAlert = `<div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:8px 12px;font-size:11px;color:#f87171;margin-bottom:10px;display:flex;align-items:center;gap:6px">
      <span style="font-size:14px">📵</span><span style="flex:1">设备离线 — 请检查 ADB 连接</span></div>`;
  } else if (_failRate >= 0.5 && _todayT >= 3) {
    topAlert = `<div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:8px 12px;font-size:11px;color:#f87171;margin-bottom:10px;display:flex;align-items:center;gap:6px">
      <span style="font-size:14px">�</span>
      <span style="flex:1">失败率 ${Math.round(_failRate*100)}% (${_todayF}/${_todayT})，建议先排查</span>
      <button onclick="_pgShowFailureDetail('${platform}','${dev.device_id}');event.stopPropagation()" style="padding:3px 10px;font-size:10px;background:#ef4444;border:none;color:#fff;border-radius:5px;cursor:pointer;font-weight:600;flex-shrink:0">查看原因→</button></div>`;
  } else if (dev.running_task) {
    topAlert = `<div style="background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.25);border-radius:8px;padding:8px 12px;font-size:11px;color:#60a5fa;margin-bottom:10px;display:flex;align-items:center;gap:6px">
      <span style="font-size:14px">🔄</span><span>正在执行: ${TASK_NAMES[dev.running_task]||dev.running_task}</span></div>`;
  }

  // ── 统计行（紧凑横排，失败可点击）──
  const statItems = (cfg.stats||[]).map(s => {
    const v = dev[s.key] ?? '-';
    const isFailed = s.key === 'today_failed' && v > 0;
    const valColor = isFailed ? '#ef4444' : 'var(--text)';
    const clickAttr = isFailed
      ? ` onclick="_pgShowFailureDetail('${platform}','${dev.device_id}');event.stopPropagation()" style="cursor:pointer;color:#ef4444;text-decoration:underline;text-decoration-style:dashed" title="点击查看失败详情"`
      : '';
    return isFailed
      ? `<span${clickAttr}><b>${v}</b> ${s.label}</span>`
      : `<span><b style="color:${valColor}">${v}</b> <span style="color:var(--text-dim)">${s.label}</span></span>`;
  });
  const statsHtml = statItems.length
    ? `<div style="font-size:11px;display:flex;align-items:center;gap:4px;flex-wrap:wrap;margin-bottom:12px;padding:7px 10px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px">${statItems.join('<span style="color:var(--border);margin:0 3px">·</span>')}<span style="margin-left:auto;color:${online?'#22c55e':'#ef4444'};font-size:10px;font-weight:600">${online?'● 在线':'○ 离线'}</span></div>`
    : '';

  // ── 配置执行流程按钮 ──
  const flowFnName = (platform === 'facebook') ? 'fbDeviceConfigFlow' : (platform + 'DeviceConfigFlow');
  const hasFlowEntry = platform !== 'tiktok' && (
    typeof window[flowFnName] === 'function' ||
    typeof window[platform + 'OpenPresetsModal'] === 'function'
  );
  const flowBtnHtml = hasFlowEntry
    ? `<button onclick="_pgOpenFlowConfig('${platform}','${dev.device_id}');event.stopPropagation()"
         ${online ? '' : 'disabled'}
         style="width:100%;margin-bottom:12px;padding:11px 14px;font-size:13px;font-weight:600;
                background:linear-gradient(135deg,${color},${color}cc);
                border:none;color:#fff;border-radius:8px;cursor:${online?'pointer':'not-allowed'};
                opacity:${online?'1':'0.5'};display:flex;align-items:center;justify-content:center;gap:8px;
                box-shadow:0 2px 8px ${color}55;transition:transform .12s,box-shadow .12s"
         onmouseover="if(!this.disabled){this.style.transform='translateY(-1px)';this.style.boxShadow='0 4px 14px ${color}66'}"
         onmouseout="this.style.transform='';this.style.boxShadow='0 2px 8px ${color}55'">
         <span style="font-size:15px">⚡</span>配置执行流程
         <span style="font-size:10px;opacity:.8;margin-left:4px">选方案 · 选市场 · 一键启动</span>
       </button>`
    : '';

  // ── 账号画像占位（仅 Facebook，异步加载渲染）──
  const profileHtml = platform === 'facebook'
    ? `<div id="pg-profile-${dev.device_id}" style="margin-bottom:10px">
         <div style="text-align:center;color:var(--text-dim);padding:16px;font-size:11px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px">⏳ 加载账号画像...</div>
       </div>`
    : '';

  // ── 单步操作（折叠，Facebook 仅 4 个核心任务）──
  let advancedBtns = '';
  if (platform === 'facebook') {
    const essentialTasks = ['facebook_browse_feed','facebook_search_leads','facebook_add_friend','facebook_send_message'];
    advancedBtns = essentialTasks.map(taskType => {
      const tIcon = PLAT_TASK_ICONS[taskType] || '▶';
      const tGrad = PLAT_TASK_GRADIENTS[taskType] || ('linear-gradient(135deg,'+color+','+color+'dd)');
      const label = TASK_NAMES[taskType] || taskType.replace(platform+'_','');
      const tHint = (typeof PLAT_TASK_HINTS !== 'undefined' && PLAT_TASK_HINTS[taskType]) || '';
      return `<div onclick="_pgPanelTask('${platform}','${taskType}','${dev.device_id}');event.stopPropagation()"
        title="${(label+(tHint?' — '+tHint:'')).replace(/"/g,'&quot;')}"
        style="flex:1;min-width:calc(50% - 4px);padding:10px 8px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;cursor:pointer;
               display:flex;align-items:center;gap:8px;transition:all .12s"
        onmouseover="this.style.borderColor='${color}55';this.style.transform='translateY(-1px)'"
        onmouseout="this.style.borderColor='var(--border)';this.style.transform=''">
        <div style="width:26px;height:26px;border-radius:6px;background:${tGrad};display:flex;align-items:center;justify-content:center;font-size:13px;flex-shrink:0">${tIcon}</div>
        <div><div style="font-size:10px;font-weight:600;color:var(--text)">${label}</div>
        <div style="font-size:9px;color:var(--text-dim);line-height:1.2">${tHint}</div></div>
      </div>`;
    }).join('');
  } else {
    advancedBtns = (cfg.quickActions || []).map(taskType => {
      const tIcon = PLAT_TASK_ICONS[taskType] || '▶';
      const label = TASK_NAMES[taskType] || taskType.replace(platform+'_','');
      return `<button onclick="_pgPanelTask('${platform}','${taskType}','${dev.device_id}');event.stopPropagation()"
        style="flex:1;min-width:80px;padding:8px 6px;font-size:11px;background:${color}15;border:1px solid ${color}30;color:${color};border-radius:6px;cursor:pointer;display:flex;flex-direction:column;align-items:center;gap:2px">
        <span style="font-size:16px">${tIcon}</span>${label}
      </button>`;
    }).join('');
  }

  // ── 设备信息（折叠内容）──
  const deviceInfoRows = `
    <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>设备 ID</span><span style="color:var(--text);font-family:monospace;font-size:10px">${dev.device_id.substring(0,16)}</span></div>
    <div style="display:flex;justify-content:space-between;margin-bottom:4px"><span>节点</span><span style="color:var(--text)">${dev.host||'主控'}</span></div>
    ${dev.phase?`<div style="display:flex;justify-content:space-between"><span>阶段</span><span style="color:var(--text)">${dev.phase}</span></div>`:''}`;

  return `
    <div style="padding:16px">
      <!-- ① 头部 -->
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div style="display:flex;align-items:center;gap:8px">
          <span style="width:10px;height:10px;border-radius:50%;background:${dotColor};${online?'box-shadow:0 0 6px '+dotColor:''}"></span>
          <span style="font-size:16px;font-weight:700">${alias}</span>
          <span style="font-size:10px;padding:2px 8px;background:${color}18;color:${color};border-radius:4px;font-weight:600">${icon} ${PLATFORMS_META[platform]||platform}</span>
        </div>
        <button onclick="_pgClosePanel('${platform}')" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px;width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;transition:all .12s" onmouseover="this.style.background='rgba(239,68,68,.15)';this.style.color='#ef4444'" onmouseout="this.style.background='none';this.style.color='var(--text-muted)'">✕</button>
      </div>

      <!-- ② 风控/状态前置警告（profile 加载后若有更详细建议则自动隐藏） -->
      <div id="pg-top-alert-${dev.device_id}">${topAlert}</div>

      <!-- ③ 统计概览（紧凑单行） -->
      ${statsHtml}

      <!-- ④ 配置执行流程 -->
      ${flowBtnHtml}

      <!-- ⑤ 账号画像（异步加载） -->
      ${profileHtml}

      <!-- ⑥ 单步操作（折叠，默认收起） -->
      <div style="margin-bottom:8px;border:1px solid var(--border);border-radius:8px;overflow:hidden">
        <div onclick="var c=this.nextElementSibling;var open=c.style.display!=='none';c.style.display=open?'none':'flex';this.querySelector('[data-arr]').textContent=open?'▸':'▾'"
          style="display:flex;align-items:center;gap:6px;padding:9px 12px;cursor:pointer;background:var(--bg-main);font-size:11px;transition:background .1s"
          onmouseover="this.style.background='var(--bg-card)'" onmouseout="this.style.background='var(--bg-main)'">
          <span data-arr style="color:${color};font-size:10px;font-weight:700">▸</span>
          <span style="font-weight:600;color:var(--text-dim)">单步操作</span>
          <span style="font-size:9px;color:var(--text-muted);font-weight:normal">点击下发到 ${alias}</span>
        </div>
        <div style="display:none;flex-wrap:wrap;gap:6px;padding:10px 12px;background:var(--bg-card)">${advancedBtns}</div>
      </div>

      <!-- ⑦ 设备信息（折叠，默认收起） -->
      <div style="border:1px solid var(--border);border-radius:8px;overflow:hidden">
        <div onclick="var c=this.nextElementSibling;var open=c.style.display!=='none';c.style.display=open?'none':'block';this.querySelector('[data-arr]').textContent=open?'▸':'▾'"
          style="display:flex;align-items:center;gap:6px;padding:9px 12px;cursor:pointer;background:var(--bg-main);font-size:11px;transition:background .1s"
          onmouseover="this.style.background='var(--bg-card)'" onmouseout="this.style.background='var(--bg-main)'">
          <span data-arr style="color:var(--text-dim);font-size:10px;font-weight:700">▸</span>
          <span style="font-weight:600;color:var(--text-dim)">设备信息</span>
          <span style="font-size:9px;color:var(--text-muted);font-weight:normal">${dev.device_id.substring(0,8)}…</span>
        </div>
        <div style="display:none;font-size:11px;color:var(--text-muted);padding:10px 12px;background:var(--bg-card)">${deviceInfoRows}</div>
      </div>
    </div>
  `;
}

// 从面板内打开"配置执行流程"弹窗 —— 通用路由
// 优先级: window[platform+'DeviceConfigFlow'] > facebook 专属别名 fbDeviceConfigFlow
//       > window[platform+'OpenPresetsModal'] (兜底)
function _pgOpenFlowConfig(platform, deviceId) {
  const fbAlias = (platform === 'facebook') ? window.fbDeviceConfigFlow : null;
  const fn = window[platform + 'DeviceConfigFlow'] || fbAlias || window[platform + 'OpenPresetsModal'];
  if (typeof fn !== 'function') {
    showToast('该平台暂未实现「配置执行流程」', 'warning');
    return;
  }
  try {
    _pgClosePanel(platform);
    fn(deviceId);
  } catch (e) {
    showToast('打开执行流程失败: ' + (e.message || e), 'error');
  }
}

// 从面板内发起单设备任务
async function _pgPanelTask(platform, taskType, deviceId) {
  const paramDefs = PLAT_TK_PARAMS[taskType] || [];
  const needsAudienceModal = platform === 'tiktok' && typeof platTkNeedsAudienceModal === 'function' && platTkNeedsAudienceModal(taskType);
  if (paramDefs.length || taskType.includes('send_message') || taskType.includes('send_dm') || needsAudienceModal) {
    _pgClosePanel(platform);
    await _tkQuickTaskWithModal(platform, taskType);
    return;
  }
  try {
    await api('POST', `/platforms/${platform}/tasks`, {task_type: taskType, device_id: deviceId, params: {}});
    const alias = ALIAS[deviceId] || deviceId.substring(0,8);
    showToast(`✓ ${alias}: ${TASK_NAMES[taskType]||taskType} 任务已创建`, 'success');
    setTimeout(() => loadPlatGridPage(platform), 1000);
  } catch(e) {
    showToast('创建失败: ' + (e.message||''), 'error');
  }
}

// ═══════════════════════════════════════════════════
// 自动刷新（30秒）
// ═══════════════════════════════════════════════════
function _pgStartAutoRefresh(platform) {
  if (_pgRefreshTimers[platform]) clearInterval(_pgRefreshTimers[platform]);
  _pgRefreshTimers[platform] = setInterval(async () => {
    if (_currentPage !== 'plat-' + platform) {
      clearInterval(_pgRefreshTimers[platform]);
      return;
    }
    try {
      const [gridData] = await Promise.all([
        api('GET', `/platforms/${platform}/device-grid`),
        _fetchPlatTasks(platform),
      ]);
      _pgCache[platform] = gridData;
      const s = gridData.summary || {};
      _pgSetText(`pg-online-${platform}`, s.online || 0);
      _pgSetText(`pg-total-${platform}`, s.total || 0);
      _pgSetText(`pg-running-${platform}`, s.running_tasks || 0);
      _pgRenderGrid(platform, gridData.devices || []);
      _loadPlatRecentTasks(platform);
    } catch(e) {}
  }, 30000);
}

function _pgSetText(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

// ═══════════════════════════════════════════════════
// P0.2 失败钻取 Modal — 消费 GET /tasks/error-analysis
// ═══════════════════════════════════════════════════
const _PG_ERROR_CAT_LABELS = {
  vpn_failure: '🌐 VPN 故障',
  network_timeout: '⏱ 网络超时',
  ui_not_found: '🎯 UI 元素未找到',
  account_limited: '🚫 账号限流/风控',
  device_offline: '📵 设备离线',
  geo_mismatch: '📍 地理位置不匹配',
  task_timeout: '⌛ 任务超时',
  unknown: '❓ 未分类',
};
const _PG_CAT_TIPS = {
  vpn_failure: '检查 VPN 配置 / 代理 IP 是否过期；尝试切换节点',
  network_timeout: '检查网络稳定性，必要时重启代理或换上行链路',
  ui_not_found: 'Facebook UI 可能更新，需要更新 selector / VLM fallback 检查',
  account_limited: '账号触发风控，建议停一段时间或换号；查看 cookies 健康度',
  device_offline: '检查 ADB 连接，重启 uiautomator 服务',
  geo_mismatch: '设备 GPS 与账号注册地不一致 — 检查 VPN 区域 / 设备伪造位置',
  task_timeout: '任务执行超时，可能是 UI 卡顿、网络慢或被风控',
  unknown: '请查看具体 error 字段，向工程师反馈完整 task_id',
};

async function _pgShowFailureDetail(platform, deviceId) {
  let modal = document.getElementById('_pg-fail-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = '_pg-fail-modal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:10000;display:flex;align-items:center;justify-content:center;padding:20px';
    modal.onclick = (e) => { if (e.target === modal) _pgCloseFailModal(); };
    document.body.appendChild(modal);
  }
  const alias = (typeof ALIAS !== 'undefined' && ALIAS[deviceId]) || deviceId.substring(0, 8);
  modal.innerHTML = `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;width:720px;max-width:100%;max-height:88vh;overflow-y:auto;padding:18px;box-shadow:0 10px 40px rgba(0,0,0,.5)">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;padding-bottom:10px;border-bottom:1px solid var(--border)">
      <div>
        <div style="font-size:15px;font-weight:700;color:var(--text)">⚠ 失败任务详情 · ${alias}</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:2px">最近 24 小时窗口 · 数据来自 /tasks/error-analysis</div>
      </div>
      <button onclick="_pgCloseFailModal()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:22px;line-height:1">✕</button>
    </div>
    <div id="_pg-fail-modal-body">
      <div style="text-align:center;padding:30px;color:var(--text-dim);font-size:12px">⏳ 加载失败详情...</div>
    </div>
  </div>`;
  modal.style.display = 'flex';

  try {
    const [analysis, tasksResp] = await Promise.all([
      api('GET', '/tasks/error-analysis?hours=24&include_samples=true'),
      api('GET', '/tasks?status=failed&limit=80'),
    ]);
    const tasksAll = Array.isArray(tasksResp) ? tasksResp : (tasksResp.tasks || []);
    const myFails = tasksAll.filter(t => t.device_id === deviceId).slice(0, 30);
    const byDev = (analysis && analysis.by_device) || {};
    const myStats = byDev[deviceId] || { total_failed: myFails.length, top_category: 'unknown' };
    const byType = {};
    myFails.forEach(t => {
      const tp = t.type || 'unknown';
      (byType[tp] = byType[tp] || []).push(t);
    });

    const summaryHtml = `
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:14px">
        <div style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.25);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:22px;font-weight:700;color:#ef4444">${myStats.total_failed||myFails.length}</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">📛 总失败数</div>
        </div>
        <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:13px;font-weight:600;color:var(--text);min-height:18px">${_PG_ERROR_CAT_LABELS[myStats.top_category]||myStats.top_category||'-'}</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:4px">主要原因</div>
        </div>
        <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:22px;font-weight:700;color:var(--text)">${Object.keys(byType).length}</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">涉及任务种类</div>
        </div>
      </div>
    `;

    const tipKey = myStats.top_category || 'unknown';
    const tipHtml = `
      <div style="background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.25);border-radius:8px;padding:10px;margin-bottom:14px;font-size:12px">
        <div style="font-weight:600;color:#60a5fa;margin-bottom:4px">💡 修复建议</div>
        <div style="color:var(--text-muted);line-height:1.5">${_PG_CAT_TIPS[tipKey] || '请查看具体错误信息'}</div>
      </div>
    `;

    let listHtml = '<div style="font-weight:600;color:var(--text);margin-bottom:8px;font-size:12px">📋 失败任务列表（按任务类型分组）</div>';
    if (!myFails.length) {
      listHtml += '<div style="text-align:center;padding:20px;color:var(--text-dim);font-size:12px">最近 24 小时该设备无失败记录</div>';
    } else {
      Object.keys(byType).forEach(tp => {
        const items = byType[tp];
        const taskName = (typeof TASK_NAMES !== 'undefined' && TASK_NAMES[tp]) || tp;
        const tIcon = (typeof PLAT_TASK_ICONS !== 'undefined' && PLAT_TASK_ICONS[tp]) || '▶';
        listHtml += `
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;margin-bottom:8px;overflow:hidden">
            <div style="padding:8px 10px;border-bottom:1px solid var(--border);font-size:11px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:6px;background:rgba(239,68,68,.04)">
              <span>${tIcon}</span><span>${taskName}</span>
              <button onclick="_pgRetryFromFailure('${platform}','${tp}','${deviceId}')" title="重新发起一个相同类型的任务（如有参数会弹出配置框）" style="margin-left:auto;padding:3px 9px;font-size:10px;background:#3b82f6;border:none;color:#fff;border-radius:4px;cursor:pointer;font-weight:600">⟳ 重试</button>
              <span style="color:#ef4444;font-weight:700">×${items.length}</span>
            </div>
            <div style="padding:4px 10px 6px">
              ${items.slice(0,5).map(it => {
                let r = it.result || {};
                if (typeof r === 'string') { try { r = JSON.parse(r); } catch(e) { r = {error: r}; } }
                const err = r.error || '(无错误信息)';
                const ts = (it.updated_at || '').replace('T',' ').substring(0,16);
                return `<div style="font-size:10px;color:var(--text-muted);padding:5px 0;border-bottom:1px dashed var(--border)">
                  <span style="color:var(--text-dim);font-family:monospace">${ts}</span>
                  <div style="color:#f87171;margin-top:2px;line-height:1.4">${String(err).substring(0,160)}${String(err).length>160?'...':''}</div>
                </div>`;
              }).join('')}
              ${items.length > 5 ? `<div style="font-size:10px;color:var(--text-dim);padding:4px 0;text-align:center">… 还有 ${items.length-5} 条</div>` : ''}
            </div>
          </div>
        `;
      });
    }

    document.getElementById('_pg-fail-modal-body').innerHTML = summaryHtml + tipHtml + listHtml + `
      <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end">
        <button onclick="_pgCloseFailModal()" style="padding:6px 14px;font-size:12px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;cursor:pointer">关闭</button>
        <button onclick="if(typeof navigateToPage==='function')navigateToPage('tasks');_pgCloseFailModal()" style="padding:6px 14px;font-size:12px;background:#3b82f6;border:none;color:#fff;border-radius:6px;cursor:pointer">查看任务中心 →</button>
      </div>
    `;
  } catch (e) {
    document.getElementById('_pg-fail-modal-body').innerHTML = `<div style="color:#f87171;padding:20px;text-align:center;font-size:12px">加载失败: ${e.message||e}</div>`;
  }
}

function _pgCloseFailModal() {
  const modal = document.getElementById('_pg-fail-modal');
  if (modal) modal.style.display = 'none';
}

// ESC 键关闭失败 modal
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') {
    const modal = document.getElementById('_pg-fail-modal');
    if (modal && modal.style.display !== 'none') _pgCloseFailModal();
  }
});

// ═══════════════════════════════════════════════════
// P1.5 失败 modal — 一键重试（关 modal + 复用 _pgPanelTask）
// ═══════════════════════════════════════════════════
async function _pgRetryFromFailure(platform, taskType, deviceId) {
  _pgCloseFailModal();
  // 复用 _pgPanelTask：含参数的任务会自动弹出参数 modal，无参数的直接 POST 创建
  if (typeof _pgPanelTask === 'function') {
    try {
      await _pgPanelTask(platform, taskType, deviceId);
    } catch (e) {
      if (typeof showToast === 'function') showToast('重试失败: ' + (e.message || e), 'error');
    }
  }
}

// ═══════════════════════════════════════════════════
// P1.1 + P2.1 + P2.4 账号画像
//   - 主路径: GET /facebook/devices/{id}/account-profile (后端聚合 + 智能建议)
//   - Fallback: 前端 3 API 并发（risk/status + risk/history + funnel）
//   - 智能建议 banner: 顶部 P2.4 突出展示「现在该做什么」
// ═══════════════════════════════════════════════════
const _PG_PHASE_MAP = {
  cold_start: { emoji: '🌱', label: '冷启动', color: '#22c55e', tip: '账号刚开始养，建议低频互动 + 内容浏览为主' },
  growth:     { emoji: '📈', label: '成长期', color: '#3b82f6', tip: '加好友/互动可适度增加，避免突发量' },
  mature:     { emoji: '🌳', label: '成熟期', color: '#8b5cf6', tip: '可承担营销动作，但保持节奏' },
  cooldown:   { emoji: '❄️', label: '冷却期', color: '#f59e0b', tip: '⚠ 触发风控，建议停止外联动作 24h+' },
};

const _PG_RISK_LEVEL_MAP = {
  low:    { color: '#22c55e', label: '🟢 健康',   bg: 'rgba(34,197,94,.1)',  tip: '可正常运行' },
  medium: { color: '#f59e0b', label: '🟡 中风险', bg: 'rgba(245,158,11,.1)', tip: '降频运行' },
  high:   { color: '#ef4444', label: '🔴 高风险', bg: 'rgba(239,68,68,.1)',  tip: '建议暂停外联动作' },
};

async function _pgLoadAccountProfile(platform, dev) {
  const target = document.getElementById('pg-profile-' + dev.device_id);
  if (!target) return;
  const deviceId = dev.device_id;
  // P2.1 主路径: 先调后端聚合 API（含智能建议）
  let profile = null;
  try {
    profile = await api('GET', `/facebook/devices/${deviceId}/account-profile`);
  } catch (e) {
    // 404 / 5xx → fallback 到前端 3 API 并发
    profile = await _pgFallbackProfile(deviceId, dev);
  }
  if (!profile) {
    target.innerHTML = `<div style="text-align:center;color:#f87171;padding:8px;font-size:10px">画像加载失败</div>`;
    return;
  }
  // race guard: 用户秒切设备时，确保画像渲染到正确占位符
  const stillTarget = document.getElementById('pg-profile-' + deviceId);
  if (!stillTarget) return;
  _pgRenderProfile(stillTarget, profile);
}

// Fallback：后端聚合 API 不可用时前端用 3 个独立 API 拼出同结构对象
async function _pgFallbackProfile(deviceId, dev) {
  try {
    const [riskStatus, riskHistory, funnel] = await Promise.allSettled([
      api('GET', '/facebook/risk/status'),
      api('GET', `/facebook/risk/history/${deviceId}`),
      api('GET', `/facebook/funnel?device_id=${deviceId}&since_hours=168`),
    ]);
    let phase = (dev && dev.phase) || 'unknown';
    let risk24h = 0, lastBlockedAt = null;
    let friends = 0, groups = 0, friendReqs = 0;
    if (riskStatus.status === 'fulfilled') {
      const v = riskStatus.value;
      const arr = Array.isArray(v) ? v : (v && (v.devices || v.list)) || [];
      const me = arr.find(d => d.device_id === deviceId) || {};
      phase = me.phase || phase;
      risk24h = me.risk_count_24h ?? me.risk_count ?? me.events_24h ?? 0;
    }
    if (riskHistory.status === 'fulfilled') {
      const v = riskHistory.value;
      const list = Array.isArray(v) ? v : (v && (v.events || v.history)) || [];
      const blockEvents = list.filter(e => /rate_limit|checkpoint|banned|policy|block/i.test(e.kind || e.type || ''));
      if (blockEvents.length) lastBlockedAt = blockEvents[0].detected_at || blockEvents[0].at;
    }
    if (funnel.status === 'fulfilled') {
      const f = funnel.value || {};
      friends = f.stage_friend_accepted ?? 0;
      groups = f.stage_groups_joined ?? f.groups_joined ?? 0;
      friendReqs = f.stage_friend_request_sent ?? 0;
    }
    // P2.5: today 数据从 dev.today_xxx 读（前端缓存有）→ 与后端规则引擎优先级保持一致
    // P2.6: 每条建议带 action 字段（CTA 按钮）
    const _todayTotal = (dev && dev.today_tasks) || 0;
    const _todayFailed = (dev && dev.today_failed) || 0;
    const _todayFailRate = _todayTotal > 0 ? _todayFailed / _todayTotal : 0;
    const _openFail = { label: '查看失败原因', type: 'open_failure_modal' };
    let level, suggestion;
    // 优先级（高→低）: 今日失败率 > phase=cooldown OR 24h 风控 ≥5 > 24h 风控 ≥2 > phase 推荐
    if (_todayFailRate >= 0.5 && _todayTotal >= 3) {
      level = 'high';
      suggestion = { tone: 'warning',
        text: `🚨 今日失败率 ${Math.round(_todayFailRate*100)}% (${_todayFailed}/${_todayTotal})，先停止外联 → 排查原因`,
        action: _openFail };
    } else if (_todayFailRate >= 0.3 && _todayTotal >= 5) {
      level = 'medium';
      suggestion = { tone: 'warning',
        text: `⚠ 今日失败率偏高 ${Math.round(_todayFailRate*100)}% (${_todayFailed}/${_todayTotal})，建议降频`,
        action: _openFail };
    } else if (phase === 'cooldown' || risk24h >= 5) {
      level = 'high';
      suggestion = { tone: 'warning', text: '🔴 高风控状态，建议立即停止外联动作', action: _openFail };
    } else if (risk24h >= 2) {
      level = 'medium';
      suggestion = { tone: 'warning', text: '🟡 风控信号偏多，建议降频运行', action: _openFail };
    } else if (phase === 'cold_start') {
      level = 'low';
      suggestion = { tone: 'info', text: '🌱 冷启动期，建议先内容浏览养号 5-7 天',
        action: { label: '立即开始养号', type: 'run_task', task_type: 'facebook_browse_feed_by_interest' } };
    } else if (phase === 'growth') {
      level = 'low';
      suggestion = { tone: 'info', text: '📈 健康成长期，可下发加好友/群成员打招呼/群内互动',
        action: { label: '发起加好友', type: 'run_task', task_type: 'facebook_add_friend' } };
    } else if (phase === 'mature') {
      level = 'low';
      suggestion = { tone: 'ok', text: '🌳 账号成熟，可承担营销动作 — 保持节奏',
        action: { label: '全链路客服拓展', type: 'run_task', task_type: 'facebook_campaign_run' } };
    } else {
      level = 'low';
      suggestion = { tone: 'info', text: '✓ 账号状态正常', action: null };
    }
    return {
      device_id: deviceId,
      account: { phase, friends_count: friends, groups_count: groups, friend_requests_sent_7d: friendReqs },
      risk: { level, count_24h: risk24h, last_blocked_at: lastBlockedAt, cooldown_remaining: 0 },
      recent_tasks: [],  // fallback 不取 task list（避免额外开销，主路径才返）
      today_stats: { tasks: _todayTotal, failed: _todayFailed, fail_rate: Math.round(_todayFailRate*100)/100 },
      suggestion,
    };
  } catch (e) {
    return null;
  }
}

// 渲染：聚合 API 和 fallback 都返回相同结构 → 同一渲染函数
// P2.5/P2.6/P2.7 升级：CTA 按钮 + 失败行可点击 + 空态提示 + tip 卡片内永久显示（修 tooltip 浮动 bug）
function _pgRenderProfile(target, p) {
  const acc = p.account || {};
  const risk = p.risk || {};
  const today = p.today_stats || {};
  const phase = acc.phase || 'unknown';
  const phaseInfo = _PG_PHASE_MAP[phase] || { emoji: '❓', label: String(phase), color: '#94a3b8', tip: '' };
  const riskLevel = _PG_RISK_LEVEL_MAP[risk.level] || _PG_RISK_LEVEL_MAP.low;
  const risk24h = risk.count_24h ?? 0;
  const lastBlockedAt = risk.last_blocked_at;
  const friends = acc.friends_count ?? '-';
  const groups = acc.groups_count ?? '-';
  const friendReqs = acc.friend_requests_sent_7d ?? '-';
  const recentTasks = p.recent_tasks || [];
  const suggestion = p.suggestion || {};
  const action = suggestion.action;
  const deviceId = p.device_id;
  const platform = 'facebook';

  // P2.4 智能建议 banner 配色 + P2.6 CTA 按钮配色
  const _toneStyle = {
    warning: { bg: 'rgba(239,68,68,.10)',  border: 'rgba(239,68,68,.3)',  color: '#f87171', btnBg: '#ef4444' },
    info:    { bg: 'rgba(99,102,241,.10)', border: 'rgba(99,102,241,.3)', color: '#a5b4fc', btnBg: '#6366f1' },
    ok:      { bg: 'rgba(34,197,94,.10)',  border: 'rgba(34,197,94,.3)',  color: '#86efac', btnBg: '#22c55e' },
  }[suggestion.tone] || { bg: 'rgba(99,102,241,.10)', border: 'rgba(99,102,241,.3)', color: '#a5b4fc', btnBg: '#6366f1' };

  // P2.6 CTA 按钮 — 根据 action.type 路由（前端只做路由，不写业务规则）
  let ctaBtnHtml = '';
  if (action && action.label) {
    const onclickStr = action.type === 'open_failure_modal'
      ? `_pgShowFailureDetail('${platform}','${deviceId}')`
      : action.type === 'run_task' && action.task_type
      ? `_pgPanelTask('${platform}','${action.task_type}','${deviceId}')`
      : '';
    if (onclickStr) {
      ctaBtnHtml = `<button onclick="${onclickStr}" style="margin-left:8px;padding:4px 10px;font-size:10px;background:${_toneStyle.btnBg};border:none;color:#fff;border-radius:5px;cursor:pointer;font-weight:600;flex-shrink:0;white-space:nowrap">${action.label} →</button>`;
    }
  }

  // P2.5 失败率 chip（数据透明度 — 让"5/7" 这种关键信号可见在风控卡里）
  const todayTasks = today.tasks ?? 0;
  const todayFailed = today.failed ?? 0;
  const todayFailRate = today.fail_rate ?? 0;
  const failChipColor = todayFailRate >= 0.3 ? '#f87171' : 'var(--text-dim)';
  const failChipBg = todayFailRate >= 0.3 ? 'rgba(239,68,68,.15)' : 'rgba(255,255,255,.05)';
  const failChipHtml = todayTasks > 0
    ? `<div style="margin-top:4px;font-size:9px;color:${failChipColor};background:${failChipBg};padding:2px 6px;border-radius:3px;font-weight:600;display:inline-block">今日 ${todayTasks} · 失败 ${Math.round(todayFailRate*100)}%</div>`
    : '';

  // P2.10 空数据提示（friends/groups/reqs 全 0 时解释）
  const emptyDataHint = (friends === 0 && groups === 0 && friendReqs === 0)
    ? `<div style="font-size:9px;color:var(--text-dim);text-align:center;padding:4px;font-style:italic;margin-bottom:8px">📭 该号尚未产生 funnel 数据 — 可能是新号或任务全失败</div>`
    : '';

  // 画像已加载 → 隐藏顶部简略 topAlert（画像建议更详细，避免重复信息）
  if (suggestion.text) {
    const topAlertEl = document.getElementById('pg-top-alert-' + deviceId);
    if (topAlertEl) topAlertEl.style.display = 'none';
  }

  target.innerHTML = `
    ${suggestion.text ? `<div style="background:${_toneStyle.bg};border:1px solid ${_toneStyle.border};color:${_toneStyle.color};border-radius:8px;padding:8px 10px;margin-bottom:8px;font-size:11px;line-height:1.45;font-weight:500;display:flex;align-items:center;gap:4px">
      <span style="flex:1">${suggestion.text}</span>${ctaBtnHtml}
    </div>` : ''}
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px;margin-bottom:8px">
      <div style="background:${phaseInfo.color}10;border:1px solid ${phaseInfo.color}40;border-radius:6px;padding:8px">
        <div style="font-size:9px;color:var(--text-dim);margin-bottom:2px">账号阶段</div>
        <div style="font-size:13px;font-weight:600;color:${phaseInfo.color}">${phaseInfo.emoji} ${phaseInfo.label}</div>
        ${phaseInfo.tip ? `<div style="font-size:9px;color:var(--text-dim);margin-top:3px;line-height:1.3">${phaseInfo.tip}</div>` : ''}
      </div>
      <div style="background:${riskLevel.bg};border:1px solid ${riskLevel.color}40;border-radius:6px;padding:8px">
        <div style="font-size:9px;color:var(--text-dim);margin-bottom:2px">风控等级 · 24h ${risk24h} 次</div>
        <div style="font-size:13px;font-weight:600;color:${riskLevel.color}">${riskLevel.label}</div>
        ${failChipHtml}
      </div>
    </div>
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;margin-bottom:8px">
      <div title="累计已通过的好友数" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px">
        <div style="font-size:14px;font-weight:700;color:var(--text)">${friends}</div>
        <div style="font-size:9px;color:var(--text-dim)">👥 好友</div>
      </div>
      <div title="已加入的群组数" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px">
        <div style="font-size:14px;font-weight:700;color:var(--text)">${groups}</div>
        <div style="font-size:9px;color:var(--text-dim)">👨‍👩‍👧 群组</div>
      </div>
      <div title="近 7 天发出的好友请求" style="text-align:center;padding:6px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px">
        <div style="font-size:14px;font-weight:700;color:var(--text)">${friendReqs}</div>
        <div style="font-size:9px;color:var(--text-dim)">📨 7天请求</div>
      </div>
    </div>
    ${emptyDataHint}
    ${lastBlockedAt ? `<div style="background:rgba(239,68,68,.06);border-left:3px solid #ef4444;padding:6px 8px;margin-bottom:8px;font-size:10px;color:#f87171">⚠ 上次限流: ${String(lastBlockedAt).replace('T',' ').substring(0,16)}</div>` : ''}
    ${recentTasks.length ? `
      <div style="font-size:10px;color:var(--text-dim);margin-bottom:4px">📋 最近任务（失败行可点击）</div>
      ${recentTasks.map(t => {
        const ts = String(t.updated_at||'').replace('T',' ').substring(11,16);
        const tname = (typeof TASK_NAMES !== 'undefined' && TASK_NAMES[t.type]) || t.type || '-';
        const statusColor = t.status==='completed' ? '#22c55e' : t.status==='failed' ? '#ef4444' : '#94a3b8';
        // P2.7: 失败行 onclick 进失败 modal
        const isFailed = t.status === 'failed';
        const onclickAttr = isFailed
          ? ` onclick="_pgShowFailureDetail('${platform}','${deviceId}')"`
          : '';
        return `<div title="${isFailed?'点击查看失败原因':''}"${onclickAttr} style="font-size:10px;color:var(--text-muted);padding:5px 4px;border-bottom:1px dashed var(--border);display:flex;align-items:center;gap:6px;${isFailed?'cursor:pointer':''}">
          <span style="color:var(--text-dim);font-family:monospace;flex-shrink:0">${ts}</span>
          <span style="color:var(--text);flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${tname}</span>
          <span style="font-size:9px;color:${statusColor};flex-shrink:0">${t.status||''}</span>
          ${isFailed?'<span style="font-size:9px;color:#f87171;flex-shrink:0">→</span>':''}
        </div>`;
      }).join('')}
    ` : ''}
  `;
}
