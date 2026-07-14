// conversations.js — 对话监控 + 升级队列 + 客户线索页面
// P0.2: Real-time conversation monitoring page

const Conv = (() => {
  let _pollTimer = null;
  let _sseHandler = null;

  async function refresh() {
    await Promise.all([_loadEscalations(), _loadConversations(), _loadDailyStats(), _loadDeviceHealth(), _loadMonitorStatus()]);
  }

  async function _loadMonitorStatus() {
    try {
      const r = await api('GET', '/tiktok/auto-monitor');
      const btn = document.getElementById('auto-monitor-toggle');
      const status = document.getElementById('auto-monitor-status');
      if (btn) {
        btn.dataset.enabled = r.enabled ? '1' : '0';
        btn.style.background = r.enabled ? '#22c55e' : 'var(--bg-main)';
        btn.style.color = r.enabled ? '#fff' : 'var(--text-muted)';
        btn.textContent = r.enabled ? `自动监控 ✓ (${r.interval_minutes}分钟)` : '自动监控: 关闭';
      }
      if (status) {
        status.textContent = r.enabled ? `每 ${r.interval_minutes} 分钟自动检查收件箱` : '点击开启自动回复';
        status.style.color = r.enabled ? '#22c55e' : 'var(--text-muted)';
      }
    } catch(e) {}
  }

  async function toggleMonitor(intervalMinutes) {
    const btn = document.getElementById('auto-monitor-toggle');
    const currently = btn ? btn.dataset.enabled === '1' : false;
    const newEnabled = !currently;
    // If enabling, ask for interval
    let interval = intervalMinutes || 10;
    if (newEnabled && !intervalMinutes) {
      const input = await ocPrompt('自动检查间隔','10',{message:'单位: 分钟',inputPlaceholder:'分钟'});
      if (input === null) return;
      interval = parseInt(input) || 10;
    }
    try {
      const r = await api('POST', '/tiktok/auto-monitor', {enabled: newEnabled, interval_minutes: interval});
      if (r.ok) {
        showToast(newEnabled ? `已开启自动监控 (每${interval}分钟)` : '已关闭自动监控', newEnabled ? 'success' : 'info');
        await _loadMonitorStatus();
      }
    } catch(e) {
      showToast('操作失败: ' + e, 'error');
    }
  }

  async function _loadDeviceHealth() {
    try {
      const r = await api('GET', '/tiktok/warmup-progress');
      const devices = r.devices || [];
      const el = document.getElementById('conv-device-health');
      if (!el || devices.length === 0) return;
      const phaseColor = {'cold_start':'#94a3b8','interest_building':'#3b82f6','active':'#22c55e'};
      el.innerHTML = devices.slice(0, 10).map(d => {
        const color = phaseColor[d.phase] || '#94a3b8';
        const score = Math.round((d.algorithm_score || 0) * 100);
        return `
          <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:8px 10px;flex-shrink:0;width:120px">
            <div style="font-size:11px;font-weight:600;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${_esc(d.alias||d.device_id.slice(0,6))}</div>
            <div style="margin:4px 0">
              <span style="font-size:9px;background:${color}22;color:${color};border-radius:3px;padding:1px 4px">${_esc(d.phase_label||d.phase)}</span>
            </div>
            <div style="font-size:10px;color:var(--text-muted)">第${d.days_active||0}天 · AI ${score}%</div>
            <div style="margin-top:4px;height:3px;background:var(--border);border-radius:2px">
              <div style="height:100%;width:${score}%;background:${color};border-radius:2px"></div>
            </div>
          </div>
        `;
      }).join('');
    } catch(e) {}
  }

  async function _loadDailyStats() {
    try {
      const r = await api('GET', '/tiktok/daily-report');
      const tot = r.total || {};
      const td = r.today || {};
      _set('cs-follows',      tot.followed ?? 0);
      _set('cs-followbacks',  td.follow_backs ?? 0);
      _set('cs-dms',          tot.dms ?? 0);
      _set('cs-autoreplied',  td.auto_replied ?? 0);
      // escalated: from escalation queue
      const eq = await api('GET', '/tiktok/escalation-queue');
      _set('cs-escalated', (eq.items||[]).length);
    } catch(e) {
      console.warn('[Conv] daily stats error:', e);
    }
  }

  async function _loadEscalations() {
    try {
      const r = await api('GET', '/tiktok/escalation-queue');
      const items = r.items || [];
      const cnt = items.length;

      // Update badge in sidebar
      const badge = document.getElementById('escalation-badge');
      if (badge) {
        badge.textContent = cnt;
        badge.style.display = cnt > 0 ? 'inline' : 'none';
      }
      _set('esc-count', cnt);

      const el = document.getElementById('escalation-list');
      if (!el) return;
      if (cnt === 0) {
        el.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:20px;font-size:12px">暂无需处理的对话 &#128522;</div>';
        return;
      }
      el.innerHTML = items.map((item, i) => {
        const msgJ = JSON.stringify(item.message||'');
        const contactJ = JSON.stringify(item.contact||'');
        const deviceJ = JSON.stringify(item.device_id||'');
        return `
        <div style="background:var(--bg-main);border:1px solid #ef444466;border-radius:8px;padding:10px;position:relative">
          <div style="display:flex;justify-content:space-between;align-items:start">
            <div>
              <span style="font-weight:600;font-size:13px">&#128172; ${_esc(item.contact || '未知用户')}</span>
              <span style="margin-left:8px;font-size:10px;background:#ef44441a;color:#ef4444;border-radius:4px;padding:1px 5px">${_esc(item.intent || '未知意向')}</span>
            </div>
            <div style="display:flex;gap:6px;align-items:center">
              <span style="font-size:10px;color:var(--text-muted)">${_relTime(item.ts)}</span>
              <button onclick="Conv.dismissEscalation(${i})" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:14px;padding:0" title="已处理">&#10005;</button>
            </div>
          </div>
          <div style="margin-top:6px;font-size:12px;color:var(--text-main);word-break:break-word">${_esc(item.message || '')}</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:4px">设备: ${(item.device_id||'').slice(0,8)}</div>
          <div style="margin-top:8px">
            <button onclick="Conv.suggestReply(${i},${msgJ},${contactJ},${deviceJ})"
                    style="font-size:11px;padding:3px 10px;background:rgba(99,102,241,.12);color:#818cf8;border:1px solid rgba(99,102,241,.3);border-radius:5px;cursor:pointer">&#129302; AI建议回复</button>
          </div>
          <div id="suggest-panel-${i}" style="display:none;margin-top:8px"></div>
        </div>
        `;
      }).join('');
    } catch(e) {
      console.warn('[Conv] escalation load error:', e);
    }
  }

  async function _loadConversations() {
    try {
      const r = await api('GET', '/tiktok/conversations?limit=30');
      const convs = r.conversations || [];
      const el = document.getElementById('conv-list');
      if (!el) return;
      if (convs.length === 0) {
        el.innerHTML = '<div style="text-align:center;color:var(--text-muted);padding:20px;font-size:12px">暂无对话记录</div>';
        return;
      }
      const _fsmColors = {
        'NEW': '#94a3b8', 'GREETING': '#3b82f6', 'QUALIFYING': '#f59e0b',
        'PITCHING': '#8b5cf6', 'NEGOTIATING': '#ec4899', 'CONVERTED': '#22c55e',
        'DORMANT': '#6b7280', 'REJECTED': '#ef4444',
      };
      el.innerHTML = convs.map(c => {
        const isInbound = c.direction === 'inbound';
        const fsm = c.fsm_state || c.status?.toUpperCase() || 'NEW';
        const fsmColor = _fsmColors[fsm] || '#94a3b8';
        const needsAttention = isInbound && !['CONVERTED','REJECTED','DORMANT'].includes(fsm);
        const score = c.score || 0;
        const scoreBar = score > 0 ? `<div style="margin-top:3px;height:2px;background:var(--border);border-radius:1px;width:50px"><div style="height:100%;width:${Math.min(score,100)}%;background:${fsmColor};border-radius:1px"></div></div>` : '';
        return `
          <div onclick="${c.lead_id ? `Conv.showLeadDetail(${c.lead_id})` : `Conv.openDetail(${JSON.stringify(c).replace(/"/g,'&quot;')})`}"
               style="border-bottom:1px solid var(--border);padding:8px 4px;display:flex;gap:8px;align-items:start;cursor:pointer;border-radius:6px;transition:background .15s"
               onmouseover="this.style.background='var(--bg-hover,rgba(255,255,255,.05))'"
               onmouseout="this.style.background=''">
            <div style="flex-shrink:0;width:32px;height:32px;border-radius:50%;background:${needsAttention?'#ef444422':'var(--bg-main)'};display:flex;align-items:center;justify-content:center;font-size:16px;border:1px solid ${needsAttention?'#ef4444':'var(--border)'}">
              ${isInbound ? '&#128100;' : '&#129302;'}
            </div>
            <div style="flex:1;min-width:0">
              <div style="display:flex;justify-content:space-between">
                <span style="font-weight:600;font-size:12px">${_esc(c.contact || c.lead_id || '')}</span>
                <span style="font-size:10px;color:var(--text-muted)">${_relTime(c.ts)}</span>
              </div>
              <div style="font-size:11px;color:${needsAttention?'var(--text-main)':'var(--text-muted)'};margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:220px">
                ${_esc((c.last_message || '(无消息)').slice(0, 70))}
              </div>
              ${c.intent ? `<div style="font-size:10px;color:#f59e0b;margin-top:2px">意向: ${_esc(c.intent)}</div>` : ''}
            </div>
            <div style="flex-shrink:0;display:flex;flex-direction:column;gap:3px;align-items:flex-end">
              <span style="font-size:9px;background:${fsmColor}22;color:${fsmColor};border-radius:4px;padding:1px 5px;font-weight:600">${_esc(fsm)}</span>
              ${scoreBar}
            </div>
          </div>
        `;
      }).join('');
    } catch(e) {
      console.warn('[Conv] conversations load error:', e);
    }
  }

  // ── Detail Panel ──────────────────────────────────────────────────────────
  let _detailContact = null;
  let _detailDeviceId = '';

  function openDetail(c) {
    _detailContact = c;
    _detailDeviceId = c.device_id || '';
    const panel = document.getElementById('conv-detail-panel');
    if (!panel) return;
    panel.style.display = 'flex';
    document.getElementById('detail-contact-name').textContent = c.contact || c.lead_id || '联系人';
    document.getElementById('detail-contact-meta').textContent = `状态: ${c.status||'active'} · ${_relTime(c.ts)}`;
    document.getElementById('detail-device-id').textContent = _detailDeviceId.slice(0, 8) || '未知';
    _loadDetailMessages(c);
  }

  function closeDetail() {
    const panel = document.getElementById('conv-detail-panel');
    if (panel) panel.style.display = 'none';
    _detailContact = null;
  }

  async function _loadDetailMessages(c) {
    const el = document.getElementById('detail-messages');
    if (!el) return;
    el.innerHTML = '<div style="color:var(--text-muted);font-size:12px;text-align:center;padding:20px">加载中...</div>';
    try {
      let interactions = [];
      if (c.lead_id) {
        const r = await api('GET', `/leads/${c.lead_id}`);
        interactions = r.interactions || [];
      }
      if (interactions.length === 0) {
        // Fallback: show from task results
        el.innerHTML = `
          <div style="background:var(--bg-main);border-radius:10px;padding:10px;max-width:85%;align-self:flex-end">
            <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">来自任务记录</div>
            <div style="font-size:12px">${_esc(c.last_message || '(无历史记录)')}</div>
          </div>
        `;
        return;
      }
      el.innerHTML = interactions.reverse().map(msg => {
        const isOut = msg.direction === 'outbound';
        const bg = isOut ? 'linear-gradient(135deg,var(--accent),#6366f1)' : 'var(--bg-main)';
        const color = isOut ? '#fff' : 'var(--text-main)';
        const align = isOut ? 'flex-end' : 'flex-start';
        return `
          <div style="display:flex;justify-content:${align}">
            <div style="background:${bg};color:${color};border-radius:10px;padding:8px 12px;max-width:85%;font-size:12px">
              <div>${_esc(msg.content || '')}</div>
              <div style="font-size:10px;opacity:.7;margin-top:3px">${_relTime(msg.created_at||'')} · ${isOut?'🤖 AI':'👤 用户'}</div>
            </div>
          </div>
        `;
      }).join('');
      el.scrollTop = el.scrollHeight;
    } catch(e) {
      el.innerHTML = `<div style="color:#ef4444;font-size:12px;padding:20px">加载失败: ${_esc(String(e))}</div>`;
    }
  }

  async function sendReply() {
    if (!_detailContact) return;
    const input = document.getElementById('detail-reply-input');
    const msg = input?.value?.trim();
    if (!msg) return;
    const contact = _detailContact.contact || '';
    const deviceId = _detailDeviceId;
    if (!contact || !deviceId) { showToast('缺少联系人或设备信息', 'error'); return; }
    try {
      input.disabled = true;
      const r = await api('POST', '/platforms/tiktok/tasks', {
        task_type: 'tiktok_send_dm',
        device_id: deviceId,
        params: { recipient: contact, message: msg }
      });
      if (r.ok || r.task_id) {
        showToast(`消息已排队发送给 ${contact}`, 'success');
        input.value = '';
        if (typeof _startTaskPoll === 'function') _startTaskPoll();
      } else {
        showToast('发送失败: ' + (r.detail || r.error || '未知错误'), 'error');
      }
    } catch(e) {
      showToast('发送失败: ' + e, 'error');
    } finally {
      if (input) input.disabled = false;
    }
  }

  async function dismissEscalation(idx) {
    if (!(await ocDialog({title:'确认删除',message:'确认删除该升级记录？',type:'danger',confirmText:'删除',dangerous:true}))) return;
    try {
      await api('DELETE', `/tiktok/escalation-queue/${idx}`);
      await _loadEscalations();
    } catch(e) {
      showToast('操作失败: ' + e,'error');
    }
  }

  async function _suggestReply(message, contact, deviceId) {
    const panel = document.getElementById('ai-suggest-panel');
    if (!panel) return;
    panel.innerHTML = '<span style="font-size:11px;color:var(--text-muted)">AI 生成中...</span>';
    try {
      const r = await api('POST', '/ai/suggest-reply', {message, contact});
      const suggs = r.suggestions || [];
      if (suggs.length === 0) throw new Error('no suggestions');
      panel.innerHTML = suggs.map(s => `
        <div style="display:flex;gap:6px;margin-bottom:5px;align-items:start">
          <div style="flex:1;font-size:11px;background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.2);border-radius:6px;padding:6px 8px;color:var(--text)">${_esc(s)}</div>
          <button onclick="Conv._sendSuggestion(${JSON.stringify(s)},${JSON.stringify(contact)},${JSON.stringify(deviceId)})"
                  style="flex-shrink:0;font-size:10px;padding:3px 8px;background:var(--accent);color:#fff;border:none;border-radius:5px;cursor:pointer">发送</button>
        </div>
      `).join('');
    } catch(e) {
      panel.innerHTML = `<div style="font-size:11px;color:#ef4444;padding:4px">生成失败，请重试</div>`;
    }
  }

  async function _sendSuggestion(message, contact, deviceId) {
    if (!contact || !deviceId) { showToast('缺少联系人或设备信息', 'error'); return; }
    try {
      const r = await api('POST', '/tasks', {
        type: 'tiktok_send_dm',
        device_id: deviceId,
        params: { recipient: contact, message }
      });
      if (r.task_id || r.ok) {
        showToast(`消息已排队发送给 ${contact}`, 'success');
      } else {
        showToast(r.detail || r.error || '发送失败', 'error');
      }
    } catch(e) {
      showToast('发送失败: ' + e, 'error');
    }
  }

  async function batchAutoReply() {
    const btn = document.getElementById('batch-reply-btn');
    if (btn) { btn.disabled = true; btn.textContent = '🤖 AI处理中...'; }
    try {
      // 先 dry_run 看有多少可处理的
      const preview = await api('POST', '/tiktok/batch-auto-reply', {dry_run: true, max_process: 10});
      const canProcess = preview.items ? preview.items.length : 0;
      if (canProcess === 0) {
        showToast('没有可处理的真实联系人（全部为占位符）', 'info');
        if (btn) { btn.disabled = false; btn.innerHTML = '&#129302; 批量AI回复'; }
        return;
      }
      // 执行真实处理
      const r = await api('POST', '/tiktok/batch-auto-reply', {max_process: 10});
      if (btn) { btn.disabled = false; btn.innerHTML = '&#129302; 批量AI回复'; }
      const processed = r.processed || 0;
      const skipped = r.skipped || 0;
      if (processed > 0) {
        showToast(`已为 ${processed} 个联系人排队AI回复，跳过 ${skipped} 个占位符`, 'success');
        setTimeout(() => { refresh(); }, 1500);
      } else {
        showToast(r.message || '未能处理任何联系人', 'info');
      }
    } catch(e) {
      if (btn) { btn.disabled = false; btn.innerHTML = '&#129302; 批量AI回复'; }
      showToast('批量回复失败: ' + e, 'error');
    }
  }

  async function clearEscalations() {
    if (!(await ocDialog({title:'清空升级记录',message:'确认清空所有升级记录？',type:'danger',confirmText:'清空',dangerous:true}))) return;
    try {
      await api('DELETE', '/tiktok/escalation-queue');
      await _loadEscalations();
    } catch(e) {
      showToast('操作失败: ' + e,'error');
    }
  }

  async function startDailyCampaign() {
    const country = await ocPrompt('目标国家','italy',{inputPlaceholder:'国家'});
    if(!country) return;
    if (!(await ocDialog({title:'启动今日运营计划',message:`目标国家: ${country}<br><br>将自动为所有在线设备提交:<br>1. 养号 (10分钟)<br>2. 关注用户<br>3. 检查收件箱 + AI回复<br>4. 回关发私信`,type:'info',confirmText:'启动',cancelText:'取消'}))) return;
    try {
      const btn = event.target;
      btn.disabled = true;
      btn.textContent = '启动中...';
      const r = await api('POST', '/tiktok/start-daily-campaign', {country});
      btn.disabled = false;
      btn.innerHTML = '&#9654; 一键启动今日运营';
      if (r.ok) {
        showToast(`已为 ${r.devices} 台设备提交 ${r.total_tasks} 个任务`, 'success');
        // Trigger task poll if available
        if (typeof _startTaskPoll === 'function') _startTaskPoll();
        if (typeof loadTasks === 'function') setTimeout(loadTasks, 1500);
      } else {
        showToast(r.error || '启动失败', 'error');
      }
    } catch(e) {
      showToast('启动失败: ' + e, 'error');
      const btn = document.querySelector('[onclick="Conv.startDailyCampaign()"]');
      if (btn) { btn.disabled = false; btn.innerHTML = '&#9654; 一键启动今日运营'; }
    }
  }

  function _startPoll() {
    if (_pollTimer) return;
    _pollTimer = setInterval(refresh, 15000);
  }

  function _stopPoll() {
    if (_pollTimer) { clearInterval(_pollTimer); _pollTimer = null; }
  }

  // Subscribe to WebSocket CustomEvents (dispatched by analytics.js _handleWsPush)
  // Fallback: SSE EventSource if WebSocket not available
  function _setupSSE() {
    if (_sseHandler) return;
    _sseHandler = (evt) => {
      try {
        const msg = evt.detail || JSON.parse(evt.data || '{}');
        const type = msg.type || '';
        if (type === 'tiktok.escalate_to_human') {
          // Toast + browser notify already handled in analytics.js
          // Just refresh the list if we're on this page
          const page = document.getElementById('page-conversations');
          if (page && page.classList.contains('active')) {
            _loadEscalations();
          }
          _updateBadge();
        }
        if (type === 'tiktok.inbox_checked') {
          _loadDailyStats();
          _loadConversations();
        }
      } catch(e) {}
    };
    // Primary: WebSocket CustomEvent (zero-latency, dispatched by analytics.js)
    window.addEventListener('oc:event', _sseHandler);
    // Fallback: SSE EventSource
    if (!window._sseEventHub) {
      try {
        window._sseEventHub = new EventSource('/events/stream');
        window._sseEventHub.addEventListener('message', (evt) => {
          try {
            const data = JSON.parse(evt.data);
            _sseHandler({detail: data});
          } catch(e) {}
        });
      } catch(e) {}
    }
  }

  async function _updateBadge() {
    try {
      const r = await api('GET', '/tiktok/escalation-queue');
      const cnt = (r.items||[]).length;
      const badge = document.getElementById('escalation-badge');
      if (badge) { badge.textContent = cnt; badge.style.display = cnt > 0 ? 'inline' : 'none'; }
    } catch(e) {}
  }

  // Helper: relative time
  function _relTime(ts) {
    if (!ts) return '';
    try {
      const d = new Date(ts);
      const diff = (Date.now() - d.getTime()) / 1000;
      if (diff < 60) return `${Math.floor(diff)}秒前`;
      if (diff < 3600) return `${Math.floor(diff/60)}分前`;
      if (diff < 86400) return `${Math.floor(diff/3600)}小时前`;
      return d.toLocaleDateString();
    } catch(e) { return ts; }
  }

  function _esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function _set(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }

  // ── Lead CRM Detail Modal ────────────────────────────────────────────────────
  async function showLeadDetail(leadId){
    // Remove any existing modal
    document.getElementById('lead-detail-modal')?.remove();
    // Show skeleton immediately
    const modal=document.createElement('div');
    modal.id='lead-detail-modal';
    modal.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,0.65);z-index:9999;display:flex;align-items:center;justify-content:center;backdrop-filter:blur(4px)';
    modal.innerHTML=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:24px;width:min(560px,96vw);max-height:85vh;overflow-y:auto;box-shadow:0 24px 60px rgba(0,0,0,0.5)">
      <div style="text-align:center;padding:40px;color:var(--text-muted)">加载中...</div></div>`;
    document.body.appendChild(modal);
    modal.addEventListener('click',e=>{if(e.target===modal)modal.remove();});

    try{
      // Parallel fetch lead + conversation data
      const [lead, conv] = await Promise.all([
        api('GET',`/leads/${leadId}`).catch(()=>null),
        api('GET',`/conversations/${leadId}`).catch(()=>null),
      ]);
      if(!lead){modal.remove();showToast('线索数据不存在','error');return;}

      const statusColors={new:'#94a3b8',contacted:'#3b82f6',responded:'#fbbf24',qualified:'#a78bfa',converted:'#22c55e',blacklisted:'#ef4444'};
      const statusLabels={new:'新线索',contacted:'已接触',responded:'已回复',qualified:'已合格',converted:'已转化',blacklisted:'已屏蔽'};
      const stColor=statusColors[lead.status]||'#94a3b8';
      const stLabel=statusLabels[lead.status]||lead.status;
      const score=Math.round(lead.score||0);
      const scoreColor=score>=70?'#22c55e':score>=40?'#fbbf24':'#94a3b8';
      const fsmState=conv?.state||'';
      const history=(conv?.transition_history||[]).slice(-5).reverse();
      const platforms=(lead.profiles||[]).map(p=>p.platform).join(', ')||lead.source_platform||'—';

      modal.querySelector('div>div').innerHTML=`
        <style>
          #lead-detail-modal .ld-row{display:flex;align-items:flex-start;gap:12px;padding:8px 0;border-bottom:1px solid rgba(255,255,255,.06)}
          #lead-detail-modal .ld-row:last-child{border-bottom:none}
          #lead-detail-modal .ld-label{font-size:11px;color:var(--text-dim);width:76px;flex-shrink:0;padding-top:2px}
        </style>
        <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:20px">
          <div>
            <div style="font-size:17px;font-weight:700;color:var(--text)">${_esc(lead.name||'未知')}</div>
            <div style="display:flex;gap:8px;margin-top:6px;flex-wrap:wrap">
              <span style="font-size:11px;background:${stColor}22;color:${stColor};padding:2px 8px;border-radius:4px;border:1px solid ${stColor}44">${stLabel}</span>
              ${fsmState?`<span style="font-size:11px;background:#3b82f622;color:#60a5fa;padding:2px 8px;border-radius:4px">FSM: ${fsmState}</span>`:''}
              <span style="font-size:11px;background:${scoreColor}22;color:${scoreColor};padding:2px 8px;border-radius:4px">评分 ${score}</span>
            </div>
          </div>
          <button onclick="document.getElementById('lead-detail-modal').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:20px;padding:4px;line-height:1">✕</button>
        </div>
        <div class="ld-row"><span class="ld-label">平台</span><span>${_esc(platforms)}</span></div>
        ${lead.company?`<div class="ld-row"><span class="ld-label">公司</span><span>${_esc(lead.company)}</span></div>`:''}
        ${lead.location?`<div class="ld-row"><span class="ld-label">地区</span><span>${_esc(lead.location)}</span></div>`:''}
        <div class="ld-row"><span class="ld-label">创建</span><span style="color:var(--text-dim)">${lead.created_at?new Date(lead.created_at).toLocaleString('zh-CN'):'—'}</span></div>
        ${lead.tags?.length?`<div class="ld-row"><span class="ld-label">标签</span><div style="display:flex;gap:4px;flex-wrap:wrap">${lead.tags.map(tag=>`<span style="font-size:10px;background:var(--bg-main);border:1px solid var(--border);padding:1px 6px;border-radius:3px">${_esc(tag)}</span>`).join('')}</div></div>`:''}
        ${conv?.message_count?`<div class="ld-row"><span class="ld-label">消息数</span><span style="color:#60a5fa">${conv.message_count} 条</span></div>`:''}
        ${history.length?`<div class="ld-row"><span class="ld-label">状态历史</span><div style="flex:1">${history.map(h=>`<div style="font-size:11px;color:var(--text-dim);padding:1px 0">→ ${_esc(h.to||h.state||String(h))}</div>`).join('')}</div></div>`:''}
        ${lead.notes?`<div class="ld-row"><span class="ld-label">备注</span><span style="color:var(--text-dim);font-size:12px">${_esc(lead.notes)}</span></div>`:''}
        <!-- Action buttons -->
        <div style="display:flex;gap:8px;margin-top:20px;flex-wrap:wrap">
          ${lead.status!=='qualified'&&lead.status!=='converted'?`<button class="qa-btn" style="color:#a78bfa;border-color:#a78bfa" onclick="_ldAction('qualify',${leadId})">⭐ 标记合格</button>`:''}
          ${lead.status!=='converted'?`<button class="qa-btn" style="color:#22c55e;border-color:#22c55e" onclick="_ldAction('convert',${leadId})">🎉 标记转化</button>`:''}
          ${lead.status!=='blacklisted'?`<button class="qa-btn" style="color:#f87171;border-color:#f87171" onclick="_ldAction('blacklist',${leadId})">🚫 屏蔽</button>`:''}
          <button class="qa-btn" style="margin-left:auto" onclick="document.getElementById('lead-detail-modal').remove()">关闭</button>
        </div>
      `;
    }catch(e){
      modal.querySelector('div>div').innerHTML=`<div style="padding:40px;text-align:center;color:#f87171">加载失败: ${_esc(e.message)}</div>
        <div style="text-align:center;margin-top:12px"><button class="qa-btn" onclick="document.getElementById('lead-detail-modal').remove()">关闭</button></div>`;
    }
  }

  async function _ldAction(action, leadId){
    const statusMap={qualify:'qualified',convert:'converted',blacklist:'blacklisted'};
    const newStatus=statusMap[action];
    if(!newStatus) return;
    try{
      await api('PUT',`/leads/${leadId}`,{status:newStatus});
      showToast(action==='qualify'?'已标记为合格线索':action==='convert'?'🎉 已标记为转化':' 已屏蔽','success');
      document.getElementById('lead-detail-modal')?.remove();
      // Refresh conversations list
      await _loadConversations();
    }catch(e){showToast('操作失败: '+e.message,'error');}
  }

  // SSE setup on DOMContentLoaded (page loaders are registered in overview.js)

  // Auto-start SSE subscription (for badge updates even when not on the page)
  document.addEventListener('DOMContentLoaded', () => {
    setTimeout(_updateBadge, 3000);
    // Poll badge every 30s regardless of active page
    setInterval(_updateBadge, 30000);
  });

  return { refresh, dismissEscalation, clearEscalations, startDailyCampaign,
           openDetail, closeDetail, sendReply, toggleMonitor, batchAutoReply,
           showLeadDetail, _ldAction, suggestReply, _sendSuggestion,
           _startPoll, _stopPoll, _setupSSE };
})();

// ── Leads page ─────────────────────────────────────────────────────────────────
const Leads = (() => {
  async function refresh() {
    try {
      const r = await api('GET', '/leads?limit=100');
      const leads = Array.isArray(r) ? r : (r.leads || r.items || []);
      const tbody = document.getElementById('leads-tbody');
      if (!tbody) return;
      if (leads.length === 0) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:20px">暂无线索数据</td></tr>';
        return;
      }
      tbody.innerHTML = leads.map(l => {
        const statusColor = l.status === 'converted' ? '#22c55e' : l.status === 'active' ? '#3b82f6' : '#94a3b8';
        return `<tr style="border-bottom:1px solid var(--border)">
          <td style="padding:7px 8px;font-size:12px">${_esc(l.username || l.name || l.id || '')}</td>
          <td style="padding:7px 8px;font-size:12px">${_esc(l.platform || 'tiktok')}</td>
          <td style="padding:7px 8px;font-size:12px">${_esc(l.intent || '-')}</td>
          <td style="padding:7px 8px">
            <span style="font-size:10px;background:${statusColor}22;color:${statusColor};border-radius:4px;padding:1px 6px">${_esc(l.status||'new')}</span>
          </td>
          <td style="padding:7px 8px;font-size:11px;color:var(--text-muted)">${_esc(l.last_interaction || l.updated_at || '')}</td>
        </tr>`;
      }).join('');
    } catch(e) {
      const tbody = document.getElementById('leads-tbody');
      if (tbody) tbody.innerHTML = `<tr><td colspan="5" style="text-align:center;color:#ef4444;padding:20px">加载失败: ${e}</td></tr>`;
    }
  }

  function _esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  return { refresh };
})();
