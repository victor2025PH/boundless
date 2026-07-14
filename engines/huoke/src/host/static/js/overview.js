/* overview.js — 总览面板: Overview Briefing、Navigation、Clock、Health、Dashboard Charts */

/* ── 今日成果 — 单一数据源: /analytics/today ── */
async function _loadTodayStats(){
  try{
    const s=await api('GET','/analytics/today').catch(()=>null);
    if(!s) return;
    const _set=(id,v)=>{const el=document.getElementById(id);if(el)el.textContent=(v==null||v==='')?0:v;};
    _set('today-watched',    s.watched);
    _set('today-followed',   s.followed);
    _set('today-dms',        s.dms_sent);
    _set('today-autoreplied',s.auto_replied);
    _set('today-leads',      s.new_leads);
    _set('today-converts',   s.conversions);
  }catch(e){console.debug('today stats not available',e);}
}

/* ── 工作流快速启动 ── */
async function _runPresetWorkflow(name){
  if(!(await ocDialog({title:'启动工作流',message:'确认启动: '+name.replace(/preset_/,'').replace(/_/g,' '),type:'info',confirmText:'启动',cancelText:'取消'})))return;
  try{
    const r=await api('POST','/workflows/run',{workflow:name});
    if(r&&r.run_id){
      _toast('工作流已启动: '+r.run_id,'success');
      _loadTodayStats();
    }else{
      _toast('工作流启动失败','error');
    }
  }catch(e){_toast('启动失败: '+(e.message||e),'error');}
}

/* ── Nav ── */
/* ── Overview Briefing ── */
async function _loadExecutionPolicyBar(){
  const el=document.getElementById('ov-exec-policy');
  if(!el)return;
  try{
    const p=await api('GET','/tasks/meta/execution-policy');
    const bits=[];
    if(p.manual_execution_only) bits.push('仅手动派发优先');
    if(p.disable_db_scheduler) bits.push('DB 定时关');
    if(p.disable_json_scheduled_jobs) bits.push('JSON 定时关');
    if(p.disable_reconnect_task_recovery) bits.push('掉线自动恢复关');
    if(p.disable_auto_tiktok_check_inbox) bits.push('无人收件箱关');
    if(bits.length){
      el.innerHTML='&#9888; <b>本机任务策略</b>：'+bits.join(' · ')+'（与任务详情「执行策略」同源；各 Worker 节点独立配置）';
      el.style.display='block';
    }else{
      el.style.display='none';
    }
  }catch(e){el.style.display='none';}
}

async function _loadOverviewBriefing(){
  const el=document.getElementById('ov-briefing-text');
  if(!el) return;
  try{
    const [b,ov]=await Promise.all([api('GET','/briefing/daily'),api('GET','/cluster/overview').catch(()=>null)]);
    const y=b.yesterday||{};
    let devOn=(b.devices||{}).online||0,devTotal=(b.devices||{}).total||0;
    if(ov&&ov.total_devices>devTotal){devOn+=(ov.total_devices_online||0);devTotal+=ov.total_devices;}
    const parts=[];
    if(y.total_tasks>0) parts.push(`昨日完成 ${y.success}/${y.total_tasks} 任务 (${y.success_rate}%)`);
    if(devOn>0) parts.push(`${devOn}/${devTotal} 设备在线`);
    if((b.devices||{}).low_battery&&b.devices.low_battery.length>0) parts.push(`${b.devices.low_battery.length} 台低电量`);
    const running=b.today?.running_tasks||0;
    if(running>0) parts.push(`${running} 个任务运行中`);
    // Fetch today's tiktok stats for business KPI summary
    try{
      const tk=await api('GET','/tiktok/daily-report').catch(()=>null);
      if(tk){
        const todayF=tk.today?.followed||0;
        const todayFB=tk.today?.follow_backs||0;
        const esc=(await api('GET','/tiktok/escalation-queue').catch(()=>({items:[]})));
        const escCnt=(esc.items||[]).length;
        if(todayF>0) parts.push(`今日关注${todayF}人`);
        if(todayFB>0) parts.push(`${todayFB}个回关`);
        if(escCnt>0) parts.push(`<span style="color:#fbbf24;font-weight:600">⚡ ${escCnt}条线索待处理</span>`);
      }
    }catch(e){}
    el.innerHTML=parts.length?parts.join(' &nbsp;·&nbsp; '):'<span style="color:var(--text-dim)">✓ 系统就绪，等待指令</span>';
  }catch(e){el.textContent='简报加载失败';}
}

const _PAGE_LOADERS={
  'overview':()=>{_loadOverviewBriefing();_loadExecutionPolicyBar();_loadTodayStats();_loadDeviceRanking();_loadAIStats();_loadScheduledJobsQuick();_loadDailyReport();_loadHealChainStats();_loadLeadSummary();_loadFbHealth();if(typeof _refreshDeviceMetaAlerts==='function')_refreshDeviceMetaAlerts();},
  'chat':()=>setTimeout(()=>document.getElementById('chat-input')?.focus(),100),
  'screens':async()=>{
    try{await loadDevices();}catch(e){console.debug('screens loadDevices',e);}
    // 2026-05-04: checkbox 默认 ON, page-load 永远 fetch /cluster/devices 一次
    // 让主控有本机 USB 设备时也能看到 W03/W175 worker 上的设备
    // (旧版只在 localDevs.length===0 时自动 toggle, 主控有手机时 worker 设备
    //  默认隐藏 → 用户痛点 "看不到 W03/W175").
    const cb=document.getElementById('show-cluster-devices');
    if(cb&&cb.checked){
      try{await toggleClusterDevices();}catch(e){console.debug('cluster devices fetch:',e);}
    }
    renderScreens();_autoEnableScreenRefresh();
  },
  'funnel':()=>loadFunnel(),
  'health':()=>loadHealthPage(),
  'workflows':()=>loadWorkflowsPage(),
  'cluster':()=>loadClusterPage(),
  'analytics':()=>loadAnalytics(),
  'audit':()=>loadAuditLogs(),
  'alert-rules':()=>loadAlertRulesPage(),
  'groups':()=>loadGroupsPage(),
  'batch-apk':()=>loadBatchApkPage(),
  'batch-text':()=>loadBatchTextPage(),
  'app-manager':()=>loadAppManagerPage(),
  'phrases':()=>loadPhrasesPage(),
  'notifications':()=>loadNotificationsPage(),
  'perf-monitor':()=>loadPerfMonitor(),
  'screen-record':()=>loadScreenRecordPage(),
  'op-replay':()=>loadOpReplayPage(),
  'script-engine':()=>loadScriptEnginePage(),
  'quick-actions':()=>loadQuickActionsPage(),
  'batch-upload':()=>loadBatchUploadPage(),
  'scheduled-jobs':()=>loadScheduledJobsPage(),
  'data-export':()=>loadDataExportPage(),
  'device-assets':()=>loadDeviceAssetsPage(),
  'ai-script':()=>loadAiScriptPage(),
  'op-timeline':()=>loadOpTimelinePage(),
  'sync-mirror':()=>loadSyncMirrorPage(),
  'health-report':()=>loadHealthReport(),
  'tpl-market':()=>loadTplMarketPage(),
  'visual-workflow':()=>loadVisualWorkflow(),
  'multi-screen':()=>loadMultiScreen(),
  'plugins':()=>loadPluginsPage(),
  'backup':()=>loadBackupPage(),
  'notify-center':()=>loadNotifyCenter(),
  'user-mgmt':()=>loadUserMgmtPage(),
  'vpn-manage':()=>loadVpnManagePage(),
  'router-manage':()=>loadRouterManagePage(),
  'roi':()=>loadROIPage(),
  'plat-tiktok':()=>{loadPlatformPage('tiktok');if(typeof loadTtOpsPanel==='function')loadTtOpsPanel();},
  'plat-telegram':()=>loadPlatGridPage('telegram'),
  'plat-whatsapp':()=>loadPlatGridPage('whatsapp'),
  'plat-facebook':()=>{if(typeof loadFbOpsPanel==='function'){loadFbOpsPanel();}else{loadPlatGridPage('facebook');}},
  'plat-linkedin':()=>loadPlatGridPage('linkedin'),
  'plat-instagram':()=>loadPlatGridPage('instagram'),
  'plat-twitter':()=>loadPlatGridPage('twitter'),
  'conversations':()=>{navigateToPage('plat-tiktok');setTimeout(()=>ttTab('conv'),50);},
  'leads':()=>{navigateToPage('plat-tiktok');setTimeout(()=>ttTab('leads'),50);},
  'messages':()=>{navigateToPage('plat-tiktok');setTimeout(()=>ttTab('msg'),50);},
  'account-farming':()=>{navigateToPage('plat-tiktok');setTimeout(()=>ttTab('farming'),50);},
  'campaigns':()=>{navigateToPage('plat-tiktok');setTimeout(()=>ttTab('farming'),50);},
};
let _currentPage='overview';
function navigateToPage(pg){
  // Tab重定向：这些页面已融合进TikTok tab
  const _ttTabMap={'conversations':'conv','leads':'leads','messages':'msg','account-farming':'farming','campaigns':'farming'};
  if(_ttTabMap[pg]){
    const _ttTarget='plat-tiktok';
    // 隐藏所有页面，显示TikTok页面
    document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
    document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
    const _ttPageEl=document.getElementById('page-plat-tiktok');
    if(_ttPageEl)_ttPageEl.classList.add('active');
    const _ttNavEl=document.querySelector('[data-page="plat-tiktok"]');
    if(_ttNavEl)_ttNavEl.classList.add('active');
    const _ttTitleEl=_ttNavEl?.querySelector('span:last-child');
    if(_ttTitleEl)document.getElementById('page-title').textContent='TikTok';
    _currentPage=_ttTarget;
    setTimeout(()=>ttTab(_ttTabMap[pg]),50);
    return;
  }
  document.querySelectorAll('.nav-item').forEach(n=>n.classList.remove('active'));
  const navEl=document.querySelector(`[data-page="${pg}"]`);
  if(navEl)navEl.classList.add('active');
  document.querySelectorAll('.page').forEach(p=>p.classList.remove('active'));
  const pageEl=document.getElementById('page-'+pg);
  if(pageEl)pageEl.classList.add('active');
  const titleEl=navEl?.querySelector('span:last-child');
  if(titleEl)document.getElementById('page-title').textContent=titleEl.textContent;
  _currentPage=pg;
  const loader=_PAGE_LOADERS[pg];
  if(loader)loader();
  else if(pg.startsWith('plat-'))loadPlatformPage(pg.replace('plat-',''));
  history.replaceState(null,null,'#'+pg);
}
document.querySelectorAll('.nav-item[data-page]').forEach(el=>{
  el.addEventListener('click',()=>navigateToPage(el.dataset.page));
});
if(location.hash.length>1){
  const hashPage=location.hash.substring(1);
  if(document.getElementById('page-'+hashPage))setTimeout(()=>navigateToPage(hashPage),100);
}

/* ── Clock ── */
function updateClock(){document.getElementById('clock').textContent=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit'});}
setInterval(updateClock,1000);updateClock();

/* ── Health ── */
async function loadHealth(){
  try{
    const h=await api('GET','/health');const s=h.status;
    document.getElementById('h-status').textContent=s==='ok'?'运行正常':s==='degraded'?'部分异常':'异常';
    document.getElementById('h-dot').className='status-dot '+(s==='ok'?'ok':s==='degraded'?'warn':'err');
    const m=Math.floor(h.uptime_seconds/60);
    document.getElementById('h-uptime').textContent='运行 '+(m>=60?Math.floor(m/60)+'h'+m%60+'m':m+'m');
    document.getElementById('h-version').textContent='v'+h.version;
    let _ovOnline=h.devices_online||0,_ovTotal=h.devices_total||0;
    try{const _ov=await api('GET','/cluster/overview');if(_ov&&_ov.total_devices>_ovTotal){_ovTotal=_ov.total_devices+(_ovTotal||0);_ovOnline=(_ov.total_devices_online||0)+(_ovOnline||0);}}catch(e){}
    document.getElementById('s-online').textContent=_ovOnline;
    document.getElementById('s-total').textContent=_ovTotal;
    // s-tasks/success/failed/running: owned exclusively by _updateOpsStatusBar() — do NOT write here
    // Battery: merge allDevices + _devicePerfCache by device_id to avoid double-counting
    const _batById={};
    (allDevices||[]).forEach(d=>{if(d.battery_level!=null)_batById[d.device_id]=d.battery_level;});
    Object.entries(_devicePerfCache).forEach(([did,p])=>{if(p.battery_level!=null)_batById[did]=p.battery_level;});
    const bats=Object.values(_batById);
    const pEntries=Object.values(_devicePerfCache);
    const mems=pEntries.filter(p=>p.mem_usage!==undefined).map(p=>p.mem_usage);
    document.getElementById('s-avg-bat').textContent=bats.length?Math.round(bats.reduce((a,b)=>a+b,0)/bats.length)+'%':'-';
    document.getElementById('s-avg-mem').textContent=mems.length?Math.round(mems.reduce((a,b)=>a+b,0)/mems.length)+'%':'-';
    document.getElementById('s-low-bat').textContent=bats.filter(b=>b<20).length;
    document.getElementById('s-high-mem').textContent=mems.filter(m=>m>80).length;
  }catch(e){document.getElementById('h-status').textContent='连接失败';}
  _loadNodeRole();
  _loadGatePolicyPill();
}

/* ── 任务门禁策略 pill（改完 task_execution_policy.yaml 自动反映） ── */
window._gatePolicySnap=null;
async function _loadGatePolicyPill(){
  const pill=document.getElementById('gate-policy-pill');
  if(!pill)return;
  try{
    const p=await api('GET','/task-dispatch/policy');
    window._gatePolicySnap=p;
    const mg=p.manual_gate||{};
    const pre=!!mg.enforce_preflight;
    const geo=!!mg.enforce_geo_for_risky;
    const mode=p.gate_mode||'strict';
    let color='#22c55e',bg='rgba(34,197,94,.12)',label='门禁: 放行';
    if(pre&&geo){color='#ef4444';bg='rgba(239,68,68,.14)';label='门禁: 严格';}
    else if(pre||geo){color='#eab308';bg='rgba(234,179,8,.14)';label='门禁: 部分';}
    // P0.3: 黑话翻译 + tooltip 说明含义
    const _modeLabels={strict:'严格档',balanced:'平衡档',relaxed:'宽松档',loose:'宽松档'};
    const modeLabel=_modeLabels[mode]||mode;
    pill.textContent=label+' · '+modeLabel;
    pill.title='🛡 反风控门禁策略 (Anti-detection Gate)\n'
      +'─────────────────────────\n'
      +'当前: '+label+' · '+modeLabel+'（'+mode+'）\n\n'
      +'• 起飞前检查 (preflight): '+(pre?'✓ 开启':'✗ 关闭')+'\n'
      +'• 高风险任务地理校验 (geo): '+(geo?'✓ 开启':'✗ 关闭')+'\n\n'
      +'档位说明:\n'
      +'  严格档 = 全检查，封号率最低（推荐养号期）\n'
      +'  平衡档 = 关键检查，吞吐率与风控折中（默认）\n'
      +'  宽松档 = 最少干预，吞吐率最高（仅测试）\n\n'
      +'👉 点击查看详情 / 热加载 task_execution_policy.yaml';
    pill.style.color=color;
    pill.style.background=bg;
    pill.style.borderColor=color+'66';
    pill.style.display='inline-block';
    pill.style.cursor='help';
  }catch(e){
    pill.textContent='门禁: ?';
    pill.title='反风控门禁策略加载失败 — 检查 /task-dispatch/policy 接口';
    pill.style.color='#94a3b8';
    pill.style.background='transparent';
    pill.style.display='inline-block';
  }
}
async function _showGatePolicyDetail(){
  const p=window._gatePolicySnap||{};
  const mg=p.manual_gate||{};
  const mt=p.policy_mtime_iso||'(未知)';
  const lines=[
    '任务门禁策略 (config/task_execution_policy.yaml)',
    '',
    '  gate_mode = '+(p.gate_mode||'strict'),
    '  manual_execution_only = '+!!p.manual_execution_only,
    '  manual_gate.enforce_preflight = '+!!mg.enforce_preflight,
    '  manual_gate.enforce_geo_for_risky = '+!!mg.enforce_geo_for_risky,
    '  default_tier = '+(p.default_tier||'L2'),
    '  YAML 最后加载 = '+mt,
    '',
    '点击「确定」强制热加载一次（等价 POST /task-dispatch/policy/reload）',
    '点击「取消」关闭'
  ];
  if(await ocDialog({title:'策略信息',message:lines.join('<br>'),type:'info',confirmText:'热加载',cancelText:'关闭'})){
    api('POST','/task-dispatch/policy/reload').then(r=>{
      toast('策略已热加载','success');
      _loadGatePolicyPill();
    }).catch(e=>toast('热加载失败: '+e.message,'err'));
  }
}
async function _loadNodeRole(){
  const badge=document.getElementById('node-role-badge');
  if(!badge) return;
  try{
    const cfg=await api('GET','/cluster/config');
    const role=cfg.role||'standalone';
    const name=cfg.host_name||'';
    if(role==='coordinator'){
      try{
        const ov=await api('GET','/cluster/overview');
        const hl=ov.hosts||[];
        const coordUsb=(ov.coordinator_usb_devices!==undefined&&ov.coordinator_usb_devices!==null)?ov.coordinator_usb_devices:0;
        const parts=[];
        for(let i=0;i<hl.length;i++){
          const h=hl[i];
          const hn=String(h.host_name||h.host_id||'Worker').trim();
          const short=(/^worker[-\s]?/i.test(hn)?'W'+hn.replace(/^worker[-\s]?/i,''):hn);
          const on=h.devices_online!=null?h.devices_online:0;
          const tot=h.devices!=null?h.devices:0;
          parts.push(short+' '+on+'/'+tot);
        }
        if(coordUsb>0) parts.push('\u672c\u673aUSB '+coordUsb);
        const summary=parts.length?parts.join(' + '):((ov.total_devices||0)+' \u53f0');
        badge.textContent='\u2601 '+(name||'\u4e3b\u63a7')+' \u00b7 '+summary;
        badge.title='Worker: \u5728\u7ebf/\u5fc3\u8df3\u91cc\u7684\u8bbe\u5907\u6570(\u542b\u6389\u7ebf)\u3002\u672c\u673aUSB: \u4e3b\u63a7\u76f4\u8fde\u3002\u6389\u7ebf\u540e\u5fc3\u8df3\u5df2\u6539\u4e3a\u4ee5 adb \u4e3a\u51c6\u3002';
        badge.style.cssText='display:inline-block;margin-left:6px;font-size:10px;padding:2px 8px;border-radius:4px;font-weight:500;background:rgba(59,130,246,.15);color:#60a5fa;border:1px solid rgba(59,130,246,.3)';
      }catch(e){
        badge.textContent='\u2601 Coordinator';
        badge.style.cssText='display:inline-block;margin-left:6px;font-size:10px;padding:2px 8px;border-radius:4px;font-weight:500;background:rgba(59,130,246,.15);color:#60a5fa;border:1px solid rgba(59,130,246,.3)';
      }
    }else if(role==='worker'){
      const coordUrl=cfg.coordinator_url||'';
      let coordStatus='\u2713 \u5df2\u8fde\u63a5\u4e3b\u63a7';
      let coordColor='#22c55e';let coordBg='rgba(34,197,94,.15)';let coordBorder='rgba(34,197,94,.3)';
      try{
        const hp=await api('GET','/health');
        const onAd=hp.devices_online!=null?hp.devices_online:0;
        const totAd=hp.devices_total!=null?hp.devices_total:0;
        coordStatus=(name||'Worker')+' \u00b7 adb '+onAd+'/'+totAd;
      }catch(e){
        const localDevs=(allDevices||[]).filter(function(d){return !d._isCluster;}).length;
        coordStatus=(name||'Worker')+' \u00b7 '+localDevs+' \u53f0(\u5217\u8868)';
      }
      if(coordUrl){
        try{
          const r=await fetch(coordUrl.replace(/\/$/,'')+'/health',{signal:AbortSignal.timeout(3000)});
          if(!r.ok) throw new Error();
        }catch(e){
          coordStatus=(name||'Worker')+' \u00b7 \u4e3b\u63a7\u65ad\u8fde(\u672c\u5730\u4e0d\u53d7\u5f71\u54cd)';
          coordColor='#f59e0b';coordBg='rgba(245,158,11,.15)';coordBorder='rgba(245,158,11,.3)';
        }
      }
      badge.title='\u672c\u673a: /health \u7684 adb \u5728\u7ebf/\u767b\u8bb0\u6570\u3002\u4e0e\u4e3b\u63a7\u65ad\u8fde\u65e0\u5173\u3002';
      badge.textContent='\u2699 '+coordStatus;
      badge.style.cssText='display:inline-block;margin-left:6px;font-size:10px;padding:2px 8px;border-radius:4px;font-weight:500;background:'+coordBg+';color:'+coordColor+';border:1px solid '+coordBorder;
    }else{
      badge.textContent='\u25cf \u5355\u673a\u6a21\u5f0f';
      badge.style.cssText='display:inline-block;margin-left:6px;font-size:10px;padding:2px 8px;border-radius:4px;font-weight:500;background:rgba(148,163,184,.15);color:#94a3b8;border:1px solid rgba(148,163,184,.3)';
    }
  }catch(e){badge.style.display='none';}
}

/* ── Dashboard Charts ── */
let _chartDevTrend=null,_chartTaskTrend=null,_chartDevPie=null,_chartTaskPie=null,_chartBatBar=null;
const _chartColors={bg:'transparent',grid:'rgba(148,163,184,.15)',text:'#94a3b8'};

async function loadTrendChart(){
  const range=document.getElementById('chart-trend-range')?.value||'24h';
  try{
    const data=await api('GET','/analytics/device-trend?range='+range);
    const ctx=document.getElementById('chart-device-trend');if(!ctx)return;
    if(_chartDevTrend)_chartDevTrend.destroy();
    _chartDevTrend=new Chart(ctx,{type:'line',data:{
      labels:data.labels||[],
      datasets:[
        {label:'在线',data:data.online||[],borderColor:'#22c55e',backgroundColor:'rgba(34,197,94,.1)',fill:true,tension:.3,pointRadius:1},
        {label:'总数',data:data.total||[],borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,.05)',fill:true,tension:.3,pointRadius:1},
      ]},options:{responsive:true,plugins:{legend:{labels:{color:_chartColors.text,font:{size:10}}}},scales:{
        x:{ticks:{color:_chartColors.text,font:{size:9},maxTicksLimit:8},grid:{color:_chartColors.grid}},
        y:{ticks:{color:_chartColors.text,font:{size:9}},grid:{color:_chartColors.grid},beginAtZero:true}
      }}
    });
  }catch(e){}
}

async function loadTaskChart(){
  const range=document.getElementById('chart-task-range')?.value||'24h';
  try{
    const data=await api('GET','/analytics/task-trend?range='+range);
    const ctx=document.getElementById('chart-task-trend');if(!ctx)return;
    if(_chartTaskTrend)_chartTaskTrend.destroy();
    _chartTaskTrend=new Chart(ctx,{type:'line',data:{
      labels:data.labels||[],
      datasets:[
        {label:'成功',data:data.success||[],borderColor:'#22c55e',backgroundColor:'rgba(34,197,94,.1)',fill:true,tension:.3,pointRadius:1},
        {label:'失败',data:data.failed||[],borderColor:'#ef4444',backgroundColor:'rgba(239,68,68,.1)',fill:true,tension:.3,pointRadius:1},
        {label:'总数',data:data.total||[],borderColor:'#3b82f6',backgroundColor:'rgba(59,130,246,.05)',fill:false,tension:.3,pointRadius:1,borderDash:[4,4]},
      ]},options:{responsive:true,plugins:{legend:{labels:{color:_chartColors.text,font:{size:10}}}},scales:{
        x:{ticks:{color:_chartColors.text,font:{size:9},maxTicksLimit:8},grid:{color:_chartColors.grid}},
        y:{ticks:{color:_chartColors.text,font:{size:9}},grid:{color:_chartColors.grid},beginAtZero:true}
      }}
    });
  }catch(e){}
}

function renderDevicePieChart(){
  const ctx=document.getElementById('chart-device-pie');if(!ctx)return;
  const online=allDevices.filter(d=>d.status==='connected'||d.status==='online').length;
  const busy=allDevices.filter(d=>d.busy).length;
  const offline=allDevices.length-online;
  if(_chartDevPie)_chartDevPie.destroy();
  _chartDevPie=new Chart(ctx,{type:'doughnut',data:{
    labels:['空闲','执行中','离线'],
    datasets:[{data:[online-busy,busy,offline],backgroundColor:['#22c55e','#eab308','#ef4444'],borderWidth:0}]
  },options:{responsive:true,plugins:{legend:{position:'bottom',labels:{color:_chartColors.text,font:{size:10},padding:8}}}}});
}

function renderTaskPieChart(){
  const ctx=document.getElementById('chart-task-pie');if(!ctx)return;
  const types={};
  (allTasks||[]).forEach(t=>{const tp=t.task_type||t.type||'unknown';types[tp]=(types[tp]||0)+1;});
  const labels=Object.keys(types).slice(0,8);
  const vals=labels.map(l=>types[l]);
  const palette=['#3b82f6','#22c55e','#eab308','#ef4444','#8b5cf6','#ec4899','#06b6d4','#f97316'];
  if(_chartTaskPie)_chartTaskPie.destroy();
  _chartTaskPie=new Chart(ctx,{type:'doughnut',data:{
    labels,datasets:[{data:vals,backgroundColor:palette.slice(0,labels.length),borderWidth:0}]
  },options:{responsive:true,plugins:{legend:{position:'bottom',labels:{color:_chartColors.text,font:{size:9},padding:6}}}}});
}

function renderBatteryChart(){
  const ctx=document.getElementById('chart-battery-bar');if(!ctx)return;
  const buckets={'0-20':0,'21-40':0,'41-60':0,'61-80':0,'81-100':0};
  // Merge battery data: allDevices (primary, includes cluster devices) + _devicePerfCache (WS perf events)
  const batteryById={};
  (allDevices||[]).forEach(d=>{if(d.battery_level!=null)batteryById[d.device_id]=d.battery_level;});
  Object.entries(_devicePerfCache).forEach(([did,p])=>{if(p.battery_level!=null)batteryById[did]=p.battery_level;});
  Object.values(batteryById).forEach(b=>{
    if(b<=20)buckets['0-20']++;
    else if(b<=40)buckets['21-40']++;
    else if(b<=60)buckets['41-60']++;
    else if(b<=80)buckets['61-80']++;
    else buckets['81-100']++;
  });
  if(_chartBatBar)_chartBatBar.destroy();
  _chartBatBar=new Chart(ctx,{type:'bar',data:{
    labels:Object.keys(buckets),
    datasets:[{label:'设备数',data:Object.values(buckets),backgroundColor:['#ef4444','#f97316','#eab308','#22c55e','#3b82f6'],borderRadius:4}]
  },options:{responsive:true,plugins:{legend:{display:false}},scales:{
    x:{ticks:{color:_chartColors.text,font:{size:9}},grid:{display:false}},
    y:{ticks:{color:_chartColors.text,font:{size:9},stepSize:1},grid:{color:_chartColors.grid},beginAtZero:true}
  }}});
}

function loadAllCharts(){loadTrendChart();loadTaskChart();renderDevicePieChart();renderTaskPieChart();renderBatteryChart();}

/* ── 运营大盘 ── */
async function _loadOpsDashboard(){
  try{
    // ops-* 运营大盘用 cluster-summary（聚合所有 Worker 真实累计数据）
    const act=await api('GET','/analytics/cluster-summary').catch(()=>null);
    const el=id=>document.getElementById(id);
    if(act){
      if(el('ops-watched'))  el('ops-watched').textContent=act.funnel?.watched||0;
      if(el('ops-followed')) el('ops-followed').textContent=act.funnel?.followed||0;
      if(el('ops-dms'))      el('ops-dms').textContent=act.funnel?.dms_sent||0;
    }
    // unique_chats/follows 仍从 daily-report
    const d=await api('GET','/tiktok/daily-report').catch(()=>null);
    if(d){
      if(el('ops-chats'))        el('ops-chats').textContent=d.unique_chats||0;
      if(el('ops-follows-dedup'))el('ops-follows-dedup').textContent=d.unique_follows||0;
    }
    // today-* 卡片由 _loadTodayStats() 统一负责，这里不再设置
  }catch(e){}
  try{
    const v=await api('GET','/vpn/status');
    const connected=(v.devices||[]).filter(d=>d.connected).length;
    const el=document.getElementById('ops-vpn');
    if(el) el.textContent=connected+'/'+(v.devices||[]).length;
  }catch(e){}
  // Check escalation queue and show banner
  try{
    const eq=await api('GET','/tiktok/escalation-queue');
    const cnt=(eq.items||[]).length;
    const banner=document.getElementById('ov-escalation-banner');
    const detail=document.getElementById('ov-esc-detail');
    if(banner){
      if(cnt>0){
        banner.style.display='flex';
        if(detail) detail.textContent=`${cnt} 条对话待处理`;
      } else {
        banner.style.display='none';
      }
    }
  }catch(e){}
}
/* ── P10-D: 7天活跃趋势图 ── */
let _chartActivityTrend = null;
async function _loadActivityTrend(){
  try{
    const d = await api('GET', '/analytics/daily-trend?days=7');
    const ctx = document.getElementById('chart-activity-trend');
    if(!ctx || !d || !d.trend) return;
    const labels = d.trend.map(e=>e.label);
    const followed = d.trend.map(e=>e.followed);
    const dms = d.trend.map(e=>e.dms);
    if(_chartActivityTrend) _chartActivityTrend.destroy();
    _chartActivityTrend = new Chart(ctx, {
      type: 'bar',
      data: {
        labels,
        datasets: [
          {label:'关注', data:followed, backgroundColor:'rgba(59,130,246,.7)', borderRadius:4, order:2},
          {label:'私信', data:dms, backgroundColor:'rgba(34,197,94,.7)', borderRadius:4, order:2},
          {label:'_关注线', data:followed, type:'line', borderColor:'#3b82f6', borderWidth:2,
           pointRadius:3, fill:false, tension:0.3, order:1},
          {label:'_私信线', data:dms, type:'line', borderColor:'#22c55e', borderWidth:2,
           pointRadius:3, fill:false, tension:0.3, order:1},
        ]
      },
      options:{
        responsive:true,
        plugins:{
          legend:{labels:{color:_chartColors.text,font:{size:10},boxWidth:10,filter:(item)=>!item.text.startsWith('_')},position:'top'},
          tooltip:{mode:'index',intersect:false}
        },
        scales:{
          x:{ticks:{color:_chartColors.text,font:{size:10}},grid:{display:false}},
          y:{ticks:{color:_chartColors.text,font:{size:10},stepSize:10},grid:{color:_chartColors.grid},beginAtZero:true}
        }
      }
    });
  }catch(e){console.debug('activity trend failed',e);}
}

// 页面加载时刷新运营大盘
if(typeof _updateOpsStatusBar==='function') _updateOpsStatusBar();
setTimeout(_loadOpsDashboard,2000);
setTimeout(_loadActivityTrend,3000);
setTimeout(_loadAIStats,2500);
setInterval(_loadOpsDashboard,60000);
setInterval(_loadActivityTrend,300000);
setInterval(_loadOverviewBriefing, 120000);
setInterval(_loadDeviceRanking, 120000);
setInterval(_loadAIStats, 60000);
setInterval(_loadHealChainStats, 60000);
setInterval(_loadLeadSummary, 120000);
setInterval(_loadFbHealth, 120000);

/* ── BB5: Facebook 引流健康度卡片 ── */
async function _loadFbHealth(){
  const wrap=document.getElementById('ov-fb-health');
  if(!wrap) return;
  try{
    const d=await api('GET','/facebook/health-summary');
    wrap.style.display='';

    // 评分 badge
    const score=d.health_score||0;
    const badge=document.getElementById('ov-fb-score-badge');
    if(badge){
      badge.textContent=score+'分';
      if(score>=80){badge.style.background='rgba(34,197,94,.15)';badge.style.color='#22c55e';}
      else if(score>=50){badge.style.background='rgba(245,158,11,.15)';badge.style.color='#f59e0b';}
      else{badge.style.background='rgba(239,68,68,.15)';badge.style.color='#ef4444';}
    }
    // persona 标签
    const ptag=document.getElementById('ov-fb-persona-tag');
    if(ptag) ptag.textContent=(d.persona_flag||'')+' '+(d.persona_label||'');

    // 配置状态 3 卡片
    const cfgGrid=document.getElementById('ov-fb-config-cards');
    if(cfgGrid){
      const total=d.total_devices||0;
      const cfg=d.configured||0;
      const mainN=d.main_ch_configured||0;
      const mainName=d.main_channel_name||'LINE';
      const cfgRate=total?Math.round(cfg/total*100):0;
      const mainRate=total?Math.round(mainN/total*100):0;
      cfgGrid.innerHTML=''
        +'<div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">'
        +'<div style="font-size:18px;font-weight:700;color:#3b82f6">'+cfg+'/'+total+'</div>'
        +'<div style="font-size:9px;color:var(--text-muted);margin-top:2px">\u5F15\u6D41\u5DF2\u914D\u7F6E ('+cfgRate+'%)</div></div>'
        +'<div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">'
        +'<div style="font-size:18px;font-weight:700;color:'+(mainRate>=80?'#22c55e':mainRate>=50?'#f59e0b':'#ef4444')+'">'+mainN+'/'+total+'</div>'
        +'<div style="font-size:9px;color:var(--text-muted);margin-top:2px">'+mainName+' \u4E3B\u6E20\u9053 ('+mainRate+'%)</div></div>'
        +'<div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">'
        +'<div style="font-size:18px;font-weight:700;color:'+(d.unconfigured>0?'#ef4444':'#22c55e')+'">'+(d.unconfigured||0)+'</div>'
        +'<div style="font-size:9px;color:var(--text-muted);margin-top:2px">\u672A\u914D\u7F6E\u8BBE\u5907</div></div>';
    }

    // 漏斗进度 5 卡片
    const fGrid=document.getElementById('ov-fb-funnel-cards');
    if(fGrid){
      const f=d.funnel_24h||{};
      const items=[
        {v:f.friend_requests_sent||0,l:'\u597D\u53CB\u8BF7\u6C42',c:'#3b82f6'},
        {v:f.friends_accepted||0,l:'\u5DF2\u901A\u8FC7',c:'#22c55e'},
        {v:f.greetings_sent||0,l:'\u6253\u62DB\u547C',c:'#8b5cf6'},
        {v:f.dm_conversations||0,l:'DM \u5BF9\u8BDD',c:'#06b6d4'},
        {v:f.wa_referrals||0,l:'\u5F15\u6D41\u6210\u529F',c:'#f59e0b'},
      ];
      fGrid.innerHTML=items.map(function(it){
        return '<div style="background:var(--bg-main);border-radius:6px;padding:8px;text-align:center">'
          +'<div style="font-size:16px;font-weight:700;color:'+it.c+'">'+it.v+'</div>'
          +'<div style="font-size:8px;color:var(--text-dim);margin-top:1px">'+it.l+'</div></div>';
      }).join('');
    }

    // 操作按钮
    const acts=document.getElementById('ov-fb-health-actions');
    if(acts){
      var btns='';
      if(d.unconfigured>0){
        btns+='<button onclick="typeof fbOpenReferralModal===\'function\'&&fbOpenReferralModal()" style="padding:4px 12px;font-size:10px;background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.3);color:#f59e0b;border-radius:6px;cursor:pointer;font-weight:600">\uD83D\uDD17 \u914D\u7F6E\u5F15\u6D41</button>';
      }
      btns+='<button onclick="typeof fbOpenFunnelModal===\'function\'&&fbOpenFunnelModal()" style="padding:4px 12px;font-size:10px;background:rgba(59,130,246,.1);border:1px solid rgba(59,130,246,.3);color:#60a5fa;border-radius:6px;cursor:pointer;font-weight:600">\uD83D\uDCCA \u5B8C\u6574\u6F0F\u6597</button>';
      btns+='<button onclick="typeof fbOpenPresetsModal===\'function\'&&fbOpenPresetsModal()" style="padding:4px 12px;font-size:10px;background:rgba(24,119,242,.1);border:1px solid rgba(24,119,242,.3);color:#60a5fa;border-radius:6px;cursor:pointer;font-weight:600">\u26A1 \u6267\u884C\u65B9\u6848</button>';
      acts.innerHTML=btns;
    }
  }catch(e){
    // FB 模块未启用时静默隐藏
  }
}

/* ── 2026-05-09 P3-2: 自愈 + 链式任务运维状态 ── */
async function _loadHealChainStats(){
  const bar=document.getElementById('ov-heal-chain-bar');
  if(!bar) return;
  try{
    const [heal,chains]=await Promise.all([
      api('GET','/tasks/auto-heal/stats').catch(()=>({})),
      api('GET','/tasks/chain-runs?limit=50').catch(()=>({runs:[]})),
    ]);
    const total=heal.total||0;
    const healed=heal.healed||0;
    const runs=chains.runs||[];
    const runningChains=runs.filter(r=>r.status==='running').length;
    if(total===0&&runs.length===0){bar.style.display='none';return;}
    bar.style.display='block';
    const _s=(id,v)=>{const el=document.getElementById(id);if(el)el.textContent=v;};
    _s('heal-total',total);
    _s('heal-success',healed);
    _s('heal-rate',(heal.heal_rate_pct||0)+'%');
    _s('heal-chains',runningChains);
    _s('heal-rotate',heal.rotate_ip||0);
    _s('heal-reconnect',heal.reconnect_usb||0);
    _s('heal-retry',heal.smart_retry||0);
    _s('heal-1h',heal.heals_last_hour||0);
    _s('heal-escalated',heal.escalated||0);
    // P4-2: 加载自愈效果分析
    _loadHealEffectiveness();
    // P5-4: 链执行趋势
    _loadChainTrend();
    // P6-2: 自愈配置面板
    _loadHealConfig();
  }catch(e){/* 静默 */}
}

/* ── 2026-05-09 P6-2: 自愈策略热配置 ── */
async function _loadHealConfig(){
  const panel=document.getElementById('heal-config-panel');
  if(!panel) return;
  try{
    const cfg=await api('GET','/tasks/auto-heal/config');
    panel.style.display='block';
    const el=id=>document.getElementById(id);
    if(el('hc-max-heals')) el('hc-max-heals').value=cfg.max_heals_per_hour||3;
    if(el('hc-min-rate')) el('hc-min-rate').value=cfg.min_recovery_rate||30;
    if(el('hc-enabled')) el('hc-enabled').checked=cfg.enabled!==false;
  }catch(e){if(panel) panel.style.display='none';}
}
async function _saveHealConfig(){
  try{
    const body={
      max_heals_per_hour:parseInt(document.getElementById('hc-max-heals')?.value)||3,
      min_recovery_rate:parseInt(document.getElementById('hc-min-rate')?.value)||30,
      enabled:document.getElementById('hc-enabled')?.checked!==false,
    };
    await api('POST','/tasks/auto-heal/config',body);
    showToast('自愈配置已更新 (热生效)','success');
  }catch(e){showToast('保存失败: '+(e.message||e),'error');}
}

/* ── 2026-05-09 P5-4: 链执行趋势图 ── */
async function _loadChainTrend(){
  const wrap=document.getElementById('chain-trend-chart');
  const bars=document.getElementById('chain-trend-bars');
  if(!wrap||!bars) return;
  try{
    const r=await api('GET','/tasks/chain-trend?days=7');
    const trend=r.trend||[];
    if(!trend.length){wrap.style.display='none';return;}
    wrap.style.display='block';
    const maxVal=Math.max(...trend.map(t=>t.total),1);
    bars.innerHTML=trend.map(t=>{
      const h=Math.max(4,Math.round(t.total/maxVal*44));
      const okPct=t.total?Math.round(t.completed/t.total*100):0;
      const color=okPct>=80?'#22c55e':okPct>=50?'#f59e0b':'#ef4444';
      const day=t.date?t.date.slice(5):'';
      return `<div style="flex:1;display:flex;flex-direction:column;align-items:center;gap:2px">
        <div style="font-size:9px;color:var(--text-dim)">${t.total}</div>
        <div style="width:100%;max-width:28px;height:${h}px;background:${color};border-radius:3px 3px 0 0;opacity:0.8" title="${t.date}: ${t.completed}完成/${t.aborted}中止/${t.total}总"></div>
        <div style="font-size:8px;color:var(--text-dim)">${day}</div>
      </div>`;
    }).join('');
  }catch(e){if(wrap) wrap.style.display='none';}
}

/* ── 2026-05-09 P4-2: 自愈效果分析 ── */
async function _loadHealEffectiveness(){
  const el=document.getElementById('heal-effect-row');
  if(!el) return;
  try{
    const r=await api('GET','/tasks/auto-heal/effectiveness');
    const actions=r.actions||{};
    const keys=Object.keys(actions);
    if(!keys.length){el.style.display='none';return;}
    el.style.display='flex';
    const labels={'rotate_ip':'换IP','reconnect_usb':'重连','smart_retry':'重试'};
    el.innerHTML=keys.map(k=>{
      const a=actions[k];
      const color=a.recovery_rate>=70?'#22c55e':a.recovery_rate>=40?'#f59e0b':'#ef4444';
      return `<span>${labels[k]||k}: <b style="color:${color}">${a.recovery_rate}%</b> <span style="color:var(--text-dim)">(${a.completed}/${a.total})</span></span>`;
    }).join('');
  }catch(e){el.style.display='none';}
}

/* ── 设备效能排行 ── */
async function _loadDeviceRanking(){
  try{
    const r=await api('GET','/tiktok/daily-report');
    const devs=(r.devices||[]).filter(d=>d.total_followed>0||d.sessions_today>0);
    const el=document.getElementById('device-ranking-body');
    if(!el||!devs.length){
      const empty=document.getElementById('device-ranking-empty');
      if(empty) empty.style.display='block';
      return;
    }
    // Compute efficiency: DMs sent / follows (lead conversion proxy)
    devs.forEach(d=>{
      d._eff=d.total_followed>0?Math.round(d.total_dms/d.total_followed*100):0;
      d._today_score=d.sessions_today*20+d.watched_today;
    });
    devs.sort((a,b)=>b.total_followed-a.total_followed);
    const phaseLabel={'cold_start':'冷启动','interest_building':'兴趣培养','active':'活跃'};
    const phaseColor={'cold_start':'#64748b','interest_building':'#3b82f6','active':'#22c55e'};
    el.innerHTML=devs.slice(0,8).map((d,i)=>{
      const alias=ALIAS[d.device_id]||d.short||d.device_id.substring(0,8);
      const ph=phaseLabel[d.phase]||d.phase;
      const color=phaseColor[d.phase]||'#64748b';
      const effColor=d._eff>=50?'#22c55e':d._eff>=20?'#fbbf24':'#f87171';
      return `<tr style="border-bottom:1px solid rgba(255,255,255,.04)">
        <td style="padding:7px 12px;font-weight:600;color:var(--text)">${i+1}. ${alias}</td>
        <td style="padding:7px 12px"><span style="font-size:10px;background:${color}22;color:${color};padding:2px 6px;border-radius:4px">${ph}</span></td>
        <td style="padding:7px 12px;text-align:center;color:#60a5fa">${d.total_followed}</td>
        <td style="padding:7px 12px;text-align:center;color:#a78bfa">${d.total_dms}</td>
        <td style="padding:7px 12px;text-align:center;color:${effColor};font-weight:600">${d._eff}%</td>
        <td style="padding:7px 12px;text-align:center;color:var(--text-muted)">${d.sessions_today||0}</td>
      </tr>`;
    }).join('');
  }catch(e){
    const el=document.getElementById('device-ranking-body');
    if(el) el.innerHTML='<tr><td colspan="6" style="padding:20px;text-align:center;color:var(--text-muted)">加载失败</td></tr>';
  }
}

/* ── AI 状态监控 ── */
function _loadAIStats() {
  api('GET', '/ai/stats').then(r => {
    if (!r) return;
    const llm = r.llm || {};
    const usage = llm.usage || {};
    const rewriter = r.rewriter || {};
    const vision = r.vision || {};

    const totalCalls = usage.total_calls || 0;
    const cachedCalls = usage.cached_calls || 0;
    const cacheRate = totalCalls > 0 ? Math.round(cachedCalls / totalCalls * 100) : 0;
    const errors = usage.errors || 0;
    const errorRate = totalCalls > 0 ? Math.round(errors / totalCalls * 100) : 0;
    const cost = (usage.total_cost_usd || 0).toFixed(4);
    const tokens = (usage.total_input_tokens || 0) + (usage.total_output_tokens || 0);

    const dot = document.getElementById('ai-status-dot');
    const statusText = document.getElementById('ai-status-text');
    if (totalCalls > 0) {
      if (dot) { dot.style.background = '#4ade80'; }
      if (statusText) statusText.textContent = '正常运行中';
    } else {
      if (dot) { dot.style.background = '#94a3b8'; }
      if (statusText) statusText.textContent = '待激活（运行任务后生效）';
    }

    const set = (id, val) => { const el = document.getElementById(id); if (el) el.textContent = val; };
    set('ai-total-calls', totalCalls.toLocaleString());
    set('ai-cache-rate', cacheRate + '%');
    set('ai-rewrites', rewriter.pool ? Object.values(rewriter.pool).reduce((a,b)=>a+(b||0),0) : '-');
    set('ai-auto-replies', r.auto_replies || '-');
    set('ai-provider', llm.provider || r.provider || 'zhipu');
    set('ai-tokens', tokens > 0 ? tokens.toLocaleString() : '0');
    set('ai-cost', '$' + cost);
    set('ai-error-rate', errorRate + '%');
  }).catch(() => {});
}

/* ── AI 指令框 ── */
function _ovAiSet(cmd){
  const input=document.getElementById('ov-ai-input');
  if(input) input.value=cmd;
}
async function _ovAiExec(){
  const input=document.getElementById('ov-ai-input');
  const result=document.getElementById('ov-ai-result');
  if(!input||!input.value.trim()) return;
  const cmd=input.value.trim();
  if(result) result.innerHTML='<span style="color:var(--accent)">执行中...</span>';
  try{
    const d=await api('POST','/ai/quick-command',{command:cmd});
    if(d.ok===false && d.message){
      // quick-command 不识别，转 /chat
      const d2=await api('POST','/chat',{message:cmd});
      if(result){
        let html='<span style="color:#22c55e">'+((d2.reply||'').replace(/\n/g,'<br>'))+'</span>';
        if(d2.task_ids&&d2.task_ids.length) html+=' <span style="color:var(--text-dim)">('+d2.task_ids.length+' 个任务)</span>';
        if(typeof formatChatTaskHintsHtml==='function') html+=formatChatTaskHintsHtml(d2);
        result.innerHTML=html;
      }
    }else{
      if(result){
        let msg=d.message||'';
        if(d.created) msg+=' (创建 '+d.created+' 个任务)';
        result.innerHTML='<span style="color:#22c55e">'+(msg||'OK')+'</span>';
      }
    }
    input.value='';
    setTimeout(_loadOpsDashboard,3000);
  }catch(e){
    if(result) result.innerHTML='<span style="color:#ef4444">'+e.message+'</span>';
  }
}

/* ── TikTok Tab 切换 ── */
let _currentTtTab='ops';
const _TT_TABS=['ops','farming','conv','leads','msg'];
function ttTab(name){
  _TT_TABS.forEach(t=>{
    const el=document.getElementById('tt-tab-'+t);
    if(el)el.style.display=(t===name)?'':'none';
  });
  _TT_TABS.forEach(t=>{
    const btn=document.getElementById('tt-btn-'+t);
    if(!btn)return;
    if(t===name){
      btn.style.background='var(--accent)';btn.style.color='#fff';btn.style.borderColor='var(--accent)';
    }else{
      btn.style.background='var(--bg-card)';btn.style.color='var(--text)';btn.style.borderColor='var(--border)';
    }
  });
  _currentTtTab=name;
  const el=document.getElementById('tt-tab-'+name);
  if(!el)return;
  if(name==='ops'){
    if(typeof loadPlatformPage==='function')loadPlatformPage('tiktok');
    loadTtOpsPanel();
  } else if(name==='farming'){
    if(el.children.length===0||el.innerHTML.trim()===''){
      // 把 page-account-farming 的内容移入
      const src=document.getElementById('page-account-farming');
      if(src){el.appendChild(src);src.style.display='';}
      else el.innerHTML='<div style="padding:8px;color:var(--text-muted)">加载中...</div>';
    }
    if(typeof AccountFarming!=='undefined')AccountFarming.refresh();
  } else if(name==='conv'){
    if(el.children.length===0||el.innerHTML.trim()===''){
      const src=document.getElementById('page-conversations');
      if(src){el.appendChild(src);src.style.display='';}
      else el.innerHTML='<div style="padding:8px;color:var(--text-muted)">加载中...</div>';
    }
    if(typeof Conv!=='undefined')Conv.refresh();
  } else if(name==='leads'){
    if(el.children.length===0||el.innerHTML.trim()===''){
      const src=document.getElementById('page-leads');
      if(src){el.appendChild(src);src.style.display='';}
      else el.innerHTML='<div style="padding:8px;color:var(--text-muted)">加载中...</div>';
    }
    if(typeof Leads!=='undefined')Leads.refresh();
  } else if(name==='msg'){
    if(el.children.length===0||el.innerHTML.trim()===''){
      const src=document.getElementById('page-messages');
      if(src){el.appendChild(src);src.style.display='';}
      else el.innerHTML='<div style="padding:8px;color:var(--text-muted)">加载中...</div>';
    }
    if(typeof Msg!=='undefined')Msg.refresh();
  }
}

/* ── TikTok 运营指挥台数据加载 ── */
let _ttCtaUrl = localStorage.getItem('tt_cta_url') || '';

async function ttSetCtaUrl() {
  const v = await ocPrompt('转化链接', _ttCtaUrl, {message:'WhatsApp/产品页 URL',inputPlaceholder:'https://...'});
  if (v !== null) {
    _ttCtaUrl = v.trim();
    localStorage.setItem('tt_cta_url', _ttCtaUrl);
    const hint = document.getElementById('tt-hot-cta-hint');
    if (hint) hint.innerHTML = _ttCtaUrl
      ? `CTA: <span style="color:var(--accent)">${_ttCtaUrl.slice(0,30)}...</span> <button onclick="ttSetCtaUrl()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:11px">✎</button>`
      : '配置话术链接: <button onclick="ttSetCtaUrl()" style="background:none;border:none;color:var(--accent);cursor:pointer;font-size:11px">✎ 设置</button>';
    showToast(_ttCtaUrl ? '话术链接已保存' : '话术链接已清除', 'success');
  }
}

/* ═══ 设备网格 - 以手机为主的运营中心 ═══ */
function _ttInjectStyles() {
  if (document.getElementById('tt-grid-styles')) return;
  const s = document.createElement('style');
  s.id = 'tt-grid-styles';
  s.textContent = `
    .tt-cmd-bar{display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px;margin-bottom:12px;padding:8px 12px;background:var(--bg-card);border:1px solid var(--border);border-radius:8px}
    .tt-cmd-stats{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
    .tt-cmd-pill{font-size:11px;padding:3px 10px;background:rgba(255,255,255,.05);border:1px solid var(--border);border-radius:20px;color:var(--text-muted)}
    .tt-cmd-pill b{color:var(--text-main)}
    .tt-cmd-actions{display:flex;gap:6px;flex-wrap:wrap}
    .tt-cmd-btn{padding:5px 12px;font-size:12px;background:var(--bg-card);color:var(--text);border:1px solid var(--border);border-radius:6px;cursor:pointer;white-space:nowrap}
    .tt-cmd-btn.primary{background:var(--accent);color:#fff;border-color:var(--accent);font-weight:600}
    .tt-cmd-btn:hover{opacity:.85}
    .tt-cmd-tier-primary{align-items:center}
    .tt-cmd-dd{position:relative;display:inline-block}
    .tt-cmd-dd-summary{list-style:none;cursor:pointer}
    .tt-cmd-dd-summary::-webkit-details-marker{display:none}
    .tt-cmd-dd[open]>.tt-cmd-dd-summary{background:rgba(99,102,241,.12);border-color:rgba(99,102,241,.35);color:#a5b4fc}
    .tt-cmd-dd-menu{position:absolute;right:0;top:calc(100% + 4px);min-width:228px;padding:6px;background:#0f172a;border:1px solid rgba(99,102,241,.28);border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.5);z-index:2000;display:flex;flex-direction:column;gap:2px}
    .tt-cmd-dd-item{display:block;width:100%;text-align:left;padding:8px 12px;font-size:12px;color:#e2e8f0;background:transparent;border:none;border-radius:6px;cursor:pointer;line-height:1.35}
    .tt-cmd-dd-item:hover{background:rgba(99,102,241,.18);color:#fff}
    .tt-card-quick{display:flex;gap:5px;margin-top:6px;padding-top:8px;border-top:1px solid rgba(255,255,255,.06)}
    .tt-card-qbtn{flex:1;font-size:10px;padding:5px 4px;border-radius:6px;border:1px solid rgba(99,102,241,.28);background:rgba(99,102,241,.1);color:#a5b4fc;cursor:pointer;white-space:nowrap;font-weight:500}
    .tt-card-qbtn:hover{background:rgba(99,102,241,.22)}
    .tt-card-qbtn:disabled{opacity:.4;cursor:not-allowed}
    .ttp-quick-actions{display:flex;flex-wrap:wrap;gap:6px;margin-top:8px}
    .ttp-qa-btn{flex:1;min-width:120px;padding:8px 10px;font-size:11px;border-radius:8px;border:1px solid rgba(99,102,241,.35);background:rgba(99,102,241,.12);color:#c7d2fe;cursor:pointer;font-weight:600}
    .ttp-qa-btn:hover{background:rgba(99,102,241,.22)}
    .ttp-qa-btn:disabled{opacity:.45;cursor:not-allowed}
    .tt-dev-card{background:#111827;border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:14px;cursor:pointer;transition:all .2s ease;position:relative;overflow:hidden}
    .tt-dev-card:hover{border-color:rgba(99,102,241,.35);box-shadow:0 4px 16px rgba(99,102,241,.12);transform:translateY(-1px)}
    .tt-dev-card.offline{opacity:.5}
    .tt-dev-card-head{display:flex;align-items:center;gap:8px;margin-bottom:10px}
    .tt-dev-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
    .tt-dev-alias{font-size:14px;font-weight:700;color:#f1f5f9;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .tt-dev-node{font-size:9px;padding:2px 6px;border-radius:4px;flex-shrink:0;font-weight:500}
    .tt-dev-stats{display:grid;grid-template-columns:1fr 1fr;gap:4px;margin-bottom:8px}
    .tt-dev-stat{font-size:10px;color:#64748b;background:rgba(255,255,255,.03);border-radius:6px;padding:4px 6px;text-align:center}
    .tt-dev-stat b{display:block;font-size:14px;font-weight:700;color:#e2e8f0}
    .tt-dev-tags{display:flex;flex-wrap:wrap;gap:4px}
    .tt-dev-tag{font-size:9px;padding:2px 7px;border-radius:4px;background:rgba(255,255,255,.05);color:#94a3b8;font-weight:500}
    .tt-dev-tag.ok{background:rgba(34,197,94,.1);color:#22c55e}
    .tt-dev-tag.warn{background:rgba(245,158,11,.1);color:#f59e0b}
    .tt-dev-tag.hot{background:rgba(239,68,68,.1);color:#f87171}
    #tt-dev-panel{position:fixed;top:0;right:-480px;width:420px;height:100vh;background:#0b1120;border-left:1px solid rgba(99,102,241,.15);z-index:1000;transition:right .28s cubic-bezier(.4,0,.2,1);overflow-y:auto;padding:0;box-shadow:-8px 0 40px rgba(0,0,0,.6)}
    #tt-dev-panel.open{right:0}
    #tt-dev-panel.shifted{right:360px}
    #tt-leads-panel{position:fixed;top:0;right:-400px;width:360px;height:100vh;background:#111827;border-left:1px solid rgba(99,102,241,.25);z-index:1001;display:flex;flex-direction:column;transition:right .28s cubic-bezier(.4,0,.2,1);box-shadow:-12px 0 40px rgba(0,0,0,.6)}
    #tt-leads-panel.open{right:0}
    .tt-lp-header{display:flex;align-items:center;gap:10px;padding:14px 16px 12px;border-bottom:1px solid rgba(255,255,255,.07);flex-shrink:0;background:#111827;position:sticky;top:0;z-index:1}
    .tt-lp-back{width:32px;height:32px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:8px;color:var(--text-muted);cursor:pointer;font-size:17px;flex-shrink:0;transition:background .15s}
    .tt-lp-back:hover{background:rgba(255,255,255,.12);color:var(--text-main)}
    .tt-lp-filter{display:flex;gap:4px;padding:10px 16px 2px;flex-shrink:0;overflow-x:auto}
    .tt-lp-tab{padding:4px 12px;font-size:11px;font-weight:600;border-radius:20px;border:1px solid rgba(255,255,255,.1);background:transparent;color:var(--text-muted);cursor:pointer;white-space:nowrap;transition:all .15s}
    .tt-lp-tab.active{background:rgba(99,102,241,.2);border-color:rgba(99,102,241,.5);color:#a5b4fc}
    .tt-lp-body{flex:1;overflow-y:auto;padding:12px 16px 20px}
    .tt-lp-card{padding:12px;margin-bottom:8px;border-radius:10px;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);transition:border-color .15s,background .15s}
    .tt-lp-card:hover{background:rgba(255,255,255,.07);border-color:rgba(99,102,241,.3)}
    .tt-lp-card.sent{opacity:.45;pointer-events:none}
    .tt-lp-pitch{font-size:11px;padding:5px 11px;border-radius:6px;background:rgba(99,102,241,.15);border:1px solid rgba(99,102,241,.3);color:#a5b4fc;cursor:pointer;transition:all .15s;white-space:nowrap}
    .tt-lp-pitch:hover{background:rgba(99,102,241,.3);border-color:#818cf8}
    .tt-lp-pitch:disabled{opacity:.5;cursor:not-allowed}
    .tt-lp-footer{padding:12px 16px;border-top:1px solid rgba(255,255,255,.07);flex-shrink:0}
    .tt-lp-batch{width:100%;padding:10px;font-size:13px;font-weight:600;border-radius:8px;background:rgba(99,102,241,.2);border:1px solid rgba(99,102,241,.4);color:#a5b4fc;cursor:pointer;transition:all .15s}
    .tt-lp-batch:hover{background:rgba(99,102,241,.35)}
    #tt-dev-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.45);z-index:999;backdrop-filter:blur(2px)}
    /* ── 新面板样式 ── */
    .ttp-header{display:flex;align-items:center;gap:10px;padding:16px 20px;border-bottom:1px solid rgba(255,255,255,.06);position:sticky;top:0;background:#0b1120;z-index:1}
    .ttp-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0}
    .ttp-hinfo{flex:1;min-width:0}
    .ttp-name{font-size:16px;font-weight:700;color:#f1f5f9}
    .ttp-meta{font-size:11px;color:#64748b;margin-top:1px}
    .ttp-close{margin-left:auto;width:28px;height:28px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.06);border:none;border-radius:6px;color:#64748b;cursor:pointer;font-size:14px;flex-shrink:0;transition:.15s}
    .ttp-close:hover{background:rgba(255,255,255,.12);color:#f1f5f9}
    .ttp-body{padding:14px 18px 20px}
    .ttp-zone{margin-bottom:14px;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px}
    .ttp-zone-title{font-size:12px;font-weight:600;color:#94a3b8;margin-bottom:8px}
    .ttp-stats{display:grid;grid-template-columns:repeat(3,1fr);gap:4px}
    .ttp-st{text-align:center;padding:6px 2px;border-radius:6px;background:rgba(255,255,255,.02);transition:background .15s}
    .ttp-st:hover{background:rgba(255,255,255,.06)}
    .ttp-st b{display:block;font-size:17px;font-weight:700;line-height:1.3}
    .ttp-st span{font-size:10px;color:#64748b}
    .ttp-primary-btn{width:100%;padding:11px 16px;background:linear-gradient(135deg,#6366f1,#8b5cf6);border:none;border-radius:8px;color:#fff;font-size:13px;font-weight:600;cursor:pointer;text-align:center;transition:.2s;display:flex;align-items:center;justify-content:center;gap:8px;margin-bottom:10px}
    .ttp-primary-btn:hover{filter:brightness(1.1);transform:translateY(-1px)}
    .ttp-primary-btn:disabled{opacity:.4;cursor:not-allowed;transform:none}
    .ttp-cron{font-size:9px;color:rgba(255,255,255,.5);font-weight:400}
    .ttp-action-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px}
    .ttp-action-card{display:flex;flex-direction:column;align-items:center;gap:4px;padding:14px 8px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:10px;cursor:pointer;transition:.2s}
    .ttp-action-card:hover{background:rgba(99,102,241,.08);border-color:rgba(99,102,241,.25);transform:translateY(-1px)}
    .ttp-ac-icon{font-size:22px}
    .ttp-ac-label{font-size:12px;font-weight:600;color:#cbd5e1}
    .ttp-ac-count{font-size:10px;color:#64748b}
    .ttp-small-btn{background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.1);border-radius:5px;padding:4px 10px;font-size:11px;cursor:pointer;transition:.15s}
    .ttp-small-btn:hover{background:rgba(255,255,255,.08)}
    /* ── 弹出模态框 (通讯录 / 智能分析) ── */
    .ttp-modal-overlay{position:fixed;inset:0;z-index:2000;background:rgba(0,0,0,.6);backdrop-filter:blur(4px);display:flex;align-items:center;justify-content:center;padding:20px;animation:ttp-fade-in .2s ease}
    .ttp-modal{background:#111827;border-radius:16px;border:1px solid rgba(99,102,241,.2);width:680px;max-width:90vw;max-height:85vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.5);animation:ttp-scale-in .25s ease}
    .ttp-modal-header{display:flex;align-items:center;gap:12px;padding:16px 20px;border-bottom:1px solid rgba(255,255,255,.06);flex-shrink:0}
    .ttp-modal-title{flex:1;font-size:16px;font-weight:700;color:#f1f5f9}
    .ttp-modal-close{width:32px;height:32px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.06);border:none;border-radius:8px;color:#64748b;cursor:pointer;font-size:16px;transition:.15s}
    .ttp-modal-close:hover{background:rgba(255,255,255,.12);color:#f1f5f9}
    .ttp-modal-body{flex:1;overflow-y:auto;padding:16px 20px}
    .ttp-modal-footer{padding:12px 20px;border-top:1px solid rgba(255,255,255,.06);flex-shrink:0;display:flex;gap:8px;justify-content:flex-end}
    @keyframes ttp-fade-in{from{opacity:0}to{opacity:1}}
    @keyframes ttp-scale-in{from{opacity:0;transform:scale(.96)}to{opacity:1;transform:scale(1)}}

    /* ── 通讯录模态框专属 ── */
    .ctm-funnel{display:flex;align-items:center;gap:4px;margin-bottom:12px;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:12px;padding:14px 10px}
    .ctm-fn-step{flex:1;text-align:center}
    .ctm-fn-num{font-size:22px;font-weight:700;line-height:1.2}
    .ctm-fn-pct{font-size:11px;font-weight:400;opacity:.6;margin-left:3px}
    .ctm-fn-label{font-size:10px;color:#64748b;margin-top:2px}
    .ctm-fn-arrow{color:#334155;font-size:18px;font-weight:700;flex-shrink:0}
    .ctm-progress-bar{display:flex;height:6px;border-radius:3px;overflow:hidden;margin-bottom:14px;position:relative;background:#1e293b}
    .ctm-pb-fill{position:absolute;left:0;top:0;height:100%;border-radius:3px;opacity:.85;transition:width .4s ease}
    .ctm-progress-bar.ctm-progress-degraded{opacity:.38;pointer-events:none}
    .ctm-banner-info{margin-bottom:12px;padding:12px 14px;border-radius:10px;border:1px solid rgba(59,130,246,.28);background:rgba(59,130,246,.07)}
    .ctm-banner-info-title{font-size:13px;font-weight:600;color:#93c5fd;margin-bottom:6px}
    .ctm-banner-info-sub{font-size:11px;color:#94a3b8;line-height:1.55}
    .ctm-details-tech{margin-top:8px;font-size:10px;color:#64748b}
    .ctm-details-tech summary{cursor:pointer;color:#64748b;user-select:none}
    .ctm-details-tech > div{margin-top:6px;line-height:1.45}
    .ctm-funnel-degraded{opacity:.42;position:relative}
    .ctm-funnel-degraded::after{content:"";position:absolute;inset:0;border-radius:12px;background:rgba(15,23,42,.35);pointer-events:none}
    .ctm-funnel-caption{font-size:10px;color:#64748b;margin:-8px 0 10px;padding:0 4px}
    .ctm-actions{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
    .ctm-btn{padding:7px 14px;font-size:12px;font-weight:500;border-radius:8px;cursor:pointer;border:1px solid transparent;transition:.15s}
    .ctm-btn:hover{filter:brightness(1.2)}
    .ctm-btn-primary{background:rgba(99,102,241,.12);border-color:rgba(99,102,241,.3);color:#818cf8}
    .ctm-btn-warn{background:rgba(245,158,11,.08);border-color:rgba(245,158,11,.3);color:#f59e0b}
    .ctm-btn-purple{background:rgba(139,92,246,.08);border-color:rgba(139,92,246,.3);color:#8b5cf6}
    .ctm-btn-danger{background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.3);color:#ef4444}
    .ctm-add-row{display:flex;gap:6px;margin-bottom:8px;align-items:center}
    .ctm-filter-row{display:flex;gap:6px;align-items:center}
    .ctm-input{background:#1e293b;border:1px solid rgba(255,255,255,.08);border-radius:6px;padding:7px 10px;color:#e2e8f0;font-size:12px;outline:none}
    .ctm-input:focus{border-color:rgba(99,102,241,.4)}
    .ctm-table{width:100%;border-collapse:collapse;font-size:12px}
    .ctm-table thead tr{border-bottom:1px solid rgba(255,255,255,.08)}
    .ctm-table th{padding:7px 8px;color:#64748b;font-weight:500;font-size:10px;text-transform:uppercase;letter-spacing:.04em}
    .ctm-table tbody tr{border-bottom:1px solid rgba(255,255,255,.03);transition:background .1s}
    .ctm-table tbody tr:hover{background:rgba(99,102,241,.04)}
    .ctm-table td{padding:6px 8px}
    .ctm-td-name{color:#e2e8f0;font-weight:500;font-size:12px;max-width:140px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .ctm-tag{display:inline-block;font-size:10px;padding:2px 7px;border-radius:8px;font-weight:500;white-space:nowrap}
    .ctm-tag-indigo{background:rgba(99,102,241,.12);color:#818cf8}
    .ctm-tag-green{background:rgba(34,197,94,.12);color:#22c55e}
    .ctm-tag-success{background:rgba(34,197,94,.1);color:#22c55e}
    .ctm-tag-purple{background:rgba(139,92,246,.12);color:#a78bfa}
    .ctm-greet-btn{background:rgba(99,102,241,.15);border:1px solid rgba(99,102,241,.3);border-radius:6px;padding:3px 10px;color:#818cf8;font-size:10px;font-weight:500;cursor:pointer;transition:.15s}
    .ctm-greet-btn:hover{background:rgba(99,102,241,.25)}
    .ctm-preview{background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.2);border-radius:10px;padding:12px;animation:ttp-scale-in .2s ease}
    .ctm-pv-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px;font-size:12px;color:#e2e8f0}
    .ctm-pv-ab{font-size:10px;margin-left:6px;padding:2px 6px;border-radius:6px;background:rgba(99,102,241,.2);color:#a5b4fc}
    .ctm-pv-engine{font-size:10px;color:#818cf8;background:rgba(99,102,241,.12);padding:2px 8px;border-radius:12px}
    .ctm-pv-textarea{width:100%;min-height:60px;background:#0f172a;border:1px solid rgba(255,255,255,.1);border-radius:8px;padding:10px;color:#e2e8f0;font-size:12px;line-height:1.5;resize:vertical;outline:none;box-sizing:border-box;font-family:inherit}
    .ctm-pv-textarea:focus{border-color:rgba(99,102,241,.4)}
    .ctm-pv-actions{display:flex;gap:6px;margin-top:8px}

    /* ── 智能分析漏斗 ── */
    .anm-funnel{margin-bottom:14px}
    .anm-fn-row{display:flex;flex-direction:column;align-items:center}
    .anm-fn-bar{position:relative;height:32px;border-radius:6px;overflow:hidden;display:flex;align-items:center;margin:2px 0}
    .anm-fn-bar-fill{position:absolute;left:0;top:0;height:100%;border-radius:6px;transition:width .5s ease}
    .anm-fn-bar-text{position:relative;z-index:1;padding:0 12px;font-size:11px;color:#cbd5e1;white-space:nowrap;width:100%}
    .anm-fn-arrow{color:#334155;font-size:10px;line-height:1}
    .anm-dist{display:flex;height:8px;border-radius:4px;overflow:hidden;gap:2px;margin-bottom:6px}
    .anm-dist-seg{min-width:4px;border-radius:4px;transition:flex .4s ease}
    .anm-dist-legend{display:flex;gap:12px;flex-wrap:wrap;font-size:10px;color:#94a3b8;margin-bottom:6px}
    .anm-dot{display:inline-block;width:8px;height:8px;border-radius:50%;margin-right:4px;vertical-align:middle}
    .anm-lead-grid{display:grid;gap:6px;max-height:260px;overflow-y:auto}
    .anm-lead-card{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:10px 14px;display:flex;align-items:center;gap:10px;transition:background .15s}
    .anm-lead-card:hover{background:rgba(255,255,255,.05)}
    .anm-lead-name{font-size:12px;font-weight:600;color:#e2e8f0;display:flex;align-items:center;gap:6px}
    .anm-stage-tag{font-size:9px;padding:2px 6px;border-radius:8px;font-weight:500}
    .anm-lead-bio{font-size:11px;color:#94a3b8;margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .anm-lead-meta{font-size:10px;color:#64748b;margin-top:2px}
    .anm-lead-score{text-align:center;min-width:36px;font-size:18px;font-weight:700;line-height:1}
    .anm-lead-score span{display:block;font-size:9px;font-weight:400;color:#64748b}

    .ttp-dev-tag{display:inline-block;font-size:10px;padding:2px 8px;margin:2px 4px 0 0;border-radius:6px;background:rgba(99,102,241,.12);color:#a5b4fc}
    .tt-card-cron{font-size:9px;font-weight:500}

    /* ── 转化漏斗 ── */
    .cvf-kpi-row{display:grid;grid-template-columns:repeat(5,1fr);gap:8px;margin-bottom:14px}
    .cvf-kpi{text-align:center;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:10px 4px}
    .cvf-kpi-num{font-size:20px;font-weight:700;line-height:1.2}
    .cvf-kpi-label{font-size:10px;color:#64748b;margin-top:2px}
    .cvf-funnel{margin-bottom:16px}
    .cvf-stage{display:flex;justify-content:center}
    .cvf-bar{position:relative;margin:0 auto;padding:8px 14px;border-radius:8px;min-height:28px;display:flex;align-items:center;transition:.3s}
    .cvf-bar-fill{position:absolute;inset:0;border-radius:8px}
    .cvf-bar-text{position:relative;z-index:1;font-size:12px;color:#e2e8f0;white-space:nowrap}
    .cvf-arrow{text-align:center;color:#475569;font-size:12px;line-height:1.2;margin:2px 0}
    .cvf-section{margin-top:14px;padding-top:14px;border-top:1px solid rgba(255,255,255,.06)}
    .cvf-section-title{font-size:12px;font-weight:600;color:#e2e8f0;margin-bottom:8px}
    .cvf-mini-kpi{text-align:center;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:8px;display:flex;flex-direction:column;gap:2px}

    /* ── 对话质量 ── */
    .conv-kpi-row{display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:12px}
    .conv-kpi{text-align:center;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:10px 4px}
    .conv-kpi-num{font-size:22px;font-weight:700;line-height:1.2}
    .conv-kpi-label{font-size:10px;color:#64748b;margin-top:2px}
    .conv-quality-ring{width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:700;flex-shrink:0;background:conic-gradient(var(--q-color) calc(var(--q-pct) * 1%),rgba(255,255,255,.06) 0);position:relative}
    .conv-quality-ring::before{content:'';position:absolute;inset:3px;border-radius:50%;background:#111827}
    .conv-quality-ring span{position:relative;z-index:1}

    /* ── 多选模式 ── */
    .tt-ms-cb{position:absolute;top:8px;right:8px;font-size:18px;color:#64748b;z-index:10;cursor:pointer;line-height:1}
    .tt-ms-selected{border-color:rgba(99,102,241,.5)!important;box-shadow:0 0 0 2px rgba(99,102,241,.2)!important;background:#0f1729!important}
    .tt-ms-selected .tt-ms-cb{color:#818cf8}
    .tt-batch-bar{position:fixed;bottom:0;left:0;right:0;z-index:1500;background:#111827;border-top:1px solid rgba(99,102,241,.3);padding:10px 20px;display:flex;align-items:center;justify-content:space-between;gap:12px;animation:ttp-fade-in .2s ease;box-shadow:0 -4px 20px rgba(0,0,0,.4)}
    .tt-bb-info{font-size:13px;color:#e2e8f0;white-space:nowrap}
    .tt-bb-info b{color:#818cf8}
    .tt-bb-actions{display:flex;gap:6px;flex-wrap:wrap}
    .tt-bb-btn{padding:7px 14px;font-size:12px;font-weight:500;border-radius:8px;cursor:pointer;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.05);color:#e2e8f0;transition:.15s}
    .tt-bb-btn:hover{background:rgba(255,255,255,.1)}
    .tt-bb-primary{background:rgba(99,102,241,.2);border-color:rgba(99,102,241,.4);color:#a5b4fc}
    .tt-bb-contacts{background:rgba(245,158,11,.1);border-color:rgba(245,158,11,.3);color:#f59e0b}
    .tt-bb-cancel{background:rgba(239,68,68,.08);border-color:rgba(239,68,68,.2);color:#ef4444}

    /* 保留旧引用兼容 */
    .tt-panel-header{display:none}
    .tt-panel-body{display:none}
    .tt-panel-section{margin-bottom:16px}
    .tt-panel-section-title{font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:.05em;margin-bottom:8px}
    .tt-ref-row{display:flex;align-items:center;gap:6px;margin-bottom:6px}
    .tt-ref-label{font-size:11px;font-weight:600;width:32px;flex-shrink:0}
    .tt-ref-input{flex:1;background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);border-radius:6px;padding:6px 8px;color:#e2e8f0;font-size:12px;outline:none;transition:.15s}
    .tt-ref-input:focus{border-color:rgba(99,102,241,.5);background:rgba(99,102,241,.05)}
    .tt-lead-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid rgba(51,65,85,.35)}
    .tt-lead-name{flex:1;font-size:12px;font-weight:500;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .tt-lead-score{font-size:10px;font-weight:700;padding:2px 6px;border-radius:4px}
    .tt-lead-pitch{padding:3px 8px;font-size:11px;background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.3);border-radius:5px;cursor:pointer}
    /* ── P3: 卡片状态条 ── */
    .tt-dev-card::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:10px 0 0 10px}
    .tt-dev-card.state-replied::before{background:#f59e0b;width:4px;box-shadow:0 0 8px #f59e0b80}
    .tt-dev-card.state-leads::before{background:#f59e0b}
    .tt-dev-card.state-active::before{background:#22c55e}
    .tt-dev-card.state-idle::before{background:#94a3b840}
    .tt-dev-card.state-offline::before{background:#ef444450}
    @keyframes tt-replied-pulse{0%,100%{box-shadow:0 0 0 0 rgba(245,158,11,.4)}50%{box-shadow:0 0 0 4px rgba(245,158,11,.1)}}
    .tt-dev-card.state-replied{animation:tt-replied-pulse 2s infinite}
    /* ── P3: 线索 badge ── */
    .tt-dev-badge{position:absolute;top:7px;right:7px;min-width:18px;height:18px;padding:0 5px;border-radius:9px;background:#ef4444;color:#fff;font-size:9px;font-weight:700;display:flex;align-items:center;justify-content:center;line-height:1;box-shadow:0 2px 6px rgba(239,68,68,.5)}
    /* ── P3: 紧凑统计行 ── */
    .tt-dev-stat-row{display:flex;gap:10px;margin-bottom:6px;font-size:11px;color:var(--text-muted)}
    .tt-dev-stat-row span{display:flex;align-items:center;gap:2px}
    .tt-dev-stat-row b{color:var(--text-main);font-weight:600}
    /* ── P3: 算法分进度条 ── */
    .tt-dev-algo-row{margin-bottom:5px}
    .tt-dev-algo-bar{height:3px;background:rgba(255,255,255,.08);border-radius:2px;overflow:hidden;margin-top:3px}
    .tt-dev-algo-fill{height:100%;border-radius:2px;transition:width .4s ease}
    /* ── P1: 优先操作横幅 ── */
    #tt-priority-banner{display:flex;align-items:center;gap:10px;flex-wrap:wrap;margin-bottom:10px;padding:10px 14px;background:rgba(245,158,11,.07);border:1px solid rgba(245,158,11,.22);border-radius:8px;animation:tt-banner-in .3s ease}
    @keyframes tt-banner-in{from{opacity:0;transform:translateY(-6px)}to{opacity:1;transform:none}}
    /* ── P2: 线索评分条 ── */
    .tt-lp-score-wrap{display:flex;flex-direction:column;align-items:flex-end;gap:3px;min-width:44px}
    .tt-lp-score-num{font-size:12px;font-weight:700}
    .tt-lp-score-bar{height:3px;width:44px;background:rgba(255,255,255,.1);border-radius:2px;overflow:hidden}
    .tt-lp-score-fill{height:100%;border-radius:2px}
    .tt-lp-score-stars{font-size:9px;letter-spacing:1px}
    /* ── P2: 线索卡左状态条 ── */
    .tt-lp-card{position:relative;padding:12px 12px 12px 16px}
    .tt-lp-card::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:10px 0 0 10px}
    .tt-lp-card.st-responded::before{background:#f59e0b}
    .tt-lp-card.st-qualified::before{background:#22c55e}
    .tt-lp-card.st-contacted::before{background:#60a5fa}
    .tt-lp-card.st-converted::before{background:#a78bfa}
    /* ── P4: Toast 通知 ── */
    #tt-toast-container{position:fixed;bottom:24px;right:24px;z-index:9900;display:flex;flex-direction:column;gap:8px;pointer-events:none}
    .tt-toast{padding:10px 14px;border-radius:8px;font-size:12px;font-weight:500;pointer-events:none;max-width:300px;box-shadow:0 4px 20px rgba(0,0,0,.5);animation:tt-toast-in .3s cubic-bezier(.4,0,.2,1)}
    .tt-toast.info{background:#1e293b;color:#e2e8f0;border:1px solid rgba(99,102,241,.4)}
    .tt-toast.success{background:rgba(34,197,94,.15);color:#86efac;border:1px solid rgba(34,197,94,.35)}
    .tt-toast.warning{background:rgba(245,158,11,.15);color:#fcd34d;border:1px solid rgba(245,158,11,.35)}
    .tt-toast.error{background:rgba(239,68,68,.15);color:#fca5a5;border:1px solid rgba(239,68,68,.35)}
    @keyframes tt-toast-in{from{opacity:0;transform:translateY(10px) scale(.96)}to{opacity:1;transform:none}}
    @keyframes tt-toast-out{from{opacity:1;transform:none}to{opacity:0;transform:translateY(10px) scale(.96)}}
    /* ── P4: 卡片新线索闪烁 ── */
    @keyframes tt-new-leads{0%{box-shadow:0 0 0 0 rgba(245,158,11,.5)}50%{box-shadow:0 0 0 5px rgba(245,158,11,.15)}100%{box-shadow:0 0 0 0 rgba(245,158,11,0)}}
    .tt-dev-card.new-leads{animation:tt-new-leads 1.2s ease 2}
    /* ── P4: 自动刷新状态指示 ── */
    #tt-refresh-dot{width:6px;height:6px;border-radius:50%;background:#22c55e;display:inline-block;margin-left:4px;vertical-align:middle}
    #tt-refresh-dot.stale{background:#94a3b8}
    #tt-refresh-dot.loading{background:#f59e0b;animation:tt-pulse-dot 1s ease-in-out infinite}
    @keyframes tt-pulse-dot{0%,100%{opacity:.4}50%{opacity:1}}
    /* ── P5: 话术预览弹窗 ── */
    #tt-pitch-overlay{position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.65);backdrop-filter:blur(5px);display:flex;align-items:center;justify-content:center;padding:20px;animation:tt-toast-in .2s ease}
    #tt-pitch-modal{background:#1e293b;border:1px solid rgba(255,255,255,.1);border-radius:14px;width:100%;max-width:400px;box-shadow:0 24px 64px rgba(0,0,0,.7);overflow:hidden}
    .tt-pitch-modal-head{padding:16px 20px 12px;border-bottom:1px solid rgba(255,255,255,.07)}
    .tt-pitch-modal-body{padding:16px 20px}
    .tt-pitch-textarea{width:100%;min-height:90px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12);border-radius:8px;padding:10px 12px;color:var(--text-main);font-size:13px;line-height:1.5;resize:vertical;outline:none;font-family:inherit;box-sizing:border-box}
    .tt-pitch-textarea:focus{border-color:rgba(99,102,241,.5)}
    .tt-pitch-modal-foot{padding:12px 20px 16px;display:flex;gap:8px;justify-content:flex-end}
    .tt-pitch-cancel{padding:8px 16px;font-size:12px;border-radius:7px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);color:var(--text-muted);cursor:pointer}
    .tt-pitch-confirm{padding:8px 18px;font-size:12px;font-weight:600;border-radius:7px;background:rgba(99,102,241,.25);border:1px solid rgba(99,102,241,.4);color:#a5b4fc;cursor:pointer}
    .tt-pitch-confirm:hover{background:rgba(99,102,241,.4)}
    /* ── P6: 健康警告 ── */
    .tt-dev-card.state-warn-idle::before{background:#f59e0b;animation:tt-warn-pulse 2.5s ease-in-out infinite}
    @keyframes tt-warn-pulse{0%,100%{opacity:.3}50%{opacity:1}}
    /* ── P0: Flow Config Modal (策略卡片 v2) ── */
    #tt-flow-overlay{position:fixed;inset:0;z-index:9600;background:rgba(0,0,0,.72);backdrop-filter:blur(4px);display:none;align-items:center;justify-content:center;padding:16px}
    #tt-flow-overlay.open{display:flex}
    #tt-flow-modal{background:#0f172a;border:1px solid rgba(99,102,241,.35);border-radius:16px;width:100%;max-width:660px;max-height:92vh;display:flex;flex-direction:column;box-shadow:0 28px 80px rgba(0,0,0,.85);overflow:hidden;animation:tt-flow-in .22s cubic-bezier(.4,0,.2,1)}
    @keyframes tt-flow-in{from{opacity:0;transform:scale(.94) translateY(12px)}to{opacity:1;transform:none}}
    .ttf-head{padding:18px 24px 14px;border-bottom:1px solid rgba(255,255,255,.08);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
    .ttf-title{font-size:18px;font-weight:700;color:var(--text-main)}
    .ttf-subtitle{font-size:13px;color:var(--text-muted);margin-top:3px}
    .ttf-head-refresh-audience{font-size:11px;padding:7px 12px;border-radius:8px;border:1px solid rgba(255,255,255,.14);background:rgba(255,255,255,.06);color:var(--text-dim);cursor:pointer;transition:background .15s,border-color .15s}
    .ttf-head-refresh-audience:hover{background:rgba(99,102,241,.12);border-color:rgba(99,102,241,.35);color:var(--text-main)}
    .ttf-head-refresh-audience:disabled{opacity:.55;cursor:not-allowed}
    .ttf-close{width:32px;height:32px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.06);border:none;border-radius:8px;color:var(--text-muted);cursor:pointer;font-size:18px;transition:background .15s}
    .ttf-close:hover{background:rgba(255,255,255,.12)}
    .ttf-body{flex:1;overflow-y:auto;padding:16px 24px}
    .ttf-suggest{padding:12px 16px;border-radius:10px;font-size:13px;margin-bottom:14px;line-height:1.6}
    .ttf-suggest b{font-weight:700}
    .ttf-suggest.suggest-warn{background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.25);color:#fbbf24}
    .ttf-suggest.suggest-info{background:rgba(99,102,241,.08);border:1px solid rgba(99,102,241,.25);color:#a5b4fc}
    .ttf-suggest.suggest-good{background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.25);color:#4ade80}
    .ttf-cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));gap:12px;margin-bottom:16px}
    .ttf-card{background:rgba(255,255,255,.025);border:1.5px solid rgba(255,255,255,.08);border-radius:14px;padding:16px;cursor:pointer;transition:all .2s;position:relative;display:flex;flex-direction:column;gap:6px}
    .ttf-card:hover{border-color:rgba(99,102,241,.4);background:rgba(99,102,241,.04);transform:translateY(-2px);box-shadow:0 8px 24px rgba(0,0,0,.3)}
    .ttf-card.recommended{border-color:rgba(99,102,241,.45);box-shadow:0 0 0 1px rgba(99,102,241,.12)}
    .ttf-card.selected{border-color:rgba(99,102,241,.7);background:rgba(99,102,241,.1);box-shadow:0 0 0 2px rgba(99,102,241,.2)}
    .ttf-card-badge{position:absolute;top:-1px;right:12px;font-size:10px;font-weight:700;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;padding:3px 10px;border-radius:0 0 6px 6px;letter-spacing:.03em}
    .ttf-card-icon{font-size:26px;line-height:1}
    .ttf-card-name{font-size:15px;font-weight:700;color:var(--text-main)}
    .ttf-card-desc{font-size:12px;color:var(--text-muted);line-height:1.5;min-height:36px}
    .ttf-card-meta{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--text-dim);margin-top:auto}
    .ttf-card-steps{display:flex;gap:3px;font-size:14px}
    .ttf-card-time{margin-left:auto;font-size:11px;color:var(--text-muted)}
    .ttf-card-exec{width:100%;padding:8px;font-size:13px;font-weight:600;border-radius:8px;border:1px solid;cursor:pointer;transition:all .15s;text-align:center;margin-top:4px;background:transparent}
    .ttf-card-exec:hover{filter:brightness(1.3);transform:scale(1.02)}
    .ttf-adv-toggle{display:flex;align-items:center;gap:8px;padding:10px 14px;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:10px;cursor:pointer;transition:all .15s;margin-bottom:14px;user-select:none}
    .ttf-adv-toggle:hover{background:rgba(255,255,255,.04);border-color:rgba(255,255,255,.12)}
    .ttf-adv-icon{font-size:12px;transition:transform .25s;display:inline-block}
    .ttf-adv-icon.open{transform:rotate(90deg)}
    .ttf-advanced{display:none;margin-bottom:14px;padding:14px;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:10px}
    .ttf-advanced.open{display:block}
    .ttf-last-run{display:flex;align-items:center;gap:10px;padding:10px 14px;background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:10px;font-size:12px;color:var(--text-muted)}
    .ttf-last-run-replay{padding:5px 12px;font-size:11px;font-weight:600;background:rgba(99,102,241,.12);border:1px solid rgba(99,102,241,.3);border-radius:6px;color:#818cf8;cursor:pointer;transition:all .15s;white-space:nowrap;margin-left:auto;border:1px solid rgba(99,102,241,.3)}
    .ttf-last-run-replay:hover{background:rgba(99,102,241,.25)}
    .tt-flow-presets{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
    .tt-flow-preset{flex:1;min-width:80px;padding:7px 8px;font-size:12px;font-weight:600;border-radius:8px;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);color:var(--text-muted);cursor:pointer;text-align:center;transition:all .15s;white-space:nowrap}
    .tt-flow-preset:hover{background:rgba(99,102,241,.12);border-color:rgba(99,102,241,.3);color:#a5b4fc}
    .tt-flow-preset.active{background:rgba(99,102,241,.2);border-color:var(--accent);color:#c7d2fe}
    .tt-flow-steps{display:flex;flex-direction:column;gap:4px}
    /* 步骤分组 */
    .tts-group{margin-bottom:6px}
    .tts-group-label{font-size:10px;font-weight:700;text-transform:uppercase;letter-spacing:.08em;color:rgba(148,163,184,.4);padding:6px 4px 4px;display:flex;align-items:center;gap:6px}
    .tts-group-label::after{content:'';flex:1;height:1px;background:rgba(255,255,255,.05)}
    /* 步骤卡片 */
    .tts-card{border:1px solid rgba(255,255,255,.07);border-radius:10px;background:rgba(255,255,255,.015);overflow:hidden;transition:all .2s;position:relative}
    .tts-card.enabled{border-color:rgba(99,102,241,.4);background:rgba(99,102,241,.04)}
    .tts-card.enabled::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px;border-radius:10px 0 0 10px}
    .tts-card.enabled.grp-0::before{background:linear-gradient(180deg,#22c55e,#16a34a)}
    .tts-card.enabled.grp-1::before{background:linear-gradient(180deg,#60a5fa,#818cf8)}
    .tts-card.enabled.grp-2::before{background:linear-gradient(180deg,#f59e0b,#ef4444)}
    .tts-card.expanded .tts-params{display:block}
    /* 卡片头部 */
    .tts-head{display:flex;align-items:center;gap:10px;padding:10px 14px 10px 16px;cursor:pointer;user-select:none;transition:background .12s}
    .tts-head:hover{background:rgba(255,255,255,.025)}
    .tts-icon{font-size:20px;width:28px;text-align:center;flex-shrink:0}
    .tts-info{flex:1;min-width:0}
    .tts-name{font-size:14px;font-weight:600;color:var(--text-main);display:flex;align-items:center;gap:6px}
    .tts-desc{font-size:11px;color:var(--text-dim);margin-top:2px;line-height:1.4}
    .tts-time{font-size:13px;font-weight:600;color:var(--text-muted);flex-shrink:0;margin-right:6px;min-width:50px;text-align:right;transition:color .2s}
    .tts-card.enabled .tts-time{color:#f59e0b}
    /* 开关（替代小圆圈） */
    .tts-switch{width:36px;height:20px;border-radius:10px;background:rgba(255,255,255,.1);border:none;cursor:pointer;position:relative;flex-shrink:0;transition:background .2s;padding:0}
    .tts-switch::after{content:'';position:absolute;left:2px;top:2px;width:16px;height:16px;border-radius:50%;background:rgba(255,255,255,.3);transition:all .2s}
    .tts-card.enabled .tts-switch{background:var(--accent)}
    .tts-card.enabled .tts-switch::after{left:18px;background:#fff}
    /* 展开箭头 */
    .tts-chevron{font-size:10px;color:rgba(148,163,184,.4);flex-shrink:0;transition:transform .2s;margin-left:2px}
    .tts-card.expanded .tts-chevron{transform:rotate(90deg);color:var(--text-muted)}
    /* 参数面板 */
    .tts-params{display:none;padding:4px 16px 14px 16px;border-top:1px solid rgba(255,255,255,.04)}
    .tts-param-row{display:flex;align-items:center;gap:10px;margin-bottom:8px;min-height:30px}
    .tts-param-row:last-child{margin-bottom:0}
    .tts-param-label{font-size:12px;color:var(--text-muted);width:78px;flex-shrink:0;font-weight:500}
    /* 滑块+数值组合 */
    .tts-slider-wrap{flex:1;display:flex;align-items:center;gap:10px}
    .tts-slider{-webkit-appearance:none;appearance:none;flex:1;height:6px;border-radius:3px;background:rgba(255,255,255,.08);outline:none;cursor:pointer;transition:background .2s}
    .tts-slider::-webkit-slider-thumb{-webkit-appearance:none;width:18px;height:18px;border-radius:50%;background:var(--accent);border:2px solid rgba(255,255,255,.2);cursor:pointer;transition:box-shadow .15s}
    .tts-slider::-webkit-slider-thumb:hover{box-shadow:0 0 0 4px rgba(99,102,241,.25)}
    .tts-slider::-moz-range-thumb{width:18px;height:18px;border-radius:50%;background:var(--accent);border:2px solid rgba(255,255,255,.2);cursor:pointer}
    .tts-val{min-width:42px;text-align:center;font-size:14px;font-weight:700;color:var(--text-main);font-variant-numeric:tabular-nums}
    .tts-val-unit{font-size:10px;font-weight:400;color:var(--text-dim);margin-left:1px}
    .tts-range-hint{display:flex;justify-content:space-between;font-size:9px;color:rgba(148,163,184,.3);margin-top:-4px;padding:0 2px}
    /* toggle 开关（参数 checkbox） */
    .tts-toggle-wrap{flex:1;display:flex;align-items:center;gap:8px}
    .tts-toggle{width:34px;height:18px;border-radius:9px;background:rgba(255,255,255,.1);border:none;cursor:pointer;position:relative;flex-shrink:0;transition:background .2s;padding:0}
    .tts-toggle::after{content:'';position:absolute;left:2px;top:2px;width:14px;height:14px;border-radius:50%;background:rgba(255,255,255,.3);transition:all .2s}
    .tts-toggle.on{background:#22c55e}
    .tts-toggle.on::after{left:18px;background:#fff}
    .tts-toggle-label{font-size:12px;color:var(--text-muted)}
    /* 风险/影响标记 */
    .tts-risk{font-size:10px;padding:2px 6px;border-radius:4px;font-weight:600;white-space:nowrap;flex-shrink:0}
    .tts-risk.safe{background:rgba(34,197,94,.08);color:#4ade80;border:1px solid rgba(34,197,94,.2)}
    .tts-risk.warn{background:rgba(245,158,11,.08);color:#fbbf24;border:1px solid rgba(245,158,11,.25)}
    .tts-risk.danger{background:rgba(239,68,68,.08);color:#f87171;border:1px solid rgba(239,68,68,.25)}
    .tts-impact{font-size:10px;color:rgba(148,163,184,.5);margin-top:0;padding-left:88px}
    .ttf-footer{padding:12px 24px 16px;border-top:1px solid rgba(255,255,255,.07);flex-shrink:0}
    .tt-flow-est{font-size:13px;color:var(--text-muted);margin-bottom:10px;text-align:center;min-height:18px}
    .tt-flow-est b{color:#f59e0b}
    .ttf-btns{display:flex;gap:8px}
    .tt-flow-save{padding:9px 16px;font-size:12px;border-radius:8px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);color:var(--text-muted);cursor:pointer;transition:all .15s;white-space:nowrap}
    .tt-flow-save:hover{background:rgba(255,255,255,.09);color:var(--text-main)}
    .tt-flow-sync{padding:9px 14px;font-size:12px;border-radius:8px;background:rgba(16,185,129,.07);border:1px solid rgba(16,185,129,.25);color:#34d399;cursor:pointer;transition:all .15s;white-space:nowrap}
    .tt-flow-sync:hover{background:rgba(16,185,129,.15);border-color:rgba(16,185,129,.5)}
    .tt-flow-sync:disabled{opacity:.4;cursor:not-allowed}
    .tt-flow-exec{flex:1;padding:10px;font-size:14px;font-weight:700;border-radius:8px;background:rgba(99,102,241,.22);border:1px solid rgba(99,102,241,.5);color:#a5b4fc;cursor:pointer;transition:all .15s}
    .tt-flow-exec:hover{background:rgba(99,102,241,.38)}
    .tt-flow-exec:disabled{opacity:.45;cursor:not-allowed}
    /* ── P1: Device Role Badge ── */
    .tt-role-badge{display:inline-block;font-size:9px;padding:1px 6px;border-radius:4px;font-weight:600;margin-left:4px;vertical-align:middle}
    .tt-role-badge.warmup{background:rgba(34,197,94,.12);color:#22c55e;border:1px solid rgba(34,197,94,.28)}
    .tt-role-badge.follow{background:rgba(59,130,246,.12);color:#60a5fa;border:1px solid rgba(59,130,246,.28)}
    .tt-role-badge.full{background:rgba(99,102,241,.12);color:#a5b4fc;border:1px solid rgba(99,102,241,.28)}
    .tt-role-badge.idle{background:rgba(148,163,184,.08);color:#94a3b8;border:1px solid rgba(148,163,184,.2)}
    /* ── P2: Step Progress in Action Result ── */
    .tt-flow-progress{margin-top:8px;font-size:11px}
    .tt-flow-progress-step{display:flex;align-items:center;gap:6px;padding:3px 0;color:var(--text-muted)}
    .tt-flow-progress-step.done{color:#22c55e}
    .tt-flow-progress-step.running{color:#f59e0b}
    .tt-flow-progress-step.fail{color:#f87171}
    /* ── P2: 实时进度追踪面板 ── */
    .tt-prog-panel{margin-top:8px;background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:8px 10px;font-size:11px}
    .tt-prog-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;color:var(--text-muted)}
    .tt-prog-title{font-size:10px;font-weight:600;text-transform:uppercase;letter-spacing:.05em}
    .tt-prog-cancel{font-size:10px;padding:2px 8px;border:1px solid rgba(239,68,68,.3);border-radius:4px;background:rgba(239,68,68,.08);color:#f87171;cursor:pointer;transition:all .15s}
    .tt-prog-cancel:hover{background:rgba(239,68,68,.2)}
    .tt-prog-step{display:flex;align-items:center;gap:7px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)}
    .tt-prog-step:last-child{border-bottom:none}
    .tt-prog-icon{width:14px;text-align:center;font-size:11px;flex-shrink:0}
    .tt-prog-name{flex:1;color:var(--text-muted)}
    .tt-prog-status{font-size:10px;flex-shrink:0}
    .tt-prog-step.st-pending .tt-prog-icon{color:#475569}
    .tt-prog-step.st-pending .tt-prog-status{color:#475569}
    .tt-prog-step.st-running .tt-prog-icon{color:#f59e0b;animation:tt-spin .8s linear infinite}
    .tt-prog-step.st-running .tt-prog-name{color:var(--text-main)}
    .tt-prog-step.st-running .tt-prog-status{color:#f59e0b}
    .tt-prog-step.st-completed .tt-prog-icon{color:#22c55e}
    .tt-prog-step.st-completed .tt-prog-name{color:var(--text-main)}
    .tt-prog-step.st-completed .tt-prog-status{color:#22c55e}
    .tt-prog-step.st-failed .tt-prog-icon{color:#f87171}
    .tt-prog-step.st-failed .tt-prog-status{color:#f87171}
    @keyframes tt-spin{to{transform:rotate(360deg)}}
    .tt-prog-bar-wrap{margin-top:6px;height:2px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden}
    .tt-prog-bar{height:100%;background:linear-gradient(90deg,var(--accent),#22c55e);border-radius:2px;transition:width .4s ease}
    /* ── P3: 设备卡片流程角色标识 ── */
    .tt-dev-role-strip{display:flex;gap:3px;margin-top:5px;padding-top:5px;border-top:1px solid rgba(255,255,255,.05)}
    .tt-dev-role-icon{font-size:11px;opacity:.55;transition:opacity .15s}
    .tt-dev-role-icon.active{opacity:1}
    .tt-dev-role-label{font-size:9px;color:var(--text-muted);margin-left:auto;font-weight:500}
    /* ── P4-2: 设备卡片 GEO 统计条 ── */
    .tt-dev-geo-bar{display:flex;align-items:center;gap:3px;margin-top:3px;font-size:11px;flex-wrap:wrap}
    .tt-dev-geo-bar b{font-size:9px;color:var(--text-muted);font-weight:600;margin-left:1px}
    /* ── P0新增: 关键词搜索/直播互动步骤高亮 ── */
    .tt-step-kwsearch .tt-step-icon,.tt-step-live .tt-step-icon{filter:drop-shadow(0 0 4px rgba(245,158,11,.6))}
    /* ── P4: 执行历史面板 ── */
    .tt-hist-section{margin-bottom:16px}
    .tt-hist-row{display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04);cursor:pointer;transition:background .12s;border-radius:4px}
    .tt-hist-row:last-child{border-bottom:none}
    .tt-hist-row:hover{background:rgba(99,102,241,.07);padding-left:4px}
    .tt-hist-time{font-size:10px;color:var(--text-muted);flex-shrink:0;min-width:50px}
    .tt-hist-steps{display:flex;gap:2px;flex-shrink:0}
    .tt-hist-result{font-size:10px;color:var(--text-muted);flex:1;text-align:right;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
    .tt-hist-replay{font-size:9px;padding:1px 6px;border:1px solid rgba(99,102,241,.3);border-radius:4px;color:#818cf8;flex-shrink:0;opacity:0;transition:opacity .15s}
    .tt-hist-row:hover .tt-hist-replay{opacity:1}
    .tt-hist-empty{font-size:11px;color:var(--text-muted);text-align:center;padding:8px 0;font-style:italic}
    /* ── GEO: 单选市场选择器 v2 ── */
    .tt-geo-section{padding:0 0 14px;flex-shrink:0;margin-bottom:2px}
    .ttg-empty-btn{display:flex;align-items:center;justify-content:center;gap:8px;width:100%;padding:14px;font-size:14px;font-weight:600;color:var(--text-muted);background:rgba(255,255,255,.02);border:1.5px dashed rgba(99,102,241,.3);border-radius:12px;cursor:pointer;transition:all .15s}
    .ttg-empty-btn:hover{border-color:rgba(99,102,241,.6);color:#a5b4fc;background:rgba(99,102,241,.04)}
    .ttg-selected{display:flex;align-items:center;gap:12px;padding:12px 16px;background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.25);border-radius:12px;margin-bottom:10px}
    .ttg-sel-flag{font-size:32px;line-height:1}
    .ttg-sel-info{flex:1;min-width:0}
    .ttg-sel-name{font-size:16px;font-weight:700;color:var(--text-main)}
    .ttg-sel-meta{font-size:12px;color:var(--text-muted);margin-top:2px;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
    .ttg-sel-actions{display:flex;gap:6px;flex-shrink:0}
    .ttg-sel-btn{padding:5px 12px;font-size:12px;border-radius:6px;cursor:pointer;transition:all .15s;border:1px solid rgba(255,255,255,.1);background:rgba(255,255,255,.04);color:var(--text-muted)}
    .ttg-sel-btn:hover{background:rgba(255,255,255,.08);color:var(--text-main)}
    .ttg-sel-btn.danger{border-color:rgba(248,113,113,.25);color:#f87171}
    .ttg-sel-btn.danger:hover{background:rgba(248,113,113,.1)}
    .ttg-langs{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin-bottom:10px;padding:0 4px}
    .ttg-lang-label{font-size:12px;font-weight:600;color:var(--text-dim)}
    .ttg-lang-chip{display:inline-flex;align-items:center;gap:5px;padding:4px 12px;border-radius:16px;font-size:12px;cursor:pointer;transition:all .15s;user-select:none;border:1px solid}
    .ttg-lang-chip.primary{background:rgba(34,197,94,.1);border-color:rgba(34,197,94,.35);color:#4ade80}
    .ttg-lang-chip.optional{background:rgba(255,255,255,.02);border-color:rgba(255,255,255,.08);color:var(--text-muted)}
    .ttg-lang-chip.optional:hover{border-color:rgba(99,102,241,.3);color:#a5b4fc}
    .ttg-lang-chip.optional.active{background:rgba(99,102,241,.1);border-color:rgba(99,102,241,.4);color:#a5b4fc}
    .ttg-picker{display:none;background:rgba(7,13,26,.97);border:1px solid rgba(99,102,241,.25);border-radius:12px;padding:12px;animation:tt-flow-in .15s ease;margin-bottom:10px}
    .ttg-picker.open{display:block}
    .ttg-search{width:100%;padding:8px 12px;font-size:13px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.12);border-radius:8px;color:var(--text-main);outline:none;box-sizing:border-box;margin-bottom:10px}
    .ttg-search:focus{border-color:rgba(99,102,241,.5)}
    .ttg-search::placeholder{color:rgba(148,163,184,.4)}
    .ttg-section-label{font-size:11px;font-weight:700;color:rgba(148,163,184,.5);text-transform:uppercase;letter-spacing:.06em;margin-bottom:6px;display:block}
    .ttg-recent-list{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px}
    .ttg-country-btn{padding:5px 12px;font-size:13px;border-radius:10px;border:1px solid rgba(255,255,255,.07);background:rgba(255,255,255,.02);cursor:pointer;transition:all .12s;color:var(--text-muted);user-select:none;white-space:nowrap}
    .ttg-country-btn:hover{background:rgba(99,102,241,.12);border-color:rgba(99,102,241,.35);color:#a5b4fc}
    .ttg-country-btn.sel{background:rgba(99,102,241,.22);border-color:rgba(99,102,241,.6);color:#c7d2fe}
    .ttg-region-block{margin-bottom:10px}
    .ttg-region-block:last-child{margin-bottom:0}
    .ttg-region-name{font-size:11px;color:rgba(148,163,184,.45);text-transform:uppercase;letter-spacing:.06em;margin-bottom:5px;font-weight:700}
    .ttg-region-grid{display:flex;flex-wrap:wrap;gap:5px}
    .ttg-picker-scroll{max-height:220px;overflow-y:auto;padding-right:4px}
    .ttg-picker-scroll::-webkit-scrollbar{width:4px}
    .ttg-picker-scroll::-webkit-scrollbar-thumb{background:rgba(255,255,255,.1);border-radius:2px}
  `;
  document.head.appendChild(s);
}

// loadTtOpsPanel 由 tiktok-ops.js 提供（避免与 overview 重复声明导致整段脚本解析失败）

// ════════════════════════════════════════════════════════
// P4 — Toast 通知系统
// ════════════════════════════════════════════════════════
function _ttEnsureToastContainer() {
  if (!document.getElementById('tt-toast-container')) {
    const c = document.createElement('div');
    c.id = 'tt-toast-container';
    document.body.appendChild(c);
  }
}

function _ttShowToast(msg, type, duration) {
  _ttEnsureToastContainer();
  const c = document.getElementById('tt-toast-container');
  const t = document.createElement('div');
  t.className = 'tt-toast ' + (type || 'info');
  t.textContent = msg;
  c.appendChild(t);
  const d = duration || 4000;
  setTimeout(function() {
    t.style.animation = 'tt-toast-out .3s ease forwards';
    setTimeout(function() { t.remove(); }, 320);
  }, d);
}

// ════════════════════════════════════════════════════════
// P4 — 自动刷新（90秒静默后台刷新）
// ════════════════════════════════════════════════════════
let _ttAutoRefreshTimer = null;
let _ttLastRefreshTs = 0;

function _ttStartAutoRefresh() {
  if (_ttAutoRefreshTimer) clearInterval(_ttAutoRefreshTimer);
  _ttAutoRefreshTimer = setInterval(_ttSilentRefresh, 90000);
  _ttStartSSE();
}

var _ttSSE = null;
function _ttStartSSE() {
  if (_ttSSE) return;
  try {
    _ttSSE = new EventSource('/events/stream');
    _ttSSE.onmessage = function(e) {
      try {
        var ev = JSON.parse(e.data);
        var t = ev.type || '';
        // 设备状态变化 → 刷新网格
        if (t.startsWith('device.') || t.startsWith('task.')) {
          _ttHandleSSEEvent(ev);
        }
      } catch(x) {}
    };
    _ttSSE.onerror = function() {
      _ttSSE.close(); _ttSSE = null;
      setTimeout(_ttStartSSE, 10000);
    };
  } catch(e) {}
}

function _ttHandleSSEEvent(ev) {
  var t = ev.type;
  var did = ev.device_id || '';
  var data = ev.data || {};

  if (t === 'device.online' || t === 'device.offline') {
    // 更新单张卡片状态指示
    var card = document.querySelector('.tt-dev-card[data-did="' + did + '"]');
    if (card) {
      var dot = card.querySelector('.tt-dev-dot');
      if (dot) dot.style.background = (t === 'device.online') ? '#22c55e' : '#ef4444';
      if (t === 'device.offline') card.classList.add('offline');
      else card.classList.remove('offline');
    }
  }

  if (t === 'task.progress') {
    var pct = data.progress || 0;
    var safeId = did.replace(/[^a-zA-Z0-9]/g, '_');
    var prog = document.getElementById('tt-act-result-' + safeId);
    if (prog) prog.innerHTML = '<span style="color:#818cf8">\u{23F3} ' + (data.step || '') + ' ' + pct + '%</span>';
  }

  if (t === 'task.completed' || t === 'task.failed') {
    _ttDebounceRefresh();
  }

  if (t === 'device.alert') {
    _toast((data.message || '\u8BBE\u5907\u544A\u8B66: ' + did), 'error');
  }
}

var _ttRefreshDebounce = null;
function _ttDebounceRefresh() {
  if (_ttRefreshDebounce) clearTimeout(_ttRefreshDebounce);
  _ttRefreshDebounce = setTimeout(function() { _loadTtDeviceGrid(); }, 2000);
}

function _ttStopAutoRefresh() {
  if (_ttAutoRefreshTimer) { clearInterval(_ttAutoRefreshTimer); _ttAutoRefreshTimer = null; }
}

async function _ttSilentRefresh() {
  // 如果有弹窗/面板打开，跳过
  const panel = document.getElementById('tt-dev-panel');
  if (panel && panel.classList.contains('open')) return;
  if (document.getElementById('tt-pitch-overlay')) return;

  // 更新刷新指示点
  const dot = document.getElementById('tt-refresh-dot');
  if (dot) dot.className = 'loading';

  try {
    const ctrl = new AbortController();
    setTimeout(function() { ctrl.abort(); }, 7000);
    const resp = await fetch('/tiktok/device-grid', { signal: ctrl.signal, credentials: 'include' });
    if (!resp.ok) { if (dot) dot.className = 'stale'; return; }
    const data = await resp.json();
    const devices = data.devices || [];

    // 比较变化
    const prev = window._ttDevicesCache || {};
    const newLeadsDevices = [];
    const newOnlineDevices = [];
    const newRepliedDevices = [];   // N3: 有新回复消息的设备
    devices.forEach(function(dev) {
      const p = prev[dev.device_id];
      if (p) {
        if ((dev.leads_count || 0) > (p.leads_count || 0)) newLeadsDevices.push(dev.alias || dev.device_id);
        if (dev.online && !p.online) newOnlineDevices.push(dev.alias || dev.device_id);
        // N3: replied_count 增加 = 有新入站消息
        if ((dev.replied_count || 0) > (p.replied_count || 0)) {
          newRepliedDevices.push({ alias: dev.alias || dev.device_id, device_id: dev.device_id });
        }
      }
    });

    // 通知
    if (newLeadsDevices.length > 0) {
      _ttShowToast('🔥 新线索！' + newLeadsDevices.join('、') + ' 有新线索', 'warning', 5000);
    }
    if (newOnlineDevices.length > 0) {
      _ttShowToast('📱 ' + newOnlineDevices.join('、') + ' 已上线', 'success', 3000);
    }
    // N3: 有新回复消息 — 高优先级 toast + 自动刷新线索面板
    if (newRepliedDevices.length > 0) {
      var repliedNames = newRepliedDevices.map(function(d) { return d.alias; }).join('、');
      _ttShowToast('📨 ' + repliedNames + ' 有新回复消息！', 'warning', 6000);
      // 如果线索面板当前打开且对应该设备，自动刷新并切到"有回复"标签
      var leadsPanel = document.getElementById('tt-leads-panel');
      if (leadsPanel && leadsPanel.classList.contains('open')) {
        var openDevId = leadsPanel.dataset.deviceId || '';
        var match = newRepliedDevices.some(function(d) { return d.device_id === openDevId; });
        if (match) {
          // 切到有回复标签
          var repliedTab = document.querySelector('.tt-lp-tab[data-f="replied"]');
          if (repliedTab) _ttLpSetFilter(repliedTab);
          // 刷新线索
          setTimeout(function() { _ttRefreshLeads(openDevId, null); }, 500);
        }
      }
    }

    // 更新缓存
    const newCache = {};
    devices.forEach(function(d) { newCache[d.device_id] = d; });
    window._ttDevicesCache = newCache;
    window._ttLeadsCache = data.leads || [];

    // 静默重渲网格（带变化高亮）
    _ttSilentRenderGrid(devices, prev, data.summary || {});

    if (dot) { dot.className = ''; }
    _ttLastRefreshTs = Date.now();
  } catch(e) {
    if (dot) { dot.className = 'stale'; }
  }
}

function _ttSilentRenderGrid(devices, prev, summary) {
  const grid = document.getElementById('tt-device-grid');
  if (!grid) return;

  // 更新汇总 pills
  const _gs = function(id, v) { const e = document.getElementById(id); if (e) e.textContent = v; };
  _gs('tt-gs-online', summary.online !== undefined ? summary.online : devices.filter(function(d) { return d.online; }).length);
  _gs('tt-gs-leads', summary.hot_leads !== undefined ? summary.hot_leads : 0);
  _gs('tt-gs-cfg', summary.configured !== undefined ? summary.configured : devices.filter(function(d) { return d.configured; }).length);

  // 排序
  devices.sort(function(a, b) {
    const pri = function(d) {
      if ((d.leads_count || 0) > 0 && d.online) return 3;
      const act = (d.sessions_today || 0) + (d.today_dms || 0) + (d.today_followed || 0);
      if (d.online && act > 0) return 2;
      if (d.online) return 1;
      return 0;
    };
    return pri(b) - pri(a);
  });

  // 更新横幅
  const totalLeads = devices.reduce(function(s, d) { return s + (d.leads_count || 0); }, 0);
  const leadsDevCount = devices.filter(function(d) { return (d.leads_count || 0) > 0 && d.online; }).length;
  let banner = document.getElementById('tt-priority-banner');
  if (totalLeads > 0) {
    if (!banner) {
      banner = document.createElement('div');
      banner.id = 'tt-priority-banner';
      grid.parentNode.insertBefore(banner, grid);
    }
    banner.style.display = 'flex';
    banner.innerHTML =
      '<span style="font-size:16px;flex-shrink:0">🔥</span>' +
      '<div style="flex:1;min-width:0">' +
        '<span style="font-size:12px;color:var(--text-main);font-weight:600">' + leadsDevCount + ' 台设备有线索</span>' +
        '<span style="font-size:11px;color:var(--text-muted);margin-left:6px">共 ' + totalLeads + ' 条待处理</span>' +
      '</div>' +
      '<button onclick="_ttBatchPitchAll(this)" style="padding:6px 14px;font-size:12px;font-weight:600;border-radius:6px;background:rgba(245,158,11,.2);border:1px solid rgba(245,158,11,.4);color:#f59e0b;cursor:pointer;white-space:nowrap;flex-shrink:0">💰 批量发话术</button>';
  } else if (banner) {
    banner.style.display = 'none';
  }

  // 重渲卡片（标记新线索设备）
  grid.innerHTML = devices.map(function(dev) {
    const p = prev[dev.device_id];
    const hasNewLeads = p && (dev.leads_count || 0) > (p.leads_count || 0);
    return _ttRenderCard(dev, hasNewLeads);
  }).join('');
}

// ════════════════════════════════════════════════════════
// P5 — 话术预览弹窗（发送前可编辑确认）
// ════════════════════════════════════════════════════════
let _ttPitchModalResolve = null;

function _ttShowPitchModal(leadName, previewText, charHint) {
  return new Promise(function(resolve) {
    _ttPitchModalResolve = resolve;
    const ov = document.createElement('div');
    ov.id = 'tt-pitch-overlay';
    ov.innerHTML =
      '<div id="tt-pitch-modal">' +
        '<div class="tt-pitch-modal-head">' +
          '<div style="font-size:13px;font-weight:700;color:var(--text-main)">📝 发话术前确认</div>' +
          '<div style="font-size:11px;color:var(--text-muted);margin-top:3px">发送给：<b style="color:#a5b4fc">' + _escHtml(leadName) + '</b></div>' +
        '</div>' +
        '<div class="tt-pitch-modal-body">' +
          '<div style="font-size:10px;color:var(--text-muted);margin-bottom:6px;display:flex;justify-content:space-between">' +
            '<span>话术内容（可编辑）</span>' +
            '<span id="tt-pitch-char">0 字</span>' +
          '</div>' +
          '<textarea class="tt-pitch-textarea" id="tt-pitch-text" oninput="_ttPitchCharCount()" placeholder="输入话术内容...">' + _escHtml(previewText) + '</textarea>' +
          ((!charHint || charHint.configured === false)
            ? '<div style="font-size:10px;color:#f59e0b;margin-top:6px">⚠ 未配置 TG/WA，话术中无联系方式</div>'
            : '') +
        '</div>' +
        '<div class="tt-pitch-modal-foot">' +
          '<button class="tt-pitch-cancel" onclick="_ttClosePitchModal(false)">取消</button>' +
          ((charHint && charHint.deviceId)
            ? '<button id="tt-ai-opt-btn" style="padding:8px 14px;font-size:12px;border-radius:7px;background:rgba(139,92,246,.2);border:1px solid rgba(139,92,246,.4);color:#c4b5fd;cursor:pointer;display:flex;align-items:center;gap:5px" onclick="_ttPitchAiOptimize()">&#10024; AI优化</button>'
            : '') +
          '<button class="tt-pitch-confirm" onclick="_ttClosePitchModal(true)">确认发送</button>' +
        '</div>' +
      '</div>';
    // 存储 deviceId/username 到弹窗元素供 AI 优化使用
    if (charHint && charHint.deviceId) {
      ov.dataset.deviceId = charHint.deviceId;
      ov.dataset.username = charHint.username || '';
    }
    document.body.appendChild(ov);
    // 点背景关闭
    ov.addEventListener('click', function(e) { if (e.target === ov) _ttClosePitchModal(false); });
    // 更新字数
    setTimeout(_ttPitchCharCount, 50);
  });
}

async function _ttPitchAiOptimize() {
  const ov = document.getElementById('tt-pitch-overlay');
  const btn = document.getElementById('tt-ai-opt-btn');
  if (!ov || !btn) return;
  const deviceId = ov.dataset.deviceId;
  const username = ov.dataset.username || '';
  if (!deviceId) return;

  btn.disabled = true;
  btn.innerHTML = '&#10024; 生成中...';

  try {
    const pr = await fetch(
      '/tiktok/device/' + encodeURIComponent(deviceId) +
      '/pitch/ai-preview?username=' + encodeURIComponent(username),
      { credentials: 'include' }
    );
    if (pr.ok) {
      const data = await pr.json();
      if (data.ok && data.preview) {
        const ta = document.getElementById('tt-pitch-text');
        if (ta) { ta.value = data.preview; _ttPitchCharCount(); }
        _ttShowToast('AI话术已生成', 'success', 2500);
      } else {
        _ttShowToast('AI生成失败', 'error', 2500);
      }
    } else {
      _ttShowToast('请求失败', 'error', 2500);
    }
  } catch(e) {
    _ttShowToast('AI生成出错: ' + (e.message || e), 'error', 3000);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '&#10024; AI优化';
  }
}

function _ttPitchCharCount() {
  const ta = document.getElementById('tt-pitch-text');
  const ct = document.getElementById('tt-pitch-char');
  if (ta && ct) ct.textContent = ta.value.length + ' 字';
}

function _ttClosePitchModal(confirmed) {
  const ov = document.getElementById('tt-pitch-overlay');
  const text = confirmed ? (document.getElementById('tt-pitch-text') || {}).value : null;
  if (ov) { ov.style.animation = 'tt-toast-out .2s ease forwards'; setTimeout(function() { ov.remove(); }, 220); }
  if (_ttPitchModalResolve) { _ttPitchModalResolve(confirmed ? (text || '') : null); _ttPitchModalResolve = null; }
}

let _ttGridLoading = false;
async function _loadTtDeviceGrid() {
  if (_ttGridLoading) return;
  _ttGridLoading = true;
  const grid = document.getElementById('tt-device-grid');
  if (!grid) { _ttGridLoading = false; return; }

  grid.innerHTML = Array(6).fill(0).map(() =>
    '<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:12px;min-height:110px">' +
    '<div style="height:12px;background:rgba(255,255,255,.06);border-radius:4px;margin-bottom:8px;width:60%"></div>' +
    '<div style="height:8px;background:rgba(255,255,255,.04);border-radius:4px;margin-bottom:6px;width:80%"></div>' +
    '<div style="height:8px;background:rgba(255,255,255,.04);border-radius:4px;width:50%"></div></div>'
  ).join('');

  try {
    const ctrl = new AbortController();
    const timer = setTimeout(() => ctrl.abort(), 8000);
    let resp;
    try {
      resp = await fetch('/tiktok/device-grid', { signal: ctrl.signal, credentials: 'include' });
      clearTimeout(timer);
    } catch (fe) {
      clearTimeout(timer);
      const msg = fe.name === 'AbortError' ? '请求超时，点击 ⟳ 重试' : fe.message;
      grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:#f87171;padding:24px;font-size:13px">' + msg + '</div>';
      _ttGridLoading = false;
      return;
    }
    if (!resp.ok) {
      grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:#f87171;padding:24px;font-size:13px">HTTP ' + resp.status + '</div>';
      _ttGridLoading = false;
      return;
    }
    const data = await resp.json();
    const devices = data.devices || [];
    const summary = data.summary || {};

    const _gs = (id, v) => { const e = document.getElementById(id); if (e) e.textContent = v; };
    _gs('tt-gs-online', summary.online !== undefined ? summary.online : devices.filter(d => d.online).length);
    _gs('tt-gs-leads', summary.hot_leads !== undefined ? summary.hot_leads : (data.leads || []).length);
    _gs('tt-gs-cfg', summary.configured !== undefined ? summary.configured : devices.filter(d => d.configured).length);

    if (!devices.length) {
      grid.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:var(--text-muted);padding:32px;font-size:13px">未发现设备</div>';
      _ttGridLoading = false;
      return;
    }

    window._ttDevicesCache = {};
    devices.forEach(d => { window._ttDevicesCache[d.device_id] = d; });
    window._ttLeadsCache = data.leads || [];

    // P1: 按优先级排序（有线索 > 今日活跃 > 在线 > 离线）
    devices.sort(function(a, b) {
      const pri = function(d) {
        if ((d.leads_count || 0) > 0 && d.online) return 3;
        const act = (d.sessions_today || 0) + (d.today_dms || 0) + (d.today_followed || 0);
        if (d.online && act > 0) return 2;
        if (d.online) return 1;
        return 0;
      };
      return pri(b) - pri(a);
    });

    // P1: 优先处理横幅
    const totalLeads = devices.reduce(function(s, d) { return s + (d.leads_count || 0); }, 0);
    const leadsDevCount = devices.filter(function(d) { return (d.leads_count || 0) > 0 && d.online; }).length;
    let banner = document.getElementById('tt-priority-banner');
    if (totalLeads > 0) {
      if (!banner) {
        banner = document.createElement('div');
        banner.id = 'tt-priority-banner';
        grid.parentNode.insertBefore(banner, grid);
      }
      banner.style.display = 'flex';
      banner.innerHTML =
        '<span style="font-size:16px;flex-shrink:0">🔥</span>' +
        '<div style="flex:1;min-width:0">' +
          '<span style="font-size:12px;color:var(--text-main);font-weight:600">' + leadsDevCount + ' 台设备有线索</span>' +
          '<span style="font-size:11px;color:var(--text-muted);margin-left:6px">共 ' + totalLeads + ' 条待处理</span>' +
        '</div>' +
        '<button onclick="_ttBatchPitchAll(this)" style="padding:6px 14px;font-size:12px;font-weight:600;' +
          'border-radius:6px;background:rgba(245,158,11,.2);border:1px solid rgba(245,158,11,.4);' +
          'color:#f59e0b;cursor:pointer;white-space:nowrap;flex-shrink:0">💰 批量发话术</button>';
    } else if (banner) {
      banner.style.display = 'none';
    }

    grid.innerHTML = devices.map(dev => _ttRenderCard(dev)).join('');
    _ttLoadCardCronStatus();
  } catch (e) {
    const grid2 = document.getElementById('tt-device-grid');
    if (grid2) grid2.innerHTML = '<div style="grid-column:1/-1;text-align:center;color:#f87171;padding:24px;font-size:13px">\u5F02\u5E38: ' + e.message + '</div>';
  }
  _ttGridLoading = false;
}

/* 设备卡片 cron 状态加载 */
async function _ttLoadCardCronStatus() {
  try {
    var r = await fetch('/tiktok/cron-status', {credentials:'include'});
    if (!r.ok) return;
    var d = await r.json();
    var jobs = (d.jobs || []).filter(function(j){return j.enabled;});
    if (!jobs.length) return;
    jobs.sort(function(a,b){return (a.next_run_secs||9999)-(b.next_run_secs||9999);});
    var nearest = jobs[0];
    var secs = nearest.next_run_secs || 0;
    var label = secs > 3600 ? Math.round(secs/3600)+'h' : secs > 60 ? Math.round(secs/60)+'m' : secs+'s';
    // 填充到所有在线设备卡片
    document.querySelectorAll('.tt-card-cron').forEach(function(el) {
      if (secs > 0) {
        var color = secs < 120 ? '#22c55e' : secs < 600 ? '#818cf8' : '#64748b';
        el.innerHTML = '<span style="color:' + color + '">\u23F0 ' + label + '</span>';
      }
    });
    // 启动倒计时
    if (window._ttCardCronTimer) clearInterval(window._ttCardCronTimer);
    window._ttCardCronTimer = setInterval(function() {
      secs--;
      if (secs <= 0) { clearInterval(window._ttCardCronTimer); return; }
      var l2 = secs > 3600 ? Math.round(secs/3600)+'h' : secs > 60 ? Math.round(secs/60)+'m' : secs+'s';
      var c2 = secs < 120 ? '#22c55e' : secs < 600 ? '#818cf8' : '#64748b';
      document.querySelectorAll('.tt-card-cron').forEach(function(el) {
        el.innerHTML = '<span style="color:' + c2 + '">\u23F0 ' + l2 + '</span>';
      });
    }, 1000);
  } catch(e) {}
}

/* 全检按钮 — 触发后台预检并刷新设备卡片就绪状态 */
async function _ttCheckAllReadiness() {
  const bar = document.getElementById('tt-readiness-summary');
  if (bar) bar.innerHTML = '<span style="color:var(--text-muted)">🔍 检测中，请稍候...</span>';
  try {
    const r = await fetch('/preflight/devices?quick=0', { credentials: 'include' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const ready = data.ready_count || 0;
    const bNet  = data.blocked_network || 0;
    const bVpn  = data.blocked_vpn || 0;
    const bAcc  = data.blocked_account || 0;
    const offline = data.offline_count || 0;
    let html = `<span style="color:#22c55e;font-weight:600">✅ 就绪 ${ready}台</span>`;
    if (bNet)  html += `&nbsp;&nbsp;<span style="color:#ef4444">❌ 无网络 ${bNet}台</span>`;
    if (bVpn)  html += `&nbsp;&nbsp;<span style="color:#f59e0b">⚠️ 无VPN ${bVpn}台</span>`;
    if (bAcc)  html += `&nbsp;&nbsp;<span style="color:#f59e0b">⚠️ 账号异常 ${bAcc}台</span>`;
    if (offline) html += `&nbsp;&nbsp;<span style="color:var(--text-muted)">○ 离线 ${offline}台</span>`;
    if (bar) bar.innerHTML = html;
    // 刷新卡片（此时缓存已更新，新卡片会显示就绪状态行）
    setTimeout(() => _loadTtDeviceGrid(), 400);
  } catch(e) {
    if (bar) bar.innerHTML = '<span style="color:#f87171">检测失败: ' + e.message + '</span>';
  }
}

function _ttRenderCard(dev, isNew) {
  const alias = dev.alias || dev.device_id.substring(0, 8);
  const online = dev.online;
  const did = dev.device_id;
  const nodeLabel = dev.host || (dev.node === 'worker03' ? 'W03' : '主控');
  const nodeColor = _getWorkerColor(nodeLabel);
  const nodeBg = _hexToRgba(nodeColor, .12);
  const phaseMap = { cold_start: '冷启动', interest_building: '建兴趣', active: '活跃', recovery: '恢复' };
  const phaseLabel = phaseMap[dev.phase] || dev.phase || '-';
  const leadsCount = dev.leads_count || 0;
  const repliedCount = dev.replied_count || 0;
  const algoScore = dev.algo_score || 0;
  const hasActivity = (dev.sessions_today || 0) + (dev.today_watched || 0) +
                      (dev.today_followed || 0) + (dev.today_dms || 0) > 0;

  // 卡片状态（左侧彩条）— replied优先于普通leads
  let state = 'offline';
  if (online && repliedCount > 0) state = 'replied';   // 有回复 = 最高优先
  else if (online && leadsCount > 0) state = 'leads';
  else if (online && hasActivity)  state = 'active';
  else if (online)                  state = 'idle';

  const dotColor = online ? '#22c55e' : '#ef4444';
  const algoColor = algoScore >= 60 ? '#22c55e' : algoScore >= 30 ? '#f59e0b' : '#94a3b8';
  const algoWidth = Math.min(algoScore, 100);

  // 红色线索徽章 + 橙色回复徽章
  const badge = leadsCount > 0
    ? '<div class="tt-dev-badge">' + leadsCount + '</div>' : '';
  const repliedBadge = repliedCount > 0
    ? '<div class="tt-dev-badge" style="background:#f59e0b;right:auto;left:6px;top:6px" title="' + repliedCount + '条有回复线索">&#128276; ' + repliedCount + '</div>'
    : '';

  const statItems = [
    { icon: '💬', v: dev.sessions_today },
    { icon: '▶',  v: dev.today_watched  },
    { icon: '👤', v: dev.today_followed },
    { icon: '📨', v: dev.today_dms      },
  ];
  const statRow = '<div class="tt-dev-stat-row">' +
    statItems.map(function(s) {
      const v = (s.v !== undefined && s.v !== null) ? s.v : 0;
      return '<span style="' + (v === 0 ? 'opacity:.35' : '') + '">' + s.icon + '<b>' + v + '</b></span>';
    }).join('') + '</div>';

  const algoBar = '<div class="tt-dev-algo-row">' +
    '<div style="display:flex;justify-content:space-between;align-items:center">' +
      '<span style="font-size:9px;color:var(--text-muted)">⚡ 算法分</span>' +
      '<span style="font-size:10px;font-weight:700;color:' + algoColor + '">' + (algoScore || '-') + '</span>' +
    '</div>' +
    '<div class="tt-dev-algo-bar"><div class="tt-dev-algo-fill" style="width:' + algoWidth + '%;background:' + algoColor + '"></div></div>' +
  '</div>';

  const quickRow = '<div class="tt-card-quick" data-did="' + String(did).replace(/"/g, '&quot;') + '" onclick="_ttCardQuick(event)">' +
    '<button type="button" class="tt-card-qbtn" data-act="inbox" ' + (online ? '' : 'disabled ') +
      'title="' + (online ? '仅本机：检查收件箱并 AI 回复' : '设备离线') + '">本机收件</button>' +
    '<button type="button" class="tt-card-qbtn" data-act="flow" ' + (online ? '' : 'disabled ') +
      'title="' + (online ? '打开该设备流程配置' : '设备离线') + '">流程</button>' +
    '</div>';

  var safeIdCard = did.replace(/[^a-zA-Z0-9]/g, '_');
  const foot = '<div style="font-size:9px;color:var(--text-muted);display:flex;justify-content:space-between;align-items:center;margin-top:2px">' +
    '<span>' + phaseLabel + ' \u00B7 \u7B2C' + (dev.day !== undefined ? dev.day : '-') + '\u5929</span>' +
    '<span id="tt-card-cron-' + safeIdCard + '" class="tt-card-cron"></span>' +
    (dev.configured
      ? '<span style="color:#22c55e;font-weight:600">\u2713 \u5DF2\u914D\u7F6E</span>'
      : '<span style="color:#f59e0b">\u672A\u914D\u7F6E</span>') +
  '</div>';

  // P6: 健康状态细化（在线但今日零活动 = 橙色警告）
  if (online && !hasActivity && state === 'idle') state = 'warn-idle';

  const extraClass = isNew ? ' new-leads' : '';

  // P3: 流程角色标识条（基于 localStorage 配置）
  const roleStrip = _ttGetRoleStrip(did);

  const geoBar = _ttGetGeoBar(did);

  return '<div class="tt-dev-card state-' + state + (online ? '' : ' offline') + extraClass +
    '" data-did="' + _escHtml(did) + '" onclick="_ttCardClick(this.dataset.did)">' +
    badge + repliedBadge +
    '<div class="tt-dev-card-head">' +
      '<span class="tt-dev-dot" style="background:' + dotColor + '"></span>' +
      '<span class="tt-dev-alias">' + _escHtml(alias) + '</span>' +
      '<span class="tt-dev-node" style="color:' + nodeColor + ';background:' + nodeBg + '">' + nodeLabel + '</span>' +
    '</div>' +
    statRow + algoBar + quickRow + foot + roleStrip + geoBar +
    (dev.readiness ? _ttReadinessRow(dev.readiness) : '') +
  '</div>';
}

function _ttReadinessRow(r) {
  if (!r) return '';
  const icon = (ok) => ok ? '<span style="color:#22c55e">✅</span>' : '<span style="color:#ef4444">❌</span>';
  const passed = r.passed;
  const rowColor = passed ? 'rgba(34,197,94,.08)' : 'rgba(239,68,68,.08)';
  const borderColor = passed ? 'rgba(34,197,94,.2)' : 'rgba(239,68,68,.2)';
  const statusText = passed ? '就绪' : (r.blocked_step === 'network' ? '无网络' : r.blocked_step === 'vpn' ? '无VPN' : '需处理');
  return '<div style="margin-top:5px;padding:3px 6px;border-radius:4px;background:' + rowColor + ';border:1px solid ' + borderColor + ';font-size:9px;display:flex;gap:6px;align-items:center">' +
    icon(r.network_ok) + '网' +
    icon(r.vpn_ok) + 'VPN' +
    icon(r.account_ok) + '账号' +
    '<span style="margin-left:auto;font-weight:600;color:' + (passed ? '#22c55e' : '#ef4444') + '">' + statusText + '</span>' +
  '</div>';
}

let _ttPanelDevId = null;
function _ttOpenPanel(deviceId) {
  _ttPanelDevId = deviceId;
  const panel = document.getElementById('tt-dev-panel');
  const overlay = document.getElementById('tt-dev-overlay');
  const contentEl = document.getElementById('tt-dev-panel-content');
  if (!panel || !contentEl) return;
  const dev = (window._ttDevicesCache || {})[deviceId] || {};
  contentEl.innerHTML = _ttBuildPanel(dev, deviceId);
  panel.classList.add('open');
  if (overlay) overlay.style.display = 'block';
  document.body.style.overflow = 'hidden';
  _ttLoadCronNext(deviceId);
  // 异步加载通讯录计数
  var safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  _ttPanelCtLoadCount(deviceId, safeId);
}

async function _ttPanelCtLoadCount(deviceId, safeId) {
  var info = document.getElementById('ppnl-ct-' + safeId);
  if (!info) return;
  try {
    var r = await api('GET', '/devices/' + encodeURIComponent(deviceId) + '/contacts/enriched');
    var s = r.stats || {};
    var total = s.total || 0;
    var matched = s.matched || 0;
    var greeted = s.greeted || 0;
    if (total === 0) { info.textContent = '0'; return; }
    info.innerHTML = total + ' <span style="font-size:9px;color:#64748b">(\u2713' + matched + ' \u{1F44B}' + greeted + ')</span>';
  } catch(e) {
    try {
      var r2 = await api('GET', '/devices/' + encodeURIComponent(deviceId) + '/contacts?limit=500');
      var n2 = (r2.contacts || []).length;
      info.innerHTML = n2 + ' \u8054\u7CFB\u4EBA <span style="font-size:9px;color:#64748b"><span style="opacity:.8" title="\u589E\u5F3A\u6570\u636E\u672A\u52A0\u8F7D\uFF0C\u4EC5\u663E\u793A\u6570\u91CF">\u00B7 \u6807\u51C6</span></span>';
    } catch(e2) { info.textContent = '-'; }
  }
}

// 加载并显示该设备下次定时任务时间
async function _ttLoadCronNext(deviceId) {
  const safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  const el = document.getElementById('tt-cron-next-' + safeId);
  if (!el) return;
  try {
    const r = await fetch('/tiktok/cron-status', {credentials:'include'}).then(function(x){return x.json();});
    const jobs = (r.jobs || []).filter(function(j){return j.enabled && j.next_run;});
    if (!jobs.length) { el.textContent = ''; return; }
    // 找最近的下次运行
    jobs.sort(function(a,b){return new Date(a.next_run)-new Date(b.next_run);});
    const next = jobs[0];
    const mins = Math.round((new Date(next.next_run)-Date.now())/60000);
    if (mins < 0) { el.textContent = ''; return; }
    const label = mins < 60 ? mins+'分钟后自动运行' : Math.round(mins/60)+'小时后自动运行';
    el.textContent = '⏱ ' + label;
  } catch(e) { el.textContent = ''; }
}

function _ttCloseDevicePanel() {
  const panel = document.getElementById('tt-dev-panel');
  const overlay = document.getElementById('tt-dev-overlay');
  if (panel) { panel.classList.remove('open'); panel.classList.remove('shifted'); }
  if (overlay) overlay.style.display = 'none';
  document.body.style.overflow = '';
  _ttPanelDevId = null;
  // 同时收起线索面板
  const lp = document.getElementById('tt-leads-panel');
  if (lp) lp.classList.remove('open');
}

function _ttBuildPanel(dev, deviceId) {
  const alias = dev.alias || deviceId.substring(0, 8);
  const online = dev.online;
  const dotColor = online ? '#22c55e' : '#ef4444';
  const phaseMap = { cold_start: '冷启动', interest_building: '建兴趣', active: '活跃', recovery: '恢复' };
  const phaseLabel = phaseMap[dev.phase] || dev.phase || '-';
  const safeIdAttr = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  const indId = 'ppnl-ind-' + safeIdAttr;
  // 通用联系方式（支持任意 app）
  const _allContacts = dev.contacts || { telegram: dev.telegram || '', whatsapp: dev.whatsapp || '' };
  const _appMeta = {
    telegram: {label:'TG', color:'#60a5fa', placeholder:'@username'},
    whatsapp: {label:'WA', color:'#22c55e', placeholder:'+639...'},
    instagram: {label:'IG', color:'#f472b6', placeholder:'@username'},
    line:      {label:'Line', color:'#4ade80', placeholder:'@username'},
    wechat:    {label:'WeChat', color:'#22c55e', placeholder:'ID/username'},
    viber:     {label:'Viber', color:'#8b5cf6', placeholder:'+xx...'},
    signal:    {label:'Signal', color:'#64748b', placeholder:'+xx...'},
    facebook:  {label:'FB', color:'#3b82f6', placeholder:'username'},
    tiktok:    {label:'TikTok', color:'#ef4444', placeholder:'@username'},
  };
  // 始终显示 telegram/whatsapp（即使为空），其余 app 只在有值时显示
  const _baseApps = ['telegram', 'whatsapp'];
  const _extraApps = Object.keys(_allContacts).filter(function(a) { return !_baseApps.includes(a) && _allContacts[a]; });
  const _orderedApps = _baseApps.concat(_extraApps);
  let _refRows = '';
  _orderedApps.forEach(function(app) {
    const meta = _appMeta[app] || {label: app.charAt(0).toUpperCase()+app.slice(1), color:'#94a3b8', placeholder:'contact'};
    const val = (_allContacts[app] || '').replace(/"/g, '&quot;');
    const isBase = _baseApps.includes(app);
    _refRows += '<div class="tt-ref-row" id="ref-row-' + safeIdAttr + '-' + app + '">' +
      '<span class="tt-ref-label" style="color:' + meta.color + ';min-width:48px">' + meta.label + '</span>' +
      '<input class="tt-ref-input" data-did="' + deviceId + '" data-field="' + app + '" data-ind="' + indId + '" type="text" value="' + val + '" placeholder="' + meta.placeholder + '"' +
        ' onblur="_ttSaveRef(this.dataset.did,this.dataset.field,this.value,this,this.dataset.ind)"' +
        ' onkeydown="if(event.key===\'Enter\')this.blur()">' +
      (!isBase ? '<button onclick="_ttDelContact(\'' + deviceId + '\',\'' + app + '\',\'' + safeIdAttr + '\',\'' + indId + '\')" style="background:none;border:none;cursor:pointer;color:#ef4444;padding:0 4px;font-size:16px;line-height:1" title="删除此联系方式">×</button>' : '') +
    '</div>';
  });
  const algoColor = dev.algo_score >= 60 ? '#22c55e' : dev.algo_score >= 30 ? '#f59e0b' : '#94a3b8';
  const algoTip = dev.algo_score >= 60 ? '优秀' : dev.algo_score >= 30 ? '正常' : dev.algo_score > 0 ? '偏低' : '未知';

  // 判断今日是否有任何活动
  const hasActivity = (dev.sessions_today || 0) > 0 || (dev.today_watched || 0) > 0 ||
                      (dev.today_followed || 0) > 0 || (dev.today_dms || 0) > 0;

  // 智能建议：根据设备实际状态给出最优下一步
  let suggestion = '';
  if (!online) {
    suggestion = '<div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.25);border-radius:6px;padding:8px 10px;font-size:11px;color:#f87171;margin-bottom:12px">⚠ 设备离线，请检查 ADB 连接</div>';
  } else if (!hasActivity) {
    suggestion = '<div style="background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.25);border-radius:6px;padding:8px 10px;font-size:11px;color:#818cf8;margin-bottom:12px">📋 今日尚未启动 — 点下方「▶ 启动流程」开始工作</div>';
  } else if (!dev.configured) {
    suggestion = '<div style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.25);border-radius:6px;padding:8px 10px;font-size:11px;color:#f59e0b;margin-bottom:12px">💡 未配置引流账号，AI回复无法自动附加联系方式 — 请在下方填入 TG/WA/IG 等账号</div>';
  } else if ((dev.leads_count || 0) > 0) {
    suggestion = '<div style="background:rgba(34,197,94,.1);border:1px solid rgba(34,197,94,.25);border-radius:6px;padding:8px 10px;font-size:11px;color:#22c55e;margin-bottom:12px">🔥 有 ' + dev.leads_count + ' 条线索待转化 — 建议优先「💰 发话术」</div>';
  }

  // 统计格子（可点击展开明细）
  function statBox(label, value, statKey, icon) {
    const v = (value !== undefined && value !== null) ? value : '-';
    const isZero = v === 0 || v === '0';
    return '<div class="tt-panel-stat tt-stat-clickable" data-stat="' + statKey +
      '" data-devid="' + safeIdAttr + '" onclick="_ttStatDetail(this.dataset.stat,this.dataset.devid,this)" title="点击查看明细">' +
      '<b style="' + (isZero ? 'color:var(--text-muted)' : '') + '">' + v + '</b>' +
      '<span>' + icon + ' ' + label + '</span>' +
    '</div>';
  }

  const onlineAttr = online ? '' : ' disabled title="设备离线"';

  // 推荐操作（突出显示最应该点的那个）
  let launchStyle = 'tt-panel-action-btn';
  let pitchStyle  = 'tt-panel-action-btn';
  if (!hasActivity && online) launchStyle += ' recommended';
  else if ((dev.leads_count || 0) > 0 && online) pitchStyle += ' recommended';

  // 线索计数
  var leadsCount = dev.leads_count || 0;
  var leadsColor = leadsCount > 0 ? '#f59e0b' : 'var(--text-muted)';

  return '<div class="ttp-header">' +
      '<span class="ttp-dot" style="background:' + dotColor + '"></span>' +
      '<div class="ttp-hinfo">' +
        '<div class="ttp-name">' + _escHtml(alias) + '</div>' +
        '<div class="ttp-meta">' + phaseLabel + ' \u00B7 Day ' + (dev.day || '-') + ' \u00B7 ' +
          (online ? '<span style="color:#22c55e">Online</span>' : '<span style="color:#ef4444">Offline</span>') +
        '</div>' +
      '</div>' +
      '<button class="ttp-close" onclick="_ttCloseDevicePanel()">\u2715</button>' +
    '</div>' +

    '<div class="ttp-body">' +
      suggestion +

      '<div class="ttp-zone">' +
        '<div class="ttp-zone-title">\u26A1 \u5355\u673A\u5FEB\u6377</div>' +
        '<div class="ttp-quick-actions">' +
          '<button type="button" class="ttp-qa-btn" onclick="ttCheckInboxForDevice(' + JSON.stringify(deviceId) + ')"' + onlineAttr + ' title="仅该设备执行检查收件箱">' +
            '\u{1F4E5} \u672C\u673A\u6536\u4EF6\u7BB1</button>' +
          '<button type="button" class="ttp-qa-btn" onclick="_ttOpenFlowConfigEntry(' + JSON.stringify(deviceId) + ')"' + onlineAttr + ' title="该设备的自动化步骤">' +
            '\u2699\uFE0F \u914D\u7F6E\u6D41\u7A0B</button>' +
        '</div>' +
      '</div>' +

      // ── ZONE 1: 今日数据 (紧凑条形)
      '<div class="ttp-zone">' +
        '<div class="ttp-stats">' +
          '<div class="ttp-st"><b style="color:#60a5fa">' + (dev.sessions_today || 0) + '</b><span>\u{1F4AC} \u4F1A\u8BDD</span></div>' +
          '<div class="ttp-st"><b style="color:#818cf8">' + (dev.today_watched || 0) + '</b><span>\u25B6 \u89C6\u9891</span></div>' +
          '<div class="ttp-st"><b style="color:#22c55e">' + (dev.today_followed || 0) + '</b><span>\u{1F464} \u5173\u6CE8</span></div>' +
          '<div class="ttp-st"><b style="color:#f472b6">' + (dev.today_dms || 0) + '</b><span>\u{1F4E8} \u79C1\u4FE1</span></div>' +
          '<div class="ttp-st"><b style="color:' + algoColor + '">' + (dev.algo_score || '-') + '</b><span>\u26A1 \u7B97\u6CD5</span></div>' +
          '<div class="ttp-st"><b style="color:' + leadsColor + '">' + leadsCount + '</b><span>\u2B50 \u7EBF\u7D22</span></div>' +
        '</div>' +
      '</div>' +

      // ── ZONE 2: 核心操作 (只留配置流程, 其他整合到弹窗)
      '<div class="ttp-zone">' +
        '<button class="ttp-primary-btn" onclick="_ttOpenFlowConfigEntry(_ttPanelDevId)"' + onlineAttr + '>' +
          '\u2699\uFE0F \u914D\u7F6E\u6267\u884C\u6D41\u7A0B' +
          (online ? '<span id="tt-cron-next-' + safeIdAttr + '" class="ttp-cron"></span>' : '') +
        '</button>' +
        '<div class="ttp-action-grid">' +
          '<button class="ttp-action-card" onclick="_ttOpenContactsModal(\'' + deviceId + '\')">' +
            '<span class="ttp-ac-icon">\u{1F4DE}</span>' +
            '<span class="ttp-ac-label">\u901A\u8BAF\u5F55</span>' +
            '<span class="ttp-ac-count" id="ppnl-ct-' + safeIdAttr + '">-</span>' +
          '</button>' +
          '<button class="ttp-action-card" onclick="_ttOpenAnalysisModal(\'' + deviceId + '\')">' +
            '<span class="ttp-ac-icon">\u{1F9E0}</span>' +
            '<span class="ttp-ac-label">\u667A\u80FD\u5206\u6790</span>' +
            '<span class="ttp-ac-count" style="color:' + leadsColor + '">' + leadsCount + ' \u7EBF\u7D22</span>' +
          '</button>' +
        '</div>' +
        '<div id="tt-act-result-' + safeIdAttr + '" style="font-size:11px;min-height:1px;margin-top:4px;color:var(--text-muted)"></div>' +
        '<div id="tt-prog-panel-' + safeIdAttr + '" class="tt-prog-panel" style="display:none"></div>' +
      '</div>' +

      // ── ZONE 3: 引流账号 (简洁)
      '<div class="ttp-zone">' +
        '<div class="ttp-zone-title">\u{1F517} \u5F15\u6D41\u8D26\u53F7' +
          (dev.configured ? ' <span style="color:#22c55e;font-size:10px">\u2713</span>' : '') +
        '</div>' +
        _refRows +
        '<div id="ref-add-' + safeIdAttr + '" style="margin-top:6px">' +
          '<button onclick="_ttShowAddContact(\'' + deviceId + '\',\'' + safeIdAttr + '\',\'' + indId + '\')" ' +
            'class="ttp-small-btn" style="color:#818cf8;border-color:rgba(99,102,241,.3)">+ \u6DFB\u52A0</button>' +
          ' <button onclick="_ttApplyContactsAll(\'' + deviceId + '\',\'' + indId + '\')" ' +
            'class="ttp-small-btn" style="color:#f59e0b;border-color:rgba(245,158,11,.3)">\u2197 \u540C\u6B65\u6240\u6709</button>' +
        '</div>' +
        '<div style="font-size:10px;min-height:1px;margin-top:4px;color:var(--text-muted)" id="' + indId + '"></div>' +
      '</div>' +

      // ── ZONE 标签
      '<div class="ttp-zone">' +
        '<div class="ttp-zone-title">\u{1F3F7}\uFE0F \u6807\u7B7E</div>' +
        '<div id="ttp-tags-' + safeIdAttr + '" style="font-size:11px;color:var(--text-muted)">\u52A0\u8F7D\u4E2D...</div>' +
        '<div style="display:flex;gap:6px;margin-top:6px">' +
          '<input id="ttp-tag-inp-' + safeIdAttr + '" type="text" placeholder="GEO / \u9636\u6BB5 / \u81EA\u5B9A\u4E49" ' +
            'style="flex:1;padding:6px 8px;border-radius:6px;border:1px solid rgba(255,255,255,.1);background:rgba(0,0,0,.2);color:var(--text-main);font-size:11px" />' +
          '<button class="ttp-small-btn" style="color:#818cf8" onclick="_ttSaveDeviceTags(\'' + deviceId + '\',\'' + safeIdAttr + '\')">+\u6DFB\u52A0</button>' +
        '</div>' +
      '</div>' +

      // ── ZONE 4: 设备健康度
      '<div class="ttp-zone" id="ttp-health-' + safeIdAttr + '">' +
        '<div class="ttp-zone-title">\u{1F3E5} \u8BBE\u5907\u5065\u5EB7\u5EA6</div>' +
        '<div id="ttp-health-body-' + safeIdAttr + '" style="font-size:11px;color:var(--text-muted);padding:4px 0">\u52A0\u8F7D\u4E2D...</div>' +
      '</div>' +

      // ── ZONE 5: 历史
      _ttBuildHistorySection(deviceId, safeIdAttr) +

    '</div>';

  setTimeout(function() {
    _ttLoadHealthPanel(deviceId, safeIdAttr);
    _ttLoadDeviceTagsPanel(deviceId, safeIdAttr);
  }, 100);
}

async function _ttLoadDeviceTagsPanel(deviceId, safeId) {
  var el = document.getElementById('ttp-tags-' + safeId);
  if (!el) return;
  try {
    var r = await api('GET', '/devices/' + encodeURIComponent(deviceId) + '/tags');
    var tags = r.tags || [];
    el.innerHTML = tags.length
      ? tags.map(function(t) {
          return '<span class="ttp-dev-tag">' + _escHtml(t) + '</span>';
        }).join(' ')
      : '<span style="opacity:.6">\u6682\u65E0\u6807\u7B7E</span>';
    window['_ttpTags_' + safeId] = tags.slice();
  } catch(e) {
    var em = String((e && e.message) || e || '');
    if (em.indexOf('\u8BBE\u5907\u4E0D\u5B58\u5728') >= 0) {
      el.innerHTML = '<span style="color:#f87171;font-size:11px;line-height:1.4">\u672A\u5728\u53EF\u8FBE Worker \u4E0A\u5B9A\u4F4D\u8BE5\u673A\uFF08\u8282\u70B9\u79BB\u7EBF\u6216\u672A\u8F6C\u53D1\uFF09\uFF0C\u6807\u7B7E\u6682\u4E0D\u53EF\u7528\u3002</span>';
    } else {
      el.innerHTML = '<span style="color:#ef4444">\u8BFB\u53D6\u5931\u8D25</span>';
    }
  }
}

async function _ttSaveDeviceTags(deviceId, safeId) {
  var inp = document.getElementById('ttp-tag-inp-' + safeId);
  if (!inp || !inp.value.trim()) return;
  var cur = window['_ttpTags_' + safeId] || [];
  var next = cur.concat([inp.value.trim()]);
  inp.value = '';
  try {
    await api('PUT', '/devices/' + encodeURIComponent(deviceId) + '/tags', {tags: next});
    _toast('\u6807\u7B7E\u5DF2\u4FDD\u5B58', 'success');
    _ttLoadDeviceTagsPanel(deviceId, safeId);
    _loadTtDeviceGrid();
  } catch(e) {
    _toast(e.message, 'error');
  }
}

// ── 设备健康度面板 ──────────────────────────────────────────────
async function _ttLoadHealthPanel(deviceId, safeId) {
  var el = document.getElementById('ttp-health-body-' + safeId);
  if (!el) return;
  try {
    var d = await api('GET', '/devices/' + encodeURIComponent(deviceId) + '/health-score');
    var total = d.total || 0;
    var tc = total >= 70 ? '#22c55e' : total >= 40 ? '#f59e0b' : '#ef4444';
    var dims = [
      {key:'stability',     label:'\u7A33\u5B9A\u6027', color:'#60a5fa'},
      {key:'responsiveness', label:'\u54CD\u5E94\u901F\u5EA6', color:'#818cf8'},
      {key:'task_success',   label:'\u4EFB\u52A1\u6210\u529F', color:'#22c55e'},
      {key:'u2_health',      label:'U2\u5065\u5EB7',   color:'#f59e0b'},
    ];
    var html = '<div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">' +
      '<div class="conv-quality-ring" style="--q-color:' + tc + ';--q-pct:' + total + ';width:48px;height:48px;font-size:16px">' +
        '<span style="color:' + tc + '">' + total + '</span>' +
      '</div>' +
      '<div style="flex:1">';
    dims.forEach(function(dm) {
      var v = d[dm.key] || 0;
      html += '<div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">' +
        '<span style="font-size:10px;color:#94a3b8;min-width:56px">' + dm.label + '</span>' +
        '<div style="flex:1;height:4px;background:rgba(255,255,255,.06);border-radius:2px;overflow:hidden">' +
          '<div style="height:100%;width:' + v + '%;background:' + dm.color + ';border-radius:2px;transition:width .4s"></div>' +
        '</div>' +
        '<span style="font-size:10px;color:' + dm.color + ';min-width:24px;text-align:right">' + v + '</span>' +
      '</div>';
    });
    html += '</div></div>';
    if (d.disconnects_24h > 0) html += '<div style="font-size:10px;color:#f59e0b;margin-top:2px">\u26A0 24h\u5185\u65AD\u8FDE ' + d.disconnects_24h + ' \u6B21</div>';
    if (d.latency_ms > 500) html += '<div style="font-size:10px;color:#ef4444;margin-top:2px">\u26A0 ADB\u5EF6\u8FDF ' + d.latency_ms + 'ms</div>';
    // 低于阈值时显示修复按钮
    if (total < 60) {
      html += '<div style="margin-top:6px;display:flex;gap:6px">' +
        '<button class="ttp-small-btn" style="color:#f59e0b;border-color:rgba(245,158,11,.3);flex:1" ' +
          'onclick="_ttAutoRecover(\'' + deviceId + '\',\'' + safeId + '\')">\u{1F527} \u4E00\u952E\u4FEE\u590D</button>' +
        '<button class="ttp-small-btn" style="color:#818cf8;border-color:rgba(99,102,241,.3)" ' +
          'onclick="_ttLoadHealthPanel(\'' + deviceId + '\',\'' + safeId + '\')">\u21BB</button>' +
      '</div>';
    }
    html += '<div id="ttp-recover-log-' + safeId + '"></div>';
    el.innerHTML = html;
  } catch(e) {
    var em = String((e && e.message) || e || '');
    if (em.indexOf('\u8BBE\u5907\u4E0D\u5B58\u5728') >= 0) {
      el.innerHTML = '<span style="color:#f87171;font-size:11px;line-height:1.4">\u672A\u5728\u53EF\u8FBE Worker \u4E0A\u5B9A\u4F4D\u8BE5\u673A\uFF08\u8282\u70B9\u79BB\u7EBF\u6216\u672A\u8F6C\u53D1\uFF09\uFF0C\u5065\u5EB7\u5EA6\u6682\u4E0D\u53EF\u7528\u3002</span>';
    } else {
      el.innerHTML = '<span style="color:#ef4444">\u52A0\u8F7D\u5931\u8D25</span>';
    }
  }
}

async function _ttAutoRecover(deviceId, safeId) {
  var log = document.getElementById('ttp-recover-log-' + safeId);
  if (!log) return;
  log.innerHTML = '<div style="margin-top:6px;font-size:11px;color:#818cf8">\u{1F527} \u6B63\u5728\u8BCA\u65AD\u4E0E\u4FEE\u590D...</div>';
  try {
    var d = await api('POST', '/devices/' + encodeURIComponent(deviceId) + '/auto-recover', {});
    var html = '<div style="margin-top:6px;display:grid;gap:3px">';
    (d.steps || []).forEach(function(s) {
      var icon = s.ok ? '\u2705' : '\u274C';
      var color = s.ok ? '#22c55e' : '#ef4444';
      html += '<div style="font-size:10px;color:' + color + ';display:flex;align-items:center;gap:4px">' +
        '<span>' + icon + '</span><span>' + _escHtml(s.detail || s.step) + '</span></div>';
    });
    html += '</div>';
    html += '<div style="font-size:11px;margin-top:4px;font-weight:600;color:' +
      (d.ok ? '#22c55e' : '#f59e0b') + '">' +
      (d.ok ? '\u2705 \u5168\u90E8\u901A\u8FC7' : '\u26A0 ' + d.passed_steps + '/' + d.total_steps + ' \u901A\u8FC7') +
    '</div>';
    log.innerHTML = html;
    // 修复后刷新健康评分
    setTimeout(function() { _ttLoadHealthPanel(deviceId, safeId); }, 2000);
  } catch(e) {
    log.innerHTML = '<div style="margin-top:4px;font-size:11px;color:#ef4444">\u274C ' + e.message + '</div>';
  }
}

// ── 线索独立页面 ──────────────────────────────────────────────
// ── 线索侧边面板（从设备面板左侧滑出）─────────────────────────
let _ttLeadsFilter = 'all';

function _ttOpenLeadsPage(deviceId) {
  const dev = (window._ttDevicesCache || {})[deviceId] || {};
  const alias = dev.alias || deviceId.substring(0, 8);
  const leadsCount = dev.leads_count || 0;

  // 确保面板 DOM 存在
  let panel = document.getElementById('tt-leads-panel');
  if (!panel) {
    panel = document.createElement('div');
    panel.id = 'tt-leads-panel';
    document.body.appendChild(panel);
  }

  // 重置过滤器
  _ttLeadsFilter = 'all';

  panel.innerHTML =
    // 头部
    '<div class="tt-lp-header">' +
      '<button class="tt-lp-back" onclick="_ttCloseLeadsPage()" title="返回">‹</button>' +
      '<div style="flex:1;min-width:0;margin:0 4px">' +
        '<div style="font-size:14px;font-weight:700;color:var(--text-main);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' +
          _escHtml(alias) +
        '</div>' +
        '<div style="font-size:10px;color:var(--text-muted);margin-top:1px">设备线索 · <span id="tt-lp-count">' + leadsCount + '</span> 条 &nbsp;<span id="tt-lp-cron" style="color:rgba(255,255,255,.3)"></span></div>' +
      '</div>' +
      '<button data-devid="' + _escHtml(deviceId) + '" onclick="_ttRefreshLeads(this.dataset.devid,this)" ' +
        'style="width:30px;height:30px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:7px;color:var(--text-muted);cursor:pointer;font-size:14px;flex-shrink:0">⟳</button>' +
    '</div>' +
    // 搜索栏
    '<div style="padding:8px 12px 0">' +
      '<input id="tt-lp-search" type="text" placeholder="🔍 搜索姓名..." ' +
        'style="width:100%;box-sizing:border-box;padding:6px 10px;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:7px;color:var(--text-main);font-size:12px;outline:none" ' +
        'oninput="_ttLpSearch(this.value)">' +
    '</div>' +
    // 过滤 tab
    '<div class="tt-lp-filter">' +
      '<button class="tt-lp-tab active" data-f="all" data-devid="' + _escHtml(deviceId) + '" onclick="_ttLpSetFilter(this)">全部</button>' +
      '<button class="tt-lp-tab" data-f="unpitched" data-devid="' + _escHtml(deviceId) + '" onclick="_ttLpSetFilter(this)">未发送</button>' +
      '<button class="tt-lp-tab" data-f="qualified" data-devid="' + _escHtml(deviceId) + '" onclick="_ttLpSetFilter(this)">已合格</button>' +
      '<button class="tt-lp-tab" data-f="hot" data-devid="' + _escHtml(deviceId) + '" onclick="_ttLpSetFilter(this)">高分</button>' +
      '<button class="tt-lp-tab" data-f="replied" data-devid="' + _escHtml(deviceId) + '" onclick="_ttLpSetFilter(this)">&#128276; 有回复</button>' +
    '</div>' +
    // 快速统计栏（加载后动态填充）
    '<div id="tt-lp-stats" style="padding:4px 12px;font-size:10px;color:var(--text-muted);display:flex;gap:12px;border-bottom:1px solid rgba(255,255,255,.05)"></div>' +
    // 线索列表
    '<div class="tt-lp-body" id="tt-lp-body">' +
      '<div style="color:var(--text-muted);font-size:12px;padding:30px;text-align:center">加载中...</div>' +
    '</div>' +
    // 底部批量操作
    '<div class="tt-lp-footer">' +
      '<button class="tt-lp-batch" data-devid="' + _escHtml(deviceId) + '" onclick="_ttLpBatchPitch(this.dataset.devid,this)">💰 批量发话术（未发送）</button>' +
    '</div>';

  // 设备面板右移
  const devPanel = document.getElementById('tt-dev-panel');
  if (devPanel) devPanel.classList.add('shifted');

  // 滑入，记录当前设备 ID 供 N3 检测用
  panel.classList.add('open');
  panel.dataset.deviceId = deviceId;

  _ttLoadLeadsPanel(deviceId);
}

function _ttCloseLeadsPage() {
  const panel = document.getElementById('tt-leads-panel');
  if (panel) panel.classList.remove('open');
  const devPanel = document.getElementById('tt-dev-panel');
  if (devPanel) devPanel.classList.remove('shifted');
}

function _ttLpSetFilter(btn) {
  document.querySelectorAll('.tt-lp-tab').forEach(function(t) { t.classList.remove('active'); });
  btn.classList.add('active');
  _ttLeadsFilter = btn.dataset.f;
  _ttLeadsSearch = '';  // 切换 filter 时清空搜索
  _ttLeadsSearchOrig = null;  // 清除搜索缓存备份
  clearTimeout(_ttLpSearchTimer);
  var searchEl = document.getElementById('tt-lp-search');
  if (searchEl) searchEl.value = '';
  const deviceId = btn.dataset.devid;
  const cached = window._ttLeadsRaw || [];
  _ttRenderLeadsPanel(document.getElementById('tt-lp-body'), cached, deviceId);
}

let _ttLeadsSearch = '';
var _ttLpSearchTimer = null;
var _ttLeadsSearchOrig = null;  // 搜索前的原始缓存

function _ttLpSearch(q) {
  _ttLeadsSearch = (q || '').toLowerCase().trim();
  clearTimeout(_ttLpSearchTimer);
  const devId = (document.querySelector('.tt-lp-tab') || {dataset: {}}).dataset.devid || '';

  // 清空搜索：恢复原始缓存并重新渲染
  if (!_ttLeadsSearch) {
    if (_ttLeadsSearchOrig !== null) {
      window._ttLeadsRaw = _ttLeadsSearchOrig;
      _ttLeadsSearchOrig = null;
      _ttLeadsTotalFromApi = window._ttLeadsRaw.length;
    }
    _ttRenderLeadsPanel(document.getElementById('tt-lp-body'), window._ttLeadsRaw || [], devId);
    return;
  }

  // 立即客户端过滤（快速视觉反馈）
  _ttRenderLeadsPanel(document.getElementById('tt-lp-body'), window._ttLeadsRaw || [], devId);

  // 防抖服务端搜索（≥2字符，覆盖未在首页的线索）
  if (_ttLeadsSearch.length >= 2) {
    _ttLpSearchTimer = setTimeout(async function() {
      const curQ = _ttLeadsSearch;
      if (!curQ) return;
      try {
        const r = await fetch('/tiktok/device/' + encodeURIComponent(devId) +
          '/leads?limit=200&offset=0&q=' + encodeURIComponent(curQ), {credentials: 'include'});
        if (!r.ok || curQ !== _ttLeadsSearch) return;
        const d = await r.json();
        if (_ttLeadsSearchOrig === null) _ttLeadsSearchOrig = window._ttLeadsRaw || [];
        window._ttLeadsRaw = d.leads || [];
        _ttLeadsTotalFromApi = d.total || window._ttLeadsRaw.length;
        _ttRenderLeadsPanel(document.getElementById('tt-lp-body'), window._ttLeadsRaw, devId);
      } catch(e) {}
    }, 400);
  }
}

function _ttRefreshLeads(deviceId, btn) {
  if (btn) { btn.style.transform = 'rotate(360deg)'; btn.style.transition = 'transform .5s'; }
  setTimeout(function() { if (btn) btn.style.transform = ''; }, 500);
  _ttLoadLeadsPanel(deviceId);
}

var _ttLeadsPageSize = 50;  // 每页加载数量
var _ttLeadsOffset = 0;     // 当前分页偏移
var _ttLeadsTotalFromApi = 0;  // API 返回的总数

async function _ttLoadLeadsPanel(deviceId, append) {
  const body = document.getElementById('tt-lp-body');
  if (!body) return;
  if (!append) {
    _ttLeadsOffset = 0;
    body.innerHTML = '<div style="color:var(--text-muted);font-size:12px;padding:30px;text-align:center"><div style="font-size:20px;margin-bottom:8px;animation:spin 1s linear infinite">⟳</div>加载中...</div>';
  }
  try {
    const url = '/tiktok/device/' + encodeURIComponent(deviceId) + '/leads?limit=' + _ttLeadsPageSize + '&offset=' + _ttLeadsOffset;
    const r = await fetch(url, { credentials: 'include' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    var newLeads = d.leads || [];
    _ttLeadsTotalFromApi = d.total || newLeads.length;
    if (append) {
      window._ttLeadsRaw = (window._ttLeadsRaw || []).concat(newLeads);
    } else {
      window._ttLeadsRaw = newLeads;
    }
    _ttLeadsOffset += newLeads.length;
    // 更新 count
    const countEl = document.getElementById('tt-lp-count');
    if (countEl) countEl.textContent = _ttLeadsTotalFromApi;
    // 自动切换到"有回复"标签（如果有回复消息且当前是全部视图，且是第一次加载）
    if (!append) {
      var hasReplied = window._ttLeadsRaw.some(function(l) { return l.last_message && l.last_message.trim(); });
      if (hasReplied && _ttLeadsFilter === 'all') {
        var repliedTabAuto = document.querySelector('.tt-lp-tab[data-f="replied"]');
        if (repliedTabAuto) {
          document.querySelectorAll('.tt-lp-tab').forEach(function(t) { t.classList.remove('active'); });
          repliedTabAuto.classList.add('active');
          _ttLeadsFilter = 'replied';
        }
      }
    }
    _ttRenderLeadsPanel(body, window._ttLeadsRaw, deviceId);
    // 加载 cron 倒计时（仅首次加载）
    if (!append) _ttLoadCronStatus();
  } catch(e) {
    if (!append) body.innerHTML = '<div style="color:#f87171;font-size:12px;padding:30px;text-align:center">加载失败<br><small>' + _escHtml(e.message) + '</small></div>';
  }
}

// 加载更多（分页追加）
async function _ttLoadMoreLeads(deviceId) {
  if (_ttLeadsOffset >= _ttLeadsTotalFromApi) return;
  await _ttLoadLeadsPanel(deviceId, true);
}

// 收件箱 cron 倒计时（加载 leads 面板时顺带拉取）
async function _ttLoadCronStatus() {
  try {
    var r = await fetch('/tiktok/cron-status', { credentials: 'include' });
    if (!r.ok) return;
    var d = await r.json();
    var inboxJob = (d.jobs || []).find(function(j) { return j.id === 'tiktok_inbox_10m'; });
    if (!inboxJob) return;
    var el = document.getElementById('tt-lp-cron');
    if (!el) return;
    if (!inboxJob.enabled) { el.textContent = '⏸ 收件箱暂停'; return; }
    var secs = inboxJob.next_run_secs;
    if (secs >= 0) {
      var mins = Math.floor(secs / 60), s = secs % 60;
      el.textContent = '⏰ ' + mins + ':' + String(s).padStart(2,'0') + ' 后检查';
      el.style.color = secs < 60 ? '#22c55e' : 'rgba(255,255,255,.3)';
      // 倒计时更新
      var _cronTimer = setInterval(function() {
        secs--;
        if (secs <= 0) { clearInterval(_cronTimer); el.textContent = '⏰ 检查中...'; el.style.color='#22c55e'; return; }
        var m2 = Math.floor(secs/60), s2 = secs%60;
        el.textContent = '⏰ ' + m2 + ':' + String(s2).padStart(2,'0') + ' 后检查';
        el.style.color = secs < 60 ? '#22c55e' : 'rgba(255,255,255,.3)';
      }, 1000);
    }
    // 上次运行时间提示
    var lastRun = inboxJob.last_run || '';
    if (lastRun) {
      var cronEl = document.getElementById('tt-lp-cron');
      if (cronEl) cronEl.title = '上次: ' + lastRun;
    }
  } catch(e) {}
}

function _ttRenderLeadsPanel(el, allLeads, deviceId) {
  if (!el) return;

  // ── 过滤（先 filter，再 search）──
  let leads = allLeads;
  if (_ttLeadsFilter === 'unpitched') leads = allLeads.filter(function(l) { return !l.pitched_at && (l.status === 'responded' || l.status === 'new' || l.status === 'contacted'); });
  else if (_ttLeadsFilter === 'qualified') leads = allLeads.filter(function(l) { return l.status === 'qualified'; });
  else if (_ttLeadsFilter === 'responded') leads = allLeads.filter(function(l) { return l.status === 'responded'; });
  else if (_ttLeadsFilter === 'hot') leads = allLeads.filter(function(l) { return (l.score || 0) >= 20; });
  else if (_ttLeadsFilter === 'replied') leads = allLeads.filter(function(l) { return l.last_message && l.last_message.trim(); });

  // ── 搜索过滤 ──
  if (_ttLeadsSearch) {
    var q = _ttLeadsSearch;
    leads = leads.filter(function(l) {
      var name = ((l.name || '') + ' ' + (l.username || '')).toLowerCase();
      return name.indexOf(q) >= 0;
    });
  }

  // ── 更新快速统计栏 ──
  var needsReplyCount = allLeads.filter(function(l) { return l.intent === 'NEEDS_REPLY'; }).length;
  var repliedCount = allLeads.filter(function(l) { return l.last_message && l.last_message.trim(); }).length;
  var qualifiedCount = allLeads.filter(function(l) { return l.status === 'qualified'; }).length;
  var statsEl = document.getElementById('tt-lp-stats');
  if (statsEl) {
    statsEl.innerHTML =
      '<span>总计 <b style="color:var(--text-main)">' + allLeads.length + '</b></span>' +
      (repliedCount > 0 ? '<span>&#128276; 有回复 <b style="color:#f59e0b">' + repliedCount + '</b></span>' : '') +
      (needsReplyCount > 0 ? '<span>&#128200; 待跟进 <b style="color:#22c55e">' + needsReplyCount + '</b></span>' : '') +
      (qualifiedCount > 0 ? '<span>&#11088; 已合格 <b style="color:#a78bfa">' + qualifiedCount + '</b></span>' : '');
  }

  // ── 动态更新"有回复"标签徽章 ──
  var repliedTab = document.querySelector('.tt-lp-tab[data-f="replied"]');
  if (repliedTab) {
    repliedTab.innerHTML = '&#128276; 有回复' + (repliedCount > 0
      ? ' <span style="background:#f59e0b;color:#000;border-radius:8px;padding:0 5px;font-size:10px;font-weight:700;margin-left:2px">' + repliedCount + '</span>'
      : '');
  }

  var isRepliedMode = _ttLeadsFilter === 'replied';

  if (!leads.length) {
    var emptyIcon = isRepliedMode ? '&#128236;' : '&#128269;';
    var emptyMsg = isRepliedMode ? '暂无有回复的线索' : (_ttLeadsFilter !== 'all' ? '该分类暂无线索' : '暂无线索');
    el.innerHTML =
      '<div style="text-align:center;padding:40px 20px">' +
        '<div style="font-size:36px;margin-bottom:10px;opacity:.5">' + emptyIcon + '</div>' +
        '<div style="font-size:13px;color:var(--text-muted)">' + emptyMsg + '</div>' +
        (isRepliedMode
          ? '<div style="font-size:11px;color:var(--text-muted);margin-top:6px;line-height:1.5">收件箱每10分钟自动检查<br>有回复时将自动出现在这里</div>'
          : (_ttLeadsFilter === 'all' ? '<div style="font-size:11px;color:var(--text-muted);margin-top:6px;line-height:1.5">先关注目标用户<br>积累互动后产生线索</div>' : '')) +
      '</div>';
    return;
  }

  var scoreColor = function(s) { return s >= 25 ? '#22c55e' : s >= 15 ? '#f59e0b' : '#94a3b8'; };
  var statusLabel = { responded: '已回应', qualified: '已合格', contacted: '已接触', converted: '已成交' };
  var statusColor = { responded: '#f59e0b', qualified: '#22c55e', contacted: '#60a5fa', converted: '#a78bfa' };

  // 按评分降序
  leads = leads.slice().sort(function(a, b) { return (b.score || 0) - (a.score || 0); });

  // ── 批量操作栏（有回复模式 + NEEDS_REPLY模式） ──
  var batchBar = '';
  var withMsg = leads.filter(function(l) { return l.last_message && l.last_message.length > 0; }).length;
  var needsReplyLeads = leads.filter(function(l) { return l.intent === 'NEEDS_REPLY' && l.status !== 'qualified'; });
  if (isRepliedMode || needsReplyLeads.length > 0) {
    var barBg = isRepliedMode ? 'rgba(245,158,11,.06)' : 'rgba(34,197,94,.06)';
    var barBorder = isRepliedMode ? 'rgba(245,158,11,.2)' : 'rgba(34,197,94,.2)';
    var barLabel = isRepliedMode
      ? '&#129302; ' + leads.length + ' 条待跟进' + (withMsg > 0 ? '，其中 ' + withMsg + ' 条有消息' : '')
      : '&#128200; ' + needsReplyLeads.length + ' 条NEEDS_REPLY待升格';
    batchBar =
      '<div style="display:flex;align-items:center;gap:8px;padding:8px 10px;background:' + barBg + ';border:1px solid ' + barBorder + ';border-radius:8px;margin-bottom:10px">' +
        '<span style="font-size:11px;color:' + (isRepliedMode ? '#f59e0b' : '#22c55e') + ';flex:1">' + barLabel + '</span>' +
        (withMsg > 0 && isRepliedMode
          ? '<button onclick="_ttBatchAiReply(\'' + _escHtml(deviceId) + '\')" style="font-size:11px;padding:5px 12px;border-radius:6px;background:rgba(139,92,246,.2);border:1px solid rgba(139,92,246,.4);color:#c4b5fd;cursor:pointer;white-space:nowrap">&#129302; 批量AI回复</button>'
          : '') +
        (needsReplyLeads.length > 0
          ? '<button onclick="_ttBatchQualify(\'' + _escHtml(deviceId) + '\')" style="font-size:11px;padding:5px 12px;border-radius:6px;background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.3);color:#22c55e;cursor:pointer;white-space:nowrap">&#11088; 批量升格</button>'
          : '') +
        '<button onclick="_ttAiRescore(this,\'' + _escHtml(deviceId) + '\')" style="font-size:11px;padding:5px 12px;border-radius:6px;background:rgba(99,102,241,.15);border:1px solid rgba(99,102,241,.3);color:#a5b4fc;cursor:pointer;white-space:nowrap">&#129504; AI重评</button>' +
      '</div>';
  }

  // N1: intent 徽章辅助函数
  var intentBadge = function(intent) {
    if (!intent) return '';
    var imap = {
      'NEEDS_REPLY': ['感兴趣', '#22c55e'],
      'OPTIONAL':    ['询问中', '#f59e0b'],
      'NO_REPLY':    ['不感兴趣', '#ef4444'],
    };
    var parts = imap[intent] || [intent, '#94a3b8'];
    return '<span style="font-size:9px;padding:2px 5px;border-radius:8px;background:' + parts[1] + '22;color:' + parts[1] + ';border:1px solid ' + parts[1] + '44;flex-shrink:0">' + parts[0] + '</span>';
  };

  // N2: 展开对话按钮（含互动数量徽章）
  var historyBtn = function(lid, deviceId, intCount) {
    var badge = (intCount > 0)
      ? ' <span style="background:rgba(99,102,241,.35);color:#a5b4fc;border-radius:8px;padding:0 4px;font-size:9px;font-weight:600">' + intCount + '</span>'
      : '';
    return '<button style="font-size:10px;padding:3px 8px;border-radius:5px;background:transparent;border:1px solid rgba(255,255,255,.1);color:var(--text-muted);cursor:pointer;white-space:nowrap"' +
      ' id="tt-hist-btn-' + lid + '" data-lid="' + lid + '" data-did="' + _escHtml(deviceId||'') + '" data-open="0"' +
      ' onclick="_ttToggleHistory(this)">&#128172; 对话' + badge + '</button>';
  };

  // 设备归因 badge（含 W03 节点标识）
  var deviceBadge = function(alias, overridden, node) {
    if (!alias) return '';
    var isW03 = node === 'worker03';
    var bg = overridden ? 'rgba(34,197,94,.15)' : (isW03 ? 'rgba(96,165,250,.12)' : 'rgba(255,255,255,.06)');
    var border = overridden ? 'rgba(34,197,94,.3)' : (isW03 ? 'rgba(96,165,250,.25)' : 'rgba(255,255,255,.1)');
    var color = overridden ? '#86efac' : (isW03 ? '#93c5fd' : 'var(--text-muted)');
    var icon = overridden ? '&#11088; ' : (isW03 ? '&#127760; ' : '&#128241; ');
    var nodeTag = isW03 ? '<span style="font-size:7px;background:rgba(96,165,250,.2);color:#60a5fa;border-radius:3px;padding:0 3px;margin-left:2px">W03</span>' : '';
    return '<span style="font-size:8px;padding:1px 5px;border-radius:8px;background:' + bg + ';border:1px solid ' + border + ';color:' + color + ';flex-shrink:0;display:inline-flex;align-items:center;gap:2px">' +
      icon + _escHtml(alias) + nodeTag + '</span>';
  };

  var rows = leads.map(function(l) {
    var name = l.username || l.name || '?';
    var lid  = String(l.id || l.lead_id || 0);
    var score = l.score || 0;
    var sc    = scoreColor(score);
    var st    = l.status || 'responded';
    var stLabel = statusLabel[st] || st;
    var stColor = statusColor[st] || '#94a3b8';
    var pitched = !!(l.pitched_at || l.last_pitched);
    var lastMsg = (l.last_message || '').trim();
    var safeLastMsg = lastMsg.replace(/'/g, '&apos;').replace(/"/g, '&quot;');
    var intent = l.intent || '';
    var intCount = l.interaction_count || 0;
    var srcAlias = l.source_device_alias || '';
    var srcNode = l.source_node || 'local';
    var overridden = !!l.status_overridden;

    if (isRepliedMode) {
      // ══ 回复模式：消息气泡 + AI回复为主操作 + intent 徽章 ══
      var msgBubble = lastMsg
        ? '<div style="background:rgba(245,158,11,.08);border:1px solid rgba(245,158,11,.2);border-radius:0 8px 8px 8px;padding:8px 10px;margin:6px 0;font-size:12px;color:var(--text-main);line-height:1.5">' +
            '<div style="font-size:9px;color:#f59e0b;margin-bottom:3px">&#128172; 对方消息</div>' +
            _escHtml(lastMsg.substring(0, 120)) + (lastMsg.length > 120 ? '...' : '') +
          '</div>'
        : '<div style="font-size:11px;color:var(--text-muted);font-style:italic;padding:4px 0">等待下次收件箱检查获取消息...</div>';

      var aiReplyBtn = lastMsg
        ? '<button style="flex:1;font-size:12px;padding:6px 8px;border-radius:6px;background:rgba(139,92,246,.2);border:1px solid rgba(139,92,246,.4);color:#c4b5fd;cursor:pointer;white-space:nowrap"' +
            ' data-lid="' + lid + '" data-name="' + _escHtml(name) + '" data-did="' + _escHtml(deviceId||'') + '" data-msg="' + safeLastMsg.substring(0,100) + '"' +
            ' onclick="_ttAiReplyLead(this.dataset.lid,this.dataset.name,this.dataset.did,this.dataset.msg,this)">&#129302; AI回复</button>'
        : '';
      var pitchBtn = '<button style="font-size:11px;padding:5px 10px;border-radius:6px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);color:var(--text-muted);cursor:pointer;white-space:nowrap"' +
          ' data-lid="' + lid + '" data-name="' + _escHtml(name) + '" data-did="' + _escHtml(deviceId||'') + '"' +
          ' onclick="_ttPitchLeadsPanel(this.dataset.lid,this.dataset.name,this.dataset.did,this)">' + (pitched ? '再发话术' : '发话术') + '</button>';

      return '<div class="tt-lp-card st-' + st + '" id="tt-lp-row-' + lid + '" data-lastmsg="' + safeLastMsg.substring(0,100) + '" data-username="' + _escHtml(name) + '">' +
        '<div style="display:flex;align-items:center;gap:5px;margin-bottom:2px">' +
          '<span style="font-size:13px;font-weight:600;color:var(--text-main);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:110px">' + _escHtml(name) + '</span>' +
          '<span style="font-size:9px;padding:2px 6px;border-radius:10px;background:' + stColor + '1a;color:' + stColor + ';border:1px solid ' + stColor + '40">' + stLabel + '</span>' +
          intentBadge(intent) +
          deviceBadge(srcAlias, overridden, srcNode) +
          '<span style="margin-left:auto;font-size:10px;color:' + sc + ';flex-shrink:0">★ ' + score + '</span>' +
        '</div>' +
        msgBubble +
        '<div style="display:flex;gap:6px;margin-top:6px">' +
          aiReplyBtn + pitchBtn + historyBtn(lid, deviceId||'', intCount) +
        '</div>' +
        '<div id="tt-hist-' + lid + '" style="display:none"></div>' +  // N2: 对话历史容器
      '</div>';
    } else {
      // ══ 普通模式：评分 + 话术按钮，有消息时显示气泡预览 ══
      var preview = lastMsg
        ? '<div style="font-size:11px;color:var(--text-muted);margin-top:4px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden">' +
            '&#128236; ' + _escHtml(lastMsg.substring(0, 70)) + '</div>'
        : '';
      return '<div class="tt-lp-card st-' + st + (pitched ? ' sent' : '') + '" id="tt-lp-row-' + lid + '" data-pitched="' + (pitched ? '1' : '') + '" data-username="' + _escHtml(name) + '">' +
        '<div style="display:flex;align-items:flex-start;gap:8px">' +
          '<div style="flex:1;min-width:0">' +
            '<div style="display:flex;align-items:center;gap:5px;flex-wrap:wrap;margin-bottom:2px">' +
              '<span style="font-size:13px;font-weight:600;color:var(--text-main);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:130px">' + _escHtml(name) + '</span>' +
              '<span style="font-size:9px;padding:2px 6px;border-radius:10px;background:' + stColor + '1a;color:' + stColor + ';border:1px solid ' + stColor + '40;flex-shrink:0">' + stLabel + '</span>' +
              intentBadge(intent) +
              deviceBadge(srcAlias, overridden, srcNode) +
            '</div>' +
            preview +
          '</div>' +
          '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:5px;flex-shrink:0">' +
            '<div class="tt-lp-score-wrap">' +
              '<span class="tt-lp-score-num" style="color:' + sc + '">' + score + '</span>' +
              '<div class="tt-lp-score-bar"><div class="tt-lp-score-fill" style="width:' + Math.min(score*2.5,100).toFixed(0) + '%;background:' + sc + '"></div></div>' +
              '<span class="tt-lp-score-stars" style="color:' + sc + '">' + (score>=25?'★★★':score>=15?'★★':'★') + '</span>' +
            '</div>' +
            '<button class="tt-lp-pitch" data-lid="' + lid + '" data-name="' + _escHtml(name) + '" data-did="' + _escHtml(deviceId||'') + '" ' +
              'onclick="_ttPitchLeadsPanel(this.dataset.lid,this.dataset.name,this.dataset.did,this)">' + (pitched ? '再次发送' : '发话术') + '</button>' +
            historyBtn(lid, deviceId||'', intCount) +
          '</div>' +
        '</div>' +
        '<div id="tt-hist-' + lid + '" style="display:none"></div>' +
      '</div>';
    }
  }).join('');

  var summary =
    '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px;padding-bottom:8px;border-bottom:1px solid rgba(255,255,255,.06)">' +
      '<span style="font-size:11px;color:var(--text-muted)">' + leads.length + ' 条' + (leads.length < allLeads.length ? '（已过滤）' : '') +
        (_ttLeadsTotalFromApi > leads.length ? ' / 共 ' + _ttLeadsTotalFromApi : '') + '</span>' +
      '<span style="font-size:10px;color:var(--text-muted)">' + (isRepliedMode ? '最新消息优先' : '按评分排序') + '</span>' +
    '</div>';

  // 加载更多按钮（当 API 返回总数 > 当前已加载数时显示）
  var loadMoreBtn = '';
  var hasMore = _ttLeadsTotalFromApi > (window._ttLeadsRaw || []).length;
  if (hasMore) {
    var remaining = _ttLeadsTotalFromApi - (window._ttLeadsRaw || []).length;
    loadMoreBtn = '<div style="text-align:center;margin-top:12px">' +
      '<button onclick="_ttLoadMoreLeads(\'' + _escHtml(deviceId) + '\')" ' +
        'style="font-size:11px;padding:7px 20px;border-radius:8px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);color:var(--text-muted);cursor:pointer">' +
        '加载更多（剩余 ' + remaining + ' 条）' +
      '</button>' +
    '</div>';
  }

  el.innerHTML = batchBar + summary + rows + loadMoreBtn;
}

async function _ttPitchLeadsPanel(leadId, name, deviceId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '预览中...'; }

  // P5: 获取预览话术并弹窗确认
  let previewData = { preview: '', configured: false };
  try {
    const pr = await fetch(
      '/tiktok/device/' + encodeURIComponent(deviceId) + '/pitch/preview?username=' + encodeURIComponent(name),
      { credentials: 'include' }
    );
    if (pr.ok) previewData = await pr.json();
  } catch(e) {
    // 降级：用前端缓存的设备配置生成预览
    const dev = (window._ttDevicesCache || {})[deviceId] || {};
    const tg = dev.telegram || '', wa = dev.whatsapp || '';
    const cleanName = name.replace(/^@/, '').split('_')[0];
    let txt = '嗨 ' + cleanName + '，谢谢你的互动！';
    if (tg) txt += ' 有兴趣可加 Telegram: ' + tg;
    else if (wa) txt += ' 有兴趣可加 WhatsApp: ' + wa;
    previewData = { preview: txt, configured: !!(tg || wa) };
  }

  // 注入 deviceId/username 供 AI 优化按钮使用
  previewData.deviceId = deviceId;
  previewData.username = name;

  // 弹出预览/编辑弹窗
  if (btn) { btn.disabled = true; btn.textContent = '待确认...'; }
  const confirmedText = await _ttShowPitchModal(name, previewData.preview, previewData);
  if (confirmedText === null) {
    // 用户取消
    if (btn) { btn.disabled = false; btn.textContent = '发话术'; }
    return;
  }

  // 发送
  if (btn) { btn.textContent = '发送中...'; }
  try {
    const r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/pitch', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lead_id: leadId, lead_name: name, custom_message: confirmedText })
    });
    const d = await r.json();
    if (d.ok || d.status === 'ok') {
      const row = document.getElementById('tt-lp-row-' + leadId);
      if (row) { row.classList.add('sent'); row.dataset.pitched = '1'; }
      if (btn) { btn.textContent = '✓ 已发'; }
      _ttShowToast('✓ 话术已发送给 ' + name, 'success', 3000);
    } else {
      if (btn) { btn.disabled = false; btn.textContent = '重试'; }
      _ttShowToast('发送失败: ' + (d.message || '未知错误'), 'error');
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '失败'; }
    _ttShowToast('网络错误: ' + e.message, 'error');
  }
}

// ── AI 回复单条线索（带 last_message 上下文）──────────────────────
async function _ttAiReplyLead(leadId, name, deviceId, lastMessage, btn) {
  if (btn) { btn.disabled = true; btn.textContent = 'AI生成中...'; }

  // 调用 AI 预览，传递对方消息作上下文
  var previewData = { preview: '', configured: false };
  try {
    var qs = '/tiktok/device/' + encodeURIComponent(deviceId) +
      '/pitch/ai-preview?username=' + encodeURIComponent(name) +
      (lastMessage ? '&last_message=' + encodeURIComponent(lastMessage) : '');
    var pr = await fetch(qs, { credentials: 'include' });
    if (pr.ok) previewData = await pr.json();
  } catch(e) {
    // 降级：从缓存生成
    var dev = (window._ttDevicesCache || {})[deviceId] || {};
    var tg = dev.telegram || '', wa = dev.whatsapp || '';
    var n = name.replace(/^@/, '');
    previewData = {
      preview: '嗨 ' + n + '，谢谢回复！' + (tg ? ' 加我TG: ' + tg : wa ? ' 加我WA: ' + wa : ''),
      configured: !!(tg || wa)
    };
  }

  previewData.deviceId = deviceId;
  previewData.username = name;

  if (btn) { btn.disabled = true; btn.textContent = '待确认...'; }
  var confirmedText = await _ttShowPitchModal(
    name + (lastMessage ? '（续聊）' : ''),
    previewData.preview,
    previewData
  );
  if (confirmedText === null) {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#129302; AI回复'; }
    return;
  }

  if (btn) { btn.textContent = '发送中...'; }
  try {
    var r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/pitch', {
      method: 'POST', credentials: 'include',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ lead_id: leadId, lead_name: name, custom_message: confirmedText })
    });
    var d = await r.json();
    if (d.ok || d.status === 'ok') {
      var row = document.getElementById('tt-lp-row-' + leadId);
      if (row) { row.style.opacity = '0.45'; row.style.pointerEvents = 'none'; }
      if (btn) { btn.textContent = '✓ 已回复'; }
      _ttShowToast('✓ AI回复已发给 ' + name, 'success', 3000);
    } else {
      if (btn) { btn.disabled = false; btn.innerHTML = '&#129302; AI回复'; }
      _ttShowToast('发送失败: ' + (d.message || '未知'), 'error');
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.innerHTML = '&#129302; AI回复'; }
    _ttShowToast('网络错误: ' + (e.message || e), 'error');
  }
}

// ── 批量 AI 回复（"有回复"标签下所有有消息的线索）────────────────
async function _ttBatchAiReply(deviceId) {
  var cards = Array.from(document.querySelectorAll('#tt-lp-body .tt-lp-card[data-lastmsg]'))
    .filter(function(c) { return c.dataset.lastmsg && c.dataset.lastmsg.length > 0; });

  if (!cards.length) {
    _ttShowToast('暂无带消息的线索可回复', 'info');
    return;
  }

  if (!(await ocDialog({title:'批量AI回复',message:'将逐一为 ' + cards.length + ' 条有消息的线索生成AI回复，每条需要手动确认后发送。',type:'info',confirmText:'开始',cancelText:'取消'}))) return;

  for (var i = 0; i < cards.length; i++) {
    var card = cards[i];
    var lid = card.id.replace('tt-lp-row-', '');
    var lastMsg = card.dataset.lastmsg || '';
    var nameSpan = card.querySelector('span[style*="font-weight:600"]');
    var name = nameSpan ? nameSpan.textContent.trim() : '';
    if (!name || !lid) continue;

    _ttShowToast('(' + (i+1) + '/' + cards.length + ') 处理: ' + name, 'info', 2000);
    await _ttAiReplyLead(lid, name, deviceId, lastMsg, null);
    await new Promise(function(r) { setTimeout(r, 400); });
  }
  _ttShowToast('批量AI回复处理完成', 'success', 3000);
}

// ── 批量升格 NEEDS_REPLY → qualified ──
async function _ttBatchQualify(deviceId) {
  var leads = (window._ttLeadsRaw || []).filter(function(l) {
    return l.intent === 'NEEDS_REPLY' && l.status !== 'qualified';
  });
  if (!leads.length) {
    _ttShowToast('没有NEEDS_REPLY线索需要升格', 'info');
    return;
  }
  if (!(await ocDialog({title:'批量升格',message:'将把 ' + leads.length + ' 条 NEEDS_REPLY 线索状态升格为 qualified。',type:'info',confirmText:'确认升格',cancelText:'取消'}))) return;

  var ok = 0, fail = 0;
  for (var i = 0; i < leads.length; i++) {
    var l = leads[i];
    var lid = l.id || l.lead_id;
    if (!lid) continue;
    try {
      var r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/lead/' + lid + '/qualify', {
        method: 'POST', credentials: 'include',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({status: 'qualified', username: l.username || l.name || ''})
      });
      var rd = await r.json();
      if (rd.ok) {
        ok++;
        // 乐观更新本地缓存
        l.status = 'qualified';
        if (window._ttLeadsRaw) {
          var cached = window._ttLeadsRaw.find(function(x) { return (x.id || x.lead_id) == lid; });
          if (cached) cached.status = 'qualified';
        }
      } else fail++;
    } catch(e) { fail++; }
  }
  _ttShowToast('&#11088; 升格完成：' + ok + ' 条成功' + (fail > 0 ? '，' + fail + ' 条失败' : ''), ok > 0 ? 'success' : 'error', 4000);
  if (ok > 0) {
    // 刷新面板
    setTimeout(function() { _ttRefreshLeads(deviceId, null); }, 800);
  }
}

// ════════════════════════════════════════════════════════
// N2 — 对话历史展开（iMessage 样式时间线）
// ════════════════════════════════════════════════════════

async function _ttToggleHistory(btn) {
  var lid = btn.dataset.lid;
  var deviceId = btn.dataset.did;
  var isOpen = btn.dataset.open === '1';
  var container = document.getElementById('tt-hist-' + lid);
  if (!container) return;

  if (isOpen) {
    // 收起
    container.style.display = 'none';
    btn.dataset.open = '0';
    btn.innerHTML = '&#128172; 对话';
    return;
  }

  // 展开：加载历史
  btn.innerHTML = '⏳';
  btn.disabled = true;
  container.style.display = 'block';
  container.innerHTML = '<div style="color:var(--text-muted);font-size:11px;padding:8px;text-align:center">加载对话中...</div>';

  try {
    var resp = await fetch(
      '/tiktok/device/' + encodeURIComponent(deviceId) + '/lead/' + encodeURIComponent(lid) + '/history',
      { credentials: 'include' }
    );
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    var data = await resp.json();
    _ttRenderHistory(container, data.history || []);
    btn.dataset.open = '1';
    btn.innerHTML = '&#128172; 收起';
  } catch(e) {
    container.innerHTML = '<div style="color:#f87171;font-size:11px;padding:8px">加载失败: ' + _escHtml(e.message || '') + '</div>';
  } finally {
    btn.disabled = false;
  }
}

function _ttRenderHistory(el, history) {
  if (!el) return;
  if (!history || !history.length) {
    el.innerHTML = '<div style="color:var(--text-muted);font-size:11px;padding:12px;text-align:center">暂无对话记录</div>';
    return;
  }

  var actionIcon = {
    'send_dm': '📤', 'dm_received': '📨', 'auto_reply': '🤖',
    'pitch': '💬', 'follow': '➕', 'follow_back': '🔄', 'message_classified': '🏷'
  };

  var html = '<div style="margin-top:8px;border-top:1px solid rgba(255,255,255,.06);padding-top:8px;display:flex;flex-direction:column;gap:6px">';

  history.forEach(function(msg) {
    var isIn = msg.direction === 'inbound';
    var icon = actionIcon[msg.action] || (isIn ? '📨' : '📤');
    var bubbleStyle = isIn
      ? 'align-self:flex-start;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);border-radius:4px 10px 10px 10px;max-width:88%'
      : 'align-self:flex-end;background:rgba(99,102,241,.18);border:1px solid rgba(99,102,241,.3);border-radius:10px 4px 10px 10px;max-width:88%';

    html +=
      '<div style="display:flex;flex-direction:column;' + (isIn ? '' : 'align-items:flex-end') + '">' +
        '<div style="' + bubbleStyle + ';padding:7px 10px">' +
          '<div style="font-size:11px;color:var(--text-main);line-height:1.5">' + _escHtml(msg.content) + '</div>' +
        '</div>' +
        '<div style="font-size:9px;color:var(--text-muted);margin-top:2px;' + (isIn ? '' : 'text-align:right') + '">' +
          icon + ' ' + (msg.action_label || msg.action) +
          (msg.display_time ? ' &nbsp;·&nbsp; ' + _escHtml(msg.display_time) : '') +
          (msg.intent && isIn ? ' &nbsp;·&nbsp; <span style="color:#f59e0b">' + _escHtml(msg.intent) + '</span>' : '') +
        '</div>' +
      '</div>';
  });

  html += '</div>';
  el.innerHTML = html;
}

async function _ttLpBatchPitch(deviceId, btn) {
  const rows = document.querySelectorAll('#tt-lp-body .tt-lp-card:not(.sent)');
  if (!rows.length) return;
  if (btn) { btn.disabled = true; btn.textContent = '批量发送中...'; }
  let done = 0;
  for (let i = 0; i < rows.length; i++) {
    const pitchBtn = rows[i].querySelector('.tt-lp-pitch');
    if (!pitchBtn) continue;
    const lid = pitchBtn.dataset.lid;
    const name = pitchBtn.dataset.name;
    await _ttPitchLeadsPanel(lid, name, deviceId, pitchBtn);
    done++;
    if (btn) btn.textContent = '批量发送中... ' + done + '/' + rows.length;
    await new Promise(function(res) { setTimeout(res, 400); });
  }
  if (btn) { btn.disabled = false; btn.textContent = '✓ 全部发送完成（' + done + ' 条）'; }
}

// P1: 跨设备批量发话术（所有有线索的设备）
async function _ttBatchPitchAll(btn) {
  const devices = Object.values(window._ttDevicesCache || {});
  const withLeads = devices.filter(function(d) { return (d.leads_count || 0) > 0 && d.online; });
  if (!withLeads.length) { if (btn) btn.textContent = '暂无线索'; return; }
  if (btn) { btn.disabled = true; btn.textContent = '准备中...'; }
  let totalDone = 0;
  for (let di = 0; di < withLeads.length; di++) {
    const dev = withLeads[di];
    try {
      const r = await fetch('/tiktok/device/' + encodeURIComponent(dev.device_id) + '/leads?limit=100', { credentials: 'include' });
      const d = await r.json();
      const unpitched = (d.leads || []).filter(function(l) { return !(l.pitched_at || l.last_pitched); });
      for (let li = 0; li < unpitched.length; li++) {
        const lead = unpitched[li];
        try {
          await fetch('/tiktok/device/' + encodeURIComponent(dev.device_id) + '/pitch', {
            method: 'POST', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ lead_id: lead.id, lead_name: lead.username || lead.name })
          });
          totalDone++;
        } catch(e) {}
        if (btn) btn.textContent = '发送中 ' + totalDone + ' 条...';
        await new Promise(function(res) { setTimeout(res, 350); });
      }
    } catch(e) {}
  }
  if (btn) { btn.disabled = false; btn.textContent = '✓ 已发送 ' + totalDone + ' 条'; }
  // 2秒后刷新网格
  setTimeout(_loadTtDeviceGrid, 2000);
}

async function _ttStatDetail(stat, deviceId, el) {
  const safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  const detailEl = document.getElementById('tt-stat-detail-' + safeId);
  if (!detailEl) return;

  // 如果已展开同一个，收起
  if (detailEl.dataset.openStat === stat) {
    detailEl.innerHTML = '';
    detailEl.dataset.openStat = '';
    return;
  }
  detailEl.dataset.openStat = stat;
  detailEl.innerHTML = '<div style="padding:8px;color:var(--text-muted);font-size:11px">加载中...</div>';

  const statLabels = { sessions: '会话记录', watched: '视频观看', followed: '关注记录', dms: '私信记录', leads: '线索列表' };
  const label = statLabels[stat] || stat;

  if (stat === 'leads') {
    detailEl.innerHTML = '';
    detailEl.dataset.openStat = '';
    // 打开独立线索页
    _ttOpenLeadsPage(deviceId);
    return;
  }

  try {
    const r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/stats?stat=' + stat, { credentials: 'include' });
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const data = await r.json();
    const items = data.items || [];
    if (!items.length) {
      detailEl.innerHTML = '<div style="padding:6px 0;font-size:11px;color:var(--text-muted)">暂无' + label + '记录</div>';
      return;
    }
    detailEl.innerHTML = '<div style="background:rgba(255,255,255,.03);border-radius:6px;padding:8px;max-height:160px;overflow-y:auto">' +
      '<div style="font-size:10px;font-weight:600;color:var(--text-muted);margin-bottom:6px">' + label + '（今日）</div>' +
      items.slice(0, 20).map(function(item) {
        return '<div style="display:flex;gap:8px;padding:3px 0;border-bottom:1px solid rgba(255,255,255,.05);font-size:11px">' +
          '<span style="color:var(--text-muted);flex-shrink:0;width:40px">' + (item.time || '') + '</span>' +
          '<span style="flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--text-main)">' + _escHtml(item.label || item.username || item.content || '-') + '</span>' +
          (item.status ? '<span style="color:#22c55e;font-size:10px;flex-shrink:0">' + item.status + '</span>' : '') +
        '</div>';
      }).join('') +
    '</div>';
  } catch(e) {
    detailEl.innerHTML = '<div style="font-size:11px;color:#f87171;padding:4px 0">无法加载: ' + e.message + '</div>';
  }
}

async function _ttLoadPanelLeads(deviceId, dev) {
  const safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  const el = document.getElementById('tt-panel-leads-' + safeId);
  if (!el) return;
  const allLeads = window._ttLeadsCache || [];
  const devLeads = allLeads.filter(l => l.device_id === deviceId || l.source_device === deviceId);
  if (devLeads.length === 0) {
    try {
      const r = await fetch('/tiktok/qualified-leads?device_id=' + encodeURIComponent(deviceId), { credentials: 'include' });
      if (r.ok) {
        const d = await r.json();
        const leads = d.leads || d.items || d || [];
        if (leads.length) { _ttRenderLeads(el, leads); return; }
      }
    } catch(e) {}
    el.innerHTML = '<div style="color:var(--text-muted);text-align:center;padding:8px">暂无线索</div>';
    return;
  }
  _ttRenderLeads(el, devLeads);
}

async function _ttLoadPanelLeads(deviceId, forceRefresh) {
  const safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  const el = document.getElementById('tt-panel-leads-' + safeId);
  if (!el) return;
  el.innerHTML = '<div style="color:var(--text-muted);font-size:11px;padding:4px">加载中...</div>';

  try {
    // 优先从新的设备专属端点获取
    const r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/leads?limit=30', { credentials: 'include' });
    if (r.ok) {
      const d = await r.json();
      const leads = d.leads || [];
      _ttRenderLeads(el, leads, deviceId);
      return;
    }
  } catch(e) {}

  // 降级：从缓存过滤
  const allLeads = window._ttLeadsCache || [];
  const devLeads = allLeads.filter(function(l) { return l.device_id === deviceId || l.source_device === deviceId; });
  _ttRenderLeads(el, devLeads, deviceId);
}

function _ttRenderLeads(el, leads, deviceId) {
  if (!leads || !leads.length) {
    el.innerHTML = '<div style="text-align:center;padding:12px 8px">' +
      '<div style="font-size:20px;margin-bottom:4px">📭</div>' +
      '<div style="font-size:11px;color:var(--text-muted)">暂无合格线索</div>' +
      '<div style="font-size:10px;color:var(--text-muted);margin-top:2px">建议：先关注更多目标用户，积累互动后产生线索</div>' +
    '</div>';
    return;
  }

  const scoreColor = function(s) { return s >= 25 ? '#22c55e' : s >= 15 ? '#f59e0b' : '#94a3b8'; };
  const statusLabel = { responded: '已回应', qualified: '已合格', contacted: '已接触', converted: '已成交' };
  const statusColor = { responded: '#f59e0b', qualified: '#22c55e', contacted: '#60a5fa', converted: '#a78bfa' };

  const header = '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">' +
    '<span style="font-size:11px;color:var(--text-muted)">' + leads.length + ' 条线索</span>' +
    '<span style="font-size:10px;color:var(--text-muted)">评分越高越易转化</span>' +
  '</div>';

  const rows = leads.slice(0, 15).map(function(l) {
    const name = l.username || l.name || '?';
    const lid = l.id || l.lead_id || 0;
    const score = l.score || 0;
    const sc = scoreColor(score);
    const st = l.status || 'responded';
    const stLabel = statusLabel[st] || st;
    const stColor = statusColor[st] || '#94a3b8';
    const pitched = l.pitched_at || l.last_pitched;
    const pitchLabel = pitched ? '再次发送' : '发话术';
    const pitchExtra = pitched ? ' style="opacity:.7"' : '';
    const preview = l.last_message ? ('<div style="font-size:10px;color:var(--text-muted);margin-top:2px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">' + _escHtml((l.last_message || '').substring(0, 40)) + '</div>') : '';
    const lidStr = String(lid);
    const nameJson = JSON.stringify(name);
    return '<div class="tt-lead-row" id="tt-lead-row-' + lidStr + '">' +
      '<div style="flex:1;min-width:0">' +
        '<div style="display:flex;align-items:center;gap:5px">' +
          '<span class="tt-lead-name">' + _escHtml(name) + '</span>' +
          '<span style="font-size:9px;padding:1px 5px;border-radius:3px;background:' + stColor + '20;color:' + stColor + ';flex-shrink:0">' + stLabel + '</span>' +
        '</div>' +
        preview +
      '</div>' +
      '<span class="tt-lead-score" style="background:' + sc + '20;color:' + sc + '">' + score + '</span>' +
      '<button class="tt-lead-pitch" data-lid="' + lidStr + '" data-name="' + _escHtml(name) + '" data-did="' + _escHtml(deviceId||'') + '"' + pitchExtra + ' onclick="_ttPitchOneLead(this.dataset.lid,this.dataset.name,this.dataset.did,this)">' + pitchLabel + '</button>' +
    '</div>';
  }).join('');

  const more = leads.length > 15 ?
    '<div style="text-align:center;padding:6px;font-size:11px;color:var(--text-muted)">还有 ' + (leads.length-15) + ' 条，共 ' + leads.length + ' 条</div>' : '';

  el.innerHTML = header + '<div style="max-height:240px;overflow-y:auto">' + rows + '</div>' + more;
}

async function _ttPitchOneLead(leadId, name, deviceId, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '发送中...'; }
  try {
    const body = { lead_id: leadId, cta_url: typeof _ttCtaUrl !== 'undefined' ? _ttCtaUrl : '' };
    if (deviceId) body.device_id = deviceId;
    const r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/pitch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
      body: JSON.stringify(body)
    }).then(function(r) { return r.json(); });
    if (r.ok !== false) {
      if (btn) { btn.textContent = '已发 ✓'; btn.style.color = '#22c55e'; btn.style.borderColor = '#22c55e'; }
      // 标记该行
      const row = document.getElementById('tt-lead-row-' + leadId);
      if (row) row.style.opacity = '0.6';
      if (typeof showToast === 'function') showToast('话术已发送给 ' + name, 'success');
    } else {
      if (btn) { btn.disabled = false; btn.textContent = '重试'; }
      if (typeof showToast === 'function') showToast(r.error || '发送失败', 'error');
    }
  } catch(e) {
    if (btn) { btn.disabled = false; btn.textContent = '重试'; }
    if (typeof showToast === 'function') showToast('网络错误: ' + e.message, 'error');
  }
}

async function _ttSaveRef(deviceId, field, value, inputEl, indId) {
  const ind = document.getElementById(indId);
  if (ind) { ind.textContent = '保存中...'; ind.style.color = '#94a3b8'; }
  try {
    const body = { device_id: deviceId };
    body[field] = value;
    const r = await fetch('/tiktok/referral-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body)
    });
    if (r.ok) {
      if (ind) { ind.textContent = '已保存 ✓'; ind.style.color = '#22c55e'; setTimeout(function() { if (ind) ind.textContent = ''; }, 2000); }
      // 更新缓存（通用联系方式）
      if (window._ttDevicesCache && window._ttDevicesCache[deviceId]) {
        const dev = window._ttDevicesCache[deviceId];
        if (!dev.contacts) dev.contacts = {};
        if (value) {
          dev.contacts[field] = value;
          dev[field] = value;  // backward compat for telegram/whatsapp
        } else {
          delete dev.contacts[field];
          dev[field] = '';
        }
        dev.configured = Object.values(dev.contacts).some(function(v) { return !!v; });
      }
    } else {
      if (ind) { ind.textContent = '保存失败'; ind.style.color = '#f87171'; }
    }
  } catch(e) {
    if (ind) { ind.textContent = '网络错误'; ind.style.color = '#f87171'; }
  }
}

// 删除引流联系方式（空值=删除）
function _ttDelContact(deviceId, app, safeId, indId) {
  _ttSaveRef(deviceId, app, '', null, indId);
  const row = document.getElementById('ref-row-' + safeId + '-' + app);
  if (row) row.remove();
}

// 显示"添加应用"内联表单
function _ttShowAddContact(deviceId, safeId, indId) {
  const addDiv = document.getElementById('ref-add-' + safeId);
  if (!addDiv) return;
  const knownApps = [
    {id:'telegram',label:'Telegram',icon:'\u{1F4AC}',color:'#60a5fa'},
    {id:'whatsapp',label:'WhatsApp',icon:'\u{1F4F1}',color:'#22c55e'},
    {id:'instagram',label:'Instagram',icon:'\u{1F4F7}',color:'#f472b6'},
    {id:'line',label:'Line',icon:'\u{1F49A}',color:'#4ade80'},
    {id:'wechat',label:'WeChat',icon:'\u{1F4E8}',color:'#22c55e'},
    {id:'viber',label:'Viber',icon:'\u{1F4DE}',color:'#8b5cf6'},
    {id:'signal',label:'Signal',icon:'\u{1F510}',color:'#64748b'},
    {id:'facebook',label:'Facebook',icon:'\u{1F310}',color:'#3b82f6'},
  ];
  const appOptions = knownApps.map(function(a) {
    return '<option value="' + a.id + '" style="background:#1e293b;color:#e2e8f0">' + a.icon + ' ' + a.label + '</option>';
  }).join('') + '<option value="_custom" style="background:#1e293b;color:#e2e8f0">\u270F\uFE0F 自定义...</option>';
  addDiv.innerHTML =
    '<div style="display:flex;gap:4px;margin-top:4px;align-items:center">' +
      '<select id="ref-new-app-' + safeId + '" style="background:#1e293b;border:1px solid rgba(99,102,241,.3);border-radius:6px;color:#e2e8f0;font-size:12px;padding:5px 8px;min-width:120px;cursor:pointer">' +
        '<option value="" style="background:#1e293b;color:#94a3b8">选择应用</option>' + appOptions +
      '</select>' +
      '<input id="ref-new-val-' + safeId + '" type="text" placeholder="用户名或电话" ' +
        'style="flex:1;background:#1e293b;border:1px solid rgba(99,102,241,.3);border-radius:6px;padding:5px 8px;color:#e2e8f0;font-size:12px;outline:none">' +
      '<button onclick="_ttAddContact(\'' + deviceId + '\',\'' + safeId + '\',\'' + indId + '\')" ' +
        'style="background:#6366f1;border:none;border-radius:6px;color:#fff;font-size:12px;padding:5px 10px;cursor:pointer;font-weight:500">确认</button>' +
      '<button onclick="_ttCancelAddContact(\'' + deviceId + '\',\'' + safeId + '\',\'' + indId + '\')" ' +
        'style="background:none;border:none;cursor:pointer;color:#94a3b8;font-size:15px;padding:2px 4px">\u2715</button>' +
    '</div>';
}

function _ttCancelAddContact(deviceId, safeId, indId) {
  const addDiv = document.getElementById('ref-add-' + safeId);
  if (!addDiv) return;
  addDiv.innerHTML =
    '<button onclick="_ttShowAddContact(\'' + deviceId + '\',\'' + safeId + '\',\'' + indId + '\')" ' +
      'style="background:rgba(99,102,241,.1);border:1px solid rgba(99,102,241,.3);border-radius:5px;padding:4px 12px;color:#818cf8;font-size:11px;cursor:pointer">+ 添加应用</button>' +
    ' <button onclick="_ttApplyContactsAll(\'' + deviceId + '\',\'' + indId + '\')" ' +
      'title="同步到所有设备" ' +
      'style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);border-radius:5px;padding:4px 12px;color:#f59e0b;font-size:11px;cursor:pointer">\u2197 同步所有</button>';
}

// 确认添加新联系方式
async function _ttAddContact(deviceId, safeId, indId) {
  const appEl = document.getElementById('ref-new-app-' + safeId);
  const valEl = document.getElementById('ref-new-val-' + safeId);
  if (!appEl || !valEl) return;
  let app = appEl.value;
  if (app === '_custom') {
    app = await ocPrompt('应用名称','',{message:'如 Line、TikTok、Viber',inputPlaceholder:'应用名称'});
    if (!app) return;
    app = app.toLowerCase().trim();
  }
  const val = (valEl.value || '').trim();
  if (!app || !val) return;
  await _ttSaveRef(deviceId, app, val, null, indId);
  // 更新缓存并重新渲染面板
  if (window._ttDevicesCache && window._ttDevicesCache[deviceId]) {
    if (!window._ttDevicesCache[deviceId].contacts) window._ttDevicesCache[deviceId].contacts = {};
    window._ttDevicesCache[deviceId].contacts[app] = val;
  }
  // 重新打开面板以刷新联系方式列表
  setTimeout(function() { _ttOpenPanel(deviceId); }, 300);
}

// 将当前设备的联系方式批量应用到所有设备
async function _ttApplyContactsAll(deviceId, indId) {
  const dev = (window._ttDevicesCache || {})[deviceId] || {};
  const contacts = dev.contacts || {};
  const hasAny = Object.values(contacts).some(function(v) { return !!v; });
  if (!hasAny) {
    _toast('此设备未配置任何联系方式', 'error');
    return;
  }
  const names = Object.entries(contacts).filter(function(e){ return !!e[1]; })
    .map(function(e){ return e[0] + ':' + e[1]; }).join(', ');
  if (!(await ocDialog({title:'应用到所有设备',message:'将 [' + names + '] 应用到所有设备？<br>这会覆盖其他设备的同名联系方式字段。',type:'warning',confirmText:'确认应用',cancelText:'取消'}))) return;
  const ind = document.getElementById(indId);
  if (ind) { ind.textContent = '应用中...'; ind.style.color = '#94a3b8'; }
  try {
    const body = Object.assign({ all: true }, contacts);
    const r = await fetch('/tiktok/referral-config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify(body)
    });
    const j = r.ok ? await r.json() : null;
    if (r.ok && j && j.ok) {
      const n = j.updated || '?';
      if (ind) { ind.textContent = '已应用到 ' + n + ' 台设备 ✓'; ind.style.color = '#22c55e'; setTimeout(function() { if (ind) ind.textContent = ''; }, 3000); }
      _toast('联系方式已应用到 ' + n + ' 台设备', 'success');
      // 更新本地缓存中所有设备的联系方式
      if (window._ttDevicesCache) {
        Object.values(window._ttDevicesCache).forEach(function(d) {
          if (!d.contacts) d.contacts = {};
          Object.entries(contacts).forEach(function(e) {
            if (e[1]) { d.contacts[e[0]] = e[1]; d[e[0]] = e[1]; }
          });
          d.configured = Object.values(d.contacts).some(function(v){ return !!v; });
        });
      }
    } else {
      if (ind) { ind.textContent = '应用失败'; ind.style.color = '#f87171'; }
      _toast('应用失败', 'error');
    }
  } catch(e) {
    if (ind) { ind.textContent = '网络错误'; ind.style.color = '#f87171'; }
    _toast('网络错误', 'error');
  }
}

function _ttSetActionResult(deviceId, msg, color) {
  const safeId = (deviceId || '').replace(/[^a-zA-Z0-9]/g, '_');
  const el = document.getElementById('tt-act-result-' + safeId);
  if (el) { el.textContent = msg; el.style.color = color || 'var(--text-muted)'; }
}

function _ttBtnState(btn, text, disabled) {
  if (!btn) return;
  btn.textContent = text;
  btn.disabled = !!disabled;
  btn.style.opacity = disabled ? '0.6' : '';
}

async function _ttDevLaunch(deviceId, btn) {
  _ttBtnState(btn, '启动中...', true);
  _ttSetActionResult(deviceId, '', '');
  try {
    const r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/launch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
      body: JSON.stringify({})
    }).then(function(r) { return r.json(); });

    if (r.ok !== false) {
      _ttBtnState(btn, '▶ 启动流程', false);
      const taskInfo = r.task_id ? ' (任务 #' + r.task_id.substring(0,8) + ')' : '';
      _ttSetActionResult(deviceId, '✓ 已提交' + taskInfo + ' · ' + (r.message || '流程启动'), '#22c55e');
      if (typeof showToast === 'function') showToast(r.message || '流程已启动', 'success');
    } else {
      _ttBtnState(btn, '▶ 启动流程', false);
      _ttSetActionResult(deviceId, '✗ ' + (r.error || '启动失败'), '#f87171');
    }
  } catch(e) {
    _ttBtnState(btn, '▶ 启动流程', false);
    _ttSetActionResult(deviceId, '✗ 网络错误: ' + e.message, '#f87171');
  }
}

// ════════════════════════════════════════════════════════
// P0: 流程配置器 (Flow Configurator)
// 深度优化：智能预选 + 快速预设 + 设备角色 + localStorage 持久化
// ════════════════════════════════════════════════════════

/** 与 config/audience_presets.yaml 对齐；有值时 _ttFlowGeoInjectParams 不再覆盖国家/语言 */
const _TT_AUDIENCE_OPTIONS_FALLBACK = [
  { value: '', label: '（无，使用 GEO 注入）' },
  { value: 'italy_male_30p', label: '意大利 30+ 男性' },
  { value: 'usa_broad', label: '美国 宽筛选' },
  { value: 'italy_light_inbox', label: '意大利 轻量收件箱' },
];
const _TT_AUDIENCE_PARAM = {
  key: 'audience_preset',
  label: '人群预设',
  type: 'select',
  def: '',
  options: _TT_AUDIENCE_OPTIONS_FALLBACK.slice(),
};

var _ttFlowAudienceClientCache = { etag: null, list: null };
if (typeof window !== 'undefined') {
  window._ttFlowAudienceClientCacheReset = function () {
    _ttFlowAudienceClientCache = { etag: null, list: null };
  };
  /** 与 _ocSeedAudiencePresetsCache 对称：无 platforms 或需与 GET if_etag 分支一致时使用 */
  window._ttFlowAudienceClientSeed = function (list, etag) {
    _ttFlowAudienceClientCache = {
      list: Array.isArray(list) ? list.slice() : [],
      etag: etag != null && etag !== '' ? String(etag) : null,
    };
  };
}

/**
 * 从 GET /task-params/audience-presets 拉取预设填充下拉（与 platforms.js 缓存同源优先）。
 * overview.js 先于 platforms.js 加载时走 api()+if_etag；失败则保留 FALLBACK。
 */
async function _ttEnsureAudiencePresetOptions() {
  var list = null;
  try {
    if (typeof window !== 'undefined' && typeof window._ocLoadAudiencePresetsCached === 'function') {
      list = await window._ocLoadAudiencePresetsCached(false);
    } else if (typeof api === 'function') {
      var url = '/task-params/audience-presets';
      if (_ttFlowAudienceClientCache.etag) {
        url += '?if_etag=' + encodeURIComponent(_ttFlowAudienceClientCache.etag);
      }
      var r = await api('GET', url);
      if (r && r.unchanged && _ttFlowAudienceClientCache.list) {
        list = _ttFlowAudienceClientCache.list;
      } else {
        list = r && r.presets ? r.presets : [];
        _ttFlowAudienceClientCache = {
          etag: r && r.etag ? String(r.etag) : null,
          list: list,
        };
      }
    }
  } catch (e) {
    console.warn('[tt-flow] audience presets', e);
    return;
  }
  if (!Array.isArray(list) || !list.length) {
    return;
  }
  var opts = [{ value: '', label: '（无，使用 GEO 注入）' }];
  var seen = {};
  list.forEach(function (p) {
    if (!p || !p.id || seen[p.id]) return;
    seen[p.id] = true;
    var lab = p.label || p.id;
    if (p.description && String(p.description).length < 80) {
      lab = lab + ' — ' + String(p.description);
    }
    opts.push({ value: p.id, label: lab });
  });
  if (opts.length > 1) {
    _TT_AUDIENCE_PARAM.options = opts;
  }
}

/** 流程弹窗：POST 重载 + 双端种子 + 重绘（避免多余 GET） */
async function _ttFlowRefreshAudiencePresets() {
  var btn = document.querySelector('button.ttf-head-refresh-audience');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '…';
  }
  try {
    if (typeof window._ocReloadAndSeedAudiencePresets === 'function') {
      await window._ocReloadAndSeedAudiencePresets();
    } else if (typeof api === 'function') {
      var presets = [];
      var etag = '';
      var pr = await api('POST', '/task-params/reload-audience-presets', {});
      presets = (pr && pr.presets) ? pr.presets : [];
      etag = (pr && pr.etag) ? String(pr.etag) : '';
      if (presets.length && etag) {
        if (typeof window._ocSeedAudiencePresetsCache === 'function') {
          window._ocSeedAudiencePresetsCache(presets, etag);
        }
        if (typeof window._ttFlowAudienceClientSeed === 'function') {
          window._ttFlowAudienceClientSeed(presets, etag);
        }
      } else if (typeof window._ocInvalidateAudiencePresetsCache === 'function') {
        window._ocInvalidateAudiencePresetsCache();
      } else if (typeof window._ttFlowAudienceClientCacheReset === 'function') {
        window._ttFlowAudienceClientCacheReset();
      }
    }
    await _ttEnsureAudiencePresetOptions(false);
    _ttFlowRender();
    if (typeof showToast === 'function') showToast('人群预设列表已刷新', 'success');
  } catch (e) {
    console.warn('[tt-flow] refresh audience', e);
    if (typeof showToast === 'function') showToast('刷新失败', 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '↻ 人群预设';
    }
  }
}

const _TT_FLOW_STEPS_DEF = [
  { id:'warmup',      icon:'🌱', name:'账号预热',   desc:'浏览推荐流，根据目标市场过滤内容', est:30,
    type:'tiktok_warmup',
    params:[_TT_AUDIENCE_PARAM, {key:'duration_minutes',label:'时长(分钟)',type:'number',def:30,min:10,max:120}]
  },
  { id:'follow',      icon:'👤', name:'关注用户',   desc:'按目标市场筛选并关注目标用户', est:15,
    type:'tiktok_follow',
    params:[
      _TT_AUDIENCE_PARAM,
      {key:'max_follows',label:'关注数量',type:'number',def:30,min:5,max:100}
    ]
  },
  { id:'inbox',       icon:'📬', name:'检查收件箱', desc:'AI智能读取并以目标语言自动回复', est:10,
    type:'tiktok_check_inbox',
    params:[
      _TT_AUDIENCE_PARAM,
      {key:'max_conversations',label:'检查数量',type:'number',def:50,min:10,max:100},
      {key:'auto_reply',label:'AI自动回复',type:'checkbox',def:true}
    ]
  },
  { id:'followbacks', icon:'💬', name:'回关私信',   desc:'对回关用户发起目标语言开场对话', est:10,
    type:'tiktok_check_and_chat_followbacks',
    params:[_TT_AUDIENCE_PARAM, {key:'max_chats',label:'私信数量',type:'number',def:10,min:3,max:50}]
  },
  { id:'pitch',       icon:'💰', name:'发引流话术', desc:'向热点线索发TG/WA联系方式', est:5,
    type:'__pitch__',
    params:[{key:'max_pitch',label:'发送数量',type:'number',def:5,min:1,max:20}]
  },
  { id:'kwsearch',    icon:'🔍', name:'关键词搜索', desc:'搜索目标市场关键词找精准用户，评论预热+关注', est:12,
    type:'tiktok_keyword_search',
    params:[
      _TT_AUDIENCE_PARAM,
      {key:'max_follows',label:'关注数量',type:'number',def:20,min:5,max:50},
      {key:'comment_warmup',label:'评论预热',type:'checkbox',def:true}
    ]
  },
  { id:'live',        icon:'📺', name:'直播互动',   desc:'进入目标市场直播间发评论，关注活跃观众', est:10,
    type:'tiktok_live_engage',
    params:[
      _TT_AUDIENCE_PARAM,
      {key:'max_live_rooms',label:'直播间数量',type:'number',def:3,min:1,max:8},
      {key:'comments_per_room',label:'评论数/间',type:'number',def:2,min:1,max:5},
      {key:'follow_active_viewers',label:'关注活跃观众',type:'checkbox',def:true}
    ]
  }
  ,
  { id:'cmtreplies', icon:'💬', name:'评论回复DM', desc:'检查评论回复通知，对回复者发私信引流', est:8,
    type:'tiktok_check_comment_replies',
    params:[
      _TT_AUDIENCE_PARAM,
      {key:'max_replies',label:'最大处理数',type:'number',def:20,min:5,max:50}
    ]
  }
];

// 动态时间计算模型：每个步骤根据参数实时算出耗时（分钟）
var _TT_STEP_TIME_CALC = {
  warmup:      function(p){ return p.duration_minutes || 30; },
  follow:      function(p){ return Math.ceil((p.max_follows || 30) * 0.5); },
  inbox:       function(p){ return Math.ceil((p.max_conversations || 50) * 0.2); },
  followbacks: function(p){ return Math.ceil((p.max_chats || 10) * 1.0); },
  pitch:       function(p){ return Math.ceil((p.max_pitch || 5) * 1.0); },
  kwsearch:    function(p){ return Math.ceil((p.max_follows || 20) * 0.6); },
  live:        function(p){ return Math.ceil((p.max_live_rooms||3)*3 + (p.max_live_rooms||3)*(p.comments_per_room||2)*0.5); },
  cmtreplies:  function(p){ return Math.ceil((p.max_replies || 20) * 0.4); },
};

function _ttCalcStepTime(stepId, params) {
  var fn = _TT_STEP_TIME_CALC[stepId];
  return fn ? fn(params || {}) : 10;
}

// 步骤分组（视觉分层：基础操作 / 主动获客 / 互动转化）
var _TT_STEP_GROUPS = [
  { label:'基础操作', ids:['warmup','follow'] },
  { label:'AI智能处理', ids:['inbox','followbacks','pitch'] },
  { label:'主动获客', ids:['kwsearch','live','cmtreplies'] },
];

// 风险阈值 + 业务影响预估
var _TT_STEP_RISK = {
  follow:   {key:'max_follows', safe:30, warn:50, danger:80, unit:'人',
             impact:function(v){ return '≈ ' + Math.ceil(v*0.18) + '-' + Math.ceil(v*0.28) + ' 条线索'; }},
  kwsearch: {key:'max_follows', safe:20, warn:35, danger:50, unit:'人',
             impact:function(v){ return '≈ ' + Math.ceil(v*0.15) + '-' + Math.ceil(v*0.25) + ' 精准线索'; }},
  inbox:    {key:'max_conversations', safe:50, warn:80, danger:100, unit:'条',
             impact:function(v){ return '≈ ' + Math.ceil(v*0.3) + ' 条有效对话'; }},
  followbacks:{key:'max_chats', safe:10, warn:30, danger:50, unit:'人',
             impact:function(v){ return '≈ ' + Math.ceil(v*0.4) + ' 人回复'; }},
  live:     {key:'max_live_rooms', safe:3, warn:5, danger:8, unit:'间',
             impact:function(v){ return '≈ ' + Math.ceil(v*4) + '-' + Math.ceil(v*8) + ' 新关注'; }},
  cmtreplies:{key:'max_replies', safe:20, warn:35, danger:50, unit:'条',
             impact:function(v){ return '≈ ' + Math.ceil(v*0.2) + ' 人转DM'; }},
  pitch:    {key:'max_pitch', safe:5, warn:12, danger:20, unit:'条',
             impact:function(v){ return '≈ ' + Math.ceil(v*0.6) + ' 人加TG/WA'; }},
  warmup:   {key:'duration_minutes', safe:30, warn:60, danger:90, unit:'分钟',
             impact:function(v){ return v >= 30 ? '算法权重提升' : '轻度预热'; }},
};

const _TT_FLOW_PRESETS = {
  warmup:  {name:'🌱 养号模式', steps:['warmup'],                                        label:'养号机', color:'#22c55e', desc:'浏览推荐流，建立目标市场内容偏好', detail:'适合新号或算法分偏低'},
  follow:  {name:'👤 获客模式', steps:['follow','inbox'],                                 label:'获客机', color:'#60a5fa', desc:'精准关注目标用户 + AI智能回复', detail:'快速触达潜在客户'},
  full:    {name:'⚡ 全流程',   steps:['warmup','follow','inbox','followbacks'],           label:'全流程', color:'#a5b4fc', desc:'养号→关注→收件→私信，完整闭环', detail:'日常首选，覆盖完整漏斗'},
  hunter:  {name:'🎯 猎客模式', steps:['kwsearch','live','inbox'],                        label:'猎客机', color:'#f59e0b', desc:'关键词搜索 + 直播互动 + 收件', detail:'主动出击找精准客户'},
  turbo:   {name:'🚀 涡轮全程', steps:['warmup','kwsearch','live','inbox','followbacks'], label:'涡轮机', color:'#ef4444', desc:'全部策略组合，最大化获客覆盖', detail:'高负载模式，适合成熟号'},
};

// ════════════════════════════════════════════════════════
// GEO: 全球市场数据库（60+国家 · 8大区域 · 20种语言）
// ════════════════════════════════════════════════════════
const _GEO_REGIONS = [
  { key:'sea',       name:'🌏 东南亚',    countries:[
    {code:'PH',flag:'🇵🇭',zh:'菲律宾',  lang:['tl','en']},
    {code:'ID',flag:'🇮🇩',zh:'印尼',    lang:['id']},
    {code:'MY',flag:'🇲🇾',zh:'马来西亚',lang:['ms','en']},
    {code:'TH',flag:'🇹🇭',zh:'泰国',    lang:['th']},
    {code:'VN',flag:'🇻🇳',zh:'越南',    lang:['vi']},
    {code:'SG',flag:'🇸🇬',zh:'新加坡',  lang:['en','zh']},
    {code:'MM',flag:'🇲🇲',zh:'缅甸',    lang:['my']},
    {code:'KH',flag:'🇰🇭',zh:'柬埔寨',  lang:['km']},
  ]},
  { key:'mideast',   name:'🕌 中东',      countries:[
    {code:'AE',flag:'🇦🇪',zh:'阿联酋',  lang:['ar']},
    {code:'SA',flag:'🇸🇦',zh:'沙特',    lang:['ar']},
    {code:'QA',flag:'🇶🇦',zh:'卡塔尔',  lang:['ar']},
    {code:'KW',flag:'🇰🇼',zh:'科威特',  lang:['ar']},
    {code:'BH',flag:'🇧🇭',zh:'巴林',    lang:['ar']},
    {code:'OM',flag:'🇴🇲',zh:'阿曼',    lang:['ar']},
    {code:'IL',flag:'🇮🇱',zh:'以色列',  lang:['he','ar']},
    {code:'EG',flag:'🇪🇬',zh:'埃及',    lang:['ar']},
    {code:'JO',flag:'🇯🇴',zh:'约旦',    lang:['ar']},
  ]},
  { key:'latam',     name:'🌎 拉丁美洲',  countries:[
    {code:'BR',flag:'🇧🇷',zh:'巴西',    lang:['pt']},
    {code:'MX',flag:'🇲🇽',zh:'墨西哥',  lang:['es']},
    {code:'CO',flag:'🇨🇴',zh:'哥伦比亚',lang:['es']},
    {code:'AR',flag:'🇦🇷',zh:'阿根廷',  lang:['es']},
    {code:'CL',flag:'🇨🇱',zh:'智利',    lang:['es']},
    {code:'PE',flag:'🇵🇪',zh:'秘鲁',    lang:['es']},
    {code:'VE',flag:'🇻🇪',zh:'委内瑞拉',lang:['es']},
    {code:'EC',flag:'🇪🇨',zh:'厄瓜多尔',lang:['es']},
  ]},
  { key:'english',   name:'🌐 英语区',    countries:[
    {code:'US',flag:'🇺🇸',zh:'美国',    lang:['en']},
    {code:'GB',flag:'🇬🇧',zh:'英国',    lang:['en']},
    {code:'CA',flag:'🇨🇦',zh:'加拿大',  lang:['en','fr']},
    {code:'AU',flag:'🇦🇺',zh:'澳大利亚',lang:['en']},
    {code:'NZ',flag:'🇳🇿',zh:'新西兰',  lang:['en']},
    {code:'IE',flag:'🇮🇪',zh:'爱尔兰',  lang:['en']},
  ]},
  { key:'europe',    name:'🏰 欧洲',      countries:[
    {code:'IT',flag:'🇮🇹',zh:'意大利',  lang:['it']},
    {code:'DE',flag:'🇩🇪',zh:'德国',    lang:['de']},
    {code:'FR',flag:'🇫🇷',zh:'法国',    lang:['fr']},
    {code:'ES',flag:'🇪🇸',zh:'西班牙',  lang:['es']},
    {code:'PT',flag:'🇵🇹',zh:'葡萄牙',  lang:['pt']},
    {code:'NL',flag:'🇳🇱',zh:'荷兰',    lang:['nl']},
    {code:'PL',flag:'🇵🇱',zh:'波兰',    lang:['pl']},
    {code:'RO',flag:'🇷🇴',zh:'罗马尼亚',lang:['ro']},
    {code:'RU',flag:'🇷🇺',zh:'俄罗斯',  lang:['ru']},
    {code:'TR',flag:'🇹🇷',zh:'土耳其',  lang:['tr']},
    {code:'UA',flag:'🇺🇦',zh:'乌克兰',  lang:['uk']},
  ]},
  { key:'africa',    name:'🌍 非洲',      countries:[
    {code:'NG',flag:'🇳🇬',zh:'尼日利亚',lang:['en']},
    {code:'GH',flag:'🇬🇭',zh:'加纳',    lang:['en']},
    {code:'KE',flag:'🇰🇪',zh:'肯尼亚',  lang:['sw','en']},
    {code:'ZA',flag:'🇿🇦',zh:'南非',    lang:['en']},
    {code:'TZ',flag:'🇹🇿',zh:'坦桑尼亚',lang:['sw']},
    {code:'ET',flag:'🇪🇹',zh:'埃塞俄比亚',lang:['am']},
    {code:'UG',flag:'🇺🇬',zh:'乌干达',  lang:['en']},
    {code:'SN',flag:'🇸🇳',zh:'塞内加尔',lang:['fr']},
  ]},
  { key:'southasia', name:'🕌 南亚',      countries:[
    {code:'IN',flag:'🇮🇳',zh:'印度',    lang:['hi','en']},
    {code:'PK',flag:'🇵🇰',zh:'巴基斯坦',lang:['ur']},
    {code:'BD',flag:'🇧🇩',zh:'孟加拉',  lang:['bn']},
    {code:'LK',flag:'🇱🇰',zh:'斯里兰卡',lang:['si','ta']},
  ]},
  { key:'eastasia',  name:'🗾 东亚',      countries:[
    {code:'JP',flag:'🇯🇵',zh:'日本',    lang:['ja']},
    {code:'KR',flag:'🇰🇷',zh:'韩国',    lang:['ko']},
    {code:'TW',flag:'🇹🇼',zh:'台湾',    lang:['zh']},
  ]},
];

const _GEO_LANGUAGES = [
  {code:'tl',zh:'菲律宾语',flag:'🇵🇭',en:'Tagalog'},
  {code:'id',zh:'印尼语',  flag:'🇮🇩',en:'Bahasa'},
  {code:'ms',zh:'马来语',  flag:'🇲🇾',en:'Melayu'},
  {code:'th',zh:'泰语',    flag:'🇹🇭',en:'Thai'},
  {code:'vi',zh:'越南语',  flag:'🇻🇳',en:'Viet'},
  {code:'ar',zh:'阿拉伯语',flag:'🇸🇦',en:'Arabic'},
  {code:'en',zh:'英语',    flag:'🇺🇸',en:'English'},
  {code:'es',zh:'西班牙语',flag:'🇲🇽',en:'Español'},
  {code:'pt',zh:'葡萄牙语',flag:'🇧🇷',en:'Português'},
  {code:'fr',zh:'法语',    flag:'🇫🇷',en:'Français'},
  {code:'de',zh:'德语',    flag:'🇩🇪',en:'Deutsch'},
  {code:'it',zh:'意大利语',flag:'🇮🇹',en:'Italiano'},
  {code:'hi',zh:'印地语',  flag:'🇮🇳',en:'Hindi'},
  {code:'ur',zh:'乌尔都语',flag:'🇵🇰',en:'Urdu'},
  {code:'ru',zh:'俄语',    flag:'🇷🇺',en:'Русский'},
  {code:'ko',zh:'韩语',    flag:'🇰🇷',en:'한국어'},
  {code:'ja',zh:'日语',    flag:'🇯🇵',en:'日本語'},
  {code:'sw',zh:'斯瓦希里',flag:'🇰🇪',en:'Swahili'},
  {code:'zh',zh:'中文',    flag:'🇨🇳',en:'中文'},
  {code:'tr',zh:'土耳其语',flag:'🇹🇷',en:'Türkçe'},
];

// 国家码 → 后端遗留名称（向后兼容 tiktok_follow）
const _GEO_CODE_TO_LEGACY = {
  'IT':'italy','US':'usa','GB':'uk','FR':'france','DE':'germany',
  'ES':'spain','BR':'brazil','JP':'japan','KR':'korea','AU':'australia',
  'CA':'canada','MX':'mexico','AR':'argentina','PH':'philippines',
  'ID':'indonesia','MY':'malaysia','TH':'thailand','VN':'vietnam',
  'AE':'uae','SA':'saudi_arabia','QA':'qatar','NG':'nigeria',
  'KE':'kenya','IN':'india','SG':'singapore','TW':'taiwan','CO':'colombia',
};

// 商务/可选语言推荐（主要语言从 _GEO_REGIONS 的 lang[0] 自动取）
const _GEO_BIZ_LANGS = {
  'IT':[{code:'en',note:'商务通用'}],
  'DE':[{code:'en',note:'商务通用'}],
  'FR':[{code:'en',note:'商务通用'}],
  'ES':[{code:'en',note:'商务通用'}],
  'PT':[{code:'en',note:'商务通用'}],
  'NL':[{code:'en',note:'商务通用'}],
  'PL':[{code:'en',note:'商务通用'}],
  'TR':[{code:'en',note:'商务通用'}],
  'RU':[{code:'en',note:'商务通用'}],
  'RO':[{code:'en',note:'商务通用'}],
  'UA':[{code:'en',note:'商务通用'}],
  'AE':[{code:'en',note:'商务通用'}],
  'SA':[{code:'en',note:'商务通用'}],
  'QA':[{code:'en',note:'商务通用'}],
  'KW':[{code:'en',note:'商务通用'}],
  'EG':[{code:'en',note:'商务通用'}],
  'JO':[{code:'en',note:'商务通用'}],
  'IL':[{code:'en',note:'商务通用'},{code:'ar',note:'阿拉伯裔'}],
  'IN':[{code:'en',note:'商务通用'}],
  'PK':[{code:'en',note:'商务通用'}],
  'BD':[{code:'en',note:'商务通用'}],
  'PH':[{code:'en',note:'商务通用'}],
  'MY':[{code:'en',note:'商务通用'}],
  'SG':[{code:'zh',note:'华人社区'},{code:'ms',note:'马来社区'}],
  'CA':[{code:'fr',note:'魁北克'}],
  'BR':[{code:'es',note:'西语邻国客户'}],
  'MX':[{code:'en',note:'商务通用'}],
  'CO':[{code:'en',note:'商务通用'}],
  'NG':[{code:'ha',note:'北方豪萨语'}],
  'KE':[{code:'en',note:'商务通用'}],
  'ZA':[{code:'af',note:'南非荷兰语'}],
  'JP':[{code:'en',note:'商务通用'}],
  'KR':[{code:'en',note:'商务通用'}],
};

// 最近使用国家（localStorage）
function _geoGetRecent() {
  try { return JSON.parse(localStorage.getItem('tt_geo_recent') || '[]'); }
  catch(e) { return []; }
}
function _geoAddRecent(code) {
  var list = _geoGetRecent().filter(function(c){ return c !== code; });
  list.unshift(code);
  if (list.length > 6) list = list.slice(0, 6);
  try { localStorage.setItem('tt_geo_recent', JSON.stringify(list)); } catch(e) {}
}

// 英文名搜索映射（弥补 _GEO_CODE_TO_LEGACY 不全的问题）
var _GEO_EN_NAMES = {
  'IT':'italy','US':'united states usa america','GB':'united kingdom uk england','FR':'france',
  'DE':'germany','ES':'spain','PT':'portugal','NL':'netherlands holland','PL':'poland',
  'RO':'romania','GR':'greece','CZ':'czech','HU':'hungary','SE':'sweden','NO':'norway',
  'DK':'denmark','FI':'finland','IE':'ireland','AT':'austria','CH':'switzerland',
  'BE':'belgium','RU':'russia','UA':'ukraine','TR':'turkey',
  'JP':'japan','KR':'korea','CN':'china','TW':'taiwan','HK':'hong kong',
  'IN':'india','PK':'pakistan','BD':'bangladesh','LK':'sri lanka',
  'PH':'philippines','ID':'indonesia','MY':'malaysia','TH':'thailand','VN':'vietnam',
  'SG':'singapore','MM':'myanmar','KH':'cambodia',
  'AU':'australia','NZ':'new zealand',
  'BR':'brazil','MX':'mexico','AR':'argentina','CO':'colombia','CL':'chile',
  'PE':'peru','VE':'venezuela','EC':'ecuador',
  'SA':'saudi arabia','AE':'uae emirates','QA':'qatar','KW':'kuwait','BH':'bahrain',
  'OM':'oman','EG':'egypt','IL':'israel','JO':'jordan',
  'NG':'nigeria','KE':'kenya','GH':'ghana','ZA':'south africa','TZ':'tanzania',
  'ET':'ethiopia','UG':'uganda','SN':'senegal','CA':'canada',
};

// 搜索状态
var _ttGeoSearchQuery = '';

// 根据选中国家推断语言集合
function _geoAutoLangs(codes) {
  const allC = _geoAllCountries();
  const seen = {};
  codes.forEach(function(code) {
    const c = allC[code];
    if (c) c.lang.forEach(function(l){ seen[l]=true; });
  });
  return Object.keys(seen);
}

function _geoAllCountries() {
  const m = {};
  _GEO_REGIONS.forEach(function(r){ r.countries.forEach(function(c){ m[c.code]=c; }); });
  return m;
}

// ── GEO 状态 ──
let _ttFlowGeo = {countries:[], languages:[]};
let _ttGeoPickerOpen = false;

function _ttGeoLoadFromStorage(deviceId) {
  try {
    var raw = localStorage.getItem('tt_geo_' + deviceId);
    var d = raw ? JSON.parse(raw) : null;
    var c = (d && Array.isArray(d.countries)) ? d.countries : [];
    // 向后兼容：旧数据可能多选，只保留首个
    if (c.length > 1) c = [c[0]];
    _ttFlowGeo = {
      countries: c,
      languages: (d && Array.isArray(d.languages)) ? d.languages : []
    };
  } catch(e) { _ttFlowGeo = {countries:[], languages:[]}; }
}

function _ttGeoSaveToStorage(deviceId) {
  try { localStorage.setItem('tt_geo_' + deviceId, JSON.stringify(_ttFlowGeo)); } catch(e) {}
  // P4-3: 异步同步到定时任务（非阻塞）
  if (deviceId) _ttSyncGeoToScheduledJobs(deviceId);
}

// 单选国家（核心）：选中后自动推荐语言、关闭 picker、记录最近
function _ttGeoSelectCountry(code) {
  _ttFlowGeo.countries = code ? [code] : [];
  _ttFlowGeo.languages = _geoAutoLangs(_ttFlowGeo.countries);
  if (code) _geoAddRecent(code);
  _ttGeoPickerOpen = false;
  _ttGeoSearchQuery = '';
  _ttFlowRenderGeo();
}

// 切换可选语言（商务语言等）
function _ttGeoToggleLang(langCode) {
  var idx = _ttFlowGeo.languages.indexOf(langCode);
  if (idx >= 0) _ttFlowGeo.languages.splice(idx, 1);
  else _ttFlowGeo.languages.push(langCode);
  _ttFlowRenderGeo();
}

function _ttGeoClear() {
  _ttFlowGeo = {countries:[], languages:[]};
  _ttGeoPickerOpen = false;
  _ttGeoSearchQuery = '';
  _ttFlowRenderGeo();
}

function _ttGeoTogglePicker() {
  _ttGeoPickerOpen = !_ttGeoPickerOpen;
  _ttGeoSearchQuery = '';
  _ttFlowRenderGeo();
  if (_ttGeoPickerOpen) {
    setTimeout(function(){
      var inp = document.getElementById('ttg-search-input');
      if (inp) inp.focus();
      // 如果已有选中国家，滚动到其所在区域
      var selCode = _ttFlowGeo.countries.length > 0 ? _ttFlowGeo.countries[0] : null;
      if (selCode) {
        var selBtn = document.querySelector('.ttg-country-btn.sel');
        if (selBtn) selBtn.scrollIntoView({block:'center', behavior:'smooth'});
      }
    }, 120);
  }
}

function _ttGeoOnSearch(val) {
  _ttGeoSearchQuery = (val || '').trim().toLowerCase();
  _ttFlowRenderGeo();
}

function _ttFlowRenderGeo() {
  var el = document.getElementById('tt-geo-section');
  if (!el) return;
  el.className = 'tt-geo-section';
  var allC = _geoAllCountries();
  var selCode = _ttFlowGeo.countries.length > 0 ? _ttFlowGeo.countries[0] : null;
  var selCountry = selCode ? allC[selCode] : null;
  var h = '';

  // ── 无选中：大按钮 ──
  if (!selCountry) {
    if (!_ttGeoPickerOpen) {
      h += '<button class="ttg-empty-btn" onclick="_ttGeoTogglePicker()">🌍 选择目标市场</button>';
    }
  } else {
    // ── 已选中：国家卡片 ──
    var tz = _geoLocalTime(selCode);
    h += '<div class="ttg-selected">';
    h += '<span class="ttg-sel-flag">' + selCountry.flag + '</span>';
    h += '<div class="ttg-sel-info">';
    h += '<div class="ttg-sel-name">' + selCountry.zh + '</div>';
    h += '<div class="ttg-sel-meta">';
    // 找到所属区域
    var regionName = '';
    for (var ri = 0; ri < _GEO_REGIONS.length; ri++) {
      for (var ci = 0; ci < _GEO_REGIONS[ri].countries.length; ci++) {
        if (_GEO_REGIONS[ri].countries[ci].code === selCode) { regionName = _GEO_REGIONS[ri].name; break; }
      }
      if (regionName) break;
    }
    h += '<span>' + selCode + '</span>';
    if (regionName) h += '<span>· ' + regionName + '</span>';
    if (tz) h += '<span>· ' + tz.dot + ' 当地 ' + tz.time + '</span>';
    h += '</div></div>';
    h += '<div class="ttg-sel-actions">';
    h += '<button class="ttg-sel-btn" onclick="_ttGeoTogglePicker()">更换</button>';
    h += '<button class="ttg-sel-btn danger" onclick="_ttGeoClear()">✕</button>';
    h += '</div></div>';

    // ── 语言推荐 ──
    h += _ttGeoRenderLangs(selCode, selCountry);
  }

  // ── Picker（搜索+最近+区域列表）──
  if (_ttGeoPickerOpen) {
    h += '<div class="ttg-picker open">';
    h += '<input class="ttg-search" id="ttg-search-input" type="text" placeholder="搜索国家（中文/英文/代码）..." ' +
      'oninput="_ttGeoOnSearch(this.value)" value="' + (_ttGeoSearchQuery || '') + '">';
    h += '<div class="ttg-picker-scroll">';

    var query = _ttGeoSearchQuery;

    // 最近使用（无搜索时显示）
    if (!query) {
      var recent = _geoGetRecent();
      if (recent.length > 0) {
        h += '<span class="ttg-section-label">📌 最近使用</span>';
        h += '<div class="ttg-recent-list">';
        for (var r = 0; r < recent.length; r++) {
          var rc = allC[recent[r]];
          if (!rc) continue;
          var rSel = (selCode === recent[r]);
          h += '<span class="ttg-country-btn' + (rSel ? ' sel' : '') + '" onclick="_ttGeoSelectCountry(\'' + recent[r] + '\')">' + rc.flag + ' ' + rc.zh + '</span>';
        }
        h += '</div>';
      }
    }

    // 区域列表（支持搜索过滤）
    for (var i = 0; i < _GEO_REGIONS.length; i++) {
      var reg = _GEO_REGIONS[i];
      var filtered = reg.countries;
      if (query) {
        filtered = reg.countries.filter(function(c) {
          return c.zh.indexOf(query) >= 0 ||
            c.code.toLowerCase().indexOf(query) >= 0 ||
            (_GEO_EN_NAMES[c.code] || '').indexOf(query) >= 0 ||
            (_GEO_CODE_TO_LEGACY[c.code] || '').indexOf(query) >= 0;
        });
      }
      if (!filtered.length) continue;

      h += '<div class="ttg-region-block">';
      h += '<div class="ttg-region-name">' + reg.name + '</div>';
      h += '<div class="ttg-region-grid">';
      for (var j = 0; j < filtered.length; j++) {
        var c = filtered[j];
        var isSel = (selCode === c.code);
        h += '<span class="ttg-country-btn' + (isSel ? ' sel' : '') + '" onclick="_ttGeoSelectCountry(\'' + c.code + '\')">' + c.flag + ' ' + c.zh + '</span>';
      }
      h += '</div></div>';
    }

    // 无搜索结果提示
    if (query) {
      var hasAny = false;
      for (var qi = 0; qi < _GEO_REGIONS.length; qi++) {
        var qr = _GEO_REGIONS[qi];
        var qf = qr.countries.filter(function(c) {
          return c.zh.indexOf(query) >= 0 || c.code.toLowerCase().indexOf(query) >= 0 ||
            (_GEO_EN_NAMES[c.code] || '').indexOf(query) >= 0 || (_GEO_CODE_TO_LEGACY[c.code] || '').indexOf(query) >= 0;
        });
        if (qf.length) { hasAny = true; break; }
      }
      if (!hasAny) {
        h += '<div style="text-align:center;padding:16px;color:var(--text-muted);font-size:13px">😔 未找到 "<b>' + query + '</b>" 相关国家</div>';
      }
    }

    h += '</div></div>';
  }

  el.innerHTML = h;
}

// 渲染语言推荐（主要 + 可选商务语言）
function _ttGeoRenderLangs(countryCode, countryInfo) {
  var h = '<div class="ttg-langs">';
  h += '<span class="ttg-lang-label">语言:</span>';

  // 主要语言（从 lang[0] 取）
  var primaryLangs = countryInfo.lang || [];
  for (var i = 0; i < primaryLangs.length; i++) {
    var pl = _GEO_LANGUAGES.find(function(x){ return x.code === primaryLangs[i]; });
    if (!pl) continue;
    h += '<span class="ttg-lang-chip primary">' + pl.flag + ' ' + pl.zh + ' <span style="font-size:10px;opacity:.7">(推荐)</span></span>';
  }

  // 可选商务语言
  var bizLangs = _GEO_BIZ_LANGS[countryCode] || [];
  for (var j = 0; j < bizLangs.length; j++) {
    var bl = bizLangs[j];
    // 跳过已在主要语言中的
    if (primaryLangs.indexOf(bl.code) >= 0) continue;
    var langInfo = _GEO_LANGUAGES.find(function(x){ return x.code === bl.code; });
    var isActive = _ttFlowGeo.languages.indexOf(bl.code) >= 0;
    var label = langInfo ? langInfo.flag + ' ' + langInfo.zh : bl.code;
    h += '<span class="ttg-lang-chip optional' + (isActive ? ' active' : '') + '" onclick="_ttGeoToggleLang(\'' + bl.code + '\')">' +
      (isActive ? '✓ ' : '+ ') + label + ' <span style="font-size:10px;opacity:.6">(' + bl.note + ')</span></span>';
  }

  h += '</div>';
  return h;
}

// 时区工具
function _geoLocalTime(code) {
  var _TZ = {'PH':8,'ID':7,'MY':8,'TH':7,'VN':7,'SG':8,'TW':8,'JP':9,'KR':9,'MM':6.5,'KH':7,
    'SA':3,'AE':4,'EG':2,'QA':3,'KW':3,'BH':3,'OM':4,'IL':2,'JO':3,
    'BR':-3,'MX':-6,'CO':-5,'AR':-3,'CL':-3,'PE':-5,'VE':-4,'EC':-5,
    'IN':5.5,'PK':5,'BD':6,'LK':5.5,
    'NG':1,'KE':3,'GH':0,'ZA':2,'TZ':3,'ET':3,'UG':3,'SN':0,
    'US':-5,'GB':1,'DE':2,'FR':2,'IT':2,'ES':2,'NL':2,'PL':2,'RO':3,'RU':3,'TR':3,'UA':3,
    'AU':10,'NZ':12,'IE':1,'CA':-5};
  var off = _TZ[code];
  if (off === undefined) return null;
  var d = new Date(Date.now() + off * 3600000);
  var hh = d.getUTCHours(), mm = d.getUTCMinutes();
  var timeStr = String(hh).padStart(2,'0') + ':' + String(mm).padStart(2,'0');
  var dot = (hh >= 19 && hh <= 23) ? '🟢' : (hh >= 7 && hh <= 12) ? '🟡' : '🔴';
  return {time: timeStr, dot: dot, hour: hh};
}

// 将全局 GEO 设置注入步骤参数
function _ttFlowGeoInjectParams(stepType, params) {
  const p = Object.assign({}, params);
  // 已选人群预设时由服务端 merge；此处不再用 GEO 覆盖国家/语言，避免与预设冲突
  if (p.audience_preset) {
    return p;
  }
  const geo = _ttFlowGeo;
  if (!geo.countries.length && !geo.languages.length) return p;

  if (stepType === 'tiktok_warmup') {
    if (geo.countries.length) p.target_countries = geo.countries;
    if (geo.languages.length) p.target_languages = geo.languages;
    p.geo_filter = true;
  } else if (stepType === 'tiktok_follow') {
    if (geo.countries.length) {
      p.target_countries = geo.countries;
      // 向后兼容：首个国家的旧式名称
      p.target_country = _GEO_CODE_TO_LEGACY[geo.countries[0]] || geo.countries[0].toLowerCase();
      p.country = p.target_country;
    }
    if (geo.languages.length) p.target_languages = geo.languages;
  } else if (stepType === 'tiktok_check_inbox' || stepType === 'tiktok_check_and_chat_followbacks') {
    if (geo.languages.length) p.target_languages = geo.languages;
  } else if (stepType === 'tiktok_keyword_search') {
    if (geo.countries.length) p.target_countries = geo.countries;
    if (geo.languages.length) p.target_languages = geo.languages;
  } else if (stepType === 'tiktok_live_engage') {
    if (geo.countries.length) p.target_countries = geo.countries;
    if (geo.languages.length) p.target_languages = geo.languages;
  } else if (stepType === 'tiktok_check_comment_replies') {
    if (geo.languages.length) p.target_languages = geo.languages;
  }
  return p;
}

let _ttFlowDevId = null;
let _ttFlowStates = {};
let _ttFlowCurrentPreset = null;
let _ttFlowBatchMode = false;
let _ttFlowBatchCount = 0;

async function _ttOpenFlowConfig(deviceId) {
  if (!deviceId) return;
  _ttFlowDevId = deviceId;
  if (!document.getElementById('tt-flow-overlay')) _ttBuildFlowModal();

  // 获取设备数据（用于智能预选）
  const dev = (window._ttDevicesCache || {})[deviceId] || {};

  // 加载已保存配置，否则根据设备状态智能预选
  const saved = _ttFlowLoadConfig(deviceId);
  if (saved) {
    _ttFlowStates = saved;
    _ttFlowCurrentPreset = null;
  } else {
    _ttFlowApplySmartDefaults(dev);
  }

  // 加载 GEO 配置
  _ttGeoLoadFromStorage(deviceId);
  _ttGeoPickerOpen = false;

  try {
    await _ttEnsureAudiencePresetOptions();
  } catch (e) {
    console.warn('[tt-flow] audience options', e);
  }

  // 更新标题：单机模式显示设备详情，批量模式显示台数
  var titleEl = document.getElementById('tt-flow-dev-name');
  if (titleEl) {
    if (_ttFlowBatchMode) {
      titleEl.innerHTML = '批量模式 · <b style="color:#f59e0b">' + _ttFlowBatchCount + '</b> 台在线设备';
    } else {
      var alias = dev.alias || deviceId.substring(0, 8);
      var score = dev.algo_score || 0;
      var extra = score > 0 ? ' · 算法分 <b style="color:' + (score >= 60 ? '#22c55e' : score >= 30 ? '#f59e0b' : '#f87171') + '">' + score + '</b>' : '';
      titleEl.innerHTML = alias + extra;
    }
  }

  _ttFlowRender();
  document.getElementById('tt-flow-overlay').classList.add('open');
}

/** onclick 入口：避免 async 未捕获 Promise */
function _ttOpenFlowConfigEntry(deviceId) {
  _ttOpenFlowConfig(deviceId).catch(function (e) {
    console.warn('[tt-flow]', e);
    if (typeof showToast === 'function') {
      showToast('流程面板加载异常: ' + (e && e.message ? e.message : e), 'error');
    }
  });
}

function _ttFlowApplySmartDefaults(dev) {
  const hasLeads   = (dev.leads_count || 0) > 0;
  const algoScore  = dev.algo_score || 0;
  const hasActivity = (dev.sessions_today||0) > 0 || (dev.today_followed||0) > 0;

  _ttFlowStates = {};
  _TT_FLOW_STEPS_DEF.forEach(function(s) {
    const defParams = {};
    s.params.forEach(function(p) { defParams[p.key] = p.def; });
    let enabled = true;
    if (s.id === 'pitch')      enabled = hasLeads;
    if (s.id === 'warmup')     enabled = !hasActivity || algoScore < 40;
    if (s.id === 'followbacks')enabled = hasActivity;
    _ttFlowStates[s.id] = { enabled: enabled, params: defParams };
  });
}

function _ttFlowLoadConfig(deviceId) {
  try {
    const raw = localStorage.getItem('tt_flow_' + deviceId);
    return raw ? JSON.parse(raw) : null;
  } catch(e) { return null; }
}

function _ttFlowSaveConfigToStorage(deviceId) {
  try { localStorage.setItem('tt_flow_' + deviceId, JSON.stringify(_ttFlowStates)); } catch(e) {}
  _ttGeoSaveToStorage(deviceId);
}

function _ttCloseFlowConfig() {
  var el = document.getElementById('tt-flow-overlay');
  if (el) el.classList.remove('open');
  _ttFlowBatchMode = false;
  _ttFlowBatchCount = 0;
}

function _ttBuildFlowModal() {
  const overlay = document.createElement('div');
  overlay.id = 'tt-flow-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _ttCloseFlowConfig(); };
  overlay.innerHTML =
    '<div id="tt-flow-modal">' +
      '<div class="ttf-head">' +
        '<div>' +
          '<div class="ttf-title">⚙ 执行方案</div>' +
          '<div class="ttf-subtitle">设备: <span id="tt-flow-dev-name" style="font-weight:600;color:var(--text-main)"></span></div>' +
        '</div>' +
        '<div style="display:flex;align-items:center;gap:8px">' +
        '<button type="button" class="ttf-head-refresh-audience" onclick="_ttFlowRefreshAudiencePresets()" title="从服务器重新拉取 config/audience_presets.yaml">↻ 人群预设</button>' +
        '<button class="ttf-close" onclick="_ttCloseFlowConfig()">✕</button>' +
        '</div>' +
      '</div>' +
      '<div class="ttf-body">' +
        '<div id="ttf-suggest"></div>' +
        '<div class="tt-geo-section" id="tt-geo-section"></div>' +
        '<div id="ttf-cards"></div>' +
        '<div class="ttf-adv-toggle" onclick="_ttToggleAdvanced()">' +
          '<span class="ttf-adv-icon" id="ttf-adv-icon">▶</span>' +
          '<span style="font-size:13px;font-weight:600;color:var(--text-main)">自定义流程</span>' +
          '<span style="font-size:11px;color:var(--text-dim);margin-left:auto">精细调整每个步骤参数</span>' +
        '</div>' +
        '<div id="ttf-advanced" class="ttf-advanced">' +
          '<div class="tt-flow-presets" id="tt-flow-presets"></div>' +
          '<div class="tt-flow-steps" id="tt-flow-steps-list"></div>' +
        '</div>' +
        '<div id="ttf-history"></div>' +
      '</div>' +
      '<div class="ttf-footer">' +
        '<div class="tt-flow-est" id="tt-flow-est"></div>' +
        '<div class="ttf-btns">' +
          '<button class="tt-flow-save" onclick="_ttFlowSaveCurrent()" title="保存为该设备默认配置">💾 保存</button>' +
          '<button class="tt-flow-sync" id="tt-flow-sync-btn" onclick="_ttFlowSyncAll()" title="同步到所有在线设备">🌍 同步全部</button>' +
          '<button class="tt-flow-exec" id="tt-flow-exec-btn" onclick="_ttExecuteFlowSteps()">▶ 自定义执行</button>' +
        '</div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);
}

function _ttFlowRender() {
  _ttFlowRenderSuggest();
  _ttFlowRenderCards();
  _ttFlowRenderGeo();
  _ttFlowRenderPresets();
  _ttFlowRenderSteps();
  _ttFlowRenderModalHistory();
  _ttFlowUpdateEst();
}

function _ttFlowRenderPresets() {
  const el = document.getElementById('tt-flow-presets');
  if (!el) return;
  let html = '';
  Object.keys(_TT_FLOW_PRESETS).forEach(function(k) {
    const p = _TT_FLOW_PRESETS[k];
    const active = _ttFlowCurrentPreset === k ? ' active' : '';
    html += '<button class="tt-flow-preset' + active + '" onclick="_ttFlowApplyPreset(\'' + k + '\')" title="' + p.label + '">' + p.name + '</button>';
  });
  el.innerHTML = html;
}

// 追踪哪些步骤已展开（即使未启用也可展开查看参数）
var _ttFlowExpandedSteps = {};

function _ttFlowRenderSteps() {
  var el = document.getElementById('tt-flow-steps-list');
  if (!el) return;
  var html = '';

  for (var gi = 0; gi < _TT_STEP_GROUPS.length; gi++) {
    var grp = _TT_STEP_GROUPS[gi];
    html += '<div class="tts-group">';
    html += '<div class="tts-group-label">' + grp.label + '</div>';

    for (var si = 0; si < grp.ids.length; si++) {
      var stepId = grp.ids[si];
      var sDef = null;
      for (var d = 0; d < _TT_FLOW_STEPS_DEF.length; d++) {
        if (_TT_FLOW_STEPS_DEF[d].id === stepId) { sDef = _TT_FLOW_STEPS_DEF[d]; break; }
      }
      if (!sDef) continue;

      var st = _ttFlowStates[stepId] || {enabled:false, params:{}};
      var enabled = st.enabled;
      var expanded = _ttFlowExpandedSteps[stepId] || enabled;
      var curParams = {};
      sDef.params.forEach(function(p){ curParams[p.key] = (st.params[p.key] !== undefined) ? st.params[p.key] : p.def; });
      var dynTime = _ttCalcStepTime(stepId, curParams);

      var cls = 'tts-card grp-' + gi + (enabled ? ' enabled' : '') + (expanded ? ' expanded' : '');

      html += '<div class="' + cls + '" id="tt-fstep-' + stepId + '">';
      // 头部
      html += '<div class="tts-head">';
      html += '<span class="tts-icon">' + sDef.icon + '</span>';
      html += '<div class="tts-info" onclick="_ttFlowExpandStep(\'' + stepId + '\')">';
      html += '<div class="tts-name">' + sDef.name + '</div>';
      html += '<div class="tts-desc">' + sDef.desc + '</div>';
      html += '</div>';
      html += '<span class="tts-time" id="tts-time-' + stepId + '">~' + dynTime + 'min</span>';
      html += '<button class="tts-switch" onclick="_ttFlowToggleStep(\'' + stepId + '\')" title="' + (enabled ? '关闭' : '启用') + '"></button>';
      html += '<span class="tts-chevron" onclick="_ttFlowExpandStep(\'' + stepId + '\')">▶</span>';
      html += '</div>';
      // 参数面板
      html += '<div class="tts-params">' + _ttFlowRenderParams(sDef, curParams) + '</div>';
      html += '</div>';
    }
    html += '</div>';
  }
  el.innerHTML = html;
}

function _ttFlowExpandStep(stepId) {
  _ttFlowExpandedSteps[stepId] = !_ttFlowExpandedSteps[stepId];
  var card = document.getElementById('tt-fstep-' + stepId);
  if (card) card.classList.toggle('expanded');
}

function _ttFlowRenderParams(stepDef, currentParams) {
  var html = '';
  var riskDef = _TT_STEP_RISK[stepDef.id];

  stepDef.params.forEach(function(p) {
    var val = (currentParams[p.key] !== undefined) ? currentParams[p.key] : p.def;

    if (p.type === 'number') {
      var pMin = p.min || 1, pMax = p.max || 999;
      // 检查这个参数是否有风险定义
      var hasRisk = riskDef && riskDef.key === p.key;
      var riskCls = '', riskLabel = '';
      if (hasRisk) {
        if (val <= riskDef.safe) { riskCls = 'safe'; riskLabel = '安全'; }
        else if (val <= riskDef.warn) { riskCls = 'warn'; riskLabel = '注意'; }
        else { riskCls = 'danger'; riskLabel = '高风险'; }
      }

      html += '<div class="tts-param-row">';
      html += '<span class="tts-param-label">' + p.label + '</span>';
      html += '<div class="tts-slider-wrap">';
      html += '<input class="tts-slider" type="range" min="' + pMin + '" max="' + pMax + '" value="' + val + '" ' +
        'oninput="_ttFlowSliderSync(\'' + stepDef.id + '\',\'' + p.key + '\',+this.value)">';
      html += '<span class="tts-val" id="tts-val-' + stepDef.id + '-' + p.key + '">' + val + '</span>';
      if (hasRisk) {
        html += '<span class="tts-risk ' + riskCls + '" id="tts-risk-' + stepDef.id + '">' +
          (riskCls === 'danger' ? '⚠ ' : '') + riskLabel + '</span>';
      }
      html += '</div>';
      html += '</div>';
      // 范围标签
      html += '<div class="tts-range-hint"><span>' + pMin + '</span><span>推荐: ' + p.def + '</span><span>' + pMax + '</span></div>';
      // 业务影响预估
      if (hasRisk && riskDef.impact) {
        html += '<div class="tts-impact" id="tts-impact-' + stepDef.id + '">💡 ' + riskDef.impact(val) + '</div>';
      }
    } else if (p.type === 'select') {
      html += '<div class="tts-param-row">';
      html += '<span class="tts-param-label">' + p.label + '</span>';
      html += '<select class="tts-slider-wrap" style="flex:1;background:rgba(255,255,255,.07);border:1px solid rgba(255,255,255,.1);border-radius:6px;padding:6px 10px;color:var(--text-main);font-size:13px;outline:none" ' +
        'onchange="_ttFlowUpdateParam(\'' + stepDef.id + '\',\'' + p.key + '\',this.value)">';
      (p.options || []).forEach(function(opt) {
        var ov = (opt && typeof opt === 'object') ? opt.value : opt;
        var ol = (opt && typeof opt === 'object') ? (opt.label || String(ov)) : String(opt);
        var escV = String(ov).replace(/&/g,'&amp;').replace(/"/g,'&quot;');
        var escL = String(ol).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        html += '<option value="' + escV + '"' + (val === ov ? ' selected' : '') + '>' + escL + '</option>';
      });
      html += '</select></div>';
    } else if (p.type === 'checkbox') {
      html += '<div class="tts-param-row">';
      html += '<span class="tts-param-label">' + p.label + '</span>';
      html += '<div class="tts-toggle-wrap">';
      html += '<button class="tts-toggle' + (val ? ' on' : '') + '" onclick="_ttFlowToggleParam(\'' + stepDef.id + '\',\'' + p.key + '\',this)"></button>';
      html += '<span class="tts-toggle-label">' + (val ? '已启用' : '未启用') + '</span>';
      html += '</div></div>';
    }
  });

  return html;
}

// 滑块实时同步：更新数值显示 + 风险标记 + 影响预估 + 步骤耗时（不重绘整个列表）
function _ttFlowSliderSync(stepId, key, value) {
  // 更新参数
  if (!_ttFlowStates[stepId]) {
    var def2 = _TT_FLOW_STEPS_DEF.find(function(s){return s.id===stepId;});
    var defP2 = {};
    if (def2) def2.params.forEach(function(p){defP2[p.key]=p.def;});
    _ttFlowStates[stepId] = {enabled: _ttFlowStates[stepId] ? _ttFlowStates[stepId].enabled : false, params:defP2};
  }
  _ttFlowStates[stepId].params[key] = value;

  // 实时更新数值显示
  var valEl = document.getElementById('tts-val-' + stepId + '-' + key);
  if (valEl) valEl.textContent = value;

  // 更新滑块轨道填充色
  var slider = valEl ? valEl.parentElement.querySelector('.tts-slider') : null;
  if (slider) {
    var pct = ((value - slider.min) / (slider.max - slider.min)) * 100;
    var color = 'var(--accent)';
    var rd = _TT_STEP_RISK[stepId];
    if (rd && rd.key === key) {
      if (value > rd.warn) color = '#ef4444';
      else if (value > rd.safe) color = '#f59e0b';
      else color = '#22c55e';
    }
    slider.style.background = 'linear-gradient(to right,' + color + ' 0%,' + color + ' ' + pct + '%,rgba(255,255,255,.08) ' + pct + '%)';
  }

  // 实时更新风险标记
  var riskDef = _TT_STEP_RISK[stepId];
  if (riskDef && riskDef.key === key) {
    var riskEl = document.getElementById('tts-risk-' + stepId);
    if (riskEl) {
      var cls2 = 'safe', lbl2 = '安全';
      if (value > riskDef.safe && value <= riskDef.warn) { cls2 = 'warn'; lbl2 = '注意'; }
      else if (value > riskDef.warn) { cls2 = 'danger'; lbl2 = '⚠ 高风险'; }
      riskEl.className = 'tts-risk ' + cls2;
      riskEl.textContent = lbl2;
    }
    // 实时更新影响预估
    var impEl = document.getElementById('tts-impact-' + stepId);
    if (impEl && riskDef.impact) impEl.innerHTML = '💡 ' + riskDef.impact(value);
  }

  // 实时更新步骤耗时
  var curP = _ttFlowStates[stepId].params;
  var newTime = _ttCalcStepTime(stepId, curP);
  var timeEl = document.getElementById('tts-time-' + stepId);
  if (timeEl) timeEl.textContent = '~' + newTime + 'min';

  // 更新总耗时
  _ttFlowUpdateEst();
}

// checkbox toggle（不重绘整个面板）
function _ttFlowToggleParam(stepId, key, btnEl) {
  if (!_ttFlowStates[stepId]) return;
  var cur = _ttFlowStates[stepId].params[key];
  var newVal = !cur;
  _ttFlowStates[stepId].params[key] = newVal;
  if (btnEl) {
    btnEl.classList.toggle('on', newVal);
    var lbl = btnEl.nextElementSibling;
    if (lbl) lbl.textContent = newVal ? '已启用' : '未启用';
  }
  _ttFlowUpdateEst();
}

function _ttFlowToggleStep(stepId) {
  if (!_ttFlowStates[stepId]) {
    var def = _TT_FLOW_STEPS_DEF.find(function(s){return s.id===stepId;});
    var defParams = {};
    if (def) def.params.forEach(function(p){defParams[p.key]=p.def;});
    _ttFlowStates[stepId] = {enabled:true, params:defParams};
  } else {
    _ttFlowStates[stepId].enabled = !_ttFlowStates[stepId].enabled;
  }
  // 启用时自动展开
  if (_ttFlowStates[stepId].enabled) _ttFlowExpandedSteps[stepId] = true;
  _ttFlowCurrentPreset = null;
  _ttFlowRender();
}

function _ttFlowUpdateParam(stepId, key, value) {
  if (!_ttFlowStates[stepId]) return;
  _ttFlowStates[stepId].params[key] = value;
  // 联动更新步骤耗时
  var curP = _ttFlowStates[stepId].params;
  var newTime = _ttCalcStepTime(stepId, curP);
  var timeEl = document.getElementById('tts-time-' + stepId);
  if (timeEl) timeEl.textContent = '~' + newTime + 'min';
  _ttFlowUpdateEst();
}

function _ttFlowApplyPreset(presetKey) {
  var preset = _TT_FLOW_PRESETS[presetKey];
  if (!preset) return;
  _ttFlowCurrentPreset = presetKey;
  // 重置展开状态——只展开被启用的步骤
  _ttFlowExpandedSteps = {};
  _TT_FLOW_STEPS_DEF.forEach(function(s) {
    if (!_ttFlowStates[s.id]) {
      var defParams = {};
      s.params.forEach(function(p){defParams[p.key]=p.def;});
      _ttFlowStates[s.id] = {enabled:false, params:defParams};
    }
    _ttFlowStates[s.id].enabled = preset.steps.indexOf(s.id) >= 0;
  });
  _ttFlowRender();
}

// ════════════════════════════════════════════════════════
// 策略卡片渲染 + 智能建议 + 历史 + 一键执行
// ════════════════════════════════════════════════════════

function _ttFlowRenderSuggest() {
  var el = document.getElementById('ttf-suggest');
  if (!el) return;
  var dev = (window._ttDevicesCache || {})[_ttFlowDevId] || {};
  var score = dev.algo_score || 0;
  var todayFollowed = dev.today_followed || 0;
  var leadsCount = dev.leads_count || 0;
  var sessionsToday = dev.sessions_today || 0;
  var html = '', cls = 'suggest-info';

  var hasGeo = _ttFlowGeo.countries.length > 0;

  if (_ttFlowBatchMode) {
    cls = 'suggest-info';
    html = '🌍 批量模式 — 选择策略后将同步到 <b>' + _ttFlowBatchCount + '</b> 台在线设备并启动执行';
  } else if (!hasGeo) {
    cls = 'suggest-warn';
    html = '🌍 请先 <b>选择目标市场</b> — 国家决定了语言、时区和内容偏好';
  } else if (score > 0 && score < 30) {
    cls = 'suggest-warn';
    html = '💡 算法分 <b>' + score + '</b> (偏低) — 建议先用「养号模式」浏览内容提升权重';
  } else if (score > 0 && score < 60 && sessionsToday === 0) {
    cls = 'suggest-warn';
    html = '💡 今日未活跃，算法分 <b>' + score + '</b> — 建议先养号再获客';
  } else if (leadsCount > 0 && todayFollowed > 10) {
    cls = 'suggest-good';
    html = '💡 已关注 <b>' + todayFollowed + '</b> 人，有 <b>' + leadsCount + '</b> 条线索 — 建议「获客模式」跟进';
  } else if (score >= 60) {
    cls = 'suggest-good';
    html = '💡 算法分 <b>' + score + '</b> (良好) — 推荐「全流程」完整获客';
  } else {
    html = '💡 选择下方策略卡片 <b>一键启动</b>，或展开「自定义流程」精细调整';
  }
  el.innerHTML = '<div class="ttf-suggest ' + cls + '">' + html + '</div>';
}

function _ttFlowRenderCards() {
  var el = document.getElementById('ttf-cards');
  if (!el) return;
  var dev = (window._ttDevicesCache || {})[_ttFlowDevId] || {};
  var score = dev.algo_score || 0;
  var selected = _ttFlowCurrentPreset;

  // 根据设备状态推荐
  var recommended = 'full';
  if (score > 0 && score < 30) recommended = 'warmup';
  else if (score > 0 && score < 60 && (dev.sessions_today || 0) === 0) recommended = 'warmup';
  else if ((dev.leads_count || 0) > 5) recommended = 'follow';

  var html = '';
  var keys = Object.keys(_TT_FLOW_PRESETS);
  for (var i = 0; i < keys.length; i++) {
    var k = keys[i];
    var p = _TT_FLOW_PRESETS[k];
    var isRec = (k === recommended);
    var isSel = (k === selected);

    var totalEst = 0;
    var stepIcons = '';
    for (var j = 0; j < p.steps.length; j++) {
      var sid = p.steps[j];
      for (var m = 0; m < _TT_FLOW_STEPS_DEF.length; m++) {
        if (_TT_FLOW_STEPS_DEF[m].id === sid) {
          // 动态计算：用当前参数或默认参数
          var st2 = _ttFlowStates[sid];
          var defP3 = {};
          _TT_FLOW_STEPS_DEF[m].params.forEach(function(pp){ defP3[pp.key] = (st2 && st2.params[pp.key] !== undefined) ? st2.params[pp.key] : pp.def; });
          totalEst += _ttCalcStepTime(sid, defP3);
          stepIcons += _TT_FLOW_STEPS_DEF[m].icon + ' ';
          break;
        }
      }
    }

    var emoji = p.name.split(' ')[0];
    var label = p.name.replace(/^[^\s]+\s/, '');

    html += '<div class="ttf-card' +
      (isRec ? ' recommended' : '') +
      (isSel ? ' selected' : '') +
      '" onclick="_ttFlowApplyPreset(\'' + k + '\')">' +
      (isRec && !isSel ? '<div class="ttf-card-badge">推荐</div>' : '') +
      (isSel ? '<div class="ttf-card-badge" style="background:linear-gradient(135deg,#22c55e,#16a34a)">已选</div>' : '') +
      '<div class="ttf-card-icon">' + emoji + '</div>' +
      '<div class="ttf-card-name">' + label + '</div>' +
      '<div class="ttf-card-desc">' + (p.desc || '') + '</div>' +
      '<div class="ttf-card-meta">' +
        '<span class="ttf-card-steps">' + stepIcons.trim() + '</span>' +
        '<span class="ttf-card-time">~' + totalEst + '分钟</span>' +
      '</div>' +
      '<button class="ttf-card-exec" style="background:' + p.color + '15;border-color:' + p.color + '40;color:' + p.color + '" ' +
        'onclick="event.stopPropagation();_ttQuickExecPreset(\'' + k + '\')">▶ ' +
        (_ttFlowBatchMode ? '全部启动 (' + _ttFlowBatchCount + '台)' :
          ((_ttFlowGeo.countries.length > 0 && _geoAllCountries()[_ttFlowGeo.countries[0]]) ?
            _geoAllCountries()[_ttFlowGeo.countries[0]].flag + ' 启动' : '一键启动')) + '</button>' +
    '</div>';
  }
  el.innerHTML = '<div class="ttf-cards">' + html + '</div>';
}

function _ttFlowRenderModalHistory() {
  var el = document.getElementById('ttf-history');
  if (!el) return;
  var history = _ttGetFlowHistory(_ttFlowDevId);
  if (!history.length) { el.innerHTML = ''; return; }
  var last = history[0];
  var mins = Math.round((Date.now() - last.ts) / 60000);
  var ago = mins < 1 ? '刚刚' : mins < 60 ? mins + '分钟前' : Math.round(mins/60) < 24 ? Math.round(mins/60) + '小时前' : Math.round(mins/60/24) + '天前';
  var icons = '';
  for (var i = 0; i < last.steps.length; i++) icons += last.steps[i].icon + ' ';
  var names = [];
  for (var j = 0; j < last.steps.length; j++) names.push(last.steps[j].name);

  el.innerHTML = '<div class="ttf-last-run">' +
    '<span style="font-size:14px">📋</span>' +
    '<span>上次: <b>' + icons.trim() + '</b> ' + names.join('→') + ' · ' + ago + ' · ' + last.taskCount + '个任务</span>' +
    '<button class="ttf-last-run-replay" onclick="_ttFlowReplay(\'' + _ttFlowDevId + '\',0)">↻ 重播</button>' +
  '</div>';
}

async function _ttQuickExecPreset(presetKey) {
  _ttFlowApplyPreset(presetKey);
  if (_ttFlowBatchMode) {
    await _ttFlowSyncAll();
  } else {
    await _ttExecuteFlowSteps();
  }
}

function _ttToggleAdvanced() {
  var adv = document.getElementById('ttf-advanced');
  var icon = document.getElementById('ttf-adv-icon');
  if (!adv) return;
  var isOpen = adv.classList.toggle('open');
  if (icon) icon.classList.toggle('open', isOpen);
}

function _ttFlowUpdateEst() {
  var el = document.getElementById('tt-flow-est');
  var btn = document.getElementById('tt-flow-exec-btn');
  if (!el) return;
  var total = 0;
  var stepCount = 0;
  _TT_FLOW_STEPS_DEF.forEach(function(s) {
    var st = _ttFlowStates[s.id];
    if (st && st.enabled) {
      var curP = {};
      s.params.forEach(function(p){ curP[p.key] = (st.params[p.key] !== undefined) ? st.params[p.key] : p.def; });
      total += _ttCalcStepTime(s.id, curP);
      stepCount++;
    }
  });
  if (total === 0) {
    el.textContent = '请至少选择一个步骤';
    if (btn) btn.disabled = true;
  } else {
    var estHtml = '预计耗时 <b>' + total + '</b> 分钟 · ' + stepCount + ' 个步骤';
    var geoCode = _ttFlowGeo.countries.length > 0 ? _ttFlowGeo.countries[0] : null;
    if (geoCode) {
      var tz = _geoLocalTime(geoCode);
      if (tz) estHtml += ' <span style="margin-left:8px;font-size:11px;opacity:.7">' + tz.dot + ' 目标市场 ' + tz.time + '</span>';
    }
    el.innerHTML = estHtml;
    if (btn) btn.disabled = false;
  }
}

function _ttFlowSaveCurrent() {
  if (_ttFlowDevId) {
    _ttFlowSaveConfigToStorage(_ttFlowDevId);
    const msg = '配置已保存';
    if (typeof showToast === 'function') showToast(msg, 'success');
    else if (typeof _ttShowToast === 'function') _ttShowToast(msg, 'success');
  }
}

async function _ttFlowSyncAll() {
  const btn = document.getElementById('tt-flow-sync-btn');
  const devs = Object.values(window._ttDevicesCache || {}).filter(function(d){return d.online;});
  if (!devs.length) { if (typeof showToast==='function') showToast('无在线设备','warn'); return; }

  if (btn) { btn.disabled = true; btn.textContent = '⏳ 同步中...'; }

  // 1. 保存当前设备配置
  if (_ttFlowDevId) _ttFlowSaveConfigToStorage(_ttFlowDevId);

  // 2. 把相同的 steps 配置 + GEO 配置应用到所有在线设备
  let synced = 0;
  devs.forEach(function(d) {
    try {
      localStorage.setItem('tt_flow_' + d.device_id, JSON.stringify(_ttFlowStates));
      localStorage.setItem('tt_geo_' + d.device_id, JSON.stringify(_ttFlowGeo));
      synced++;
    } catch(e) {}
  });

  // 3. 为每台设备依次启动流程
  const enabledSteps = _TT_FLOW_STEPS_DEF.filter(function(s){
    return _ttFlowStates[s.id] && _ttFlowStates[s.id].enabled;
  });
  if (!enabledSteps.length) {
    if (typeof showToast==='function') showToast('已同步配置到 ' + synced + ' 台设备（未选步骤，跳过执行）','success');
    if (btn) { btn.disabled = false; btn.textContent = '🌍 同步全部'; }
    return;
  }

  const flowSteps = enabledSteps.map(function(s) {
    const params = Object.assign({}, (_ttFlowStates[s.id]||{}).params||{});
    return { type: s.type, params: _ttFlowGeoInjectParams(s.type, params) };
  });

  let launched = 0, failed = 0;
  for (let i = 0; i < devs.length; i++) {
    const dev = devs[i];
    if (btn) btn.textContent = '⏳ ' + (i+1) + '/' + devs.length;
    try {
      const r = await fetch('/tiktok/device/' + encodeURIComponent(dev.device_id) + '/launch', {
        method:'POST', headers:{'Content-Type':'application/json'}, credentials:'include',
        body: JSON.stringify({flow_steps: flowSteps})
      }).then(function(res){return res.json();});
      if (r.ok !== false) {
        launched++;
        if (r.flow_tasks && r.flow_tasks.length) {
          _ttTrackFlowProgress(dev.device_id, enabledSteps, r.flow_tasks);
        }
      } else { failed++; }
    } catch(e) { failed++; }
    await new Promise(function(res){setTimeout(res, 300);});
  }

  _ttCloseFlowConfig();
  const msg = '🌍 已同步并启动 ' + launched + ' 台设备' + (failed ? '，' + failed + ' 台失败' : '');
  if (typeof showToast==='function') showToast(msg, launched>0?'success':'error', 5000);
  if (btn) { btn.disabled = false; btn.textContent = '🌍 同步全部'; }
}

async function _ttExecuteFlowSteps() {
  const deviceId = _ttFlowDevId;
  if (!deviceId) return;

  const enabledSteps = _TT_FLOW_STEPS_DEF.filter(function(s) {
    return _ttFlowStates[s.id] && _ttFlowStates[s.id].enabled;
  });
  if (!enabledSteps.length) {
    if (typeof showToast === 'function') showToast('请至少选择一个步骤', 'warn');
    return;
  }

  const btn = document.getElementById('tt-flow-exec-btn');
  if (btn) { btn.disabled = true; btn.textContent = '⏳ 创建中...'; }

  // 构建 flow_steps，注入全局 GEO 设置
  const flowSteps = enabledSteps.map(function(s) {
    const params = Object.assign({}, (_ttFlowStates[s.id]||{}).params||{});
    return { type: s.type, params: _ttFlowGeoInjectParams(s.type, params) };
  });

  // 执行前保存 GEO 配置
  if (_ttFlowDevId) _ttGeoSaveToStorage(_ttFlowDevId);

  try {
    const r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/launch', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      credentials: 'include',
      body: JSON.stringify({ flow_steps: flowSteps })
    }).then(function(res){return res.json();});

    _ttCloseFlowConfig();
    _ttFlowSaveConfigToStorage(deviceId); // 自动保存配置

    if (r.ok !== false) {
      const stepLabels = enabledSteps.map(function(s){return s.icon+s.name;}).join(' → ');
      const cnt = r.task_count || enabledSteps.length;
      _ttSetActionResult(deviceId, '✓ 已创建 ' + cnt + ' 个步骤任务', '#22c55e');
      const msg = (r.message || '流程任务已创建') + '<br><small style="opacity:.7">' + stepLabels + '</small>';
      if (typeof showToast === 'function') showToast(msg, 'success', 4000);

      // P4: 保存执行历史
      _ttSaveFlowHistory(deviceId, enabledSteps, r.flow_tasks || []);

      // P2: 启动实时进度追踪
      if (r.flow_tasks && r.flow_tasks.length) {
        _ttTrackFlowProgress(deviceId, enabledSteps, r.flow_tasks);
      }
    } else {
      _ttSetActionResult(deviceId, '✗ ' + (r.error || '启动失败'), '#f87171');
      if (typeof showToast === 'function') showToast(r.error||'流程启动失败', 'error');
    }
  } catch(e) {
    _ttSetActionResult(deviceId, '✗ 网络错误: ' + e.message, '#f87171');
    if (typeof showToast === 'function') showToast('网络错误: ' + e.message, 'error');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '▶ 立即执行'; }
  }
}

// ════════════════════════════════════════════════════════
// 多选模式：批量操作多台设备
// ════════════════════════════════════════════════════════

window._ttMultiSelectMode = false;
window._ttSelectedDevices = new Set();

function _ttCardClick(did) {
  if (window._ttMultiSelectMode) { _ttToggleDeviceSelect(did); return; }
  _ttOpenPanel(did);
}

/** 卡片底部快捷按钮（本机收件 / 流程），不冒泡到整卡点击 */
function _ttCardQuick(e) {
  e.stopPropagation();
  var btn = e.target.closest ? e.target.closest('button') : null;
  if (!btn || btn.disabled) return;
  var act = btn.getAttribute('data-act');
  var did = e.currentTarget.getAttribute('data-did');
  if (!did) return;
  if (act === 'inbox') ttCheckInboxForDevice(did);
  else if (act === 'flow') _ttOpenFlowConfigEntry(did);
}

function _ttToggleMultiSelect() {
  window._ttMultiSelectMode = !window._ttMultiSelectMode;
  var btn = document.getElementById('tt-multi-sel-btn');
  if (btn) {
    btn.style.background = window._ttMultiSelectMode ? 'rgba(99,102,241,.3)' : '';
    btn.style.borderColor = window._ttMultiSelectMode ? 'rgba(99,102,241,.5)' : '';
    btn.style.color = window._ttMultiSelectMode ? '#a5b4fc' : '';
  }

  window._ttSelectedDevices.clear();
  var cards = document.querySelectorAll('.tt-dev-card');
  cards.forEach(function(card) {
    var cb = card.querySelector('.tt-ms-cb');
    if (window._ttMultiSelectMode) {
      if (!cb) {
        cb = document.createElement('div');
        cb.className = 'tt-ms-cb';
        cb.innerHTML = '\u2610';
        card.insertBefore(cb, card.firstChild);
      }
      cb.style.display = '';
    } else {
      if (cb) cb.style.display = 'none';
      card.classList.remove('tt-ms-selected');
    }
  });

  _ttUpdateBatchBar();
}

function _ttToggleDeviceSelect(did) {
  if (!window._ttMultiSelectMode) return;
  if (window._ttSelectedDevices.has(did)) {
    window._ttSelectedDevices.delete(did);
  } else {
    window._ttSelectedDevices.add(did);
  }
  var card = document.querySelector('.tt-dev-card[data-did="' + did + '"]');
  if (card) {
    var selected = window._ttSelectedDevices.has(did);
    card.classList.toggle('tt-ms-selected', selected);
    var cb = card.querySelector('.tt-ms-cb');
    if (cb) cb.innerHTML = selected ? '\u2611' : '\u2610';
  }
  _ttUpdateBatchBar();
}

function _ttUpdateBatchBar() {
  var existing = document.getElementById('tt-batch-bar');
  var count = window._ttSelectedDevices.size;
  if (!window._ttMultiSelectMode || count === 0) {
    if (existing) existing.remove();
    return;
  }
  if (!existing) {
    existing = document.createElement('div');
    existing.id = 'tt-batch-bar';
    existing.className = 'tt-batch-bar';
    document.body.appendChild(existing);
  }
  existing.innerHTML =
    '<div class="tt-bb-info">\u2705 \u5DF2\u9009\u62E9 <b>' + count + '</b> \u53F0\u8BBE\u5907</div>' +
    '<div class="tt-bb-actions">' +
      '<button class="tt-bb-btn tt-bb-primary" onclick="_ttBatchConfigSelected()">\u2699\uFE0F \u6279\u91CF\u914D\u7F6E\u6D41\u7A0B</button>' +
      '<button class="tt-bb-btn tt-bb-contacts" onclick="_ttBatchImportContacts()">\u{1F4DE} \u6279\u91CF\u5BFC\u5165\u901A\u8BAF\u5F55</button>' +
      '<button class="tt-bb-btn" onclick="_ttSelectAll()">\u{2610} \u5168\u9009\u5728\u7EBF</button>' +
      '<button class="tt-bb-btn tt-bb-cancel" onclick="_ttToggleMultiSelect()">\u2715 \u53D6\u6D88</button>' +
    '</div>';
}

function _ttSelectAll() {
  var cards = document.querySelectorAll('.tt-dev-card:not(.offline)');
  cards.forEach(function(card) {
    var did = card.dataset.did;
    if (did && !window._ttSelectedDevices.has(did)) {
      window._ttSelectedDevices.add(did);
      card.classList.add('tt-ms-selected');
      var cb = card.querySelector('.tt-ms-cb');
      if (cb) cb.innerHTML = '\u2611';
    }
  });
  _ttUpdateBatchBar();
}

async function _ttBatchConfigSelected() {
  var dids = Array.from(window._ttSelectedDevices);
  if (!dids.length) return;
  _ttToggleMultiSelect();
  await _ttOpenFlowConfig(dids[0]);
}

async function _ttBatchImportContacts() {
  var dids = Array.from(window._ttSelectedDevices);
  if (!dids.length) return;

  // 策略选择弹窗
  _ttCloseModal();
  var overlay = document.createElement('div');
  overlay.className = 'ttp-modal-overlay';
  overlay.id = 'ttp-modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _ttCloseModal(); };
  overlay.innerHTML =
    '<div class="ttp-modal" style="max-width:420px">' +
      '<div class="ttp-modal-header">' +
        '<span>\u{1F4CB} \u9009\u62E9\u5206\u914D\u7B56\u7565</span>' +
        '<button class="ttp-modal-close" onclick="_ttCloseModal()">\u2715</button>' +
      '</div>' +
      '<div class="ttp-modal-body">' +
        '<div style="font-size:11px;color:#94a3b8;margin-bottom:10px">\u5DF2\u9009 ' + dids.length + ' \u53F0\u8BBE\u5907\uFF0C\u9009\u62E9\u5206\u914D\u65B9\u5F0F\uFF1A</div>' +
        '<div style="display:grid;gap:8px">' +
          '<div class="alloc-card" onclick="_ttAllocPick(\'even\')">' +
            '<div style="font-size:14px;margin-bottom:4px">\u2696\uFE0F \u5747\u5300\u5206\u914D</div>' +
            '<div style="font-size:11px;color:#94a3b8">\u6BCF\u53F0\u8BBE\u5907\u83B7\u5F97\u7B49\u91CF\u8054\u7CFB\u4EBA\uFF0C\u9002\u5408\u5E38\u89C4\u64CD\u4F5C</div>' +
          '</div>' +
          '<div class="alloc-card" onclick="_ttAllocPick(\'load\')">' +
            '<div style="font-size:14px;margin-bottom:4px">\u{1F4CA} \u6309\u8D1F\u8F7D\u5206\u914D</div>' +
            '<div style="font-size:11px;color:#94a3b8">\u5DF2\u6709\u8054\u7CFB\u4EBA\u5C11\u7684\u8BBE\u5907\u83B7\u5F97\u66F4\u591A\uFF0C\u5E73\u8861\u8BBE\u5907\u8D1F\u8F7D</div>' +
          '</div>' +
          '<div class="alloc-card" onclick="_ttAllocPick(\'geo\')">' +
            '<div style="font-size:14px;margin-bottom:4px">\u{1F310} \u6309GEO\u5206\u914D</div>' +
            '<div style="font-size:11px;color:#94a3b8">\u6309\u8BBE\u5907\u914D\u7F6E\u7684\u76EE\u6807\u56FD\u5BB6\u5206\u914D\u5BF9\u5E94\u5730\u533A\u53F7\u7801</div>' +
          '</div>' +
        '</div>' +
      '</div>' +
    '</div>';
  var st = document.createElement('style');
  st.textContent = '.alloc-card{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.08);border-radius:10px;padding:12px 16px;cursor:pointer;transition:.2s}.alloc-card:hover{border-color:rgba(99,102,241,.5);background:rgba(99,102,241,.08)}';
  overlay.querySelector('.ttp-modal').appendChild(st);
  document.body.appendChild(overlay);
}

function _ttAllocPick(strategy) {
  _ttCloseModal();
  var dids = Array.from(window._ttSelectedDevices);
  var inp = document.createElement('input'); inp.type='file'; inp.accept='.csv,.txt';
  inp.onchange = async function() {
    var file = inp.files[0]; if (!file) return;
    var text = await file.text();
    var lines = text.split(/\r?\n/).filter(function(l){return l.trim();});
    var contacts = [];
    for (var i = 1; i < lines.length; i++) {
      var cols = lines[i].split(/[,;\t]/).map(function(c){return c.trim().replace(/^"|"$/g,'');});
      if (cols.length >= 2) contacts.push({name:cols[0], number:cols[1]});
    }
    if (!contacts.length) { _toast('\u65E0\u6709\u6548\u6570\u636E','error'); return; }

    var alloc = {};
    if (strategy === 'load') {
      // 查询各设备已有联系人数 → 少的多分
      var loads = [];
      for (var di = 0; di < dids.length; di++) {
        try {
          var rr = await api('GET', '/devices/' + encodeURIComponent(dids[di]) + '/contacts?limit=1');
          loads.push({did:dids[di], count: (rr.contacts || []).length + (rr.total || 0)});
        } catch(e) { loads.push({did:dids[di], count:0}); }
      }
      loads.sort(function(a,b){return a.count - b.count;});
      var maxLoad = loads[loads.length-1].count || 1;
      var weights = loads.map(function(l){return Math.max(1, maxLoad - l.count + 10);});
      var wSum = weights.reduce(function(a,b){return a+b;},0);
      var cursor = 0;
      for (var wi = 0; wi < loads.length; wi++) {
        var share = Math.round(contacts.length * weights[wi] / wSum);
        if (wi === loads.length - 1) share = contacts.length - cursor;
        alloc[loads[wi].did] = contacts.slice(cursor, cursor + share);
        cursor += share;
      }
    } else if (strategy === 'geo') {
      // 按号码前缀国际区号与设备 GEO 配置匹配
      var geoMap = {};
      dids.forEach(function(d) {
        try {
          var cfg = JSON.parse(localStorage.getItem('tt_flow_' + d) || '{}');
          var cc = (cfg.country_code || '').replace('+','');
          if (cc) { if (!geoMap[cc]) geoMap[cc] = []; geoMap[cc].push(d); }
        } catch(e) {}
      });
      var unmatched = [];
      contacts.forEach(function(c) {
        var num = (c.number||'').replace(/[\s\-\(\)]/g,'');
        var matched = false;
        for (var cc in geoMap) {
          if (num.indexOf('+' + cc) === 0 || num.indexOf(cc) === 0) {
            var target = geoMap[cc][0];
            if (!alloc[target]) alloc[target] = [];
            alloc[target].push(c);
            geoMap[cc].push(geoMap[cc].shift()); // 轮转
            matched = true; break;
          }
        }
        if (!matched) unmatched.push(c);
      });
      if (unmatched.length) {
        var per = Math.ceil(unmatched.length / dids.length);
        for (var gi = 0; gi < dids.length; gi++) {
          var uc = unmatched.slice(gi*per, (gi+1)*per);
          if (!alloc[dids[gi]]) alloc[dids[gi]] = [];
          alloc[dids[gi]] = alloc[dids[gi]].concat(uc);
        }
      }
    } else {
      var per = Math.ceil(contacts.length / dids.length);
      for (var ei = 0; ei < dids.length; ei++) {
        alloc[dids[ei]] = contacts.slice(ei * per, (ei + 1) * per);
      }
    }

    var strategyLabel = {even:'\u5747\u5300',load:'\u8D1F\u8F7D',geo:'GEO'}[strategy] || strategy;
    _toast('\u5F00\u59CB\u5206\u914D ' + contacts.length + ' \u6761\uFF08' + strategyLabel + '\uFF09\u5230 ' + dids.length + ' \u53F0','info');
    var ok = 0;
    for (var did in alloc) {
      if (alloc[did].length) {
        try { await api('POST', '/devices/' + encodeURIComponent(did) + '/contacts/batch', {contacts:alloc[did]}); ok++; } catch(e) {}
      }
    }
    _toast('\u2705 ' + ok + '/' + dids.length + ' \u53F0\u5BFC\u5165\u5B8C\u6210\uFF08' + strategyLabel + '\u7B56\u7565\uFF09','success');
    _ttBatchLog({action:'\u6279\u91CF\u5BFC\u5165\u901A\u8BAF\u5F55(' + strategyLabel + ')', devices:dids.length, success:ok, contacts:contacts.length, file:file.name});
    _ttToggleMultiSelect();
  };
  inp.click();
}

// ════════════════════════════════════════════════════════
// 批量操作历史记录 (localStorage)
// ════════════════════════════════════════════════════════

function _ttBatchLog(entry) {
  entry.ts = new Date().toISOString();
  try {
    var logs = JSON.parse(localStorage.getItem('tt_batch_logs') || '[]');
    logs.unshift(entry);
    if (logs.length > 50) logs = logs.slice(0, 50);
    localStorage.setItem('tt_batch_logs', JSON.stringify(logs));
  } catch(e) {}
  _ttSyncLogsToBackend();
}

async function _ttSyncLogsToBackend() {
  try {
    var logs = JSON.parse(localStorage.getItem('tt_batch_logs') || '[]');
    if (!logs.length) return;
    var r = await fetch('/ops-logs/sync', {
      method:'POST', credentials:'include',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({logs:logs})
    });
    if (r.ok) {
      var d = await r.json();
      if (d.synced > 0) console.log('[OpsLog] synced', d.synced, 'entries');
    }
  } catch(e) {}
}

// 页面加载后延迟 5s 自动同步一次
setTimeout(_ttSyncLogsToBackend, 5000);

// ════════════════════════════════════════════════════════
// A/B 实验面板（复用 /experiments API）
// ════════════════════════════════════════════════════════

async function _ttShowABPanel() {
  _ttCloseModal();
  var overlay = document.createElement('div');
  overlay.className = 'ttp-modal-overlay';
  overlay.id = 'ttp-modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _ttCloseModal(); };
  overlay.innerHTML =
    '<div class="ttp-modal" style="width:680px">' +
      '<div class="ttp-modal-header">' +
        '<span style="font-size:20px">\u{1F9EA}</span>' +
        '<div class="ttp-modal-title">A/B \u5B9E\u9A8C</div>' +
        '<button class="ttp-modal-close" onclick="_ttCloseModal()">\u2715</button>' +
      '</div>' +
      '<div class="ttp-modal-body" id="ab-body" style="min-height:280px">' +
        '<div style="text-align:center;padding:40px;color:#64748b">\u{23F3} \u52A0\u8F7D\u5B9E\u9A8C...</div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);
  try {
    var r = await api('GET', '/experiments');
    var list = r.experiments || [];
    var body = document.getElementById('ab-body');
    if (!body) return;
    if (!list.length) {
      body.innerHTML = '<div style="text-align:center;color:#64748b;padding:40px">\u6682\u65E0\u5B9E\u9A8C<br><span style="font-size:11px">\u6253\u62DB\u547C\u9ED8\u8BA4\u5B9E\u9A8C <code>tiktok_greet_opening</code> \u5728\u9996\u6B21\u9884\u89C8\u65F6\u81EA\u52A8\u521B\u5EFA</span></div>';
      return;
    }
    var html = '<div style="font-size:11px;color:#94a3b8;margin-bottom:10px">\u6D3B\u8DC3\u5B9E\u9A8C\u4E0E\u53D8\u4F53\u56DE\u590D\u7387\uFF08\u6309 <code>reply_received / sent</code> \uFF09</div>';
    for (var i = 0; i < list.length; i++) {
      var ex = list[i];
      var name = ex.name || ex.experiment_id || '';
      if (!name) continue;
      try {
        var a = await api('GET', '/experiments/' + encodeURIComponent(name) + '/analyze');
        var vars = a.variants || {};
        var best = a.best_variant || '';
        html += '<div style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px;margin-bottom:10px">' +
          '<div style="font-size:13px;font-weight:600;color:#e2e8f0;margin-bottom:6px">' + _escHtml(name) +
            (best ? ' <span style="font-size:10px;color:#22c55e">\u2605 \u6700\u4F18: ' + _escHtml(best) + '</span>' : '') +
          '</div>';
        Object.keys(vars).forEach(function(vk) {
          var row = vars[vk] || {};
          var sent = row.sent || 0;
          var rep = row.reply_received || 0;
          var rate = sent ? Math.round(rep / sent * 1000) / 10 : 0;
          html += '<div style="display:flex;justify-content:space-between;font-size:11px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)">' +
            '<span style="color:#a5b4fc">' + _escHtml(vk) + '</span>' +
            '<span style="color:#94a3b8">\u53D1 ' + sent + ' / \u56DE ' + rep + ' <b style="color:#22c55e">' + rate + '%</b></span>' +
          '</div>';
        });
        html += '</div>';
      } catch(e2) {
        html += '<div style="color:#ef4444;font-size:11px">' + _escHtml(name) + ': ' + e2.message + '</div>';
      }
    }
    body.innerHTML = html;
  } catch(e) {
    var b = document.getElementById('ab-body');
    if (b) b.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px">' + e.message + '</div>';
  }
}

// ════════════════════════════════════════════════════════
// Worker 负载面板
// ════════════════════════════════════════════════════════

async function _ttShowWorkerLoad() {
  _ttCloseModal();
  var overlay = document.createElement('div');
  overlay.className = 'ttp-modal-overlay';
  overlay.id = 'ttp-modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _ttCloseModal(); };
  overlay.innerHTML =
    '<div class="ttp-modal" style="width:720px">' +
      '<div class="ttp-modal-header">' +
        '<span style="font-size:20px">\u2699\uFE0F</span>' +
        '<div class="ttp-modal-title">Worker \u8D1F\u8F7D\u76D1\u63A7</div>' +
        '<button class="ttp-modal-close" onclick="_ttCloseModal()">\u2715</button>' +
      '</div>' +
      '<div class="ttp-modal-body" id="wl-body" style="min-height:300px">' +
        '<div style="text-align:center;padding:40px;color:#64748b">\u{23F3} \u52A0\u8F7D\u96C6\u7FA4\u72B6\u6001...</div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);

  try {
    var r = await api('GET', '/cluster/overview');
    var body = document.getElementById('wl-body');
    if (!body) return;
    var hosts = r.hosts || {};
    var keys = Object.keys(hosts);
    if (!keys.length) {
      body.innerHTML = '<div style="text-align:center;color:#64748b;padding:40px">\u{1F4AD} \u672A\u68C0\u6D4B\u5230\u96C6\u7FA4\u8282\u70B9<br><span style="font-size:11px">\u5355\u673A\u6A21\u5F0F\u8FD0\u884C\u4E2D</span></div>';
      return;
    }

    var totalDevices = 0, totalTasks = 0;
    keys.forEach(function(k) { totalDevices += (hosts[k].devices||[]).length; totalTasks += hosts[k].tasks_active||0; });

    var html = '<div class="cvf-kpi-row" style="grid-template-columns:repeat(3,1fr)">' +
      '<div class="cvf-kpi"><div class="cvf-kpi-num" style="color:#818cf8">' + keys.length + '</div><div class="cvf-kpi-label">Worker \u8282\u70B9</div></div>' +
      '<div class="cvf-kpi"><div class="cvf-kpi-num" style="color:#60a5fa">' + totalDevices + '</div><div class="cvf-kpi-label">\u603B\u8BBE\u5907</div></div>' +
      '<div class="cvf-kpi"><div class="cvf-kpi-num" style="color:#f59e0b">' + totalTasks + '</div><div class="cvf-kpi-label">\u6D3B\u8DC3\u4EFB\u52A1</div></div>' +
    '</div>';

    html += '<div style="display:grid;gap:8px">';
    keys.forEach(function(k) {
      var h = hosts[k];
      var online = h.online !== false;
      var devCount = (h.devices||[]).length;
      var cpu = h.cpu_usage || 0;
      var mem = h.memory_usage || 0;
      var cpuColor = cpu >= 80 ? '#ef4444' : cpu >= 50 ? '#f59e0b' : '#22c55e';
      var memColor = mem >= 80 ? '#ef4444' : mem >= 50 ? '#f59e0b' : '#22c55e';

      html += '<div style="background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px 16px">' +
        '<div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">' +
          '<span style="width:8px;height:8px;border-radius:50%;background:' + (online?'#22c55e':'#ef4444') + '"></span>' +
          '<span style="font-size:13px;font-weight:600;color:#e2e8f0">' + _escHtml(h.host_name || k) + '</span>' +
          '<span style="font-size:10px;color:#64748b;margin-left:auto">' + (h.host_ip || '') + ':' + (h.port || 8000) + '</span>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px">' +
          '<div style="text-align:center"><div style="font-size:16px;font-weight:700;color:#60a5fa">' + devCount + '</div><div style="font-size:9px;color:#64748b">\u8BBE\u5907</div></div>' +
          '<div style="text-align:center"><div style="font-size:16px;font-weight:700;color:#f59e0b">' + (h.tasks_active||0) + '</div><div style="font-size:9px;color:#64748b">\u4EFB\u52A1</div></div>' +
          '<div style="text-align:center"><div style="font-size:16px;font-weight:700;color:' + cpuColor + '">' + Math.round(cpu) + '%</div><div style="font-size:9px;color:#64748b">CPU</div></div>' +
          '<div style="text-align:center"><div style="font-size:16px;font-weight:700;color:' + memColor + '">' + Math.round(mem) + '%</div><div style="font-size:9px;color:#64748b">\u5185\u5B58</div></div>' +
        '</div>' +
        // 设备列表
        (devCount > 0 ? '<div style="margin-top:8px;display:flex;flex-wrap:wrap;gap:4px">' +
          (h.devices||[]).slice(0,20).map(function(d) {
            var don = d.online !== false;
            var _did = d.device_id || '';
            return '<span style="font-size:10px;padding:2px 6px;border-radius:4px;background:' +
              (don ? 'rgba(34,197,94,.1)' : 'rgba(239,68,68,.1)') + ';color:' +
              (don ? '#22c55e' : '#ef4444') + '">' + _escHtml(d.alias || (_did ? _did.substring(0,8) : '') || '?') + '</span>';
          }).join('') +
        '</div>' : '') +
      '</div>';
    });
    html += '</div>';

    // 智能调度建议
    var busiest = null, lightest = null;
    keys.forEach(function(k) {
      var h = hosts[k];
      if (!h.online) return;
      var load = (h.tasks_active||0) + (h.devices||[]).length * 0.1 + (h.cpu_usage||0) * 0.01;
      if (!busiest || load > busiest.load) busiest = {name: h.host_name||k, load: load};
      if (!lightest || load < lightest.load) lightest = {name: h.host_name||k, load: load};
    });
    if (busiest && lightest && busiest.name !== lightest.name) {
      html += '<div style="margin-top:12px;padding:10px 14px;background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.2);border-radius:8px;font-size:11px;color:#818cf8">' +
        '\u{1F4A1} \u8C03\u5EA6\u5EFA\u8BAE\uFF1A<b>' + _escHtml(busiest.name) + '</b> \u8D1F\u8F7D\u8F83\u9AD8\uFF0C\u5EFA\u8BAE\u5C06\u65B0\u4EFB\u52A1\u8DEF\u7531\u5230 <b>' + _escHtml(lightest.name) + '</b>' +
      '</div>';
    }

    body.innerHTML = html;
  } catch(e) {
    var b = document.getElementById('wl-body');
    if (b) b.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px">' + e.message + '</div>';
  }
}

// ════════════════════════════════════════════════════════
// 集群：增强通讯录 API 探针（OpenAPI 是否声明 contacts/enriched）
// ════════════════════════════════════════════════════════

async function _ttShowContactsEnrichedProbe() {
  _ttCloseModal();
  var overlay = document.createElement('div');
  overlay.className = 'ttp-modal-overlay';
  overlay.id = 'ttp-modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _ttCloseModal(); };
  overlay.innerHTML =
    '<div class="ttp-modal" style="width:780px;max-width:96vw">' +
      '<div class="ttp-modal-header">' +
        '<span style="font-size:20px">\u{1F52C}</span>' +
        '<div class="ttp-modal-title">\u96C6\u7FA4\u63A2\u9488 \u00B7 \u589E\u5F3A\u901A\u8BAF\u5F55 API</div>' +
        '<button class="ttp-modal-close" onclick="_ttCloseModal()">\u2715</button>' +
      '</div>' +
      '<div class="ttp-modal-body" id="probe-ce-body" style="min-height:220px">' +
        '<div style="text-align:center;padding:36px;color:#64748b">\u{23F3} \u6B63\u5728\u8BF7\u6C42 /system/cluster/probe-contacts-enriched ...</div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);

  try {
    var r = await api('GET', '/system/cluster/probe-contacts-enriched');
    var body = document.getElementById('probe-ce-body');
    if (!body) return;
    var s = r.summary || {};
    var anyOk = !!s.any_node_has_enriched;
    var html = '<div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:12px;font-size:11px">' +
      '<span style="color:#94a3b8">\u8017\u65F6 <b style="color:#e2e8f0">' + (r.elapsed_ms != null ? r.elapsed_ms : '-') + '</b> ms</span>' +
      '<span style="padding:3px 10px;border-radius:999px;font-size:10px;font-weight:600;' +
        (anyOk ? 'background:rgba(34,197,94,.12);color:#4ade80' : 'background:rgba(245,158,11,.12);color:#fbbf24') + '">' +
        (anyOk ? '\u81F3\u5C11\u4E00\u8282\u70B9\u5DF2\u58F0\u660E enriched' : '\u672A\u53D1\u73B0 enriched\u58F0\u660E\uFF08\u8BF7\u5347\u7EA7 Worker\uFF09') +
      '</span>' +
      '<button type="button" class="ttp-small-btn" style="margin-left:auto" onclick="_ttShowContactsEnrichedProbe()">\u21BB \u5237\u65B0</button>' +
    '</div>';

    function rowNode(label, d) {
      var ok = !!d.contacts_enriched;
      var st = d.http_status != null ? d.http_status : '\u2014';
      var err = d.error ? String(d.error) : '';
      var paths = d.openapi_paths_with_contact || [];
      var note = err ? ('<span style="color:#f87171">' + _escHtml(err) + '</span>') : (paths.length
        ? '<span style="opacity:.9">' + paths.slice(0, 5).map(function(p) { return _escHtml(p); }).join('<br>') + (paths.length > 5 ? '<br>\u2026' : '') + '</span>'
        : '\u2014');
      return '<tr>' +
        '<td style="padding:8px 10px;font-weight:600;color:#e2e8f0;vertical-align:top;white-space:nowrap">' + label + '</td>' +
        '<td style="padding:8px 10px;font-size:10px;word-break:break-all;color:#94a3b8;vertical-align:top">' + _escHtml(d.base || '') + '</td>' +
        '<td style="padding:8px 10px;text-align:center;vertical-align:top">' + (ok ? '<span style="color:#22c55e;font-weight:700">\u2713</span>' : '<span style="color:#f87171;font-weight:700">\u2717</span>') + '</td>' +
        '<td style="padding:8px 10px;text-align:center;vertical-align:top">' + st + '</td>' +
        '<td style="padding:8px 10px;font-size:10px;color:#64748b;vertical-align:top;line-height:1.45">' + note + '</td>' +
      '</tr>';
    }

    html += '<div style="overflow-x:auto;border:1px solid rgba(255,255,255,.06);border-radius:10px">' +
      '<table style="width:100%;border-collapse:collapse;font-size:11px">' +
      '<thead><tr style="background:rgba(255,255,255,.03);border-bottom:1px solid rgba(255,255,255,.08)">' +
        '<th style="text-align:left;padding:8px 10px;color:#64748b">\u8282\u70B9</th>' +
        '<th style="text-align:left;padding:8px 10px;color:#64748b">Base URL</th>' +
        '<th style="padding:8px 10px;color:#64748b">enriched</th>' +
        '<th style="padding:8px 10px;color:#64748b">HTTP</th>' +
        '<th style="text-align:left;padding:8px 10px;color:#64748b">\u5907\u6CE8 / \u8DEF\u5F84\u6837\u4F8B</th>' +
      '</tr></thead><tbody>' +
      rowNode('\u672C\u673A\u4E3B\u63A7', r.local || {}) +
      (r.workers || []).map(function(w, i) { return rowNode('Worker ' + (i + 1), w); }).join('') +
      '</tbody></table></div>';

    html += '<p style="margin-top:12px;font-size:10px;color:#64748b;line-height:1.55">\u4EC5\u68C0\u67E5 OpenAPI \u662F\u5426\u5305\u542B <code style="font-size:10px">contacts/enriched</code>\uFF0C\u4E0D\u4EE3\u8868\u5355\u53F0\u8BBE\u5907\u5DF2\u8FDE\u63A5\u3002Worker \u663E\u793A \u2717 \u65F6\uFF0C\u8BF7\u5C06\u8BE5\u673A\u4E0E\u4E3B\u63A7\u540C\u6B65\u4EE3\u7801\u5E76\u91CD\u542F\u670D\u52A1\u3002</p>';

    body.innerHTML = html;
  } catch(e) {
    var b = document.getElementById('probe-ce-body');
    if (b) {
      b.innerHTML = '<div style="color:#f87171;text-align:center;padding:28px;font-size:12px">' + _escHtml(e.message || String(e)) + '</div>' +
        '<div style="text-align:center;margin-top:8px"><button type="button" class="ttp-small-btn" onclick="_ttShowContactsEnrichedProbe()">\u91CD\u8BD5</button></div>';
    }
  }
}

// ════════════════════════════════════════════════════════
// 全链路转化漏斗弹窗
// ════════════════════════════════════════════════════════

async function _ttShowConversionFunnel() {
  _ttCloseModal();
  var overlay = document.createElement('div');
  overlay.className = 'ttp-modal-overlay';
  overlay.id = 'ttp-modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _ttCloseModal(); };
  overlay.innerHTML =
    '<div class="ttp-modal" style="width:820px">' +
      '<div class="ttp-modal-header">' +
        '<span style="font-size:20px">\u{1F3AF}</span>' +
        '<div class="ttp-modal-title">\u5168\u94FE\u8DEF\u8F6C\u5316\u6F0F\u6597</div>' +
        '<button class="ttp-small-btn" style="margin-left:auto;margin-right:4px;color:#22c55e;border-color:rgba(34,197,94,.3)" onclick="_ttExportFunnel()" title="\u5BFC\u51FA\u6570\u636E">\u{1F4E5} \u5BFC\u51FA</button>' +
        '<button class="ttp-modal-close" onclick="_ttCloseModal()">\u2715</button>' +
      '</div>' +
      '<div class="ttp-modal-body" id="cvf-body" style="min-height:420px">' +
        '<div style="text-align:center;padding:60px;color:#64748b">\u{23F3} \u52A0\u8F7D\u8F6C\u5316\u6570\u636E...</div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);

  try {
    var r = await api('GET', '/tiktok/funnel');
    var stages = r.stages || [];
    var convStats = r.conversation_stats || {};
    var devCompare = r.device_compare || [];
    var body = document.getElementById('cvf-body');
    if (!body) return;

    // ── 顶部 KPI 摘要 ──
    var topWatched = stages[0] ? stages[0].value : 0;
    var topConverted = stages[6] ? stages[6].value : 0;
    var overallRate = topWatched > 0 ? (topConverted / topWatched * 100).toFixed(2) : '0';
    var kpiHTML = '<div class="cvf-kpi-row">' +
      '<div class="cvf-kpi"><div class="cvf-kpi-num" style="color:#8b5cf6">' + topWatched + '</div><div class="cvf-kpi-label">\u603B\u89C6\u9891</div></div>' +
      '<div class="cvf-kpi"><div class="cvf-kpi-num" style="color:#f59e0b">' + (r.total_dms || 0) + '</div><div class="cvf-kpi-label">\u603B\u79C1\u4FE1</div></div>' +
      '<div class="cvf-kpi"><div class="cvf-kpi-num" style="color:#22c55e">' + (r.leads_responded || 0) + '</div><div class="cvf-kpi-label">\u5DF2\u56DE\u590D</div></div>' +
      '<div class="cvf-kpi"><div class="cvf-kpi-num" style="color:#ef4444">' + topConverted + '</div><div class="cvf-kpi-label">\u5DF2\u8F6C\u5316</div></div>' +
      '<div class="cvf-kpi"><div class="cvf-kpi-num" style="color:' + (parseFloat(overallRate) >= 1 ? '#22c55e' : '#f59e0b') + '">' + overallRate + '%</div><div class="cvf-kpi-label">\u603B\u8F6C\u5316\u7387</div></div>' +
    '</div>';

    // ── 漏斗阶段可视化 ──
    var funnelHTML = '<div class="cvf-funnel">';
    for (var i = 0; i < stages.length; i++) {
      var s = stages[i];
      var widthPct = topWatched > 0 ? Math.max(8, s.value / topWatched * 100) : 8;
      var crText = i > 0 ? '<span class="cvf-cr">' + (s.conversion_rate || 0) + '%</span>' : '';
      funnelHTML +=
        (i > 0 ? '<div class="cvf-arrow">\u25BC <span style="font-size:10px;color:' + (s.conversion_rate >= 30 ? '#22c55e' : s.conversion_rate >= 10 ? '#f59e0b' : '#ef4444') + '">' + (s.conversion_rate || 0) + '%</span></div>' : '') +
        '<div class="cvf-stage">' +
          '<div class="cvf-bar" style="width:' + widthPct + '%;background:' + s.color + '22;border:1px solid ' + s.color + '44">' +
            '<div class="cvf-bar-fill" style="width:100%;background:' + s.color + '33"></div>' +
            '<span class="cvf-bar-text">' +
              '<b style="color:' + s.color + '">' + s.value + '</b> ' + s.name +
            '</span>' +
          '</div>' +
        '</div>';
    }
    funnelHTML += '</div>';

    // ── 对话深度统计 ──
    var convHTML = '';
    if (convStats.total > 0) {
      convHTML = '<div class="cvf-section">' +
        '<div class="cvf-section-title">\u{1F4AC} \u5BF9\u8BDD\u6DF1\u5EA6\u7EDF\u8BA1</div>' +
        '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">' +
          '<div class="cvf-mini-kpi">' +
            '<span style="color:#60a5fa;font-weight:700;font-size:16px">' + convStats.total + '</span>' +
            '<span style="font-size:10px;color:#64748b">\u603B\u5BF9\u8BDD</span>' +
          '</div>' +
          '<div class="cvf-mini-kpi">' +
            '<span style="color:#818cf8;font-weight:700;font-size:16px">' + convStats.deep + ' <small style="font-size:10px;color:#64748b">(' + (convStats.deep_rate||0) + '%)</small></span>' +
            '<span style="font-size:10px;color:#64748b">\u6DF1\u5EA6\u5BF9\u8BDD(\u22655\u8F6E)</span>' +
          '</div>' +
          '<div class="cvf-mini-kpi">' +
            '<span style="color:#22c55e;font-weight:700;font-size:16px">' + convStats.replied + ' <small style="font-size:10px;color:#64748b">(' + (convStats.reply_rate||0) + '%)</small></span>' +
            '<span style="font-size:10px;color:#64748b">\u6709\u7528\u6237\u56DE\u590D</span>' +
          '</div>' +
        '</div>' +
      '</div>';
    }

    // ── 设备对比表 ──
    var devHTML = '';
    if (devCompare.length) {
      devHTML = '<div class="cvf-section">' +
        '<div class="cvf-section-title">\u{1F4F1} \u8BBE\u5907\u8F6C\u5316\u5BF9\u6BD4</div>' +
        '<table class="ctm-table"><thead><tr>' +
          '<th style="text-align:left">\u8BBE\u5907</th>' +
          '<th style="text-align:center">\u89C6\u9891</th>' +
          '<th style="text-align:center">\u5173\u6CE8</th>' +
          '<th style="text-align:center">\u79C1\u4FE1</th>' +
          '<th style="text-align:center">\u5173\u6CE8\u7387</th>' +
          '<th style="text-align:center">\u79C1\u4FE1\u7387</th>' +
        '</tr></thead><tbody>';
      devCompare.forEach(function(d) {
        var frColor = d.follow_rate >= 10 ? '#22c55e' : d.follow_rate >= 5 ? '#f59e0b' : '#94a3b8';
        var drColor = d.dm_rate >= 20 ? '#22c55e' : d.dm_rate >= 10 ? '#f59e0b' : '#94a3b8';
        devHTML += '<tr>' +
          '<td style="color:#e2e8f0">' + _escHtml(d.alias) + '</td>' +
          '<td style="text-align:center;color:#8b5cf6">' + d.watched + '</td>' +
          '<td style="text-align:center;color:#3b82f6">' + d.followed + '</td>' +
          '<td style="text-align:center;color:#f59e0b">' + d.dms + '</td>' +
          '<td style="text-align:center"><span style="color:' + frColor + ';font-weight:600">' + d.follow_rate + '%</span></td>' +
          '<td style="text-align:center"><span style="color:' + drColor + ';font-weight:600">' + d.dm_rate + '%</span></td>' +
        '</tr>';
      });
      devHTML += '</tbody></table></div>';
    }

    body.innerHTML = kpiHTML + funnelHTML + convHTML + devHTML;
  } catch(e) {
    var b = document.getElementById('cvf-body');
    if (b) b.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px">' + e.message + '</div>';
  }
}

function _ttExportFunnel() {
  fetch('/tiktok/funnel', {credentials:'include'}).then(function(r){return r.json();}).then(function(d) {
    var stages = d.stages || [];
    var rows = ['\u9636\u6BB5,\u6570\u91CF,\u8F6C\u5316\u7387'];
    stages.forEach(function(s) { rows.push(s.name + ',' + s.value + ',' + (s.conversion_rate||100) + '%'); });
    if (d.device_compare) {
      rows.push(''); rows.push('\u8BBE\u5907,\u89C6\u9891,\u5173\u6CE8,\u79C1\u4FE1,\u5173\u6CE8\u7387,\u79C1\u4FE1\u7387');
      d.device_compare.forEach(function(v) { rows.push(v.alias+','+v.watched+','+v.followed+','+v.dms+','+v.follow_rate+'%,'+v.dm_rate+'%'); });
    }
    _downloadCSV('\u8F6C\u5316\u6F0F\u6597_' + new Date().toISOString().slice(0,10) + '.csv', rows.join('\n'));
  }).catch(function(e){ _toast('\u5BFC\u51FA\u5931\u8D25: '+e.message,'error'); });
}

function _ttShowBatchHistory() {
  var logs = [];
  try { logs = JSON.parse(localStorage.getItem('tt_batch_logs') || '[]'); } catch(e) {}
  _ttCloseModal();
  var overlay = document.createElement('div');
  overlay.className = 'ttp-modal-overlay';
  overlay.id = 'ttp-modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _ttCloseModal(); };

  var rows = '';
  if (!logs.length) {
    rows = '<div style="text-align:center;color:#64748b;padding:40px">\u{1F4AD} \u6682\u65E0\u6279\u91CF\u64CD\u4F5C\u8BB0\u5F55</div>';
  } else {
    rows = '<table class="ctm-table"><thead><tr>' +
      '<th style="text-align:left">\u65F6\u95F4</th>' +
      '<th style="text-align:left">\u64CD\u4F5C</th>' +
      '<th style="text-align:center">\u8BBE\u5907</th>' +
      '<th style="text-align:center">\u7ED3\u679C</th>' +
      '<th style="text-align:left">\u8BE6\u60C5</th>' +
    '</tr></thead><tbody>';
    logs.forEach(function(l) {
      var t = l.ts ? l.ts.replace('T',' ').slice(0,16) : '-';
      var detail = '';
      if (l.contacts) detail += l.contacts + '\u6761';
      if (l.file) detail += ' (' + l.file + ')';
      var result = (l.success||0) + '/' + (l.devices||0) + ' \u6210\u529F';
      rows += '<tr>' +
        '<td style="color:#94a3b8;font-size:11px;white-space:nowrap">' + t + '</td>' +
        '<td style="color:#e2e8f0;font-size:12px">' + (l.action||'-') + '</td>' +
        '<td style="text-align:center;color:#818cf8">' + (l.devices||'-') + '</td>' +
        '<td style="text-align:center"><span class="ctm-tag ' +
          ((l.success||0) >= (l.devices||1) ? 'ctm-tag-green' : 'ctm-tag-indigo') + '">' + result + '</span></td>' +
        '<td style="color:#94a3b8;font-size:11px">' + detail + '</td>' +
      '</tr>';
    });
    rows += '</tbody></table>';
  }

  overlay.innerHTML =
    '<div class="ttp-modal" style="width:720px">' +
      '<div class="ttp-modal-header">' +
        '<span style="font-size:20px">\u{1F4CB}</span>' +
        '<div class="ttp-modal-title">\u6279\u91CF\u64CD\u4F5C\u5386\u53F2</div>' +
        '<span id="bh-archive-tag" style="font-size:10px;color:#64748b;margin-left:8px"></span>' +
        '<button class="ttp-small-btn" style="margin-left:auto;margin-right:4px;color:#818cf8;border-color:rgba(99,102,241,.3)" onclick="_ttLoadArchivedLogs()" title="\u52A0\u8F7D\u540E\u7AEF\u5F52\u6863">\u{1F4E6} \u5F52\u6863\u8BB0\u5F55</button>' +
        '<button class="ttp-small-btn" style="margin-right:4px;color:#ef4444;border-color:rgba(239,68,68,.3)" onclick="localStorage.removeItem(\'tt_batch_logs\');_ttShowBatchHistory()">\u{1F5D1} \u6E05\u7A7A</button>' +
        '<button class="ttp-modal-close" onclick="_ttCloseModal()">\u2715</button>' +
      '</div>' +
      '<div class="ttp-modal-body" style="max-height:500px;overflow-y:auto" id="bh-body">' + rows + '</div>' +
    '</div>';
  document.body.appendChild(overlay);
  fetch('/ops-logs/stats',{credentials:'include'}).then(function(r){return r.json();}).then(function(d){
    var tag = document.getElementById('bh-archive-tag');
    if(tag && d.total_operations) tag.textContent = '\u{1F4E6} \u5DF2\u5F52\u6863 ' + d.total_operations + ' \u6761';
  }).catch(function(){});
}

async function _ttLoadArchivedLogs() {
  var body = document.getElementById('bh-body');
  if(!body) return;
  body.innerHTML = '<div style="text-align:center;padding:30px;color:#64748b">\u52A0\u8F7D\u4E2D...</div>';
  try {
    var r = await fetch('/ops-logs?limit=100',{credentials:'include'});
    var d = await r.json();
    var logs = d.logs || [];
    if(!logs.length) { body.innerHTML = '<div style="text-align:center;padding:40px;color:#64748b">\u540E\u7AEF\u65E0\u5F52\u6863\u8BB0\u5F55</div>'; return; }
    var html = '<div style="font-size:11px;color:#818cf8;padding:4px 0;margin-bottom:4px">\u{1F4E6} \u540E\u7AEF\u5F52\u6863 ' + d.total + ' \u6761\uFF08\u663E\u793A\u6700\u8FD1 100\uFF09</div>';
    html += '<table class="ctm-table"><thead><tr>' +
      '<th style="text-align:left">\u65F6\u95F4</th>' +
      '<th style="text-align:left">\u64CD\u4F5C</th>' +
      '<th style="text-align:center">\u8BBE\u5907</th>' +
      '<th style="text-align:center">\u7ED3\u679C</th>' +
      '<th style="text-align:left">\u8BE6\u60C5</th>' +
    '</tr></thead><tbody>';
    logs.forEach(function(l) {
      var t = (l.ts||'').replace('T',' ').slice(0,16) || '-';
      var detail = '';
      if(l.contacts) detail += l.contacts + '\u6761';
      if(l.file) detail += ' (' + l.file + ')';
      var result = (l.success||0) + '/' + (l.devices||0) + ' \u6210\u529F';
      html += '<tr>' +
        '<td style="color:#94a3b8;font-size:11px;white-space:nowrap">' + t + '</td>' +
        '<td style="color:#e2e8f0;font-size:12px">' + (l.action||'-') + '</td>' +
        '<td style="text-align:center;color:#818cf8">' + (l.devices||'-') + '</td>' +
        '<td style="text-align:center"><span class="ctm-tag ' +
          ((l.success||0)>=(l.devices||1) ? 'ctm-tag-green' : 'ctm-tag-indigo') + '">' + result + '</span></td>' +
        '<td style="color:#94a3b8;font-size:11px">' + detail + '</td></tr>';
    });
    html += '</tbody></table>';
    body.innerHTML = html;
  } catch(e) { body.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px">' + e.message + '</div>'; }
}

// ════════════════════════════════════════════════════════
// P1: 全局批量流程配置（为所有设备一键配置）
// ════════════════════════════════════════════════════════
async function ttBatchFlowConfig() {
  var devs = Object.values(window._ttDevicesCache || {}).filter(function(d){return d.online;});
  if (!devs.length) { if (typeof showToast==='function') showToast('无在线设备','warn'); return; }
  _ttFlowBatchMode = true;
  _ttFlowBatchCount = devs.length;
  await _ttOpenFlowConfig(devs[0].device_id);
}

// ════════════════════════════════════════════════════════
// P2: 流程执行实时进度追踪
// 轮询每个步骤任务状态，在设备面板内实时更新
// ════════════════════════════════════════════════════════

const _ttActiveTrackers = {}; // deviceId -> intervalId（防止重复轮询）

async function _ttTrackFlowProgress(deviceId, steps, flowTasks) {
  const safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  const progEl = document.getElementById('tt-prog-panel-' + safeId);
  if (!progEl) return;

  // 停止已有的追踪
  if (_ttActiveTrackers[deviceId]) {
    clearInterval(_ttActiveTrackers[deviceId]);
    delete _ttActiveTrackers[deviceId];
  }

  // 初始状态映射：taskId -> status
  const statusMap = {};
  flowTasks.forEach(function(t) {
    if (t.task_id) statusMap[t.task_id] = {status: 'pending', result: null};
    if (!t.ok) statusMap[t.task_id || t.type] = {status: 'failed', result: null};
  });

  progEl.style.display = 'block';
  _ttRenderProgPanel(progEl, steps, flowTasks, statusMap, deviceId);

  let pollCount = 0;
  const taskIds = flowTasks.filter(function(t){return t.task_id && t.ok;}).map(function(t){return t.task_id;});
  if (!taskIds.length) return;

  const doPoll = async function() {
    pollCount++;
    if (pollCount > 120) { // 10分钟最大追踪
      clearInterval(_ttActiveTrackers[deviceId]);
      delete _ttActiveTrackers[deviceId];
      return;
    }

    let allDone = true;
    await Promise.all(taskIds.map(async function(tid) {
      try {
        const r = await fetch('/tasks/' + tid, {credentials: 'include'}).then(function(x){return x.json();});
        statusMap[tid] = {
          status: r.status || 'pending',
          result: r.result || null,
          progress: r.progress || null,
          progress_msg: (r.result || {}).progress_msg || ''
        };
        if (!['completed','failed','cancelled'].includes(r.status)) allDone = false;
      } catch(e) {
        allDone = false;
      }
    }));

    // 检查面板是否还在DOM中
    const el = document.getElementById('tt-prog-panel-' + safeId);
    if (!el) {
      clearInterval(_ttActiveTrackers[deviceId]);
      delete _ttActiveTrackers[deviceId];
      return;
    }
    _ttRenderProgPanel(el, steps, flowTasks, statusMap, deviceId);

    if (allDone) {
      clearInterval(_ttActiveTrackers[deviceId]);
      delete _ttActiveTrackers[deviceId];
      // 完成后更新动作结果区
      const doneCount = Object.values(statusMap).filter(function(s){return s.status==='completed';}).length;
      const failCount = Object.values(statusMap).filter(function(s){return s.status==='failed';}).length;
      _ttSetActionResult(deviceId,
        '✓ 流程完成 · ' + doneCount + '步成功' + (failCount ? ' · ' + failCount + '步失败' : ''),
        doneCount > 0 ? '#22c55e' : '#f87171');
      // P4-2: 保存 warmup geo 统计到 localStorage，供卡片展示
      try {
        Object.values(statusMap).forEach(function(st) {
          if (st.status === 'completed' && st.result) {
            const ws = st.result.warmup_stats || (st.result.watched !== undefined ? st.result : null);
            if (ws && ws.geo_stats && Object.keys(ws.geo_stats).length > 0) {
              const geoData = {
                ts: Date.now(),
                geo_stats: ws.geo_stats,
                geo_match_watched: ws.geo_match_watched || 0,
                watched: ws.watched || 0,
              };
              localStorage.setItem('tt_warmup_geo_' + deviceId, JSON.stringify(geoData));
            }
          }
        });
      } catch(e) {}
      // 自动刷新卡片数据
      setTimeout(function(){_loadTtDeviceGrid();}, 2000);
    }
  };

  // 立即轮询一次，然后每5秒轮询
  await doPoll();
  _ttActiveTrackers[deviceId] = setInterval(doPoll, 5000);
}

function _ttRenderProgPanel(el, steps, flowTasks, statusMap, deviceId) {
  const safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');

  // 计算整体进度
  const total = flowTasks.length;
  const doneN = Object.values(statusMap).filter(function(s){return s.status==='completed';}).length;
  const failN = Object.values(statusMap).filter(function(s){return s.status==='failed';}).length;
  const runN  = Object.values(statusMap).filter(function(s){return s.status==='running';}).length;
  const pct = total > 0 ? Math.round(((doneN + failN) / total) * 100) : 0;

  const hasActive = _ttActiveTrackers[deviceId];

  let html = '<div class="tt-prog-header">' +
    '<span class="tt-prog-title">⚡ 流程执行中</span>' +
    (hasActive ? '<button class="tt-prog-cancel" onclick="_ttCancelFlowProgress(\'' + deviceId + '\')" title="停止追踪">停止追踪</button>' : '') +
  '</div>';

  steps.forEach(function(step) {
    const ft = flowTasks.find(function(t){return t.type === step.type || (step.type==='__pitch__' && t.type==='pitch');});
    if (!ft) return;
    const tid = ft.task_id;
    const st = tid ? (statusMap[tid] || {status: 'pending'}) : {status: ft.ok ? 'pending' : 'failed'};
    const stClass = 'st-' + (st.status || 'pending');

    let statusText = '';
    let iconHtml = '';
    switch(st.status) {
      case 'pending':   iconHtml = '○'; statusText = '等待中'; break;
      case 'running':   iconHtml = '↻'; statusText = st.progress_msg || '执行中...'; break;
      case 'completed': iconHtml = '✓'; statusText = _ttStepResultSummary(step.id, st); break;
      case 'failed':    iconHtml = '✗'; statusText = '失败'; break;
      case 'cancelled': iconHtml = '−'; statusText = '已取消'; break;
      default:          iconHtml = '?'; statusText = st.status;
    }

    html += '<div class="tt-prog-step ' + stClass + '">' +
      '<span class="tt-prog-icon">' + iconHtml + '</span>' +
      '<span class="tt-prog-name">' + step.icon + ' ' + step.name + '</span>' +
      '<span class="tt-prog-status">' + statusText + '</span>' +
    '</div>';
  });

  html += '<div class="tt-prog-bar-wrap"><div class="tt-prog-bar" style="width:' + pct + '%"></div></div>';
  el.innerHTML = html;
}

function _ttStepResultSummary(stepId, st) {
  if (!st || !st.result) return '完成';
  const r = st.result;
  // 根据步骤类型提取关键结果
  if (stepId === 'follow' || (r.follow_result)) {
    const n = (r.follow_result || {}).followed || r.followed;
    return n !== undefined ? '关注 ' + n + ' 人' : '完成';
  }
  if (stepId === 'inbox' || r.new_messages !== undefined) {
    return (r.new_messages || 0) + ' 条新消息';
  }
  if (stepId === 'warmup' || r.warmup_stats) {
    const ws = r.warmup_stats || r || {};
    const watched = ws.watched || 0;
    const matchN = ws.geo_match_watched || 0;
    const geoStats = ws.geo_stats || {};
    let s = '看 ' + watched + ' 个视频';
    if (matchN > 0 && watched > 0) {
      const pct = Math.round(matchN / watched * 100);
      s += ' · 目标' + pct + '%';
    }
    // Show top-2 country flags with counts
    const topGeo = Object.entries(geoStats).sort(function(a,b){return b[1]-a[1];}).slice(0,3);
    const _geoFlagMap = {'PH':'🇵🇭','ID':'🇮🇩','MY':'🇲🇾','TH':'🇹🇭','VN':'🇻🇳','SG':'🇸🇬',
      'AE':'🇦🇪','SA':'🇸🇦','QA':'🇶🇦','BR':'🇧🇷','MX':'🇲🇽','CO':'🇨🇴','AR':'🇦🇷',
      'US':'🇺🇸','GB':'🇬🇧','CA':'🇨🇦','AU':'🇦🇺','DE':'🇩🇪','FR':'🇫🇷','IT':'🇮🇹',
      'NG':'🇳🇬','KE':'🇰🇪','IN':'🇮🇳','JP':'🇯🇵','KR':'🇰🇷','TW':'🇹🇼'};
    if (topGeo.length > 0) {
      s += '  ' + topGeo.map(function(x){ return (_geoFlagMap[x[0]]||x[0]) + x[1]; }).join(' ');
    }
    return s;
  }
  if (stepId === 'followbacks') {
    const sent = r.sent_dms || r.dms_sent || 0;
    return '私信 ' + sent + ' 人';
  }
  if (stepId === 'pitch') {
    return '发送 ' + (r.sent || r.pitched || 0) + ' 条';
  }
  if (stepId === 'kwsearch' || r.keywords_used !== undefined) {
    const followed = r.followed || 0;
    const warmed = r.comment_warmed || 0;
    const kws = (r.keywords_used || []).length;
    return `关注${followed}人 · 预热${warmed} · ${kws}词`;
  }
  if (stepId === 'cmtreplies' || r.dmed !== undefined && r.users !== undefined && stepId !== 'followbacks') {
    return `DM ${r.dmed||0}位 · 检查${r.checked||0}条回复`;
  }
  if (stepId === 'live' || r.rooms_visited !== undefined) {
    const rooms = r.rooms_visited || 0;
    const comments = r.comments_sent || 0;
    const followed = (r.hosts_followed || 0) + (r.viewers_followed || 0);
    return `${rooms}直播间 · 评论${comments} · 关注${followed}`;
  }
  return '完成';
}

function _ttCancelFlowProgress(deviceId) {
  if (_ttActiveTrackers[deviceId]) {
    clearInterval(_ttActiveTrackers[deviceId]);
    delete _ttActiveTrackers[deviceId];
  }
  const safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  const el = document.getElementById('tt-prog-panel-' + safeId);
  if (el) el.style.display = 'none';
}

// ════════════════════════════════════════════════════════
// P3: 设备卡片流程角色标识条
// 从 localStorage 读取配置，生成步骤图标行
// ════════════════════════════════════════════════════════

function _ttGetRoleStrip(deviceId) {
  try {
    const raw = localStorage.getItem('tt_flow_' + deviceId);
    if (!raw) return '';
    const config = JSON.parse(raw);
    const enabledSteps = _TT_FLOW_STEPS_DEF.filter(function(s){
      return config[s.id] && config[s.id].enabled;
    });
    if (!enabledSteps.length) return '';

    // 判断属于哪个预设
    const enabledIds = enabledSteps.map(function(s){return s.id;}).sort().join(',');
    let presetLabel = '';
    Object.keys(_TT_FLOW_PRESETS).forEach(function(k) {
      const p = _TT_FLOW_PRESETS[k];
      if (p.steps.slice().sort().join(',') === enabledIds) presetLabel = p.name;
    });

    const iconsHtml = enabledSteps.map(function(s) {
      return '<span class="tt-dev-role-icon active" title="' + s.name + '">' + s.icon + '</span>';
    }).join('');

    // Show geo flags if configured
    let geoHtml = '';
    try {
      const geoRaw = localStorage.getItem('tt_geo_' + deviceId);
      if (geoRaw) {
        const geo = JSON.parse(geoRaw);
        const tcs = geo.target_countries || [];
        const _geoFlagMap = {'PH':'🇵🇭','ID':'🇮🇩','MY':'🇲🇾','TH':'🇹🇭','VN':'🇻🇳','SG':'🇸🇬',
          'AE':'🇦🇪','SA':'🇸🇦','QA':'🇶🇦','BR':'🇧🇷','MX':'🇲🇽','CO':'🇨🇴','AR':'🇦🇷',
          'US':'🇺🇸','GB':'🇬🇧','CA':'🇨🇦','AU':'🇦🇺','DE':'🇩🇪','FR':'🇫🇷','IT':'🇮🇹',
          'NG':'🇳🇬','KE':'🇰🇪','IN':'🇮🇳','JP':'🇯🇵','KR':'🇰🇷','TW':'🇹🇼'};
        const flags = tcs.slice(0,2).map(function(c){ return _geoFlagMap[c] || c; }).join('');
        if (flags) geoHtml = '<span class="tt-dev-role-geo" title="目标地区: ' + tcs.join(',') + '">' + flags + '</span>';
      }
    } catch(e) {}

    return '<div class="tt-dev-role-strip">' +
      iconsHtml +
      (presetLabel ? '<span class="tt-dev-role-label">' + presetLabel + '</span>' : '') +
      geoHtml +
    '</div>';
  } catch(e) { return ''; }
}

// ════════════════════════════════════════════════════════
// P4-2: 设备卡片 GEO 统计条
// 读取上次 warmup 地理统计，显示 Top-3 国家旗帜+数量
// ════════════════════════════════════════════════════════
const _GEO_FLAG_MAP = {
  'PH':'🇵🇭','ID':'🇮🇩','MY':'🇲🇾','TH':'🇹🇭','VN':'🇻🇳','SG':'🇸🇬',
  'AE':'🇦🇪','SA':'🇸🇦','QA':'🇶🇦','KW':'🇰🇼','EG':'🇪🇬',
  'BR':'🇧🇷','MX':'🇲🇽','CO':'🇨🇴','AR':'🇦🇷','CL':'🇨🇱',
  'US':'🇺🇸','GB':'🇬🇧','CA':'🇨🇦','AU':'🇦🇺',
  'DE':'🇩🇪','FR':'🇫🇷','IT':'🇮🇹','ES':'🇪🇸','NL':'🇳🇱',
  'NG':'🇳🇬','KE':'🇰🇪','GH':'🇬🇭','ZA':'🇿🇦',
  'IN':'🇮🇳','JP':'🇯🇵','KR':'🇰🇷','TW':'🇹🇼','CN':'🇨🇳',
};

function _ttGetGeoBar(deviceId) {
  try {
    const raw = localStorage.getItem('tt_warmup_geo_' + deviceId);
    if (!raw) return '';
    const data = JSON.parse(raw);
    const geoStats = data.geo_stats || {};
    const total = data.watched || 0;
    const matchN = data.geo_match_watched || 0;
    const topGeo = Object.entries(geoStats).sort(function(a,b){return b[1]-a[1];}).slice(0,3);
    if (!topGeo.length) return '';

    const matchPct = total > 0 ? Math.round(matchN / total * 100) : 0;
    const flagsHtml = topGeo.map(function(x) {
      return '<span title="' + x[0] + ': ' + x[1] + '个视频">' + (_GEO_FLAG_MAP[x[0]] || x[0]) + '<b>' + x[1] + '</b></span>';
    }).join('');

    // Age: show how old this data is
    const ageMs = Date.now() - (data.ts || 0);
    const ageH = Math.round(ageMs / 3600000);
    const ageLabel = ageH < 1 ? '刚刚' : ageH < 24 ? ageH + 'h前' : Math.round(ageH/24) + 'd前';

    return '<div class="tt-dev-geo-bar" title="上次刷视频地理分布 · ' + ageLabel + '">' +
      '<span style="font-size:9px;color:var(--text-muted)">🌍</span>' +
      flagsHtml +
      (matchPct > 0 ? '<span style="font-size:9px;color:#22c55e;margin-left:2px">目标' + matchPct + '%</span>' : '') +
    '</div>';
  } catch(e) { return ''; }
}

// P4-3: 将 GEO 配置同步到对应设备的定时任务 params
async function _ttSyncGeoToScheduledJobs(deviceId) {
  try {
    const geoRaw = localStorage.getItem('tt_geo_' + deviceId);
    if (!geoRaw) return;
    const geo = JSON.parse(geoRaw);
    const tcs = geo.target_countries || [];
    const tls = geo.target_languages || [];
    if (!tcs.length && !tls.length) return;

    const jobs = await fetch('/scheduled-jobs', {credentials:'include'}).then(function(r){return r.json();});
    const warmupActions = ['tiktok_warmup','tiktok_warmup_post_follow_am','tiktok_warmup_post_follow_pm','tiktok_warmup_prime_time'];
    const followActions = ['tiktok_follow','tiktok_follow_daily'];
    const inboxActions = ['tiktok_check_inbox','tiktok_inbox_10m'];
    const targetJobs = jobs.filter(function(j) {
      const a = j.action || '';
      const p = j.params || {};
      return (warmupActions.includes(a) || followActions.includes(a) || inboxActions.includes(a)) &&
             (p.device_id === deviceId || !p.device_id);
    });

    await Promise.all(targetJobs.map(async function(j) {
      const newParams = Object.assign({}, j.params || {});
      const a = j.action || '';
      if (warmupActions.includes(a) || followActions.includes(a)) {
        newParams.target_countries = tcs;
        newParams.target_languages = tls;
        if (warmupActions.includes(a)) newParams.geo_filter = tcs.length > 0;
      } else if (inboxActions.includes(a)) {
        newParams.target_languages = tls;
      }
      await fetch('/scheduled-jobs/' + j.id, {
        method:'PUT', headers:{'Content-Type':'application/json'}, credentials:'include',
        body: JSON.stringify({params: newParams})
      });
    }));

    if (targetJobs.length > 0) {
      console.log('[GEO] 已更新 ' + targetJobs.length + ' 个定时任务的地理配置');
    }
  } catch(e) {
    console.debug('[GEO] 定时任务同步跳过:', e);
  }
}

// ════════════════════════════════════════════════════════
// P4: 流程执行历史（最近10次，localStorage持久化）
// 支持一键重播：点击历史记录直接加载配置
// ════════════════════════════════════════════════════════

function _ttSaveFlowHistory(deviceId, steps, flowTasks) {
  try {
    const key = 'tt_hist_' + deviceId;
    const existing = JSON.parse(localStorage.getItem(key) || '[]');
    const entry = {
      ts: Date.now(),
      steps: steps.map(function(s){return {id:s.id, icon:s.icon, name:s.name};}),
      taskCount: flowTasks.filter(function(t){return t.ok;}).length,
      config: JSON.parse(JSON.stringify(_ttFlowStates)) // 深拷贝当前配置
    };
    existing.unshift(entry);
    localStorage.setItem(key, JSON.stringify(existing.slice(0, 10))); // 最多保留10条
  } catch(e) {}
}

function _ttGetFlowHistory(deviceId) {
  try {
    return JSON.parse(localStorage.getItem('tt_hist_' + deviceId) || '[]');
  } catch(e) { return []; }
}

function _ttBuildHistorySection(deviceId, safeIdAttr) {
  const history = _ttGetFlowHistory(deviceId);
  if (!history.length) return ''; // 无历史记录则不显示该区域

  const now = Date.now();
  function relTime(ts) {
    const mins = Math.round((now - ts) / 60000);
    if (mins < 1) return '刚刚';
    if (mins < 60) return mins + '分前';
    const hrs = Math.round(mins / 60);
    if (hrs < 24) return hrs + '小时前';
    return Math.round(hrs / 24) + '天前';
  }

  const rows = history.slice(0, 3).map(function(entry, idx) {
    const icons = entry.steps.map(function(s){return '<span style="font-size:11px">' + s.icon + '</span>';}).join('');
    const configJson = JSON.stringify(entry.config).replace(/'/g, "\\'").replace(/"/g, '&quot;');
    return '<div class="tt-hist-row" onclick="_ttFlowReplay(\'' + deviceId + '\',' + idx + ')" title="点击重播此次配置">' +
      '<span class="tt-hist-time">' + relTime(entry.ts) + '</span>' +
      '<span class="tt-hist-steps">' + icons + '</span>' +
      '<span class="tt-hist-result">' + entry.taskCount + '个任务</span>' +
      '<span class="tt-hist-replay">重播</span>' +
    '</div>';
  }).join('');

  return '<div class="tt-panel-section tt-hist-section">' +
    '<div class="tt-panel-section-title" style="display:flex;align-items:center;justify-content:space-between">' +
      '执行历史' +
      '<span style="font-size:9px;color:var(--text-muted);font-weight:400">点击重播</span>' +
    '</div>' +
    rows +
  '</div>';
}

function _ttFlowReplay(deviceId, historyIdx) {
  var history = _ttGetFlowHistory(deviceId);
  var entry = history[historyIdx];
  if (!entry || !entry.config) return;
  _ttFlowDevId = deviceId;
  _ttFlowStates = JSON.parse(JSON.stringify(entry.config));
  _ttFlowCurrentPreset = null;
  _ttFlowBatchMode = false;
  if (!document.getElementById('tt-flow-overlay')) _ttBuildFlowModal();
  var dev = (window._ttDevicesCache || {})[deviceId] || {};
  var titleEl = document.getElementById('tt-flow-dev-name');
  if (titleEl) titleEl.innerHTML = (dev.alias || deviceId.substring(0,8)) + ' <span style="color:#818cf8;font-size:11px">(重播历史)</span>';
  _ttFlowRender();
  document.getElementById('tt-flow-overlay').classList.add('open');
  if (typeof showToast === 'function') showToast('已加载历史配置，可直接执行', 'info', 3000);
}

async function _ttDevInbox(deviceId, btn) {
  _ttBtnState(btn, '检查中...', true);
  _ttSetActionResult(deviceId, '', '');
  try {
    const r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/inbox', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
      body: JSON.stringify({})
    }).then(function(r) { return r.json(); });

    _ttBtnState(btn, '📋 检查收件箱', false);
    if (r.ok !== false) {
      const info = r.new_messages !== undefined ? '发现 ' + r.new_messages + ' 条新消息' : (r.message || '已检查');
      _ttSetActionResult(deviceId, '✓ ' + info, '#22c55e');
      if (typeof showToast === 'function') showToast(info, 'success');
    } else {
      _ttSetActionResult(deviceId, '✗ ' + (r.error || '检查失败'), '#f87171');
    }
  } catch(e) {
    _ttBtnState(btn, '📋 检查收件箱', false);
    _ttSetActionResult(deviceId, '✗ 网络错误: ' + e.message, '#f87171');
  }
}

async function _ttDevFollow(deviceId, btn) {
  _ttBtnState(btn, '关注中...', true);
  _ttSetActionResult(deviceId, '', '');
  try {
    const r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/follow', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
      body: JSON.stringify({ count: 10 })
    }).then(function(r) { return r.json(); });

    _ttBtnState(btn, '👤 关注用户', false);
    if (r.ok !== false) {
      const info = r.followed !== undefined ? '已关注 ' + r.followed + ' 人' : (r.message || '任务已提交');
      _ttSetActionResult(deviceId, '✓ ' + info, '#22c55e');
      if (typeof showToast === 'function') showToast(info, 'success');
    } else {
      _ttSetActionResult(deviceId, '✗ ' + (r.error || '操作失败'), '#f87171');
    }
  } catch(e) {
    _ttBtnState(btn, '👤 关注用户', false);
    _ttSetActionResult(deviceId, '✗ 网络错误: ' + e.message, '#f87171');
  }
}

async function _ttDevPitch(deviceId, btn) {
  _ttBtnState(btn, '发送中...', true);
  _ttSetActionResult(deviceId, '', '');
  try {
    const r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/pitch', {
      method: 'POST', headers: { 'Content-Type': 'application/json' }, credentials: 'include',
      body: JSON.stringify({ max_pitch: 5, cta_url: typeof _ttCtaUrl !== 'undefined' ? _ttCtaUrl : '' })
    }).then(function(r) { return r.json(); });

    _ttBtnState(btn, '💰 发话术', false);
    if (r.ok !== false) {
      const sent = r.sent || r.pitched || 0;
      const info = sent > 0 ? '已向 ' + sent + ' 人发话术' : (r.message || '无可发对象');
      _ttSetActionResult(deviceId, '✓ ' + info, sent > 0 ? '#22c55e' : '#f59e0b');
      if (typeof showToast === 'function') showToast(info, 'success');
      // 刷新线索列表
      setTimeout(function() { _ttLoadPanelLeads(deviceId, true); }, 1500);
    } else {
      _ttSetActionResult(deviceId, '✗ ' + (r.error || '发送失败'), '#f87171');
    }
  } catch(e) {
    _ttBtnState(btn, '💰 发话术', false);
    _ttSetActionResult(deviceId, '✗ 网络错误: ' + e.message, '#f87171');
  }
}

function _escHtml(s) {
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ── 单条线索发话术 ── */
async function ttSendPitch(leadId, name) {
  const btn = event.target;
  if(btn){ btn.disabled=true; btn.textContent='发送中...'; }
  try {
    const r = await fetch('/tiktok/pitch-hot-leads', {
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body: JSON.stringify({min_score:0, max_pitch:10, cta_url: _ttCtaUrl || ''})
    }).then(r=>r.json());
    if(btn){ btn.disabled=false; btn.innerHTML='&#128176; 发话术'; }
    // Find this lead's result
    const item = (r.items||[]).find(x => (x.lead||'').includes(name.replace('@','').slice(0,10)));
    if(item && item.task_id){
      showToast(`话术已排队: ${item.pitch_preview||''}`.slice(0,60), 'success');
      setTimeout(loadTtOpsPanel, 2000);
    } else if(item && item.error) {
      showToast('发送失败: ' + item.error, 'error');
    } else {
      showToast(r.message || '已处理', 'info');
      setTimeout(loadTtOpsPanel, 1500);
    }
  } catch(e) {
    if(btn){ btn.disabled=false; btn.innerHTML='&#128176; 发话术'; }
    showToast('操作失败: '+e, 'error');
  }
}

/* ttPitchHotLeads / ttLaunchCampaign / ttCheckInbox 由 tiktok-ops.js 提供（在 overview 之后加载） */

// ════════════════════════════════════════════════════════
// P3 — 运营日报面板
// ════════════════════════════════════════════════════════

function _ttShowReportPanel() {
  const existing = document.getElementById('tt-report-overlay');
  if (existing) { existing.remove(); return; }

  const now = new Date();
  const todayStr = now.toISOString().slice(0, 16);
  const startOfDay = now.toISOString().slice(0, 10) + 'T00:00';
  const sevenDaysAgo = new Date(now - 7 * 86400000).toISOString().slice(0, 10) + 'T00:00';

  const ov = document.createElement('div');
  ov.id = 'tt-report-overlay';
  ov.style.cssText = 'position:fixed;inset:0;z-index:9600;background:rgba(0,0,0,.7);backdrop-filter:blur(5px);display:flex;align-items:center;justify-content:center;padding:20px;animation:tt-toast-in .2s ease';

  ov.innerHTML =
    '<div id="tt-report-modal" style="background:#1e293b;border:1px solid rgba(255,255,255,.1);border-radius:14px;width:100%;max-width:560px;max-height:88vh;display:flex;flex-direction:column;box-shadow:0 24px 64px rgba(0,0,0,.7)">' +
      // 头部
      '<div style="padding:16px 20px 12px;border-bottom:1px solid rgba(255,255,255,.07);display:flex;align-items:center;justify-content:space-between;flex-shrink:0">' +
        '<div>' +
          '<div style="font-size:14px;font-weight:700;color:var(--text-main)">📊 运营日报</div>' +
          '<div style="font-size:11px;color:var(--text-muted);margin-top:2px">选择时间段生成AI分析报告</div>' +
        '</div>' +
        '<button onclick="document.getElementById(\'tt-report-overlay\').remove()" style="width:28px;height:28px;display:flex;align-items:center;justify-content:center;background:rgba(255,255,255,.06);border:1px solid rgba(255,255,255,.1);border-radius:7px;color:var(--text-muted);cursor:pointer;font-size:16px">×</button>' +
      '</div>' +
      // 时间选择器
      '<div style="padding:16px 20px 0;flex-shrink:0">' +
        '<div style="display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap">' +
          '<button onclick="_ttReportQuick(\'today\')" style="padding:4px 10px;font-size:11px;border-radius:6px;background:rgba(99,102,241,.15);border:1px solid rgba(99,102,241,.3);color:#a5b4fc;cursor:pointer">今天</button>' +
          '<button onclick="_ttReportQuick(\'yesterday\')" style="padding:4px 10px;font-size:11px;border-radius:6px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);color:var(--text-muted);cursor:pointer">昨天</button>' +
          '<button onclick="_ttReportQuick(\'week\')" style="padding:4px 10px;font-size:11px;border-radius:6px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);color:var(--text-muted);cursor:pointer">近7天</button>' +
          '<button onclick="_ttReportQuick(\'month\')" style="padding:4px 10px;font-size:11px;border-radius:6px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);color:var(--text-muted);cursor:pointer">本月</button>' +
        '</div>' +
        '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:14px">' +
          '<div>' +
            '<div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">开始时间</div>' +
            '<input type="datetime-local" id="tt-rpt-start" value="' + sevenDaysAgo + '" style="width:100%;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:7px;padding:7px 10px;color:var(--text-main);font-size:12px;box-sizing:border-box;outline:none">' +
          '</div>' +
          '<div>' +
            '<div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">结束时间</div>' +
            '<input type="datetime-local" id="tt-rpt-end" value="' + todayStr + '" style="width:100%;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:7px;padding:7px 10px;color:var(--text-main);font-size:12px;box-sizing:border-box;outline:none">' +
          '</div>' +
        '</div>' +
        '<div style="display:flex;gap:8px;margin-bottom:14px">' +
          '<button id="tt-rpt-gen-btn" onclick="_ttGenerateReport()" style="flex:1;padding:10px;font-size:13px;font-weight:600;border-radius:8px;background:rgba(99,102,241,.25);border:1px solid rgba(99,102,241,.4);color:#a5b4fc;cursor:pointer">\u{1F4CA} \u751F\u6210\u65E5\u62A5</button>' +
          '<button onclick="_ttExportDailyCSV()" style="padding:10px 16px;font-size:12px;font-weight:600;border-radius:8px;background:rgba(34,197,94,.15);border:1px solid rgba(34,197,94,.3);color:#22c55e;cursor:pointer;white-space:nowrap" title="\u5BFC\u51FA\u6240\u6709\u6570\u636E\u7EFC\u5408CSV">\u{1F4E5} \u7EFC\u5408\u5BFC\u51FA</button>' +
        '</div>' +
      '</div>' +
      // 报告内容区
      '<div id="tt-rpt-body" style="flex:1;overflow-y:auto;padding:0 20px 20px">' +
        '<div style="color:var(--text-muted);font-size:12px;text-align:center;padding:20px">选择时间段后点击生成日报</div>' +
      '</div>' +
    '</div>';

  ov.addEventListener('click', function(e) { if (e.target === ov) ov.remove(); });
  document.body.appendChild(ov);
}

function _ttReportQuick(type) {
  const now = new Date();
  let start, end = now.toISOString().slice(0, 16);
  if (type === 'today') {
    start = now.toISOString().slice(0, 10) + 'T00:00';
  } else if (type === 'yesterday') {
    const y = new Date(now - 86400000);
    start = y.toISOString().slice(0, 10) + 'T00:00';
    end = y.toISOString().slice(0, 10) + 'T23:59';
  } else if (type === 'week') {
    start = new Date(now - 7 * 86400000).toISOString().slice(0, 10) + 'T00:00';
  } else if (type === 'month') {
    start = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-01T00:00';
  }
  const si = document.getElementById('tt-rpt-start');
  const ei = document.getElementById('tt-rpt-end');
  if (si) si.value = start;
  if (ei) ei.value = end;
}

async function _ttGenerateReport() {
  const btn = document.getElementById('tt-rpt-gen-btn');
  const body = document.getElementById('tt-rpt-body');
  const start = (document.getElementById('tt-rpt-start') || {}).value || '';
  const end = (document.getElementById('tt-rpt-end') || {}).value || '';
  if (!start || !end) { _ttShowToast('请选择时间段', 'error'); return; }

  if (btn) { btn.disabled = true; btn.textContent = '⏳ AI分析中...'; }
  if (body) body.innerHTML = '<div style="text-align:center;padding:30px;color:var(--text-muted);font-size:12px">正在聚合数据并生成AI分析...<br><small style="opacity:.6">这可能需要10-20秒</small></div>';

  try {
    const r = await fetch('/analytics/report', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ start_dt: start.replace('T', ' '), end_dt: end.replace('T', ' '), use_ai: true })
    });
    const data = await r.json();
    if (!data.ok) { if (body) body.innerHTML = '<div style="color:#f87171;padding:20px;font-size:12px">生成失败: ' + _escHtml(data.error || '未知错误') + '</div>'; return; }
    _ttRenderReport(body, data);
  } catch(e) {
    if (body) body.innerHTML = '<div style="color:#f87171;padding:20px;font-size:12px">请求失败: ' + _escHtml(e.message || e) + '</div>';
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '📊 生成日报'; }
  }
}

function _ttRenderReport(el, d) {
  if (!el) return;
  const t = d.totals || {};
  const l = d.leads || {};
  const period = d.period || {};
  const ai = d.ai_summary || '';

  const statCard = function(icon, label, val, color) {
    return '<div style="background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.07);border-radius:8px;padding:10px;text-align:center">' +
      '<div style="font-size:18px;font-weight:700;color:' + color + '">' + (val || 0) + '</div>' +
      '<div style="font-size:10px;color:var(--text-muted);margin-top:2px">' + icon + ' ' + label + '</div>' +
      '</div>';
  };

  let html =
    // 周期标题
    '<div style="font-size:11px;color:var(--text-muted);margin-bottom:12px;padding:8px 10px;background:rgba(255,255,255,.03);border-radius:6px">' +
      '&#128337; ' + _escHtml(period.start || '') + ' → ' + _escHtml(period.end || '') + ' &nbsp;|&nbsp; ' + (period.days || 1) + ' 天 &nbsp;|&nbsp; ' + (d.devices || []).length + ' 台设备' +
    '</div>' +
    // 行为数据
    '<div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:8px">行为数据</div>' +
    '<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px">' +
      statCard('👁', '观看视频', t.videos_watched, '#8b5cf6') +
      statCard('➕', '关注账号', t.follows, '#3b82f6') +
      statCard('💬', '发送私信', t.dms_sent, '#22c55e') +
    '</div>' +
    // 线索数据
    '<div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:8px">线索漏斗</div>' +
    '<div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px;margin-bottom:16px">' +
      statCard('🎯', '新线索', l.new_leads, '#a5b4fc') +
      statCard('📤', '已发话术', l.pitched, '#f59e0b') +
      statCard('💬', '已回复', l.responded, '#22c55e') +
      statCard('🏆', '已转化', l.converted, '#c4b5fd') +
    '</div>';

  // M2: 生命周期 + 跨设备去重
  (function() {
    var lc = d.lifecycle || {};
    var s = lc.stages || {};
    var lcT = lc.total || 0;
    if (lcT > 0) {
      var defs = [
        {key:'new',label:'新建',c:'#64748b'},{key:'contacted',label:'触达',c:'#3b82f6'},
        {key:'engaged',label:'互动',c:'#8b5cf6'},{key:'qualified',label:'合格',c:'#22c55e'},
        {key:'converted',label:'转化',c:'#f59e0b'},{key:'lost',label:'流失',c:'#ef4444'}
      ];
      html += '<div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:8px">生命周期漏斗（共 ' + lcT + '）</div>'
        + '<div style="display:grid;grid-template-columns:repeat(6,1fr);gap:6px;margin-bottom:16px">';
      defs.forEach(function(dd) {
        var cnt = s[dd.key] || 0;
        html += statCard(dd.label.charAt(0), dd.label, cnt, dd.c);
      });
      html += '</div>';
    }
    var dedup = d.cross_device_dedup || {};
    if (dedup.total_blocks > 0) {
      html += '<div style="display:flex;gap:10px;margin-bottom:16px;padding:8px 12px;background:rgba(239,68,68,.06);border:1px solid rgba(239,68,68,.15);border-radius:8px">'
        + '<span style="font-size:11px;color:#f87171">🛡 跨设备拦截: <b>' + dedup.total_blocks + '</b> 次</span>'
        + '<span style="font-size:11px;color:var(--text-muted)">|</span>'
        + '<span style="font-size:11px;color:#f87171">保护 Lead: <b>' + (dedup.unique_leads_saved||0) + '</b> 条</span></div>';
    }
  })();

  // 表现最佳设备
  if (d.top_devices && d.top_devices.length) {
    html += '<div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:8px">设备排名（按私信量）</div>' +
      '<div style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.07);border-radius:8px;overflow:hidden;margin-bottom:16px">';
    d.top_devices.slice(0, 5).forEach(function(dev, i) {
      html += '<div style="display:flex;align-items:center;gap:10px;padding:7px 12px;' + (i > 0 ? 'border-top:1px solid rgba(255,255,255,.05)' : '') + '">' +
        '<span style="font-size:10px;color:var(--text-muted);width:16px;flex-shrink:0">' + (i + 1) + '</span>' +
        '<span style="font-size:11px;color:var(--text-main);flex:1">' + _escHtml(dev.alias || dev.device_id.slice(0, 10)) + '</span>' +
        '<span style="font-size:10px;color:#60a5fa">私信 ' + (dev.dms_sent || 0) + '</span>' +
        '<span style="font-size:10px;color:#34d399">关注 ' + (dev.follows || 0) + '</span>' +
        '<span style="font-size:10px;color:#a78bfa">算法 ' + (dev.algo_score_avg || 0) + '</span>' +
      '</div>';
    });
    html += '</div>';
  }

  // AI 摘要
  if (ai) {
    html += '<div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:8px">&#10024; AI 运营分析</div>' +
      '<div style="background:rgba(139,92,246,.08);border:1px solid rgba(139,92,246,.2);border-radius:8px;padding:14px;font-size:12px;line-height:1.7;color:var(--text-main);white-space:pre-wrap">' +
      _escHtml(ai) + '</div>';
  }

  el.innerHTML = html;
}

async function _ttExportDailyCSV() {
  _toast('\u{23F3} \u6B63\u5728\u805A\u5408\u5404\u9762\u677F\u6570\u636E...','info');
  try {
    var sections = [];
    // 1. 转化漏斗
    try {
      var f = await api('GET', '/tiktok/funnel');
      sections.push('\u8F6C\u5316\u6F0F\u6597');
      sections.push('\u9636\u6BB5,\u6570\u91CF,\u8F6C\u5316\u7387');
      (f.stages||[]).forEach(function(s) { sections.push(s.name + ',' + s.value + ',' + (s.conversion_rate||100) + '%'); });
    } catch(e) {}

    // 2. 对话质量
    try {
      var c = await api('GET', '/tiktok/chat/active');
      var kpi = c.kpi || {};
      sections.push('');
      sections.push('\u5BF9\u8BDD\u8D28\u91CF KPI');
      sections.push('\u6307\u6807,\u503C');
      sections.push('\u56DE\u590D\u7387,' + (kpi.reply_rate||0) + '%');
      sections.push('\u5E73\u5747\u8D28\u91CF,' + (kpi.avg_quality||0));
      sections.push('\u5E73\u5747\u8F6E\u6B21,' + (kpi.avg_rounds||0));
      sections.push('\u603B\u6D88\u606F,' + (kpi.total_messages||0));
    } catch(e) {}

    // 3. 设备健康
    try {
      var h = await api('GET', '/devices/health-scores');
      var scores = h.scores || {};
      sections.push('');
      sections.push('\u8BBE\u5907\u5065\u5EB7\u5EA6');
      sections.push('\u8BBE\u5907,\u603B\u5206,\u7A33\u5B9A\u6027,\u54CD\u5E94,\u4EFB\u52A1\u6210\u529F,U2');
      Object.keys(scores).forEach(function(did) {
        var s = scores[did];
        sections.push(did.substring(0,12) + ',' + s.total + ',' + s.stability + ',' + s.responsiveness + ',' + s.task_success + ',' + s.u2_health);
      });
    } catch(e) {}

    // 4. 策略分析
    try {
      var sa = await api('GET', '/tiktok/chat/strategy-analysis');
      if (sa.top_openers && sa.top_openers.length) {
        sections.push('');
        sections.push('\u6700\u4F73\u5F00\u573A\u767D');
        sections.push('\u5F00\u573A\u767D,\u53D1\u9001\u6570,\u56DE\u590D\u6570,\u56DE\u590D\u7387');
        sa.top_openers.slice(0,10).forEach(function(op) {
          sections.push('"' + op.text.replace(/"/g,'""') + '",' + op.sent + ',' + op.replied + ',' + op.rate + '%');
        });
      }
    } catch(e) {}

    // 5. A/B 打招呼实验
    try {
      var ab = await api('GET', '/experiments/tiktok_greet_opening/analyze');
      if (ab.variants) {
        sections.push('');
        sections.push('A/B tiktok_greet_opening');
        sections.push('\u53D8\u4F53,sent,reply_received');
        Object.keys(ab.variants).forEach(function(vk) {
          var row = ab.variants[vk] || {};
          sections.push(vk + ',' + (row.sent||0) + ',' + (row.reply_received||0));
        });
        if (ab.best_variant) sections.push('best,' + ab.best_variant);
      }
    } catch(e) {}

    // 6. 操作日志统计
    try {
      var ol = await fetch('/ops-logs/stats',{credentials:'include'}).then(function(r){return r.json();});
      sections.push('');
      sections.push('\u64CD\u4F5C\u65E5\u5FD7\u7EDF\u8BA1');
      sections.push('\u603B\u64CD\u4F5C,' + (ol.total_operations||0));
      sections.push('\u603B\u5BFC\u5165\u8054\u7CFB\u4EBA,' + (ol.total_contacts_imported||0));
    } catch(e) {}

    var filename = '\u7EFC\u5408\u65E5\u62A5_' + new Date().toISOString().slice(0,10) + '.csv';
    _downloadCSV(filename, sections.join('\n'));
    _toast('\u2705 \u7EFC\u5408\u65E5\u62A5\u5DF2\u5BFC\u51FA','success');
  } catch(e) {
    _toast('\u5BFC\u51FA\u5931\u8D25: ' + e.message, 'error');
  }
}

// ════════════════════════════════════════════════════════
// 实时事件监听：inbox_checked → 立即更新回复状态，无需等90s
// ════════════════════════════════════════════════════════
window.addEventListener('oc:event', function(ev) {
  var msg = ev.detail || {};
  var t = msg.type || '';
  var data = msg.data || {};
  // W03 来源标记：事件来自 Worker-03 而非本地
  var _isW03 = !!(data._from_w03);
  var _w03Tag = _isW03 ? '[W03] ' : '';

  // 收件箱检查完成：如果有新消息，立即触发回复通知逻辑
  if (t === 'tiktok.inbox_checked' && (data.new_messages || 0) > 0) {
    var did = data.device_id || '';
    var newMsgs = data.new_messages || 0;
    var devAlias = (window._ttDevicesCache && window._ttDevicesCache[did] || {}).alias || did.substring(0, 8);

    // 更新设备缓存中的 replied_count（乐观更新，无需等 silentRefresh）
    if (did && window._ttDevicesCache && window._ttDevicesCache[did]) {
      window._ttDevicesCache[did].replied_count = (window._ttDevicesCache[did].replied_count || 0) + 1;
    }

    // 显示即时 Toast（W03 事件加来源标记）
    _ttShowToast('📨 ' + _w03Tag + devAlias + ' 收到 ' + newMsgs + ' 条新消息！', 'warning', 7000);

    // 如果线索面板已打开且是该设备，自动切换到"有回复"标签并刷新
    var leadsPanel = document.getElementById('tt-leads-panel');
    if (leadsPanel && leadsPanel.classList.contains('open')) {
      var openDevId = leadsPanel.dataset.deviceId || '';
      if (!openDevId || openDevId === did) {
        var repliedTab = document.querySelector('.tt-lp-tab[data-f="replied"]');
        if (repliedTab) _ttLpSetFilter(repliedTab);
        setTimeout(function() { _ttRefreshLeads(openDevId || did, null); }, 800);
      }
    }

    // 闪烁设备卡片
    var card = document.querySelector('.tt-dev-card[data-did="' + did + '"]');
    if (card) {
      card.classList.add('new-leads');
      setTimeout(function() { card.classList.remove('new-leads'); }, 3000);
    }
  }

  // 自动回复完成通知 + 刷新leads面板
  if (t === 'tiktok.inbox_checked' && (data.auto_replied || 0) > 0) {
    var did2 = data.device_id || '';
    var alias2 = (window._ttDevicesCache && window._ttDevicesCache[did2] || {}).alias || did2.substring(0, 8);
    _ttShowToast('🤖 ' + _w03Tag + alias2 + ' 已自动回复 ' + data.auto_replied + ' 条', 'success', 4000);
    // 自动回复后延迟刷新leads面板（让DB写入完成）
    var lp2 = document.getElementById('tt-leads-panel');
    if (lp2 && lp2.classList.contains('open')) {
      setTimeout(function() {
        var opDid = lp2.dataset.deviceId || did2;
        _ttRefreshLeads(opDid, null);
      }, 1500);
    }
  }

  // 实时对话追加：auto_reply_sent → 追加到已展开的历史时间轴
  if (t === 'tiktok.auto_reply_sent') {
    var username = data.username || '';
    var replyMsg = data.message || '';
    var replyIntent = data.intent || '';
    if (!username || !replyMsg) return;
    // 在当前leads缓存中找到对应lead_id（优先查面板完整数据）
    var cachedLeads = window._ttLeadsRaw || window._ttLeadsCache || [];
    var matchedLid = null;
    var targetUser = username.replace(/^@/, '').toLowerCase();
    for (var i = 0; i < cachedLeads.length; i++) {
      var cl = cachedLeads[i];
      // 同时匹配 username（@handle）和 name（显示名）
      var uname = (cl.username || '').replace(/^@/, '').toLowerCase();
      var dname = (cl.name || '').toLowerCase();
      if (uname === targetUser || dname === targetUser || uname.includes(targetUser) || targetUser.includes(uname)) {
        matchedLid = cl.id || cl.lead_id;
        break;
      }
    }
    if (!matchedLid) return;
    var histContainer = document.getElementById('tt-hist-' + matchedLid);
    var histBtn = document.getElementById('tt-hist-btn-' + matchedLid);
    // 如果历史未展开 → 自动展开（AI正在与这个人对话，值得关注）
    if (histBtn && histBtn.dataset.open !== '1') {
      _ttToggleHistory(histBtn);
      return;  // toggleHistory 会加载历史，包含新回复
    }
    if (!histContainer) return;
    // 构造新气泡并追加（实时，不需重新加载）
    var now = new Date();
    var timeStr = now.getHours() + ':' + String(now.getMinutes()).padStart(2,'0');
    var newBubble = '<div style="display:flex;flex-direction:column;align-items:flex-end;margin-top:6px">' +
      '<div style="align-self:flex-end;background:rgba(99,102,241,.18);border:1px solid rgba(99,102,241,.3);border-radius:10px 4px 10px 10px;max-width:88%;padding:7px 10px">' +
        '<div style="font-size:11px;color:var(--text-main);line-height:1.5">' + _escHtml(replyMsg) + '</div>' +
      '</div>' +
      '<div style="font-size:9px;color:var(--text-muted);margin-top:2px;text-align:right">' +
        '🤖 auto_reply &nbsp;·&nbsp; ' + timeStr +
        (replyIntent ? ' &nbsp;·&nbsp; <span style="color:#f59e0b">' + _escHtml(replyIntent) + '</span>' : '') +
      '</div>' +
    '</div>';
    var inner = histContainer.querySelector('div');
    if (inner) inner.insertAdjacentHTML('beforeend', newBubble);
  }

  // lead 自动升格通知：NEEDS_REPLY → qualified
  if (t === 'tiktok.lead_qualified') {
    var qLid = data.lead_id || '';
    var qPrev = data.prev_status || '';
    _ttShowToast('⭐ 线索 #' + qLid + ' 已自动升格（' + qPrev + ' → qualified）', 'success', 5000);
    // 乐观更新本地缓存的 status
    if (window._ttLeadsRaw && qLid) {
      var qCached = window._ttLeadsRaw.find(function(x) { return (x.id || x.lead_id) == qLid; });
      if (qCached) qCached.status = 'qualified';
    }
    // 刷新 leads 面板（如果已打开）
    var lqp = document.getElementById('tt-leads-panel');
    if (lqp && lqp.classList.contains('open')) {
      setTimeout(function() {
        var opDid = lqp.dataset.deviceId || '';
        _ttRefreshLeads(opDid, null);
      }, 600);
    }
  }

  // 新线索自动发现：实时追加面板行 + 设备卡片徽章更新
  if (t === 'tiktok.lead_discovered') {
    var dLid = data.lead_id || '';
    var dUser = data.username || '';
    var dDid = data.device_id || '';
    var dIntent = data.intent || '';
    var dAlias = (window._ttDevicesCache && window._ttDevicesCache[dDid] || {}).alias || dDid.substring(0,6);

    // Toast 通知（W03 事件加来源标记）
    _ttShowToast('🆕 ' + _w03Tag + '新线索: @' + dUser + (dAlias ? ' (' + dAlias + ')' : '') +
                 (dIntent === 'NEEDS_REPLY' ? ' — 感兴趣！' : ''), 'info', 5000);

    // 更新设备卡片 leads_count（乐观 +1）
    if (dDid && window._ttDevicesCache && window._ttDevicesCache[dDid]) {
      window._ttDevicesCache[dDid].leads_count = (window._ttDevicesCache[dDid].leads_count || 0) + 1;
      var devCard = document.querySelector('.tt-dev-card[data-did="' + dDid + '"]');
      if (devCard) {
        var leadsEl = devCard.querySelector('.tt-dev-leads-n');
        if (leadsEl) leadsEl.textContent = window._ttDevicesCache[dDid].leads_count;
      }
    }

    // 如果线索面板已打开，延迟刷新（让 DB 写入完成）
    var ldp = document.getElementById('tt-leads-panel');
    if (ldp && ldp.classList.contains('open')) {
      setTimeout(function() {
        var opDid2 = ldp.dataset.deviceId || dDid;
        _ttRefreshLeads(opDid2, null);
      }, 1000);
    }
  }

  // W03 事件到达时，更新面板右上角的 W03 状态指示灯
  if (_isW03) {
    _ttUpdateW03Indicator();
  }

  // 线索去重合并完成通知
  if (t === 'leads.merged') {
    var mg = data.merged_groups || 0;
    var mr = data.total_removed || 0;
    if (mr > 0) {
      _ttShowToast('🔗 线索去重完成：合并 ' + mg + ' 组，删除 ' + mr + ' 条重复', 'success', 5000);
      // 刷新线索面板（如已打开）
      var ldpm = document.getElementById('tt-leads-panel');
      if (ldpm && ldpm.classList.contains('open')) {
        setTimeout(function() { _ttRefreshLeads(ldpm.dataset.deviceId || '', null); }, 1000);
      }
    }
  }

  // AI意图重评完成通知
  if (t === 'leads.rescored') {
    var rsc = data.rescored || 0;
    var rnr = data.needs_reply || 0;
    var rno = data.no_reply || 0;
    if (rsc > 0) {
      _ttShowToast('🤖 AI重评完成：' + rsc + ' 条，感兴趣 ' + rnr + ' 条，拒绝 ' + rno + ' 条', 'success', 6000);
      var ldrp = document.getElementById('tt-leads-panel');
      if (ldrp && ldrp.classList.contains('open')) {
        setTimeout(function() { _ttRefreshLeads(ldrp.dataset.deviceId || '', null); }, 1200);
      }
    }
  }
});

// W03 连接状态指示器（显示在 TikTok 面板标题旁）
var _w03LastEventTs = 0;
var _w03Online = false;

function _ttUpdateW03Indicator() {
  _w03LastEventTs = Date.now();
  _w03Online = true;
  var el = document.getElementById('tt-w03-indicator');
  if (!el) return;
  el.title = 'W03 已连接，最近事件: ' + new Date().toLocaleTimeString();
  el.style.background = '#22c55e';
  // 2秒后回到常规绿色（保持在线态）
  clearTimeout(el._flashTimer);
  el._flashTimer = setTimeout(function() { if (el) el.style.background = '#4ade80'; }, 2000);
  // 更新 pill tooltip
  var pill = document.getElementById('tt-w03-pill');
  if (pill) pill.title = 'W03 在线 · 最近活动: ' + new Date().toLocaleTimeString();
}

// 周期检查 W03 状态（每 30s 主动 ping）
async function _ttPollW03Status() {
  try {
    const r = await fetch('/events/hub-snapshot?since_id=0&limit=1', {credentials:'include'});
    if (!r.ok) throw new Error('http ' + r.status);
    // 根据最近事件时间判断 W03 是否活跃
    var now = Date.now();
    var lastActive = _w03LastEventTs;
    var el = document.getElementById('tt-w03-indicator');
    var pill = document.getElementById('tt-w03-pill');
    if (!el) return;

    if (lastActive > 0 && now - lastActive < 5 * 60 * 1000) {
      // 5分钟内有事件 → 绿色活跃
      el.style.background = '#4ade80';
      el.title = 'W03 活跃 · ' + new Date(lastActive).toLocaleTimeString();
      if (pill) pill.title = 'W03 活跃 · ' + new Date(lastActive).toLocaleTimeString();
    } else if (lastActive > 0 && now - lastActive < 30 * 60 * 1000) {
      // 30分钟内有事件 → 黄色（稍久）
      el.style.background = '#f59e0b';
      el.title = 'W03 闲置 · 最近活动: ' + new Date(lastActive).toLocaleTimeString();
      if (pill) pill.title = 'W03 闲置 · ' + new Date(lastActive).toLocaleTimeString();
    } else {
      // 无事件 → 检查桥接是否工作（灰色待连接）
      el.style.background = '#475569';
      el.title = 'W03 等待中' + (lastActive ? ' · 最近: ' + new Date(lastActive).toLocaleTimeString() : '');
      if (pill) pill.title = 'W03 等待中（桥接每10s轮询）';
    }
  } catch(e) { /* 静默失败 */ }
}

// 页面加载后 2s 首次检查，之后每 30s 检查一次
setTimeout(_ttPollW03Status, 2000);
setInterval(_ttPollW03Status, 30000);

/* ── 手动同步 W03 线索 ── */
async function _ttSyncW03Leads(btn) {
  var origText = btn ? btn.textContent : '';
  if (btn) { btn.textContent = '同步中...'; btn.disabled = true; }
  try {
    const r = await fetch('/tiktok/sync-w03-leads', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'include',
      body: JSON.stringify({limit: 200})
    });
    const d = r.ok ? await r.json() : null;
    if (d && d.ok) {
      _ttShowToast('W03 同步完成：新建 ' + d.synced + ' 条，升格 ' + d.updated + ' 条，共 ' + d.total_from_w03 + ' 条', 'success', 5000);
    } else if (d && d.error) {
      _ttShowToast('W03 同步失败: ' + d.error, 'error', 6000);
    } else {
      _ttShowToast('W03 同步异常', 'error');
    }
  } catch(e) {
    _ttShowToast('同步请求失败: ' + (e.message || e), 'error');
  } finally {
    if (btn) { btn.textContent = origText; btn.disabled = false; }
  }
}

/* ── AI意图重评（OPTIONAL → NEEDS_REPLY / NO_REPLY） ── */
async function _ttAiRescore(btn, deviceId) {
  var origText = btn ? btn.textContent : '';
  if (btn) { btn.textContent = 'AI评分中...'; btn.disabled = true; }
  try {
    const r = await fetch('/tiktok/leads/ai-rescore', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      credentials: 'include',
      body: JSON.stringify({limit: 30})
    });
    const d = r.ok ? await r.json() : null;
    if (d && d.ok) {
      var msg = 'AI重评完成：处理 ' + d.rescored + ' 条';
      if (d.needs_reply > 0) msg += '，感兴趣 ' + d.needs_reply + ' 条';
      if (d.no_reply > 0) msg += '，拒绝 ' + d.no_reply + ' 条';
      _ttShowToast(msg, d.needs_reply > 0 ? 'success' : 'info', 6000);
      if (d.needs_reply > 0 || d.no_reply > 0) {
        setTimeout(function() { _ttRefreshLeads(deviceId || '', null); }, 800);
      }
    } else {
      _ttShowToast('AI重评失败: ' + (d && d.error ? d.error : '未知错误'), 'error', 6000);
    }
  } catch(e) {
    _ttShowToast('请求失败: ' + (e.message || e), 'error');
  } finally {
    if (btn) { btn.textContent = origText; btn.disabled = false; }
  }
}

/* ttCheckInboxForDevice 由 tiktok-ops.js 提供（与批量收件箱同任务通道） */

/* ── P10-A: 自动化任务快速开关 ── */
const _TIKTOK_JOB_KEYS = ['tiktok_inbox_10m','tiktok_follow_daily','tiktok_warmup_post_follow_am',
  'tiktok_warmup_post_follow_pm','tiktok_chat_2h','tiktok_followup_noon',
  'tiktok_warmup_prime_time','tiktok_ai_rescore_4h'];

async function _loadScheduledJobsQuick() {
  const el = document.getElementById('sched-quick-list');
  if (!el) return;
  try {
    const jobs = await api('GET', '/scheduled-jobs');
    // 过滤出 TikTok 相关 + purge + leads_merge 等关键任务
    const keyJobs = jobs.filter(j => {
      const act = j.action || '';
      return act.startsWith('tiktok_') || act === 'purge_old_tasks' || act === 'leads_merge_duplicates' || act === 'analytics_daily_report';
    });
    if (!keyJobs.length) { el.innerHTML = '<div style="color:var(--text-dim);font-size:12px">暂无定时任务</div>'; return; }

    el.innerHTML = keyJobs.map(j => {
      const enabled = j.enabled;
      const dotColor = enabled ? 'var(--green)' : '#6b7280';
      const lastRun = j.last_run ? j.last_run.substring(5) : '从未';
      const nameTrunc = (j.name || j.id).substring(0, 28);
      return `<div class="sched-job-card" style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:8px 10px;display:flex;align-items:center;gap:8px">
        <div style="width:8px;height:8px;border-radius:50%;background:${dotColor};flex-shrink:0"></div>
        <div style="flex:1;min-width:0">
          <div style="font-size:11px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${esc(j.name||j.id)}">${esc(nameTrunc)}</div>
          <div style="font-size:10px;color:var(--text-dim)">${esc(j.cron||'-')} &nbsp;上次: ${esc(lastRun)}</div>
        </div>
        <div style="display:flex;gap:4px;flex-shrink:0">
          <button class="sched-toggle-btn${enabled?' active':''}" onclick="_schedToggle('${j.id}',${!enabled})" style="font-size:10px;padding:2px 6px;border-radius:4px;border:1px solid ${enabled?'var(--green)':'var(--border)'};background:${enabled?'rgba(34,197,94,.12)':'transparent'};color:${enabled?'var(--green)':'var(--text-dim)'};cursor:pointer">${enabled?'已开':'已关'}</button>
          <button onclick="_schedRunNow('${j.id}')" style="font-size:10px;padding:2px 6px;border-radius:4px;border:1px solid var(--border);background:transparent;color:var(--accent);cursor:pointer">▶</button>
        </div>
      </div>`;
    }).join('');
  } catch(e) { if(el) el.innerHTML = '<div style="color:var(--text-dim);font-size:12px">加载失败</div>'; }
}

async function _schedToggle(jobId, enable) {
  try {
    await api('PUT', `/scheduled-jobs/${jobId}`, { enabled: enable });
    _loadScheduledJobsQuick();
    showToast(enable ? '已启用' : '已停用', 'success');
  } catch(e) { showToast('操作失败', 'error'); }
}

async function _schedRunNow(jobId) {
  try {
    await api('POST', `/scheduled-jobs/${jobId}/run-now`);
    showToast('已触发立即运行', 'success');
    setTimeout(_loadScheduledJobsQuick, 2000);
  } catch(e) { showToast('运行失败: ' + (e.message||e), 'error'); }
}

async function _schedQuickAll(enable) {
  try {
    const jobs = await api('GET', '/scheduled-jobs');
    const tiktokJobs = jobs.filter(j => (j.action||'').startsWith('tiktok_'));
    await Promise.all(tiktokJobs.map(j => api('PUT', `/scheduled-jobs/${j.id}`, {enabled: enable})));
    _loadScheduledJobsQuick();
    showToast(enable ? `已启用 ${tiktokJobs.length} 个TikTok任务` : '已停用所有TikTok任务', 'success');
  } catch(e) { showToast('批量操作失败', 'error'); }
}

/* ── P10-B: 今日运营日报 ── */
async function _loadDailyReport() {
  const el = document.getElementById('daily-report-body');
  if (!el) return;
  el.innerHTML = '<div style="color:var(--text-dim);font-size:12px;text-align:center;padding:16px">加载中...</div>';
  try {
    // 尝试集群日报端点，失败时降级到单节点
    let d = null;
    try {
      d = await api('GET', '/analytics/cluster-daily-report');
    } catch(e) {
      // 降级：直接调用 POST /analytics/report
      const today = new Date();
      const fmt = t => t.toISOString().substring(0,10);
      d = await api('POST', '/analytics/report', {
        start_dt: fmt(today) + ' 00:00',
        end_dt: fmt(today) + ' 23:59',
        use_ai: false
      });
      d.totals = d.totals || {};
      d.leads = d.leads || {};
    }

    const t = d.totals || {};
    const l = d.leads || {};
    const topDevs = (d.top_devices || []).slice(0, 5);

    const metricHtml = (label, val, color) =>
      `<div style="text-align:center;background:var(--bg-main);border-radius:8px;padding:10px 6px">
        <div style="font-size:22px;font-weight:700;color:${color}">${val||0}</div>
        <div style="font-size:10px;color:var(--text-dim);margin-top:2px">${label}</div>
      </div>`;

    const funnelStep = (label, val, total) => {
      const pct = total > 0 ? Math.round(val/total*100) : 0;
      return `<div style="text-align:center;flex:1">
        <div style="font-size:14px;font-weight:600">${val}</div>
        <div style="font-size:9px;color:var(--text-dim)">${label}</div>
        ${total>0?`<div style="font-size:9px;color:var(--accent)">${pct}%</div>`:''}
      </div>`;
    };

    const topDevsHtml = topDevs.length ? topDevs.map((dd,i) => {
      const alias = ALIAS[dd.device_id] || dd.device_id.substring(0,8);
      return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid var(--border)">
        <span style="font-size:10px;color:var(--text-dim);width:14px">#${i+1}</span>
        <span style="font-size:11px;flex:1">${esc(alias)}</span>
        <span style="font-size:10px;color:#3b82f6">关${dd.follows||0}</span>
        <span style="font-size:10px;color:#22c55e">私${dd.dms_sent||0}</span>
      </div>`;
    }).join('') : '<div style="font-size:11px;color:var(--text-dim)">暂无设备数据</div>';

    const periodLabel = d.sources ? `数据来源: ${d.sources.join('+')}` : '今日';

    el.innerHTML = `
      <div style="font-size:10px;color:var(--text-dim);margin-bottom:10px">${esc(periodLabel)} · ${esc(d.start_dt||'')} ~ ${esc(d.end_dt||'')}</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(80px,1fr));gap:8px;margin-bottom:14px">
        ${metricHtml('关注', t.follows, '#3b82f6')}
        ${metricHtml('私信', t.dms_sent, '#22c55e')}
        ${metricHtml('视频', t.videos_watched, '#8b5cf6')}
        ${metricHtml('AI回复', t.dms_responded, '#06b6d4')}
        ${metricHtml('新线索', l.new_leads, '#f59e0b')}
        ${metricHtml('转化', l.converted, '#22c55e')}
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
        <div>
          <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:8px">线索漏斗</div>
          <div style="display:flex;align-items:flex-end;gap:2px;background:var(--bg-main);border-radius:8px;padding:10px">
            ${funnelStep('新线索', l.new_leads||0, 0)}
            <span style="color:var(--text-dim);font-size:12px;margin-bottom:8px">→</span>
            ${funnelStep('已接触', l.pitched||0, l.new_leads||1)}
            <span style="color:var(--text-dim);font-size:12px;margin-bottom:8px">→</span>
            ${funnelStep('已回复', l.responded||0, l.new_leads||1)}
            <span style="color:var(--text-dim);font-size:12px;margin-bottom:8px">→</span>
            ${funnelStep('合格', l.qualified||0, l.new_leads||1)}
            <span style="color:var(--text-dim);font-size:12px;margin-bottom:8px">→</span>
            ${funnelStep('转化', l.converted||0, l.new_leads||1)}
          </div>
        </div>
        <div>
          <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:8px">设备效能 Top5</div>
          <div>${topDevsHtml}</div>
        </div>
      </div>
      ${d.ai_summary ? `<div style="margin-top:10px;padding:10px;background:rgba(139,92,246,.08);border-radius:8px;font-size:11px;color:var(--text-dim)">${esc(d.ai_summary.substring(0,200))}</div>` : ''}
    `;
  } catch(e) {
    if(el) el.innerHTML = `<div style="color:var(--red);font-size:12px;padding:12px">日报加载失败: ${esc(e.message||String(e))}</div>`;
  }
}

function _printDailyReport() {
  const el = document.getElementById('daily-report-body');
  if (!el) return;
  const w = window.open('', '_blank', 'width=800,height=600');
  w.document.write(`<!DOCTYPE html><html><head><title>运营日报</title>
  <style>body{font-family:sans-serif;background:#fff;color:#1e293b;padding:20px;max-width:700px;margin:0 auto;}
  @media print{body{padding:0;}button{display:none!important;}}</style></head>
  <body><h2 style="text-align:center">📊 OpenClaw 运营日报</h2>
  <div id="content">${el.innerHTML}</div>
  <div style="text-align:right;margin-top:16px"><button onclick="window.print()">🖨️ 打印</button></div>
  </body></html>`);
  w.document.close();
}

/* ══════════════════════════════════════════════════════════════
   通讯录管理面板 — 批量注入、设备通讯录查看、TikTok好友发现
   ══════════════════════════════════════════════════════════════ */

let _ctInitDone = false;
let _ctSelectedDevice = '';
let _ctContacts = [];       // 设备上的通讯录
let _ctPendingList = [];    // 待注入列表
let _ctBusy = false;

function _ttContactsInit(el) {
  if (_ctInitDone && el.children.length > 0) {
    _ctRefreshDeviceList();
    return;
  }
  _ctInitDone = true;

  const css = document.createElement('style');
  css.textContent = `
.ct-wrap{display:grid;grid-template-columns:280px 1fr;gap:16px;padding:12px 0;}
.ct-side{background:var(--bg-card);border-radius:12px;border:1px solid var(--border);padding:16px;max-height:75vh;overflow-y:auto;}
.ct-main{display:flex;flex-direction:column;gap:16px;}
.ct-card{background:var(--bg-card);border-radius:12px;border:1px solid var(--border);padding:16px;}
.ct-card h3{margin:0 0 12px;font-size:15px;font-weight:600;color:var(--text);display:flex;align-items:center;gap:8px;}
.ct-dev{padding:10px 12px;border-radius:8px;cursor:pointer;display:flex;align-items:center;gap:10px;transition:.15s;border:1px solid transparent;margin-bottom:4px;}
.ct-dev:hover{background:rgba(99,102,241,.08);}
.ct-dev.active{background:rgba(99,102,241,.12);border-color:var(--accent);}
.ct-dev-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0;}
.ct-dev-name{font-size:13px;font-weight:500;color:var(--text);}
.ct-dev-serial{font-size:11px;color:var(--text-muted);margin-top:2px;}
.ct-stat-row{display:flex;gap:12px;flex-wrap:wrap;}
.ct-stat{flex:1;min-width:120px;background:linear-gradient(135deg,rgba(99,102,241,.06),rgba(99,102,241,.02));border-radius:10px;padding:14px;text-align:center;border:1px solid rgba(99,102,241,.1);}
.ct-stat .num{font-size:24px;font-weight:700;color:var(--accent);line-height:1.2;}
.ct-stat .lbl{font-size:11px;color:var(--text-muted);margin-top:4px;}
.ct-tbl{width:100%;border-collapse:collapse;font-size:13px;}
.ct-tbl th{text-align:left;padding:8px 10px;border-bottom:2px solid var(--border);color:var(--text-muted);font-weight:500;font-size:11px;text-transform:uppercase;letter-spacing:.5px;}
.ct-tbl td{padding:8px 10px;border-bottom:1px solid var(--border);color:var(--text);}
.ct-tbl tr:hover td{background:rgba(99,102,241,.04);}
.ct-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:10px;font-weight:600;}
.ct-badge-oc{background:rgba(99,102,241,.15);color:var(--accent);}
.ct-badge-og{background:rgba(34,197,94,.15);color:#22c55e;}
.ct-btn{padding:8px 16px;border-radius:8px;border:none;cursor:pointer;font-size:13px;font-weight:500;transition:.15s;}
.ct-btn-primary{background:var(--accent);color:#fff;}
.ct-btn-primary:hover{filter:brightness(1.1);}
.ct-btn-danger{background:#ef4444;color:#fff;}
.ct-btn-danger:hover{background:#dc2626;}
.ct-btn-ghost{background:transparent;border:1px solid var(--border);color:var(--text);}
.ct-btn-ghost:hover{background:rgba(99,102,241,.06);}
.ct-btn:disabled{opacity:.5;cursor:not-allowed;}
.ct-import{border:2px dashed var(--border);border-radius:10px;padding:24px;text-align:center;cursor:pointer;transition:.15s;}
.ct-import:hover{border-color:var(--accent);background:rgba(99,102,241,.04);}
.ct-import-icon{font-size:32px;margin-bottom:8px;}
.ct-import-txt{color:var(--text-muted);font-size:13px;}
.ct-progress{height:6px;border-radius:3px;background:var(--border);overflow:hidden;margin-top:8px;}
.ct-progress-bar{height:100%;border-radius:3px;background:var(--accent);transition:width .3s;}
.ct-manual-row{display:flex;gap:8px;align-items:center;margin-top:10px;}
.ct-manual-row input{flex:1;padding:7px 10px;border:1px solid var(--border);border-radius:6px;background:var(--bg);color:var(--text);font-size:13px;}
.ct-pending{margin-top:10px;max-height:200px;overflow-y:auto;}
.ct-pending-item{display:flex;align-items:center;justify-content:space-between;padding:6px 8px;border-radius:6px;margin-bottom:3px;background:rgba(99,102,241,.04);}
.ct-pending-name{font-size:12px;font-weight:500;color:var(--text);}
.ct-pending-num{font-size:11px;color:var(--text-muted);}
.ct-empty{text-align:center;padding:32px;color:var(--text-muted);font-size:14px;}
.ct-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:12px;}
.ct-search{width:100%;padding:8px 12px;border:1px solid var(--border);border-radius:8px;background:var(--bg);color:var(--text);font-size:13px;margin-bottom:8px;}
.ct-discover-card{background:linear-gradient(135deg,#6366f1 0%,#8b5cf6 100%);border-radius:12px;padding:20px;color:#fff;}
.ct-discover-card h3{color:#fff;margin:0 0 8px;}
.ct-discover-card p{opacity:.85;font-size:13px;margin:0 0 16px;line-height:1.5;}
.ct-discover-opts{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:16px;}
.ct-discover-opt{display:flex;align-items:center;gap:6px;font-size:13px;}
.ct-discover-opt input[type=checkbox]{accent-color:#fff;}
.ct-discover-opt input[type=number]{width:60px;padding:4px 8px;border:1px solid rgba(255,255,255,.3);border-radius:6px;background:rgba(255,255,255,.15);color:#fff;font-size:12px;}
.ct-result{background:rgba(255,255,255,.12);border-radius:8px;padding:12px;margin-top:12px;font-size:13px;display:none;}
`;
  document.head.appendChild(css);

  el.innerHTML = `
<div class="ct-wrap">
  <div class="ct-side">
    <h3 style="margin:0 0 12px;font-size:15px;font-weight:600;color:var(--text)">&#128241; 设备列表</h3>
    <div id="ct-dev-list"></div>
  </div>
  <div class="ct-main">
    <div class="ct-stat-row">
      <div class="ct-stat"><div class="num" id="ct-stat-total">-</div><div class="lbl">设备联系人</div></div>
      <div class="ct-stat"><div class="num" id="ct-stat-injected">-</div><div class="lbl">已注入 (OC_)</div></div>
      <div class="ct-stat"><div class="num" id="ct-stat-original">-</div><div class="lbl">原有联系人</div></div>
      <div class="ct-stat"><div class="num" id="ct-stat-pending">0</div><div class="lbl">待注入</div></div>
    </div>

    <div class="ct-card">
      <h3>&#128228; 导入联系人</h3>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;">
        <div class="ct-import" onclick="_ctCsvUpload()">
          <div class="ct-import-icon">&#128196;</div>
          <div class="ct-import-txt">点击上传 CSV 文件<br><small>格式: name, number</small></div>
        </div>
        <div style="padding:4px;">
          <div style="font-size:13px;font-weight:500;color:var(--text);margin-bottom:8px;">手动添加</div>
          <div class="ct-manual-row">
            <input id="ct-add-name" placeholder="姓名" />
            <input id="ct-add-num" placeholder="+39..." />
            <button class="ct-btn ct-btn-ghost" onclick="_ctAddManual()">+</button>
          </div>
          <div id="ct-pending-list" class="ct-pending"></div>
        </div>
      </div>
      <div class="ct-actions">
        <button class="ct-btn ct-btn-primary" id="ct-btn-inject" onclick="_ctInjectAll()" disabled>&#128640; 批量注入到设备</button>
        <button class="ct-btn ct-btn-ghost" id="ct-btn-export" onclick="_ctExportContacts()" disabled>&#128229; 导出通讯录</button>
        <button class="ct-btn ct-btn-ghost" id="ct-btn-sync" onclick="_ctSyncToOther()" disabled>&#128260; 同步到其他设备</button>
        <button class="ct-btn ct-btn-danger" id="ct-btn-clean" onclick="_ctCleanInjected()" disabled>&#128465; 清理已注入</button>
        <div id="ct-inject-progress" style="flex:1;display:none;">
          <div class="ct-progress"><div class="ct-progress-bar" id="ct-progress-bar" style="width:0%"></div></div>
          <div id="ct-inject-status" style="font-size:11px;color:var(--text-muted);margin-top:4px;"></div>
        </div>
      </div>
    </div>

    <div class="ct-card ct-discover-card">
      <h3>&#127793; TikTok 通讯录好友发现</h3>
      <p>注入联系人后，TikTok 会匹配通讯录中的注册用户。系统会自动关注并用 AI 发送个性化消息。</p>
      <div class="ct-discover-opts">
        <label class="ct-discover-opt"><input type="checkbox" id="ct-disc-follow" checked /> 自动关注</label>
        <label class="ct-discover-opt"><input type="checkbox" id="ct-disc-msg" checked /> AI 发消息</label>
        <label class="ct-discover-opt">最多 <input type="number" id="ct-disc-max" value="20" min="1" max="50" /> 人</label>
      </div>
      <button class="ct-btn" id="ct-btn-discover" style="background:rgba(255,255,255,.2);color:#fff;border:1px solid rgba(255,255,255,.3);" onclick="_ctDiscover()" disabled>&#128270; 开始发现好友</button>
      <div class="ct-result" id="ct-discover-result"></div>
    </div>

    <div class="ct-card">
      <h3>&#128214; 设备通讯录 <span id="ct-list-count" style="font-size:12px;color:var(--text-muted);font-weight:400;"></span></h3>
      <input class="ct-search" id="ct-search" placeholder="搜索联系人..." oninput="_ctFilterList()" />
      <div id="ct-contact-list" style="max-height:400px;overflow-y:auto;">
        <div class="ct-empty">请先选择设备</div>
      </div>
    </div>
  </div>
</div>`;

  _ctRefreshDeviceList();
}

async function _ctRefreshDeviceList() {
  try {
    const r = await api('GET', '/devices');
    const devs = (r && r.devices) ? r.devices : (Array.isArray(r) ? r : []);
    const el = document.getElementById('ct-dev-list');
    if (!el) return;
    if (!devs.length) { el.innerHTML = '<div class="ct-empty">无在线设备</div>'; return; }
    el.innerHTML = devs.map(d => {
      const id = d.device_id || d.serial || d.id || '';
      const alias = d.alias || d.name || id.slice(0,8);
      const online = d.status === 'online' || d.online;
      return `<div class="ct-dev ${_ctSelectedDevice===id?'active':''}" onclick="_ctSelectDevice('${id}')">
        <div class="ct-dev-dot" style="background:${online?'#22c55e':'#94a3b8'}"></div>
        <div><div class="ct-dev-name">${alias}</div><div class="ct-dev-serial">${id.slice(0,12)}</div></div>
      </div>`;
    }).join('');
  } catch(e) { console.warn('ct dev list:', e); }
}

async function _ctSelectDevice(id) {
  _ctSelectedDevice = id;
  _ctRefreshDeviceList();
  ['ct-btn-inject','ct-btn-clean','ct-btn-discover','ct-btn-export','ct-btn-sync'].forEach(
    b => { const el=document.getElementById(b); if(el) el.disabled=false; }
  );
  await _ctLoadContacts();
}

async function _ctLoadContacts() {
  if (!_ctSelectedDevice) return;
  const el = document.getElementById('ct-contact-list');
  el.innerHTML = '<div class="ct-empty">加载中...</div>';
  try {
    const r = await api('GET', '/devices/' + encodeURIComponent(_ctSelectedDevice) + '/contacts?limit=500');
    _ctContacts = r.contacts || [];
    const injected = _ctContacts.filter(c => c.name.startsWith('OC_')).length;
    const original = _ctContacts.length - injected;
    document.getElementById('ct-stat-total').textContent = _ctContacts.length;
    document.getElementById('ct-stat-injected').textContent = injected;
    document.getElementById('ct-stat-original').textContent = original;
    document.getElementById('ct-list-count').textContent = `(${_ctContacts.length})`;
    _ctRenderList(_ctContacts);
  } catch(e) {
    el.innerHTML = '<div class="ct-empty">读取通讯录失败</div>';
  }
}

function _ctRenderList(list) {
  const el = document.getElementById('ct-contact-list');
  if (!list.length) { el.innerHTML = '<div class="ct-empty">通讯录为空</div>'; return; }
  el.innerHTML = `<table class="ct-tbl"><thead><tr><th>#</th><th>姓名</th><th>号码</th><th>来源</th></tr></thead><tbody>${
    list.map((c,i) => {
      const isOC = c.name.startsWith('OC_');
      const displayName = isOC ? c.name.slice(3) : c.name;
      return `<tr><td>${i+1}</td><td>${displayName}</td><td>${c.number||'-'}</td>
        <td><span class="ct-badge ${isOC?'ct-badge-oc':'ct-badge-og'}">${isOC?'注入':'原有'}</span></td></tr>`;
    }).join('')
  }</tbody></table>`;
}

function _ctFilterList() {
  const q = (document.getElementById('ct-search').value||'').toLowerCase();
  if (!q) { _ctRenderList(_ctContacts); return; }
  _ctRenderList(_ctContacts.filter(c => c.name.toLowerCase().includes(q) || (c.number||'').includes(q)));
}

function _ctAddManual() {
  const nameEl = document.getElementById('ct-add-name');
  const numEl = document.getElementById('ct-add-num');
  const name = (nameEl.value||'').trim();
  const number = (numEl.value||'').trim();
  if (!name || !number) { _toast('请输入姓名和号码','error'); return; }
  _ctPendingList.push({name, number});
  nameEl.value = ''; numEl.value = '';
  _ctRenderPending();
}

function _ctRenderPending() {
  const el = document.getElementById('ct-pending-list');
  document.getElementById('ct-stat-pending').textContent = _ctPendingList.length;
  if (!_ctPendingList.length) { el.innerHTML = ''; return; }
  el.innerHTML = _ctPendingList.map((c,i) =>
    `<div class="ct-pending-item">
      <div><span class="ct-pending-name">${c.name}</span> <span class="ct-pending-num">${c.number}</span></div>
      <button style="background:none;border:none;cursor:pointer;color:var(--text-muted);font-size:16px;" onclick="_ctRemovePending(${i})">&#10005;</button>
    </div>`
  ).join('');
}

function _ctRemovePending(idx) {
  _ctPendingList.splice(idx, 1);
  _ctRenderPending();
}

function _ctCsvUpload() {
  const inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.csv,.txt';
  inp.onchange = async () => {
    const file = inp.files[0];
    if (!file) return;
    const text = await file.text();
    const lines = text.split(/\r?\n/).filter(l=>l.trim());
    if (lines.length < 2) { _toast('CSV 文件格式不正确','error'); return; }
    const header = lines[0].toLowerCase();
    let nameIdx=0, numIdx=1;
    if (header.includes('number') || header.includes('phone') || header.includes('tel')) {
      const cols = lines[0].split(',');
      cols.forEach((c,i) => { if(/number|phone|tel/.test(c.toLowerCase())) numIdx=i; });
      nameIdx = numIdx === 0 ? 1 : 0;
    }
    let added = 0;
    for (let i=1; i<lines.length; i++) {
      const cols = lines[i].split(',').map(c=>c.trim().replace(/^"|"$/g,''));
      if (cols.length >= 2 && cols[nameIdx] && cols[numIdx]) {
        _ctPendingList.push({ name: cols[nameIdx], number: cols[numIdx] });
        added++;
      }
    }
    _ctRenderPending();
    _toast(`已从 CSV 导入 ${added} 条联系人`, 'success');
  };
  inp.click();
}

async function _ctInjectAll() {
  if (!_ctSelectedDevice || !_ctPendingList.length) {
    _toast('请先选择设备并添加联系人','error'); return;
  }
  if (_ctBusy) return;
  _ctBusy = true;
  const btn = document.getElementById('ct-btn-inject');
  btn.disabled = true;
  btn.textContent = '注入中...';
  const prog = document.getElementById('ct-inject-progress');
  prog.style.display = '';
  const bar = document.getElementById('ct-progress-bar');
  const status = document.getElementById('ct-inject-status');

  try {
    bar.style.width = '10%';
    status.textContent = `正在注入 ${_ctPendingList.length} 条联系人...`;
    const r = await api('POST', '/devices/' + encodeURIComponent(_ctSelectedDevice) + '/contacts/batch', {
      contacts: _ctPendingList
    });
    bar.style.width = '100%';
    status.textContent = `完成: 成功 ${r.success||0} / 失败 ${r.failed||0} / 跳过 ${r.skipped||0}`;
    _toast(`注入完成: ${r.success||0} 成功`, 'success');
    _ctPendingList = [];
    _ctRenderPending();
    await _ctLoadContacts();
  } catch(e) {
    status.textContent = '注入失败: ' + (e.message||e);
    _toast('注入失败','error');
  }
  btn.textContent = '\u{1F680} 批量注入到设备';
  btn.disabled = false;
  _ctBusy = false;
}

async function _ctCleanInjected() {
  if (!_ctSelectedDevice) return;
  if (!(await ocDialog({title:'清理注入联系人',message:'确定清理所有已注入的 OC_ 联系人？原有联系人不受影响。',type:'danger',confirmText:'清理',cancelText:'取消'}))) return;
  try {
    const r = await api('DELETE', '/devices/' + encodeURIComponent(_ctSelectedDevice) + '/contacts/clean');
    _toast(`已清理 ${r.removed||0} 条注入联系人`, 'success');
    await _ctLoadContacts();
  } catch(e) { _toast('清理失败','error'); }
}

async function _ctDiscover() {
  if (!_ctSelectedDevice) { _toast('请先选择设备','error'); return; }
  if (_ctBusy) return;
  _ctBusy = true;
  const btn = document.getElementById('ct-btn-discover');
  btn.disabled = true;
  btn.textContent = '发现中...';
  const resultEl = document.getElementById('ct-discover-result');
  resultEl.style.display = '';
  resultEl.innerHTML = '&#9203; 正在 TikTok 中查找通讯录好友...';

  try {
    const r = await api('POST', '/tiktok/device/' + encodeURIComponent(_ctSelectedDevice) + '/find-contact-friends', {
      auto_follow: document.getElementById('ct-disc-follow').checked,
      auto_message: document.getElementById('ct-disc-msg').checked,
      max_friends: parseInt(document.getElementById('ct-disc-max').value) || 20,
    });
    resultEl.innerHTML = `&#9989; 发现完成<br/>找到 <b>${r.found||0}</b> 位好友, 关注 <b>${r.followed||0}</b>, 消息 <b>${r.messaged||0}</b>` +
      (r.errors&&r.errors.length ? `<br/><small style="opacity:.7">⚠ ${r.errors.join('; ')}</small>` : '');
    _toast(`好友发现完成: ${r.found||0} 人`, 'success');
  } catch(e) {
    resultEl.innerHTML = '&#10060; 失败: ' + (e.message||e);
  }
  btn.textContent = '\u{1F50D} 开始发现好友';
  btn.disabled = false;
  _ctBusy = false;
}

async function _ctExportContacts() {
  if (!_ctSelectedDevice) return;
  try {
    const r = await api('GET', '/devices/' + encodeURIComponent(_ctSelectedDevice) + '/contacts/export?only_injected=true');
    if (!r.contacts || !r.contacts.length) { _toast('没有可导出的联系人','error'); return; }
    let csv = 'name,number\n';
    for (const c of r.contacts) {
      const name = c.name.startsWith('OC_') ? c.name.slice(3) : c.name;
      csv += name + ',' + (c.number||'') + '\n';
    }
    const blob = new Blob(['\ufeff'+csv], {type:'text/csv;charset=utf-8'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = 'contacts_' + _ctSelectedDevice.slice(0,8) + '.csv';
    a.click(); URL.revokeObjectURL(url);
    _toast(`已导出 ${r.contacts.length} 条联系人`, 'success');
  } catch(e) { _toast('导出失败','error'); }
}

async function _ctSyncToOther() {
  if (!_ctSelectedDevice) return;
  try {
    const devResp = await api('GET', '/devices');
    const devs = (devResp && devResp.devices) ? devResp.devices : (Array.isArray(devResp) ? devResp : []);
    const others = devs.filter(d => {
      const did = d.device_id || d.serial || d.id || '';
      return did !== _ctSelectedDevice && (d.status==='online'||d.online);
    });
    if (!others.length) { _toast('没有其他在线设备','error'); return; }

    const srcResp = await api('GET', '/devices/' + encodeURIComponent(_ctSelectedDevice) + '/contacts/export?only_injected=true');
    const contacts = srcResp.contacts || [];
    if (!contacts.length) { _toast('当前设备没有已注入联系人','error'); return; }

    const names = others.map(d => d.alias||d.name||(d.device_id||d.serial||'').slice(0,8));
    const devList = others.map((d,i) => (i+1)+'. '+(d.alias||d.name||(d.device_id||d.serial||'').slice(0,8))).join('<br>');
    const chosen = await ocPrompt('同步联系人','',{message:'将 ' + contacts.length + ' 条联系人同步到哪些设备？<br><span style="font-size:10px;color:var(--text-muted)">'+devList+'</span>',inputPlaceholder:'编号(逗号分隔)或 all'});
    if (!chosen) return;

    let targetDevs = [];
    if (chosen.trim().toLowerCase() === 'all') {
      targetDevs = others;
    } else {
      const nums = chosen.split(',').map(n=>parseInt(n.trim())-1).filter(n=>n>=0&&n<others.length);
      targetDevs = nums.map(n=>others[n]);
    }
    if (!targetDevs.length) { _toast('未选择有效设备','error'); return; }

    // 去掉 OC_ 前缀后重新注入
    const cleanContacts = contacts.map(c => ({
      name: c.name.startsWith('OC_') ? c.name.slice(3) : c.name,
      number: c.number
    }));

    let successCount = 0;
    for (const dev of targetDevs) {
      const did = dev.device_id || dev.serial || dev.id || '';
      try {
        await api('POST', '/devices/' + encodeURIComponent(did) + '/contacts/batch', { contacts: cleanContacts });
        successCount++;
      } catch(e) { console.warn('sync to', did, e); }
    }
    _toast(`同步完成: ${successCount}/${targetDevs.length} 台设备`, 'success');
  } catch(e) { _toast('同步失败: ' + (e.message||e), 'error'); }
}

/* ══════════════════════════════════════════════════════════════
   通讯录弹出模态框
   ══════════════════════════════════════════════════════════════ */

/** 优先请求 enriched；若 404（旧版后端无此路由）则降级为原始通讯录并合成 stats */
async function _ctmFetchEnrichedOrFallback(deviceId) {
  try {
    return await api('GET', '/devices/' + encodeURIComponent(deviceId) + '/contacts/enriched');
  } catch (e1) {
    var msg = String((e1 && e1.message) || e1 || '');
    // 与「路由缺失」不同：主控 DeviceManager 无该机且 Worker 转发也失败时
    if (msg.indexOf('\u8BBE\u5907\u4E0D\u5B58\u5728') >= 0) {
      throw new Error(
        '\u672A\u5728\u96C6\u7FA4\u4EFB\u4E00 Worker \u4E0A\u53D1\u73B0\u8BE5\u8BBE\u5907\uFF08\u8282\u70B9\u79BB\u7EBF\u3001\u522B\u540D\u672A\u5339\u914D host_name\uFF0C\u6216\u9700\u914D\u7F6E OPENCLAW_WORKER_BASES\uFF09\u3002'
      );
    }
    if (msg.indexOf('404') < 0 && msg.indexOf('Not Found') < 0) throw e1;
    var raw = await api('GET', '/devices/' + encodeURIComponent(deviceId) + '/contacts?limit=500');
    var rows = raw.contacts || [];
    var list = rows.map(function(c) {
      var name = (c.name || '').trim();
      var inj = name.indexOf('OC_') === 0;
      var clean = inj ? name.slice(3) : name;
      return {
        name: clean,
        number: c.number || '',
        source: inj ? 'injected' : 'original',
        matched_app: false,
        platform: '',
        lead_score: 0,
        lead_status: '',
        greeted: false,
        messages: 0,
        last_chat: '',
        followed: false,
        discovery_time: ''
      };
    });
    var injected = list.filter(function(x) { return x.source === 'injected'; }).length;
    return {
      device_id: deviceId,
      contacts: list,
      stats: {
        total: list.length,
        injected: injected,
        matched: 0,
        greeted: 0,
        followed: 0
      },
      _fallback_basic: true,
      _fallback_reason: 'enriched_unavailable'
    };
  }
}

/** 标准视图说明条（P0：业务话术，技术细节折叠） */
function _ctmBannerStandardView(deviceId) {
  return '<div class="ctm-banner-info">' +
    '<div class="ctm-banner-info-title">\u6807\u51C6\u901A\u8BAF\u5F55\u89C6\u56FE</div>' +
    '<div class="ctm-banner-info-sub">\u5BA2\u6237\u4E0E\u7EBF\u7D22\u7684\u5173\u8054\u5206\u6790\u3001\u5339\u914D\u4E0E\u6253\u62DB\u547C\u7EDF\u8BA1\u6682\u672A\u53EF\u7528\uFF1B\u540D\u5355\u6D4F\u89C8\u3001\u641C\u7D22\u3001\u5BFC\u5165/\u5BFC\u51FA\u4ECD\u53EF\u6B63\u5E38\u4F7F\u7528\u3002</div>' +
    '<details class="ctm-details-tech">' +
      '<summary>\u6280\u672F\u652F\u6301\u4FE1\u606F</summary>' +
      '<div>\u589E\u5F3A\u6570\u636E\u63A5\u53E3\u672A\u8FD4\u56DE\uFF08\u5E38\u89C1\uFF1A\u5DE5\u4F5C\u8282\u70B9\u672A\u90E8\u7F72\u540C\u7248\u672C API\uFF0C\u6216\u8F6C\u53D1\u672A\u751F\u6548\uFF09\u3002\u5DF2\u81EA\u52A0\u8F7D\u57FA\u7840\u901A\u8BAF\u5F55\u5217\u8868\u3002</div>' +
      '<div style="margin-top:6px">device_id: <code style="font-size:10px;word-break:break-all">' + _escHtml(deviceId) + '</code></div>' +
    '</details>' +
  '</div>';
}

function _ttOpenContactsModal(deviceId) {
  _ttCloseModal();
  var overlay = document.createElement('div');
  overlay.className = 'ttp-modal-overlay';
  overlay.id = 'ttp-modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _ttCloseModal(); };
  overlay.innerHTML =
    '<div class="ttp-modal" style="width:720px">' +
      '<div class="ttp-modal-header">' +
        '<span style="font-size:20px">\u{1F4DE}</span>' +
        '<div class="ttp-modal-title">\u901A\u8BAF\u5F55\u7BA1\u7406</div>' +
        '<button class="ttp-small-btn" style="margin-left:auto;margin-right:4px;color:#22c55e;border-color:rgba(34,197,94,.3)" onclick="_ctmExportCSV()" title="\u5BFC\u51FA\u901A\u8BAF\u5F55">\u{1F4E5} \u5BFC\u51FA</button>' +
        '<button class="ttp-modal-close" onclick="_ttCloseModal()">\u2715</button>' +
      '</div>' +
      '<div class="ttp-modal-body" id="ctm-body">' +
        '<div style="text-align:center;padding:40px;color:#64748b">\u{23F3} \u52A0\u8F7D\u4E2D...</div>' +
      '</div>' +
    '</div>';
  document.body.appendChild(overlay);
  _ctmLoadData(deviceId);
}

async function _ctmLoadData(deviceId) {
  var body = document.getElementById('ctm-body');
  if (!body) return;
  window._ctmFallbackBasic = false;
  try {
    var r = await _ctmFetchEnrichedOrFallback(deviceId);
    var list = r.contacts || [];
    var stats = r.stats || {};
    var fb = !!r._fallback_basic;
    window._ctmFallbackBasic = fb;
    var fbNote = fb ? _ctmBannerStandardView(deviceId) : '';

    // 智能排序：已打招呼→已匹配未打招呼→未匹配
    list.sort(function(a, b) {
      var wa = (a.greeted ? 0 : a.matched_app ? 1 : 2);
      var wb = (b.greeted ? 0 : b.matched_app ? 1 : 2);
      return wa !== wb ? wa - wb : (b.lead_score || 0) - (a.lead_score || 0);
    });

    var matchRate = stats.total ? Math.round(stats.matched / stats.total * 100) : 0;
    var greetRate = stats.matched ? Math.round(stats.greeted / stats.matched * 100) : 0;
    var numMatched = fb ? '\u2014' : String(stats.matched);
    var numGreeted = fb ? '\u2014' : String(stats.greeted);
    var pctMatched = fb ? '\u2014' : (matchRate + '%');
    var pctGreeted = fb ? '\u2014' : (greetRate + '%');
    var funnelCls = 'ctm-funnel' + (fb ? ' ctm-funnel-degraded' : '');
    var progCls = 'ctm-progress-bar' + (fb ? ' ctm-progress-degraded' : '');
    var funnelCap = fb
      ? '<div class="ctm-funnel-caption">\u4E0B\u5217\u4E09\u9879\u4F9D\u8D56\u589E\u5F3A\u6570\u636E\uFF08\u5339\u914D/\u6253\u62DB\u547C\u767E\u5206\u6BD4\uFF09\uFF1B\u6807\u2014\u8868\u793A\u672A\u52A0\u8F7D\uFF0C\u975E\u4E1A\u52A1\u6392\u540D\u4E3A\u96F6\u3002</div>'
      : '';

    var filterSel =
      '<select id="ctm-filter-status" onchange="_ctmFilter()" class="ctm-input" style="width:148px;cursor:pointer">' +
      '<option value="all">\u5168\u90E8</option>';
    if (fb) {
      filterSel +=
        '<option value="matched" disabled title="\u9700\u589E\u5F3A\u89C6\u56FE">\u5DF2\u5339\u914D APP \u2026</option>' +
        '<option value="greeted" disabled title="\u9700\u589E\u5F3A\u89C6\u56FE">\u5DF2\u6253\u62DB\u547C \u2026</option>' +
        '<option value="ungreeted" disabled title="\u9700\u589E\u5F3A\u89C6\u56FE">\u672A\u6253\u62DB\u547C \u2026</option>' +
        '<option value="unmatched" disabled title="\u9700\u589E\u5F3A\u89C6\u56FE">\u672A\u5339\u914D \u2026</option>';
    } else {
      filterSel +=
        '<option value="matched">\u5DF2\u5339\u914D APP</option>' +
        '<option value="greeted">\u5DF2\u6253\u62DB\u547C</option>' +
        '<option value="ungreeted">\u672A\u6253\u62DB\u547C</option>' +
        '<option value="unmatched">\u672A\u5339\u914D</option>';
    }
    filterSel += '</select>';

    body.innerHTML =
      fbNote +
      // ── 漏斗式统计条 ──
      '<div class="' + funnelCls + '">' +
        '<div class="ctm-fn-step">' +
          '<div class="ctm-fn-num" style="color:#818cf8">' + stats.total + '</div>' +
          '<div class="ctm-fn-label">\u603B\u8054\u7CFB\u4EBA</div>' +
        '</div>' +
        '<div class="ctm-fn-arrow">\u2192</div>' +
        '<div class="ctm-fn-step">' +
          '<div class="ctm-fn-num" style="color:#22c55e">' + stats.injected + '</div>' +
          '<div class="ctm-fn-label">\u5DF2\u6CE8\u5165</div>' +
        '</div>' +
        '<div class="ctm-fn-arrow">\u2192</div>' +
        '<div class="ctm-fn-step">' +
          '<div class="ctm-fn-num" style="color:#f59e0b">' + numMatched + '<span class="ctm-fn-pct">' + pctMatched + '</span></div>' +
          '<div class="ctm-fn-label">\u5339\u914D APP</div>' +
        '</div>' +
        '<div class="ctm-fn-arrow">\u2192</div>' +
        '<div class="ctm-fn-step">' +
          '<div class="ctm-fn-num" style="color:#8b5cf6">' + numGreeted + '<span class="ctm-fn-pct">' + pctGreeted + '</span></div>' +
          '<div class="ctm-fn-label">\u5DF2\u6253\u62DB\u547C</div>' +
        '</div>' +
      '</div>' +
      funnelCap +
      // 漏斗进度条
      '<div class="' + progCls + '">' +
        '<div class="ctm-pb-fill" style="width:100%;background:#818cf8" title="\u603B\u8054\u7CFB\u4EBA"></div>' +
        '<div class="ctm-pb-fill" style="width:' + (fb ? 0 : matchRate) + '%;background:#f59e0b" title="\u5339\u914D APP"></div>' +
        '<div class="ctm-pb-fill" style="width:' + (fb ? 0 : greetRate) + '%;background:#8b5cf6" title="\u5DF2\u6253\u62DB\u547C"></div>' +
      '</div>' +

      // ── 操作栏 ──
      '<div class="ctm-actions">' +
        '<button class="ctm-btn ctm-btn-primary" onclick="_ctmCsvImport(\'' + deviceId + '\')">\u{1F4C4} \u5BFC\u5165CSV</button>' +
        '<button class="ctm-btn ctm-btn-warn" onclick="_ctmBatchAll(\'' + deviceId + '\')">\u{1F4F1} \u5E73\u5747\u5206\u914D</button>' +
        '<button class="ctm-btn ctm-btn-purple" onclick="_ctmDiscover(\'' + deviceId + '\')">\u{1F50D} \u597D\u53CB\u53D1\u73B0</button>' +
        '<button class="ctm-btn ctm-btn-danger" onclick="_ctmClean(\'' + deviceId + '\')">\u{1F5D1} \u6E05\u7406\u6CE8\u5165</button>' +
        '<button class="ctm-btn" style="color:#06b6d4;border-color:rgba(6,182,212,.3)" onclick="_ctmDedupCheck()">\u{1F50D} \u8DE8\u8BBE\u5907\u53BB\u91CD</button>' +
      '</div>' +

      // ── 添加 + 搜索 ──
      '<div class="ctm-add-row">' +
        '<input id="ctm-add-name" placeholder="\u59D3\u540D" class="ctm-input" style="flex:1">' +
        '<input id="ctm-add-num" placeholder="+39..." class="ctm-input" style="flex:1">' +
        '<button onclick="_ctmAddOne(\'' + deviceId + '\')" class="ctm-btn ctm-btn-primary" style="padding:7px 14px">+ \u6DFB\u52A0</button>' +
      '</div>' +
      '<div id="ctm-status" style="font-size:11px;color:#64748b;margin-bottom:8px;min-height:1px"></div>' +
      '<div class="ctm-filter-row">' +
        '<input id="ctm-search" placeholder="\u{1F50D} \u641C\u7D22\u8054\u7CFB\u4EBA..." oninput="_ctmFilter()" class="ctm-input" style="flex:1">' +
        filterSel +
      '</div>' +

      // ── 列表 ──
      '<div id="ctm-list" style="max-height:320px;overflow-y:auto;margin-top:8px"></div>';

    window._ctmContacts = list;
    window._ctmDeviceId = deviceId;
    _ctmRenderList(list);
  } catch(e) {
    window._ctmFallbackBasic = false;
    var em = String((e && e.message) || e || '');
    var userLine = '\u65E0\u6CD5\u52A0\u8F7D\u901A\u8BAF\u5F55\uFF0C\u8BF7\u7A0D\u540E\u91CD\u8BD5\u6216\u8054\u7CFB\u7BA1\u7406\u5458\u3002';
    if (em.indexOf('\u8BBE\u5907\u4E0D\u5B58\u5728') >= 0) {
      userLine = '\u672A\u5728\u96C6\u7FA4\u4E2D\u5B9A\u4F4D\u5230\u8BE5\u8BBE\u5907\uFF0C\u8BF7\u786E\u8BA4\u8282\u70B9\u5728\u7EBF\u4E14\u522B\u540D\u914D\u7F6E\u6B63\u786E\u3002';
    }
    body.innerHTML =
      '<div style="padding:20px">' +
        '<div class="ctm-banner-info" style="border-color:rgba(248,113,113,.35);background:rgba(248,113,113,.07)">' +
          '<div class="ctm-banner-info-title" style="color:#fca5a5">\u52A0\u8F7D\u672A\u6210\u529F</div>' +
          '<div class="ctm-banner-info-sub">' + _escHtml(userLine) + '</div>' +
          '<details class="ctm-details-tech"><summary>\u8BE6\u60C5\uFF08\u6392\u969C\u7528\uFF09</summary>' +
            '<pre style="white-space:pre-wrap;word-break:break-all;font-size:10px;color:#94a3b8;margin-top:8px">' + _escHtml(em) + '</pre>' +
          '</details>' +
        '</div>' +
      '</div>';
  }
}

var _CTM_PLAT_ICONS = {tiktok:'\u{1F3B5}',telegram:'\u{1F4AC}',whatsapp:'\u{1F4F1}',instagram:'\u{1F4F7}',facebook:'\u{1F310}',line:'\u{1F49A}',wechat:'\u{1F4E8}',linkedin:'\u{1F4BC}'};

function _ctmRenderList(list) {
  var el = document.getElementById('ctm-list');
  if (!el) return;
  if (!list.length) { el.innerHTML = '<div style="text-align:center;color:#64748b;padding:24px">\u{1F4AD} \u65E0\u8054\u7CFB\u4EBA</div>'; return; }
  el.innerHTML = '<table class="ctm-table">' +
    '<thead><tr>' +
      '<th style="text-align:left;width:22%">\u59D3\u540D</th>' +
      '<th style="text-align:left;width:18%">\u53F7\u7801</th>' +
      '<th style="text-align:center;width:12%">\u6765\u6E90</th>' +
      '<th style="text-align:center;width:16%">\u5339\u914D APP</th>' +
      '<th style="text-align:center;width:16%">\u6253\u62DB\u547C</th>' +
      '<th style="text-align:center;width:16%">\u8BE6\u60C5</th>' +
    '</tr></thead><tbody>' +
    list.map(function(c) {
      var scoreColor = (c.lead_score||0) >= 70 ? '#22c55e' : (c.lead_score||0) >= 40 ? '#f59e0b' : '#64748b';
      var platIcon = _CTM_PLAT_ICONS[c.platform] || '';

      // 来源标签
      var srcTag = c.source === 'injected'
        ? '<span class="ctm-tag ctm-tag-indigo">\u6CE8\u5165</span>'
        : '<span class="ctm-tag ctm-tag-green">\u539F\u6709</span>';

      // 匹配APP列
      var matchCell = c.matched_app
        ? '<span class="ctm-tag ctm-tag-success">' + platIcon + ' \u2713 \u5DF2\u5339\u914D</span>'
        : '<span style="color:#475569;font-size:10px">\u2014</span>';

      // 打招呼列
      var greetCell;
      if (c.greeted) {
        greetCell = '<span class="ctm-tag ctm-tag-purple">\u{1F4AC} ' + c.messages + '\u6761</span>';
      } else if (c.matched_app) {
        greetCell = '<button class="ctm-greet-btn" onclick="_ctmGreetOne(\'' + window._ctmDeviceId + '\',\'' + _escHtml(c.name) + '\')" title="\u53D1\u9001AI\u6253\u62DB\u547C">\u{1F44B} \u6253\u62DB\u547C</button>';
      } else {
        greetCell = '<span style="color:#475569;font-size:10px">\u2014</span>';
      }

      // 详情列：线索评分或最后聊天
      var detailCell = '';
      if (c.lead_score > 0) {
        detailCell = '<span style="font-size:11px;font-weight:600;color:' + scoreColor + '">' + c.lead_score + '\u5206</span>';
      } else if (c.last_chat) {
        var t = c.last_chat.split('T');
        detailCell = '<span style="font-size:10px;color:#64748b">' + (t[0]||'') + '</span>';
      } else {
        detailCell = '<span style="font-size:10px;color:#475569">\u2014</span>';
      }

      return '<tr>' +
        '<td class="ctm-td-name">' + _escHtml(c.name) + '</td>' +
        '<td style="color:#94a3b8;font-size:11px">' + (c.number||'-') + '</td>' +
        '<td style="text-align:center">' + srcTag + '</td>' +
        '<td style="text-align:center">' + matchCell + '</td>' +
        '<td style="text-align:center">' + greetCell + '</td>' +
        '<td style="text-align:center">' + detailCell + '</td>' +
      '</tr>';
    }).join('') + '</tbody></table>';
}

function _ctmFilter() {
  var q = (document.getElementById('ctm-search').value||'').toLowerCase();
  var st = (document.getElementById('ctm-filter-status')||{}).value || 'all';
  var list = (window._ctmContacts||[]).filter(function(c) {
    if (q && !c.name.toLowerCase().includes(q) && !(c.number||'').includes(q)) return false;
    if (st === 'matched') return c.matched_app;
    if (st === 'greeted') return c.greeted;
    if (st === 'ungreeted') return c.matched_app && !c.greeted;
    if (st === 'unmatched') return !c.matched_app;
    return true;
  });
  _ctmRenderList(list);
}

async function _ctmGreetOne(deviceId, contactName) {
  var st = document.getElementById('ctm-status');
  if (st) st.innerHTML = '\u{23F3} AI \u6B63\u5728\u751F\u6210\u6253\u62DB\u547C\u5185\u5BB9...';
  try {
    var r = await api('POST', '/tiktok/chat/preview-greet', {
      username: contactName, source: 'contact', device_id: deviceId
    });
    var msg = r.message || '';
    window._ctmAbMeta = {
      deviceId: deviceId,
      contactName: contactName,
      variant: r.variant || '',
      experiment: r.experiment || ''
    };
    _ctmShowGreetPreview(deviceId, contactName, msg, r.variant || '');
  } catch(e) {
    if (st) st.innerHTML = '\u274C AI\u751F\u6210\u5931\u8D25: ' + e.message;
  }
}

function _ctmShowGreetPreview(deviceId, contactName, message, abVariant) {
  var st = document.getElementById('ctm-status');
  if (!st) return;
  var abTag = abVariant
    ? '<span class="ctm-pv-ab" title="A/B">\u{1F9EA} ' + _escHtml(abVariant) + '</span>'
    : '';
  st.innerHTML =
    '<div class="ctm-preview">' +
      '<div class="ctm-pv-header">' +
        '<span>\u{1F44B} \u5411 <b>' + _escHtml(contactName) + '</b> \u6253\u62DB\u547C</span>' +
        '<span class="ctm-pv-engine">' + '\u{1F916} AI \u751F\u6210' + abTag + '</span>' +
      '</div>' +
      '<textarea id="ctm-greet-msg" class="ctm-pv-textarea">' + _escHtml(message) + '</textarea>' +
      '<div class="ctm-pv-actions">' +
        '<button class="ctm-btn ctm-btn-primary" onclick="_ctmSendGreet(\'' + _escHtml(deviceId) + '\',\'' + _escHtml(contactName) + '\')">\u2713 \u53D1\u9001</button>' +
        '<button class="ctm-btn ctm-btn-purple" onclick="_ctmGreetOne(\'' + _escHtml(deviceId) + '\',\'' + _escHtml(contactName) + '\')">\u{1F504} \u91CD\u65B0\u751F\u6210</button>' +
        '<button class="ctm-btn" onclick="document.getElementById(\'ctm-status\').innerHTML=\'\'">\u2715 \u53D6\u6D88</button>' +
      '</div>' +
    '</div>';
}

async function _ctmSendGreet(deviceId, contactName) {
  var textarea = document.getElementById('ctm-greet-msg');
  if (!textarea || !textarea.value.trim()) { _toast('\u6D88\u606F\u4E0D\u80FD\u4E3A\u7A7A','error'); return; }
  var st = document.getElementById('ctm-status');
  if (st) st.innerHTML = '\u{23F3} \u53D1\u9001\u4E2D...';
  var ab = window._ctmAbMeta || {};
  try {
    await api('POST', '/tiktok/chat/generate', {
      type: 'icebreaker',
      username: contactName,
      message: textarea.value.trim(),
      device_id: deviceId,
      variant: ab.variant || '',
      experiment: ab.experiment || '',
      record_ab: true
    });
    if (st) st.innerHTML = '\u2705 \u5DF2\u5411 ' + _escHtml(contactName) + ' \u53D1\u9001\u6253\u62DB\u547C';
    setTimeout(function(){ _ctmLoadData(deviceId); }, 1500);
  } catch(e) {
    if (st) st.innerHTML = '\u274C \u53D1\u9001\u5931\u8D25: ' + e.message;
  }
}

async function _ctmAddOne(deviceId) {
  var n=document.getElementById('ctm-add-name'), p=document.getElementById('ctm-add-num');
  if(!n||!p||!n.value.trim()||!p.value.trim()){_toast('\u8BF7\u586B\u5199\u59D3\u540D\u548C\u53F7\u7801','error');return;}
  try{await api('POST','/devices/'+encodeURIComponent(deviceId)+'/contacts/add',{name:n.value.trim(),number:p.value.trim()});n.value='';p.value='';_toast('\u5DF2\u6DFB\u52A0','success');_ctmLoadData(deviceId);}catch(e){_toast('\u6DFB\u52A0\u5931\u8D25','error');}
}

function _ctmCsvImport(deviceId) {
  var inp=document.createElement('input');inp.type='file';inp.accept='.csv,.txt';
  inp.onchange=async function(){
    var file=inp.files[0];if(!file)return;
    var text=await file.text();var lines=text.split(/\r?\n/).filter(function(l){return l.trim();});
    if(lines.length<2){_toast('CSV\u683C\u5F0F\u4E0D\u6B63\u786E','error');return;}
    var contacts=[];for(var i=1;i<lines.length;i++){var cols=lines[i].split(/[,;\t]/).map(function(c){return c.trim().replace(/^"|"$/g,'');});if(cols.length>=2)contacts.push({name:cols[0],number:cols[1]});}
    var st=document.getElementById('ctm-status');if(st)st.innerHTML='\u23F3 \u6CE8\u5165\u4E2D ('+contacts.length+'\u6761)...';
    try{var r=await api('POST','/devices/'+encodeURIComponent(deviceId)+'/contacts/batch',{contacts:contacts});if(st)st.innerHTML='\u2705 '+r.success+' \u6210\u529F, '+r.skipped+' \u8DF3\u8FC7, '+r.failed+' \u5931\u8D25';_ctmLoadData(deviceId);}catch(e){if(st)st.innerHTML='\u274C '+e.message;}
  };inp.click();
}

function _ctmBatchAll(deviceId) {
  var inp=document.createElement('input');inp.type='file';inp.accept='.csv,.txt';
  inp.onchange=async function(){
    var file=inp.files[0];if(!file)return;
    var text=await file.text();var lines=text.split(/\r?\n/).filter(function(l){return l.trim();});
    var contacts=[];for(var i=1;i<lines.length;i++){var cols=lines[i].split(/[,;\t]/).map(function(c){return c.trim().replace(/^"|"$/g,'');});if(cols.length>=2)contacts.push({name:cols[0],number:cols[1]});}
    if(!contacts.length){_toast('\u65E0\u6709\u6548\u6570\u636E','error');return;}
    var st=document.getElementById('ctm-status');if(st)st.innerHTML='\u23F3 \u83B7\u53D6\u8BBE\u5907...';
    try{
      var dr=await api('GET','/devices');var devs=(dr&&dr.devices)?dr.devices:(Array.isArray(dr)?dr:[]);
      var online=devs.filter(function(d){return d.status==='online'||d.online;});
      if(!online.length){_toast('\u65E0\u5728\u7EBF\u8BBE\u5907','error');return;}
      var per=Math.ceil(contacts.length/online.length);if(st)st.innerHTML='\u{1F4F1} '+contacts.length+'\u6761 \u2192 '+online.length+'\u53F0 (\u6BCF\u53F0~'+per+')';
      var ok=0;for(var d=0;d<online.length;d++){var did=online[d].device_id||online[d].serial||online[d].id;var chunk=contacts.slice(d*per,(d+1)*per);if(chunk.length)try{await api('POST','/devices/'+encodeURIComponent(did)+'/contacts/batch',{contacts:chunk});ok++;}catch(e){}}
      if(st)st.innerHTML='\u2705 '+ok+'/'+online.length+' \u53F0\u8BBE\u5907\u5206\u914D\u5B8C\u6210';
      _ttBatchLog({action:'\u5E73\u5747\u5206\u914D\u901A\u8BAF\u5F55',devices:online.length,success:ok,contacts:contacts.length,file:file.name});
      _ctmLoadData(deviceId);
    }catch(e){if(st)st.innerHTML='\u274C '+e.message;}
  };inp.click();
}

async function _ctmDiscover(deviceId) {
  if(!(await ocDialog({title:'查找通讯录好友',message:'在 TikTok 中自动查找通讯录好友？',type:'info',confirmText:'开始',cancelText:'取消'})))return;
  var st=document.getElementById('ctm-status');if(st)st.innerHTML='\u{1F50D} \u6B63\u5728\u67E5\u627E...';
  try{var r=await api('POST','/tiktok/device/'+encodeURIComponent(deviceId)+'/find-contact-friends',{auto_follow:true,auto_message:true,max_friends:20});
    if(st)st.innerHTML='\u2705 \u627E\u5230 '+(r.found||0)+' \u4F4D, \u5173\u6CE8 '+(r.followed||0)+', \u6D88\u606F '+(r.messaged||0);
  }catch(e){if(st)st.innerHTML='\u274C '+e.message;}
}

async function _ctmClean(deviceId) {
  if(!(await ocDialog({title:'清理注入联系人',message:'确定清理所有 OC_ 前缀的注入联系人？',type:'danger',confirmText:'清理',cancelText:'取消'})))return;
  try{var r=await api('DELETE','/devices/'+encodeURIComponent(deviceId)+'/contacts/clean');_toast('\u5DF2\u6E05\u7406 '+(r.removed||0)+' \u6761','success');_ctmLoadData(deviceId);}catch(e){_toast('\u6E05\u7406\u5931\u8D25','error');}
}

async function _ctmDedupCheck() {
  var st = document.getElementById('ctm-status');
  if (st) st.innerHTML = '\u{23F3} \u6B63\u5728\u8DE8\u8BBE\u5907\u626B\u63CF\u901A\u8BAF\u5F55...';
  try {
    var r = await api('GET', '/devices/contacts/dedup-check');
    var dups = r.duplicates || [];
    var count = r.duplicate_count || 0;
    if (count === 0) {
      if (st) st.innerHTML = '\u2705 \u672A\u53D1\u73B0\u91CD\u590D — ' + r.total_devices + ' \u53F0\u8BBE\u5907\u5DF2\u68C0\u67E5';
      return;
    }
    var html = '<div class="ctm-preview" style="max-height:200px;overflow-y:auto">' +
      '<div style="font-size:12px;font-weight:600;color:#f59e0b;margin-bottom:8px">\u26A0 \u53D1\u73B0 ' + count + ' \u4E2A\u91CD\u590D\u53F7\u7801</div>' +
      '<table class="ctm-table"><thead><tr>' +
        '<th style="text-align:left">\u53F7\u7801</th>' +
        '<th style="text-align:center">\u8BBE\u5907\u6570</th>' +
        '<th style="text-align:left">\u8BBE\u5907\u5217\u8868</th>' +
      '</tr></thead><tbody>';
    dups.slice(0, 20).forEach(function(d) {
      html += '<tr>' +
        '<td style="color:#e2e8f0;font-size:11px">' + d.number + '</td>' +
        '<td style="text-align:center"><span class="ctm-tag ctm-tag-indigo">' + d.device_count + '</span></td>' +
        '<td style="color:#94a3b8;font-size:10px">' + d.devices.map(function(s){return s.substring(0,8);}).join(', ') + '</td>' +
      '</tr>';
    });
    html += '</tbody></table>';
    if (dups.length > 20) html += '<div style="font-size:10px;color:#64748b;margin-top:4px">\u663E\u793A\u524D20\u6761 / \u5171' + count + '\u6761</div>';
    html += '</div>';
    if (st) st.innerHTML = html;
  } catch(e) {
    if (st) st.innerHTML = '\u274C \u68C0\u6D4B\u5931\u8D25: ' + (e.message||e);
  }
}

// ── CSV 导出工具 ──
function _downloadCSV(filename, csvContent) {
  var bom = '\uFEFF';
  var blob = new Blob([bom + csvContent], {type:'text/csv;charset=utf-8;'});
  var url = URL.createObjectURL(blob);
  var a = document.createElement('a'); a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
}

function _ctmExportCSV() {
  var list = window._ctmContacts || [];
  if (!list.length) { _toast('\u65E0\u6570\u636E\u53EF\u5BFC\u51FA','error'); return; }
  var csv = '\u59D3\u540D,\u53F7\u7801,\u6765\u6E90,\u5339\u914DAPP,\u5E73\u53F0,\u5DF2\u6253\u62DB\u547C,\u6D88\u606F\u6570,\u7EBF\u7D22\u5206\n';
  list.forEach(function(c) {
    csv += [c.name, c.number||'', c.source||'', c.matched_app?'\u662F':'\u5426',
            c.platform||'', c.greeted?'\u662F':'\u5426', c.messages||0, c.lead_score||0].join(',') + '\n';
  });
  var ts = new Date().toISOString().slice(0,10);
  _downloadCSV('contacts_' + (window._ctmDeviceId||'all') + '_' + ts + '.csv', csv);
  _toast('\u5DF2\u5BFC\u51FA ' + list.length + ' \u6761', 'success');
}

function _anmExportCSV() {
  var tab = window._anmCurrentTab || 'leads';
  var body = document.getElementById('anm-body');
  if (!body) return;

  if (tab === 'leads') {
    var cards = body.querySelectorAll('.anm-lead-card');
    if (!cards.length) { _toast('\u65E0\u6570\u636E','error'); return; }
    var csv = '\u7528\u6237\u540D,\u9636\u6BB5,\u8BC4\u5206,Bio,Followers\n';
    cards.forEach(function(card) {
      var name = (card.querySelector('.anm-lead-name')||{}).textContent||'';
      var stage = (card.querySelector('.anm-stage-tag')||{}).textContent||'';
      var score = (card.querySelector('.anm-lead-score')||{}).textContent||'';
      var bio = (card.querySelector('.anm-lead-bio')||{}).textContent||'';
      var meta = (card.querySelector('.anm-lead-meta')||{}).textContent||'';
      csv += [name, stage, score.replace(/\D/g,''), '"'+(bio||'')+'"', meta].join(',') + '\n';
    });
    _downloadCSV('leads_' + (window._anmDeviceId||'all') + '_' + new Date().toISOString().slice(0,10) + '.csv', csv);
    _toast('\u5DF2\u5BFC\u51FA\u7EBF\u7D22\u6570\u636E','success');
  } else {
    _toast('\u5F53\u524D Tab \u6682\u4E0D\u652F\u6301\u5BFC\u51FA','info');
  }
}

function _ttCloseModal() {
  var el = document.getElementById('ttp-modal-overlay');
  if (el) el.remove();
}

/* ══════════════════════════════════════════════════════════════
   智能分析弹出模态框 (AI画像 + 线索 + 对话质量 融合)
   ══════════════════════════════════════════════════════════════ */

function _ttOpenAnalysisModal(deviceId) {
  _ttCloseModal();
  var overlay = document.createElement('div');
  overlay.className = 'ttp-modal-overlay';
  overlay.id = 'ttp-modal-overlay';
  overlay.onclick = function(e) { if (e.target === overlay) _ttCloseModal(); };
  overlay.innerHTML =
    '<div class="ttp-modal" style="width:780px">' +
      '<div class="ttp-modal-header">' +
        '<span style="font-size:20px">\u{1F9E0}</span>' +
        '<div class="ttp-modal-title">\u667A\u80FD\u5206\u6790</div>' +
        '<div style="display:flex;gap:4px;margin-left:auto;margin-right:8px" id="anm-tabs">' +
          '<button class="anm-tab active" onclick="_anmTab(\'leads\')">\u2B50 \u7EBF\u7D22</button>' +
          '<button class="anm-tab" onclick="_anmTab(\'ai\')">\u{1F916} AI\u753B\u50CF</button>' +
          '<button class="anm-tab" onclick="_anmTab(\'conv\')">\u{1F4AC} \u5BF9\u8BDD</button>' +
        '</div>' +
        '<button class="ttp-small-btn" style="color:#22c55e;border-color:rgba(34,197,94,.3);margin-right:4px" onclick="_anmExportCSV()" title="\u5BFC\u51FA\u5F53\u524D\u6570\u636E">\u{1F4E5} \u5BFC\u51FA</button>' +
        '<button class="ttp-modal-close" onclick="_ttCloseModal()">\u2715</button>' +
      '</div>' +
      '<div class="ttp-modal-body" id="anm-body" style="min-height:400px">' +
        '<div style="text-align:center;padding:40px;color:#64748b">\u{23F3} \u52A0\u8F7D\u4E2D...</div>' +
      '</div>' +
    '</div>';

  var style = document.createElement('style');
  style.textContent = '.anm-tab{padding:5px 12px;font-size:11px;font-weight:600;border-radius:6px;border:1px solid rgba(255,255,255,.1);background:transparent;color:#64748b;cursor:pointer;transition:.15s}.anm-tab.active{background:rgba(99,102,241,.2);border-color:rgba(99,102,241,.4);color:#a5b4fc}.anm-tab:hover{background:rgba(99,102,241,.1)}';
  overlay.querySelector('.ttp-modal').appendChild(style);

  document.body.appendChild(overlay);
  window._anmDeviceId = deviceId;
  window._anmCurrentTab = 'leads';
  _anmLoadLeads(deviceId);
}

function _anmTab(tab) {
  var tabs = document.querySelectorAll('.anm-tab');
  tabs.forEach(function(t) { t.classList.remove('active'); });
  var idx = {leads:0,ai:1,conv:2}[tab]||0;
  if (tabs[idx]) tabs[idx].classList.add('active');
  window._anmCurrentTab = tab;
  var deviceId = window._anmDeviceId;
  if (tab === 'leads') _anmLoadLeads(deviceId);
  else if (tab === 'ai') _anmLoadAiProfiles(deviceId);
  else if (tab === 'conv') _anmLoadConversations(deviceId);
}

async function _anmLoadLeads(deviceId) {
  var body = document.getElementById('anm-body');
  if (!body) return;
  body.innerHTML = '<div style="text-align:center;padding:40px;color:#64748b">\u{23F3} \u52A0\u8F7D\u7EBF\u7D22...</div>';
  try {
    var r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/leads?limit=50', {credentials:'include'});
    if (!r.ok) throw new Error('HTTP ' + r.status);
    var data = await r.json();
    var leads = data.leads || [];
    if (!leads.length) { body.innerHTML = '<div style="text-align:center;color:#64748b;padding:60px">\u{1F4AD} \u6682\u65E0\u7EBF\u7D22<br><span style="font-size:12px">\u8BF7\u5148\u8FD0\u884C\u5F15\u6D41\u4EFB\u52A1</span></div>'; return; }

    // 阶段聚合
    var stages = {new:0,responded:0,qualified:0,pitched:0,converted:0,lost:0};
    var stageLabel = {new:'\u65B0\u7EBF\u7D22',responded:'\u5DF2\u56DE\u590D',qualified:'\u5DF2\u8D44\u8D28',pitched:'\u5DF2\u63A8\u8350',converted:'\u5DF2\u8F6C\u5316',lost:'\u5DF1\u6D41\u5931'};
    var stageColor = {new:'#60a5fa',responded:'#818cf8',qualified:'#f59e0b',pitched:'#f472b6',converted:'#22c55e',lost:'#64748b'};
    leads.forEach(function(l) {
      var s = (l.status || l.stage || 'new').toLowerCase();
      if (stages.hasOwnProperty(s)) stages[s]++;
      else stages['new']++;
    });
    var total = leads.length;

    // ── 漏斗可视化 ──
    var funnelStages = ['new','responded','qualified','pitched','converted'];
    var funnelHTML = '<div class="anm-funnel">';
    for (var fi = 0; fi < funnelStages.length; fi++) {
      var fs = funnelStages[fi];
      var count = stages[fs] || 0;
      var pct = total > 0 ? Math.round(count / total * 100) : 0;
      var width = Math.max(20, 100 - fi * 16);
      funnelHTML += '<div class="anm-fn-row">' +
        '<div class="anm-fn-bar" style="width:' + width + '%;background:' + stageColor[fs] + '22;border:1px solid ' + stageColor[fs] + '44">' +
          '<span class="anm-fn-bar-fill" style="width:' + pct + '%;background:' + stageColor[fs] + '55"></span>' +
          '<span class="anm-fn-bar-text">' +
            '<span style="color:' + stageColor[fs] + ';font-weight:600">' + count + '</span> ' +
            (stageLabel[fs]||fs) +
            '<span style="opacity:.5;margin-left:4px">' + pct + '%</span>' +
          '</span>' +
        '</div>' +
        (fi < funnelStages.length - 1 ? '<div class="anm-fn-arrow">\u25BC</div>' : '') +
      '</div>';
    }
    funnelHTML += '</div>';

    // ── 阶段分布条 ──
    var distHTML = '<div class="anm-dist">';
    var activeStages = Object.keys(stages).filter(function(k){ return stages[k] > 0; });
    activeStages.forEach(function(s) {
      var pct = Math.round(stages[s] / total * 100);
      distHTML += '<div class="anm-dist-seg" style="flex:' + stages[s] + ';background:' + (stageColor[s]||'#475569') + '" title="' + (stageLabel[s]||s) + ': ' + stages[s] + ' (' + pct + '%)"></div>';
    });
    distHTML += '</div><div class="anm-dist-legend">';
    activeStages.forEach(function(s) {
      distHTML += '<span><span class="anm-dot" style="background:' + (stageColor[s]||'#475569') + '"></span>' + (stageLabel[s]||s) + ' ' + stages[s] + '</span>';
    });
    distHTML += '</div>';

    // ── 线索卡片列表 ──
    var listHTML = '<div class="anm-lead-grid">';
    leads.slice(0, 30).forEach(function(l) {
      var score = l.score || 0;
      var scoreColor = score >= 70 ? '#22c55e' : score >= 40 ? '#f59e0b' : '#64748b';
      var st = (l.status||l.stage||'new').toLowerCase();
      var sc = stageColor[st] || '#64748b';
      var sn = stageLabel[st] || st;
      listHTML += '<div class="anm-lead-card">' +
        '<div style="flex:1;min-width:0">' +
          '<div class="anm-lead-name">@' + _escHtml(l.username||l.name||'?') +
            '<span class="anm-stage-tag" style="background:' + sc + '18;color:' + sc + '">' + sn + '</span>' +
          '</div>' +
          (l.bio ? '<div class="anm-lead-bio">' + _escHtml(l.bio.substring(0,60)) + '</div>' : '') +
          '<div class="anm-lead-meta">' +
            (l.followers ? l.followers + ' followers' : '') +
            (l.last_interaction ? ' \u00B7 ' + l.last_interaction : '') +
          '</div>' +
        '</div>' +
        '<div class="anm-lead-score" style="color:' + scoreColor + '">' + score + '<span>\u5206</span></div>' +
      '</div>';
    });
    listHTML += '</div>';

    body.innerHTML = funnelHTML + distHTML + '<div style="margin-top:14px;border-top:1px solid rgba(255,255,255,.06);padding-top:14px">' +
      '<div style="font-size:12px;font-weight:600;color:#94a3b8;margin-bottom:8px">\u7EBF\u7D22\u5217\u8868 (' + total + ')</div>' + listHTML + '</div>';
  } catch(e) { body.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px">\u52A0\u8F7D\u5931\u8D25: ' + e.message + '</div>'; }
}

async function _anmLoadAiProfiles(deviceId) {
  var body = document.getElementById('anm-body');
  if (!body) return;
  body.innerHTML = '<div style="text-align:center;padding:40px;color:#64748b">\u{1F9E0} AI \u5206\u6790\u4E2D...</div>';
  try {
    var r = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/leads?limit=8', {credentials:'include'});
    var data = await r.json();
    var leads = data.leads || [];
    if (!leads.length) { body.innerHTML = '<div style="text-align:center;color:#64748b;padding:60px">\u{1F4AD} \u6682\u65E0\u7EBF\u7D22</div>'; return; }
    var html = '<div style="display:grid;grid-template-columns:1fr 1fr;gap:10px">';
    for (var i = 0; i < leads.length; i++) {
      var l = leads[i];
      try {
        var p = await api('POST', '/tiktok/chat/analyze-profile', {username:l.username||l.name||'',bio:l.bio||'',followers:l.followers||0});
        html += '<div style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:10px;padding:12px">' +
          '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">' +
            '<span style="font-size:12px;font-weight:600;color:#e2e8f0">@' + (l.username||l.name||'?') + '</span>' +
            '<span style="font-size:9px;padding:2px 8px;border-radius:8px;background:rgba(99,102,241,.12);color:#818cf8">' + (p.account_type||'user') + '</span>' +
          '</div>' +
          (p.industry ? '<div style="font-size:11px;color:#94a3b8;margin-bottom:2px">\u{1F3E2} ' + p.industry + '</div>' : '') +
          (p.interests && p.interests.length ? '<div style="font-size:11px;color:#94a3b8;margin-bottom:2px">\u2764\uFE0F ' + p.interests.slice(0,3).join(', ') + '</div>' : '') +
          (p.personality ? '<div style="font-size:11px;color:#94a3b8;margin-bottom:2px">\u{1F3AD} ' + p.personality + '</div>' : '') +
          (p.suggested_topics && p.suggested_topics.length ? '<div style="font-size:11px;color:#60a5fa;margin-top:4px">\u{1F4AC} ' + p.suggested_topics.slice(0,2).join(', ') + '</div>' : '') +
        '</div>';
      } catch(e) { html += '<div style="background:rgba(239,68,68,.05);border:1px solid rgba(239,68,68,.15);border-radius:10px;padding:12px;font-size:11px;color:#ef4444">@' + (l.username||'?') + ' \u5206\u6790\u5931\u8D25</div>'; }
    }
    html += '</div>';
    body.innerHTML = html;
  } catch(e) { body.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px">' + e.message + '</div>'; }
}

async function _anmLoadConversations(deviceId) {
  var body = document.getElementById('anm-body');
  if (!body) return;
  body.innerHTML = '<div style="text-align:center;padding:40px;color:#64748b">\u{1F4AC} \u52A0\u8F7D\u5BF9\u8BDD...</div>';
  try {
    var r = await api('GET', '/tiktok/chat/active');
    var convs = r.conversations || [];
    var kpi = r.kpi || {};
    var stageDist = r.stage_distribution || {};
    if (!convs.length) { body.innerHTML = '<div style="text-align:center;color:#64748b;padding:60px">\u{1F4AD} \u6682\u65E0\u6D3B\u8DC3\u5BF9\u8BDD</div>'; return; }

    var stageColors = {icebreak:'#60a5fa',rapport:'#818cf8',qualify:'#f59e0b',soft_pitch:'#f472b6',referral:'#22c55e',follow_up:'#94a3b8',cool_down:'#64748b'};
    var stageNames = {icebreak:'\u7834\u51B0',rapport:'\u5EFA\u7ACB\u5173\u7CFB',qualify:'\u8D44\u8D28',soft_pitch:'\u8F6F\u63A8\u8350',referral:'\u5F15\u6D41',follow_up:'\u8DDF\u8FDB',cool_down:'\u51B7\u5374'};

    // ── KPI 指标卡 ──
    var rrColor = kpi.reply_rate >= 50 ? '#22c55e' : kpi.reply_rate >= 20 ? '#f59e0b' : '#ef4444';
    var qColor = kpi.avg_quality >= 60 ? '#22c55e' : kpi.avg_quality >= 30 ? '#f59e0b' : '#ef4444';
    var kpiHTML = '<div class="conv-kpi-row">' +
      '<div class="conv-kpi"><div class="conv-kpi-num" style="color:' + rrColor + '">' + (kpi.reply_rate||0) + '%</div><div class="conv-kpi-label">\u56DE\u590D\u7387</div></div>' +
      '<div class="conv-kpi"><div class="conv-kpi-num" style="color:' + qColor + '">' + (kpi.avg_quality||0) + '</div><div class="conv-kpi-label">\u5E73\u5747\u8D28\u91CF</div></div>' +
      '<div class="conv-kpi"><div class="conv-kpi-num" style="color:#818cf8">' + (kpi.avg_rounds||0) + '</div><div class="conv-kpi-label">\u5E73\u5747\u8F6E\u6B21</div></div>' +
      '<div class="conv-kpi"><div class="conv-kpi-num" style="color:#60a5fa">' + (kpi.total_messages||0) + '</div><div class="conv-kpi-label">\u603B\u6D88\u606F</div></div>' +
    '</div>';

    // ── 阶段分布条 ──
    var distTotal = Object.values(stageDist).reduce(function(a,b){return a+b;},0) || 1;
    var distHTML = '<div class="anm-dist" style="margin-bottom:4px">';
    Object.keys(stageDist).forEach(function(s) {
      distHTML += '<div class="anm-dist-seg" style="flex:' + stageDist[s] + ';background:' + (stageColors[s]||'#475569') + '" title="' + (stageNames[s]||s) + ': ' + stageDist[s] + '"></div>';
    });
    distHTML += '</div><div class="anm-dist-legend" style="margin-bottom:12px">';
    Object.keys(stageDist).forEach(function(s) {
      distHTML += '<span><span class="anm-dot" style="background:' + (stageColors[s]||'#475569') + '"></span>' + (stageNames[s]||s) + ' ' + stageDist[s] + '</span>';
    });
    distHTML += '</div>';

    // ── 对话列表（含质量评分）──
    var listHTML = '<div class="anm-lead-grid" style="max-height:240px">';
    convs.forEach(function(c) {
      var sc = stageColors[c.stage] || '#94a3b8';
      var sn = stageNames[c.stage] || c.stage || '-';
      var qs = c.quality_score || 0;
      var qc = qs >= 60 ? '#22c55e' : qs >= 30 ? '#f59e0b' : '#ef4444';
      var replyTag = c.has_reply
        ? '<span class="anm-stage-tag" style="background:rgba(34,197,94,.12);color:#22c55e">\u2713 \u5DF2\u56DE\u590D</span>'
        : '<span class="anm-stage-tag" style="background:rgba(100,116,139,.12);color:#94a3b8">\u23F3 \u672A\u56DE</span>';
      listHTML += '<div class="anm-lead-card">' +
        '<div style="flex:1;min-width:0">' +
          '<div class="anm-lead-name">@' + _escHtml(c.username||c.lead_id||'?') +
            '<span class="anm-stage-tag" style="background:' + sc + '18;color:' + sc + '">' + sn + '</span>' +
            replyTag +
          '</div>' +
          '<div class="anm-lead-meta">' + (c.msg_count||c.message_count||0) + '\u8F6E \u00B7 \u{1F464}' + (c.user_messages||0) + ' \u{1F916}' + (c.bot_messages||0) +
            (c.last_preview ? ' \u00B7 "' + _escHtml(c.last_preview.substring(0,30)) + '..."' : '') +
          '</div>' +
        '</div>' +
        '<div style="display:flex;flex-direction:column;align-items:flex-end;gap:4px;flex-shrink:0">' +
          '<div class="conv-quality-ring" style="--q-color:' + qc + ';--q-pct:' + qs + '">' +
            '<span style="color:' + qc + '">' + qs + '</span>' +
          '</div>' +
          '<div style="display:flex;gap:3px">' +
            '<button type="button" class="ttp-small-btn" style="font-size:9px;padding:2px 6px;color:#22c55e" ' +
              'onclick=\'_ttConvQaSubmit(' + JSON.stringify(String(c.lead_id||c.username||'')) + ',"good")\'>\u{1F44D}</button>' +
            '<button type="button" class="ttp-small-btn" style="font-size:9px;padding:2px 6px;color:#ef4444" ' +
              'onclick=\'_ttConvQaSubmit(' + JSON.stringify(String(c.lead_id||c.username||'')) + ',"bad")\'>\u{1F44E}</button>' +
          '</div>' +
        '</div>' +
      '</div>';
    });
    listHTML += '</div>';

    body.innerHTML = kpiHTML + distHTML + listHTML +
      '<div id="conv-strategy-zone" style="margin-top:12px;padding-top:12px;border-top:1px solid rgba(255,255,255,.06)">' +
        '<div style="text-align:center;color:#64748b;font-size:11px">\u{1F9E0} \u52A0\u8F7D\u7B56\u7565\u5206\u6790...</div>' +
      '</div>';
    _loadStrategyAnalysis();
  } catch(e) { body.innerHTML = '<div style="color:#ef4444;text-align:center;padding:40px">' + e.message + '</div>'; }
}

async function _ttConvQaSubmit(leadKey, label) {
  try {
    await api('POST', '/conversations/qa', {lead_key: leadKey, label: label, note: ''});
    _toast(label === 'good' ? '\u5DF2\u6807\u8BB0\u4F18\u8D28' : '\u5DF2\u6807\u8BB0\u5F85\u6539\u8FDB', 'success');
  } catch(e) {
    _toast(e.message, 'error');
  }
}

async function _loadStrategyAnalysis() {
  var zone = document.getElementById('conv-strategy-zone');
  if (!zone) return;
  try {
    var r = await api('GET', '/tiktok/chat/strategy-analysis');
    var insights = r.insights || [];
    var openers = r.top_openers || [];
    var patterns = r.reply_patterns || {};
    var summary = r.summary || {};

    var html = '<div style="font-size:12px;font-weight:600;color:#e2e8f0;margin-bottom:8px">\u{1F9E0} AI \u7B56\u7565\u5206\u6790</div>';

    // 优化建议
    if (insights.length) {
      html += '<div style="display:grid;gap:6px;margin-bottom:10px">';
      var insightIcon = {warning:'\u26A0\uFE0F',info:'\u{1F4A1}',success:'\u2705'};
      var insightBg = {warning:'rgba(245,158,11,.08)',info:'rgba(99,102,241,.08)',success:'rgba(34,197,94,.08)'};
      var insightBorder = {warning:'rgba(245,158,11,.2)',info:'rgba(99,102,241,.2)',success:'rgba(34,197,94,.2)'};
      var insightColor = {warning:'#f59e0b',info:'#818cf8',success:'#22c55e'};
      insights.forEach(function(ins) {
        var t = ins.type || 'info';
        html += '<div style="background:' + (insightBg[t]||insightBg.info) + ';border:1px solid ' + (insightBorder[t]||insightBorder.info) + ';border-radius:8px;padding:8px 10px;font-size:11px;color:' + (insightColor[t]||insightColor.info) + '">' +
          (insightIcon[t]||'') + ' ' + _escHtml(ins.text) + '</div>';
      });
      html += '</div>';
    }

    // 最佳开场白 TOP 5
    if (openers.length) {
      html += '<div style="font-size:11px;font-weight:600;color:#a5b4fc;margin-bottom:4px">\u{1F3C6} \u6700\u4F73\u5F00\u573A\u767D TOP ' + Math.min(5,openers.length) + '</div>';
      html += '<div style="display:grid;gap:4px;margin-bottom:10px">';
      openers.slice(0,5).forEach(function(op, idx) {
        var rateColor = op.rate >= 50 ? '#22c55e' : op.rate >= 25 ? '#f59e0b' : '#94a3b8';
        html += '<div style="background:rgba(255,255,255,.02);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:6px 10px;display:flex;align-items:center;gap:8px">' +
          '<span style="font-size:14px;font-weight:700;color:#818cf8;min-width:20px">#' + (idx+1) + '</span>' +
          '<div style="flex:1;min-width:0;font-size:11px;color:#cbd5e1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">"' + _escHtml(op.text) + '"</div>' +
          '<div style="text-align:right;flex-shrink:0">' +
            '<div style="font-size:12px;font-weight:600;color:' + rateColor + '">' + op.rate + '%</div>' +
            '<div style="font-size:9px;color:#64748b">' + op.replied + '/' + op.sent + '</div>' +
          '</div>' +
        '</div>';
      });
      html += '</div>';
    }

    // 回复模式分布
    var patternKeys = Object.keys(patterns);
    if (patternKeys.length) {
      var patternLabels = {referral_intent:'\u{1F517} \u5F15\u6D41\u610F\u5411',positive:'\u{1F44D} \u6B63\u9762',negative:'\u{1F44E} \u8D1F\u9762',question:'\u2753 \u63D0\u95EE',neutral:'\u{1F4AC} \u4E2D\u6027'};
      var patternColors = {referral_intent:'#22c55e',positive:'#60a5fa',negative:'#ef4444',question:'#f59e0b',neutral:'#94a3b8'};
      var ptotal = patternKeys.reduce(function(a,k){return a+patterns[k];},0) || 1;
      html += '<div style="font-size:11px;font-weight:600;color:#a5b4fc;margin-bottom:4px">\u{1F4CA} \u7528\u6237\u56DE\u590D\u6A21\u5F0F</div>';
      html += '<div class="anm-dist" style="margin-bottom:4px">';
      patternKeys.forEach(function(k) {
        html += '<div class="anm-dist-seg" style="flex:' + patterns[k] + ';background:' + (patternColors[k]||'#475569') + '" title="' + (patternLabels[k]||k) + ': ' + patterns[k] + '"></div>';
      });
      html += '</div><div class="anm-dist-legend">';
      patternKeys.forEach(function(k) {
        html += '<span><span class="anm-dot" style="background:' + (patternColors[k]||'#475569') + '"></span>' + (patternLabels[k]||k) + ' ' + patterns[k] + ' (' + Math.round(patterns[k]/ptotal*100) + '%)</span>';
      });
      html += '</div>';
    }

    zone.innerHTML = html;
  } catch(e) { zone.innerHTML = '<div style="color:#ef4444;font-size:11px">\u7B56\u7565\u5206\u6790\u52A0\u8F7D\u5931\u8D25</div>'; }
}

// 旧函数兼容（保持面板计数加载）
async function _ttPanelCtLoad(deviceId, safeId) {
  const el = document.getElementById('ppnl-ct-detail-' + safeId);
  const info = document.getElementById('ppnl-ct-' + safeId);
  if (!el) return;
  el.innerHTML = '<div style="color:var(--text-muted);font-size:11px;padding:6px 0">加载中...</div>';
  try {
    const r = await api('GET', '/devices/' + encodeURIComponent(deviceId) + '/contacts?limit=200');
    const list = r.contacts || [];
    const injected = list.filter(c => c.name.startsWith('OC_')).length;
    if (info) info.innerHTML = '<span style="color:#818cf8">' + list.length + '</span> 联系人 · <span style="color:#22c55e">' + injected + '</span> 已注入 · <span style="color:var(--text-muted)">' + (list.length - injected) + '</span> 原有';
    if (!list.length) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:11px;padding:8px 0;text-align:center">通讯录为空，请先导入联系人</div>';
      return;
    }
    el.innerHTML = '<div style="max-height:180px;overflow-y:auto;border:1px solid rgba(255,255,255,.06);border-radius:6px">' +
      '<table style="width:100%;font-size:11px;border-collapse:collapse">' +
      '<thead><tr style="border-bottom:1px solid rgba(255,255,255,.1)">' +
        '<th style="text-align:left;padding:5px 6px;color:var(--text-muted);font-weight:500">姓名</th>' +
        '<th style="text-align:left;padding:5px 6px;color:var(--text-muted);font-weight:500">号码</th>' +
        '<th style="padding:5px 4px;color:var(--text-muted);font-weight:500;width:40px">类型</th>' +
      '</tr></thead><tbody>' +
      list.slice(0, 100).map(function(c) {
        var isOC = c.name.startsWith('OC_');
        var dname = isOC ? c.name.slice(3) : c.name;
        return '<tr style="border-bottom:1px solid rgba(255,255,255,.04)">' +
          '<td style="padding:4px 6px;color:#e2e8f0">' + dname + '</td>' +
          '<td style="padding:4px 6px;color:#94a3b8">' + (c.number || '-') + '</td>' +
          '<td style="padding:4px 4px;text-align:center"><span style="font-size:9px;padding:1px 5px;border-radius:8px;' +
            (isOC ? 'background:rgba(99,102,241,.15);color:#818cf8' : 'background:rgba(34,197,94,.15);color:#22c55e') +
            '">' + (isOC ? '注入' : '原有') + '</span></td></tr>';
      }).join('') +
      '</tbody></table></div>' +
      (list.length > 100 ? '<div style="font-size:10px;color:var(--text-muted);text-align:center;margin-top:4px">显示前 100 条 / 共 ' + list.length + ' 条</div>' : '');
  } catch(e) {
    el.innerHTML = '<div style="color:#ef4444;font-size:11px">读取失败</div>';
  }
}

async function _ttPanelCtImport(deviceId, safeId) {
  const el = document.getElementById('ppnl-ct-detail-' + (safeId || deviceId.replace(/[^a-zA-Z0-9]/g,'_')));
  if (!el) return;

  el.innerHTML =
    '<div style="background:rgba(99,102,241,.06);border:1px solid rgba(99,102,241,.2);border-radius:8px;padding:10px;margin-top:6px">' +
      '<div style="font-size:12px;font-weight:600;color:var(--text-main);margin-bottom:8px">\u{1F4E5} 导入联系人</div>' +
      '<div style="display:flex;gap:6px;margin-bottom:8px">' +
        '<button id="ppnl-ct-csv-btn-' + safeId + '" onclick="_ttPanelCtCsvPick(\'' + deviceId + '\',\'' + safeId + '\')" ' +
          'style="flex:1;background:rgba(99,102,241,.12);border:1px dashed rgba(99,102,241,.3);border-radius:6px;padding:12px 8px;color:#818cf8;font-size:11px;cursor:pointer;text-align:center;font-weight:500">' +
          '\u{1F4C4} 上传 CSV 文件</button>' +
        '<button onclick="_ttPanelCtBatchAll(\'' + deviceId + '\',\'' + safeId + '\')" ' +
          'style="flex:1;background:rgba(245,158,11,.08);border:1px dashed rgba(245,158,11,.3);border-radius:6px;padding:12px 8px;color:#f59e0b;font-size:11px;cursor:pointer;text-align:center;font-weight:500">' +
          '\u{1F4F1} 平均分配到所有手机</button>' +
      '</div>' +
      '<div style="display:flex;gap:4px;align-items:center">' +
        '<input id="ppnl-ct-name-' + safeId + '" placeholder="姓名" style="flex:1;background:#1e293b;border:1px solid rgba(255,255,255,.1);border-radius:5px;padding:5px 8px;color:#e2e8f0;font-size:11px">' +
        '<input id="ppnl-ct-num-' + safeId + '" placeholder="+39..." style="flex:1;background:#1e293b;border:1px solid rgba(255,255,255,.1);border-radius:5px;padding:5px 8px;color:#e2e8f0;font-size:11px">' +
        '<button onclick="_ttPanelCtAddOne(\'' + deviceId + '\',\'' + safeId + '\')" style="background:#6366f1;border:none;border-radius:5px;color:#fff;font-size:11px;padding:5px 10px;cursor:pointer">+</button>' +
      '</div>' +
      '<div id="ppnl-ct-status-' + safeId + '" style="font-size:10px;color:var(--text-muted);margin-top:6px"></div>' +
    '</div>';
}

function _ttPanelCtCsvPick(deviceId, safeId) {
  var inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.csv,.txt';
  inp.onchange = async function() {
    var file = inp.files[0];
    if (!file) return;
    var text = await file.text();
    var lines = text.split(/\r?\n/).filter(function(l){return l.trim();});
    if (lines.length < 2) { _toast('CSV 格式不正确','error'); return; }
    var contacts = [];
    for (var i = 1; i < lines.length; i++) {
      var cols = lines[i].split(/[,;\t]/).map(function(c){return c.trim().replace(/^"|"$/g,'');});
      if (cols.length >= 2) contacts.push({name: cols[0], number: cols[1]});
    }
    var status = document.getElementById('ppnl-ct-status-' + safeId);
    if (status) status.innerHTML = '\u23F3 正在注入 ' + contacts.length + ' 条...';
    try {
      var r = await api('POST', '/devices/' + encodeURIComponent(deviceId) + '/contacts/batch', {contacts: contacts});
      if (status) status.innerHTML = '\u2705 完成: ' + (r.success||0) + ' 成功, ' + (r.skipped||0) + ' 跳过, ' + (r.failed||0) + ' 失败';
      _toast('注入完成: ' + (r.success||0) + ' 条', 'success');
      _ttPanelCtLoad(deviceId, safeId);
    } catch(e) {
      if (status) status.innerHTML = '\u274C 失败: ' + (e.message||e);
    }
  };
  inp.click();
}

async function _ttPanelCtAddOne(deviceId, safeId) {
  var nameEl = document.getElementById('ppnl-ct-name-' + safeId);
  var numEl = document.getElementById('ppnl-ct-num-' + safeId);
  if (!nameEl || !numEl) return;
  var name = nameEl.value.trim(), number = numEl.value.trim();
  if (!name || !number) { _toast('请填写姓名和号码','error'); return; }
  try {
    await api('POST', '/devices/' + encodeURIComponent(deviceId) + '/contacts/add', {name: name, number: number});
    nameEl.value = ''; numEl.value = '';
    _toast('已添加: ' + name, 'success');
    _ttPanelCtLoad(deviceId, safeId);
  } catch(e) { _toast('添加失败','error'); }
}

async function _ttPanelCtBatchAll(deviceId, safeId) {
  var inp = document.createElement('input');
  inp.type = 'file';
  inp.accept = '.csv,.txt';
  inp.onchange = async function() {
    var file = inp.files[0];
    if (!file) return;
    var text = await file.text();
    var lines = text.split(/\r?\n/).filter(function(l){return l.trim();});
    if (lines.length < 2) { _toast('CSV 格式不正确','error'); return; }
    var allContacts = [];
    for (var i = 1; i < lines.length; i++) {
      var cols = lines[i].split(/[,;\t]/).map(function(c){return c.trim().replace(/^"|"$/g,'');});
      if (cols.length >= 2) allContacts.push({name: cols[0], number: cols[1]});
    }
    if (!allContacts.length) { _toast('CSV 中没有有效数据','error'); return; }

    var status = document.getElementById('ppnl-ct-status-' + safeId);
    if (status) status.innerHTML = '\u23F3 正在获取在线设备...';

    try {
      var devResp = await api('GET', '/devices');
      var devs = (devResp && devResp.devices) ? devResp.devices : (Array.isArray(devResp) ? devResp : []);
      var onlineDevs = devs.filter(function(d) { return d.status === 'online' || d.online; });
      if (!onlineDevs.length) { _toast('没有在线设备','error'); return; }

      var perDevice = Math.ceil(allContacts.length / onlineDevs.length);
      if (status) status.innerHTML = '\u{1F4F1} 分配中: ' + allContacts.length + ' 条 \u2192 ' + onlineDevs.length + ' 台设备 (每台~' + perDevice + '条)';

      var successDevs = 0;
      for (var d = 0; d < onlineDevs.length; d++) {
        var did = onlineDevs[d].device_id || onlineDevs[d].serial || onlineDevs[d].id;
        var chunk = allContacts.slice(d * perDevice, (d + 1) * perDevice);
        if (!chunk.length) continue;
        try {
          await api('POST', '/devices/' + encodeURIComponent(did) + '/contacts/batch', {contacts: chunk});
          successDevs++;
        } catch(e) { console.warn('batch to', did, e); }
      }
      if (status) status.innerHTML = '\u2705 分配完成: ' + allContacts.length + ' 条 \u2192 ' + successDevs + '/' + onlineDevs.length + ' 台设备';
      _toast('平均分配完成: ' + successDevs + ' 台设备', 'success');
      _ttPanelCtLoad(deviceId, safeId);
    } catch(e) {
      if (status) status.innerHTML = '\u274C 失败: ' + (e.message||e);
    }
  };
  inp.click();
}

async function _ttPanelCtDiscover(deviceId) {
  if (!(await ocDialog({title:'查找通讯录好友',message:'在 TikTok 中自动查找通讯录好友？',type:'info',confirmText:'开始',cancelText:'取消'}))) return;
  _toast('正在查找通讯录好友...', 'info');
  try {
    var r = await api('POST', '/tiktok/device/' + encodeURIComponent(deviceId) + '/find-contact-friends', {
      auto_follow: true, auto_message: true, max_friends: 20
    });
    _toast('找到 ' + (r.found||0) + ' 位好友, 关注 ' + (r.followed||0) + ', 消息 ' + (r.messaged||0), 'success');
  } catch(e) { _toast('好友发现失败: ' + (e.message||e), 'error'); }
}

/* ══════════════════════════════════════════════════════════════
   设备面板 — AI 画像分析
   ══════════════════════════════════════════════════════════════ */

async function _ttPanelAiProfile(deviceId) {
  var safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  var el = document.getElementById('ppnl-ai-' + safeId);
  if (!el) return;
  el.innerHTML = '<div style="color:var(--text-muted);font-size:11px;padding:6px 0">\u{1F9E0} 分析中...</div>';
  try {
    var leadsResp = await fetch('/tiktok/device/' + encodeURIComponent(deviceId) + '/leads?limit=5', {credentials:'include'});
    if (!leadsResp.ok) throw new Error('HTTP ' + leadsResp.status);
    var leadsData = await leadsResp.json();
    var leads = leadsData.leads || [];
    if (!leads.length) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:11px;text-align:center;padding:12px 0">\u{1F4AD} 暂无线索数据，请先运行引流任务</div>';
      return;
    }
    var html = '<div style="max-height:220px;overflow-y:auto">';
    for (var i = 0; i < leads.length; i++) {
      var lead = leads[i];
      try {
        var pResp = await api('POST', '/tiktok/chat/analyze-profile', {
          username: lead.username || lead.name || '',
          bio: lead.bio || '',
          followers: lead.followers || 0
        });
        var p = pResp;
        html += '<div style="background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:8px;padding:8px;margin-bottom:6px">' +
          '<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">' +
            '<span style="font-size:12px;font-weight:600;color:#e2e8f0">@' + (lead.username||lead.name||'?') + '</span>' +
            '<span style="font-size:9px;padding:2px 6px;border-radius:8px;background:rgba(99,102,241,.15);color:#818cf8">' + (p.account_type||'user') + '</span>' +
          '</div>' +
          (p.industry ? '<div style="font-size:10px;color:#94a3b8">\u{1F3E2} ' + p.industry + '</div>' : '') +
          (p.interests && p.interests.length ? '<div style="font-size:10px;color:#94a3b8">\u{2764}\uFE0F ' + p.interests.slice(0,3).join(', ') + '</div>' : '') +
          (p.personality ? '<div style="font-size:10px;color:#94a3b8">\u{1F3AD} ' + p.personality + '</div>' : '') +
          (p.suggested_topics && p.suggested_topics.length ? '<div style="font-size:10px;color:#60a5fa;margin-top:2px">\u{1F4AC} 建议话题: ' + p.suggested_topics.slice(0,2).join(', ') + '</div>' : '') +
        '</div>';
      } catch(e) {
        html += '<div style="font-size:10px;color:#ef4444">@' + (lead.username||'?') + ' 分析失败</div>';
      }
    }
    html += '</div>';
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div style="color:#ef4444;font-size:11px">加载线索失败: ' + (e.message||e) + '</div>';
  }
}

async function _ttPanelAiChat(deviceId) {
  var safeId = deviceId.replace(/[^a-zA-Z0-9]/g, '_');
  var el = document.getElementById('ppnl-ai-' + safeId);
  if (!el) return;
  el.innerHTML = '<div style="color:var(--text-muted);font-size:11px;padding:6px 0">\u{1F4AC} 加载对话数据...</div>';
  try {
    var r = await api('GET', '/tiktok/chat/active');
    var convs = r.conversations || [];
    if (!convs.length) {
      el.innerHTML = '<div style="color:var(--text-muted);font-size:11px;text-align:center;padding:12px 0">\u{1F4AD} 暂无活跃对话</div>';
      return;
    }
    var stageColors = {icebreak:'#60a5fa',rapport:'#818cf8',qualify:'#f59e0b',soft_pitch:'#f472b6',referral:'#22c55e',follow_up:'#94a3b8',cool_down:'#64748b'};
    var stageNames = {icebreak:'破冰',rapport:'建立关系',qualify:'资质评估',soft_pitch:'软推荐',referral:'引流',follow_up:'跟进',cool_down:'冷却'};
    var html = '<div style="max-height:200px;overflow-y:auto">';
    convs.slice(0, 8).forEach(function(c) {
      var sc = stageColors[c.stage] || '#94a3b8';
      var sn = stageNames[c.stage] || c.stage;
      html += '<div style="display:flex;align-items:center;gap:8px;padding:5px 0;border-bottom:1px solid rgba(255,255,255,.04)">' +
        '<span style="font-size:11px;font-weight:500;color:#e2e8f0;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">@' + (c.username||c.lead_id||'?') + '</span>' +
        '<span style="font-size:9px;padding:2px 6px;border-radius:8px;background:' + sc + '20;color:' + sc + ';white-space:nowrap">' + sn + '</span>' +
        '<span style="font-size:10px;color:var(--text-muted)">' + (c.message_count||0) + '\u8F6E</span>' +
      '</div>';
    });
    html += '</div>';
    el.innerHTML = html;
  } catch(e) {
    el.innerHTML = '<div style="color:#ef4444;font-size:11px">加载失败: ' + (e.message||e) + '</div>';
  }
}

/* ── Z2: Lead Mesh 摘要卡片 ── */
async function _loadLeadSummary(){
  var wrap=document.getElementById('ov-lead-summary');
  var grid=document.getElementById('ov-lead-cards');
  var trend=document.getElementById('ov-lead-trend');
  if(!wrap||!grid) return;
  try{
    var r=await api('GET','/lead-mesh/leads/summary');
    if(!r||!r.total){wrap.style.display='none';return;}
    wrap.style.display='block';
    var stages=r.stages||{};
    var defs=[
      {key:'new',label:'新建',color:'#64748b'},
      {key:'contacted',label:'触达',color:'#3b82f6'},
      {key:'engaged',label:'互动',color:'#8b5cf6'},
      {key:'qualified',label:'合格',color:'#22c55e'},
      {key:'converted',label:'转化',color:'#f59e0b'},
      {key:'lost',label:'流失',color:'#ef4444'},
    ];
    grid.innerHTML=defs.map(function(d){
      var n=stages[d.key]||0;
      return '<div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">'
        +'<div style="font-size:18px;font-weight:700;color:'+d.color+'">'+n+'</div>'
        +'<div style="font-size:10px;color:var(--text-muted);margin-top:2px">'+d.label+'</div></div>';
    }).join('');
    // 趋势 sparkline
    var t7=r.trend_7d||[];
    if(t7.length&&trend){
      var maxC=Math.max.apply(null,t7.map(function(x){return x.count;}))||1;
      trend.innerHTML='7天趋势: '+t7.map(function(x){
        var h=Math.max(2,Math.round(x.count/maxC*14));
        return '<span style="display:inline-block;width:16px;height:'+h+'px;background:#0ea5e9;border-radius:2px;vertical-align:bottom;margin:0 1px" title="'+x.date+': '+x.count+'"></span>';
      }).join('')+' <span style="color:var(--text-dim)">共 '+r.total+'</span>';
    }
  }catch(e){wrap.style.display='none';}
}
