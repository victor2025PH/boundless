/* analytics.js — 数据分析: 日志、漏斗、统一WebSocket、审计日志、健康仪表板 */
/* ── Logs ── */
let logLevel='', logAutoTimer=null;

function setLogFilter(btn,lv){
  document.querySelectorAll('.log-filter').forEach(b=>b.classList.remove('active'));
  btn.classList.add('active');
  logLevel=lv;loadLogs();
}

async function loadLogs(){
  try{
    const url='/logs?limit=200'+(logLevel?'&level='+logLevel:'');
    const r=await api('GET',url);
    const panel=document.getElementById('log-panel');
    if(!r.logs||!r.logs.length){panel.innerHTML='<div style="text-align:center;padding:40px;color:var(--text-muted)">暂无日志</div>';return;}
    const wasBottom=panel.scrollHeight-panel.scrollTop-panel.clientHeight<50;
    panel.innerHTML=r.logs.map(e=>{
      const ts=(e.ts||'').split(' ').pop()||'';
      const lv=e.level||'INFO';
      const src=(e.logger||'').split('.').pop()||'';
      const ctx=e.task_id?`[${e.task_id.substring(0,8)}]`:'';
      return `<div class="log-entry"><span class="log-ts">${ts}</span><span class="log-lv ${lv}">${lv}</span><span class="log-src">${src}</span><span class="log-msg">${esc(e.msg||'')}</span>${ctx?`<span class="log-ctx">${ctx}</span>`:''}</div>`;
    }).join('');
    if(wasBottom)panel.scrollTop=panel.scrollHeight;
  }catch(e){document.getElementById('log-panel').innerHTML='<div style="color:var(--text-muted);padding:20px">加载失败</div>';}
}

function toggleLogAuto(){
  if(document.getElementById('log-auto').checked){
    logAutoTimer=setInterval(loadLogs,60000);
  }else{if(logAutoTimer){clearInterval(logAutoTimer);logAutoTimer=null;}}
}
logAutoTimer=setInterval(loadLogs,60000);

/* ── Funnel ── */
async function loadFunnel(){
  try{
    const f=await api('GET','/funnel?days=30');
    const d=f.funnel||{};
    const steps=[
      {label:'发现',count:d.discovered||0,color:'funnel-colors-1'},
      {label:'关注',count:d.followed||0,color:'funnel-colors-2'},
      {label:'回关',count:d.follow_back||0,color:'funnel-colors-3'},
      {label:'聊天',count:d.chatted||0,color:'funnel-colors-4'},
      {label:'回复',count:d.replied||0,color:'funnel-colors-5'},
      {label:'合格',count:d.qualified||0,color:'funnel-colors-6'},
      {label:'转化',count:d.converted||0,color:'funnel-colors-7'},
    ];
    const maxVal=Math.max(...steps.map(s=>s.count),1);
    const rates=f.rates||{};
    const rateKeys=['','follow_rate','followback_rate','chat_rate','reply_rate','qualification_rate','conversion_rate'];
    document.getElementById('funnel-chart').innerHTML=steps.map((s,i)=>{
      const pct=Math.max(s.count/maxVal*100,2);
      const rk=rateKeys[i]||'';const rv=rk&&rates[rk]!=null?(rates[rk]*100).toFixed(1)+'%':'';
      return `<div class="funnel-step"><span class="funnel-step-label">${s.label}</span><span class="funnel-step-count">${s.count}</span><div class="funnel-bar-wrap"><div class="funnel-bar ${s.color}" style="width:${pct}%"></div><span class="funnel-bar-label">${pct.toFixed(0)}%</span></div><span class="funnel-rate">${rv}</span></div>`;
    }).join('');

    const eng=f.engagement||{};const st=f.status_distribution||{};
    document.getElementById('funnel-stats').innerHTML=`
      <div class="f-stat"><div class="f-stat-num" style="color:#60a5fa">${(rates.overall_funnel*100||0).toFixed(2)}%</div><div class="f-stat-label">总转化率</div></div>
      <div class="f-stat"><div class="f-stat-num" style="color:#a78bfa">${(rates.followback_rate*100||0).toFixed(1)}%</div><div class="f-stat-label">回关率</div></div>
      <div class="f-stat"><div class="f-stat-num" style="color:#fbbf24">${(rates.reply_rate*100||0).toFixed(1)}%</div><div class="f-stat-label">回复率</div></div>
      <div class="f-stat"><div class="f-stat-num" style="color:#4ade80">${eng.auto_replies_sent||0}</div><div class="f-stat-label">自动回复</div></div>
      <div class="f-stat"><div class="f-stat-num" style="color:#f472b6">${eng.follow_ups_sent||0}</div><div class="f-stat-label">跟进消息</div></div>
      <div class="f-stat"><div class="f-stat-num" style="color:#22d3ee">${st.new||0}</div><div class="f-stat-label">新线索</div></div>
    `;
  }catch(e){document.getElementById('funnel-chart').innerHTML='<div style="color:var(--text-muted)">加载失败</div>';}

  try{
    const dd=await api('GET','/funnel/daily?days=7');
    const tbody=document.getElementById('funnel-daily');
    if(dd&&dd.length){
      tbody.innerHTML=dd.map(r=>`<tr><td>${r.date||''}</td><td>${r.discovered||0}</td><td>${r.followed||0}</td><td>${r.follow_back||0}</td><td>${r.chatted||0}</td><td>${r.replied||0}</td><td>${r.converted||0}</td></tr>`).join('');
    }else{tbody.innerHTML='<tr><td colspan="7" style="text-align:center;color:var(--text-muted)">暂无数据</td></tr>';}
  }catch(e){}

  // Load real aggregate data from device_state
  try{
    const agg=await api('GET','/analytics/cluster-summary').catch(()=>api('GET','/analytics/activity-summary'));
    const f=agg.funnel||{};
    const statsEl=document.getElementById('funnel-aggregate-stats');
    if(statsEl){
      statsEl.innerHTML=`
        <div style="display:flex;gap:16px;flex-wrap:wrap;margin-top:12px;padding:12px;background:var(--bg-main);border-radius:8px;border:1px solid var(--border)">
          <div style="text-align:center"><div style="font-size:20px;font-weight:700;color:#60a5fa">${f.watched||0}</div><div style="font-size:11px;color:var(--text-dim)">总观看</div></div>
          <div style="text-align:center"><div style="font-size:20px;font-weight:700;color:#22c55e">${f.followed||0}</div><div style="font-size:11px;color:var(--text-dim)">总关注</div></div>
          <div style="text-align:center"><div style="font-size:20px;font-weight:700;color:#a78bfa">${f.dms_sent||0}</div><div style="font-size:11px;color:var(--text-dim)">总私信</div></div>
          <div style="text-align:center"><div style="font-size:20px;font-weight:700;color:#fbbf24">${f.dm_rate_pct||0}%</div><div style="font-size:11px;color:var(--text-dim)">DM转化率</div></div>
          ${agg.cluster?`<div style="text-align:center"><div style="font-size:20px;font-weight:700;color:#22d3ee">${agg.worker_nodes||0}</div><div style="font-size:11px;color:var(--text-dim)">Worker节点</div></div>`:''}
        </div>
      `;
    }
  }catch(e){}
}

/* ── Unified WebSocket Channel (replaces SSE + polling) ── */
let _unifiedWs=null;
let _wsReconnectTimer=null;
let _prevDeviceOnline={};
let _wsLoadLogDebounce=null;

function connectUnifiedWs(){
  if(_unifiedWs && (_unifiedWs.readyState===WebSocket.OPEN || _unifiedWs.readyState===WebSocket.CONNECTING)) return;
  _unifiedWs=new WebSocket(_wsUrl('/ws'));
  _unifiedWs.onopen=function(){
    document.getElementById('h-status').textContent='实时连接 (WS)';
    document.getElementById('h-dot').className='status-dot ok';
    if(_wsReconnectTimer){clearTimeout(_wsReconnectTimer);_wsReconnectTimer=null;}
  };
  _unifiedWs.onmessage=function(ev){
    try{ _handleWsPush(JSON.parse(ev.data)); }catch(e){}
  };
  _unifiedWs.onclose=function(){
    document.getElementById('h-status').textContent='重连中...';
    document.getElementById('h-dot').className='status-dot warn';
    _wsReconnectTimer=setTimeout(connectUnifiedWs, 3000);
  };
  _unifiedWs.onerror=function(){};
}

function _requestNotifPermission(){
  if('Notification' in window && Notification.permission==='default'){
    Notification.requestPermission();
  }
}
_requestNotifPermission();

function _browserNotify(title,body,level){
  if('Notification' in window && Notification.permission==='granted'){
    try{
      const n=new Notification('OpenClaw: '+title,{body:body.replace(/&#\d+;/g,''),icon:'/favicon.ico',tag:'oc-'+Date.now(),silent:false});
      setTimeout(()=>n.close(),8000);
    }catch(e){}
  }
}

function _flashDeviceCard(deviceId,color){
  const card=document.querySelector(`.scr-card[data-did="${deviceId}"]`);
  if(!card)return;
  card.style.transition='box-shadow .3s';
  card.style.boxShadow=`0 0 0 3px ${color}, 0 0 20px ${color}80`;
  setTimeout(()=>{card.style.boxShadow='';},3000);
}

function _handleWsPush(msg){
  const t=msg.type;
  if(!t) return;

  if(t==='push.screenshots'){_handleScreenshotPush(msg.data);return;}
  if(t==='push.notifications'){
    const notifs=msg.data?.notifications||[];
    if(notifs.length){
      notifs.forEach(n=>_notifData.unshift(n));
      if(_notifData.length>200) _notifData.length=200;
      if(document.getElementById('page-notifications')?.classList.contains('active')) renderNotifications();
      const soundOn=document.getElementById('notif-sound-enabled')?.checked;
      if(soundOn&&notifs.length>0){try{new Audio('data:audio/wav;base64,UklGRnoGAABXQVZFZm10IBAAAAABAAEAQB8AAEAfAAABAAgAZGF0YQoGAACBhYqFbF1fdJOkrp+QgHRxeImao6iflot+eHuEkZmeoJyVjoh+eH6IkZaYl5SSjomFgoKGi5CSlJKPjIqIhoWGh4qNj5CQj46MiomIh4eIiYuMjY2NjIuKiYiIiImKi4yMjIyLioqJiYiJiYqLi4yLi4uKiomJiImJiouLi4uLiomJiYmJiYqKi4uLi4qKiYmJiYmJioqLi4uKioqJiYmJiYqKiouLi4qKiomJiYmJiYqKiouLioqKiomJiYmJiYqKiouLioqJiYmJiYmJioqKi4uKioqJiYmJiYmKioqLi4qKiomJiYmJ').play();}catch(e){}}
    }
    return;
  }

  if(t==='push.devices' || t==='snapshot'){
    const d=msg.data||{};
    if(d.health_scores) _deviceHealthScores=d.health_scores;
    if(d.recovery) _deviceRecoveryState=d.recovery;
    if(d.devices){
      let online=0, total=0, changed=false;
      for(const [did,info] of Object.entries(d.devices)){
        total++;
        const isOn=(typeof info==='object')?(info.status==='connected'||info.status==='online'):(info==='connected'||info==='online');
        if(isOn) online++;
        if(_prevDeviceOnline[did]!==undefined && _prevDeviceOnline[did]!==isOn) changed=true;
        _prevDeviceOnline[did]=isOn;
      }
      try{document.getElementById('s-online').textContent=online;document.getElementById('d-online').textContent=online;document.getElementById('s-total').textContent=total;document.getElementById('d-total').textContent=total;}catch(e){}
      if(changed) _scheduleLoadDevices();
    }
    // task_stats from coordinator is local-only (0 on coordinator node); skip — _updateOpsStatusBar() owns s-tasks/s-success
  }

  if(t==='push.tasks'){
    const d=msg.data||{};
    if(d.recent && Array.isArray(d.recent)){
      // 合并更新而不是直接替换，防止覆盖掉 Worker 节点的任务
      const idx=new Map(allTasks.map(x=>([x.task_id,x])));
      d.recent.forEach(x=>{if(x.task_id)idx.set(x.task_id,x);});
      // 统一排序：运行中/等待中优先，同状态按 updated_at 降序
      const _rank={running:0,pending:1,completed:2,failed:2,cancelled:3};
      allTasks=Array.from(idx.values()).sort((a,b)=>{
        const ra=_rank[a.status]??2, rb=_rank[b.status]??2;
        if(ra!==rb) return ra-rb;
        return (b.updated_at||'').localeCompare(a.updated_at||'');
      });
      renderTasks();
      _updateOpsStatusBar();
      // Fix: use slice(0,8) to show newest 8 tasks (allTasks sorted newest-first)
      try{document.getElementById('ov-tasks-body').innerHTML=allTasks.slice(0,8).map(taskRow).join('');}catch(e){}
    }
    // d.stats is coordinator-local; skip — _updateOpsStatusBar() already called above with allTasks
  }

  if(t==='push.performance'){
    const devs=msg.data?.devices||{};
    Object.assign(_devicePerfCache,devs);
    const pEntries=Object.values(_devicePerfCache);
    if(pEntries.length){
      const bats=pEntries.filter(p=>p.battery_level!==undefined).map(p=>p.battery_level);
      const mems=pEntries.filter(p=>p.mem_usage!==undefined).map(p=>p.mem_usage);
      try{
        document.getElementById('s-avg-bat').textContent=bats.length?Math.round(bats.reduce((a,b)=>a+b,0)/bats.length)+'%':'-';
        document.getElementById('s-avg-mem').textContent=mems.length?Math.round(mems.reduce((a,b)=>a+b,0)/mems.length)+'%':'-';
        document.getElementById('s-low-bat').textContent=bats.filter(b=>b<20).length;
        document.getElementById('s-high-mem').textContent=mems.filter(m=>m>80).length;
      }catch(e){}
      try{renderBatteryChart();}catch(e){}
    }
    return;
  }

  if(t==='push.charts'){
    const d=msg.data||{};
    if(d.device_trend){
      try{
        const ctx=document.getElementById('chart-device-trend');if(ctx){
          if(_chartDevTrend)_chartDevTrend.destroy();
          _chartDevTrend=new Chart(ctx,{type:'line',data:{labels:d.device_trend.labels,datasets:[
            {label:'在线',data:d.device_trend.online,borderColor:'#22c55e',backgroundColor:'rgba(34,197,94,.1)',fill:true,tension:.3,pointRadius:1},
            {label:'总数',data:d.device_trend.total,borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,.05)',fill:true,tension:.3,pointRadius:1},
          ]},options:{responsive:true,plugins:{legend:{labels:{color:_chartColors.text,font:{size:10}}}},scales:{x:{ticks:{color:_chartColors.text,font:{size:9},maxTicksLimit:8},grid:{color:_chartColors.grid}},y:{ticks:{color:_chartColors.text,font:{size:9}},grid:{color:_chartColors.grid},beginAtZero:true}}}});
        }
      }catch(e){}
    }
    if(d.task_trend){
      try{
        const ctx=document.getElementById('chart-task-trend');if(ctx){
          if(_chartTaskTrend)_chartTaskTrend.destroy();
          _chartTaskTrend=new Chart(ctx,{type:'line',data:{labels:d.task_trend.labels,datasets:[
            {label:'成功',data:d.task_trend.success,borderColor:'#22c55e',backgroundColor:'rgba(34,197,94,.1)',fill:true,tension:.3,pointRadius:1},
            {label:'失败',data:d.task_trend.failed,borderColor:'#ef4444',backgroundColor:'rgba(239,68,68,.1)',fill:true,tension:.3,pointRadius:1},
            {label:'总数',data:d.task_trend.total,borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,.05)',fill:false,tension:.3,pointRadius:1,borderDash:[4,4]},
          ]},options:{responsive:true,plugins:{legend:{labels:{color:_chartColors.text,font:{size:10}}}},scales:{x:{ticks:{color:_chartColors.text,font:{size:9},maxTicksLimit:8},grid:{color:_chartColors.grid}},y:{ticks:{color:_chartColors.text,font:{size:9}},grid:{color:_chartColors.grid},beginAtZero:true}}}});
        }
      }catch(e){}
    }
    return;
  }

  if(t==='push.logs'){
    if(document.getElementById('log-auto')?.checked){
      if(!_wsLoadLogDebounce){
        _wsLoadLogDebounce=setTimeout(()=>{_wsLoadLogDebounce=null;loadLogs();},1000);
      }
    }
  }

  if(t.startsWith('task.')){
    const tid=msg.data?.task_id;
    // In-place update: avoid full reload when we already have this task
    if(tid){
      const idx=allTasks.findIndex(x=>x.task_id===tid);
      if(idx>=0){
        const task=allTasks[idx];
        if(t==='task.running') task.status='running';
        else if(t==='task.completed'){task.status='completed';if(task.result)task.result.success=true;else task.result={success:true};}
        else if(t==='task.failed'){task.status='failed';if(task.result)task.result.error=msg.data?.error||'';else task.result={success:false,error:msg.data?.error||''};if(typeof _eapOnTaskFailed==='function') _eapOnTaskFailed();}
        else if(t==='task.cancelled') task.status='cancelled';
        else if(t==='task.progress'){
          // In-place progress update — no re-render needed, just update DOM directly
          const pct=msg.data?.progress??0;
          const pmsg=msg.data?.message||'';
          if(!task.result) task.result={};
          task.result.progress=pct;
          task.result.progress_msg=pmsg;
          // Update progress bar in DOM directly (avoid full re-render)
          const rows=document.querySelectorAll('#task-tbody tr, #ov-tasks-body tr');
          rows.forEach(row=>{
            if(row.innerHTML.includes(tid.substring(0,8))||row.onclick?.toString().includes(tid)){
              const bar=row.querySelector('.progress-fill');
              const txt=row.querySelector('.progress-text');
              if(bar) bar.style.width=pct+'%';
              if(txt){
                const stepDesc=(typeof _getStepDesc==='function')?_getStepDesc(task.type,pct)||pmsg:pmsg;
                txt.textContent=pct+'%'+(stepDesc?' · '+stepDesc:'');
              }
            }
          });
          // P7: 实时更新设备卡任务进度条（直接操作DOM，不重渲染）
          if(typeof _taskMap!=='undefined'&&task.device_id&&_taskMap[task.device_id]){
            const _tm=_taskMap[task.device_id];
            _tm.progress=pct;
            const _ds=task.device_id.substring(0,8);
            const _strip=document.getElementById('ts-'+_ds);
            const _badge=document.getElementById('tb-'+_ds);
            if(_strip&&typeof _applyTaskStrip==='function') _applyTaskStrip(_strip,_badge,_tm);
          }
          return; // Don't re-render entire task table for progress updates
        }
        task.updated_at=new Date().toISOString();
        renderTasks();
        _updateOpsStatusBar();
        // P7: WS事件驱动实时同步设备卡任务状态（毫秒级，无需等待5秒轮询）
        if(typeof _syncTaskMap==='function') _syncTaskMap();
        if(typeof _updateBatchProgress==='function') _updateBatchProgress();
      } else {
        // Unknown task (new or from worker) — do a full reload
        setTimeout(loadTasks,800);
      }
    } else {
      setTimeout(loadTasks,800);
    }
    // Toast notifications for completed/failed
    if(t==='task.completed'||t==='task.failed'){
      const isOk=t==='task.completed';
      const icon=isOk?'✅':'❌';
      const lab=isOk?'完成':'失败';
      const tn=TASK_NAMES[msg.data?.task_type]||msg.data?.task_type||'';
      const dev=ALIAS[msg.data?.device_id]||msg.data?.device_id?.substring(0,8)||'';
      if(tn) showToast(`${icon} 任务${lab}: ${tn}${dev?' ('+dev+')':''}`,isOk?'success':'error');
      _updateOpsStatusBar();
      // 2026-05-08 P2-4: 实时同步平台页任务列表
      if(typeof window._onWsTaskDone==='function') window._onWsTaskDone(msg.data,t);
    }
  }

  if(t==='auto_heal.escalated'){
    const d=msg.data||{};
    const dev=ALIAS[d.device_id]||d.device_id?.substring(0,8)||'?';
    showToast(`&#9888; 设备 ${dev} 自愈超限已隔离 (${d.error_code||''})`,'error',10000);
    if(typeof _browserNotify==='function') _browserNotify('设备自动隔离',dev+' 自愈频率超限，已停止任务分配','error');
  }
  if(t==='chain.completed'){
    const d=msg.data||{};
    const ok=d.steps_ok||0,tot=d.steps_total||0,fail=d.failed_steps||0;
    const s=d.status==='completed'?'完成':'中止';
    const errSum=d.error_summary||{};
    const errKeys=Object.keys(errSum);
    let errHint='';
    if(errKeys.length){errHint='\n失败: '+errKeys.map(k=>k.substring(0,40)+(errSum[k]>1?'(x'+errSum[k]+')':'')).join(', ');}
    const isGood=d.status==='completed'&&fail===0;
    const isBad=fail>0&&(d.status==='aborted'||fail>=tot*0.5);
    showToast(`&#128279; 链${s}: &#9989;${ok}/${tot}${fail>0?' &#10060;'+fail:''}${errHint} (${d.chain_id||''})`,isGood?'success':(isBad?'error':'warn'),isGood?6000:10000);
    if(isBad&&typeof _browserNotify==='function') _browserNotify('链任务告警',`${d.chain_id||'链'} ${s}: ${fail}步失败`,'warning');
    const plat=_currentPage?.replace('plat-','');
    if(plat&&typeof _refreshChainRuns==='function') _refreshChainRuns(plat);
  }
  if(t.startsWith('workflow.')){
    const pg=document.querySelector('.page.active');
    if(pg && pg.id==='page-workflows') loadWorkflowsPage();
    if(t==='workflow.finished'){
      const s=msg.data?.success?'成功':'失败';
      showToast(`工作流${s}: ${msg.data?.workflow_name||''}`);
    }
  }
  if(t==='device.alert'||t==='device.disconnected'||t==='task.failed'||t==='watchdog.captcha_detected'){
    _alertItems.push({level:msg.data?.level||'warning',device_id:msg.data?.device_id||'',message:msg.data?.message||t,timestamp:new Date().toISOString()});
    if(_alertItems.length>50) _alertItems=_alertItems.slice(-40);
    _updateAlertBadge(_alertItems.filter(a=>a.level==='critical'||a.level==='error'||a.level==='warning').length);
    if(_alertPanelOpen) _renderAlerts();
  }
  if(t==='device.disconnected'){
    const did=msg.data?.device_id;
    const name=ALIAS[did]||did?.substring(0,8)||'?';
    showToast('设备掉线: '+name, 'warn');
    _browserNotify('设备掉线','&#9888; '+name+' 已断开连接','warn');
    _flashDeviceCard(did,'#ef4444');
    setTimeout(()=>{loadDevices();if(_currentPage==='screens')renderScreens();},1000);
  }
  if(t==='device.reconnected'||t==='device.online'){
    const did2=msg.data?.device_id;
    const name2=ALIAS[did2]||did2?.substring(0,8)||'?';
    const isFirst=msg.data?.first_time;
    showToast((isFirst?'新设备上线: ':'设备重连: ')+name2, 'success');
    _browserNotify(isFirst?'新设备上线':'设备重连',name2+' 已连接');
    _flashDeviceCard(did2,'#22c55e');
    setTimeout(()=>{loadDevices();if(_currentPage==='screens')renderScreens();},1000);
  }
  if(t==='batch.created'){
    showToast('批量任务已创建: '+(msg.data?.count||'?')+' 个');
    setTimeout(loadTasks,500);
  }

  // TikTok 自动化事件 → 通过 window CustomEvent 分发给其他模块 (conversations.js等)
  if(t.startsWith('tiktok.')){
    window.dispatchEvent(new CustomEvent('oc:event',{detail:msg}));
    if(t==='tiktok.escalate_to_human'){
      const u=msg.data?.username||'用户', intent=msg.data?.intent||'';
      showToast('&#128226; 需人工处理: '+u+(intent?' ('+intent+')':''),'warn');
      _browserNotify('需人工处理',u+' 需要回复'+(intent?' — 意向: '+intent:''),'warn');
      try{
        const b=document.getElementById('escalation-badge');
        if(b){b.textContent=(parseInt(b.textContent||'0')+1);b.style.display='inline';}
        const banner=document.getElementById('ov-escalation-banner');
        const detail=document.getElementById('ov-esc-detail');
        if(banner){banner.style.display='flex';if(detail)detail.textContent='有新对话待处理';}
      }catch(e){}
    }
    if(t==='tiktok.inbox_checked'||t==='tiktok.dm_sent'){
      if(typeof _loadOpsDashboard==='function') setTimeout(_loadOpsDashboard,1500);
    }
    if(t==='tiktok.lead_converted'){
      const u=msg.data?.username||'用户';
      showToast('&#127881; 成功引流: '+u+' 已转化为线索','success');
      _browserNotify('线索转化',u+' 已被成功引流到 Telegram/WhatsApp','info');
      if(typeof _loadOpsDashboard==='function') setTimeout(_loadOpsDashboard,2000);
    }
  }

  // Facebook 自动化事件 → 通过 window CustomEvent 分发给 facebook-ops.js
  if(t.startsWith('facebook.')){
    window.dispatchEvent(new CustomEvent('oc:event',{detail:msg}));
    if(t==='facebook.campaign_done'){
      const d=msg.data||{};
      const devName=ALIAS[d.device_id]||d.device_id?.substring(0,8)||'?';
      const sent=d.friend_requests_sent||0;
      const steps=d.steps_completed||[];
      showToast('📋 '+devName+' 方案执行完成: 发送好友请求 '+sent+' · 完成步骤 '+steps.length,'success',6000);
      if(typeof fbShowCampaignResult==='function') setTimeout(function(){fbShowCampaignResult(d);},500);
    }
  }

  // VPN 地理位置不匹配告警
  if(t==='vpn.geo_mismatch'){
    const d=msg.data||{};
    const devName=ALIAS[d.device_id]||d.device_id?.substring(0,8)||'?';
    const reconnected=d.reconnected?'已自动重连':'重连失败';
    showToast('&#127758; VPN地理异常: '+devName+' 检测到 '+d.detected+' (期望: '+d.expected+')，'+reconnected,'warn');
    _browserNotify('VPN地理异常',devName+': '+d.detected+' → '+d.expected+' '+reconnected,'warn');
  }

  // 运营日报就绪通知
  if(t==='analytics.daily_report_ready'){
    const d=msg.data||{};
    const date=d.date||'今日';
    const totals=d.totals||{};
    const leads=d.leads||{};
    const dms=totals.dms_sent||0;
    const newLeads=leads.new_leads||0;
    const aiSummary=(d.ai_summary||'').substring(0,80);
    showToast('&#128202; '+date+' 日报已生成：私信 '+dms+' 条 · 新线索 '+newLeads+' 个','success',8000);
    _browserNotify('运营日报已生成',date+' — 私信'+dms+'条，新线索'+newLeads+'个'+(aiSummary?'\n'+aiSummary:''),'info');
    // 如果当前在分析页则刷新日报面板
    if(typeof _ttShowReportPanel==='function'){
      setTimeout(function(){
        if(document.getElementById('tt-report-panel')) _ttGenerateReport && _ttGenerateReport();
      },2000);
    }
  }
}

function _wsRequestRefresh(target){
  if(_unifiedWs && _unifiedWs.readyState===WebSocket.OPEN){
    _unifiedWs.send(JSON.stringify({cmd:'refresh',target:target}));
  }
}

/* ── 审计日志页 ── */
let _auditData=[];
async function loadAuditLogs(){
  try{
    const r=await api('GET','/audit/logs?limit=200');
    _auditData=r.logs||[];
    filterAuditLogs();
  }catch(e){console.error('audit',e);}
}
function filterAuditLogs(){
  const q=(document.getElementById('audit-search')?.value||'').toLowerCase();
  const filtered=q?_auditData.filter(l=>(l.action+l.target+l.detail).toLowerCase().includes(q)):_auditData;
  const tbody=document.getElementById('audit-tbody');
  if(!tbody) return;
  tbody.innerHTML=filtered.slice().reverse().slice(0,100).map(l=>{
    const actionColors={create_task:'#4ade80',cancel_all_tasks:'#ef4444',shell_command:'#60a5fa',fix_device:'#fbbf24',rotate_device:'#a78bfa'};
    const color=actionColors[l.action]||'var(--text-dim)';
    const target=l.target?ALIAS[l.target]||l.target.substring(0,10):'—';
    return `<tr style="border-bottom:1px solid rgba(255,255,255,.03)">
      <td style="padding:8px 14px;color:var(--text-muted);font-size:11px;white-space:nowrap">${l.timestamp||''}</td>
      <td style="padding:8px 14px"><span style="color:${color};font-weight:600;font-size:11px">${l.action||''}</span></td>
      <td style="padding:8px 14px;font-size:11px">${target}</td>
      <td style="padding:8px 14px;color:var(--text-dim);font-size:11px;max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${l.detail||''}</td>
      <td style="padding:8px 14px;font-size:10px;color:var(--text-muted)">${l.source||'api'}</td>
    </tr>`;
  }).join('');
}

/* mobile sidebar */
(function(){
  const btn=document.getElementById('mob-menu-btn');
  function check(){if(btn) btn.style.display=window.innerWidth<=768?'block':'none';}
  window.addEventListener('resize',check);check();
  document.querySelector('.main')?.addEventListener('click',()=>{
    if(window.innerWidth<=768) document.querySelector('.sidebar')?.classList.remove('mob-open');
  });
})();

function _updateOpsStatusBar(){
  try{
    const running=allTasks.filter(t=>t.status==='running').length;
    const pending=allTasks.filter(t=>t.status==='pending').length;
    const failed=allTasks.filter(t=>t.status==='failed').length;
    // Fix: top stat cards use allTasks (includes Worker-03) as authoritative source
    const sRunEl=document.getElementById('s-running');
    if(sRunEl) sRunEl.textContent=running;
    const sTasks=document.getElementById('s-tasks');
    if(sTasks) sTasks.textContent=allTasks.length;
    const sOk=document.getElementById('s-success');
    if(sOk) sOk.textContent=allTasks.filter(t=>t.status==='completed').length;
    const sFail=document.getElementById('s-failed');
    if(sFail) sFail.textContent=allTasks.filter(t=>t.status==='failed').length;
    const el=document.getElementById('ops-status-bar');
    if(!el)return;
    let parts=[];
    if(running>0) parts.push(`<span style="color:#4ade80">▶ ${running} 个任务运行中</span>`);
    if(pending>0) parts.push(`<span style="color:#fbbf24">⏳ ${pending} 个等待中</span>`);
    if(failed>0) parts.push(`<span style="color:#f87171">✗ ${failed} 个失败</span>`);
    if(!parts.length) parts.push('<span style="color:var(--text-dim)">✓ 系统空闲</span>');
    el.innerHTML=parts.join(' &nbsp;·&nbsp; ');
  }catch(e){}
}

function showToast(msg,type,duration){
  // Remove existing toasts to prevent stacking
  document.querySelectorAll('.oc-toast').forEach(el=>el.remove());
  const t=document.createElement('div');
  t.className='oc-toast';
  const colors={
    'success':'background:#052e16;color:#4ade80;border:1px solid #166534',
    'error':'background:#2d0a0a;color:#f87171;border:1px solid #7f1d1d',
    'warn':'background:#1c1206;color:#fbbf24;border:1px solid #92400e',
  };
  const style=colors[type]||'background:#1e3a5f;color:#93c5fd;border:1px solid #3b82f6';
  t.style.cssText='position:fixed;top:70px;right:20px;padding:10px 18px;border-radius:8px;font-size:13px;z-index:9999;animation:msgIn .3s ease;max-width:360px;word-break:break-word;cursor:pointer;'+style;
  t.innerHTML=msg;
  t.onclick=()=>t.remove();
  document.body.appendChild(t);
  setTimeout(()=>t.remove(),duration||4000);
}

/* ── Health Dashboard ── */
async function loadHealthPage(){
  try{
    const [scores,timeline,trends,isolated]=await Promise.all([
      api('GET','/devices/health-scores'),
      api('GET','/devices/recovery-timeline?limit=50'),
      api('GET','/devices/health-trends?hours=24'),
      api('GET','/devices/isolated'),
    ]);

    const isoSet=new Set(isolated?.isolated||[]);

    // Stats row
    const sc=scores?.scores||{};
    const devIds=Object.keys(sc);
    const avgScore=devIds.length?Math.round(devIds.reduce((a,d)=>a+(sc[d].total||0),0)/devIds.length):0;
    const onlineCount=devIds.filter(d=>sc[d].online).length;
    const critCount=devIds.filter(d=>sc[d].total<40).length;
    document.getElementById('health-stats-row').innerHTML=`
      <div class="stat-card blue"><div class="stat-num">${avgScore}</div><div class="stat-label">平均健康分</div></div>
      <div class="stat-card green"><div class="stat-num">${onlineCount}/${devIds.length}</div><div class="stat-label">在线设备</div></div>
      <div class="stat-card orange"><div class="stat-num">${critCount}</div><div class="stat-label">低分设备(<40)</div></div>
      <div class="stat-card purple"><div class="stat-num">${isoSet.size}</div><div class="stat-label">已隔离</div></div>
    `;

    // Ranking table
    const ranked=Object.entries(sc).sort((a,b)=>b[1].total-a[1].total);
    document.getElementById('health-rank-body').innerHTML=ranked.map(([did,s])=>{
      const alias=ALIAS[did]||did.substring(0,8);
      const cls=s.total>=75?'badge-completed':s.total>=50?'badge-running':s.total>=30?'badge-pending':'badge-failed';
      const stLabel=s.online?'在线':'离线';
      const iso=isoSet.has(did);
      const isoBtn=iso
        ?`<button class="dev-btn" style="font-size:10px;padding:2px 8px;color:var(--green)" onclick="unisolateDevice('${did}')">解除隔离</button>`
        :`<button class="dev-btn" style="font-size:10px;padding:2px 8px;color:var(--red)" onclick="isolateDevice('${did}')">隔离</button>`;
      return `<tr>
        <td><b>${alias}</b>${iso?' <span style="color:var(--red);font-size:10px">[隔离]</span>':''}</td>
        <td><span class="badge ${cls}">${s.total}</span></td>
        <td>${s.stability}</td>
        <td>${s.responsiveness} <span style="color:var(--text-muted);font-size:10px">(${s.latency_ms}ms)</span></td>
        <td>${s.task_success}</td>
        <td><span class="status-dot ${s.online?'ok':'err'}" style="width:6px;height:6px;display:inline-block;vertical-align:middle"></span> ${stLabel}</td>
        <td>${isoBtn}</td>
      </tr>`;
    }).join('');

    // Recovery timeline
    const events=timeline?.events||[];
    if(events.length){
      document.getElementById('recovery-timeline').innerHTML=events.reverse().map(e=>{
        const alias=ALIAS[e.device_id]||e.device_id?.substring(0,8)||'?';
        const ts=(e.ts_str||'').split('T').pop()?.replace('Z','')||'';
        const icon=e.success?'<span style="color:var(--green)">&#10003;</span>':'<span style="color:var(--red)">&#10007;</span>';
        const lvNames={reconnect:'L1 重连',reset_transport:'L2 重置',kill_server:'L3 ADB重启',usb_power_cycle:'L4 USB电源'};
        return `<div class="log-entry"><span class="log-ts">${ts}</span><span style="min-width:70px">${icon} ${e.success?'成功':'失败'}</span><span style="min-width:80px;color:var(--accent)">${alias}</span><span class="log-msg">${lvNames[e.level]||e.level} - ${e.details||''}</span></div>`;
      }).join('');
    }else{
      document.getElementById('recovery-timeline').innerHTML='<div style="text-align:center;padding:30px;color:var(--text-muted)">暂无恢复事件</div>';
    }

    // Health score trends (simple text-based chart)
    const trendData=trends?.trends||{};
    const trendDevIds=Object.keys(trendData).filter(d=>trendData[d].length>0);
    if(trendDevIds.length){
      let html='<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px">';
      for(const did of trendDevIds){
        const alias=ALIAS[did]||did.substring(0,8);
        const points=trendData[did];
        const latest=points[points.length-1]||{};
        const oldest=points[0]||{};
        const delta=(latest.total||0)-(oldest.total||0);
        const deltaColor=delta>0?'var(--green)':delta<0?'var(--red)':'var(--text-muted)';
        const deltaSign=delta>0?'+':'';
        const minScore=Math.min(...points.map(p=>p.total||0));
        const maxScore=Math.max(...points.map(p=>p.total||0));

        const chartWidth=220;const chartHeight=40;
        const svgPoints=points.map((p,i)=>{
          const x=Math.round(i/(points.length-1||1)*chartWidth);
          const y=chartHeight-Math.round(((p.total||0)-minScore)/(maxScore-minScore||1)*chartHeight);
          return `${x},${y}`;
        }).join(' ');

        html+=`<div style="background:var(--bg-input);border-radius:8px;padding:10px 12px">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
            <span style="font-weight:600;font-size:13px">${alias}</span>
            <span style="font-size:12px"><span style="font-weight:700">${latest.total||'-'}</span> <span style="color:${deltaColor};font-size:11px">${deltaSign}${delta}</span></span>
          </div>
          <svg width="${chartWidth}" height="${chartHeight}" style="display:block">
            <polyline points="${svgPoints}" fill="none" stroke="#3b82f6" stroke-width="1.5"/>
          </svg>
          <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-muted);margin-top:3px">
            <span>${points.length}个采样</span>
            <span>范围: ${minScore}-${maxScore}</span>
          </div>
        </div>`;
      }
      html+='</div>';
      document.getElementById('health-trend-panel').innerHTML=html;
    }else{
      document.getElementById('health-trend-panel').innerHTML='<div style="text-align:center;padding:30px;color:var(--text-muted)">暂无趋势数据（需运行一段时间积累）</div>';
    }

    _loadRecoveryStats();

  }catch(e){
    console.error('Health page load error:',e);
  }
}

async function _loadRecoveryStats() {
    const el = document.getElementById('recovery-stats-content');
    if (!el) return;
    try {
        const d = await api('GET', '/health/recovery-stats');
        if (d.total_disconnects === 0) {
            el.innerHTML = '<div style="text-align:center;padding:12px;color:var(--text-dim)">暂无掉线记录 — 所有设备运行稳定 &#9989;</div>';
            return;
        }

        const rateColor = d.overall_rate >= 90 ? '#22c55e' : d.overall_rate >= 70 ? '#f59e0b' : '#ef4444';

        let html = `
            <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:14px">
                <div style="text-align:center;padding:12px;background:var(--bg-main);border-radius:8px">
                    <div style="font-size:22px;font-weight:700;color:#ef4444">${d.total_disconnects}</div>
                    <div style="font-size:10px;color:var(--text-muted)">总掉线次数</div>
                </div>
                <div style="text-align:center;padding:12px;background:var(--bg-main);border-radius:8px">
                    <div style="font-size:22px;font-weight:700;color:#22c55e">${d.total_recoveries}</div>
                    <div style="font-size:10px;color:var(--text-muted)">自动恢复次数</div>
                </div>
                <div style="text-align:center;padding:12px;background:var(--bg-main);border-radius:8px">
                    <div style="font-size:22px;font-weight:700;color:${rateColor}">${d.overall_rate}%</div>
                    <div style="font-size:10px;color:var(--text-muted)">自动恢复率</div>
                </div>
            </div>`;

        if (d.devices && d.devices.length > 0) {
            html += '<div style="font-size:12px;font-weight:600;margin-bottom:8px">&#128200; 掉线排行榜</div>';
            html += '<div style="max-height:200px;overflow-y:auto">';
            d.devices.forEach((dev, i) => {
                const alias = ALIAS[dev.device_id] || dev.display_name || dev.device_id.substring(0, 8);
                const barWidth = Math.min(100, dev.disconnects / Math.max(d.total_disconnects, 1) * 200);
                html += `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">
                    <span style="min-width:20px;font-weight:500;color:var(--text-dim)">#${i+1}</span>
                    <span style="min-width:60px;font-weight:500">${alias}</span>
                    <div style="flex:1;height:6px;background:var(--bg-main);border-radius:3px;overflow:hidden">
                        <div style="height:100%;width:${barWidth}%;background:${dev.recovery_rate>=90?'#22c55e':'#ef4444'};border-radius:3px"></div>
                    </div>
                    <span style="min-width:30px;text-align:right;color:#ef4444">${dev.disconnects}</span>
                    <span style="min-width:20px;text-align:center;color:var(--text-dim)">/</span>
                    <span style="min-width:30px;color:#22c55e">${dev.recoveries}</span>
                    <span style="min-width:40px;text-align:right;font-weight:500;color:${dev.recovery_rate>=90?'#22c55e':'#ef4444'}">${dev.recovery_rate}%</span>
                </div>`;
            });
            html += '</div>';
        }

        el.innerHTML = html;
    } catch (e) {
        el.innerHTML = '<div style="color:var(--text-dim)">加载失败</div>';
    }
}

async function isolateDevice(did){
  if(!(await ocDialog({title:'隔离设备',message:'确定隔离此设备？隔离后不会分配新任务。',type:'warning',confirmText:'隔离',cancelText:'取消'})))return;
  await api('POST',`/devices/${did}/isolate`);
  showToast('设备已隔离');
  loadHealthPage();
}

async function unisolateDevice(did){
  await api('POST',`/devices/${did}/unisolate`);
  showToast('设备已解除隔离');
  loadHealthPage();
}

