/* alerts-notify.js — 告警通知: 告警通知面板、告警规则、消息通知、通知中心 */
/* ── 告警通知面板 ── */
let _alertItems=[];
let _alertPanelOpen=false;
function toggleAlertPanel(){
  _alertPanelOpen=!_alertPanelOpen;
  document.getElementById('alert-panel').style.display=_alertPanelOpen?'block':'none';
  if(_alertPanelOpen) loadAlerts();
}
async function loadAlerts(){
  try{
    const r=await api('GET','/health/alerts?limit=30');
    _alertItems=r.alerts||[];
    _renderAlerts();
  }catch(e){}
}
function _renderAlerts(){
  const list=document.getElementById('alert-list');
  const empty=document.getElementById('alert-empty');
  if(!_alertItems.length){list.innerHTML='';empty.style.display='block';return;}
  empty.style.display='none';
  list.innerHTML=_alertItems.slice().reverse().map(a=>{
    const icons={info:'ℹ️',warning:'⚠️',error:'❌',critical:'🚨'};
    const icon=icons[a.level]||'📢';
    const did=a.device_id?ALIAS[a.device_id]||a.device_id.substring(0,8):'系统';
    const time=a.timestamp?new Date(a.timestamp).toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'}):'';
    const bgMap={critical:'rgba(239,68,68,.08)',error:'rgba(239,68,68,.05)',warning:'rgba(234,179,8,.05)',info:'transparent'};
    return `<div style="padding:8px 14px;border-bottom:1px solid rgba(255,255,255,.04);background:${bgMap[a.level]||'transparent'}">
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:13px">${icon}</span>
        <span style="font-size:11px;font-weight:600;flex:1">${did}</span>
        <span style="font-size:9px;color:var(--text-muted)">${time}</span>
      </div>
      <div style="font-size:11px;color:var(--text-dim);margin-top:2px;padding-left:19px">${a.message||''}</div>
    </div>`;
  }).join('');
}
function clearAlerts(){_alertItems=[];_renderAlerts();_updateAlertBadge(0);}
function _updateAlertBadge(count){
  const badge=document.getElementById('alert-badge');
  if(!badge) return;
  if(count>0){badge.style.display='block';badge.textContent=count>99?'99+':count;}
  else{badge.style.display='none';}
}
async function configureAlerts(){
  try{
    const r=await api('GET','/notifications/config');
    const cfg=r.config||{};
    const enabled=cfg.enabled?'已启用':'未启用';
    const tgToken=cfg.telegram?.bot_token||'未配置';
    const tgChat=cfg.telegram?.chat_id||'未配置';
    const whUrl=cfg.webhook?.url||'未配置';
    const action=await ocPrompt('告警推送配置','',{message:`状态: ${enabled}<br>Telegram Token: ${tgToken}<br>Telegram Chat: ${tgChat}<br>Webhook: ${whUrl}<br><br>1 = 配置 Telegram<br>2 = 配置 Webhook<br>3 = 开启通知<br>4 = 关闭通知<br>5 = 发送测试`,inputPlaceholder:'输入 1-5'});
    if(action==='1'){
      const token=await ocPrompt('Telegram Bot Token','',{inputPlaceholder:'Bot Token'});
      const chatId=await ocPrompt('Telegram Chat ID','',{inputPlaceholder:'Chat ID'});
      if(token&&chatId){
        await api('POST','/notifications/config',{enabled:true,min_level:'warning',telegram:{bot_token:token,chat_id:chatId},webhook:cfg.webhook||{}});
        showToast('Telegram 配置已保存');
      }
    }else if(action==='2'){
      const url=await ocPrompt('Webhook URL','',{inputPlaceholder:'https://...'});
      if(url){
        await api('POST','/notifications/config',{enabled:true,min_level:'warning',telegram:cfg.telegram||{},webhook:{url:url,method:'POST'}});
        showToast('Webhook 配置已保存');
      }
    }else if(action==='3'){
      await api('POST','/notifications/config',{...cfg,enabled:true});
      showToast('通知已开启');
    }else if(action==='4'){
      await api('POST','/notifications/config',{...cfg,enabled:false});
      showToast('通知已关闭');
    }else if(action==='5'){
      await api('POST','/notifications/test',{message:'这是一条测试通知'});
      showToast('测试通知已发送');
    }
  }catch(e){showToast('配置失败: '+e.message,'warn');}
}

/* ── Telegram 配置卡片 ── */
async function _loadTelegramConfig(){
  try{
    const s=await api('GET','/notifications/telegram/status');
    const dot=document.getElementById('tg-status-dot');
    const label=document.getElementById('tg-status-label');
    const cb=document.getElementById('tg-enabled');
    const lvl=document.getElementById('tg-min-level');
    if(dot){dot.style.background=s.enabled&&s.configured?'#22c55e':s.configured?'#eab308':'#6b7280';}
    if(label){label.textContent=s.enabled&&s.configured?'已启用':s.configured?'已配置(未启用)':'未配置';}
    if(cb){cb.checked=s.enabled||false;}
    if(lvl&&s.min_level){lvl.value=s.min_level;}
    // Populate chat_id (token stays masked)
    const chatEl=document.getElementById('tg-chat-id');
    if(chatEl&&s.chat_id){chatEl.value=s.chat_id;}
    const recEl=document.getElementById('tg-recipients');
    if(recEl&&Array.isArray(s.recipients)){recEl.value=s.recipients.join('\n');}
    const invEl=document.getElementById('tg-invite-notes');
    if(invEl&&s.invite_link_notes!=null){invEl.value=s.invite_link_notes;}
  }catch(e){}
}
async function _saveTelegramConfig(){
  const token=document.getElementById('tg-bot-token')?.value?.trim()||'';
  const chatId=document.getElementById('tg-chat-id')?.value?.trim()||'';
  const recText=document.getElementById('tg-recipients')?.value||'';
  const inviteNotes=document.getElementById('tg-invite-notes')?.value?.trim()||'';
  const enabled=document.getElementById('tg-enabled')?.checked||false;
  const minLevel=document.getElementById('tg-min-level')?.value||'warning';
  const extraLines=recText.split(/\r?\n/).map(l=>l.trim()).filter(Boolean);
  if(!chatId&&!extraLines.length){showToast('请填写主 Chat ID 或至少一行额外接收方','warn');return;}
  try{
    const cur=await api('GET','/notifications/config');
    const existing=cur.config||{};
    const etg=existing.telegram||{};
    const body={
      ...existing,
      enabled:enabled,
      min_level:minLevel,
      telegram:{
        ...etg,
        bot_token:token||etg.bot_token||'',
        chat_id:chatId,
        recipients:extraLines,
        invite_link_notes:inviteNotes
      },
      webhook:existing.webhook||{}
    };
    if(!token&&etg.bot_token){body.telegram.bot_token=etg.bot_token;}
    await api('POST','/notifications/config',body);
    showToast('Telegram 配置已保存');
    _loadTelegramConfig();
  }catch(e){showToast('保存失败: '+e.message,'warn');}
}
async function _testTelegramMsg(){
  try{
    const r=await api('POST','/notifications/telegram/test',{});
    if(r.ok){
      const n=(r.targets&&r.targets.length)||0;
      showToast(n?('测试消息已发送（'+n+' 个目标）✅'):'测试消息已发送到 Telegram ✅');
    }
    else{showToast('发送失败: '+(r.error||'未知错误'),'warn');}
  }catch(e){showToast('请求失败: '+e.message,'warn');}
}
function _tgToggleEnabled(checked){
  // Just updates UI; actual save happens on _saveTelegramConfig
  const dot=document.getElementById('tg-status-dot');
  if(dot){dot.style.background=checked?'#eab308':'#6b7280';}
}

/* ── 告警规则管理页 ── */
const METRIC_NAMES={devices_offline:'设备掉线数',error_rate:'错误率(%)',tasks_failed:'失败任务数',tasks_pending:'待执行任务数',health_score_min:'最低健康分',vpn_down_count:'VPN异常数'};
function showAddRuleForm(){document.getElementById('add-rule-form').style.display='block';}
function hideAddRuleForm(){document.getElementById('add-rule-form').style.display='none';}
async function loadAlertRulesPage(){
  _loadTelegramConfig();
  try{
    const [all,custom,history,notifCfg]=await Promise.all([
      api('GET','/observability/alerts'),
      api('GET','/observability/alerts/rules/custom'),
      api('GET','/observability/alerts/history?limit=30'),
      api('GET','/notifications/config')
    ]);
    const rules=all.rules||[];
    const customNames=new Set((custom.rules||[]).map(r=>r.name));
    const list=document.getElementById('rules-list');
    list.innerHTML=rules.map(r=>{
      const sevColors={critical:'#ef4444',warning:'#eab308',info:'#3b82f6'};
      const isCustom=customNames.has(r.name);
      const metricLabel=METRIC_NAMES[r.metric]||r.name;
      const stateIcon=r.state==='firing'?'🔴':r.state==='resolved'?'🟢':'⚪';
      return `<div style="padding:8px 10px;border-bottom:1px solid rgba(255,255,255,.04);display:flex;align-items:center;gap:8px">
        <span>${stateIcon}</span>
        <div style="flex:1">
          <div style="font-size:11px;font-weight:600">${r.name}</div>
          <div style="font-size:10px;color:var(--text-muted)">${r.description||''}</div>
        </div>
        <span style="font-size:9px;padding:2px 6px;border-radius:4px;background:${sevColors[r.severity]||'#666'};color:#fff">${r.severity}</span>
        ${isCustom?`<button onclick="deleteAlertRule('${r.name}')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:12px" title="删除">&#10060;</button>`:''}
      </div>`;
    }).join('')||'<div style="padding:12px;color:var(--text-muted);font-size:11px">暂无规则</div>';
    const hlist=document.getElementById('alert-history-list');
    const hItems=(history||[]).slice().reverse();
    hlist.innerHTML=hItems.map(h=>{
      const t=new Date(h.timestamp*1000).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'});
      const icon=h.state==='firing'?'🔴':'🟢';
      return `<div style="padding:6px 10px;border-bottom:1px solid rgba(255,255,255,.04);display:flex;gap:8px;align-items:center">
        <span>${icon}</span>
        <div style="flex:1;font-size:11px">${h.rule||''} — ${h.description||''}</div>
        <span style="font-size:9px;color:var(--text-muted)">${t}</span>
      </div>`;
    }).join('')||'<div style="padding:12px;color:var(--text-muted);font-size:11px">暂无触发历史</div>';
    const cfg=notifCfg.config||{};
    const nc=document.getElementById('notif-config-display');
    nc.innerHTML=`状态: <b>${cfg.enabled?'✅ 已启用':'❌ 未启用'}</b> &nbsp;|&nbsp; 最低级别: <b>${cfg.min_level||'warning'}</b> &nbsp;|&nbsp; Telegram: <b>${cfg.telegram?.bot_token?'已配置':'未配置'}</b> &nbsp;|&nbsp; Webhook: <b>${cfg.webhook?.url?'已配置':'未配置'}</b>`;
  }catch(e){console.error('alert-rules',e);}
}
async function submitNewRule(){
  const name=document.getElementById('rule-name').value.trim();
  if(!name){showToast('请输入规则名称','warn');return;}
  try{
    await api('POST','/observability/alerts/rules',{
      name:name,
      metric:document.getElementById('rule-metric').value,
      operator:document.getElementById('rule-op').value,
      threshold:parseFloat(document.getElementById('rule-threshold').value),
      severity:document.getElementById('rule-severity').value,
      cooldown_sec:parseInt(document.getElementById('rule-cooldown').value)||300,
    });
    showToast('规则已创建');hideAddRuleForm();loadAlertRulesPage();
  }catch(e){showToast('创建失败: '+e.message,'warn');}
}
async function deleteAlertRule(name){
  if(!(await ocDialog({title:'删除规则',message:`确认删除规则: <b>${name}</b> ？`,type:'danger',confirmText:'删除',dangerous:true}))) return;
  try{
    await api('DELETE',`/observability/alerts/rules/${name}`);
    showToast('规则已删除');loadAlertRulesPage();
  }catch(e){showToast('删除失败','warn');}
}
async function evalAlertRules(){
  try{
    const r=await api('POST','/observability/alerts/evaluate');
    showToast(`评估完成: ${r.evaluated} 条规则, ${r.fired} 条触发`);
    loadAlertRulesPage();
  }catch(e){showToast('评估失败','warn');}
}


/* ── 消息通知 ── */
let _notifData=[];
async function loadNotificationsPage(){
  try{
    _notifData=await api('GET','/device-notifications?limit=100');
    renderNotifications();
  }catch(e){}
}
function renderNotifications(){
  const c=document.getElementById('notif-list');
  c.innerHTML=_notifData.map(n=>{
    const alias=ALIAS[n.device_id]||n.device_id?.substring(0,8)||'?';
    return `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:10px;display:flex;gap:10px;align-items:center">
      <span style="background:var(--accent);color:#111;font-size:10px;padding:2px 6px;border-radius:4px;font-weight:700">${alias}</span>
      <span style="font-size:11px;font-family:monospace;color:var(--text-dim);min-width:55px">${n.time||''}</span>
      <span style="font-size:11px;color:var(--text-muted)">${n.package||''}</span>
      <span style="font-size:12px;flex:1">${n.title||''}</span>
    </div>`;
  }).join('')||'<div style="color:var(--text-dim);font-size:12px">暂无通知</div>';
}
function clearNotifications(){
  api('DELETE','/device-notifications').then(()=>{_notifData=[];renderNotifications();showToast('已清空');});
}
function toggleNotifSound(){}


/* ── Alert Rules ── */
let _alertRules=[];
async function loadAlertRulesPage(){
  try{
    const r=await api('GET','/alert-rules');
    _alertRules=r.rules||[];
    renderAlertRules();
  }catch(e){}
}
function showAddRuleForm(){document.getElementById('add-rule-form').style.display='block';}
const _METRIC_LABELS={battery_level:'电量(%)',mem_usage:'内存(%)',battery_temp:'温度(℃)'};
const _ACTION_LABELS={notify:'仅通知',notify_and_cmd:'通知+命令',cmd:'仅命令'};
function renderAlertRules(){
  const container=document.getElementById('alert-rules-list');
  container.innerHTML=_alertRules.map(r=>{
    const metricLabel=_METRIC_LABELS[r.metric]||r.metric;
    const actionLabel=_ACTION_LABELS[r.action]||r.action;
    const statusColor=r.enabled?'var(--green)':'var(--text-muted)';
    return `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:12px;display:flex;justify-content:space-between;align-items:center">
      <div>
        <div style="font-size:13px;font-weight:600"><span style="color:${statusColor}">●</span> ${r.name}</div>
        <div style="font-size:10px;color:var(--text-muted);margin-top:2px">${metricLabel} ${r.operator} ${r.threshold} → ${actionLabel} ${r.action_cmd?'<code style="font-size:9px">'+r.action_cmd+'</code>':''}</div>
      </div>
      <div style="display:flex;gap:6px;align-items:center">
        <label style="font-size:10px;cursor:pointer"><input type="checkbox" ${r.enabled?'checked':''} onchange="toggleAlertRule('${r.id}',this.checked)"/> 启用</label>
        <button class="sb-btn2" onclick="deleteAlertRule('${r.id}')" style="font-size:9px;color:var(--red)">删除</button>
      </div>
    </div>`;
  }).join('')||'<div style="color:var(--text-muted);font-size:12px">暂无告警规则</div>';
}
async function addAlertRule(){
  try{
    await api('POST','/alert-rules/add',{
      name:document.getElementById('ar-name').value,
      metric:document.getElementById('ar-metric').value,
      operator:document.getElementById('ar-op').value,
      threshold:parseFloat(document.getElementById('ar-threshold').value),
      action:document.getElementById('ar-action').value,
      action_cmd:document.getElementById('ar-cmd').value,
    });
    document.getElementById('add-rule-form').style.display='none';
    showToast('规则已创建');loadAlertRulesPage();
  }catch(e){showToast('创建失败','warn');}
}
async function toggleAlertRule(id,enabled){
  const rule=_alertRules.find(r=>r.id===id);
  if(rule)rule.enabled=enabled;
  try{await api('POST','/alert-rules',{rules:_alertRules});showToast(enabled?'已启用':'已禁用');}catch(e){}
}
async function deleteAlertRule(id){
  if(!(await ocDialog({title:'删除规则',message:'确认删除此规则？',type:'danger',confirmText:'删除',dangerous:true})))return;
  try{await api('DELETE','/alert-rules/'+id);loadAlertRulesPage();}catch(e){showToast('删除失败','warn');}
}

/* ── Notification Center ── */
const _ALL_EVENTS=['device.disconnected','device.reconnected','task.failed','task.completed',
  'watchdog.captcha_detected','device.alert','workflow.finished','lead.new','batch.completed',
  'fb.phase_changed'];
let _ncEnabledEvents=[];
async function loadNotifyCenter(){
  try{
    const cfg=await api('GET','/notify/config');
    document.getElementById('nc-webhook-url').value=cfg.webhook_url||'';
    document.getElementById('nc-webhook-type').value=cfg.webhook_type||'generic';
    document.getElementById('nc-quiet-start').value=cfg.quiet_hours?.start||'23:00';
    document.getElementById('nc-quiet-end').value=cfg.quiet_hours?.end||'07:00';
    _ncEnabledEvents=cfg.enabled_events||[];
    const evDiv=document.getElementById('nc-events');
    evDiv.innerHTML=_ALL_EVENTS.map(ev=>`<label style="display:flex;align-items:center;gap:6px;font-size:11px;cursor:pointer">
      <input type="checkbox" ${_ncEnabledEvents.includes(ev)?'checked':''} onchange="_toggleNotifyEvent('${ev}',this.checked)"/>
      <span>${ev}</span>
    </label>`).join('');
  }catch(e){}
  try{
    const hist=await api('GET','/notify/history');
    const container=document.getElementById('nc-history');
    container.innerHTML=hist.reverse().map(h=>{
      const col=h.level==='error'||h.level==='critical'?'#ef4444':h.level==='warning'?'#f97316':'#3b82f6';
      const status=h.sent?'&#9989;':h.skipped?`&#9940; ${h.skipped}`:h.error?`&#10060; ${h.error}`:'&#8987;';
      return `<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border);font-size:10px">
        <span><span style="color:${col}">&#9679;</span> <strong>${h.title||''}</strong> ${h.message||''}</span>
        <span style="color:var(--text-muted)">${status} | ${h.ts||''}</span>
      </div>`;
    }).join('')||'<span style="color:var(--text-muted)">暂无推送记录</span>';
  }catch(e){}
}
function _toggleNotifyEvent(ev,checked){
  if(checked&&!_ncEnabledEvents.includes(ev))_ncEnabledEvents.push(ev);
  if(!checked)_ncEnabledEvents=_ncEnabledEvents.filter(e=>e!==ev);
}
async function saveNotifyConfig(){
  try{
    await api('POST','/notify/config',{
      webhook_url:document.getElementById('nc-webhook-url').value,
      webhook_type:document.getElementById('nc-webhook-type').value,
      enabled_events:_ncEnabledEvents,
      quiet_hours:{start:document.getElementById('nc-quiet-start').value,end:document.getElementById('nc-quiet-end').value}
    });
    showToast('配置已保存');
  }catch(e){showToast('保存失败','warn');}
}
async function testNotification(){
  try{await api('POST','/notify/test');showToast('测试通知已发送');}catch(e){showToast('发送失败','warn');}
}

