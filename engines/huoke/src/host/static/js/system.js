/* system.js — 系统功能: 数据分析、ROI面板、性能监控、录屏管理、操作录制回放、备份恢复、插件管理、多屏并行控制、PWA、初始化 */
/* ── 数据分析页 ── */
let _chartTasks=null,_chartFunnel=null,_chartTypes=null;
async function loadAnalytics(){
  const days=parseInt(document.getElementById('ana-days')?.value||30);
  try{
    const [summary,funnel]=await Promise.all([
      api('GET',`/analytics/summary?days=${days}`),
      api('GET',`/funnel/daily?days=${days}`)
    ]);
    const sum=document.getElementById('ana-summary');
    sum.innerHTML=`
      <div class="stat-card blue"><div class="stat-num" style="color:#60a5fa">${summary.total}</div><div class="stat-label">总任务</div></div>
      <div class="stat-card green"><div class="stat-num" style="color:#4ade80">${summary.success}</div><div class="stat-label">成功</div></div>
      <div class="stat-card" style="border-top:3px solid #ef4444"><div class="stat-num" style="color:#f87171">${summary.failed}</div><div class="stat-label">失败</div></div>
      <div class="stat-card orange"><div class="stat-num" style="color:#fb923c">${summary.success_rate}%</div><div class="stat-label">成功率</div></div>`;
    const dailyKeys=Object.keys(summary.daily);
    const dailyTotal=dailyKeys.map(k=>summary.daily[k].total);
    const dailySuccess=dailyKeys.map(k=>summary.daily[k].success);
    const dailyFailed=dailyKeys.map(k=>summary.daily[k].failed);
    if(_chartTasks){_chartTasks.destroy();}
    const ctx1=document.getElementById('chart-tasks');
    if(ctx1&&typeof Chart!=='undefined'){
      _chartTasks=new Chart(ctx1,{type:'line',data:{labels:dailyKeys,datasets:[
        {label:'总数',data:dailyTotal,borderColor:'#60a5fa',backgroundColor:'rgba(96,165,250,.1)',fill:true,tension:.3},
        {label:'成功',data:dailySuccess,borderColor:'#4ade80',backgroundColor:'rgba(74,222,128,.1)',fill:true,tension:.3},
        {label:'失败',data:dailyFailed,borderColor:'#f87171',backgroundColor:'rgba(248,113,113,.1)',fill:true,tension:.3}
      ]},options:{responsive:true,plugins:{legend:{labels:{color:'#9ca3af',font:{size:10}}}},scales:{x:{ticks:{color:'#6b7280',font:{size:9}}},y:{ticks:{color:'#6b7280'},beginAtZero:true}}}});
    }
    const funnelData=funnel.daily||[];
    if(funnelData.length&&typeof Chart!=='undefined'){
      if(_chartFunnel){_chartFunnel.destroy();}
      const fLabels=funnelData.map(d=>d.date?.substring(5)||'');
      _chartFunnel=new Chart(document.getElementById('chart-funnel'),{type:'bar',data:{labels:fLabels,datasets:[
        {label:'发现',data:funnelData.map(d=>d.discovered||0),backgroundColor:'#60a5fa'},
        {label:'关注',data:funnelData.map(d=>d.followed||0),backgroundColor:'#a78bfa'},
        {label:'回关',data:funnelData.map(d=>d.follow_back||0),backgroundColor:'#4ade80'},
        {label:'私信',data:funnelData.map(d=>d.chatted||0),backgroundColor:'#fbbf24'}
      ]},options:{responsive:true,plugins:{legend:{labels:{color:'#9ca3af',font:{size:10}}}},scales:{x:{ticks:{color:'#6b7280',font:{size:9}},stacked:true},y:{ticks:{color:'#6b7280'},beginAtZero:true,stacked:true}}}});
    }
    const typeLabels=Object.keys(summary.type_counts);
    const typeData=Object.values(summary.type_counts);
    if(typeLabels.length&&typeof Chart!=='undefined'){
      if(_chartTypes){_chartTypes.destroy();}
      _chartTypes=new Chart(document.getElementById('chart-types'),{type:'doughnut',data:{labels:typeLabels.map(l=>TASK_NAMES[l]||l),datasets:[{data:typeData,backgroundColor:['#60a5fa','#4ade80','#a78bfa','#fb923c','#f87171','#22d3ee','#fbbf24','#e879f9']}]},options:{responsive:true,plugins:{legend:{labels:{color:'#9ca3af',font:{size:10}}}}}});
    }
    const rank=document.getElementById('ana-device-rank');
    rank.innerHTML=(summary.device_rank||[]).map((d,i)=>{
      const alias=ALIAS[d.device_id]||d.device_id?.substring(0,8);
      const barW=Math.max(5,d.rate);
      return `<div style="display:flex;align-items:center;gap:8px;padding:4px 0;border-bottom:1px solid rgba(255,255,255,.04)">
        <span style="width:18px;font-size:11px;color:var(--text-muted);text-align:right">${i+1}</span>
        <span style="width:60px;font-size:11px;font-weight:600">${alias}</span>
        <div style="flex:1;background:var(--bg-main);border-radius:4px;height:14px;overflow:hidden">
          <div style="height:100%;width:${barW}%;background:linear-gradient(90deg,#22c55e,#4ade80);border-radius:4px;transition:width .3s"></div>
        </div>
        <span style="width:40px;font-size:10px;color:var(--text-muted);text-align:right">${d.rate}%</span>
        <span style="width:50px;font-size:9px;color:var(--text-muted)">${d.success}/${d.total}</span>
      </div>`;
    }).join('')||'<div style="color:var(--text-muted);padding:12px;font-size:11px">暂无数据</div>';
  }catch(e){console.error('analytics',e);}
}

function exportCSV(){
  const days=parseInt(document.getElementById('ana-days')?.value||30);
  window.open(`/analytics/export?days=${days}`,'_blank');
}

/* ── ROI 面板 ── */
async function loadROIPage(){
  const el=document.getElementById('roi-content');
  el.innerHTML='<div style="text-align:center;padding:40px;color:var(--text-muted)">加载 ROI 数据...</div>';
  try{
    const d=await api('GET','/analytics/roi');
    const inv=d.investment||{};
    const out=d.output||{};
    const eff=d.efficiency||{};
    const hasROI=(inv.tasks_executed||0)>0;
    el.innerHTML=`
    <div style="margin-bottom:16px;display:flex;align-items:center;gap:12px">
      <h3 style="font-size:15px;font-weight:600;color:var(--text);margin:0">ROI 投入产出比</h3>
      <span style="font-size:11px;color:var(--text-muted);background:var(--bg-main);padding:2px 8px;border-radius:8px">近 ${d.period||'7d'}</span>
    </div>
    ${!hasROI?`<div style="background:linear-gradient(135deg,rgba(96,165,250,.1),rgba(168,85,247,.08));border:1px solid rgba(96,165,250,.25);border-radius:14px;padding:30px;text-align:center;margin-bottom:16px">
      <div style="font-size:32px;margin-bottom:10px">&#128200;</div>
      <div style="font-size:16px;font-weight:600;margin-bottom:6px">开始积累 ROI 数据</div>
      <div style="font-size:12px;color:var(--text-muted);margin-bottom:16px;max-width:400px;margin-left:auto;margin-right:auto">执行养号、关注、私信等任务后，系统将自动统计投入产出比。<br>数据越多，分析越精准。</div>
      <div style="display:flex;gap:8px;justify-content:center">
        <button class="qa-btn" onclick="navigateToPage('plat-tiktok')" style="padding:8px 18px;font-size:12px;background:linear-gradient(135deg,#ff0050,#ff4081);border:none;color:#fff;border-radius:6px;cursor:pointer">&#127916; 去 TikTok 开始</button>
        <button class="qa-btn" onclick="navigateToPage('overview')" style="padding:8px 18px;font-size:12px">&#9776; 总览</button>
      </div>
    </div>`:''}

    <!-- 顶部三大卡片 -->
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px">
      <div style="background:linear-gradient(135deg,rgba(96,165,250,.12),rgba(96,165,250,.04));border:1px solid rgba(96,165,250,.2);border-radius:14px;padding:18px">
        <div style="font-size:12px;color:#60a5fa;font-weight:600;margin-bottom:12px">&#128178; 投入</div>
        <div style="display:grid;gap:10px">
          <div><div style="font-size:22px;font-weight:700;color:#60a5fa">${inv.devices_active||0}</div><div style="font-size:10px;color:var(--text-muted)">活跃设备</div></div>
          <div><div style="font-size:22px;font-weight:700;color:#60a5fa">${inv.total_hours||0}<span style="font-size:12px;font-weight:400">h</span></div><div style="font-size:10px;color:var(--text-muted)">总运行时长</div></div>
          <div><div style="font-size:22px;font-weight:700;color:#60a5fa">${inv.tasks_executed||0}</div><div style="font-size:10px;color:var(--text-muted)">任务执行数</div></div>
        </div>
      </div>
      <div style="background:linear-gradient(135deg,rgba(74,222,128,.12),rgba(74,222,128,.04));border:1px solid rgba(74,222,128,.2);border-radius:14px;padding:18px">
        <div style="font-size:12px;color:#4ade80;font-weight:600;margin-bottom:12px">&#128200; 产出</div>
        <div style="display:grid;gap:10px">
          <div><div style="font-size:22px;font-weight:700;color:#4ade80">${out.follows_sent||0}</div><div style="font-size:10px;color:var(--text-muted)">发送关注</div></div>
          <div><div style="font-size:22px;font-weight:700;color:#4ade80">${out.followbacks||0}</div><div style="font-size:10px;color:var(--text-muted)">回关数</div></div>
          <div><div style="font-size:22px;font-weight:700;color:#4ade80">${out.dms_sent||0}</div><div style="font-size:10px;color:var(--text-muted)">私信发送</div></div>
          <div><div style="font-size:22px;font-weight:700;color:#4ade80">${out.leads_generated||0}</div><div style="font-size:10px;color:var(--text-muted)">生成线索</div></div>
        </div>
      </div>
      <div style="background:linear-gradient(135deg,rgba(251,191,36,.12),rgba(251,191,36,.04));border:1px solid rgba(251,191,36,.2);border-radius:14px;padding:18px">
        <div style="font-size:12px;color:#fbbf24;font-weight:600;margin-bottom:12px">&#9889; 效率</div>
        <div style="display:grid;gap:10px">
          <div><div style="font-size:22px;font-weight:700;color:#fbbf24">${eff.follows_per_device_per_day||0}</div><div style="font-size:10px;color:var(--text-muted)">关注/设备/天</div></div>
          <div><div style="font-size:22px;font-weight:700;color:#fbbf24">${eff.dms_per_device_per_day||0}</div><div style="font-size:10px;color:var(--text-muted)">私信/设备/天</div></div>
          <div><div style="font-size:22px;font-weight:700;color:#fbbf24">${eff.cost_per_lead_minutes||0}<span style="font-size:12px;font-weight:400">min</span></div><div style="font-size:10px;color:var(--text-muted)">每线索耗时</div></div>
        </div>
      </div>
    </div>

    <!-- 转化漏斗 -->
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:18px;margin-bottom:16px">
      <div style="font-size:13px;font-weight:600;color:var(--text);margin-bottom:14px">&#127891; 转化漏斗</div>
      <div style="display:flex;align-items:center;gap:0">
        ${_roiFunnelStep('关注', out.follows_sent||0, '#a78bfa', true)}
        ${_roiFunnelArrow()}
        ${_roiFunnelStep('回关', out.followbacks||0, '#4ade80', false)}
        ${_roiFunnelArrow()}
        ${_roiFunnelStep('私信', out.dms_sent||0, '#60a5fa', false)}
        ${_roiFunnelArrow()}
        ${_roiFunnelStep('线索', out.leads_generated||0, '#fbbf24', false)}
      </div>
      <div style="margin-top:12px;display:flex;gap:20px;justify-content:center">
        <span style="font-size:11px;color:var(--text-muted)">回关率 <b style="color:#4ade80">${out.followback_rate||0}%</b></span>
        <span style="font-size:11px;color:var(--text-muted)">私信转化 <b style="color:#60a5fa">${out.follows_sent? (out.dms_sent/out.follows_sent*100).toFixed(1) : 0}%</b></span>
        <span style="font-size:11px;color:var(--text-muted)">线索转化 <b style="color:#fbbf24">${out.follows_sent? (out.leads_generated/out.follows_sent*100).toFixed(1) : 0}%</b></span>
      </div>
    </div>

    <!-- 效率指标底部 -->
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">
        <div style="font-size:20px;font-weight:700;color:#a78bfa">${inv.devices_active||0}</div>
        <div style="font-size:10px;color:var(--text-muted);margin-top:4px">活跃设备</div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">
        <div style="font-size:20px;font-weight:700;color:#60a5fa">${inv.tasks_executed? (inv.tasks_executed/7).toFixed(1) : 0}</div>
        <div style="font-size:10px;color:var(--text-muted);margin-top:4px">日均任务</div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">
        <div style="font-size:20px;font-weight:700;color:#4ade80">${out.followback_rate||0}%</div>
        <div style="font-size:10px;color:var(--text-muted);margin-top:4px">回关率</div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center">
        <div style="font-size:20px;font-weight:700;color:#fbbf24">${eff.cost_per_lead_minutes||0}<span style="font-size:11px;font-weight:400">m</span></div>
        <div style="font-size:10px;color:var(--text-muted);margin-top:4px">每线索耗时</div>
      </div>
    </div>`;
  }catch(e){
    el.innerHTML=`<div style="text-align:center;padding:40px;color:#f87171">加载失败: ${e.message}</div>`;
    console.error('roi',e);
  }
}
function _roiFunnelStep(label,value,color,first){
  return `<div style="flex:1;text-align:center;padding:12px 8px;background:linear-gradient(135deg,${color}22,${color}08);border-radius:10px;${first?'':'margin-left:-1px'}">
    <div style="font-size:20px;font-weight:700;color:${color}">${value}</div>
    <div style="font-size:10px;color:var(--text-muted);margin-top:2px">${label}</div>
  </div>`;
}
function _roiFunnelArrow(){
  return `<div style="color:var(--text-muted);font-size:16px;padding:0 4px">&#10132;</div>`;
}


/* ── 性能监控 ── */
let _perfAutoTimer=null;
async function loadPerfMonitor(){
  try{
    const r=await api('GET','/devices/performance/all');
    const devs=r.devices||{};
    const entries=Object.entries(devs);
    let sumCpu=0,sumMem=0,sumBat=0,sumStor=0,cntCpu=0,cntMem=0,cntBat=0,cntStor=0;
    let html='';
    entries.forEach(([did,d])=>{
      const alias=ALIAS[did]||did.substring(0,8);
      if(d.cpu_usage!==undefined){sumCpu+=d.cpu_usage;cntCpu++;}
      if(d.mem_usage!==undefined){sumMem+=d.mem_usage;cntMem++;}
      if(d.battery_level!==undefined){sumBat+=d.battery_level;cntBat++;}
      if(d.storage_usage!==undefined){sumStor+=d.storage_usage;cntStor++;}
      const batColor=d.battery_level>50?'var(--green)':d.battery_level>20?'#eab308':'var(--red)';
      html+=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-size:13px;font-weight:600">${alias}</span>
          <span style="font-size:10px;color:var(--text-muted)">${did.substring(0,12)}</span>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:6px">
          ${_perfBar('内存',d.mem_usage,'var(--accent)')}
          ${_perfBar('电量',d.battery_level,batColor)}
          ${_perfBar('存储',d.storage_usage,'#a78bfa')}
          <div style="font-size:11px;color:var(--text-dim)">
            ${d.battery_temp?'温度: '+d.battery_temp+'°C':''}
            ${d.charging?'⚡充电中':''}
            ${d.mem_total_mb?' | 总内存: '+(d.mem_total_mb/1024).toFixed(1)+'GB':''}
          </div>
        </div>
      </div>`;
    });
    document.getElementById('perf-avg-cpu').textContent=cntCpu?Math.round(sumCpu/cntCpu)+'%':'-';
    document.getElementById('perf-avg-mem').textContent=cntMem?Math.round(sumMem/cntMem)+'%':'-';
    document.getElementById('perf-avg-bat').textContent=cntBat?Math.round(sumBat/cntBat)+'%':'-';
    document.getElementById('perf-avg-storage').textContent=cntStor?Math.round(sumStor/cntStor)+'%':'-';
    document.getElementById('perf-cards').innerHTML=html||'<div style="color:var(--text-dim)">无在线设备</div>';
  }catch(e){showToast('加载性能数据失败','warn');}
}
function _perfBar(label,val,color){
  const v=val||0;
  return `<div style="font-size:11px">
    <div style="display:flex;justify-content:space-between"><span>${label}</span><span>${v}%</span></div>
    <div style="background:var(--bg-main);border-radius:4px;height:6px;margin-top:2px;overflow:hidden">
      <div style="width:${v}%;height:100%;background:${color};border-radius:4px;transition:width .3s"></div>
    </div>
  </div>`;
}
function togglePerfAutoRefresh(){
  if(document.getElementById('perf-auto-refresh')?.checked){
    _perfAutoTimer=setInterval(loadPerfMonitor,8000);
  }else{
    clearInterval(_perfAutoTimer);_perfAutoTimer=null;
  }
}

/* ── 录屏管理 ── */
async function loadScreenRecordPage(){
  const sel=document.getElementById('rec-device');
  sel.innerHTML='';
  allDevices.filter(d=>d.is_online).forEach(d=>{
    const alias=ALIAS[d.device_id]||d.display_name||d.device_id.substring(0,8);
    sel.innerHTML+=`<option value="${d.device_id}">${alias}</option>`;
  });
  try{
    const r=await api('GET','/recordings');
    document.getElementById('rec-list').innerHTML=(r.recordings||[]).map(f=>
      `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:10px;display:flex;justify-content:space-between;align-items:center">
        <div>
          <div style="font-size:12px;font-weight:600">${f.name}</div>
          <div style="font-size:10px;color:var(--text-dim)">${f.size_mb} MB</div>
        </div>
        <a href="/recordings/${f.name}" download style="font-size:11px;color:var(--accent);text-decoration:none">下载</a>
      </div>`
    ).join('')||'<div style="color:var(--text-dim);font-size:12px">暂无录屏</div>';
  }catch(e){}
}
async function startRecording(){
  const did=document.getElementById('rec-device').value;
  const dur=parseInt(document.getElementById('rec-duration').value)||30;
  if(!did){showToast('请选择设备','warn');return;}
  try{
    const r=await api('POST',`/devices/${did}/record/start`,{duration:dur});
    showToast(r.ok?`开始录屏 ${dur}秒`:'录屏失败: '+(r.error||''));
  }catch(e){showToast('录屏失败','warn');}
}
async function stopRecording(){
  const did=document.getElementById('rec-device').value;
  if(!did) return;
  try{
    const r=await api('POST',`/devices/${did}/record/stop`);
    showToast(r.ok?`已保存: ${r.file}`:'停止失败');
    loadScreenRecordPage();
  }catch(e){showToast('停止失败','warn');}
}

/* ── 操作录制与回放 ── */
let _opRecSession=null;
async function loadOpReplayPage(){
  const devSel=document.getElementById('oprec-device');
  const repSel=document.getElementById('oprep-device');
  devSel.innerHTML='';repSel.innerHTML='<option value="">使用原设备</option>';
  allDevices.forEach(d=>{
    const alias=ALIAS[d.device_id]||d.device_id.substring(0,8);
    devSel.innerHTML+=`<option value="${d.device_id}">${alias}</option>`;
    repSel.innerHTML+=`<option value="${d.device_id}">${alias}</option>`;
  });
  try{
    const r=await api('GET','/recording/list');
    const list=document.getElementById('oprec-list');
    list.innerHTML=(r.recordings||[]).map(rec=>{
      const alias=ALIAS[rec.device_id]||rec.device_id?.substring(0,8)||'?';
      const dur=rec.duration?Math.round(rec.duration)+'s':'?';
      return `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px;background:var(--bg-main);border:1px solid var(--border);border-radius:8px">
        <div>
          <div style="font-size:12px;font-weight:600">${rec.name||rec.filename}</div>
          <div style="font-size:10px;color:var(--text-muted)">${alias} · ${rec.events} 步 · ${dur}</div>
        </div>
        <div style="display:flex;gap:4px">
          <button class="dev-btn" onclick="replayRecording('${rec.filename}')" style="font-size:10px;padding:4px 10px;background:var(--green);color:#111">&#9654; 回放</button>
          <button class="sb-btn2" onclick="deleteRecording('${rec.filename}')" style="font-size:10px;color:var(--red)">删除</button>
        </div>
      </div>`;
    }).join('')||'<div style="color:var(--text-muted);font-size:12px">暂无录制文件。在屏幕监控中操作设备时，点击"开始录制"记录操作。</div>';
  }catch(e){}
}
async function startOpRecording(){
  const did=document.getElementById('oprec-device').value;
  const name=document.getElementById('oprec-name').value||undefined;
  if(!did){showToast('请选择设备','warn');return;}
  try{
    const r=await api('POST','/recording/start',{device_id:did,name});
    _opRecSession=r.session_id;
    document.getElementById('oprec-start-btn').style.display='none';
    document.getElementById('oprec-stop-btn').style.display='inline-block';
    document.getElementById('oprec-status').innerHTML='<span style="color:var(--red)">&#9679; 录制中... 现在操作设备屏幕，所有点击/滑动都会被记录</span>';
    showToast('录制已开始');
  }catch(e){showToast('启动失败','warn');}
}
async function stopOpRecording(){
  if(!_opRecSession){showToast('无活动录制','warn');return;}
  try{
    const r=await api('POST','/recording/stop',{session_id:_opRecSession});
    document.getElementById('oprec-start-btn').style.display='inline-block';
    document.getElementById('oprec-stop-btn').style.display='none';
    document.getElementById('oprec-status').innerHTML=`<span style="color:var(--green)">录制完成: ${r.events} 步, ${r.duration}s</span>`;
    _opRecSession=null;
    loadOpReplayPage();
  }catch(e){showToast('停止失败','warn');}
}
async function replayRecording(filename){
  const targetDev=document.getElementById('oprep-device').value;
  const speed=parseFloat(document.getElementById('oprep-speed').value)||1;
  try{
    const r=await api('POST','/recording/replay',{filename,target_device_id:targetDev||undefined,speed});
    showToast(`正在回放 ${r.replaying} 步操作 (${speed}x 速度)`);
  }catch(e){showToast('回放失败: '+e.message,'warn');}
}
async function deleteRecording(filename){
  if(!(await ocDialog({title:'删除录制',message:'确认删除此录制？',type:'danger',confirmText:'删除',dangerous:true})))return;
  try{await api('DELETE','/recording/'+filename);loadOpReplayPage();}catch(e){showToast('删除失败','warn');}
}


/* ── Backup & Restore ── */
async function loadBackupPage(){
  try{
    const files=await api('GET','/system/config-list');
    const list=document.getElementById('config-file-list');
    if(list)list.innerHTML=files.map(f=>`<div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid var(--border)">
      <span><code>${f.path}</code></span>
      <span style="color:var(--text-muted)">${(f.size/1024).toFixed(1)} KB | ${f.modified}</span>
    </div>`).join('')||'<span style="color:var(--text-muted)">无配置文件</span>';
  }catch(e){}
}
function exportBackup(){
  window.open('/system/export-config','_blank');
}
async function importBackup(){
  const file=document.getElementById('backup-file').files[0];
  if(!file)return;
  const result=document.getElementById('backup-result');
  result.innerHTML='<span style="color:var(--text-muted)">上传中...</span>';
  const reader=new FileReader();
  reader.onload=async function(){
    const b64=reader.result.split(',')[1];
    try{
      const r=await api('POST','/system/import-config',{data:b64});
      result.innerHTML=`<span style="color:#22c55e">恢复成功! 已恢复 ${(r.restored||[]).length} 个文件</span>`;
      if(r.skipped?.length)result.innerHTML+=`<br><span style="color:var(--text-muted)">跳过: ${r.skipped.join(', ')}</span>`;
    }catch(e){result.innerHTML=`<span style="color:#ef4444">恢复失败: ${e.message}</span>`;}
  };
  reader.readAsDataURL(file);
}

/* ── Plugin Management ── */
async function loadPluginsPage(){
  const list=document.getElementById('plugins-list');
  if(!list)return;list.innerHTML='<div style="color:var(--text-muted)">加载中…</div>';
  try{
    const plugins=await api('GET','/plugins');
    list.innerHTML=plugins.map(p=>{
      const statusColor=p.error&&p.error!=='未加载'?'#ef4444':p.enabled?'#22c55e':'#94a3b8';
      const statusText=p.error&&p.error!=='未加载'?'错误':p.enabled?'已启用':'未启用';
      return `<div style="display:flex;align-items:center;justify-content:space-between;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px 18px">
        <div>
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-weight:600;font-size:13px">${p.name}</span>
            <span style="font-size:10px;color:var(--text-muted)">v${p.version}</span>
            <span style="font-size:9px;padding:2px 8px;border-radius:6px;background:${statusColor};color:#fff">${statusText}</span>
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:2px">${p.description||'无描述'}</div>
          ${p.author?`<div style="font-size:10px;color:var(--text-muted);margin-top:1px">作者: ${p.author}</div>`:''}
          ${p.hooks&&p.hooks.length?`<div style="font-size:9px;color:var(--text-muted);margin-top:2px">钩子: ${p.hooks.join(', ')}</div>`:''}
          ${p.error&&p.error!=='未加载'?`<div style="font-size:10px;color:#f87171;margin-top:2px">错误: ${p.error}</div>`:''}
        </div>
        <div style="display:flex;gap:6px">
          ${!p.loaded_at?`<button class="dev-btn" onclick="pluginAction('${p.name}','load')" style="font-size:10px;padding:4px 10px">加载</button>`:''}
          ${p.loaded_at&&!p.enabled?`<button class="dev-btn" onclick="pluginAction('${p.name}','enable')" style="font-size:10px;padding:4px 10px;color:#22c55e;border-color:#22c55e">启用</button>`:''}
          ${p.enabled?`<button class="sb-btn2" onclick="pluginAction('${p.name}','disable')" style="font-size:10px">禁用</button>`:''}
          ${p.loaded_at?`<button class="sb-btn2" onclick="pluginAction('${p.name}','reload')" style="font-size:10px;color:#eab308">热重载</button>`:''}
          <button class="sb-btn2" onclick="pluginAction('${p.name}','unload')" style="font-size:10px;color:#f87171">卸载</button>
        </div>
      </div>`;
    }).join('')||'<div style="color:var(--text-muted);text-align:center;padding:20px">未发现插件。将 .py 文件放入 plugins/ 目录。</div>';
  }catch(e){list.innerHTML='<div style="color:#f87171">加载失败: '+e.message+'</div>';}
}

async function pluginAction(name,action){
  try{
    if(action==='load')await api('POST',`/plugins/${name}/load`);
    else if(action==='enable')await api('POST',`/plugins/${name}/enable`);
    else if(action==='disable')await api('POST',`/plugins/${name}/disable`);
    else if(action==='unload')await api('DELETE',`/plugins/${name}`);
    else if(action==='reload')await api('POST',`/plugins/${name}/reload`);
    loadPluginsPage();
  }catch(e){showToast('操作失败: '+e.message,'error');}
}
async function reloadAllPlugins(){
  try{await api('POST','/plugins/reload-all');loadPluginsPage();}catch(e){showToast('失败: '+e.message,'error');}
}

/* ── Multi-Screen Parallel Control ── */
let _msPanels=[];let _msLayout=3;let _msRefreshTimers={};let _msStreamMode='mjpeg';

function loadMultiScreen(){
  const container=document.getElementById('ms-panels');
  if(!container)return;
  if(!_msPanels.length){
    const online=allDevices.filter(d=>d.status==='connected'||d.status==='online');
    _msPanels=online.slice(0,Math.min(6,online.length)).map(d=>d.device_id);
  }
  _renderMsPanels();
}

function setMsLayout(cols){
  _msLayout=cols;
  const container=document.getElementById('ms-panels');
  if(container)container.style.gridTemplateColumns=`repeat(${cols},1fr)`;
}

function addMsPanel(){
  const online=allDevices.filter(d=>d.status==='connected'||d.status==='online').filter(d=>!_msPanels.includes(d.device_id));
  if(!online.length){showToast('没有更多在线设备','warn');return;}
  const did=online[0].device_id;
  _msPanels.push(did);
  _renderMsPanels();
}

function removeMsPanel(did){
  _msPanels=_msPanels.filter(d=>d!==did);
  if(_msRefreshTimers[did]){clearInterval(_msRefreshTimers[did]);delete _msRefreshTimers[did];}
  _renderMsPanels();
}

function _renderMsPanels(){
  const container=document.getElementById('ms-panels');
  if(!container)return;
  container.style.gridTemplateColumns=`repeat(${_msLayout},1fr)`;
  const online=allDevices.filter(d=>d.status==='connected'||d.status==='online');
  container.innerHTML=_msPanels.map(did=>{
    const dev=allDevices.find(d=>d.device_id===did);
    const alias=ALIAS[did]||did.substring(0,8);
    const isOn=dev&&(dev.status==='connected'||dev.status==='online');
    return `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;overflow:hidden;display:flex;flex-direction:column">
      <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 10px;border-bottom:1px solid var(--border);font-size:11px">
        <div>
          <select onchange="_msSwitchDevice('${did}',this.value)" style="background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:4px;font-size:10px;padding:2px">
            ${online.map(d=>`<option value="${d.device_id}" ${d.device_id===did?'selected':''}>${ALIAS[d.device_id]||d.device_id.substring(0,8)}</option>`).join('')}
          </select>
          <span class="status-dot ${isOn?'ok':'err'}" style="width:6px;height:6px;display:inline-block;margin-left:4px"></span>
        </div>
        <div style="display:flex;gap:3px">
          <button class="sb-btn2" onclick="_msAction('${did}','home')" style="font-size:9px;padding:2px 4px">H</button>
          <button class="sb-btn2" onclick="_msAction('${did}','back')" style="font-size:9px;padding:2px 4px">B</button>
          <button class="sb-btn2" onclick="_msAction('${did}','recents')" style="font-size:9px;padding:2px 4px">R</button>
          <button class="sb-btn2" onclick="openScreenModal('${did}')" style="font-size:9px;padding:2px 4px">&#9881;</button>
          <button class="sb-btn2" onclick="removeMsPanel('${did}')" style="font-size:9px;padding:2px 4px;color:#f87171">&times;</button>
        </div>
      </div>
      <div style="flex:1;position:relative;min-height:200px;background:#000;cursor:crosshair" id="ms-screen-${did}" onclick="_msTap(event,'${did}')" oncontextmenu="event.preventDefault()">
        <img id="ms-img-${did}" style="width:100%;height:100%;object-fit:contain" alt=""/>
        <div style="position:absolute;bottom:4px;left:4px;font-size:9px;color:rgba(255,255,255,.5)">${alias}</div>
      </div>
      <div style="display:flex;gap:2px;padding:4px;border-top:1px solid var(--border)">
        <input id="ms-text-${did}" placeholder="输入文本..." style="flex:1;padding:3px 6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:4px;font-size:10px"/>
        <button class="sb-btn2" onclick="_msText('${did}')" style="font-size:9px;padding:2px 6px">发送</button>
      </div>
    </div>`;
  }).join('');
  _msPanels.forEach(did=>_msRefreshScreen(did));
}

function _msRefreshScreen(did){
  if(_msRefreshTimers[did])clearInterval(_msRefreshTimers[did]);
  const img=document.getElementById('ms-img-'+did);
  if(!img)return;
  if(_msStreamMode==='mjpeg'){
    img.src=_apiUrl('/devices/'+did+'/mjpeg?fps=4&quality=45&max_h=400');
  }else{
    const refresh=()=>{
      const el=document.getElementById('ms-img-'+did);
      if(!el)return;
      el.src=_apiUrl('/devices/'+did+'/screenshot?t='+Date.now());
    };
    refresh();
    _msRefreshTimers[did]=setInterval(refresh,3000);
  }
}

function refreshAllMsScreens(){_msPanels.forEach(did=>_msRefreshScreen(did));}
function toggleMsStreamMode(){
  _msStreamMode=_msStreamMode==='mjpeg'?'poll':'mjpeg';
  _renderMsPanels();
}

function _msSwitchDevice(oldDid,newDid){
  const idx=_msPanels.indexOf(oldDid);
  if(idx>=0)_msPanels[idx]=newDid;
  if(_msRefreshTimers[oldDid]){clearInterval(_msRefreshTimers[oldDid]);delete _msRefreshTimers[oldDid];}
  _renderMsPanels();
}

function _msTap(event,did){
  const rect=event.target.getBoundingClientRect();
  const xPct=((event.clientX-rect.left)/rect.width*100).toFixed(1);
  const yPct=((event.clientY-rect.top)/rect.height*100).toFixed(1);
  api('POST','/sync/touch',{action:'tap',x:parseFloat(xPct),y:parseFloat(yPct),device_ids:[did]}).catch(()=>{});
  setTimeout(()=>_msRefreshScreen(did),500);
}

async function _msAction(did,action){
  const keys={home:'KEYCODE_HOME',back:'KEYCODE_BACK',recents:'KEYCODE_APP_SWITCH'};
  try{await api('POST','/sync/key',{keycode:keys[action]||action,device_ids:[did]});}catch(e){}
  setTimeout(()=>_msRefreshScreen(did),500);
}

async function _msText(did){
  const inp=document.getElementById('ms-text-'+did);
  if(!inp||!inp.value.trim())return;
  try{await api('POST','/sync/text',{text:inp.value,device_ids:[did]});inp.value='';}catch(e){}
  setTimeout(()=>_msRefreshScreen(did),500);
}


/* ── PWA Service Worker ── */
if('serviceWorker' in navigator){
  navigator.serviceWorker.register('/sw.js',{scope:'/'}).catch(()=>{});
}

/* ── Init ── */
function loadAll(){loadHealth();loadDevices();loadTasks();_loadOverviewBriefing();setTimeout(loadAllCharts,500);}
loadAll();loadLogs();connectUnifiedWs();
setInterval(()=>{loadTrendChart();loadTaskChart();},120000);
