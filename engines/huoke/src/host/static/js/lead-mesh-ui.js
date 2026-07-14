// -*- coding: utf-8 -*-
/**
 * Lead Mesh Dashboard UI (Phase 5.5 · 2026-04-23)
 *
 * 三大视图:
 *   1. 接收方工作台 (lmOpenHandoffInbox)   — 按接收方账号聚合的待处理队列
 *   2. Lead 档案 / 时间轴 (lmOpenLeadSearch / lmOpenLeadDossier)
 *   3. 运营指挥台 (lmOpenCommandCenter)     — 漏斗 + 接收方负载 + 告警
 *
 * 全部 window.lm* 挂载, 纯 JS + innerHTML, 沿用 PlatShell 公共组件。
 */
(function () {
  'use strict';

  // ─── Shell 引用 + helpers ──────────────────────────────────────
  function _shell() {
    const s = window.PlatShell;
    if (!s) { showToast && showToast('PlatShell 未加载', 'error'); return null; }
    return s;
  }

  function _fmtTime(iso) {
    if (!iso) return '-';
    try {
      const d = new Date(iso.replace(' ', 'T').replace(/Z?$/, 'Z'));
      const now = new Date();
      const diffMin = Math.round((now - d) / 60000);
      if (diffMin < 60) return diffMin + ' 分钟前';
      if (diffMin < 1440) return Math.round(diffMin / 60) + ' 小时前';
      return Math.round(diffMin / 1440) + ' 天前';
    } catch (e) { return iso; }
  }

  function _safe(s) {
    return String(s == null ? '' : s).replace(/[<>&"']/g, function (c) {
      return { '<': '&lt;', '>': '&gt;', '&': '&amp;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function _lcColor(stage) {
    return {'new':'#64748b','contacted':'#3b82f6','engaged':'#8b5cf6',
            'qualified':'#22c55e','converted':'#f59e0b','lost':'#ef4444'}[stage||'new'] || '#64748b';
  }

  const _ACTION_ICON = {
    extracted: '🟢', friend_requested: '🤝', friend_accepted: '✅',
    friend_rejected: '❌',
    greeting_sent: '✉️', greeting_fallback: '🔄', greeting_replied: '💬',
    inbox_received: '📨', reply_sent: '📤',
    referral_sent: '🔗', referral_blocked: '🚫',
    handoff_created: '📤', handoff_acknowledged: '👀',
    handoff_completed: '✅', handoff_rejected: '❌', handoff_expired: '⏰',
    lead_merged: '🔀', lead_marked_duplicate: '🔗',
    human_intervention: '👤', risk_detected: '⚠️',
    note_added: '📝', intent_send_friend_request: '👋',
    intent_resend_greeting: '🔄', lifecycle_advanced: '📈',
  };
  const _ACTION_COLOR = {
    handoff_created: '#0ea5e9', handoff_completed: '#22c55e',
    handoff_rejected: '#ef4444', handoff_expired: '#f59e0b',
    risk_detected: '#ef4444', referral_blocked: '#ef4444',
    greeting_replied: '#22d3ee', referral_sent: '#a855f7',
    note_added: '#0ea5e9', lifecycle_advanced: '#8b5cf6',
  };

  const _STATE_COLOR = {
    pending: '#f59e0b', acknowledged: '#0ea5e9',
    completed: '#22c55e', rejected: '#ef4444',
    expired: '#64748b', duplicate_blocked: '#94a3b8',
  };

  const _STATE_LABEL_ZH = {
    pending: '待处理', acknowledged: '已确认', completed: '已完成',
    rejected: '已拒接', expired: '已过期', duplicate_blocked: '重复拦截',
  };


  // ─────────────────────────────────────────────────────────────────
  // P0 · 接收方工作台 (Handoff Inbox)
  // ─────────────────────────────────────────────────────────────────

  let _lmInboxState = { receiver: '', tab: 'pending', autoTimer: null };

  function _lmStopInboxAutoRefresh() {
    if (_lmInboxState.autoTimer) {
      clearInterval(_lmInboxState.autoTimer);
      _lmInboxState.autoTimer = null;
    }
  }

  window.lmOpenHandoffInbox = async function (receiverKey) {
    const Shell = _shell();
    if (!Shell) return;
    if (receiverKey) _lmInboxState.receiver = receiverKey;
    Shell.modal.open('lm-inbox-modal',
      '<div id="lm-inbox-body" style="padding:18px;">加载中…</div>',
      { maxWidth: '1100px' });
    await _lmRenderInbox();

    // 自动刷新: 每 30s 静默刷新一次, 让运营能看到新进来的 handoff。
    // 闭模态时 (DOM 不在了) 自动清理。
    _lmStopInboxAutoRefresh();
    _lmInboxState.autoTimer = setInterval(function () {
      const m = document.getElementById('lm-inbox-modal');
      if (!m) { _lmStopInboxAutoRefresh(); return; }
      // 静默刷新 — 如果用户正在 hover 某张卡片, 保留其 details 展开状态
      const openedDetails = new Set();
      document.querySelectorAll('#lm-inbox-body details[open]').forEach(function (el) {
        const card = el.closest('[id^="lm-card-"]');
        if (card) openedDetails.add(card.id);
      });
      _lmRenderInbox().then(function () {
        openedDetails.forEach(function (cid) {
          const card = document.getElementById(cid);
          if (card) {
            const d = card.querySelector('details');
            if (d) d.open = true;
          }
        });
      });
    }, 30000);
  };

  async function _lmRenderInbox() {
    const Shell = _shell();
    const body = document.getElementById('lm-inbox-body');
    if (!body) return;
    body.innerHTML = '加载中…';
    try {
      // 拉取所有状态以展示 Tab 计数
      const stateQs = '';  // 全量拉来分 tab
      const receiver = _lmInboxState.receiver;
      const recvQs = receiver ? ('&receiver_account_key=' + encodeURIComponent(receiver)) : '';
      const [p, a, c, r] = await Promise.all([
        Shell.api.get('/lead-mesh/handoffs?state=pending&limit=200' + recvQs),
        Shell.api.get('/lead-mesh/handoffs?state=acknowledged&limit=200' + recvQs),
        Shell.api.get('/lead-mesh/handoffs?state=completed&limit=100' + recvQs),
        Shell.api.get('/lead-mesh/handoffs?state=rejected&limit=100' + recvQs),
      ]);
      const pending = (p && p.handoffs) || [];
      const ack = (a && a.handoffs) || [];
      const completed = (c && c.handoffs) || [];
      const rejected = (r && r.handoffs) || [];

      const tab = _lmInboxState.tab;
      const list = tab === 'pending' ? pending
                 : tab === 'acknowledged' ? ack
                 : tab === 'completed' ? completed
                 : rejected;

      // 收集所有 receiver 的 key (去重) 供下拉
      const receiverSet = new Set();
      [pending, ack, completed, rejected].forEach(function (arr) {
        arr.forEach(function (h) {
          if (h.receiver_account_key) receiverSet.add(h.receiver_account_key);
        });
      });
      const receivers = Array.from(receiverSet).sort();

      const tabBtn = function (key, label, count, color) {
        const active = tab === key;
        return '<button onclick="lmSwitchInboxTab(\'' + key + '\')"'
          + ' style="padding:8px 16px;background:' + (active ? color : 'transparent')
          + ';color:' + (active ? '#fff' : 'var(--text)')
          + ';border:1px solid ' + color + ';border-radius:8px;font-size:13px;'
          + 'font-weight:' + (active ? '600' : '400') + ';cursor:pointer;margin-right:6px">'
          + label + ' <span style="font-size:11px;opacity:0.85">(' + count + ')</span></button>';
      };

      const receiverSelect = '<select id="lm-inbox-receiver" onchange="lmSwitchInboxReceiver(this.value)" '
        + 'style="background:var(--bg-card);border:1px solid var(--border);color:var(--text);'
        + 'padding:5px 10px;border-radius:6px;font-size:12px;min-width:180px">'
        + '<option value="">— 所有接收方 —</option>'
        + receivers.map(function (r) {
            return '<option value="' + _safe(r) + '"' + (r === receiver ? ' selected' : '') + '>' + _safe(r) + '</option>';
          }).join('')
        + '</select>';

      const cards = list.length === 0
        ? '<div style="text-align:center;padding:40px;color:var(--text-dim)">暂无 ' + _STATE_LABEL_ZH[tab] + ' 交接单</div>'
        : list.map(_lmHandoffCardHtml).join('');

      body.innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:18px;font-weight:700">🤝 接收方工作台</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        + '      下发到接收账号 → 标记 已看到 → 已接上 → 完成引流</div>'
        + '  </div>'
        + '  <button onclick="PlatShell.modal.close(\'lm-inbox-modal\')" '
        + '          style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + '<div style="display:flex;gap:10px;align-items:center;margin-bottom:14px;padding:10px 12px;'
        + '            background:var(--bg-main);border-radius:8px;flex-wrap:wrap">'
        + '  <span style="font-size:12px;color:var(--text-muted)">📬 接收账号:</span>'
        + receiverSelect
        + '  <span style="margin-left:auto;font-size:11px;color:var(--text-dim)">'
        + '    pending=' + pending.length + ' ack=' + ack.length + ' done=' + completed.length + '</span>'
        + '  <button onclick="lmRefreshInbox()" '
        + '          style="padding:5px 10px;background:rgba(96,165,250,.15);color:#60a5fa;border:1px solid rgba(96,165,250,.4);border-radius:6px;font-size:11px;cursor:pointer">🔄 刷新</button>'
        + '</div>'
        + '<div style="display:flex;margin-bottom:14px;flex-wrap:wrap;gap:4px">'
        + tabBtn('pending', _STATE_LABEL_ZH.pending, pending.length, _STATE_COLOR.pending)
        + tabBtn('acknowledged', _STATE_LABEL_ZH.acknowledged, ack.length, _STATE_COLOR.acknowledged)
        + tabBtn('completed', _STATE_LABEL_ZH.completed, completed.length, _STATE_COLOR.completed)
        + tabBtn('rejected', _STATE_LABEL_ZH.rejected, rejected.length, _STATE_COLOR.rejected)
        + '</div>'
        + '<div style="display:grid;gap:10px;max-height:60vh;overflow-y:auto">'
        + cards
        + '</div>';
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  }

  function _lmHandoffCardHtml(h) {
    const state = h.state || 'pending';
    const color = _STATE_COLOR[state] || '#60a5fa';
    const snap = h.conversation_snapshot || [];
    const snapCount = snap.length;
    const hid = h.handoff_id || '';
    const actions = (state === 'pending' || state === 'acknowledged')
      ? '<div style="display:flex;gap:6px;margin-top:8px;flex-wrap:wrap">'
        + (state === 'pending'
           ? '<button onclick="lmHandoffAction(\'' + hid + '\', \'acknowledge\')" '
             + 'style="padding:6px 14px;background:rgba(14,165,233,.15);color:#0ea5e9;border:1px solid rgba(14,165,233,.4);border-radius:6px;font-size:12px;cursor:pointer">👀 已看到</button>'
           : '')
        + '<button onclick="lmHandoffAction(\'' + hid + '\', \'complete\')" '
        + 'style="padding:6px 14px;background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.4);border-radius:6px;font-size:12px;cursor:pointer;font-weight:600">✅ 已接上</button>'
        + '<button onclick="lmHandoffAction(\'' + hid + '\', \'reject\')" '
        + 'style="padding:6px 14px;background:rgba(239,68,68,.1);color:#ef4444;border:1px solid rgba(239,68,68,.3);border-radius:6px;font-size:12px;cursor:pointer">❌ 拒接</button>'
        + '</div>'
      : '';

    // PR-6: 真人客服接管区 (assign / reply / note / outcome)
    const csAssigned = h.assigned_to_username || '';
    const csOutcome = h.outcome || '';
    const csReplies = h.customer_service_replies_json
      ? (function () { try { return JSON.parse(h.customer_service_replies_json).length; } catch (e) { return 0; } })() : 0;
    const csNotes = h.internal_notes_json
      ? (function () { try { return JSON.parse(h.internal_notes_json).length; } catch (e) { return 0; } })() : 0;

    const csButtons = csOutcome
      ? '<span style="font-size:11px;padding:3px 10px;border-radius:6px;background:'
          + (csOutcome === 'converted' ? 'rgba(34,197,94,.2);color:#22c55e' :
             csOutcome === 'lost' ? 'rgba(239,68,68,.15);color:#ef4444' :
             'rgba(245,158,11,.15);color:#f59e0b')
          + '">' + (csOutcome === 'converted' ? '✓ 成交' : csOutcome === 'lost' ? '× 流失' : '⏳ 待跟进') + '</span>'
      : (!csAssigned
        ? '<button onclick="lmCsAssign(\'' + hid + '\')" '
          + 'style="padding:5px 12px;background:rgba(168,85,247,.15);color:#a855f7;border:1px solid rgba(168,85,247,.4);border-radius:6px;font-size:11px;cursor:pointer;font-weight:600">🙋 我接手</button>'
        : '<span style="font-size:11px;color:var(--text-muted)">已被 <code>' + _safe(csAssigned) + '</code> 接管</span>'
          + '<button onclick="lmCsReply(\'' + hid + '\')" '
          + 'style="padding:5px 12px;background:rgba(96,165,250,.15);color:#60a5fa;border:1px solid rgba(96,165,250,.4);border-radius:6px;font-size:11px;cursor:pointer">💬 回复 (' + csReplies + ')</button>'
          + '<button onclick="lmCsNote(\'' + hid + '\')" '
          + 'style="padding:5px 12px;background:rgba(251,191,36,.15);color:#fbbf24;border:1px solid rgba(251,191,36,.4);border-radius:6px;font-size:11px;cursor:pointer">📝 备注 (' + csNotes + ')</button>'
          + '<button onclick="lmCsOutcome(\'' + hid + '\')" '
          + 'style="padding:5px 12px;background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.4);border-radius:6px;font-size:11px;cursor:pointer;font-weight:600">🏁 标记结果</button>');

    const csRow = !csOutcome || csOutcome
      ? '<div style="display:flex;gap:6px;align-items:center;margin-top:8px;padding-top:8px;border-top:1px dashed var(--border);flex-wrap:wrap">'
        + '<span style="font-size:11px;color:var(--text-dim)">真人客服:</span>' + csButtons + '</div>'
      : '';

    return ''
      + '<div id="lm-card-' + hid + '" style="background:var(--bg-main);border:1px solid var(--border);'
      + '  border-left:4px solid ' + color + ';border-radius:10px;padding:14px">'
      + '  <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:10px">'
      + '    <div style="flex:1;min-width:0">'
      + '      <div style="font-weight:600;font-size:14px;margin-bottom:4px">'
      + '        📌 ' + _safe(hid.substring(0, 12)) + '…'
      + '        <span style="margin-left:8px;font-size:11px;padding:2px 8px;background:rgba(0,0,0,.2);border-radius:4px;color:' + color + '">'
      +            _STATE_LABEL_ZH[state] + '</span>'
      + '      </div>'
      + '      <div style="font-size:12px;color:var(--text-muted);margin-bottom:4px">'
      + '        来自 <code>' + _safe(h.source_agent) + '</code>'
      +          (h.source_device ? '@<code>' + _safe(h.source_device.substring(0, 8)) + '</code>' : '')
      + '        · 渠道 <b style="color:#0ea5e9">' + _safe(h.channel) + '</b>'
      + '        · 接收方 <code>' + _safe(h.receiver_account_key || '未指派') + '</code>'
      + '      </div>'
      + '      <div style="font-size:11px;color:var(--text-dim)">'
      + '        🕒 ' + _fmtTime(h.created_at) + ' · 聊天 ' + snapCount + ' 轮'
      + '      </div>'
      + '      <details style="margin-top:8px;font-size:12px">'
      + '        <summary style="cursor:pointer;color:#60a5fa">💬 展开聊天 + 引流内容</summary>'
      + '        <div style="margin-top:8px;padding:10px;background:var(--bg-card);border-radius:6px">'
      + '          <div style="font-size:11px;color:var(--text-dim);margin-bottom:6px">引流话术:</div>'
      + '          <div style="padding:6px 10px;background:rgba(168,85,247,.1);border-radius:4px;font-size:12px;margin-bottom:8px;white-space:pre-wrap">'
      +              _safe(h.snippet_sent || '(无)') + '</div>'
      + '          <div style="font-size:11px;color:var(--text-dim);margin-bottom:6px">最近对话 (已脱敏):</div>'
      +            snap.map(function (t) {
                     const dir = t.direction === 'outgoing' ? '→' : '←';
                     const dcol = t.direction === 'outgoing' ? '#22d3ee' : '#f59e0b';
                     const txt = t.text || t.message_text || '';
                     return '<div style="padding:4px 0;font-size:11px">'
                       + '<span style="color:' + dcol + ';font-weight:600">' + dir + '</span> '
                       + _safe(txt) + '</div>';
                   }).join('')
      + '        </div>'
      + '      </details>'
      + '    </div>'
      + '    <button onclick="lmOpenLeadDossier(\'' + _safe(h.canonical_id) + '\')" '
      + '            style="padding:5px 10px;background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:6px;font-size:11px;cursor:pointer;white-space:nowrap">'
      + '      🔍 Lead 档案</button>'
      + '  </div>'
      + actions
      + csRow
      + '</div>';
  }

  // ── Phase-2: 客服个人工作台 lmOpenMyDesk ────────────────────────
  window.lmOpenMyDesk = async function () {
    const Shell = _shell();
    if (!Shell) return;
    let username = localStorage.getItem('oc_user') || localStorage.getItem('oc_cs_id') || '';
    if (!username) {
      username = await ocPrompt('客服 ID','agent_001',{inputPlaceholder:'客服 ID'});
      if (!username) return;
      localStorage.setItem('oc_cs_id', username);
    }
    Shell.modal.open('lm-mydesk-modal',
      '<div id="lm-mydesk-body" style="padding:18px">加载中…</div>',
      { maxWidth: '900px' });
    await _lmRenderMyDesk(username);

    // 自动刷新 30s
    if (window._lmMyDeskTimer) clearInterval(window._lmMyDeskTimer);
    window._lmMyDeskTimer = setInterval(function () {
      if (!document.getElementById('lm-mydesk-modal')) {
        clearInterval(window._lmMyDeskTimer);
        return;
      }
      _lmRenderMyDesk(username);
    }, 30000);
  };

  async function _lmRenderMyDesk(username) {
    const body = document.getElementById('lm-mydesk-body');
    if (!body) return;
    try {
      const resp = await _shell().api.get('/lead-mesh/handoffs/assigned/' +
        encodeURIComponent(username));
      const handoffs = (resp && resp.handoffs) || [];
      const cnt = handoffs.length;

      const header = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<div>'
        + '<div style="font-size:18px;font-weight:700">👤 我的工作台 — ' + _safe(username) + '</div>'
        + '<div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        + '我正在跟进的客户 (' + cnt + ' 个) · 红色 = 超过 5 分钟未操作</div>'
        + '</div>'
        + '<button onclick="PlatShell.modal.close(\'lm-mydesk-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 12px;border-radius:6px;cursor:pointer">关闭 ✕</button>'
        + '</div>';

      if (cnt === 0) {
        body.innerHTML = header + '<div style="text-align:center;padding:50px;color:var(--text-dim)">'
          + '✅ 当前没有正在跟进的客户<br>'
          + '<span style="font-size:12px">去 <a href="javascript:void(0)" onclick="PlatShell.modal.close(\'lm-mydesk-modal\');lmOpenHandoffInbox(\'\')" style="color:#a855f7">📥 待接管队列</a> 接新客户</span>'
          + '</div>';
        return;
      }

      const cards = handoffs.map(function (h) {
        const assignedAt = h.assigned_at ? new Date(h.assigned_at) : null;
        const ageMin = assignedAt ? Math.floor((Date.now() - assignedAt.getTime()) / 60000) : 0;
        const isOverdue = ageMin >= 5;
        let replies = [];
        let notes = [];
        try { replies = JSON.parse(h.customer_service_replies_json || '[]'); } catch (e) {}
        try { notes = JSON.parse(h.internal_notes_json || '[]'); } catch (e) {}
        const lastReply = replies.length > 0 ? replies[replies.length - 1] : null;

        const ageColor = isOverdue ? '#ef4444' : '#94a3b8';
        const cardBorder = isOverdue ? '#ef4444' : 'var(--border)';

        return ''
          + '<div style="background:var(--bg-main);border:1px solid ' + cardBorder + ';border-radius:10px;padding:14px;margin-bottom:10px">'
          + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
          + '<div style="font-weight:600;font-size:14px">'
          + '📌 ' + _safe(h.handoff_id.substring(0, 12)) + '… · '
          + '<span style="font-size:11px;color:var(--text-muted)">渠道 ' + _safe(h.channel) + '</span>'
          + '</div>'
          + '<div style="font-size:11px;color:' + ageColor + ';font-weight:' + (isOverdue ? '700' : '400') + '">'
          + (isOverdue ? '🔥 ' : '🕒 ') + ageMin + ' 分钟前接的'
          + '</div>'
          + '</div>'
          + '<div style="font-size:11px;color:var(--text-dim);margin-bottom:10px">'
          + 'lead <code>' + _safe((h.canonical_id || '').substring(0, 8)) + '</code>'
          + ' · ' + replies.length + ' 条回复 · ' + notes.length + ' 条备注'
          + (lastReply ? ' · <i>last: "' + _safe((lastReply.text || '').substring(0, 40)) + '..."</i>' : '')
          + '</div>'
          + '<div style="display:flex;gap:6px;flex-wrap:wrap">'
          + '<button onclick="lmCsReply(\'' + h.handoff_id + '\')" style="padding:5px 12px;background:rgba(96,165,250,.15);color:#60a5fa;border:1px solid rgba(96,165,250,.4);border-radius:6px;font-size:11px;cursor:pointer">💬 回复</button>'
          + '<button onclick="lmCsNote(\'' + h.handoff_id + '\')" style="padding:5px 12px;background:rgba(251,191,36,.15);color:#fbbf24;border:1px solid rgba(251,191,36,.4);border-radius:6px;font-size:11px;cursor:pointer">📝 备注</button>'
          + '<button onclick="lmCsOutcome(\'' + h.handoff_id + '\')" style="padding:5px 12px;background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.4);border-radius:6px;font-size:11px;cursor:pointer;font-weight:600">🏁 标记结果</button>'
          + '<button onclick="lmOpenLeadDossier(\'' + _safe(h.canonical_id) + '\')" style="margin-left:auto;padding:5px 10px;background:none;border:1px solid var(--border);color:var(--text-muted);border-radius:6px;font-size:11px;cursor:pointer">🔍 档案</button>'
          + '</div>'
          + '</div>';
      }).join('');

      body.innerHTML = header + cards;
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:14px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  }

  // ── PR-6.5+: 真 modal 替代 prompt() (轻量内联实现) ──────────────
  function _lmCsCurrentUser() {
    // 优先 dashboard 登录的 username, 然后 localStorage, 最后弹 modal 让用户输
    try {
      const stored = localStorage.getItem('oc_user') || localStorage.getItem('oc_cs_id') || '';
      if (stored) return stored;
      const me = window.PlatShell && window.PlatShell.session && window.PlatShell.session.user;
      if (me && me.username) return me.username;
    } catch (e) {}
    return localStorage.getItem('oc_cs_id') || '';
  }

  /* 通用 modal: title + 字段定义 + 提交回调.
     fields: [{name, label, type:text|textarea|select|info, value, required, options:[]}]
     onSubmit(data) 是 async, 异常会显示在 modal 内. */
  function _lmModal(title, fields, onSubmit) {
    return new Promise(function (resolve) {
      const overlay = document.createElement('div');
      overlay.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,.7);'
        + 'z-index:99999;display:flex;align-items:center;justify-content:center;padding:10px;'
        + 'overflow-y:auto';
      const isMobile = window.innerWidth < 600;
      const card = document.createElement('div');
      card.style.cssText = 'background:#1e293b;border:1px solid #475569;border-radius:'
        + (isMobile ? '10px' : '14px') + ';'
        + 'width:100%;max-width:' + (isMobile ? '100%' : '520px')
        + ';box-shadow:0 24px 60px rgba(0,0,0,.6);overflow:hidden;'
        + 'max-height:' + (isMobile ? '95vh' : 'auto') + ';overflow-y:auto';
      let bodyHtml = '<div style="padding:18px 22px;border-bottom:1px solid #334155;display:flex;justify-content:space-between;align-items:center">'
        + '<h3 style="margin:0;font-size:16px;font-weight:600;color:#e2e8f0">' + _safe(title) + '</h3>'
        + '<button data-act="close" style="background:none;border:1px solid #475569;color:#cbd5e1;padding:4px 12px;border-radius:6px;cursor:pointer">关闭 ✕</button>'
        + '</div><form data-form="1" style="padding:22px"><div data-error style="display:none;margin-bottom:12px;padding:10px 14px;background:rgba(239,68,68,.12);border:1px solid rgba(239,68,68,.4);border-radius:8px;color:#f87171;font-size:12px"></div>';
      fields.forEach(function (f) {
        if (f.type === 'info') {
          bodyHtml += '<div style="font-size:12px;color:#94a3b8;margin-bottom:12px;padding:10px;background:rgba(148,163,184,.06);border-radius:6px">' + (f.html || _safe(f.value || '')) + '</div>';
          return;
        }
        bodyHtml += '<div style="margin-bottom:14px">'
          + '<label style="display:block;font-size:12px;color:#94a3b8;margin-bottom:6px">' + _safe(f.label) + (f.required ? ' <span style="color:#ef4444">*</span>' : '') + '</label>';
        if (f.type === 'textarea') {
          bodyHtml += '<textarea name="' + _safe(f.name) + '" rows="4" '
            + 'style="width:100%;background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:9px 12px;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical">'
            + _safe(f.value || '') + '</textarea>';
        } else if (f.type === 'select') {
          bodyHtml += '<select name="' + _safe(f.name) + '" '
            + 'style="width:100%;background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:10px 12px;border-radius:8px;font-size:13px">';
          (f.options || []).forEach(function (opt) {
            bodyHtml += '<option value="' + _safe(opt.value) + '"' + (opt.value === f.value ? ' selected' : '') + '>' + _safe(opt.label) + '</option>';
          });
          bodyHtml += '</select>';
        } else {
          bodyHtml += '<input name="' + _safe(f.name) + '" type="text" '
            + 'value="' + _safe(f.value || '') + '" '
            + (f.placeholder ? 'placeholder="' + _safe(f.placeholder) + '" ' : '')
            + 'style="width:100%;background:#0f172a;border:1px solid #334155;color:#e2e8f0;padding:10px 12px;border-radius:8px;font-size:13px"/>';
        }
        if (f.hint) {
          bodyHtml += '<div style="font-size:11px;color:#64748b;margin-top:4px">' + _safe(f.hint) + '</div>';
        }
        bodyHtml += '</div>';
      });
      bodyHtml += '<div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">'
        + '<button type="button" data-act="cancel" style="padding:9px 18px;background:none;border:1px solid #475569;color:#cbd5e1;border-radius:8px;cursor:pointer;font-size:13px">取消</button>'
        + '<button type="submit" data-act="submit" style="padding:9px 22px;background:#a855f7;border:none;color:#fff;border-radius:8px;cursor:pointer;font-size:13px;font-weight:600">提交</button>'
        + '</div></form>';
      card.innerHTML = bodyHtml;
      overlay.appendChild(card);
      document.body.appendChild(overlay);

      const form = card.querySelector('[data-form]');
      const errBox = card.querySelector('[data-error]');
      function close(result) {
        document.body.removeChild(overlay);
        resolve(result);
      }
      card.querySelector('[data-act="close"]').onclick = function () { close(null); };
      card.querySelector('[data-act="cancel"]').onclick = function () { close(null); };
      overlay.onclick = function (e) { if (e.target === overlay) close(null); };
      form.onsubmit = async function (e) {
        e.preventDefault();
        const data = {};
        fields.forEach(function (f) {
          if (f.type === 'info') return;
          const el = form.querySelector('[name="' + f.name + '"]');
          data[f.name] = el ? el.value : '';
        });
        for (let i = 0; i < fields.length; i++) {
          const f = fields[i];
          if (f.required && !((data[f.name] || '').trim())) {
            errBox.textContent = '"' + f.label + '" 必填';
            errBox.style.display = '';
            return;
          }
        }
        errBox.style.display = 'none';
        try {
          if (onSubmit) await onSubmit(data);
          close(data);
        } catch (err) {
          errBox.textContent = '提交失败: ' + (err && err.message || err);
          errBox.style.display = '';
        }
      };
      // 自动 focus 第一个 input
      setTimeout(function () {
        const f = card.querySelector('input, textarea, select');
        if (f) f.focus();
      }, 100);
    });
  }

  // ── PR-6 真人客服 4 个动作 (改成真 modal) ──────────────────────
  window.lmCsAssign = async function (handoffId) {
    const Shell = _shell();
    if (!Shell) return;
    await _lmModal('🙋 接手客户', [
      { type: 'info', html: '接管后 worker 会自动暂停对该客户的 AI 自动回复, 由你手动回. 标"成交/流失"后释放.' },
      { name: 'username', label: '客服 ID (username)', value: _lmCsCurrentUser() || 'agent_001', required: true, hint: '会记录到 lead_journey.actor = "human:<你>"' },
      { name: 'peer_name', label: '客户姓名 (peer_name)', placeholder: '从聊天卡片可看到, 例: Yumi Tanaka', hint: '空 = 不调 ai_takeover_state.mark_taken_over (不推荐)' },
      { name: 'device_id', label: '设备 ID (device_id)', placeholder: '例: 4HUSIB4TBQC69TJZ', hint: '客户在哪台手机上接触的, 留空跟 peer_name 同步留空' },
    ], async function (data) {
      try { localStorage.setItem('oc_cs_id', data.username); } catch (e) {}
      await Shell.api.post('/lead-mesh/handoffs/' + handoffId + '/assign', data);
      showToast('✅ 接管成功' + ((data.peer_name && data.device_id) ? ' (AI 已暂停)' : ''), 'success');
      _lmRenderInbox();
    });
  };

  window.lmCsReply = async function (handoffId) {
    const Shell = _shell();
    if (!Shell) return;
    await _lmModal('💬 回复客户', [
      { type: 'info', html: '输入要发给客户的话. <b>勾选下面"真发"</b> 时会通过 worker 用对应物理手机发出 (PR-6.6); 否则只本地记录.' },
      { name: 'username', label: '你的客服 ID', value: _lmCsCurrentUser() || 'agent_001', required: true },
      { name: 'text', label: '消息内容', type: 'textarea', required: true, placeholder: '例: もちろんです、よかったらLINEでもっとお話しませんか？' },
      { name: 'sent_via_worker', label: '是否真发', type: 'select', value: 'false',
        options: [
          { value: 'false', label: '只记录 (不真发)' },
          { value: 'true', label: '真发到客户手机 (需 worker listener)' },
        ] },
    ], async function (data) {
      try { localStorage.setItem('oc_cs_id', data.username); } catch (e) {}
      await Shell.api.post('/lead-mesh/handoffs/' + handoffId + '/reply', {
        username: data.username, text: data.text,
        sent_via_worker: data.sent_via_worker === 'true',
      });
      showToast('💬 ' + (data.sent_via_worker === 'true' ? '已通过 worker 发出' : '已本地记录'), 'success');
      _lmRenderInbox();
    });
  };

  window.lmCsNote = async function (handoffId) {
    const Shell = _shell();
    if (!Shell) return;
    await _lmModal('📝 加内部备注', [
      { type: 'info', html: '内部备注不发给客户, 给同事 / 主管看' },
      { name: 'username', label: '你的客服 ID', value: _lmCsCurrentUser() || 'agent_001', required: true },
      { name: 'note', label: '备注内容', type: 'textarea', required: true,
        placeholder: '例: 客户提到孩子, 关注亲子话题, 优先' },
    ], async function (data) {
      try { localStorage.setItem('oc_cs_id', data.username); } catch (e) {}
      await Shell.api.post('/lead-mesh/handoffs/' + handoffId + '/note', data);
      showToast('📝 备注已记录', 'success');
      _lmRenderInbox();
    });
  };

  window.lmCsOutcome = async function (handoffId) {
    const Shell = _shell();
    if (!Shell) return;
    await _lmModal('🏁 标记客户结果', [
      { type: 'info', html: '<b>成交 / 流失</b>: 终态, 自动释放 AI 接管 (worker 重新接手 AI 自动回).<br><b>待跟进</b>: 保留接管态, 后续继续跟.' },
      { name: 'username', label: '你的客服 ID', value: _lmCsCurrentUser() || 'agent_001', required: true },
      { name: 'outcome', label: '结果', type: 'select', required: true, value: 'converted',
        options: [
          { value: 'converted', label: '✅ 成交 (converted)' },
          { value: 'lost', label: '❌ 流失 (lost)' },
          { value: 'pending_followup', label: '⏳ 待跟进 (pending_followup)' },
        ] },
      { name: 'notes', label: '备注 / 原因', type: 'textarea',
        placeholder: '例: 客户加 LINE 后真买了; 或: 客户失联' },
      { name: 'peer_name', label: '客户姓名 (释放 AI 用)', hint: '终态时填了会自动释放 ai_takeover_state' },
      { name: 'device_id', label: '设备 ID (释放 AI 用)' },
    ], async function (data) {
      try { localStorage.setItem('oc_cs_id', data.username); } catch (e) {}
      await Shell.api.post('/lead-mesh/handoffs/' + handoffId + '/outcome', data);
      const final = data.outcome === 'converted' || data.outcome === 'lost';
      showToast('🏁 已标 ' + data.outcome + (final ? ' (AI 接管已释放)' : ''), 'success');
      _lmRenderInbox();
    });
  };

  window.lmSwitchInboxTab = function (tab) {
    _lmInboxState.tab = tab;
    _lmRenderInbox();
  };
  window.lmSwitchInboxReceiver = function (r) {
    _lmInboxState.receiver = r;
    _lmRenderInbox();
  };
  window.lmRefreshInbox = function () { _lmRenderInbox(); };

  window.lmHandoffAction = async function (handoffId, action) {
    const Shell = _shell();
    if (!Shell) return;
    const actionLabel = {acknowledge: '标记已看到', complete: '标记已接上', reject: '拒接'}[action] || action;
    if (!(await ocDialog({title:actionLabel,message:'确认 ' + actionLabel + ' handoff ' + handoffId.substring(0, 12) + '… ?',type:'info',confirmText:'确认',cancelText:'取消'}))) return;
    try {
      await Shell.api.post('/lead-mesh/handoffs/' + handoffId + '/' + action,
                            { by: 'human:dashboard' });
      showToast(actionLabel + ' 成功', 'success');
      _lmRenderInbox();
    } catch (e) {
      showToast(actionLabel + ' 失败: ' + (e.message || e), 'error');
    }
  };


  // ─────────────────────────────────────────────────────────────────
  // P1 · Lead 档案搜索 + 时间轴
  // ─────────────────────────────────────────────────────────────────

  window.lmOpenLeadSearch = async function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-search-modal', ''
      + '<div style="padding:18px">'
      + '  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
      + '    <div style="font-size:18px;font-weight:700">🔍 Lead 档案搜索</div>'
      + '    <button onclick="PlatShell.modal.close(\'lm-search-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
      + '  </div>'
      + '  <div style="display:flex;gap:10px;margin-bottom:14px;flex-wrap:wrap">'
      + '    <input id="lm-search-name" placeholder="名字(模糊)…" '
      + '           onkeydown="if(event.key===\'Enter\')lmDoSearch()" '
      + '           style="flex:1;min-width:180px;padding:8px 12px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px">'
      + '    <select id="lm-search-platform" style="padding:8px 12px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px">'
      + '      <option value="">所有平台</option>'
      + '      <option value="facebook">Facebook</option>'
      + '      <option value="line">LINE</option>'
      + '      <option value="whatsapp">WhatsApp</option>'
      + '      <option value="telegram">Telegram</option>'
      + '      <option value="instagram">Instagram</option>'
      + '    </select>'
      + '    <select id="lm-search-lifecycle" style="padding:8px 12px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px">'
      + '      <option value="">所有阶段</option>'
      + '      <option value="new">新建</option>'
      + '      <option value="contacted">已触达</option>'
      + '      <option value="engaged">互动中</option>'
      + '      <option value="qualified">合格</option>'
      + '      <option value="converted">已转化</option>'
      + '      <option value="lost">流失</option>'
      + '    </select>'
      + '    <input id="lm-search-account" placeholder="账号 id (模糊)…" '
      + '           onkeydown="if(event.key===\'Enter\')lmDoSearch()" '
      + '           style="min-width:160px;padding:8px 12px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:13px">'
      + '    <button onclick="lmDoSearch()" '
      + '            style="padding:8px 20px;background:#0ea5e9;color:#fff;border:none;border-radius:6px;font-weight:600;cursor:pointer">搜索</button>'
      + '    <button onclick="lmExportCsv()" '
      + '            style="padding:8px 14px;background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.4);border-radius:6px;font-size:12px;cursor:pointer">📥 导出CSV</button>'
      + '  </div>'
      + '  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px;align-items:center">'
      + '    <input id="lm-search-tags" placeholder="标签过滤 (逗号分隔)…" style="min-width:130px;padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:12px">'
      + '    <span style="font-size:11px;color:var(--text-dim)">Score:</span>'
      + '    <input id="lm-search-score-min" type="number" placeholder="最低" min="0" max="100" style="width:56px;padding:6px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:12px">'
      + '    <span style="font-size:11px;color:var(--text-dim)">~</span>'
      + '    <input id="lm-search-score-max" type="number" placeholder="最高" min="0" max="100" style="width:56px;padding:6px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:12px">'
      + '    <select id="lm-search-sort" style="padding:6px 8px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:12px">'
      + '      <option value="">最近更新</option>'
      + '      <option value="score_desc">分数高→低</option>'
      + '      <option value="score_asc">分数低→高</option>'
      + '      <option value="created_at">创建时间</option>'
      + '    </select>'
      + '  </div>'
      + '  <div id="lm-search-results" style="max-height:60vh;overflow-y:auto"></div>'
      + '</div>',
      { maxWidth: '900px' });
    setTimeout(function () {
      const el = document.getElementById('lm-search-name');
      if (el) el.focus();
    }, 150);
  };

  window.lmDoSearch = async function () {
    const Shell = _shell();
    if (!Shell) return;
    const name = (document.getElementById('lm-search-name') || {}).value || '';
    const platform = (document.getElementById('lm-search-platform') || {}).value || '';
    const account = (document.getElementById('lm-search-account') || {}).value || '';
    const lifecycle = (document.getElementById('lm-search-lifecycle') || {}).value || '';
    const box = document.getElementById('lm-search-results');
    if (!box) return;
    box.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-dim)">搜索中…</div>';
    try {
      const qs = [];
      if (name) qs.push('name_like=' + encodeURIComponent(name));
      if (platform) qs.push('platform=' + encodeURIComponent(platform));
      if (account) qs.push('account_id_like=' + encodeURIComponent(account));
      if (lifecycle) qs.push('lifecycle_stage=' + encodeURIComponent(lifecycle));
      var tags = (document.getElementById('lm-search-tags') || {}).value || '';
      var scoreMin = (document.getElementById('lm-search-score-min') || {}).value || '';
      var scoreMax = (document.getElementById('lm-search-score-max') || {}).value || '';
      var sortBy = (document.getElementById('lm-search-sort') || {}).value || '';
      if (tags) qs.push('tags=' + encodeURIComponent(tags));
      if (scoreMin !== '') qs.push('score_min=' + parseInt(scoreMin, 10));
      if (scoreMax !== '') qs.push('score_max=' + parseInt(scoreMax, 10));
      if (sortBy) qs.push('sort_by=' + encodeURIComponent(sortBy));
      const r = await Shell.api.get('/lead-mesh/leads/search?' + qs.join('&'));
      const results = (r && r.results) || [];
      if (results.length === 0) {
        box.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-dim)">无匹配</div>';
        return;
      }
      // Y3: 批量操作 bar
      var batchBar = '<div id="lm-batch-bar" style="display:none;margin-bottom:8px;padding:8px 12px;background:rgba(14,165,233,.1);border:1px solid rgba(14,165,233,.3);border-radius:8px;display:none;align-items:center;gap:8px;flex-wrap:wrap">'
        + '<span id="lm-batch-count" style="font-size:11px;color:#0ea5e9;font-weight:600">已选 0</span>'
        + '<select id="lm-batch-stage" style="padding:4px 6px;font-size:11px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
        + '<option value="">推进阶段…</option><option value="contacted">contacted</option><option value="engaged">engaged</option>'
        + '<option value="qualified">qualified</option><option value="converted">converted</option><option value="lost">lost</option></select>'
        + '<button onclick="lmBatchAction(\'lifecycle\')" style="padding:4px 10px;font-size:10px;background:#0ea5e920;color:#0ea5e9;border:1px solid #0ea5e940;border-radius:4px;cursor:pointer">推进</button>'
        + '<input id="lm-batch-tag" placeholder="标签名" style="width:80px;padding:4px 6px;font-size:11px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
        + '<button onclick="lmBatchAction(\'tag_add\')" style="padding:4px 10px;font-size:10px;background:#22c55e20;color:#22c55e;border:1px solid #22c55e40;border-radius:4px;cursor:pointer">+标签</button>'
        + '<button onclick="lmBatchAction(\'tag_remove\')" style="padding:4px 10px;font-size:10px;background:#ef444420;color:#ef4444;border:1px solid #ef444440;border-radius:4px;cursor:pointer">-标签</button>'
        + '</div>';
      box.innerHTML = batchBar + '<div style="display:grid;gap:8px">'
        + results.map(function (r) {
            return ''
              + '<div style="display:flex;align-items:center;gap:10px;padding:12px 16px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;transition:border-color .15s"'
              + '     onmouseover="this.style.borderColor=\'#0ea5e9\'" '
              + '     onmouseout="this.style.borderColor=\'var(--border)\'">'
              + '<input type="checkbox" class="lm-batch-cb" value="' + _safe(r.canonical_id) + '" onchange="lmUpdateBatchBar()" style="cursor:pointer;width:16px;height:16px">'
              + '<div onclick="lmOpenLeadDossier(\'' + _safe(r.canonical_id) + '\')" style="flex:1;cursor:pointer">'
              + '  <div style="display:flex;justify-content:space-between">'
              + '    <div>'
              + '      <div style="font-weight:600">' + _safe(r.primary_name || '(无名)') + '</div>'
              + '      <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
              +         'cid <code>' + _safe(r.canonical_id.substring(0, 12)) + '…</code>'
              +         ' · lang:' + _safe(r.primary_language || '?')
              +         ' · persona:' + _safe(r.primary_persona_key || '?')
              +         ' · <span style="padding:1px 6px;border-radius:3px;font-size:10px;background:' + _lcColor(r.lifecycle_stage) + '20;color:' + _lcColor(r.lifecycle_stage) + '">' + _safe(r.lifecycle_stage || 'new') + '</span>'
              +         (r.lead_score!=null ? ' · <span style="font-weight:700;color:' + (r.lead_score>=70?'#22c55e':r.lead_score>=40?'#f59e0b':'#ef4444') + '">⭐' + r.lead_score + '</span>' : '')
              +         (r.tags ? ' · <span style="font-size:9px;color:var(--text-dim)">' + _safe(r.tags.substring(0,40)) + '</span>' : '')
              +         '</div>'
              + '    </div>'
              + '    <div style="font-size:11px;color:var(--text-dim)">' + _fmtTime(r.created_at) + '</div>'
              + '  </div>'
              + '</div></div>';
          }).join('') + '</div>';
    } catch (e) {
      box.innerHTML = '<div style="color:#ef4444;padding:20px">搜索失败: ' + _safe(e.message || e) + '</div>';
    }
  };

  window.lmUpdateBatchBar = function () {
    var cbs = document.querySelectorAll('.lm-batch-cb:checked');
    var bar = document.getElementById('lm-batch-bar');
    var cnt = document.getElementById('lm-batch-count');
    if (!bar) return;
    if (cbs.length > 0) {
      bar.style.display = 'flex';
      if (cnt) cnt.textContent = '已选 ' + cbs.length;
    } else {
      bar.style.display = 'none';
    }
  };

  window.lmBatchAction = async function (type) {
    var Shell = _shell();
    if (!Shell) return;
    var cbs = document.querySelectorAll('.lm-batch-cb:checked');
    var ids = [];
    cbs.forEach(function(cb){ ids.push(cb.value); });
    if (!ids.length) { if (typeof showToast==='function') showToast('请先勾选 lead','error'); return; }
    try {
      if (type === 'lifecycle') {
        var stage = (document.getElementById('lm-batch-stage')||{}).value || '';
        if (!stage) { if (typeof showToast==='function') showToast('请选择目标阶段','error'); return; }
        var res = await Shell.api.post('/lead-mesh/leads/lifecycle/batch',
          {canonical_ids: ids, stage: stage});
        if (typeof showToast==='function') showToast('批量推进 ' + (res.updated||ids.length) + ' 条','success');
      } else if (type === 'tag_add' || type === 'tag_remove') {
        var tag = (document.getElementById('lm-batch-tag')||{}).value || '';
        if (!tag.trim()) { if (typeof showToast==='function') showToast('请输入标签名','error'); return; }
        var res2 = await Shell.api.post('/lead-mesh/leads/tags/batch',
          {canonical_ids: ids, action: type==='tag_add'?'add':'remove', tag: tag.trim()});
        if (typeof showToast==='function') showToast('批量标签 ' + (res2.updated||ids.length) + ' 条','success');
      }
      lmDoSearch(); // 刷新结果
    } catch (e) {
      if (typeof showToast==='function') showToast('批量操作失败: ' + (e.message||e),'error');
    }
  };

  window.lmExportCsv = function () {
    var lifecycle = (document.getElementById('lm-search-lifecycle') || {}).value || '';
    var qs = [];
    if (lifecycle) qs.push('lifecycle_stage=' + encodeURIComponent(lifecycle));
    qs.push('format=csv');
    window.open('/lead-mesh/leads/export?' + qs.join('&'), '_blank');
  };

  window.lmRunAuditFix = async function () {
    var Shell = _shell();
    if (!Shell) return;
    try {
      var res = await Shell.api.get('/lead-mesh/leads/audit?auto_fix=true');
      var f = res.fixed || {};
      var msg = '修复完成: 删除孤儿 ' + (f.orphan_deleted||0) + ', 重置状态 ' + (f.lifecycle_reset||0);
      if (typeof showToast === 'function') showToast(msg, 'success');
      setTimeout(function(){ lmOpenIdentityKPI(); }, 500);
    } catch (e) {
      if (typeof showToast === 'function') showToast('修复失败: ' + (e.message||e), 'error');
    }
  };

  window.lmRetryDeadLetter = async function (id) {
    var Shell = _shell();
    if (!Shell) return;
    try {
      var res = await Shell.api.post('/lead-mesh/webhooks/' + id + '/retry');
      if (res && res.ok) {
        if (typeof showToast === 'function') showToast('已重新入队 #' + id, 'success');
        setTimeout(function(){ lmOpenIdentityKPI(); }, 500);
      } else {
        if (typeof showToast === 'function') showToast('重试失败', 'error');
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('重试失败: ' + (e.message||e), 'error');
    }
  };

  window.lmFilterTimeline = function () {
    var sel = document.getElementById('lm-timeline-filter');
    var box = document.getElementById('lm-timeline-box');
    if (!sel || !box || !window._lm_dossier_journey) return;
    var filter = sel.value;
    var filtered = filter
      ? window._lm_dossier_journey.filter(function(j){ return j.action === filter; })
      : window._lm_dossier_journey;
    if (!filtered.length) {
      box.innerHTML = '<div style="text-align:center;padding:20px;color:var(--text-dim)">无匹配事件</div>';
      return;
    }
    box.innerHTML = _lmTimelineHtml(filtered);
  };

  window.lmSearchByStage = function (stage) {
    // X1: 从漏斗图点击 → 打开搜索并预填 lifecycle 阶段
    lmOpenSearchModal();
    setTimeout(function() {
      var sel = document.getElementById('lm-search-lifecycle');
      if (sel) { sel.value = stage; lmDoSearch(); }
    }, 200);
  };

  window.lmAddNote = async function (cid) {
    var Shell = _shell();
    if (!Shell) return;
    var inp = document.getElementById('lm-note-input');
    var text = (inp && inp.value || '').trim();
    if (!text) return;
    inp.disabled = true;
    try {
      var res = await Shell.api.post('/lead-mesh/leads/' + encodeURIComponent(cid) + '/notes',
        { text: text });
      if (res && res.ok) {
        if (typeof showToast === 'function') showToast('备注已添加', 'success');
        setTimeout(function(){ lmOpenLeadDossier(cid); }, 300);
      } else {
        if (typeof showToast === 'function') showToast('添加失败', 'error');
        inp.disabled = false;
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('添加失败: ' + (e.message||e), 'error');
      inp.disabled = false;
    }
  };

  window.lmQuickAction = async function (cid, action, params, btnEl) {
    var Shell = _shell();
    if (!Shell) return;
    if (btnEl) { btnEl.disabled = true; btnEl.textContent = '…'; }
    try {
      var res = await Shell.api.post('/lead-mesh/leads/' + encodeURIComponent(cid) + '/quick-action',
        { action: action, params: params || {} });
      if (res && res.ok) {
        if (typeof showToast === 'function') showToast(action + ' 成功', 'success');
        // 刷新 dossier
        setTimeout(function(){ lmOpenLeadDossier(cid); }, 300);
      } else {
        if (typeof showToast === 'function') showToast((res && res.error) || '操作失败', 'error');
        if (btnEl) { btnEl.disabled = false; btnEl.textContent = '执行'; }
      }
    } catch (e) {
      if (typeof showToast === 'function') showToast('执行失败: ' + (e.message||e), 'error');
      if (btnEl) { btnEl.disabled = false; btnEl.textContent = '执行'; }
    }
  };

  window.lmOpenLeadDossier = async function (canonicalId) {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-dossier-modal',
      '<div id="lm-dossier-body" style="padding:18px">加载中…</div>',
      { maxWidth: '1000px' });
    const body = document.getElementById('lm-dossier-body');
    try {
      const d = await Shell.api.get('/lead-mesh/leads/' + encodeURIComponent(canonicalId) + '?journey_limit=200');
      const canonical = d.canonical || {};
      const identities = d.identities || [];
      const journey = d.journey || [];
      const handoffs = d.handoffs || [];
      const summary = d.journey_summary || {};

      // 身份列表
      const idPlatformIcon = { facebook: '📘', line: '💬', whatsapp: '📱',
                                telegram: '✈️', instagram: '📷', messenger: '💬' };
      const idsHtml = identities.map(function (i) {
        return '<span style="display:inline-block;padding:4px 10px;background:rgba(96,165,250,.12);border-radius:4px;font-size:11px;margin:2px">'
          + (idPlatformIcon[i.platform] || '🔗') + ' ' + _safe(i.platform)
          + ': <code>' + _safe(i.account_id) + '</code>'
          + (i.verified ? '' : ' <span style="color:#f59e0b">(soft)</span>')
          + '</span>';
      }).join('');

      // 时间轴 (按天分组 - Phase 6 UX)
      const timeline = _lmTimelineHtml(journey);

      // 统计
      const statsHtml = Object.entries(summary.by_action || {}).sort(function (a, b) { return b[1] - a[1]; })
        .slice(0, 8)
        .map(function (kv) {
          return '<span style="display:inline-block;padding:2px 8px;background:var(--bg-card);border-radius:4px;font-size:11px;margin:2px">'
            + (_ACTION_ICON[kv[0]] || '') + ' ' + _safe(kv[0]) + ' ×' + kv[1] + '</span>';
        }).join('');

      body.innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:18px;font-weight:700">📋 ' + _safe(canonical.primary_name || '(无名)') + '</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        + '      <code>' + _safe(canonical.canonical_id) + '</code>'
        +        (canonical.merged_into ? ' <span style="color:#f59e0b">已合并 → ' + _safe(canonical.merged_into.substring(0, 12)) + '</span>' : '')
        + (function(){ var sc = (canonical.metadata||{}).lead_score; if(sc==null) return '';
             var c = sc>=70?'#22c55e':sc>=40?'#f59e0b':'#ef4444';
             return ' · <span style="padding:1px 6px;border-radius:10px;font-size:10px;font-weight:700;background:'+c+'20;color:'+c+'">⭐ '+sc+'</span>'; })()
        + '    </div>'
        + '  </div>'
        + '  <div style="display:flex;gap:8px">'
        + '    <button onclick="lmMergeToSearch(\'' + _safe(canonical.canonical_id) + '\',\'' + _safe(canonical.primary_name || '') + '\')" style="background:rgba(251,191,36,.12);color:#eab308;border:1px solid rgba(251,191,36,.3);padding:4px 10px;border-radius:6px;cursor:pointer;font-size:11px">🔗 合并到…</button>'
        + '    <button onclick="PlatShell.modal.close(\'lm-dossier-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '  </div>'
        + '</div>'
        // P1: lifecycle progress bar + transition timeline
        + (function() {
          var stg = canonical.lifecycle_stage || 'new';
          var stages = ['new','contacted','engaged','qualified','converted'];
          var labels = {'new':'新建','contacted':'触达','engaged':'互动','qualified':'合格','converted':'转化','lost':'流失'};
          var colors = {'new':'#64748b','contacted':'#3b82f6','engaged':'#8b5cf6','qualified':'#22c55e','converted':'#f59e0b','lost':'#ef4444'};
          var idx = stages.indexOf(stg);
          if (stg === 'lost') idx = -1;
          var bar = '<div style="margin-bottom:14px;padding:10px 14px;background:rgba(255,255,255,.03);border:1px solid var(--border);border-radius:8px">'
            + '<div style="display:flex;align-items:center;gap:2px;margin-bottom:6px">';
          stages.forEach(function(s, i) {
            var active = (i <= idx);
            var isCurrent = (s === stg);
            bar += '<div style="flex:1;height:6px;border-radius:3px;background:'
              + (active ? colors[s] : 'rgba(255,255,255,.08)') + ';position:relative'
              + (isCurrent ? ';box-shadow:0 0 6px ' + colors[s] : '') + '"></div>';
            if (i < stages.length - 1) bar += '<div style="width:3px"></div>';
          });
          bar += '</div><div style="display:flex;justify-content:space-between">';
          stages.forEach(function(s) {
            bar += '<span style="font-size:9px;color:' + (s===stg ? colors[s] : 'var(--text-dim)') + ';font-weight:' + (s===stg?'700':'400') + '">' + labels[s] + '</span>';
          });
          bar += '</div>';
          if (stg === 'lost') bar += '<div style="margin-top:4px;font-size:10px;color:#ef4444;font-weight:600">⚠ 已流失</div>';
          // lifecycle transitions from journey
          var lcEvents = journey.filter(function(j){ return j.action === 'lifecycle_advanced'; });
          if (lcEvents.length > 0) {
            bar += '<div style="margin-top:8px;border-top:1px solid var(--border);padding-top:6px">'
              + '<div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">变迁记录:</div>';
            lcEvents.slice(-6).forEach(function(ev) {
              var dd = ev.data || {};
              bar += '<div style="font-size:10px;color:var(--text-main);margin-bottom:2px">'
                + '<span style="color:' + (colors[dd.from]||'#64748b') + '">' + (labels[dd.from]||dd.from||'?') + '</span>'
                + ' → <span style="color:' + (colors[dd.to]||'#64748b') + '">' + (labels[dd.to]||dd.to||'?') + '</span>'
                + ' <span style="color:var(--text-dim)">' + _fmtTime(ev.created_at) + '</span></div>';
            });
            bar += '</div>';
          }
          return bar + '</div>';
        })()
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">🔗 跨平台身份 ('
        +        identities.length + ')</div>'
        + '    <div>' + (idsHtml || '<span style="color:var(--text-dim)">无</span>') + '</div>'
        + '  </div>'
        + '  <div>'
        + '    <div style="font-size:12px;color:var(--text-muted);margin-bottom:6px">📊 事件分布 (top 8)</div>'
        + '    <div>' + statsHtml + '</div>'
        + '    <div style="font-size:11px;color:var(--text-dim);margin-top:4px">当前 owner: <code>'
        +         _safe(d.current_owner || '-') + '</code></div>'
        + '  </div>'
        + '</div>'
        + (function() {
          // X2: 事件筛选下拉
          window._lm_dossier_journey = journey;
          var actionSet = {};
          journey.forEach(function(j){ actionSet[j.action] = 1; });
          var actKeys = Object.keys(actionSet).sort();
          var opts = '<option value="">全部事件 (' + journey.length + ')</option>';
          actKeys.forEach(function(a) {
            opts += '<option value="' + _safe(a) + '">' + (_ACTION_ICON[a]||'•') + ' ' + _safe(a) + '</option>';
          });
          return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">'
            + '<div style="font-size:12px;color:var(--text-muted)">⏱ 时间轴 (' + journey.length + ' 事件)</div>'
            + '<select id="lm-timeline-filter" onchange="lmFilterTimeline()" style="padding:3px 6px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px;font-size:11px">'
            + opts + '</select></div>';
        })()
        + '<div id="lm-timeline-box" style="max-height:50vh;overflow-y:auto;background:var(--bg-main);padding:10px;border-radius:8px">'
        + (timeline || '<div style="text-align:center;padding:20px;color:var(--text-dim)">无事件</div>')
        + '</div>'
        + (handoffs.length ? ''
          + '<div style="margin-top:14px;font-size:12px;color:var(--text-muted);margin-bottom:6px">🤝 交接记录 ('
          + handoffs.length + ')</div>'
          + '<div style="display:grid;gap:6px">'
          + handoffs.map(function (h) {
              return '<div style="padding:8px 12px;background:var(--bg-main);border-left:3px solid '
                + (_STATE_COLOR[h.state] || '#64748b')
                + ';border-radius:6px;font-size:11px">'
                + '<b>' + _safe(h.channel) + '</b> → '
                + _safe(h.receiver_account_key || '未指派')
                + ' · <span style="color:' + (_STATE_COLOR[h.state] || '#64748b') + '">' + _STATE_LABEL_ZH[h.state] + '</span>'
                + ' · ' + _fmtTime(h.created_at)
                + ' · <code style="color:var(--text-dim)">' + _safe(h.handoff_id.substring(0, 12)) + '</code>'
                + '</div>';
            }).join('')
          + '</div>' : '')
        // W4: 运营备注输入
        + '<div style="margin-top:14px;display:flex;gap:8px;align-items:stretch">'
        + '  <input id="lm-note-input" placeholder="添加运营备注…" style="flex:1;padding:8px 12px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:6px;font-size:12px"'
        + '    onkeydown="if(event.key===\'Enter\')lmAddNote(\'' + _safe(canonical.canonical_id) + '\')">'
        + '  <button onclick="lmAddNote(\'' + _safe(canonical.canonical_id) + '\')"'
        + '    style="padding:8px 16px;background:rgba(14,165,233,.1);color:#0ea5e9;border:1px solid rgba(14,165,233,.3);border-radius:6px;font-size:12px;cursor:pointer;font-weight:600;white-space:nowrap">📝 添加</button>'
        + '</div>'
        // T3+U1: 智能推荐 + 一键执行
        + (function() {
          var cid = canonical.canonical_id;
          var stg = (canonical.lifecycle_stage || 'new');
          var sc = ((canonical.metadata||{}).lead_score) || 0;
          var hasReply = journey.some(function(j){ return j.action==='greeting_replied'||j.action==='message_received'||j.action==='friend_accepted'; });
          var hasLine = journey.some(function(j){ return j.action==='line_dispatch_planned'; });
          var hasWa = journey.some(function(j){ return j.action==='wa_referral_sent'; });
          // recs: {icon, text, pri, action?, params?}  action 存在时显示执行按钮
          var recs = [];
          if (stg==='new') {
            recs.push({icon:'👋', text:'发送好友请求', pri:'high', action:'log_intent', params:{intent:'send_friend_request'}});
          } else if (stg==='contacted') {
            if (!hasReply) recs.push({icon:'🔄', text:'重发问候', pri:'high', action:'log_intent', params:{intent:'resend_greeting'}});
            else recs.push({icon:'📱', text:'发送 WA referral', pri:'medium', action:'create_handoff', params:{channel:'whatsapp'}});
          } else if (stg==='engaged') {
            if (!hasWa) recs.push({icon:'📱', text:'发送 WA referral', pri:'high', action:'create_handoff', params:{channel:'whatsapp'}});
            if (!hasLine && sc>=50) recs.push({icon:'💬', text:'分配 LINE', pri:'medium', action:'allocate_line', params:{}});
          } else if (stg==='qualified') {
            if (!hasLine) recs.push({icon:'💬', text:'分配 LINE', pri:'high', action:'allocate_line', params:{}});
            recs.push({icon:'🏁', text:'确认转化', pri:'medium', action:'advance_lifecycle', params:{stage:'converted'}});
          } else if (stg==='converted') {
            recs.push({icon:'✅', text:'已转化', pri:'low'});
          } else if (stg==='lost') {
            if (sc>=40) recs.push({icon:'🔙', text:'尝试挽回', pri:'medium', action:'advance_lifecycle', params:{stage:'contacted',force:true}});
            else recs.push({icon:'❌', text:'已流失', pri:'low'});
          }
          if (!recs.length) return '';
          var priColor = {high:'#ef4444',medium:'#f59e0b',low:'#64748b'};
          var h = '<div style="margin-top:14px;padding:10px 14px;background:rgba(14,165,233,.06);border:1px solid rgba(14,165,233,.2);border-radius:8px">'
            + '<div style="font-size:12px;font-weight:600;color:#0ea5e9;margin-bottom:6px">💡 推荐操作</div>';
          recs.forEach(function(r, idx) {
            var btnId = 'lm-qa-btn-' + idx;
            h += '<div style="font-size:11px;margin-bottom:4px;display:flex;align-items:center;gap:6px">'
              + '<span style="font-size:13px">' + r.icon + '</span>'
              + '<span style="color:var(--text-main);flex:1">' + _safe(r.text) + '</span>';
            if (r.action) {
              h += '<button id="' + btnId + '" onclick="lmQuickAction(\'' + _safe(cid) + '\',\'' + _safe(r.action) + '\',' + JSON.stringify(r.params||{}).replace(/"/g,'&quot;') + ',this)" '
                + 'style="padding:3px 10px;font-size:10px;border-radius:4px;cursor:pointer;border:1px solid ' + priColor[r.pri] + ';background:' + priColor[r.pri] + '15;color:' + priColor[r.pri] + ';font-weight:600">执行</button>';
            }
            h += '<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:' + priColor[r.pri] + '20;color:' + priColor[r.pri] + '">' + r.pri + '</span></div>';
          });
          return h + '</div>';
        })();
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  };


  // ─────────────────────────────────────────────────────────────────
  // P2 · 运营指挥台
  // ─────────────────────────────────────────────────────────────────

  // Phase 8d/8g: Command Center 过滤器状态 (保留在 window 作用域, 切换时重渲染)
  //   date (Phase 8g): 点 sparkline 某天后, 所有 funnel API 加 date=X 下钻
  window._lmCCFilter = window._lmCCFilter || { days: 7, actor: 'agent_a', date: '' };

  // Phase 10-lite: 自动刷新 state — setInterval ID + 暂停标志 + 最后刷新时间戳
  window._lmCCRefresh = window._lmCCRefresh || {
    intervalId: null,
    paused: false,
    intervalMs: 15000,
    lastRefreshAt: 0,
  };

  window.lmOpenCommandCenter = async function () {
    const Shell = _shell();
    if (!Shell) return;
    // 每次 modal open 视为新 session, 清 toast dedup 让当前所有告警都能弹一次
    window._lmCCToastKeys = {};
    Shell.modal.open('lm-cc-modal',
      '<div id="lm-cc-body" style="padding:18px">加载中…</div>',
      { maxWidth: '980px' });
    await _lmRenderCommandCenter();
    // Phase 10-lite: 启动 15s 轮询自动刷新
    _lmCCStartAutoRefresh();
  };

  function _lmCCStartAutoRefresh() {
    const state = window._lmCCRefresh;
    if (state.intervalId) return;  // 已启动
    state.intervalId = setInterval(function () {
      if (state.paused) return;
      // modal 不存在时自动停 (用户已关闭)
      if (!document.getElementById('lm-cc-body')) {
        _lmCCStopAutoRefresh();
        return;
      }
      _lmRenderCommandCenter();
    }, state.intervalMs);
  }

  function _lmCCStopAutoRefresh() {
    const state = window._lmCCRefresh;
    if (state.intervalId) {
      clearInterval(state.intervalId);
      state.intervalId = null;
    }
  }

  // Phase 10-lite: 暂停 / 继续切换
  window.lmCCToggleRefresh = function () {
    const state = window._lmCCRefresh;
    state.paused = !state.paused;
    // 刷新按钮图标 (不整屏重渲染, 只改 button state)
    const btn = document.getElementById('lm-cc-refresh-btn');
    if (btn) {
      btn.innerHTML = state.paused ? '▶ 继续刷新' : '⏸ 暂停刷新';
      btn.title = state.paused
        ? '当前已暂停 15s 自动刷新, 点击恢复'
        : '暂停自动刷新 (保留当前截图观察)';
    }
  };

  // Phase 8d/8g: 过滤器 change handler (供 select / sparkline click 调用)
  window.lmCCSetFilter = async function (kind, val) {
    const f = window._lmCCFilter;
    if (kind === 'days') { f.days = parseInt(val) || 7; f.date = ''; }
    else if (kind === 'actor') f.actor = val || '';
    else if (kind === 'date') f.date = val || '';
    const body = document.getElementById('lm-cc-body');
    if (body) body.innerHTML = '<div style="padding:18px">加载中…</div>';
    await _lmRenderCommandCenter();
  };

  // Phase 8g: sparkline 点 circle → 设 date 过滤重渲染
  window.lmCCDrillDate = async function (date) {
    await window.lmCCSetFilter('date', date);
  };

  async function _lmRenderCommandCenter() {
    const Shell = _shell();
    const body = document.getElementById('lm-cc-body');
    if (!body) return;
    const f = window._lmCCFilter;
    const funnelUrl = '/lead-mesh/funnel?days=' + f.days
      + (f.actor ? '&actor=' + encodeURIComponent(f.actor) : '')
      + (f.date ? '&date=' + encodeURIComponent(f.date) : '');
    try {
      // 时序始终用 days (date 下钻时不展示 sparkline 因为单点无意义)
      const tsUrl = '/lead-mesh/funnel/timeseries?days=' + f.days
        + (f.actor ? '&actor=' + encodeURIComponent(f.actor) : '');
      const [pending, ack, completed, rejected, dead, receivers, funnel, timeseries] = await Promise.all([
        Shell.api.get('/lead-mesh/handoffs?state=pending&limit=500'),
        Shell.api.get('/lead-mesh/handoffs?state=acknowledged&limit=500'),
        Shell.api.get('/lead-mesh/handoffs?state=completed&limit=500'),
        Shell.api.get('/lead-mesh/handoffs?state=rejected&limit=500'),
        Shell.api.get('/lead-mesh/webhooks/dead-letters?limit=100'),
        Shell.api.get('/lead-mesh/receivers?with_load=true&enabled_only=true'),
        Shell.api.get(funnelUrl),
        Shell.api.get(tsUrl),
      ]);
      const pn = (pending.handoffs || []).length;
      const an = (ack.handoffs || []).length;
      const cn = (completed.handoffs || []).length;
      const rn = (rejected.handoffs || []).length;
      const total = pn + an + cn + rn;
      const deadN = (dead.dead_letters || []).length;

      // 接收方负载
      const rvLoad = {};
      [].concat(pending.handoffs || [], ack.handoffs || []).forEach(function (h) {
        const k = h.receiver_account_key || '(未指派)';
        rvLoad[k] = (rvLoad[k] || 0) + 1;
      });
      // 按渠道分组 - 每渠道分别算各 state 数, 转化率 = completed/(total excl. pending)
      // (excluding pending 因为 pending 还没定结果, 算进分母拉低实际转化数据)
      const chStats = { pending: {}, ack: {}, completed: {}, rejected: {} };
      (pending.handoffs || []).forEach(function (h) {
        chStats.pending[h.channel] = (chStats.pending[h.channel] || 0) + 1;
      });
      (ack.handoffs || []).forEach(function (h) {
        chStats.ack[h.channel] = (chStats.ack[h.channel] || 0) + 1;
      });
      (completed.handoffs || []).forEach(function (h) {
        chStats.completed[h.channel] = (chStats.completed[h.channel] || 0) + 1;
      });
      (rejected.handoffs || []).forEach(function (h) {
        chStats.rejected[h.channel] = (chStats.rejected[h.channel] || 0) + 1;
      });

      const funnelBar = function (label, count, color) {
        const pct = total > 0 ? Math.round(count * 100 / total) : 0;
        return ''
          + '<div style="margin-bottom:8px">'
          + '  <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:2px">'
          + '    <span>' + label + '</span>'
          + '    <span style="color:' + color + ';font-weight:600">' + count + ' (' + pct + '%)</span>'
          + '  </div>'
          + '  <div style="height:10px;background:rgba(255,255,255,.05);border-radius:4px;overflow:hidden">'
          + '    <div style="width:' + pct + '%;height:100%;background:' + color + '"></div>'
          + '  </div>'
          + '</div>';
      };

      // 接收方负载 — 优先走 receivers API (含 cap/percent), 回退到 rvLoad 计数
      const rvList = (receivers && receivers.receivers) || [];
      const atRiskReceivers = [];   // 收集 ≥90% 的, 稍后弹 toast
      const rvRows = rvList.length > 0
        ? rvList.map(function (r) {
            const cap = r.cap || r.daily_cap || 0;
            const cur = r.current || 0;
            const pct = cap > 0 ? Math.round(cur * 100 / cap) : 0;
            const barColor = pct >= 90 ? '#ef4444' : pct >= 60 ? '#f59e0b' : '#22c55e';
            const atRisk = pct >= 90;
            if (atRisk) atRiskReceivers.push(r.key + '(' + pct + '%)');
            const rowStyle = atRisk
              ? 'border:1px solid #ef4444;background:rgba(239,68,68,.06);animation:lmPulseRed 2s ease-in-out infinite'
              : 'background:var(--bg-main)';
            const nameHtml = atRisk
              ? '<b style="color:#ef4444">⚠ ' + _safe(r.key) + '</b>'
              : '<code>' + _safe(r.key) + '</code>';
            return ''
              + '<div style="padding:6px 10px;border-radius:4px;margin-bottom:4px;font-size:12px;' + rowStyle + '">'
              + '  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:3px">'
              + '    <span>' + nameHtml
              + '      <span style="color:var(--text-dim);font-size:10px;margin-left:4px">' + _safe(r.channel || '') + '</span></span>'
              + '    <span style="color:' + barColor + ';font-weight:600">' + cur + ' / ' + cap
              + '    <span style="font-size:10px">(' + pct + '%)</span></span>'
              + '  </div>'
              + '  <div style="height:5px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden">'
              + '    <div style="width:' + pct + '%;height:100%;background:' + barColor + '"></div>'
              + '  </div>'
              + '</div>';
          }).join('')
        : Object.entries(rvLoad).sort(function (a, b) { return b[1] - a[1]; })
            .map(function (kv) {
              return '<div style="display:flex;justify-content:space-between;padding:6px 10px;background:var(--bg-main);border-radius:4px;margin-bottom:4px;font-size:12px">'
                + '<code>' + _safe(kv[0]) + '</code>'
                + '<span style="color:#f59e0b">' + kv[1] + ' 待/已确认</span></div>';
            }).join('');

      const allChannels = {};
      ['pending', 'ack', 'completed', 'rejected'].forEach(function (s) {
        Object.keys(chStats[s]).forEach(function (ch) { allChannels[ch] = 1; });
      });
      const chRows = Object.keys(allChannels).sort()
        .map(function (ch) {
          const p = chStats.pending[ch] || 0;
          const a = chStats.ack[ch] || 0;
          const c = chStats.completed[ch] || 0;
          const rj = chStats.rejected[ch] || 0;
          // 转化率: completed / (completed + rejected + ack)
          // pending 不算分母 (结果未定), ack 算已投递但未完成
          const resolved = c + rj + a;
          const rate = resolved > 0 ? Math.round(c * 100 / resolved) : 0;
          const rateColor = rate >= 60 ? '#22c55e' : rate >= 30 ? '#f59e0b' : '#ef4444';
          return '<tr>'
            + '<td style="padding:6px 10px"><b>' + _safe(ch) + '</b></td>'
            + '<td style="padding:6px 10px;color:#f59e0b;text-align:right">' + p + '</td>'
            + '<td style="padding:6px 10px;color:#22c55e;text-align:right">' + c + '</td>'
            + '<td style="padding:6px 10px;color:' + rateColor + ';text-align:right;font-weight:600">'
            + (resolved > 0 ? rate + '%' : '-')
            + '<span style="color:var(--text-dim);font-size:10px;font-weight:400"> (' + c + '/' + resolved + ')</span>'
            + '</td></tr>';
        }).join('');

      const deadHtml = deadN === 0
        ? '<div style="color:#22c55e;font-size:12px">✓ 无失败 webhook</div>'
        : '<div style="color:#ef4444;font-size:12px;margin-bottom:6px">⚠ ' + deadN + ' 条死信</div>'
          + '<button onclick="lmViewDeadLetters()" '
          + 'style="padding:5px 12px;background:rgba(239,68,68,.12);color:#ef4444;border:1px solid rgba(239,68,68,.3);border-radius:6px;font-size:11px;cursor:pointer">查看 / 重试</button>';

      // Phase 8b: A 端获客漏斗 (从 /lead-mesh/funnel 拿到)
      const fu = funnel || {};
      const fuTotal = fu.total_extracted || 0;
      const fuFr = fu.total_friend_requested || 0;
      const fuGs = fu.total_greeting_sent || 0;
      const fuGb = fu.total_greeting_blocked || 0;
      const fuRate = Math.round(((fu.rate_greet_after_friend || 0) * 100));
      const fuInline = fu.greeting_via_inline || 0;
      const fuFallback = fu.greeting_via_fallback || 0;
      const fuUnknown = fu.greeting_via_unknown || 0;
      const fuRateColor = fuRate >= 50 ? '#22c55e' : fuRate >= 25 ? '#f59e0b' : '#ef4444';
      const topBlocked = (fu.top_blocked_reason || '').trim();
      // persona 分布 top 3
      const perPersona = fu.per_persona_friend_requested || {};
      const personaEntries = Object.entries(perPersona)
        .sort(function (a, b) { return b[1] - a[1]; }).slice(0, 3);
      const personaHtml = personaEntries.length === 0
        ? '<span style="color:var(--text-dim);font-size:11px">暂无</span>'
        : personaEntries.map(function (kv) {
            return '<span style="display:inline-block;padding:2px 8px;background:rgba(96,165,250,.12);'
              + 'color:#60a5fa;border-radius:10px;font-size:11px;margin-right:6px">'
              + _safe(kv[0]) + ': ' + kv[1] + '</span>';
          }).join('');

      const funnelNumber = function (label, n, color) {
        return '<div style="flex:1;text-align:center">'
          + '  <div style="font-size:22px;font-weight:700;color:' + color + '">' + n + '</div>'
          + '  <div style="font-size:11px;color:var(--text-dim);margin-top:2px">' + label + '</div>'
          + '</div>';
      };

      // Phase 8d 过滤器: days + actor select
      const ff = window._lmCCFilter;
      const daysOpts = [1, 3, 7, 14, 30].map(function (v) {
        return '<option value="' + v + '"'
          + (v === ff.days ? ' selected' : '') + '>'
          + (v === 1 ? '24 小时' : v + ' 天') + '</option>';
      }).join('');
      const actorOpts = [
        { v: 'agent_a', label: 'A 端' },
        { v: 'agent_b', label: 'B 端' },
        { v: '', label: '全部' },
      ].map(function (o) {
        return '<option value="' + o.v + '"'
          + (o.v === ff.actor ? ' selected' : '') + '>'
          + o.label + '</option>';
      }).join('');
      // Phase 8g: date 下钻时显示 chip, 点 × 清除回到 days 窗口
      const dateChipHtml = ff.date
        ? '  <span style="display:inline-flex;align-items:center;gap:4px;'
          + '             padding:3px 8px;background:rgba(245,158,11,.15);'
          + '             color:#f59e0b;border:1px solid rgba(245,158,11,.4);'
          + '             border-radius:12px;font-size:11px">'
          + '    📅 ' + _safe(ff.date)
          + '    <button onclick="lmCCSetFilter(\'date\', \'\')" '
          + '            title="清除单日过滤, 回到 ' + ff.days + ' 天窗口"'
          + '            style="background:none;border:none;color:#f59e0b;'
          + '                   cursor:pointer;padding:0;font-size:13px;line-height:1">✕</button>'
          + '  </span>'
        : '';

      const filterHtml = ''
        + '<div style="display:flex;gap:8px;align-items:center;font-size:11px">'
        + '  <select onchange="lmCCSetFilter(\'days\', this.value)" '
        + '          ' + (ff.date ? 'disabled title="清除 date chip 才能切 days"' : '')
        + '          style="padding:3px 8px;background:var(--bg-main);color:var(--text);'
        + '                 border:1px solid var(--border);border-radius:4px;font-size:11px'
        + (ff.date ? ';opacity:.5' : '') + '">'
        +    daysOpts
        + '  </select>'
        + '  <select onchange="lmCCSetFilter(\'actor\', this.value)" '
        + '          style="padding:3px 8px;background:var(--bg-main);color:var(--text);'
        + '                 border:1px solid var(--border);border-radius:4px;font-size:11px">'
        +    actorOpts
        + '  </select>'
        +    dateChipHtml
        + '</div>';

      // 瓶颈可点击: 点 code 跳 blocked peer 子 modal
      const topBlockedHtml = topBlocked
        ? '    <div><span style="color:var(--text-dim)">瓶颈:</span>'
          + '      <code style="color:#f59e0b;margin-left:4px;cursor:pointer;'
          + '                  text-decoration:underline dotted" '
          + '            onclick="lmOpenBlockedPeers(\'' + _safe(topBlocked) + '\')" '
          + '            title="点击查看具体被挡的 peer">' + _safe(topBlocked) + '</code></div>'
        : '    <div style="color:#22c55e">✓ 无主要瓶颈</div>';

      // Phase 8e: sparkline SVG — 纯 SVG 零依赖
      const series = (timeseries && timeseries.series) || [];
      const sparkHtml = _lmBuildSparkline(series, f.days);

      const aFunnelCard = ''
        + '<div style="grid-column:1/-1;padding:14px;background:rgba(96,165,250,.06);'
        + '            border:1px solid rgba(96,165,250,.25);border-radius:8px">'
        + '  <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">'
        + '    <div style="display:flex;gap:12px;align-items:center">'
        + '      <span style="font-size:13px;color:var(--text-muted)">🎯 A 端获客漏斗</span>'
        +        filterHtml
        + '    </div>'
        + '    <div style="font-size:11px">'
        + '      转化率: <b style="color:' + fuRateColor + ';font-size:14px">' + fuRate + '%</b>'
        + '      <span style="color:var(--text-dim)"> (greeting/friend_req)</span>'
        + '    </div>'
        + '  </div>'
        + '  <div style="display:flex;gap:4px;align-items:center">'
        +      funnelNumber('extracted', fuTotal, '#94a3b8')
        + '    <span style="color:var(--text-dim);font-size:20px">→</span>'
        +      funnelNumber('friend_req', fuFr, '#60a5fa')
        + '    <span style="color:var(--text-dim);font-size:20px">→</span>'
        +      funnelNumber('greeting', fuGs, '#22c55e')
        + '    <span style="color:var(--text-dim);font-size:20px">·</span>'
        +      funnelNumber('blocked', fuGb, fuGb > 0 ? '#f59e0b' : '#94a3b8')
        + '  </div>'
        + '  <div style="display:flex;justify-content:space-between;margin-top:12px;'
        + '              padding-top:10px;border-top:1px dashed rgba(255,255,255,.08);font-size:11px">'
        + '    <div>'
        + '      <span style="color:var(--text-dim)">via</span>:'
        + '      <span style="color:#22c55e;margin-left:4px">inline=' + fuInline + '</span>'
        + '      <span style="color:#60a5fa;margin-left:8px">fallback=' + fuFallback + '</span>'
        + (fuUnknown > 0
            ? '      <span style="color:var(--text-dim);margin-left:8px">unknown=' + fuUnknown + '</span>'
            : '')
        + '    </div>'
        +      topBlockedHtml
        + '  </div>'
        + '  <div style="margin-top:10px;font-size:11px">'
        + '    <span style="color:var(--text-dim)">top persona:</span> ' + personaHtml
        + '  </div>'
        +    sparkHtml
        + '</div>';

      // Phase 10-lite: 记录本次刷新时间戳 + 渲染刷新控件
      const refreshState = window._lmCCRefresh;
      refreshState.lastRefreshAt = Date.now();
      const nowHms = new Date().toLocaleTimeString('zh-CN', { hour12: false });
      const refreshBtnLabel = refreshState.paused ? '▶ 继续刷新' : '⏸ 暂停刷新';
      const refreshBtnTitle = refreshState.paused
        ? '当前已暂停 15s 自动刷新, 点击恢复'
        : '暂停自动刷新 (保留当前截图观察)';

      body.innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:18px;font-weight:700">📊 运营指挥台</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        +        '本周总 ' + total + ' 单 · 完成率 ' + (total > 0 ? Math.round(cn * 100 / total) : 0) + '%'
        + '      <span style="margin-left:10px;color:var(--text-dim)">'
        + '        · 自动刷新 15s · last ' + nowHms + '</span></div>'
        + '  </div>'
        + '  <div style="display:flex;gap:6px;align-items:center">'
        + '    <button id="lm-cc-refresh-btn" onclick="lmCCToggleRefresh()" '
        + '            title="' + refreshBtnTitle + '"'
        + '            style="padding:4px 10px;background:rgba(96,165,250,.12);'
        + '                   color:#60a5fa;border:1px solid rgba(96,165,250,.3);'
        + '                   border-radius:6px;font-size:11px;cursor:pointer">'
        +        refreshBtnLabel + '</button>'
        + '    <button onclick="PlatShell.modal.close(\'lm-cc-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '  </div>'
        + '</div>'
        + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">'
        +      aFunnelCard
        + '  <div>'
        + '    <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">🔻 交接漏斗</div>'
        +      funnelBar('待处理', pn, _STATE_COLOR.pending)
        +      funnelBar('已确认', an, _STATE_COLOR.acknowledged)
        +      funnelBar('已完成', cn, _STATE_COLOR.completed)
        +      funnelBar('已拒接', rn, _STATE_COLOR.rejected)
        + '  </div>'
        + '  <div>'
        + '    <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">📊 按渠道</div>'
        + '    <table style="width:100%;font-size:12px">'
        + '      <thead><tr style="color:var(--text-dim)">'
        + '        <th style="text-align:left;padding:6px 10px">渠道</th>'
        + '        <th style="text-align:right;padding:6px 10px">待/确认</th>'
        + '        <th style="text-align:right;padding:6px 10px">完成</th>'
        + '        <th style="text-align:right;padding:6px 10px">转化率</th>'
        + '      </tr></thead><tbody>' + (chRows || '<tr><td colspan="4" style="text-align:center;color:var(--text-dim);padding:14px">暂无数据</td></tr>') + '</tbody>'
        + '    </table>'
        + '  </div>'
        + '  <div>'
        + '    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
        + '      <span style="font-size:13px;color:var(--text-muted)">📬 接收方负载</span>'
        + (atRiskReceivers.length > 0
            ? '      <span style="font-size:10px;color:#ef4444;font-weight:700">⚠ ' + atRiskReceivers.length + ' 已接近满载</span>'
            : '')
        + '    </div>'
        +      (rvRows || '<div style="color:var(--text-dim);font-size:12px">无接收方或无待处理交接</div>')
        + '  </div>'
        + '  <div>'
        + '    <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">⚠ Webhook 健康</div>'
        +      deadHtml
        + '    <div style="margin-top:14px">'
        + '      <div style="font-size:13px;color:var(--text-muted);margin-bottom:8px">🔧 运维操作</div>'
        + '      <button onclick="lmFlushWebhooks()" '
        + '              style="padding:6px 12px;background:rgba(96,165,250,.15);color:#60a5fa;border:1px solid rgba(96,165,250,.4);border-radius:6px;font-size:11px;cursor:pointer;margin-right:6px">⚡ 手动 flush webhook</button>'
        + '    </div>'
        + '  </div>'
        + '</div>';

      // 注入一次 keyframes — 让 atRisk 行呼吸红光
      _lmInjectPulseKeyframes();

      // Phase 10-lite: toast 防抖 — 基于内容 key 的跟踪, 状态无变化时不重复弹.
      // 15s 轮询刷新下原本会每次都弹, 改成 "内容变了才弹" 运营不烦.
      window._lmCCToastKeys = window._lmCCToastKeys || {};
      const seenToast = window._lmCCToastKeys;
      const maybeToast = function (key, msg, level) {
        if (seenToast[key]) return;   // 同 key 跳过
        seenToast[key] = Date.now();
        showToast(msg, level);
      };
      // 每次 render 前清理 10 分钟前的记录 (避免状态恢复后永不弹)
      const tenMinAgo = Date.now() - 600000;
      Object.keys(seenToast).forEach(function (k) {
        if (seenToast[k] < tenMinAgo) delete seenToast[k];
      });

      // 负载告警 toast — 有 ≥90% 的 receiver 时弹红色警告
      if (atRiskReceivers.length > 0 && typeof showToast === 'function') {
        maybeToast('receivers:' + atRiskReceivers.sort().join(','),
          '⚠ 接收方负载告警: ' + atRiskReceivers.join(', ')
          + ' 已 ≥90%, 请考虑启用备用或提升 daily_cap',
          'error');
      }

      // Phase 8b: 获客漏斗瓶颈 toast — 有足够样本 (friend_req ≥ 5) 且
      // 转化率 <25% 或 top_blocked_reason 明显时主动提醒
      if (fuFr >= 5 && typeof showToast === 'function') {
        if (fuRate < 25) {
          maybeToast('funnel_low:' + fuRate + ':' + topBlocked,
            '⚠ A 端 greeting 转化率仅 ' + fuRate + '% '
            + '(' + fuGs + '/' + fuFr + '). '
            + (topBlocked ? '主要瓶颈: ' + topBlocked : '')
            + ' 建议检查 profile UI 或 Messenger fallback 配置',
            'warning');
        } else if (topBlocked === 'messenger_not_installed') {
          maybeToast('messenger_not_installed',
            '⚠ 瓶颈: messenger_not_installed — '
            + '多台设备未装 Messenger, fallback 链路无法启用',
            'warning');
        }
      }
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  };

  // Phase 8e: sparkline — 3 条线 (friend_req/greeting/blocked) 纯 SVG 零依赖.
  //   series: [{date: "YYYY-MM-DD", friend_req, greeting_sent, blocked}]
  //   days: 时间窗口 (<= 1 时 return ''; 单点无 sparkline 意义)
  function _lmBuildSparkline(series, days) {
    if (!Array.isArray(series) || series.length <= 1 || days <= 1) return '';
    const W = 600, H = 64;
    const PAD_X = 6, PAD_Y = 6;
    const plotW = W - 2 * PAD_X;
    const plotH = H - 2 * PAD_Y;
    const n = series.length;
    const stepX = n > 1 ? plotW / (n - 1) : 0;
    // y 范围: max 向上取整到 5 的倍数 (留 20% 头部空间)
    let ymax = 0;
    series.forEach(function (p) {
      ymax = Math.max(ymax,
        (p.friend_req || 0),
        (p.greeting_sent || 0),
        (p.blocked || 0));
    });
    ymax = Math.max(5, Math.ceil(ymax * 1.2 / 5) * 5);

    const yToPx = function (v) {
      return PAD_Y + plotH - (v / ymax) * plotH;
    };
    const makeLine = function (key, color) {
      const pts = series.map(function (p, i) {
        return (PAD_X + i * stepX).toFixed(1) + ',' + yToPx(p[key] || 0).toFixed(1);
      }).join(' ');
      // Phase 8g: circle 加 onclick → 下钻到单日 (点任意颜色都是同一天)
      const circles = series.map(function (p, i) {
        const cx = (PAD_X + i * stepX).toFixed(1);
        const cy = yToPx(p[key] || 0).toFixed(1);
        const v = p[key] || 0;
        return '<circle cx="' + cx + '" cy="' + cy + '" r="2.5" fill="' + color + '"'
          + ' style="cursor:pointer"'
          + ' onclick="lmCCDrillDate(\'' + p.date + '\')">'
          + '<title>' + p.date + ' ' + key + '=' + v + ' (点击下钻)</title></circle>';
      }).join('');
      return '<polyline fill="none" stroke="' + color
        + '" stroke-width="1.5" points="' + pts + '"/>' + circles;
    };

    const legendItem = function (color, label) {
      return '<span style="display:inline-flex;align-items:center;margin-right:10px">'
        + '  <span style="display:inline-block;width:10px;height:2px;background:' + color
        + ';margin-right:4px"></span>' + label + '</span>';
    };
    return '<div style="margin-top:14px;padding-top:10px;'
      + '              border-top:1px dashed rgba(255,255,255,.08)">'
      + '  <div style="display:flex;justify-content:space-between;align-items:center;'
      + '              margin-bottom:4px;font-size:10px;color:var(--text-dim)">'
      + '    <span>📈 近 ' + days + ' 天每日</span>'
      + '    <span>'
      + legendItem('#60a5fa', 'friend_req')
      + legendItem('#22c55e', 'greeting')
      + legendItem('#f59e0b', 'blocked')
      + '    </span>'
      + '  </div>'
      + '  <svg width="100%" height="' + H + '" viewBox="0 0 ' + W + ' ' + H + '" '
      + '       preserveAspectRatio="none" style="display:block">'
      + '    <line x1="' + PAD_X + '" y1="' + yToPx(0)
      + '" x2="' + (W - PAD_X) + '" y2="' + yToPx(0)
      + '" stroke="rgba(255,255,255,.06)" stroke-width="1"/>'
      +      makeLine('friend_req', '#60a5fa')
      +      makeLine('greeting_sent', '#22c55e')
      +      makeLine('blocked', '#f59e0b')
      + '  </svg>'
      + '  <div style="display:flex;justify-content:space-between;font-size:10px;'
      + '              color:var(--text-dim);margin-top:2px">'
      + '    <span>' + _safe(series[0].date.substring(5)) + '</span>'
      + '    <span>' + _safe(series[series.length - 1].date.substring(5)) + '</span>'
      + '  </div>'
      + '</div>';
  }

  function _lmInjectPulseKeyframes() {
    if (document.getElementById('lm-pulse-keyframes')) return;
    const s = document.createElement('style');
    s.id = 'lm-pulse-keyframes';
    s.textContent = '@keyframes lmPulseRed {'
      + ' 0%,100% { box-shadow: 0 0 0 0 rgba(239,68,68,.4); }'
      + ' 50% { box-shadow: 0 0 0 4px rgba(239,68,68,.15); } }';
    document.head.appendChild(s);
  }

  // Phase 8h: 一键加入 blocklist — 从 blocked peer modal 里点"🚫 加黑"触发
  window.lmAddToBlocklist = async function (cid, reasonHint) {
    const Shell = _shell();
    if (!Shell || !cid) return;
    const note = window.prompt(
      '加入 blocklist 后, 后续 A 端 add_friend / send_greeting 对该 peer 自动 skip.\n'
      + '输入可选备注 (原因 / 运营姓名等, 可为空):',
      reasonHint ? '来自 ' + reasonHint + ' 瓶颈' : '');
    if (note === null) return;   // 用户 cancel
    try {
      await Shell.api.post(
        '/lead-mesh/peers/' + encodeURIComponent(cid) + '/blocklist',
        { reason: reasonHint || '', note: note, created_by: 'dashboard' });
      if (typeof showToast === 'function') {
        showToast('✓ 已加入 blocklist · ' + cid.substring(0, 8) + '…', 'success');
      }
      // 刷新当前 blocked peers modal (让列表视觉上反映变化, 虽然 peer 仍在 journey)
      if (reasonHint) {
        lmOpenBlockedPeers(reasonHint);
      }
    } catch (e) {
      if (typeof showToast === 'function') {
        showToast('加黑失败: ' + (e.message || e), 'error');
      }
    }
  };

  // Phase 8d: 点击漏斗瓶颈看具体被挡 peer 列表
  window.lmOpenBlockedPeers = async function (reason) {
    const Shell = _shell();
    if (!Shell || !reason) return;
    const f = window._lmCCFilter || { days: 7 };
    Shell.modal.open('lm-blocked-peers-modal',
      '<div id="lm-bp-body" style="padding:18px">加载中…</div>',
      { maxWidth: '720px' });
    try {
      const r = await Shell.api.get(
        '/lead-mesh/funnel/blocked-peers?reason=' + encodeURIComponent(reason)
        + '&days=' + f.days + '&limit=50'
        + (f.date ? '&date=' + encodeURIComponent(f.date) : ''));
      const peers = (r && r.peers) || [];
      const rows = peers.length === 0
        ? '<div style="text-align:center;padding:30px;color:var(--text-dim)">✓ 该 reason 下无被挡 peer (时间窗口内)</div>'
        : peers.map(function (p) {
            const cid = p.canonical_id || '';
            const cidShort = cid.substring(0, 8);
            const at = (p.last_blocked_at || '').substring(0, 19);
            const persona = p.persona_key || '';
            return ''
              + '<div style="padding:10px 14px;background:var(--bg-main);'
              + '            border-left:3px solid #f59e0b;border-radius:4px;margin-bottom:6px;'
              + '            display:flex;justify-content:space-between;align-items:center">'
              + '  <div style="flex:1;min-width:0">'
              + '    <div style="font-size:12px">'
              + '      <code style="color:#60a5fa">' + _safe(cidShort) + '…</code>'
              + '      <span style="margin-left:10px;color:var(--text-dim);font-size:10px">'
              +          _safe(at) + '</span>'
              + (persona
                  ? '      <span style="margin-left:10px;padding:1px 6px;background:rgba(96,165,250,.12);'
                    + '                   color:#60a5fa;border-radius:8px;font-size:10px">'
                    + _safe(persona) + '</span>'
                  : '')
              + '    </div>'
              + '    <div style="font-size:10px;color:var(--text-dim);margin-top:2px">'
              + '      被挡次数: <b style="color:#f59e0b">' + p.n_blocked + '</b>'
              + '    </div>'
              + '  </div>'
              + '  <div style="display:flex;gap:4px">'
              + '    <button onclick="PlatShell.modal.close(\'lm-blocked-peers-modal\');'
              + '                     lmOpenLeadDossier(\'' + _safe(cid) + '\')" '
              + '            style="padding:4px 10px;background:rgba(96,165,250,.12);color:#60a5fa;'
              + '                   border:1px solid rgba(96,165,250,.3);border-radius:4px;'
              + '                   font-size:11px;cursor:pointer">📖 dossier</button>'
              + '    <button onclick="lmAddToBlocklist(\'' + _safe(cid) + '\', \'' + _safe(reason) + '\')" '
              + '            title="加入 blocklist, 后续 A 端 add_friend/greeting 自动 skip"'
              + '            style="padding:4px 10px;background:rgba(239,68,68,.12);color:#ef4444;'
              + '                   border:1px solid rgba(239,68,68,.3);border-radius:4px;'
              + '                   font-size:11px;cursor:pointer">🚫 加黑</button>'
              + '  </div>'
              + '</div>';
          }).join('');
      document.getElementById('lm-bp-body').innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:16px;font-weight:700">🔍 被挡 peer 列表</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        + '      reason: <code style="color:#f59e0b">' + _safe(reason) + '</code>'
        + '      · 近 ' + f.days + ' 天 · 共 ' + peers.length + ' 个唯一 peer'
        + '    </div>'
        + '  </div>'
        + '  <button onclick="PlatShell.modal.close(\'lm-blocked-peers-modal\')" '
        + '          style="background:none;border:1px solid var(--border);color:var(--text);'
        + '                 padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + rows;
    } catch (e) {
      document.getElementById('lm-bp-body').innerHTML =
        '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  };

  window.lmFlushWebhooks = async function () {
    const Shell = _shell();
    if (!Shell) return;
    try {
      const r = await Shell.api.post('/lead-mesh/webhooks/flush?max_batch=100', {});
      const s = (r && r.stats) || {};
      showToast('Flush 完成: delivered=' + (s.delivered || 0) + ' retried=' + (s.retried || 0) + ' dead=' + (s.dead_letter || 0),
                 (s.dead_letter > 0 ? 'warning' : 'success'));
    } catch (e) {
      showToast('flush 失败: ' + (e.message || e), 'error');
    }
  };

  window.lmViewDeadLetters = async function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-dead-modal',
      '<div id="lm-dead-body" style="padding:18px">加载中…</div>',
      { maxWidth: '920px' });
    try {
      const r = await Shell.api.get('/lead-mesh/webhooks/dead-letters?limit=100');
      const list = (r && r.dead_letters) || [];
      const rows = list.length === 0
        ? '<div style="text-align:center;padding:40px;color:#22c55e">✓ 没有死信</div>'
        : list.map(function (d) {
            return ''
              + '<div style="padding:10px 14px;background:var(--bg-main);border-left:3px solid #ef4444;border-radius:6px;margin-bottom:6px">'
              + '  <div style="display:flex;justify-content:space-between;align-items:center">'
              + '    <div style="flex:1;min-width:0">'
              + '      <div style="font-weight:600;font-size:12px">'
              + _safe(d.event_type) + ' <span style="color:var(--text-muted);font-size:10px">→ ' + _safe(d.target_url) + '</span></div>'
              + '      <div style="font-size:10px;color:var(--text-dim);margin-top:2px">' + _safe(d.last_error || '') + '</div>'
              + '    </div>'
              + '    <button onclick="lmRetryDeadLetter(' + d.id + ')" '
              + '            style="padding:4px 10px;background:rgba(34,197,94,.15);color:#22c55e;border:1px solid rgba(34,197,94,.4);border-radius:4px;font-size:11px;cursor:pointer">🔄 重试</button>'
              + '  </div>'
              + '</div>';
          }).join('');
      document.getElementById('lm-dead-body').innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div style="font-size:16px;font-weight:700">⚠ Webhook 死信队列 (' + list.length + ')</div>'
        + '  <button onclick="PlatShell.modal.close(\'lm-dead-modal\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + rows;
    } catch (e) {
      document.getElementById('lm-dead-body').innerHTML = '<div style="color:#ef4444">加载失败</div>';
    }
  };

  window.lmRetryDeadLetter = async function (dispatchId) {
    const Shell = _shell();
    if (!Shell) return;
    try {
      await Shell.api.post('/lead-mesh/webhooks/' + dispatchId + '/retry', {});
      showToast('已重置为 pending, 下次 flush 会重试', 'success');
      lmViewDeadLetters();
    } catch (e) {
      showToast('重置失败: ' + (e.message || e), 'error');
    }
  };


  // ─────────────────────────────────────────────────────────────────
  // P1 · 接收方账号管理 (Phase 6.B, 2026-04-23)
  // ─────────────────────────────────────────────────────────────────

  window.lmOpenReceiversConfig = async function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-receivers-modal',
      '<div id="lm-receivers-body" style="padding:18px">加载中…</div>',
      { maxWidth: '1000px' });
    await _lmRenderReceivers();
  };

  async function _lmRenderReceivers() {
    const Shell = _shell();
    const body = document.getElementById('lm-receivers-body');
    if (!body) return;
    try {
      const r = await Shell.api.get('/lead-mesh/receivers?with_load=true');
      const list = (r && r.receivers) || [];
      _lmInjectPulseKeyframes();
      // 计算负载告警 banner
      const atRisk = list.filter(function (x) {
        const cap = x.cap || x.daily_cap || 0;
        const cur = x.current || 0;
        const pct = cap > 0 ? Math.round(cur * 100 / cap) : 0;
        return x.enabled !== false && pct >= 90;
      });
      const alertBanner = atRisk.length === 0
        ? ''
        : ('<div style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.35);'
            + 'border-left:4px solid #ef4444;padding:8px 12px;margin-bottom:12px;border-radius:4px;'
            + 'font-size:12px;color:#ef4444">'
            + '⚠ <b>' + atRisk.length + ' 个接收方负载 ≥ 90%</b>: '
            + atRisk.map(function (x) { return _safe(x.key); }).join(', ')
            + ' — 建议启用 backup_key 或上调 daily_cap'
            + '</div>');
      const rows = list.length === 0
        ? '<tr><td colspan="7" style="text-align:center;padding:40px;color:var(--text-dim)">'
          + '尚无接收方。参考 <code>config/referral_receivers.yaml.example</code> 创建。'
          + '</td></tr>'
        : list.map(_lmReceiverRowHtml).join('');
      body.innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '  <div>'
        + '    <div style="font-size:18px;font-weight:700">📬 接收方账号管理</div>'
        + '    <div style="font-size:11px;color:var(--text-muted);margin-top:2px">'
        + '      每个 receiver 是一个接收引流的账号(LINE/WA/TG/IG/Messenger),'
        + ' handoff 自动按 channel + persona + 剩余 cap 路由</div>'
        + '  </div>'
        + '  <div style="display:flex;gap:8px">'
        + '    <button onclick="lmOpenNewReceiverDialog()" '
        + '            style="padding:6px 14px;background:#22c55e;color:#fff;border:none;border-radius:6px;font-size:12px;cursor:pointer">'
        + '      ➕ 新增接收方</button>'
        + '    <button onclick="_lmRenderReceivers()" '
        + '            style="padding:6px 12px;background:rgba(96,165,250,.15);color:#60a5fa;border:1px solid rgba(96,165,250,.4);border-radius:6px;font-size:11px;cursor:pointer">'
        + '      🔄 刷新</button>'
        + '    <button onclick="PlatShell.modal.close(\'lm-receivers-modal\')" '
        + '            style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '  </div>'
        + '</div>'
        + alertBanner
        + '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        + '  <thead><tr style="color:var(--text-dim);background:rgba(255,255,255,.03)">'
        + '    <th style="text-align:left;padding:8px">Key</th>'
        + '    <th style="text-align:left;padding:8px">渠道</th>'
        + '    <th style="text-align:left;padding:8px">账号(脱敏)</th>'
        + '    <th style="text-align:left;padding:8px">今日负载</th>'
        + '    <th style="text-align:left;padding:8px">备用</th>'
        + '    <th style="text-align:left;padding:8px">状态</th>'
        + '    <th style="text-align:left;padding:8px">操作</th>'
        + '  </tr></thead><tbody>' + rows + '</tbody>'
        + '</table>'
        + '<div style="margin-top:14px;font-size:11px;color:var(--text-dim)">'
        + '  💡 配置文件: <code>config/referral_receivers.yaml</code>(热加载);'
        + ' 轮转算法 least_loaded; at_cap 时自动跳 backup_key'
        + '</div>';
    } catch (e) {
      body.innerHTML = '<div style="color:#ef4444;padding:20px">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  }

  function _lmReceiverRowHtml(r) {
    const enabled = r.enabled !== false;
    const cap = r.cap || r.daily_cap || 0;
    const cur = r.current || 0;
    const pct = cap > 0 ? Math.round(cur * 100 / cap) : 0;
    const barColor = pct >= 90 ? '#ef4444' : pct >= 60 ? '#f59e0b' : '#22c55e';
    // 接近/已满: 红字 + 行脉冲发光, 引导 ops 立即处置
    const atRisk = enabled && pct >= 90;
    const rowExtraStyle = atRisk
      ? ';background:rgba(239,68,68,.06);animation:lmPulseRed 2s ease-in-out infinite'
      : '';
    const pctLabel = atRisk
      ? '<span style="color:#ef4444;font-weight:700">⚠ ' + pct + '%</span>'
      : '<span style="color:' + barColor + '">' + pct + '%</span>';
    const statusBadge = enabled
      ? '<span style="color:#22c55e;font-weight:600">● 启用</span>'
      : '<span style="color:#94a3b8">○ 禁用</span>';
    const toggleBtn = enabled
      ? ('<button onclick="lmToggleReceiver(\'' + _safe(r.key) + '\', false)" '
         + 'style="padding:3px 8px;font-size:11px;background:rgba(245,158,11,.12);color:#f59e0b;border:1px solid rgba(245,158,11,.3);border-radius:4px;cursor:pointer">禁用</button>')
      : ('<button onclick="lmToggleReceiver(\'' + _safe(r.key) + '\', true)" '
         + 'style="padding:3px 8px;font-size:11px;background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.3);border-radius:4px;cursor:pointer">启用</button>');
    const personaTags = (r.persona_filter || []).slice(0, 2).join(', ') || '所有';

    return ''
      + '<tr style="border-bottom:1px solid var(--border)' + rowExtraStyle + '">'
      + '  <td style="padding:8px;cursor:pointer" onclick="lmOpenEditReceiver(\'' + _safe(r.key) + '\')"'
      + '      title="点击编辑">'
      + '    <b style="color:#60a5fa;text-decoration:underline">' + _safe(r.key) + '</b>'
      + '    <div style="font-size:10px;color:var(--text-dim)">' + _safe(r.display_name || '') + '</div>'
      + '    <div style="font-size:10px;color:var(--text-dim)">persona: ' + _safe(personaTags) + '</div>'
      + '  </td>'
      + '  <td style="padding:8px;text-transform:uppercase">' + _safe(r.channel) + '</td>'
      + '  <td style="padding:8px;font-family:monospace">' + _safe(r.account_id_masked || r.account_id || '') + '</td>'
      + '  <td style="padding:8px;min-width:140px">'
      + '    <div style="display:flex;justify-content:space-between;font-size:10px;margin-bottom:2px">'
      + '      <span>' + cur + ' / ' + cap + '</span>'
      + '      ' + pctLabel
      + '    </div>'
      + '    <div style="height:6px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden">'
      + '      <div style="width:' + pct + '%;height:100%;background:' + barColor + '"></div>'
      + '    </div>'
      + '  </td>'
      + '  <td style="padding:8px"><code>' + _safe(r.backup_key || '—') + '</code></td>'
      + '  <td style="padding:8px">' + statusBadge + '</td>'
      + '  <td style="padding:8px">'
      + '    <button onclick="lmOpenEditReceiver(\'' + _safe(r.key) + '\')" '
      + '            style="padding:3px 8px;font-size:11px;background:rgba(14,165,233,.12);color:#0ea5e9;border:1px solid rgba(14,165,233,.3);border-radius:4px;cursor:pointer;margin-right:4px">✏️ 编辑</button>'
      + toggleBtn
      + '    <button onclick="lmDeleteReceiver(\'' + _safe(r.key) + '\')" '
      + '            style="margin-left:4px;padding:3px 8px;font-size:11px;background:rgba(239,68,68,.12);color:#ef4444;border:1px solid rgba(239,68,68,.3);border-radius:4px;cursor:pointer">🗑</button>'
      + '  </td>'
      + '</tr>';
  }

  window.lmToggleReceiver = async function (key, enabled) {
    const Shell = _shell();
    if (!Shell) return;
    try {
      await Shell.api.post('/lead-mesh/receivers/' + encodeURIComponent(key),
                            { enabled: enabled });
      showToast((enabled ? '启用' : '禁用') + ' ' + key + ' 成功', 'success');
      _lmRenderReceivers();
    } catch (e) {
      showToast('切换失败: ' + (e.message || e), 'error');
    }
  };

  window.lmDeleteReceiver = async function (key) {
    const Shell = _shell();
    if (!Shell) return;
    if (!(await ocDialog({title:'删除接收方',message:'删除接收方 ' + key + ' ？<br>已入账的 handoff 不会受影响，但无法继续路由新 handoff 到该账号。',type:'danger',confirmText:'删除',dangerous:true}))) return;
    try {
      await Shell.api.delete('/lead-mesh/receivers/' + encodeURIComponent(key));
      showToast('已删除', 'success');
      _lmRenderReceivers();
    } catch (e) {
      showToast('删除失败: ' + (e.message || e), 'error');
    }
  };

  // ─── 表单 HTML 共用工厂 (new / edit 模式复用) ───────────────────────
  function _lmReceiverFormHtml(mode, r) {
    const isEdit = mode === 'edit';
    r = r || {};
    const title = isEdit ? '✏️ 编辑接收方' : '➕ 新增接收方';
    const btnLabel = isEdit ? '保存' : '创建';
    const btnColor = isEdit ? '#0ea5e9' : '#22c55e';
    const keyReadonly = isEdit ? 'readonly disabled style="opacity:0.6;cursor:not-allowed"' : '';
    const keyValue = _safe(r.key || '');
    const channels = ['line', 'whatsapp', 'telegram', 'messenger', 'instagram'];
    const channelOpts = channels.map(function (ch) {
      const selected = r.channel === ch ? ' selected' : '';
      return '<option value="' + ch + '"' + selected + '>' + ch.toUpperCase() + '</option>';
    }).join('');
    const personaVal = (r.persona_filter || []).join(', ');
    const enabledChecked = r.enabled !== false ? 'checked' : '';

    return ''
      + '<div style="padding:18px">'
      + '  <div style="font-size:16px;font-weight:700;margin-bottom:14px">' + title + '</div>'
      + '  <div style="display:grid;grid-template-columns:1fr 2fr;gap:8px;font-size:12px">'
      + '    <label style="align-self:center">Key *:</label>'
      + '    <input id="lm-rf-key" placeholder="line_jp_01" value="' + keyValue + '" ' + keyReadonly
      + '       style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">渠道 *:</label>'
      + '    <select id="lm-rf-channel" style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      +        channelOpts
      + '    </select>'
      + '    <label style="align-self:center">账号 ID *:</label>'
      + '    <input id="lm-rf-account" placeholder="@jpline01 / +8190... / @username" value="' + _safe(r.account_id || '')
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">显示名:</label>'
      + '    <input id="lm-rf-display" placeholder="主号 / 首选 LINE" value="' + _safe(r.display_name || '')
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">日上限:</label>'
      + '    <input id="lm-rf-cap" type="number" value="' + (r.daily_cap || 15)
      + '       " min="0" max="500" style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">备用 key:</label>'
      + '    <input id="lm-rf-backup" placeholder="(可空) 配额满时转路由到此 key" value="' + _safe(r.backup_key || '')
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">persona 过滤:</label>'
      + '    <input id="lm-rf-persona" placeholder="(可空,逗号分隔) jp_female_midlife" value="' + _safe(personaVal)
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">tags:</label>'
      + '    <input id="lm-rf-tags" placeholder="(可空,逗号分隔) primary, japan" value="' + _safe((r.tags || []).join(', '))
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">Webhook URL:</label>'
      + '    <input id="lm-rf-webhook" placeholder="(可空) receiver 专属 webhook" value="' + _safe(r.webhook_url || '')
      + '       " style="padding:6px 10px;background:var(--bg-main);border:1px solid var(--border);color:var(--text);border-radius:4px">'
      + '    <label style="align-self:center">启用:</label>'
      + '    <div style="display:flex;align-items:center;gap:6px"><input id="lm-rf-enabled" type="checkbox" ' + enabledChecked
      + '       style="width:16px;height:16px;cursor:pointer"><span style="font-size:11px;color:var(--text-dim)">勾选即启用,不勾选则不接收新 handoff</span></div>'
      + '  </div>'
      + (isEdit
          ? ('<div style="margin-top:10px;padding:8px 12px;background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.3);border-radius:6px;font-size:11px;color:#fbbf24">'
             + '⚠ 修改 account_id 会影响现有 handoff 的路由, 但已入账的 handoff.receiver_account_key 不会跟随变化</div>')
          : '')
      + '  <div style="margin-top:14px;display:flex;gap:8px;justify-content:flex-end">'
      + '    <button onclick="PlatShell.modal.close(\'lm-receiver-form\')" style="padding:6px 14px;background:none;border:1px solid var(--border);color:var(--text);border-radius:6px;cursor:pointer">取消</button>'
      + '    <button onclick="lmSubmitReceiverForm(\'' + mode + '\')" style="padding:6px 14px;background:' + btnColor
      + '       ;color:#fff;border:none;border-radius:6px;font-weight:600;cursor:pointer">' + btnLabel + '</button>'
      + '  </div>'
      + '</div>';
  }

  window.lmOpenNewReceiverDialog = function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-receiver-form',
      _lmReceiverFormHtml('new', {}),
      { maxWidth: '620px' });
    setTimeout(function () {
      const el = document.getElementById('lm-rf-key');
      if (el) el.focus();
    }, 100);
  };

  window.lmOpenEditReceiver = async function (key) {
    const Shell = _shell();
    if (!Shell) return;
    try {
      const r = await Shell.api.get('/lead-mesh/receivers/' + encodeURIComponent(key));
      Shell.modal.open('lm-receiver-form',
        _lmReceiverFormHtml('edit', r),
        { maxWidth: '620px' });
    } catch (e) {
      showToast('加载失败: ' + (e.message || e), 'error');
    }
  };

  // ─── Phase 6 UX: 时间轴按天分组 ─────────────────────────────
  function _lmDayBucket(atIso) {
    // 输入格式 "YYYY-MM-DD HH:MM:SS" 或 ISO; 提取日期部分并按本地时区比较
    const ymd = (atIso || '').substring(0, 10);   // "2026-04-23"
    if (!ymd) return 'unknown';
    const today = new Date();
    const t0 = new Date(today.getFullYear(), today.getMonth(), today.getDate());
    const evtDate = new Date(ymd + 'T00:00:00');
    const diffDays = Math.round((t0 - evtDate) / 86400000);
    if (diffDays === 0) return '今天 · ' + ymd;
    if (diffDays === 1) return '昨天 · ' + ymd;
    if (diffDays < 7) return diffDays + ' 天前 · ' + ymd;
    if (diffDays < 30) return Math.round(diffDays / 7) + ' 周前 · ' + ymd;
    return Math.round(diffDays / 30) + ' 个月前 · ' + ymd;
  }

  function _lmTimelineHtml(journey) {
    if (!journey || !journey.length) {
      return '<div style="text-align:center;padding:20px;color:var(--text-dim)">无事件</div>';
    }
    // 按天分桶 (保持倒序 - 最新在上)
    const buckets = [];   // [{label, events[]}, ...]
    let currentLabel = null;
    const reversed = journey.slice().reverse();  // 最新在上
    reversed.forEach(function (ev) {
      const lbl = _lmDayBucket(ev.at || '');
      if (lbl !== currentLabel) {
        buckets.push({ label: lbl, events: [] });
        currentLabel = lbl;
      }
      buckets[buckets.length - 1].events.push(ev);
    });

    return buckets.map(function (b) {
      const eventsHtml = b.events.map(function (ev) {
        const icon = _ACTION_ICON[ev.action] || '•';
        const color = _ACTION_COLOR[ev.action] || '#94a3b8';
        const actor = ev.actor || '';
        const actorColor = actor.startsWith('agent_a') ? '#22c55e'
                         : actor.startsWith('agent_b') ? '#a855f7'
                         : actor.startsWith('human') ? '#0ea5e9'
                         : '#64748b';
        const actorBadge = actor.startsWith('agent_a') ? '🟢 A'
                         : actor.startsWith('agent_b') ? '🟣 B'
                         : actor.startsWith('human') ? '👤 人'
                         : '⚙️ 系统';
        const dataKeys = ev.data ? Object.keys(ev.data) : [];
        const dataStr = dataKeys.length
          ? ' <details style="display:inline-block;vertical-align:middle"><summary style="cursor:pointer;color:var(--text-dim);font-size:10px">' + dataKeys.length + ' 字段</summary>'
            + '<pre style="margin:4px 0 0 0;padding:6px 8px;background:var(--bg-card);border-radius:4px;font-size:10px;max-width:500px;overflow:auto">' + _safe(JSON.stringify(ev.data, null, 2)) + '</pre></details>'
          : '';
        return ''
          + '<div style="display:flex;gap:10px;padding:6px 0;border-bottom:1px dashed rgba(255,255,255,.05)">'
          + '  <div style="min-width:56px;color:var(--text-dim);font-size:10px;font-family:monospace;padding-top:2px">'
          +      _safe((ev.at || '').substring(11, 19)) + '</div>'
          + '  <div style="font-size:14px">' + icon + '</div>'
          + '  <div style="flex:1;min-width:0">'
          + '    <div style="font-weight:600;color:' + color + ';font-size:12px">' + _safe(ev.action)
          +       ' <span style="font-size:10px;color:' + actorColor + ';font-weight:400;margin-left:6px">' + actorBadge + '</span>'
          + '    </div>'
          + '    <div style="font-size:10px;color:var(--text-muted)">'
          + '      <span style="color:' + actorColor + '">' + _safe(actor) + '</span>'
          +        (ev.actor_device ? ' @<code>' + _safe(ev.actor_device.substring(0, 8)) + '</code>' : '')
          +        (ev.platform ? ' · ' + _safe(ev.platform) : '')
          +        (ev.action==='note_added' && ev.data && ev.data.text
              ? '<div style="margin-top:3px;padding:4px 8px;background:rgba(14,165,233,.08);border-left:2px solid #0ea5e9;border-radius:3px;color:var(--text-main);font-size:11px">'
              + _safe(ev.data.text) + '</div>' : dataStr)
          + '    </div>'
          + '  </div>'
          + '</div>';
      }).join('');
      return ''
        + '<div style="margin-bottom:14px">'
        + '  <div style="font-size:11px;color:var(--text-dim);font-weight:600;margin-bottom:4px;padding:4px 8px;background:rgba(255,255,255,.03);border-left:3px solid #60a5fa;border-radius:0 4px 4px 0">'
        + '    📅 ' + _safe(b.label) + ' <span style="color:var(--text-dim);font-weight:400">(' + b.events.length + ' 事件)</span>'
        + '  </div>'
        +    eventsHtml
        + '</div>';
    }).join('');
  }

  window.lmSubmitReceiverForm = async function (mode) {
    const Shell = _shell();
    if (!Shell) return;
    const key = (document.getElementById('lm-rf-key') || {}).value || '';
    if (!key) { showToast('Key 必填', 'warning'); return; }
    const body = {
      channel: (document.getElementById('lm-rf-channel') || {}).value,
      account_id: (document.getElementById('lm-rf-account') || {}).value,
      display_name: (document.getElementById('lm-rf-display') || {}).value,
      daily_cap: parseInt((document.getElementById('lm-rf-cap') || {}).value) || 15,
      backup_key: (document.getElementById('lm-rf-backup') || {}).value || null,
      persona_filter: ((document.getElementById('lm-rf-persona') || {}).value || '')
          .split(',').map(function (s) { return s.trim(); }).filter(Boolean),
      tags: ((document.getElementById('lm-rf-tags') || {}).value || '')
          .split(',').map(function (s) { return s.trim(); }).filter(Boolean),
      webhook_url: (document.getElementById('lm-rf-webhook') || {}).value || '',
      enabled: !!((document.getElementById('lm-rf-enabled') || {}).checked),
    };
    if (mode === 'new' && (!body.channel || !body.account_id)) {
      showToast('渠道和账号 ID 必填', 'warning');
      return;
    }
    try {
      await Shell.api.post('/lead-mesh/receivers/' + encodeURIComponent(key), body);
      showToast((mode === 'edit' ? '保存' : '创建') + ' 成功', 'success');
      PlatShell.modal.close('lm-receiver-form');
      _lmRenderReceivers();
    } catch (e) {
      showToast((mode === 'edit' ? '保存' : '创建') + ' 失败: ' + (e.message || e), 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // Phase E2: 合并审计面板
  // ════════════════════════════════════════════════════════
  window.lmOpenMergeAudit = async function () {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-merge-audit',
      '<div id="lm-merge-body">加载中…</div>', { maxWidth: '900px' });
    try {
      const r = await Shell.api.get('/lead-mesh/leads/merges?limit=50&include_reverted=true');
      const merges = r.merges || [];
      const rows = merges.map(function (m) {
        const reverted = !!m.reverted_at;
        const blocked = (m.merge_mode || '').indexOf('blocked') >= 0;
        const reasons = m.merge_reasons_json || '[]';
        let reasonsArr;
        try { reasonsArr = JSON.parse(reasons); } catch (e) { reasonsArr = [reasons]; }
        var rowStyle = reverted ? 'opacity:.5' : (blocked ? 'background:rgba(251,191,36,.06)' : '');
        var modeLabel = blocked ? '🚫 ' + (m.merge_mode||'') : (m.merge_mode || '');
        return '<tr style="' + rowStyle + '">'
          + '<td style="padding:6px 8px;font-size:11px;font-family:monospace">' + (m.id || '') + '</td>'
          + '<td style="padding:6px 8px;font-size:11px;font-family:monospace">' + (m.source_canonical_id || '').substr(0, 12) + '…</td>'
          + '<td style="padding:6px 8px;font-size:11px;font-family:monospace">' + (m.target_canonical_id || '').substr(0, 12) + '…</td>'
          + '<td style="padding:6px 8px;font-size:11px;' + (blocked ? 'color:#f59e0b' : '') + '">' + modeLabel + '</td>'
          + '<td style="padding:6px 8px;font-size:11px">' + ((m.confidence || 0) * 100).toFixed(0) + '%</td>'
          + '<td style="padding:6px 8px;font-size:10px;color:var(--text-muted)">' + reasonsArr.join(', ') + '</td>'
          + '<td style="padding:6px 8px;font-size:11px">' + _fmtTime(m.merged_at) + '</td>'
          + '<td style="padding:6px 8px">'
          + (blocked
            ? '<span style="font-size:10px;color:#f59e0b">已拦截</span>'
            : reverted
              ? '<span style="font-size:10px;color:#fbbf24">已撤销</span>'
              : '<button onclick="lmRevertMerge(' + m.id + ')" style="background:rgba(239,68,68,.12);color:#ef4444;border:1px solid rgba(239,68,68,.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px">撤销</button>')
          + '</td></tr>';
      }).join('');

      // H2: 并行加载可疑合并
      var suspectsHtml = '';
      try {
        var sr = await Shell.api.get('/lead-mesh/leads/merges/suspects?limit=50');
        var suspects = sr.suspects || [];
        if (suspects.length > 0) {
          var srows = suspects.map(function(s) {
            return '<tr style="background:rgba(239,68,68,.04)">'
              + '<td style="padding:5px 8px;font-size:11px;font-family:monospace">' + (s.merge_id||'') + '</td>'
              + '<td style="padding:5px 8px;font-size:11px">' + _safe(s.source_name||'?') + '</td>'
              + '<td style="padding:5px 8px;font-size:11px">' + _safe(s.target_name||'?') + '</td>'
              + '<td style="padding:5px 8px;font-size:11px">' + (s.name_similarity * 100).toFixed(0) + '%</td>'
              + '<td style="padding:5px 8px;font-size:11px">' + (s.target_identity_count||0) + '</td>'
              + '<td style="padding:5px 8px;font-size:10px;color:#ef4444">' + (s.reasons||[]).join(', ') + '</td>'
              + '<td style="padding:5px 8px;white-space:nowrap">'
              + '<button onclick="lmRevertMerge(' + s.merge_id + ')" style="background:rgba(239,68,68,.12);color:#ef4444;border:1px solid rgba(239,68,68,.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px;margin-right:4px">撤销</button>'
              + '<button onclick="lmMarkMergeSafe(' + s.merge_id + ')" style="background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.3);padding:2px 8px;border-radius:4px;cursor:pointer;font-size:10px">安全✓</button>'
              + '</td>'
              + '</tr>';
          }).join('');
          suspectsHtml = '<div style="margin-top:18px;padding-top:14px;border-top:1px solid var(--border)">'
            + '<div style="font-size:12px;font-weight:600;color:#ef4444;margin-bottom:8px">⚠️ 可疑合并 (' + suspects.length + ' 条需审核)</div>'
            + '<table style="width:100%;border-collapse:collapse;font-size:12px"><thead><tr style="background:rgba(239,68,68,.06)">'
            + '<th style="text-align:left;padding:5px 8px">ID</th><th style="text-align:left;padding:5px 8px">源名</th>'
            + '<th style="text-align:left;padding:5px 8px">目标名</th><th style="text-align:left;padding:5px 8px">相似度</th>'
            + '<th style="text-align:left;padding:5px 8px">身份数</th><th style="text-align:left;padding:5px 8px">原因</th>'
            + '<th style="text-align:left;padding:5px 8px">操作</th></tr></thead><tbody>' + srows + '</tbody></table></div>';
        }
      } catch(se) { /* suspects 加载失败不影响主面板 */ }

      document.getElementById('lm-merge-body').innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<h3 style="margin:0">🔗 身份合并审计 (最近 50 条)</h3>'
        + '<button onclick="PlatShell.modal.close(\'lm-merge-audit\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        + '<div style="margin-bottom:12px;font-size:12px;color:var(--text-muted)">'
        + '活跃: ' + merges.filter(function(m){return !m.reverted_at && (m.merge_mode||'').indexOf('blocked')<0;}).length
        + ' | 已撤销: ' + merges.filter(function(m){return !!m.reverted_at;}).length
        + ' | 🚫 拦截: ' + merges.filter(function(m){return (m.merge_mode||'').indexOf('blocked')>=0;}).length
        + ' | 自动合并: ' + merges.filter(function(m){return m.merge_mode!=='manual'&&(m.merge_mode||'').indexOf('blocked')<0;}).length
        + '</div>'
        + '<div style="overflow-x:auto">'
        + '<table style="width:100%;border-collapse:collapse;font-size:12px">'
        + '<thead><tr style="background:rgba(255,255,255,.03)">'
        + '<th style="text-align:left;padding:6px 8px">ID</th>'
        + '<th style="text-align:left;padding:6px 8px">Source</th>'
        + '<th style="text-align:left;padding:6px 8px">Target</th>'
        + '<th style="text-align:left;padding:6px 8px">模式</th>'
        + '<th style="text-align:left;padding:6px 8px">置信度</th>'
        + '<th style="text-align:left;padding:6px 8px">原因</th>'
        + '<th style="text-align:left;padding:6px 8px">时间</th>'
        + '<th style="text-align:left;padding:6px 8px">操作</th>'
        + '</tr></thead><tbody>'
        + (rows || '<tr><td colspan="8" style="padding:16px;text-align:center;color:var(--text-muted)">暂无合并记录</td></tr>')
        + '</tbody></table></div>'
        + suspectsHtml;
    } catch (e) {
      document.getElementById('lm-merge-body').innerHTML =
        '<div style="color:#ef4444">加载失败: ' + (e.message || e) + '</div>';
    }
  };

  window.lmRevertMerge = async function (mergeId) {
    const Shell = _shell();
    if (!Shell) return;
    if (!confirm('确定撤销合并 #' + mergeId + '？\n撤销后身份将恢复为两个独立 Lead。')) return;
    try {
      await Shell.api.post('/lead-mesh/leads/merges/' + mergeId + '/revert', {
        reverted_by: 'human_dashboard',
        reason: 'manual_audit_revert'
      });
      showToast('合并 #' + mergeId + ' 已撤销', 'success');
      lmOpenMergeAudit();  // refresh
    } catch (e) {
      showToast('撤销失败: ' + (e.message || e), 'error');
    }
  };

  window.lmMarkMergeSafe = async function (mergeId) {
    const Shell = _shell();
    if (!Shell) return;
    try {
      await Shell.api.post('/lead-mesh/leads/merges/' + mergeId + '/review', {
        audit_status: 'safe',
        reviewed_by: 'human_dashboard'
      });
      showToast('合并 #' + mergeId + ' 已标记安全', 'success');
      lmOpenMergeAudit();  // refresh
    } catch (e) {
      showToast('标记失败: ' + (e.message || e), 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // Phase G2: "合并到" 操作入口 — 搜索目标 + 确认执行
  // ════════════════════════════════════════════════════════
  window.lmMergeToSearch = async function (sourceCid, sourceName) {
    const Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-merge-search',
      '<div id="lm-merge-search-body">'
      + '<div style="margin-bottom:12px"><b>源 Lead:</b> ' + _safe(sourceName) + ' <code style="font-size:10px">' + _safe(sourceCid.substring(0, 12)) + '…</code></div>'
      + '<div style="margin-bottom:8px;font-size:12px;color:var(--text-muted)">搜索目标 Lead (将合并到目标):</div>'
      + '<input id="lm-merge-target-q" type="text" placeholder="输入名字或 canonical_id…" style="width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:6px;background:var(--bg-main);color:var(--text);font-size:13px" onkeydown="if(event.key===\'Enter\')lmMergeSearchExec(\'' + _safe(sourceCid) + '\')">'
      + '<div id="lm-merge-candidates" style="margin-top:10px;max-height:300px;overflow-y:auto"></div>'
      + '</div>', { maxWidth: '500px' });
    setTimeout(function() {
      var el = document.getElementById('lm-merge-target-q');
      if (el) el.focus();
    }, 200);
  };

  window.lmMergeSearchExec = async function (sourceCid) {
    const Shell = _shell();
    if (!Shell) return;
    var q = (document.getElementById('lm-merge-target-q') || {}).value || '';
    if (!q.trim()) return;
    var container = document.getElementById('lm-merge-candidates');
    if (!container) return;
    container.innerHTML = '<div style="padding:12px;color:var(--text-muted)">搜索中…</div>';
    try {
      var r = await Shell.api.get('/lead-mesh/leads/search?name_like=' + encodeURIComponent(q.trim()) + '&limit=10');
      var leads = r.results || r.leads || [];
      if (!leads.length) {
        container.innerHTML = '<div style="padding:12px;color:var(--text-muted)">未找到匹配的 Lead</div>';
        return;
      }
      container.innerHTML = leads.filter(function(l) { return l.canonical_id !== sourceCid; })
        .map(function(l) {
          return '<div style="padding:8px 12px;border:1px solid var(--border);border-radius:6px;margin-bottom:6px;display:flex;justify-content:space-between;align-items:center">'
            + '<div><div style="font-size:13px;font-weight:600">' + _safe(l.primary_name || l.display_name || '(无名)') + '</div>'
            + '<code style="font-size:10px;color:var(--text-muted)">' + _safe((l.canonical_id || '').substring(0, 16)) + '</code></div>'
            + '<button onclick="lmConfirmMerge(\'' + _safe(sourceCid) + '\',\'' + _safe(l.canonical_id) + '\',\'' + _safe(l.primary_name || l.display_name || '') + '\')" style="background:rgba(251,191,36,.15);color:#eab308;border:1px solid rgba(251,191,36,.3);padding:4px 10px;border-radius:4px;cursor:pointer;font-size:11px">合并到此</button>'
            + '</div>';
        }).join('');
    } catch (e) {
      container.innerHTML = '<div style="color:#ef4444">搜索失败: ' + _safe(e.message || e) + '</div>';
    }
  };

  window.lmConfirmMerge = async function (sourceCid, targetCid, targetName) {
    const Shell = _shell();
    if (!Shell) return;
    if (!confirm('确认合并？\n\n源 (将被合并): ' + sourceCid.substring(0, 12) + '…\n目标 (保留): ' + targetName + ' (' + targetCid.substring(0, 12) + '…)\n\n合并后源 Lead 的所有身份和事件将归属到目标 Lead。')) return;
    try {
      await Shell.api.post('/lead-mesh/leads/merge', {
        source_canonical_id: sourceCid,
        target_canonical_id: targetCid,
        merged_by: 'human_dashboard',
        reason: 'manual_dossier_merge'
      });
      showToast('合并成功!', 'success');
      PlatShell.modal.close('lm-merge-search');
      // 刷新 dossier 到目标
      if (typeof lmOpenLeadDossier === 'function') {
        lmOpenLeadDossier(targetCid);
      }
    } catch (e) {
      showToast('合并失败: ' + (e.message || e), 'error');
    }
  };

  // ════════════════════════════════════════════════════════
  // Phase H1: 统一身份解析 KPI 面板
  // ════════════════════════════════════════════════════════
  window.lmOpenIdentityKPI = async function () {
    var Shell = _shell();
    if (!Shell) return;
    Shell.modal.open('lm-identity-kpi',
      '<div id="lm-kpi-body">加载中…</div>', { maxWidth: '780px' });
    try {
      var _whPromise = Shell.api.get('/lead-mesh/webhooks/stats?since_days=7').catch(function(){return {};});
      var _auditPromise = Shell.api.get('/lead-mesh/leads/audit').catch(function(){return {};});
      var r = await Shell.api.get('/lead-mesh/leads/identity-kpi?since_days=30');
      var _whStats = await _whPromise;
      var _auditResult = await _auditPromise;
      r._webhook_stats = _whStats || {};
      r._audit = _auditResult || {};
      var plat = r.identities_by_platform || {};
      var ms = r.merge_stats || {};
      var trend = r.daily_new_leads || [];

      // 平台分布 bar
      var platEntries = Object.entries(plat).sort(function(a,b){return b[1]-a[1];});
      var platTotal = r.total_identities || 1;
      var platHtml = platEntries.map(function(kv) {
        var pct = (kv[1] / platTotal * 100).toFixed(1);
        return '<div style="display:flex;align-items:center;gap:8px;margin-bottom:3px">'
          + '<span style="font-size:10px;width:70px;color:var(--text-muted);text-overflow:ellipsis;overflow:hidden">' + _safe(kv[0]) + '</span>'
          + '<div style="flex:1;height:12px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden">'
          + '<div style="height:100%;width:' + pct + '%;background:#818cf8;border-radius:3px"></div></div>'
          + '<span style="font-size:10px;width:55px;text-align:right;color:#818cf8">' + kv[1] + '</span></div>';
      }).join('');

      // 趋势 sparkline (纯文本表格)
      var trendHtml = trend.map(function(t) {
        return '<tr><td style="padding:2px 8px;font-size:11px">' + (t.date||'').substr(5) + '</td>'
          + '<td style="padding:2px 8px;font-size:11px;color:#818cf8">' + (t.count||0) + '</td></tr>';
      }).join('');

      document.getElementById('lm-kpi-body').innerHTML = ''
        + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:14px">'
        + '<h3 style="margin:0">🌐 统一身份解析 KPI (近 30 天)</h3>'
        + '<button onclick="PlatShell.modal.close(\'lm-identity-kpi\')" style="background:none;border:1px solid var(--border);color:var(--text);padding:4px 10px;border-radius:6px;cursor:pointer">✕</button>'
        + '</div>'
        // KPI Cards row
        + (function() { var dd = r.cross_device_dedup || {}; return ''
        + '<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:16px">'
        + '<div style="background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.3);border-radius:8px;padding:10px;text-align:center">'
        + '<div style="font-size:22px;font-weight:700;color:#818cf8">' + (r.active_leads||0) + '</div>'
        + '<div style="font-size:10px;color:var(--text-muted)">活跃 Lead</div></div>'
        + '<div style="background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.3);border-radius:8px;padding:10px;text-align:center">'
        + '<div style="font-size:22px;font-weight:700;color:#22c55e">' + (r.total_identities||0) + '</div>'
        + '<div style="font-size:10px;color:var(--text-muted)">总身份数</div></div>'
        + '<div style="background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.3);border-radius:8px;padding:10px;text-align:center">'
        + '<div style="font-size:22px;font-weight:700;color:#f59e0b">' + (r.cross_platform_leads||0) + '</div>'
        + '<div style="font-size:10px;color:var(--text-muted)">跨平台 Lead</div></div>'
        + '<div style="background:rgba(59,130,246,.08);border:1px solid rgba(59,130,246,.3);border-radius:8px;padding:10px;text-align:center">'
        + '<div style="font-size:22px;font-weight:700;color:#3b82f6">' + (r.avg_identities_per_lead||0) + '</div>'
        + '<div style="font-size:10px;color:var(--text-muted)">身份/Lead</div></div>'
        + '<div style="background:rgba(168,85,247,.08);border:1px solid rgba(168,85,247,.3);border-radius:8px;padding:10px;text-align:center">'
        + '<div style="font-size:22px;font-weight:700;color:#a855f7">' + (ms.total||0) + '</div>'
        + '<div style="font-size:10px;color:var(--text-muted)">合并 (A' + (ms.auto||0) + '/M' + (ms.manual||0) + '/R' + (ms.reverted||0) + ')</div></div>'
        + '<div style="background:rgba(239,68,68,.08);border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:10px;text-align:center">'
        + '<div style="font-size:22px;font-weight:700;color:#ef4444">' + (dd.total_blocks||0) + '</div>'
        + '<div style="font-size:10px;color:var(--text-muted)">跨设备拦截 (' + (dd.unique_leads_saved||0) + ' leads)</div></div>'
        + '</div>'; })()
        // K2: Lifecycle funnel
        + (function() {
          var lc = r.lifecycle || {};
          var stages = lc.stages || {};
          var lcTotal = lc.total || 0;
          if (lcTotal === 0) return '';
          var defs = [
            {key:'new', label:'新建', color:'#64748b'},
            {key:'contacted', label:'已触达', color:'#3b82f6'},
            {key:'engaged', label:'互动中', color:'#8b5cf6'},
            {key:'qualified', label:'合格', color:'#22c55e'},
            {key:'converted', label:'已转化', color:'#f59e0b'},
            {key:'lost', label:'流失', color:'#ef4444'},
          ];
          // X1: SVG 漏斗图
          var W = 420, rowH = 36, pad = 4, svgH = defs.length * (rowH + pad) + 10;
          var maxCnt = Math.max.apply(null, defs.map(function(d){ return stages[d.key]||0; })) || 1;
          var svgParts = ['<svg width="' + W + '" height="' + svgH + '" xmlns="http://www.w3.org/2000/svg">'];
          defs.forEach(function(d, i) {
            var cnt = stages[d.key] || 0;
            var pct = cnt / lcTotal * 100;
            var bw = Math.max(30, (cnt / maxCnt) * (W - 120));
            var topW = (i === 0) ? bw : Math.max(30, ((stages[defs[i-1].key]||0) / maxCnt) * (W - 120));
            var y = i * (rowH + pad);
            var x1 = (W - 120 - topW) / 2 + 60;
            var x2 = x1 + topW;
            var x3 = (W - 120 - bw) / 2 + 60 + bw;
            var x4 = (W - 120 - bw) / 2 + 60;
            svgParts.push(
              '<g onclick="lmSearchByStage(\'' + d.key + '\')" style="cursor:pointer">'
              + '<polygon points="' + x1 + ',' + y + ' ' + x2 + ',' + y + ' ' + x3 + ',' + (y+rowH) + ' ' + x4 + ',' + (y+rowH) + '"'
              + ' fill="' + d.color + '" opacity="0.75" rx="4">'
              + '<title>' + d.label + ': ' + cnt + ' (' + pct.toFixed(1) + '%)</title></polygon>'
              + '<text x="' + (W/2) + '" y="' + (y + rowH/2 + 4) + '" text-anchor="middle" fill="#fff" font-size="11" font-weight="600">'
              + d.label + ' ' + cnt + ' (' + pct.toFixed(1) + '%)</text>'
              + '</g>');
          });
          svgParts.push('</svg>');
          return '<div style="margin-bottom:14px">'
            + '<div style="font-size:12px;font-weight:600;margin-bottom:6px">🔄 生命周期漏斗 (共 ' + lcTotal + ') <span style="font-size:9px;color:var(--text-dim);font-weight:400">点击阶段搜索</span></div>'
            + '<div style="text-align:center">' + svgParts.join('') + '</div></div>';
        })()
        // Three columns: platform dist + lifecycle trend + conversion rates
        + '<div style="display:grid;grid-template-columns:1fr 2fr 1fr;gap:14px">'
        + '<div><div style="font-size:12px;font-weight:600;margin-bottom:6px">📊 平台身份分布</div>' + (platHtml || '<span style="color:var(--text-dim)">无数据</span>') + '</div>'
        + '<div><div style="font-size:12px;font-weight:600;margin-bottom:6px">📈 生命周期趋势 (每日)</div>'
        + (function() {
          var lt = r.lifecycle_trend || [];
          if (!lt.length) return '<span style="color:var(--text-dim)">暂无趋势数据</span>';
          var cols = [
            {key:'contacted', label:'触达', color:'#3b82f6'},
            {key:'engaged', label:'互动', color:'#8b5cf6'},
            {key:'qualified', label:'合格', color:'#22c55e'},
            {key:'converted', label:'转化', color:'#f59e0b'},
            {key:'lost', label:'流失', color:'#ef4444'},
          ];
          var h = '<table style="width:100%;border-collapse:collapse"><thead><tr style="background:rgba(255,255,255,.03)">'
            + '<th style="text-align:left;padding:2px 6px;font-size:10px">日期</th>';
          cols.forEach(function(c){ h += '<th style="text-align:right;padding:2px 6px;font-size:10px;color:' + c.color + '">' + c.label + '</th>'; });
          h += '</tr></thead><tbody>';
          lt.slice(0, 7).forEach(function(row) {
            h += '<tr><td style="padding:2px 6px;font-size:10px">' + (row.date||'').substr(5) + '</td>';
            cols.forEach(function(c) {
              var v = row[c.key] || 0;
              h += '<td style="text-align:right;padding:2px 6px;font-size:10px;color:' + (v > 0 ? c.color : 'var(--text-dim)') + '">' + v + '</td>';
            });
            h += '</tr>';
          });
          return h + '</tbody></table>';
        })()
        + '</div>'
        + '<div><div style="font-size:12px;font-weight:600;margin-bottom:6px">📋 转化率</div>'
        + (function() {
          var lc = r.lifecycle || {}; var s = lc.stages || {}; var t = lc.total || 1;
          var contacted = (s.contacted||0)+(s.engaged||0)+(s.qualified||0)+(s.converted||0);
          var engaged = (s.engaged||0)+(s.qualified||0)+(s.converted||0);
          return '<div style="font-size:11px;line-height:1.8">'
            + '触达率: <b style="color:#3b82f6">' + (contacted/t*100).toFixed(1) + '%</b><br>'
            + '互动率: <b style="color:#8b5cf6">' + (engaged/t*100).toFixed(1) + '%</b><br>'
            + '合格率: <b style="color:#22c55e">' + (((s.qualified||0)+(s.converted||0))/Math.max(t,1)*100).toFixed(1) + '%</b><br>'
            + '转化率: <b style="color:#f59e0b">' + ((s.converted||0)/Math.max(t,1)*100).toFixed(1) + '%</b></div>';
        })()
        + '</div></div>'
        // P2: 阶段停留时长
        + (function() {
          var dw = r.lifecycle_dwell || {};
          var hasData = Object.keys(dw).some(function(k){ return dw[k] && dw[k].avg_days !== null; });
          if (!hasData) return '';
          var stageLabels = {'new':'新建','contacted':'触达','engaged':'互动','qualified':'合格','converted':'转化','lost':'流失'};
          var stageColors = {'new':'#64748b','contacted':'#3b82f6','engaged':'#8b5cf6','qualified':'#22c55e','converted':'#f59e0b','lost':'#ef4444'};
          var h = '<div style="margin-top:14px"><div style="font-size:12px;font-weight:600;margin-bottom:6px">⏱ 平均停留时长</div>'
            + '<div style="display:flex;gap:8px;flex-wrap:wrap">';
          ['new','contacted','engaged','qualified'].forEach(function(s){
            var d = dw[s] || {};
            if (d.avg_days === null) return;
            h += '<div style="padding:6px 10px;background:rgba(255,255,255,.04);border:1px solid var(--border);border-radius:6px;text-align:center">'
              + '<div style="font-size:14px;font-weight:700;color:' + stageColors[s] + '">' + d.avg_days + 'd</div>'
              + '<div style="font-size:9px;color:var(--text-muted)">' + stageLabels[s] + ' (n=' + d.samples + ')</div></div>';
          });
          return h + '</div></div>';
        })()
        // T2: 评分排行榜 + 分布
        + (function() {
          var sb = r.score_leaderboard || {};
          var topList = sb.top || [];
          var dist = sb.distribution || {};
          if (!topList.length && !sb.total_scored) return '';
          var h = '<div style="margin-top:14px;display:grid;grid-template-columns:1fr 1fr;gap:14px">';
          // 排行榜
          h += '<div><div style="font-size:12px;font-weight:600;margin-bottom:6px">🏆 Lead 评分 Top 10 (平均 ' + (sb.avg_score||0) + ')</div>';
          topList.slice(0,10).forEach(function(l, i) {
            var c = l.lead_score>=70?'#22c55e':l.lead_score>=40?'#f59e0b':'#ef4444';
            h += '<div style="display:flex;justify-content:space-between;align-items:center;font-size:11px;padding:2px 0;border-bottom:1px solid rgba(255,255,255,.04)">'
              + '<span onclick="lmOpenLeadDossier(\'' + _safe(l.canonical_id) + '\')" style="cursor:pointer;text-decoration:underline;color:var(--text-main)">'
              + (i+1) + '. ' + _safe(l.primary_name || l.canonical_id.substring(0,8)) + '</span>'
              + '<span style="font-weight:700;color:' + c + '">' + l.lead_score + '</span></div>';
          });
          h += '</div>';
          // 分布
          h += '<div><div style="font-size:12px;font-weight:600;margin-bottom:6px">📊 评分分布 (共 ' + (sb.total_scored||0) + ')</div>';
          var maxN = Math.max(dist['0-20']||0, dist['21-40']||0, dist['41-60']||0, dist['61-80']||0, dist['81-100']||0, 1);
          var bands = [
            {k:'0-20',label:'0-20',color:'#ef4444'},{k:'21-40',label:'21-40',color:'#f59e0b'},
            {k:'41-60',label:'41-60',color:'#eab308'},{k:'61-80',label:'61-80',color:'#22c55e'},
            {k:'81-100',label:'81-100',color:'#10b981'}];
          bands.forEach(function(b) {
            var n = dist[b.k] || 0;
            var pct = (n / maxN * 100).toFixed(0);
            h += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">'
              + '<span style="font-size:10px;width:36px;color:var(--text-dim)">' + b.label + '</span>'
              + '<div style="flex:1;height:10px;background:rgba(255,255,255,.05);border-radius:3px;overflow:hidden">'
              + '<div style="height:100%;width:' + pct + '%;background:' + b.color + ';border-radius:3px"></div></div>'
              + '<span style="font-size:10px;width:28px;text-align:right;color:' + b.color + '">' + n + '</span></div>';
          });
          h += '</div></div>';
          return h;
        })()
        // O1: 漏斗告警
        + (function() {
          var al = r.lifecycle_alerts || [];
          if (!al.length) return '';
          var h = '<div style="margin-top:14px;padding:10px 14px;background:rgba(251,191,36,.08);border:1px solid rgba(251,191,36,.25);border-radius:8px">'
            + '<div style="font-size:12px;font-weight:600;color:#f59e0b;margin-bottom:6px">⚠️ 漏斗瓶颈告警 (' + al.length + ')</div>';
          al.forEach(function(a) {
            h += '<div style="font-size:11px;color:var(--text-main);margin-bottom:3px">• ' + _safe(a.message || '') + '</div>';
          });
          return h + '</div>';
        })()
        // R4: SLA at-risk leads
        + (function() {
          var sla = r.lifecycle_sla || {};
          var leads = sla.leads || [];
          if (!leads.length) return '';
          var h = '<div style="margin-top:14px;padding:10px 14px;background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.2);border-radius:8px">'
            + '<div style="font-size:12px;font-weight:600;color:#ef4444;margin-bottom:6px">⏰ SLA 超时 (' + sla.at_risk_count + ' 条)</div>';
          leads.slice(0, 5).forEach(function(l) {
            h += '<div style="font-size:11px;margin-bottom:3px;display:flex;justify-content:space-between">'
              + '<span onclick="lmOpenLeadDossier(\'' + _safe(l.canonical_id) + '\')" style="cursor:pointer;color:var(--text-main);text-decoration:underline">'
              + _safe(l.primary_name || l.canonical_id.substring(0,8)) + '</span>'
              + '<span style="color:var(--text-dim)">' + _safe(l.stage) + ' · <b style="color:#ef4444">' + (l.dwell_days||'?') + '天</b> (SLA ' + l.sla_days + '天)</span></div>';
          });
          return h + '</div>';
        })()
        // Y1: Webhook 监控
        + (function() {
          var wh = r._webhook_stats || {};
          if (!wh.total) return '';
          var bs = wh.by_status || {};
          var be = wh.by_event_type || {};
          var rf = wh.recent_failures || [];
          var statusColors = {delivered:'#22c55e',pending:'#f59e0b',dead_letter:'#ef4444',failed:'#ef4444'};
          var h = '<div style="margin-top:14px;padding:10px 14px;background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.2);border-radius:8px">'
            + '<div style="font-size:12px;font-weight:600;color:#818cf8;margin-bottom:8px">📡 Webhook 监控 (近7天, 共 ' + wh.total + ')</div>';
          // status 卡片
          h += '<div style="display:flex;gap:8px;margin-bottom:8px;flex-wrap:wrap">';
          Object.keys(bs).forEach(function(s) {
            var c = statusColors[s] || '#64748b';
            h += '<div style="padding:4px 10px;background:' + c + '15;border:1px solid ' + c + '40;border-radius:5px;text-align:center">'
              + '<div style="font-size:16px;font-weight:700;color:' + c + '">' + bs[s] + '</div>'
              + '<div style="font-size:9px;color:var(--text-dim)">' + s + '</div></div>';
          });
          h += '</div>';
          // event type 分布
          var evKeys = Object.keys(be);
          if (evKeys.length) {
            h += '<div style="font-size:10px;color:var(--text-muted);margin-bottom:3px">事件类型:</div>';
            evKeys.forEach(function(k) {
              h += '<span style="display:inline-block;font-size:10px;margin:1px 4px 1px 0;padding:2px 6px;background:rgba(255,255,255,.04);border-radius:3px">'
                + _safe(k) + ' ×' + be[k] + '</span>';
            });
          }
          // 最近失败
          if (rf.length) {
            h += '<div style="margin-top:8px;font-size:10px;color:var(--text-muted)">最近失败:</div>';
            rf.forEach(function(f) {
              h += '<div style="font-size:10px;margin-bottom:2px;color:#ef4444">'
                + '• #' + f.id + ' ' + _safe(f.event_type) + ' → ' + _safe((f.target_url||'').substring(0,40)) + '…'
                + ' <span style="color:var(--text-dim)">(' + f.attempt_count + '次)</span>'
                + (f.status==='dead_letter' ? ' <button onclick="lmRetryDeadLetter(' + f.id + ')" style="font-size:9px;padding:1px 6px;cursor:pointer;background:#ef444420;color:#ef4444;border:1px solid #ef444440;border-radius:3px">重试</button>' : '')
                + '</div>';
            });
          }
          return h + '</div>';
        })()
        // Z1: 数据完整性审计
        + (function() {
          var au = r._audit || {};
          var s = au.summary || {};
          if (!s.has_issues) return '<div style="margin-top:14px;padding:8px 14px;background:rgba(34,197,94,.06);border:1px solid rgba(34,197,94,.2);border-radius:8px;font-size:11px;color:#22c55e">✅ 数据完整性检查: 无异常</div>';
          var h = '<div style="margin-top:14px;padding:10px 14px;background:rgba(251,146,60,.06);border:1px solid rgba(251,146,60,.2);border-radius:8px">'
            + '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">'
            + '<div style="font-size:12px;font-weight:600;color:#fb923c">🔍 数据完整性审计</div>'
            + '<button onclick="lmRunAuditFix()" style="font-size:9px;padding:2px 8px;cursor:pointer;background:#fb923c20;color:#fb923c;border:1px solid #fb923c40;border-radius:3px">自动修复</button></div>';
          var checks = [
            {k:'orphan_count', label:'孤儿身份', icon:'👻', color:'#ef4444'},
            {k:'empty_journey_count', label:'无事件 lead', icon:'📭', color:'#f59e0b'},
            {k:'invalid_lifecycle_count', label:'非法状态', icon:'⚠️', color:'#ef4444'},
            {k:'duplicate_identity_count', label:'重复身份', icon:'🔗', color:'#a855f7'},
          ];
          h += '<div style="display:flex;gap:8px;flex-wrap:wrap">';
          checks.forEach(function(c) {
            var n = s[c.k] || 0;
            if (n === 0) return;
            h += '<div style="padding:4px 10px;background:' + c.color + '12;border:1px solid ' + c.color + '30;border-radius:5px;text-align:center">'
              + '<div style="font-size:14px;font-weight:700;color:' + c.color + '">' + n + '</div>'
              + '<div style="font-size:9px;color:var(--text-dim)">' + c.icon + ' ' + c.label + '</div></div>';
          });
          h += '</div>';
          return h + '</div>';
        })();
    } catch (e) {
      document.getElementById('lm-kpi-body').innerHTML =
        '<div style="color:#ef4444">加载失败: ' + _safe(e.message || e) + '</div>';
    }
  };

})();
