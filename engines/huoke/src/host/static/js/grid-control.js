/* grid-control.js — 多屏并控: 屏幕网格、WebSocket群控通道、集群设备墙、设备筛选、网格大小控制、WebSocket截图推送 */
/* ── Screen Grid ── */
let _groupMode=false;
let _selectedDevices=new Set();

function toggleGroupMode(){
  _groupMode=!_groupMode;
  const btn=document.getElementById('group-mode-btn');
  const tb=document.getElementById('group-toolbar');
  if(_groupMode){
    btn.style.background='var(--accent)';btn.style.color='#fff';
    tb.classList.add('active');
    document.querySelectorAll('.scr-card').forEach(c=>c.classList.add('group-mode'));
    _ensureGroupWs();
  }else{
    btn.style.background='';btn.style.color='';
    tb.classList.remove('active');
    _selectedDevices.clear();
    document.querySelectorAll('.scr-card').forEach(c=>{c.classList.remove('group-mode','selected');});
    _updateGroupCount();
    _closeGroupWs();
  }
}

function _toggleDeviceSelect(did,e){
  if(!_groupMode) return false;
  e.stopPropagation();
  if(_selectedDevices.has(did)) _selectedDevices.delete(did);
  else _selectedDevices.add(did);
  const card=document.querySelector(`[data-did="${did}"]`);
  if(card){
    card.classList.toggle('selected',_selectedDevices.has(did));
    const cb=card.querySelector('.scr-check');
    if(cb) cb.checked=_selectedDevices.has(did);
  }
  _updateGroupCount();
  _groupSyncSelection();
  return true;
}

function _updateGroupCount(){
  const el=document.getElementById('grp-count');
  if(el) el.textContent=`已选: ${_selectedDevices.size}`;
  if(typeof _refreshScrApkPreview==='function') _refreshScrApkPreview();
}

function groupSelectAll(){
  allDevices.forEach(d=>{
    if(d.status==='connected'||d.status==='online') _selectedDevices.add(d.device_id);
  });
  document.querySelectorAll('.scr-card').forEach(c=>{
    const did=c.getAttribute('data-did');
    if(_selectedDevices.has(did)){c.classList.add('selected');const cb=c.querySelector('.scr-check');if(cb)cb.checked=true;}
  });
  _updateGroupCount();
  _groupSyncSelection();
}

function groupDeselectAll(){
  _selectedDevices.clear();
  document.querySelectorAll('.scr-card').forEach(c=>{c.classList.remove('selected');const cb=c.querySelector('.scr-check');if(cb)cb.checked=false;});
  _updateGroupCount();
  _groupSyncSelection();
}

/* ── WebSocket Group Control Channel ── */
let _groupWs=null;
let _groupWsReady=false;
let _groupLatencyEl=null;

function _ensureGroupWs(){
  if(_groupWs && _groupWs.readyState===WebSocket.OPEN) return true;
  if(_groupWs && _groupWs.readyState===WebSocket.CONNECTING) return false;
  _groupWs=new WebSocket(_wsUrl('/ws/group-control'));
  _groupWsReady=false;
  _groupWs.onopen=function(){
    _groupWsReady=true;
    _groupSyncSelection();
    showToast('群控通道已连接 (WebSocket)');
  };
  _groupWs.onmessage=function(ev){
    try{
      const msg=JSON.parse(ev.data);
      if(msg.type==='ack'){
        const latEl=document.getElementById('grp-latency');
        if(latEl) latEl.textContent=`${msg.latency_ms.toFixed(0)}ms [${msg.method}]`;
        const bar=document.getElementById('anomaly-bar');
        if(bar && msg.results){
          const ok=Object.values(msg.results).filter(v=>v==='ok').length;
          const total=Object.keys(msg.results).length;
          bar.classList.add('active');
          bar.style.borderColor='rgba(34,197,94,.3)';bar.style.background='rgba(34,197,94,.05)';
          bar.innerHTML=`<span class="anomaly-item info">✓ ${msg.cmd} → ${ok}/${total} 成功 (${msg.latency_ms.toFixed(0)}ms ${msg.method})</span>`;
          setTimeout(()=>{bar.classList.remove('active');bar.innerHTML='';},3000);
        }
        setTimeout(refreshAllScreens,300);
      } else if(msg.type==='status'){
        const el=document.getElementById('grp-ws-status');
        if(el) el.textContent=`在线:${msg.connected} 已选:${msg.selected}`;
      }
    }catch(e){}
  };
  _groupWs.onclose=function(){
    _groupWsReady=false;
    _groupWs=null;
  };
  _groupWs.onerror=function(){_groupWs=null;_groupWsReady=false;};
  return false;
}

function _groupSend(msg){
  if(!_ensureGroupWs()){
    setTimeout(()=>_groupSend(msg),200);
    return;
  }
  if(_groupWs && _groupWs.readyState===WebSocket.OPEN){
    _groupWs.send(JSON.stringify(msg));
  }
}

function _groupSyncSelection(){
  if(_groupWs && _groupWs.readyState===WebSocket.OPEN){
    _groupWs.send(JSON.stringify({cmd:'select',devices:[..._selectedDevices]}));
  }
}

function _closeGroupWs(){
  if(_groupWs){try{_groupWs.close();}catch(e){}_groupWs=null;_groupWsReady=false;}
}

function groupAction(action){
  const ids=[..._selectedDevices];
  if(!ids.length){showToast('请先选择设备','warn');return;}
  _groupSyncSelection();
  const W=1080,H=2400;
  switch(action){
    case 'home': _groupSend({cmd:'home'}); break;
    case 'back': _groupSend({cmd:'back'}); break;
    case 'recent': _groupSend({cmd:'recent'}); break;
    case 'swipe_up': _groupSend({cmd:'swipe',x1:W/2,y1:H*0.75,x2:W/2,y2:H*0.25,dur:300}); break;
    case 'swipe_down': _groupSend({cmd:'swipe',x1:W/2,y1:H*0.25,x2:W/2,y2:H*0.75,dur:300}); break;
    case 'swipe_left': _groupSend({cmd:'swipe',x1:W*0.8,y1:H/2,x2:W*0.2,y2:H/2,dur:300}); break;
    case 'swipe_right': _groupSend({cmd:'swipe',x1:W*0.2,y1:H/2,x2:W*0.8,y2:H/2,dur:300}); break;
    case 'tap_center': _groupSend({cmd:'tap',x:W/2,y:H/2}); break;
  }
  showToast(`群控指令: ${action} → ${ids.length} 台设备`);
}

async function groupCustomTap(){
  const ids=[..._selectedDevices];
  if(!ids.length){showToast('请先选择设备','warn');return;}
  const input=await ocPrompt('输入坐标','',{message:'例如: 540,1200',inputPlaceholder:'x,y'});
  if(!input) return;
  const parts=input.split(',').map(Number);
  if(parts.length!==2||isNaN(parts[0])||isNaN(parts[1])){showToast('坐标格式错误','warn');return;}
  _groupSyncSelection();
  _groupSend({cmd:'tap',x:parts[0],y:parts[1]});
  showToast(`群控点击 (${parts[0]},${parts[1]}) → ${ids.length} 台设备`);
}

async function groupInputText(){
  const ids=[..._selectedDevices];
  if(!ids.length){showToast('请先选择设备','warn');return;}
  const text=await ocPrompt('发送文字','',{message:'输入要发送到设备的文字',inputPlaceholder:'输入文字...'});
  if(!text) return;
  _groupSyncSelection();
  _groupSend({cmd:'text',text:text});
  showToast(`群控输入文字 → ${ids.length} 台设备`);
}

async function groupLaunchTask(){
  const ids=[..._selectedDevices];
  if(!ids.length){showToast('请先选择设备','warn');return;}
  const sel=document.getElementById('grp-task-type');
  const taskType=sel.value;
  if(!taskType){showToast('请选择任务类型','warn');return;}
  if(!(await ocDialog({title:'启动任务',message:`将在 <b>${ids.length}</b> 台设备上启动「${sel.options[sel.selectedIndex].text}」任务。`,type:'info',confirmText:'启动',cancelText:'取消'}))) return;
  await api('POST','/devices/group/task',{device_ids:ids,task_type:taskType,params:{}});
  showToast(`批量任务已提交: ${sel.options[sel.selectedIndex].text} → ${ids.length} 台设备`);
}


/* ── Cluster device wall ── */
let _clusterDevices=[], _showCluster=false, _hostFilter='';
async function toggleClusterDevices(){
  _showCluster=document.getElementById('show-cluster-devices')?.checked||false;
  const hostSel=document.getElementById('scr-host-filter');
  if(_showCluster){
    try{
      const [overview,devList]=await Promise.all([
        api('GET','/cluster/overview'),
        api('GET','/cluster/devices'),
      ]);
      _clusterDevices=(devList.devices||[]).map(d=>{
        d._isCluster=true;
        if(!d.host_name) d.host_name=d.host_id?.substring(0,6)||'remote';
        return d;
      });
      const hosts=[...new Set(_clusterDevices.map(d=>d.host_name).filter(Boolean))];
      hostSel.style.display='inline-block';
      hostSel.innerHTML='<option value="">全部主机 ('+_clusterDevices.length+'台)</option>'+
        hosts.map(h=>`<option value="${h}">${h}</option>`).join('');
    }catch(e){_clusterDevices=[];showToast('加载集群设备失败','warn');}
  }else{
    _clusterDevices=[];
    hostSel.style.display='none';
    _hostFilter='';
  }
  renderScreens();
}
function filterByHost(host){
  _hostFilter=host;
  _scrPage=0;
  renderScreens();
}
/** 屏幕监控角标：取分域标签的槽位（如 主控-04→04），避免 W03-04 被拼成 0304 或与另一台 04 混淆 */
function _scrBadgeText(alias, deviceId){
  const a=(alias||'').trim();
  if(!a) return (deviceId||'').substring(0,6);
  let m=a.match(/-(\d{1,3})$/);
  if(m) return m[1];
  m=a.match(/(\d{1,3})号$/);
  if(m) return m[1];
  const parts=a.match(/(\d+)/g);
  if(parts&&parts.length) return parts[parts.length-1].slice(-3);
  return a.substring(0,6);
}
function _scrSlotSortKey(alias, deviceId){
  const n=parseInt(_scrBadgeText(alias, deviceId),10);
  return isNaN(n)?9999:n;
}
/** 同一 device_id 只保留一条，避免两张卡片共用同一 DOM id、截图推送到错卡 */
function _dedupeDevicesById(devs){
  const score=function(d){
    const st=d.status||'';
    let s=0;
    if(st==='connected'||st==='online') s+=4;
    else if(st==='busy') s+=3;
    if(!d._isCluster) s+=1;
    if(d.usb_issue) s-=1;
    return s;
  };
  const m=new Map();
  for(const d of devs){
    const id=d&&d.device_id;
    if(!id) continue;
    const prev=m.get(id);
    if(!prev||score(d)>score(prev)) m.set(id,d);
  }
  return [...m.values()];
}
/** 同指纹多路（USB 序列号 + 无线 IP:5555）合并为一条，优先保留 USB；无指纹则无法合并 */
function _dedupeDevicesByFingerprint(devs){
  const _wifiId=(did)=>/^\d{1,3}(\.\d{1,3}){3}:\d+$/.test((did||'').trim());
  const _score=function(d){
    const st=d.status||'';
    let s=0;
    if(st==='connected'||st==='online') s+=4;
    else if(st==='busy') s+=3;
    if(!_wifiId(d.device_id)) s+=3;
    if(!d._isCluster) s+=1;
    if(d.usb_issue) s-=1;
    return s;
  };
  const byFp=new Map();
  const noFp=[];
  for(const d of devs){
    const fp=(d.fingerprint||'').trim();
    if(!fp){
      noFp.push(d);
      continue;
    }
    const prev=byFp.get(fp);
    if(!prev||_score(d)>_score(prev)) byFp.set(fp,d);
  }
  return [...byFp.values(),...noFp];
}
function _scrImgByDid(did){
  if(!did) return null;
  const imgs=document.querySelectorAll('img.scr-img[data-did]');
  for(const img of imgs){ if(img.dataset.did===did) return img; }
  return null;
}
function _getMergedDevices(){
  // loadDevices() 会把集群设备并入 allDevices（_isCluster=true）；未勾选「集群设备」时只显示本机/主控直连
  let devs=allDevices.filter(d=>_showCluster||!d._isCluster);
  if(_showCluster && _clusterDevices.length){
    const localIds=new Set(allDevices.map(d=>d.device_id));
    const remote=_clusterDevices.filter(d=>!localIds.has(d.device_id));
    devs=[...devs,...remote];
  }
  if(_hostFilter){
    devs=devs.filter(d=>(d.host_name||'')=== _hostFilter);
  }
  devs=_dedupeDevicesById(devs);
  devs=_dedupeDevicesByFingerprint(devs);
  devs.sort((a,b)=>{
    const aa=ALIAS[a.device_id]||'', ab=ALIAS[b.device_id]||'';
    const na=_scrSlotSortKey(aa,a.device_id), nb=_scrSlotSortKey(ab,b.device_id);
    if(na!==nb) return na-nb;
    return (a.host_name||'').localeCompare(b.host_name||'')||a.device_id.localeCompare(b.device_id);
  });
  return devs;
}

/* ── Device filter ── */
let _deviceFilter='all';
function setDeviceFilter(f){
  _deviceFilter=f;
  document.querySelectorAll('#filter-all,#filter-online,#filter-offline').forEach(b=>{
    b.classList.toggle('active',b.id==='filter-'+f);
  });
  renderScreens();
}

/** 群控勾选数量（与「当前将安装」可能不同：后者会排除集群/离线） */
function getScrGroupSelectedCount(){
  return _selectedDevices.size;
}
window.getScrGroupSelectedCount=getScrGroupSelectedCount;

/** 集群设备：优先 _isCluster；合并去重后可能只剩带 host_id 的 Worker 行 */
function _scrIsClusterDevice(d){
  if(!d) return false;
  if(d._isCluster===true) return true;
  const hid=d.host_id;
  if(hid&&String(hid).length>=4&&hid!=='__local__') return true;
  return false;
}
/** 可装 APK 的在线判定（含 busy；兼容 is_online；集群未知状态时参考 health_score） */
function _scrDevOnlineForApk(d){
  if(!d) return false;
  if(d.is_online===true) return true;
  const st=d.status||'';
  if(st==='connected'||st==='online'||st==='busy') return true;
  if(_scrIsClusterDevice(d)&&(st===''||st==='unknown')){
    const hs=d.health_score;
    if(hs!=null&&hs>35) return true;
  }
  return false;
}

/** 屏幕监控页 APK 安装目标（本机 ADB / 集群转发分流） */
function getScreenApkTargetIds(mode){
  const merged=_getMergedDevices();
  const localAdb=d=>!_scrIsClusterDevice(d);
  const pick=ids=>ids.filter(id=>{
    const d=merged.find(x=>x.device_id===id);
    return d&&_scrDevOnlineForApk(d)&&localAdb(d);
  });
  const pickCluster=ids=>ids.filter(id=>{
    const d=merged.find(x=>x.device_id===id);
    return d&&_scrDevOnlineForApk(d)&&_scrIsClusterDevice(d);
  });
  if(mode==='selected') return pick([..._selectedDevices]);
  if(mode==='cluster_selected') return pickCluster([..._selectedDevices]);
  if(mode==='page') return pick(window._scrLastPageDidList||[]);
  return merged.filter(d=>_scrDevOnlineForApk(d)&&localAdb(d)).map(d=>d.device_id);
}
window.getScreenApkTargetIds=getScreenApkTargetIds;

/**
 * 仅当 APK 面板已打开：若群控里只有集群机、没有本机目标，则自动切到「群控已选→集群」。
 */
function maybeAutoSelectClusterApkMode(){
  const panel=document.getElementById('scr-apk-panel');
  if(!panel||panel.style.display!=='block') return;
  ensureClusterApkModeFromSelection();
}

/**
 * 点击「开始安装」前调用：不依赖面板是否打开。群控里若只有集群、无本机，则切到「群控已选→集群」。
 * @returns {boolean} 是否切换了单选项
 */
function ensureClusterApkModeFromSelection(){
  if(!_selectedDevices.size) return false;
  try{
    const nLoc=getScreenApkTargetIds('selected').length;
    const nCl=getScreenApkTargetIds('cluster_selected').length;
    if(nLoc===0&&nCl>0){
      const r=document.querySelector('input[name="scr-apk-mode"][value="cluster_selected"]');
      if(r){ r.checked=true; return true; }
    }
  }catch(e){}
  return false;
}
window.ensureClusterApkModeFromSelection=ensureClusterApkModeFromSelection;
window.maybeAutoSelectClusterApkMode=maybeAutoSelectClusterApkMode;

/** 当前页网格内在线集群机数量（用于提示文案） */
function getScreenApkClusterOnlineOnPageCount(){
  const merged=_getMergedDevices();
  const page=window._scrLastPageDidList||[];
  let n=0;
  for(let i=0;i<page.length;i++){
    const id=page[i];
    const d=merged.find(x=>x.device_id===id);
    if(d&&_scrDevOnlineForApk(d)&&_scrIsClusterDevice(d)) n++;
  }
  return n;
}
window.getScreenApkClusterOnlineOnPageCount=getScreenApkClusterOnlineOnPageCount;

/* ── Device management actions ── */
async function deleteDevice(deviceId){
  const alias=ALIAS[deviceId]||deviceId.substring(0,8);
  if(!(await ocDialog({title:'删除设备',message:`确定要删除设备 <b>${alias}</b> (${deviceId.substring(0,12)}) 吗？<br>此操作将从配置中永久移除该设备。`,type:'danger',confirmText:'删除',dangerous:true}))) return;
  try{
    await api('DELETE',`/devices/${deviceId}`);
    showToast(`设备 ${alias} 已删除`);
    await loadDevices();
    renderScreens();
  }catch(e){
    showToast(e.message||'删除失败','error');
  }
}

async function reconnectDevice(deviceId){
  const alias=ALIAS[deviceId]||deviceId.substring(0,8);
  showToast(`正在重连 ${alias}...`);
  try{
    const r=await api('POST',`/devices/${deviceId}/reconnect`);
    if(r.ok){
      showToast(`${alias} 重连成功!`);
      await loadDevices();
      renderScreens();
    }else{
      showToast(`${alias} 重连失败`,'warn');
    }
  }catch(e){
    showToast('重连失败: '+(e.message||''),'error');
  }
}

async function batchDeleteSelected(){
  const ids=[..._selectedDevices];
  if(!ids.length){showToast('请先选择设备','warn');return;}
  const offlineIds=ids.filter(id=>{
    const d=allDevices.find(x=>x.device_id===id);
    return d && d.status!=='connected' && d.status!=='online';
  });
  if(!offlineIds.length){showToast('只能删除离线设备','warn');return;}
  if(!(await ocDialog({title:'批量删除离线设备',message:`确定要删除 <b>${offlineIds.length}</b> 台离线设备吗？此操作不可撤销。`,type:'danger',confirmText:'删除',dangerous:true}))) return;
  try{
    const r=await api('POST','/devices/batch-delete',{device_ids:offlineIds});
    showToast(`已删除 ${r.deleted} 台设备`);
    _selectedDevices.clear();
    await loadDevices();
    renderScreens();
  }catch(e){
    showToast('批量删除失败','error');
  }
}

async function batchReconnectSelected(){
  const ids=[..._selectedDevices];
  if(!ids.length){showToast('请先选择设备','warn');return;}
  showToast(`正在重连 ${ids.length} 台设备...`);
  try{
    const r=await api('POST','/devices/batch-reconnect',{device_ids:ids});
    showToast(`重连完成: ${r.reconnected}/${ids.length} 台成功`);
    await loadDevices();
    renderScreens();
  }catch(e){
    showToast('批量重连失败','error');
  }
}

function _formatLastSeen(ts){
  if(!ts) return '';
  const diff=Math.floor((Date.now()/1000)-ts);
  if(diff<60) return '刚刚';
  if(diff<3600) return Math.floor(diff/60)+'分钟前';
  if(diff<86400) return Math.floor(diff/3600)+'小时前';
  return Math.floor(diff/86400)+'天前';
}

/* ── Grid size + multi-stream ── */
let _gridSize='md';
function setGridSize(sz){
  _gridSize=sz;
  const grid=document.getElementById('screen-grid');
  grid.classList.remove('sz-sm','sz-lg');
  if(sz==='sm') grid.classList.add('sz-sm');
  if(sz==='lg') grid.classList.add('sz-lg');
  document.querySelectorAll('.sz-btn').forEach(b=>{b.classList.toggle('active',b.textContent==={sm:'小',md:'中',lg:'大'}[sz]);});
  renderScreens();
}
function openMultiStream(){
  const sel=_groupMode?[..._selectedDevices]:allDevices.filter(d=>d.status==='connected'||d.status==='online').slice(0,16).map(d=>d.device_id);
  if(!sel.length){showToast('请先选择设备或进入群控模式','warn');return;}
  const count=Math.min(sel.length,16);
  const cls=count<=4?'g2x2':count<=9?'g3x3':'g4x4';
  const dids=sel.slice(0,count);
  const overlay=document.createElement('div');
  overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.95);z-index:250;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:20px';
  overlay.innerHTML=`<div style="display:flex;justify-content:space-between;width:100%;max-width:900px;margin-bottom:8px;align-items:center">
    <span style="color:#fff;font-size:14px;font-weight:600">多宫格实时预览 (${count}台)</span>
    <div style="display:flex;gap:8px;align-items:center">
      <label style="color:#fff;font-size:11px;display:flex;align-items:center;gap:4px">
        <input type="checkbox" id="ms-h264-toggle" onchange="toggleMultiStreamH264(this.checked)"> H.264 实时流
      </label>
      <select id="ms-layout-sel" style="background:#222;color:#fff;border:1px solid #555;border-radius:4px;padding:3px 6px;font-size:11px" onchange="changeMultiLayout(this.value)">
        <option value="g2x2" ${cls==='g2x2'?'selected':''}>2x2</option>
        <option value="g3x3" ${cls==='g3x3'?'selected':''}>3x3</option>
        <option value="g4x4" ${cls==='g4x4'?'selected':''}>4x4</option>
      </select>
      <button class="sb-btn2" onclick="_closeMultiStream()">关闭</button>
    </div>
  </div>
  <div class="multi-stream-grid ${cls}" style="width:100%;max-width:900px;max-height:80vh" id="ms-grid"></div>`;
  document.body.appendChild(overlay);
  window._msOverlay=overlay;
  window._msDids=dids;
  const msGrid=document.getElementById('ms-grid');
  _renderMultiCells(msGrid,dids,false);
  window._msRefreshTimer=setInterval(()=>{
    if(!document.getElementById('ms-grid')){clearInterval(window._msRefreshTimer);return;}
    if(document.getElementById('ms-h264-toggle')?.checked) return;
    msGrid.querySelectorAll('.ms-cell img').forEach(img=>{
      const did=img.dataset.did;
      if(did) img.src=_apiUrl(`/devices/${did}/screenshot?max_h=360&quality=40&t=${Date.now()}`);
    });
  },2500);
}
function _renderMultiCells(grid,dids,useH264){
  grid.innerHTML='';
  dids.forEach(did=>{
    const alias=ALIAS[did]||did.substring(0,8);
    const cell=document.createElement('div');
    cell.className='ms-cell';
    if(useH264){
      if(_WEBCODECS_OK){
        const cvs=document.createElement('canvas');
        cvs.width=320;cvs.height=480;
        cvs.style.cssText='width:100%;height:100%;object-fit:contain;background:#000';
        cvs.dataset.did=did;
        cell.appendChild(cvs);
        _startMultiH264(did,cvs);
      }else{
        const vid=document.createElement('video');
        vid.autoplay=true;vid.muted=true;vid.playsInline=true;
        vid.dataset.did=did;
        cell.appendChild(vid);
        _startMultiH264(did,vid);
      }
    }else{
      cell.innerHTML=`<img data-did="${did}" src="${_apiUrl('/devices/'+did+'/screenshot?max_h=360&quality=40&t='+Date.now())}" alt="${alias}" onerror="this.outerHTML='<div style=\\'color:#666;text-align:center;padding-top:40%\\'>离线</div>'">`;
    }
    const lbl=document.createElement('div');lbl.className='ms-label';lbl.textContent=alias;
    cell.appendChild(lbl);
    cell.onclick=()=>{_closeMultiStream();openScreenModal(did);};
    grid.appendChild(cell);
  });
}
function _startMultiH264(did,el){
  try{
    const _clDev=_clusterDevices.find(d=>d.device_id===did&&d._isCluster);
    const _pfx=_clDev?'/cluster':'';
    const ws=new WebSocket(_wsUrl(`${_pfx}/devices/${did}/stream/ws`));
    ws.binaryType='arraybuffer';
    el._msWs=ws;
    if(_WEBCODECS_OK&&el.tagName==='CANVAS'){
      const dec=new WcH264(el);
      dec.onResize=(w,h)=>{el.width=w;el.height=h;};
      dec.init();
      el._msDec=dec;
      ws.onmessage=e=>{
        if(typeof e.data==='string') return;
        dec.feed(e.data);
      };
      ws.onclose=()=>dec.destroy();
    }else if(typeof JMuxer!=='undefined'){
      const jm=new JMuxer({node:el,mode:'video',fps:10,flushingTime:100,
        onError:()=>{},debug:false});
      ws.onmessage=e=>{
        if(e.data instanceof ArrayBuffer) jm.feed({video:new Uint8Array(e.data)});
      };
      ws.onclose=()=>jm.destroy();
    }else{
      ws.close();
    }
  }catch(e){}
}
function toggleMultiStreamH264(on){
  const grid=document.getElementById('ms-grid');
  if(!grid||!window._msDids) return;
  grid.querySelectorAll('video,canvas').forEach(v=>{
    if(v._msDec)try{v._msDec.destroy();}catch(e){}
    if(v._msWs)try{v._msWs.close();}catch(e){}
  });
  _renderMultiCells(grid,window._msDids,on);
}
function changeMultiLayout(cls){
  const grid=document.getElementById('ms-grid');
  if(!grid) return;
  grid.className='multi-stream-grid '+cls;
}
function _closeMultiStream(){
  clearInterval(window._msRefreshTimer);
  const grid=document.getElementById('ms-grid');
  if(grid) grid.querySelectorAll('video,canvas').forEach(v=>{
    if(v._msDec)try{v._msDec.destroy();}catch(e){}
    if(v._msWs)try{v._msWs.close();}catch(e){}
  });
  if(window._msOverlay) window._msOverlay.remove();
}

/* ── WebSocket screenshot push handler ── */
function _handleScreenshotPush(data){
  if(!data?.screenshots) return;
  for(const [did,b64] of Object.entries(data.screenshots)){
    const img=_scrImgByDid(did);
    if(!img) continue;
    const tmp=new Image();
    const src='data:image/jpeg;base64,'+b64;
    tmp.onload=function(){
      if(!img.isConnected) return;
      // Fade 过渡
      img.style.transition='opacity 0.2s ease';
      img.style.opacity='0.7';
      setTimeout(()=>{
        img.src=src;
        img.style.opacity='1';
      },200);
    };
    tmp.src=src;
  }
  // WS 推送可用时，降低轮询频率
  if(!window._wsScreenshotsActive){
    window._wsScreenshotsActive=true;
    // 当 WS 推送在工作时，将轮询间隔翻倍以节省带宽
    const cb=document.getElementById('auto-refresh-all');
    if(cb && cb.checked && allRefreshTimer){
      clearInterval(allRefreshTimer);
      allRefreshTimer=setInterval(_smartRefreshScreenshots, _autoRefreshInterval()*2);
    }
  }
}

let _scrPage=0, _scrPageSize=20, _scrObserver=null;
function setPageSize(n){_scrPageSize=n;_scrPage=0;renderScreens();}
function scrGoPage(p){_scrPage=p;renderScreens();}

function _thumbParams(){
  if(_gridSize==='sm') return 'max_h=180&quality=30';
  if(_gridSize==='lg') return 'max_h=600&quality=60';
  return 'max_h=360&quality=40';
}

async function renderScreens(){
  const grid=document.getElementById('screen-grid');
  if(!allDevices.length){
    grid.innerHTML='<div style="color:var(--text-dim)">正在加载设备...</div>';
    try{await loadDevices();}catch(e){console.error('loadDevices error:',e);}
  }
  let merged=_getMergedDevices();
  if(!merged.length){
    // 仅当「本机无任何直连设备」但集群上有设备时，自动勾选集群（避免主控 0 台却空白）
    const cb=document.getElementById('show-cluster-devices');
    const hasLocal=allDevices.some(d=>!d._isCluster);
    const hasCluster=allDevices.some(d=>d._isCluster);
    if(cb&&!cb.checked&&!_showCluster&&!hasLocal&&hasCluster){
      cb.checked=true;
      try{await toggleClusterDevices();return;}catch(e){}
    }
    grid.innerHTML='<div style="color:var(--text-muted)">未检测到设备 — <a href="#" onclick="renderScreens();return false" style="color:var(--accent)">点击重试</a> | <a href="#" onclick="document.getElementById(\'show-cluster-devices\').checked=true;toggleClusterDevices();return false" style="color:#8b5cf6">加载集群设备</a></div>';
    window._scrLastPageDidList=[];
    if(typeof _refreshScrApkPreview==='function') _refreshScrApkPreview();
    return;
  }
  _fetchHealthData();
  const onlineCount=merged.filter(d=>_scrDevOnlineForApk(d)).length;
  const offlineCount=merged.length-onlineCount;
  if(_deviceFilter==='online') merged=merged.filter(d=>_scrDevOnlineForApk(d));
  else if(_deviceFilter==='offline') merged=merged.filter(d=>!_scrDevOnlineForApk(d));
  const total=merged.length;
  const ps=_scrPageSize||total;
  const pages=Math.max(1,Math.ceil(total/ps));
  _scrPage=Math.min(_scrPage,pages-1);
  const start=_scrPage*ps;
  const pageDevs=ps>=total?merged:merged.slice(start,start+ps);
  window._scrLastPageDidList=pageDevs.map(d=>d.device_id);
  const tp=_thumbParams();

  const countEl=document.getElementById('scr-device-count');
  if(countEl) countEl.textContent=`${onlineCount}在线 / ${offlineCount}离线`;
  if(typeof _refreshDeviceBreakdownUI==='function') _refreshDeviceBreakdownUI();
  const fOnline=document.getElementById('filter-online');
  if(fOnline) fOnline.textContent=`在线(${onlineCount})`;
  const fOffline=document.getElementById('filter-offline');
  if(fOffline) fOffline.textContent=`离线(${offlineCount})`;

  const pager=document.getElementById('scr-pager');
  if(pager && pages>1){
    let h=`<button class="sz-btn" onclick="scrGoPage(${Math.max(0,_scrPage-1)})" ${_scrPage===0?'disabled':''}>◀</button>`;
    h+=`<span style="color:var(--text-main);min-width:40px;text-align:center">${_scrPage+1}/${pages}</span>`;
    h+=`<button class="sz-btn" onclick="scrGoPage(${Math.min(pages-1,_scrPage+1)})" ${_scrPage>=pages-1?'disabled':''}>▶</button>`;
    pager.innerHTML=h;
  }else if(pager){pager.innerHTML='';}

  grid.innerHTML=pageDevs.map(d=>{
    const isOn=_scrDevOnlineForApk(d);
    const alias=ALIAS[d.device_id]||d.device_id.substring(0,8);
    const badgeTxt=_scrBadgeText(alias,d.device_id);
    const sel=_selectedDevices.has(d.device_id);
    const gm=_groupMode?' group-mode':'';
    const sm=sel?' selected':'';
    const hostTag=d.host_name?`<span style="position:absolute;top:8px;left:50%;transform:translateX(-50%);z-index:5;font-size:9px;background:rgba(59,130,246,.8);color:#fff;padding:1px 6px;border-radius:3px">${d.host_name}</span>`:'';
    const stCls=isOn?' st-online':' st-offline';
    return `<div class="scr-card${gm}${sm}${stCls}" data-did="${d.device_id}" onclick="if(!_toggleDeviceSelect('${d.device_id}',event))openScreenModal('${d.device_id}')" oncontextmenu="_showCtxMenu(event,'${d.device_id}')">
      <input type="checkbox" class="scr-check" ${sel?'checked':''} onclick="_toggleDeviceSelect('${d.device_id}',event)">
      <span class="scr-badge" style="background:${_badgeColor(badgeTxt,d.device_id)}">${badgeTxt||'?'}</span>
      ${hostTag}
      ${_healthBadge(d.device_id,isOn)}
      ${!isOn?`<div class="scr-fix-bar" style="display:flex">
        <span>${d.usb_issue?'USB: '+(({'unauthorized':'需授权','offline':'连接异常'})[d.usb_issue]||d.usb_issue):'设备离线'}</span>
        <button onclick="event.stopPropagation();reconnectDevice('${d.device_id}')">重连</button>
        <button onclick="event.stopPropagation();diagnoseDev('${d.device_id}')">诊断</button>
      </div>`:''}
      ${isOn?`<img class="scr-img" data-did="${d.device_id}" data-src="${_apiUrl((d._isCluster?'/cluster':'')+'/devices/'+d.device_id+'/screenshot?'+tp+'&t='+Date.now())}" src="${_apiUrl((d._isCluster?'/cluster':'')+'/devices/'+d.device_id+'/screenshot?'+tp+'&t='+Date.now())}" alt="${alias}" loading="lazy" onerror="this.style.opacity='0.3'">`
            :d.usb_issue?`<div class="scr-placeholder" style="color:#fbbf24;font-size:12px;flex-direction:column;gap:6px"><span style="font-size:24px">&#9888;</span><span>USB: ${({unauthorized:'请在手机上确认调试授权',offline:'连接异常,请重新插拔',authorizing:'等待授权确认','no permissions':'缺少USB权限'})[d.usb_issue]||d.usb_issue}</span><span style="font-size:10px;margin-top:4px;display:flex;gap:6px"><a href="#" onclick="event.stopPropagation();reconnectDevice('${d.device_id}');return false" style="color:var(--accent)">重连</a><a href="#" onclick="event.stopPropagation();deleteDevice('${d.device_id}');return false" style="color:#ef4444">删除</a></span></div>`
            :_recoveryStatus[d.device_id]&&_recoveryStatus[d.device_id].recovering&&!_recoveryStatus[d.device_id].exhausted?`<div class="scr-placeholder" style="flex-direction:column;gap:8px"><span style="font-size:22px;animation:spin 1.5s linear infinite">&#x1F504;</span><span style="color:#60a5fa;font-weight:600">自动恢复中</span><span style="font-size:11px;color:var(--text-muted)">L${_recoveryStatus[d.device_id].level}/${_recoveryStatus[d.device_id].level_name} (尝试 ${_recoveryStatus[d.device_id].attempts}次)</span><div style="width:60%;height:3px;background:rgba(255,255,255,.1);border-radius:2px;overflow:hidden"><div style="height:100%;background:#3b82f6;width:${Math.min(100,(_recoveryStatus[d.device_id].level/5)*100)}%;animation:pulse 1.5s ease-in-out infinite"></div></div></div>`
            :_recoveryStatus[d.device_id]&&_recoveryStatus[d.device_id].exhausted?`<div class="scr-placeholder" style="flex-direction:column;gap:6px"><span style="font-size:22px">&#x26A0;</span><span style="color:#fbbf24;font-weight:600">需要人工干预</span><span style="font-size:10px;color:var(--text-dim)">自动恢复已用尽</span><span style="font-size:10px;margin-top:4px;display:flex;gap:6px"><a href="#" onclick="event.stopPropagation();reconnectDevice('${d.device_id}');return false" style="color:var(--accent)">重连</a><a href="#" onclick="event.stopPropagation();deleteDevice('${d.device_id}');return false" style="color:#ef4444">删除</a></span></div>`
            :`<div class="scr-placeholder" style="flex-direction:column;gap:6px"><span>设备离线</span>${d.last_seen?`<span style="font-size:10px;color:var(--text-dim)">最后在线: ${_formatLastSeen(d.last_seen)}</span>`:''}<span style="font-size:10px;margin-top:4px;display:flex;gap:6px"><a href="#" onclick="event.stopPropagation();reconnectDevice('${d.device_id}');return false" style="color:var(--accent)">重连</a><a href="#" onclick="event.stopPropagation();deleteDevice('${d.device_id}');return false" style="color:#ef4444">删除</a></span></div>`}
      ${_recoveryBanner(d.device_id)}
      ${isOn?_scrOverlayBar(d):''}
      <div class="scr-footer">
        <span class="scr-label"><span class="status-dot ${isOn?'ok':d.usb_issue?'warn':'err'}" style="width:6px;height:6px;${d.usb_issue?'background:#fbbf24':''}"></span>${alias}</span>
        <span style="display:flex;gap:3px;align-items:center">
          <span class="scr-vpn-ind" data-did="${d.device_id}" style="font-size:8px;padding:1px 4px;border-radius:3px;background:rgba(100,116,139,.3);color:var(--text-muted)">VPN</span>
          <button onclick="event.stopPropagation();editDeviceNumber('${d.device_id}')" title="修改编号" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:11px;padding:2px 4px">&#9998;</button>
          ${!isOn?`<button onclick="event.stopPropagation();deleteDevice('${d.device_id}')" title="删除设备" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:11px;padding:2px 4px">&#128465;</button>`:''}
          <span class="scr-st">${isOn?'在线':d.usb_issue?'USB异常':'离线'}</span>
        </span>
      </div>
    </div>`;
  }).join('');

  _initLazyImages();
  _sendVisibleDevices();
  _refreshScreenVpnStatus();
  _refreshScreenTaskStatus();
  _refreshScreenStatsBar(onlineCount, merged.length);
  if(typeof _refreshScrApkPreview==='function') _refreshScrApkPreview();
}

/* ── 屏幕卡片信息叠加层 ── */

const _TASK_LABELS={'tiktok_warmup':'🌱 养号','tiktok_browse_feed':'📺 刷视频','tiktok_follow':'👥 关注','tiktok_send_dm':'💬 私信','tiktok_inbox':'📥 收件箱','tiktok_auto':'🚀 全流程','telegram_send_message':'✉ TG消息','whatsapp_send_message':'✉ WA消息'};

function _scrOverlayBar(d){
  const perf=_devicePerfCache[d.device_id]||{};
  const bat=perf.battery_level;
  const batCls=bat>50?'high':bat>20?'mid':'low';
  const batBar=bat!==undefined?`<div class="scr-bat-bar" title="${bat}%"><div class="scr-bat-fill ${batCls}" style="width:${bat}%"></div></div><span style="min-width:22px">${bat}%</span>`:'';
  const host=d.host_name||'';
  const canShowApk=_scrDevOnlineForApk(d);
  const apkTitle=_scrIsClusterDevice(d)?'安装APK（主控→Worker 转发）':'安装APK（本机 ADB）';
  const apkMini=canShowApk?`<button type="button" title="${apkTitle}" onclick="event.stopPropagation();window._scrCardInstallApk(event,'${d.device_id}')" style="flex-shrink:0;background:rgba(34,197,94,.2);border:1px solid rgba(34,197,94,.45);color:#86efac;border-radius:4px;font-size:10px;padding:1px 5px;cursor:pointer;line-height:1.1">APK</button>`:'';
  return `<div class="scr-overlay-bottom" data-ov-did="${d.device_id}">
    <span class="scr-task-ind" data-did="${d.device_id}" style="flex:1;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;cursor:pointer;pointer-events:auto" onclick="event.stopPropagation();_showTaskDetail('${d.device_id}')">😴 空闲</span>
    ${apkMini}
    ${batBar}
    ${host?`<span style="color:#60a5fa">${host}</span>`:''}
  </div>`;
}

/* ── 编号徽章颜色 — 槽位 + device_id 微调，避免两台同为 04 时颜色完全一致 ── */
function _badgeColor(badgeText, deviceId){
  if(!badgeText||badgeText==='?') return 'rgba(59,130,246,.85)';
  const base=parseInt(badgeText,10);
  let mix=0;
  if(deviceId){
    for(let i=0;i<Math.min(deviceId.length,16);i++) mix=(mix*31+deviceId.charCodeAt(i))>>>0;
  }
  const n=(isNaN(base)?mix:base)+(mix%73);
  const hue=(n*137.508)%360;
  return `hsl(${hue},65%,45%)`;
}

/* ── 右键菜单 ── */
let _ctxMenu=null;
function _showCtxMenu(e,did){
  e.preventDefault();e.stopPropagation();
  _removeCtxMenu();
  const alias=ALIAS[did]||did.substring(0,8);
  const m=document.createElement('div');
  m.className='scr-ctx-menu';
  m.innerHTML=`
    <div onclick="_ctxAction('screenshot','${did}')">📸 截图保存</div>
    <div onclick="_ctxAction('reconnect','${did}')">🔄 重连设备</div>
    <div onclick="_ctxAction('dual_channel','${did}')" style="display:${did.indexOf(':')>=0?'none':'block'}">📡 双通道 (USB+Wi‑Fi)</div>
    <div onclick="_ctxAction('reboot','${did}')">⚡ 重启设备</div>
    <hr>
    <div onclick="_ctxAction('rename','${did}')">✏️ 修改编号</div>
    <div onclick="_ctxAction('wallpaper','${did}')">🖼️ 部署壁纸</div>
    <hr>
    <div onclick="_ctxAction('detail','${did}')">📋 设备详情</div>
    <div onclick="_ctxAction('delete','${did}')" style="color:#ef4444">🗑️ 删除设备</div>
  `;
  m.style.left=Math.min(e.clientX,window.innerWidth-180)+'px';
  m.style.top=Math.min(e.clientY,window.innerHeight-250)+'px';
  document.body.appendChild(m);
  _ctxMenu=m;
  setTimeout(()=>document.addEventListener('click',_removeCtxMenu,{once:true}),50);
}
function _removeCtxMenu(){
  if(_ctxMenu){_ctxMenu.remove();_ctxMenu=null;}
}
async function _ctxAction(action,did){
  _removeCtxMenu();
  const alias=ALIAS[did]||did.substring(0,8);
  const isCluster=allDevices.find(d=>d.device_id===did)?._isCluster;
  const prefix=isCluster?'/cluster':'';
  switch(action){
    case 'screenshot':
      window.open(_apiUrl(prefix+'/devices/'+did+'/screenshot?max_h=1920&quality=90'));break;
    case 'reconnect':
      showToast('正在重连 '+alias+'...');
      try{await api('POST',prefix+'/devices/'+did+'/reconnect');showToast(alias+' 重连成功');}catch(e){showToast('重连失败','error');}
      break;
    case 'dual_channel':
      if(isCluster||prefix){showToast('双通道仅主控本机 USB','warn');break;}
      if(!(await ocDialog({title:'双通道',message:'为该设备建立双通道（tcpip + Wi‑Fi）？',type:'info',confirmText:'建立',cancelText:'取消'})))break;
      await setupDualChannelOne(did);
      break;
    case 'reboot':
      if(!(await ocDialog({title:'重启设备',message:'确定要重启 '+alias+' 吗？',type:'warning',confirmText:'重启',cancelText:'取消'})))return;
      try{await api('POST',prefix+'/devices/'+did+'/shell',{command:'reboot'});showToast(alias+' 正在重启');}catch(e){showToast('重启失败','error');}
      break;
    case 'rename': editDeviceNumber(did);break;
    case 'wallpaper':
      showToast('正在部署壁纸...');
      try{const entry=ALIAS[did]?parseInt((ALIAS[did]).replace(/[^0-9]/g,'')):0;
        await api('POST','/devices/auto-number',{deploy_wallpaper:true});
        showToast('壁纸部署完成');loadAliases();renderScreens();
      }catch(e){showToast('壁纸部署失败','error');}
      break;
    case 'detail': openScreenModal(did);break;
    case 'delete': deleteDevice(did);break;
  }
}

async function _refreshScreenVpnStatus(){
  try{
    const d=await api('GET','/vpn/status');
    for(const v of (d.devices||[])){
      const vid=v.device_id||'';
      const el=vid?[...document.querySelectorAll('.scr-vpn-ind[data-did]')].find(x=>x.dataset.did===vid):null;
      if(el){
        if(v.connected){
          el.textContent='VPN✓';
          el.style.background='rgba(34,197,94,.2)';
          el.style.color='#22c55e';
        }else{
          el.textContent='VPN✗';
          el.style.background='rgba(239,68,68,.2)';
          el.style.color='#ef4444';
        }
      }
      const card=v.device_id?[...document.querySelectorAll('.scr-card[data-did]')].find(c=>c.dataset.did===v.device_id):null;
      if(card && !v.connected && card.classList.contains('st-online')){
        card.classList.remove('st-online');
        card.classList.add('st-vpn-warn');
      }else if(card && v.connected && card.classList.contains('st-vpn-warn')){
        card.classList.remove('st-vpn-warn');
        if(!card.classList.contains('st-busy')) card.classList.add('st-online');
      }
    }
  }catch(e){}
}

async function _refreshScreenTaskStatus(){
  try{
    const tasks=await api('GET','/tasks?status=running');
    const arr=Array.isArray(tasks)?tasks:(tasks.tasks||[]);
    const devTask={};
    for(const t of arr){
      const did=t.device_id;
      if(did) devTask[did]=t;
    }
    document.querySelectorAll('.scr-task-ind[data-did]').forEach(el=>{
      const fullDid=el.dataset.did;
      const match=fullDid&&devTask[fullDid]?fullDid:null;
      const card=el.closest('.scr-card');
      if(match){
        const t=devTask[match];
        const label=_TASK_LABELS[t.type]||t.type;
        el.textContent=label;
        el.style.color='#60a5fa';
        // 切换为蓝色(执行中)
        if(card){
          card.classList.remove('st-online','st-offline','st-vpn-warn');
          card.classList.add('st-busy');
        }
      }else{
        el.textContent='😴 空闲';
        el.style.color='';
        if(card && card.classList.contains('st-busy')){
          card.classList.remove('st-busy');
          card.classList.add('st-online');
        }
      }
    });
  }catch(e){}
}

async function _showTaskDetail(did){
  const alias=ALIAS[did]||did.substring(0,8);
  try{
    const tasks=await api('GET','/tasks?status=running');
    const arr=Array.isArray(tasks)?tasks:(tasks.tasks||[]);
    const task=arr.find(t=>t.device_id===did);
    if(!task){
      showToast(`${alias}: 当前没有运行中的任务`);
      return;
    }
    const label=_TASK_LABELS[task.type]||task.type;
    const elapsed=task.created_at?Math.round((Date.now()/1000)-(new Date(task.created_at).getTime()/1000)):0;
    const mins=Math.floor(elapsed/60);
    const params=task.params||{};
    let paramStr=Object.entries(params).map(([k,v])=>`${k}: ${v}`).join(', ')||'默认参数';
    // 简单弹出信息
    const msg=`设备: ${alias}\n任务: ${label}\n运行时间: ${mins} 分钟\n参数: ${paramStr}\n任务ID: ${task.task_id||'?'}`;
    if(await ocDialog({title:'任务详情',message:msg.replace(/\n/g,'<br>'),type:'info',confirmText:'取消任务',cancelText:'关闭'})){
      try{
        await api('POST',`/tasks/${task.task_id}/cancel`);
        showToast(`${alias}: 任务已取消`,'success');
        _refreshScreenTaskStatus();
      }catch(e){showToast('取消失败: '+e.message,'warn');}
    }
  }catch(e){showToast('查询失败: '+e.message,'warn');}
}

async function _batchQuickTask(taskType){
  const labels={'tiktok_warmup':'养号','tiktok_follow':'关注','tiktok_inbox':'收件箱','tiktok_browse_feed':'刷视频'};
  const sel=[..._selectedDevices];
  if(!sel.length){showToast('请先选择设备（点击设备卡片或使用全选）','warn');return;}
  if(!(await ocDialog({title:'创建任务',message:`为 <b>${sel.length}</b> 台设备创建「${labels[taskType]||taskType}」任务？`,type:'info',confirmText:'创建',cancelText:'取消'}))) return;
  let ok=0;
  for(const did of sel){
    try{
      await api('POST','/tasks',{type:taskType,device_id:did,params:{}});
      ok++;
    }catch(e){}
  }
  showToast(`已创建 ${ok}/${sel.length} 个${labels[taskType]||''}任务`,'success');
  _refreshScreenTaskStatus();
}

async function _refreshScreenStatsBar(onlineCount, totalCount){
  const el=id=>document.getElementById(id);
  if(el('ss-online')) el('ss-online').textContent=`${onlineCount} 在线`;
  // 单请求获取所有统计
  try{
    const s=await api('GET','/screen-stats');
    const run=s.tasks_running||0;
    const idle=Math.max(0,onlineCount-run);
    if(el('ss-tasks')) el('ss-tasks').textContent=`${run} 任务中`;
    if(el('ss-idle')) el('ss-idle').textContent=`${idle} 空闲`;
    if(el('ss-vpn')){
      el('ss-vpn').textContent=`🔒 VPN ${s.vpn_ok||0}/${s.vpn_total||0}`;
      const allOk=s.vpn_ok===s.vpn_total&&s.vpn_total>0;
      el('ss-vpn').style.color=allOk?'var(--green)':s.vpn_ok>0?'var(--yellow)':'var(--red)';
    }
    const avg=s.health_avg||0;
    if(el('ss-health')){
      el('ss-health').textContent=`⚡ 健康 ${avg||'-'}`;
      el('ss-health').style.color=avg>=80?'var(--green)':avg>=50?'var(--yellow)':'var(--red)';
    }
    const tk=s.tiktok||{};
    if(el('ss-today')){
      el('ss-today').textContent=`今日: ${tk.followed||0}关注 ${tk.dms_sent||0}私信 ${tk.watched||0}视频`;
    }
  }catch(e){}
}

function _sendVisibleDevices(){
  try{
    if(window._unifiedWs && window._unifiedWs.readyState===1){
      const imgs=document.querySelectorAll('.scr-img[data-did]');
      const ids=[...imgs].map(i=>i.dataset.did).filter(Boolean);
      window._unifiedWs.send(JSON.stringify({cmd:'set_visible_devices',ids}));
    }
  }catch(e){}
}

function _initLazyImages(){}

async function refreshAllScreens(){
  await loadDevices();
  renderScreens();
}

/** 双通道：对本机所有在线 USB 设备执行 tcpip 5555 + adb connect（需手机与电脑同局域网） */
async function setupDualChannelAll(){
  if(!(await ocDialog({title:'全部双通道',message:'对当前所有 USB 在线设备开启双通道（tcpip 5555 + Wi‑Fi）？<br>手机需已连 Wi‑Fi 且与电脑同网段。',type:'info',confirmText:'开启',cancelText:'取消'})))return;
  showToast('正在建立双通道...');
  try{
    const r=await api('POST','/devices/dual-channel/setup',{all_usb:true});
    const n=(r.results||[]).filter(x=>x.ok).length;
    const t=(r.results||[]).length;
    let extra='';
    if(Array.isArray(r.workers)&&r.workers.length){
      const wn=r.workers.filter(w=>w.ok!==false&&!w.error).length;
      extra+=' · Worker '+wn+'/'+r.workers.length;
    }
    showToast((r.ok?`双通道完成 ${n}/${t}`:`双通道部分失败 ${n}/${t}`)+extra,r.ok?'success':'warn');
    await refreshAllScreens();
  }catch(e){
    showToast('双通道失败: '+(e.message||e),'error');
  }
}

/** 单台 USB 设备双通道（右键菜单调用） */
async function setupDualChannelOne(deviceId){
  if(!deviceId||deviceId.indexOf(':')>=0){
    showToast('仅支持 USB 序列号设备','warn');
    return;
  }
  showToast('双通道: '+deviceId.substring(0,8)+'...');
  try{
    const r=await api('POST','/devices/dual-channel/setup',{device_id:deviceId});
    const row=(r.results||[])[0];
    showToast(row&&row.ok?'双通道已建立':'双通道未完全成功',row&&row.ok?'success':'warn');
    await refreshAllScreens();
  }catch(e){
    showToast('双通道失败: '+(e.message||e),'error');
  }
}
function _quickRefreshScreenshots(){
  const tp=_thumbParams();
  document.querySelectorAll('.scr-img').forEach(img=>{
    const did=img.dataset.did||img.id.replace('scr-','');
    if(!did||img.dataset._scrPending) return; // 跳过正在加载的
    const prefix=img.dataset.src&&img.dataset.src.indexOf('/cluster/')>=0?'/cluster':'';
    const newSrc=_apiUrl(`${prefix}/devices/${did}/screenshot?${tp}&t=${Date.now()}`);
    const token=Date.now()+'-'+did;
    img.dataset._scrPending=token;
    const pre=new Image();
    pre.onload=function(){
      if(!img.isConnected||img.dataset._scrPending!==token) return;
      // Fade 过渡: 淡出→替换→淡入
      img.style.transition='opacity 0.25s ease';
      img.style.opacity='0.6';
      setTimeout(()=>{
        img.src=newSrc;
        img.dataset.src=newSrc;
        img.style.opacity='1';
        delete img.dataset._scrPending;
      },250);
    };
    pre.onerror=function(){
      if(img.dataset._scrPending===token) delete img.dataset._scrPending;
    };
    pre.src=newSrc;
  });
}

async function checkAllAnomalies(){
  showToast('正在检测所有设备异常...');
  try{
    const r=await api('POST','/devices/anomaly/check-all',{});
    const bar=document.getElementById('anomaly-bar');
    if(r.anomalies&&r.anomalies.length>0){
      bar.classList.add('active');
      const typeIcons={captcha:'\u26A0',ban:'\u26D4',login:'\uD83D\uDD11',popup:'\uD83D\uDCAC',network:'\uD83C\uDF10',update:'\u2B06',crash:'\uD83D\uDCA5'};
      const typeNames={captcha:'验证码',ban:'封禁',login:'需登录',popup:'弹窗',network:'网络错误',update:'需更新',crash:'崩溃'};
      bar.innerHTML='<span style="font-size:12px;font-weight:600;color:#ef4444">\u26A0 发现 '+r.anomalies.length+' 个异常:</span>'+
        r.anomalies.map(a=>{
          const alias=ALIAS[a.device_id]||a.device_id.substring(0,8);
          return `<span class="anomaly-item ${a.severity}">${typeIcons[a.type]||'\u2753'} ${alias}: ${typeNames[a.type]||a.type} (${Math.round(a.confidence*100)}%)</span>`;
        }).join('');
      showToast(`检测完成: 发现 ${r.anomalies.length} 个异常`,'warn');
    }else{
      bar.classList.remove('active');
      bar.innerHTML='';
      showToast('所有设备正常 \u2705');
    }
  }catch(e){showToast('异常检测失败','warn');}
}

async function checkDeviceAnomaly(deviceId){
  try{
    const r=await api('POST',`/devices/${deviceId}/anomaly/check`,{use_vision:true});
    if(r.anomaly){
      const typeNames={captcha:'验证码',ban:'封禁',login:'需登录',popup:'弹窗',network:'网络错误',update:'需更新',crash:'崩溃'};
      showToast(`\u26A0 ${typeNames[r.type]||r.type}: ${r.description}`,'warn');
    }else{
      showToast('设备状态正常 \u2705');
    }
    return r;
  }catch(e){return null;}
}

function _autoRefreshInterval(){
  const n=allDevices.length;
  if(n<=10) return 2000;
  if(n<=30) return 3000;
  if(n<=100) return 5000;
  return 8000;
}
function toggleAutoRefreshAll(){
  if(document.getElementById('auto-refresh-all').checked){
    allRefreshTimer=setInterval(_smartRefreshScreenshots,_autoRefreshInterval());
  }else{
    if(allRefreshTimer){clearInterval(allRefreshTimer);allRefreshTimer=null;}
  }
}
function _autoEnableScreenRefresh(){
  const cb=document.getElementById('auto-refresh-all');
  if(cb&&!cb.checked){cb.checked=true;toggleAutoRefreshAll();}
}
async function _smartRefreshScreenshots(){
  // 仅刷新缩略图，避免每 2～8 秒 loadDevices 导致 ADB 列表抖动、卡片被整页重绘后「消失」
  _quickRefreshScreenshots();
}

