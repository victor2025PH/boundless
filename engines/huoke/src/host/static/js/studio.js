// ═══════════════════════════════════════════════════════════════════
// studio.js  —  Content Studio 前端模块
// 注册页面: studio  (内部 tabs: dash / review / calendar / personas / config)
// 依赖: core.js (api / showToast), overview.js (_PAGE_LOADERS / navigateToPage)
// ═══════════════════════════════════════════════════════════════════
'use strict';

// ── 状态 ─────────────────────────────────────────────────────────
let _studioCurrentTab = 'dash';
let _studioPollTimer  = null;
let _studioDevices    = [];   // ADB 在线设备列表（审核时选择）

// ── 平台图标 / 颜色映射 ──────────────────────────────────────────
const _PLAT_ICON = {
  tiktok:'🎵', instagram:'📸', telegram:'✈️',
  facebook:'📘', linkedin:'💼', twitter:'🐦',
  whatsapp:'💬', xiaohongshu:'📕',
};
const _PLAT_COLOR = {
  tiktok:'#ff2d55', instagram:'#e1306c', telegram:'#229ed9',
  facebook:'#1877f2', linkedin:'#0a66c2', twitter:'#1da1f2',
  whatsapp:'#25d366', xiaohongshu:'#ff2442',
};

// ─────────────────────────────────────────────────────────────────
// 注册页面加载器
// ─────────────────────────────────────────────────────────────────
(function _registerStudioPage() {
  const register = () => {
    if (typeof _PAGE_LOADERS !== 'undefined') {
      _PAGE_LOADERS['studio'] = () => _studioEnter();
    }
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', register);
  } else {
    register();
  }
})();

// ─────────────────────────────────────────────────────────────────
// 进入 Studio 页面
// ─────────────────────────────────────────────────────────────────
function _studioEnter() {
  _renderStudioShell();          // 渲染 tab 外壳
  studioTab(_studioCurrentTab);  // 加载当前 tab
  _studioPollStart();            // 启动轮询（job进度）
  _studioFetchDevices();         // 获取在线设备
  // 异步检查就绪度（不阻塞主流程）
  _studioCheckReadiness();
}

// 离开 Studio 页面时停止轮询
document.addEventListener('studioLeave', () => _studioPollStop());

// ─────────────────────────────────────────────────────────────────
// Tab 切换
// ─────────────────────────────────────────────────────────────────
function studioTab(tab) {
  _studioCurrentTab = tab;
  // 更新 tab 按钮样式
  document.querySelectorAll('.studio-tab').forEach(btn => {
    btn.classList.toggle('studio-tab-active', btn.dataset.tab === tab);
  });
  // 显示对应面板
  document.querySelectorAll('.studio-panel').forEach(p => {
    p.style.display = p.id === 'studio-panel-' + tab ? 'block' : 'none';
  });
  // 加载面板内容
  const loaders = {
    dash:      _studioLoadDash,
    review:    _studioLoadReview,
    calendar:  _studioLoadCalendar,
    personas:  _studioLoadPersonas,
    analytics: _studioLoadAnalytics,
    config:    _studioLoadConfig,
  };
  if (loaders[tab]) loaders[tab]();
}

// ─────────────────────────────────────────────────────────────────
// 渲染 Studio 外壳 HTML（只渲染一次）
// ─────────────────────────────────────────────────────────────────
function _renderStudioShell() {
  const page = document.getElementById('page-studio');
  if (!page || page.querySelector('.studio-tabs')) return; // 已渲染

  page.innerHTML = `
<style>
/* ── Studio Tabs ── */
.studio-tabs{display:flex;gap:4px;padding:16px 20px 0;border-bottom:1px solid var(--border);background:var(--bg-main);position:sticky;top:0;z-index:10}
.studio-tab{background:none;border:none;padding:8px 18px;border-radius:8px 8px 0 0;font-size:13px;font-weight:500;cursor:pointer;color:var(--text-muted);border-bottom:2px solid transparent;transition:all .15s}
.studio-tab:hover{color:var(--text-main);background:var(--bg-card)}
.studio-tab-active{color:#6366f1!important;border-bottom-color:#6366f1!important;background:var(--bg-card)!important}
/* ── Studio Panels ── */
.studio-panel{padding:20px;display:none}
/* ── Stat Grid ── */
.studio-stat-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:12px;margin-bottom:20px}
.studio-stat-card{background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px 18px;position:relative;overflow:hidden}
.studio-stat-card::before{content:'';position:absolute;inset:0;opacity:.06;border-radius:12px}
.studio-stat-card.indigo::before{background:#6366f1}.studio-stat-card.green::before{background:#22c55e}
.studio-stat-card.amber::before{background:#f59e0b}.studio-stat-card.rose::before{background:#f43f5e}
.studio-stat-card.sky::before{background:#38bdf8}
.studio-stat-num{font-size:28px;font-weight:700;line-height:1.1}
.studio-stat-label{font-size:11px;color:var(--text-muted);margin-top:4px}
/* ── Generate Form ── */
.studio-gen-card{background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:20px}
.studio-gen-title{font-size:14px;font-weight:600;margin-bottom:14px;display:flex;align-items:center;gap:8px}
.studio-gen-row{display:flex;flex-wrap:wrap;gap:12px;align-items:flex-end}
.studio-field{display:flex;flex-direction:column;gap:4px;flex:1;min-width:140px}
.studio-label{font-size:11px;color:var(--text-muted);font-weight:500}
.studio-select,.studio-input{background:var(--bg-input,#1e1e2e);color:var(--text-main);border:1px solid var(--border);border-radius:8px;padding:7px 10px;font-size:13px;width:100%;box-sizing:border-box}
.studio-plat-checks{display:flex;flex-wrap:wrap;gap:8px}
.studio-plat-check{display:flex;align-items:center;gap:4px;background:var(--bg-input,#1e1e2e);border:1px solid var(--border);border-radius:8px;padding:5px 10px;cursor:pointer;font-size:12px;transition:border-color .15s}
.studio-plat-check:hover{border-color:#6366f1}
.studio-plat-check.checked{border-color:#6366f1;background:#6366f115}
/* ── Review Cards ── */
.studio-review-grid{display:grid;gap:16px}
.studio-review-card{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;overflow:hidden;transition:box-shadow .15s}
.studio-review-card:hover{box-shadow:0 4px 20px #0003}
.studio-review-header{padding:14px 16px 10px;display:flex;justify-content:space-between;align-items:center}
.studio-plat-badge{font-size:11px;padding:3px 10px;border-radius:20px;font-weight:600;color:#fff}
.studio-review-body{padding:0 16px 14px;display:grid;grid-template-columns:1fr 1fr;gap:14px}
.studio-review-images{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
.studio-review-images img{width:100%;aspect-ratio:9/16;object-fit:cover;border-radius:6px;background:var(--bg-input)}
.studio-review-text{font-size:12px;line-height:1.6}
.studio-review-script{color:var(--text-main);white-space:pre-wrap;max-height:200px;overflow-y:auto;background:var(--bg-input,#1a1a2e);padding:10px;border-radius:8px;margin-bottom:8px;font-family:inherit}
.studio-review-caption{color:var(--text-muted);font-size:11px;margin-bottom:6px}
.studio-hashtags{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:10px}
.studio-hashtag{font-size:10px;padding:2px 7px;border-radius:20px;background:#6366f115;color:#6366f1}
.studio-review-actions{padding:12px 16px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.studio-btn-approve{background:#22c55e;color:#fff;border:none;border-radius:8px;padding:7px 18px;font-size:13px;cursor:pointer;font-weight:600}
.studio-btn-approve:hover{background:#16a34a}
.studio-btn-reject{background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:8px;padding:7px 14px;font-size:13px;cursor:pointer}
.studio-btn-reject:hover{border-color:#f43f5e;color:#f43f5e}
.studio-btn-gen{background:#6366f1;color:#fff;border:none;border-radius:8px;padding:8px 20px;font-size:13px;cursor:pointer;font-weight:600}
.studio-btn-gen:hover{background:#4f46e5}
.studio-btn-gen:disabled{opacity:.5;cursor:default}
/* ── Jobs Table ── */
.studio-jobs-table{width:100%;border-collapse:collapse;font-size:12px}
.studio-jobs-table th{color:var(--text-muted);font-weight:500;padding:6px 10px;text-align:left;border-bottom:1px solid var(--border)}
.studio-jobs-table td{padding:8px 10px;border-bottom:1px solid var(--border)4d;vertical-align:middle}
.studio-status{padding:2px 8px;border-radius:20px;font-size:10px;font-weight:600}
.studio-status.generating{background:#f59e0b22;color:#f59e0b}
.studio-status.ready{background:#22c55e22;color:#22c55e}
.studio-status.published{background:#6366f122;color:#6366f1}
.studio-status.failed{background:#f43f5e22;color:#f43f5e}
.studio-status.pending{background:#6b728022;color:#9ca3af}
/* ── Calendar ── */
.studio-cal-grid{display:grid;grid-template-columns:80px repeat(7,1fr);gap:2px;font-size:11px}
.studio-cal-head{background:var(--bg-card);border-radius:6px;padding:8px 6px;text-align:center;font-weight:600;color:var(--text-muted)}
.studio-cal-plat{background:var(--bg-card);border-radius:6px;padding:8px;display:flex;align-items:center;gap:6px;font-weight:500}
.studio-cal-cell{background:var(--bg-card);border-radius:6px;padding:6px;min-height:50px;cursor:pointer;transition:background .15s}
.studio-cal-cell:hover{background:var(--bg-hover,#ffffff08)}
.studio-cal-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin:2px}
/* ── Persona Cards ── */
.studio-persona-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px}
.studio-persona-card{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:18px;position:relative}
.studio-persona-card.active-persona{border-color:#6366f1;box-shadow:0 0 0 2px #6366f130}
.studio-persona-card .active-badge{position:absolute;top:12px;right:12px;background:#6366f1;color:#fff;font-size:10px;padding:2px 8px;border-radius:20px;font-weight:600}
.studio-persona-name{font-size:15px;font-weight:600;margin-bottom:4px}
.studio-persona-meta{font-size:11px;color:var(--text-muted);margin-bottom:12px}
.studio-persona-themes{font-size:11px;color:var(--text-muted);margin-bottom:12px;max-height:60px;overflow:hidden}
.studio-persona-plats{display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px}
/* ── Config ── */
.studio-config-card{background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:20px;margin-bottom:16px}
.studio-config-title{font-size:13px;font-weight:600;margin-bottom:14px;color:var(--text-muted)}
.studio-mode-btns{display:flex;gap:8px}
.studio-mode-btn{flex:1;padding:10px;border:2px solid var(--border);border-radius:10px;background:none;color:var(--text-main);cursor:pointer;font-size:12px;font-weight:500;transition:all .15s}
.studio-mode-btn.selected{border-color:#6366f1;background:#6366f115;color:#6366f1}
/* ── Empty ── */
.studio-empty{text-align:center;padding:48px 20px;color:var(--text-muted);font-size:13px}
.studio-empty-icon{font-size:40px;margin-bottom:12px}
/* ── Progress bar ── */
.studio-progress{height:3px;background:var(--border);border-radius:2px;overflow:hidden;margin-top:6px}
.studio-progress-bar{height:100%;background:#6366f1;border-radius:2px;transition:width .4s}
/* ── Progress Steps ── */
.studio-progress-steps{display:flex;align-items:center;gap:0;margin:10px 0;flex-wrap:nowrap;overflow-x:auto}
.studio-step{display:flex;align-items:center;gap:6px;font-size:11px;white-space:nowrap}
.studio-step-dot{width:22px;height:22px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;flex-shrink:0;font-weight:700;transition:all .3s}
.studio-step-dot.done{background:#22c55e;color:#fff}
.studio-step-dot.active{background:#6366f1;color:#fff;animation:studioSpinPulse 1.4s ease-in-out infinite}
.studio-step-dot.pending{background:var(--bg-input,#1e1e2e);color:var(--text-muted);border:2px solid var(--border)}
.studio-step-line{width:24px;height:2px;background:var(--border);flex-shrink:0;margin:0 2px}
.studio-step-line.done{background:#22c55e}
/* ── Active Job Card ── */
.studio-active-job{background:linear-gradient(135deg,#6366f108,#6366f104);border:1px solid #6366f133;border-radius:12px;padding:14px 16px;margin-bottom:12px;animation:fadeIn .3s}
.studio-active-job-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
/* ── Cost Badge ── */
.studio-cost-badge{display:inline-flex;align-items:center;gap:4px;padding:5px 12px;border-radius:20px;font-size:12px;font-weight:600;background:#f59e0b18;color:#f59e0b;border:1px solid #f59e0b33;margin-top:8px}
/* ── Modal / Dialog ── */
.studio-overlay{position:fixed;inset:0;background:#0009;z-index:1000;display:flex;align-items:center;justify-content:center;animation:fadeIn .15s}
.studio-dialog{background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:24px;max-width:440px;width:92vw;position:relative;box-shadow:0 20px 60px #0006}
.studio-dialog-title{font-size:15px;font-weight:700;margin-bottom:16px;padding-right:28px}
.studio-dialog-close{position:absolute;right:14px;top:12px;background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer;line-height:1;padding:0}
.studio-dialog-footer{display:flex;gap:8px;justify-content:flex-end;margin-top:16px}
/* ── Reason Chips ── */
.studio-reason-chips{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
.studio-reason-chip{padding:5px 12px;border-radius:20px;border:1px solid var(--border);font-size:12px;cursor:pointer;background:none;color:var(--text-muted);transition:all .15s}
.studio-reason-chip:hover,.studio-reason-chip.sel{border-color:#f43f5e;color:#f43f5e;background:#f43f5e15}
/* ── Batch Bar ── */
.studio-batch-bar{display:flex;align-items:center;gap:10px;padding:10px 14px;background:#22c55e0d;border:1px solid #22c55e33;border-radius:10px;margin-bottom:14px;font-size:12px}
@keyframes studioSpinPulse{0%,100%{box-shadow:0 0 0 0 #6366f166}50%{box-shadow:0 0 0 5px #6366f100}}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
/* ── Suggestion Cards ── */
.studio-suggestions{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px;margin-bottom:20px}
.studio-sug-card{background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:16px;cursor:pointer;transition:all .2s;position:relative;overflow:hidden}
.studio-sug-card::before{content:'';position:absolute;inset:0;background:linear-gradient(135deg,var(--sug-color,#6366f1)08,transparent);pointer-events:none}
.studio-sug-card:hover{border-color:var(--sug-color,#6366f1);box-shadow:0 4px 20px #0003;transform:translateY(-2px)}
.studio-sug-rank{position:absolute;top:10px;right:10px;font-size:10px;font-weight:700;padding:2px 7px;border-radius:20px;border:1px solid}
.studio-sug-framework{font-size:10px;color:var(--text-muted);margin-bottom:6px;display:flex;align-items:center;gap:4px}
.studio-sug-title{font-size:13px;font-weight:600;margin-bottom:8px;line-height:1.4}
.studio-sug-hook{font-size:11px;color:var(--text-muted);font-style:italic;border-left:2px solid var(--sug-color,#6366f1);padding-left:8px;margin-bottom:10px;line-height:1.5}
.studio-sug-meta{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px}
.studio-sug-tag{font-size:10px;padding:2px 7px;border-radius:10px;background:var(--bg-input);color:var(--text-muted)}
.studio-sug-actions{display:flex;gap:6px}
.studio-sug-btn{flex:1;padding:6px;border-radius:8px;font-size:11px;font-weight:600;border:none;cursor:pointer;transition:opacity .15s}
.studio-sug-btn:hover{opacity:.85}
/* ── Storyboard ── */
.studio-storyboard{display:flex;flex-direction:column;gap:10px;max-height:60vh;overflow-y:auto;padding-right:4px}
.studio-scene{border:1px solid var(--border);border-radius:10px;padding:12px;background:var(--bg-input,#1a1a2e);transition:border-color .15s}
.studio-scene:hover{border-color:#6366f144}
.studio-scene.is-hook{border-color:#f59e0b55;background:#f59e0b08}
.studio-scene.is-cta{border-color:#22c55e55;background:#22c55e08}
.studio-scene-header{display:flex;align-items:center;gap:8px;margin-bottom:8px}
.studio-scene-badge{font-size:9px;padding:2px 6px;border-radius:10px;font-weight:700}
.studio-scene-ts{font-size:10px;color:var(--text-muted)}
.studio-scene-narration{width:100%;background:transparent;border:1px solid transparent;color:var(--text-main);font-size:12px;resize:none;padding:4px;border-radius:6px;font-family:inherit;line-height:1.6}
.studio-scene-narration:focus{outline:none;border-color:#6366f1;background:var(--bg-card)}
.studio-scene-visual{font-size:10px;color:var(--text-muted);margin-top:4px;padding-top:4px;border-top:1px solid var(--border)}
/* ── Brief Builder ── */
.studio-brief-step{display:none}
.studio-brief-step.active{display:block;animation:fadeIn .2s}
.studio-brief-progress{display:flex;gap:4px;margin-bottom:16px}
.studio-brief-dot{flex:1;height:3px;border-radius:2px;background:var(--border);transition:background .3s}
.studio-brief-dot.done{background:#6366f1}
.studio-framework-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;max-height:260px;overflow-y:auto}
.studio-fw-card{border:1px solid var(--border);border-radius:8px;padding:10px;cursor:pointer;transition:all .15s;font-size:11px}
.studio-fw-card:hover,.studio-fw-card.selected{border-color:#6366f1;background:#6366f115}
.studio-fw-score{font-size:10px;float:right;color:#f59e0b;font-weight:700}
.studio-tone-grid{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px}
.studio-tone-btn{padding:10px 6px;border:1px solid var(--border);border-radius:8px;cursor:pointer;font-size:11px;text-align:center;transition:all .15s;background:none;color:var(--text-main)}
.studio-tone-btn:hover,.studio-tone-btn.selected{border-color:#6366f1;background:#6366f115;color:#6366f1}
</style>

<div class="studio-tabs">
  <button class="studio-tab studio-tab-active" data-tab="dash"   onclick="studioTab('dash')">🎨 工作台</button>
  <button class="studio-tab" data-tab="review"   onclick="studioTab('review')">✅ 审核队列 <span id="studio-review-badge" style="background:#f43f5e;color:#fff;border-radius:20px;font-size:10px;padding:1px 6px;margin-left:4px;display:none"></span></button>
  <button class="studio-tab" data-tab="calendar" onclick="studioTab('calendar')">📅 发布日历</button>
  <button class="studio-tab" data-tab="personas" onclick="studioTab('personas')">🎭 人设配置</button>
  <button class="studio-tab" data-tab="analytics" onclick="studioTab('analytics')">📊 数据大盘</button>
  <button class="studio-tab" data-tab="config"   onclick="studioTab('config')">⚙️ 设置</button>
</div>

<div id="studio-panel-dash"      class="studio-panel"></div>
<div id="studio-panel-review"    class="studio-panel" style="display:none"></div>
<div id="studio-panel-calendar"  class="studio-panel" style="display:none"></div>
<div id="studio-panel-personas"  class="studio-panel" style="display:none"></div>
<div id="studio-panel-analytics" class="studio-panel" style="display:none"></div>
<div id="studio-panel-config"    class="studio-panel" style="display:none"></div>
`;
}

// ─────────────────────────────────────────────────────────────────
// TAB: 工作台 (Dash)
// ─────────────────────────────────────────────────────────────────
async function _studioLoadDash() {
  const el = document.getElementById('studio-panel-dash');
  if (!el) return;
  el.innerHTML = '<div class="studio-empty"><div class="studio-empty-icon">⏳</div>加载中...</div>';

  try {
    // 先获取激活人设，再并行加载其他数据
    const cfgPreload = await api('GET', '/studio/config').catch(() => ({}));
    const activePid  = cfgPreload?.config?.active_persona || sugsPersonaId || 'italy_lifestyle';
    sugsPersonaId    = activePid;   // 同步全局变量供预览/生成用

    const [stats, jobs, sugsData, allPersonasSugs] = await Promise.all([
      api('GET', '/studio/stats').catch(() => ({})),
      api('GET', '/studio/jobs?limit=10').catch(() => ({ jobs: [] })),
      api(`GET`, `/studio/suggestions?persona_id=${encodeURIComponent(activePid)}&n=3`).catch(() => ({ suggestions: [] })),
      api('GET', '/studio/suggestions/all-personas?n=2').catch(() => ({ personas: {} })),
    ]);

    const s = stats || {};
    const jobList = Array.isArray(jobs.jobs) ? jobs.jobs : [];

    // 计算今日数据
    const today = new Date().toISOString().slice(0, 10);
    const todayJobs    = jobList.filter(j => (j.created_at || '').startsWith(today));
    const generatingN  = (s.jobs || {}).generating || 0;
    const pendingN     = (s.jobs || {}).pending || 0;
    const publishedN   = (s.posts || {}).total || 0;

    // 审核徽章
    _studioSetReviewBadge(pendingN);

    // 分离生成中任务和历史任务
    const generatingJobs = jobList.filter(j => j.status === 'generating');
    const historyJobs    = jobList.filter(j => j.status !== 'generating');
    const suggestions    = sugsData.suggestions || [];

    el.innerHTML = `
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
  <div style="font-size:16px;font-weight:700">内容工作室</div>
  <div style="display:flex;gap:8px">
    <button onclick="_studioRefreshSuggestions()" style="background:none;border:1px solid var(--border);border-radius:8px;padding:7px 12px;cursor:pointer;font-size:12px;color:var(--text-muted)">🔄 刷新建议</button>
    <button class="studio-btn-gen" onclick="_studioOpenBriefBuilder()">✨ 自定义生成</button>
  </div>
</div>

<!-- 统计卡片 -->
<div class="studio-stat-grid">
  <div class="studio-stat-card indigo">
    <div class="studio-stat-num">${generatingN}</div>
    <div class="studio-stat-label">⚡ 生成中</div>
  </div>
  <div class="studio-stat-card amber">
    <div class="studio-stat-num">${pendingN}</div>
    <div class="studio-stat-label">📋 待审核</div>
    ${pendingN > 0 ? `<div style="margin-top:8px"><button onclick="studioTab('review')" style="font-size:11px;background:#f59e0b22;color:#f59e0b;border:1px solid #f59e0b44;border-radius:6px;padding:3px 10px;cursor:pointer">立即审核 →</button></div>` : ''}
  </div>
  <div class="studio-stat-card green">
    <div class="studio-stat-num">${todayJobs.length}</div>
    <div class="studio-stat-label">📅 今日任务</div>
  </div>
  <div class="studio-stat-card sky">
    <div class="studio-stat-num">${publishedN}</div>
    <div class="studio-stat-label">🚀 累计发布</div>
  </div>
  <div class="studio-stat-card rose">
    <div class="studio-stat-num">${(s.posts || {}).likes || 0}</div>
    <div class="studio-stat-label">❤️ 累计点赞</div>
  </div>
</div>

<!-- 今日内容建议（系统主动推荐）-->
${suggestions.length > 0 ? `
<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
  <div style="font-size:13px;font-weight:600">💡 今日内容建议 <span style="font-size:11px;font-weight:400;color:var(--text-muted)">— AI基于人设 × 框架库自动生成，点击一键预览</span></div>
</div>
<div class="studio-suggestions" id="studio-sug-grid">
  ${suggestions.map((s, i) => _studioSuggestionCardHTML(s, i)).join('')}
</div>` : `
<div style="background:var(--bg-card);border:1px dashed var(--border);border-radius:12px;padding:16px;margin-bottom:16px;text-align:center;font-size:12px;color:var(--text-muted)">
  💡 <strong>今日内容建议加载中...</strong> — 系统正在基于人设分析最优内容方向
</div>`}

<!-- 多人设运营大盘（仅当有多个人设时显示）-->
${Object.keys(allPersonasSugs.personas || {}).length > 1 ? `
<div style="margin-bottom:20px">
  <div style="font-size:13px;font-weight:600;margin-bottom:10px;display:flex;align-items:center;justify-content:space-between">
    <span>🌍 全人设今日建议 <span style="font-size:11px;font-weight:400;color:var(--text-muted)">${Object.keys(allPersonasSugs.personas).length} 个账号矩阵</span></span>
    <button onclick="_studioOpenAllPersonasBrief()" style="font-size:11px;background:#6366f115;border:1px solid #6366f144;color:#6366f1;border-radius:6px;padding:4px 10px;cursor:pointer">批量生成 →</button>
  </div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px">
    ${Object.entries(allPersonasSugs.personas).map(([pid, pdata]) => {
      const sugs = pdata.suggestions || [];
      const topSug = sugs[0] || {};
      const brief = topSug.brief || {};
      return `
      <div style="border:1px solid var(--border);border-radius:10px;padding:12px;background:var(--bg-card);cursor:pointer" onclick="_studioSwitchPersona('${pid}')">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
          <div style="font-weight:600;font-size:12px">${pdata.persona_name||pid}</div>
          <span style="font-size:10px;padding:2px 6px;border-radius:8px;background:#6366f122;color:#6366f1">${sugs.length}条建议</span>
        </div>
        ${topSug.preview_hook ? `<div style="font-size:11px;color:var(--text-muted);line-height:1.4;margin-bottom:6px">"${topSug.preview_hook.slice(0,60)}…"</div>` : ''}
        <div style="display:flex;gap:6px;flex-wrap:wrap">
          ${sugs.slice(0,2).map(s => `<span style="font-size:10px;padding:2px 7px;border-radius:8px;background:var(--bg-input);color:var(--text-muted)">${(s.framework||{}).name||(s.brief||{}).framework_id||'—'}</span>`).join('')}
        </div>
        <div style="margin-top:8px;display:flex;gap:6px">
          <button onclick="event.stopPropagation();_studioQuickGenerate(${JSON.stringify(brief).replace(/"/g,'&quot;')},null,null,null)"
            style="flex:1;font-size:11px;padding:4px;border-radius:6px;background:#6366f115;border:1px solid #6366f133;color:#6366f1;cursor:pointer">⚡ 生成</button>
          <button onclick="event.stopPropagation();_studioSwitchPersona('${pid}')"
            style="font-size:11px;padding:4px 8px;border-radius:6px;border:1px solid var(--border);background:none;color:var(--text-muted);cursor:pointer">详情 →</button>
        </div>
      </div>`;
    }).join('')}
  </div>
</div>` : ''}

<!-- 活跃生成任务（实时进度）-->
${generatingJobs.length > 0 ? `
<div style="font-size:13px;font-weight:600;margin-bottom:10px;color:#6366f1">⚡ 正在生成 (${generatingJobs.length})</div>
${generatingJobs.map(_studioActiveJobHTML).join('')}` : ''}

<!-- 最近任务 -->
<div style="font-size:13px;font-weight:600;margin-bottom:10px;display:flex;align-items:center;justify-content:space-between">
  <span>最近任务</span>
  <button onclick="_studioLoadDash()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:12px">🔄 刷新</button>
</div>
${historyJobs.length === 0 && generatingJobs.length === 0
  ? '<div class="studio-empty"><div class="studio-empty-icon">📭</div>暂无任务，点击「✨ 生成内容」开始</div>'
  : historyJobs.length === 0 ? '' : `<table class="studio-jobs-table">
      <thead><tr>
        <th>任务ID</th><th>人设</th><th>平台</th><th>状态</th><th>创建时间</th>
      </tr></thead>
      <tbody>
      ${historyJobs.map(j => `
        <tr>
          <td style="font-family:monospace;font-size:11px;color:var(--text-muted)">${(j.job_id||'').slice(0,8)}…</td>
          <td>${j.persona_id || '-'}</td>
          <td>${(j.platforms || []).map(p => (_PLAT_ICON[p] || p)).join(' ')}</td>
          <td><span class="studio-status ${j.status}">${_studioStatusLabel(j.status)}</span></td>
          <td style="color:var(--text-muted)">${_studioFmtTime(j.created_at)}</td>
        </tr>`).join('')}
      </tbody>
    </table>`}
`;
    // 绑定生成按钮
    document.querySelector('#studio-panel-dash .studio-btn-gen')
      ?.addEventListener('click', _studioOpenGenModal);

  } catch(e) {
    el.innerHTML = `<div class="studio-empty">
      <div class="studio-empty-icon">⚠️</div>
      <div style="font-weight:600;margin-bottom:8px">工作台加载失败</div>
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:12px">${e.message}</div>
      <button onclick="_studioLoadDash()" style="font-size:12px;padding:6px 16px;border-radius:8px;background:#6366f1;border:none;color:#fff;cursor:pointer">重试</button>
    </div>`;
  }
}

// 快速生成表单 HTML
function _studioGenFormHTML(personas) {
  const platOptions = ['tiktok','instagram','telegram','facebook','linkedin','twitter','whatsapp','xiaohongshu'];
  return `
<div class="studio-gen-card" id="studio-gen-form">
  <div class="studio-gen-title">✨ 生成内容</div>
  <div class="studio-gen-row" style="margin-bottom:12px">
    <div class="studio-field">
      <label class="studio-label">人设</label>
      <select class="studio-select" id="sgen-persona">
        <option value="">加载中...</option>
      </select>
    </div>
    <div class="studio-field">
      <label class="studio-label">内容类型</label>
      <select class="studio-select" id="sgen-type">
        <option value="slideshow">图文混剪（推荐）</option>
        <option value="video">AI视频</option>
        <option value="text">纯文字</option>
      </select>
    </div>
    <div class="studio-field">
      <label class="studio-label">发布模式</label>
      <select class="studio-select" id="sgen-mode">
        <option value="semi_auto">半自动（人工审核）</option>
        <option value="full_auto">全自动</option>
      </select>
    </div>
  </div>
  <div style="margin-bottom:12px">
    <div class="studio-label" style="margin-bottom:6px">目标平台</div>
    <div class="studio-plat-checks" id="sgen-plats">
      ${platOptions.map(p => `
      <label class="studio-plat-check ${['tiktok','instagram','telegram'].includes(p)?'checked':''}" onclick="_studioTogglePlat(this,'${p}')">
        <input type="checkbox" style="display:none" value="${p}" ${['tiktok','instagram','telegram'].includes(p)?'checked':''}>
        ${_PLAT_ICON[p]||''} ${p}
      </label>`).join('')}
    </div>
  </div>
  <!-- 成本估算徽章 -->
  <div id="sgen-cost-badge" class="studio-cost-badge" style="display:block;margin-bottom:12px">
    <span>💚 无需API Key，免费生成（模板文案）</span>
  </div>
  <div style="display:flex;gap:8px">
    <button class="studio-btn-gen" onclick="_studioSubmitGen()" id="sgen-submit">🚀 开始生成</button>
    <button onclick="this.closest('.studio-dialog,.studio-gen-card')?.remove?.() || document.getElementById('studio-gen-modal')?.remove()" style="background:none;border:1px solid var(--border);border-radius:8px;padding:7px 14px;cursor:pointer;font-size:13px;color:var(--text-muted)">取消</button>
  </div>
</div>`;
}

// 平台勾选切换
function _studioTogglePlat(label, plat) {
  label.classList.toggle('checked');
  const cb = label.querySelector('input[type=checkbox]');
  if (cb) cb.checked = !cb.checked;
  _studioUpdateCostBadge();
}

// 打开生成弹窗（模态）
async function _studioOpenGenModal() {
  // 用模态框方式弹出
  let modal = document.getElementById('studio-gen-modal');
  if (!modal) {
    modal = document.createElement('div');
    modal.id = 'studio-gen-modal';
    modal.style.cssText = 'position:fixed;inset:0;background:#0008;z-index:999;display:flex;align-items:center;justify-content:center';
    modal.onclick = e => { if (e.target === modal) modal.remove(); };
    modal.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:24px;max-width:600px;width:90vw;max-height:85vh;overflow-y:auto;position:relative">
        <button onclick="this.closest('#studio-gen-modal').remove()" style="position:absolute;right:14px;top:12px;background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">&times;</button>
        ${_studioGenFormHTML()}
      </div>`;
    document.body.appendChild(modal);
  }
  modal.style.display = 'flex';

  // 加载人设列表
  try {
    const data = await api('GET', '/studio/personas');
    const sel = document.getElementById('sgen-persona');
    if (sel && data.personas) {
      sel.innerHTML = Object.entries(data.personas).map(([id, p]) =>
        `<option value="${id}">${p.display_name || id}</option>`
      ).join('');
    }
  } catch(e) { /* ignore */ }

  // 绑定内容类型切换 → 更新成本估算
  const typeEl = document.getElementById('sgen-type');
  if (typeEl) typeEl.addEventListener('change', _studioUpdateCostBadge);
  _studioUpdateCostBadge(); // 初始显示
}

// 提交生成任务
async function _studioSubmitGen() {
  const persona  = document.getElementById('sgen-persona')?.value;
  const type     = document.getElementById('sgen-type')?.value;
  const mode     = document.getElementById('sgen-mode')?.value;
  const checked  = [...document.querySelectorAll('#sgen-plats input:checked')].map(c=>c.value);

  if (!persona) { showToast('请选择人设', 'warning'); return; }
  if (checked.length === 0) { showToast('请至少选择一个平台', 'warning'); return; }

  const btn = document.getElementById('sgen-submit');
  if (btn) { btn.disabled = true; btn.textContent = '提交中...'; }

  try {
    const res = await api('POST', '/studio/jobs', {
      persona_id: persona,
      platforms: checked,
      content_type: type,
      mode: mode,
    });
    showToast(`任务已提交 ${(res.job_id||'').slice(0,8)}…`, 'success');
    // 关闭弹窗
    document.getElementById('studio-gen-modal')?.remove();
    // 刷新工作台
    setTimeout(_studioLoadDash, 600);
  } catch(e) {
    showToast('提交失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🚀 开始生成'; }
  }
}

// ─────────────────────────────────────────────────────────────────
// TAB: 审核队列 (Review)
// ─────────────────────────────────────────────────────────────────
async function _studioLoadReview() {
  const el = document.getElementById('studio-panel-review');
  if (!el) return;
  el.innerHTML = '<div class="studio-empty"><div class="studio-empty-icon">⏳</div>加载中...</div>';

  try {
    const data = await api('GET', '/studio/pending');
    const items = data.pending || [];
    _studioSetReviewBadge(items.length);

    if (items.length === 0) {
      el.innerHTML = `
        <div class="studio-empty">
          <div class="studio-empty-icon">✅</div>
          <div>所有内容已处理完毕</div>
          <div style="font-size:11px;margin-top:6px">生成新内容后，待审核项将在此显示</div>
          <button onclick="_studioLoadReview()" style="margin-top:16px;background:none;border:1px solid var(--border);border-radius:8px;padding:7px 18px;cursor:pointer;color:var(--text-muted)">🔄 刷新</button>
        </div>`;
      return;
    }

    const allIds = items.map(i => i.content_id);

    el.innerHTML = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div style="font-size:13px;font-weight:600">待审核内容 <span style="color:#f43f5e">(${items.length})</span></div>
        <div style="display:flex;gap:8px;align-items:center">
          <label style="font-size:11px;color:var(--text-muted)">发布设备:</label>
          <select id="review-device-sel" class="studio-select" style="width:160px">
            <option value="">自动选择</option>
            ${_studioDevices.map(d => `<option value="${d}">${d}</option>`).join('')}
          </select>
          <button onclick="_studioLoadReview()" style="background:none;border:1px solid var(--border);border-radius:8px;padding:5px 10px;cursor:pointer;font-size:12px;color:var(--text-muted)">🔄</button>
        </div>
      </div>

      <!-- 批量操作栏 -->
      <div class="studio-batch-bar">
        <span style="flex:1;color:var(--text-muted)">共 <strong style="color:var(--text-main)">${items.length}</strong> 条待处理</span>
        <button onclick="_studioApproveAll([${allIds.join(',')}])" style="background:#22c55e;color:#fff;border:none;border-radius:8px;padding:6px 16px;font-size:12px;cursor:pointer;font-weight:600">
          ✅ 全部发布
        </button>
        <button onclick="_studioRejectAll([${allIds.join(',')}])" style="background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:8px;padding:6px 12px;font-size:12px;cursor:pointer">
          ✕ 全部拒绝
        </button>
      </div>

      <div class="studio-review-grid">
        ${items.map(item => _studioReviewCardHTML(item)).join('')}
      </div>`;

  } catch(e) {
    el.innerHTML = `<div class="studio-empty"><div class="studio-empty-icon">⚠️</div>加载失败: ${e.message}</div>`;
  }
}

// 渲染单个审核卡片
function _studioReviewCardHTML(item) {
  const platColor = _PLAT_COLOR[item.platform] || '#6366f1';
  const platIcon  = _PLAT_ICON[item.platform]  || '📱';
  const hashtags  = (item.hashtags || []).slice(0, 6);
  const visuals   = item.visual_prompts || [];

  // 图片预览：优先用生成好的图片路径，否则展示AI提示词
  const imgSection = visuals.length > 0 ? `
    <div>
      <div class="studio-label" style="margin-bottom:6px">视觉素材预览</div>
      <div class="studio-review-images" id="imgs-${item.content_id}">
        ${visuals.slice(0,6).map((v, i) =>
          item.image_paths && item.image_paths[i]
            ? `<img src="/studio/media/images/${item.image_paths[i].split('/').pop()}"
                   onerror="this.style.display='none'"
                   title="${v}" loading="lazy">`
            : `<div style="aspect-ratio:9/16;background:var(--bg-input);border-radius:6px;display:flex;align-items:center;justify-content:center;font-size:9px;color:var(--text-muted);padding:6px;text-align:center;line-height:1.3">${v.slice(0,60)}…</div>`
        ).join('')}
      </div>
    </div>` : '';

  return `
<div class="studio-review-card" id="review-card-${item.content_id}">
  <div class="studio-review-header">
    <div style="display:flex;align-items:center;gap:10px">
      <span class="studio-plat-badge" style="background:${platColor}">${platIcon} ${item.platform}</span>
      <span style="font-size:11px;color:var(--text-muted)">#${item.content_id} · ${_studioFmtTime(item.created_at)}</span>
    </div>
    <span style="font-size:11px;color:var(--text-muted)">${item.persona_id || ''}</span>
  </div>

  <div class="studio-review-body">
    ${imgSection || '<div></div>'}
    <div class="studio-review-text">
      <div class="studio-label" style="margin-bottom:6px">脚本内容</div>
      <div class="studio-review-script">${_escHtml(item.script || item.voiceover_text || '（无脚本）')}</div>

      ${item.caption ? `
      <div class="studio-label" style="margin-bottom:4px">发布文案</div>
      <div class="studio-review-caption">${_escHtml(item.caption)}</div>` : ''}

      ${hashtags.length > 0 ? `
      <div class="studio-hashtags">
        ${hashtags.map(h => `<span class="studio-hashtag">${h}</span>`).join('')}
      </div>` : ''}

      ${item.cta_text ? `<div style="font-size:11px;color:#6366f1;font-weight:500">CTA: ${_escHtml(item.cta_text)}</div>` : ''}
    </div>
  </div>

  <div class="studio-review-actions">
    <button class="studio-btn-approve" onclick="_studioApprove(${item.content_id}, this)">
      ✅ 立即发布
    </button>
    <button onclick="_studioSchedule(${item.content_id})" style="background:#6366f122;border:1px solid #6366f1;color:#6366f1;border-radius:8px;padding:7px 14px;font-size:12px;cursor:pointer">
      ⏰ 定时发布
    </button>
    <button class="studio-btn-reject" onclick="_studioReject(${item.content_id}, this)">
      ✕ 拒绝
    </button>
    <span id="review-status-${item.content_id}" style="font-size:11px;color:var(--text-muted)"></span>
  </div>
</div>`;
}

// 审核通过（单条，带重试）
async function _studioApprove(contentId, btn) {
  const device = document.getElementById('review-device-sel')?.value || '';
  const statusEl = document.getElementById(`review-status-${contentId}`);

  btn.disabled = true;
  btn.textContent = '发布中...';
  if (statusEl) statusEl.textContent = '正在发布...';

  try {
    await api('POST', `/studio/approve/${contentId}`, { device_id: device || null });
    showToast('✅ 内容已发布！', 'success');
    document.getElementById(`review-card-${contentId}`)?.remove();
    const data = await api('GET', '/studio/pending').catch(() => ({ pending: [] }));
    _studioSetReviewBadge((data.pending || []).length);
    if ((data.pending || []).length === 0) _studioLoadReview();
  } catch(e) {
    // 发布失败：显示友好错误 + 重试按钮
    const errMsg = e.message?.includes('FAL_KEY') ? '缺少 FAL_KEY，请在设置中配置' :
                   e.message?.includes('ADB')      ? 'ADB 设备未连接，请检查设备' :
                   e.message?.includes('network')  ? '网络超时，请稍后重试' : e.message;
    if (statusEl) statusEl.innerHTML = `<span style="color:#f43f5e">❌ ${errMsg}</span>
      <button onclick="_studioApprove(${contentId},this.previousSibling.previousSibling)"
        style="margin-left:8px;font-size:11px;background:#f43f5e22;color:#f43f5e;border:1px solid #f43f5e44;border-radius:6px;padding:2px 8px;cursor:pointer">
        🔄 重试
      </button>`;
    btn.disabled = false;
    btn.textContent = '✅ 立即发布';
    showToast('发布失败: ' + errMsg, 'error');
  }
}

// 拒绝内容（模态框替换 prompt）
function _studioReject(contentId, _btn) {
  const reasons = ['画面质量差', '文案不合规', '话题不相关', '平台规则限制', '需要重新生成'];
  _studioDialog({
    title: '拒绝内容',
    body: `
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">快选原因：</div>
      <div class="studio-reason-chips" id="reject-chips">
        ${reasons.map(r => `<button class="studio-reason-chip" onclick="_studioSelectReason(this,'${r}')">${r}</button>`).join('')}
      </div>
      <textarea id="reject-reason-input" class="studio-input" rows="2"
        placeholder="自定义拒绝原因（可选）..."
        style="resize:none;margin-top:4px"></textarea>`,
    confirmText: '确认拒绝',
    danger: true,
    onConfirm: async (ov) => {
      const reason = document.getElementById('reject-reason-input')?.value?.trim()
        || document.querySelector('#reject-chips .sel')?.textContent
        || '内容不符合要求';
      ov.remove();
      try {
        await api('POST', `/studio/reject/${contentId}`, { reason });
        showToast('已拒绝', 'info');
        document.getElementById(`review-card-${contentId}`)?.remove();
        const data = await api('GET', '/studio/pending').catch(() => ({ pending: [] }));
        _studioSetReviewBadge((data.pending || []).length);
        if ((data.pending || []).length === 0) _studioLoadReview();
      } catch(e) {
        showToast('操作失败: ' + e.message, 'error');
      }
    },
  });
}

function _studioSelectReason(el, reason) {
  document.querySelectorAll('#reject-chips .sel').forEach(b => b.classList.remove('sel'));
  el.classList.add('sel');
  const input = document.getElementById('reject-reason-input');
  if (input) input.value = reason;
}

// 批量发布
async function _studioApproveAll(ids) {
  if (!ids || ids.length === 0) return;
  _studioDialog({
    title: '批量发布确认',
    body: `<div style="font-size:13px">确认发布全部 <strong style="color:#22c55e">${ids.length}</strong> 条内容？<br><span style="font-size:11px;color:var(--text-muted)">将依次调用 ADB 自动发布，请确保设备在线。</span></div>`,
    confirmText: `✅ 发布全部 ${ids.length} 条`,
    onConfirm: async (ov) => {
      ov.remove();
      const device = document.getElementById('review-device-sel')?.value || '';
      let ok = 0, fail = 0;
      for (const id of ids) {
        try {
          await api('POST', `/studio/approve/${id}`, { device_id: device || null });
          document.getElementById(`review-card-${id}`)?.remove();
          ok++;
        } catch(e) { fail++; }
      }
      showToast(`批量发布完成：成功 ${ok} 条${fail>0?'，失败 '+fail+' 条':''}`, ok>0?'success':'error');
      const data = await api('GET', '/studio/pending').catch(() => ({ pending: [] }));
      _studioSetReviewBadge((data.pending || []).length);
      if ((data.pending || []).length === 0) _studioLoadReview();
    },
  });
}

// 批量拒绝
function _studioRejectAll(ids) {
  if (!ids || ids.length === 0) return;
  _studioDialog({
    title: '批量拒绝',
    body: `<div style="font-size:13px">确认拒绝全部 <strong style="color:#f43f5e">${ids.length}</strong> 条内容？此操作不可恢复。</div>`,
    confirmText: `✕ 拒绝全部`,
    danger: true,
    onConfirm: async (ov) => {
      ov.remove();
      let ok = 0;
      for (const id of ids) {
        try {
          await api('POST', `/studio/reject/${id}`, { reason: '批量拒绝' });
          document.getElementById(`review-card-${id}`)?.remove();
          ok++;
        } catch(e) {}
      }
      showToast(`已拒绝 ${ok} 条内容`, 'info');
      _studioSetReviewBadge(0);
      _studioLoadReview();
    },
  });
}

// ─────────────────────────────────────────────────────────────────
// TAB: 发布日历 (Calendar)
// ─────────────────────────────────────────────────────────────────
async function _studioLoadCalendar() {
  const el = document.getElementById('studio-panel-calendar');
  if (!el) return;
  el.innerHTML = '<div class="studio-empty"><div class="studio-empty-icon">⏳</div>加载中...</div>';

  try {
    const [jobs, stats] = await Promise.all([
      api('GET', '/studio/jobs?limit=50').catch(() => ({ jobs: [] })),
      api('GET', '/studio/stats').catch(() => ({})),
    ]);

    const jobList = jobs.jobs || [];
    const platforms = ['tiktok', 'instagram', 'telegram', 'facebook', 'linkedin', 'twitter', 'whatsapp'];

    // 构建过去7天日期
    const days = [];
    for (let i = 6; i >= 0; i--) {
      const d = new Date();
      d.setDate(d.getDate() - i);
      days.push(d);
    }

    const weekDays = ['日','一','二','三','四','五','六'];

    // 统计每天每平台发布数
    const calData = {}; // calData[date][platform] = count
    jobList.forEach(j => {
      const d = (j.created_at || '').slice(0, 10);
      if (!calData[d]) calData[d] = {};
      (j.platforms || []).forEach(p => {
        calData[d][p] = (calData[d][p] || 0) + 1;
      });
    });

    el.innerHTML = `
<div style="font-size:16px;font-weight:700;margin-bottom:16px">📅 发布日历 <span style="font-size:12px;font-weight:400;color:var(--text-muted)">近7天</span></div>

<div class="studio-cal-grid" style="margin-bottom:20px">
  <!-- 角落 -->
  <div class="studio-cal-head"></div>
  <!-- 日期表头 -->
  ${days.map(d => `
    <div class="studio-cal-head" style="${d.toISOString().slice(0,10)===new Date().toISOString().slice(0,10)?'color:#6366f1':''}">
      <div>${weekDays[d.getDay()]}</div>
      <div style="font-size:16px;font-weight:700">${d.getDate()}</div>
    </div>`).join('')}

  <!-- 各平台行 -->
  ${platforms.map(p => `
    <div class="studio-cal-plat">
      <span>${_PLAT_ICON[p]||'📱'}</span>
      <span>${p}</span>
    </div>
    ${days.map(d => {
      const dk = d.toISOString().slice(0,10);
      const cnt = (calData[dk]||{})[p] || 0;
      const isToday = dk === new Date().toISOString().slice(0,10);
      return `<div class="studio-cal-cell" style="${isToday?'background:#6366f108;border:1px solid #6366f133':''}">
        ${cnt > 0
          ? `<span class="studio-cal-dot" style="background:${_PLAT_COLOR[p]||'#6366f1'}"></span>`.repeat(Math.min(cnt,3))
            + (cnt > 3 ? `<span style="font-size:10px;color:var(--text-muted)"> +${cnt-3}</span>` : '')
          : '<span style="color:var(--border);font-size:10px">-</span>'}
      </div>`;
    }).join('')}
  `).join('')}
</div>

<!-- 总结 -->
<div class="studio-gen-card">
  <div class="studio-gen-title">📊 本周概览</div>
  <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:10px">
    ${platforms.map(p => {
      const total = days.reduce((sum, d) => sum + ((calData[d.toISOString().slice(0,10)]||{})[p]||0), 0);
      return `<div style="text-align:center;background:var(--bg-input,#1a1a2e);border-radius:10px;padding:12px">
        <div style="font-size:20px">${_PLAT_ICON[p]||'📱'}</div>
        <div style="font-size:20px;font-weight:700;color:${_PLAT_COLOR[p]||'#6366f1'}">${total}</div>
        <div style="font-size:10px;color:var(--text-muted)">${p}</div>
      </div>`;
    }).join('')}
  </div>
</div>`;

  } catch(e) {
    el.innerHTML = `<div class="studio-empty"><div class="studio-empty-icon">⚠️</div>加载失败: ${e.message}</div>`;
  }
}

// ─────────────────────────────────────────────────────────────────
// TAB: 人设配置 (Personas)
// ─────────────────────────────────────────────────────────────────
async function _studioLoadPersonas() {
  const el = document.getElementById('studio-panel-personas');
  if (!el) return;
  el.innerHTML = '<div class="studio-empty"><div class="studio-empty-icon">⏳</div>加载中...</div>';

  try {
    const [personaData, configData] = await Promise.all([
      api('GET', '/studio/personas'),
      api('GET', '/studio/config'),
    ]);

    const personas   = personaData.personas || {};
    const activePers = (configData.config || {}).active_persona || '';

    el.innerHTML = `
<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
  <div style="font-size:16px;font-weight:700">🎭 人设配置</div>
  <div style="font-size:11px;color:var(--text-muted)">激活人设: <strong style="color:#6366f1">${activePers}</strong></div>
</div>
<div class="studio-persona-grid">
  ${Object.entries(personas).map(([id, p]) => `
  <div class="studio-persona-card ${id===activePers?'active-persona':''}">
    ${id===activePers?'<div class="active-badge">当前激活</div>':''}
    <div class="studio-persona-name">${p.display_name || id}</div>
    <div class="studio-persona-meta">
      ${p.country||''} · ${p.language||''} · ${p.target_gender||''} · ${p.target_age||''}
    </div>
    <div class="studio-persona-themes">
      ${(p.content_themes||[]).slice(0,3).map(t=>`<div>• ${t}</div>`).join('')}
    </div>
    <div class="studio-persona-plats">
      ${Object.keys(p.platform_strategy||{}).map(pl=>
        `<span class="studio-hashtag">${_PLAT_ICON[pl]||''} ${pl}</span>`).join('')}
    </div>
    <div style="display:flex;gap:8px">
      ${id !== activePers
        ? `<button class="studio-btn-approve" style="font-size:12px;padding:6px 14px" onclick="_studioSetPersona('${id}',this)">激活</button>`
        : `<span style="font-size:12px;color:#22c55e;font-weight:600">✅ 已激活</span>`}
      <button onclick="_studioGenForPersona('${id}')" class="studio-btn-gen" style="font-size:12px;padding:6px 14px">✨ 生成</button>
    </div>
  </div>`).join('')}
</div>`;

  } catch(e) {
    el.innerHTML = `<div class="studio-empty"><div class="studio-empty-icon">⚠️</div>加载失败: ${e.message}</div>`;
  }
}

// 激活人设
async function _studioSetPersona(personaId, btn) {
  btn.disabled = true;
  btn.textContent = '切换中...';
  try {
    await api('POST', '/studio/config', { active_persona: personaId });
    showToast(`已切换到 ${personaId}`, 'success');
    _studioLoadPersonas(); // 刷新
  } catch(e) {
    showToast('切换失败: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = '激活';
  }
}

// 为指定人设生成内容
async function _studioGenForPersona(personaId) {
  await _studioOpenGenModal();
  const sel = document.getElementById('sgen-persona');
  if (sel) sel.value = personaId;
}

// ─────────────────────────────────────────────────────────────────
// 就绪度检查 + 冷启动向导
// ─────────────────────────────────────────────────────────────────

let _readinessData = null;

async function _studioCheckReadiness() {
  try {
    const data = await api('GET', '/studio/readiness');
    _readinessData = data;
    if (data.level === 'not_ready') {
      _studioShowReadinessBanner(data, 'error');
    } else if (data.level === 'partial') {
      _studioShowReadinessBanner(data, 'warning');
    }
    // ready 状态不显示 banner
  } catch(e) {
    // 就绪度检查失败不影响主流程
  }
}

function _studioShowReadinessBanner(data, type) {
  // 在 dash 面板顶部插入 banner（已存在则更新）
  const el = document.getElementById('studio-panel-dash');
  if (!el) return;

  const existing = document.getElementById('studio-readiness-banner');
  if (existing) existing.remove();

  const checks = data.checks || {};
  const failed = Object.entries(checks).filter(([,v]) => !v.ok);
  const blocking = (data.blocking_failed || []);
  const colors = { error: { bg:'#f43f5e15', border:'#f43f5e44', text:'#f43f5e' }, warning: { bg:'#f59e0b15', border:'#f59e0b44', text:'#f59e0b' } };
  const c = colors[type] || colors.warning;
  const icon = type === 'error' ? '🔴' : '🟡';

  const banner = document.createElement('div');
  banner.id = 'studio-readiness-banner';
  banner.style.cssText = `background:${c.bg};border:1px solid ${c.border};border-radius:10px;padding:12px 16px;margin-bottom:16px;font-size:12px`;
  banner.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:flex-start">
      <div>
        <div style="font-weight:600;color:${c.text};margin-bottom:6px">${icon} ${type === 'error' ? '缺少关键配置，无法生成内容' : '部分功能未配置，已降级运行'}</div>
        <div style="display:flex;flex-direction:column;gap:4px">
          ${failed.map(([k,v]) => `
          <div style="display:flex;align-items:flex-start;gap:6px">
            <span style="color:${v.ok?'#22c55e':'#f43f5e'};flex-shrink:0">${v.ok?'✅':'❌'}</span>
            <span style="color:var(--text-main);font-weight:500">${v.label}</span>
            <span style="color:var(--text-muted)">— ${v.hint}</span>
          </div>`).join('')}
        </div>
      </div>
      <div style="display:flex;gap:6px;flex-shrink:0;margin-left:12px">
        <button onclick="studioTab('config')" style="font-size:11px;padding:5px 10px;border-radius:6px;background:#6366f115;border:1px solid #6366f144;color:#6366f1;cursor:pointer">去配置 →</button>
        <button onclick="document.getElementById('studio-readiness-banner').remove()" style="font-size:11px;padding:5px 8px;border-radius:6px;border:1px solid var(--border);background:none;color:var(--text-muted);cursor:pointer">×</button>
      </div>
    </div>
    ${blocking.length > 0 ? `
    <div style="margin-top:10px;padding-top:10px;border-top:1px solid ${c.border}">
      <button onclick="_studioOpenSetupWizard()" style="font-size:12px;font-weight:600;padding:7px 16px;border-radius:8px;background:#6366f1;border:none;color:#fff;cursor:pointer">🚀 一键配置向导</button>
    </div>` : ''}
  `;

  // 插到 dash 面板顶部（在 innerHTML 设置之前，banner 需要在内容渲染后插入）
  // 监听 dash 面板加载完成
  const observer = new MutationObserver(() => {
    const dashEl = document.getElementById('studio-panel-dash');
    if (dashEl && dashEl.children.length > 0) {
      observer.disconnect();
      dashEl.insertBefore(banner, dashEl.firstChild);
    }
  });
  observer.observe(document.getElementById('studio-panel-dash') || document.body, { childList: true, subtree: false });
  // 也直接尝试插入（如果已加载）
  if (el.children.length > 0) el.insertBefore(banner, el.firstChild);
}

// 一键配置向导
async function _studioOpenSetupWizard() {
  const checks = (_readinessData || {}).checks || {};

  const steps = [];
  if (!checks.active_persona?.ok) steps.push({ key: 'persona', label: '选择激活人设', action: "studioTab('personas')" });
  if (!checks.fal_key?.ok) steps.push({ key: 'fal', label: '配置 FAL_KEY（图片/视频生成）', action: "studioTab('config')" });
  if (!checks.llm_key?.ok && !checks.ollama?.ok) steps.push({ key: 'llm', label: '配置 LLM Key 或安装 Ollama', action: "studioTab('config')" });
  if (!checks.cta_link?.ok) steps.push({ key: 'cta', label: '填写 CTA 引流链接', action: "studioTab('config')" });

  const body = `
<div style="font-size:13px;line-height:1.8">
  <div style="font-weight:600;margin-bottom:12px">完成以下步骤，开始运行第一条内容：</div>
  ${steps.map((s,i) => `
  <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
    <div style="width:24px;height:24px;border-radius:50%;background:#6366f1;color:#fff;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;flex-shrink:0">${i+1}</div>
    <div style="flex:1">${s.label}</div>
    <button onclick="${s.action};document.querySelector('.studio-dialog-overlay')?.remove()"
      style="font-size:11px;padding:4px 10px;border-radius:6px;background:#6366f115;border:1px solid #6366f144;color:#6366f1;cursor:pointer">前往 →</button>
  </div>`).join('')}
  ${steps.length === 0 ? '<div style="color:#22c55e;font-weight:600">✅ 所有关键配置已完成！可以开始生成内容了。</div>' : ''}
</div>`;

  await _studioDialog({
    title: '🚀 快速配置向导',
    body,
    confirmText: '我知道了',
    cancelText: null,
  });
}

// ─────────────────────────────────────────────────────────────────
// TAB: 设置 (Config)
// ─────────────────────────────────────────────────────────────────
async function _studioLoadConfig() {
  const el = document.getElementById('studio-panel-config');
  if (!el) return;
  el.innerHTML = '<div class="studio-empty"><div class="studio-empty-icon">⏳</div>加载中...</div>';

  try {
    const data = await api('GET', '/studio/config');
    const cfg  = data.config || {};
    const gen  = cfg.generation || {};
    const pub  = cfg.publishing || {};
    const cta  = cfg.cta || {};

    el.innerHTML = `
<div style="font-size:16px;font-weight:700;margin-bottom:16px">⚙️ 工作室设置</div>

<!-- 发布模式 -->
<div class="studio-config-card">
  <div class="studio-config-title">发布模式</div>
  <div class="studio-mode-btns">
    <button class="studio-mode-btn ${cfg.mode==='semi_auto'?'selected':''}" onclick="_studioSaveMode('semi_auto',this)">
      <div style="font-size:16px">🎯</div>
      <div style="font-weight:600;margin-top:4px">半自动</div>
      <div style="font-size:10px;color:var(--text-muted);margin-top:2px">生成后等待人工审核</div>
    </button>
    <button class="studio-mode-btn ${cfg.mode==='full_auto'?'selected':''}" onclick="_studioSaveMode('full_auto',this)">
      <div style="font-size:16px">🤖</div>
      <div style="font-weight:600;margin-top:4px">全自动</div>
      <div style="font-size:10px;color:var(--text-muted);margin-top:2px">生成后自动发布</div>
    </button>
  </div>
</div>

<!-- CTA 引流 -->
<div class="studio-config-card">
  <div class="studio-config-title">CTA 引流链接</div>
  <div class="studio-gen-row">
    <div class="studio-field">
      <label class="studio-label">主引流链接 (Telegram)</label>
      <input class="studio-input" id="cfg-cta-primary" value="${cta.primary_link||'t.me/yourchannel'}" placeholder="t.me/yourchannel">
    </div>
    <div class="studio-field">
      <label class="studio-label">备用链接 (WhatsApp)</label>
      <input class="studio-input" id="cfg-cta-secondary" value="${cta.secondary_link||''}" placeholder="wa.me/+1234567890">
    </div>
  </div>
  <button class="studio-btn-gen" style="margin-top:12px;font-size:12px;padding:7px 18px" onclick="_studioSaveCTA()">保存链接</button>
</div>

<!-- 内容生成参数 -->
<div class="studio-config-card">
  <div class="studio-config-title">内容生成参数</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:12px">
    <div><span class="studio-label">图片模型</span><div style="margin-top:4px;color:var(--text-main)">${gen.image_model||'fal-ai/flux/schnell'}</div></div>
    <div><span class="studio-label">视频模型</span><div style="margin-top:4px;color:var(--text-main)">${gen.video_model||'wan/v2.6/text-to-video'}</div></div>
    <div><span class="studio-label">AI文案模型</span><div style="margin-top:4px;color:var(--text-main)">${gen.llm_model||'gpt-4o-mini'}</div></div>
    <div><span class="studio-label">每期图片数</span><div style="margin-top:4px;color:var(--text-main)">${gen.images_per_video||6}</div></div>
  </div>
</div>

<!-- 平台状态 -->
<div class="studio-config-card">
  <div class="studio-config-title">启用平台</div>
  <div style="display:flex;flex-wrap:wrap;gap:8px">
    ${['tiktok','instagram','telegram','facebook','linkedin','twitter','whatsapp','xiaohongshu'].map(p => {
      const enabled = (pub.enabled_platforms || cfg.enabled_platforms || ['tiktok','instagram','telegram']).includes(p);
      return `<div style="display:flex;align-items:center;gap:6px;padding:6px 12px;border-radius:20px;background:${enabled?_PLAT_COLOR[p]+'22':'var(--bg-input)'};border:1px solid ${enabled?_PLAT_COLOR[p]:'var(--border)'};font-size:12px">
        ${_PLAT_ICON[p]||''} ${p}
        ${enabled?'<span style="color:'+_PLAT_COLOR[p]+'">✓</span>':'<span style="color:var(--text-muted)">Phase 2</span>'}
      </div>`;
    }).join('')}
  </div>
</div>

<!-- ▼ 内容策略 ▼ -->
<div style="font-size:15px;font-weight:700;margin:28px 0 14px;padding-top:20px;border-top:1px solid var(--border)">🧭 内容策略</div>

<!-- 竞品监控 -->
<div class="studio-config-card" id="cfg-competitor-card">
  <div class="studio-config-title">竞品账号监控 <span style="font-size:10px;color:var(--text-muted);font-weight:400">— 系统自动提取爆款框架，注入每日建议</span></div>
  <div id="cfg-competitor-list" style="display:flex;flex-direction:column;gap:8px;margin-bottom:12px">
    ${(cfg.strategy?.competitors || []).map((c, i) => `
    <div style="display:flex;gap:8px;align-items:center">
      <input class="studio-input" style="flex:1" value="${c.url||''}" placeholder="https://www.tiktok.com/@username" id="cfg-comp-${i}">
      <select class="studio-input" style="width:110px" id="cfg-comp-plat-${i}">
        ${['tiktok','instagram','youtube','xiaohongshu'].map(pl => `<option value="${pl}" ${c.platform===pl?'selected':''}>${_PLAT_ICON[pl]||''} ${pl}</option>`).join('')}
      </select>
      <button onclick="this.parentElement.remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:16px;padding:0 4px">×</button>
    </div>`).join('') || '<div style="font-size:12px;color:var(--text-muted);padding:4px 0">暂无竞品账号，点击下方添加</div>'}
  </div>
  <div style="display:flex;gap:8px">
    <button onclick="_cfgAddCompetitor()" style="font-size:12px;background:none;border:1px dashed var(--border);border-radius:8px;padding:6px 14px;cursor:pointer;color:var(--text-muted)">+ 添加账号</button>
    <button onclick="_cfgSaveStrategy()" class="studio-btn-gen" style="font-size:12px;padding:6px 16px">保存策略</button>
  </div>
</div>

<!-- 内容节奏 -->
<div class="studio-config-card">
  <div class="studio-config-title">发布节奏</div>
  <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;font-size:12px">
    <div>
      <label class="studio-label">每日发布频次</label>
      <select class="studio-input" id="cfg-freq" style="margin-top:4px">
        ${[1,2,3,4,5].map(n => `<option value="${n}" ${(cfg.strategy?.daily_posts||2)===n?'selected':''}>${n} 条/天</option>`).join('')}
      </select>
    </div>
    <div>
      <label class="studio-label">首选发布时段</label>
      <select class="studio-input" id="cfg-hour" style="margin-top:4px">
        ${[6,8,10,12,16,18,20,22].map(h => `<option value="${h}" ${(cfg.strategy?.preferred_hour||20)===h?'selected':''}>${h}:00</option>`).join('')}
      </select>
    </div>
    <div>
      <label class="studio-label">Serper API Key <span style="color:var(--text-muted)">(趋势注入)</span></label>
      <input class="studio-input" id="cfg-serper" style="margin-top:4px" type="password"
        placeholder="${cfg.strategy?.has_serper ? '已配置 ✓' : '输入 key 启用趋势注入'}"
        value="">
    </div>
    <div>
      <label class="studio-label">激活人设</label>
      <select class="studio-input" id="cfg-active-persona" style="margin-top:4px">
        <option value="">-- 加载中 --</option>
      </select>
    </div>
  </div>
  <button onclick="_cfgSaveRhythm()" class="studio-btn-gen" style="margin-top:14px;font-size:12px;padding:6px 18px">保存节奏设置</button>
</div>

<!-- 内容框架库 -->
<div class="studio-config-card">
  <div class="studio-config-title">内容框架库 <span style="font-size:10px;color:var(--text-muted);font-weight:400">— 20种爆款框架，AI自动匹配最优</span></div>
  <div id="cfg-framework-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin-top:12px">
    <div style="grid-column:1/-1;text-align:center;padding:20px;color:var(--text-muted);font-size:12px">⏳ 加载框架库...</div>
  </div>
</div>
`;

    // 异步加载框架库 + 人设列表（非阻塞）
    _cfgLoadFrameworksAndPersonas();

  } catch(e) {
    el.innerHTML = `<div class="studio-empty"><div class="studio-empty-icon">⚠️</div>加载失败: ${e.message}</div>`;
  }
}

async function _studioSaveMode(mode, btn) {
  try {
    await api('POST', '/studio/config', { mode });
    showToast(`已切换为 ${mode==='semi_auto'?'半自动':'全自动'} 模式`, 'success');
    document.querySelectorAll('.studio-mode-btn').forEach(b => b.classList.remove('selected'));
    btn.classList.add('selected');
  } catch(e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

async function _studioSaveCTA() {
  const primary   = document.getElementById('cfg-cta-primary')?.value;
  const secondary = document.getElementById('cfg-cta-secondary')?.value;
  try {
    await api('POST', '/studio/config', { cta_link: primary });
    showToast('CTA链接已保存', 'success');
  } catch(e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

// ─────────────────────────────────────────────────────────────────
// 内容策略配置：竞品 / 节奏 / 框架库
// ─────────────────────────────────────────────────────────────────

// 添加竞品输入行
function _cfgAddCompetitor() {
  const list = document.getElementById('cfg-competitor-list');
  if (!list) return;
  const i = list.querySelectorAll('div[style*="display:flex"]').length;
  const row = document.createElement('div');
  row.style.cssText = 'display:flex;gap:8px;align-items:center';
  row.innerHTML = `
    <input class="studio-input" style="flex:1" placeholder="https://www.tiktok.com/@username" id="cfg-comp-${i}">
    <select class="studio-input" style="width:110px" id="cfg-comp-plat-${i}">
      <option value="tiktok">🎵 tiktok</option>
      <option value="instagram">📸 instagram</option>
      <option value="youtube">▶️ youtube</option>
      <option value="xiaohongshu">📕 小红书</option>
    </select>
    <button onclick="this.parentElement.remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:16px;padding:0 4px">×</button>`;
  list.appendChild(row);
}

// 保存竞品 + 节奏 + Serper 一并提交
async function _cfgSaveStrategy() {
  const list = document.getElementById('cfg-competitor-list');
  const rows = list ? list.querySelectorAll('div[style*="display:flex"]') : [];
  const competitors = [];
  rows.forEach((row, i) => {
    const url  = row.querySelector(`#cfg-comp-${i}`)?.value?.trim() || row.querySelector('input')?.value?.trim();
    const plat = row.querySelector(`#cfg-comp-plat-${i}`)?.value   || row.querySelector('select')?.value;
    if (url) competitors.push({ url, platform: plat || 'tiktok' });
  });
  try {
    await api('POST', '/studio/config', { strategy: { competitors } });
    showToast(`✅ 已保存 ${competitors.length} 个竞品账号`, 'success');
  } catch(e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

// 保存节奏设置
async function _cfgSaveRhythm() {
  const daily_posts    = parseInt(document.getElementById('cfg-freq')?.value  || 2);
  const preferred_hour = parseInt(document.getElementById('cfg-hour')?.value  || 20);
  const serper_key     = document.getElementById('cfg-serper')?.value?.trim() || '';
  const active_persona = document.getElementById('cfg-active-persona')?.value || '';
  const payload = { strategy: { daily_posts, preferred_hour } };
  if (serper_key)     payload.serper_api_key  = serper_key;
  if (active_persona) payload.active_persona  = active_persona;
  try {
    await api('POST', '/studio/config', payload);
    showToast('✅ 节奏设置已保存', 'success');
    if (active_persona) {
      sugsPersonaId = active_persona;
      // 如果当前在工作台，自动刷新建议卡
      if (_studioCurrentTab === 'dash') {
        setTimeout(_studioLoadDash, 300);
      }
    }
  } catch(e) {
    showToast('保存失败: ' + e.message, 'error');
  }
}

// 在 _studioLoadConfig 渲染完毕后异步加载框架库 + 人设列表
async function _cfgLoadFrameworksAndPersonas() {
  // 框架库
  const fGrid = document.getElementById('cfg-framework-grid');
  if (fGrid) {
    try {
      const [fData, perfData] = await Promise.all([
        api('GET', '/studio/frameworks'),
        api('GET', '/studio/framework-perf').catch(() => ({ perf: {} })),
      ]);
      const frameworks = fData.frameworks || [];
      const perf = perfData.perf || {};
      const viralityColor = { very_high: '#22c55e', high: '#6366f1', medium: '#f59e0b', low: '#94a3b8' };
      const viralityLabel = { very_high: '🔥极高', high: '⚡高', medium: '📈中', low: '💤低' };
      fGrid.innerHTML = frameworks.map(f => `
        <div style="border:1px solid var(--border);border-radius:10px;padding:12px;font-size:11px;background:var(--bg-input);transition:all .15s;cursor:default"
             title="${f.description||''}">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:6px">
            <div style="font-weight:600;font-size:12px;color:var(--text-main)">${f.name_zh||f.id}</div>
            <span style="font-size:10px;padding:2px 7px;border-radius:10px;background:${viralityColor[f.virality_score]||'#888'}22;color:${viralityColor[f.virality_score]||'#888'}">${viralityLabel[f.virality_score]||f.virality_score}</span>
          </div>
          <div style="color:var(--text-muted);margin-bottom:6px;line-height:1.4">"${f.hook_template||''}"</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px">
            ${(f.platform_fit||[]).map(pl => `<span style="font-size:10px;padding:1px 6px;border-radius:8px;background:${(_PLAT_COLOR[pl]||'#888')}22;color:${_PLAT_COLOR[pl]||'#888'}">${_PLAT_ICON[pl]||''} ${pl}</span>`).join('')}
          </div>
          <div style="margin-top:6px;color:var(--text-muted)">预估扩散 ${f.estimated_shares||'—'} / 基础调性：${f.best_tone||'—'}</div>
          ${perf[f.id] ? `
          <div style="margin-top:4px;padding-top:4px;border-top:1px solid var(--border);display:flex;gap:8px;font-size:10px;color:var(--text-muted)">
            <span title="审核通过">✅ ${perf[f.id].approved}</span>
            <span title="审核拒绝">❌ ${perf[f.id].rejected}</span>
            <span title="通过率" style="color:${perf[f.id].approval_rate>0.7?'#22c55e':'#f59e0b'}">
              ${Math.round(perf[f.id].approval_rate*100)}%通过
            </span>
          </div>` : ''}
        </div>`).join('');
    } catch(e) {
      fGrid.innerHTML = `<div style="color:var(--text-muted);font-size:12px;padding:8px">框架库加载失败：${e.message}</div>`;
    }
  }

  // 人设下拉（personas 是 {id: {...}} 对象格式）
  const pSel = document.getElementById('cfg-active-persona');
  if (pSel) {
    try {
      const pData = await api('GET', '/studio/personas');
      const current = sugsPersonaId;
      let opts = '';
      const raw = pData.personas;
      if (raw && typeof raw === 'object' && !Array.isArray(raw)) {
        // 对象格式 {persona_id: {display_name, ...}}
        opts = Object.entries(raw).map(([pid, pcfg]) =>
          `<option value="${pid}" ${pid===current?'selected':''}>${pcfg.display_name||pid}</option>`
        ).join('');
      } else if (Array.isArray(raw)) {
        opts = raw.map(p =>
          `<option value="${p.id||p.persona_id}" ${(p.id||p.persona_id)===current?'selected':''}>${p.display_name||p.name||p.id}</option>`
        ).join('');
      }
      pSel.innerHTML = opts || '<option value="">暂无人设</option>';
    } catch(e) {
      pSel.innerHTML = '<option value="">加载失败</option>';
    }
  }
}

// ─────────────────────────────────────────────────────────────────
// 轮询：每5秒更新审核徽章 + 若在工作台则刷新任务列表
// ─────────────────────────────────────────────────────────────────
function _studioPollStart() {
  _studioPollStop();
  _studioPollTimer = setInterval(async () => {
    // 只在 studio 页面激活时轮询
    const page = document.getElementById('page-studio');
    if (!page || !page.classList.contains('active')) {
      _studioPollStop(); return;
    }
    try {
      const data = await api('GET', '/studio/pending').catch(() => null);
      if (data) _studioSetReviewBadge((data.pending||[]).length);

      // 若在工作台且有生成中任务，刷新
      if (_studioCurrentTab === 'dash') {
        const jobs = await api('GET', '/studio/jobs?limit=5').catch(()=>null);
        if (jobs && (jobs.jobs||[]).some(j=>j.status==='generating')) {
          _studioLoadDash();
        }
      }
    } catch(e) { /* ignore */ }
  }, 10000);
}

function _studioPollStop() {
  if (_studioPollTimer) { clearInterval(_studioPollTimer); _studioPollTimer = null; }
}

// ─────────────────────────────────────────────────────────────────
// 获取在线 ADB 设备列表
// ─────────────────────────────────────────────────────────────────
async function _studioFetchDevices() {
  try {
    const data = await api('GET', '/devices');
    _studioDevices = (data.devices || [])
      .filter(d => d.online || d.status === 'online')
      .map(d => d.serial || d.device_id || d.id)
      .filter(Boolean);
  } catch(e) { _studioDevices = []; }
}

// ─────────────────────────────────────────────────────────────────
// 工具函数
// ─────────────────────────────────────────────────────────────────
function _studioSetReviewBadge(count) {
  const badge = document.getElementById('studio-review-badge');
  if (!badge) return;
  if (count > 0) {
    badge.textContent = count;
    badge.style.display = 'inline-block';
  } else {
    badge.style.display = 'none';
  }
}

function _studioStatusLabel(status) {
  const map = {
    generating: '⚡ 生成中',
    pending:    '📋 待审核',
    ready:      '✅ 待发布',
    publishing: '🚀 发布中',
    published:  '✓ 已发布',
    failed:     '❌ 失败',
    rejected:   '✕ 已拒绝',
  };
  return map[status] || status;
}

function _studioFmtTime(iso) {
  if (!iso) return '-';
  try {
    const d = new Date(iso);
    const now = new Date();
    const diff = Math.floor((now - d) / 1000);
    if (diff < 60)  return `${diff}秒前`;
    if (diff < 3600) return `${Math.floor(diff/60)}分钟前`;
    if (diff < 86400) return `${Math.floor(diff/3600)}小时前`;
    return d.toLocaleDateString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
  } catch(e) { return iso.slice(0, 16); }
}

function _escHtml(str) {
  return String(str)
    .replace(/&/g,'&amp;')
    .replace(/</g,'&lt;')
    .replace(/>/g,'&gt;')
    .replace(/"/g,'&quot;');
}

// ─────────────────────────────────────────────────────────────────
// 今日建议卡片渲染
// ─────────────────────────────────────────────────────────────────
function _studioSuggestionCardHTML(s, rank) {
  const viralityColor = { very_high: '#22c55e', high: '#f59e0b', medium: '#6366f1' }[s.estimated_virality] || '#6366f1';
  const viralityLabel = { very_high: '🔥 极高', high: '⚡ 高', medium: '📈 中' }[s.estimated_virality] || s.estimated_virality;
  const platIcons = (s.platform_fit || []).map(p => _PLAT_ICON[p] || p).join('');

  return `
<div class="studio-sug-card" style="--sug-color:${viralityColor}">
  <div class="studio-sug-rank" style="color:${viralityColor};border-color:${viralityColor}33;background:${viralityColor}15">
    ${viralityLabel}
  </div>
  <div class="studio-sug-framework">
    <span style="background:#6366f122;color:#6366f1;padding:1px 6px;border-radius:10px">${s.framework?.name || ''}</span>
    <span>${s.tone_emoji || '✨'} ${s.tone_label || ''}</span>
  </div>
  <div class="studio-sug-title">${_escHtml(s.title || '')}</div>
  <div class="studio-sug-hook">"${_escHtml((s.preview_hook || '').slice(0, 90))}…"</div>
  <div class="studio-sug-meta">
    <span class="studio-sug-tag">${s.hook_type_label || ''}</span>
    <span class="studio-sug-tag">${platIcons} ${(s.platform_fit||[]).join('·')}</span>
    <span class="studio-sug-tag">预估 ${s.why_today?.match(/\+\d+%/)?.[0] || '高'}完播</span>
    <span class="studio-sug-tag">💰 $${s.estimated_cost}</span>
  </div>
  <div class="studio-sug-actions">
    <button class="studio-sug-btn" style="background:#6366f122;color:#6366f1"
      onclick="_studioPreviewSuggestion('${_escHtml(JSON.stringify(s).replace(/'/g,"\\'"))}')">
      👁 故事板预览
    </button>
    <button class="studio-sug-btn" style="background:#22c55e22;color:#22c55e"
      onclick="_studioAcceptSuggestion('${s.suggestion_id}','${_escHtml(JSON.stringify(s.brief).replace(/'/g,"\\'"))}')">
      🚀 一键生成
    </button>
  </div>
</div>`;
}

// 预览建议故事板
async function _studioPreviewSuggestion(sJson) {
  let s;
  try { s = JSON.parse(sJson); } catch(e) { showToast('解析失败', 'error'); return; }

  // 选择平台
  const platform = s.platform_fit?.[0] || 'tiktok';

  _studioDialog({
    title: `👁 故事板预览 — ${s.framework?.name || ''}`,
    cancelText: '关闭',
    confirmText: null,
    body: '<div style="font-size:12px;color:var(--text-muted)">正在生成故事板...</div>',
  });

  // 找到刚创建的 dialog body
  const dlgBody = document.querySelector('.studio-overlay:last-child #' + document.querySelector('.studio-overlay:last-child').id + '-body');
  const ov = document.querySelector('.studio-overlay:last-child');
  if (!ov) return;
  const bodyEl = ov.querySelector('[id$="-body"]');

  try {
    const sb = await api('POST', '/studio/storyboard', {
      brief: s.brief,
      persona_id: s.brief?.source === 'advisor' ? (sugsPersonaId || 'italy_lifestyle') : 'italy_lifestyle',
      platform,
    });
    if (bodyEl) bodyEl.innerHTML = _studioStoryboardHTML(sb, s.brief, platform);
  } catch(e) {
    if (bodyEl) bodyEl.innerHTML = `<div style="color:#f43f5e">故事板生成失败: ${e.message}</div>`;
  }
}

// 渲染故事板 HTML
function _studioStoryboardHTML(sb, brief, platform) {
  const scenes = sb.storyboard || [];
  const platIcon = _PLAT_ICON[platform] || '📱';

  const scenesHTML = scenes.map(sc => `
<div class="studio-scene ${sc.is_hook?'is-hook':''} ${sc.is_cta?'is-cta':''}">
  <div class="studio-scene-header">
    <span class="studio-scene-badge" style="background:${sc.is_hook?'#f59e0b33':sc.is_cta?'#22c55e33':'#6366f133'};color:${sc.is_hook?'#f59e0b':sc.is_cta?'#22c55e':'#6366f1'}">
      ${sc.is_hook ? '🎣 钩子' : sc.is_cta ? '📣 CTA' : `第${sc.index}幕`}
    </span>
    <span class="studio-scene-ts">${sc.timestamp}</span>
    <span style="font-size:10px;color:var(--text-muted);margin-left:auto">✏️ 点击编辑旁白</span>
  </div>
  <textarea class="studio-scene-narration" rows="2" id="scene-narr-${sc.index}"
    placeholder="旁白文字...">${_escHtml(sc.narration || '')}</textarea>
  <div class="studio-scene-visual">🎬 ${sc.scene_description}</div>
</div>`).join('');

  return `
<div style="display:flex;align-items:center;gap:10px;margin-bottom:12px;flex-wrap:wrap">
  <span style="font-size:12px;font-weight:600">${platIcon} ${platform}</span>
  <span style="font-size:11px;color:var(--text-muted)">${sb.duration}秒 · ${sb.aspect_ratio} · ${scenes.length}个场景</span>
  <span style="font-size:11px;padding:2px 8px;background:#22c55e22;color:#22c55e;border-radius:10px">🔥 ${sb.framework_name}</span>
  <span style="font-size:11px;color:var(--text-muted);margin-left:auto">病毒力评分: ${sb.virality_score}/10</span>
</div>
<div class="studio-storyboard">${scenesHTML}</div>
<div style="margin-top:12px;padding:10px;background:#6366f108;border-radius:8px;font-size:11px;color:var(--text-muted)">
  💡 <strong>旁白可直接编辑</strong> — 确认后点击生成，系统将按此脚本制作视频
</div>
<div style="display:flex;gap:8px;margin-top:12px">
  <button onclick="_studioConfirmFromStoryboard(${JSON.stringify(brief).replace(/"/g,'&quot;')}, '${platform}')"
    style="flex:1;background:#6366f1;color:#fff;border:none;border-radius:8px;padding:9px;font-size:13px;cursor:pointer;font-weight:600">
    🚀 确认脚本，生成视频（预估 $${brief?.variance_seed ? '0.02' : '0.02'}）
  </button>
</div>`;
}

// 从故事板确认生成
async function _studioConfirmFromStoryboard(brief, platform) {
  // 收集用户编辑过的旁白
  const editedNarrations = {};
  document.querySelectorAll('[id^="scene-narr-"]').forEach(el => {
    const idx = el.id.replace('scene-narr-', '');
    editedNarrations[idx] = el.value;
  });

  // 把编辑后的旁白注入 brief
  if (brief && editedNarrations) {
    brief.user_narrations = editedNarrations;
  }

  // 关闭故事板弹窗
  document.querySelector('.studio-overlay:last-child')?.remove();

  // 触发生成
  await _studioQuickGenerate(brief, platform);
}

// 接受建议，直接生成
async function _studioAcceptSuggestion(suggestionId, briefJson) {
  let brief;
  try { brief = JSON.parse(briefJson); } catch(e) { showToast('解析失败', 'error'); return; }

  _studioDialog({
    title: '🚀 确认生成',
    body: `<div style="font-size:13px">
      <div style="margin-bottom:8px">将基于以下方向生成内容：</div>
      <div style="background:var(--bg-input);border-radius:8px;padding:10px;font-size:12px">
        <div>📌 主题：${brief.topic || ''}</div>
        <div>🎭 框架：${brief.framework_id || ''}</div>
        <div>⚡ 情绪：${brief.tone || ''}</div>
      </div>
      <div style="margin-top:8px;font-size:11px;color:var(--text-muted)">平台：TikTok + Instagram + Telegram · 预估费用：$0.02</div>
    </div>`,
    confirmText: '🚀 开始生成',
    onConfirm: async (ov) => {
      ov.remove();
      await _studioQuickGenerate(brief, null);
    },
  });
}

// 刷新建议
async function _studioRefreshSuggestions() {
  const grid = document.getElementById('studio-sug-grid');
  if (grid) grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;padding:20px;color:var(--text-muted);font-size:12px">🔄 刷新中...</div>';
  await _studioLoadDash();
}

// 存储当前 persona_id 供预览用
let sugsPersonaId = 'italy_lifestyle';

// 快速生成（使用 brief）
async function _studioQuickGenerate(brief, platform, customPlatforms, scheduleTime) {
  const platforms = customPlatforms || (platform ? [platform] : ['tiktok', 'instagram', 'telegram']);

  // 获取当前激活人设
  let personaId = sugsPersonaId;
  try {
    const cfg = await api('GET', '/studio/config');
    personaId = cfg.config?.active_persona || personaId;
  } catch(e) {}

  try {
    const jobPayload = {
      persona_id: personaId,
      platforms,
      content_type: 'slideshow',
      mode: 'semi_auto',
      content_brief: brief,
    };
    if (scheduleTime) jobPayload.schedule_time = scheduleTime;
    const res = await api('POST', '/studio/jobs', jobPayload);
    showToast(`✅ 任务已提交 ${(res.job_id||'').slice(0,8)}… | 基于「${brief?.framework_id || ''}」框架`, 'success');
    // 订阅 SSE 实时进度
    if (res.job_id) {
      _studioSubscribeJob(res.job_id);
    }
    setTimeout(_studioLoadDash, 800);
  } catch(e) {
    showToast('提交失败: ' + e.message, 'error');
  }
}

// SSE 订阅任务实时进度
function _studioSubscribeJob(jobId) {
  const es = new EventSource(`/studio/jobs/${jobId}/stream`);
  let toastShown = false;

  es.addEventListener('progress', e => {
    try {
      const d = JSON.parse(e.data);
      // 在工作台更新进度（不刷新整页，只更新进度条）
      const progressEl = document.querySelector(`[data-job-id="${jobId}"] .studio-job-progress`);
      if (progressEl) progressEl.style.width = d.progress + '%';
      if (!toastShown && d.n_done > 0) {
        toastShown = true;
        showToast(`⚡ 生成进度 ${d.n_done}/${d.n_total}`, 'info');
      }
    } catch(e) {}
  });

  es.addEventListener('done', e => {
    es.close();
    try {
      const d = JSON.parse(e.data);
      showToast(`✅ 生成完成！${d.n_total} 个平台内容已就绪，请前往审核队列`, 'success');
      // 刷新工作台 + 审核徽章
      if (_studioCurrentTab === 'dash') _studioLoadDash();
      api('GET', '/studio/pending').then(r => _studioSetReviewBadge((r.pending||[]).length)).catch(()=>{});
    } catch(e) {}
  });

  es.addEventListener('error', e => {
    es.close();
    try {
      const d = JSON.parse(e.data || '{}');
      if (d.message && d.message !== 'SSE 超时（5分钟）') {
        showToast('生成出错: ' + d.message, 'error');
      }
    } catch(_) {}
    // 兜底：关闭时刷新一次
    if (_studioCurrentTab === 'dash') setTimeout(_studioLoadDash, 1000);
  });

  // 网络异常兜底
  es.onerror = () => {
    es.close();
    if (_studioCurrentTab === 'dash') setTimeout(_studioLoadDash, 2000);
  };
}

// ─────────────────────────────────────────────────────────────────
// Brief 构建器（自定义生成，替换旧的生成弹窗）
// ─────────────────────────────────────────────────────────────────
let _briefBuilderState = { step: 1, framework: null, tone: 'energetic', description: '' };

async function _studioOpenBriefBuilder() {
  _briefBuilderState = { step: 1, framework: null, tone: 'energetic', description: '' };

  _studioDialog({
    title: '✨ 内容方向引导',
    cancelText: '取消',
    confirmText: null,
    body: await _briefBuilderBodyHTML(1),
  });
}

async function _briefBuilderBodyHTML(step) {
  const totalSteps = 4;
  const dots = Array.from({length: totalSteps}, (_, i) =>
    `<div class="studio-brief-dot ${i < step ? 'done' : ''}"></div>`
  ).join('');

  if (step === 1) {
    // 步骤1：描述主题
    return `
<div class="studio-brief-progress">${dots}</div>
<div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">步骤 1/4 — 描述你今天想做的视频主题</div>
<textarea id="bb-desc" class="studio-input" rows="3"
  placeholder="用一句话描述，例如：&#10;• 晨间五分钟健身，给刚入门的新手&#10;• 地中海饮食食谱，减脂效果好&#10;• 如何30天坚持健身打卡"
  style="resize:none;margin-bottom:12px"></textarea>
<div style="font-size:11px;color:var(--text-muted);margin-bottom:8px">或者从热门方向选：</div>
<div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px" id="bb-quick-topics">
  ${['晨间健身习惯','饮食营养技巧','成功励志故事','健身避坑指南','家庭健身方案','心态转变方法'].map(t =>
    `<button class="studio-reason-chip" onclick="document.getElementById('bb-desc').value='${t}';this.classList.toggle('sel')">${t}</button>`
  ).join('')}
</div>
<div style="display:flex;justify-content:flex-end">
  <button onclick="_briefBuilderNext(2)" style="background:#6366f1;color:#fff;border:none;border-radius:8px;padding:8px 20px;cursor:pointer;font-size:13px;font-weight:600">下一步 →</button>
</div>`;
  }

  if (step === 2) {
    // 步骤2：选择内容框架
    let fws = [];
    try { const r = await api('GET', '/studio/frameworks'); fws = r.frameworks || []; } catch(e) {}
    const fwHTML = fws.slice(0, 10).map(f => `
      <div class="studio-fw-card ${_briefBuilderState.framework === f.id ? 'selected' : ''}"
           onclick="_selectFramework('${f.id}', this)">
        <span class="studio-fw-score">★${f.virality_score}</span>
        <div style="font-weight:600;margin-bottom:3px">${f.name}</div>
        <div style="color:var(--text-muted)">${f.description}</div>
      </div>`).join('');
    return `
<div class="studio-brief-progress">${dots}</div>
<div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">步骤 2/4 — 选择内容框架（影响视频结构和传播力）</div>
<div class="studio-framework-grid">${fwHTML}</div>
<div style="display:flex;justify-content:space-between;margin-top:12px">
  <button onclick="_briefBuilderBack(1)" style="background:none;border:1px solid var(--border);border-radius:8px;padding:7px 14px;cursor:pointer;font-size:12px;color:var(--text-muted)">← 返回</button>
  <button onclick="_briefBuilderNext(3)" style="background:#6366f1;color:#fff;border:none;border-radius:8px;padding:8px 20px;cursor:pointer;font-size:13px;font-weight:600">下一步 →</button>
</div>`;
  }

  if (step === 3) {
    // 步骤3：选择情绪风格 + 预览
    const tones = [
      {id:'energetic', label:'⚡ 激励型', desc:'让人立刻想行动'},
      {id:'educational', label:'📚 教育型', desc:'干货实用'},
      {id:'inspiring', label:'🔥 励志型', desc:'情感共鸣'},
      {id:'casual', label:'😄 轻松型', desc:'好玩易分享'},
      {id:'emotional', label:'💙 情感型', desc:'深层连接'},
    ];
    return `
<div class="studio-brief-progress">${dots}</div>
<div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">步骤 3/4 — 选择内容风格</div>
<div class="studio-tone-grid" style="margin-bottom:16px">
  ${tones.map(t => `
    <button class="studio-tone-btn ${_briefBuilderState.tone === t.id ? 'selected' : ''}"
            onclick="_selectTone('${t.id}', this)">
      <div>${t.label}</div>
      <div style="font-size:10px;color:var(--text-muted);margin-top:2px">${t.desc}</div>
    </button>`).join('')}
</div>
<div style="background:#6366f108;border-radius:8px;padding:10px;font-size:11px;margin-bottom:12px">
  <div style="font-weight:600;margin-bottom:4px">📋 内容摘要</div>
  <div>主题：${_briefBuilderState.description || '（未填写）'}</div>
  <div>框架：${_briefBuilderState.framework || '（未选择）'}</div>
  <div>平台：TikTok + Instagram + Telegram</div>
  <div>预估费用：$0.02</div>
</div>
<div style="display:flex;gap:8px">
  <button onclick="_briefBuilderBack(2)" style="background:none;border:1px solid var(--border);border-radius:8px;padding:7px 14px;cursor:pointer;font-size:12px;color:var(--text-muted)">← 返回</button>
  <button onclick="_briefBuilderNext(4)" style="flex:1;background:#6366f1;color:#fff;border:none;border-radius:8px;padding:8px;cursor:pointer;font-size:13px;font-weight:600">下一步 →</button>
</div>`;
  }

  if (step === 4) {
    const allPlatforms = ['tiktok','instagram','telegram','facebook','linkedin','twitter','whatsapp'];
    const platIcons = {tiktok:'🎵',instagram:'📸',telegram:'✈️',facebook:'📘',linkedin:'💼',twitter:'🐦',whatsapp:'💬'};
    const activePlatforms = ['tiktok','instagram','telegram'];
    const platBtns = allPlatforms.map(p => {
      const active = activePlatforms.includes(p);
      return `<button id="bb-plat-${p}" onclick="_bbTogglePlat('${p}',this)"
        style="padding:8px 14px;border-radius:20px;border:1px solid ${active?'#6366f1':'var(--border)'};background:${active?'#6366f115':'none'};color:${active?'#6366f1':'var(--text-muted)'};cursor:pointer;font-size:12px" data-selected="${active?'1':'0'}">${platIcons[p]||''} ${p}</button>`;
    }).join('');
    const timePresets = [['立即发布',''],['今晚20:00',_bbTimeStr(20,0)],['明早08:00',_bbTimeStr(8,1)],['明晚20:00',_bbTimeStr(20,1)]];
    const timeBtns = timePresets.map(([label,val]) =>
      `<button onclick="_bbSetTime('${val}',this)" style="font-size:11px;padding:5px 10px;border-radius:6px;border:1px solid var(--border);background:none;cursor:pointer;color:var(--text-muted)">${label}</button>`
    ).join('');
    return `
<div class="studio-brief-progress">${dots}</div>
<div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">步骤 4/4 — 选择发布平台和时间</div>
<div style="margin-bottom:16px">
  <div class="studio-label" style="margin-bottom:8px">选择发布平台</div>
  <div style="display:flex;flex-wrap:wrap;gap:8px" id="bb-plat-grid">${platBtns}</div>
</div>
<div>
  <div class="studio-label" style="margin-bottom:8px">发布时间 <span style="font-weight:400;color:var(--text-muted)">(可选，留空立即发布)</span></div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px">${timeBtns}</div>
  <input type="datetime-local" id="bb-schedule-time" class="studio-input" style="max-width:240px" value="">
</div>
<div style="display:flex;gap:8px;margin-top:16px">
  <button onclick="_briefBuilderBack(3)" style="background:none;border:1px solid var(--border);border-radius:8px;padding:7px 14px;cursor:pointer;font-size:12px;color:var(--text-muted)">← 返回</button>
  <button onclick="_briefBuilderPreview()" style="flex:1;background:#6366f122;color:#6366f1;border:1px solid #6366f1;border-radius:8px;padding:8px;cursor:pointer;font-size:12px">👁 故事板预览</button>
  <button onclick="_briefBuilderSubmit()" style="flex:1;background:#6366f1;color:#fff;border:none;border-radius:8px;padding:8px;cursor:pointer;font-size:13px;font-weight:600">🚀 生成</button>
</div>`;
  }
  return '';
}

function _selectFramework(id, el) {
  _briefBuilderState.framework = id;
  document.querySelectorAll('.studio-fw-card').forEach(c => c.classList.remove('selected'));
  el.classList.add('selected');
}

function _selectTone(id, el) {
  _briefBuilderState.tone = id;
  document.querySelectorAll('.studio-tone-btn').forEach(b => b.classList.remove('selected'));
  el.classList.add('selected');
}

async function _briefBuilderNext(step) {
  if (step === 2) {
    _briefBuilderState.description = document.getElementById('bb-desc')?.value?.trim() || '';
    if (!_briefBuilderState.description) { showToast('请描述视频主题', 'warning'); return; }
  }
  _briefBuilderState.step = step;
  const bodyEl = document.querySelector('.studio-overlay:last-child [id$="-body"]');
  if (bodyEl) bodyEl.innerHTML = await _briefBuilderBodyHTML(step);
}

async function _briefBuilderBack(step) {
  _briefBuilderState.step = step;
  const bodyEl = document.querySelector('.studio-overlay:last-child [id$="-body"]');
  if (bodyEl) bodyEl.innerHTML = await _briefBuilderBodyHTML(step);
}

async function _briefBuilderPreview() {
  const brief = await _buildBriefFromState();
  if (!brief) return;
  document.querySelector('.studio-overlay:last-child')?.remove();
  // 构造伪 suggestion 对象以复用 preview 函数
  const sugObj = {
    brief,
    framework: { name: _briefBuilderState.framework || '自定义' },
    platform_fit: ['tiktok'],
  };
  await _studioPreviewSuggestion(JSON.stringify(sugObj));
}

async function _briefBuilderSubmit() {
  const brief = await _buildBriefFromState();
  if (!brief) return;
  document.querySelector('.studio-overlay:last-child')?.remove();
  // 读取第4步的平台选择
  const selectedPlatforms = [];
  document.querySelectorAll('#bb-plat-grid button[data-selected="1"]').forEach(btn => {
    const id = btn.id.replace('bb-plat-','');
    if (id) selectedPlatforms.push(id);
  });
  const platforms = selectedPlatforms.length ? selectedPlatforms : ['tiktok','instagram','telegram'];
  const scheduleTime = document.getElementById('bb-schedule-time')?.value || null;
  await _studioQuickGenerate(brief, null, platforms, scheduleTime);
}

function _bbTogglePlat(plat, btn) {
  const sel = btn.dataset.selected === '1';
  btn.dataset.selected = sel ? '0' : '1';
  btn.style.borderColor = sel ? 'var(--border)' : '#6366f1';
  btn.style.background  = sel ? 'none' : '#6366f115';
  btn.style.color       = sel ? 'var(--text-muted)' : '#6366f1';
}

function _bbTimeStr(hour, daysAhead) {
  const d = new Date();
  d.setDate(d.getDate() + daysAhead);
  d.setHours(hour, 0, 0, 0);
  return d.toISOString().slice(0, 16);
}

function _bbSetTime(val, btn) {
  document.getElementById('bb-schedule-time').value = val;
  btn.style.borderColor = '#6366f1';
  btn.style.color = '#6366f1';
}

async function _buildBriefFromState() {
  if (!_briefBuilderState.description) { showToast('请描述视频主题', 'warning'); return null; }
  try {
    const res = await api('POST', '/studio/brief-from-text', {
      description: _briefBuilderState.description,
      persona_id: sugsPersonaId || 'italy_lifestyle',
      tone: _briefBuilderState.tone,
      framework_id: _briefBuilderState.framework || null,
    });
    return res.brief;
  } catch(e) {
    showToast('Brief生成失败: ' + e.message, 'error');
    return null;
  }
}

// ─────────────────────────────────────────────────────────────────
// 通用模态弹窗（替换所有 prompt() / alert()）
// opts: { title, body, confirmText, cancelText, onConfirm, danger }
// ─────────────────────────────────────────────────────────────────
function _studioDialog(opts) {
  const id = 'sdlg-' + Date.now();
  const ov = document.createElement('div');
  ov.className = 'studio-overlay';
  ov.id = id;
  const danger = opts.danger ? 'background:#f43f5e;' : 'background:#6366f1;';
  ov.innerHTML = `
<div class="studio-dialog">
  <button class="studio-dialog-close" onclick="document.getElementById('${id}').remove()">&times;</button>
  <div class="studio-dialog-title">${opts.title || ''}</div>
  <div id="${id}-body">${opts.body || ''}</div>
  <div class="studio-dialog-footer">
    ${opts.cancelText !== null ? `<button onclick="document.getElementById('${id}').remove()" style="background:none;border:1px solid var(--border);border-radius:8px;padding:7px 16px;cursor:pointer;font-size:13px;color:var(--text-muted)">${opts.cancelText||'取消'}</button>` : ''}
    ${opts.confirmText ? `<button id="${id}-confirm" style="${danger}color:#fff;border:none;border-radius:8px;padding:7px 18px;cursor:pointer;font-size:13px;font-weight:600">${opts.confirmText}</button>` : ''}
  </div>
</div>`;
  ov.addEventListener('click', e => { if (e.target === ov) ov.remove(); });
  document.body.appendChild(ov);
  if (opts.onConfirm) {
    const btn = document.getElementById(`${id}-confirm`);
    if (btn) btn.addEventListener('click', () => opts.onConfirm(ov, id));
  }
  return ov;
}

// ─────────────────────────────────────────────────────────────────
// 成本估算
// ─────────────────────────────────────────────────────────────────
function _studioCostEstimate(contentType, platCount) {
  const count = Math.max(1, platCount);
  const costs = { slideshow: 0.02, video: 1.20, text: 0.005 };
  const labels = { slideshow: '图文混剪(6图+TTS)', video: 'AI视频(15秒)', text: '纯文字' };
  const unit = costs[contentType] ?? 0.02;
  const total = (unit * count).toFixed(3);
  return { unit, total: parseFloat(total), label: labels[contentType] || contentType, count };
}

function _studioUpdateCostBadge() {
  const typeEl = document.getElementById('sgen-type');
  const badgeEl = document.getElementById('sgen-cost-badge');
  if (!typeEl || !badgeEl) return;
  const checked = document.querySelectorAll('#sgen-plats input:checked').length;
  const est = _studioCostEstimate(typeEl.value, checked);
  const isFree = est.unit < 0.01;
  badgeEl.innerHTML = isFree
    ? `<span>💚 无需API Key，免费生成</span>`
    : `<span>💰 预估费用: $${est.total} (${est.count}平台 × $${est.unit}/${est.label})</span>`;
  badgeEl.style.background = isFree ? '#22c55e18' : '#f59e0b18';
  badgeEl.style.color       = isFree ? '#22c55e'   : '#f59e0b';
  badgeEl.style.borderColor = isFree ? '#22c55e33' : '#f59e0b33';
}

// ─────────────────────────────────────────────────────────────────
// 生成进度步骤渲染（工作台活跃任务区）
// ─────────────────────────────────────────────────────────────────
function _studioActiveJobHTML(job) {
  // 根据任务 status 和 updated_at 推断当前步骤
  const steps = [
    { key: 'script',  label: '文案' },
    { key: 'image',   label: '图片' },
    { key: 'video',   label: '合成' },
    { key: 'queue',   label: '待审' },
  ];

  const isGenerating = job.status === 'generating';
  // 若无 current_step 字段，通过时间粗估步骤（每步约25秒）
  const elapsed = job.updated_at ? Math.floor((Date.now() - new Date(job.updated_at)) / 1000) : 0;
  const stepIdx = isGenerating ? Math.min(Math.floor(elapsed / 25), 2) : (job.status === 'pending' ? 3 : -1);

  const stepsHTML = steps.map((s, i) => {
    let dotClass = 'pending', icon = String(i+1);
    if (i < stepIdx)       { dotClass = 'done';   icon = '✓'; }
    else if (i === stepIdx) { dotClass = 'active';  icon = '⚡'; }
    const lineClass = i < stepIdx ? 'done' : '';
    return `
      <div class="studio-step">
        <div class="studio-step-dot ${dotClass}">${icon}</div>
        <span style="color:${dotClass==='done'?'#22c55e':dotClass==='active'?'#6366f1':'var(--text-muted)'}">${s.label}</span>
      </div>
      ${i < steps.length-1 ? `<div class="studio-step-line ${lineClass}"></div>` : ''}`;
  }).join('');

  const age = _studioFmtTime(job.created_at);
  const plats = (job.platforms||[]).map(p => (_PLAT_ICON[p]||p)).join(' ');

  return `
<div class="studio-active-job">
  <div class="studio-active-job-header">
    <div style="display:flex;align-items:center;gap:8px">
      <span style="font-size:12px;font-weight:600">${job.persona_id}</span>
      <span style="font-size:11px;color:var(--text-muted)">${plats}</span>
    </div>
    <span style="font-size:11px;color:var(--text-muted)">${age}</span>
  </div>
  <div class="studio-progress-steps">${stepsHTML}</div>
  ${isGenerating ? `<div class="studio-progress" style="margin-top:6px"><div class="studio-progress-bar" style="width:${Math.min(((stepIdx+1)/4)*100,95)}%"></div></div>` : ''}
</div>`;
}

// ─────────────────────────────────────────────────────────────────
// 定时发布
// ─────────────────────────────────────────────────────────────────
function _studioSchedule(contentId) {
  // 推荐时间：今天 20:00，如已过则明天
  const now = new Date();
  const rec = new Date(now);
  rec.setHours(20, 0, 0, 0);
  if (rec <= now) rec.setDate(rec.getDate() + 1);

  // datetime-local 格式（本地时间）
  const toLocal = d => {
    const pad = n => String(n).padStart(2,'0');
    return `${d.getFullYear()}-${pad(d.getMonth()+1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  };

  // 快捷时间选项
  const shortcuts = [
    { label: '今晚 20:00', hours: 20, offset: 0 },
    { label: '明早 08:00', hours: 8,  offset: 1 },
    { label: '明晚 20:00', hours: 20, offset: 1 },
    { label: '后天 12:00', hours: 12, offset: 2 },
  ];

  _studioDialog({
    title: '⏰ 定时发布',
    body: `
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:10px">
        当前时间: ${now.toLocaleString('zh-CN')} (本地)
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">
        ${shortcuts.map(s => {
          const d = new Date(); d.setDate(d.getDate()+s.offset); d.setHours(s.hours,0,0,0);
          if (d <= now) return '';
          return `<button class="studio-reason-chip" onclick="document.getElementById('sched-dt').value='${toLocal(d)}';document.querySelectorAll('#sched-shortcuts .sel').forEach(b=>b.classList.remove('sel'));this.classList.add('sel')" id="sched-shortcuts">${s.label}</button>`;
        }).join('')}
      </div>
      <label class="studio-label" style="display:block;margin-bottom:6px">自定义时间（本地时间）</label>
      <input type="datetime-local" id="sched-dt" class="studio-input"
        value="${toLocal(rec)}"
        min="${toLocal(new Date(now.getTime()+60000))}"
        style="font-size:13px">
      <div style="font-size:11px;color:var(--text-muted);margin-top:8px">
        💡 系统每60秒自动检查并触发定时发布
      </div>`,
    confirmText: '⏰ 确认排期',
    onConfirm: async (ov) => {
      const val = document.getElementById('sched-dt')?.value;
      if (!val) { showToast('请选择发布时间', 'warning'); return; }
      const localDate = new Date(val);
      if (isNaN(localDate.getTime()) || localDate <= new Date()) {
        showToast('请选择未来的时间', 'warning'); return;
      }
      const utcStr = localDate.toISOString();
      ov.remove();
      try {
        await api('POST', `/studio/schedule/${contentId}`, { publish_at_utc: utcStr });
        showToast(`⏰ 已排期：${localDate.toLocaleString('zh-CN')}`, 'success');
        const card = document.getElementById(`review-card-${contentId}`);
        if (card) {
          card.style.opacity = '0.6';
          const actionsEl = card.querySelector('.studio-review-actions');
          if (actionsEl) actionsEl.innerHTML =
            `<span style="color:#6366f1;font-size:12px;font-weight:500">⏰ 已排期：${localDate.toLocaleString('zh-CN')}</span>
             <button onclick="_studioCancelSchedule(${contentId},this)" style="font-size:11px;background:none;border:1px solid var(--border);border-radius:6px;padding:2px 8px;cursor:pointer;color:var(--text-muted)">取消排期</button>`;
        }
      } catch(e) {
        showToast('定时设置失败: ' + e.message, 'error');
      }
    },
  });
}

async function _studioCancelSchedule(contentId, btn) {
  try {
    await api('POST', `/studio/schedule/${contentId}`, { publish_at_utc: null });
    showToast('已取消定时发布', 'info');
    _studioLoadReview();
  } catch(e) {
    showToast('取消失败: ' + e.message, 'error');
  }
}

// ─────────────────────────────────────────────────────────────────
// TAB: 数据大盘 (Analytics)
// ─────────────────────────────────────────────────────────────────
async function _studioLoadAnalytics() {
  const el = document.getElementById('studio-panel-analytics');
  if (!el) return;
  el.innerHTML = '<div class="studio-empty"><div class="studio-empty-icon">⏳</div>加载跨系统数据...</div>';

  try {
    const [overview, funnel, studioTL, leadsTL] = await Promise.all([
      api('GET', '/analytics/cross-overview').catch(()=>({})),
      api('GET', '/analytics/studio-funnel?days=30').catch(()=>({})),
      api('GET', '/analytics/studio-timeline?days=14').catch(()=>({})),
      api('GET', '/analytics/leads-timeline?days=14').catch(()=>({})),
    ]);

    const cs  = overview.content_studio  || {};
    const tk  = overview.tiktok_funnel   || {};
    const fn  = funnel.funnel            || {};
    const cr  = funnel.conversion_rates  || {};
    const pp  = funnel.posts_by_platform || {};

    el.innerHTML = `
<div style="font-size:16px;font-weight:700;margin-bottom:16px">📊 两系统数据大盘</div>

<!-- 大盘 KPI -->
<div class="studio-stat-grid" style="grid-template-columns:repeat(auto-fill,minmax(140px,1fr))">
  <div class="studio-stat-card indigo">
    <div class="studio-stat-num">${cs.posts_this_week||0}</div>
    <div class="studio-stat-label">📤 本周发布内容</div>
  </div>
  <div class="studio-stat-card green">
    <div class="studio-stat-num">${tk.new_leads_week||0}</div>
    <div class="studio-stat-label">👥 本周新增 Leads</div>
  </div>
  <div class="studio-stat-card amber">
    <div class="studio-stat-num">${tk.dms_sent_week||0}</div>
    <div class="studio-stat-label">💬 本周 DM 发送</div>
  </div>
  <div class="studio-stat-card sky">
    <div class="studio-stat-num">${tk.replies_this_week||0}</div>
    <div class="studio-stat-label">↩️ 本周 DM 回复</div>
  </div>
  <div class="studio-stat-card rose">
    <div class="studio-stat-num">${cs.pending_review||0}</div>
    <div class="studio-stat-label">📋 待审核内容</div>
  </div>
  <div class="studio-stat-card indigo">
    <div class="studio-stat-num">${tk.new_leads_month||0}</div>
    <div class="studio-stat-label">📅 本月新增 Leads</div>
  </div>
</div>

<!-- 30天转化漏斗 -->
<div class="studio-gen-card" style="margin-bottom:20px">
  <div class="studio-gen-title">🔻 30天转化漏斗</div>
  <div style="display:flex;flex-direction:column;gap:8px">
    ${_funnelBar('📤 内容发布', fn.content_published||0, fn.content_published||1, '#6366f1')}
    ${_funnelBar('👁 总曝光', fn.total_views||0, fn.content_published||1, '#38bdf8')}
    ${_funnelBar('👥 新增关注', fn.new_leads_followed||0, fn.total_views||fn.content_published||1, '#22c55e')}
    ${_funnelBar('💬 DM 发送', fn.dms_sent||0, fn.new_leads_followed||fn.content_published||1, '#f59e0b')}
    ${_funnelBar('↩️ DM 回复', fn.dms_replied||0, fn.dms_sent||fn.content_published||1, '#f43f5e')}
    ${_funnelBar('🎯 CRM 入站', fn.crm_inbound_msgs||0, fn.dms_replied||fn.content_published||1, '#a78bfa')}
  </div>
  <div style="margin-top:14px;display:flex;flex-wrap:wrap;gap:10px;font-size:11px;color:var(--text-muted)">
    <span>关注→DM: <b>${cr.follow_to_dm||0}%</b></span>
    <span>DM→回复: <b>${cr.dm_to_reply||0}%</b></span>
    <span>回复→CRM: <b>${cr.reply_to_crm||0}%</b></span>
  </div>
</div>

<!-- 各平台发布数 -->
<div class="studio-gen-card" style="margin-bottom:20px">
  <div class="studio-gen-title">📱 各平台内容发布（30天）</div>
  <div style="display:flex;flex-wrap:wrap;gap:10px">
    ${Object.entries(pp).length === 0
      ? '<div style="color:var(--text-muted);font-size:12px">暂无发布数据</div>'
      : Object.entries(pp).map(([p,c]) => `
          <div style="text-align:center;padding:12px 18px;background:var(--bg-input);border-radius:10px;min-width:80px">
            <div style="font-size:22px">${_PLAT_ICON[p]||'📱'}</div>
            <div style="font-size:20px;font-weight:700;color:${_PLAT_COLOR[p]||'#6366f1'}">${c}</div>
            <div style="font-size:10px;color:var(--text-muted)">${p}</div>
          </div>`).join('')}
  </div>
</div>

<!-- 时间线对比 -->
<div class="studio-gen-card">
  <div class="studio-gen-title">📈 近14天趋势对比</div>
  <div id="studio-analytics-chart" style="width:100%;height:200px;position:relative">
    ${_renderMiniTimeline(studioTL, leadsTL)}
  </div>
</div>`;

  } catch(e) {
    el.innerHTML = `<div class="studio-empty"><div class="studio-empty-icon">⚠️</div>加载失败: ${e.message}</div>`;
  }
}

// 漏斗条
function _funnelBar(label, value, base, color) {
  const pct = base > 0 ? Math.min(100, Math.round(value / base * 100)) : 0;
  return `
  <div style="display:flex;align-items:center;gap:10px">
    <div style="width:130px;font-size:12px;text-align:right;color:var(--text-muted)">${label}</div>
    <div style="flex:1;background:var(--bg-input);border-radius:4px;height:22px;position:relative">
      <div style="width:${pct}%;background:${color};height:100%;border-radius:4px;transition:width .5s"></div>
      <span style="position:absolute;left:8px;top:3px;font-size:11px;font-weight:600;color:#fff;text-shadow:0 1px 2px #0006">${value.toLocaleString()}</span>
    </div>
    <div style="width:36px;font-size:11px;color:var(--text-muted);text-align:right">${pct}%</div>
  </div>`;
}

// 简易时间线 SVG
function _renderMiniTimeline(studioTL, leadsTL) {
  const postsByDay  = {};
  const leadsByDay  = {};
  (studioTL.daily_total || []).forEach(r => postsByDay[r.date] = r.posts);
  (leadsTL.leads_by_day  || []).forEach(r => leadsByDay[r.date] = r.leads);

  // 生成过去14天的日期列表
  const dates = [];
  for (let i = 13; i >= 0; i--) {
    const d = new Date();
    d.setDate(d.getDate() - i);
    dates.push(d.toISOString().slice(0,10));
  }

  const postVals  = dates.map(d => postsByDay[d]  || 0);
  const leadsVals = dates.map(d => leadsByDay[d]  || 0);
  const maxP = Math.max(...postVals,  1);
  const maxL = Math.max(...leadsVals, 1);
  const W = 100 / dates.length;

  const postBars  = postVals.map((v,i) =>
    `<rect x="${i*W+W*0.1}%" y="${100-v/maxP*80}%" width="${W*0.35}%" height="${v/maxP*80}%" fill="#6366f1" rx="2" opacity=".8"><title>发布 ${dates[i]}: ${v}</title></rect>`
  ).join('');
  const leadBars = leadsVals.map((v,i) =>
    `<rect x="${i*W+W*0.55}%" y="${100-v/maxL*80}%" width="${W*0.35}%" height="${v/maxL*80}%" fill="#22c55e" rx="2" opacity=".8"><title>Leads ${dates[i]}: ${v}</title></rect>`
  ).join('');

  return `
<svg viewBox="0 0 100 100" preserveAspectRatio="none" style="width:100%;height:160px">
  ${postBars}
  ${leadBars}
</svg>
<div style="display:flex;gap:16px;font-size:11px;color:var(--text-muted);margin-top:6px">
  <span><span style="display:inline-block;width:10px;height:10px;background:#6366f1;border-radius:2px;margin-right:4px"></span>内容发布</span>
  <span><span style="display:inline-block;width:10px;height:10px;background:#22c55e;border-radius:2px;margin-right:4px"></span>新增 Leads</span>
</div>`;
}

// ─────────────────────────────────────────────────────────────
// 多人设运营大盘辅助函数
// ─────────────────────────────────────────────────────────────

// 切换激活人设并刷新建议
async function _studioSwitchPersona(personaId) {
  sugsPersonaId = personaId;
  try {
    await api('POST', '/studio/config', { active_persona: personaId });
    showToast(`已切换到 ${personaId}`, 'success');
  } catch(e) {}
  _studioLoadDash();
}

// 批量为所有人设生成内容
async function _studioOpenAllPersonasBrief() {
  const result = await _studioDialog({
    title: '批量生成确认',
    body: `<div style="font-size:13px;line-height:1.8">
      将为所有人设批量提交生成任务<br>
      <span style="color:var(--text-muted);font-size:11px">每个人设使用今日最高评分建议，生成后进入审核队列</span>
    </div>`,
    confirmText: '🚀 开始批量生成',
    cancelText: '取消',
  });
  if (!result) return;

  try {
    const allData = await api('GET', '/studio/suggestions/all-personas?n=1');
    const personas = allData.personas || {};
    let submitted = 0;
    for (const [pid, pdata] of Object.entries(personas)) {
      const topSug = (pdata.suggestions || [])[0];
      if (!topSug) continue;
      try {
        await api('POST', '/studio/jobs', {
          persona_id: pid,
          platforms: ['tiktok', 'instagram', 'telegram'],
          content_type: 'slideshow',
          mode: 'semi_auto',
          content_brief: topSug.brief,
        });
        submitted++;
      } catch(e) {}
    }
    showToast(`✅ 已为 ${submitted} 个人设提交生成任务`, 'success');
    setTimeout(_studioLoadDash, 1000);
  } catch(e) {
    showToast('批量生成失败: ' + e.message, 'error');
  }
}
