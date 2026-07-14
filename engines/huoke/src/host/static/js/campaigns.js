// campaigns.js — Campaign（引流活动）管理
'use strict';

let _campaigns = [];
let _campaignRefreshTimer = null;

function initCampaignsPage() {
  loadCampaigns();
  if (_campaignRefreshTimer) clearInterval(_campaignRefreshTimer);
  _campaignRefreshTimer = setInterval(loadCampaigns, 10000);
}

function loadCampaigns() {
  api('GET', '/campaigns').then(data => {
    _campaigns = data || [];
    renderCampaignList();
  }).catch(() => {});
}

function renderCampaignList() {
  const el = document.getElementById('campaign-list');
  if (!el) return;
  if (!_campaigns.length) {
    el.innerHTML = '<div style="text-align:center;padding:40px;color:var(--text-muted)">暂无活动，点击「新建活动」开始</div>';
    return;
  }
  el.innerHTML = _campaigns.map(c => {
    const stats = c.stats || {};
    const pct = stats.progress || 0;
    const statusColor = {draft:'#94a3b8', active:'#4ade80', paused:'#fb923c', completed:'#60a5fa'}[c.status] || '#94a3b8';
    const statusLabel = {draft:'草稿', active:'运行中', paused:'已暂停', completed:'已完成'}[c.status] || c.status;
    const devices = (c.device_ids || []).length;
    const targets = (c.target_accounts || []).length;
    return `<div class="campaign-card" data-id="${c.campaign_id}">
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div>
          <div style="font-weight:600;font-size:14px;margin-bottom:4px">${c.name}</div>
          <div style="font-size:11px;color:var(--text-muted)">${c.description || '无描述'}</div>
          <div style="margin-top:6px;display:flex;gap:10px;font-size:11px;color:var(--text-muted)">
            <span>设备: <b style="color:var(--text)">${devices}</b></span>
            <span>目标账号: <b style="color:var(--text)">${targets}</b></span>
            <span>AI改写: <b style="color:${c.ai_rewrite?'#4ade80':'#94a3b8'}">${c.ai_rewrite?'开':'关'}</b></span>
          </div>
        </div>
        <span style="font-size:11px;font-weight:600;padding:3px 10px;border-radius:20px;background:${statusColor}22;color:${statusColor}">${statusLabel}</span>
      </div>
      ${c.batch_id ? `
      <div style="margin-top:10px">
        <div style="display:flex;justify-content:space-between;font-size:11px;color:var(--text-muted);margin-bottom:4px">
          <span>进度 ${pct}%</span>
          <span>${stats.completed||0}完成 · ${stats.failed||0}失败 · ${stats.running||0}运行中 · ${stats.pending||0}等待</span>
        </div>
        <div style="background:var(--bg-main);border-radius:4px;height:6px;overflow:hidden">
          <div style="width:${pct}%;height:100%;background:linear-gradient(90deg,#3b82f6,#8b5cf6);transition:width .3s"></div>
        </div>
      </div>` : ''}
      <div style="margin-top:10px;display:flex;gap:8px;justify-content:flex-end">
        <button class="sb-btn2" onclick="showCampaignDetail('${c.campaign_id}')">详情</button>
        ${c.status==='draft'||c.status==='paused' ? `<button class="sb-btn2" style="background:#3b82f622;border-color:#3b82f6;color:#3b82f6" onclick="startCampaign('${c.campaign_id}')">启动</button>` : ''}
        ${c.status==='active' ? `<button class="sb-btn2" style="background:#fb923c22;border-color:#fb923c;color:#fb923c" onclick="pauseCampaign('${c.campaign_id}')">暂停</button>` : ''}
        ${c.status==='active'||c.status==='paused' ? `<button class="sb-btn2" style="background:#ef444422;border-color:#ef4444;color:#ef4444" onclick="stopCampaign('${c.campaign_id}')">停止</button>` : ''}
        ${c.status!=='active' ? `<button class="sb-btn2" onclick="editCampaign('${c.campaign_id}')">编辑</button>` : ''}
        ${c.status!=='active' ? `<button class="sb-btn2" style="color:#ef4444" onclick="deleteCampaign('${c.campaign_id}')">删除</button>` : ''}
      </div>
    </div>`;
  }).join('');
}

function showCreateCampaignModal() {
  const devOptions = (allDevices||[]).map(d =>
    `<label style="display:block;padding:4px 0"><input type="checkbox" value="${d.device_id}" style="margin-right:6px">${d.display_name||d.device_id}</label>`
  ).join('') || '<div style="color:var(--text-muted);font-size:12px">暂无在线设备</div>';

  showModal('新建引流活动', `
    <div style="display:flex;flex-direction:column;gap:12px">
      <div>
        <label style="font-size:12px;color:var(--text-muted)">活动名称 *</label>
        <input id="c-name" class="form-input" placeholder="如：美妆竞品4月引流" style="width:100%;margin-top:4px">
      </div>
      <div>
        <label style="font-size:12px;color:var(--text-muted)">描述</label>
        <input id="c-desc" class="form-input" placeholder="活动描述（选填）" style="width:100%;margin-top:4px">
      </div>
      <div>
        <label style="font-size:12px;color:var(--text-muted)">目标竞品账号（每行一个TikTok用户名）</label>
        <textarea id="c-targets" class="form-input" rows="4" placeholder="@beautytips&#10;@makeuptutorials&#10;@skincare_daily" style="width:100%;margin-top:4px;resize:vertical"></textarea>
      </div>
      <div>
        <label style="font-size:12px;color:var(--text-muted)">分配设备（多选）</label>
        <div id="c-devices" style="background:var(--bg-main);border:1px solid var(--border);border-radius:6px;padding:8px;max-height:120px;overflow-y:auto;margin-top:4px">${devOptions}</div>
      </div>
      <div>
        <label style="font-size:12px;color:var(--text-muted)">任务序列（执行顺序，逗号分隔）</label>
        <input id="c-tasks" class="form-input" value="tiktok_follow,tiktok_check_and_chat_followbacks,tiktok_check_inbox" style="width:100%;margin-top:4px">
      </div>
      <div>
        <label style="font-size:12px;color:var(--text-muted)">私信话术模板（支持{name}变量）</label>
        <textarea id="c-template" class="form-input" rows="3" placeholder="Hi {name}! Love your content. I'm in the same space. Feel free to add me on WA (link in bio) 😊" style="width:100%;margin-top:4px;resize:vertical"></textarea>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <input type="checkbox" id="c-ai" checked style="width:16px;height:16px">
        <label for="c-ai" style="font-size:12px">启用 AI 改写（每条私信自动生成不同变体，避免被检测）</label>
      </div>
    </div>`,
    `<button class="sb-btn2" onclick="closeModal()">取消</button>
     <button class="sb-btn" onclick="submitCreateCampaign()">创建活动</button>`
  );
}

function submitCreateCampaign() {
  const name = document.getElementById('c-name')?.value.trim();
  if (!name) { showToast('请填写活动名称', 'warn'); return; }
  const targets = (document.getElementById('c-targets')?.value||'')
    .split('\n').map(s=>s.trim()).filter(Boolean);
  const deviceIds = [...(document.querySelectorAll('#c-devices input:checked')||[])].map(el=>el.value);
  const taskSeq = (document.getElementById('c-tasks')?.value||'').split(',').map(s=>s.trim()).filter(Boolean);
  const template = document.getElementById('c-template')?.value.trim();
  const aiRewrite = document.getElementById('c-ai')?.checked;

  api('POST', '/campaigns', {
    name, description: document.getElementById('c-desc')?.value.trim(),
    target_accounts: targets,
    device_ids: deviceIds,
    task_sequence: taskSeq,
    message_template: template,
    ai_rewrite: aiRewrite,
    params: { target_accounts: targets }
  }).then(r => {
    if (r.ok) { closeModal(); loadCampaigns(); showToast('活动已创建', 'ok'); }
    else showToast(r.message || '创建失败', 'error');
  }).catch(e => showToast('创建失败: '+e, 'error'));
}

function startCampaign(id) {
  api('POST', `/campaigns/${id}/start`).then(r => {
    if (r.ok) { showToast(`活动已启动，${r.count} 个任务开始执行`, 'ok'); loadCampaigns(); }
    else showToast(r.message || '启动失败', 'error');
  }).catch(e => showToast('启动失败: '+e, 'error'));
}

function pauseCampaign(id) {
  api('POST', `/campaigns/${id}/pause`).then(r => {
    if (r.ok) { showToast('活动已暂停', 'warn'); loadCampaigns(); }
    else showToast(r.message || '暂停失败', 'error');
  });
}

async function stopCampaign(id) {
  if (!(await ocDialog({title:'停止活动',message:'确认停止该活动？',type:'warning',confirmText:'停止',cancelText:'取消'}))) return;
  api('POST', `/campaigns/${id}/stop`).then(r => {
    if (r.ok) { showToast('活动已停止', 'warn'); loadCampaigns(); }
  });
}

async function deleteCampaign(id) {
  if (!(await ocDialog({title:'删除活动',message:'确认删除该活动？',type:'danger',confirmText:'删除',dangerous:true}))) return;
  api('DELETE', `/campaigns/${id}`).then(r => {
    if (r.ok) { showToast('已删除', 'ok'); loadCampaigns(); }
    else showToast(r.message || '删除失败', 'error');
  });
}

function editCampaign(id) {
  const c = _campaigns.find(x => x.campaign_id === id);
  if (!c) return;
  showModal('编辑活动 — ' + c.name, `
    <div style="display:flex;flex-direction:column;gap:12px">
      <div>
        <label style="font-size:12px;color:var(--text-muted)">活动名称</label>
        <input id="ce-name" class="form-input" value="${c.name||''}" style="width:100%;margin-top:4px">
      </div>
      <div>
        <label style="font-size:12px;color:var(--text-muted)">目标账号（每行一个）</label>
        <textarea id="ce-targets" class="form-input" rows="4" style="width:100%;margin-top:4px">${(c.target_accounts||[]).join('\n')}</textarea>
      </div>
      <div>
        <label style="font-size:12px;color:var(--text-muted)">任务序列</label>
        <input id="ce-tasks" class="form-input" value="${(c.task_sequence||[]).join(',')}" style="width:100%;margin-top:4px">
      </div>
      <div>
        <label style="font-size:12px;color:var(--text-muted)">私信话术模板</label>
        <textarea id="ce-template" class="form-input" rows="3" style="width:100%;margin-top:4px">${c.message_template||''}</textarea>
      </div>
      <div style="display:flex;align-items:center;gap:8px">
        <input type="checkbox" id="ce-ai" ${c.ai_rewrite?'checked':''} style="width:16px;height:16px">
        <label for="ce-ai" style="font-size:12px">启用 AI 改写</label>
      </div>
    </div>`,
    `<button class="sb-btn2" onclick="closeModal()">取消</button>
     <button class="sb-btn" onclick="submitEditCampaign('${id}')">保存</button>`
  );
}

function submitEditCampaign(id) {
  const targets = (document.getElementById('ce-targets')?.value||'').split('\n').map(s=>s.trim()).filter(Boolean);
  const taskSeq = (document.getElementById('ce-tasks')?.value||'').split(',').map(s=>s.trim()).filter(Boolean);
  api('PUT', `/campaigns/${id}`, {
    name: document.getElementById('ce-name')?.value.trim(),
    target_accounts: targets,
    task_sequence: taskSeq,
    message_template: document.getElementById('ce-template')?.value.trim(),
    ai_rewrite: document.getElementById('ce-ai')?.checked,
    params: { target_accounts: targets }
  }).then(r => {
    if (r.ok) { closeModal(); loadCampaigns(); showToast('已保存', 'ok'); }
    else showToast(r.message || '保存失败', 'error');
  });
}

function showCampaignDetail(id) {
  api('GET', `/campaigns/${id}/stats`).then(stats => {
    const c = _campaigns.find(x => x.campaign_id === id) || {};
    const seq = (c.task_sequence||[]).join(' → ') || '未配置';
    showModal('活动详情 — ' + (c.name||id), `
      <div style="display:flex;flex-direction:column;gap:14px">
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">
          ${[
            ['总任务', stats.total||0, '#3b82f6'],
            ['已完成', stats.completed||0, '#4ade80'],
            ['已失败', stats.failed||0, '#ef4444'],
            ['进度', (stats.progress||0)+'%', '#a78bfa'],
          ].map(([l,v,col])=>`<div style="background:${col}18;border:1px solid ${col}44;border-radius:8px;padding:10px;text-align:center">
            <div style="font-size:20px;font-weight:700;color:${col}">${v}</div>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">${l}</div>
          </div>`).join('')}
        </div>
        <div style="font-size:12px">
          <div style="margin-bottom:6px;color:var(--text-muted)">任务序列</div>
          <div style="background:var(--bg-main);padding:8px;border-radius:6px;font-family:monospace">${seq}</div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:12px">
          <div><span style="color:var(--text-muted)">目标账号:</span> ${(c.target_accounts||[]).length} 个</div>
          <div><span style="color:var(--text-muted)">分配设备:</span> ${(c.device_ids||[]).length} 台</div>
          <div><span style="color:var(--text-muted)">新增线索:</span> ${stats.leads_generated||0}</div>
          <div><span style="color:var(--text-muted)">已转化:</span> ${stats.leads_converted||0}</div>
          <div><span style="color:var(--text-muted)">AI改写:</span> ${c.ai_rewrite?'开启':'关闭'}</div>
          <div><span style="color:var(--text-muted)">Batch ID:</span> ${c.batch_id||'未启动'}</div>
        </div>
      </div>`,
      `<button class="sb-btn2" onclick="closeModal()">关闭</button>
       ${c.status!=='active' ? `<button class="sb-btn" onclick="closeModal();startCampaign('${id}')">启动活动</button>` : ''}`
    );
  }).catch(() => showToast('加载详情失败', 'error'));
}
