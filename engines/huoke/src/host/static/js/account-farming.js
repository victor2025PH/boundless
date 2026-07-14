// account-farming.js — 账号养号页：互相关注 + 互相互动 + 健康评分
// v2: 新增健康评分、养号阶段徽章、去重冷却提示

const AccountFarming = (() => {
  let _usernames = {};   // device_id → username
  let _health = {};      // device_id → health data
  let _devices = [];

  const PHASE_LABELS = {
    cold_start:        { label: '冷启动',  color: '#6b7280', bg: '#f3f4f6' },
    interest_building: { label: '算法养成', color: '#d97706', bg: '#fef3c7' },
    active:            { label: '活跃',    color: '#059669', bg: '#d1fae5' },
  };

  // ── 主刷新 ──
  async function refresh() {
    try {
      const [rU, rH] = await Promise.all([
        api('GET', '/tiktok/account-usernames'),
        api('GET', '/tiktok/account-health').catch(() => ({ health: {} })),
      ]);
      _usernames = rU.usernames || {};
      _health    = rH.health    || {};
      _devices   = Object.keys(_usernames);
      _renderPage();
    } catch(e) {
      console.warn('[AccountFarming] refresh error:', e);
      showToast('加载失败: ' + e.message, 'error');
    }
  }

  // ── 渲染 ──
  function _renderPage() {
    const el = document.getElementById('page-account-farming');
    if (!el) return;

    const configured = _devices.filter(d => _usernames[d]).length;
    const total      = _devices.length;
    const avgHealth  = _devices.length
      ? Math.round(_devices.reduce((s,d) => s + (_health[d]?.health_score||0), 0) / _devices.length)
      : 0;

    el.innerHTML = `
      <div style="padding:12px 16px">
        <!-- 页头 -->
        <div style="margin-bottom:14px">
          <h3 style="margin:0;font-size:16px">&#127807; 账号养号</h3>
          <p style="margin:4px 0 0;font-size:12px;color:var(--text-muted)">
            管理 TikTok 用户名，一键互相关注 & 互动，提升账号算法权重
          </p>
        </div>

        <!-- 概览卡片 -->
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:14px">
          ${_statCard('在线设备', total, '#3b82f6')}
          ${_statCard('已配置用户名', configured, configured===total&&total>0?'#22c55e':'#f59e0b')}
          ${_statCard('平均健康分', avgHealth, avgHealth>=60?'#22c55e':avgHealth>=30?'#f59e0b':'#ef4444')}
          ${_statCard('可互动对数', configured>=2?configured*(configured-1):0, '#8b5cf6')}
        </div>

        <!-- 操作按钮行 -->
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:14px">
          <button class="qa-btn" onclick="AccountFarming.scanAll()" style="background:var(--accent);color:#fff;border-color:var(--accent)">
            &#128247; 扫描所有用户名
          </button>
          <button class="qa-btn" onclick="AccountFarming.crossFollowAll()" ${configured<2?'disabled':''}
            style="${configured>=2?'border-color:#22c55e;color:#22c55e':''}">
            &#128101; 一键互相关注
          </button>
          <button class="qa-btn" onclick="AccountFarming.crossInteractAll()" ${configured<2?'disabled':''}
            style="${configured>=2?'border-color:#8b5cf6;color:#8b5cf6':''}">
            &#10084; 互相点赞观看
          </button>
          <button class="qa-btn" onclick="AccountFarming.crossInteractAll(true)" ${configured<2?'disabled':''}
            style="${configured>=2?'border-color:#f59e0b;color:#f59e0b':''}">
            &#128172; 互相点赞+评论
          </button>
          <button class="qa-btn" onclick="AccountFarming.refresh()">&#8635; 刷新</button>
        </div>

        ${configured < 2 ? `
        <div style="margin-bottom:14px;padding:10px 12px;background:#fef3c7;border-left:4px solid #f59e0b;border-radius:4px;font-size:12px">
          <strong>提示：</strong>请先「扫描所有用户名」或手动填写下方表格，至少配置 2 台设备的用户名后才能开始互相关注/互动。
        </div>` : ''}

        <!-- 互动参数 (compact) -->
        <div style="display:flex;align-items:center;gap:16px;margin-bottom:14px;padding:8px 12px;background:var(--bg-input);border-radius:6px;font-size:12px">
          <span style="color:var(--text-muted)">互动参数：</span>
          <label style="display:flex;align-items:center;gap:4px">
            观看
            <input type="number" id="af-watch-secs" value="15" min="5" max="60"
              style="width:50px;padding:2px 6px;background:var(--bg-main);border:1px solid var(--border);border-radius:4px;color:var(--text-main)">秒
          </label>
          <label style="display:flex;align-items:center;gap:4px">
            <input type="checkbox" id="af-do-like" checked> 点赞
          </label>
          <label style="display:flex;align-items:center;gap:4px">
            <input type="checkbox" id="af-do-comment"> AI评论
          </label>
          <label style="display:flex;align-items:center;gap:4px">
            冷却
            <input type="number" id="af-cooldown-h" value="4" min="1" max="24"
              style="width:45px;padding:2px 6px;background:var(--bg-main);border:1px solid var(--border);border-radius:4px;color:var(--text-main)">h
          </label>
        </div>

        <!-- 设备健康 & 用户名配置表 -->
        <div class="card">
          <div class="card-header" style="font-size:13px;font-weight:600">设备状态 & 用户名配置</div>
          <div style="overflow-x:auto">
            <table style="width:100%;border-collapse:collapse;font-size:12px">
              <thead>
                <tr style="background:var(--bg-input);border-bottom:1px solid var(--border)">
                  <th style="padding:8px 10px;text-align:left">设备</th>
                  <th style="padding:8px 10px;text-align:left">用户名</th>
                  <th style="padding:8px 10px;text-align:center">健康分</th>
                  <th style="padding:8px 10px;text-align:center">阶段</th>
                  <th style="padding:8px 10px;text-align:right">观看/关注/DM</th>
                  <th style="padding:8px 10px;text-align:right">算法分</th>
                  <th style="padding:8px 10px;text-align:center">第N天</th>
                  <th style="padding:8px 10px;text-align:center">操作</th>
                </tr>
              </thead>
              <tbody>
                ${_devices.length === 0
                  ? `<tr><td colspan="8" style="text-align:center;color:var(--text-muted);padding:20px">暂无在线设备</td></tr>`
                  : _devices.map(did => _renderDeviceRow(did)).join('')}
              </tbody>
            </table>
          </div>
        </div>
      </div>
    `;
  }

  function _statCard(label, value, color) {
    return `<div style="background:var(--bg-input);border-radius:8px;padding:10px 12px;border-left:3px solid ${color}">
      <div style="font-size:20px;font-weight:700;color:${color}">${value}</div>
      <div style="font-size:11px;color:var(--text-muted);margin-top:2px">${label}</div>
    </div>`;
  }

  function _renderDeviceRow(did) {
    const uname  = _usernames[did] || '';
    const h      = _health[did]    || {};
    const score  = h.health_score  ?? 0;
    const phase  = h.phase         || 'cold_start';
    const pInfo  = PHASE_LABELS[phase] || PHASE_LABELS.cold_start;
    const watched  = h.total_watched  || 0;
    const followed = h.total_followed || 0;
    const dms      = h.total_dms_sent || 0;
    const algoRaw  = h.algorithm_score|| 0;
    const algoStr  = (algoRaw * 100).toFixed(0) + '%';
    const day      = h.day || 0;
    const shortId  = did.substring(0, 8);

    // 健康条颜色
    const barColor = score >= 60 ? '#22c55e' : score >= 30 ? '#f59e0b' : '#ef4444';

    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:8px 10px">
        <code style="font-size:11px;color:var(--text-muted)">${shortId}…</code>
      </td>
      <td style="padding:8px 10px">
        <input type="text" id="uname-${did}" value="${uname}"
          placeholder="@username"
          style="width:130px;padding:3px 6px;background:var(--bg-main);border:1px solid var(--border);border-radius:4px;color:var(--text-main);font-size:12px"
          onkeydown="if(event.key==='Enter')AccountFarming.saveUsername('${did}')">
        ${uname
          ? `<span style="color:#22c55e;font-size:11px">&#10003;</span>`
          : `<button class="qa-btn" style="margin-left:4px;padding:2px 6px;font-size:11px" onclick="AccountFarming.scanDevice('${did}')">扫描</button>`}
      </td>
      <td style="padding:8px 10px;text-align:center">
        <div style="display:flex;flex-direction:column;align-items:center;gap:2px">
          <span style="font-weight:700;color:${barColor}">${score}</span>
          <div style="width:50px;height:4px;background:var(--border);border-radius:2px">
            <div style="width:${score}%;height:100%;background:${barColor};border-radius:2px"></div>
          </div>
        </div>
      </td>
      <td style="padding:8px 10px;text-align:center">
        <span style="padding:2px 7px;border-radius:10px;font-size:11px;background:${pInfo.bg};color:${pInfo.color}">
          ${pInfo.label}
        </span>
      </td>
      <td style="padding:8px 10px;text-align:right;color:var(--text-muted)">
        ${watched}/<span style="color:#22c55e">${followed}</span>/<span style="color:#3b82f6">${dms}</span>
      </td>
      <td style="padding:8px 10px;text-align:right">
        <span style="color:${algoRaw>=0.5?'#22c55e':algoRaw>=0.25?'#f59e0b':'var(--text-muted)'}">${algoStr}</span>
      </td>
      <td style="padding:8px 10px;text-align:center;color:var(--text-muted)">${day}d</td>
      <td style="padding:8px 10px;text-align:center">
        <button class="qa-btn" style="padding:2px 8px;font-size:11px"
          onclick="AccountFarming.saveUsername('${did}')">保存</button>
        <button class="qa-btn" style="padding:2px 8px;font-size:11px;margin-left:2px"
          onclick="AccountFarming.scanDevice('${did}')">&#128247;</button>
      </td>
    </tr>`;
  }

  // ── 扫描所有设备 ──
  async function scanAll() {
    showToast('正在启动所有设备用户名扫描...', 'info');
    try {
      const r = await api('POST', '/tiktok/scan-all-usernames');
      showToast(`已创建 ${r.created} 个扫描任务，约 30 秒后刷新`, 'success');
      setTimeout(refresh, 32000);
    } catch(e) { showToast('扫描失败: ' + e.message, 'error'); }
  }

  // ── 单设备扫描 ──
  async function scanDevice(did) {
    showToast(`为 ${did.substring(0,8)} 启动扫描...`, 'info');
    try {
      await api('POST', '/tasks', { task_type: 'tiktok_scan_username', device_id: did, params: {} });
      showToast('扫描任务已创建', 'success');
      setTimeout(refresh, 28000);
    } catch(e) { showToast('扫描失败: ' + e.message, 'error'); }
  }

  // ── 保存用户名 ──
  async function saveUsername(did) {
    const input = document.getElementById('uname-' + did);
    if (!input) return;
    const uname = input.value.trim();
    if (!uname) return;
    try {
      await api('PUT', '/tiktok/account-username', { device_id: did, username: uname });
      showToast(`已保存: ${uname}`, 'success');
      await refresh();
    } catch(e) { showToast('保存失败: ' + e.message, 'error'); }
  }

  // ── 互相关注 ──
  async function crossFollowAll() {
    if (!(await ocDialog({title:'互相关注',message:'将为所有在线设备创建互相关注任务。',type:'info',confirmText:'确认',cancelText:'取消'}))) return;
    try {
      const r = await api('POST', '/tiktok/cross-follow-all');
      if (r.ok === false) { showToast(r.error || '失败', 'error'); return; }
      showToast(`已创建 ${r.created} 个互相关注任务`, 'success');
    } catch(e) { showToast('失败: ' + e.message, 'error'); }
  }

  // ── 互相互动 ──
  async function crossInteractAll(withComment = false) {
    const watchSecs   = parseInt(document.getElementById('af-watch-secs')?.value  || '15');
    const doLike      = document.getElementById('af-do-like')?.checked !== false;
    const doComment   = withComment || (document.getElementById('af-do-comment')?.checked || false);
    const cooldownH   = parseInt(document.getElementById('af-cooldown-h')?.value  || '4');
    const desc = `观看${watchSecs}s${doLike?'+点赞':''}${doComment?'+AI评论':''} (冷却${cooldownH}h)`;
    if (!(await ocDialog({title:'互相互动',message:`互相互动任务：${desc}`,type:'info',confirmText:'确认',cancelText:'取消'}))) return;
    try {
      const r = await api('POST', '/tiktok/cross-interact-all', {
        watch_seconds: watchSecs, do_like: doLike,
        do_comment: doComment, cooldown_hours: cooldownH,
      });
      if (r.ok === false) { showToast(r.error || '失败', 'error'); return; }
      const skipMsg = r.skipped ? `，${r.skipped} 个在冷却中已跳过` : '';
      showToast(`已创建 ${r.created} 个互动任务${skipMsg}`, 'success');
    } catch(e) { showToast('失败: ' + e.message, 'error'); }
  }

  return { refresh, scanAll, scanDevice, saveUsername, crossFollowAll, crossInteractAll };
})();

if (typeof registerPage === 'function') {
  registerPage('account-farming', AccountFarming.refresh);
}
