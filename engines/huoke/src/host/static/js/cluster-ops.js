/* cluster-ops.js — 集群管理: OTA更新、集群管理 */
/* ── OTA 一键更新 ── */
async function _otaPushAll(){
  if(!(await ocDialog({title:'OTA 更新',message:'将向所有 Worker 推送最新代码并要求重启。',type:'info',confirmText:'推送更新',cancelText:'取消'}))) return;
  showToast('正在推送更新到所有 Worker...');
  try{
    // 通过主控 API 中转推送（避免浏览器 CORS 限制）
    const r=await api('POST','/cluster/push-update-all');
    if(r.results){
      for(const w of r.results){
        if(w.ok) showToast(`${w.host}: 更新成功 (${w.updated_files} 文件)`,'success');
        else showToast(`${w.host}: ${w.error||'失败'}`,'warn');
      }
    }
    showToast(r.summary||'更新完成');
  }catch(e){showToast('更新失败: '+e.message,'warn');}
}

function _clusterExecPolicyLine(pn,hOnline){
  if(!pn||pn.role==='coordinator') return '';
  if(!hOnline) return '';
  if(pn.error) return `<div style="font-size:10px;color:var(--yellow);margin-top:4px">执行策略: 拉取失败 — ${String(pn.error).slice(0,120)}</div>`;
  const p=pn.policy;
  if(!p) return '';
  const bits=[];
  if(p.manual_execution_only) bits.push('仅手动');
  if(p.disable_db_scheduler) bits.push('DB定时关');
  if(p.disable_json_scheduled_jobs) bits.push('JSON定时关');
  if(p.disable_reconnect_task_recovery) bits.push('掉线恢复关');
  if(p.disable_auto_tiktok_check_inbox) bits.push('无人收件箱关');
  if(!bits.length) return `<div style="font-size:10px;color:var(--text-muted);margin-top:4px">执行策略: 未启用上述限制（与总览「本机任务策略」同源）</div>`;
  return `<div style="font-size:10px;color:var(--text-muted);margin-top:4px">执行策略: ${bits.join(' · ')}</div>`;
}

/* ── Cluster ── */
async function loadClusterPage(){
  loadClusterConfig();
  _loadClusterScripts();
  _loadRangesSection();
  try{
    const [overview,devList,execPol]=await Promise.all([
      api('GET','/cluster/overview'),
      api('GET','/cluster/devices'),
      api('GET','/cluster/execution-policies').catch(()=>({nodes:[]})),
    ]);
    const polByHost={};
    (execPol.nodes||[]).forEach(n=>{ if(n&&n.host_id) polByHost[n.host_id]=n; });
    const hosts=overview.hosts||[];
    document.getElementById('cl-stats-row').innerHTML=`
      <div class="stat-card"><div class="stat-value">${overview.total_hosts||0}</div><div class="stat-label">主机总数</div></div>
      <div class="stat-card"><div class="stat-value" style="color:var(--green)">${overview.hosts_online||0}</div><div class="stat-label">在线主机</div></div>
      <div class="stat-card"><div class="stat-value">${overview.total_devices||0}</div><div class="stat-label">总设备</div></div>
      <div class="stat-card"><div class="stat-value">${overview.total_devices_online||0}</div><div class="stat-label">在线设备</div></div>
      <div class="stat-card"><div class="stat-value">${overview.total_tasks_active||0}</div><div class="stat-label">活动任务</div></div>`;
    if(hosts.length){
      document.getElementById('cl-hosts').innerHTML=hosts.map(h=>{
        const dotCls=h.online?'ok':'err';
        const stLabel=h.online?'在线':'离线';
        const stColor=h.online?'var(--green)':'var(--red)';
        const cpuCls=h.cpu_usage>80?'color:var(--red)':h.cpu_usage>50?'color:var(--yellow)':'color:var(--green)';
        const memCls=h.memory_usage>80?'color:var(--red)':h.memory_usage>50?'color:var(--yellow)':'color:var(--green)';
        const ago=h.last_heartbeat?Math.round((Date.now()/1000-h.last_heartbeat))+'s 前':'—';
        const hbWarn=h.last_heartbeat&&(Date.now()/1000-h.last_heartbeat)>60;
        const wkColor=_getWorkerColor(h.host_name||h.host_id.substring(0,8));
        return `<div style="background:var(--bg-card);border:1px solid var(--border);border-left:3px solid ${wkColor};border-radius:12px;padding:14px">
          <!-- 标题行 -->
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
            <div style="display:flex;align-items:center;gap:6px">
              <span class="status-dot ${dotCls}" style="width:8px;height:8px"></span>
              <b style="font-size:14px">${h.host_name||h.host_id.substring(0,8)}</b>
              <span style="font-size:10px;padding:1px 6px;border-radius:3px;background:${h.online?'rgba(34,197,94,.12)':'rgba(239,68,68,.12)'};color:${stColor}">${stLabel}</span>
            </div>
            <div style="display:flex;gap:4px">
              ${h.online?`<button class="dev-btn" style="font-size:10px;padding:2px 8px;color:#60a5fa;border-color:#60a5fa" onclick="restartWorker('${h.host_id}','${h.host_name||h.host_id.substring(0,8)}')">🔄 重启</button>`:''}
              <button class="dev-btn" style="font-size:10px;padding:2px 8px;color:#f59e0b;border-color:#f59e0b" onclick="updateWorker('${h.host_id}','${h.host_name||h.host_id.substring(0,8)}')">⬆ 更新</button>
              <button class="dev-btn" style="font-size:10px;padding:2px 8px;color:var(--red)" onclick="removeHost('${h.host_id}')">✕</button>
            </div>
          </div>
          <!-- IP & 版本 -->
          <div style="font-size:11px;color:var(--text-muted);margin-bottom:8px;display:flex;align-items:center;gap:8px;flex-wrap:wrap">
            <span>🌐 <a href="http://${h.host_ip}:${h.port}" target="_blank" style="color:#60a5fa;text-decoration:none">${h.host_ip}:${h.port}</a></span>
            ${(()=>{const pn=polByHost[h.host_id];const r=pn&&pn.reachable_ip;const hip=(h.host_ip||'').trim();return r&&hip&&r!==hip?'<span style="font-size:10px;color:var(--accent)" title="拉取执行策略时实际连通的 IP（与心跳上报可能不同）">探测 '+r+'</span>':'';})()}
            <span>v${h.version||'?'}</span>
            ${hbWarn?'<span style="color:var(--yellow)">⚠ 心跳延迟</span>':''}
          </div>
          ${_clusterExecPolicyLine(polByHost[h.host_id],h.online)}
          <!-- 资源指标 -->
          <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px;font-size:11px;margin-bottom:8px">
            <div style="background:var(--bg-main);border-radius:6px;padding:6px;text-align:center">
              <div style="color:var(--text-muted);font-size:9px;margin-bottom:2px">设备</div>
              <b style="font-size:14px">${h.devices_online||0}</b><span style="color:var(--text-muted)">/${h.devices||0}</span>
            </div>
            <div style="background:var(--bg-main);border-radius:6px;padding:6px;text-align:center">
              <div style="color:var(--text-muted);font-size:9px;margin-bottom:2px">CPU</div>
              <b style="font-size:14px;${cpuCls}">${h.cpu_usage||0}%</b>
            </div>
            <div style="background:var(--bg-main);border-radius:6px;padding:6px;text-align:center">
              <div style="color:var(--text-muted);font-size:9px;margin-bottom:2px">内存</div>
              <b style="font-size:14px;${memCls}">${h.memory_usage||0}%</b>
            </div>
          </div>
          <!-- 任务 & 心跳 -->
          <div style="font-size:10px;color:var(--text-muted);display:flex;justify-content:space-between">
            <span>任务: <b style="color:var(--text)">${h.tasks_active||0}</b> 运行 / ${h.tasks_completed||0} 完成</span>
            <span>心跳: ${ago}</span>
          </div>
        </div>`;
      }).join('');
    }else{
      document.getElementById('cl-hosts').innerHTML='<div style="text-align:center;padding:30px;color:var(--text-muted);grid-column:1/-1">暂无主机注册。本机自动作为协调器，其他PC通过"加入集群"连接。</div>';
    }
    const devices=devList.devices||[];
    const sel=document.getElementById('cl-device-select');
    sel.innerHTML='<option value="">自动选择最优设备</option>'+devices.map(d=>{
      const alias=d.display_name||d.device_id.substring(0,8);
      const host=d.host_name||d.host_id?.substring(0,6)||'';
      return `<option value="${d.device_id}">${alias} (${host})</option>`;
    }).join('');
    if(devices.length){
      document.getElementById('cl-devices').innerHTML='<table style="width:100%;font-size:12px"><thead><tr><th>设备</th><th>主机</th><th>状态</th><th>健康</th></tr></thead><tbody>'+devices.map(d=>{
        const alias=d.display_name||d.device_id.substring(0,8);
        const dot=d.status==='connected'?'ok':'err';
        const hs=d.health_score!==undefined?d.health_score:'—';
        return `<tr>
          <td><b>${alias}</b></td>
          <td style="color:var(--text-muted)">${d.host_name||'—'}</td>
          <td><span class="status-dot ${dot}" style="width:6px;height:6px;display:inline-block;vertical-align:middle"></span> ${d.status}</td>
          <td>${hs}</td>
        </tr>`;
      }).join('')+'</tbody></table>';
    }else{
      document.getElementById('cl-devices').innerHTML='<div style="text-align:center;padding:20px;color:var(--text-muted)">暂无跨主机设备</div>';
    }
  }catch(e){console.error('loadClusterPage error:',e);}
}
function showJoinCluster(){document.getElementById('cl-join-modal').style.display='flex';}
async function joinCluster(){
  const url=document.getElementById('cl-coord-url').value.trim();
  if(!url){showToast('请输入中心节点地址');return;}
  try{
    await api('POST','/cluster/join',{coordinator_url:url});
    showToast('已加入集群: '+url);
    document.getElementById('cl-join-modal').style.display='none';
    setTimeout(loadClusterPage,2000);
  }catch(e){showToast('加入失败: '+e.message);}
}
/* ── Worker 重启 ── */
async function restartWorker(hostId, hostName){
  if(!(await ocDialog({title:'重启 Worker',message:`确定要重启 <b>${hostName||hostId.substring(0,8)}</b> 吗？<br>重启期间该机器上的任务将中断，约 10-30 秒后自动恢复。`,type:'warning',confirmText:'重启',cancelText:'取消'}))) return;
  const btn=event.target;
  btn.disabled=true; btn.textContent='重启中...';
  try{
    const r=await api('POST','/cluster/restart-worker/'+hostId);
    showToast(`${hostName}: ${r.message||'重启指令已发送'}，请等待10-30秒`,'success');
    // 10秒后刷新集群状态
    setTimeout(()=>{loadClusterPage();showToast(`${hostName} 状态已刷新`);},12000);
  }catch(e){
    showToast(`重启失败: ${e.message}`,'error');
    btn.disabled=false; btn.textContent='🔄 重启';
  }
}

/* ── 单台 Worker 更新 ── */
async function updateWorker(hostId, hostName){
  if(!(await ocDialog({title:'更新 Worker',message:`向 <b>${hostName||hostId.substring(0,8)}</b> 推送最新代码并重启？`,type:'info',confirmText:'更新',cancelText:'取消'}))) return;
  showToast(`正在推送更新到 ${hostName}...`);
  try{
    const r=await api('POST','/cluster/push-update-all');
    const w=(r.results||[]).find(x=>x.host_id===hostId||x.host===hostName);
    if(w){
      showToast(w.ok?`${hostName}: 更新成功 (${w.updated_files||0} 文件)`:`${hostName}: ${w.error||'更新失败'}`
        , w.ok?'success':'warn');
    }else{
      showToast(r.summary||'更新指令已发送','success');
    }
    setTimeout(loadClusterPage, 8000);
  }catch(e){showToast('更新失败: '+e.message,'error');}
}

async function removeHost(hostId){
  try{
    await api('DELETE','/cluster/hosts/'+hostId);
    showToast('主机已移除');
    loadClusterPage();
  }catch(e){showToast('移除失败: '+e.message);}
}
async function clusterDispatch(){
  const taskType=document.getElementById('cl-task-type').value;
  const deviceId=document.getElementById('cl-device-select').value;
  try{
    const res=await api('POST','/cluster/dispatch',{type:taskType,device_id:deviceId||undefined});
    showToast('任务已路由: '+(res.routed_to?.host_name||res.routed_to?.host_ip||''));
  }catch(e){showToast('路由失败: '+e.message);}
}
async function loadClusterConfig(){
  try{
    const cfg=await api('GET','/cluster/config');
    document.getElementById('cl-cfg-role').value=cfg.role||'standalone';
    document.getElementById('cl-cfg-url').value=cfg.coordinator_url||'';
    document.getElementById('cl-cfg-secret').value=cfg.shared_secret||'';
    document.getElementById('cl-cfg-port').value=cfg.local_port||8000;
    document.getElementById('cl-cfg-name').value=cfg.host_name||'';
  }catch(e){}
}
async function saveClusterConfig(){
  try{
    const cfg={
      role:document.getElementById('cl-cfg-role').value,
      coordinator_url:document.getElementById('cl-cfg-url').value,
      shared_secret:document.getElementById('cl-cfg-secret').value,
      local_port:parseInt(document.getElementById('cl-cfg-port').value)||8000,
      host_name:document.getElementById('cl-cfg-name').value,
      auto_join:true,
    };
    await api('POST','/cluster/config',cfg);
    showToast('集群配置已保存并应用');
    setTimeout(loadClusterPage,1500);
  }catch(e){showToast('保存失败: '+e.message,'warn');}
}
async function clusterBatchAll(){
  const taskType=document.getElementById('cl-task-type').value;
  if(!taskType){showToast('请选择任务类型','warn');return;}
  try{
    const r=await api('POST','/cluster/batch',{type:taskType,target:'all',params:{}});
    const ok=r.results?.filter(x=>x.status==='ok').length||0;
    const err=r.results?.filter(x=>x.status==='error').length||0;
    showToast(`全集群批量: ${ok} 成功, ${err} 失败, 共 ${r.hosts_targeted||0} 主机`);
  }catch(e){showToast('批量失败: '+e.message);}
}

/* ── Worker 编号段分配 ── */
async function _loadRangesSection(){
  const wrap = document.getElementById('cl-ranges-rows');
  if(!wrap) return;
  let ranges={}, hosts=[];
  try{
    const [r,ov] = await Promise.all([
      api('GET','/cluster/number-ranges'),
      api('GET','/cluster/overview'),
    ]);
    ranges = r||{};
    hosts = (ov.hosts||[]).filter(h=>h.online||true);
  }catch(e){ wrap.innerHTML='<span style="color:var(--red)">加载失败</span>'; return; }

  if(!hosts.length){ wrap.innerHTML='<span style="color:var(--text-muted);font-size:12px">暂无已注册主机</span>'; return; }
  wrap.innerHTML = hosts.map(h=>{
    const hid = h.host_id;
    const nm = h.host_name||hid.substring(0,8);
    const rng = ranges[hid]||{};
    const s = rng.start||'';
    const e = rng.end||'';
    const color = _getWorkerColor(nm);
    return `<div class="range-config-row" style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
      <span style="width:80px;font-size:12px;font-weight:600;color:${color}">${nm}</span>
      <span style="font-size:11px;color:var(--text-muted)">编号</span>
      <input class="range-inp" id="rng-s-${hid}" type="number" min="1" value="${s}" placeholder="起始" style="width:64px;text-align:center">
      <span style="font-size:11px;color:var(--text-muted)">~</span>
      <input class="range-inp" id="rng-e-${hid}" type="number" min="1" value="${e}" placeholder="结束" style="width:64px;text-align:center">
      ${h.online?'':'<span style="font-size:10px;color:var(--red)">离线</span>'}
    </div>`;
  }).join('');
  // 智能建议按钮
  wrap.insertAdjacentHTML('beforeend',
    `<button onclick="suggestRanges()" style="margin-top:6px;font-size:11px;padding:3px 10px;background:rgba(99,102,241,.15);border:1px solid rgba(99,102,241,.4);color:#818cf8;border-radius:5px;cursor:pointer">🎯 智能建议</button>`);
  // store host list for save
  wrap.dataset.hosts = JSON.stringify(hosts.map(h=>h.host_id));
}

async function suggestRanges(){
  try{
    const r = await api('GET','/cluster/number-ranges/suggest');
    const sug = r.suggestions||{};
    if(!Object.keys(sug).length){ showToast('暂无主机信息，无法建议','warn'); return; }
    let filled = 0;
    for(const [hid, s] of Object.entries(sug)){
      const si = document.getElementById('rng-s-'+hid);
      const ei = document.getElementById('rng-e-'+hid);
      if(si&&ei){ si.value=s.start; ei.value=s.end; filled++; }
    }
    if(filled) showToast(`已填入 ${filled} 个主机的建议编号段（含1.5x缓冲），确认后点保存`,'success');
    else showToast('未找到匹配的输入框','warn');
  }catch(e){ showToast('获取建议失败: '+e.message,'warn'); }
}

async function saveClusterRanges(){
  const wrap = document.getElementById('cl-ranges-rows');
  if(!wrap) return;
  const hostIds = JSON.parse(wrap.dataset.hosts||'[]');
  if(!hostIds.length){ showToast('没有可配置的主机','warn'); return; }
  const body={};
  for(const hid of hostIds){
    const s = parseInt(document.getElementById('rng-s-'+hid)?.value)||0;
    const e = parseInt(document.getElementById('rng-e-'+hid)?.value)||0;
    if(s>0&&e>=s) body[hid]={start:s,end:e};
  }
  if(!Object.keys(body).length){ showToast('请填写有效的编号段','warn'); return; }
  try{
    await api('POST','/cluster/number-ranges', body);
    showToast('编号段配置已保存','success');
  }catch(e){ showToast('保存失败: '+e.message,'warn'); }
}

async function _loadClusterScripts(){
  try{
    const [scripts,stats]=await Promise.all([api('GET','/scripts'),api('GET','/cluster/stats')]);
    const sel=document.getElementById('cl-script-sel');
    if(sel)sel.innerHTML=(scripts.scripts||[]).map(s=>`<option value="${s.name}">${s.name}</option>`).join('')||'<option value="">无脚本</option>';
    const tgt=document.getElementById('cl-script-target');
    if(tgt)tgt.innerHTML='<option value="all">全部主机</option>'+
      (stats.hosts||[]).map(h=>`<option value="host:${h.name}">${h.name} (${h.online_devices}台)</option>`).join('');
  }catch(e){}
}
async function clusterExecScript(){
  const filename=document.getElementById('cl-script-sel')?.value;
  const target=document.getElementById('cl-script-target')?.value||'all';
  if(!filename){showToast('请选择脚本','warn');return;}
  const res=document.getElementById('cl-script-results');
  res.innerHTML='<span style="color:var(--text-muted)">执行中...</span>';
  try{
    const r=await api('POST','/cluster/execute-script',{filename,type:'adb',target});
    let html='';
    for(const[host,info] of Object.entries(r.hosts||{})){
      const color=info.status==='ok'?'#22c55e':'#ef4444';
      html+=`<div style="margin-bottom:6px"><strong style="color:${color}">${host}</strong>: `;
      if(info.status==='ok'){
        html+=`${info.total} 台设备`;
        for(const[did,dr] of Object.entries(info.results||{})){
          html+=`<div style="margin-left:12px;font-size:9px"><code>${did.substring(0,8)}</code>: ${dr.success?'OK':'FAIL'} — ${(dr.output||'').substring(0,80)}</div>`;
        }
      }else{html+=`<span style="color:#ef4444">${info.error||'失败'}</span>`;}
      html+='</div>';
    }
    res.innerHTML=html||'无结果';
    showToast('跨集群脚本执行完成');
  }catch(e){res.innerHTML=`<span style="color:#ef4444">失败: ${e.message}</span>`;}
}


