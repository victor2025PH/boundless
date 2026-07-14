/* ═══════════════════════════════════════════
   VPN 管理页面 — 配置池 + 状态大盘
   ═══════════════════════════════════════════ */

// 国家旗帜映射（复用 devices.js 中的，但保持独立可用）
const _vpnFlags={italy:'\u{1F1EE}\u{1F1F9}',us:'\u{1F1FA}\u{1F1F8}',usa:'\u{1F1FA}\u{1F1F8}',uk:'\u{1F1EC}\u{1F1E7}',germany:'\u{1F1E9}\u{1F1EA}',france:'\u{1F1EB}\u{1F1F7}',japan:'\u{1F1EF}\u{1F1F5}',korea:'\u{1F1F0}\u{1F1F7}',brazil:'\u{1F1E7}\u{1F1F7}',canada:'\u{1F1E8}\u{1F1E6}',australia:'\u{1F1E6}\u{1F1FA}',singapore:'\u{1F1F8}\u{1F1EC}',india:'\u{1F1EE}\u{1F1F3}',russia:'\u{1F1F7}\u{1F1FA}',spain:'\u{1F1EA}\u{1F1F8}',netherlands:'\u{1F1F3}\u{1F1F1}',turkey:'\u{1F1F9}\u{1F1F7}',thailand:'\u{1F1F9}\u{1F1ED}',vietnam:'\u{1F1FB}\u{1F1F3}',philippines:'\u{1F1F5}\u{1F1ED}',indonesia:'\u{1F1EE}\u{1F1E9}',malaysia:'\u{1F1F2}\u{1F1FE}',mexico:'\u{1F1F2}\u{1F1FD}',argentina:'\u{1F1E6}\u{1F1F7}',colombia:'\u{1F1E8}\u{1F1F4}',chile:'\u{1F1E8}\u{1F1F1}',poland:'\u{1F1F5}\u{1F1F1}',romania:'\u{1F1F7}\u{1F1F4}',ukraine:'\u{1F1FA}\u{1F1E6}',sweden:'\u{1F1F8}\u{1F1EA}',norway:'\u{1F1F3}\u{1F1F4}',finland:'\u{1F1EB}\u{1F1EE}',denmark:'\u{1F1E9}\u{1F1F0}',ireland:'\u{1F1EE}\u{1F1EA}',portugal:'\u{1F1F5}\u{1F1F9}',switzerland:'\u{1F1E8}\u{1F1ED}',austria:'\u{1F1E6}\u{1F1F9}',belgium:'\u{1F1E7}\u{1F1EA}',czech:'\u{1F1E8}\u{1F1FF}',greece:'\u{1F1EC}\u{1F1F7}',hungary:'\u{1F1ED}\u{1F1FA}',israel:'\u{1F1EE}\u{1F1F1}',uae:'\u{1F1E6}\u{1F1EA}',egypt:'\u{1F1EA}\u{1F1EC}',southafrica:'\u{1F1FF}\u{1F1E6}',hongkong:'\u{1F1ED}\u{1F1F0}',taiwan:'\u{1F1F9}\u{1F1FC}'};
function _vpnFlag(c){if(!c)return'\u{1F310}';return _vpnFlags[c.toLowerCase().replace(/[^a-z]/g,'')]||'\u{1F310}';}

let _vpnMgrData=null;

async function loadVpnManagePage(){
  try{
    const d=await api('GET','/vpn/dashboard-stats');
    _vpnMgrData=d;
    _vpnMgrRenderStats(d.summary);
    _vpnMgrRenderPool(d.pool);
    _vpnMgrRenderDevices(d.devices, d.pool);
    _vpnMgrRestoreRotation(d.pool);
    // 自动重连开关
    const cb=document.getElementById('vpn-mgr-auto-reconnect');
    if(cb) cb.checked=d.auto_reconnect||false;
    // 加载图表 + 启动自动刷新
    _vpnMgrLoadChart();
    if(document.getElementById('vpn-mgr-auto-refresh')?.checked) _vpnMgrStartAutoRefresh();
  }catch(e){
    showToast('加载 VPN 数据失败: '+e.message,'warn');
  }
}

function _vpnMgrRenderStats(s){
  const el=id=>document.getElementById(id);
  el('vpn-stat-total').textContent=s.total;
  el('vpn-stat-connected').textContent=s.connected;
  el('vpn-stat-disconnected').textContent=s.disconnected;
  el('vpn-stat-failed').textContent=s.failed;
}

/* ── 配置池渲染 ── */
function _vpnMgrRenderPool(pool){
  const container=document.getElementById('vpn-pool-list');
  const configs=pool.configs||[];
  const assignments=pool.assignments||{};

  if(!configs.length){
    container.innerHTML=`<div style="text-align:center;padding:30px 10px;color:var(--text-muted)">
      <div style="font-size:24px;margin-bottom:6px">\u{1F512}</div>
      <div style="font-size:11px;margin-bottom:8px">配置池为空</div>
      <div style="font-size:10px;color:var(--text-dim)">点击上方"+ 添加"导入 VPN 配置<br>支持 VLESS / VMess / Trojan / SS</div>
    </div>`;
    return;
  }

  // 统计每个配置分配了多少设备
  const countMap={};
  for(const [did,cid] of Object.entries(assignments)){
    countMap[cid]=(countMap[cid]||0)+1;
  }

  let html='';
  for(const c of configs){
    const flag=_vpnFlag(c.country);
    const cnt=countMap[c.id]||0;
    const score=c.score!==undefined?c.score:null;
    const scoreColor=score===null?'var(--text-dim)':score>=70?'#22c55e':score>=40?'#eab308':'#ef4444';
    const scoreBadge=score!==null?`<span style="padding:1px 5px;border-radius:3px;font-size:9px;font-weight:600;background:${score>=70?'rgba(34,197,94,.15)':score>=40?'rgba(234,179,8,.15)':'rgba(239,68,68,.15)'};color:${scoreColor}">${score}</span>`:'';
    const connectRate=c.last_connect_rate!==undefined?`<span style="font-size:9px;color:var(--text-dim)">${c.last_connect_rate}%</span>`:'';
    html+=`<div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:8px;position:relative" data-config-id="${c.id}">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="display:flex;align-items:center;gap:4px">
          <span style="font-size:16px">${flag}</span>
          <span style="font-size:12px;font-weight:600">${_esc(c.label||c.remark)}</span>
          ${scoreBadge}
        </div>
        <button onclick="_vpnMgrDeleteConfig('${c.id}')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:14px;padding:2px 4px" title="删除">&times;</button>
      </div>
      <div style="font-size:9px;color:var(--text-dim);margin-top:4px;font-family:monospace">
        ${_esc(c.protocol)} \u{2192} ${_esc(c.server)}:${c.port} ${connectRate}
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">
        <span style="font-size:10px;color:var(--text-muted)">${c.country?flag+' '+c.country:'\u{1F310} 未设国家'} \u{00B7} ${cnt} 台</span>
        <div style="display:flex;gap:3px">
          <button class="sb-btn2" onclick="_vpnMgrDeploy('${c.id}')" style="font-size:9px;padding:2px 8px;color:#22c55e;border-color:#22c55e44" title="一键部署到全部设备">\u{1F680}</button>
          <button class="sb-btn2" onclick="_vpnMgrAssignConfig('${c.id}','selected')" style="font-size:9px;padding:2px 6px" title="分配给勾选">勾选</button>
          <button class="sb-btn2" onclick="_vpnMgrShowGroupAssign('${c.id}')" style="font-size:9px;padding:2px 6px" title="分配给分组">分组</button>
          <button class="sb-btn2" onclick="_vpnMgrAssignConfig('${c.id}','all')" style="font-size:9px;padding:2px 6px;border-color:#7c3aed44" title="分配给全部">全部</button>
        </div>
      </div>
    </div>`;
  }
  container.innerHTML=html;
}

/* ── 设备状态表渲染 ── */
function _vpnMgrRenderDevices(devices, pool){
  const container=document.getElementById('vpn-devices-table');
  const filter=document.getElementById('vpn-dev-filter')?.value||'all';
  const assignments=pool?.assignments||{};
  const configMap={};
  for(const c of (pool?.configs||[])){configMap[c.id]=c;}

  let filtered=devices;
  if(filter==='connected') filtered=devices.filter(d=>d.connected);
  else if(filter==='disconnected') filtered=devices.filter(d=>!d.connected);
  else if(filter==='failed') filtered=devices.filter(d=>d.failures>=3);

  if(!filtered.length){
    container.innerHTML='<div style="text-align:center;padding:20px;color:var(--text-muted);font-size:11px">无匹配设备</div>';
    return;
  }

  let html=`<table style="width:100%;border-collapse:collapse;font-size:11px">
    <thead><tr style="color:var(--text-muted);font-size:10px;border-bottom:1px solid var(--border)">
      <th style="padding:6px;text-align:left"><input type="checkbox" id="vpn-dev-select-all" onchange="_vpnMgrToggleSelectAll(this)"></th>
      <th style="padding:6px;text-align:left">设备</th>
      <th style="padding:6px;text-align:center">VPN</th>
      <th style="padding:6px;text-align:left">IP / 国家</th>
      <th style="padding:6px;text-align:left">配置</th>
      <th style="padding:6px;text-align:center">操作</th>
    </tr></thead><tbody>`;

  for(const d of filtered){
    const alias=(typeof ALIAS==='object'&&ALIAS[d.device_id])?ALIAS[d.device_id]:'';
    const displayName=alias||d.short;
    const statusColor=d.connected?'#22c55e':'#ef4444';
    const statusIcon=d.connected?'\u2705':'\u274C';
    const assignedCfg=configMap[assignments[d.device_id]];
    const cfgLabel=assignedCfg?_vpnFlag(assignedCfg.country)+' '+_esc(assignedCfg.label||assignedCfg.remark):'<span style="color:var(--text-dim)">未分配</span>';
    const countryFlag=d.country?_vpnFlag(d.country)+' '+d.country:'-';

    html+=`<tr style="border-bottom:1px solid var(--border);transition:background .15s" onmouseenter="this.style.background='var(--bg-main)'" onmouseleave="this.style.background='transparent'">
      <td style="padding:6px"><input type="checkbox" class="vpn-dev-cb" data-did="${d.device_id}"></td>
      <td style="padding:6px;font-weight:600">${_esc(displayName)}</td>
      <td style="padding:6px;text-align:center;color:${statusColor}">${statusIcon}</td>
      <td style="padding:6px;font-family:monospace;font-size:10px">${d.ip||'-'} ${countryFlag}</td>
      <td style="padding:6px;font-size:10px">${cfgLabel}</td>
      <td style="padding:6px;text-align:center">
        <button class="sb-btn2" onclick="_vpnMgrDevAction('${d.device_id}','check')" style="font-size:9px;padding:1px 6px" title="检查">&#128270;</button>
        <button class="sb-btn2" onclick="_vpnMgrDevAction('${d.device_id}','start')" style="font-size:9px;padding:1px 6px;color:#22c55e" title="启动">&#9654;</button>
        <button class="sb-btn2" onclick="_vpnMgrDevAction('${d.device_id}','stop')" style="font-size:9px;padding:1px 6px;color:#ef4444" title="停止">&#9632;</button>
      </td>
    </tr>`;
  }
  html+='</tbody></table>';
  container.innerHTML=html;
}

function _vpnMgrFilterDevices(){
  if(_vpnMgrData) _vpnMgrRenderDevices(_vpnMgrData.devices, _vpnMgrData.pool);
}

/* ── HTML 转义 ── */
function _esc(s){
  if(!s) return '';
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

/* ── 操作函数 ── */
async function _vpnMgrRefresh(){
  showToast('刷新中...');
  await loadVpnManagePage();
  showToast('VPN 状态已刷新','success');
}

async function _vpnMgrStartAll(){
  if(!(await ocDialog({title:'启动全部 VPN',message:'即将启动全部设备的 VPN。',type:'info',confirmText:'启动',cancelText:'取消'}))) return;
  try{
    const d=await api('POST','/vpn/toggle',{});
    const ok=Object.values(d.results||{}).filter(v=>v==='OK').length;
    showToast('VPN 启动: '+ok+'/'+d.total+' 成功','success');
    setTimeout(loadVpnManagePage,3000);
  }catch(e){showToast('启动失败: '+e.message,'warn');}
}

async function _vpnMgrStopAll(){
  if(!(await ocDialog({title:'停止全部 VPN',message:'即将停止全部设备的 VPN。',type:'warning',confirmText:'停止',cancelText:'取消'}))) return;
  try{
    const d=await api('POST','/vpn/batch-stop',{});
    showToast('VPN 停止: '+d.stopped+'/'+d.total,'success');
    setTimeout(loadVpnManagePage,2000);
  }catch(e){showToast('停止失败: '+e.message,'warn');}
}

async function _vpnMgrToggleAutoReconnect(){
  const on=document.getElementById('vpn-mgr-auto-reconnect').checked;
  try{
    await api('POST','/vpn/auto-reconnect',{enabled:on});
    showToast('自动重连已'+(on?'启用':'禁用'),'success');
  }catch(e){showToast('设置失败: '+e.message,'warn');}
}

/* ── 配置池操作 ── */
function _vpnMgrShowAddConfig(){
  const form=document.getElementById('vpn-pool-add-form');
  form.style.display=form.style.display==='none'?'block':'none';
}

async function _vpnMgrAddConfig(){
  const uri=document.getElementById('vpn-pool-uri').value.trim();
  const country=document.getElementById('vpn-pool-country').value.trim();
  const label=document.getElementById('vpn-pool-label').value.trim();
  if(!uri){showToast('请输入 VPN 链接','warn');return;}
  try{
    await api('POST','/vpn/pool/add',{uri,country,label});
    showToast('配置已添加','success');
    document.getElementById('vpn-pool-add-form').style.display='none';
    document.getElementById('vpn-pool-uri').value='';
    document.getElementById('vpn-pool-country').value='';
    document.getElementById('vpn-pool-label').value='';
    await loadVpnManagePage();
  }catch(e){showToast('添加失败: '+e.message,'warn');}
}

async function _vpnMgrDeleteConfig(configId){
  if(!(await ocDialog({title:'删除配置',message:'确认删除此配置？关联的设备分配也将被清除。',type:'danger',confirmText:'删除',dangerous:true}))) return;
  try{
    await api('DELETE','/vpn/pool/'+configId);
    showToast('已删除','success');
    await loadVpnManagePage();
  }catch(e){showToast('删除失败: '+e.message,'warn');}
}

async function _vpnMgrAssignConfig(configId, mode){
  let deviceIds;
  if(mode==='all'){
    if(!(await ocDialog({title:'分配配置',message:'将此配置分配给全部在线设备？',type:'info',confirmText:'分配',cancelText:'取消'}))) return;
  }else{
    // 获取勾选的设备
    deviceIds=[...document.querySelectorAll('.vpn-dev-cb:checked')].map(cb=>cb.dataset.did);
    if(!deviceIds.length){showToast('请先勾选设备','warn');return;}
  }
  try{
    const body={config_id:configId};
    if(mode==='all') body.all=true;
    else body.device_ids=deviceIds;
    const d=await api('POST','/vpn/pool/assign',body);
    showToast('已分配 '+d.assigned+' 台设备','success');
    await loadVpnManagePage();
  }catch(e){showToast('分配失败: '+e.message,'warn');}
}

async function _vpnMgrShowGroupAssign(configId){
  // 加载分组列表让用户选择
  try{
    const d=await api('GET','/device-groups');
    const groups=d.groups||[];
    if(!groups.length){showToast('还没有设备分组，请先在"设备分组"页面创建','warn');return;}
    const names=groups.map((g,i)=>(i+1)+'. '+g.name+' ('+g.devices.length+'台)').join('\n');
    const choice=await ocPrompt('选择分组','',{message:names.replace(/\n/g,'<br>'),inputPlaceholder:'输入编号'});
    if(!choice) return;
    const idx=parseInt(choice)-1;
    if(idx<0||idx>=groups.length){showToast('无效选择','warn');return;}
    const group=groups[idx];
    const body={config_id:configId,group_id:group.id};
    const r=await api('POST','/vpn/pool/assign',body);
    showToast('已分配 '+r.assigned+' 台设备 ('+group.name+')','success');
    await loadVpnManagePage();
  }catch(e){showToast('分配失败: '+e.message,'warn');}
}

async function _vpnMgrApplyPool(){
  if(!(await ocDialog({title:'应用配置池',message:'按配置池分配关系，给每台设备应用 VPN 配置并启动。<br>此操作可能需要 30-60 秒。',type:'info',confirmText:'应用',cancelText:'取消'}))) return;
  const prog=document.getElementById('vpn-mgr-progress');
  const progLabel=document.getElementById('vpn-mgr-prog-label');
  const progCount=document.getElementById('vpn-mgr-prog-count');
  const progBar=document.getElementById('vpn-mgr-prog-bar');
  const progDetails=document.getElementById('vpn-mgr-prog-details');
  if(prog) prog.style.display='block';
  if(progLabel) progLabel.textContent='应用配置中...';
  if(progBar) progBar.style.width='10%';
  if(progDetails) progDetails.innerHTML='';

  try{
    const d=await api('POST','/vpn/pool/apply',{});
    if(progBar) progBar.style.width='100%';
    if(progLabel) progLabel.innerHTML='<span style="color:#22c55e">\u2705 完成</span>';
    if(progCount) progCount.textContent=d.connected+'/'+d.total+' 台已连接';
    // 渲染详情
    if(progDetails&&d.results){
      let html='';
      for(const [short,r] of Object.entries(d.results)){
        const icon=r.connected?'\u2705':r.ok?'\u26A0':'\u274C';
        const color=r.connected?'#22c55e':r.ok?'#eab308':'#ef4444';
        html+=`<div style="color:${color}">${icon} ${short} ${r.connected?'已连接':'失败'}${r.config?' \u00B7 '+r.config:''}${r.time?' ('+r.time.toFixed(1)+'s)':''}${r.error?' \u00B7 '+r.error:''}</div>`;
      }
      progDetails.innerHTML=html;
    }
    showToast('配置池应用完成: '+d.connected+'/'+d.total+' 已连接','success');
    setTimeout(loadVpnManagePage,2000);
  }catch(e){
    if(progLabel) progLabel.innerHTML='<span style="color:#ef4444">\u274C '+e.message+'</span>';
  }
}

/* ═══════════════════════════════════════════
   订阅链接导入
   ═══════════════════════════════════════════ */

function _vpnMgrShowImportSub(){
  const form=document.getElementById('vpn-sub-import-form');
  form.style.display=form.style.display==='none'?'block':'none';
  // 隐藏添加表单
  document.getElementById('vpn-pool-add-form').style.display='none';
}

async function _vpnMgrImportSub(){
  const url=document.getElementById('vpn-sub-url').value.trim();
  const text=document.getElementById('vpn-sub-text').value.trim();
  const country=document.getElementById('vpn-sub-country').value.trim();
  const replace=document.getElementById('vpn-sub-replace').checked;
  const resultEl=document.getElementById('vpn-sub-result');

  if(!url&&!text){showToast('请输入订阅链接或 VPN 配置','warn');return;}

  if(resultEl) resultEl.innerHTML='<span style="color:var(--accent)">\u23F3 导入中...</span>';
  try{
    const body={country,replace};
    if(url) body.url=url;
    else body.text=text;
    const d=await api('POST','/vpn/pool/import-subscription',body);
    let msg='\u2705 导入成功: '+d.added+' 个配置';
    if(d.errors) msg+=' ('+d.errors+' 个失败)';
    msg+=' \u00B7 配置池共 '+d.total_in_pool+' 个';
    if(resultEl) resultEl.innerHTML='<span style="color:#22c55e">'+msg+'</span>';
    showToast(msg,'success');
    // 清空表单
    document.getElementById('vpn-sub-url').value='';
    document.getElementById('vpn-sub-text').value='';
    await loadVpnManagePage();
  }catch(e){
    if(resultEl) resultEl.innerHTML='<span style="color:#ef4444">\u274C '+e.message+'</span>';
    showToast('导入失败: '+e.message,'warn');
  }
}

/* ═══════════════════════════════════════════
   创建定时轮换任务
   ═══════════════════════════════════════════ */

async function _vpnMgrCreateScheduledRotation(){
  const interval=parseInt(document.getElementById('vpn-rotation-interval').value)||120;
  const strategy=document.getElementById('vpn-rotation-strategy').value;

  // 计算 cron 表达式
  let cron;
  if(interval<=60) cron=`0 */${interval} * * *`;      // 每 N 分钟（小时级）
  else if(interval<=1440){
    const hours=Math.round(interval/60);
    cron=`0 */${hours} * * *`;                          // 每 N 小时
  }else{
    cron='0 0 * * *';                                   // 每天
  }

  const name='VPN 自动轮换 ('+strategy+', '+cron+')';
  if(!(await ocDialog({title:'创建定时任务',message:'名称: '+name+'<br>周期: '+cron+'<br>策略: '+strategy,type:'info',confirmText:'创建',cancelText:'取消'}))) return;

  try{
    const d=await api('POST','/scheduled-jobs',{
      name:name,
      cron:cron,
      action:'vpn_rotate',
      params:{strategy:strategy,apply:true}
    });
    showToast('定时轮换任务已创建 (ID: '+d.id+')','success');
    const statusEl=document.getElementById('vpn-rotation-status');
    if(statusEl) statusEl.textContent='\u2705 定时任务已创建: '+cron+' '+strategy;
  }catch(e){
    showToast('创建失败: '+e.message,'warn');
  }
}

/* ── 单设备操作 ── */
async function _vpnMgrDevAction(deviceId, action){
  try{
    if(action==='check'){
      const d=await api('GET','/vpn/status/'+deviceId);
      showToast(deviceId.substring(0,8)+': '+(d.connected?'VPN \u2705 已连接':'VPN \u274C 未连接'));
    }else if(action==='start'){
      await api('POST','/vpn/toggle',{device_ids:[deviceId]});
      showToast(deviceId.substring(0,8)+': VPN 启动命令已发送','success');
    }else if(action==='stop'){
      await api('POST','/vpn/stop/'+deviceId);
      showToast(deviceId.substring(0,8)+': VPN 已停止');
    }
    setTimeout(loadVpnManagePage,2000);
  }catch(e){showToast('操作失败: '+e.message,'warn');}
}

/* ── 全选/取消全选 ── */
function _vpnMgrToggleSelectAll(masterCb){
  document.querySelectorAll('.vpn-dev-cb').forEach(cb=>{cb.checked=masterCb.checked;});
}

/* ═══════════════════════════════════════════
   轮换引擎
   ═══════════════════════════════════════════ */

async function _vpnMgrSaveRotation(){
  const enabled=document.getElementById('vpn-rotation-enabled').checked;
  const interval=parseInt(document.getElementById('vpn-rotation-interval').value)||120;
  const strategy=document.getElementById('vpn-rotation-strategy').value;
  try{
    await api('POST','/vpn/pool/rotation-settings',{enabled,interval_minutes:interval,strategy});
    showToast('轮换设置已保存','success');
  }catch(e){showToast('保存失败: '+e.message,'warn');}
}

async function _vpnMgrRotateNow(applyNow){
  const strategy=document.getElementById('vpn-rotation-strategy').value;
  const statusEl=document.getElementById('vpn-rotation-status');
  if(statusEl) statusEl.textContent='轮换中...';
  try{
    const d=await api('POST','/vpn/pool/rotate',{strategy,apply:applyNow});
    if(!d.ok){
      showToast(d.error||'轮换失败','warn');
      if(statusEl) statusEl.textContent='轮换失败: '+(d.error||'');
      return;
    }
    let msg='已轮换 '+d.changed+' 台设备 ('+strategy+')';
    if(applyNow&&d.apply_result){
      msg+=' \u00B7 '+d.apply_result.connected+'/'+d.apply_result.total+' 已连接';
    }
    showToast(msg,'success');
    if(statusEl) statusEl.textContent='上次轮换: '+new Date().toLocaleTimeString()+' \u00B7 '+msg;
    setTimeout(loadVpnManagePage,2000);
  }catch(e){
    showToast('轮换失败: '+e.message,'warn');
    if(statusEl) statusEl.textContent='轮换失败';
  }
}

// 页面加载时恢复轮换设置
function _vpnMgrRestoreRotation(pool){
  const rot=pool?.rotation;
  if(!rot) return;
  const en=document.getElementById('vpn-rotation-enabled');
  const iv=document.getElementById('vpn-rotation-interval');
  const st=document.getElementById('vpn-rotation-strategy');
  const statusEl=document.getElementById('vpn-rotation-status');
  if(en) en.checked=rot.enabled||false;
  if(iv) iv.value=String(rot.interval_minutes||120);
  if(st) st.value=rot.strategy||'round-robin';
  if(statusEl&&pool.last_rotation) statusEl.textContent='上次轮换: '+pool.last_rotation;
}

/* ═══════════════════════════════════════════
   连接质量测速
   ═══════════════════════════════════════════ */

async function _vpnMgrSpeedTest(){
  const btn=document.getElementById('vpn-speed-btn');
  const resultEl=document.getElementById('vpn-speed-result');
  if(btn) btn.disabled=true;
  if(btn) btn.textContent='\u23F3 测速中...';
  if(resultEl) resultEl.innerHTML='<span style="color:var(--accent)">正在测试所有设备的 VPN 连接质量，请稍候...</span>';
  try{
    const d=await api('POST','/vpn/speed-test',{});
    let html=`<div style="margin-bottom:4px"><b>${d.reachable}/${d.total}</b> 可达`;
    if(d.avg_latency_ms!==null) html+=` \u00B7 平均延迟 <b>${d.avg_latency_ms}ms</b>`;
    html+='</div>';
    // 逐设备结果
    html+='<div style="max-height:100px;overflow-y:auto">';
    for(const r of (d.results||[])){
      const icon=r.reachable?'\u2705':'\u274C';
      const color=r.reachable?'#22c55e':'#ef4444';
      const lat=r.latency_ms?r.latency_ms.toFixed(0)+'ms':'-';
      const dns=r.dns_ok===true?'DNS\u2705':r.dns_ok===false?'DNS\u274C':'';
      html+=`<div style="color:${color};font-size:9px">${icon} ${r.short} \u00B7 ${lat} ${dns}${r.error?' \u00B7 '+r.error:''}</div>`;
    }
    html+='</div>';
    if(resultEl) resultEl.innerHTML=html;
  }catch(e){
    if(resultEl) resultEl.innerHTML='<span style="color:#ef4444">\u274C 测速失败: '+e.message+'</span>';
  }
  if(btn){btn.disabled=false;btn.textContent='\u{1F4E1} 开始测速';}
}

/* ═══════════════════════════════════════════
   连接趋势图表 (Chart.js)
   ═══════════════════════════════════════════ */

let _vpnChart=null;

async function _vpnMgrLoadChart(){
  try{
    const d=await api('GET','/vpn/connection-history');
    const history=d.history||[];
    if(!history.length) return;

    const labels=history.map(h=>h.hour);
    const connected=history.map(h=>h.connected);
    const total=history.map(h=>h.total);
    const rates=history.map(h=>h.rate);

    const ctx=document.getElementById('vpn-history-chart');
    if(!ctx) return;

    if(_vpnChart){_vpnChart.destroy();_vpnChart=null;}

    if(typeof Chart==='undefined') return;

    // 强制固定 canvas 尺寸，防止 Chart.js 累积拉伸
    const wrapper=ctx.parentElement;
    if(wrapper){
      wrapper.style.height='240px';
      wrapper.style.position='relative';
    }
    ctx.style.height='240px';
    ctx.style.maxHeight='240px';
    ctx.width=wrapper?wrapper.clientWidth:500;
    ctx.height=240;

    _vpnChart=new Chart(ctx,{
      type:'line',
      data:{
        labels:labels,
        datasets:[
          {
            label:'连接率 %',
            data:rates,
            borderColor:'#22c55e',
            backgroundColor:'rgba(34,197,94,0.1)',
            fill:true,
            tension:0.4,
            pointRadius:2,
            yAxisID:'y',
          },
          {
            label:'已连接',
            data:connected,
            borderColor:'#3b82f6',
            borderDash:[4,4],
            tension:0.4,
            pointRadius:0,
            yAxisID:'y1',
          },
          {
            label:'总设备',
            data:total,
            borderColor:'rgba(148,163,184,0.4)',
            borderDash:[2,2],
            tension:0.4,
            pointRadius:0,
            yAxisID:'y1',
          }
        ]
      },
      options:{
        responsive:true,
        maintainAspectRatio:false,
        interaction:{mode:'index',intersect:false},
        plugins:{
          legend:{display:true,position:'top',labels:{boxWidth:12,font:{size:10},color:'#94a3b8'}},
        },
        scales:{
          x:{ticks:{font:{size:9},color:'#64748b',maxTicksLimit:12},grid:{color:'rgba(148,163,184,0.1)'}},
          y:{position:'left',min:0,max:100,ticks:{font:{size:9},color:'#22c55e',callback:v=>v+'%'},grid:{color:'rgba(148,163,184,0.08)'}},
          y1:{position:'right',min:0,ticks:{font:{size:9},color:'#3b82f6'},grid:{drawOnChartArea:false}},
        }
      }
    });
  }catch(e){console.error('Chart load failed:',e);}
}

/* ═══════════════════════════════════════════
   一键部署
   ═══════════════════════════════════════════ */

async function _vpnMgrDeploy(configId){
  if(!(await ocDialog({title:'一键部署',message:'一键部署此配置到全部在线设备。<br><br>流程: 分配 → 应用 → Geo-IP 验证<br>预计 30-60 秒',type:'info',confirmText:'部署',cancelText:'取消'}))) return;
  const prog=document.getElementById('vpn-mgr-progress');
  const progLabel=document.getElementById('vpn-mgr-prog-label');
  const progCount=document.getElementById('vpn-mgr-prog-count');
  const progBar=document.getElementById('vpn-mgr-prog-bar');
  const progDetails=document.getElementById('vpn-mgr-prog-details');
  if(prog) prog.style.display='block';
  if(progLabel) progLabel.textContent='\u{1F680} 一键部署中...';
  if(progBar) progBar.style.width='10%';
  if(progDetails) progDetails.innerHTML='';
  if(progCount) progCount.textContent='';

  try{
    const d=await api('POST','/vpn/pool/deploy',{config_id:configId,verify_geo:true});
    if(progBar) progBar.style.width='100%';
    if(progLabel) progLabel.innerHTML='<span style="color:#22c55e">\u2705 部署完成</span>';
    if(progCount) progCount.textContent=d.connected+'/'+d.total+' 已连接'+(d.deployed_country?' \u00B7 '+d.deployed_country:'');
    if(progDetails&&d.results){
      let html='';
      for(const [short,r] of Object.entries(d.results)){
        const icon=r.connected?'\u2705':'\u274C';
        const color=r.connected?'#22c55e':'#ef4444';
        const geo=r.geo_match===true?' \u{1F310}\u2705':r.geo_match===false?' \u{1F310}\u274C':'';
        const geoIp=r.geo_ip?' IP:'+r.geo_ip:'';
        html+=`<div style="color:${color}">${icon} ${short}${r.time?' ('+r.time.toFixed(1)+'s)':''}${geo}${geoIp}${r.error?' \u00B7 '+r.error:''}</div>`;
      }
      progDetails.innerHTML=html;
    }
    showToast('\u{1F680} 部署完成: '+d.connected+'/'+d.total+' 已连接','success');
    setTimeout(loadVpnManagePage,2000);
  }catch(e){
    if(progLabel) progLabel.innerHTML='<span style="color:#ef4444">\u274C '+e.message+'</span>';
  }
}

/* ═══════════════════════════════════════════
   Geo-IP 验证
   ═══════════════════════════════════════════ */

async function _vpnMgrGeoVerify(){
  const prog=document.getElementById('vpn-mgr-progress');
  const progLabel=document.getElementById('vpn-mgr-prog-label');
  const progCount=document.getElementById('vpn-mgr-prog-count');
  const progBar=document.getElementById('vpn-mgr-prog-bar');
  const progDetails=document.getElementById('vpn-mgr-prog-details');
  if(prog) prog.style.display='block';
  if(progLabel) progLabel.textContent='\u{1F310} Geo-IP 验证中...';
  if(progBar) progBar.style.width='30%';
  if(progDetails) progDetails.innerHTML='';
  if(progCount) progCount.textContent='';

  try{
    const d=await api('POST','/vpn/geo-verify-all',{});
    if(progBar) progBar.style.width='100%';
    if(progLabel) progLabel.innerHTML='<span style="color:#22c55e">\u2705 验证完成</span>';
    if(progCount) progCount.textContent=d.matched+' 匹配 / '+d.mismatched+' 不匹配 / '+d.total+' 总计';
    if(progDetails&&d.results){
      let html='';
      for(const r of d.results){
        const icon=r.match===true?'\u2705':r.match===false?'\u274C':'\u2753';
        const color=r.match===true?'#22c55e':r.match===false?'#ef4444':'#eab308';
        html+=`<div style="color:${color}">${icon} ${r.short} \u00B7 ${r.ip||'-'} \u00B7 ${r.country||'-'}${r.expected?' (expect: '+r.expected+')':''}${r.vpn_detected?' [VPN]':''}${r.error?' \u00B7 '+r.error:''}</div>`;
      }
      progDetails.innerHTML=html;
    }
    showToast('Geo-IP: '+d.matched+' \u2705 / '+d.mismatched+' \u274C','success');
  }catch(e){
    if(progLabel) progLabel.innerHTML='<span style="color:#ef4444">\u274C '+e.message+'</span>';
  }
}

/* ═══════════════════════════════════════════
   配置导出
   ═══════════════════════════════════════════ */

async function _vpnMgrExport(){
  const choice=await ocPrompt('导出格式','',{message:'1. JSON（完整配置池）<br>2. 订阅链接（base64）',inputPlaceholder:'输入 1 或 2'});
  if(!choice) return;
  const fmt=choice.trim()==='2'?'subscription':'json';
  try{
    const d=await api('GET','/vpn/pool/export?format='+fmt);
    if(fmt==='subscription'){
      // 复制到剪贴板
      await navigator.clipboard.writeText(d.data);
      showToast('已复制 base64 订阅内容到剪贴板 ('+d.count+' 个配置)','success');
    }else{
      // JSON 下载
      const blob=new Blob([JSON.stringify(d.data,null,2)],{type:'application/json'});
      const url=URL.createObjectURL(blob);
      const a=document.createElement('a');
      a.href=url;a.download='vpn_pool_export.json';a.click();
      URL.revokeObjectURL(url);
      showToast('已下载 vpn_pool_export.json ('+d.count+' 个配置)','success');
    }
  }catch(e){showToast('导出失败: '+e.message,'warn');}
}

/* ═══════════════════════════════════════════
   自动刷新（30秒刷新状态卡片）
   ═══════════════════════════════════════════ */

let _vpnAutoRefreshTimer=null;

function _vpnMgrToggleAutoRefresh(){
  const on=document.getElementById('vpn-mgr-auto-refresh')?.checked;
  if(on){
    _vpnMgrStartAutoRefresh();
  }else{
    _vpnMgrStopAutoRefresh();
  }
}

function _vpnMgrStartAutoRefresh(){
  _vpnMgrStopAutoRefresh();
  _vpnAutoRefreshTimer=setInterval(async()=>{
    if(_currentPage!=='vpn-manage') return;
    try{
      const d=await api('GET','/vpn/dashboard-stats');
      _vpnMgrData=d;
      _vpnMgrRenderStats(d.summary);
      // 只刷新状态卡片，不刷新配置池和设备表（避免打断用户操作）
    }catch(e){}
  },30000);
}

function _vpnMgrStopAutoRefresh(){
  if(_vpnAutoRefreshTimer){clearInterval(_vpnAutoRefreshTimer);_vpnAutoRefreshTimer=null;}
}

/* ── 安装 V2RayNG ── */
async function _vpnMgrInstallV2RayNG(){
  // 先检查 APK 是否已放入 apk_repo/
  let apkName='';
  try{
    const chk=await api('GET','/vpn/install-v2rayng/check');
    if(!chk.ready){
      showToast(`未找到 V2RayNG APK。\n请将 v2rayng-*.apk 放入:\n${chk.apk_repo}`, 'warn');
      // 弹出说明弹窗
      const msg=`⚠️ 安装前需手动准备 APK\n\n`+
        `1. 从 GitHub 下载 V2RayNG APK：\n`+
        `   https://github.com/2dust/v2rayNG/releases\n\n`+
        `2. 将下载的 .apk 文件放入：\n`+
        `   ${chk.apk_repo}\n\n`+
        `3. 文件名需包含 "v2ray"（如 v2rayNG_1.9.12.apk）\n\n`+
        `放好后重新点击此按钮即可批量安装。`;
      await ocAlert(msg);
      return;
    }
    apkName=chk.found[0]?.name||'v2rayng.apk';
  }catch(e){
    showToast('检查 APK 失败: '+e.message,'error');
    return;
  }

  // 选择目标设备
  const allDevices=_vpnMgrData?.devices||[];
  const onlineIds=allDevices.filter(d=>d.adb_status==='online'||d.online).map(d=>d.device_id);
  const checked=Array.from(document.querySelectorAll('.vpn-dev-chk:checked')).map(el=>el.dataset.did);
  const targets=checked.length>0?checked:null; // null = 全部在线设备

  const targetDesc=checked.length>0?`已勾选 ${checked.length} 台设备`:`全部在线设备 (${onlineIds.length} 台)`;
  if(!(await ocDialog({title:'安装 V2RayNG',message:`将安装 ${apkName} 到 ${targetDesc}。`,type:'info',confirmText:'安装',cancelText:'取消'}))) return;

  showToast(`正在安装 V2RayNG 到 ${targetDesc}...`,'info');

  // 显示进度面板
  const progPanel=document.getElementById('vpn-mgr-progress');
  const progLabel=document.getElementById('vpn-mgr-prog-label');
  const progDetails=document.getElementById('vpn-mgr-prog-details');
  if(progPanel){
    progPanel.style.display='block';
    progLabel.textContent='正在安装 V2RayNG...';
    progDetails.textContent='';
  }

  try{
    const body=targets?{device_ids:targets}:{};
    const r=await api('POST','/vpn/install-v2rayng',body);

    // 更新进度面板
    if(progPanel){
      progLabel.textContent=`安装完成：${r.success}/${r.total} 成功`;
      let details='';
      for(const [did,res] of Object.entries(r.results||{})){
        const icon=res.success?'✅':'❌';
        details+=`${icon} ${did.slice(0,12)}: ${res.message}\n`;
      }
      progDetails.textContent=details;
      setTimeout(()=>{if(progPanel)progPanel.style.display='none';},8000);
    }

    const tone=r.failed>0?'warn':'ok';
    showToast(`V2RayNG 安装完成：${r.success}/${r.total} 成功 | ${r.failed} 失败`, tone);
  }catch(e){
    if(progPanel) progPanel.style.display='none';
    showToast('安装失败: '+e.message,'error');
  }
}
