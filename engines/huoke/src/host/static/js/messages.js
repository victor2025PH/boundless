// messages.js — TikTok 话术模板管理页面
// P1.2: Message template management with visual editor

const Msg = (() => {
  let _data = {};  // local copy of chat_messages.yaml

  async function refresh() {
    try {
      const [r, stats] = await Promise.all([
        api('GET', '/tiktok/messages'),
        api('GET', '/tiktok/messages/stats').catch(() => ({})),
      ]);
      _data = r;
      _render(r);
      _set('msg-total-sent', stats.total_sent ?? 0);
      _set('msg-total-replied', stats.total_auto_replied ?? 0);
      _set('msg-country', r.country || 'italy');
      await _loadDeviceList();
    } catch(e) {
      console.warn('[Msg] refresh error:', e);
    }
  }

  function _render(r) {
    _renderList('msg-greeting-list', r.greeting_messages || [], 'greeting');
    _renderList('msg-tg-list', r.referral_telegram || [], 'tg');
    _renderList('msg-wa-list', r.referral_whatsapp || [], 'wa');
    _renderDeviceRefs(r.device_referrals || {});
  }

  function _renderList(containerId, items, type) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (items.length === 0) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:6px 0">暂无模板，点击下方添加</div>';
      return;
    }
    el.innerHTML = items.map((msg, i) => `
      <div style="display:flex;gap:6px;align-items:start" data-type="${type}" data-idx="${i}">
        <textarea rows="2" style="flex:1;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;color:var(--text-main);font-size:12px;resize:vertical;font-family:inherit"
          onchange="Msg._update('${type}',${i},this.value)">${_esc(msg)}</textarea>
        <div style="display:flex;flex-direction:column;gap:4px">
          <button onclick="Msg._remove('${type}',${i})" title="删除" style="background:none;border:1px solid var(--border);border-radius:4px;color:#ef4444;cursor:pointer;padding:2px 6px;font-size:11px">&#128465;</button>
          <button onclick="Msg._moveUp('${type}',${i})" title="上移" style="background:none;border:1px solid var(--border);border-radius:4px;color:var(--text-muted);cursor:pointer;padding:2px 6px;font-size:11px" ${i===0?'disabled':''}>&#8593;</button>
        </div>
      </div>
    `).join('');
  }

  function _renderDeviceRefs(refs) {
    const el = document.getElementById('msg-device-refs');
    if (!el) return;
    if (Object.keys(refs).length === 0) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:12px">暂无设备配置。请先在 VPN 管理页面连接设备。</div>';
      return;
    }
    el.innerHTML = Object.entries(refs).map(([did, cfg]) => `
      <div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px">
        <div style="font-size:11px;font-weight:600;margin-bottom:8px;color:var(--text-muted)">${_esc(did.slice(0,8))}...</div>
        <div style="display:flex;flex-direction:column;gap:6px">
          <div style="display:flex;gap:6px;align-items:center">
            <span style="font-size:11px;min-width:28px">TG:</span>
            <input type="text" value="${_esc(cfg.telegram||'')}" style="flex:1;background:var(--bg-card);border:1px solid var(--border);border-radius:4px;padding:4px 6px;color:var(--text-main);font-size:11px"
              onchange="Msg._updateRef('${did}','telegram',this.value)" placeholder="@username">
          </div>
          <div style="display:flex;gap:6px;align-items:center">
            <span style="font-size:11px;min-width:28px">WA:</span>
            <input type="text" value="${_esc(cfg.whatsapp||'')}" style="flex:1;background:var(--bg-card);border:1px solid var(--border);border-radius:4px;padding:4px 6px;color:var(--text-main);font-size:11px"
              onchange="Msg._updateRef('${did}','whatsapp',this.value)" placeholder="+1234567890">
          </div>
        </div>
      </div>
    `).join('');
  }

  async function _loadDeviceList() {
    try {
      const r = await api('GET', '/tiktok/referral-config');
      const refs = r.referrals || {};
      const sel = document.getElementById('preview-device');
      if (!sel) return;
      sel.innerHTML = Object.keys(refs).map(did =>
        `<option value="${_esc(did)}">${_esc(did.slice(0,8))}</option>`
      ).join('') || '<option value="">无设备</option>';
    } catch(e) {}
  }

  function _update(type, idx, val) {
    const map = {
      greeting: 'greeting_messages',
      tg: 'referral_telegram',
      wa: 'referral_whatsapp',
    };
    const key = map[type];
    if (key && _data[key]) _data[key][idx] = val;
  }

  function _updateRef(did, field, val) {
    if (!_data.device_referrals) _data.device_referrals = {};
    if (!_data.device_referrals[did]) _data.device_referrals[did] = {};
    _data.device_referrals[did][field] = val;
  }

  function _remove(type, idx) {
    const map = { greeting: 'greeting_messages', tg: 'referral_telegram', wa: 'referral_whatsapp' };
    const key = map[type];
    if (key && _data[key]) {
      _data[key].splice(idx, 1);
      _renderList(`msg-${type === 'greeting' ? 'greeting' : type}-list`, _data[key], type);
    }
  }

  function _moveUp(type, idx) {
    if (idx === 0) return;
    const map = { greeting: 'greeting_messages', tg: 'referral_telegram', wa: 'referral_whatsapp' };
    const key = map[type];
    if (key && _data[key]) {
      [_data[key][idx-1], _data[key][idx]] = [_data[key][idx], _data[key][idx-1]];
      _renderList(`msg-${type === 'greeting' ? 'greeting' : type}-list`, _data[key], type);
    }
  }

  function addGreeting() {
    if (!_data.greeting_messages) _data.greeting_messages = [];
    _data.greeting_messages.push('Ciao {name}! ');
    _renderList('msg-greeting-list', _data.greeting_messages, 'greeting');
  }

  function addTg() {
    if (!_data.referral_telegram) _data.referral_telegram = [];
    _data.referral_telegram.push('Scrivimi su Telegram: {telegram}');
    _renderList('msg-tg-list', _data.referral_telegram, 'tg');
  }

  function addWa() {
    if (!_data.referral_whatsapp) _data.referral_whatsapp = [];
    _data.referral_whatsapp.push('Contattami su WhatsApp: {whatsapp}');
    _renderList('msg-wa-list', _data.referral_whatsapp, 'wa');
  }

  async function save() {
    try {
      const payload = {
        greeting_messages: _data.greeting_messages || [],
        referral_telegram: _data.referral_telegram || [],
        referral_whatsapp: _data.referral_whatsapp || [],
        messages: _data.messages || [],
        country: document.getElementById('msg-country')?.textContent || _data.country || 'italy',
      };
      const r = await api('PUT', '/tiktok/messages', payload);
      if (r.ok) {
        // Also save device referrals via existing endpoint
        for (const [did, cfg] of Object.entries(_data.device_referrals || {})) {
          await api('POST', '/tiktok/referral-config', {
            device_id: did,
            telegram: cfg.telegram || '',
            whatsapp: cfg.whatsapp || '',
          }).catch(() => {});
        }
        showToast('话术模板已保存', 'success');
      }
    } catch(e) {
      showToast('保存失败: ' + e, 'error');
    }
  }

  async function preview() {
    const tmpl = document.getElementById('preview-template')?.value || '';
    const did = document.getElementById('preview-device')?.value || '';
    const resultEl = document.getElementById('preview-result');
    if (!tmpl || !resultEl) return;
    try {
      const r = await api('POST', '/tiktok/messages/preview', {
        template: tmpl, device_id: did, name: 'Marco'
      });
      resultEl.style.color = 'var(--text-main)';
      resultEl.innerHTML = `
        <div style="font-size:12px;margin-bottom:6px">${_esc(r.rendered)}</div>
        <div style="font-size:10px;color:var(--text-muted)">TG: ${_esc(r.telegram)} · WA: ${_esc(r.whatsapp)}</div>
      `;
    } catch(e) {
      resultEl.style.color = '#ef4444';
      resultEl.textContent = '预览失败: ' + e;
    }
  }

  function _esc(s) {
    return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  function _set(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val;
  }

  // Register page loader in overview.js _PAGE_LOADERS
  window._PAGE_LOADERS = window._PAGE_LOADERS || {};

  return { refresh, save, preview, addGreeting, addTg, addWa, _update, _updateRef, _remove, _moveUp };
})();

async function _loadAbStats(){
  try{
    const r=await api('GET','/tiktok/messages/ab-stats');
    const el=document.getElementById('ab-stats-body');
    if(!el) return;
    const variants=r.variants||[];
    if(!variants.length){
      el.innerHTML='<div style="text-align:center;color:var(--text-muted);padding:20px;font-size:12px">尚未配置 A/B 测试变体</div>';
      return;
    }
    const maxSent=Math.max(...variants.map(v=>v.sent),1);
    el.innerHTML=`
      <table style="width:100%;border-collapse:collapse">
        <thead><tr style="border-bottom:1px solid var(--border)">
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:var(--text-dim);font-weight:500">变体</th>
          <th style="padding:8px 12px;text-align:center;font-size:11px;color:var(--text-dim);font-weight:500">发送</th>
          <th style="padding:8px 12px;text-align:center;font-size:11px;color:var(--text-dim);font-weight:500">回复</th>
          <th style="padding:8px 12px;text-align:center;font-size:11px;color:var(--text-dim);font-weight:500">回复率</th>
          <th style="padding:8px 12px;text-align:left;font-size:11px;color:var(--text-dim);font-weight:500">发送量</th>
        </tr></thead>
        <tbody>
          ${variants.map(v=>{
            const rateColor=v.reply_rate>=50?'#22c55e':v.reply_rate>=20?'#fbbf24':'#94a3b8';
            const barPct=Math.round(v.sent/maxSent*100);
            const winner=variants.length>1&&v.reply_rate===Math.max(...variants.map(x=>x.reply_rate))&&v.sent>0;
            return `<tr style="border-bottom:1px solid rgba(255,255,255,.04)">
              <td style="padding:8px 12px">
                <div style="font-weight:600;color:var(--text)">${v.name}${winner?' <span style="font-size:10px;color:#fbbf24">&#128081; 领先</span>':''}</div>
                <div style="font-size:10px;color:var(--text-dim)">${v.description||v.id}</div>
              </td>
              <td style="padding:8px 12px;text-align:center;color:#60a5fa">${v.sent}</td>
              <td style="padding:8px 12px;text-align:center;color:#a78bfa">${v.replied}</td>
              <td style="padding:8px 12px;text-align:center;font-weight:700;color:${rateColor}">${v.reply_rate}%</td>
              <td style="padding:8px 12px">
                <div style="height:6px;background:var(--border);border-radius:3px;width:120px">
                  <div style="height:100%;width:${barPct}%;background:#3b82f6;border-radius:3px"></div>
                </div>
              </td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    `;
  }catch(e){
    const el=document.getElementById('ab-stats-body');
    if(el) el.innerHTML='<div style="color:var(--text-muted);padding:16px;font-size:12px">加载失败</div>';
  }
}
