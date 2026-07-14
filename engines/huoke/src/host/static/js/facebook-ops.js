/* facebook-ops.js — Facebook 平台专属面板逻辑
 *
 * 职责:
 *   - 加载 5 套执行方案预设(从 GET /facebook/presets)
 *   - 渲染设备网格上方的"执行方案"指挥栏(指挥栏 + 引流账号 + 批量发请求)
 *   - 「配置执行流程」模态框(点设备卡片 → 选预设 → 一键启动)
 *   - 引流账号配置弹窗(默认 WA 优先)
 *
 * 与 tiktok-ops.js 的关系:
 *   - 风格、命名、调用方式尽量对齐
 *   - 但 API 路径用 /facebook/* 命名空间,DOM ID 用 fb-* 前缀,避免冲突
 *
 * 与 platform-grid.js 的关系:
 *   - platform-grid.js 提供通用设备网格(已工作)
 *   - facebook-ops.js 在网格之上增加专属指挥栏 + 弹窗
 */

(function () {
  'use strict';

  // ════════════════════════════════════════════════════════
  // 缓存
  // ════════════════════════════════════════════════════════
  let _fbPresets = null;
  let _fbReferrals = null;
  let _fbReferralPriority = null;   // P2-UI Sprint：从 /referral-config 拿
  let _fbActivePersona = null;      // P2-UI Sprint：当前目标客群展示包
  let _fbAvailablePersonas = null;  // 下拉可选客群列表
  let _fbPersonaYamlDefault = null; // GET /active-persona 的 yaml_default_key
  let _fbPersonaOverrideKey = null;   // 运行时 override（若有）
  let _fbInited = false;

  // ════════════════════════════════════════════════════════
  // 公共入口 — 由 overview.js 在导航到 facebook 页时调用
  // ════════════════════════════════════════════════════════
  window.loadFbOpsPanel = async function () {
    try {
      // persona 必须先于其他加载：它决定引流排序、模态默认 GEO 等
      await _fbLoadActivePersona();
      await Promise.all([_fbLoadPresets(), _fbLoadReferrals()]);
      _fbRenderCommandBar();
      if (typeof loadPlatGridPage === 'function') {
        loadPlatGridPage('facebook');
      }
      _fbInited = true;
      // 订阅 phase 变更事件
      _fbListenPhaseChanges();
    } catch (e) {
      console.warn('[facebook-ops] init failed', e);
    }
  };

  // ── 设备阶段变更通知 ──
  var _fbPhaseListenerAttached = false;
  var _PHASE_LABELS = { cold_start: '🧊 冷启动', growth: '🌱 成长期', mature: '🌳 成熟期', cooldown: '❄️ 冷却期' };
  var _PHASE_UNLOCKS = {
    growth: '已解锁: 群组探查、好友拓展',
    mature: '已解锁: 所有方案（含高频操作）',
    cooldown: '建议: 暂停操作，等待恢复'
  };
  function _fbListenPhaseChanges() {
    if (_fbPhaseListenerAttached) return;
    _fbPhaseListenerAttached = true;
    window.addEventListener('oc:event', function (evt) {
      var d = (evt && evt.detail) || {};
      if (d.type !== 'fb.phase_changed') return;
      var data = d.data || d;
      var did = (data.device_id || '').substring(0, 8) || '?';
      var oldL = _PHASE_LABELS[data.old_phase] || data.old_phase;
      var newL = _PHASE_LABELS[data.new_phase] || data.new_phase;
      var unlock = _PHASE_UNLOCKS[data.new_phase] || '';
      var msg = did + '… 阶段变更: ' + oldL + ' → ' + newL;
      if (unlock) msg += ' (' + unlock + ')';
      if (typeof showToast === 'function') {
        showToast(msg, data.new_phase === 'cooldown' ? 'warning' : 'success');
      }
    });
  }

  // ════════════════════════════════════════════════════════
  // 数据加载
  // ════════════════════════════════════════════════════════
  async function _fbLoadPresets(force) {
    if (_fbPresets && !force) return _fbPresets;
    try {
      const r = await api('GET', '/facebook/presets');
      _fbPresets = (r && r.presets) || [];
    } catch (e) {
      _fbPresets = [];
    }
    return _fbPresets;
  }

  async function _fbLoadReferrals() {
    try {
      const r = await api('GET', '/facebook/referral-config');
      _fbReferrals = (r && r.referrals) || {};
      _fbReferralPriority = (r && r.priority_order) || null;
      if (r && r.persona) _fbActivePersona = r.persona;
    } catch (e) {
      _fbReferrals = {};
    }
    return _fbReferrals;
  }

  // P2-UI Sprint：加载目标客群（persona）展示包
  // 合并系统预设 + 用户自建 profile，统一放入 _fbAvailablePersonas
  async function _fbLoadActivePersona(force) {
    if (_fbActivePersona && !force) return _fbActivePersona;
    try {
      const r = await api('GET', '/facebook/active-persona');
      _fbActivePersona = (r && r.active) || null;
      _fbAvailablePersonas = (r && r.available) || [];
      _fbPersonaYamlDefault = (r && r.yaml_default_key) || null;
      _fbPersonaOverrideKey = (r && r.override_key) || null;
    } catch (e) {
      _fbActivePersona = null;
      _fbAvailablePersonas = [];
    }
    // 追加用户自建 profiles
    try {
      const pr = await api('GET', '/facebook/persona-profiles');
      if (pr && pr.profiles) {
        var userProfiles = pr.profiles.filter(function (x) { return x.source === 'user'; });
        // 去重（避免和系统预设重复）
        var existKeys = new Set((_fbAvailablePersonas || []).map(function (x) { return x.persona_key; }));
        userProfiles.forEach(function (up) {
          if (!existKeys.has(up.persona_key)) {
            _fbAvailablePersonas.push(up);
          }
        });
      }
    } catch (e) { /* user profiles 加载失败不影响主流程 */ }
    if (!_fbActivePersona) {
      _fbActivePersona = {
        persona_key: '',
        display_flag: '🌐',
        display_label: '🌐 未配置客群',
        short_label: '未配置',
        country_code: '',
        language: 'en',
        gender: '',
        age_min: 25,
        age_max: 55,
        referral_priority: ['whatsapp', 'telegram', 'instagram', 'line'],
        interest_topics: [],
        seed_group_keywords: [],
      };
    }
    if (!_fbAvailablePersonas || !_fbAvailablePersonas.length) {
      _fbAvailablePersonas = [_fbActivePersona];
    }
    return _fbActivePersona;
  }

  // 渠道元数据（图标/中文名/输入占位符/校验提示）
  // 与后端 fb_target_personas.referral_priority 的 4 个枚举值严格对齐。
  const _FB_CHANNEL_META = {
    line:      { icon: '💚', zh: 'LINE',      placeholder: '@xxxxx 或 https://line.me/...', note: '日本/泰国主力' },
    whatsapp:  { icon: '💬', zh: 'WhatsApp',  placeholder: '+81xxxxxxxxxx',               note: '欧美/东南亚主力' },
    instagram: { icon: '📷', zh: 'Instagram', placeholder: '@username',                   note: '全球年轻女性' },
    telegram:  { icon: '✈️', zh: 'Telegram',  placeholder: '@username',                   note: '技术/加密圈' },
  };

  // ════════════════════════════════════════════════════════
  // 顶部指挥栏 — 渲染到 #fb-cmd-bar(若存在)
  // 退化: 如果模板里没有 #fb-cmd-bar,自动注入到 .plat-page 顶部
  // ════════════════════════════════════════════════════════
  function _fbRenderCommandBar() {
    const page = document.querySelector('#page-plat-facebook .plat-page');
    if (!page) return;

    let bar = document.getElementById('fb-cmd-bar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'fb-cmd-bar';
      bar.style.cssText = 'background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:12px;display:flex;flex-wrap:wrap;align-items:center;gap:10px';
      page.insertBefore(bar, page.firstChild);
    }

    // P2-UI Sprint：
    //   * 顶部从 8 并列按钮 → 3 分组（流程 / 数据 / 配置），
    //     每组之间用竖线分隔，视觉层级更清晰。
    //   * 删除「批量发请求」按钮（它只是 fbOpenPresetsModal 的别名，
    //     和「配置执行流程」功能完全重复）。
    //   * 左侧显示当前目标客群（persona）徽章，一眼看出正在为哪个
    //     人群下发任务；点击徽章切换客群。
    const persona = _fbActivePersona || {
      display_flag: '🌐', short_label: '未配置', display_label: '未配置客群'
    };
    const personaBadge = `
      <span class="qa-btn" onclick="fbOpenPersonaPicker()" title="${persona.display_label}"
        style="cursor:pointer;display:inline-flex;align-items:center;gap:6px;font-size:11px;
        padding:4px 10px;background:rgba(236,72,153,.15);color:#f472b6;border:1px solid rgba(236,72,153,.3);
        border-radius:6px;font-weight:600">
        <span style="font-size:14px">${persona.display_flag}</span>
        <span>目标客群:${persona.short_label}</span>
        <span style="font-size:10px;opacity:.7">▾</span>
      </span>`;

    const groupDivider = '<span style="width:1px;height:20px;background:var(--border);margin:0 4px"></span>';

    bar.innerHTML = `
      <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
        <span style="font-size:11px;padding:4px 10px;background:rgba(24,119,242,.15);color:#60a5fa;border-radius:6px;font-weight:600">
          📘 Facebook 指挥台
        </span>
        ${personaBadge}
      </div>
      <div style="margin-left:auto;display:flex;gap:6px;flex-wrap:wrap;align-items:center">

        <!-- ① 流程：任务下发（最核心，主色强调） -->
        <button class="qa-btn" onclick="fbOpenPresetsModal()" style="background:linear-gradient(135deg,#1877f2,#0d6efd);color:#fff;border:none;font-weight:600;padding:6px 14px;font-size:12px">
          ⚡ 配置执行流程
        </button>

        ${groupDivider}

        <!-- ② 数据：漏斗/风控/线索/画像/日报 —— 只看不改的诊断类 -->
        <button class="qa-btn" onclick="fbOpenFunnelModal()" style="padding:6px 10px;font-size:12px;background:rgba(34,197,94,.15);color:#22c55e">
          📊 漏斗
        </button>
        <button class="qa-btn" onclick="fbOpenRiskModal()" style="padding:6px 10px;font-size:12px;background:rgba(239,68,68,.15);color:#ef4444">
          🛡️ 风控
        </button>
        <button class="qa-btn" onclick="fbOpenLeadsModal()" style="padding:6px 10px;font-size:12px;background:rgba(59,130,246,.15);color:#3b82f6">
          🎯 高分线索
        </button>
        <button class="qa-btn" onclick="fbOpenNameHunterCandidates()" style="padding:6px 10px;font-size:12px;background:rgba(14,165,233,.15);color:#38bdf8">
          🔎 点名候选
        </button>
        <button class="qa-btn" onclick="fbOpenInsightsModal()" style="padding:6px 10px;font-size:12px;background:rgba(139,92,246,.18);color:#a78bfa">
          🧠 画像识别
        </button>
        <button class="qa-btn" onclick="fbOpenDailyBriefModal()" style="padding:6px 10px;font-size:12px;background:rgba(168,85,247,.15);color:#a855f7">
          📰 AI 日报
        </button>
        <button class="qa-btn" onclick="fbOpenDedupModal()" style="padding:6px 10px;font-size:12px;background:rgba(251,191,36,.15);color:#fbbf24">
          🔒 去重防线
        </button>

        ${groupDivider}

        <!-- ③ 配置：引流账号/文案（低频改动） -->
        <button class="qa-btn" onclick="fbOpenReferralModal()" style="padding:6px 10px;font-size:12px">
          🔗 引流账号
        </button>

        ${groupDivider}

        <!-- ④ Lead Mesh 交接 / 档案 / 指挥台 (Phase 5.5 新增, 跨 Agent 协同) -->
        <button class="qa-btn" onclick="lmOpenHandoffInbox()" style="padding:6px 10px;font-size:12px;background:rgba(245,158,11,.15);color:#f59e0b">
          🤝 接收方工作台
        </button>
        <button class="qa-btn" onclick="lmOpenLeadSearch()" style="padding:6px 10px;font-size:12px;background:rgba(14,165,233,.15);color:#0ea5e9">
          🔍 Lead 档案
        </button>
        <button class="qa-btn" onclick="lmOpenCommandCenter()" style="padding:6px 10px;font-size:12px;background:rgba(168,85,247,.15);color:#a855f7">
          📊 运营指挥台
        </button>
        <button class="qa-btn" onclick="lmOpenReceiversConfig()" style="padding:6px 10px;font-size:12px;background:rgba(34,197,94,.15);color:#22c55e">
          📬 接收方管理
        </button>
        <button class="qa-btn" onclick="lmOpenMergeAudit()" style="padding:6px 10px;font-size:12px;background:rgba(251,191,36,.15);color:#eab308">
          🔗 合并审计
        </button>
        <button class="qa-btn" onclick="lmOpenIdentityKPI()" style="padding:6px 10px;font-size:12px;background:rgba(99,102,241,.15);color:#818cf8">
          🌐 身份 KPI
        </button>
      </div>
    `;
  }

  // ════════════════════════════════════════════════════════
  // P2-4 Sprint B: 画像识别 Dashboard（读 /facebook/insights/stats）
  // ════════════════════════════════════════════════════════
  window.fbOpenInsightsModal = async function (hours) {
    const h = hours || 24;
    const overlay = _fbModalOverlay('fb-insights-modal');
    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:920px;width:95%;max-height:86vh;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div>
            <div style="font-size:18px;font-weight:700">🧠 画像识别 Dashboard</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">最近 <span id="fb-ins-hours">${h}</span> 小时 L1/L2/命中 + 分画像/分设备/成本</div>
          </div>
          <div style="display:flex;gap:6px;align-items:center">
            <select id="fb-ins-hours-sel" onchange="fbOpenInsightsModal(parseInt(this.value))" style="background:var(--bg-main);border:1px solid var(--border);border-radius:6px;color:var(--text);padding:4px 8px;font-size:12px">
              <option value="1" ${h===1?'selected':''}>1 小时</option>
              <option value="6" ${h===6?'selected':''}>6 小时</option>
              <option value="24" ${h===24?'selected':''}>24 小时</option>
              <option value="168" ${h===168?'selected':''}>7 天</option>
            </select>
            <button onclick="document.getElementById('fb-insights-modal').remove()" style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
          </div>
        </div>
        <div id="fb-ins-body" style="font-size:13px;color:var(--text-dim)">加载中…</div>
      </div>
    `;
    try {
      const r = await api('GET', `/facebook/insights/stats?hours=${h}`);
      const body = document.getElementById('fb-ins-body');
      if (!body) return;
      if (!r || !r.ok) { body.innerHTML = '<div style="color:#ef4444">接口返回异常</div>'; return; }
      const t = r.totals || {};
      const kpi = [
        ['L1 扫描总数', (t.l1 || 0), '#60a5fa'],
        ['L2 深判总数', (t.l2 || 0), '#8b5cf6'],
        ['命中', (t.matched || 0), '#22c55e'],
        ['L1→L2 转化率', ((t.l1_to_l2_rate || 0) * 100).toFixed(1) + '%', '#f59e0b'],
        ['L2 命中率', ((t.l2_match_rate || 0) * 100).toFixed(1) + '%', '#22c55e'],
        ['L2 平均耗时', (t.avg_l2_latency_ms || 0) + ' ms', '#94a3b8'],
      ].map(([lbl, v, c]) => `
        <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px 12px">
          <div style="color:var(--text-dim);font-size:10px">${lbl}</div>
          <div style="font-weight:700;font-size:18px;color:${c};margin-top:4px">${v}</div>
        </div>
      `).join('');
      const byPersona = (r.by_persona || []).map(p => `
        <tr><td style="padding:6px 10px">${p.persona_key}</td><td style="text-align:right;padding:6px 10px">${p.l1}</td><td style="text-align:right;padding:6px 10px">${p.l2}</td><td style="text-align:right;padding:6px 10px;color:#22c55e;font-weight:600">${p.matched}</td></tr>
      `).join('') || '<tr><td colspan="4" style="padding:8px;color:var(--text-muted);text-align:center">暂无数据</td></tr>';
      const byDev = (r.top_devices || []).slice(0, 10).map(d => {
        const alias = (typeof ALIAS !== 'undefined' ? (ALIAS[d.device_id] || d.device_id.substring(0, 8)) : d.device_id.substring(0, 8));
        return `<tr><td style="padding:6px 10px">${alias}</td><td style="text-align:right;padding:6px 10px">${d.l1}</td><td style="text-align:right;padding:6px 10px">${d.l2}</td><td style="text-align:right;padding:6px 10px;color:#22c55e">${d.matched}</td></tr>`;
      }).join('') || '<tr><td colspan="4" style="padding:8px;color:var(--text-muted);text-align:center">暂无设备数据</td></tr>';
      const costRows = (r.ai_cost || []).map(c => {
        const waitStr = (c.avg_queue_wait_ms != null)
          ? `<span title="本窗口平均排队 / 峰值" style="color:${c.avg_queue_wait_ms>1000?'#f59e0b':'var(--text-dim)'}">${c.avg_queue_wait_ms}ms / ${c.peak_queue_wait_ms}ms</span>`
          : '<span style="color:var(--text-muted)">—</span>';
        return `<tr><td style="padding:6px 10px">${c.provider}/${c.model}</td><td style="padding:6px 10px">${c.scene || '—'}</td><td style="text-align:right;padding:6px 10px">${c.count}</td><td style="text-align:right;padding:6px 10px">${c.avg_latency_ms || 0} ms</td><td style="text-align:right;padding:6px 10px">${waitStr}</td><td style="text-align:right;padding:6px 10px">$${(c.total_usd || 0).toFixed(4)}</td></tr>`;
      }).join('') || '<tr><td colspan="6" style="padding:8px;color:var(--text-muted);text-align:center">暂无成本数据</td></tr>';
      const conc = r.vlm_concurrency || {};
      const concBadge = (conc.total_calls > 0)
        ? `<div style="font-size:10px;color:var(--text-dim);margin-top:6px">VLM 并发 · 累计调用 <b>${conc.total_calls}</b> · 峰值等待 <b style="color:${conc.peak_wait_ms>2000?'#ef4444':'#a78bfa'}">${conc.peak_wait_ms}ms</b> · 平均等待 <b>${conc.total_calls?Math.round(conc.total_wait_ms/conc.total_calls):0}ms</b></div>`
        : '';
      body.innerHTML = `
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(130px,1fr));gap:8px;margin-bottom:14px">${kpi}</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:10px;padding:10px">
            <div style="font-size:12px;font-weight:600;color:var(--text-dim);margin-bottom:6px">分画像</div>
            <table style="width:100%;border-collapse:collapse;font-size:11px">
              <thead><tr style="color:var(--text-muted)"><th style="text-align:left;padding:4px 10px;font-weight:500">画像</th><th style="text-align:right;padding:4px 10px;font-weight:500">L1</th><th style="text-align:right;padding:4px 10px;font-weight:500">L2</th><th style="text-align:right;padding:4px 10px;font-weight:500">命中</th></tr></thead>
              <tbody>${byPersona}</tbody>
            </table>
          </div>
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:10px;padding:10px">
            <div style="font-size:12px;font-weight:600;color:var(--text-dim);margin-bottom:6px">分设备（Top 10）</div>
            <table style="width:100%;border-collapse:collapse;font-size:11px">
              <thead><tr style="color:var(--text-muted)"><th style="text-align:left;padding:4px 10px;font-weight:500">设备</th><th style="text-align:right;padding:4px 10px;font-weight:500">L1</th><th style="text-align:right;padding:4px 10px;font-weight:500">L2</th><th style="text-align:right;padding:4px 10px;font-weight:500">命中</th></tr></thead>
              <tbody>${byDev}</tbody>
            </table>
          </div>
        </div>
        <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:10px;padding:10px;margin-top:12px">
          <div style="font-size:12px;font-weight:600;color:var(--text-dim);margin-bottom:6px">AI 成本 & 延迟（本地 VLM = $0）</div>
          <table style="width:100%;border-collapse:collapse;font-size:11px">
            <thead><tr style="color:var(--text-muted)"><th style="text-align:left;padding:4px 10px;font-weight:500">模型</th><th style="text-align:left;padding:4px 10px;font-weight:500">场景</th><th style="text-align:right;padding:4px 10px;font-weight:500">调用数</th><th style="text-align:right;padding:4px 10px;font-weight:500">平均耗时</th><th style="text-align:right;padding:4px 10px;font-weight:500">平均/峰值排队</th><th style="text-align:right;padding:4px 10px;font-weight:500">累计</th></tr></thead>
            <tbody>${costRows}</tbody>
          </table>
          ${concBadge}
        </div>
      `;
    } catch (e) {
      const body = document.getElementById('fb-ins-body');
      if (body) body.innerHTML = `<div style="color:#ef4444">加载失败: ${e.message || e}</div>`;
    }
  };

  // ════════════════════════════════════════════════════════
  // 目标客群（persona）列表
  // P2-UI Sprint：
  //   原先这里是 FB_QUICK_GEO —— 一个散装 GEO 列表 + 另一个 persona_key
  //   输入框，容易出现「IT + jp_female_midlife」这类错配。
  //
  //   现在改为从 /facebook/active-persona 读后端 YAML 定义好的客群，
  //   一次锁定「国家 + 年龄 + 性别 + 兴趣 + 引流优先级」，
  //   前端不再可能凭空组合。添加/修改客群 → 改 fb_target_personas.yaml
  //   即可热加载生效，不必改前端。
  // ════════════════════════════════════════════════════════
  // 本地仅保留一个最小回退项，真正列表来自 _fbAvailablePersonas。
  const _FB_PERSONA_FALLBACK = [
    { persona_key: '', display_flag: '🌐', display_label: '🌐 全球(未绑定客群)',
      short_label: '全球', country_code: '', language: 'en' },
  ];

  function _fbPersonaOptions() {
    const arr = (_fbAvailablePersonas && _fbAvailablePersonas.length)
      ? _fbAvailablePersonas
      : _FB_PERSONA_FALLBACK;
    const activeKey = (_fbActivePersona && _fbActivePersona.persona_key) || '';
    return arr.map(function (p) {
      const sel = (p.persona_key === activeKey) ? 'selected' : '';
      return `<option value="${p.persona_key}" ${sel}>${p.display_label}</option>`;
    }).join('');
  }

  // ── 国家列表（常用 + 全球覆盖） ──
  var _FB_COUNTRIES = [
    { code: 'JP', flag: '🇯🇵', zh: '日本', lang: 'ja' },
    { code: 'CN', flag: '🇨🇳', zh: '中国', lang: 'zh' },
    { code: 'TW', flag: '🇹🇼', zh: '台湾', lang: 'zh' },
    { code: 'HK', flag: '🇭🇰', zh: '香港', lang: 'zh' },
    { code: 'KR', flag: '🇰🇷', zh: '韩国', lang: 'ko' },
    { code: 'TH', flag: '🇹🇭', zh: '泰国', lang: 'th' },
    { code: 'VN', flag: '🇻🇳', zh: '越南', lang: 'vi' },
    { code: 'PH', flag: '🇵🇭', zh: '菲律宾', lang: 'en' },
    { code: 'MY', flag: '🇲🇾', zh: '马来西亚', lang: 'ms' },
    { code: 'SG', flag: '🇸🇬', zh: '新加坡', lang: 'en' },
    { code: 'ID', flag: '🇮🇩', zh: '印尼', lang: 'id' },
    { code: 'US', flag: '🇺🇸', zh: '美国', lang: 'en' },
    { code: 'GB', flag: '🇬🇧', zh: '英国', lang: 'en' },
    { code: 'AU', flag: '🇦🇺', zh: '澳洲', lang: 'en' },
    { code: 'CA', flag: '🇨🇦', zh: '加拿大', lang: 'en' },
    { code: 'DE', flag: '🇩🇪', zh: '德国', lang: 'de' },
    { code: 'FR', flag: '🇫🇷', zh: '法国', lang: 'fr' },
    { code: 'BR', flag: '🇧🇷', zh: '巴西', lang: 'pt' },
    { code: '', flag: '🌐', zh: '全球/不限', lang: 'en' },
  ];

  // ── 渲染客群构建器面板 ──
  function _fbRenderPersonaBuilder() {
    const p = _fbActivePersona || {};
    const activeKey = p.persona_key || '';
    const allProfiles = (_fbAvailablePersonas || []);

    // 预设卡片（增强：性别/年龄徽章 + 右键菜单）
    const presetCards = allProfiles.map(function (pr) {
      const isActive = pr.persona_key === activeKey;
      const border = isActive ? 'border-color:rgba(24,119,242,.6);background:rgba(24,119,242,.08)' : '';
      const gIcon = pr.gender === 'female' ? '♀' : pr.gender === 'male' ? '♂' : '';
      const topicCount = (pr.interest_topics || []).length;
      const kwCount = (pr.seed_group_keywords || []).length;
      const metaLine = [
        gIcon,
        (pr.age_min && pr.age_max) ? pr.age_min + '-' + pr.age_max : '',
        topicCount ? topicCount + '标签' : '',
        kwCount ? kwCount + '词' : '',
      ].filter(Boolean).join(' · ');
      return `<button class="fb-persona-preset-btn" data-key="${pr.persona_key}" onclick="fbSelectPersonaPreset('${pr.persona_key}')" oncontextmenu="event.preventDefault();fbPresetContextMenu(event,'${pr.persona_key}')"
        style="padding:6px 10px;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);cursor:pointer;font-size:11px;color:var(--text);transition:all .15s;display:flex;align-items:center;gap:6px;white-space:nowrap;${border};position:relative"
        onmouseover="if(!this.dataset.active)this.style.borderColor='rgba(24,119,242,.4)'" onmouseout="if(!this.dataset.active)this.style.borderColor='var(--border)'"
        ${isActive ? 'data-active="1"' : ''}>
        <span style="font-size:14px">${pr.display_flag || '🌐'}</span>
        <div style="text-align:left">
          <div style="line-height:1.2">${pr.short_label || pr.name || pr.persona_key}</div>
          ${metaLine ? '<div style="font-size:8px;color:var(--text-dim);line-height:1.1;margin-top:1px">' + metaLine + '</div>' : ''}
        </div>
        ${pr.source === 'user' ? '<span style="font-size:8px;background:rgba(168,85,247,.15);color:#c084fc;padding:1px 4px;border-radius:3px;position:absolute;top:2px;right:2px">自建</span>' : ''}
      </button>`;
    }).join('');

    // 国家选项
    const countryOpts = _FB_COUNTRIES.map(function (c) {
      const sel = (c.code === (p.country_code || '')) ? 'selected' : '';
      return `<option value="${c.code}" ${sel}>${c.flag} ${c.zh}</option>`;
    }).join('');

    // 当前兴趣标签
    const topics = (p.interest_topics || []);
    const topicChips = topics.map(function (t, i) {
      return `<span class="fb-chip" style="display:inline-flex;align-items:center;gap:3px;padding:3px 8px;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.25);border-radius:12px;font-size:10px;color:#4ade80">
        ${t}<button onclick="fbRemoveInterest(${i})" style="background:none;border:none;color:#4ade80;cursor:pointer;font-size:10px;padding:0 2px;opacity:.7">×</button>
      </span>`;
    }).join('');

    // 群组关键词
    const keywords = (p.seed_group_keywords || []);
    const keywordChips = keywords.map(function (k, i) {
      return `<span class="fb-chip" style="display:inline-flex;align-items:center;gap:3px;padding:3px 8px;background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.25);border-radius:12px;font-size:10px;color:#60a5fa">
        ${k}<button onclick="fbRemoveKeyword(${i})" style="background:none;border:none;color:#60a5fa;cursor:pointer;font-size:10px;padding:0 2px;opacity:.7">×</button>
      </span>`;
    }).join('');

    const ageMin = p.age_min || 18;
    const ageMax = p.age_max || 65;
    const gender = p.gender || '';
    const isUserProfile = activeKey && allProfiles.some(function (x) { return x.persona_key === activeKey && x.source === 'user'; });
    const deleteDisabled = activeKey && !isUserProfile ? 'opacity:.4;pointer-events:none' : '';

    return `
      <div id="fb-persona-builder" style="border:1px solid var(--border);border-radius:10px;overflow:hidden;margin-bottom:8px;background:var(--bg-main)">
        <!-- 收起状态：紧凑摘要 -->
        <div id="fb-persona-compact" style="padding:10px 14px;display:flex;align-items:center;gap:10px;cursor:pointer" onclick="fbTogglePersonaBuilder()">
          <span style="font-size:18px">${p.display_flag || '🌐'}</span>
          <div style="flex:1;min-width:0">
            <div style="font-size:12px;font-weight:600;color:var(--text)">${p.display_label || '点击配置客群'}</div>
            <div style="font-size:10px;color:var(--text-dim);margin-top:1px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
              ${gender === 'female' ? '♀ 女性' : gender === 'male' ? '♂ 男性' : '⚥ 不限'}
              · ${ageMin}-${ageMax}岁
              · ${topics.slice(0, 3).join(' · ') || '未设兴趣'}
            </div>
          </div>
          <span id="fb-persona-chevron" style="font-size:12px;color:var(--text-dim);transition:transform .2s">▼</span>
          <button onclick="event.stopPropagation();fbOpenGreetingLibrary()" style="padding:3px 10px;background:rgba(168,85,247,.1);border:1px solid rgba(168,85,247,.3);color:#c084fc;border-radius:6px;cursor:pointer;font-size:9px;font-weight:600;white-space:nowrap;transition:all .12s" onmouseover="this.style.background='rgba(168,85,247,.2)'" onmouseout="this.style.background='rgba(168,85,247,.1)'">📚 话术库</button>
        </div>

        <!-- 展开状态：完整构建器 -->
        <div id="fb-persona-expanded" style="display:none;padding:0 14px 14px;border-top:1px solid var(--border)">
          <!-- 预设快选 -->
          <div style="padding:12px 0 10px">
            <div style="font-size:10px;color:var(--text-dim);margin-bottom:6px;font-weight:600">⚡ 快速预设</div>
            <div style="display:flex;flex-wrap:wrap;gap:6px">
              ${presetCards}
              <button onclick="fbCreateNewPersona()" style="padding:6px 12px;border:1px dashed var(--border);border-radius:8px;background:none;cursor:pointer;font-size:11px;color:var(--text-dim);transition:all .15s;display:flex;align-items:center;gap:4px" onmouseover="this.style.borderColor='#22c55e';this.style.color='#22c55e'" onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">
                <span>＋</span> 新建
              </button>
            </div>
          </div>

          <!-- 基本筛选 -->
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;padding:10px 0;border-top:1px solid rgba(255,255,255,.05)">
            <div>
              <label style="font-size:10px;color:var(--text-dim);font-weight:600;display:block;margin-bottom:4px">🌍 国家/地区</label>
              <select id="fb-pb-country" onchange="fbPersonaFieldChange()" style="width:100%;background:var(--bg-card);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:11px">
                ${countryOpts}
              </select>
            </div>
            <div>
              <label style="font-size:10px;color:var(--text-dim);font-weight:600;display:block;margin-bottom:4px">🗣 语言</label>
              <select id="fb-pb-language" onchange="fbPersonaFieldChange()" style="width:100%;background:var(--bg-card);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:11px">
                <option value="ja" ${p.language === 'ja' ? 'selected' : ''}>日本語</option>
                <option value="zh" ${p.language === 'zh' ? 'selected' : ''}>中文</option>
                <option value="en" ${p.language === 'en' ? 'selected' : ''}>English</option>
                <option value="ko" ${p.language === 'ko' ? 'selected' : ''}>한국어</option>
                <option value="th" ${p.language === 'th' ? 'selected' : ''}>ภาษาไทย</option>
                <option value="vi" ${p.language === 'vi' ? 'selected' : ''}>Tiếng Việt</option>
                <option value="id" ${p.language === 'id' ? 'selected' : ''}>Bahasa</option>
              </select>
            </div>
          </div>

          <!-- 性别 + 年龄 -->
          <div style="display:grid;grid-template-columns:auto 1fr;gap:14px;align-items:start;padding:10px 0;border-top:1px solid rgba(255,255,255,.05)">
            <div>
              <label style="font-size:10px;color:var(--text-dim);font-weight:600;display:block;margin-bottom:6px">⚥ 性别</label>
              <div style="display:flex;gap:4px">
                <button onclick="fbSetGender('female')" id="fb-pb-g-female" class="fb-gender-btn" style="padding:5px 12px;border-radius:6px;border:1px solid ${gender === 'female' ? 'rgba(236,72,153,.5)' : 'var(--border)'};background:${gender === 'female' ? 'rgba(236,72,153,.1)' : 'var(--bg-card)'};color:${gender === 'female' ? '#ec4899' : 'var(--text-dim)'};cursor:pointer;font-size:11px;transition:all .12s">♀ 女</button>
                <button onclick="fbSetGender('male')" id="fb-pb-g-male" class="fb-gender-btn" style="padding:5px 12px;border-radius:6px;border:1px solid ${gender === 'male' ? 'rgba(59,130,246,.5)' : 'var(--border)'};background:${gender === 'male' ? 'rgba(59,130,246,.1)' : 'var(--bg-card)'};color:${gender === 'male' ? '#3b82f6' : 'var(--text-dim)'};cursor:pointer;font-size:11px;transition:all .12s">♂ 男</button>
                <button onclick="fbSetGender('')" id="fb-pb-g-all" class="fb-gender-btn" style="padding:5px 12px;border-radius:6px;border:1px solid ${gender === '' ? 'rgba(168,85,247,.5)' : 'var(--border)'};background:${gender === '' ? 'rgba(168,85,247,.1)' : 'var(--bg-card)'};color:${gender === '' ? '#a855f7' : 'var(--text-dim)'};cursor:pointer;font-size:11px;transition:all .12s">⚥ 不限</button>
              </div>
            </div>
            <div>
              <label style="font-size:10px;color:var(--text-dim);font-weight:600;display:block;margin-bottom:6px">🎂 年龄范围: <span id="fb-pb-age-label" style="color:var(--text)">${ageMin} ~ ${ageMax} 岁</span></label>
              <div style="display:flex;align-items:center;gap:8px">
                <input type="range" id="fb-pb-age-min" min="18" max="70" value="${ageMin}" oninput="fbAgeChange()" style="flex:1;accent-color:#f59e0b">
                <input type="range" id="fb-pb-age-max" min="18" max="70" value="${ageMax}" oninput="fbAgeChange()" style="flex:1;accent-color:#f59e0b">
              </div>
            </div>
          </div>

          <!-- 一键智能填充 -->
          <div style="padding:8px 0;border-top:1px solid rgba(255,255,255,.05)">
            <button onclick="fbSmartFill()" style="width:100%;padding:7px;background:linear-gradient(135deg,rgba(168,85,247,.08),rgba(59,130,246,.08));border:1px dashed rgba(168,85,247,.3);border-radius:8px;color:#c084fc;cursor:pointer;font-size:10px;font-weight:600;transition:all .15s" onmouseover="this.style.background='linear-gradient(135deg,rgba(168,85,247,.15),rgba(59,130,246,.15))';this.style.borderStyle='solid'" onmouseout="this.style.background='linear-gradient(135deg,rgba(168,85,247,.08),rgba(59,130,246,.08))';this.style.borderStyle='dashed'">✨ 一键智能填充（基于国家+性别推荐兴趣和关键词）</button>
          </div>

          <!-- 兴趣标签 -->
          <div style="padding:10px 0;border-top:1px solid rgba(255,255,255,.05)">
            <label style="font-size:10px;color:var(--text-dim);font-weight:600;display:block;margin-bottom:6px">🏷 兴趣标签</label>
            <div id="fb-pb-topics" style="display:flex;flex-wrap:wrap;gap:4px;min-height:24px">
              ${topicChips}
              <button onclick="fbAddInterestPrompt()" style="padding:3px 8px;background:none;border:1px dashed rgba(34,197,94,.4);border-radius:12px;color:#4ade80;cursor:pointer;font-size:10px;transition:all .12s" onmouseover="this.style.background='rgba(34,197,94,.08)'" onmouseout="this.style.background='none'">✨ 智能推荐</button>
            </div>
          </div>

          <!-- 群组关键词 -->
          <div style="padding:10px 0;border-top:1px solid rgba(255,255,255,.05)">
            <label style="font-size:10px;color:var(--text-dim);font-weight:600;display:block;margin-bottom:6px">🔍 群组搜索关键词</label>
            <div id="fb-pb-keywords" style="display:flex;flex-wrap:wrap;gap:4px;min-height:24px">
              ${keywordChips}
              <button onclick="fbAddKeywordPrompt()" style="padding:3px 8px;background:none;border:1px dashed rgba(59,130,246,.4);border-radius:12px;color:#60a5fa;cursor:pointer;font-size:10px;transition:all .12s" onmouseover="this.style.background='rgba(59,130,246,.08)'" onmouseout="this.style.background='none'">✨ 智能推荐</button>
            </div>
          </div>

          <!-- 效果预览 -->
          <div id="fb-pb-stats" style="padding:10px 0;border-top:1px solid rgba(255,255,255,.05)">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:8px">
              <span style="font-size:10px;color:var(--text-dim);font-weight:600">📊 效果预览</span>
              <button onclick="fbLoadPersonaStats()" style="background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:9px;padding:2px 6px;border-radius:4px;transition:all .12s" onmouseover="this.style.color='#60a5fa'" onmouseout="this.style.color='var(--text-dim)'">刷新</button>
            </div>
            <div id="fb-pb-stats-content" style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px">
              <div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px;border:1px solid var(--border)">
                <div style="font-size:14px;font-weight:700;color:var(--text)" id="fb-stat-targets">—</div>
                <div style="font-size:9px;color:var(--text-dim)">候选人</div>
              </div>
              <div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px;border:1px solid var(--border)">
                <div style="font-size:14px;font-weight:700;color:#22c55e" id="fb-stat-friended">—</div>
                <div style="font-size:9px;color:var(--text-dim)">已加好友</div>
              </div>
              <div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px;border:1px solid var(--border)">
                <div style="font-size:14px;font-weight:700;color:#f59e0b" id="fb-stat-sent">—</div>
                <div style="font-size:9px;color:var(--text-dim)">已发话术</div>
              </div>
              <div style="text-align:center;padding:6px;background:var(--bg-card);border-radius:6px;border:1px solid var(--border)">
                <div style="font-size:14px;font-weight:700;color:#a855f7" id="fb-stat-reply-rate">—</div>
                <div style="font-size:9px;color:var(--text-dim)">回复率</div>
              </div>
            </div>
            <div id="fb-pb-funnel" style="margin-top:8px;display:none">
              <div style="display:flex;align-items:center;gap:4px;font-size:9px;color:var(--text-dim);margin-bottom:3px">
                <span>漏斗</span>
                <span style="flex:1;height:1px;background:var(--border)"></span>
              </div>
              <div style="display:flex;align-items:center;height:18px;border-radius:4px;overflow:hidden;background:rgba(255,255,255,.03);border:1px solid var(--border)">
                <div id="fb-funnel-discovered" style="height:100%;background:rgba(99,102,241,.4);transition:width .4s" title="发现"></div>
                <div id="fb-funnel-friended" style="height:100%;background:rgba(34,197,94,.5);transition:width .4s" title="好友"></div>
                <div id="fb-funnel-greeted" style="height:100%;background:rgba(245,158,11,.5);transition:width .4s" title="已触达"></div>
                <div id="fb-funnel-qualified" style="height:100%;background:rgba(168,85,247,.5);transition:width .4s" title="合格"></div>
              </div>
              <div style="display:flex;justify-content:space-between;font-size:8px;color:var(--text-dim);margin-top:2px;padding:0 2px">
                <span>🔍 发现</span><span>🤝 好友</span><span>💬 触达</span><span>✅ 合格</span>
              </div>
            </div>
            <div id="fb-pb-best-greeting" style="margin-top:6px;display:none;padding:6px 10px;background:rgba(34,197,94,.05);border:1px solid rgba(34,197,94,.2);border-radius:6px;font-size:10px;color:var(--text-dim)"></div>
            <!-- 跨客群对比 -->
            <div style="margin-top:8px">
              <button onclick="fbToggleCompare()" id="fb-compare-toggle" style="background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:9px;padding:2px 0;transition:color .1s" onmouseover="this.style.color='#c084fc'" onmouseout="this.style.color='var(--text-dim)'">📊 查看全部客群对比 ▸</button>
              <div id="fb-compare-panel" style="display:none;margin-top:6px"></div>
            </div>
          </div>

          <!-- 操作栏 -->
          <div style="padding:10px 0 4px;border-top:1px solid rgba(255,255,255,.05);display:flex;align-items:center;gap:6px;flex-wrap:wrap">
            <button onclick="fbSavePersonaProfile()" style="padding:5px 14px;background:linear-gradient(135deg,#1877f2,#0d6efd);border:none;color:#fff;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600;transition:all .12s;box-shadow:0 2px 8px rgba(24,119,242,.3)" onmouseover="this.style.transform='translateY(-1px)'" onmouseout="this.style.transform=''">💾 保存</button>
            <button onclick="fbClonePersona()" style="padding:5px 10px;background:var(--bg-card);border:1px solid var(--border);color:var(--text-dim);border-radius:6px;cursor:pointer;font-size:10px;transition:all .12s" onmouseover="this.style.borderColor='#22c55e';this.style.color='#22c55e'" onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">📋 克隆</button>
            <button onclick="fbDeletePersona()" id="fb-pb-delete-btn" style="padding:5px 10px;background:var(--bg-card);border:1px solid var(--border);color:var(--text-dim);border-radius:6px;cursor:pointer;font-size:10px;transition:all .12s;${deleteDisabled}" onmouseover="this.style.borderColor='#ef4444';this.style.color='#ef4444'" onmouseout="this.style.borderColor='var(--border)';this.style.color='var(--text-dim)'">🗑 删除</button>
            <button onclick="fbTogglePersonaBuilder()" style="padding:5px 10px;background:var(--bg-card);border:1px solid var(--border);color:var(--text-dim);border-radius:6px;cursor:pointer;font-size:10px;transition:all .12s">收起 ▲</button>
            <span style="flex:1"></span>
            <span style="font-size:9px;color:var(--text-dim)">修改即时生效 · 保存可复用</span>
          </div>
        </div>
      </div>`;
  }

  // ── 客群构建器交互函数 ──
  var _fbPersonaBuilderExpanded = false;

  window.fbTogglePersonaBuilder = function () {
    _fbPersonaBuilderExpanded = !_fbPersonaBuilderExpanded;
    var expanded = document.getElementById('fb-persona-expanded');
    var chevron = document.getElementById('fb-persona-chevron');
    if (expanded) expanded.style.display = _fbPersonaBuilderExpanded ? 'block' : 'none';
    if (chevron) chevron.style.transform = _fbPersonaBuilderExpanded ? 'rotate(180deg)' : '';
    // 展开时自动加载统计
    if (_fbPersonaBuilderExpanded) fbLoadPersonaStats();
  };

  window.fbSelectPersonaPreset = function (key) {
    var p = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === key; });
    if (p) {
      _fbActivePersona = p;
      // 重新渲染构建器
      var container = document.getElementById('fb-persona-builder');
      if (container) {
        container.outerHTML = _fbRenderPersonaBuilder();
        _fbPersonaBuilderExpanded = true;
        var expanded = document.getElementById('fb-persona-expanded');
        var chevron = document.getElementById('fb-persona-chevron');
        if (expanded) expanded.style.display = 'block';
        if (chevron) chevron.style.transform = 'rotate(180deg)';
      }
      // 切换后刷新统计
      fbLoadPersonaStats();
    }
    // 更新 GEO hint
    var geoHint = document.getElementById('fb-geo-hint');
    if (geoHint && p) geoHint.textContent = p.country_code || '目标';
  };

  window.fbSetGender = function (g) {
    if (_fbActivePersona) _fbActivePersona.gender = g;
    // 视觉更新
    ['female', 'male', ''].forEach(function (v) {
      var btn = document.getElementById('fb-pb-g-' + (v || 'all'));
      if (!btn) return;
      var active = v === g;
      var colors = { female: ['236,72,153', '#ec4899'], male: ['59,130,246', '#3b82f6'], '': ['168,85,247', '#a855f7'] };
      var c = colors[v];
      btn.style.borderColor = active ? 'rgba(' + c[0] + ',.5)' : 'var(--border)';
      btn.style.background = active ? 'rgba(' + c[0] + ',.1)' : 'var(--bg-card)';
      btn.style.color = active ? c[1] : 'var(--text-dim)';
    });
    _fbUpdateCompactSummary();
    _fbMarkDirty();
  };

  window.fbAgeChange = function () {
    var minEl = document.getElementById('fb-pb-age-min');
    var maxEl = document.getElementById('fb-pb-age-max');
    if (!minEl || !maxEl) return;
    var mn = parseInt(minEl.value), mx = parseInt(maxEl.value);
    if (mn > mx) { var tmp = mn; mn = mx; mx = tmp; minEl.value = mn; maxEl.value = mx; }
    var label = document.getElementById('fb-pb-age-label');
    if (label) label.textContent = mn + ' ~ ' + mx + ' 岁';
    if (_fbActivePersona) { _fbActivePersona.age_min = mn; _fbActivePersona.age_max = mx; }
    _fbUpdateCompactSummary();
    _fbMarkDirty();
  };

  window.fbPersonaFieldChange = function () {
    var countryEl = document.getElementById('fb-pb-country');
    var langEl = document.getElementById('fb-pb-language');
    if (countryEl && _fbActivePersona) {
      var cc = countryEl.value;
      var oldCC = _fbActivePersona.country_code;
      _fbActivePersona.country_code = cc;
      var cObj = _FB_COUNTRIES.find(function (c) { return c.code === cc; });
      if (cObj) {
        _fbActivePersona.display_flag = cObj.flag;
        _fbActivePersona.country_zh = cObj.zh;
        if (langEl) { langEl.value = cObj.lang; _fbActivePersona.language = cObj.lang; }
      }
      // 国家切换时清空推荐缓存，确保下次推荐基于新国家
      if (cc !== oldCC) _fbSuggestCache = {};
    }
    if (langEl && _fbActivePersona) _fbActivePersona.language = langEl.value;
    _fbUpdateCompactSummary();
    _fbMarkDirty();
  };

  window.fbRemoveInterest = function (idx) {
    if (!_fbActivePersona || !_fbActivePersona.interest_topics) return;
    _fbActivePersona.interest_topics.splice(idx, 1);
    _fbRefreshChips();
    _fbMarkDirty();
  };

  window.fbRemoveKeyword = function (idx) {
    if (!_fbActivePersona || !_fbActivePersona.seed_group_keywords) return;
    _fbActivePersona.seed_group_keywords.splice(idx, 1);
    _fbRefreshChips();
    _fbMarkDirty();
  };

  window.fbAddInterestPrompt = function () {
    _fbShowSuggestPopup('topics');
  };

  window.fbAddKeywordPrompt = function () {
    _fbShowSuggestPopup('keywords');
  };

  // ── 智能推荐弹出面板 ──
  var _fbSuggestCache = {};
  var _FB_SUGGEST_CACHE_TTL = 60000; // 60s

  async function _fbShowSuggestPopup(type) {
    var p = _fbActivePersona || {};
    var cacheKey = (p.country_code || '') + '|' + (p.gender || '') + '|' + type + '|' + (p.interest_topics || []).join(',');

    // 检查缓存
    var cached = _fbSuggestCache[cacheKey];
    if (cached && (Date.now() - cached.ts < _FB_SUGGEST_CACHE_TTL)) {
      _fbRenderSuggestPopup(type, cached.data);
      return;
    }

    // 请求推荐
    try {
      var body = {
        country_code: p.country_code || '',
        gender: p.gender || '',
        current_topics: p.interest_topics || [],
        type: type,
      };
      var r = await api('POST', '/facebook/persona-profiles/suggest', body);
      if (r) {
        _fbSuggestCache[cacheKey] = { ts: Date.now(), data: r };
        _fbRenderSuggestPopup(type, r);
      }
    } catch (e) {
      // 降级为手动输入
      _fbFallbackManualInput(type);
    }
  }

  function _fbRenderSuggestPopup(type, data) {
    // 移除旧弹窗
    var old = document.getElementById('fb-suggest-popup');
    if (old) old.remove();

    var items = type === 'topics' ? (data.topics || []) : (data.keywords || []);
    var title = type === 'topics' ? '🏷 推荐兴趣标签' : '🔍 推荐群组关键词';
    var color = type === 'topics' ? '#4ade80' : '#60a5fa';
    var bgColor = type === 'topics' ? 'rgba(34,197,94,' : 'rgba(59,130,246,';

    var chipHtml = items.map(function (item) {
      return '<button class="fb-suggest-chip" data-val="' + _escAttr(item) + '" onclick="fbPickSuggestion(\'' + _escAttr(item) + '\',\'' + type + '\')"'
        + ' style="padding:4px 10px;background:' + bgColor + '.06);border:1px solid ' + bgColor + '.25);border-radius:14px;color:' + color + ';cursor:pointer;font-size:10px;transition:all .12s;white-space:nowrap"'
        + ' onmouseover="this.style.background=\'' + bgColor + '.15)\'" onmouseout="this.style.background=\'' + bgColor + '.06)\'">'
        + item + '</button>';
    }).join('');

    var popup = document.createElement('div');
    popup.id = 'fb-suggest-popup';
    popup.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:9999;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.4);backdrop-filter:blur(2px)';
    popup.innerHTML = '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;max-width:440px;width:90%;box-shadow:0 12px 40px rgba(0,0,0,.4)">'
      + '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">'
      + '<span style="font-size:12px;font-weight:600;color:var(--text)">' + title + '</span>'
      + '<button onclick="document.getElementById(\'fb-suggest-popup\').remove()" style="background:none;border:none;color:var(--text-dim);cursor:pointer;font-size:14px">✕</button>'
      + '</div>'
      + '<div style="font-size:9px;color:var(--text-dim);margin-bottom:10px">点击标签即可添加，可多选</div>'
      + '<div style="display:flex;flex-wrap:wrap;gap:6px;max-height:200px;overflow-y:auto;padding:4px 0">'
      + (chipHtml || '<span style="color:var(--text-dim);font-size:10px">暂无推荐，请手动输入</span>')
      + '</div>'
      + '<div style="margin-top:12px;padding-top:10px;border-top:1px solid var(--border);display:flex;gap:8px;align-items:center">'
      + '<input id="fb-suggest-manual-input" type="text" placeholder="或手动输入（逗号分隔）" style="flex:1;background:var(--bg-main);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:10px" onkeydown="if(event.key===\'Enter\')fbManualSuggestAdd(\'' + type + '\')">'
      + '<button onclick="fbManualSuggestAdd(\'' + type + '\')" style="padding:5px 12px;background:linear-gradient(135deg,' + color + ',' + color + '80);border:none;color:#000;border-radius:6px;cursor:pointer;font-size:10px;font-weight:600">添加</button>'
      + '</div>'
      + '</div>';
    popup.addEventListener('click', function (e) {
      if (e.target === popup) popup.remove();
    });
    document.body.appendChild(popup);
    // 聚焦输入框
    setTimeout(function () {
      var input = document.getElementById('fb-suggest-manual-input');
      if (input) input.focus();
    }, 100);
  }

  window.fbPickSuggestion = function (val, type) {
    if (!_fbActivePersona) return;
    if (type === 'topics') {
      if (!_fbActivePersona.interest_topics) _fbActivePersona.interest_topics = [];
      if (_fbActivePersona.interest_topics.indexOf(val) === -1) {
        _fbActivePersona.interest_topics.push(val);
      }
    } else {
      if (!_fbActivePersona.seed_group_keywords) _fbActivePersona.seed_group_keywords = [];
      if (_fbActivePersona.seed_group_keywords.indexOf(val) === -1) {
        _fbActivePersona.seed_group_keywords.push(val);
      }
    }
    // 视觉反馈：标记已选
    var btns = document.querySelectorAll('#fb-suggest-popup .fb-suggest-chip[data-val="' + val + '"]');
    btns.forEach(function (btn) {
      btn.style.opacity = '.4';
      btn.style.pointerEvents = 'none';
      btn.textContent = '✓ ' + val;
    });
    _fbRefreshChips();
  };

  window.fbManualSuggestAdd = function (type) {
    var input = document.getElementById('fb-suggest-manual-input');
    if (!input || !input.value.trim()) return;
    if (!_fbActivePersona) return;
    var vals = input.value.split(/[,，、]/);
    vals.forEach(function (v) {
      v = v.trim();
      if (!v) return;
      if (type === 'topics') {
        if (!_fbActivePersona.interest_topics) _fbActivePersona.interest_topics = [];
        if (_fbActivePersona.interest_topics.indexOf(v) === -1) _fbActivePersona.interest_topics.push(v);
      } else {
        if (!_fbActivePersona.seed_group_keywords) _fbActivePersona.seed_group_keywords = [];
        if (_fbActivePersona.seed_group_keywords.indexOf(v) === -1) _fbActivePersona.seed_group_keywords.push(v);
      }
    });
    input.value = '';
    _fbRefreshChips();
  };

  async function _fbFallbackManualInput(type) {
    var label = type === 'topics' ? '兴趣标签' : '群组关键词';
    var val = await ocPrompt('添加' + label, '', {message:'多个请用逗号分隔', inputPlaceholder:'例：旅行, 美食, 健身'});
    if (!val || !_fbActivePersona) return;
    val.split(/[,，、]/).forEach(function (v) {
      v = v.trim();
      if (!v) return;
      if (type === 'topics') {
        if (!_fbActivePersona.interest_topics) _fbActivePersona.interest_topics = [];
        if (_fbActivePersona.interest_topics.indexOf(v) === -1) _fbActivePersona.interest_topics.push(v);
      } else {
        if (!_fbActivePersona.seed_group_keywords) _fbActivePersona.seed_group_keywords = [];
        if (_fbActivePersona.seed_group_keywords.indexOf(v) === -1) _fbActivePersona.seed_group_keywords.push(v);
      }
    });
    _fbRefreshChips();
  }

  function _escAttr(s) {
    return String(s).replace(/'/g, "\\'").replace(/"/g, '&quot;');
  }

  function _fbRefreshChips() {
    // 重新渲染兴趣和关键词区域
    var topicsEl = document.getElementById('fb-pb-topics');
    var keywordsEl = document.getElementById('fb-pb-keywords');
    var p = _fbActivePersona || {};
    if (topicsEl) {
      var topics = p.interest_topics || [];
      topicsEl.innerHTML = topics.map(function (t, i) {
        return '<span class="fb-chip" style="display:inline-flex;align-items:center;gap:3px;padding:3px 8px;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.25);border-radius:12px;font-size:10px;color:#4ade80">'
          + t + '<button onclick="fbRemoveInterest(' + i + ')" style="background:none;border:none;color:#4ade80;cursor:pointer;font-size:10px;padding:0 2px;opacity:.7">\u00d7</button></span>';
      }).join('') + '<button onclick="fbAddInterestPrompt()" style="padding:3px 8px;background:none;border:1px dashed rgba(34,197,94,.4);border-radius:12px;color:#4ade80;cursor:pointer;font-size:10px">\u2728 智能推荐</button>';
    }
    if (keywordsEl) {
      var keywords = p.seed_group_keywords || [];
      keywordsEl.innerHTML = keywords.map(function (k, i) {
        return '<span class="fb-chip" style="display:inline-flex;align-items:center;gap:3px;padding:3px 8px;background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.25);border-radius:12px;font-size:10px;color:#60a5fa">'
          + k + '<button onclick="fbRemoveKeyword(' + i + ')" style="background:none;border:none;color:#60a5fa;cursor:pointer;font-size:10px;padding:0 2px;opacity:.7">\u00d7</button></span>';
      }).join('') + '<button onclick="fbAddKeywordPrompt()" style="padding:3px 8px;background:none;border:1px dashed rgba(59,130,246,.4);border-radius:12px;color:#60a5fa;cursor:pointer;font-size:10px">\u2728 智能推荐</button>';
    }
    _fbUpdateCompactSummary();
  }

  function _fbUpdateCompactSummary() {
    var compact = document.getElementById('fb-persona-compact');
    if (!compact) return;
    var p = _fbActivePersona || {};
    var gender = p.gender || '';
    var gLabel = gender === 'female' ? '♀ 女性' : gender === 'male' ? '♂ 男性' : '⚥ 不限';
    var topics = (p.interest_topics || []).slice(0, 3).join(' · ') || '未设兴趣';
    compact.querySelector('div > div:first-child').textContent = p.display_label || '点击配置客群';
    compact.querySelector('div > div:last-child').textContent = gLabel + ' · ' + (p.age_min || 18) + '-' + (p.age_max || 65) + '岁 · ' + topics;
  }

  window.fbSavePersonaProfile = async function () {
    var p = _fbActivePersona;
    if (!p) return;
    // 检查是否是已有用户 profile（更新 vs 新建）
    var existingUserKey = p.persona_key && (_fbAvailablePersonas || []).find(function (x) {
      return x.persona_key === p.persona_key && x.source === 'user';
    });
    var body = {
      name: p.name || p.display_label || '',
      country_code: p.country_code || '',
      country_zh: p.country_zh || '',
      language: p.language || 'en',
      gender: p.gender || '',
      age_min: p.age_min || null,
      age_max: p.age_max || null,
      interest_topics: p.interest_topics || [],
      seed_group_keywords: p.seed_group_keywords || [],
      display_flag: p.display_flag || '🌐',
    };
    if (existingUserKey) {
      // 更新已有 profile
      try {
        var r = await api('PUT', '/facebook/persona-profiles/' + encodeURIComponent(p.persona_key), body);
        if (r && r.ok) {
          if (typeof showToast === 'function') showToast('客群已更新', 'success');
          _fbClearDirty();
          await _fbLoadActivePersona(true);
          var container = document.getElementById('fb-persona-builder');
          if (container) container.outerHTML = _fbRenderPersonaBuilder();
        }
      } catch (e) {
        if (typeof showToast === 'function') showToast('更新失败: ' + (e.message || e), 'error');
      }
    } else {
      // 新建
      var name = await ocPrompt('客群名称', body.name, {message:'为新客群起一个名称'});
      if (!name) return;
      body.name = name;
      try {
        var r = await api('POST', '/facebook/persona-profiles', body);
        if (r && r.ok) {
          if (typeof showToast === 'function') showToast('客群「' + name + '」已保存', 'success');
          _fbClearDirty();
          await _fbLoadActivePersona(true);
          // 切换到新建的
          var newP = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === r.persona_key; });
          if (newP) _fbActivePersona = newP;
          var container = document.getElementById('fb-persona-builder');
          if (container) container.outerHTML = _fbRenderPersonaBuilder();
        }
      } catch (e) {
        if (typeof showToast === 'function') showToast('保存失败: ' + (e.message || e), 'error');
      }
    }
  };

  window.fbCreateNewPersona = function () {
    // 清空当前 persona 到默认值
    _fbActivePersona = {
      persona_key: '',
      display_flag: '🌐',
      display_label: '🌐 新建客群',
      short_label: '新建客群',
      name: '',
      country_code: '',
      country_zh: '',
      language: 'en',
      gender: '',
      age_min: 25,
      age_max: 55,
      interest_topics: [],
      seed_group_keywords: [],
      referral_priority: ['whatsapp', 'telegram', 'instagram', 'line'],
    };
    var container = document.getElementById('fb-persona-builder');
    if (container) {
      container.outerHTML = _fbRenderPersonaBuilder();
      _fbPersonaBuilderExpanded = true;
      var expanded = document.getElementById('fb-persona-expanded');
      var chevron = document.getElementById('fb-persona-chevron');
      if (expanded) expanded.style.display = 'block';
      if (chevron) chevron.style.transform = 'rotate(180deg)';
    }
  };

  // ── 未保存修改跟踪 ──
  var _fbDirty = false;

  function _fbMarkDirty() {
    if (_fbDirty) return;
    _fbDirty = true;
    var saveBtn = document.querySelector('#fb-persona-builder button[onclick*="fbSavePersonaProfile"]');
    if (saveBtn && !saveBtn.querySelector('.fb-dirty-dot')) {
      var dot = document.createElement('span');
      dot.className = 'fb-dirty-dot';
      dot.style.cssText = 'display:inline-block;width:6px;height:6px;border-radius:50%;background:#f59e0b;margin-left:4px;animation:fb-pulse 1.5s infinite';
      saveBtn.appendChild(dot);
      // 动画
      if (!document.getElementById('fb-pulse-style')) {
        var st = document.createElement('style');
        st.id = 'fb-pulse-style';
        st.textContent = '@keyframes fb-pulse{0%,100%{opacity:1}50%{opacity:.3}}';
        document.head.appendChild(st);
      }
    }
  }

  function _fbClearDirty() {
    _fbDirty = false;
    var dots = document.querySelectorAll('.fb-dirty-dot');
    dots.forEach(function (d) { d.remove(); });
  }

  // ── 一键智能填充 ──
  window.fbSmartFill = async function () {
    var p = _fbActivePersona || {};
    try {
      var r = await api('POST', '/facebook/persona-profiles/suggest', {
        country_code: p.country_code || '',
        gender: p.gender || '',
        current_topics: [],
        type: 'both',
      });
      if (!r) return;
      // 填充兴趣（前 6 个）
      var newTopics = (r.topics || []).slice(0, 6);
      if (!_fbActivePersona) return;
      _fbActivePersona.interest_topics = newTopics;
      // 再请求关键词（基于新兴趣）
      var r2 = await api('POST', '/facebook/persona-profiles/suggest', {
        country_code: p.country_code || '',
        gender: p.gender || '',
        current_topics: newTopics,
        type: 'keywords',
      });
      if (r2 && r2.keywords) _fbActivePersona.seed_group_keywords = r2.keywords.slice(0, 8);
      _fbRefreshChips();
      _fbMarkDirty();
      if (typeof showToast === 'function') showToast('已智能填充 ' + newTopics.length + ' 个兴趣 + ' + (_fbActivePersona.seed_group_keywords || []).length + ' 个关键词', 'success');
    } catch (e) {
      if (typeof showToast === 'function') showToast('填充失败: ' + (e.message || e), 'error');
    }
  };

  // ── 右键菜单 ──
  window.fbPresetContextMenu = function (e, key) {
    var old = document.getElementById('fb-preset-ctx');
    if (old) old.remove();
    var pr = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === key; });
    var isUser = pr && pr.source === 'user';
    var menu = document.createElement('div');
    menu.id = 'fb-preset-ctx';
    menu.style.cssText = 'position:fixed;z-index:10000;background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:4px 0;box-shadow:0 8px 24px rgba(0,0,0,.35);min-width:120px;font-size:11px';
    menu.style.left = Math.min(e.clientX, window.innerWidth - 140) + 'px';
    menu.style.top = Math.min(e.clientY, window.innerHeight - 120) + 'px';
    var items = [
      { label: '📋 克隆', action: function () { _fbCtxClone(key); } },
      { label: '📝 重命名', action: function () { _fbCtxRename(key); }, disabled: !isUser },
      { label: '🗑 删除', action: function () { _fbCtxDelete(key); }, disabled: !isUser, danger: true },
    ];
    items.forEach(function (it) {
      var btn = document.createElement('button');
      btn.textContent = it.label;
      btn.style.cssText = 'display:block;width:100%;text-align:left;padding:6px 14px;background:none;border:none;color:' + (it.danger ? '#ef4444' : 'var(--text)') + ';cursor:pointer;font-size:11px;transition:background .1s';
      if (it.disabled) {
        btn.style.opacity = '.35';
        btn.style.pointerEvents = 'none';
      }
      btn.onmouseover = function () { this.style.background = 'rgba(255,255,255,.05)'; };
      btn.onmouseout = function () { this.style.background = 'none'; };
      btn.onclick = function () { menu.remove(); it.action(); };
      menu.appendChild(btn);
    });
    document.body.appendChild(menu);
    var dismiss = function (ev) { if (!menu.contains(ev.target)) { menu.remove(); document.removeEventListener('click', dismiss); } };
    setTimeout(function () { document.addEventListener('click', dismiss); }, 0);
  };

  async function _fbCtxClone(key) {
    var pr = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === key; });
    var newName = await ocPrompt('克隆客群', (pr && (pr.name || pr.short_label) || key) + ' (副本)', {message:'为克隆的客群起一个名称'});
    if (!newName) return;
    try {
      var r = await api('POST', '/facebook/persona-profiles/' + encodeURIComponent(key) + '/clone', { name: newName });
      if (r && r.ok) {
        if (typeof showToast === 'function') showToast('已克隆「' + (r.display_label || newName) + '」', 'success');
        await _fbLoadActivePersona(true);
        var newP = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === r.persona_key; });
        if (newP) _fbActivePersona = newP;
        _fbRebuildPanel();
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('克隆失败: ' + (e.message || e), 'error');
    }
  }

  async function _fbCtxRename(key) {
    var pr = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === key; });
    if (!pr || pr.source !== 'user') return;
    var newName = await ocPrompt('重命名客群', pr.name || pr.short_label || key, {message:'输入新名称'});
    if (!newName || newName === (pr.name || pr.short_label)) return;
    try {
      var r = await api('PUT', '/facebook/persona-profiles/' + encodeURIComponent(key), { name: newName });
      if (r && r.ok) {
        if (typeof showToast === 'function') showToast('已重命名', 'success');
        await _fbLoadActivePersona(true);
        var updated = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === key; });
        if (updated) _fbActivePersona = updated;
        _fbRebuildPanel();
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('重命名失败: ' + (e.message || e), 'error');
    }
  }

  async function _fbCtxDelete(key) {
    var pr = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === key; });
    if (!pr || pr.source !== 'user') return;
    if (!(await ocDialog({title:'删除客群',message:'确定删除「' + (pr.display_label || key) + '」？此操作不可撤销。',type:'danger',confirmText:'删除',dangerous:true}))) return;
    try {
      var r = await api('DELETE', '/facebook/persona-profiles/' + encodeURIComponent(key));
      if (r && r.ok) {
        if (typeof showToast === 'function') showToast('已删除', 'success');
        await _fbLoadActivePersona(true);
        if (_fbActivePersona && _fbActivePersona.persona_key === key) _fbActivePersona = _fbAvailablePersonas[0] || null;
        _fbRebuildPanel();
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('删除失败: ' + (e.message || e), 'error');
    }
  }

  // ── 跨客群对比 ──
  var _fbCompareOpen = false;
  window.fbToggleCompare = async function () {
    _fbCompareOpen = !_fbCompareOpen;
    var panel = document.getElementById('fb-compare-panel');
    var toggle = document.getElementById('fb-compare-toggle');
    if (!panel) return;
    if (!_fbCompareOpen) {
      panel.style.display = 'none';
      if (toggle) toggle.textContent = '📊 查看全部客群对比 ▸';
      return;
    }
    if (toggle) toggle.textContent = '📊 全部客群对比 ▾';
    panel.style.display = 'block';
    panel.innerHTML = '<div style="text-align:center;padding:10px;color:var(--text-dim);font-size:10px">加载中…</div>';
    try {
      var r = await api('GET', '/facebook/persona-profiles/compare');
      if (!r || !r.personas) return;
      var activeKey = (_fbActivePersona || {}).persona_key || '';
      var maxT = Math.max.apply(null, r.personas.map(function (x) { return x.targets || 1; }));
      var rows = r.personas.map(function (p) {
        var isMe = p.persona_key === activeKey;
        var barW = Math.max(((p.targets || 0) / maxT) * 100, 2);
        return '<div style="display:grid;grid-template-columns:28px 1fr 48px 48px 48px;gap:4px;align-items:center;padding:4px 6px;border-radius:4px;font-size:10px;' + (isMe ? 'background:rgba(24,119,242,.08);border:1px solid rgba(24,119,242,.2)' : '') + ';cursor:pointer" onclick="fbSelectPersonaPreset(\'' + p.persona_key + '\')">'
          + '<span style="font-size:13px;text-align:center">' + p.flag + '</span>'
          + '<div style="min-width:0">'
          + '<div style="font-weight:600;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + p.label + (p.source === 'user' ? ' <span style="font-size:7px;color:#c084fc">自建</span>' : '') + '</div>'
          + '<div style="height:4px;background:rgba(255,255,255,.05);border-radius:2px;margin-top:2px"><div style="height:100%;width:' + barW + '%;background:linear-gradient(90deg,#6366f1,#22c55e);border-radius:2px;transition:width .3s"></div></div>'
          + '</div>'
          + '<div style="text-align:right;color:var(--text);font-weight:600">' + (p.targets || 0) + '</div>'
          + '<div style="text-align:right;color:#22c55e">' + (p.friended || 0) + '</div>'
          + '<div style="text-align:right;color:#a855f7">' + (p.reply_rate ? (p.reply_rate * 100).toFixed(0) + '%' : '—') + '</div>'
          + '</div>';
      }).join('');
      panel.innerHTML = '<div style="display:grid;grid-template-columns:28px 1fr 48px 48px 48px;gap:4px;padding:0 6px 4px;font-size:8px;color:var(--text-dim)">'
        + '<span></span><span></span><span style="text-align:right">候选</span><span style="text-align:right">好友</span><span style="text-align:right">回复率</span>'
        + '</div>' + rows;
    } catch (e) {
      panel.innerHTML = '<div style="text-align:center;padding:8px;color:var(--text-dim);font-size:10px">加载失败</div>';
    }
  };

  // ── ESC 全局关闭 ──
  document.addEventListener('keydown', function (e) {
    if (e.key === 'Escape') {
      var suggest = document.getElementById('fb-suggest-popup');
      if (suggest) { suggest.remove(); return; }
      var ctx = document.getElementById('fb-preset-ctx');
      if (ctx) { ctx.remove(); return; }
    }
  });

  function _fbRebuildPanel() {
    var container = document.getElementById('fb-persona-builder');
    if (container) {
      container.outerHTML = _fbRenderPersonaBuilder();
      if (_fbPersonaBuilderExpanded) {
        var expanded = document.getElementById('fb-persona-expanded');
        var chevron = document.getElementById('fb-persona-chevron');
        if (expanded) expanded.style.display = 'block';
        if (chevron) chevron.style.transform = 'rotate(180deg)';
      }
    }
  }

  // ── 效果预览 & 管理操作 ──
  window.fbLoadPersonaStats = async function () {
    var p = _fbActivePersona || {};
    var key = p.persona_key;
    if (!key) {
      _fbClearStats();
      return;
    }
    try {
      var r = await api('GET', '/facebook/persona-profiles/' + encodeURIComponent(key) + '/stats');
      if (!r) return;
      var t = r.targets || {};
      var g = r.greetings || {};
      _fbSetStat('fb-stat-targets', t.total || 0);
      _fbSetStat('fb-stat-friended', t.friended || 0);
      _fbSetStat('fb-stat-sent', g.total_sent || 0);
      _fbSetStat('fb-stat-reply-rate', g.avg_reply_rate ? (g.avg_reply_rate * 100).toFixed(1) + '%' : '0%');
      // 漏斗可视化
      var funnelEl = document.getElementById('fb-pb-funnel');
      var total = Math.max(t.total || 1, 1);
      if (funnelEl && t.total > 0) {
        funnelEl.style.display = 'block';
        var disc = ((t.discovered || 0) / total * 100).toFixed(1);
        var fri = ((t.friended || 0) / total * 100).toFixed(1);
        var gre = ((t.greeted || 0) / total * 100).toFixed(1);
        var qua = ((t.qualified || 0) / total * 100).toFixed(1);
        document.getElementById('fb-funnel-discovered').style.width = disc + '%';
        document.getElementById('fb-funnel-friended').style.width = fri + '%';
        document.getElementById('fb-funnel-greeted').style.width = gre + '%';
        document.getElementById('fb-funnel-qualified').style.width = qua + '%';
      } else if (funnelEl) {
        funnelEl.style.display = 'none';
      }
      // 最佳话术
      var bestEl = document.getElementById('fb-pb-best-greeting');
      if (bestEl && r.best_greeting && r.best_greeting.text) {
        bestEl.style.display = 'block';
        bestEl.innerHTML = '🔥 <b style="color:#4ade80">最佳话术</b> (' + (r.best_greeting.reply_rate * 100).toFixed(0) + '% 回复): <span style="color:var(--text)">' + _escHtml(r.best_greeting.text) + '</span>';
      } else if (bestEl) {
        bestEl.style.display = 'none';
      }
    } catch (e) {
      _fbClearStats();
    }
  };

  function _fbSetStat(id, val) {
    var el = document.getElementById(id);
    if (el) el.textContent = val;
  }
  function _fbClearStats() {
    ['fb-stat-targets', 'fb-stat-friended', 'fb-stat-sent', 'fb-stat-reply-rate'].forEach(function (id) {
      _fbSetStat(id, '—');
    });
    var bestEl = document.getElementById('fb-pb-best-greeting');
    if (bestEl) bestEl.style.display = 'none';
  }

  window.fbClonePersona = async function () {
    var p = _fbActivePersona || {};
    var key = p.persona_key;
    if (!key) {
      if (typeof showToast === 'function') showToast('请先选择一个客群再克隆', 'warning');
      return;
    }
    var newName = await ocPrompt('克隆客群', (p.name || p.short_label || key) + ' (副本)', {message:'为克隆的客群起一个名称'});
    if (!newName) return;
    try {
      var r = await api('POST', '/facebook/persona-profiles/' + encodeURIComponent(key) + '/clone', { name: newName });
      if (r && r.ok) {
        if (typeof showToast === 'function') showToast('已克隆为「' + (r.display_label || newName) + '」', 'success');
        await _fbLoadActivePersona(true);
        // 切换到新克隆的
        var newP = (_fbAvailablePersonas || []).find(function (x) { return x.persona_key === r.persona_key; });
        if (newP) _fbActivePersona = newP;
        var container = document.getElementById('fb-persona-builder');
        if (container) {
          container.outerHTML = _fbRenderPersonaBuilder();
          _fbPersonaBuilderExpanded = true;
          document.getElementById('fb-persona-expanded').style.display = 'block';
          document.getElementById('fb-persona-chevron').style.transform = 'rotate(180deg)';
        }
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('克隆失败: ' + (e.message || e), 'error');
    }
  };

  window.fbDeletePersona = async function () {
    var p = _fbActivePersona || {};
    var key = p.persona_key;
    if (!key) return;
    // 检查是否是系统预设
    var isUser = (_fbAvailablePersonas || []).find(function (x) {
      return x.persona_key === key && x.source === 'user';
    });
    if (!isUser) {
      if (typeof showToast === 'function') showToast('系统预设不可删除，可克隆后编辑', 'warning');
      return;
    }
    if (!(await ocDialog({title:'删除客群',message:'确定删除「' + (p.display_label || key) + '」？<br>此操作不可撤销。',type:'danger',confirmText:'删除',dangerous:true}))) return;
    try {
      var r = await api('DELETE', '/facebook/persona-profiles/' + encodeURIComponent(key));
      if (r && r.ok) {
        if (typeof showToast === 'function') showToast('已删除', 'success');
        await _fbLoadActivePersona(true);
        _fbActivePersona = _fbAvailablePersonas[0] || null;
        var container = document.getElementById('fb-persona-builder');
        if (container) {
          container.outerHTML = _fbRenderPersonaBuilder();
          _fbPersonaBuilderExpanded = true;
          document.getElementById('fb-persona-expanded').style.display = 'block';
          document.getElementById('fb-persona-chevron').style.transform = 'rotate(180deg)';
        }
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('删除失败: ' + (e.message || e), 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // 5 套执行方案模态(全设备 / 选定设备)
  // ════════════════════════════════════════════════════════
  window.fbOpenPresetsModal = async function (preselectedDevice) {
    await _fbLoadActivePersona();
    await _fbLoadPresets();
    const presets = _fbPresets || [];

    const overlay = _fbModalOverlay('fb-presets-modal');

    // 获取当前设备阶段（如有）用于智能推荐
    var _devPhase = '';
    if (preselectedDevice && window._pgGetDevicePhase) {
      _devPhase = window._pgGetDevicePhase('facebook', preselectedDevice);
    }
    // 推荐逻辑：cold_start/cooldown → 推荐 maintain；growth → 推荐 outreach；mature → 推荐 full_auto
    var _phaseRecommend = {
      cold_start: 'maintain', cooldown: 'maintain',
      growth: 'outreach', mature: 'full_auto',
    };
    var _recommendedCategory = _phaseRecommend[_devPhase] || '';

    // 风险等级映射
    var _riskMeta = {
      safe: { dot: '#22c55e', label: '安全', bg: 'rgba(34,197,94,.1)' },
      moderate: { dot: '#f59e0b', label: '中风险', bg: 'rgba(245,158,11,.1)' },
      high: { dot: '#ef4444', label: '高风险', bg: 'rgba(239,68,68,.1)' },
    };
    // 分区配置
    var _categories = [
      { key: 'custom', icon: '⭐', title: '我的方案', desc: '从系统预设克隆 + 自定义参数' },
      { key: 'maintain', icon: '🛡️', title: '安全维护', desc: '任何阶段均可运行，无触达风险' },
      { key: 'outreach', icon: '🚀', title: '主动拓展', desc: '发起好友请求/DM，需 growth+ 阶段' },
      { key: 'full_auto', icon: '⚡', title: '全自动闭环', desc: '完整漏斗，时间较长，建议 mature 阶段' },
    ];

    // C: 引流渠道状态徽章（全局计算一次）
    var _refBadgeHtml = '';
    (function () {
      var refs = _fbReferrals || {};
      var devKeys = Object.keys(refs);
      if (!devKeys.length) {
        _refBadgeHtml = '<div style="font-size:9px;color:#f59e0b;padding:3px 8px;background:rgba(245,158,11,.08);border-radius:4px;margin-bottom:8px">⚠️ 引流账号未配置</div>';
        return;
      }
      var first = refs[devKeys[0]] || {};
      var _chMeta = {line:'💚LINE', whatsapp:'💬WA', instagram:'📷IG', telegram:'✈️TG'};
      var order = (_fbReferralPriority && _fbReferralPriority.length)
        ? _fbReferralPriority : ['whatsapp', 'telegram', 'instagram', 'line'];
      var chips = order.map(function (ch) {
        var val = first[ch];
        var ok = val && val.trim();
        return '<span style="font-size:8px;padding:1px 5px;border-radius:3px;' + (ok ? 'background:rgba(34,197,94,.1);color:#22c55e' : 'background:rgba(239,68,68,.08);color:#ef4444;opacity:.6') + '">' + (_chMeta[ch] || ch) + (ok ? '✓' : '✗') + '</span>';
      }).join('');
      _refBadgeHtml = '<div style="display:flex;gap:3px;flex-wrap:wrap;margin-bottom:8px">' + chips + '</div>';
    })();

    function _renderCard(p) {
      var rm = _riskMeta[p.risk] || _riskMeta.moderate;
      // C: 仅 outreach/full_auto 显示引流渠道徽章
      var refBadge = (p.category === 'outreach' || p.category === 'full_auto') ? _refBadgeHtml : '';
      // 阶段兼容性判定
      var phaseOk = !_devPhase || !p.phase_gate || p.phase_gate.indexOf(_devPhase) !== -1;
      var isRecommended = _recommendedCategory && p.category === _recommendedCategory && phaseOk;
      var gateBadge = '';
      if (!phaseOk) {
        gateBadge = '<span style="position:absolute;top:8px;right:8px;font-size:9px;padding:2px 6px;background:rgba(239,68,68,.15);color:#f87171;border-radius:4px;font-weight:600">⚠ 阶段不匹配</span>';
      } else if (isRecommended) {
        gateBadge = '<span style="position:absolute;top:8px;right:8px;font-size:9px;padding:2px 6px;background:rgba(34,197,94,.15);color:#22c55e;border-radius:4px;font-weight:600;box-shadow:0 0 8px rgba(34,197,94,.3)">🌟 推荐</span>';
      }
      var cardOpacity = phaseOk ? '1' : '0.55';
      var stepsArr = (p.steps || []).map(function (s) { return s.type.replace('facebook_', '').replace(/_/g,' '); });
      var stepsFlow = stepsArr.map(function(s){return '<span style="background:'+p.color+'18;color:'+p.color+';padding:2px 6px;border-radius:3px;font-size:9px;font-weight:600">'+s+'</span>';}).join('<span style="color:var(--text-dim);font-size:9px">→</span>');
      return `
        <div onclick="fbLaunchPresetWithPersona('${p.key}', ${preselectedDevice ? `'${preselectedDevice}'` : 'null'})"
             style="background:var(--bg-main);border:1px solid var(--border);border-radius:12px;padding:0;cursor:pointer;transition:all .18s;overflow:hidden;position:relative;opacity:${cardOpacity}"
             onmouseover="this.style.transform='translateY(-3px)';this.style.boxShadow='0 8px 24px ${p.color}25';this.style.borderColor='${p.color}55'"
             onmouseout="this.style.transform='';this.style.boxShadow='';this.style.borderColor='var(--border)'">
          ${gateBadge}
          <div style="height:4px;background:linear-gradient(90deg,${p.color},${p.color}88)"></div>
          <div style="padding:14px 16px">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
              <div style="font-size:16px;font-weight:700;flex:1">${p.name}</div>
              <span style="width:7px;height:7px;border-radius:50%;background:${rm.dot};flex-shrink:0;box-shadow:0 0 4px ${rm.dot}" title="${rm.label}"></span>
              <span style="font-size:9px;padding:2px 8px;background:${p.color}18;color:${p.color};border-radius:4px;font-weight:600;flex-shrink:0">${p.label||''}</span>
            </div>
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px;line-height:1.4">${p.desc}</div>
            <div style="font-size:10px;color:var(--text-dim);margin-bottom:10px;line-height:1.4">${p.detail}</div>
            <div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap;margin-bottom:10px">${stepsFlow}</div>
            ${refBadge}
            ${p.next_hint ? '<div style="font-size:9px;color:#60a5fa;margin-bottom:8px;padding:4px 8px;background:rgba(96,165,250,.08);border-radius:4px;line-height:1.4">💡 '+_escHtml(p.next_hint)+'</div>' : ''}
            <div style="display:flex;justify-content:space-between;align-items:center;padding-top:10px;border-top:1px solid var(--border)">
              <span style="font-size:10px;color:var(--text-dim)">⏱ ≈${p.estimated_minutes}min</span>
              ${p.today_runs ? '<span style="font-size:9px;padding:1px 6px;background:rgba(96,165,250,.12);color:#60a5fa;border-radius:3px;font-weight:600">今日 '+p.today_runs+' 次</span>' : '<span style="font-size:10px;color:'+p.color+';font-weight:600">📊 '+p.estimated_output+'</span>'}
              <span style="display:flex;align-items:center;gap:6px">
                ${p._custom ? '<span onclick="event.stopPropagation();fbDeleteCustomPreset(\''+p.key+'\')" style="font-size:10px;color:#ef4444;cursor:pointer;padding:2px 6px;border-radius:4px;background:rgba(239,68,68,.08)" title="删除">🗑</span>' : '<span onclick="event.stopPropagation();fbSavePresetAs(\''+p.key+'\')" style="font-size:10px;color:#f59e0b;cursor:pointer;padding:2px 6px;border-radius:4px;background:rgba(245,158,11,.08)" title="另存为自定义">⭐</span>'}
                <span style="font-size:12px;color:${p.color};font-weight:700">启动 →</span>
              </span>
            </div>
          </div>
        </div>`;
    }

    // 按 category 分区渲染
    var _catKeys = _categories.map(function (c) { return c.key; });
    var _uncategorized = presets.filter(function (p) { return _catKeys.indexOf(p.category) === -1; });
    var presetSections = _categories.map(function (cat) {
      var items = presets.filter(function (p) { return p.category === cat.key; });
      if (!items.length) return '';
      var cards = items.map(_renderCard).join('');
      var isRecSection = _recommendedCategory === cat.key;
      var sectionBorder = isRecSection ? 'border-left:3px solid #22c55e;padding-left:10px' : '';
      var recLabel = isRecSection ? '<span style="font-size:9px;padding:1px 6px;background:rgba(34,197,94,.15);color:#22c55e;border-radius:3px;font-weight:600;margin-left:4px">适合当前阶段</span>' : '';
      return `
        <div style="margin-bottom:16px;${sectionBorder}">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
            <span style="font-size:14px">${cat.icon}</span>
            <span style="font-size:12px;font-weight:700;color:var(--text)">${cat.title}</span>
            <span style="font-size:10px;color:var(--text-dim);font-weight:normal">${cat.desc}</span>
            ${recLabel}
          </div>
          <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px">${cards}</div>
        </div>`;
    }).join('');
    // 兜底：未归类 preset 不会丢失
    if (_uncategorized.length) {
      presetSections += '<div style="margin-bottom:16px"><div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:10px">'
        + _uncategorized.map(_renderCard).join('') + '</div></div>';
    }

    const persona = _fbActivePersona || {};

    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:0;max-width:960px;width:96%;max-height:88vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.4)">
        <!-- 渐变头部 -->
        <div style="padding:20px 24px 16px;background:linear-gradient(180deg,rgba(24,119,242,.1) 0%,transparent 100%)">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
            <div style="display:flex;align-items:center;gap:12px">
              <div style="width:42px;height:42px;border-radius:12px;background:linear-gradient(135deg,#1877f2,#0d6efd);display:flex;align-items:center;justify-content:center;font-size:20px;box-shadow:0 4px 12px rgba(24,119,242,.3)">⚡</div>
              <div>
                <div style="font-size:18px;font-weight:700;color:var(--text)">Facebook 执行方案</div>
                <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                  ${preselectedDevice ? '🎯 指定设备 ' + preselectedDevice.substring(0, 8) : '📡 下发到所有在线 FB 设备'}
                </div>
              </div>
            </div>
            <button onclick="document.getElementById('fb-presets-modal').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px;width:32px;height:32px;border-radius:8px;display:flex;align-items:center;justify-content:center;transition:all .12s" onmouseover="this.style.background='rgba(239,68,68,.15)';this.style.color='#ef4444'" onmouseout="this.style.background='none';this.style.color='var(--text-muted)'">✕</button>
          </div>

          <!-- 客群构建器（替代旧版单一下拉） -->
          ${_fbRenderPersonaBuilder()}

          <div style="background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.25);border-radius:6px;padding:6px 12px;font-size:10px;color:#fbbf24;display:flex;align-items:center;gap:6px">
            <span>⚠</span> 启动前确认：账号已登录 · Messenger 就绪 · VPN 已连接 <b id="fb-geo-hint">${persona.country_code || '目标'}</b> 地区
          </div>
        </div>

        <!-- 方案卡片（按漏斗阶段分区） -->
        <div style="padding:0 24px 20px">
          ${presetSections || '<div style="color:#f87171;padding:20px;text-align:center">未加载到预设 — 请检查 /facebook/presets</div>'}

          <div style="padding-top:10px;border-top:1px solid var(--border);font-size:10px;color:var(--text-dim);line-height:1.5;display:flex;align-items:center;gap:12px">
            <span>💡 点击客群面板展开配置 · 修改即时生效</span>
            <span style="margin-left:auto;display:flex;gap:8px;align-items:center">
              <span style="display:flex;align-items:center;gap:3px"><span style="width:6px;height:6px;border-radius:50%;background:#22c55e"></span>安全</span>
              <span style="display:flex;align-items:center;gap:3px"><span style="width:6px;height:6px;border-radius:50%;background:#f59e0b"></span>中风险</span>
              <span style="display:flex;align-items:center;gap:3px"><span style="width:6px;height:6px;border-radius:50%;background:#ef4444"></span>高风险</span>
            </span>
          </div>
        </div>
      </div>
    `;
  };

  // persona 切换 — 现在由构建器面板直接管理 _fbActivePersona
  window.fbOnPersonaChange = function () {
    var geoHint = document.getElementById('fb-geo-hint');
    var p = _fbActivePersona || {};
    if (geoHint) geoHint.textContent = p.country_code || '目标';
  };

  // 包装版:从模态读取 persona + 群组,再调用 fbLaunchPreset
  //
  // 2026-04-30 hotfix (real device complaint "点确定无响应"):
  // 原版同步函数, 若用户在 _fbPresets 未加载完时点启动 → preset=undefined →
  // schema=null → 跳过 schema-form dialog → 走原生 confirm → API 返 422
  // missing_required_inputs → fbLaunchPreset 把 422 当作"由 dialog 标红"
  // 静默吞了 → 用户看到的就是"点确定无响应". 改异步先 await 加载 preset.
  window.fbLaunchPresetWithPersona = async function (presetKey, deviceId) {
    // 关键: 确保 preset 元数据 (含 input_schema) 已加载, 否则下面所有分支
    // 都会判定为"无 schema" → 错误地落到原生 confirm 分支.
    try { await _fbLoadPresets(); } catch (e) { /* ignore, 下面会兜底 */ }

    const p = _fbActivePersona || {};
    const persona_key = p.persona_key || '';
    const target_country = p.country_code || '';
    const language = p.language || '';

    const preset = (_fbPresets || []).find(function (x) { return x.key === presetKey; });
    const needs = (preset && preset.needs_input) || [];
    const schema = (preset && preset.input_schema) || null;

    // phase_gate 检查：如果能获取设备阶段，阻止不兼容的 preset 运行
    if (deviceId && preset && preset.phase_gate && window._pgGetDevicePhase) {
      var devPhase = window._pgGetDevicePhase('facebook', deviceId);
      if (devPhase && preset.phase_gate.indexOf(devPhase) === -1) {
        var phaseNames = { cold_start: '冷启动', growth: '增长', mature: '成熟', cooldown: '冷却' };
        if (!(await ocDialog({title:'阶段不匹配',message:'当前设备处于「' + (phaseNames[devPhase] || devPhase) + '」阶段，该方案建议在「' + preset.phase_gate.map(function(x){return phaseNames[x]||x;}).join('/') + '」阶段使用。<br><br>强制启动可能触发风控。',type:'warning',confirmText:'强制启动',cancelText:'返回'}))) return;
      }
    }

    // D: 引流账号校验 — outreach/full_auto 启动前检测
    if (preset && (preset.category === 'outreach' || preset.category === 'full_auto')) {
      var _refs = _fbReferrals || {};
      var _hasRef = Object.keys(_refs).length > 0;
      if (!_hasRef) {
        if (typeof ocDialog === 'function') {
          var goRef = await ocDialog({title:'引流账号未配置',message:'当前没有为任何设备配置引流账号（LINE / WhatsApp / Instagram / Telegram）。<br><br>该方案需要引流账号才能完成最终转化。建议先配置引流账号再启动。',type:'warning',confirmText:'仍然启动',cancelText:'去配置'});
          if (!goRef) { fbOpenReferralModal(); return; }
        }
      } else {
        // 检查主引流渠道是否已配
        var _order = (_fbReferralPriority && _fbReferralPriority.length) ? _fbReferralPriority : ['whatsapp','telegram','instagram','line'];
        var _mainCh = _order[0] || 'line';
        var _firstDev = _refs[Object.keys(_refs)[0]] || {};
        var _mainVal = _firstDev[_mainCh];
        if (!_mainVal || !_mainVal.trim()) {
          var _chNames = {line:'LINE',whatsapp:'WhatsApp',instagram:'Instagram',telegram:'Telegram'};
          if (typeof showToast === 'function') showToast('提示: 主引流渠道 ' + (_chNames[_mainCh]||_mainCh) + ' 尚未配置', 'warn');
        }
      }
    }

    // 通用上下文（persona/country/language），各分支共用
    const _ctx = { persona_key: persona_key, target_country: target_country, language: language };

    // P1 (Sprint A): schema-driven 表单（friend_growth / full_funnel 等有 input_schema 的 preset）
    // 群组/话术/节流等参数由各方案自己的表单收集，不再依赖顶部全局输入。
    if (schema && Object.keys(schema).length > 0) {
      fbOpenLaunchInputDialog(presetKey, deviceId, _ctx);
      return;
    }

    // 向后兼容：name_hunter 仅声明 needs_input=add_friend_targets，没 schema → 老逻辑
    if (needs.includes('add_friend_targets')) {
      fbOpenNameHunterInput(presetKey, deviceId, _ctx);
      return;
    }

    // 无 schema 预设（warmup / inbox_pro）直接启动
    fbLaunchPreset(presetKey, deviceId, _ctx);
  };

  // 2026-05-01: 点名添加输入模态 —— 姓名生成/导入去重/评分预览/确认后启动
  window.fbOpenNameHunterInput = function (presetKey, deviceId, extra) {
    const overlay = _fbModalOverlay('fb-name-hunter-input');
    const personaLabel = (extra && extra.persona_key) || '默认';
    // 尝试回读上次输入,方便运营复用
    let lastNames = '';
    let lastGreeting = '';
    try {
      lastNames = localStorage.getItem('fb_name_hunter_last') || '';
      lastGreeting = localStorage.getItem('fb_name_hunter_greeting') || '';
    } catch (e) { /* ignore */ }
    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:760px;width:96%;max-height:90vh;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div>
            <div style="font-size:17px;font-weight:700">🔎 点名添加 — 精准名单</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
              客群: <code>${personaLabel}</code> · 先生成/导入并预览，再确认启动
            </div>
          </div>
          <button onclick="document.getElementById('fb-name-hunter-input').remove()"
                  style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px">
          <button id="fb-nh-gen-mixed" data-pack="mixed"
            style="padding:8px 10px;background:rgba(14,165,233,.16);border:1px solid rgba(14,165,233,.35);color:#38bdf8;border-radius:8px;cursor:pointer;font-size:12px">
            生成混合常用名
          </button>
          <button id="fb-nh-gen-46" data-pack="46_55"
            style="padding:8px 10px;background:rgba(168,85,247,.14);border:1px solid rgba(168,85,247,.35);color:#c084fc;border-radius:8px;cursor:pointer;font-size:12px">
            生成 46-55 名字包
          </button>
          <button id="fb-nh-clear"
            style="padding:8px 10px;background:transparent;border:1px solid var(--border);color:var(--text-muted);border-radius:8px;cursor:pointer;font-size:12px">
            清空
          </button>
        </div>

        <textarea id="fb-nh-names" rows="8"
          placeholder="山田花子&#10;佐藤美咲&#10;鈴木 由美"
          style="width:100%;box-sizing:border-box;background:var(--bg-main);border:1px solid var(--border);color:var(--text);padding:10px;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical">${lastNames.replace(/</g,'&lt;')}</textarea>
        ${lastNames ? '<div style="font-size:10px;color:var(--text-dim);margin-top:4px">🕑 已回填上次输入</div>' : ''}

        <div style="margin-top:10px">
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">打招呼文案(可选,为空则按客群自动生成本地化问候)</div>
          <textarea id="fb-nh-greeting" rows="2"
            placeholder="例:はじめまして😊つながれて嬉しいです🌸"
            style="width:100%;box-sizing:border-box;background:var(--bg-main);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:6px;font-size:12px;font-family:inherit;resize:vertical">${lastGreeting.replace(/</g,'&lt;')}</textarea>
        </div>

        <div style="margin-top:10px;display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px" id="fb-nh-kpis">
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:8px">
            <div style="font-size:10px;color:var(--text-dim)">唯一姓名</div>
            <div id="fb-nh-kpi-unique" style="font-size:18px;font-weight:700;color:#60a5fa">-</div>
          </div>
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:8px">
            <div style="font-size:10px;color:var(--text-dim)">高置信种子</div>
            <div id="fb-nh-kpi-high" style="font-size:18px;font-weight:700;color:#22c55e">-</div>
          </div>
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:8px">
            <div style="font-size:10px;color:var(--text-dim)">需确认</div>
            <div id="fb-nh-kpi-review" style="font-size:18px;font-weight:700;color:#f59e0b">-</div>
          </div>
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:8px">
            <div style="font-size:10px;color:var(--text-dim)">弱种子</div>
            <div id="fb-nh-kpi-weak" style="font-size:18px;font-weight:700;color:#ef4444">-</div>
          </div>
        </div>

        <div id="fb-nh-preview" style="margin-top:10px;display:none;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;max-height:220px;overflow:auto"></div>

        <div style="margin-top:10px;padding:8px 12px;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);border-radius:6px;font-size:11px;color:#fbbf24;line-height:1.5">
          ⚠ 名字只是搜索入口，不作为客户判定。系统会搜索资料并走画像识别；默认仅把评分 ≥80 的高置信姓名种子送入任务。
          单次实际处理量仍受 playbook 阶段上限控制，cold_start/cooldown 会跳过主动触达。
        </div>
        <div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">
          <button onclick="document.getElementById('fb-name-hunter-input').remove()"
                  style="padding:8px 16px;background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:8px;cursor:pointer">取消</button>
          <button id="fb-nh-preview-btn"
                  style="padding:8px 16px;background:transparent;color:#38bdf8;border:1px solid rgba(56,189,248,.45);border-radius:8px;font-weight:600;cursor:pointer">
            预览评分
          </button>
          <button id="fb-nh-submit"
                  disabled
                  style="padding:8px 16px;background:#334155;color:#94a3b8;border:none;border-radius:8px;font-weight:600;cursor:not-allowed">
            ▶ 确认启动
          </button>
        </div>
      </div>
    `;
    let previewRows = [];
    let launchTargets = [];

    function _nhCurrentNamesRaw() {
      return (document.getElementById('fb-nh-names') || {}).value || '';
    }
    function _nhSetSubmitEnabled(enabled) {
      const btn = document.getElementById('fb-nh-submit');
      if (!btn) return;
      btn.disabled = !enabled;
      btn.style.background = enabled ? '#0ea5e9' : '#334155';
      btn.style.color = enabled ? '#fff' : '#94a3b8';
      btn.style.cursor = enabled ? 'pointer' : 'not-allowed';
    }
    function _nhRenderPreview(r) {
      previewRows = (r && r.rows) || [];
      launchTargets = (r && r.launch_targets) || [];
      const setTxt = function (id, val) {
        const el = document.getElementById(id);
        if (el) el.textContent = String(val);
      };
      setTxt('fb-nh-kpi-unique', r.unique_count || 0);
      setTxt('fb-nh-kpi-high', r.high_confidence_count || 0);
      setTxt('fb-nh-kpi-review', r.review_required_count || 0);
      setTxt('fb-nh-kpi-weak', r.weak_count || 0);
      const box = document.getElementById('fb-nh-preview');
      if (!box) return;
      box.style.display = 'block';
      const rows = previewRows.slice(0, 80).map(function (x) {
        const color = x.score >= 80 ? '#22c55e' : (x.score >= 50 ? '#f59e0b' : '#ef4444');
        const reasons = (x.reasons || []).join(' / ');
        return `<div style="display:grid;grid-template-columns:150px 58px 1fr;gap:8px;align-items:center;padding:8px 10px;border-bottom:1px solid var(--border);font-size:12px">
          <div style="font-weight:600;color:var(--text)">${_escHtml(x.name || '')}</div>
          <div style="font-weight:700;color:${color}">${x.score || 0}</div>
          <div style="color:var(--text-dim);font-size:11px">${_escHtml(reasons)}</div>
        </div>`;
      }).join('');
      box.innerHTML = rows || '<div style="padding:12px;color:var(--text-muted);font-size:12px">暂无可预览姓名</div>';
      _nhSetSubmitEnabled(launchTargets.length > 0);
    }
    async function _nhPreview() {
      const raw = _nhCurrentNamesRaw();
      if (!raw.trim()) {
        showToast('请先输入或生成名字', 'warning');
        return;
      }
      const btn = document.getElementById('fb-nh-preview-btn');
      if (btn) btn.textContent = '预览中...';
      try {
        const r = await api('POST', '/facebook/name-hunter/preview', {
          persona_key: (extra && extra.persona_key) || '',
          names: raw,
        });
        _nhRenderPreview(r || {});
      } catch (e) {
        showToast('预览失败: ' + (e.message || e), 'error');
      } finally {
        if (btn) btn.textContent = '预览评分';
      }
    }

    ['fb-nh-gen-mixed', 'fb-nh-gen-46'].forEach(function (id) {
      const genBtn = document.getElementById(id);
      if (!genBtn) return;
      genBtn.onclick = async function () {
        const pack = genBtn.getAttribute('data-pack') || 'mixed';
        try {
          const r = await api('POST', '/facebook/name-hunter/suggest', {
            persona_key: (extra && extra.persona_key) || '',
            age_pack: pack,
            count: 30,
          });
          const names = ((r && r.names) || []).map(function (x) { return x.name; }).filter(Boolean);
          const ta = document.getElementById('fb-nh-names');
          if (ta) ta.value = names.join('\n');
          _nhRenderPreview({ rows: r.names || [], launch_targets: r.names || [],
            unique_count: names.length, high_confidence_count: names.length,
            review_required_count: 0, weak_count: 0 });
        } catch (e) {
          showToast('生成失败: ' + (e.message || e), 'error');
        }
      };
    });
    const clearBtn = document.getElementById('fb-nh-clear');
    if (clearBtn) clearBtn.onclick = function () {
      const ta = document.getElementById('fb-nh-names');
      if (ta) ta.value = '';
      _nhRenderPreview({ rows: [], launch_targets: [], unique_count: 0,
        high_confidence_count: 0, review_required_count: 0, weak_count: 0 });
    };
    const previewBtn = document.getElementById('fb-nh-preview-btn');
    if (previewBtn) previewBtn.onclick = _nhPreview;
    const ta = document.getElementById('fb-nh-names');
    if (ta) ta.oninput = function () { _nhSetSubmitEnabled(false); };

    const submitBtn = document.getElementById('fb-nh-submit');
    submitBtn.onclick = async function () {
      const namesRaw = (document.getElementById('fb-nh-names') || {}).value || '';
      const greetingRaw = (document.getElementById('fb-nh-greeting') || {}).value || '';
      if (!namesRaw.trim()) {
        showToast('请至少输入 1 个名字', 'warning');
        return;
      }
      if (!launchTargets.length) {
        await _nhPreview();
      }
      if (!launchTargets.length) {
        showToast('没有评分达到执行门槛的姓名种子', 'warning');
        return;
      }
      if (!(await ocDialog({title:'提交姓名种子',message:'将提交 ' + launchTargets.length + ' 个已预览姓名种子。<br>系统仍会在搜索资料后再按画像过滤。',type:'info',confirmText:'提交',cancelText:'返回'}))) return;
      const payload = Object.assign({}, extra || {}, {
        add_friend_targets: launchTargets.map(function (x) {
          return { name: x.name, seed_score: x.score, seed_stage: x.stage, candidate_id: x.candidate_id || 0 };
        }),
      });
      // 记住本次输入,方便刷新后复用
      try {
        localStorage.setItem('fb_name_hunter_last', namesRaw);
        if (greetingRaw) localStorage.setItem('fb_name_hunter_greeting', greetingRaw);
      } catch (e) { /* localStorage may be disabled */ }
      if (greetingRaw.trim()) payload.greeting = greetingRaw.trim();
      // 关闭输入模态,再走常规 launch 路径
      const m = document.getElementById('fb-name-hunter-input');
      if (m) m.remove();
      fbLaunchPreset(presetKey, deviceId, payload);
    };
  };

  window.fbOpenNameHunterCandidates = async function () {
    const overlay = _fbModalOverlay('fb-name-hunter-candidates');
    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:980px;width:96%;max-height:88vh;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div>
            <div style="font-size:18px;font-weight:700">🔎 点名候选池</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">姓名种子 → 资料识别 → qualified 后才允许触达</div>
          </div>
          <button onclick="document.getElementById('fb-name-hunter-candidates').remove()" style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
        </div>
        <div id="fb-nh-cand-body" style="font-size:12px;color:var(--text-dim)">加载中...</div>
      </div>`;
    try {
      const personaKey = (_fbActivePersona && _fbActivePersona.persona_key) || '';
      const qs = (personaKey ? '&persona_key=' + encodeURIComponent(personaKey) : '');
      const pair = await Promise.all([
        api('GET', '/facebook/name-hunter/candidates?limit=120' + qs),
        api('GET', '/facebook/name-hunter/stats?' + (personaKey ? 'persona_key=' + encodeURIComponent(personaKey) : '')),
      ]);
      const r = pair[0] || {};
      const stats = pair[1] || {};
      const rows = (r && r.items) || [];
      const body = document.getElementById('fb-nh-cand-body');
      if (!body) return;
      const qualifiedN = rows.filter(function (x) { return x.status === 'qualified'; }).length;
      const minReady = 3;
      const badgeColor = function (st) {
        if (st === 'qualified') return '#22c55e';
        if (st === 'seeded') return '#38bdf8';
        if (st === 'review_required') return '#f59e0b';
        if (st === 'rejected' || st === 'weak_seed') return '#ef4444';
        if (st === 'friend_requested' || st === 'greeted') return '#a78bfa';
        return '#94a3b8';
      };
      const html = rows.map(function (x) {
        const ins = x.insights || {};
        const ev = ins.qualification_evidence || {};
        const gaps = ev.gaps || [];
        const reasons = (gaps.length ? gaps : (ins.reasons || ins.top_reasons || [])).join(' / ');
        const seed = ins.seed_score == null ? '-' : String(ins.seed_score);
        const prof = ins.profile_score == null ? '-' : String(ins.profile_score);
        const evidence = ev.age_37plus_confirmed
          ? '37+'
          : (gaps.indexOf('age_30s_needs_manual_37plus_review') >= 0 ? '30s复核' : '-');
        return `<tr>
          <td style="padding:8px;font-weight:600;color:var(--text)">${_escHtml(x.display_name || '')}</td>
          <td style="padding:8px"><span style="color:${badgeColor(x.status)};font-weight:700">${_escHtml(x.status || '')}</span></td>
          <td style="padding:8px;text-align:right;color:#38bdf8;font-weight:700">${seed}</td>
          <td style="padding:8px;text-align:right;color:#22c55e;font-weight:700">${prof}</td>
          <td style="padding:8px;text-align:center;color:${ev.age_37plus_confirmed ? '#22c55e' : '#f59e0b'};font-weight:700">${_escHtml(evidence)}</td>
          <td style="padding:8px;color:var(--text-dim);font-size:11px">${_escHtml(reasons)}</td>
          <td style="padding:8px;color:var(--text-muted);font-size:11px">${_escHtml(x.last_touch_at || x.created_at || '')}</td>
          <td style="padding:8px;text-align:right;white-space:nowrap">
            <button onclick="fbNameHunterCandidateAction(${Number(x.id) || 0}, 'qualify')" title="人工确认为高匹配" style="padding:4px 7px;border:1px solid rgba(34,197,94,.35);background:rgba(34,197,94,.12);color:#22c55e;border-radius:6px;cursor:pointer;font-size:11px">通过</button>
            <button onclick="fbNameHunterCandidateAction(${Number(x.id) || 0}, 'requeue')" title="重新进入资料预筛" style="padding:4px 7px;border:1px solid rgba(14,165,233,.35);background:rgba(14,165,233,.12);color:#38bdf8;border-radius:6px;cursor:pointer;font-size:11px">重筛</button>
            <button onclick="fbNameHunterCandidateAction(${Number(x.id) || 0}, 'blocklist')" title="排除该候选" style="padding:4px 7px;border:1px solid rgba(239,68,68,.35);background:rgba(239,68,68,.12);color:#ef4444;border-radius:6px;cursor:pointer;font-size:11px">排除</button>
          </td>
        </tr>`;
      }).join('');
      const sourceRows = ((stats && stats.sources) || []).slice(0, 4).map(function (s) {
        const health = s.source_health || 'learning';
        const hc = health === 'strong' ? '#22c55e' : (health === 'degraded' ? '#ef4444' : '#f59e0b');
        return `<div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:9px">
          <div style="font-size:10px;color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_escHtml(s.source_ref || 'name_hunter')}</div>
          <div style="display:flex;justify-content:space-between;gap:6px;margin-top:4px">
            <span style="color:#22c55e;font-weight:700">Q ${Number(s.qualified || 0)}</span>
            <span style="color:var(--text-muted)">总 ${Number(s.total || 0)}</span>
            <span style="color:#38bdf8">${Math.round(Number(s.qualified_rate || 0) * 100)}%</span>
          </div>
          <div style="margin-top:4px;color:${hc};font-size:10px;font-weight:700">${_escHtml(health)}</div>
        </div>`;
      }).join('');
      body.innerHTML = `
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-bottom:10px;flex-wrap:wrap">
          <button onclick="fbStartNameHunterPrescreen()" style="padding:7px 12px;background:rgba(14,165,233,.16);border:1px solid rgba(14,165,233,.4);color:#38bdf8;border-radius:8px;font-weight:600;cursor:pointer;font-size:12px">
            只预筛资料
          </button>
          <button onclick="fbStartNameHunterTouchQualified()" ${qualifiedN >= minReady ? '' : 'disabled'}
            style="padding:7px 12px;background:${qualifiedN >= minReady ? '#22c55e' : '#334155'};border:0;color:${qualifiedN >= minReady ? '#fff' : '#94a3b8'};border-radius:8px;font-weight:600;cursor:${qualifiedN >= minReady ? 'pointer' : 'not-allowed'};font-size:12px">
            触达 qualified (${qualifiedN}/${minReady})
          </button>
        </div>
        <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-bottom:10px">
          ${['seeded','qualified','rejected','greeted'].map(function (st) {
            const n = rows.filter(function (x) { return x.status === st; }).length;
            return `<div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px">
              <div style="font-size:10px;color:var(--text-dim)">${st}</div>
              <div style="font-size:20px;font-weight:700;color:${badgeColor(st)}">${n}</div>
            </div>`;
          }).join('')}
        </div>
        <div style="display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:8px;margin-bottom:10px">
          ${sourceRows || '<div style="grid-column:1/-1;color:var(--text-muted);background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px">暂无名字包复盘数据</div>'}
        </div>
        <table style="width:100%;border-collapse:collapse;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;overflow:hidden">
          <thead><tr style="color:var(--text-muted);font-size:11px">
            <th style="text-align:left;padding:8px">姓名</th>
            <th style="text-align:left;padding:8px">状态</th>
            <th style="text-align:right;padding:8px">种子分</th>
            <th style="text-align:right;padding:8px">资料分</th>
            <th style="text-align:center;padding:8px">37+</th>
            <th style="text-align:left;padding:8px">原因</th>
            <th style="text-align:left;padding:8px">更新时间</th>
            <th style="text-align:right;padding:8px">操作</th>
          </tr></thead>
          <tbody>${html || '<tr><td colspan="8" style="padding:18px;text-align:center;color:var(--text-muted)">暂无候选</td></tr>'}</tbody>
        </table>`;
    } catch (e) {
      const body = document.getElementById('fb-nh-cand-body');
      if (body) body.innerHTML = '<div style="color:#ef4444">加载失败: ' + _escHtml(e.message || e) + '</div>';
    }
  };

  window.fbNameHunterCandidateAction = async function (candidateId, action) {
    if (!candidateId) return;
    if (action === 'blocklist' && !(await ocDialog({title:'排除候选',message:'排除后该候选不会再进入触达。',type:'warning',confirmText:'确认排除',cancelText:'取消'}))) return;
    try {
      await api('POST', '/facebook/name-hunter/candidates/' + encodeURIComponent(candidateId) + '/action', {
        action: action,
      });
      showToast('候选已更新: ' + action, 'success');
      fbOpenNameHunterCandidates();
    } catch (e) {
      showToast('候选更新失败: ' + (e.message || e), 'error');
    }
  };

  async function _fbFirstOnlineDevice() {
    const r = await api('GET', '/platforms/facebook/device-grid');
    const d = ((r && r.devices) || []).find(function (x) { return x.online; });
    return d && d.device_id;
  }

  window.fbStartNameHunterPrescreen = async function () {
    try {
      const did = await _fbFirstOnlineDevice();
      if (!did) { showToast('没有在线 Facebook 设备', 'warning'); return; }
      const personaKey = (_fbActivePersona && _fbActivePersona.persona_key) || '';
      const r = await api('POST', '/facebook/name-hunter/prescreen', {
        device_id: did,
        persona_key: personaKey,
        max_targets: 20,
      });
      showToast('已创建点名预筛任务: ' + ((r && r.task_id) || ''), 'success');
    } catch (e) {
      showToast('创建预筛任务失败: ' + (e.message || e), 'error');
    }
  };

  window.fbStartNameHunterTouchQualified = async function () {
    try {
      const did = await _fbFirstOnlineDevice();
      if (!did) { showToast('没有在线 Facebook 设备', 'warning'); return; }
      if (!(await ocDialog({title:'批量触达',message:'只会触达候选池中 status=qualified 的用户。',type:'info',confirmText:'开始触达',cancelText:'取消'}))) return;
      const personaKey = (_fbActivePersona && _fbActivePersona.persona_key) || '';
      const r = await api('POST', '/facebook/name-hunter/touch-qualified', {
        device_id: did,
        persona_key: personaKey,
        max_targets: 5,
        min_qualified_ready: 3,
        send_greeting_inline: true,
      });
      showToast('已创建 qualified 触达任务: ' + ((r && r.task_id) || ''), 'success');
    } catch (e) {
      showToast('创建触达任务失败: ' + (e.message || e), 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // P1 Sprint A: 通用 schema-driven 启动对话框
  // 根据 preset.input_schema 动态渲染表单字段，覆盖 friend_growth 等新版 preset
  // ════════════════════════════════════════════════════════
  function _escHtml(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;')
      .replace(/>/g, '&gt;').replace(/"/g, '&quot;');
  }

  function _lsKey(presetKey, field) { return 'fb_launch_input::' + presetKey + '::' + field; }

  function _readLastValue(presetKey, field) {
    try { return localStorage.getItem(_lsKey(presetKey, field)) || ''; }
    catch (e) { return ''; }
  }

  function _saveValue(presetKey, field, value) {
    try { localStorage.setItem(_lsKey(presetKey, field), value || ''); }
    catch (e) { /* ignore */ }
  }

  // 渲染单个字段的 HTML — 根据 spec.type 分发
  // prefill: 调用方提供的预填值；非空时优先于 localStorage（用于"任务徽章重打 dialog"场景）
  function _renderField(presetKey, field, spec, persona, prefill) {
    const label = _escHtml(spec.label || field);
    const help = spec.help ? `<div style="font-size:10px;color:var(--text-dim);margin-top:3px;line-height:1.4">${_escHtml(spec.help)}</div>` : '';
    const required = spec.required ? '<span style="color:#ef4444;margin-left:3px">*</span>' : '';
    const aiBtn = spec.ai_assist
      ? `<button type="button" data-ai-field="${field}"
            onclick="fbDialogAiSuggest('${presetKey}','${field}')"
            style="position:absolute;top:6px;right:8px;padding:2px 8px;background:rgba(168,85,247,.15);
                   border:1px solid rgba(168,85,247,.4);color:#c084fc;border-radius:4px;
                   font-size:10px;cursor:pointer">✨ AI 建议</button>`
      : '';
    // 优先级：prefill (任务参数回填) > localStorage (用户上次输入)
    let lastVal;
    if (prefill != null && prefill !== '') {
      lastVal = Array.isArray(prefill) ? prefill.join('\n') : String(prefill);
    } else {
      lastVal = _readLastValue(presetKey, field);
    }
    const personaSeeds = (persona.seed_group_keywords || []).slice(0, 3).join('\n');
    let inputHtml = '';
    if (spec.type === 'list_str') {
      // 多行 textarea；fallback_from=persona.seed_group_keywords 时提示当前 persona 的 seeds
      const ph = _escHtml(spec.placeholder || (personaSeeds || ''));
      const fb = spec.fallback_from === 'persona.seed_group_keywords' && personaSeeds
        ? `<div style="font-size:10px;color:#60a5fa;margin-top:3px">💡 留空将使用客群默认: ${_escHtml(personaSeeds.split('\n').join(' / '))}</div>`
        : '';
      inputHtml = `
        <textarea data-field="${field}" data-type="list_str" rows="${Math.min(Math.max(spec.max||3,2),5)}"
          placeholder="${ph}"
          style="width:100%;box-sizing:border-box;background:var(--bg-main);border:1px solid var(--border);
                 color:var(--text);padding:8px 10px;border-radius:6px;font-size:13px;
                 font-family:inherit;resize:vertical">${_escHtml(lastVal)}</textarea>${fb}`;
    } else if (spec.type === 'text') {
      const ph = _escHtml(spec.placeholder || '');
      const max = spec.max_chars || 200;
      inputHtml = `
        <div style="position:relative">
          ${aiBtn}
          <textarea data-field="${field}" data-type="text" rows="${max > 80 ? 3 : 2}"
            maxlength="${max}" placeholder="${ph}"
            oninput="fbDialogCharCount('${field}', ${max})"
            style="width:100%;box-sizing:border-box;background:var(--bg-main);border:1px solid var(--border);
                   color:var(--text);padding:8px 10px;${spec.ai_assist?'padding-right:80px;':''}
                   border-radius:6px;font-size:12px;font-family:inherit;resize:vertical">${_escHtml(lastVal)}</textarea>
          <div style="font-size:10px;color:var(--text-dim);text-align:right;margin-top:2px">
            <span data-charcount="${field}">${lastVal.length}</span>/${max}
          </div>
        </div>`;
    } else if (spec.type === 'int') {
      const def = lastVal || (spec.default != null ? spec.default : '');
      inputHtml = `
        <input type="number" data-field="${field}" data-type="int"
          min="${spec.min || 1}" max="${spec.max || 999}" value="${_escHtml(def)}"
          style="width:120px;background:var(--bg-main);border:1px solid var(--border);
                 color:var(--text);padding:6px 10px;border-radius:6px;font-size:13px">
        <span style="font-size:11px;color:var(--text-dim);margin-left:8px">
          范围 ${spec.min || 1}–${spec.max || 999}
        </span>`;
    } else {
      inputHtml = `<div style="color:#f87171;font-size:11px">⚠ 未知字段类型: ${_escHtml(spec.type)}</div>`;
    }
    return `
      <div data-field-row="${field}" style="margin-bottom:14px">
        <label style="display:block;font-size:12px;color:var(--text);font-weight:600;margin-bottom:4px">
          ${label}${required}
        </label>
        ${inputHtml}
        ${help}
      </div>`;
  }

  // 字符数实时更新
  window.fbDialogCharCount = function (field, max) {
    const ta = document.querySelector('textarea[data-field="' + field + '"]');
    const cnt = document.querySelector('span[data-charcount="' + field + '"]');
    if (ta && cnt) cnt.textContent = ta.value.length;
  };

  // ═══════════════════════════════════════════════════════════
  // AI 模板库（共享：手动"AI 建议"弹窗 + 表单首次自动预填 共用）
  // P2 (Sprint D) 会替换为后端 /ai/fb/suggest-line 调用。
  // ═══════════════════════════════════════════════════════════
  var _FB_AI_TEMPLATES = {
    verification_note: {
      jp: ['您好🌸看到我们都在同一个群，想认识下志同道合的朋友 ☺️',
           'はじめまして🌸同じグループで拝見しました。仲良くしていただけたら嬉しいです',
           '同じ趣味の方とつながりたく、フォロー失礼します😊'],
      in: ['Hello! Saw we are in the same group — would love to connect 🌸',
           'Hi there, I noticed we share similar interests. Nice to meet you!',
           'Namaste 🙏 saw your profile in our group, would be great to connect.'],
      zh: ['您好，看到我们都在同一个群组，想认识一下 🌸',
           '你好呀，刚刚在群里看到你的留言，希望能交个朋友 ☺️',
           '同好相聚，希望能多多交流'],
    },
    greeting: {
      jp: ['ご通過ありがとうございます😊これからよろしくお願いいたします🌸',
           'こんにちは🌸お友達になっていただけて嬉しいです。よろしくお願いします',
           'はじめまして！同じ趣味でつながれて嬉しいです、よろしくお願いします😊'],
      in: ['Thanks for accepting! Looking forward to chatting 🌸',
           'Hi! So glad we connected — what brought you to the group?',
           'Hello there! Would love to know more about your interests 😊'],
      zh: ['谢谢通过！很高兴认识你 🌸',
           '你好呀～我也是在群里看到你的，期待多多交流',
           '感谢通过好友请求，希望我们可以多多交流分享'],
    },
  };

  // 根据 persona 语言获取某个字段的最佳推荐文案
  function _fbGetAiDefault(field, persona) {
    var personaKey = (persona || {}).persona_key || 'jp_female_midlife';
    var lang = ((persona || {}).language || personaKey || '').slice(0, 2).toLowerCase();
    var pool = (_FB_AI_TEMPLATES[field] || {})[lang]
      || (_FB_AI_TEMPLATES[field] || {}).jp || [];
    return pool[0] || '';
  }

  // ✨ AI 建议弹窗（点击「AI 建议」按钮触发）
  // P2 Sprint D: 优先调用后端 /ai/fb/suggest-line（LLM 生成个性化文案），
  // 失败/超时自动降级到本地 _FB_AI_TEMPLATES。
  // items: Array<string> | Array<{text, proven?, reply_rate?}>
  function _fbRenderAiPop(field, personaKey, items) {
    var existing = document.getElementById('fb-ai-suggest-pop');
    if (existing) existing.remove();
    // 统一格式
    var normalized = items.map(function (c) {
      return typeof c === 'string' ? { text: c, proven: false, reply_rate: null } : c;
    });
    var pop = document.createElement('div');
    pop.id = 'fb-ai-suggest-pop';
    pop.style.cssText = 'position:fixed;z-index:10001;background:var(--bg-card);border:1px solid #a855f7;'
      + 'border-radius:10px;padding:12px;max-width:520px;width:92%;box-shadow:0 10px 40px rgba(0,0,0,.5);'
      + 'top:50%;left:50%;transform:translate(-50%,-50%)';
    pop.innerHTML = '<div style="font-size:13px;font-weight:600;margin-bottom:8px;color:#c084fc">'
      + '✨ ' + (field === 'verification_note' ? '验证语' : '打招呼') + '建议'
      + '<span style="font-size:10px;color:var(--text-dim);font-weight:normal;margin-left:6px">'
      + '基于客群 ' + _escHtml(personaKey) + '</span></div>'
      + normalized.map(function (c, i) {
        var badge = '';
        if (c.proven && c.reply_rate != null) {
          badge = '<span style="display:inline-block;margin-left:6px;padding:1px 6px;background:rgba(34,197,94,.12);color:#22c55e;border-radius:3px;font-size:9px;font-weight:600;vertical-align:middle">高回复 ' + (c.reply_rate * 100).toFixed(0) + '%</span>';
        } else if (c.proven) {
          badge = '<span style="display:inline-block;margin-left:6px;padding:1px 6px;background:rgba(34,197,94,.12);color:#22c55e;border-radius:3px;font-size:9px;font-weight:600;vertical-align:middle">历史验证</span>';
        }
        return '<div style="padding:8px 10px;background:var(--bg-main);border:1px solid ' + (c.proven ? 'rgba(34,197,94,.3)' : 'var(--border)') + ';'
          + 'border-radius:6px;margin-bottom:6px;font-size:12px;cursor:pointer;line-height:1.5"'
          + ' onclick="fbDialogApplySuggest(\'' + field + '\',' + i + ')"'
          + ' onmouseover="this.style.borderColor=\'#a855f7\'"'
          + ' onmouseout="this.style.borderColor=\'' + (c.proven ? 'rgba(34,197,94,.3)' : 'var(--border)') + '\'">'
          + _escHtml(c.text) + badge + '</div>';
      }).join('')
      + '<div style="display:flex;gap:8px;margin-top:8px;justify-content:flex-end">'
      + '<button onclick="fbOpenGreetingLibrary()" style="padding:5px 14px;background:none;border:1px solid rgba(168,85,247,.3);color:#c084fc;border-radius:6px;cursor:pointer;font-size:11px">📚 话术库</button>'
      + '<button onclick="document.getElementById(\'fb-ai-suggest-pop\').remove()"'
      + ' style="padding:5px 14px;background:none;border:1px solid var(--border);'
      + 'color:var(--text-muted);border-radius:6px;cursor:pointer;font-size:11px">关闭</button></div>';
    document.body.appendChild(pop);
    window._fbAiSuggestList = normalized.map(function (c) { return c.text; });
  }

  // AI 建议短时缓存（30s TTL，避免反复点击重复调用 LLM）
  var _fbAiSuggestCache = {};  // key: field+personaKey → {ts, items}
  var _FB_AI_CACHE_TTL = 30000;

  window.fbDialogAiSuggest = function (presetKey, field) {
    var persona = (_fbActivePersona || {});
    var personaKey = persona.persona_key || 'jp_female_midlife';
    var lang = (persona.language || personaKey || '').slice(0, 2).toLowerCase();
    // 本地兜底候选
    var localCandidates = (_FB_AI_TEMPLATES[field] || {})[lang]
      || (_FB_AI_TEMPLATES[field] || {}).jp
      || ['（暂无该字段的预设模板）'];

    // 命中缓存则直接展示
    var cacheKey = field + ':' + personaKey;
    var cached = _fbAiSuggestCache[cacheKey];
    if (cached && (Date.now() - cached.ts < _FB_AI_CACHE_TTL)) {
      _fbRenderAiPop(field, personaKey, cached.items);
      return;
    }

    // 先显示"加载中"
    var existing = document.getElementById('fb-ai-suggest-pop');
    if (existing) existing.remove();
    var loading = document.createElement('div');
    loading.id = 'fb-ai-suggest-pop';
    loading.style.cssText = 'position:fixed;z-index:10001;background:var(--bg-card);border:1px solid #a855f7;'
      + 'border-radius:10px;padding:20px;max-width:520px;width:92%;box-shadow:0 10px 40px rgba(0,0,0,.5);'
      + 'top:50%;left:50%;transform:translate(-50%,-50%);text-align:center;color:#c084fc;font-size:13px';
    loading.textContent = '✨ AI 正在生成个性化文案...';
    document.body.appendChild(loading);

    // 尝试调用后端 API
    var ctrl = new AbortController();
    var timer = setTimeout(function () { ctrl.abort(); }, 6000);  // 6s 超时
    fetch(_apiUrl('/ai/fb/suggest-line'), {
      method: 'POST',
      headers: Object.assign({ 'Content-Type': 'application/json' }, _authHeaders()),
      body: JSON.stringify({ field: field, persona_key: personaKey, language: lang }),
      signal: ctrl.signal,
    }).then(function (r) { return r.json(); }).then(function (data) {
      clearTimeout(timer);
      // 优先使用 items（结构化，含 proven/reply_rate），向后兼容 suggestions（纯字符串数组）
      var list = (data && data.items && data.items.length) ? data.items
        : (data && data.suggestions && data.suggestions.length) ? data.suggestions
        : localCandidates;
      _fbAiSuggestCache[cacheKey] = { ts: Date.now(), items: list };
      _fbRenderAiPop(field, personaKey, list);
    }).catch(function () {
      clearTimeout(timer);
      _fbRenderAiPop(field, personaKey, localCandidates);
    });
  };

  window.fbDialogApplySuggest = function (field, idx) {
    const list = window._fbAiSuggestList || [];
    const text = list[idx];
    if (text == null) return;
    const ta = document.querySelector('textarea[data-field="' + field + '"]');
    if (ta) {
      ta.value = text;
      ta.dispatchEvent(new Event('input'));
      ta.focus();
    }
    const pop = document.getElementById('fb-ai-suggest-pop');
    if (pop) pop.remove();
  };

  // ════════════════════════════════════════════════════════
  // 话术库管理 UI（P1）
  // ════════════════════════════════════════════════════════
  var _fbGLSort = 'reply_rate';
  var _fbGLSearch = '';
  var _fbGLItems = [];

  window.fbOpenGreetingLibrary = async function (sort) {
    if (sort) _fbGLSort = sort;
    var existing = document.getElementById('fb-greeting-lib-modal');
    var overlay;
    if (existing) {
      overlay = existing;
    } else {
      overlay = _fbModalOverlay('fb-greeting-lib-modal');
      overlay.innerHTML = '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:24px;max-width:860px;width:96%;max-height:88vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.4)"></div>';
    }
    overlay.querySelector('div').innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:40px 0;font-size:13px">加载话术库...</div>';

    try {
      var pk = (_fbActivePersona || {}).persona_key || '';
      var data = await api('GET', '/facebook/greeting-library' + (pk ? '?persona_key=' + encodeURIComponent(pk) + '&sort=' + _fbGLSort + '&limit=100' : '?sort=' + _fbGLSort + '&limit=100'));
      _fbGLItems = (data && data.items) || [];
      var stats = (data && data.stats) || {};
      _fbRenderGLContent(overlay, data, stats);
    } catch (e) {
      overlay.querySelector('div').innerHTML = '<div style="color:#ef4444;text-align:center;padding:20px">加载失败: ' + _escHtml(e.message || String(e)) + '</div>';
    }
  };

  function _fbRenderGLContent(overlay, data, stats) {
    var pk = data.persona_key || '';
    var items = _fbGLSearch ? _fbGLItems.filter(function (g) { return (g.text_ja || '').indexOf(_fbGLSearch) !== -1 || (g.style_tag || '').indexOf(_fbGLSearch) !== -1; }) : _fbGLItems;

    // ── 统计卡片 ──
    var maxRR = 0;
    items.forEach(function (g) { if ((g.reply_rate || 0) > maxRR) maxRR = g.reply_rate; });
    var statsHtml = '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:14px">'
      + '<div style="padding:10px 14px;background:var(--bg-main);border:1px solid var(--border);border-radius:10px">'
      + '<div style="font-size:9px;color:var(--text-dim)">总话术数</div>'
      + '<div style="font-size:22px;font-weight:700;color:var(--text)">' + (stats.total || 0) + '</div></div>'
      + '<div style="padding:10px 14px;background:var(--bg-main);border:1px solid var(--border);border-radius:10px">'
      + '<div style="font-size:9px;color:var(--text-dim)">平均回复率</div>'
      + '<div style="font-size:22px;font-weight:700;color:#22c55e">' + ((stats.avg_reply_rate || 0) * 100).toFixed(1) + '%</div></div>'
      + '<div style="padding:10px 14px;background:var(--bg-main);border:1px solid var(--border);border-radius:10px">'
      + '<div style="font-size:9px;color:var(--text-dim);margin-bottom:4px">🏆 最佳话术</div>'
      + ((stats.best_greetings || []).slice(0, 2).map(function (g) {
          return '<div style="font-size:9px;color:var(--text-muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;line-height:1.5">'
            + '<span style="color:#22c55e;font-weight:600">' + ((g.reply_rate || 0) * 100).toFixed(0) + '%</span> '
            + _escHtml((g.text_ja || '').substring(0, 30)) + '</div>';
        }).join('') || '<span style="font-size:9px;color:var(--text-dim)">暂无数据</span>')
      + '</div></div>';

    // ── 工具栏 ──
    var sortBtns = [
      { key: 'reply_rate', label: '回复率' },
      { key: 'sent_count', label: '发送量' },
      { key: 'created_at', label: '最新' },
    ].map(function (s) {
      var active = _fbGLSort === s.key;
      return '<button onclick="fbOpenGreetingLibrary(\'' + s.key + '\')" style="padding:3px 10px;border-radius:4px;border:1px solid ' + (active ? 'rgba(24,119,242,.5)' : 'var(--border)') + ';background:' + (active ? 'rgba(24,119,242,.1)' : 'none') + ';color:' + (active ? '#3b82f6' : 'var(--text-dim)') + ';cursor:pointer;font-size:9px">' + s.label + '</button>';
    }).join('');
    var toolHtml = '<div style="display:flex;align-items:center;gap:8px;margin-bottom:12px;flex-wrap:wrap">'
      + '<input id="fb-gl-search" type="text" placeholder="🔍 搜索话术..." value="' + _escAttr(_fbGLSearch) + '" oninput="_fbGLSearchFilter()" style="flex:1;min-width:140px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);padding:6px 10px;border-radius:6px;font-size:10px">'
      + '<div style="display:flex;gap:4px">' + sortBtns + '</div>'
      + '<button onclick="fbAddGreetingPrompt()" style="padding:4px 12px;background:linear-gradient(135deg,#22c55e,#16a34a);border:none;color:#fff;border-radius:6px;cursor:pointer;font-size:10px;font-weight:600">＋ 新增</button>'
      + '<button onclick="fbAIGenerateGreetings()" style="padding:4px 12px;background:linear-gradient(135deg,#a855f7,#7c3aed);border:none;color:#fff;border-radius:6px;cursor:pointer;font-size:10px;font-weight:600">✨ AI 生成</button>'
      + '</div>';

    // ── 表格 ──
    var tableRows = items.map(function (g) {
      var rr = g.reply_rate || 0;
      var rrPct = (rr * 100).toFixed(1);
      var rrColor = rr >= 0.2 ? '#22c55e' : (rr >= 0.05 ? '#eab308' : '#64748b');
      var barW = maxRR > 0 ? Math.max((rr / maxRR) * 100, 2) : 2;
      var tagColors = { ai_gen: '#a855f7', manual: '#f59e0b', casual: '#60a5fa', formal: '#06b6d4', warm: '#ec4899' };
      var tagColor = tagColors[g.style_tag] || '#60a5fa';
      return '<tr style="border-bottom:1px solid rgba(255,255,255,.04)">'
        + '<td style="padding:7px 6px;font-size:11px;max-width:320px;color:var(--text);cursor:pointer" title="' + _escHtml(g.text_ja || '') + '" ondblclick="fbEditGreeting(' + g.id + ',this)">'
        + '<div style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _escHtml((g.text_ja || '').substring(0, 60)) + '</div></td>'
        + '<td style="padding:7px 4px;text-align:center"><span style="padding:1px 6px;background:' + tagColor + '15;color:' + tagColor + ';border-radius:3px;font-size:8px;font-weight:600">' + _escHtml(g.style_tag || '-') + '</span></td>'
        + '<td style="padding:7px 6px;min-width:90px"><div style="display:flex;align-items:center;gap:4px">'
        + '<div style="flex:1;height:6px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden"><div style="height:100%;width:' + barW + '%;background:' + rrColor + ';border-radius:3px;transition:width .3s"></div></div>'
        + '<span style="font-size:10px;color:' + rrColor + ';font-weight:600;min-width:32px;text-align:right">' + rrPct + '%</span>'
        + '</div></td>'
        + '<td style="padding:7px 4px;font-size:10px;text-align:center;color:var(--text-muted)">' + (g.sent_count || 0) + '/' + (g.replied_count || 0) + '</td>'
        + '<td style="padding:7px 4px;text-align:center;white-space:nowrap">'
        + '<button onclick="fbCopyGreeting(' + g.id + ')" style="font-size:9px;padding:2px 6px;background:none;border:1px solid rgba(96,165,250,.25);color:#60a5fa;border-radius:3px;cursor:pointer;margin-right:2px" title="复制">📋</button>'
        + '<button onclick="fbDeleteGreeting(' + g.id + ')" style="font-size:9px;padding:2px 6px;background:none;border:1px solid rgba(239,68,68,.25);color:#ef4444;border-radius:3px;cursor:pointer" title="删除">✕</button></td>'
        + '</tr>';
    }).join('');

    var tableHtml = items.length
      ? '<div style="max-height:380px;overflow-y:auto"><table style="width:100%;border-collapse:collapse"><thead><tr style="border-bottom:2px solid var(--border);position:sticky;top:0;background:var(--bg-card);z-index:1">'
        + '<th style="padding:6px;font-size:9px;color:var(--text-dim);text-align:left;font-weight:600">话术内容<span style="font-weight:400;opacity:.6"> (双击编辑)</span></th>'
        + '<th style="padding:6px;font-size:9px;color:var(--text-dim);text-align:center;font-weight:600">来源</th>'
        + '<th style="padding:6px;font-size:9px;color:var(--text-dim);text-align:left;font-weight:600">回复率</th>'
        + '<th style="padding:6px;font-size:9px;color:var(--text-dim);text-align:center;font-weight:600">发/回</th>'
        + '<th style="padding:6px;font-size:9px;color:var(--text-dim);text-align:center;font-weight:600">操作</th>'
        + '</tr></thead><tbody>' + tableRows + '</tbody></table></div>'
        + '<div style="margin-top:6px;font-size:9px;color:var(--text-dim);text-align:right">显示 ' + items.length + ' / ' + _fbGLItems.length + ' 条</div>'
      : '<div style="text-align:center;color:var(--text-dim);padding:30px 0;font-size:12px">话术库为空 — 点击「＋ 新增」或「✨ AI 生成」添加</div>';

    overlay.querySelector('div').innerHTML =
      '<div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">'
      + '<div><div style="font-size:16px;font-weight:700;color:var(--text)">📚 话术库</div>'
      + '<div style="font-size:9px;color:var(--text-dim);margin-top:2px">客群: ' + _escHtml(pk || '默认') + '</div></div>'
      + '<button onclick="document.getElementById(\'fb-greeting-lib-modal\').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:16px">✕</button>'
      + '</div>'
      + statsHtml + toolHtml + tableHtml;
  }

  window._fbGLSearchFilter = function () {
    var input = document.getElementById('fb-gl-search');
    _fbGLSearch = input ? input.value.trim() : '';
    // 重新渲染表格（不重新请求）
    var overlay = document.getElementById('fb-greeting-lib-modal');
    if (!overlay) return;
    var pk = (_fbActivePersona || {}).persona_key || '';
    var stats = {};
    try {
      // 从已有数据重算 stats
      var total = _fbGLItems.length;
      var rateSum = 0; var rateCount = 0;
      _fbGLItems.forEach(function (g) { if (g.sent_count > 0) { rateSum += g.reply_rate || 0; rateCount++; } });
      stats = { total: total, avg_reply_rate: rateCount > 0 ? rateSum / rateCount : 0, best_greetings: _fbGLItems.filter(function (g) { return g.sent_count >= 3; }).sort(function (a, b) { return (b.reply_rate || 0) - (a.reply_rate || 0); }).slice(0, 5) };
    } catch (e) { stats = {}; }
    _fbRenderGLContent(overlay, { persona_key: pk, items: _fbGLItems }, stats);
    // 恢复搜索框焦点和值
    var newInput = document.getElementById('fb-gl-search');
    if (newInput) { newInput.value = _fbGLSearch; newInput.focus(); }
  };

  window.fbDeleteGreeting = async function (id) {
    if (!(await ocDialog({title:'删除话术',message:'删除后不可恢复，确认删除？',type:'danger',confirmText:'删除',dangerous:true}))) return;
    try {
      await api('DELETE', '/facebook/greeting-library/' + id);
      showToast('话术已删除', 'success');
      fbOpenGreetingLibrary();  // 刷新
    } catch (e) {
      showToast('删除失败: ' + (e.message || e), 'error');
    }
  };

  window.fbCopyGreeting = function (id) {
    var g = _fbGLItems.find(function (x) { return x.id === id; });
    if (!g) return;
    var text = g.text_ja || '';
    navigator.clipboard.writeText(text).then(function () {
      showToast('已复制到剪贴板', 'success');
    }).catch(function () {
      var ta = document.createElement('textarea');
      ta.value = text; ta.style.position = 'fixed'; ta.style.opacity = '0';
      document.body.appendChild(ta); ta.select(); document.execCommand('copy');
      document.body.removeChild(ta);
      showToast('已复制到剪贴板', 'success');
    });
  };

  // ── 新增话术 ──
  window.fbAddGreetingPrompt = function () {
    var old = document.getElementById('fb-gl-add-popup');
    if (old) { old.remove(); return; }
    var pk = (_fbActivePersona || {}).persona_key || '';
    var popup = document.createElement('div');
    popup.id = 'fb-gl-add-popup';
    popup.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:10001;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.45)';
    popup.innerHTML = '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;max-width:420px;width:90%">'
      + '<div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:10px">＋ 新增话术</div>'
      + '<textarea id="fb-gl-add-text" rows="3" placeholder="输入话术内容..." style="width:100%;background:var(--bg-main);border:1px solid var(--border);color:var(--text);padding:8px;border-radius:6px;font-size:11px;resize:vertical;box-sizing:border-box"></textarea>'
      + '<div style="display:flex;gap:8px;margin-top:6px">'
      + '<select id="fb-gl-add-type" style="background:var(--bg-main);border:1px solid var(--border);color:var(--text);padding:5px 8px;border-radius:5px;font-size:10px">'
      + '<option value="dm_greeting">打招呼</option><option value="verification">验证语</option></select>'
      + '<select id="fb-gl-add-style" style="background:var(--bg-main);border:1px solid var(--border);color:var(--text);padding:5px 8px;border-radius:5px;font-size:10px">'
      + '<option value="manual">手动</option><option value="casual">轻松</option><option value="formal">正式</option><option value="warm">温暖</option></select>'
      + '<span style="flex:1"></span>'
      + '<button onclick="document.getElementById(\'fb-gl-add-popup\').remove()" style="padding:5px 12px;background:none;border:1px solid var(--border);color:var(--text-dim);border-radius:6px;cursor:pointer;font-size:10px">取消</button>'
      + '<button onclick="_fbDoAddGreeting()" style="padding:5px 14px;background:linear-gradient(135deg,#22c55e,#16a34a);border:none;color:#fff;border-radius:6px;cursor:pointer;font-size:10px;font-weight:600">添加</button>'
      + '</div></div>';
    popup.addEventListener('click', function (e) { if (e.target === popup) popup.remove(); });
    document.body.appendChild(popup);
    document.getElementById('fb-gl-add-text').focus();
  };

  window._fbDoAddGreeting = async function () {
    var text = (document.getElementById('fb-gl-add-text') || {}).value || '';
    var topicId = (document.getElementById('fb-gl-add-type') || {}).value || 'dm_greeting';
    var styleTag = (document.getElementById('fb-gl-add-style') || {}).value || 'manual';
    if (!text.trim()) { showToast('请输入话术内容', 'error'); return; }
    try {
      var pk = (_fbActivePersona || {}).persona_key || '';
      await api('POST', '/facebook/greeting-library', { text_ja: text.trim(), persona_key: pk, topic_id: topicId, style_tag: styleTag });
      showToast('话术已添加', 'success');
      var popup = document.getElementById('fb-gl-add-popup');
      if (popup) popup.remove();
      fbOpenGreetingLibrary();
    } catch (e) {
      showToast((e.message || '').indexOf('已存在') !== -1 ? '此话术已存在' : '添加失败: ' + (e.message || e), 'error');
    }
  };

  // ── 双击编辑话术 ──
  window.fbEditGreeting = function (id, td) {
    var g = _fbGLItems.find(function (x) { return x.id === id; });
    if (!g) return;
    var origText = g.text_ja || '';
    var input = document.createElement('input');
    input.type = 'text';
    input.value = origText;
    input.style.cssText = 'width:100%;background:var(--bg-main);border:1px solid rgba(24,119,242,.5);color:var(--text);padding:4px 6px;border-radius:4px;font-size:11px;outline:none';
    td.innerHTML = '';
    td.appendChild(input);
    input.focus();
    input.select();
    var save = async function () {
      var newText = input.value.trim();
      if (!newText || newText === origText) { fbOpenGreetingLibrary(); return; }
      try {
        await api('PUT', '/facebook/greeting-library/' + id, { text_ja: newText });
        showToast('话术已更新', 'success');
        fbOpenGreetingLibrary();
      } catch (e) {
        showToast('更新失败: ' + (e.message || e), 'error');
        fbOpenGreetingLibrary();
      }
    };
    input.addEventListener('blur', save);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter') { e.preventDefault(); input.blur(); }
      if (e.key === 'Escape') { fbOpenGreetingLibrary(); }
    });
  };

  // ── AI 生成话术 ──
  window.fbAIGenerateGreetings = async function () {
    var old = document.getElementById('fb-gl-ai-popup');
    if (old) { old.remove(); return; }
    var p = _fbActivePersona || {};
    var pk = p.persona_key || '';
    var lang = p.language || 'ja';

    var popup = document.createElement('div');
    popup.id = 'fb-gl-ai-popup';
    popup.style.cssText = 'position:fixed;top:0;left:0;right:0;bottom:0;z-index:10001;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.45)';
    popup.innerHTML = '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;max-width:480px;width:92%">'
      + '<div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:4px">✨ AI 生成话术</div>'
      + '<div style="font-size:9px;color:var(--text-dim);margin-bottom:12px">基于客群 ' + _escHtml(pk) + ' 属性自动生成匹配话术</div>'
      + '<div style="display:flex;gap:8px;margin-bottom:12px">'
      + '<button onclick="_fbAIGenDo(\'verification_note\')" class="fb-ai-gen-btn" style="flex:1;padding:8px;background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.3);color:#60a5fa;border-radius:8px;cursor:pointer;font-size:11px;font-weight:600;transition:all .12s" onmouseover="this.style.background=\'rgba(59,130,246,.15)\'" onmouseout="this.style.background=\'rgba(59,130,246,.08)\'">📝 生成验证语</button>'
      + '<button onclick="_fbAIGenDo(\'greeting\')" class="fb-ai-gen-btn" style="flex:1;padding:8px;background:rgba(168,85,247,.08);border:1px solid rgba(168,85,247,.3);color:#c084fc;border-radius:8px;cursor:pointer;font-size:11px;font-weight:600;transition:all .12s" onmouseover="this.style.background=\'rgba(168,85,247,.15)\'" onmouseout="this.style.background=\'rgba(168,85,247,.08)\'">💬 生成打招呼</button>'
      + '</div>'
      + '<div id="fb-ai-gen-results" style="min-height:40px"></div>'
      + '<div style="display:flex;justify-content:flex-end;gap:8px;margin-top:10px">'
      + '<button onclick="document.getElementById(\'fb-gl-ai-popup\').remove()" style="padding:5px 12px;background:none;border:1px solid var(--border);color:var(--text-dim);border-radius:6px;cursor:pointer;font-size:10px">关闭</button>'
      + '<button id="fb-ai-gen-batch-btn" onclick="_fbAIGenBatchAdd()" style="display:none;padding:5px 14px;background:linear-gradient(135deg,#22c55e,#16a34a);border:none;color:#fff;border-radius:6px;cursor:pointer;font-size:10px;font-weight:600">全部入库</button>'
      + '</div></div>';
    popup.addEventListener('click', function (e) { if (e.target === popup) popup.remove(); });
    document.body.appendChild(popup);
  };

  var _fbAIGenSuggestions = [];
  window._fbAIGenDo = async function (field) {
    var results = document.getElementById('fb-ai-gen-results');
    if (!results) return;
    results.innerHTML = '<div style="text-align:center;padding:16px;color:var(--text-dim);font-size:11px">⏳ AI 正在生成中...</div>';
    // 禁用按钮
    document.querySelectorAll('.fb-ai-gen-btn').forEach(function (b) { b.style.opacity = '.5'; b.style.pointerEvents = 'none'; });
    var p = _fbActivePersona || {};
    try {
      var r = await api('POST', '/ai/fb/suggest-line', {
        field: field,
        persona_key: p.persona_key || '',
        language: p.language || 'ja',
      });
      _fbAIGenSuggestions = (r && r.items) || (r && r.suggestions || []).map(function (s) { return { text: s }; });
      var html = _fbAIGenSuggestions.map(function (item, idx) {
        var text = item.text || '';
        var proven = item.proven;
        var rr = item.reply_rate;
        return '<div style="display:flex;align-items:center;gap:8px;padding:8px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;margin-bottom:6px">'
          + '<div style="flex:1;font-size:11px;color:var(--text);line-height:1.4">' + _escHtml(text)
          + (proven ? ' <span style="font-size:8px;background:rgba(34,197,94,.15);color:#22c55e;padding:1px 4px;border-radius:3px">实证' + (rr ? ' ' + (rr * 100).toFixed(0) + '%' : '') + '</span>' : '')
          + '</div>'
          + '<button onclick="_fbAIGenAddOne(' + idx + ',this)" style="padding:3px 10px;background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.3);color:#22c55e;border-radius:5px;cursor:pointer;font-size:9px;font-weight:600;white-space:nowrap">入库</button>'
          + '</div>';
      }).join('');
      results.innerHTML = html || '<div style="text-align:center;padding:12px;color:var(--text-dim);font-size:11px">未能生成，请重试</div>';
      var batchBtn = document.getElementById('fb-ai-gen-batch-btn');
      if (batchBtn && _fbAIGenSuggestions.length > 1) batchBtn.style.display = '';
    } catch (e) {
      results.innerHTML = '<div style="text-align:center;padding:12px;color:#ef4444;font-size:11px">生成失败: ' + _escHtml(e.message || String(e)) + '</div>';
    }
    document.querySelectorAll('.fb-ai-gen-btn').forEach(function (b) { b.style.opacity = ''; b.style.pointerEvents = ''; });
  };

  window._fbAIGenAddOne = async function (idx, btn) {
    var item = _fbAIGenSuggestions[idx];
    if (!item) return;
    var pk = (_fbActivePersona || {}).persona_key || '';
    try {
      await api('POST', '/facebook/greeting-library', { text_ja: item.text, persona_key: pk, style_tag: 'ai_gen' });
      btn.textContent = '✓ 已入库';
      btn.style.opacity = '.5';
      btn.style.pointerEvents = 'none';
      btn.style.background = 'rgba(34,197,94,.05)';
    } catch (e) {
      btn.textContent = (e.message || '').indexOf('已存在') !== -1 ? '已存在' : '失败';
      btn.style.color = '#ef4444';
    }
  };

  window._fbAIGenBatchAdd = async function () {
    var pk = (_fbActivePersona || {}).persona_key || '';
    var items = _fbAIGenSuggestions.map(function (s) { return { text_ja: s.text, style_tag: 'ai_gen' }; });
    try {
      var r = await api('POST', '/facebook/greeting-library/batch-add', { items: items, persona_key: pk });
      showToast('已批量入库 ' + (r.added || 0) + ' 条', 'success');
      var popup = document.getElementById('fb-gl-ai-popup');
      if (popup) popup.remove();
      fbOpenGreetingLibrary();
    } catch (e) {
      showToast('批量入库失败: ' + (e.message || e), 'error');
    }
  };

  // 收集表单值并归一化
  function _collectFormValues(presetKey, schema) {
    const out = {};
    Object.keys(schema).forEach(function (field) {
      const spec = schema[field] || {};
      const el = document.querySelector('[data-field="' + field + '"]');
      if (!el) return;
      const raw = el.value || '';
      _saveValue(presetKey, field, raw);
      if (spec.type === 'list_str') {
        out[field] = raw.split(/[,\n;]+/).map(function (s) { return s.trim(); }).filter(Boolean);
      } else if (spec.type === 'int') {
        const n = parseInt(raw, 10);
        if (!isNaN(n)) out[field] = n;
      } else {
        out[field] = raw.trim();
      }
    });
    return out;
  }

  // 客户端预校验（与后端 _validate_preset_inputs 同义）
  function _localValidate(preset, values, persona) {
    const needs = preset.needs_input || [];
    const schema = preset.input_schema || {};
    const missing = [];
    needs.forEach(function (field) {
      const spec = schema[field] || {};
      if (!spec.required) return;
      const v = values[field];
      let filled = false;
      if (Array.isArray(v)) filled = v.length > 0;
      else if (typeof v === 'string') filled = v.trim().length > 0;
      else if (v != null) filled = true;
      if (filled) return;
      // fallback 路径
      if (spec.fallback_from === 'persona.seed_group_keywords') {
        const seeds = (persona.seed_group_keywords || []);
        if (seeds.length > 0) return;
      }
      missing.push(field);
    });
    return missing;
  }

  // 标红字段（接收 422 detail.missing 或本地预校验结果）
  function _markFieldErrors(missingFields) {
    document.querySelectorAll('[data-field-row]').forEach(function (row) {
      row.style.borderLeft = '';
      row.style.paddingLeft = '';
    });
    missingFields.forEach(function (field) {
      const row = document.querySelector('[data-field-row="' + field + '"]');
      if (row) {
        row.style.borderLeft = '3px solid #ef4444';
        row.style.paddingLeft = '8px';
      }
    });
  }

  window.fbOpenLaunchInputDialog = function (presetKey, deviceId, extra) {
    extra = extra || {};
    const preset = (_fbPresets || []).find(function (x) { return x.key === presetKey; });
    if (!preset || !preset.input_schema) {
      // 安全网：schema 不存在直接降级到无表单启动
      return fbLaunchPreset(presetKey, deviceId, extra);
    }
    const persona = (_fbAvailablePersonas || []).find(function (x) {
      return x.persona_key === extra.persona_key;
    }) || _fbActivePersona || {};

    const overlay = _fbModalOverlay('fb-launch-input-dialog');
    const schema = preset.input_schema;
    const prefill = extra.prefill || null;
    const fieldsHtml = Object.keys(schema).map(function (f) {
      return _renderField(presetKey, f, schema[f], persona, prefill ? prefill[f] : null);
    }).join('');
    // 来自失败任务徽章重打开时显示提示横幅
    const reopenHint = extra.reopenFromTask
      ? `<div style="margin-bottom:10px;padding:8px 12px;background:rgba(245,158,11,.1);
                     border:1px solid rgba(245,158,11,.35);border-radius:6px;color:#fbbf24;font-size:11px">
           🔁 已回填上次失败任务的参数（${_escHtml(extra.reopenFromTask)}），请修改必填项后重启
         </div>`
      : '';

    const _pColor = preset.color || '#60a5fa';

    // A: 空 schema 时展示步骤预览
    var _stepsPreview = '';
    if (!Object.keys(schema).length && preset.steps && preset.steps.length) {
      var _stFlow = (preset.steps || []).map(function (s) {
        var n = s.type.replace(/facebook_/g, '').replace(/_/g, ' ');
        return '<span style="padding:3px 8px;background:' + _pColor + '15;color:' + _pColor + ';border-radius:4px;font-size:10px;font-weight:600">' + n + '</span>';
      }).join('<span style="color:var(--text-dim);font-size:10px"> → </span>');
      _stepsPreview = '<div style="margin-bottom:10px;padding:10px 14px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px">'
        + '<div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:6px">执行步骤预览</div>'
        + '<div style="display:flex;gap:4px;align-items:center;flex-wrap:wrap">' + _stFlow + '</div>'
        + '<div style="font-size:10px;color:var(--text-dim);margin-top:6px">⏱ ≈' + (preset.estimated_minutes || '?') + ' min · ' + _escHtml(preset.estimated_output || '') + '</div>'
        + '</div>';
    }

    // B: 引流渠道预览栏
    var _referralBar = '<div id="fb-launch-referral-bar" style="margin-bottom:10px;padding:8px 12px;background:rgba(14,165,233,.04);border:1px solid rgba(14,165,233,.18);border-radius:8px">'
      + '<div style="display:flex;align-items:center;justify-content:space-between">'
      + '<span style="font-size:10px;font-weight:600;color:#0ea5e9">🔗 引流渠道</span>'
      + '<button onclick="document.getElementById(\'fb-launch-input-dialog\')&&document.getElementById(\'fb-launch-input-dialog\').remove();fbOpenReferralModal()" style="font-size:9px;padding:2px 8px;background:rgba(14,165,233,.1);border:1px solid rgba(14,165,233,.3);border-radius:4px;color:#0ea5e9;cursor:pointer">配置引流账号</button>'
      + '</div>'
      + '<div id="fb-launch-referral-status" style="font-size:10px;color:var(--text-dim);margin-top:4px">检测中…</div>'
      + '</div>';

    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:0;max-width:640px;width:96%;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.4)">
        <!-- 渐变头部 -->
        <div style="padding:18px 24px 14px;background:linear-gradient(180deg,${_pColor}12 0%,transparent 100%)">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:4px">
            <div style="display:flex;align-items:center;gap:10px">
              <div style="width:36px;height:36px;border-radius:10px;background:linear-gradient(135deg,${_pColor},${_pColor}bb);display:flex;align-items:center;justify-content:center;font-size:16px;color:#fff;box-shadow:0 3px 10px ${_pColor}40">${preset.name.charAt(0)}</div>
              <div>
                <div style="font-size:16px;font-weight:700;color:var(--text)">${_escHtml(preset.name)}</div>
                <div style="font-size:10px;color:var(--text-muted);margin-top:2px">
                  ${_escHtml(preset.desc)} · 客群 <code style="background:var(--bg-main);padding:1px 5px;border-radius:3px;font-size:10px">${_escHtml(persona.persona_key || extra.persona_key || '默认')}</code>
                  ${deviceId ? ' · 设备 <code style="background:var(--bg-main);padding:1px 5px;border-radius:3px;font-size:10px">' + _escHtml((deviceId+'').substring(0,8)) + '</code>' : ' · 全设备'}
                </div>
              </div>
            </div>
            <button onclick="document.getElementById('fb-launch-input-dialog').remove()"
              style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:16px;width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center;transition:all .12s" onmouseover="this.style.background='rgba(239,68,68,.15)';this.style.color='#ef4444'" onmouseout="this.style.background='none';this.style.color='var(--text-muted)'">✕</button>
          </div>
        </div>

        <!-- 表单内容 -->
        <div style="padding:8px 24px 20px">
          ${reopenHint}

          <div id="fb-launch-input-error"
               style="display:none;margin-bottom:10px;padding:8px 12px;background:rgba(239,68,68,.1);
                      border:1px solid rgba(239,68,68,.4);border-radius:8px;color:#fca5a5;font-size:12px"></div>

          ${fieldsHtml}

          ${_stepsPreview}

          ${_referralBar}

          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px;padding-top:14px;border-top:1px solid var(--border)">
            <button onclick="document.getElementById('fb-launch-input-dialog').remove()"
                    style="padding:8px 18px;background:none;border:1px solid var(--border);
                           color:var(--text-muted);border-radius:8px;cursor:pointer;font-size:12px;transition:all .12s"
                    onmouseover="this.style.borderColor='var(--text-dim)'" onmouseout="this.style.borderColor='var(--border)'">取消</button>
            <button id="fb-launch-input-submit"
                    style="padding:8px 22px;background:linear-gradient(135deg,${_escHtml(_pColor)},${_escHtml(_pColor)}cc);color:#fff;
                           border:none;border-radius:8px;font-weight:600;cursor:pointer;font-size:12px;
                           box-shadow:0 2px 8px ${_escHtml(_pColor)}40;transition:all .15s"
                    onmouseover="this.style.transform='translateY(-1px)';this.style.boxShadow='0 4px 14px ${_escHtml(_pColor)}55'"
                    onmouseout="this.style.transform='';this.style.boxShadow='0 2px 8px ${_escHtml(_pColor)}40'">
              ▶ 启动
            </button>
          </div>
        </div>
      </div>
    `;

    // ── 首次打开时自动预填 ai_assist 字段 ──
    // 如果用户从未填过（localStorage 无缓存、无 prefill），用 persona 匹配的
    // 推荐模板自动写入，省去手动点击「AI 建议」。字段旁显示浅色提示。
    Object.keys(schema).forEach(function (f) {
      if (!schema[f].ai_assist) return;
      var ta = overlay.querySelector('textarea[data-field="' + f + '"]');
      if (!ta || ta.value.trim()) return;  // 已有值（localStorage 或 prefill）
      var rec = _fbGetAiDefault(f, persona);
      if (rec) {
        ta.value = rec;
        // 更新字符计数
        var cnt = overlay.querySelector('span[data-charcount="' + f + '"]');
        if (cnt) cnt.textContent = rec.length;
        // 添加浅色提示，让用户知道这是自动推荐
        var hint = document.createElement('div');
        hint.style.cssText = 'font-size:9px;color:#a78bfa;margin-top:2px;opacity:.8';
        hint.textContent = '✨ 已自动填入推荐文案，可直接使用或点「AI 建议」选择其他';
        ta.parentNode.appendChild(hint);
      }
    });

    // B: 异步加载引流渠道状态
    (async function () {
      try {
        var _refStatus = document.getElementById('fb-launch-referral-status');
        if (!_refStatus) return;
        var rr = await api('GET', '/facebook/referral-config');
        var _refs = (rr && rr.referrals) || {};
        var _order = (rr && rr.priority_order && rr.priority_order.length)
          ? rr.priority_order : ['whatsapp', 'telegram', 'instagram', 'line'];
        var _chMeta = {line: {icon: '💚', zh: 'LINE'}, whatsapp: {icon: '💬', zh: 'WA'}, instagram: {icon: '📷', zh: 'IG'}, telegram: {icon: '✈️', zh: 'TG'}};
        var devCount = Object.keys(_refs).length;
        if (!devCount) {
          _refStatus.innerHTML = '<span style="color:#f59e0b">⚠️ 尚未配置任何引流账号 — 请先配置再启动引流类任务</span>';
          return;
        }
        var firstDev = _refs[Object.keys(_refs)[0]] || {};
        var _mainCh = _order[0] || '';
        var chHtml = _order.map(function (ch) {
          var m = _chMeta[ch] || {icon: '·', zh: ch};
          var val = firstDev[ch];
          var ok = val && val.trim();
          var isMain = ch === _mainCh;
          var badge = isMain ? '<span style="font-size:7px;padding:0 3px;background:rgba(14,165,233,.2);color:#0ea5e9;border-radius:2px;margin-left:2px;vertical-align:top">推荐</span>' : '';
          return '<span style="margin-right:6px;' + (ok ? 'color:#22c55e' : (isMain ? 'color:#ef4444;font-weight:600' : 'color:#ef4444')) + '">'
            + m.icon + ' ' + m.zh + badge + ': ' + (ok ? _escHtml(val.substring(0, 20)) : '未配') + '</span>';
        }).join('');
        var _personaTag = (rr.persona && rr.persona.short_label) ? '<span style="font-size:9px;color:var(--text-dim);margin-left:4px">' + (rr.persona.display_flag || '') + ' ' + rr.persona.short_label + '</span>' : '';
        _refStatus.innerHTML = chHtml + '<span style="color:var(--text-dim)">(' + devCount + '台设备)</span>' + _personaTag;
      } catch (e) {
        var _s = document.getElementById('fb-launch-referral-status');
        if (_s) _s.textContent = '加载失败';
      }
    })();

    document.getElementById('fb-launch-input-submit').onclick = async function () {
      const values = _collectFormValues(presetKey, schema);
      const missing = _localValidate(preset, values, persona);
      if (missing.length) {
        _markFieldErrors(missing);
        const errBox = document.getElementById('fb-launch-input-error');
        errBox.style.display = 'block';
        errBox.innerHTML = '请填写必填字段：' + missing.map(function (f) {
          return _escHtml((schema[f] || {}).label || f);
        }).join('、');
        return;
      }
      _markFieldErrors([]);
      // 合并 extra（persona/target_country/language）+ 表单值
      const payload = Object.assign({}, extra, values);
      // 422 错误捕获 → 字段级标红
      const res = await fbLaunchPreset(presetKey, deviceId, payload, { suppressConfirm: true });
      if (res && res.error422) {
        const errFields = (res.error422.missing || []).map(function (m) { return m.field; });
        _markFieldErrors(errFields);
        const errBox = document.getElementById('fb-launch-input-error');
        errBox.style.display = 'block';
        errBox.textContent = res.error422.message || '后端校验失败：缺少必填参数';
        return;
      }
      // 成功关闭
      const m = document.getElementById('fb-launch-input-dialog');
      if (m) m.remove();
      // 关闭外层 preset 选择模态
      const m2 = document.getElementById('fb-presets-modal');
      if (m2) m2.remove();
      // ── next_preset 引导 ──
      // 如果当前 preset 有 next_preset 元数据，显示引导 toast
      if (preset.next_preset && preset.next_hint) {
        var _nextKey = preset.next_preset;
        var _did = deviceId;
        setTimeout(function () {
          var nb = document.createElement('div');
          nb.style.cssText = 'position:fixed;bottom:24px;right:24px;z-index:9999;background:var(--bg-card);'
            + 'border:1px solid #60a5fa;border-radius:12px;padding:14px 18px;max-width:360px;'
            + 'box-shadow:0 8px 30px rgba(0,0,0,.4);animation:fadeIn .3s';
          nb.innerHTML = '<div style="font-size:12px;color:#60a5fa;font-weight:600;margin-bottom:6px">💡 下一步建议</div>'
            + '<div style="font-size:11px;color:var(--text-muted);margin-bottom:10px;line-height:1.4">' + _escHtml(preset.next_hint) + '</div>'
            + '<div style="display:flex;gap:8px;justify-content:flex-end">'
            + '<button onclick="this.parentNode.parentNode.remove()" style="padding:4px 12px;background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:6px;cursor:pointer;font-size:11px">稍后</button>'
            + '<button onclick="this.parentNode.parentNode.remove();fbLaunchPresetWithPersona(\'' + _nextKey + '\',' + (_did ? '\'' + _did + '\'' : 'null') + ')" style="padding:4px 14px;background:linear-gradient(135deg,#60a5fa,#3b82f6);color:#fff;border:none;border-radius:6px;cursor:pointer;font-size:11px;font-weight:600">立即前往 →</button>'
            + '</div>';
          document.body.appendChild(nb);
          setTimeout(function () { if (nb.parentNode) nb.remove(); }, 15000);
        }, 1200);
      }
    };
  };

  // P1 Sprint C: 从失败/0 结果任务一键重打 dialog 并回填参数
  // 入口：tasks-chat.js 的 outcome 徽章 onclick / 详情页"重新配置"按钮
  window.fbReopenLaunchByTask = async function (task) {
    if (!task || !task.params) {
      showToast('任务数据缺失，无法回填', 'warning');
      return;
    }
    const presetKey = task.params._preset_key || task.params.preset_key;
    if (!presetKey) {
      showToast('该任务非 preset 启动，无法重新配置（请走完整启动入口）', 'info');
      return;
    }
    await _fbLoadPresets();
    const preset = (_fbPresets || []).find(function (p) { return p.key === presetKey; });
    if (!preset || !preset.input_schema) {
      showToast('该 preset (' + presetKey + ') 无配置表单', 'info');
      return;
    }
    // 反向映射 task.params → 字段值
    const p = task.params || {};
    const prefill = {};
    Object.keys(preset.input_schema).forEach(function (field) {
      if (field === 'target_groups') {
        // launch 注入：campaign_run 拿 target_groups 列表；群成员打招呼任务拿 group_name 单值
        prefill[field] = p.target_groups || (p.group_name ? [p.group_name] : []);
      } else if (p[field] != null) {
        prefill[field] = p[field];
      }
    });
    fbOpenLaunchInputDialog(presetKey, task.device_id || null, {
      persona_key: p.persona_key || '',
      target_country: p.target_country || '',
      language: p.language || '',
      prefill: prefill,
      reopenFromTask: (task.id || task.task_id || '').substring(0, 12),
    });
  };

  // 向后兼容旧名字（设备侧边栏、其他入口可能还在调）
  window.fbLaunchPresetWithGeo = window.fbLaunchPresetWithPersona;

  // 顶部指挥栏的"目标客群"徽章点击：打开快速切换
  window.fbOpenPersonaPicker = async function () {
    await _fbLoadActivePersona(true);
    const overlay = _fbModalOverlay('fb-persona-picker');
    const list = (_fbAvailablePersonas || []).map(function (p) {
      const active = (_fbActivePersona && p.persona_key === _fbActivePersona.persona_key);
      const pk = (p.persona_key || '').replace(/'/g, '');
      const setBtn = (!active && pk)
        ? `<button type="button" onclick="fbSetActivePersona('${pk}')"
            style="margin-top:8px;padding:6px 10px;font-size:11px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);cursor:pointer">
            设为全局默认</button>`
        : '';
      return `
        <div style="background:var(--bg-main);border:1px solid ${active?'#ec4899':'var(--border)'};
                    border-radius:10px;padding:12px;margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:10px">
            <span style="font-size:22px">${p.display_flag || '🌐'}</span>
            <div style="flex:1">
              <div style="font-weight:600;font-size:14px">${p.display_label}</div>
              <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
                引流优先: ${(p.referral_priority||[]).map(function(c){return (_FB_CHANNEL_META[c]||{}).zh||c;}).join(' › ')}
              </div>
            </div>
            ${active ? '<span style="color:#ec4899;font-weight:600;font-size:12px">✓ 当前</span>' : ''}
          </div>
          ${(p.interest_topics||[]).length ? `
            <div style="font-size:10px;color:var(--text-dim);margin-top:8px;padding-top:8px;border-top:1px solid var(--border)">
              兴趣: ${p.interest_topics.slice(0,8).join(' · ')}
            </div>` : ''}
          ${setBtn}
        </div>`;
    }).join('');
    const ovHint = _fbPersonaOverrideKey
      ? `<div style="font-size:10px;color:#f59e0b;margin-bottom:8px">运行时覆盖: <code>${_fbPersonaOverrideKey}</code> · YAML 默认: <code>${_fbPersonaYamlDefault || '—'}</code></div>`
      : `<div style="font-size:10px;color:var(--text-dim);margin-bottom:8px">YAML 默认: <code>${_fbPersonaYamlDefault || '—'}</code>（未设运行时覆盖）</div>`;
    const clrBtn = _fbPersonaOverrideKey
      ? `<button type="button" onclick="fbClearPersonaOverride()"
          style="padding:8px 12px;font-size:12px;border-radius:8px;border:1px solid var(--border);background:transparent;cursor:pointer;margin-right:8px">
          清除覆盖（恢复 YAML default）</button>`
      : '';
    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:560px;width:96%;max-height:86vh;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div>
            <div style="font-size:17px;font-weight:700">🎯 目标客群</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">
              点「设为全局默认」写入本机 <code>data/fb_active_persona_override.json</code>，不改 YAML；改 YAML 的 <code>default_persona</code> 为永久切换。
            </div>
          </div>
          <button onclick="document.getElementById('fb-persona-picker').remove()"
            style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
        </div>
        ${ovHint}
        <div style="margin-bottom:10px">${clrBtn}</div>
        ${list || '<div style="color:#f87171">未加载到客群</div>'}
      </div>
    `;
  };

  window.fbSetActivePersona = async function (personaKey) {
    try {
      await api('POST', '/facebook/active-persona', { persona_key: personaKey });
      if (typeof showToast === 'function') showToast('已切换全局默认客群', 'success');
      _fbPresets = null;
      await _fbLoadPresets(true);
      await _fbLoadActivePersona(true);
      await _fbLoadReferrals();
      _fbRenderCommandBar();
      const m = document.getElementById('fb-persona-picker');
      if (m) m.remove();
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message || String(e), 'error');
    }
  };

  window.fbClearPersonaOverride = async function () {
    try {
      await api('POST', '/facebook/active-persona', { clear: true });
      if (typeof showToast === 'function') showToast('已清除运行时覆盖', 'success');
      _fbPresets = null;
      await _fbLoadPresets(true);
      await _fbLoadActivePersona(true);
      await _fbLoadReferrals();
      _fbRenderCommandBar();
      const m = document.getElementById('fb-persona-picker');
      if (m) m.remove();
    } catch (e) {
      if (typeof showToast === 'function') showToast(e.message || String(e), 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // Facebook launch 响应摘要（供预设启动与其它调用方复用）
  // ════════════════════════════════════════════════════════
  function _fbFormatLaunchStepErrors(tasks) {
    var bad = (tasks || []).filter(function (t) { return t && !t.ok; });
    if (!bad.length) return '';
    var parts = bad.map(function (t) {
      var msg = (t.error != null && String(t.error)) ? String(t.error) : '';
      if (t.detail && typeof t.detail === 'object') {
        var de = (t.detail.error || t.detail.message || '').trim();
        if (de) msg = de + (msg && msg !== de ? (' (' + msg + ')') : '');
      }
      return (t.type || '?') + (t.http_status ? ' [HTTP ' + t.http_status + ']' : '') + ': ' + (msg || '失败');
    });
    return parts.join(' · ').slice(0, 420);
  }

  /**
   * @param {object|null} data - POST /facebook/device/.../launch 的 JSON
   * @param {string} deviceId
   * @returns {{ fullDeviceOk: boolean, taskCount: number, stepCount: number, errorLine: string }}
   */
  window.fbSummarizeLaunchResponse = function (data, deviceId) {
    var tasks = (data && data.flow_tasks) || [];
    var nOk = (data && data.task_count) || 0;
    var pref = ((deviceId || '').substring(0, 8) || '?') + '… ';
    var errBlob = _fbFormatLaunchStepErrors(tasks);
    return {
      fullDeviceOk: nOk === tasks.length && tasks.length > 0,
      taskCount: nOk,
      stepCount: tasks.length,
      errorLine: errBlob ? (pref + errBlob) : ''
    };
  };

  // ════════════════════════════════════════════════════════
  // 启动单个预设
  // ════════════════════════════════════════════════════════
  window.fbLaunchPreset = async function (presetKey, deviceId, extra, opts) {
    extra = extra || {};
    opts = opts || {};
    let devices = [];
    if (deviceId) {
      devices = [deviceId];
    } else {
      try {
        const r = await api('GET', '/platforms/facebook/device-grid');
        devices = ((r && r.devices) || []).filter(function (d) { return d.online; }).map(function (d) { return d.device_id; });
      } catch (e) {
        showToast('无法获取设备列表: ' + e.message, 'error');
        return { ok: false };
      }
    }

    if (!devices.length) {
      showToast('没有在线设备可启动', 'warning');
      return { ok: false };
    }

    // suppressConfirm: 由新版 dialog 调用时跳过原生 confirm（用户已经在表单里点了"启动"）
    if (!opts.suppressConfirm) {
      const geoTxt = extra.target_country ? ('  GEO=' + extra.target_country) : '';
      const grpTxt = (extra.target_groups || []).length ? ('  目标群=' + extra.target_groups.length + '个') : '';
      const nameTxt = (extra.add_friend_targets || []).length
        ? ('  名字=' + extra.add_friend_targets.length + '个') : '';
      if (!(await ocDialog({title:'启动预设',message:'将在 <b>' + devices.length + '</b> 台设备上启动「' + presetKey + '」' + geoTxt + grpTxt + nameTxt,type:'info',confirmText:'启动',cancelText:'取消'}))) return { ok: false, cancelled: true };
    }

    let okCount = 0;
    let workerCapWarnShown = false;
    let error422 = null;     // 任一设备拿到 422 → 缓存 detail 给调用方做字段级标红
    for (const did of devices) {
      try {
        const body = { preset_key: presetKey };
        if (extra.persona_key) body.persona_key = extra.persona_key;
        if (extra.target_country) body.target_country = extra.target_country;
        if (extra.language) body.language = extra.language;
        if (extra.target_groups && extra.target_groups.length) body.target_groups = extra.target_groups;
        if (extra.add_friend_targets && extra.add_friend_targets.length) {
          body.add_friend_targets = extra.add_friend_targets;
        }
        if (extra.greeting) body.greeting = extra.greeting;
        if (extra.verification_note) body.verification_note = extra.verification_note;
        // P1 Sprint A 新增：透传 schema 表单收集的数值字段
        if (extra.max_friends_per_run != null) body.max_friends_per_run = extra.max_friends_per_run;
        if (extra.outreach_goal != null) body.outreach_goal = extra.outreach_goal;
        if (extra.max_members != null) body.max_members = extra.max_members;
        const data = await api('POST', '/facebook/device/' + did + '/launch', body);
        if (data && data.worker_capabilities_warning && typeof showToast === 'function' && !workerCapWarnShown) {
          workerCapWarnShown = true;
          showToast(String(data.worker_capabilities_warning).slice(0, 480), 'warning');
        }
        const sum = window.fbSummarizeLaunchResponse(data, did);
        if (sum.errorLine && typeof showToast === 'function') {
          showToast(sum.errorLine, 'error');
        }
        if (sum.fullDeviceOk) okCount += 1;
        else if (sum.taskCount > 0 && sum.stepCount && typeof showToast === 'function') {
          showToast((did || '').substring(0, 8) + '… 仅 ' + sum.taskCount + '/' + sum.stepCount + ' 步入队', 'warning');
        }
      } catch (e) {
        // 捕获 HTTPException(422) — api() 通常把响应体放到 e.detail 或 e.body
        const status = e && (e.status || e.statusCode || (e.response && e.response.status));
        const detail = e && (e.detail || (e.body && e.body.detail) || (e.response && e.response.data && e.response.data.detail));
        if (status === 422 && detail && detail.code === 'missing_required_inputs') {
          error422 = detail;
          // 不 toast，由 dialog 字段级标红展示
          continue;
        }
        console.warn('launch failed for ' + did, e);
        if (typeof showToast === 'function') showToast((did || '').substring(0, 8) + '… ' + (e.message || e), 'error');
      }
    }

    if (error422) {
      // 有 schema 表单上下文 → 让 caller 字段标红, 不弹 toast (suppressConfirm=true)
      // 无表单上下文 → 用户已经点过原生 confirm, 此时静默吞会让人误以为"点了无响应".
      // 加明确 toast 提示缺哪些字段, 用户能直接定位问题.
      if (!opts.suppressConfirm && typeof showToast === 'function') {
        const missing = (error422.missing || [])
          .map(function (m) { return m.label || m.field; })
          .filter(Boolean);
        const msg = missing.length
          ? ('启动失败: 缺少必填字段 ' + missing.join('、') +
             ' (preset=' + presetKey + '). 请改用「☰ 启动」表单输入')
          : ('启动失败: 422 missing_required_inputs (preset=' + presetKey + ')');
        showToast(msg, 'error');
      }
      return { ok: false, error422: error422 };
    }

    showToast('已下发到 ' + okCount + '/' + devices.length + ' 台设备', okCount === devices.length ? 'success' : 'warning');
    if (!opts.suppressConfirm) {
      const m = document.getElementById('fb-presets-modal');
      if (m) m.remove();
    }
    return { ok: okCount > 0, ok_count: okCount, total: devices.length };
  };

  // ════════════════════════════════════════════════════════
  // 引流账号配置(默认 WA 优先排序)
  // ════════════════════════════════════════════════════════
  window.fbOpenReferralModal = async function () {
    await _fbLoadActivePersona();
    await _fbLoadReferrals();
    const refs = _fbReferrals || {};
    // priority_order 从 /referral-config 返回（按当前 persona 排序）
    const order = (_fbReferralPriority && _fbReferralPriority.length)
      ? _fbReferralPriority
      : ['whatsapp', 'telegram', 'instagram', 'line'];
    const persona = _fbActivePersona || {};

    const overlay = _fbModalOverlay('fb-referral-modal');

    // ── 表头：按 priority_order 动态生成列（主引流渠道放第一列并高亮）
    const tableHeaders = order.map(function (ch, i) {
      const meta = _FB_CHANNEL_META[ch] || { icon: '·', zh: ch };
      const main = (i === 0);
      return `<th style="text-align:left;padding:8px;font-weight:600;${main?'color:#ec4899':'color:var(--text-muted)'}">
        ${meta.icon} ${meta.zh}${main?' <span style="font-size:9px;padding:1px 4px;background:rgba(236,72,153,.2);border-radius:3px">主</span>':''}
      </th>`;
    }).join('');

    const tableRows = Object.keys(refs).length
      ? Object.entries(refs).map(function (entry) {
          const did = entry[0];
          const r = entry[1] || {};
          const cells = order.map(function (ch) {
            return `<td style="padding:6px 8px;font-size:12px">${r[ch] || '<span style="color:var(--text-dim)">—</span>'}</td>`;
          }).join('');
          return `<tr>
            <td style="padding:6px 8px;font-family:monospace;font-size:11px">${did.substring(0, 12)}…</td>
            ${cells}
          </tr>`;
        }).join('')
      : `<tr><td colspan="${order.length + 1}" style="text-align:center;padding:14px;color:var(--text-muted);font-size:12px">尚未配置任何引流账号</td></tr>`;

    // ── 批量配置输入框：按 priority_order 渲染，主渠道 placeholder 带客群提示
    const inputBlocks = order.map(function (ch, i) {
      const meta = _FB_CHANNEL_META[ch] || { icon: '·', zh: ch, placeholder: '', note: '' };
      const main = (i === 0);
      const hint = main ? `(主${persona.country_zh ? ' · ' + persona.country_zh + '客群' : ''})` : `(备选·${meta.note || ''})`;
      return `
        <div>
          <label style="font-size:11px;color:var(--text-muted)">${meta.icon} ${meta.zh} ${hint}</label>
          <input type="text" id="fb-ref-${ch}" placeholder="${meta.placeholder}"
            style="width:100%;padding:6px;background:var(--bg-main);border:1px solid ${main?'rgba(236,72,153,.4)':'var(--border)'};border-radius:6px;color:var(--text);font-size:12px;margin-top:2px">
        </div>`;
    }).join('');

    // ── 客群感知的提示语
    const mainCh = order[0] || 'whatsapp';
    const mainMeta = _FB_CHANNEL_META[mainCh] || { zh: mainCh };
    const personaHint = persona.short_label
      ? `当前目标客群 <b>${persona.display_flag || ''} ${persona.short_label}</b>：${mainMeta.zh} 渗透率最高，主引流优先填 ${mainMeta.zh}。`
      : `未配置目标客群 → 使用全球默认优先级（WA > TG > IG > LINE）。`;

    overlay.innerHTML = `
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;max-width:760px;width:96%;max-height:88vh;overflow-y:auto">
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
          <div style="font-size:18px;font-weight:700">🔗 Facebook 引流账号</div>
          <button onclick="document.getElementById('fb-referral-modal').remove()" style="background:none;border:none;color:var(--text-muted);font-size:22px;cursor:pointer">✕</button>
        </div>

        <div style="background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.3);border-radius:8px;padding:8px 12px;font-size:11px;color:#4ade80;margin-bottom:14px">
          💡 ${personaHint}
        </div>

        <!-- BB2b: 未配置设备告警 + 一键克隆 -->
        <div id="fb-ref-autofill-bar" style="display:none;margin-bottom:14px;padding:10px 14px;background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.25);border-radius:8px">
          <div style="display:flex;align-items:center;justify-content:space-between">
            <span id="fb-ref-autofill-msg" style="font-size:11px;color:#fbbf24">检测中…</span>
            <button id="fb-ref-autofill-btn" onclick="fbAutoFillReferrals()" style="display:none;font-size:10px;padding:4px 12px;background:linear-gradient(135deg,#f59e0b,#d97706);color:#fff;border:none;border-radius:5px;cursor:pointer;font-weight:600;white-space:nowrap">⚡ 一键填充</button>
          </div>
        </div>

        <!-- BB2b: 主渠道缺配告警 -->
        <div id="fb-ref-main-alert" style="display:none;margin-bottom:14px;padding:8px 12px;background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.25);border-radius:8px;font-size:11px;color:#fca5a5"></div>

        <div style="margin-bottom:14px">
          <div style="font-size:13px;font-weight:600;margin-bottom:8px">批量配置 (适用全部 FB 设备)</div>
          <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:8px">
            ${inputBlocks}
          </div>
          <button onclick="fbSaveReferralBatch()" style="margin-top:10px;background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff;border:none;padding:6px 14px;font-size:12px;font-weight:600;border-radius:6px;cursor:pointer">💾 应用到全部设备</button>
          <span style="margin-left:10px;font-size:10px;color:var(--text-dim)">只填有值的框；空框不会清除已有配置</span>
        </div>

        <div style="font-size:13px;font-weight:600;margin-bottom:6px">当前配置</div>
        <div style="overflow-x:auto">
        <table style="width:100%;border-collapse:collapse;font-size:11px;background:var(--bg-main);border-radius:6px;overflow:hidden;min-width:520px">
          <thead>
            <tr style="background:rgba(255,255,255,.03)">
              <th style="text-align:left;padding:8px;font-weight:600">设备</th>
              ${tableHeaders}
            </tr>
          </thead>
          <tbody>${tableRows}</tbody>
        </table>
        </div>
      </div>
    `;

    // BB2b: 异步检测未配置设备 + 主渠道缺配
    (function () {
      var refObj = refs;
      var devCount = 0;
      var configuredCount = 0;
      var unconfiguredCount = 0;
      try {
        // 统计
        var allDevIds = Object.keys(refObj);
        configuredCount = allDevIds.filter(function (d) {
          var r = refObj[d] || {};
          return Object.values(r).some(function (v) { return v && String(v).trim(); });
        }).length;
      } catch (e) {}

      // 主渠道缺配检测
      var mainAlert = document.getElementById('fb-ref-main-alert');
      if (mainAlert && configuredCount > 0) {
        var mainMissing = 0;
        Object.keys(refObj).forEach(function (d) {
          var r = refObj[d] || {};
          var mainVal = r[mainCh];
          if (!mainVal || !String(mainVal).trim()) mainMissing++;
        });
        if (mainMissing > 0) {
          var _chMeta = _FB_CHANNEL_META[mainCh] || { icon: '', zh: mainCh };
          mainAlert.style.display = 'block';
          mainAlert.innerHTML = '⚠️ 有 <b>' + mainMissing + '</b> 台设备缺少主引流渠道 <b>' + _chMeta.icon + ' ' + _chMeta.zh + '</b> 的配置。'
            + (persona.short_label ? ' 当前客群 <b>' + (persona.display_flag || '') + ' ' + persona.short_label + '</b> 最推荐使用 ' + _chMeta.zh + '。' : '')
            + ' 请在下方批量配置中填入 ' + _chMeta.zh + ' 账号。';
        }
      }

      // 异步获取真实设备数,检测未注册设备
      (async function () {
        try {
          var grid = await api('GET', '/facebook/device-grid');
          var allDevs = (grid && grid.devices) || [];
          devCount = allDevs.length;
          var regstered = Object.keys(refObj);
          unconfiguredCount = allDevs.filter(function (d) {
            return !regstered.includes(d.device_id) || !Object.values(refObj[d.device_id] || {}).some(function (v) { return v && String(v).trim(); });
          }).length;

          var bar = document.getElementById('fb-ref-autofill-bar');
          var msg = document.getElementById('fb-ref-autofill-msg');
          var btn = document.getElementById('fb-ref-autofill-btn');
          if (!bar || !msg) return;

          if (unconfiguredCount > 0 && configuredCount > 0) {
            bar.style.display = 'block';
            msg.innerHTML = '🔍 检测到 <b>' + unconfiguredCount + '/' + devCount + '</b> 台设备尚未配置引流账号';
            if (btn) btn.style.display = 'inline-block';
          } else if (unconfiguredCount > 0 && configuredCount === 0) {
            bar.style.display = 'block';
            bar.style.borderColor = 'rgba(239,68,68,.3)';
            msg.style.color = '#fca5a5';
            msg.innerHTML = '❌ 全部 <b>' + devCount + '</b> 台设备均未配置引流账号 — 请先在下方批量配置';
          } else if (devCount > 0) {
            bar.style.display = 'block';
            bar.style.background = 'rgba(34,197,94,.06)';
            bar.style.borderColor = 'rgba(34,197,94,.25)';
            msg.style.color = '#4ade80';
            msg.innerHTML = '✅ 全部 <b>' + devCount + '</b> 台设备已配置引流账号';
          }
        } catch (e) {}
      })();
    })();
  };

  // BB2b: 一键克隆引流配置
  window.fbAutoFillReferrals = async function () {
    try {
      var r = await api('POST', '/facebook/referral-config/auto-fill');
      if (!r.ok) {
        showToast(r.message || '自动填充失败', 'warning');
        return;
      }
      if (r.filled === 0) {
        showToast('所有设备已有配置，无需填充', 'info');
        return;
      }
      showToast('已从模板设备克隆到 ' + r.filled + ' 台设备', 'success');
      if (!r.main_channel_configured) {
        var _chNames = {line: 'LINE', whatsapp: 'WhatsApp', instagram: 'Instagram', telegram: 'Telegram'};
        showToast('注意: 主渠道 ' + (_chNames[r.main_channel] || r.main_channel) + ' 未在模板中配置', 'warn');
      }
      _fbReferrals = null;
      var m = document.getElementById('fb-referral-modal');
      if (m) m.remove();
      setTimeout(function () { fbOpenReferralModal(); }, 200);
    } catch (e) {
      showToast('自动填充失败: ' + (e.message || e), 'error');
    }
  };

  window.fbSaveReferralBatch = async function () {
    // 遍历 priority_order 的所有渠道输入框，非空就加进 body
    const order = (_fbReferralPriority && _fbReferralPriority.length)
      ? _fbReferralPriority
      : ['whatsapp', 'telegram', 'instagram', 'line'];
    const body = { all: true };
    let anyValue = false;
    order.forEach(function (ch) {
      const el = document.getElementById('fb-ref-' + ch);
      if (!el) return;
      const v = (el.value || '').trim();
      if (v) {
        body[ch] = v;
        anyValue = true;
      }
    });
    if (!anyValue) {
      showToast('请至少填写一个账号', 'warning');
      return;
    }
    try {
      const r = await api('POST', '/facebook/referral-config', body);
      showToast('已应用到 ' + (r.updated || 0) + ' 台设备', 'success');
      _fbReferrals = null;
      const m = document.getElementById('fb-referral-modal');
      if (m) m.remove();
      setTimeout(function () { fbOpenReferralModal(); }, 200);
    } catch (e) {
      showToast('保存失败: ' + e.message, 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // 「批量发请求」入口 —— P2-UI Sprint 已在顶部栏删除该按钮,
  // 但保留别名供旧代码 / 设备卡片侧栏 / 高分线索模态回调 (fbBatchRequestSingle)。
  // 底层就是 fbOpenPresetsModal，避免多路径维护。
  // ════════════════════════════════════════════════════════
  window.fbBatchRequest = function () {
    fbOpenPresetsModal(null);
  };

  // ════════════════════════════════════════════════════════
  // Modal 工具
  // ════════════════════════════════════════════════════════
  function _fbModalOverlay(id) {
    let overlay = document.getElementById(id);
    if (overlay) overlay.remove();
    overlay = document.createElement('div');
    overlay.id = id;
    overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.5);backdrop-filter:blur(6px);-webkit-backdrop-filter:blur(6px);z-index:9999;display:flex;align-items:center;justify-content:center;padding:20px';
    overlay.onclick = function (ev) { if (ev.target === overlay) overlay.remove(); };
    document.body.appendChild(overlay);
    return overlay;
  }

  // 暴露给设备卡片侧栏使用 — 用户在卡片上点"配置流程",带上设备 ID 进入预设模态
  window.fbDeviceConfigFlow = function (deviceId) {
    fbOpenPresetsModal(deviceId);
  };

  // ════════════════════════════════════════════════════════
  // Sprint 2 新增 — 用 PlatShell 公共组件渲染
  // ════════════════════════════════════════════════════════

  window.fbOpenFunnelModal = async function () {
    const Shell = window.PlatShell;
    if (!Shell) { showToast('PlatShell 未加载', 'error'); return; }
    Shell.modal.open('fb-funnel-modal',
      '<div id="fb-funnel-body">加载中…</div>', { maxWidth: '860px' });
    try {
      const r = await Shell.api.get('/facebook/funnel?since_hours=168');
      const steps = r.steps || [];
      // P3-4: greeting 维度专属数据(取自 /facebook/funnel 响应根字段)
      const greetSent = r.stage_greetings_sent || 0;
      const greetFallback = r.stage_greetings_fallback || 0;
      const frSent = r.stage_friend_request_sent || 0;
      const rateGreetAfterAdd = r.rate_greet_after_add || 0;
      const templateDist = r.greeting_template_distribution || [];

      // 模板分布 top 5 的水平柱状
      const maxCnt = Math.max(1, ...templateDist.map(function (kv) { return kv[1] || 0; }));
      const tplBars = templateDist.length
        ? templateDist.map(function (kv) {
            const tid = kv[0] || '-';
            const cnt = kv[1] || 0;
            const w = Math.round(cnt * 100 / maxCnt);
            return ''
              + '<div style="display:flex;align-items:center;gap:8px;font-size:11px;margin-bottom:4px">'
              +   '<code style="min-width:110px;color:var(--text-muted)">' + tid + '</code>'
              +   '<div style="flex:1;background:rgba(96,165,250,.12);border-radius:4px;overflow:hidden;height:14px">'
              +     '<div style="width:' + w + '%;height:100%;background:linear-gradient(90deg,#60a5fa,#22d3ee)"></div>'
              +   '</div>'
              +   '<span style="min-width:32px;text-align:right;font-weight:600">' + cnt + '</span>'
              + '</div>';
          }).join('')
        : '<div style="color:var(--text-dim);font-size:11px">暂无 greeting 模板命中数据</div>';

      const fallbackPct = greetSent > 0 ? Math.round(greetFallback * 100 / greetSent) : 0;
      const fbPctColor = fallbackPct > 30 ? '#ef4444'
                       : fallbackPct > 10 ? '#f59e0b' : '#22c55e';

      const html = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<h3 style="margin:0">📊 Facebook 引流漏斗 (近 7 天)</h3>'
        + '<button onclick="PlatShell.modal.close(\'fb-funnel-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">'
        //  ── 左列: 传统漏斗 (已有)
        + '<div>'
        +   '<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">🔻 主链路</div>'
        +   '<div style="display:grid;gap:8px">'
        +     steps.map(function (s) {
            const rate = s.rate != null ? ' (' + (s.rate * 100).toFixed(0) + '%)' : '';
            return '<div style="display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg-main);border-radius:6px;border-left:3px solid #1877f2">'
              + '<span style="font-size:12px">' + s.label + '</span>'
              + '<span style="font-weight:700;color:#60a5fa;font-size:13px">' + s.value + rate + '</span>'
              + '</div>';
          }).join('')
        +   '</div>'
        + '</div>'
        //  ── 右列: greeting 专项 (新增)
        + '<div>'
        +   '<div style="font-size:12px;color:var(--text-muted);margin-bottom:8px">💬 打招呼 (P3)</div>'
        +   '<div style="display:grid;gap:8px;margin-bottom:12px">'
        +     '<div style="display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg-main);border-radius:6px;border-left:3px solid #0ea5e9">'
        +       '<span style="font-size:12px">Greeting 总数</span>'
        +       '<span style="font-weight:700;color:#0ea5e9;font-size:13px">' + greetSent + '</span>'
        +     '</div>'
        +     '<div style="display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg-main);border-radius:6px;border-left:3px solid ' + fbPctColor + '">'
        +       '<span style="font-size:12px">Fallback 路径</span>'
        +       '<span style="font-weight:700;color:' + fbPctColor + ';font-size:13px">' + greetFallback + ' (' + fallbackPct + '%)</span>'
        +     '</div>'
        +     '<div style="display:flex;justify-content:space-between;padding:8px 12px;background:var(--bg-main);border-radius:6px;border-left:3px solid #a855f7">'
        +       '<span style="font-size:12px">加友后打招呼率</span>'
        +       '<span style="font-weight:700;color:#a855f7;font-size:13px">' + (rateGreetAfterAdd * 100).toFixed(1) + '% (' + greetSent + '/' + frSent + ')</span>'
        +     '</div>'
        +   '</div>'
        +   '<div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">模板命中 Top 5 (A/B 样本)</div>'
        +   '<div style="background:var(--bg-main);border-radius:6px;padding:10px">'
        +     tplBars
        +   '</div>'
        +   '<div style="margin-top:6px;font-size:10px;color:var(--text-dim)">'
        +     '回复率 A/B 需机器 B 的 Messenger 自动回复就位后查看 <code>/facebook/greeting-reply-rate</code>'
        +   '</div>'
        + '</div>'
        + '</div>'
        + '<div style="margin-top:14px;font-size:11px;color:var(--text-muted)">'
        + '设备范围: ' + (r._scope_device || 'all') + ' | 起始: ' + (r._scope_since || '?')
        + '</div>';
      document.getElementById('fb-funnel-body').innerHTML = html;
    } catch (e) {
      document.getElementById('fb-funnel-body').innerHTML =
        '<div style="color:#ef4444">加载失败: ' + e.message + '</div>';
    }
  };

  window.fbOpenRiskModal = async function () {
    const Shell = window.PlatShell;
    if (!Shell) { showToast('PlatShell 未加载', 'error'); return; }
    Shell.modal.open('fb-risk-modal',
      '<div id="fb-risk-body">加载中…</div>', { maxWidth: '760px' });
    try {
      const r = await Shell.api.get('/facebook/risk/status');
      const devs = r.devices || [];
      const cfg = r.config || {};
      const tableRows = devs.map(function (d) {
        const last = d.last_event || {};
        return '<tr>'
          + '<td style="padding:8px;font-family:monospace;font-size:11px">' + (d.device_id || '').substr(0, 12) + '…</td>'
          + '<td style="padding:8px">' + d.risk_count + '</td>'
          + '<td style="padding:8px;color:' + (d.cooldown_remaining > 0 ? '#ef4444' : '#22c55e') + '">'
          + (d.cooldown_remaining > 0 ? d.cooldown_remaining + 's' : '✓ 正常') + '</td>'
          + '<td style="padding:8px;font-size:11px;color:var(--text-muted)">'
          + (last.message || '—').substr(0, 40) + '</td>'
          + '<td style="padding:8px"><button onclick="fbClearRisk(\'' + d.device_id + '\')" style="background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.4);padding:3px 8px;border-radius:4px;cursor:pointer;font-size:11px">清除</button></td>'
          + '</tr>';
      }).join('');
      document.getElementById('fb-risk-body').innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<h3 style="margin:0">🛡️ Facebook 风控自愈状态</h3>'
        + '<button onclick="PlatShell.modal.close(\'fb-risk-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + '<div style="background:var(--bg-main);border-radius:8px;padding:10px 14px;margin-bottom:14px;font-size:12px">'
        + '策略: <b>' + (cfg.strategy || '?') + '</b>(B=降级 warmup) | cooldown: ' + (cfg.cooldown_seconds || 0) + 's | 启用: ' + (cfg.enabled !== false ? '✓' : '✗')
        + '</div>'
        + '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        + '<thead><tr style="background:rgba(255,255,255,.03)">'
        + '<th style="text-align:left;padding:8px">设备</th>'
        + '<th style="text-align:left;padding:8px">风控次数</th>'
        + '<th style="text-align:left;padding:8px">Cooldown</th>'
        + '<th style="text-align:left;padding:8px">最近消息</th>'
        + '<th style="text-align:left;padding:8px">操作</th>'
        + '</tr></thead><tbody>'
        + (tableRows || '<tr><td colspan="5" style="padding:14px;text-align:center;color:var(--text-muted)">暂无风控记录</td></tr>')
        + '</tbody></table>';
    } catch (e) {
      document.getElementById('fb-risk-body').innerHTML =
        '<div style="color:#ef4444">加载失败: ' + e.message + '</div>';
    }
  };

  window.fbClearRisk = async function (deviceId) {
    try {
      await window.PlatShell.api.post('/facebook/risk/clear/' + deviceId, {});
      showToast('已清除 ' + deviceId.substr(0, 8) + '… 的风控状态', 'success');
      fbOpenRiskModal();
    } catch (e) {
      showToast('清除失败: ' + e.message, 'error');
    }
  };

  window.fbOpenDailyBriefModal = async function () {
    const Shell = window.PlatShell;
    if (!Shell) { showToast('PlatShell 未加载', 'error'); return; }
    Shell.modal.open('fb-brief-modal',
      '<div id="fb-brief-body">加载中…</div>', { maxWidth: '780px' });
    const renderBody = function (md, meta) {
      // 简易 markdown → HTML(只支持 # ## - 列表、加粗、emoji)
      const html = md
        .replace(/^### (.*)$/gm, '<h4>$1</h4>')
        .replace(/^## (.*)$/gm, '<h3 style="margin-top:14px">$1</h3>')
        .replace(/^# (.*)$/gm, '<h2 style="margin-top:0">$1</h2>')
        .replace(/\*\*([^*]+)\*\*/g, '<b>$1</b>')
        .replace(/^- (.*)$/gm, '<li>$1</li>')
        .replace(/(<li>[\s\S]*?<\/li>)+/g, '<ul style="margin:6px 0;padding-left:20px">$&</ul>')
        .replace(/\n\n/g, '<br><br>');
      const m = meta || {};
      return ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<h3 style="margin:0">📰 Facebook AI 日报</h3>'
        + '<div style="display:flex;gap:8px">'
        + '<button onclick="fbRegenerateBrief()" style="background:linear-gradient(135deg,#a855f7,#7c3aed);color:#fff;border:none;padding:5px 12px;border-radius:6px;font-size:11px;cursor:pointer">🔄 重新生成</button>'
        + '<button onclick="PlatShell.modal.close(\'fb-brief-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div></div>'
        + '<div style="background:var(--bg-main);padding:14px;border-radius:8px;line-height:1.6;font-size:13px">'
        + html + '</div>'
        + '<div style="margin-top:10px;font-size:10px;color:var(--text-muted)">'
        + '生成时间: ' + (m.generated_at || '?') + ' | 窗口: ' + (m.window_hours || 24) + 'h | LLM: '
        + (m.llm_generated ? '✓' : '⚠ fallback 模板')
        + '</div>';
    };
    try {
      const r = await Shell.api.get('/facebook/daily-brief/latest?limit=1');
      const briefs = r.briefs || [];
      if (briefs.length === 0) {
        document.getElementById('fb-brief-body').innerHTML = ''
          + '<div style="text-align:center;padding:30px">'
          + '<div style="font-size:14px;margin-bottom:14px">尚无日报</div>'
          + '<button onclick="fbRegenerateBrief()" style="background:linear-gradient(135deg,#a855f7,#7c3aed);color:#fff;border:none;padding:8px 18px;border-radius:8px;cursor:pointer">🔄 立即生成第 1 份</button>'
          + '</div>';
        return;
      }
      const b = briefs[0];
      document.getElementById('fb-brief-body').innerHTML = renderBody(b.markdown || '', b);
    } catch (e) {
      document.getElementById('fb-brief-body').innerHTML =
        '<div style="color:#ef4444">加载失败: ' + e.message + '</div>';
    }
  };

  window.fbRegenerateBrief = async function () {
    const body = document.getElementById('fb-brief-body');
    if (body) body.innerHTML = '<div style="text-align:center;padding:20px">⏳ 正在调用 AI 生成…(可能需要 5~15 秒)</div>';
    try {
      await window.PlatShell.api.post('/facebook/daily-brief/generate?hours=24', {});
      showToast('日报已生成', 'success');
      fbOpenDailyBriefModal();
    } catch (e) {
      showToast('生成失败: ' + e.message, 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // Sprint 3 P1: 高分线索模态(用 PlatShell.leadList 公共组件)
  // ════════════════════════════════════════════════════════
  window.fbOpenLeadsModal = async function () {
    const html = `
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">
        <div>
          <div style="font-size:18px;font-weight:700">🎯 Facebook 高分线索</div>
          <div style="font-size:11px;color:var(--text-dim);margin-top:2px">
            score≥60 的待加好友候选 · 点 "加好友" 立刻排队
          </div>
        </div>
        <div>
          <label style="font-size:11px;margin-right:8px">最低分:</label>
          <select id="fb-leads-minscore" onchange="fbReloadLeads()"
            style="padding:4px 8px;background:var(--bg-elev);color:var(--text);border:1px solid var(--border);border-radius:4px;font-size:11px">
            <option value="60">60(B+)</option>
            <option value="45">45(B)</option>
            <option value="80">80(S)</option>
          </select>
          <button onclick="fbReloadLeads()"
            style="margin-left:8px;padding:4px 10px;background:var(--accent);color:#fff;border:none;border-radius:4px;font-size:11px;cursor:pointer">
            🔄 刷新
          </button>
        </div>
      </div>
      <div id="fb-leads-body" style="font-size:12px">⏳ 加载中…</div>
    `;
    window.PlatShell.modal.open('fb-leads-modal', html, {
      title: '高分线索', maxWidth: '880px',
    });
    fbReloadLeads();
  };

  window.fbReloadLeads = async function () {
    const minScore = parseInt(document.getElementById('fb-leads-minscore').value) || 60;
    const body = document.getElementById('fb-leads-body');
    if (!body) return;
    body.innerHTML = '⏳ 加载中…';
    try {
      const data = await window.PlatShell.api.get(
        '/facebook/qualified-leads?limit=100&min_score=' + minScore);
      const leads = (data && data.leads) || [];
      body.innerHTML = window.PlatShell.leadList.render({
        leads: leads,
        actions: [
          { key: 'request', label: '加好友', color: '#1877f2' },
          { key: 'view',    label: '档案', color: '#6b7280' },
        ],
        onAction: function (action, lead) {
          if (action === 'request') {
            fbBatchRequestSingle(lead);
          } else if (action === 'view') {
            // 简单展示分数原因
            ocAlert('Lead: ' + lead.name + '\n\n分数: ' + (lead.score || 0)
              + '\nTier: ' + (lead.tier || '?')
              + '\n\n原因:\n  · ' + (lead.reasons || lead.score_reasons || []).join('\n  · '));
          }
        },
      });
      // 头部加汇总
      const top = leads.length
        ? '<div style="margin-bottom:8px;font-size:11px;color:var(--text-dim)">'
          + '当前共 <b style="color:var(--text)">' + leads.length + '</b> 条 '
          + '· S 档 ' + leads.filter(function(l){return (l.tier||'')==='S';}).length
          + ' · A 档 ' + leads.filter(function(l){return (l.tier||'')==='A';}).length
          + ' · B 档 ' + leads.filter(function(l){return (l.tier||'')==='B';}).length
          + '</div>'
        : '';
      body.innerHTML = top + body.innerHTML;
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444">加载失败: ' + e.message + '</div>';
    }
  };

  window.fbBatchRequestSingle = async function (lead) {
    if (!lead || !lead.name) return;
    if (!(await ocDialog({title:'创建任务',message:'为「' + lead.name + '」创建 facebook_add_friend 任务？',type:'info',confirmText:'创建',cancelText:'取消'}))) return;
    showToast('已加入队列(需要选定执行设备)', 'success');
    // 简化:跳到 BatchRequest 流程,带名字预填
    if (typeof fbBatchRequest === 'function') {
      fbBatchRequest();
    }
  };

  // ════════════════════════════════════════════════════════
  // Phase D3: 跨设备去重效果面板
  // ════════════════════════════════════════════════════════
  window.fbOpenDedupModal = async function () {
    const Shell = window.PlatShell;
    if (!Shell) { showToast('PlatShell 未加载', 'error'); return; }
    Shell.modal.open('fb-dedup-modal',
      '<div id="fb-dedup-body">加载中…</div>', { maxWidth: '820px' });
    try {
      const r = await Shell.api.get('/facebook/dedup-stats?since_hours=168');
      const cov = r.canonical_coverage || {};
      const trend = r.daily_trend || [];
      const topPeers = r.top_duplicated_peers || [];

      // 趋势 mini chart (text-based)
      const trendHtml = trend.length ? trend.map(function(d) {
        return '<tr><td style="padding:4px 8px;font-size:11px">' + (d.date || '') + '</td>'
          + '<td style="padding:4px 8px;color:#ef4444">' + (d.blocked || 0) + '</td>'
          + '<td style="padding:4px 8px;color:#3b82f6">' + (d.claims || 0) + '</td></tr>';
      }).join('') : '<tr><td colspan="3" style="padding:12px;text-align:center;color:var(--text-muted)">暂无数据</td></tr>';

      // Top duplicated peers
      const peersHtml = topPeers.length ? topPeers.map(function(p) {
        return '<tr><td style="padding:4px 8px;font-size:11px">' + (p.peer_name || '') + '</td>'
          + '<td style="padding:4px 8px;color:#ef4444">' + (p.blocked_count || 0) + '</td>'
          + '<td style="padding:4px 8px;font-size:10px;color:var(--text-muted)">' + (p.devices || []).map(function(d){return d.substr(0,8);}).join(', ') + '</td></tr>';
      }).join('') : '<tr><td colspan="3" style="padding:12px;text-align:center;color:var(--text-muted)">暂无重复</td></tr>';

      const html = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<h3 style="margin:0">🔒 跨设备去重防线 (近 7 天)</h3>'
        + '<button onclick="PlatShell.modal.close(\'fb-dedup-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        // KPI cards
        + '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">'
        + '<div style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:12px;text-align:center">'
        + '<div style="font-size:24px;font-weight:700;color:#ef4444">' + (r.friend_requests_blocked || 0) + '</div>'
        + '<div style="font-size:11px;color:var(--text-muted)">好友请求拦截</div></div>'
        + '<div style="background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.3);border-radius:8px;padding:12px;text-align:center">'
        + '<div style="font-size:24px;font-weight:700;color:#3b82f6">' + (r.conv_lock_conflicts || 0) + '</div>'
        + '<div style="font-size:11px;color:var(--text-muted)">对话锁冲突</div></div>'
        + '<div style="background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.3);border-radius:8px;padding:12px;text-align:center">'
        + '<div style="font-size:24px;font-weight:700;color:#22c55e">' + ((cov.coverage_rate || 0) * 100).toFixed(1) + '%</div>'
        + '<div style="font-size:11px;color:var(--text-muted)">事件 CID 覆盖率</div></div>'
        + '<div style="background:rgba(168,85,247,.08);border:1px solid rgba(168,85,247,.3);border-radius:8px;padding:12px;text-align:center">'
        + '<div style="font-size:24px;font-weight:700;color:#a855f7">' + ((cov.fr_coverage_rate || 0) * 100).toFixed(1) + '%</div>'
        + '<div style="font-size:11px;color:var(--text-muted)">好友请求 CID 覆盖率</div></div>'
        + '</div>'
        // G1: TTL 分布可视化
        + (function() {
          var ttl = r.ttl_distribution || {};
          var tiers = ttl.tiers || {};
          var total = ttl.total_claims || 0;
          if (total === 0) return '';
          var bars = [
            {key:'active_96h', label:'活跃 96h', color:'#22c55e', count: tiers.active_96h || 0},
            {key:'default_48h', label:'正常 48h', color:'#3b82f6', count: tiers.default_48h || 0},
            {key:'stale_24h', label:'冷却 24h', color:'#f59e0b', count: tiers.stale_24h || 0},
            {key:'dormant_12h', label:'休眠 12h', color:'#6b7280', count: tiers.dormant_12h || 0},
          ];
          var barHtml = bars.map(function(b) {
            var pct = (b.count / total * 100).toFixed(1);
            return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">'
              + '<span style="font-size:10px;width:60px;color:var(--text-muted)">' + b.label + '</span>'
              + '<div style="flex:1;height:14px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden">'
              + '<div style="height:100%;width:' + pct + '%;background:' + b.color + ';border-radius:3px;transition:width .3s"></div></div>'
              + '<span style="font-size:10px;width:50px;text-align:right;color:' + b.color + '">' + b.count + ' (' + pct + '%)</span></div>';
          }).join('');
          return '<div style="margin-bottom:14px"><div style="font-size:12px;font-weight:600;margin-bottom:6px">🕐 对话锁 TTL 分布 (共 ' + total + ' 次 claim)</div>' + barHtml + '</div>';
        })()
        // Two tables side by side
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">'
        // Left: daily trend
        + '<div><div style="font-size:12px;font-weight:600;margin-bottom:6px">每日趋势</div>'
        + '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        + '<thead><tr style="background:rgba(255,255,255,.03)"><th style="text-align:left;padding:4px 8px">日期</th><th style="text-align:left;padding:4px 8px">拦截</th><th style="text-align:left;padding:4px 8px">Claims</th></tr></thead>'
        + '<tbody>' + trendHtml + '</tbody></table></div>'
        // Right: top duplicated peers
        + '<div><div style="font-size:12px;font-weight:600;margin-bottom:6px">高频重复 Peer</div>'
        + '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        + '<thead><tr style="background:rgba(255,255,255,.03)"><th style="text-align:left;padding:4px 8px">姓名</th><th style="text-align:left;padding:4px 8px">次数</th><th style="text-align:left;padding:4px 8px">设备</th></tr></thead>'
        + '<tbody>' + peersHtml + '</tbody></table></div>'
        + '</div>'
        // Footer + backfill button
        + '<div style="margin-top:14px;display:flex;justify-content:space-between;align-items:center">'
        + '<span style="font-size:11px;color:var(--text-muted)">'
        + '统计范围: ' + (r.scope_device || 'all') + ' | 近 ' + (r.scope_since_hours || 168) + ' 小时'
        + ' | 事件数: ' + (cov.contact_events_total || 0) + ' | 好友请求数: ' + (cov.friend_requests_total || 0)
        + '</span>'
        + '<button onclick="fbDedupBackfill()" style="background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.3);padding:4px 12px;border-radius:6px;cursor:pointer;font-size:11px">🔄 身份回填</button>'
        + '</div>';
      document.getElementById('fb-dedup-body').innerHTML = html;
    } catch (e) {
      document.getElementById('fb-dedup-body').innerHTML =
        '<div style="color:#ef4444">加载失败: ' + e.message + '</div>';
    }
  };

  window.fbDedupBackfill = async function () {
    const Shell = window.PlatShell;
    if (!Shell) return;
    // 先 dry-run 预览
    try {
      const preview = await Shell.api.post('/facebook/dedup-backfill', { dry_run: true, limit: 200 });
      const count = preview.processed || 0;
      if (count === 0) { showToast('没有可回填的目标', 'info'); return; }
      if (!confirm('预览: 可回填 ' + count + ' 条身份标识\n\n确认执行？(非 dry-run)')) return;
      const result = await Shell.api.post('/facebook/dedup-backfill', { dry_run: false, limit: 200 });
      showToast('回填完成: 新增 ' + (result.new_identities || 0) + ' 条, 跳过 ' + (result.skipped || 0) + ' 条', 'success');
      // refresh panel
      fbOpenDedupModal();
    } catch (e) {
      showToast('回填失败: ' + (e.message || e), 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // BB1: 方案执行结果摘要面板
  // 任务完成后自动弹出 / 手动从任务列表点击查看
  // 数据来源: facebook.campaign_done 事件 或 task.result JSON
  // ════════════════════════════════════════════════════════
  window.fbShowCampaignResult = async function (data) {
    if (!data) return;
    var overlay = _fbModalOverlay('fb-campaign-result');
    var devId = data.device_id || '';
    var devName = (typeof ALIAS !== 'undefined' ? ALIAS[devId] : '') || devId.substring(0, 8) || '?';

    var extracted = parseInt(data.extracted_members || 0);
    var sent = parseInt(data.friend_requests_sent || 0);
    var greeted = parseInt(data.greetings_sent || 0);
    var replied = parseInt(data.messages_replied || 0);
    var stepsOk = data.steps_completed || [];
    var stepsFail = data.steps_failed || [];
    var outcome = data.outcome || (stepsFail.length ? 'partial_failed' : 'ok');

    var outcomeMap = {
      ok: {color: '#22c55e', icon: '✅', label: '全部完成'},
      partial_failed: {color: '#f59e0b', icon: '⚠️', label: '部分失败'},
      outreach_goal_not_met: {color: '#f59e0b', icon: '📉', label: '目标未达成'},
    };
    var oc = outcomeMap[outcome] || {color: '#64748b', icon: '📋', label: outcome};

    var funnelSteps = [
      {key: 'extracted', label: '候选提取', value: extracted, icon: '👥', color: '#64748b'},
      {key: 'sent', label: '好友请求', value: sent, icon: '📤', color: '#3b82f6'},
      {key: 'greeted', label: '打招呼', value: greeted, icon: '👋', color: '#8b5cf6'},
      {key: 'replied', label: '收件处理', value: replied, icon: '💬', color: '#22c55e'},
    ];

    var rateExtToSent = extracted > 0 ? Math.round(sent / extracted * 100) : 0;
    var rateSentToGreet = sent > 0 ? Math.round(greeted / sent * 100) : 0;

    var funnelHtml = funnelSteps.map(function (s, i) {
      var barW = extracted > 0 ? Math.max(8, Math.round(s.value / extracted * 100)) : (s.value > 0 ? 100 : 8);
      var rateLabel = '';
      if (i === 1 && extracted > 0) rateLabel = ' (' + rateExtToSent + '%)';
      if (i === 2 && sent > 0) rateLabel = ' (' + rateSentToGreet + '%)';
      return '<div style="display:flex;align-items:center;gap:10px;margin-bottom:6px">'
        + '<span style="width:80px;font-size:11px;color:var(--text-dim);text-align:right">' + s.icon + ' ' + s.label + '</span>'
        + '<div style="flex:1;height:22px;background:var(--bg-main);border-radius:4px;overflow:hidden;position:relative">'
        + '<div style="height:100%;width:' + barW + '%;background:' + s.color + '22;border-radius:4px;transition:width .5s"></div>'
        + '<span style="position:absolute;left:8px;top:3px;font-size:11px;font-weight:700;color:' + s.color + '">' + s.value + rateLabel + '</span>'
        + '</div></div>';
    }).join('');

    var stepsHtml = stepsOk.map(function (s) {
      return '<span style="padding:2px 8px;background:rgba(34,197,94,.1);color:#22c55e;border-radius:4px;font-size:10px;font-weight:600">\u2713 ' + s.replace(/facebook_/g, '').replace(/_/g, ' ') + '</span>';
    }).join(' ');
    if (stepsFail.length) {
      stepsHtml += ' ' + stepsFail.map(function (f) {
        var s = typeof f === 'string' ? f : (f.step || '');
        return '<span style="padding:2px 8px;background:rgba(239,68,68,.1);color:#ef4444;border-radius:4px;font-size:10px;font-weight:600">\u2717 ' + s.replace(/facebook_/g, '').replace(/_/g, ' ') + '</span>';
      }).join(' ');
    }

    var goalHtml = '';
    if (data.outreach_goal) {
      var prog = data.outreach_goal_progress || {};
      var reason = prog.exhaust_reason || '';
      var reasonMap = {
        no_candidates_extracted: '候选池为空',
        all_candidates_filtered: '全部被过滤',
        all_attempts_rejected: '所有尝试被拒',
        quota_or_pool_exhausted: '配额/池耗尽',
      };
      goalHtml = '<div style="margin-top:8px;padding:8px 12px;background:rgba(245,158,11,.06);border:1px solid rgba(245,158,11,.2);border-radius:8px;font-size:11px;color:#fbbf24">'
        + '\uD83C\uDFAF 目标: ' + data.outreach_goal + ' 好友请求 \u2192 实际: ' + sent
        + (reason ? ' \u00B7 原因: ' + (reasonMap[reason] || reason) : '')
        + '</div>';
    }

    var closeId = 'fb-campaign-result';
    overlay.innerHTML = '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:0;max-width:560px;width:96%;box-shadow:0 20px 60px rgba(0,0,0,.4);overflow:hidden">'
      + '<div style="padding:18px 24px 14px;background:linear-gradient(180deg,' + oc.color + '12,transparent)">'
      + '<div style="display:flex;align-items:center;justify-content:space-between">'
      + '<div style="display:flex;align-items:center;gap:10px">'
      + '<div style="width:40px;height:40px;border-radius:12px;background:' + oc.color + '20;display:flex;align-items:center;justify-content:center;font-size:20px">' + oc.icon + '</div>'
      + '<div><div style="font-size:16px;font-weight:700;color:var(--text)">\u6267\u884C\u7ED3\u679C\u6458\u8981</div>'
      + '<div style="font-size:11px;color:var(--text-muted);margin-top:2px">\u8BBE\u5907 ' + _escHtml(devName) + ' \u00B7 ' + oc.label + '</div></div></div>'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:16px;width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center">\u2715</button>'
      + '</div></div>'
      + '<div style="padding:0 24px 20px">'
      + '<div style="margin-bottom:14px"><div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:8px">\uD83D\uDCCA \u8F6C\u5316\u6F0F\u6597</div>' + funnelHtml + '</div>'
      + '<div style="margin-bottom:14px"><div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:6px">\uD83D\uDD17 \u6B65\u9AA4\u72B6\u6001</div>'
      + '<div style="display:flex;flex-wrap:wrap;gap:4px">' + stepsHtml + '</div></div>'
      + goalHtml
      + '<div id="fb-result-today-summary" style="margin-top:10px;font-size:10px;color:var(--text-dim)">\uD83D\uDCC8 \u52A0\u8F7D\u4ECA\u65E5\u6C47\u603B\u2026</div>'
      + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove()" style="padding:7px 16px;background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:8px;cursor:pointer;font-size:12px">\u5173\u95ED</button>'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove();fbOpenFunnelModal()" style="padding:7px 16px;background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.3);color:#60a5fa;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600">\uD83D\uDCCA \u67E5\u770B\u5B8C\u6574\u6F0F\u6597</button>'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove();fbOpenPresetsModal()" style="padding:7px 16px;background:linear-gradient(135deg,#1877f2,#0d6efd);border:none;color:#fff;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600">\u26A1 \u518D\u6B21\u6267\u884C</button>'
      + '</div></div></div>';

    // 异步加载今日汇总
    (async function () {
      try {
        var r = await api('GET', '/facebook/funnel?since_hours=24');
        var el = document.getElementById('fb-result-today-summary');
        if (!el) return;
        var steps = r.steps || [];
        var todaySent = 0, todayAccepted = 0, todayDM = 0, todayRef = 0;
        steps.forEach(function (st) {
          if (st.key === 'friend_requests_sent') todaySent = st.value || 0;
          if (st.key === 'friends_accepted') todayAccepted = st.value || 0;
          if (st.key === 'dm_conversations') todayDM = st.value || 0;
          if (st.key === 'wa_referrals') todayRef = st.value || 0;
        });
        el.innerHTML = '<div style="padding:8px 12px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px">'
          + '<div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:6px">\uD83D\uDCC8 \u4ECA\u65E5\u5168\u91CF\u6C47\u603B (24h)</div>'
          + '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;text-align:center">'
          + '<div><div style="font-size:16px;font-weight:700;color:#3b82f6">' + todaySent + '</div><div style="font-size:9px;color:var(--text-dim)">\u597D\u53CB\u8BF7\u6C42</div></div>'
          + '<div><div style="font-size:16px;font-weight:700;color:#22c55e">' + todayAccepted + '</div><div style="font-size:9px;color:var(--text-dim)">\u5DF2\u901A\u8FC7</div></div>'
          + '<div><div style="font-size:16px;font-weight:700;color:#8b5cf6">' + todayDM + '</div><div style="font-size:9px;color:var(--text-dim)">DM \u5BF9\u8BDD</div></div>'
          + '<div><div style="font-size:16px;font-weight:700;color:#f59e0b">' + todayRef + '</div><div style="font-size:9px;color:var(--text-dim)">\u5F15\u6D41\u6210\u529F</div></div>'
          + '</div></div>';
      } catch (e) {
        var el2 = document.getElementById('fb-result-today-summary');
        if (el2) el2.textContent = '';
      }
    })();
  };

  // BB1: 从任务列表手动查看结果
  window.fbViewTaskResult = async function (taskId) {
    if (!taskId) return;
    try {
      var t = await api('GET', '/tasks/' + taskId);
      var r = (t && t.result) || {};
      if (r.card_type !== 'fb_campaign') {
        showToast('\u8BE5\u4EFB\u52A1\u975E Facebook \u65B9\u6848\u4EFB\u52A1', 'info');
        return;
      }
      r.device_id = t.device_id || '';
      fbShowCampaignResult(r);
    } catch (e) {
      showToast('\u52A0\u8F7D\u5931\u8D25: ' + (e.message || e), 'error');
    }
  };

  // BB1.1: 多设备聚合结果面板
  window.fbShowBatchResult = function (results) {
    if (!results || !results.length) return;
    if (results.length === 1) { fbShowCampaignResult(results[0]); return; }

    var overlay = _fbModalOverlay('fb-batch-result');
    var n = results.length;
    var totExtracted = 0, totSent = 0, totGreeted = 0, totReplied = 0;
    var allStepsOk = 0, allStepsFail = 0;
    results.forEach(function (d) {
      totExtracted += parseInt(d.extracted_members || 0);
      totSent += parseInt(d.friend_requests_sent || 0);
      totGreeted += parseInt(d.greetings_sent || 0);
      totReplied += parseInt(d.messages_replied || 0);
      allStepsOk += (d.steps_completed || []).length;
      allStepsFail += (d.steps_failed || []).length;
    });

    var rateAccept = totExtracted > 0 ? Math.round(totSent / totExtracted * 100) : 0;
    var rateGreet = totSent > 0 ? Math.round(totGreeted / totSent * 100) : 0;

    // 每设备行
    var devRows = results.map(function (d) {
      var devId = d.device_id || '';
      var devName = (typeof ALIAS !== 'undefined' ? ALIAS[devId] : '') || devId.substring(0, 8) || '?';
      var ok = (d.steps_completed || []).length;
      var fail = (d.steps_failed || []).length;
      var sent = parseInt(d.friend_requests_sent || 0);
      var greeted = parseInt(d.greetings_sent || 0);
      var statusColor = fail > 0 ? '#f59e0b' : '#22c55e';
      return '<tr style="border-bottom:1px solid var(--border)">'
        + '<td style="padding:6px 8px;font-size:11px">' + _escHtml(devName) + '</td>'
        + '<td style="padding:6px 8px;font-size:11px;color:#3b82f6;text-align:center">' + sent + '</td>'
        + '<td style="padding:6px 8px;font-size:11px;color:#8b5cf6;text-align:center">' + greeted + '</td>'
        + '<td style="padding:6px 8px;font-size:11px;color:' + statusColor + ';text-align:center">' + ok + '/' + (ok + fail) + '</td>'
        + '</tr>';
    }).join('');

    var closeId = 'fb-batch-result';
    overlay.innerHTML = '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:0;max-width:600px;width:96%;box-shadow:0 20px 60px rgba(0,0,0,.4);overflow:hidden">'
      + '<div style="padding:18px 24px 14px;background:linear-gradient(180deg,rgba(34,197,94,.08),transparent)">'
      + '<div style="display:flex;align-items:center;justify-content:space-between">'
      + '<div style="display:flex;align-items:center;gap:10px">'
      + '<div style="width:40px;height:40px;border-radius:12px;background:rgba(34,197,94,.15);display:flex;align-items:center;justify-content:center;font-size:20px">\uD83D\uDCCA</div>'
      + '<div><div style="font-size:16px;font-weight:700;color:var(--text)">\u6279\u91CF\u6267\u884C\u7ED3\u679C</div>'
      + '<div style="font-size:11px;color:var(--text-muted);margin-top:2px">' + n + ' \u53F0\u8BBE\u5907\u5B8C\u6210</div></div></div>'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:16px;width:28px;height:28px;border-radius:6px;display:flex;align-items:center;justify-content:center">\u2715</button>'
      + '</div></div>'
      + '<div style="padding:0 24px 20px">'
      // 聚合指标
      + '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px">'
      + '<div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center"><div style="font-size:20px;font-weight:700;color:#64748b">' + totExtracted + '</div><div style="font-size:9px;color:var(--text-dim)">\u5019\u9009\u63D0\u53D6</div></div>'
      + '<div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center"><div style="font-size:20px;font-weight:700;color:#3b82f6">' + totSent + '</div><div style="font-size:9px;color:var(--text-dim)">\u597D\u53CB\u8BF7\u6C42 (' + rateAccept + '%)</div></div>'
      + '<div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center"><div style="font-size:20px;font-weight:700;color:#8b5cf6">' + totGreeted + '</div><div style="font-size:9px;color:var(--text-dim)">\u6253\u62DB\u547C (' + rateGreet + '%)</div></div>'
      + '<div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center"><div style="font-size:20px;font-weight:700;color:#22c55e">' + totReplied + '</div><div style="font-size:9px;color:var(--text-dim)">\u6536\u4EF6\u5904\u7406</div></div>'
      + '</div>'
      // 设备明细表
      + '<div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:6px">\u8BBE\u5907\u660E\u7EC6</div>'
      + '<div style="max-height:200px;overflow-y:auto;border:1px solid var(--border);border-radius:8px">'
      + '<table style="width:100%;border-collapse:collapse;font-size:11px">'
      + '<thead><tr style="background:rgba(255,255,255,.03)"><th style="padding:6px 8px;text-align:left">\u8BBE\u5907</th><th style="padding:6px 8px;text-align:center">\u597D\u53CB\u8BF7\u6C42</th><th style="padding:6px 8px;text-align:center">\u6253\u62DB\u547C</th><th style="padding:6px 8px;text-align:center">\u6B65\u9AA4</th></tr></thead>'
      + '<tbody>' + devRows + '</tbody></table></div>'
      // 操作按钮
      + '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:14px;padding-top:12px;border-top:1px solid var(--border)">'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove()" style="padding:7px 16px;background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:8px;cursor:pointer;font-size:12px">\u5173\u95ED</button>'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove();fbOpenFunnelModal()" style="padding:7px 16px;background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.3);color:#60a5fa;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600">\uD83D\uDCCA \u67E5\u770B\u5B8C\u6574\u6F0F\u6597</button>'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove();fbOpenPresetsModal()" style="padding:7px 16px;background:linear-gradient(135deg,#1877f2,#0d6efd);border:none;color:#fff;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600">\u26A1 \u518D\u6B21\u6267\u884C</button>'
      + '</div></div></div>';
  };

  // BB1: 监听 WS campaign_done 事件 — 在 FB 页面时自动弹出结果面板
  var _fbCampaignListenerAttached = false;
  function _fbListenCampaignDone() {
    if (_fbCampaignListenerAttached) return;
    _fbCampaignListenerAttached = true;
    window.addEventListener('oc:event', function (evt) {
      var d = (evt && evt.detail) || {};
      if (d.type !== 'facebook.campaign_done') return;
      var evtData = d.data || d;
      var onFbPage = document.querySelector('#page-plat-facebook');
      if (onFbPage && onFbPage.offsetParent !== null) {
        fbShowCampaignResult(evtData);
      }
    });
  }
  _fbListenCampaignDone();

  // BB3c: 另存为自定义预设
  window.fbSavePresetAs = function (sourceKey) {
    if (!sourceKey) return;
    var source = (_fbPresets || []).find(function (p) { return p.key === sourceKey; });
    var srcName = source ? source.name : sourceKey;

    var overlay = _fbModalOverlay('fb-save-as-modal');
    var closeId = 'fb-save-as-modal';

    // 可调参数（从源预设提取）
    var srcParams = {};
    if (source && source.steps && source.steps.length) {
      source.steps.forEach(function (s) {
        var p = s.params || {};
        if (p.max_friends_per_run !== undefined) srcParams.max_friends_per_run = p.max_friends_per_run;
        if (p.outreach_goal !== undefined) srcParams.outreach_goal = p.outreach_goal;
        if (p.warmup_scrolls !== undefined) srcParams.warmup_scrolls = p.warmup_scrolls;
        if (p.max_conversations !== undefined) srcParams.max_conversations = p.max_conversations;
        if (p.group_max_posts !== undefined) srcParams.group_max_posts = p.group_max_posts;
      });
    }
    var paramRows = '';
    var editableKeys = [
      { k: 'max_friends_per_run', label: '\u6BCF\u6B21\u6700\u591A\u597D\u53CB\u8BF7\u6C42', min: 1, max: 15 },
      { k: 'outreach_goal', label: '\u76EE\u6807\u5B8C\u6210\u6570', min: 1, max: 15 },
      { k: 'warmup_scrolls', label: '\u517B\u53F7\u6ED1\u52A8\u6B21\u6570', min: 5, max: 50 },
      { k: 'max_conversations', label: '\u6700\u591A\u5904\u7406\u5BF9\u8BDD\u6570', min: 1, max: 30 },
      { k: 'group_max_posts', label: '\u7FA4\u6700\u591A\u6D4F\u89C8\u5E16\u6570', min: 1, max: 20 },
    ];
    editableKeys.forEach(function (def) {
      if (srcParams[def.k] !== undefined) {
        paramRows += '<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)">'
          + '<span style="font-size:11px;color:var(--text-muted)">' + def.label + '</span>'
          + '<input id="fb-sa-' + def.k + '" type="number" min="' + def.min + '" max="' + def.max + '" value="' + srcParams[def.k] + '" style="width:60px;padding:4px 8px;border:1px solid var(--border);border-radius:6px;background:var(--bg-main);color:var(--text);font-size:12px;text-align:center">'
          + '</div>';
      }
    });

    overlay.innerHTML = '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:0;max-width:460px;width:94%;box-shadow:0 20px 60px rgba(0,0,0,.4);overflow:hidden">'
      + '<div style="padding:18px 24px 14px;background:linear-gradient(180deg,rgba(245,158,11,.08),transparent)">'
      + '<div style="display:flex;align-items:center;justify-content:space-between">'
      + '<div><div style="font-size:16px;font-weight:700;color:var(--text)">\u2B50 \u53E6\u5B58\u4E3A\u81EA\u5B9A\u4E49\u65B9\u6848</div>'
      + '<div style="font-size:11px;color:var(--text-muted);margin-top:2px">\u57FA\u4E8E\u300C' + _escHtml(srcName) + '\u300D\u514B\u9686</div></div>'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:16px">\u2715</button>'
      + '</div></div>'
      + '<div style="padding:0 24px 20px">'
      + '<div style="margin-bottom:12px"><label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:4px">\u65B9\u6848\u540D\u79F0</label>'
      + '<input id="fb-sa-name" type="text" placeholder="\u4F8B: \u4FDD\u5B88\u62D3\u5C55 x3" value="' + _escHtml(srcName) + ' (\u81EA\u5B9A\u4E49)" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text);font-size:13px;box-sizing:border-box"></div>'
      + '<div style="margin-bottom:12px"><label style="font-size:11px;color:var(--text-muted);display:block;margin-bottom:4px">\u63CF\u8FF0 (\u53EF\u9009)</label>'
      + '<input id="fb-sa-desc" type="text" placeholder="\u7B80\u8981\u8BF4\u660E\u8C03\u6574\u5185\u5BB9" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg-main);color:var(--text);font-size:12px;box-sizing:border-box"></div>'
      + (paramRows ? '<div style="margin-bottom:14px"><div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:6px">\u53C2\u6570\u8C03\u6574</div>' + paramRows + '</div>' : '')
      + '<div style="display:flex;gap:8px;justify-content:flex-end;padding-top:12px;border-top:1px solid var(--border)">'
      + '<button onclick="document.getElementById(\'' + closeId + '\').remove()" style="padding:8px 16px;background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:8px;cursor:pointer;font-size:12px">\u53D6\u6D88</button>'
      + '<button onclick="_fbDoSaveAs(\'' + sourceKey + '\')" style="padding:8px 20px;background:linear-gradient(135deg,#f59e0b,#d97706);border:none;color:#fff;border-radius:8px;cursor:pointer;font-size:12px;font-weight:600">\u2B50 \u4FDD\u5B58</button>'
      + '</div></div></div>';
  };

  window._fbDoSaveAs = async function (sourceKey) {
    var name = (document.getElementById('fb-sa-name') || {}).value || '';
    var desc = (document.getElementById('fb-sa-desc') || {}).value || '';
    if (!name.trim()) { showToast('\u8BF7\u8F93\u5165\u65B9\u6848\u540D\u79F0', 'warn'); return; }
    var overrides = {};
    ['max_friends_per_run', 'outreach_goal', 'warmup_scrolls', 'max_conversations', 'group_max_posts'].forEach(function (k) {
      var el = document.getElementById('fb-sa-' + k);
      if (el) overrides[k] = parseInt(el.value) || 0;
    });
    try {
      var r = await api('POST', '/facebook/presets/save-as', {
        source_key: sourceKey, name: name.trim(), desc: desc.trim(),
        param_overrides: overrides
      });
      var modal = document.getElementById('fb-save-as-modal');
      if (modal) modal.remove();
      showToast('\u2B50 \u5DF2\u4FDD\u5B58\u81EA\u5B9A\u4E49\u65B9\u6848\u300C' + name.trim() + '\u300D', 'success');
      _fbPresets = null;
      // 刷新 presets modal
      var pm = document.getElementById('fb-presets-modal');
      if (pm) { pm.remove(); setTimeout(function () { fbOpenPresetsModal(); }, 200); }
    } catch (e) {
      showToast('\u4FDD\u5B58\u5931\u8D25: ' + (e.message || e), 'error');
    }
  };

  // BB3c: 删除自定义预设
  window.fbDeleteCustomPreset = async function (presetKey) {
    if (!confirm('\u786E\u5B9A\u5220\u9664\u8BE5\u81EA\u5B9A\u4E49\u65B9\u6848\uFF1F')) return;
    try {
      await api('DELETE', '/facebook/presets/' + presetKey);
      showToast('\u5DF2\u5220\u9664\u81EA\u5B9A\u4E49\u65B9\u6848', 'success');
      _fbPresets = null;
      var pm = document.getElementById('fb-presets-modal');
      if (pm) { pm.remove(); setTimeout(function () { fbOpenPresetsModal(); }, 200); }
    } catch (e) {
      showToast('\u5220\u9664\u5931\u8D25: ' + (e.message || e), 'error');
    }
  };

})();
