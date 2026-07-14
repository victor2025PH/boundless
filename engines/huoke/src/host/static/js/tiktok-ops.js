/* tiktok-ops.js — TikTok 运营指挥台功能实现
   依赖: core.js (api, showToast), platforms.js (loadPlatformPage)
   人群预设与 config/audience_presets.yaml 对齐（服务端 merge_audience_preset）
*/

/** 默认获客定向（意大利 30+ 男）；与 italy_male_30p 预设一致 */
const _TT_OPS_AUDIENCE_DEFAULT = 'italy_male_30p';
/** 收件箱轻量会话预设 */
const _TT_OPS_AUDIENCE_INBOX = 'italy_light_inbox';
const _LS_AUDIENCE_DEFAULT = 'tt_ops_audience_default';
const _LS_AUDIENCE_INBOX = 'tt_ops_audience_inbox';

function _ttOpsGetAudienceDefault() {
  try {
    var v = localStorage.getItem(_LS_AUDIENCE_DEFAULT);
    return v && String(v).trim() ? String(v).trim() : _TT_OPS_AUDIENCE_DEFAULT;
  } catch (e) {
    return _TT_OPS_AUDIENCE_DEFAULT;
  }
}

function _ttOpsGetAudienceInbox() {
  try {
    var v = localStorage.getItem(_LS_AUDIENCE_INBOX);
    return v && String(v).trim() ? String(v).trim() : _TT_OPS_AUDIENCE_INBOX;
  } catch (e) {
    return _TT_OPS_AUDIENCE_INBOX;
  }
}

/** 用预设数组填充指挥台两个下拉（空数组时用内置兜底） */
function _ttOpsApplyAudienceSelects(presets) {
  var selDef = document.getElementById('tt-ops-preset-default');
  var selInbox = document.getElementById('tt-ops-preset-inbox');
  if (!selDef || !selInbox) return false;
  var list = Array.isArray(presets) ? presets.slice() : [];
  if (!list.length) {
    [['italy_male_30p', '意大利 30+ 男性'], ['italy_light_inbox', '意大利 轻量收件箱'], ['usa_broad', '美国 宽筛选']].forEach(function (x) {
      list.push({ id: x[0], label: x[1] });
    });
  }
  function fill(sel, current) {
    sel.innerHTML = '';
    list.forEach(function (p) {
      if (!p || !p.id) return;
      var o = document.createElement('option');
      o.value = p.id;
      o.textContent = p.label || p.id;
      if (p.id === current) o.selected = true;
      sel.appendChild(o);
    });
  }
  fill(selDef, _ttOpsGetAudienceDefault());
  fill(selInbox, _ttOpsGetAudienceInbox());
  if (selDef.dataset.bound !== '1') {
    selDef.dataset.bound = '1';
    selInbox.dataset.bound = '1';
    selDef.onchange = function () {
      try {
        localStorage.setItem(_LS_AUDIENCE_DEFAULT, selDef.value);
      } catch (e) {}
      if (typeof showToast === 'function') showToast('已保存：获客/养号/关注 默认预设', 'success');
    };
    selInbox.onchange = function () {
      try {
        localStorage.setItem(_LS_AUDIENCE_INBOX, selInbox.value);
      } catch (e) {}
      if (typeof showToast === 'function') showToast('已保存：收件箱 默认预设', 'success');
    };
  }
  return true;
}

/** 填充指挥台「人群预设」下拉并绑定 localStorage（loadTtOpsPanel 末尾调用）；forceRefresh 时强制重拉缓存 */
async function _ttOpsInitAudiencePrefUI(forceRefresh) {
  if (!document.getElementById('tt-ops-audience-prefs')) return;
  var presets = [];
  var fr = !!forceRefresh;
  try {
    if (typeof window._ocLoadAudiencePresetsCached === 'function') {
      presets = await window._ocLoadAudiencePresetsCached(fr);
    } else if (typeof api === 'function') {
      var r = await api('GET', '/task-params/audience-presets');
      presets = (r && r.presets) ? r.presets : [];
    }
  } catch (e) {
    console.warn('[tt-ops] audience presets', e);
  }
  _ttOpsApplyAudienceSelects(presets);
}

/** 仅 POST 一次：服务端重载 + 用响应填充下拉 + 种子写入 platforms 缓存，避免再 GET */
async function ttOpsRefreshAudiencePresets() {
  var btn = document.getElementById('tt-ops-audience-refresh');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '…';
  }
  try {
    var presets = [];
    if (typeof window._ocReloadAndSeedAudiencePresets === 'function') {
      var out = await window._ocReloadAndSeedAudiencePresets();
      presets = (out && out.presets) ? out.presets : [];
    } else if (typeof api === 'function') {
      var r = await api('POST', '/task-params/reload-audience-presets', {});
      presets = (r && r.presets) ? r.presets : [];
      var etag = (r && r.etag) ? String(r.etag) : '';
      if (presets.length && etag) {
        if (typeof window._ocSeedAudiencePresetsCache === 'function') {
          window._ocSeedAudiencePresetsCache(presets, etag);
        }
        if (typeof window._ttFlowAudienceClientSeed === 'function') {
          window._ttFlowAudienceClientSeed(presets, etag);
        }
      } else if (typeof window._ocInvalidateAudiencePresetsCache === 'function') {
        window._ocInvalidateAudiencePresetsCache();
      }
    }
    _ttOpsApplyAudienceSelects(presets);
    if (typeof showToast === 'function') showToast('人群预设列表已更新', 'success');
  } catch (e) {
    console.warn('[tt-ops] refresh audience', e);
    if (typeof showToast === 'function') showToast('刷新失败: ' + (e && e.message ? e.message : e), 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '↻';
    }
  }
}

/* ══════════════════════════════════════════════
   全局状态（_ttCtaUrl 由 overview.js 声明，此处勿重复 let 以免脚本解析失败）
══════════════════════════════════════════════ */
let _ttOpsLoaded = false;
let _ttOpsLoading = false;  // 防止并发重复加载

/* ══════════════════════════════════════════════
   loadTtOpsPanel — 加载运营面板数据
══════════════════════════════════════════════ */
async function loadTtOpsPanel() {
  // 原 overview.js 中的壳层初始化（样式/Toast/设备网格/自动刷新），合并至此避免重复声明 loadTtOpsPanel
  if (typeof _ttInjectStyles === 'function') {
    _ttInjectStyles();
  }
  if (typeof _ttEnsureToastContainer === 'function') {
    _ttEnsureToastContainer();
  }
  if (typeof _loadTtDeviceGrid === 'function') {
    _loadTtDeviceGrid().catch(function () {});
  }
  if (typeof _ttStartAutoRefresh === 'function') {
    _ttStartAutoRefresh();
  }
  if (_ttOpsLoading) return;  // 已在加载中，防止重复
  _ttOpsLoading = true;
  _ttOpsLoaded = false;
  // 引流配置优先启动，不等待其他面板（独立超时8s）
  _loadTtRefConfig();
  try {
    // 主面板数据并行加载
    await Promise.all([
      _loadTtRevStats(),
      _loadTtHotLeads(),
      _loadTtOpsStats(),
    ]);
    _ttOpsLoaded = true;
    _ttOpsInitAudiencePrefUI().catch(function (e) {
      console.warn('[tt-ops] audience UI', e);
    });
  } finally {
    _ttOpsLoading = false;
  }
}

/* 收入/线索统计 */
async function _loadTtRevStats() {
  try {
    const [funnelData, leadsStats] = await Promise.all([
      api('GET', '/tiktok/funnel').catch(() => ({})),
      api('GET', '/leads/stats').catch(() => ({})),
    ]);
    const converted = funnelData.leads_converted || 0;
    const responded = funnelData.leads_responded || 0;
    const hotCount = responded + (funnelData.leads_qualified || 0);
    // 收入（暂以 50 EUR/单估算）
    const rev = converted * 50;
    _setEl('tt-rev-today-conv', converted + ' 单');
    _setEl('tt-rev-today', '€' + rev.toFixed(0));
    _setEl('tt-rev-hot', hotCount + ' 人');
    // 全局累计
    const total = leadsStats.total_leads || 0;
    _setEl('tt-rev-total', total + ' 线索');
    // 漏斗条
    _updateFunnelBar('contacted', funnelData.total_followed || 0);
    _updateFunnelBar('responded', funnelData.leads_responded || 0);
    _updateFunnelBar('qualified', funnelData.leads_qualified || 0);
    _updateFunnelBar('converted', funnelData.leads_converted || 0);
  } catch (e) { console.warn('[TtOps] rev stats:', e); }
}

function _updateFunnelBar(stage, val) {
  const numEl = document.getElementById('tt-fnl-' + stage);
  if (numEl) numEl.textContent = val;
  const row = document.querySelector('.tt-funnel-row[data-stage="' + stage + '"]');
  if (!row) return;
  const bar = row.querySelector('.tt-fnl-bar');
  if (!bar) return;
  // 动态宽度：以最大值为基准
  const allNums = [...document.querySelectorAll('.tt-fnl-num')].map(el => parseInt(el.textContent) || 0);
  const maxVal = Math.max(...allNums, 1);
  bar.style.width = Math.max(2, Math.round(val / maxVal * 100)) + '%';
}

/* 今日运营统计（关注/私信/AI回复/待处理） */
async function _loadTtOpsStats() {
  try {
    const [funnel, inbox] = await Promise.all([
      api('GET', '/tiktok/funnel').catch(() => ({})),
      api('GET', '/tiktok/escalation-queue').catch(() => ({ items: [] })),
    ]);
    _setEl('tt-stat-follows', funnel.total_followed ?? '-');
    _setEl('tt-stat-dms', funnel.total_dms ?? '-');
    // AI 回复数来自今日任务统计
    try {
      const dr = await api('GET', '/tiktok/daily-report');
      const total = dr.total || {};
      _setEl('tt-stat-autoreplied', total.auto_replied ?? total.dms_today ?? '-');
    } catch (_) { _setEl('tt-stat-autoreplied', '-'); }
    // 待处理 = 升级队列中未处理数量
    const escalated = (inbox.items || inbox.queue || []).length;
    _setEl('tt-stat-escalated', escalated || '0');
    // 任务进度卡片
    try {
      const tasks = await api('GET', '/tasks?limit=100&status=completed');
      const arr = tasks.tasks || tasks || [];
      const today = Date.now() / 1000 - 86400;
      const todayArr = arr.filter(t => (t.created_at || 0) > today);
      const count = (type) => todayArr.filter(t => (t.type || '').includes(type)).length;
      _setEl('tt-prog-warmup', count('warmup'));
      _setEl('tt-prog-follow', count('follow'));
      _setEl('tt-prog-inbox', count('inbox'));
      _setEl('tt-prog-dm', count('chat') + count('dm'));
    } catch (_) {}
  } catch (e) { console.warn('[TtOps] ops stats:', e); }
}

/* ══════════════════════════════════════════════
   热门线索列表（已回应 + 可成交）
══════════════════════════════════════════════ */
async function _loadTtHotLeads() {
  const el = document.getElementById('tt-hot-leads-list');
  if (!el) return;
  el.innerHTML = '<div style="text-align:center;color:var(--text-muted);font-size:12px;padding:10px">加载中...</div>';
  try {
    const data = await api('GET', '/tiktok/qualified-leads?limit=15');
    const leads = data.leads || [];
    if (!leads.length) {
      el.innerHTML = '<div style="text-align:center;color:var(--text-dim);font-size:12px;padding:14px">'
        + '暂无已回应线索 · 等待用户回复后自动显示 🌱</div>';
      _setEl('tt-rev-hot', '0 人');
      return;
    }
    _setEl('tt-rev-hot', leads.length + ' 人');
    el.innerHTML = leads.map(lead => _renderHotLeadRow(lead)).join('');
  } catch (e) {
    el.innerHTML = '<div style="color:#f87171;font-size:12px;padding:8px">加载失败: ' + e.message + '</div>';
  }
}

function _renderHotLeadRow(lead) {
  const uname = (lead.username || lead.name || 'Unknown').replace(/^@/, '');
  const score = Math.round(lead.score || 0);
  const scColor = score >= 25 ? '#22c55e' : score >= 15 ? '#f59e0b' : '#94a3b8';
  const status = lead.status || 'responded';
  const statusLabel = { responded: '已回复', qualified: '已合格', converted: '已成交' }[status] || status;
  const statusColor = { responded: '#f59e0b', qualified: '#a78bfa', converted: '#22c55e' }[status] || '#94a3b8';
  const preview = (lead.last_message || (lead.recent_interactions || [])[0]?.content || '').substring(0, 35);
  const source = lead.source === 'worker03' ? '<span style="font-size:9px;color:#60a5fa;margin-left:4px">W3</span>' : '';
  const leadId = lead.lead_id || lead.id || '';

  return `<div style="display:flex;align-items:center;gap:8px;padding:7px 0;border-bottom:1px solid var(--border)">
    <div style="width:32px;height:32px;border-radius:50%;background:linear-gradient(135deg,#f97316,#ef4444);display:flex;align-items:center;justify-content:center;font-size:14px;flex-shrink:0">🙋</div>
    <div style="flex:1;min-width:0">
      <div style="font-size:12px;font-weight:600">@${uname}${source} <span style="font-size:9px;color:${statusColor};background:${statusColor}22;padding:1px 5px;border-radius:3px;margin-left:4px">${statusLabel}</span></div>
      <div style="font-size:10px;color:var(--text-muted);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${preview || '无最近消息'}</div>
    </div>
    <div style="font-size:11px;font-weight:700;color:${scColor};flex-shrink:0">★${score}</div>
    <button onclick="_ttSendReferral('${uname}','${leadId}')"
      style="padding:3px 10px;font-size:10px;background:linear-gradient(135deg,rgba(34,197,94,.15),rgba(59,130,246,.15));color:#22c55e;border:1px solid rgba(34,197,94,.35);border-radius:5px;cursor:pointer;white-space:nowrap;flex-shrink:0"
      title="发送引流话术">发话术</button>
  </div>`;
}

/* ══════════════════════════════════════════════
   话术确认弹窗（共用）
══════════════════════════════════════════════ */

/**
 * 显示话术预览弹窗
 * @param {Array} items   dry_run 返回的 items[]
 * @param {Function} onConfirm  用户点确认后的回调
 * @param {string} title  弹窗标题
 */
function _ttShowBatchPitchModal(items, onConfirm, title) {
  // 移除旧弹窗
  const old = document.getElementById('tt-pitch-modal');
  if (old) old.remove();

  const rows = items.map(it => `
    <div style="background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:6px;padding:8px 10px;margin-bottom:6px">
      <div style="display:flex;align-items:center;gap:6px;margin-bottom:4px">
        <span style="font-size:11px;font-weight:700;color:#f1f5f9">@${it.lead}</span>
        ${it.score ? `<span style="font-size:9px;color:#f59e0b">★${it.score}</span>` : ''}
        <span style="font-size:9px;color:#60a5fa;margin-left:auto">设备 ${it.device || '?'}</span>
      </div>
      <div style="font-size:10px;color:#94a3b8;line-height:1.5;word-break:break-all">${it.pitch_preview || '(消息为空)'}</div>
    </div>`).join('');

  const modal = document.createElement('div');
  modal.id = 'tt-pitch-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  modal.innerHTML = `
    <div style="background:#1e293b;border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:20px;width:min(480px,92vw);max-height:80vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.5)">
      <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:4px">${title}</div>
      <div style="font-size:11px;color:#64748b;margin-bottom:12px">共 ${items.length} 条 · 请确认内容后发送</div>
      <div style="overflow-y:auto;flex:1;padding-right:2px">${rows}</div>
      <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">
        <button id="tt-modal-cancel"
          style="padding:6px 16px;font-size:12px;background:rgba(255,255,255,.06);color:#94a3b8;border:1px solid rgba(255,255,255,.12);border-radius:6px;cursor:pointer">
          取消
        </button>
        <button id="tt-modal-confirm"
          style="padding:6px 18px;font-size:12px;background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600">
          确认发送
        </button>
      </div>
    </div>`;

  document.body.appendChild(modal);

  document.getElementById('tt-modal-cancel').onclick = () => modal.remove();
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });

  document.getElementById('tt-modal-confirm').onclick = async () => {
    const confirmBtn = document.getElementById('tt-modal-confirm');
    confirmBtn.disabled = true;
    confirmBtn.textContent = '发送中...';
    try {
      await onConfirm();
    } finally {
      modal.remove();
    }
  };
}

/**
 * 显示批量发送结果弹窗
 */
function _ttShowResultModal(results, pitched, total) {
  const old = document.getElementById('tt-result-modal');
  if (old) old.remove();

  const rows = results.map(it => {
    const ok = it.task_id;
    const icon = ok ? '✓' : '✗';
    const color = ok ? '#22c55e' : '#ef4444';
    return `<div style="display:flex;gap:8px;align-items:center;padding:4px 0;font-size:11px">
      <span style="color:${color};font-weight:700;width:12px">${icon}</span>
      <span style="color:#f1f5f9;flex:1">@${it.lead}</span>
      <span style="color:#60a5fa;font-size:10px">${it.task_id ? '任务 '+it.task_id : (it.error || '失败')}</span>
    </div>`;
  }).join('');

  const modal = document.createElement('div');
  modal.id = 'tt-result-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  modal.innerHTML = `
    <div style="background:#1e293b;border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:20px;width:min(400px,90vw);max-height:75vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.5)">
      <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:4px">发送结果</div>
      <div style="font-size:11px;color:#64748b;margin-bottom:12px">
        成功 <span style="color:#22c55e;font-weight:700">${pitched}</span> / 共 ${total} 人
      </div>
      <div style="overflow-y:auto;flex:1">${rows}</div>
      <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">
        <button onclick="document.getElementById('tt-result-modal').remove();loadTtOpsPanel();"
          style="padding:6px 18px;font-size:12px;background:rgba(255,255,255,.08);color:#f1f5f9;border:1px solid rgba(255,255,255,.12);border-radius:6px;cursor:pointer">
          关闭并刷新
        </button>
      </div>
    </div>`;

  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
}

/* 单人发引流话术 — 先预览再确认 */
async function _ttSendReferral(username, leadId) {
  const btn = event.target;
  btn.disabled = true;
  btn.textContent = '加载中...';
  try {
    // Step 1: dry_run 获取预览
    const preview = await api('POST', '/tiktok/pitch-hot-leads', {
      target_username: username,
      lead_id: leadId ? parseInt(leadId) : undefined,
      max_pitch: 1,
      min_score: 0,
      dry_run: true,
    });
    const items = preview.items || [];
    if (!items.length) {
      showToast('无法生成话术：该用户无可用设备配置', 'error');
      btn.disabled = false;
      btn.textContent = '发话术';
      return;
    }
    btn.disabled = false;
    btn.textContent = '发话术';

    // Step 2: 显示确认弹窗
    _ttShowBatchPitchModal(items, async () => {
      // Step 3: 实际发送
      const r = await api('POST', '/tiktok/pitch-hot-leads', {
        target_username: username,
        lead_id: leadId ? parseInt(leadId) : undefined,
        max_pitch: 1,
        min_score: 0,
        dry_run: false,
      });
      if (r.ok) {
        showToast('@' + username + ' 引流话术已发送 ✓', 'success');
        // 乐观更新按钮
        btn.textContent = '✓ 已发';
        btn.style.color = '#22c55e';
        btn.disabled = true;
        setTimeout(() => _loadTtHotLeads(), 2500);
      } else {
        showToast('发送失败: ' + (r.error || '未知错误'), 'error');
      }
    }, `向 @${username} 发送引流话术`);
  } catch (e) {
    showToast('操作失败: ' + e.message, 'error');
    btn.disabled = false;
    btn.textContent = '发话术';
  }
}

/* ══════════════════════════════════════════════
   按钮功能实现
══════════════════════════════════════════════ */

/* 制定工作计划 — 替代原来的直接启动（顶栏 / 下拉菜单共用，可选传入按钮元素） */
async function ttLaunchCampaign(btn) {
  ttOpenWorkPlan();
}

const _TT_PITCH_BTN_HTML = '&#128176; 发话术';
const _TT_INBOX_BTN_HTML = '&#128172; 批量收件箱';

/* 一键发转化话术（批量）— 先预览再确认 */
async function ttPitchHotLeads(btn) {
  btn = btn || document.getElementById('tt-pitch-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 加载预览...'; }
  try {
    // Step 1: dry_run 获取全部预览
    const preview = await api('POST', '/tiktok/pitch-hot-leads', {
      min_score: 0,
      max_pitch: 10,
      cta_url: _ttCtaUrl || '',
      dry_run: true,
    });

    if (!preview.ok) {
      showToast('预览失败: ' + (preview.error || '未知错误'), 'error');
      if (btn) { btn.disabled = false; btn.innerHTML = _TT_PITCH_BTN_HTML; }
      return;
    }
    const items = preview.items || [];
    if (!items.length) {
      showToast(preview.message || '暂无可发送的线索', 'warn');
      if (btn) { btn.disabled = false; btn.innerHTML = _TT_PITCH_BTN_HTML; }
      return;
    }

    if (btn) { btn.disabled = false; btn.innerHTML = _TT_PITCH_BTN_HTML; }

    // Step 2: 显示确认弹窗（含全部预览）
    _ttShowBatchPitchModal(items, async () => {
      // Step 3: 实际发送
      const r = await api('POST', '/tiktok/pitch-hot-leads', {
        min_score: 0,
        max_pitch: 10,
        cta_url: _ttCtaUrl || '',
        dry_run: false,
      });
      if (r.ok) {
        // Step 4: 显示发送结果弹窗
        _ttShowResultModal(r.items || [], r.pitched || 0, r.total_hot || items.length);
      } else {
        showToast('发送失败: ' + (r.error || '未知错误'), 'error');
      }
    }, `批量发送引流话术`);
  } catch (e) {
    showToast('操作失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = _TT_PITCH_BTN_HTML; }
  }
}

/* 检查收件箱 + AI 回复（带预检门控）*/
async function ttCheckInbox() {
  const btn = document.getElementById('tt-inbox-btn') || document.querySelector('[onclick*="ttCheckInbox"]');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 预检中...'; }
  try {
    // 快速预检：仅检查就绪设备
    const readiness = await api('GET', '/preflight/devices?quick=1').catch(() => null);
    const ready = readiness ? (readiness.ready_count || 0) : null;
    const total = readiness ? (readiness.total_count || 0) : null;
    const blocked = readiness ? (readiness.blocked_count || 0) : 0;

    let msg = '将为所有设备创建收件箱检查任务';
    if (ready !== null) {
      msg = `就绪 ${ready}台，已阻塞 ${blocked}台（无网络/VPN）\n仅对就绪设备执行`;
    }
    if (!(await ocDialog({title:'收件箱检查',message:msg.replace(/\n/g,'<br>'),type:'info',confirmText:'开始',cancelText:'取消'}))) return;

    await api('POST', '/tasks', {
      type: 'tiktok_check_inbox',
      params: {
        audience_preset: _ttOpsGetAudienceInbox(),
        auto_reply: true,
        max_conversations: 50,
        _preflight_required: true,
      },
    });
    showToast('✓ 收件箱检查任务已创建（仅就绪设备会执行）', 'success');
    setTimeout(() => _loadTtOpsStats(), 2000);
  } catch (e) {
    showToast('创建失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.innerHTML = _TT_INBOX_BTN_HTML; }
  }
}

/**
 * 单台检查收件箱 — 与「批量收件箱」同一任务类型（tiktok_check_inbox），走预检缓存；不弹全集群确认框。
 */
async function ttCheckInboxForDevice(deviceId) {
  if (!deviceId) return;
  try {
    showToast('正在预检本机...', 'info');
    const pf = await api('GET', '/preflight/devices?quick=1').catch(function() { return null; });
    if (pf && Array.isArray(pf.devices)) {
      const row = pf.devices.find(function(d) { return d.device_id === deviceId; });
      if (row && row.passed === false) {
        showToast('该设备未就绪：' + (row.blocked_reason || row.blocked_step || '请检查网络/VPN'), 'warn');
        return;
      }
    }
    await api('POST', '/tasks', {
      type: 'tiktok_check_inbox',
      device_id: deviceId,
      params: {
        audience_preset: _ttOpsGetAudienceInbox(),
        auto_reply: true,
        max_conversations: 50,
        _preflight_required: true,
      },
    });
    showToast('✓ 已为本机创建收件箱检查任务', 'success');
    setTimeout(function() { _loadTtOpsStats(); }, 2000);
  } catch (e) {
    showToast('创建失败: ' + (e.message || e), 'error');
  }
}

/* ttSetCtaUrl 由 overview.js 提供（与本文件曾重复声明导致整段脚本无法解析） */

/* ══════════════════════════════════════════════
   工具函数
══════════════════════════════════════════════ */
function _setEl(id, val) {
  const el = document.getElementById(id);
  if (el) el.textContent = val;
}

/* ══════════════════════════════════════════════
   自动初始化：TikTok 运营页面激活时加载
══════════════════════════════════════════════ */
/** TikTok 顶栏下拉：互斥展开 + 点击外部关闭 */
function _ttBindCmdBarDetails() {
  const bar = document.getElementById('tt-cmd-bar');
  if (!bar || bar.dataset.detailsBound === '1') return;
  bar.dataset.detailsBound = '1';
  function closeOtherDetails(keep) {
    bar.querySelectorAll('details.tt-cmd-dd').forEach(function(d2) {
      if (d2 !== keep) d2.removeAttribute('open');
    });
  }
  bar.querySelectorAll('details.tt-cmd-dd').forEach(function(d) {
    d.addEventListener('toggle', function() {
      if (!d.open) return;
      closeOtherDetails(d);
    });
  });
  /* toggle 在部分浏览器上与 summary 点击顺序不一致，补一层 click 确保只保留当前项 */
  bar.querySelectorAll('details.tt-cmd-dd > summary.tt-cmd-dd-summary').forEach(function(sum) {
    sum.addEventListener(
      'click',
      function() {
        const det = sum.parentElement;
        window.setTimeout(function() {
          if (det && det.open) closeOtherDetails(det);
        }, 0);
      },
      false
    );
  });
  document.addEventListener('click', function(ev) {
    if (!ev.target || !ev.target.closest) return;
    if (bar.contains(ev.target)) return;
    bar.querySelectorAll('details.tt-cmd-dd[open]').forEach(function(d) {
      d.removeAttribute('open');
    });
  }, false);
}

/** 首次进入 TikTok 页：提示「批量 vs 本机」 */
function _ttTryShowIaHint() {
  try {
    if (localStorage.getItem('oc_dismiss_tt_ia_hint') === '1') return;
    var el = document.getElementById('tt-ia-hint');
    var page = document.getElementById('page-plat-tiktok');
    if (el && page && page.classList.contains('active')) {
      el.style.display = 'flex';
      el.style.alignItems = 'flex-start';
    }
  } catch (e) {}
}

window._ttDismissIaHint = function() {
  try {
    localStorage.setItem('oc_dismiss_tt_ia_hint', '1');
  } catch (e) {}
  var el = document.getElementById('tt-ia-hint');
  if (el) el.style.display = 'none';
};

document.addEventListener('DOMContentLoaded', () => {
  _ttBindCmdBarDetails();
  setTimeout(_ttTryShowIaHint, 400);
  // 监听页面切换事件，当进入 TikTok 运营 tab 时自动加载
  const observer = new MutationObserver(() => {
    const pg = document.getElementById('page-plat-tiktok');
    if (pg && pg.classList.contains('active')) _ttTryShowIaHint();
    const opsPanel = document.getElementById('tt-ops-panel');
    const hotList = document.getElementById('tt-hot-leads-list');
    if (opsPanel && hotList && !_ttOpsLoaded) {
      const isVisible = opsPanel.offsetParent !== null;
      if (isVisible) loadTtOpsPanel();
    }
  });
  const root = document.getElementById('main-content') || document.body;
  observer.observe(root, { childList: true, subtree: true, attributes: true, attributeFilter: ['class', 'style'] });

  // 3秒后检查是否需要初始化（页面直接打开 TikTok 面板时）
  setTimeout(() => {
    const opsPanel = document.getElementById('tt-ops-panel');
    if (opsPanel && opsPanel.offsetParent !== null && !_ttOpsLoaded) {
      loadTtOpsPanel();
    }
  }, 3000);

  // ★ P3-1: 启动涨粉仪表盘（延迟2s等 API 就绪）
  setTimeout(() => {
    if (typeof _startGrowthDashboard === 'function') _startGrowthDashboard();
  }, 2000);
});

/* 关注用户（默认人群预设 italy_male_30p，30人/台） */
async function ttFollowUsers() {
  try {
    await api('POST', '/tasks', {
      type: 'tiktok_follow',
      params: { audience_preset: _ttOpsGetAudienceDefault(), max_follows: 30 },
    });
    showToast('✓ 关注任务已创建（人群预设 ' + _ttOpsGetAudienceDefault() + '，30人/台）', 'success');
  } catch (e) {
    showToast('创建失败: ' + e.message, 'error');
  }
}

/* 回关私信 */
async function ttFollowBacks() {
  try {
    await api('POST', '/tasks', {
      type: 'tiktok_check_and_chat_followbacks',
      params: { audience_preset: _ttOpsGetAudienceDefault(), max_chats: 20 },
    });
    showToast('✓ 回关私信任务已创建', 'success');
  } catch (e) {
    showToast('创建失败: ' + e.message, 'error');
  }
}

/* ══════════════════════════════════════════════
   设备引流账号配置面板
   P1-A: 独立入口（嵌入 TikTok 运营面板）
   P1-B: 设备别名 + 状态 + 节点标签
   P1-C: 行内编辑 + blur 自动保存 + 格式校验
   P2-A: 一键复制到所有未配置设备
   P2-B: CSV 批量导入弹窗
   P2-C: 后端批量更新 API 调用
══════════════════════════════════════════════ */

/* 加载并渲染引流配置面板 */
async function _loadTtRefConfig() {
  const el = document.getElementById('tt-ref-devices');
  if (!el) return;
  const t0 = Date.now();
  el.innerHTML = '<div style="text-align:center;color:var(--text-muted);font-size:12px;padding:10px">请求中...</div>';
  try {
    // 用 5 秒超时，直接 fetch 绕过 api() 缓存层
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 5000);
    let resp, data;
    try {
      resp = await fetch('/tiktok/referral-config/full', { signal: ctrl.signal, credentials: 'include' });
      clearTimeout(timer);
      if (!resp.ok) throw new Error('HTTP ' + resp.status);
      data = await resp.json();
    } catch (fe) {
      clearTimeout(timer);
      const elapsed = ((Date.now()-t0)/1000).toFixed(1);
      if (fe.name === 'AbortError') {
        el.innerHTML = `<div style="color:#f87171;font-size:11px;padding:8px">超时(${elapsed}s)，点击 ⟳ 重试</div>`;
      } else {
        el.innerHTML = `<div style="color:#f87171;font-size:11px;padding:8px">失败(${elapsed}s): ${fe.message} · 点击 ⟳ 重试</div>`;
      }
      return;
    }
    const devices = data.devices || [];
    if (!devices.length) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:8px 0">未找到活跃设备</div>';
      return;
    }
    const configured = devices.filter(d => d.configured).length;
    const total = devices.length;
    const statEl = document.getElementById('tt-ref-stat');
    if (statEl) {
      statEl.textContent = `${configured}/${total} 已配置`;
      statEl.style.color = configured === total ? '#22c55e' : '#f59e0b';
    }
    el.innerHTML = devices.map(d => _renderRefDeviceRow(d)).join('');
  } catch (e) {
    el.innerHTML = `<div style="color:#f87171;font-size:11px;padding:8px">异常: ${e.message}</div>`;
  }
}

/* 渲染单台设备的配置行 */
function _renderRefDeviceRow(dev) {
  const did = dev.device_id;
  const alias = dev.alias || did.substring(0, 8);
  const host = dev.host || '主控';
  const nodeLabel = dev.host || (dev.node === 'worker03' ? 'W03' : '主控');
  const nodeColor = _getWorkerColor(nodeLabel);
  const nodeBg = _hexToRgba(nodeColor, .12);

  // 在线状态点
  const online = dev.status === 'connected' || dev.status === 'online' || dev.status === 'active';
  const offline = dev.status === 'offline' || dev.status === 'disconnected' || dev.status === 'unknown';
  const dotColor = online ? '#22c55e' : offline ? '#ef4444' : '#f59e0b';
  const dotTitle = online ? '在线' : offline ? '离线' : dev.status;

  // 配置状态
  const hasTg = !!dev.telegram;
  const hasWa = !!dev.whatsapp;
  const fullyConfigured = hasTg && hasWa;
  const partlyConfigured = hasTg || hasWa;
  const statusBadge = fullyConfigured
    ? '<span style="font-size:9px;color:#22c55e;background:rgba(34,197,94,.12);padding:1px 5px;border-radius:3px">✓ 已配置</span>'
    : partlyConfigured
      ? '<span style="font-size:9px;color:#f59e0b;background:rgba(245,158,11,.12);padding:1px 5px;border-radius:3px">⚠ 部分</span>'
      : '<span style="font-size:9px;color:#94a3b8;background:rgba(148,163,184,.1);padding:1px 5px;border-radius:3px">未配置</span>';

  const tgVal = (dev.telegram || '').replace(/"/g, '&quot;');
  const waVal = (dev.whatsapp || '').replace(/"/g, '&quot;');
  const indicatorId = `ref-ind-${did.replace(/[^a-zA-Z0-9]/g, '_')}`;

  // 只在有配置时显示复制按钮
  const copyBtn = fullyConfigured
    ? `<button onclick="_ttCopyRefToAll('${did}','${tgVal}','${waVal}')"
         style="padding:2px 8px;font-size:9px;background:rgba(255,255,255,.06);color:#94a3b8;border:1px solid rgba(255,255,255,.1);border-radius:4px;cursor:pointer;white-space:nowrap;flex-shrink:0"
         title="复制此设备配置到所有未配置设备">复制到全部</button>`
    : '';

  return `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(51,65,85,.4)">
    <!-- 设备信息 -->
    <div style="display:flex;align-items:center;gap:5px;min-width:90px;flex-shrink:0">
      <span style="width:7px;height:7px;border-radius:50%;background:${dotColor};display:inline-block;flex-shrink:0" title="${dotTitle}"></span>
      <span style="font-size:12px;font-weight:600;color:var(--text-main)">${alias}</span>
      <span style="font-size:9px;color:${nodeColor};background:${nodeBg};padding:1px 4px;border-radius:3px">${nodeLabel}</span>
    </div>
    <!-- 状态徽章 -->
    <div style="min-width:52px;flex-shrink:0">${statusBadge}</div>
    <!-- TG 输入 -->
    <div style="display:flex;align-items:center;gap:4px;flex:1;min-width:0">
      <span style="font-size:10px;color:#60a5fa;flex-shrink:0">TG</span>
      <input type="text" value="${tgVal}" placeholder="@username"
        style="flex:1;min-width:80px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:4px;padding:3px 6px;color:var(--text-main);font-size:11px;outline:none"
        onblur="_ttAutoSaveRef('${did}','telegram',this.value,this,'${indicatorId}')"
        onkeydown="if(event.key==='Enter')this.blur()"
        onfocus="this.style.borderColor='rgba(96,165,250,.5)'"
        id="ref-tg-${did.replace(/[^a-zA-Z0-9]/g,'_')}">
    </div>
    <!-- WA 输入 -->
    <div style="display:flex;align-items:center;gap:4px;flex:1;min-width:0">
      <span style="font-size:10px;color:#22c55e;flex-shrink:0">WA</span>
      <input type="text" value="${waVal}" placeholder="+639..."
        style="flex:1;min-width:80px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:4px;padding:3px 6px;color:var(--text-main);font-size:11px;outline:none"
        onblur="_ttAutoSaveRef('${did}','whatsapp',this.value,this,'${indicatorId}')"
        onkeydown="if(event.key==='Enter')this.blur()"
        onfocus="this.style.borderColor='rgba(34,197,94,.5)'"
        id="ref-wa-${did.replace(/[^a-zA-Z0-9]/g,'_')}">
    </div>
    <!-- 保存状态指示器 + 复制按钮 -->
    <div style="display:flex;align-items:center;gap:4px;flex-shrink:0">
      <span id="${indicatorId}" style="font-size:10px;color:transparent;min-width:32px;text-align:center">✓</span>
      ${copyBtn}
    </div>
  </div>`;
}

/* blur 自动保存单台设备配置 */
async function _ttAutoSaveRef(deviceId, field, value, inputEl, indicatorId) {
  // 格式自动修正
  let val = value.trim();
  if (field === 'telegram' && val && !val.startsWith('@')) val = '@' + val;
  if (field === 'whatsapp' && val && !val.startsWith('+')) {
    inputEl.style.borderColor = '#ef4444';
    showToast('WhatsApp 号码必须以 + 开头，例如 +639...', 'warn');
    return;
  }
  if (inputEl.value !== val) inputEl.value = val;
  inputEl.style.borderColor = '';

  const ind = document.getElementById(indicatorId);
  if (ind) { ind.textContent = '保存中'; ind.style.color = '#94a3b8'; }

  try {
    await api('POST', '/tiktok/referral-config', { device_id: deviceId, [field]: val });
    if (ind) { ind.textContent = '✓ 已存'; ind.style.color = '#22c55e'; }
    inputEl.style.borderColor = 'rgba(34,197,94,.4)';
    setTimeout(() => {
      if (ind) { ind.textContent = ''; ind.style.color = 'transparent'; }
      inputEl.style.borderColor = '';
    }, 2000);
    // 刷新配置缓存（让后续发话术能用到新配置）
    setTimeout(() => _loadTtRefConfig(), 500);
  } catch (e) {
    if (ind) { ind.textContent = '✗ 失败'; ind.style.color = '#ef4444'; }
    inputEl.style.borderColor = '#ef4444';
    showToast('保存失败: ' + e.message, 'error');
  }
}

/* 复制一台设备的配置到所有未配置的设备 */
async function _ttCopyRefToAll(sourceDeviceId, tg, wa) {
  try {
    const data = await api('GET', '/tiktok/referral-config/full');
    const unconfigured = (data.devices || []).filter(d => !d.configured && d.device_id !== sourceDeviceId);
    if (!unconfigured.length) {
      showToast('所有在线设备均已配置引流账号 ✓', 'success');
      return;
    }

    const items = unconfigured.map(d => ({ device_id: d.device_id, telegram: tg, whatsapp: wa }));
    const r = await api('POST', '/tiktok/referral-config/batch', { items });
    showToast(`✓ 已为 ${r.updated} 台设备复制配置`, 'success');
    setTimeout(() => _loadTtRefConfig(), 500);
  } catch (e) {
    showToast('复制失败: ' + e.message, 'error');
  }
}

/* CSV 批量导入弹窗 */
function _ttImportCsvModal() {
  const old = document.getElementById('tt-csv-modal');
  if (old) old.remove();

  const modal = document.createElement('div');
  modal.id = 'tt-csv-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.65);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
  modal.innerHTML = `
    <div style="background:#1e293b;border:1px solid rgba(255,255,255,.12);border-radius:10px;padding:20px;width:min(520px,92vw);box-shadow:0 20px 60px rgba(0,0,0,.5)">
      <div style="font-size:14px;font-weight:700;color:#f1f5f9;margin-bottom:4px">批量导入引流账号</div>
      <div style="font-size:11px;color:#64748b;margin-bottom:12px">
        每行一台设备，格式：<code style="background:rgba(255,255,255,.06);padding:1px 4px;border-radius:3px">别名或设备ID, @TG账号, +WA号码</code><br>
        示例：<code style="color:#94a3b8">01号, @myaccount, +639270135480</code><br>
        支持逗号/Tab 分隔，WA 可省略，也可从 Excel 直接粘贴。
      </div>
      <textarea id="tt-csv-input"
        style="width:100%;height:140px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.12);border-radius:6px;padding:8px;color:#f1f5f9;font-size:12px;font-family:monospace;resize:vertical;box-sizing:border-box"
        placeholder="01号, @account_tg1, +639270135480&#10;02号, @account_tg2, +639952948809&#10;03号, @account_tg3&#10;04号, @account_tg4, +639123456789"></textarea>
      <div id="tt-csv-preview" style="margin-top:8px;font-size:11px;color:#64748b;min-height:20px"></div>
      <div style="display:flex;gap:8px;margin-top:14px;justify-content:flex-end">
        <button onclick="document.getElementById('tt-csv-modal').remove()"
          style="padding:6px 16px;font-size:12px;background:rgba(255,255,255,.06);color:#94a3b8;border:1px solid rgba(255,255,255,.12);border-radius:6px;cursor:pointer">
          取消
        </button>
        <button onclick="_ttPreviewCsvImport()"
          style="padding:6px 14px;font-size:12px;background:rgba(255,255,255,.08);color:#f1f5f9;border:1px solid rgba(255,255,255,.15);border-radius:6px;cursor:pointer">
          预览解析
        </button>
        <button id="tt-csv-confirm" onclick="_ttSubmitCsvImport()"
          style="padding:6px 18px;font-size:12px;background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff;border:none;border-radius:6px;cursor:pointer;font-weight:600">
          确认导入
        </button>
      </div>
    </div>`;

  document.body.appendChild(modal);
  modal.addEventListener('click', e => { if (e.target === modal) modal.remove(); });
  document.getElementById('tt-csv-input').addEventListener('input', _ttPreviewCsvImport);
}

/* 解析 CSV 文本，返回 [{alias_or_id, telegram, whatsapp}] */
async function _ttParseCsvLines(text) {
  // 获取设备列表用于别名→device_id 映射
  let deviceMap = {};
  try {
    const data = await api('GET', '/tiktok/referral-config/full');
    for (const d of (data.devices || [])) {
      deviceMap[d.alias.toLowerCase()] = d.device_id;
      deviceMap[d.device_id.toLowerCase()] = d.device_id;
    }
  } catch (e) {}

  const items = [];
  const errors = [];
  for (const line of text.split('\n')) {
    const raw = line.trim();
    if (!raw || raw.startsWith('#')) continue;
    const parts = raw.split(/[\t,，]+/).map(s => s.trim());
    if (parts.length < 2) { errors.push(`跳过（格式错误）: ${raw}`); continue; }
    const [idOrAlias, tg, wa] = parts;
    const did = deviceMap[idOrAlias.toLowerCase()] || idOrAlias;
    if (!tg && !wa) { errors.push(`跳过（无账号）: ${raw}`); continue; }
    items.push({ device_id: did, telegram: tg || '', whatsapp: wa || '' });
  }
  return { items, errors };
}

/* 预览解析结果（不发送） */
async function _ttPreviewCsvImport() {
  const text = document.getElementById('tt-csv-input')?.value || '';
  const previewEl = document.getElementById('tt-csv-preview');
  if (!previewEl) return;
  if (!text.trim()) { previewEl.textContent = ''; return; }
  const { items, errors } = await _ttParseCsvLines(text);
  let html = '';
  if (items.length) html += `<span style="color:#22c55e">✓ ${items.length} 条可导入</span>`;
  if (errors.length) html += `<span style="color:#f59e0b;margin-left:8px">⚠ ${errors.length} 行被跳过</span>`;
  previewEl.innerHTML = html;
}

/* 执行 CSV 导入 */
async function _ttSubmitCsvImport() {
  const text = document.getElementById('tt-csv-input')?.value || '';
  if (!text.trim()) { showToast('请输入内容', 'warn'); return; }

  const confirmBtn = document.getElementById('tt-csv-confirm');
  if (confirmBtn) { confirmBtn.disabled = true; confirmBtn.textContent = '导入中...'; }

  try {
    const { items, errors } = await _ttParseCsvLines(text);
    if (!items.length) { showToast('未解析到有效数据', 'warn'); return; }

    const r = await api('POST', '/tiktok/referral-config/batch', { items });
    document.getElementById('tt-csv-modal')?.remove();

    showToast(`✓ 成功导入 ${r.updated} 台设备${r.errors?.length ? `，${r.errors.length} 条错误` : ''}`, 'success');
    setTimeout(() => _loadTtRefConfig(), 500);
  } catch (e) {
    showToast('导入失败: ' + e.message, 'error');
  } finally {
    if (confirmBtn) { confirmBtn.disabled = false; confirmBtn.textContent = '确认导入'; }
  }
}

/* ── 自动重试：若引流面板一直显示"加载中..."则每3秒尝试一次 ── */
setInterval(function _ttRefAutoRetry() {
  const el = document.getElementById('tt-ref-devices');
  if (!el) return;
  // 只有显示"加载中..."（静态初始 HTML）才重试，避免覆盖正常内容
  const txt = el.innerText || el.textContent || '';
  if (txt.trim() === '加载中...') {
    _loadTtRefConfig();
  }
}, 3000);

/* ══════════════════════════════════════════════
   工作计划弹窗
══════════════════════════════════════════════ */

function ttOpenWorkPlan() {
  let modal = document.getElementById('tt-work-plan-modal');
  if (!modal) {
    _ttBuildWorkPlanModal();
    modal = document.getElementById('tt-work-plan-modal');
  }
  if (!modal) {
    showToast('无法打开工作计划弹窗', 'error');
    return;
  }
  modal.style.display = 'flex';
  _ttRefreshReadiness();
}

function ttCloseWorkPlan() {
  const modal = document.getElementById('tt-work-plan-modal');
  if (modal) modal.style.display = 'none';
}

async function _ttRefreshReadiness() {
  const el = document.getElementById('wp-readiness-result');
  if (!el) return;
  el.innerHTML = '<span style="color:var(--text-muted)">检测中...</span>';
  try {
    const r = await api('GET', '/preflight/devices?quick=1');
    const ready = r.ready_count || 0;
    const blocked_net = r.blocked_network || 0;
    const blocked_vpn = r.blocked_vpn || 0;
    const offline = r.offline_count || 0;
    el.innerHTML =
      `<span style="color:#22c55e">✅ 就绪 ${ready}台</span>` +
      (blocked_net ? `　<span style="color:#ef4444">❌ 无网络 ${blocked_net}台</span>` : '') +
      (blocked_vpn ? `　<span style="color:#f59e0b">⚠️ 无VPN ${blocked_vpn}台</span>` : '') +
      (offline ? `　<span style="color:var(--text-muted)">○ 离线 ${offline}台</span>` : '');
  } catch(e) {
    el.innerHTML = '<span style="color:#f59e0b">⚠️ 无法获取就绪状态</span>';
  }
}

async function ttExecuteWorkPlan() {
  const workflow = document.querySelector('input[name="wp-workflow"]:checked');
  if (!workflow) { showToast('请选择工作内容', 'warn'); return; }
  const wf = workflow.value;

  const timing = document.querySelector('input[name="wp-timing"]:checked');
  const immediate = !timing || timing.value === 'now';

  const btn = document.getElementById('wp-start-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 预检并创建任务...'; }

  try {
    // 所有任务都带 _preflight_required 标记，executor 会在执行前再次门控
    const taskMap = {
      'warmup':    [{ type: 'tiktok_warmup', params: { audience_preset: _ttOpsGetAudienceDefault(), duration_minutes: 25 } }],
      'inbox':     [{ type: 'tiktok_check_inbox', params: { audience_preset: _ttOpsGetAudienceInbox(), auto_reply: true, max_conversations: 50 } }],
      'follow':    [{ type: 'tiktok_follow', params: { audience_preset: _ttOpsGetAudienceDefault(), max_follows: 30 } }],
      'full':      [
        { type: 'tiktok_warmup',      params: { audience_preset: _ttOpsGetAudienceDefault(), duration_minutes: 20 } },
        { type: 'tiktok_follow',      params: { audience_preset: _ttOpsGetAudienceDefault(), max_follows: 30 } },
        { type: 'tiktok_check_inbox', params: { audience_preset: _ttOpsGetAudienceInbox(), auto_reply: true, max_conversations: 50 } },
      ],
    };

    const tasks = taskMap[wf] || [];
    if (!tasks.length) { showToast('未知工作内容', 'warn'); return; }

    const results = [];
    for (const t of tasks) {
      const r = await api('POST', '/tasks', {
        type: t.type,
        params: { ...t.params, _preflight_required: true }
      }).catch(e => ({ error: e.message }));
      results.push(t.type + (r.error ? ' ✗' : ' ✓'));
    }

    showToast('✓ 任务已创建:<br>' + results.join('<br>'), 'success', 4000);
    ttCloseWorkPlan();
    setTimeout(() => { _loadTtDeviceGrid(); loadTtOpsPanel && loadTtOpsPanel(); }, 1500);
  } catch(e) {
    showToast('创建失败: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '预检并开始'; }
  }
}

function _ttBuildWorkPlanModal() {
  // 动态创建弹窗 DOM
  const div = document.createElement('div');
  div.id = 'tt-work-plan-modal';
  div.style.cssText = 'display:none;position:fixed;inset:0;z-index:9000;align-items:center;justify-content:center;background:rgba(0,0,0,.6)';
  div.innerHTML = `
<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:24px;width:480px;max-width:95vw;max-height:90vh;overflow-y:auto">
  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:18px">
    <span style="font-size:16px;font-weight:600">📋 制定工作计划</span>
    <button onclick="ttCloseWorkPlan()" style="background:none;border:none;color:var(--text-muted);font-size:20px;cursor:pointer">✕</button>
  </div>

  <!-- 就绪状态 -->
  <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px 14px;margin-bottom:16px">
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">📡 设备就绪状态</div>
    <div id="wp-readiness-result" style="font-size:13px">检测中...</div>
  </div>

  <!-- 选择工作内容 -->
  <div style="margin-bottom:16px">
    <div style="font-size:12px;font-weight:600;margin-bottom:8px;color:var(--text-muted)">选择工作内容</div>
    <label style="display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;cursor:pointer">
      <input type="radio" name="wp-workflow" value="full" checked>
      <div><div style="font-size:13px;font-weight:500">🔄 智能全流程</div><div style="font-size:11px;color:var(--text-muted)">养号 → 关注 → 收件箱（推荐）</div></div>
    </label>
    <label style="display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;cursor:pointer">
      <input type="radio" name="wp-workflow" value="warmup">
      <div><div style="font-size:13px;font-weight:500">🌱 仅养号</div><div style="font-size:11px;color:var(--text-muted)">浏览信息流，提升算法分</div></div>
    </label>
    <label style="display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;cursor:pointer">
      <input type="radio" name="wp-workflow" value="inbox">
      <div><div style="font-size:13px;font-weight:500">📬 仅收件箱</div><div style="font-size:11px;color:var(--text-muted)">AI自动回复新消息</div></div>
    </label>
    <label style="display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;cursor:pointer">
      <input type="radio" name="wp-workflow" value="follow">
      <div><div style="font-size:13px;font-weight:500">👥 仅关注</div><div style="font-size:11px;color:var(--text-muted)">关注目标用户（每台最多30人）</div></div>
    </label>
  </div>

  <!-- 执行时机 -->
  <div style="margin-bottom:20px">
    <div style="font-size:12px;font-weight:600;margin-bottom:8px;color:var(--text-muted)">执行时机</div>
    <label style="display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--border);border-radius:8px;margin-bottom:6px;cursor:pointer">
      <input type="radio" name="wp-timing" value="now" checked>
      <div style="font-size:13px">⚡ 立即开始（通过预检的设备马上执行）</div>
    </label>
  </div>

  <!-- 提示 -->
  <div style="background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.2);border-radius:8px;padding:10px 14px;margin-bottom:16px;font-size:11px;color:var(--text-muted)">
    ℹ️ 预检通过的设备才会执行任务。无网络或VPN未连接的设备将自动跳过，不会创建失败任务。
  </div>

  <div style="display:flex;gap:8px;justify-content:flex-end">
    <button onclick="ttCloseWorkPlan()" style="padding:8px 16px;border:1px solid var(--border);border-radius:8px;background:none;color:var(--text-muted);cursor:pointer">取消</button>
    <button id="wp-start-btn" onclick="ttExecuteWorkPlan()" style="padding:8px 20px;border:none;border-radius:8px;background:#6366f1;color:#fff;font-weight:600;cursor:pointer">预检并开始</button>
  </div>
</div>`;
  document.body.appendChild(div);
  div.addEventListener('click', e => { if (e.target === div) ttCloseWorkPlan(); });
  _ttRefreshReadiness();
}
