/* devices.js — 设备管理+交互控制: 设备列表、Interactive Control Modal（截图、点击、滑动、缩放等） */

/* ── P6: 任务状态可视化 ─────────────────────────────────────────────────── */
let _taskMap = {}; // device_id → {status, type, progress, updated_at}

/* ── P8: 设备健康指标 ─────────────────────────────────────────────────────── */
let _perfMap = {};  // device_id → {battery_level, mem_usage}

async function _syncPerfData() {
  try {
    const r = await api('GET', '/devices/performance/all').catch(() => null);
    if (!r || !r.devices) return;
    Object.entries(r.devices).forEach(([did, d]) => {
      _perfMap[did] = { battery: d.battery_level || 0, mem: d.mem_usage || 0 };
    });
  } catch(e) {}
}

async function _renderClusterBar() {
  const el = document.getElementById('cluster-node-bar');
  if (!el) return;
  try {
    const ov = await api('GET', '/cluster/overview').catch(() => null);
    if (!ov) { el.style.display = 'none'; return; }
    const hosts = ov.hosts || [];
    const localDev = (ov.local_devices || 0);
    const localTask = (ov.local_active_tasks || 0);
    // Coordinator (this node)
    let html = `<div class="cnb-node cnb-online">
      <span class="cnb-dot"></span>
      <span class="cnb-name">主控</span>
      <span class="cnb-stat">${localDev}设备</span>
      ${localTask > 0 ? `<span class="cnb-tasks">${localTask}任务</span>` : ''}
    </div>`;
    for (const h of hosts) {
      const cls = h.online ? 'cnb-online' : 'cnb-offline';
      const devCnt = h.device_count || 0;
      const taskCnt = h.active_tasks || 0;
      html += `<div class="cnb-node ${cls}">
        <span class="cnb-dot"></span>
        <span class="cnb-name">${h.host_name || h.host_id}</span>
        <span class="cnb-stat">${devCnt}设备</span>
        ${taskCnt > 0 ? `<span class="cnb-tasks">${taskCnt}任务</span>` : ''}
        ${!h.online ? '<span class="cnb-offline-lbl">离线</span>' : ''}
      </div>`;
    }
    if (hosts.length === 0) { el.style.display = 'none'; return; }
    el.innerHTML = html;
    el.style.display = 'flex';
  } catch(e) { el.style.display = 'none'; }
}

const _taskTypeLabels = {
  warmup:'养号', warmup_30:'养号30m', warmup_60:'养号60m',
  script:'脚本', screenshot:'截图', install_apk:'安装APK',
  uninstall:'卸载', quick_action:'快捷操作',
  tiktok_warmup:'TK养号', tiktok_follow:'TK关注', tiktok_like:'TK点赞',
  telegram_warmup:'TG养号', telegram_send:'TG发送',
  whatsapp_warmup:'WA养号',
};

function _syncTaskMap(){
  if(typeof allTasks==='undefined') return;
  const now=Date.now();
  const prio={running:0,pending:1,completed:2,failed:2,cancelled:3};
  const newMap={};
  for(const t of allTasks){
    const did=t.device_id;
    if(!did) continue;
    const s=t.status;
    const isFinal=s==='completed'||s==='failed'||s==='cancelled';
    if(isFinal){
      // 仅保留30秒内完成的（用于完成闪烁动画）
      try{
        const ms=new Date(t.updated_at).getTime();
        if(!ms||now-ms>30000) continue;
      }catch(e){ continue; }
    }
    const existing=newMap[did];
    const curRank=prio[s]??9;
    const exRank=existing?prio[existing.status]??9:99;
    if(curRank<exRank){
      newMap[did]={
        status:s, type:t.type||'',
        progress:(t.result&&t.result.progress)||0,
        updated_at:t.updated_at,
      };
    }
  }
  _taskMap=newMap;
  _updateTaskStrips();
}

function _updateTaskStrips(){
  for(const d of(allDevices||[])){
    const ds=d.device_id.substring(0,8);
    const strip=document.getElementById('ts-'+ds);
    const badge=document.getElementById('tb-'+ds);
    if(strip) _applyTaskStrip(strip,badge,_taskMap[d.device_id]);
  }
  _updateActiveTasksBadge();
}

function _applyTaskStrip(strip,badge,info){
  strip.style.width=''; strip.style.animation=''; strip.style.opacity='';
  if(!info){
    strip.className='task-status-strip';
    if(badge){badge.className='task-badge';badge.textContent='';badge.onclick=null;badge.style.cursor='';}
    return;
  }
  const lbl=_taskTypeLabels[info.type]||info.type||'任务';
  const pct=info.progress||0;
  // 设置徽章点击 → 历史弹出（从 id 反推 device_id）
  if(badge){
    badge.style.cursor='pointer';
    badge.style.pointerEvents='auto';
    // badge id = "tb-{didShort}", 从 allDevices 反查完整 did
    badge.onclick=(e)=>{
      e.stopPropagation();
      const ds=badge.id.replace('tb-','');
      const d=(allDevices||[]).find(x=>x.device_id.startsWith(ds));
      if(d) _showDeviceTaskHistory(d.device_id,badge);
    };
  }
  if(info.status==='pending'){
    strip.className='task-status-strip ts-queued';
    if(badge){badge.className='task-badge tb-queued';badge.textContent='⏳';badge.title='排队·'+lbl;}
  }else if(info.status==='running'){
    strip.className='task-status-strip ts-running';
    if(pct>0){strip.style.width=pct+'%';strip.style.animation='none';}
    if(badge){badge.className='task-badge tb-running';badge.textContent=pct?pct+'%':'▶';badge.title='执行中·'+lbl+(pct?' '+pct+'%':'');}
  }else if(info.status==='completed'){
    strip.className='task-status-strip ts-done';
    if(badge){badge.className='task-badge tb-done';badge.textContent='✓';badge.title='完成·'+lbl+'(点击查看历史)';}
  }else if(info.status==='failed'||info.status==='cancelled'){
    strip.className='task-status-strip ts-failed';
    if(badge){badge.className='task-badge tb-failed';badge.textContent='✗';badge.title='失败·'+lbl+'(点击查看历史)';}
  }
}

function _updateActiveTasksBadge(){
  const badge=document.getElementById('active-tasks-badge');
  if(!badge) return;
  const active=(allDevices||[]).filter(d=>{
    const info=_taskMap[d.device_id];
    return info&&(info.status==='running'||info.status==='pending');
  });
  const running=active.filter(d=>_taskMap[d.device_id].status==='running').length;
  const pending=active.filter(d=>_taskMap[d.device_id].status==='pending').length;
  if(active.length){
    badge.textContent=running&&pending?`🔄 ${running}执行 ${pending}排队`
      :running?`🔄 ${running} 执行中`:`⏳ ${pending} 排队中`;
    badge.style.display='inline-flex';
  }else{
    badge.style.display='none';
  }
}

/* ── P7: 时间估算进度（无真实进度数据时提供平滑视觉反馈）── */
const _taskStartTimes={};  // device_id → Date.now() when running started
const _taskDurations={     // 各任务类型预估秒数
  warmup:1800,warmup_30:1800,warmup_60:3600,warmup_120:7200,
  screenshot:15,install_apk:120,uninstall:30,quick_action:30,
  tiktok_warmup:1800,tiktok_follow:120,tiktok_like:60,
  telegram_warmup:1800,telegram_send:30,whatsapp_warmup:1800,
  script:300,
};
let _smoothProgressTimer=null;

function _startSmoothProgress(){
  if(_smoothProgressTimer) return;
  _smoothProgressTimer=setInterval(()=>{
    const now=Date.now();
    let hasRunning=false;
    for(const [did,info] of Object.entries(_taskMap)){
      if(info.status!=='running') continue;
      hasRunning=true;
      if(info.progress>0) continue; // 有真实进度，跳过估算
      if(!_taskStartTimes[did]) _taskStartTimes[did]=now;
      const durMs=(_taskDurations[info.type]||300)*1000;
      const t=Math.min(0.97,(now-_taskStartTimes[did])/durMs);
      // ease-out cubic: 快速起步、接近预估时减速，最高92%（留出真实完成空间）
      const est=Math.round((1-(1-t)**3)*92);
      if(est>0){
        const ds=did.substring(0,8);
        const strip=document.getElementById('ts-'+ds);
        const badge=document.getElementById('tb-'+ds);
        if(strip&&strip.classList.contains('ts-running')){
          strip.style.width=est+'%';
          strip.style.animation='none';
          if(badge&&badge.classList.contains('tb-running')) badge.textContent=est+'%';
        }
      }
    }
    if(!hasRunning){ clearInterval(_smoothProgressTimer); _smoothProgressTimer=null; }
  },2000);
}

// 当_syncTaskMap检测到有running任务时自动启动估算器
const _origSyncTaskMap=_syncTaskMap;
// 在 _syncTaskMap 后注入：检测到 running 则启动平滑估算
const _syncTaskMapWithSmooth=function(){
  _syncTaskMap();
  const hasRunning=Object.values(_taskMap).some(i=>i.status==='running');
  if(hasRunning) _startSmoothProgress();
  // 记录 running 任务的开始时间
  for(const [did,info] of Object.entries(_taskMap)){
    if(info.status==='running'&&!_taskStartTimes[did]) _taskStartTimes[did]=Date.now();
    if(info.status!=='running') delete _taskStartTimes[did];
  }
};

/* ── P7: 设备任务历史弹出 ── */
async function _showDeviceTaskHistory(did,anchorEl){
  // 关闭已有弹窗
  document.querySelectorAll('.task-history-popup').forEach(el=>el.remove());
  const alias=ALIAS[did]||did.substring(0,8);
  let popup;
  try{
    const tasks=await api('GET',`/tasks?device_id=${encodeURIComponent(did)}&limit=10`);
    if(!tasks.length){showToast(`${alias} 暂无任务记录`,'info');return;}
    popup=document.createElement('div');
    popup.className='task-history-popup';
    const statusIcon={running:'▶',pending:'⏳',completed:'✓',failed:'✗',cancelled:'–'};
    const statusColor={running:'#60a5fa',pending:'#f59e0b',completed:'#22c55e',failed:'#f87171',cancelled:'#6b7280'};
    popup.innerHTML=`
      <div style="font-size:12px;font-weight:700;margin-bottom:8px;color:var(--text)">${alias} 最近任务</div>
      ${tasks.map(t=>{
        const ic=statusIcon[t.status]||'?';
        const cl=statusColor[t.status]||'#9ca3af';
        const lbl=(_taskTypeLabels&&_taskTypeLabels[t.type])||t.type||'任务';
        const dt=t.updated_at?new Date(t.updated_at).toLocaleTimeString('zh',{hour:'2-digit',minute:'2-digit'}):'';
        return `<div class="thp-row"><span style="color:${cl};font-weight:700;min-width:12px">${ic}</span><span class="thp-lbl">${lbl}</span><span class="thp-time">${dt}</span></div>`;
      }).join('')}`;
    // 定位：尝试贴近徽章，若超出视口则反向
    document.body.appendChild(popup);
    if(anchorEl){
      const r=anchorEl.getBoundingClientRect();
      let top=r.bottom+6, left=r.left-popup.offsetWidth+r.width;
      if(top+popup.offsetHeight>window.innerHeight-20) top=r.top-popup.offsetHeight-6;
      if(left<8) left=8;
      popup.style.top=top+'px'; popup.style.left=left+'px';
    }
    // 点击外部关闭
    const close=e=>{if(!popup.contains(e.target)){popup.remove();document.removeEventListener('click',close);}};
    setTimeout(()=>document.addEventListener('click',close),100);
  }catch(e){showToast('加载任务历史失败','error');}
}

/* ── Devices ── */
function _healthTotalNum(did){
  const s=_deviceHealthScores[did];
  if(s==null||s==='') return undefined;
  if(typeof s==='number'&&!isNaN(s)) return s;
  if(typeof s==='object'&&s!=null&&s.total!=null&&!isNaN(Number(s.total))) return Number(s.total);
  return undefined;
}
function _fmtBatteryTemp(bt){
  if(bt==null||bt==='') return '';
  if(typeof bt==='object') return '';
  return String(bt)+'°C';
}
function devCard(d,compact){
  const isOn=d.status==='connected'||d.status==='online';
  const isBusy=d.busy;
  const cls=isOn?(isBusy?'busy':'on'):'off';
  const _ti=_taskMap&&_taskMap[d.device_id];
  const _tl=_ti?(_taskTypeLabels[_ti.type]||_ti.type||''):'';
  const stText=isOn?(isBusy?(_tl?`执行中·${_tl}`:'执行中'):'空闲'):'离线';
  const alias=ALIAS[d.device_id]||'';
  const numVal=parseInt((alias).replace(/[^0-9]/g,''))||0;
  const isUnset=!numVal;
  // Avatar：有编号显示数字，无编号显示"?"
  const avatarLabel=numVal?String(numVal):'?';
  const avatarCls=`dev-avatar ${isUnset?'unset':cls}`;
  const label=alias||d.display_name||d.device_id.substring(0,8);
  const did=d.device_id;
  const didShort=did.substring(0,8);

  const offlineActions=isOn?'':`<div style="padding:5px 12px;background:rgba(239,68,68,.06);border-top:1px solid rgba(239,68,68,.12);display:flex;gap:4px;align-items:center">
    <span style="font-size:10px;color:#f87171;flex:1">&#9888; 离线</span>
    <button class="dev-btn" onclick="event.stopPropagation();diagnoseDev('${did}')" style="font-size:9px;padding:2px 8px;color:#fbbf24;border-color:#fbbf24">诊断</button>
    <button class="dev-btn" onclick="event.stopPropagation();fixDev('${did}','reconnect')" style="font-size:9px;padding:2px 8px;color:#22c55e;border-color:#22c55e">修复</button>
  </div>`;

  const perf=_devicePerfCache[did]||{};
  const batLvl=perf.battery_level;
  const memPct=perf.mem_usage;
  const score=_healthTotalNum(did);
  const wkName=_getDeviceWorker(did);
  const wkColor=_getWorkerColor(wkName);
  const wkBadge=wkName!=='本机'?`<span style="flex-shrink:0;font-size:9px;font-weight:700;padding:1px 5px;border-radius:3px;background:${wkColor};color:#fff;white-space:nowrap">${wkName}</span>`:'';
  const dualBadge=d.dual_transport?`<span style="flex-shrink:0;font-size:8px;font-weight:600;padding:1px 5px;border-radius:3px;background:rgba(59,130,246,.2);color:#93c5fd;white-space:nowrap" title="USB+无线双通道（API 已合并，后台可故障转移）">双通道</span>`:'';
  const batColor=batLvl>50?'#22c55e':batLvl>20?'#eab308':'#ef4444';
  const tempStr=_fmtBatteryTemp(perf.battery_temp);
  const lowBat=batLvl!=null&&batLvl<20;
  const highMem=memPct!=null&&memPct>88;
  const resourceWarn=(lowBat||highMem)&&isOn;
  const warnTip=lowBat&&highMem?`⚡ 低电(${batLvl}%) 内存(${memPct}%)`:
                lowBat?`⚡ 低电量(${batLvl}%)`:
                `💾 内存高(${memPct}%)`;
  const perfStrip=isOn?`<div style="padding:4px 16px 6px;display:flex;gap:6px;align-items:center;font-size:9px;color:var(--text-muted)">
    ${batLvl!==undefined?`<div style="flex:1"><div style="display:flex;justify-content:space-between;margin-bottom:2px"><span style="color:${batColor}">&#128267; ${batLvl}%</span>${perf.charging?'<span style="color:#eab308">&#9889;</span>':''}</div><div style="height:3px;background:var(--bg-input);border-radius:2px;overflow:hidden"><div style="height:100%;width:${batLvl}%;background:${batColor};border-radius:2px"></div></div></div>`:''}
    ${memPct!==undefined?`<span>&#128190; ${memPct}%</span>`:''}
    ${tempStr?`<span>${tempStr}</span>`:''}
    ${resourceWarn?`<span title="${warnTip}" style="margin-left:auto;padding:1px 5px;border-radius:4px;font-size:8px;font-weight:600;background:rgba(239,68,68,.15);color:#ef4444;cursor:default">⚠</span>`:
      score!==undefined?`<span style="margin-left:auto;padding:1px 5px;border-radius:4px;font-size:8px;font-weight:600;background:${score>=80?'rgba(34,197,94,.15)':score>=50?'rgba(234,179,8,.15)':'rgba(239,68,68,.15)'};color:${score>=80?'#22c55e':score>=50?'#eab308':'#ef4444'}">${score}</span>`:''}
  </div>`:'';

  // 设备名称区：alias标注 + worker标记
  const nameSpan=`${d.display_name||''}${alias?' <span style="color:var(--text-muted);font-size:12px">('+alias+')</span>':''}`;

  // P8: battery bar from _perfMap (lightweight, refreshed independently)
  const _bat = _perfMap[d.device_id];
  const _batHtml = (_bat && _bat.battery > 0) ? `<div class="dev-bat-bar-wrap"><div class="dev-bat-bar" style="width:${_bat.battery}%;background:${_bat.battery>50?'var(--green)':_bat.battery>20?'#eab308':'var(--red)'}"></div><span class="dev-bat-pct">${_bat.battery}%</span></div>` : '';

  return `<div class="dev-card" style="position:relative"
    onclick="_swapMode?_swapCardClick(event,'${did}'):openScreenModal('${did}')"
    ${resourceWarn?`title="${warnTip}"`:''}>
    <div class="task-status-strip" id="ts-${didShort}"></div>
    <span class="task-badge" id="tb-${didShort}"></span>
    ${isUnset?'<span class="unset-badge">未编号</span>':''}
    <div class="dev-card-top">
      <div class="${avatarCls}"
           id="av-${didShort}"
           title="点击设置编号"
           onclick="event.stopPropagation();_avatarEdit(event,this,'${did}')">${avatarLabel}</div>
      <div class="dev-info">
        <div class="dev-name" style="display:flex;align-items:center;gap:4px;overflow:hidden">
          <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${nameSpan}</span>
          ${dualBadge}
          ${wkBadge}
        </div>
        <div class="dev-serial">${did.substring(0,12)}</div>
        ${_batHtml}
      </div>
    </div>${perfStrip}
    <div class="dev-card-bottom">
      <div class="dev-status">
        <span class="status-dot ${isOn?(isBusy?'warn':'ok'):'err'}" style="width:6px;height:6px"></span> ${stText}
        <span id="stream-ind-${didShort}" style="display:none;margin-left:4px;font-size:9px;padding:1px 5px;border-radius:3px;background:rgba(34,197,94,.15);color:#22c55e" title="实时流活跃">📡</span>
        <span id="vpn-ind-${didShort}" style="margin-left:4px;font-size:9px;padding:1px 5px;border-radius:3px;background:var(--bg-input);color:var(--text-muted);cursor:pointer" title="点击检查/修复VPN" onclick="event.stopPropagation();_vpnFixDevice('${did}')">VPN?</span>
      </div>
      <div class="dev-actions">
        <button class="dev-btn screen" onclick="event.stopPropagation();openScreenModal('${did}')">&#128247; 屏幕</button>
        ${(()=>{
          const wpN = window.WP_NUM&&window.WP_NUM[did];
          const wpOk = numVal&&wpN===numVal;
          const wpOutdated = numVal&&wpN&&wpN!==numVal;
          const wpTitle = !numVal?'请先设置编号':wpOk?`壁纸最新 #${String(numVal).padStart(2,'0')}`:wpOutdated?`壁纸已过期 (#${wpN}→#${numVal})，点击更新`:'未知状态，点击部署';
          const cls = `dev-btn wp${wpOk?' wp-ok':wpOutdated?' wp-outdated':''}`;
          const dot = wpOutdated?'<span class="wp-dot outdated"></span>':wpOk?'<span class="wp-dot ok"></span>':'';
          return `<button class="${cls}" id="wp-${didShort}" onclick="event.stopPropagation();_deployWallpaperSingle('${did}')" title="${wpTitle}">🖼${dot}</button>`;
        })()}
        ${isUnset&&!compact?`<button class="dev-btn" style="color:#f59e0b;border-color:#f59e0b;font-weight:600" onclick="event.stopPropagation();_avatarEdit(event,document.getElementById('av-${didShort}'),'${did}')" title="点击分配编号">📋 编号</button>`:''}
        ${compact?'':`<button class="dev-btn" onclick="event.stopPropagation();quickCmdDev('${alias||did.substring(0,4)}','养号30分钟')">养号</button>`}
      </div>
    </div>
    ${offlineActions}
  </div>`;
}

/* ── 头像内联编号编辑 ── */
function _avatarEdit(e, el, did){
  e.stopPropagation();
  if(el.classList.contains('editing')) return;
  el.classList.add('editing');
  const cur=parseInt((ALIAS[did]||'').replace(/[^0-9]/g,''))||'';
  el.innerHTML=`<input class="num-inp" type="number" min="1" max="999"
    value="${cur}" placeholder="编号"
    onclick="event.stopPropagation()"
    onkeydown="_avatarKey(event,'${did}')"
    onblur="_avatarBlur(this,'${did}')">`;
  const inp=el.querySelector('input');
  inp.focus();
  inp.select();
}
function _avatarKey(e,did){
  if(e.key==='Enter'){e.preventDefault();_avatarCommit(e.target,did);}
  if(e.key==='Escape'){e.stopPropagation();renderScreens();}
}
function _avatarBlur(inp,did){
  // blur 时如果值没变就取消，否则提交
  const cur=parseInt((ALIAS[did]||'').replace(/[^0-9]/g,''))||0;
  const n=parseInt(inp.value)||0;
  if(!n||n===cur){renderScreens();return;}
  _avatarCommit(inp,did);
}
async function _avatarCommit(inp,did){
  const n=parseInt(inp.value)||0;
  if(!n||n<=0){renderScreens();return;}
  const cur=parseInt((ALIAS[did]||'').replace(/[^0-9]/g,''))||0;
  if(n===cur){renderScreens();return;}
  // 乐观UI：立即显示新编号，让用户感知到响应
  const el=document.getElementById('av-'+did.substring(0,8));
  if(el){el.classList.remove('unset','editing');el.innerHTML=String(n);}
  try{
    const r=await api('PUT',`/devices/${did}/number`,{number:n,deploy_wallpaper:true});
    const numStr='#'+String(n).padStart(2,'0');
    if(r.swapped_with){
      const swapStr='#'+String(r.swapped_with.number).padStart(2,'0');
      showToast(`${numStr} ↔ ${swapStr} 已交换，壁纸部署中...`,'success');
    }else{
      showToast(`${numStr} 设置成功，壁纸部署中...`,'success');
    }
    await loadAliases();
    renderScreens();
    _updateUnsetCount(); // 更新顶部"未编号"计数
  }catch(ex){
    renderScreens(); // 回滚乐观UI
    const msg=ex.message||'';
    if(msg.includes('404')||msg.includes('不存在'))
      showToast('设置失败：Worker未响应，请检查服务状态','error');
    else showToast('设置失败: '+msg,'error');
  }
}

/* ── 单台设备壁纸部署 ── */
async function _deployWallpaperSingle(did){
  const num=parseInt((ALIAS[did]||'').replace(/[^0-9]/g,''))||0;
  if(!num){showToast('请先点击头像设置编号','warn');return;}
  const btn=document.getElementById('wp-'+did.substring(0,8));
  if(btn){btn.innerHTML='⏳';btn.disabled=true;}
  try{
    const r=await api('POST',`/devices/${did}/wallpaper`,{number:num});
    if(r.ok){
      window.WP_NUM=window.WP_NUM||{};
      window.WP_NUM[did]=num;  // 乐观更新壁纸状态
      showToast(`#${String(num).padStart(2,'0')} 壁纸已部署`,'success');
      renderScreens();  // 重渲卡片更新状态指示
    }else{
      showToast('壁纸部署失败','warn');
    }
  }catch(ex){
    showToast('壁纸部署失败: '+(ex.message||''),'error');
  }finally{
    if(btn){btn.innerHTML='🖼';btn.disabled=false;}
  }
}

/* ── 顶部工具栏"未编号"计数徽章 + 自动编号提示 ── */
function _updateUnsetCount(){
  const cnt=allDevices.filter(d=>!parseInt((ALIAS[d.device_id]||'').replace(/[^0-9]/g,''))).length;
  const el=document.getElementById('unset-count-badge');
  if(el){
    if(cnt>0){el.textContent=cnt;el.style.display='inline-block';}
    else el.style.display='none';
  }
  // 若有未编号设备 + 有段配置 → 显示一键自动编号按钮
  const hasRanges = Object.keys(window._globalRanges||{}).length>0;
  const autoBtn = document.getElementById('auto-assign-btn');
  if(autoBtn){
    autoBtn.style.display = (cnt>0&&hasRanges) ? 'inline-flex' : 'none';
    if(cnt>0&&hasRanges) autoBtn.textContent = `⚡ 自动编号 (${cnt})`;
  }
  // 更新壁纸状态徽章
  const outdatedCnt = allDevices.filter(d=>{
    const num = parseInt((ALIAS[d.device_id]||'').replace(/[^0-9]/g,''))||0;
    const wpN = window.WP_NUM&&window.WP_NUM[d.device_id];
    return num && wpN !== num;
  }).length;
  const wpBadge = document.getElementById('wp-outdated-badge');
  if(wpBadge){
    wpBadge.textContent = outdatedCnt||'';
    wpBadge.style.display = outdatedCnt>0 ? 'inline-block' : 'none';
  }
}

/* ── 补缺壁纸：仅为 wallpaper_number ≠ number 的设备部署 ── */
let _wpPollTimer = null;

async function deployOutdatedWallpapers(){
  const btn=document.getElementById('wp-outdated-btn');
  if(btn){btn.disabled=true;btn.innerHTML='⏳ 准备中...';}
  try{
    const r=await api('POST','/devices/wallpaper/deploy-outdated');
    if(r.total===0){
      showToast('所有设备壁纸均为最新 ✓','success');
    }else if(r.job_id){
      showToast(`开始为 ${r.total} 台设备补充壁纸`,'success');
      _wpStartProgress(r.job_id, r.total);
    }
  }catch(ex){ showToast('补缺壁纸失败: '+ex.message,'error'); }
  finally{
    if(btn){btn.disabled=false;btn.innerHTML='🖼 补缺壁纸 <span id="wp-outdated-badge" style="display:none;background:#f59e0b;color:#fff;font-size:9px;font-weight:700;padding:0 5px;border-radius:3px;margin-left:4px"></span>';}
    _updateUnsetCount();
  }
}

function _wpStartProgress(jobId, total){
  if(_wpPollTimer) clearInterval(_wpPollTimer);
  // 创建或重置进度面板
  let panel = document.getElementById('wp-progress-panel');
  if(!panel){
    panel = document.createElement('div');
    panel.id = 'wp-progress-panel';
    panel.className = 'wp-progress-panel';
    document.body.appendChild(panel);
  }
  panel.innerHTML = `
    <div class="wpp-header">
      <span class="wpp-title">🖼 壁纸部署进度</span>
      <button class="wpp-close" onclick="clearInterval(_wpPollTimer);this.closest('#wp-progress-panel').remove()">×</button>
    </div>
    <div class="wpp-bar-wrap"><div class="wpp-bar" id="wpp-bar"></div></div>
    <div class="wpp-stats" id="wpp-stats">正在启动...</div>`;

  let pollFails = 0;
  _wpPollTimer = setInterval(async ()=>{
    try{
      const s = await api('GET', `/devices/wallpaper/deploy-status/${jobId}`, null, 10000);
      if(s.error){ clearInterval(_wpPollTimer); return; }
      const processed = s.done + s.failed;
      const pct = total > 0 ? Math.round(processed / total * 100) : 0;
      const bar = document.getElementById('wpp-bar');
      const stats = document.getElementById('wpp-stats');
      if(bar) bar.style.width = pct + '%';
      if(stats) stats.textContent = `${processed}/${total} · ${s.done} 成功${s.failed?' · '+s.failed+' 失败':''} (${pct}%)`;
      if(!s.running){
        clearInterval(_wpPollTimer);
        if(bar){ bar.style.width='100%'; bar.style.background='#22c55e'; }
        if(stats) stats.textContent = `部署完成 ✓  ${s.done} 成功${s.failed?' · '+s.failed+' 失败':''}`;
        await loadAliases(); renderScreens(); _updateUnsetCount();
        setTimeout(()=>{ const p=document.getElementById('wp-progress-panel'); if(p) p.remove(); }, 4000);
      }
      pollFails = 0;
    }catch(e){
      pollFails++;
      if(pollFails > 5) clearInterval(_wpPollTimer);
    }
  }, 1500);
}

/* ── 一键自动编号（无需打开面板，直接按段分配） ── */
async function _quickAutoAssign(){
  const cnt=allDevices.filter(d=>!parseInt((ALIAS[d.device_id]||'').replace(/[^0-9]/g,''))).length;
  if(!cnt){showToast('所有设备已有编号');return;}
  const btn=document.getElementById('auto-assign-btn');
  if(btn){btn.disabled=true;btn.textContent='⏳ 分配中...';}
  try{
    const r=await api('POST','/devices/auto-assign-segments');
    if(r.assigned>0){
      showToast(`已为 ${r.assigned} 台设备自动分配编号`,'success');
      await loadAliases();
      renderScreens();
      _updateUnsetCount();
    }else{
      showToast(r.message||'所有设备已有编号');
    }
  }catch(ex){ showToast('自动编号失败: '+ex.message,'error'); }
  finally{
    if(btn){btn.disabled=false;}
    _updateUnsetCount();
  }
}

let _conflictData=null; // 缓存冲突数据
async function _refreshConflictBadge(){
  try{
    const r=await api('GET','/devices/conflicts');
    _conflictData=r;
    const btn=document.getElementById('conflict-fix-btn');
    if(!btn)return;
    const n=r.conflict_count||0;
    const s=r.stale_local_count||0;
    const total=n+(s>0?1:0); // 合并展示
    if(total>0){
      btn.style.display='inline-flex';
      btn.textContent=`⚠ ${n} 个重复编号`;
      btn.title=`${n} 组重复编号${s>0?`，${s} 条历史遗留数据`:''}，点击修复`;
    }else{
      btn.style.display='none';
    }
  }catch(e){}
}

/* 一键修复冲突（直接执行，不弹窗） */
async function fixAllConflicts(){
  const btn=document.getElementById('conflict-fix-btn');
  if(btn){btn.textContent='⏳ 修复中...';btn.disabled=true;}
  try{
    const r=await api('POST','/devices/fix-conflicts',{deploy_wallpaper:true});
    showToast(r.message||'修复完成','success');
    await loadAliases();
    renderScreens();
    _updateUnsetCount();
    await _refreshConflictBadge();
    // 如果面板开着，刷新面板
    if(document.getElementById('num-mgr-overlay')) _nmRefresh();
  }catch(ex){
    showToast('修复失败: '+ex.message,'error');
    if(btn){btn.textContent='⚠ 重复编号';btn.disabled=false;}
  }
}

let _devPage=0;
const _devPageSize=50;
let _filteredDevices=[];
let _activeWorkerFilter='';

function _renderWorkerTabs(){
  const bar=document.getElementById('worker-tab-bar');
  if(!bar) return;
  // Gather unique worker names: local devices → "本机", cluster devices → host_name
  const workers=new Set();
  allDevices.forEach(d=>{
    if(d._isCluster && d.host_name) workers.add(d.host_name);
    else workers.add('本机');
  });
  if(workers.size<=1){bar.innerHTML='';return;}
  let html='<button class="wtab'+(''===_activeWorkerFilter?' active':'')+'" onclick="_setWorkerFilter(\'\')">全部</button>';
  workers.forEach(w=>{
    html+=`<button class="wtab${w===_activeWorkerFilter?' active':''}" onclick="_setWorkerFilter('${w.replace(/'/g,"\\'")}')">${w}</button>`;
  });
  bar.innerHTML=html;
}

function _setWorkerFilter(name){
  _activeWorkerFilter=name;
  _renderWorkerTabs();
  filterDeviceGrid();
}

function _sortDevices(list){
  return [...list].sort((a,b)=>{
    const na=parseInt((ALIAS[a.device_id]||'').replace(/[^0-9]/g,''))||0;
    const nb=parseInt((ALIAS[b.device_id]||'').replace(/[^0-9]/g,''))||0;
    // 未编号设备排最后
    if(!na && nb) return 1;
    if(na && !nb) return -1;
    if(na!==nb) return na-nb;
    // 编号相同时在线优先
    const ao=(a.status==='connected'||a.status==='online')?0:1;
    const bo=(b.status==='connected'||b.status==='online')?0:1;
    return ao-bo;
  });
}

function _renderHealthBar(){
  if(!allDevices||!allDevices.length) return;
  let bar=document.getElementById('dev-health-bar');
  if(!bar){
    bar=document.createElement('div');
    bar.id='dev-health-bar';
    bar.style.cssText='display:flex;align-items:center;gap:8px;padding:5px 12px;background:var(--bg-card);border-radius:6px;border:1px solid var(--border);font-size:11px;flex-wrap:wrap;margin-bottom:8px';
    const grid=document.getElementById('dev-grid');
    if(grid&&grid.parentNode) grid.parentNode.insertBefore(bar,grid);
    else return;
  }
  // 统计基于真实 allDevices（ADB实时扫描），而非 aliases 历史记录
  let online=0,offline=0,unset=0,wpIssue=0;
  allDevices.forEach(d=>{
    const alias=ALIAS[d.device_id]||'';
    const num=parseInt(alias.replace(/[^0-9]/g,''))||0;
    const isOn=d.status==='connected'||d.status==='online';
    if(!num){unset++;return;}
    if(!isOn){offline++;return;}
    const wpN=window.WP_NUM&&window.WP_NUM[d.device_id];
    if(wpN&&wpN!==num){wpIssue++;return;}
    online++;
  });
  const total=allDevices.length;
  const parts=[`<span style="color:var(--text-muted)">共 <b style="color:var(--text)">${total}</b> 台</span>`];
  if(online) parts.push(`<span style="color:#22c55e;font-weight:600">✅ 在线 ${online}台</span>`);
  if(offline) parts.push(`<span style="color:#6b7280;font-weight:600" title="已编号但当前离线">⚫ 离线 ${offline}台</span>`);
  if(unset) parts.push(`<span style="color:#f59e0b;font-weight:600;cursor:pointer" title="点击前往编号">🟡 未编号 ${unset}台</span>`);
  if(wpIssue) parts.push(`<span style="color:#ef4444;font-weight:600" title="壁纸编号与设备编号不匹配">🔴 壁纸异常 ${wpIssue}台</span>`);
  bar.innerHTML=parts.join('<span style="color:var(--text-muted);margin:0 6px">|</span>');
}

function filterDeviceGrid(){
  const q=(document.getElementById('dev-search').value||'').toLowerCase();
  const sf=document.getElementById('dev-status-filter').value;
  const gf=document.getElementById('dev-group-filter')?.value||'';
  const groupDevIds=gf?new Set((_groupsData.find(g=>g.id===gf)||{}).devices||[]):null;
  _filteredDevices=allDevices.filter(d=>{
    const isOn=d.status==='connected'||d.status==='online';
    if(sf==='online'&&!isOn)return false;
    if(sf==='offline'&&isOn)return false;
    if(sf==='busy'&&!d.busy)return false;
    if(groupDevIds&&!groupDevIds.has(d.device_id))return false;
    if(_activeWorkerFilter){
      if(_activeWorkerFilter==='本机'){if(d._isCluster)return false;}
      else{if(!d._isCluster||d.host_name!==_activeWorkerFilter)return false;}
    }
    if(q){
      const alias=(ALIAS[d.device_id]||'').toLowerCase();
      const name=(d.display_name||'').toLowerCase();
      const id=d.device_id.toLowerCase();
      if(!alias.includes(q)&&!name.includes(q)&&!id.includes(q))return false;
    }
    return true;
  });
  _filteredDevices=_sortDevices(_filteredDevices);
  _devPage=0;
  _renderDevPage();
}

function _renderDevPage(){
  const start=_devPage*_devPageSize;
  const page=_filteredDevices.slice(start,start+_devPageSize);
  document.getElementById('dev-grid').innerHTML=page.length?page.map(d=>devCard(d,false)).join(''):'<div style="color:var(--text-muted);padding:20px;text-align:center">无匹配设备</div>';
  // 重新应用任务状态条（DOM 重建后 strip 元素已重置）
  if(Object.keys(_taskMap).length) setTimeout(_updateTaskStrips, 0);
  const total=_filteredDevices.length;
  const pages=Math.ceil(total/_devPageSize);
  document.getElementById('dev-count-info').textContent=`显示 ${Math.min(start+1,total)}-${Math.min(start+_devPageSize,total)} / 共 ${total} 台`;
  const pager=document.getElementById('dev-pager');
  if(pages<=1){pager.innerHTML='';return;}
  let btns='';
  for(let i=0;i<pages;i++){
    btns+=`<button class="sb-btn2" style="font-size:10px;padding:3px 10px;${i===_devPage?'background:var(--accent);color:#111':''}" onclick="_devPage=${i};_renderDevPage()">${i+1}</button>`;
  }
  pager.innerHTML=btns;
}

function setDevGridSize(size){
  const g=document.getElementById('dev-grid');
  if(size==='small')g.style.gridTemplateColumns='repeat(auto-fill,minmax(180px,1fr))';
  else if(size==='large')g.style.gridTemplateColumns='repeat(auto-fill,minmax(320px,1fr))';
  else g.style.gridTemplateColumns='repeat(auto-fill,minmax(240px,1fr))';
  localStorage.setItem('oc-grid-size',size);
}

/** 总览/设备页：本机 vs 集群 分解（与 loadDevices 合并后列表一致） */
function _refreshDeviceBreakdownUI(){
  const b=window._deviceBreakdown||{};
  const loc=b.local|0, clu=b.cluster|0;
  let line='';
  if(clu>0) line=`本机 ${loc} · 集群 ${clu}`;
  else line=`本机 ${loc}`;
  const s1=document.getElementById('s-total-breakdown');
  if(s1) s1.textContent=line;
  const s2=document.getElementById('d-total-breakdown');
  if(s2) s2.textContent=line;
  const sh=document.getElementById('scr-device-breakdown');
  if(sh) sh.textContent=line?' · '+line:'';
}

/** 总览 + 设备页：本机 ADB/别名文件告警（GET /devices/meta） */
async function _refreshDeviceMetaAlerts(){
  const ov=document.getElementById('ov-device-alerts');
  const dev=document.getElementById('dev-page-alerts');
  if(!ov&&!dev) return;
  try{
    const m=await api('GET','/devices/meta').catch(()=>null);
    if(!m||!m.alerts||!m.alerts.has_warning){
      if(ov){ov.style.display='none';ov.innerHTML='';}
      if(dev){dev.style.display='none';dev.innerHTML='';}
      return;
    }
    const parts=[];
    (m.alerts.items||[]).forEach(it=>{
      const msg=(it.message||'').replace(/&/g,'&amp;').replace(/</g,'&lt;');
      parts.push(`<div style="margin-top:4px"><b style="color:#fca5a5">&#9888;</b> ${msg}</div>`);
    });
    const pv=(m.adb_problem_preview||[]).map(x=>{
      const id=(x.device_id||'').substring(0,14);
      return `<code style="font-size:10px">${id}</code> <span style="opacity:.85">${(x.status||'').replace(/</g,'&lt;')}</span>`;
    }).join(' · ');
    if(pv) parts.push(`<div style="margin-top:6px;font-size:11px;color:var(--text-dim)">USB 异常预览: ${pv}</div>`);
    const onDevPage=document.getElementById('page-devices')&&document.getElementById('page-devices').classList.contains('active');
    const actions=[];
    if((m.breakdown||{}).stale_alias_keys>0){
      actions.push(`<button type="button" class="qa-btn" style="margin-top:8px;padding:4px 10px;font-size:11px;border-color:#f59e0b;color:#fbbf24" onclick="_pruneOrphanAliases(false)">修剪别名孤儿</button>`);
      actions.push(`<button type="button" class="qa-btn" style="margin-top:8px;margin-left:6px;padding:4px 10px;font-size:11px" onclick="_pruneOrphanAliases(true)">仅预览修剪</button>`);
    }
    if(!onDevPage){
      actions.push(`<button type="button" class="qa-btn" style="margin-top:8px;margin-left:6px;padding:4px 10px;font-size:11px" onclick="navigateToPage('devices')">打开设备管理</button>`);
    }else{
      actions.push(`<button type="button" class="qa-btn" style="margin-top:8px;margin-left:6px;padding:4px 10px;font-size:11px" onclick="_refreshDeviceMetaAlerts()">刷新告警</button>`);
    }
    const html=`<div style="font-weight:600;color:#fecaca">设备配置告警（本机 ADB / 别名）</div>${parts.join('')}${actions.join('')}`;
    if(ov){ov.innerHTML=html;ov.style.display='block';}
    if(dev){dev.innerHTML=html;dev.style.display='block';}
  }catch(e){
    if(ov){ov.style.display='none';}
    if(dev){dev.style.display='none';}
  }
}

async function _pruneOrphanAliases(dryRun){
  if(!dryRun&&!(await ocDialog({title:'清理孤儿别名',message:'确认从 device_aliases.json 中删除「当前 ADB 列表中不存在」的条目？<br>将先备份到 data/backups/。',type:'warning',confirmText:'清理',cancelText:'取消'}))) return;
  try{
    const r=await api('POST','/devices/aliases/prune-orphans',{dry_run:!!dryRun,backup:true});
    if(dryRun){
      showToast(`将删除 ${r.would_remove||0} 条孤儿别名（dry_run）`,'success');
      return;
    }
    const bp=r.backup&&r.backup.backup_dir?(' 备份: '+r.backup.backup_dir):'';
    showToast(`已修剪 ${r.removed||0} 条。`+bp,'success');
    if(typeof loadDevices==='function') await loadDevices();
    await _refreshDeviceMetaAlerts();
  }catch(e){showToast('失败: '+e.message,'warn');}
}

async function loadDevices(){
  try{
    allDevices=await api('GET','/devices');
    try{
      const cdResp=await api('GET','/cluster/devices');
      const cd=Array.isArray(cdResp)?cdResp:(cdResp&&cdResp.devices?cdResp.devices:[]);
      if(cd.length){
        const localIds=new Set(allDevices.map(d=>d.device_id));
        cd.forEach(d=>{
          if(!localIds.has(d.device_id)){
            d._isCluster=true;
            if(!d.status&&d.health_score) d.status=d.health_score>50?'connected':'disconnected';
            allDevices.push(d);
          }
        });
      }
    }catch(e){}
    window._deviceBreakdownSchema=1;
    const _loc=allDevices.filter(d=>!d._isCluster).length;
    const _clu=allDevices.filter(d=>d._isCluster).length;
    window._deviceBreakdown={total:allDevices.length,local:_loc,cluster:_clu};
    if(typeof _refreshDeviceBreakdownUI==='function') _refreshDeviceBreakdownUI();
    let online=0;allDevices.forEach(d=>{if(d.status==='connected'||d.status==='online')online++;});
    document.getElementById('s-online').textContent=online;
    document.getElementById('s-total').textContent=allDevices.length;
    try{document.getElementById('d-online').textContent=online;document.getElementById('d-total').textContent=allDevices.length;}catch(e){}

    const sorted=_sortDevices(allDevices);
    _buildDeviceWorkerMap(allDevices);
    document.getElementById('ov-device-grid').innerHTML=sorted.slice(0,20).map(d=>devCard(d,true)).join('');
    _filteredDevices=sorted;
    _renderDevPage();
    setTimeout(_refreshStreamIndicators,2000);
    _renderWorkerTabs();
    try{_renderHealthBar();}catch(e){}
    try{renderDevicePieChart();renderBatteryChart();}catch(e){}
    // Load battery/memory perf data from cluster workers (coordinator has no local ADB devices)
    api('GET','/analytics/device-perf').then(r=>{
      if(r&&r.devices){
        Object.assign(_devicePerfCache,r.devices);
        try{renderBatteryChart();}catch(e){}
        // Update battery stat cards (loadHealth() ran before this data was available)
        const _batById={};
        (allDevices||[]).forEach(d=>{if(d.battery_level!=null)_batById[d.device_id]=d.battery_level;});
        Object.entries(_devicePerfCache).forEach(([did,p])=>{if(p.battery_level!=null)_batById[did]=p.battery_level;});
        const bats=Object.values(_batById);
        const pEntries=Object.values(_devicePerfCache);
        const mems=pEntries.filter(p=>p.mem_usage!==undefined).map(p=>p.mem_usage);
        const avgBat=document.getElementById('s-avg-bat');
        if(avgBat) avgBat.textContent=bats.length?Math.round(bats.reduce((a,b)=>a+b,0)/bats.length)+'%':'-';
        const lowBat=document.getElementById('s-low-bat');
        if(lowBat) lowBat.textContent=bats.filter(b=>b<20).length;
        const avgMem=document.getElementById('s-avg-mem');
        if(avgMem&&mems.length) avgMem.textContent=Math.round(mems.reduce((a,b)=>a+b,0)/mems.length)+'%';
        const highMem=document.getElementById('s-high-mem');
        if(highMem&&mems.length) highMem.textContent=mems.filter(m=>m>80).length;
      }
    }).catch(()=>{});
    try{_loadGroupsForFilter();}catch(e){}
    const savedSize=localStorage.getItem('oc-grid-size');
    if(savedSize)setDevGridSize(savedSize);
    // 刷新设备卡片上的 VPN 状态指示
    _updateDeviceVpnIndicators();
    // 今日任务推荐
    _updateTodayTip(online, allDevices.length);
    // 更新未编号计数徽章 + 冲突检测
    _updateUnsetCount();
    _refreshConflictBadge();
    try{await _refreshDeviceMetaAlerts();}catch(e){}
  }catch(e){console.error('loadDevices failed:',e);document.getElementById('ov-device-grid').innerHTML='<div style="color:var(--text-muted)">加载失败</div>';}
}

function _refreshStreamIndicators(){
  (allDevices||[]).forEach(d=>{
    const prefix=d._isCluster?'/cluster':'';
    api('GET',`${prefix}/devices/${d.device_id}/stream/stats`,null,3000).then(r=>{
      if(r&&r.active){
        const el=document.getElementById('stream-ind-'+d.device_id.substring(0,8));
        if(el) el.style.display='inline';
      }
    }).catch(()=>{});
  });
}

// 国家代码→旗帜 emoji 映射
const _countryFlags={italy:'🇮🇹',us:'🇺🇸',usa:'🇺🇸',uk:'🇬🇧',germany:'🇩🇪',france:'🇫🇷',japan:'🇯🇵',korea:'🇰🇷',brazil:'🇧🇷',canada:'🇨🇦',australia:'🇦🇺',singapore:'🇸🇬',india:'🇮🇳',russia:'🇷🇺',spain:'🇪🇸',netherlands:'🇳🇱',turkey:'🇹🇷',thailand:'🇹🇭',vietnam:'🇻🇳',philippines:'🇵🇭',indonesia:'🇮🇩',malaysia:'🇲🇾',mexico:'🇲🇽',argentina:'🇦🇷',colombia:'🇨🇴',chile:'🇨🇱',poland:'🇵🇱',romania:'🇷🇴',ukraine:'🇺🇦',sweden:'🇸🇪',norway:'🇳🇴',finland:'🇫🇮',denmark:'🇩🇰',ireland:'🇮🇪',portugal:'🇵🇹',switzerland:'🇨🇭',austria:'🇦🇹',belgium:'🇧🇪',czech:'🇨🇿',greece:'🇬🇷',hungary:'🇭🇺',israel:'🇮🇱',uae:'🇦🇪',egypt:'🇪🇬',southafrica:'🇿🇦',hongkong:'🇭🇰',taiwan:'🇹🇼'};
function _getFlag(country){
  if(!country) return '';
  const c=country.toLowerCase().replace(/[^a-z]/g,'');
  return _countryFlags[c]||('🌐');
}

async function _updateDeviceVpnIndicators(){
  try{
    const d=await api('GET','/vpn/status');
    const devs=d.devices||[];
    for(const v of devs){
      const shortId=v.device_id?.substring(0,8)||'';
      const el=document.getElementById('vpn-ind-'+shortId);
      if(!el) continue;
      if(v.connected){
        const flag=_getFlag(v.country);
        el.textContent=flag+' VPN ✓';
        el.style.background='rgba(34,197,94,.15)';
        el.style.color='#22c55e';
        el.title='VPN 已连接'+(v.ip?' · IP: '+v.ip:'')+(v.country?' · '+v.country:'');
      }else{
        el.textContent='VPN ✗';
        el.style.background='rgba(239,68,68,.15)';
        el.style.color='#ef4444';
        el.title='VPN 未连接，点击修复';
      }
    }
  }catch(e){}
}

let _todayTipCmd='';
function _updateTodayTip(online, total){
  const tip=document.getElementById('dev-tip-text');
  const btn=document.getElementById('dev-tip-btn');
  if(!tip||!btn) return;
  // 简单的推荐逻辑
  const hour=new Date().getHours();
  if(online===0){
    tip.textContent='没有在线设备。请检查 USB 连接和 Worker 状态。';
    btn.style.display='none'; _todayTipCmd=''; return;
  }
  btn.style.display='';
  if(hour<10){
    tip.textContent=`早间养号 — ${online} 台设备可用，建议先养号30分钟提升账号权重`;
    _todayTipCmd='所有手机养号30分钟'; btn.textContent='&#127793; 开始养号';
  }else if(hour<14){
    tip.textContent=`上午活跃期 — 适合关注目标用户，${online} 台设备在线`;
    _todayTipCmd='关注意大利用户20人'; btn.textContent='&#128101; 开始关注';
  }else if(hour<18){
    tip.textContent=`下午转化期 — 检查收件箱回复消息，抓住回关用户`;
    _todayTipCmd='检查所有收件箱'; btn.textContent='&#128229; 查收件箱';
  }else{
    tip.textContent=`晚间维护 — 建议刷视频保持活跃，${online} 台设备在线`;
    _todayTipCmd='所有手机刷视频15分钟'; btn.textContent='&#128250; 刷视频';
  }
}
function _execTodayTip(){
  if(!_todayTipCmd){showToast('没有可执行的推荐');return;}
  if(typeof _aiQuickSet==='function'){
    _aiQuickSet(_todayTipCmd);
    navigateToPage('chat');
  }else{
    showToast('跳转到 AI 指令页面执行: '+_todayTipCmd);
    navigateToPage('chat');
  }
}

async function _cleanupGhosts(){
  const escAttr=(s)=>String(s||'').replace(/&/g,'&amp;').replace(/"/g,'&quot;').replace(/</g,'&lt;');
  const pick=await new Promise((resolve)=>{
    const m=document.createElement('div');
    m.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:12000;display:flex;align-items:center;justify-content:center;padding:16px';
    const ageMin=parseInt(localStorage.getItem('cleanup_max_age_minutes')||'1440',10)||1440;
    m.innerHTML=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;max-width:520px;width:100%;max-height:90vh;overflow:auto;box-shadow:0 8px 32px rgba(0,0,0,.4)">
      <div style="font-weight:600;margin-bottom:8px">清理离线幽灵</div>
      <p style="font-size:12px;color:var(--text-dim);line-height:1.45;margin:0 0 12px">仅移除<strong>已离线</strong>且超过下方阈值的配置记录；默认会先备份 <code>devices.yaml</code> 与 <code>device_aliases.json</code>。可<strong>勾选子集</strong>只删部分；全选等价于未指定白名单。集群主控会转发到各 Worker。</p>
      <label style="font-size:12px;display:block;margin-bottom:8px">离线超过（分钟）</label>
      <input id="cg-age" type="number" min="1" value="${ageMin}" style="width:100%;padding:8px;border-radius:8px;border:1px solid var(--border);background:var(--bg-input);color:var(--text-main);margin-bottom:8px"/>
      <div id="cg-summary" style="font-size:11px;color:var(--text-muted);margin-bottom:6px">加载候选列表…</div>
      <div id="cg-list" style="max-height:220px;overflow:auto;font-size:11px;border:1px solid var(--border);border-radius:8px;padding:8px;margin-bottom:8px;background:var(--bg-main)"></div>
      <div style="display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap;font-size:11px">
        <button type="button" class="qa-btn" id="cg-sel-all" style="padding:4px 10px">全选</button>
        <button type="button" class="qa-btn" id="cg-sel-none" style="padding:4px 10px">全不选</button>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button type="button" class="qa-btn" id="cg-cancel">取消</button>
        <button type="button" class="qa-btn" id="cg-refresh" style="border-color:var(--accent);color:var(--accent)">刷新列表</button>
        <button type="button" class="qa-btn" id="cg-ok" style="border-color:#ef4444;color:#ef4444">确认清理</button>
      </div></div>`;
    document.body.appendChild(m);
    const listEl=m.querySelector('#cg-list');
    const sumEl=m.querySelector('#cg-summary');
    const ageEl=m.querySelector('#cg-age');
    const okBtn=m.querySelector('#cg-ok');
    function _renderList(c){
      if(!c||!c.length){
        listEl.innerHTML='';
        sumEl.innerHTML='<span style="color:var(--text-dim)">无符合条件的设备（可调低「离线超过」分钟或检查 USB）</span>';
        okBtn.disabled=true;
        return;
      }
      okBtn.disabled=false;
      sumEl.innerHTML='共 <strong>'+c.length+'</strong> 台候选，请勾选要清理的条目：';
      listEl.innerHTML=c.map(x=>{
        const id=x.device_id||'';
        const lab=(x.display_name||id.substring(0,8))+' · '+Math.floor(x.age_minutes||0)+'min · '+(x.reason||'');
        return '<label style="display:flex;gap:8px;align-items:flex-start;padding:4px 0;border-bottom:1px solid rgba(128,128,128,.12)"><input type="checkbox" class="cg-cb" checked value="'+escAttr(id)+'"><span style="word-break:break-all">'+lab.replace(/</g,'&lt;')+'</span></label>';
      }).join('');
    }
    async function loadPrev(){
      const mns=Math.max(1,parseInt(ageEl.value,10)||1440);
      sumEl.textContent='加载中…';
      listEl.innerHTML='';
      okBtn.disabled=true;
      try{
        const j=await api('GET','/devices/cleanup-candidates?max_age_minutes='+mns);
        const c=j.candidates||[];
        _renderList(c);
      }catch(e){sumEl.textContent='预览失败: '+e.message;listEl.innerHTML='';}
    }
    m.querySelector('#cg-sel-all').onclick=()=>{m.querySelectorAll('input.cg-cb').forEach(i=>{i.checked=true;});};
    m.querySelector('#cg-sel-none').onclick=()=>{m.querySelectorAll('input.cg-cb').forEach(i=>{i.checked=false;});};
    m.querySelector('#cg-cancel').onclick=()=>{document.body.removeChild(m);resolve(null);};
    m.querySelector('#cg-refresh').onclick=loadPrev;
    m.querySelector('#cg-ok').onclick=()=>{
      const mns=Math.max(1,parseInt(ageEl.value,10)||1440);
      const all=m.querySelectorAll('input.cg-cb');
      const sel=[...m.querySelectorAll('input.cg-cb:checked')].map(i=>i.value);
      if(all.length&&sel.length===0){showToast('请至少勾选一台设备','warn');return;}
      localStorage.setItem('cleanup_max_age_minutes',String(mns));
      document.body.removeChild(m);
      resolve({mns,selectedIds:sel,total:all.length});
    };
    ageEl.onchange=loadPrev;
    loadPrev();
  });
  if(pick==null)return;
  if(!pick.total){
    showToast('当前没有可清理的候选设备','warn');
    return;
  }
  const subset=(pick.selectedIds.length>0&&pick.selectedIds.length<pick.total);
  const msg=subset
    ? ('确认清理已勾选的 '+pick.selectedIds.length+' 台（阈值 '+pick.mns+' 分钟）？将先备份配置文件。')
    : ('确认按阈值 '+pick.mns+' 分钟清理全部 '+pick.total+' 台候选？将先备份配置文件。');
  if(!(await ocDialog({title:'清理确认',message:msg,type:'warning',confirmText:'清理',cancelText:'取消'})))return;
  try{
    showToast('正在备份并清理…');
    const body={max_age_minutes:pick.mns,backup:true};
    if(subset) body.device_ids=pick.selectedIds;
    const c=await api('POST','/devices/cleanup',body);
    const bp=c.backup&&c.backup.backup_dir?(' 备份: '+c.backup.backup_dir):'';
    const fx=c.device_ids_filter?(' 白名单: '+c.device_ids_filter.length+' 个'):'';
    showToast(`已清理 ${c.removed} 台，剩余约 ${c.remaining} 台。`+bp+fx,'success');
    setTimeout(loadDevices,1500);
  }catch(e){showToast('操作失败: '+e.message,'warn');}
}

async function _vpnFixDevice(deviceId){
  const short=deviceId.substring(0,8);
  const el=document.getElementById('vpn-ind-'+short);
  if(el&&el.textContent.includes('✓')){showToast(short+' VPN 正常');return;}
  if(!(await ocDialog({title:'VPN 重连',message:short+' VPN 未连接，尝试重新连接？',type:'info',confirmText:'重连',cancelText:'取消'}))) return;
  showToast('正在重连 '+short+' 的 VPN...');
  try{
    const d=await api('POST','/vpn/health/'+deviceId+'/check');
    if(d.connected){
      showToast(short+' VPN 已恢复','success');
      if(el){el.textContent='VPN ✓';el.style.background='rgba(34,197,94,.15)';el.style.color='#22c55e';}
    }else{
      showToast(short+' 重连失败，请上传新 VPN 二维码','warn');
    }
  }catch(e){showToast('修复失败: '+e.message,'warn');}
}

/* ═══════════════════════════════════════════
   VPN 管理（工具面板）
   ═══════════════════════════════════════════ */
// 检查当前设备 VPN 状态（使用快速status接口，不触发ADB重连）
async function _vpnCheckCurrent(){
  if(!modalDeviceId){showToast('无设备','warn');return;}
  const st=document.getElementById('vpn-tool-status');
  if(st) st.innerHTML='<span style="color:var(--accent)">检测中...</span>';
  try{
    // 用/vpn/status查全部设备（VPN API自带集群感知，不需要/cluster前缀）
    const d=await api('GET','/vpn/status');
    const devs=d.devices||[];
    const dev=devs.find(v=>v.device_id===modalDeviceId);
    const short=modalDeviceId.substring(0,8);
    const el=document.getElementById('vpn-ind-'+short);
    if(dev&&dev.connected){
      const flag=_getFlag(dev.country);
      if(st) st.innerHTML='<span style="color:#22c55e">&#9989; VPN 已连接</span>'+(dev.country?' '+flag+' '+dev.country:'')+(dev.ip?' · IP: '+dev.ip:'');
      if(el){el.textContent=flag+' VPN ✓';el.style.background='rgba(34,197,94,.15)';el.style.color='#22c55e';}
    }else{
      const cfg=d.current_config;
      const cfgInfo=cfg?(cfg.protocol||'')+' → '+(cfg.server||''):'无配置';
      if(st) st.innerHTML='<span style="color:#ef4444">&#10060; VPN 未连接</span> <span style="color:var(--text-muted);font-size:9px">('+cfgInfo+')</span>';
      if(el){el.textContent='VPN ✗';el.style.background='rgba(239,68,68,.15)';el.style.color='#ef4444';}
    }
  }catch(e){
    if(st) st.innerHTML='<span style="color:#ef4444">检测失败: '+e.message+'</span>';
  }
}

// ── VPN 上传二维码（scope='single'|'all'）──
async function _vpnToolUpload(input, scope){
  const file=input.files[0];
  if(!file) return;
  scope=scope||'all';
  const st=document.getElementById('vpn-tool-status');

  if(scope==='single'&&!modalDeviceId){showToast('无设备','warn');input.value='';return;}

  // 第1步: 解码二维码获取 URI
  if(st) st.innerHTML='<span style="color:var(--accent)">&#9203; 解码二维码...</span>';
  const formData=new FormData();
  formData.append('file',file);
  try{
    const r=await fetch(_apiUrl('/vpn/upload-qr'),{method:'POST',body:formData});
    const d=await r.json();
    if(!r.ok){
      if(st) st.innerHTML='<span style="color:#ef4444">&#10060; '+(d.detail||'解码失败')+'</span>';
      input.value='';return;
    }
    // 第2步: 用解码出的 URI 进行配置
    await _vpnDoSetup(d.uri, scope);
  }catch(e){
    if(st) st.innerHTML='<span style="color:#ef4444">&#10060; '+e.message+'</span>';
  }
  input.value='';
}

// 批量操作栏的VPN上传（全局入口）
async function _vpnGlobalUpload(input){
  _vpnToolUpload(input,'all');
}

// ── 粘贴URI（scope='single'|'all'）──
async function _vpnPasteUri(scope){
  scope=scope||'all';
  if(scope==='single'&&!modalDeviceId){showToast('无设备','warn');return;}
  const uri=await ocPrompt('VPN 配置链接','',{message:'粘贴 vless:// vmess:// trojan:// ss:// 链接',inputPlaceholder:'vless://...'});
  if(!uri||!uri.trim()) return;
  const trimmed=uri.trim();
  if(!trimmed.match(/^(vless|vmess|trojan|ss):\/\//)){
    showToast('无效链接，需以 vless:// vmess:// trojan:// ss:// 开头','warn');
    return;
  }
  await _vpnDoSetup(trimmed, scope);
}

// ── 统一配置入口（带实时进度）──
async function _vpnDoSetup(uri, scope){
  const st=document.getElementById('vpn-tool-status');
  const prog=document.getElementById('vpn-batch-progress');
  const progBar=document.getElementById('vpn-prog-bar');
  const progLabel=document.getElementById('vpn-prog-label');
  const progCount=document.getElementById('vpn-prog-count');
  const progDetails=document.getElementById('vpn-prog-details');

  const body={uri:uri,auto_start:true,stream:true};
  if(scope==='single'&&modalDeviceId) body.device_ids=[modalDeviceId];

  // 单台设备：简单模式，无需进度条
  if(scope==='single'){
    if(st) st.innerHTML='<span style="color:var(--accent)">&#9203; 配置中...</span>';
    try{
      const d=await api('POST','/vpn/batch-setup',body);
      const ok=d.imported||d.success||0;
      if(st) st.innerHTML=ok?'<span style="color:#22c55e">&#9989; 配置成功</span>'+(d.config_name?' ('+d.config_name+')':'')
                             :'<span style="color:#ef4444">&#10060; 配置失败</span>';
      if(ok) _vpnCheckCurrent();
    }catch(e){
      if(st) st.innerHTML='<span style="color:#ef4444">&#10060; '+e.message+'</span>';
    }
    return;
  }

  // 全部设备：SSE 流式进度
  if(prog) prog.style.display='block';
  if(progBar) progBar.style.width='0%';
  if(progLabel) progLabel.textContent='准备中...';
  if(progCount) progCount.textContent='';
  if(progDetails) progDetails.innerHTML='';
  if(st) st.innerHTML='';

  try{
    const r=await fetch(_apiUrl('/vpn/batch-setup-stream'),{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify(body)
    });
    if(!r.ok){
      const err=await r.json().catch(()=>({detail:'请求失败'}));
      if(progLabel) progLabel.innerHTML='<span style="color:#ef4444">&#10060; '+(err.detail||'失败')+'</span>';
      return;
    }
    const reader=r.body.getReader();
    const decoder=new TextDecoder();
    let buf='';
    let total=0,done_count=0,ok_count=0;
    while(true){
      const {value,done}=await reader.read();
      if(done) break;
      buf+=decoder.decode(value,{stream:true});
      const lines=buf.split('\n');
      buf=lines.pop();
      for(const line of lines){
        if(!line.startsWith('data:')) continue;
        try{
          const ev=JSON.parse(line.slice(5));
          if(ev.type==='start'){
            total=ev.total||0;
            if(progLabel) progLabel.textContent='配置中...';
            if(progCount) progCount.textContent='0/'+total;
          }else if(ev.type==='device_done'){
            done_count++;
            if(ev.ok) ok_count++;
            const pct=total?Math.round(done_count/total*100):0;
            if(progBar) progBar.style.width=pct+'%';
            if(progCount) progCount.textContent=done_count+'/'+total;
            const icon=ev.connected?'&#9989;':ev.ok?'&#9888;':'&#10060;';
            const color=ev.connected?'#22c55e':ev.ok?'#eab308':'#ef4444';
            if(progDetails) progDetails.innerHTML+='<div style="color:'+color+'">'+icon+' '+ev.short+' '+(ev.connected?'已连接':'失败')+
              (ev.time?' ('+ev.time.toFixed(1)+'s)':'')+(ev.error?' · '+ev.error:'')+'</div>';
            if(progDetails) progDetails.scrollTop=progDetails.scrollHeight;
            // 同步更新设备卡片VPN指示器
            const el=document.getElementById('vpn-ind-'+ev.short);
            if(el){
              if(ev.connected){
                el.textContent='VPN ✓';el.style.background='rgba(34,197,94,.15)';el.style.color='#22c55e';
              }else{
                el.textContent='VPN ✗';el.style.background='rgba(239,68,68,.15)';el.style.color='#ef4444';
              }
            }
          }else if(ev.type==='done'){
            if(progLabel) progLabel.innerHTML='<span style="color:#22c55e">&#9989; 完成</span>';
            if(progCount) progCount.textContent=ok_count+'/'+total+' 台成功'+(ev.config_name?' · '+ev.config_name:'');
            showToast('VPN: '+ok_count+'/'+total+' 台配置成功','success');
          }
        }catch(_){}
      }
    }
  }catch(e){
    if(progLabel) progLabel.innerHTML='<span style="color:#ef4444">&#10060; '+e.message+'</span>';
  }
}

// ── 启动当前设备VPN ──
async function _vpnStartCurrent(){
  if(!modalDeviceId){showToast('无设备','warn');return;}
  const st=document.getElementById('vpn-tool-status');
  if(st) st.innerHTML='<span style="color:var(--accent)">启动中...</span>';
  try{
    await api('POST','/vpn/toggle',{device_ids:[modalDeviceId]});
    showToast('VPN 启动命令已发送','success');
    setTimeout(_vpnCheckCurrent,3000);
  }catch(e){
    if(st) st.innerHTML='<span style="color:#ef4444">启动失败: '+e.message+'</span>';
  }
}

// ── 停止当前设备VPN ──
async function _vpnStopCurrent(){
  if(!modalDeviceId){showToast('无设备','warn');return;}
  const st=document.getElementById('vpn-tool-status');
  try{
    await api('POST','/vpn/stop/'+modalDeviceId);
    if(st) st.innerHTML='VPN 已停止';
    showToast('VPN 已停止');
    const short=modalDeviceId.substring(0,8);
    const el=document.getElementById('vpn-ind-'+short);
    if(el){el.textContent='VPN ✗';el.style.background='rgba(239,68,68,.15)';el.style.color='#ef4444';}
  }catch(e){
    if(st) st.innerHTML='<span style="color:#ef4444">停止失败: '+e.message+'</span>';
  }
}

// ── 全部启动 VPN ──
async function _vpnStartAll(){
  if(!(await ocDialog({title:'启动全部 VPN',message:'即将启动全部设备的 VPN。',type:'info',confirmText:'启动',cancelText:'取消'}))) return;
  const prog=document.getElementById('vpn-batch-progress');
  const progLabel=document.getElementById('vpn-prog-label');
  const progCount=document.getElementById('vpn-prog-count');
  const progBar=document.getElementById('vpn-prog-bar');
  const progDetails=document.getElementById('vpn-prog-details');
  if(prog) prog.style.display='block';
  if(progLabel) progLabel.textContent='启动中...';
  if(progCount) progCount.textContent='';
  if(progBar) progBar.style.width='0%';
  if(progDetails) progDetails.innerHTML='';
  try{
    const d=await api('POST','/vpn/toggle',{});
    const total=d.total||0;
    const ok=Object.values(d.results||{}).filter(v=>v==='OK').length;
    if(progBar) progBar.style.width='100%';
    if(progLabel) progLabel.innerHTML='<span style="color:#22c55e">&#9989; 完成</span>';
    if(progCount) progCount.textContent=ok+'/'+total+' 台启动成功';
    showToast('VPN 启动: '+ok+'/'+total+' 成功','success');
    setTimeout(_vpnRefreshAll,3000);
  }catch(e){
    if(progLabel) progLabel.innerHTML='<span style="color:#ef4444">&#10060; '+e.message+'</span>';
  }
}

// ── 全部停止 VPN ──
async function _vpnStopAll(){
  if(!(await ocDialog({title:'停止全部 VPN',message:'即将停止全部设备的 VPN。',type:'warning',confirmText:'停止',cancelText:'取消'}))) return;
  const prog=document.getElementById('vpn-batch-progress');
  const progLabel=document.getElementById('vpn-prog-label');
  const progCount=document.getElementById('vpn-prog-count');
  const progBar=document.getElementById('vpn-prog-bar');
  const progDetails=document.getElementById('vpn-prog-details');
  if(prog) prog.style.display='block';
  if(progLabel) progLabel.textContent='停止中...';
  if(progCount) progCount.textContent='';
  if(progBar) progBar.style.width='0%';
  if(progDetails) progDetails.innerHTML='';
  try{
    const d=await api('POST','/vpn/batch-stop',{});
    const total=d.total||0;
    const ok=d.stopped||0;
    if(progBar) progBar.style.width='100%';
    if(progLabel) progLabel.innerHTML='<span style="color:#22c55e">&#9989; 完成</span>';
    if(progCount) progCount.textContent=ok+'/'+total+' 台已停止';
    showToast('VPN 停止: '+ok+'/'+total,'success');
    setTimeout(_vpnRefreshAll,2000);
  }catch(e){
    if(progLabel) progLabel.innerHTML='<span style="color:#ef4444">&#10060; '+e.message+'</span>';
  }
}

// ── 刷新全部设备 VPN 状态 ──
async function _vpnRefreshAll(){
  const prog=document.getElementById('vpn-batch-progress');
  const progLabel=document.getElementById('vpn-prog-label');
  const progCount=document.getElementById('vpn-prog-count');
  if(prog) prog.style.display='block';
  if(progLabel) progLabel.textContent='检测中...';
  const progBar=document.getElementById('vpn-prog-bar');
  if(progBar) progBar.style.width='50%';
  try{
    const d=await api('GET','/vpn/status');
    const devs=d.devices||[];
    const connected=devs.filter(v=>v.connected).length;
    const total=devs.length;
    const cfg=d.current_config;
    const cfgInfo=cfg?' · '+(cfg.protocol||'')+' → '+(cfg.server||''):'';
    if(progBar) progBar.style.width='100%';
    if(progLabel) progLabel.innerHTML='<span style="color:#22c55e">&#9989;</span>';
    if(progCount) progCount.textContent=connected+'/'+total+' 台已连接 VPN'+cfgInfo;
    // 更新全部设备卡片上的 VPN 指示器（含国家旗帜）
    devs.forEach(v=>{
      const short=(v.device_id||'').substring(0,8);
      const el=document.getElementById('vpn-ind-'+short);
      if(!el) return;
      if(v.connected){
        const flag=_getFlag(v.country);
        el.textContent=flag+' VPN ✓';el.style.background='rgba(34,197,94,.15)';el.style.color='#22c55e';
        el.title='VPN 已连接'+(v.ip?' · IP: '+v.ip:'')+(v.country?' · '+v.country:'');
      }else{
        el.textContent='VPN ✗';el.style.background='rgba(239,68,68,.15)';el.style.color='#ef4444';
        el.title='VPN 未连接，点击修复';
      }
    });
    // 更新全部设备区计数
    const allCount=document.getElementById('vpn-scope-all-count');
    if(allCount) allCount.textContent='('+connected+'/'+total+' 在线)';
  }catch(e){
    if(progLabel) progLabel.innerHTML='<span style="color:#ef4444">检测失败: '+e.message+'</span>';
  }
}

// ── 打开设备弹窗时更新VPN区域的设备标识 ──
function _vpnUpdateScopeLabel(){
  const el=document.getElementById('vpn-scope-device');
  if(!el) return;
  if(modalDeviceId){
    const short=modalDeviceId.substring(0,8);
    const alias=(typeof ALIAS==='object'&&ALIAS[modalDeviceId])?ALIAS[modalDeviceId]:'';
    el.textContent=alias?'(#'+alias+')':'('+short+')';
  }else{
    el.textContent='';
  }
}

/* ── Interactive Control Modal ── */
let modalScreenSize=null;
// 来源优先级: 'none' < 'default' < 'http' < 'ws'
// ws（WebSocket config）最权威，来自 scrcpy 实际流参数
let _modalScreenSizeSource='none';
let _dragState=null;

let _currentModalIsCluster=false;
function _modalApiPrefix(){return _currentModalIsCluster?'/cluster':'';}

let _autoStreamOnOpen=true;

// 暴露给 video-stream.js 的坐标设置接口，带优先级保护
function _setModalScreenSize(w, h, source){
  const rank={none:0,default:1,http:2,ws:3};
  if((rank[source]||0) >= (rank[_modalScreenSizeSource]||0)){
    modalScreenSize={w,h};
    _modalScreenSizeSource=source;
  }
}

function openScreenModal(deviceId){
  stopStreaming();
  modalDeviceId=deviceId;
  _currentModalDevice=deviceId;
  modalScreenSize=null;
  _modalScreenSizeSource='none';
  _currentRotation=0;
  _zoomLevel=1.0;
  _applyZoom();
  clearAdbTerminal();
  const clusterDev=_clusterDevices.find(d=>d.device_id===deviceId && d._isCluster);
  _currentModalIsCluster=!!clusterDev;
  const alias=ALIAS[deviceId]||deviceId.substring(0,12);
  const hostLabel=clusterDev?` [${clusterDev.host_name}]`:'';
  document.getElementById('modal-title').textContent=alias+hostLabel+' — 点击屏幕操控手机';
  document.getElementById('modal-body').innerHTML='<div class="loading-text">连接中...</div>';
  document.getElementById('screen-modal').classList.add('open');
  document.getElementById('modal-auto').checked=true;

  // 立即用缓存性能数据推荐初始画质
  _autoSuggestQuality(deviceId);

  _fetchScreenSize(deviceId);
  _loadDeviceQuickInfo(deviceId);
  _vpnUpdateScopeLabel();
  renderCustomButtons();
  switchRightTab('tools');
  document.addEventListener('keydown',_onModalKey);
  if(_autoStreamOnOpen&&(_WEBCODECS_OK||(typeof JMuxer!=='undefined'))){
    setTimeout(()=>{if(modalDeviceId===deviceId) startStreaming();},200);
  }else{
    captureModalScreen();
    startModalAuto();
  }
}

// 根据设备当前资源状态自动推荐画质
function _autoSuggestQuality(deviceId){
  const perf=_devicePerfCache[deviceId]||{};
  const bat=perf.battery_level;
  const mem=perf.mem_usage;
  const sel=document.getElementById('quality-sel');
  if(!sel) return;
  let suggestedQ=null;
  let reason='';
  if(bat!=null&&bat<20){suggestedQ='low';reason=`电量低(${bat}%)，已降低画质节省电量`;}
  else if(mem!=null&&mem>88){suggestedQ='low';reason=`内存不足(${mem}%)，已降低画质减少负载`;}
  else if(bat!=null&&bat<40&&mem!=null&&mem>75){suggestedQ='low';reason=`电量(${bat}%) & 内存(${mem}%)偏低，已优化画质`;}
  if(suggestedQ&&sel.value!==suggestedQ){
    sel.value=suggestedQ;
    if(typeof _lastSuccessQuality!=='undefined') _lastSuccessQuality=suggestedQ;
    showToast(`⚡ 画质自动调整为"${suggestedQ}"：${reason}`,'info',4000,'quality-auto');
  }
}

async function _loadDeviceQuickInfo(did){
  try{
    const info=await api('GET',`/devices/${did}/battery`);
    const lvl=info.level||'?';
    const temp=info.temperature?((parseInt(info.temperature)/10).toFixed(1)+'°C'):'?';
    appendAdbOutput(`[设备] 电量:${lvl}% 温度:${temp}`);
  }catch(e){}
}

function closeModal(){
  stopStreaming();
  document.getElementById('screen-modal').classList.remove('open');
  modalDeviceId=null;
  modalScreenSize=null;
  stopModalAuto();
  document.removeEventListener('keydown',_onModalKey);
}

async function _fetchScreenSize(did){
  try{
    const r=await api('GET',`/devices/${did}/screen-size`);
    // 仅当 WS 还未给出更权威的值时才写入（优先级: ws > http > default）
    _setModalScreenSize(r.width, r.height, 'http');
    const info=document.getElementById('ctrl-device-info');
    if(info) info.textContent=`${ALIAS[did]||did.substring(0,8)} | ${r.width}x${r.height}`;
  }catch(e){
    // API 失败时仅作为 fallback，不覆盖已有值
    if(!modalScreenSize) _setModalScreenSize(720,1600,'default');
  }
}

function _imgCoordToDevice(img,clientX,clientY){
  const rect=img.getBoundingClientRect();
  const rx=(clientX-rect.left)/rect.width;
  const ry=(clientY-rect.top)/rect.height;
  if(!modalScreenSize) return null;
  const dx=Math.round(rx*modalScreenSize.w);
  const dy=Math.round(ry*modalScreenSize.h);
  if(dx<0||dy<0||dx>modalScreenSize.w||dy>modalScreenSize.h) return null;
  return {x:dx,y:dy,rx,ry};
}

function _showTapRipple(img,clientX,clientY){
  const body=document.getElementById('modal-body');
  const rect=img.getBoundingClientRect();
  const bRect=body.getBoundingClientRect();
  const el=document.createElement('div');
  el.className='tap-ripple';
  el.style.left=(clientX-bRect.left)+'px';
  el.style.top=(clientY-bRect.top)+'px';
  body.appendChild(el);
  setTimeout(()=>el.remove(),500);
}

function _showSwipeTrail(clientX,clientY){
  const body=document.getElementById('modal-body');
  const bRect=body.getBoundingClientRect();
  const dot=document.createElement('div');
  dot.className='swipe-trail';
  dot.style.left=(clientX-bRect.left-5)+'px';
  dot.style.top=(clientY-bRect.top-5)+'px';
  body.appendChild(dot);
  setTimeout(()=>dot.remove(),400);
}

function _attachImgListeners(img){
  img.addEventListener('mousedown',function(e){
    e.preventDefault();
    const pt=_imgCoordToDevice(img,e.clientX,e.clientY);
    if(!pt) return;
    _dragState={startX:pt.x,startY:pt.y,clientX:e.clientX,clientY:e.clientY,img:img,moved:false};
  });
  img.addEventListener('mousemove',function(e){
    const pt=_imgCoordToDevice(img,e.clientX,e.clientY);
    if(pt){const c=document.getElementById('ctrl-coord');if(c)c.textContent=`${pt.x}, ${pt.y}`;}
    if(!_dragState) return;
    const dx=e.clientX-_dragState.clientX;
    const dy=e.clientY-_dragState.clientY;
    if(Math.abs(dx)+Math.abs(dy)>8){
      _dragState.moved=true;
      _showSwipeTrail(e.clientX,e.clientY);
    }
  });
  img.addEventListener('mouseup',function(e){
    if(!_dragState||!modalDeviceId) return;
    const endPt=_imgCoordToDevice(img,e.clientX,e.clientY);
    if(!endPt){_dragState=null;return;}
    if(!_dragState.moved){
      _showTapRipple(img,e.clientX,e.clientY);
      _recordMacroStep({type:'tap',x:endPt.x,y:endPt.y});
      api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/tap`,{x:endPt.x,y:endPt.y});
      setTimeout(captureModalScreen,300);
    }else{
      const dur=Math.min(800,Math.max(100,Math.hypot(e.clientX-_dragState.clientX,e.clientY-_dragState.clientY)*1.8|0));
      _recordMacroStep({type:'swipe',x1:_dragState.startX,y1:_dragState.startY,x2:endPt.x,y2:endPt.y,duration:dur});
      api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/swipe`,{x1:_dragState.startX,y1:_dragState.startY,x2:endPt.x,y2:endPt.y,duration:dur});
      setTimeout(captureModalScreen,400);
    }
    _dragState=null;
  });
  img.addEventListener('mouseleave',function(){_dragState=null;});
  img.addEventListener('wheel',function(e){
    e.preventDefault();
    if(e.ctrlKey||e.metaKey){
      if(e.deltaY<0) zoomIn(); else zoomOut();
    }
  },{passive:false});
  img.addEventListener('touchstart',function(e){
    e.preventDefault();
    const t=e.touches[0];
    const pt=_imgCoordToDevice(img,t.clientX,t.clientY);
    if(!pt) return;
    _dragState={startX:pt.x,startY:pt.y,clientX:t.clientX,clientY:t.clientY,img:img,moved:false};
  },{passive:false});
  img.addEventListener('touchmove',function(e){
    if(!_dragState) return;
    const t=e.touches[0];
    const dx=t.clientX-_dragState.clientX;
    const dy=t.clientY-_dragState.clientY;
    if(Math.abs(dx)+Math.abs(dy)>8) _dragState.moved=true;
  });
  img.addEventListener('touchend',function(e){
    if(!_dragState||!modalDeviceId) return;
    const t=e.changedTouches[0];
    const endPt=_imgCoordToDevice(img,t.clientX,t.clientY);
    if(!endPt){_dragState=null;return;}
    if(!_dragState.moved){
      api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/tap`,{x:endPt.x,y:endPt.y});
      setTimeout(captureModalScreen,300);
    }else{
      api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/swipe`,{x1:_dragState.startX,y1:_dragState.startY,x2:endPt.x,y2:endPt.y,duration:300});
      setTimeout(captureModalScreen,400);
    }
    _dragState=null;
  });
}

async function captureModalScreen(){
  if(!modalDeviceId)return;
  const did=modalDeviceId; // 捕获当前设备ID，防止异步回调时设备已切换
  const body=document.getElementById('modal-body');
  try{
    const ts=Date.now();
    const prefix=_modalApiPrefix();
    const url=_apiUrl(`${prefix}/devices/${did}/screenshot?mode=control&t=${ts}`);
    const img=new Image();
    img.onload=function(){
      if(modalDeviceId!==did) return; // 设备已切换，丢弃旧截图，避免覆盖新设备的canvas
      if(_streamActive) return;       // 流媒体已激活，不覆盖canvas
      body.innerHTML='';
      body.appendChild(img);
      _attachImgListeners(img);
    };
    img.onerror=function(){
      if(modalDeviceId!==did) return;
      if(_streamActive) return;
      body.innerHTML='<div style="text-align:center;padding:40px;color:var(--text-muted)">'+
        '<div style="font-size:24px;margin-bottom:8px">&#128247;</div>'+
        '<div style="font-size:14px;font-weight:500;margin-bottom:6px">截屏加载中</div>'+
        '<div style="font-size:12px;color:var(--text-dim);margin-bottom:14px">网络延迟较高，正在尝试重新获取</div>'+
        '<button class="qa-btn" onclick="captureModalScreen()" style="padding:6px 16px;font-size:12px">&#128260; 重试截屏</button>'+
        '</div>';
      setTimeout(captureModalScreen,3000);
    };
    img.src=url;
    img.style.cssText='max-width:100%;max-height:74vh;cursor:crosshair;touch-action:none';
  }catch(e){body.innerHTML='<div class="loading-text">截屏失败</div>';}
}

async function sendKey(keycode){
  if(!modalDeviceId)return;
  _recordMacroStep({type:'key',keycode:keycode});
  if(_streamHasControl){_sendCtrl({type:'key',keycode:keycode});return;}
  await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/key`,{keycode:keycode});
  setTimeout(captureModalScreen,400);
}

// 一键亮屏解锁（电源键 + 上滑）
async function wakeAndUnlock(){
  if(!modalDeviceId)return;
  showToast('亮屏解锁中...','info',2000);
  try{
    // 1. 电源键亮屏
    await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/key`,{keycode:26});
    await new Promise(r=>setTimeout(r,800));
    // 2. 上滑解锁
    await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/swipe`,{x1:540,y1:1800,x2:540,y2:800,duration:300});
    await new Promise(r=>setTimeout(r,500));
    setTimeout(captureModalScreen,500);
    showToast('已解锁','success',2000);
  }catch(e){showToast('解锁失败: '+e.message,'warn');}
}

function _onModalKey(e){
  if(!modalDeviceId) return;
  const modal=document.getElementById('screen-modal');
  if(!modal.classList.contains('open')) return;
  if(document.activeElement&&document.activeElement.id==='adb-input') return;
  if((e.ctrlKey||e.metaKey)&&(e.key==='='||e.key==='+')){e.preventDefault();zoomIn();return;}
  if((e.ctrlKey||e.metaKey)&&e.key==='-'){e.preventDefault();zoomOut();return;}
  if((e.ctrlKey||e.metaKey)&&e.key==='0'){e.preventDefault();zoomReset();return;}
  if((e.ctrlKey||e.metaKey)&&e.key==='r'){e.preventDefault();rotateScreen();return;}
  const keyMap={Escape:4,Home:3,Backspace:4,ArrowUp:19,ArrowDown:20,ArrowLeft:21,ArrowRight:22,Enter:66};
  if(keyMap[e.key]){
    e.preventDefault();
    if(_streamHasControl) _sendCtrl({type:'key',keycode:keyMap[e.key]});
    else sendKey(keyMap[e.key]);
    return;
  }
  if(e.key==='F2'){e.preventDefault();_toggleHud();return;}
  if(e.key==='F5'){e.preventDefault();captureModalScreen();return;}
  if(e.key==='F11'){e.preventDefault();_toggleFullscreen();return;}
  if(e.key.length===1&&!e.ctrlKey&&!e.metaKey){
    e.preventDefault();
    _recordMacroStep({type:'text',text:e.key});
    if(_streamHasControl) _sendCtrl({type:'text',text:e.key});
    else{api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/text`,{text:e.key});setTimeout(captureModalScreen,300);}
  }
}

function startModalAuto(){stopModalAuto();modalTimer=setInterval(captureModalScreen,500);}
function stopModalAuto(){if(modalTimer){clearInterval(modalTimer);modalTimer=null;}}
function toggleModalAuto(){document.getElementById('modal-auto').checked?startModalAuto():stopModalAuto();}

/* ── 缩放控制 ── */
let _zoomLevel=1.0;
function _applyZoom(){
  const body=document.getElementById('modal-body');
  body.style.transform=`scale(${_zoomLevel})`;
  document.getElementById('ctrl-zoom-level').textContent=Math.round(_zoomLevel*100)+'%';
}
function zoomIn(){_zoomLevel=Math.min(3.0,_zoomLevel+0.15);_applyZoom();}
function zoomOut(){_zoomLevel=Math.max(0.3,_zoomLevel-0.15);_applyZoom();}
function zoomReset(){_zoomLevel=1.0;_applyZoom();}

const viewport=document.getElementById('screen-viewport');
if(viewport){
  viewport.addEventListener('wheel',function(e){
    if(!modalDeviceId) return;
    if(e.ctrlKey||e.metaKey){
      e.preventDefault();
      if(e.deltaY<0) zoomIn(); else zoomOut();
    }
  },{passive:false});
  let _pinchStartDist=0;
  let _pinchStartZoom=1.0;
  viewport.addEventListener('touchstart',function(e){
    if(e.touches.length===2){
      const dx=e.touches[0].clientX-e.touches[1].clientX;
      const dy=e.touches[0].clientY-e.touches[1].clientY;
      _pinchStartDist=Math.hypot(dx,dy);
      _pinchStartZoom=_zoomLevel;
    }
  },{passive:true});
  viewport.addEventListener('touchmove',function(e){
    if(e.touches.length===2){
      e.preventDefault();
      const dx=e.touches[0].clientX-e.touches[1].clientX;
      const dy=e.touches[0].clientY-e.touches[1].clientY;
      const dist=Math.hypot(dx,dy);
      if(_pinchStartDist>0){
        _zoomLevel=Math.max(0.3,Math.min(3.0,_pinchStartZoom*(dist/_pinchStartDist)));
        _applyZoom();
      }
    }
  },{passive:false});
  viewport.addEventListener('touchend',function(e){
    if(e.touches.length<2) _pinchStartDist=0;
  },{passive:true});
}

/* ── 屏幕旋转 ── */
let _currentRotation=0;
async function rotateScreen(){
  if(!modalDeviceId) return;
  _currentRotation=(_currentRotation+1)%4;
  const labels=['竖屏','横屏(左)','倒置','横屏(右)'];
  try{
    await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/rotate`,{orientation:_currentRotation});
    showToast('旋转: '+labels[_currentRotation]);
    setTimeout(()=>{_fetchScreenSize(modalDeviceId);captureModalScreen();},800);
  }catch(e){showToast('旋转失败','warn');}
}

/* ── 通知栏/快捷设置 ── */
async function pullNotification(){
  if(!modalDeviceId) return;
  await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/shell`,{command:'cmd statusbar expand-notifications'});
  setTimeout(captureModalScreen,500);
}
// 键盘快捷键面板使用的别名
function expandNotifications(){return pullNotification();}

/* ── 系统截图（Power+VolDown，比截屏API更原生）── */
async function _takeScreenshot(){
  if(!modalDeviceId) return;
  try{
    // 方案1：通过 input keyevent 触发系统截图（KEYCODE_SYSRQ=120）
    await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/key`,{keycode:120});
    showToast('截图已触发','success',1500);
    setTimeout(captureModalScreen,1000);
  }catch(e){
    // 方案2：fallback 到 screencap 命令
    try{
      await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/shell`,
        {command:'screencap -p /sdcard/ocscreenshot.png && am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file:///sdcard/ocscreenshot.png'});
      showToast('截图已保存到 /sdcard/ocscreenshot.png','info',2500);
    }catch(e2){showToast('截图失败','warn');}
  }
}
async function openQuickSettings(){
  if(!modalDeviceId) return;
  await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/shell`,{command:'cmd statusbar expand-settings'});
  setTimeout(captureModalScreen,500);
}

/* ── 保存截图到本地 ── */
function saveScreenshot(){
  const body=document.getElementById('modal-body');
  const img=body.querySelector('img');
  const video=body.querySelector('video');
  const target=img||video;
  if(!target){showToast('无画面可保存','warn');return;}
  if(img){
    const a=document.createElement('a');
    a.href=img.src;a.download=`screenshot_${modalDeviceId}_${Date.now()}.jpg`;
    document.body.appendChild(a);a.click();a.remove();
  }else if(video){
    const canvas=document.createElement('canvas');
    canvas.width=video.videoWidth;canvas.height=video.videoHeight;
    canvas.getContext('2d').drawImage(video,0,0);
    const a=document.createElement('a');
    a.href=canvas.toDataURL('image/png');a.download=`stream_${modalDeviceId}_${Date.now()}.png`;
    document.body.appendChild(a);a.click();a.remove();
  }
  showToast('截图已保存');
}

/* ── 文字输入弹窗 ── */
async function inputTextPrompt(){
  if(!modalDeviceId) return;
  const text=await ocPrompt('发送文字','',{message:'输入要发送到设备的文字',inputPlaceholder:'输入文字...'});
  if(!text) return;
  await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/input/text`,{text:text});
  setTimeout(captureModalScreen,300);
}

/* ── 打开应用选择器 ── */
async function openAppPicker(){
  if(!modalDeviceId) return;
  try{
    const res=await api('GET',`${_modalApiPrefix()}/devices/${modalDeviceId}/installed-apps`);
    const apps=res.apps||[];
    if(!apps.length){showToast('无已安装应用');return;}
    const sel=await ocPrompt('打开应用','',{message:'输入要打开的应用包名<br><span style="font-size:10px;color:var(--text-muted)">'+apps.slice(0,15).join(', ')+(apps.length>15?' ... 共'+apps.length+'个':'')+'</span>',inputPlaceholder:'com.example.app'});
    if(sel){
      await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/open-app`,{package:sel.trim()});
      setTimeout(captureModalScreen,1000);
    }
  }catch(e){showToast('获取应用列表失败','warn');}
}

/* ── 电池信息 ── */
async function getBatteryInfo(){
  if(!modalDeviceId) return;
  try{
    const info=await api('GET',`${_modalApiPrefix()}/devices/${modalDeviceId}/battery`);
    const lvl=info.level||'?';
    const temp=info.temperature?((parseInt(info.temperature)/10).toFixed(1)+'°C'):'?';
    const status=info.status||'?';
    const usb=info.usb_powered==='true'?'USB供电':'电池';
    appendAdbOutput(`[电池] 电量:${lvl}% 温度:${temp} 状态:${status} ${usb}`);
  }catch(e){appendAdbOutput('[电池] 查询失败: '+e.message);}
}

/* ── ADB 终端 ── */
let _adbHistory=[];
let _adbHistIdx=-1;
function appendAdbOutput(text){
  const el=document.getElementById('adb-output');
  if(!el) return;
  el.textContent+=text+'\n';
  const lines=el.textContent.split('\n');
  if(lines.length>500) el.textContent=lines.slice(-400).join('\n');
  el.scrollTop=el.scrollHeight;
}
function clearAdbTerminal(){
  const el=document.getElementById('adb-output');
  if(el) el.textContent='';
}
async function runAdbCmd(){
  const input=document.getElementById('adb-input');
  if(!input||!modalDeviceId) return;
  const cmd=input.value.trim();
  if(!cmd) return;
  _adbHistory.push(cmd);
  _adbHistIdx=_adbHistory.length;
  input.value='';
  appendAdbOutput('$ '+cmd);
  try{
    const res=await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/shell`,{command:cmd});
    appendAdbOutput(res.output||'(无输出)');
  }catch(e){appendAdbOutput('ERROR: '+e.message);}
}

const _adbSuggestions=['getprop ro.product.model','dumpsys battery','pm list packages -3','settings get system screen_brightness','wm size','wm density','top -n 1','df -h','cat /proc/meminfo','logcat -d -t 20','input keyevent 26','input tap','input swipe','settings put system user_rotation','am start -n','am force-stop','screencap -p /sdcard/screen.png','ip addr show wlan0','netstat -tlnp','ps -A | grep'];
const _adbInput=document.getElementById('adb-input');
if(_adbInput){
  _adbInput.addEventListener('keydown',function(e){
    if(e.key==='ArrowUp'){
      e.preventDefault();e.stopPropagation();
      if(_adbHistIdx>0){_adbHistIdx--;_adbInput.value=_adbHistory[_adbHistIdx]||'';}
    }else if(e.key==='ArrowDown'){
      e.preventDefault();e.stopPropagation();
      if(_adbHistIdx<_adbHistory.length-1){_adbHistIdx++;_adbInput.value=_adbHistory[_adbHistIdx]||'';}
      else{_adbHistIdx=_adbHistory.length;_adbInput.value='';}
    }else if(e.key==='Tab'){
      e.preventDefault();e.stopPropagation();
      const val=_adbInput.value.trim().toLowerCase();
      if(!val) return;
      const match=_adbSuggestions.find(s=>s.toLowerCase().startsWith(val));
      if(match) _adbInput.value=match;
    }
  });
}

/* ── 文件拖拽上传 ── */
const _fbListEl=document.getElementById('rpanel-files');
if(_fbListEl){
  _fbListEl.addEventListener('dragover',function(e){e.preventDefault();e.stopPropagation();_fbListEl.style.outline='2px dashed var(--accent)';});
  _fbListEl.addEventListener('dragleave',function(){_fbListEl.style.outline='';});
  _fbListEl.addEventListener('drop',async function(e){
    e.preventDefault();e.stopPropagation();
    _fbListEl.style.outline='';
    if(!modalDeviceId||!e.dataTransfer.files.length) return;
    for(const file of e.dataTransfer.files){
      if(file.size>50*1024*1024){showToast(file.name+' 超过50MB限制','warn');continue;}
      document.getElementById('fb-status').textContent='上传中: '+file.name+' ('+_formatSize(file.size)+')...';
      try{
        const buf=await file.arrayBuffer();
        const arr=new Uint8Array(buf);
        let b64='';
        const chunk=8192;
        for(let i=0;i<arr.length;i+=chunk){b64+=String.fromCharCode.apply(null,arr.subarray(i,i+chunk));}
        b64=btoa(b64);
        await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/files/push`,{dest_path:_fbCurrentPath+'/'+file.name,content_base64:b64});
        showToast('已上传: '+file.name);
      }catch(ex){showToast('上传失败: '+file.name,'warn');}
    }
    loadFileList();
  });
}

/* ── 右侧面板标签切换 ── */
function switchRightTab(tab){
  document.querySelectorAll('.rpanel-tab').forEach(t=>{
    t.classList.toggle('active',t.dataset.tab===tab);
  });
  document.querySelectorAll('.rpanel-content').forEach(c=>{
    c.style.display='none';c.classList.remove('active');
  });
  const el=document.getElementById('rpanel-'+tab);
  if(el){el.style.display='flex';el.classList.add('active');}
  if(tab==='files') loadFileList();
  if(tab==='clipboard') getDeviceClipboard();
}

/* ── 文件管理器 ── */
let _fbCurrentPath='/sdcard';
async function loadFileList(path){
  if(!modalDeviceId) return;
  if(path) _fbCurrentPath=path;
  else _fbCurrentPath=document.getElementById('fb-path').value.trim()||'/sdcard';
  document.getElementById('fb-path').value=_fbCurrentPath;
  document.getElementById('fb-status').textContent='加载中...';
  const list=document.getElementById('fb-list');
  try{
    const r=await api('GET',`${_modalApiPrefix()}/devices/${modalDeviceId}/files?path=${encodeURIComponent(_fbCurrentPath)}`);
    const items=r.items||[];
    if(!items.length){list.innerHTML='<div style="padding:12px;color:var(--text-muted);font-size:11px">空目录</div>';document.getElementById('fb-status').textContent=`${_fbCurrentPath} (空)`;return;}
    list.innerHTML=items.map(it=>{
      const icon=it.is_dir?'&#128193;':_fileIcon(it.name);
      const sz=it.is_dir?'':_formatSize(it.size);
      return `<div class="fb-item" ondblclick="${it.is_dir?`loadFileList('${_fbCurrentPath==='/'?'':_fbCurrentPath}/${it.name}')`:`filePreview('${_fbCurrentPath}/${it.name}')`}">
        <span class="fb-icon">${icon}</span>
        <span class="fb-name" title="${it.name}">${it.name}</span>
        <span class="fb-size">${sz}</span>
        <div class="fb-actions">
          ${!it.is_dir?`<button class="sb-btn2" onclick="event.stopPropagation();fileDownload('${_fbCurrentPath}/${it.name}')" style="font-size:9px;padding:1px 4px" title="下载">&#11015;</button>`:''}
          <button class="sb-btn2" onclick="event.stopPropagation();fileDelete('${_fbCurrentPath}/${it.name}')" style="font-size:9px;padding:1px 4px;color:#ef4444" title="删除">&#10005;</button>
        </div>
      </div>`;
    }).join('');
    document.getElementById('fb-status').textContent=`${_fbCurrentPath} (${items.length} 项)`;
  }catch(e){
    list.innerHTML='<div style="padding:12px;color:#ef4444;font-size:11px">加载失败: '+e.message+'</div>';
    document.getElementById('fb-status').textContent='错误';
  }
}
function fileBrowserUp(){
  const parts=_fbCurrentPath.split('/').filter(Boolean);
  parts.pop();
  loadFileList('/'+parts.join('/')||'/');
}
function _fileIcon(name){
  const ext=(name.split('.').pop()||'').toLowerCase();
  const map={jpg:'&#127748;',jpeg:'&#127748;',png:'&#127748;',gif:'&#127748;',mp4:'&#127909;',avi:'&#127909;',mkv:'&#127909;',mp3:'&#127925;',wav:'&#127925;',apk:'&#128230;',zip:'&#128230;',txt:'&#128196;',log:'&#128196;',json:'&#128196;',xml:'&#128196;'};
  return map[ext]||'&#128196;';
}
function _formatSize(bytes){
  if(bytes<1024) return bytes+'B';
  if(bytes<1048576) return (bytes/1024).toFixed(1)+'K';
  if(bytes<1073741824) return (bytes/1048576).toFixed(1)+'M';
  return (bytes/1073741824).toFixed(1)+'G';
}
function fileDownload(path){
  if(!modalDeviceId) return;
  window.open(_apiUrl(`/devices/${modalDeviceId}/files/download?path=${encodeURIComponent(path)}`),'_blank');
}
async function fileDelete(path){
  if(!modalDeviceId) return;
  if(!(await ocDialog({title:'删除文件',message:'确认删除 <code>'+path+'</code> ？',type:'danger',confirmText:'删除',dangerous:true}))) return;
  try{
    await api('DELETE',`${_modalApiPrefix()}/devices/${modalDeviceId}/files?path=${encodeURIComponent(path)}`);
    showToast('已删除');
    loadFileList();
  }catch(e){showToast('删除失败: '+e.message,'warn');}
}
async function fileMkdir(){
  if(!modalDeviceId) return;
  const name=await ocPrompt('新建文件夹','',{inputPlaceholder:'文件夹名称'});
  if(!name) return;
  try{
    await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/files/mkdir`,{path:_fbCurrentPath+'/'+name});
    showToast('已创建');
    loadFileList();
  }catch(e){showToast('创建失败','warn');}
}
function filePreview(path){
  const ext=(path.split('.').pop()||'').toLowerCase();
  if(['jpg','jpeg','png','gif','bmp'].includes(ext)){
    window.open(_apiUrl(`/devices/${modalDeviceId}/files/download?path=${encodeURIComponent(path)}`),'_blank');
  }else{
    fileDownload(path);
  }
}

/* ── 剪贴板同步 ── */
async function sendClipboard(){
  if(!modalDeviceId) return;
  const text=document.getElementById('clip-send-text').value;
  if(!text){showToast('请输入文字','warn');return;}
  try{
    await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/clipboard`,{text:text});
    showToast('已发送到设备剪贴板');
  }catch(e){showToast('发送失败','warn');}
}
async function getDeviceClipboard(){
  if(!modalDeviceId) return;
  try{
    const r=await api('GET',`${_modalApiPrefix()}/devices/${modalDeviceId}/clipboard`);
    document.getElementById('clip-device-text').textContent=r.text||'(空)';
  }catch(e){document.getElementById('clip-device-text').textContent='获取失败';}
}
function copyDeviceClipToPC(){
  const text=document.getElementById('clip-device-text').textContent;
  if(!text||text==='(空)'||text==='获取失败') return;
  navigator.clipboard.writeText(text).then(()=>showToast('已复制到PC剪贴板'));
}

/* ── 自定义按钮 ── */
function _loadCustomButtons(){
  const stored=localStorage.getItem('openclaw_custom_btns');
  return stored?JSON.parse(stored):[];
}
function _saveCustomButtons(btns){
  localStorage.setItem('openclaw_custom_btns',JSON.stringify(btns));
}
function renderCustomButtons(){
  const area=document.getElementById('custom-buttons-area');
  if(!area) return;
  const btns=_loadCustomButtons();
  area.innerHTML=btns.map((b,i)=>`<button class="sb-btn2" onclick="runCustomBtn(${i})" oncontextmenu="event.preventDefault();editCustomBtn(${i})" title="${b.cmd}">${b.label}</button>`).join('');
}
async function addCustomButton(){
  const label=await ocPrompt('按钮文字','',{message:'例如：截屏',inputPlaceholder:'按钮显示文字'});
  if(!label) return;
  const cmd=await ocPrompt('ADB 命令','',{message:'例如：screencap -p /sdcard/s.png',inputPlaceholder:'ADB Shell 命令'});
  if(!cmd) return;
  const btns=_loadCustomButtons();
  btns.push({label,cmd});
  _saveCustomButtons(btns);
  renderCustomButtons();
}
async function runCustomBtn(idx){
  const btns=_loadCustomButtons();
  if(!btns[idx]||!modalDeviceId) return;
  appendAdbOutput('$ '+btns[idx].cmd);
  try{
    const r=await api('POST',`${_modalApiPrefix()}/devices/${modalDeviceId}/shell`,{command:btns[idx].cmd});
    appendAdbOutput(r.output||'(无输出)');
    switchRightTab('terminal');
  }catch(e){appendAdbOutput('ERROR: '+e.message);}
}
async function editCustomBtn(idx){
  const btns=_loadCustomButtons();
  if(!btns[idx]) return;
  const action=await ocPrompt('编辑按钮',''  ,{title:'编辑按钮「'+btns[idx].label+'」',message:'输入 <b>1</b> 修改，<b>2</b> 删除',inputPlaceholder:'1 或 2'});
  if(action==='2'){btns.splice(idx,1);_saveCustomButtons(btns);renderCustomButtons();return;}
  if(action==='1'){
    const label=await ocPrompt('新名称',btns[idx].label);
    if(label) btns[idx].label=label;
    const cmd=await ocPrompt('新命令',btns[idx].cmd);
    if(cmd) btns[idx].cmd=cmd;
    _saveCustomButtons(btns);
    renderCustomButtons();
  }
}

async function deployAllWallpapers(){
  showToast('正在为所有设备部署编号壁纸...');
  try{
    const r=await api('POST','/devices/wallpaper/all',{});
    showToast(`壁纸部署完成: ${r.success||0}/${r.total||0} 成功`);
    setTimeout(renderScreens,3000);
  }catch(e){showToast('壁纸部署失败','warn');}
}

async function deploySingleWallpaper(deviceId, number){
  try{
    const r=await api('POST',`/devices/${deviceId}/wallpaper`,{number});
    return true;
  }catch(e){return false;}
}

function toggleWpMenu(e){
  if(e)e.stopPropagation();
  const m=document.getElementById('wp-dropdown-menu');
  const show=m.style.display==='none';
  m.style.display=show?'block':'none';
  if(show){const hide=()=>{m.style.display='none';document.removeEventListener('click',hide);};setTimeout(()=>document.addEventListener('click',hide),0);}
}

async function quickDeployAllWallpapers(){
  // 调用后端统一接口，自动广播到所有 Worker，覆盖全集群设备
  showToast('正在为全集群设备部署编号壁纸...');
  try{
    const r=await api('POST','/devices/wallpaper/all',{});
    showToast(`壁纸部署完成: ${r.success||0}/${r.total||0} 成功`);
    setTimeout(renderScreens,3000);
  }catch(e){showToast('壁纸部署失败: '+e.message,'warn');}
}

async function quickDeploySelectedWallpapers(){
  const sel=[..._selectedDevices];
  if(!sel.length){showToast('请先在群控模式中勾选设备','warn');return;}
  showToast(`正在为 ${sel.length} 台已选设备部署壁纸...`);
  let ok=0,fail=0;
  for(let i=0;i<sel.length;i++){
    const did=sel[i];
    const num=parseInt((ALIAS[did]||'').replace(/[^0-9]/g,''))||i+1;
    const r=await deploySingleWallpaper(did, num);
    if(r)ok++;else fail++;
  }
  showToast(`壁纸部署完成: ${ok}/${sel.length} 成功${fail?', '+fail+' 失败':''}`);
  setTimeout(renderScreens,3000);
}

function showWallpaperDialog(){
  const online=allDevices.filter(d=>d.status==='connected'||d.status==='online');
  const all=allDevices;
  const overlay=document.createElement('div');
  overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center';
  overlay.onclick=e=>{if(e.target===overlay)overlay.remove();};

  let rows=all.map((d,i)=>{
    const alias=ALIAS[d.device_id]||d.display_name||d.device_id.substring(0,8);
    const num=alias.replace(/[^0-9]/g,'')||String(i+1);
    const isOn=d.status==='connected'||d.status==='online';
    return `<div style="display:flex;align-items:center;gap:10px;padding:8px 12px;border-bottom:1px solid var(--border);${isOn?'':'opacity:.5'}" data-did="${d.device_id}" data-online="${isOn}">
      <span style="width:30px;font-weight:700;color:var(--accent);text-align:center">${num}</span>
      <span style="flex:1;font-size:13px">${alias}</span>
      <input type="number" min="1" max="999" value="${num}" style="width:60px;padding:4px 6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:13px;text-align:center" class="wp-num-input"/>
      <button class="qa-btn" onclick="deploySingleFromDialog(this)" style="padding:4px 12px;font-size:12px" ${isOn?'':'disabled'}>部署</button>
      <span class="wp-status" style="width:20px;text-align:center"></span>
    </div>`;
  }).join('');

  overlay.innerHTML=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;width:500px;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.5)">
    <div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
      <div>
        <h3 style="font-size:16px;font-weight:700;margin:0">&#127912; 壁纸编号管理</h3>
        <p style="font-size:11px;color:var(--text-muted);margin:4px 0 0">为每台设备设置带编号的壁纸，方便识别</p>
      </div>
      <button onclick="this.closest('div[style*=fixed]').remove()" style="background:none;border:none;color:var(--text-muted);font-size:18px;cursor:pointer">&#10005;</button>
    </div>
    <div style="padding:12px 20px;display:flex;gap:8px;border-bottom:1px solid var(--border);align-items:center">
      <button class="qa-btn" onclick="deployAllFromDialog()" style="padding:6px 14px;font-size:12px;background:var(--accent);color:#fff">全部部署(按当前编号)</button>
      <button class="qa-btn" onclick="resetNumbersInDialog()" style="padding:6px 14px;font-size:12px">重置为顺序编号</button>
      <span style="flex:1"></span>
      <span style="font-size:11px;color:var(--text-muted)">${online.length}/${all.length} 在线</span>
    </div>
    <div style="overflow-y:auto;flex:1" id="wp-device-list">
      <div style="display:flex;padding:6px 12px;font-size:11px;color:var(--text-muted);border-bottom:1px solid var(--border)">
        <span style="width:30px;text-align:center">编号</span>
        <span style="flex:1;padding-left:10px">设备</span>
        <span style="width:60px;text-align:center">指定号</span>
        <span style="width:70px;text-align:center">操作</span>
        <span style="width:20px"></span>
      </div>
      ${rows}
    </div>
  </div>`;
  document.body.appendChild(overlay);
}

async function deploySingleFromDialog(btn){
  const row=btn.closest('div[data-did]');
  const did=row.dataset.did;
  const num=parseInt(row.querySelector('.wp-num-input').value)||1;
  const status=row.querySelector('.wp-status');
  btn.disabled=true;
  status.textContent='⏳';
  const ok=await deploySingleWallpaper(did, num);
  status.textContent=ok?'✅':'❌';
  btn.disabled=false;
}

async function deployAllFromDialog(){
  const rows=document.querySelectorAll('#wp-device-list div[data-did][data-online=true]');
  if(!rows.length){showToast('没有在线设备','warn');return;}
  showToast(`正在为 ${rows.length} 台在线设备部署壁纸...`);
  let ok=0,fail=0;
  for(const row of rows){
    const did=row.dataset.did;
    const num=parseInt(row.querySelector('.wp-num-input').value)||1;
    const status=row.querySelector('.wp-status');
    const btn=row.querySelector('button');
    btn.disabled=true;
    status.textContent='⏳';
    const r=await deploySingleWallpaper(did, num);
    status.textContent=r?'✅':'❌';
    btn.disabled=false;
    if(r)ok++;else fail++;
  }
  showToast(`壁纸部署完成: ${ok} 成功, ${fail} 失败`);
  setTimeout(renderScreens,3000);
}

function resetNumbersInDialog(){
  const inputs=document.querySelectorAll('#wp-device-list .wp-num-input');
  inputs.forEach((inp,i)=>{inp.value=i+1;});
  showToast('已重置为顺序编号 1,2,3...');
}

/* ── Manual Number Editing ── */

async function editDeviceNumber(deviceId){
  const alias=ALIAS[deviceId]||deviceId.substring(0,8);
  const current=parseInt((alias).replace(/[^0-9]/g,''))||0;
  const num=await ocPrompt('设置编号',(current||'').toString(),{message:alias+' 当前编号: '+(current||'无'),inputPlaceholder:'输入数字'});
  if(num===null||num==='')return;
  const n=parseInt(num);
  if(!n||n<=0){showToast('编号必须为正整数','warn');return;}
  try{
    const r=await api('PUT',`/devices/${deviceId}/number`,{number:n,deploy_wallpaper:true});
    if(r.swapped_with){
      showToast(`编号已交换: ${alias}→#${String(n).padStart(2,'0')}, ${r.swapped_with.device_id.substring(0,8)}→#${String(r.swapped_with.number).padStart(2,'0')}`,'success');
    }else{
      showToast(`${alias} 编号设为 #${String(n).padStart(2,'0')}`,'success');
    }
    await loadAliases();
    renderScreens();
  }catch(e){
    const msg=e.message||'';
    if(msg.includes('404')||msg.includes('不存在')){
      showToast('设置编号失败：设备连接异常，请检查Worker服务是否运行','error');
    }else if(msg.includes('500')){
      showToast('设置编号失败：壁纸部署出错，编号已保存','warning');
    }else{
      showToast('设置编号失败: '+msg,'error');
    }
  }
}

async function rescanAllDevices(){
  showToast('正在重新扫描所有设备并更新指纹...');
  try{
    const r=await api('POST','/devices/rescan');
    showToast(`扫描完成: ${r.total} 台设备, ${r.deployed} 台已部署壁纸`,'success');
    await loadDevices();
    await loadAliases();
    renderScreens();
  }catch(e){showToast('扫描失败: '+e.message,'error');}
}

async function renumberAllDevices(){
  if(!(await ocDialog({title:'重新编号',message:'将所有设备按名称顺序重新编号为 01, 02, 03... 并部署壁纸。',type:'info',confirmText:'重新编号',cancelText:'取消'})))return;
  showToast('正在重新编号所有设备...');
  try{
    const r=await api('POST','/devices/renumber-all',{deploy_wallpaper:true});
    showToast(`重新编号完成: ${r.total} 台设备, ${r.deployed} 台已部署壁纸`,'success');
    await loadAliases();
    renderScreens();
  }catch(e){showToast('重新编号失败: '+e.message,'error');}
}

async function showDeviceRegistryInfo(){
  try{
    const reg=await api('GET','/devices/registry');
    const entries=Object.entries(reg);
    const overlay=document.createElement('div');
    overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center';
    overlay.onclick=e=>{if(e.target===overlay)overlay.remove();};
    const rows=entries.map(([fp,e])=>{
      const prevs=(e.previous_serials||[]).map(s=>s.substring(0,8)).join(', ');
      return `<tr style="border-bottom:1px solid var(--border)">
        <td style="padding:6px 10px;font-weight:700;color:var(--accent)">#${String(e.number||0).padStart(2,'0')}</td>
        <td style="padding:6px 10px;font-size:11px">${e.alias||''}</td>
        <td style="padding:6px 10px;font-size:10px;font-family:monospace">${(e.current_serial||'').substring(0,12)}</td>
        <td style="padding:6px 10px;font-size:10px;color:var(--text-muted)">${e.imei||'-'}</td>
        <td style="padding:6px 10px;font-size:10px;color:var(--text-muted)">${(e.hw_serial||'-').substring(0,12)}</td>
        <td style="padding:6px 10px;font-size:10px;color:var(--text-muted)">${(e.android_id||'-').substring(0,12)}</td>
        <td style="padding:6px 10px;font-size:10px;color:var(--text-dim)">${prevs||'-'}</td>
        <td style="padding:6px 10px;font-size:10px">${e.model||''}</td>
      </tr>`;
    }).join('');
    overlay.innerHTML=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;width:900px;max-width:95vw;max-height:80vh;display:flex;flex-direction:column;box-shadow:0 20px 60px rgba(0,0,0,.5)">
      <div style="padding:16px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
        <div>
          <h3 style="font-size:16px;font-weight:700;margin:0">&#128270; 设备指纹注册表</h3>
          <p style="font-size:11px;color:var(--text-muted);margin:4px 0 0">USB重插后通过指纹(IMEI/序列号/AndroidID)自动识别原设备</p>
        </div>
        <button onclick="this.closest('div[style*=fixed]').remove()" style="background:none;border:none;color:var(--text-muted);font-size:18px;cursor:pointer">&#10005;</button>
      </div>
      <div style="overflow:auto;flex:1">
        <table style="width:100%;border-collapse:collapse">
          <thead><tr style="background:rgba(255,255,255,.03);font-size:10px;color:var(--text-muted)">
            <th style="padding:8px 10px;text-align:left">编号</th>
            <th style="padding:8px 10px;text-align:left">别名</th>
            <th style="padding:8px 10px;text-align:left">当前串号</th>
            <th style="padding:8px 10px;text-align:left">IMEI</th>
            <th style="padding:8px 10px;text-align:left">硬件序列号</th>
            <th style="padding:8px 10px;text-align:left">Android ID</th>
            <th style="padding:8px 10px;text-align:left">历史串号</th>
            <th style="padding:8px 10px;text-align:left">型号</th>
          </tr></thead>
          <tbody>${rows||'<tr><td colspan="8" style="padding:20px;text-align:center;color:var(--text-muted)">暂无注册设备 — 首次启动后自动采集</td></tr>'}</tbody>
        </table>
      </div>
      <div style="padding:10px 20px;border-top:1px solid var(--border);font-size:11px;color:var(--text-dim)">${entries.length} 条记录</div>
    </div>`;
    document.body.appendChild(overlay);
  }catch(e){showToast('获取注册表失败: '+e.message,'error');}
}

async function quickAddToGroup(deviceId){
  try{
    const r=await api('GET','/device-groups');
    const groups=r.groups||[];
    if(!groups.length){
      const name=await ocPrompt('创建分组','',{message:'还没有分组，输入名称创建',inputPlaceholder:'分组名称'});
      if(!name)return;
      const g=await api('POST','/device-groups',{name});
      await api('POST',`/device-groups/${g.id}/devices`,{device_id:deviceId});
      showToast(`已创建分组"${name}"并添加设备`,'success');
      return;
    }
    const list=groups.map((g,i)=>`${i+1}. ${g.name} (${g.devices?.length||0}台)`).join('\n');
    const pick=await ocPrompt('选择分组','',{message:'输入编号或新名称<br><span style="font-size:10px;color:var(--text-muted)">'+list.replace(/\n/g,'<br>')+'</span>',inputPlaceholder:'编号或新名称'});
    if(!pick)return;
    const idx=parseInt(pick)-1;
    if(idx>=0&&idx<groups.length){
      await api('POST',`/device-groups/${groups[idx].id}/devices`,{device_id:deviceId});
      showToast(`已添加到"${groups[idx].name}"`,'success');
    }else{
      const g=await api('POST','/device-groups',{name:pick});
      await api('POST',`/device-groups/${g.id}/devices`,{device_id:deviceId});
      showToast(`已创建分组"${pick}"并添加设备`,'success');
    }
  }catch(e){showToast('操作失败: '+e.message,'error');}
}

async function quickBatchAddToGroup(){
  const sel=[..._selectedDevices];
  if(!sel.length){showToast('请先在群控模式中勾选设备','warn');return;}
  try{
    const r=await api('GET','/device-groups');
    const groups=r.groups||[];
    let groupId;
    if(!groups.length){
      const name=await ocPrompt('创建分组','',{message:'为 '+sel.length+' 台设备创建新分组',inputPlaceholder:'分组名称'});
      if(!name)return;
      const g=await api('POST','/device-groups',{name});
      groupId=g.id;
    }else{
      const list=groups.map((g,i)=>`${i+1}. ${g.name}`).join('\n');
      const pick=await ocPrompt('加入分组','',{message:'将 '+sel.length+' 台设备加入哪个分组？<br><span style="font-size:10px;color:var(--text-muted)">'+list.replace(/\n/g,'<br>')+'</span>',inputPlaceholder:'编号或新名称'});
      if(!pick)return;
      const idx=parseInt(pick)-1;
      if(idx>=0&&idx<groups.length){
        groupId=groups[idx].id;
      }else{
        const g=await api('POST','/device-groups',{name:pick});
        groupId=g.id;
      }
    }
    await api('POST',`/device-groups/${groupId}/batch-add`,{device_ids:sel});
    showToast(`${sel.length} 台设备已加入分组`,'success');
  }catch(e){showToast('批量编组失败: '+e.message,'error');}
}

/* ═══════════════════════════════════════════════════════════
   编号管理面板 (Number Manager Panel)
   ─ 全量设备列表 / 可拖拽排序 / 批量赋号 / 一键部署壁纸
   ═══════════════════════════════════════════════════════════ */

let _nmRows = [];        // [{did, name, worker, online, num, pendingNum}, ...]
let _nmFilter = 'all';   // 'all' | worker name | 'unset' | 'conflict'
let _nmDragIdx = null;
let _nmRanges = {};      // {host_id: {start,end}} 从后端加载

async function showNumMgr(initialFilter){
  // 并行加载编号段配置 + 集群概览（建立 host_name→host_id 精确映射）
  try{
    const [rng, ov] = await Promise.all([
      api('GET','/cluster/number-ranges'),
      api('GET','/cluster/overview'),
    ]);
    _nmRanges = rng||{};
    window._hostNameToId = {};
    (ov.hosts||[]).forEach(h=>{ if(h.host_name&&h.host_id) window._hostNameToId[h.host_name]=h.host_id; });
  }catch(e){ _nmRanges={}; }

  // 合并所有设备（本机 + 集群），带编号信息
  _nmRows = allDevices.map(d=>{
    const num = parseInt((ALIAS[d.device_id]||'').replace(/[^0-9]/g,''))||0;
    const wk  = _getDeviceWorker(d.device_id);
    const wkKey = wk==='本机'?'主控':wk; // 与 host_id 不同，是显示名
    return {
      did:    d.device_id,
      name:   d.display_name || d.device_id.substring(0,10),
      worker: wkKey,
      online: d.status==='connected'||d.status==='online',
      num,
      pendingNum: num,
    };
  });
  _nmRows.sort((a,b)=>(a.pendingNum||999)-(b.pendingNum||999));

  const workers = [...new Set(_nmRows.map(r=>r.worker))];
  const tabs = [
    {key:'all',   label:'全部'},
    {key:'unset', label:'未编号'},
    {key:'conflict', label:'⚠ 冲突'},
    ...workers.map(w=>({key:w, label:w}))
  ];

  // 编号段配置行（按已知 worker）+ 容量进度
  const rangeRows = workers.filter(w=>w!=='主控').map(w=>{
    const hostId = _nmWorkerToHostId(w);
    const rng = _nmRanges[hostId]||{};
    const total = (rng.start&&rng.end) ? rng.end-rng.start+1 : 0;
    const used  = total ? _nmRows.filter(r=>r.worker===w&&r.pendingNum>=rng.start&&r.pendingNum<=rng.end).length : 0;
    const capHtml = total
      ? `<span style="font-size:10px;padding:1px 5px;border-radius:3px;background:${used>=total?'rgba(239,68,68,.15)':'rgba(34,197,94,.12)'};color:${used>=total?'#ef4444':'#22c55e'}">${used}/${total}</span>`
      : '';
    return `<div class="nm-range-chip">
      <span class="nm-worker" style="background:${_getWorkerColor(w)}">${w}</span>
      ${capHtml}
      <span style="color:var(--text-muted)">段:</span>
      <input class="rng-edit" type="number" min="1" id="rng-s-${hostId}" value="${rng.start||''}" placeholder="起">
      <span style="color:var(--text-muted)">~</span>
      <input class="rng-edit" type="number" min="1" id="rng-e-${hostId}" value="${rng.end||''}" placeholder="止">
    </div>`;
  }).join('');

  const overlay = document.createElement('div');
  overlay.id = 'num-mgr-overlay';
  overlay.innerHTML = `
    <div id="num-mgr-panel">
      <div class="nm-header">
        <span style="font-size:16px">📋</span>
        <b style="flex:1;font-size:15px">编号管理</b>
        <span style="font-size:11px" id="nm-status-text">${_nmRows.length} 台设备</span>
        <button class="qa-btn" onclick="_nmSaveRanges()" style="font-size:10px;height:24px;padding:0 8px;margin-left:8px" title="保存编号段配置">💾 保存编号段</button>
        <button onclick="document.getElementById('num-mgr-overlay').remove()" style="background:none;border:none;color:var(--text-muted);font-size:18px;cursor:pointer;padding:0 4px;margin-left:4px">×</button>
      </div>
      ${rangeRows?`<div class="nm-range-bar">${rangeRows}</div>`:''}
      <div class="nm-tabs" id="nm-tabs">
        ${tabs.map(t=>`<div class="nm-tab${(initialFilter||'all')===t.key?' active':''}" onclick="_nmSetFilter('${t.key}')" id="nmtab-${t.key}">${t.label}</div>`).join('')}
      </div>
      <div class="nm-toolbar">
        <span style="font-size:11px;color:var(--text-muted)">从</span>
        <input class="nm-seq-inp" id="nm-seq-start" type="number" min="1" max="999" value="1" style="width:52px">
        <span style="font-size:11px;color:var(--text-muted)">号开始</span>
        <button class="qa-btn" onclick="_nmSeqAssign()" style="font-size:11px;height:26px;padding:0 10px">顺序赋号</button>
        <span style="width:1px;height:16px;background:var(--border)"></span>
        <button class="qa-btn" onclick="_nmClearAll()" style="font-size:11px;height:26px;padding:0 10px;color:#ef4444;border-color:#ef4444">清空</button>
        <button class="qa-btn" onclick="_nmAutoAssign()" style="font-size:11px;height:26px;padding:0 10px;color:#a78bfa;border-color:#a78bfa" title="按编号段配置自动为未编号设备分配最小可用号">🎯 按段分配</button>
        <span style="flex:1"></span>
        <button id="nm-fix-btn" class="qa-btn" onclick="_nmFixConflicts()" style="font-size:11px;height:26px;padding:0 10px;display:none;color:#ef4444;border-color:#ef4444">🔧 修复冲突</button>
        <span id="nm-conflict-warn" style="display:none;font-size:11px;color:#ef4444;font-weight:600">⚠ 有重复编号</span>
      </div>
      <div class="nm-list" id="nm-list"></div>
      <div class="nm-footer">
        <div style="font-size:11px;color:var(--text-muted)">☰ 拖动排序 · 点编号修改 · Enter 确认</div>
        <div style="display:flex;gap:8px">
          <button class="qa-btn" onclick="_nmSave(false)" style="font-size:12px;height:32px;padding:0 16px">💾 保存编号</button>
          <button class="qa-btn" onclick="_nmSave(true)" style="font-size:12px;height:32px;padding:0 16px;background:var(--accent);border-color:var(--accent);color:#fff">💾 保存+壁纸</button>
        </div>
      </div>
    </div>`;
  overlay.onclick = e => { if(e.target===overlay) overlay.remove(); };
  document.body.appendChild(overlay);
  _nmFilter = initialFilter||'all';
  _nmRenderList();
}

function _nmWorkerToHostId(workerName){
  // 优先使用集群概览建立的 host_name→host_id 精确映射
  if(window._hostNameToId && window._hostNameToId[workerName])
    return window._hostNameToId[workerName];
  // 降级：简单转换 lowercase（W03→w03，兜底）
  return workerName.toLowerCase().replace(/[^a-z0-9]/g,'-');
}

async function _nmSaveRanges(){
  const ranges = {};
  document.querySelectorAll('[id^="rng-s-"]').forEach(inp=>{
    const hostId = inp.id.replace('rng-s-','');
    const s = parseInt(inp.value)||0;
    const eInp = document.getElementById('rng-e-'+hostId);
    const e = parseInt(eInp?.value)||0;
    if(s>0 && e>=s) ranges[hostId] = {start:s, end:e};
  });
  try{
    await api('POST','/cluster/number-ranges', ranges);
    _nmRanges = ranges;
    showToast('编号段配置已保存','success');
    _nmRenderList(); // 刷新越界高亮
  }catch(ex){ showToast('保存失败: '+ex.message,'error'); }
}

function _nmRefresh(){
  if(!document.getElementById('num-mgr-overlay')) return;
  // 用最新数据刷新已打开的面板（不重建 DOM，只刷新列表）
  _nmRows.forEach(r=>{
    const n = parseInt((ALIAS[r.did]||'').replace(/[^0-9]/g,''))||0;
    r.num = n;
    // 只有 pendingNum 与服务器一致时才同步（避免覆盖未保存的改动）
    if(r.pendingNum===r.num) r.pendingNum = n;
  });
  _nmRenderList();
}

async function _nmFixConflicts(){
  showToast('正在修复冲突...');
  try{
    const r = await api('POST','/devices/fix-conflicts',{deploy_wallpaper:true});
    showToast(r.message||'修复完成','success');
    await loadAliases();
    renderScreens();
    _updateUnsetCount();
    await _refreshConflictBadge();
    // 重建面板数据
    document.getElementById('num-mgr-overlay')?.remove();
    await showNumMgr();
  }catch(ex){ showToast('修复失败: '+ex.message,'error'); }
}

function _nmSetFilter(key){
  _nmFilter = key;
  document.querySelectorAll('.nm-tab').forEach(t=>t.classList.remove('active'));
  const el = document.getElementById('nmtab-'+key);
  if(el) el.classList.add('active');
  // 切换到特定 Worker tab 时，自动建议该 Worker 的段起始号
  const inp = document.getElementById('nm-seq-start');
  if(inp && key!=='all' && key!=='unset' && key!=='conflict'){
    const hostId = _nmWorkerToHostId(key);
    const rng = _nmRanges[hostId]||{};
    if(rng.start) inp.value = rng.start;
  } else if(inp && key==='all'){
    inp.value = 1;
  }
  _nmRenderList();
}

function _nmRenderList(){
  const list = document.getElementById('nm-list');
  if(!list) return;

  // 全局冲突检测
  const numCount={};
  _nmRows.forEach(r=>{ if(r.pendingNum){ numCount[r.pendingNum]=(numCount[r.pendingNum]||0)+1; }});
  const conflictNums = new Set(Object.entries(numCount).filter(([,c])=>c>1).map(([n])=>parseInt(n)));
  const hasConflict = conflictNums.size>0;

  // 范围越界检测（build worker→range map，key = workerName）
  const rangeByWorker={};
  Object.entries(_nmRanges).forEach(([hid,rng])=>{
    // 尝试匹配 workerName（简单：host_id 与 workerName 相近）
    _nmRows.forEach(r=>{
      if(_nmWorkerToHostId(r.worker)===hid) rangeByWorker[r.worker]=rng;
    });
  });
  const _isOutOfRange = r => {
    if(!r.pendingNum) return false;
    const rng = rangeByWorker[r.worker];
    if(!rng) return false;
    return r.pendingNum < rng.start || r.pendingNum > rng.end;
  };

  // 过滤
  let rows = _nmRows;
  if(_nmFilter==='unset')    rows = rows.filter(r=>!r.pendingNum);
  else if(_nmFilter==='conflict') rows = rows.filter(r=>conflictNums.has(r.pendingNum)||_isOutOfRange(r));
  else if(_nmFilter!=='all') rows = rows.filter(r=>r.worker===_nmFilter);

  // 更新 UI 状态
  const warn = document.getElementById('nm-conflict-warn');
  if(warn) warn.style.display = hasConflict?'':'none';
  const fixBtn = document.getElementById('nm-fix-btn');
  if(fixBtn) fixBtn.style.display = hasConflict?'':'none';

  // 更新冲突 tab 标签数
  const conflictTab = document.getElementById('nmtab-conflict');
  if(conflictTab) conflictTab.textContent = conflictNums.size>0?`⚠ 冲突 (${conflictNums.size})`:'⚠ 冲突';

  const st = document.getElementById('nm-status-text');
  if(st){
    const unsetCnt = _nmRows.filter(r=>!r.pendingNum).length;
    const conflictDevs = _nmRows.filter(r=>conflictNums.has(r.pendingNum)).length;
    let txt = `${_nmRows.length} 台`;
    if(unsetCnt>0) txt += ` · <span style="color:#f59e0b">${unsetCnt} 未编号</span>`;
    if(conflictDevs>0) txt += ` · <span style="color:#ef4444">${conflictDevs} 冲突</span>`;
    if(!unsetCnt&&!conflictDevs) txt += ' · <span style="color:var(--green)">✓ 编号整洁</span>';
    st.innerHTML = txt;
  }

  list.innerHTML = rows.map((r)=>{
    const idx = _nmRows.indexOf(r);
    const isConflict = conflictNums.has(r.pendingNum);
    const oor = _isOutOfRange(r);
    const rng = rangeByWorker[r.worker];
    const wkColor = _getWorkerColor(r.worker==='主控'?'本机':r.worker);
    const dotCls  = r.online?'ok':'err';
    const rowCls = isConflict?'conflict':oor?'out-of-range':'';
    const numTip  = isConflict?'⚠ 重复编号':oor?`⚠ 超出 ${rng.start}-${rng.end} 范围`:'';
    return `<div class="nm-row${rowCls?' '+rowCls:''}"
         draggable="true"
         data-did="${r.did}"
         data-idx="${idx}"
         ondragstart="_nmDragStart(event,${idx})"
         ondragover="_nmDragOver(event)"
         ondrop="_nmDrop(event,${idx})"
         ondragleave="this.classList.remove('drag-over')">
      <span class="nm-drag" title="拖动排序">☰</span>
      <input class="nm-num${isConflict?' conflict-input':oor?' conflict-input':''}"
        type="number" min="1" max="999"
        value="${r.pendingNum||''}"
        placeholder="?"
        title="${numTip}"
        onchange="_nmNumChange(${idx},this.value)"
        onkeydown="if(event.key==='Enter')this.blur()">
      <div class="nm-name" title="${r.did}">
        ${r.name}
        ${!r.pendingNum?'<span style="font-size:9px;background:#f59e0b;color:#fff;padding:0 4px;border-radius:2px;margin-left:4px">未编号</span>':''}
        ${isConflict?'<span style="font-size:9px;background:rgba(239,68,68,.15);color:#ef4444;padding:0 4px;border-radius:2px;margin-left:4px">重复</span>':''}
        ${oor?`<span style="font-size:9px;background:rgba(234,179,8,.15);color:#eab308;padding:0 4px;border-radius:2px;margin-left:4px">越界</span>`:''}
      </div>
      <div><span class="nm-worker" style="background:${wkColor}">${r.worker}</span></div>
      <div class="nm-status">
        <span class="status-dot ${dotCls}" style="width:5px;height:5px"></span>
        <span style="font-size:10px;color:var(--text-muted)">${r.online?'在线':'离线'}</span>
      </div>
      <button class="dev-btn wp" style="padding:2px 6px;font-size:11px"
        onclick="_nmDeployOne('${r.did}',${r.pendingNum||0})"
        title="${r.pendingNum?'部署壁纸':'请先设置编号'}">🖼</button>
    </div>`;
  }).join('') || `<div style="text-align:center;padding:30px;color:var(--text-muted)">此分类下无设备</div>`;

  // 动态更新各 Worker tab 的容量标签（已用/总容量）
  const allWorkers = [...new Set(_nmRows.map(r=>r.worker))];
  allWorkers.forEach(w=>{
    const tab = document.getElementById('nmtab-'+w);
    if(!tab) return;
    const hostId = _nmWorkerToHostId(w);
    const rng = _nmRanges[hostId]||{};
    if(!rng.start||!rng.end){ tab.textContent=w; return; }
    const total = rng.end-rng.start+1;
    const used  = _nmRows.filter(r=>r.worker===w&&r.pendingNum>=rng.start&&r.pendingNum<=rng.end).length;
    const color = used>=total?'#ef4444':used>total*0.8?'#f59e0b':'#22c55e';
    tab.innerHTML = `${w} <span style="font-size:9px;color:${color}">${used}/${total}</span>`;
  });
}

function _nmNumChange(idx, val){
  const n = parseInt(val)||0;
  _nmRows[idx].pendingNum = n;
  _nmRenderList(); // 重渲以更新冲突提示
}

/* 按编号段自动分配（调用后端 auto-assign-segments） */
async function _nmAutoAssign(){
  showToast('正在按编号段智能分配未编号设备...');
  try{
    const r = await api('POST','/devices/auto-assign-segments');
    if(r.assigned===0){ showToast(r.message||'所有设备已有编号'); return; }
    showToast(`已为 ${r.assigned} 台设备分配编号`, 'success');
    await loadAliases();
    // 刷新面板数据（不关闭面板）
    _nmRows.forEach(row=>{
      const n = parseInt((ALIAS[row.did]||'').replace(/[^0-9]/g,''))||0;
      row.num = n;
      if(row.pendingNum===0 || row.pendingNum===row.num) row.pendingNum = n;
    });
    _nmRenderList();
    renderScreens && renderScreens();
    _updateUnsetCount();
    _refreshConflictBadge();
  }catch(ex){ showToast('分配失败: '+ex.message,'error'); }
}

/* 顺序赋号（仅对当前过滤的设备） */
function _nmSeqAssign(){
  let start = parseInt(document.getElementById('nm-seq-start')?.value)||1;
  // Worker tab 且段有配置时：自动使用段起始号（如果用户没有手动改）
  if(_nmFilter!=='all' && _nmFilter!=='unset' && _nmFilter!=='conflict'){
    const hostId = _nmWorkerToHostId(_nmFilter);
    const rng = _nmRanges[hostId]||{};
    if(rng.start && start===1){
      start = rng.start;
      const inp = document.getElementById('nm-seq-start');
      if(inp) inp.value = start;
    }
  }
  let rows = _nmRows;
  if(_nmFilter==='unset')    rows = rows.filter(r=>!r.pendingNum);
  else if(_nmFilter==='conflict') rows = _nmRows; // 对全部设备重排（冲突 tab 不限制范围）
  else if(_nmFilter!=='all') rows = rows.filter(r=>r.worker===_nmFilter);
  rows.forEach((r,i)=>{ r.pendingNum = start + i; });
  _nmRenderList();
  showToast(`已预览从 ${start} 号开始的顺序编号，点"保存"生效`);
}

function _nmClearAll(){
  const rows = _nmFilter==='all'?_nmRows:
               _nmFilter==='unset'?_nmRows.filter(r=>!r.pendingNum):
               _nmRows.filter(r=>r.worker===_nmFilter);
  rows.forEach(r=>r.pendingNum=0);
  _nmRenderList();
}

/* 拖拽排序 */
function _nmDragStart(e, idx){ _nmDragIdx=idx; e.currentTarget.classList.add('dragging'); }
function _nmDragOver(e){
  e.preventDefault();
  e.currentTarget.classList.add('drag-over');
}
function _nmDrop(e, toIdx){
  e.preventDefault();
  e.currentTarget.classList.remove('drag-over');
  if(_nmDragIdx===null||_nmDragIdx===toIdx) return;
  // 把行从 fromIdx 移到 toIdx
  const [moved] = _nmRows.splice(_nmDragIdx, 1);
  _nmRows.splice(toIdx, 0, moved);
  _nmDragIdx = null;
  document.querySelectorAll('.nm-row.dragging').forEach(el=>el.classList.remove('dragging'));
  // 重新按新顺序顺序赋号（仅对有编号的，保留无编号的）
  const start = parseInt(document.getElementById('nm-seq-start')?.value)||1;
  let n = start;
  _nmRows.forEach(r=>{ if(r.pendingNum||true){ r.pendingNum = n++; }});
  _nmRenderList();
}

/* 单台壁纸部署（不关闭面板） */
async function _nmDeployOne(did, num){
  if(!num){ showToast('请先设置编号','warn'); return; }
  showToast(`正在为 #${String(num).padStart(2,'0')} 部署壁纸...`);
  try{
    const r = await api('POST',`/devices/${did}/wallpaper`,{number:num});
    showToast(r.ok?`#${String(num).padStart(2,'0')} 壁纸已部署`:'部署失败',r.ok?'success':'warn');
  }catch(ex){ showToast('部署失败: '+ex.message,'error'); }
}

/* 保存全部变更 */
async function _nmSave(deployWp){
  const changed = _nmRows.filter(r=>r.pendingNum>0 && r.pendingNum!==r.num);
  if(!changed.length){ showToast('没有需要保存的变更'); return; }

  const assignments = changed.map(r=>({device_id:r.did, number:r.pendingNum}));
  showToast(`正在保存 ${assignments.length} 台设备编号...`);
  try{
    const r = await api('POST','/devices/batch-number',{assignments, deploy_wallpaper:deployWp});
    showToast(`已保存 ${r.total} 台设备编号${deployWp?' 并后台部署壁纸':''}`, 'success');
    // 更新 pendingNum → num（已保存）
    changed.forEach(r=>{ r.num=r.pendingNum; });
    await loadAliases();
    renderScreens();
    _updateUnsetCount();
    _nmRenderList();
  }catch(ex){ showToast('保存失败: '+ex.message,'error'); }
}

/* ═══════════════════════════════════════════════════════════
   互换模式 (Swap Mode)
   ─ 点第一张卡选中 → 点第二张 → 自动互换编号
   ═══════════════════════════════════════════════════════════ */

let _swapMode = false;
let _swapFirst = null; // {did, num}

function toggleSwapMode(){
  _swapMode = !_swapMode;
  _swapFirst = null;
  const btn = document.getElementById('swap-mode-btn');
  if(btn){
    btn.classList.toggle('swap-active', _swapMode);
    btn.textContent = _swapMode ? '⇄ 互换中... (点击取消)' : '⇄ 互换编号';
  }
  // 清除所有已选卡片
  document.querySelectorAll('.dev-card.swap-selected').forEach(c=>c.classList.remove('swap-selected'));
  if(_swapMode) showToast('互换模式：先点第一台，再点第二台，自动互换编号');
}

function _swapCardClick(e, did){
  if(!_swapMode) return;
  e.stopPropagation();
  const num = parseInt((ALIAS[did]||'').replace(/[^0-9]/g,''))||0;

  if(!_swapFirst){
    // 第一步：记录第一台
    _swapFirst = {did, num};
    // 高亮这张卡
    const card = e.currentTarget;
    card.classList.add('swap-selected');
    const alias = ALIAS[did]||did.substring(0,8);
    showToast(`已选: ${alias}${num?' (#'+String(num).padStart(2,'0')+')':`(未编号)`} → 再点一台完成互换`);
  } else {
    // 第二步：执行互换
    const a = _swapFirst;
    const b = {did, num};
    _swapFirst = null;
    document.querySelectorAll('.dev-card.swap-selected').forEach(c=>c.classList.remove('swap-selected'));
    _doSwap(a, b);
  }
}

async function _doSwap(a, b){
  if(a.did===b.did){ showToast('请选择不同的设备','warn'); toggleSwapMode(); return; }
  const aAlias = ALIAS[a.did]||a.did.substring(0,8);
  const bAlias = ALIAS[b.did]||b.did.substring(0,8);
  showToast(`互换: ${aAlias} ↔ ${bAlias}`);
  try{
    // 设备A→设备B的编号：API自动检测冲突并swap
    if(b.num>0){
      await api('PUT',`/devices/${a.did}/number`,{number:b.num, deploy_wallpaper:true});
    } else if(a.num>0){
      await api('PUT',`/devices/${b.did}/number`,{number:a.num, deploy_wallpaper:true});
    } else {
      showToast('两台设备都未编号，无法互换','warn');
      toggleSwapMode(); return;
    }
    showToast(`互换完成: ${aAlias} ↔ ${bAlias}`,'success');
    await loadAliases(); renderScreens(); _updateUnsetCount();
  }catch(ex){ showToast('互换失败: '+ex.message,'error'); }
  toggleSwapMode();
}

/* ── P8: 初始化性能数据轮询 + 集群节点状态栏 ── */
_syncPerfData();
setInterval(_syncPerfData, 30000);
_renderClusterBar();
setInterval(_renderClusterBar, 15000);

