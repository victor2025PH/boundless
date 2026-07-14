/* device-mgmt.js — 设备高级管理: 设备分组、设备编号、批量APK安装、批量文字输入、应用管理器、设备资产 */
/** 集群 APK 主控转发 Worker（与 POST /cluster/batch/install-apk 等价，挂在 /batch 便于反代） */
const OC_CLUSTER_BATCH_INSTALL='/batch/install-apk-cluster';
const OC_CLUSTER_BATCH_INSTALL_FALLBACK='/cluster/batch/install-apk';
const _OC_APK_POST_TIMEOUT_MS=420000;

/** 集群 APK：优先 /batch/install-apk-cluster（易过反代），404 时尝试 /cluster/batch/install-apk */
async function postClusterApkInstall(body,timeoutMs){
  try{
    return await api('POST',OC_CLUSTER_BATCH_INSTALL,body,timeoutMs);
  }catch(e1){
    const m1=String(e1&&e1.message||e1);
    if(!/404|not found/i.test(m1)) throw _formatApkInstallErr(e1);
    try{
      return await api('POST',OC_CLUSTER_BATCH_INSTALL_FALLBACK,body,timeoutMs);
    }catch(e2){
      throw _formatApkInstallErr(e2,true,m1);
    }
  }
}
function _formatApkInstallErr(e,triedFallback,firstMsg){
  const m=String(e&&e.message||e);
  if(/404|not found/i.test(m)){
    let msg='集群 APK 主控接口不可用（404）。请确认：① 主控已用最新代码并重启 ② 反向代理放行 POST '+OC_CLUSTER_BATCH_INSTALL+' 与 '+OC_CLUSTER_BATCH_INSTALL_FALLBACK+' ③ 浏览器 Ctrl+F5 强刷。可在浏览器打开 GET /health 核对 capabilities。';
    if(triedFallback&&firstMsg) msg+=' 已尝试备用路径仍失败。首次：'+firstMsg.slice(0,120)+' 末次：'+m.slice(0,120);
    return new Error(msg);
  }
  return e instanceof Error?e:new Error(m);
}
/* ── 设备分组页 ── */
let _groupsData=[];
async function loadGroupsPage(){
  try{
    const r=await api('GET','/device-groups');
    _groupsData=r.groups||[];
    renderGroups();
    _populateGroupFilter();
  }catch(e){console.error('groups',e);}
}
function _populateGroupFilter(){
  const sel=document.getElementById('dev-group-filter');
  if(!sel)return;
  const cur=sel.value;
  sel.innerHTML='<option value="">全部分组</option>'+
    _groupsData.map(g=>`<option value="${g.id}" ${g.id===cur?'selected':''}>${g.name} (${(g.devices||[]).length})</option>`).join('');
}
async function _loadGroupsForFilter(){
  try{const r=await api('GET','/device-groups');_groupsData=r.groups||[];_populateGroupFilter();}catch(e){}
}
function showCreateGroupForm(){document.getElementById('create-group-form').style.display='block';}
async function createGroup(){
  const name=document.getElementById('group-name').value.trim();
  if(!name){showToast('请输入分组名称','warn');return;}
  const color=document.getElementById('group-color').value;
  try{
    await api('POST','/device-groups',{name,color});
    showToast('分组已创建');
    document.getElementById('create-group-form').style.display='none';
    loadGroupsPage();
  }catch(e){showToast('创建失败','warn');}
}
async function deleteGroup(gid){
  if(!(await ocDialog({title:'删除分组',message:'确认删除此分组？',type:'danger',confirmText:'删除',dangerous:true}))) return;
  try{await api('DELETE',`/device-groups/${gid}`);showToast('已删除');loadGroupsPage();}catch(e){showToast('删除失败','warn');}
}
async function addDeviceToGroup(gid){
  const group=_groupsData.find(g=>g.id===gid);
  const existing=new Set(group?.devices||[]);
  const online=allDevices.filter(d=>!existing.has(d.device_id));
  if(!online.length){showToast('没有可添加的设备','warn');return;}
  const opts=online.map(d=>`${ALIAS[d.device_id]||d.device_id.substring(0,8)}`).join(', ');
  const pick=await ocPrompt('添加设备','',{message:'输入设备名（可逗号分隔批量添加）<br><span style="font-size:10px;color:var(--text-muted)">'+opts+'</span>',inputPlaceholder:'设备名称'});
  if(!pick)return;
  const picks=pick.split(',').map(s=>s.trim()).filter(Boolean);
  const matchIds=[];
  picks.forEach(p=>{
    const match=online.find(d=>d.device_id.startsWith(p)||(ALIAS[d.device_id]||'').includes(p));
    if(match)matchIds.push(match.device_id);
  });
  if(!matchIds.length){showToast('未匹配到设备','warn');return;}
  try{
    await api('POST',`/device-groups/${gid}/batch-add`,{device_ids:matchIds});
    showToast(`已添加 ${matchIds.length} 台`);loadGroupsPage();
  }catch(e){showToast('添加失败','warn');}
}
async function removeDeviceFromGroup(gid,did){
  try{await api('DELETE',`/device-groups/${gid}/devices/${did}`);loadGroupsPage();}catch(e){showToast('移除失败','warn');}
}
async function batchGroupTask(gid,taskType){
  try{
    const r=await api('POST',`/device-groups/${gid}/batch-task`,{task_type:taskType});
    showToast(`已创建 ${r.created||0} 个任务`);
  }catch(e){showToast('批量任务失败','warn');}
}
function renderGroups(){
  const container=document.getElementById('groups-container');
  container.innerHTML=_groupsData.map(g=>{
    const devList=(g.devices||[]).map(did=>{
      const alias=ALIAS[did]||did.substring(0,8);
      return `<div style="display:flex;align-items:center;gap:4px;padding:3px 6px;background:rgba(255,255,255,.04);border-radius:4px;font-size:10px">
        <span>${alias}</span>
        <button onclick="removeDeviceFromGroup('${g.id}','${did}')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:9px">✕</button>
      </div>`;
    }).join('');
    return `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;border-left:4px solid ${g.color||'#60a5fa'}">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div style="font-size:14px;font-weight:600">${g.name} <span style="font-size:10px;color:var(--text-muted)">(${(g.devices||[]).length} 台)</span></div>
        <div style="display:flex;gap:4px">
          <button class="sb-btn2" onclick="addDeviceToGroup('${g.id}')" style="font-size:9px">+ 添加设备</button>
          <button class="sb-btn2" onclick="deleteGroup('${g.id}')" style="font-size:9px;color:#ef4444">删除</button>
        </div>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:10px">${devList||'<span style="font-size:10px;color:var(--text-muted)">无设备</span>'}</div>
      <div style="display:flex;gap:4px;flex-wrap:wrap">
        <button class="sb-btn2" onclick="batchGroupTask('${g.id}','warmup')" style="font-size:9px">全部养号</button>
        <button class="sb-btn2" onclick="batchGroupTask('${g.id}','watch_videos')" style="font-size:9px">全部刷视频</button>
        <button class="sb-btn2" onclick="batchGroupTask('${g.id}','follow_users')" style="font-size:9px">全部关注</button>
      </div>
    </div>`;
  }).join('')||'<div style="padding:20px;text-align:center;color:var(--text-muted);font-size:12px">暂无分组，点击"+ 新建分组"创建</div>';
}


/* ── 设备编号管理 ── */
async function autoNumberDevices(deployWp){
  try{
    const r=await api('POST','/devices/auto-number',{deploy_wallpaper:!!deployWp});
    await loadAliases();
    showToast(`已为 ${r.total} 台设备自动编号${deployWp?' 并部署壁纸':''}`);
    loadDevices();
  }catch(e){showToast('自动编号失败','warn');}
}
async function setDeviceAlias(did){
  const alias=await ocPrompt('设备别名','',{message:'例如：01号',inputPlaceholder:'别名'});
  if(!alias) return;
  const number=parseInt(alias)||0;
  try{
    await api('POST',`/devices/${did}/alias`,{alias,number,remark:''});
    await loadAliases();
    showToast('别名已设置');
    loadDevices();
  }catch(e){showToast('设置失败','warn');}
}

/* ── 批量APK安装 ── */
let _apkFile=null;
function _apkLocalOnline(d){
  const on=d&&(d.status==='connected'||d.status==='online'||d.is_online===true);
  return on&&!d._isCluster;
}
function loadBatchApkPage(){
  _apkFile=null;
  document.getElementById('apk-selected-info').style.display='none';
  document.getElementById('apk-progress').innerHTML='';
  const sel=document.getElementById('apk-target');
  sel.innerHTML='<option value="all">全部在线（本机 ADB）</option>';
  (allDevices||[]).filter(_apkLocalOnline).forEach(d=>{
    const alias=ALIAS[d.device_id]||d.display_name||d.device_id.substring(0,8);
    sel.innerHTML+=`<option value="${d.device_id}">${alias}（单机）</option>`;
  });
}
function handleApkDrop(e){
  const f=e.dataTransfer.files[0];
  if(f&&f.name.endsWith('.apk'))_setApkFile(f);
}
function handleApkSelect(input){
  if(input.files[0])_setApkFile(input.files[0]);
}
function _setApkFile(f){
  _apkFile=f;
  document.getElementById('apk-file-name').textContent=f.name;
  document.getElementById('apk-file-size').textContent=`(${(f.size/1024/1024).toFixed(1)} MB)`;
  document.getElementById('apk-selected-info').style.display='block';
}
async function _apkFileToBase64(file){
  const buf=await file.arrayBuffer();
  return btoa(new Uint8Array(buf).reduce((s,b)=>s+String.fromCharCode(b),''));
}
function _escHtml(s){
  if(s==null||s==='') return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
function _apkOneResultRow(did,info){
  const alias=_escHtml(ALIAS[did]||did.substring(0,8));
  const color=info.success?'var(--green)':'var(--red)';
  return `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:8px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:12px;font-weight:600">${alias}</span>
        <span style="color:${color};font-size:11px">${info.success?'成功':'失败'}</span>
      </div>
      <div style="font-size:10px;color:var(--text-dim);margin-top:4px;word-break:break-all">${_escHtml(info.message)}</div>
    </div>`;
}
function _renderApkResultsToContainer(r, containerEl){
  const res=r.results||{};
  const hosts=r.hosts;
  let html='';
  const covered=new Set();
  if(hosts&&hosts.length){
    for(const h of hosts){
      const label=_escHtml((h.host_name||h.host_id||'Worker').toString().slice(0,40));
      const ids=h.device_ids||[];
      let okc=0;
      for(const did of ids){
        if((res[did]||{}).success) okc++;
      }
      const tot=h.total!=null?h.total:ids.length;
      const okh=h.ok!=null?h.ok:okc;
      const headExtra=h.error?`<span style="color:var(--red);font-weight:400;margin-left:8px">${_escHtml(h.error.slice(0,120))}</span>`:'';
      html+=`<div style="margin-bottom:12px;border:1px solid var(--border);border-radius:10px;overflow:hidden">
        <div style="background:rgba(59,130,246,.12);padding:6px 10px;font-size:11px;font-weight:600;color:#93c5fd;display:flex;flex-wrap:wrap;align-items:center;gap:6px">
          <span>Worker ${label}</span>
          <span style="color:var(--text-muted);font-weight:400">${okh}/${tot} 成功</span>
          ${headExtra}
        </div>
        <div style="padding:8px;display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px">`;
      for(const did of ids){
        covered.add(did);
        html+=_apkOneResultRow(did,res[did]||{success:false,message:'无返回'});
      }
      html+='</div></div>';
    }
  }
  const orphan=[];
  for(const did of Object.keys(res)){
    if(!covered.has(did)) orphan.push(did);
  }
  if(orphan.length){
    html+=`<div style="margin-bottom:8px;font-size:11px;font-weight:600;color:var(--text-muted)">主控校验 / 其它</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px">`;
    for(const did of orphan){
      html+=_apkOneResultRow(did,res[did]||{});
    }
    html+='</div>';
  }
  if(!html){
    for(const[did,info] of Object.entries(res)){
      html+=_apkOneResultRow(did,info);
    }
  }
  containerEl.innerHTML=html||'<div style="font-size:11px;color:var(--text-muted)">无结果</div>';
}
async function batchInstallApk(){
  if(!_apkFile){showToast('请先选择APK文件','warn');return;}
  const target=document.getElementById('apk-target').value;
  if(_apkFile.size>96*1024*1024&&!(await ocDialog({title:'APK 较大',message:`APK 约 ${(_apkFile.size/1024/1024).toFixed(0)} MB，上传可能较慢。`,type:'warning',confirmText:'继续上传',cancelText:'取消'}))) return;
  const prog=document.getElementById('apk-progress');
  prog.innerHTML='<div style="color:var(--accent)">正在上传并安装中，请稍候...</div>';
  try{
    const b64=await _apkFileToBase64(_apkFile);
    const body=target==='all'
      ?{apk_data:b64,filename:_apkFile.name,target:'all'}
      :{apk_data:b64,filename:_apkFile.name,device_ids:[target]};
    const r=await api('POST','/batch/install-apk',body);
    _renderApkResultsToContainer(r,prog);
    showToast(`安装完成: ${r.success}/${r.total} 成功`);
  }catch(e){prog.innerHTML=`<div style="color:var(--red)">安装失败: ${e.message}</div>`;}
}
let _scrCardApkTargetDid=null;
function _scrCardInstallApk(ev,deviceId){
  if(ev){ev.preventDefault();ev.stopPropagation();}
  const d=(allDevices||[]).find(x=>x.device_id===deviceId);
  if(!d){showToast('找不到设备','warn');return;}
  if(d.status!=='connected'&&d.status!=='online'){showToast('设备非在线','warn');return;}
  _scrCardApkTargetDid=deviceId;
  let inp=document.getElementById('scr-card-apk-file');
  if(!inp){
    inp=document.createElement('input');
    inp.type='file';
    inp.id='scr-card-apk-file';
    inp.accept='.apk,.APK';
    inp.style.display='none';
    inp.addEventListener('change',_scrCardApkFileChosen);
    document.body.appendChild(inp);
  }
  inp.value='';
  inp.click();
}
async function _scrCardApkFileChosen(){
  const inp=document.getElementById('scr-card-apk-file');
  const did=_scrCardApkTargetDid;
  const f=inp&&inp.files&&inp.files[0];
  if(!did||!f) return;
  if(!f.name.toLowerCase().endsWith('.apk')){showToast('请选择 .apk 文件','warn');return;}
  if(f.size>96*1024*1024&&!(await ocDialog({title:'APK 较大',message:`APK 约 ${(f.size/1024/1024).toFixed(0)} MB，上传可能较慢。`,type:'warning',confirmText:'继续',cancelText:'取消'}))) return;
  const alias=ALIAS[did]||did.substring(0,8);
  if(!(await ocDialog({title:'安装 APK',message:`将「${f.name}」安装到 ${alias} ？`,type:'info',confirmText:'安装',cancelText:'取消'}))) return;
  showToast('上传并安装中…');
  try{
    const b64=await _apkFileToBase64(f);
    const dev=(allDevices||[]).find(x=>x.device_id===did);
    const body={apk_data:b64,filename:f.name,device_ids:[did]};
    const r=dev&&(dev._isCluster||(dev.host_id&&dev.host_id!=='__local__'))
      ?await postClusterApkInstall(body,_OC_APK_POST_TIMEOUT_MS)
      :await api('POST','/batch/install-apk',body,_OC_APK_POST_TIMEOUT_MS);
    const row=(r.results||{})[did];
    if(row&&row.success) showToast(`${alias} 安装成功`);
    else showToast((row&&row.message)||'安装失败','warn');
    try{renderScreens();}catch(e){}
  }catch(e){showToast(e.message||'请求失败','warn');}
}
window._scrCardInstallApk=_scrCardInstallApk;
let _screenApkLastFile=null,_screenApkFailedIds=[],_screenApkUseClusterForward=false;
let _scrApkHealthCache=null,_scrApkHealthCacheTs=0;
function _applyScrApkCapHint(box, ok, detail){
  if(!box) return;
  if(ok){
    box.style.display='block';
    box.style.borderColor='rgba(34,197,94,.35)';
    box.style.background='rgba(34,197,94,.1)';
    box.innerHTML='<span style="color:#86efac;font-weight:600">✓ 主控 /health 已声明集群 APK 能力</span>'+(detail?'<span style="color:var(--text-muted);margin-left:8px">'+detail+'</span>':'');
  }else{
    box.style.display='block';
    box.style.borderColor='rgba(239,68,68,.4)';
    box.style.background='rgba(239,68,68,.08)';
    box.innerHTML='<span style="color:#fca5a5;font-weight:600">✗ 集群 APK 接口可能未部署</span> <span style="color:var(--text-dim)">'+detail+'</span>';
  }
}
async function updateScrApkCapabilityBanner(){
  const box=document.getElementById('scr-apk-cap-hint');
  if(!box) return;
  const modeEl=document.querySelector('input[name="scr-apk-mode"]:checked');
  const mode=modeEl?modeEl.value:'page';
  if(mode!=='cluster_selected'){
    box.style.display='none';
    box.innerHTML='';
    return;
  }
  const now=Date.now();
  if(_scrApkHealthCache && now-_scrApkHealthCacheTs<30000){
    _applyScrApkCapHint(box,_scrApkHealthCache.ok,_scrApkHealthCache.detail);
    return;
  }
  box.style.display='block';
  box.style.borderColor='var(--border)';
  box.style.background='rgba(59,130,246,.08)';
  box.innerHTML='<span style="color:var(--accent)">正在检查 GET /health …</span>';
  try{
    const h=await api('GET','/health');
    const c=(h&&h.capabilities)||{};
    const ok=!!c.post_batch_install_apk_cluster && !!c.post_cluster_batch_install_apk;
    const bid=(h&&h.build_id)||'';
    const ver=(h&&h.version)||'';
    let detail='';
    if(ver) detail+='version '+_escHtml(String(ver));
    if(bid) detail+=(detail?' · ':'')+'build '+_escHtml(String(bid));
    if(ok){
      _scrApkHealthCache={ok:true,detail}; _scrApkHealthCacheTs=now;
      _applyScrApkCapHint(box,true,detail);
    }else{
      const miss=[];
      if(!c.post_batch_install_apk_cluster) miss.push('post_batch_install_apk_cluster');
      if(!c.post_cluster_batch_install_apk) miss.push('post_cluster_batch_install_apk');
      const d='capabilities 缺少或未开启: '+miss.join(', ')+'。请用最新代码重启主控；若仍 404 再查反代是否放行 POST /batch/install-apk-cluster。';
      _scrApkHealthCache={ok:false,detail:d}; _scrApkHealthCacheTs=now;
      _applyScrApkCapHint(box,false,_escHtml(d));
    }
  }catch(e){
    const msg=_escHtml(String(e&&e.message||e));
    _scrApkHealthCache={ok:false,detail:msg}; _scrApkHealthCacheTs=now;
    _applyScrApkCapHint(box,false,'无法访问 /health：'+msg);
  }
}
function _refreshScrApkPreview(){
  const el=document.getElementById('scr-apk-preview-count');
  if(!el) return;
  if(typeof maybeAutoSelectClusterApkMode==='function') maybeAutoSelectClusterApkMode();
  const modeEl=document.querySelector('input[name="scr-apk-mode"]:checked');
  const mode=modeEl?modeEl.value:'page';
  let n=0;
  let extra='';
  let titleHint='';
  try{
    n=typeof getScreenApkTargetIds==='function'?getScreenApkTargetIds(mode).length:0;
    const nCl=typeof getScreenApkTargetIds==='function'?getScreenApkTargetIds('cluster_selected').length:0;
    const nLoc=typeof getScreenApkTargetIds==='function'?getScreenApkTargetIds('selected').length:0;
    if(mode==='selected'&&n===0&&nCl>0){
      extra=' [+'+nCl+' 集群]';
      titleHint='当前为「群控已选·本机」只统计 USB；已勾选 '+nCl+' 台集群机，请点「群控已选→集群」或直接开始安装（将自动走集群转发）。';
    }else if(mode==='cluster_selected'&&n===0&&nLoc>0){
      extra=' [+'+nLoc+' 本机]';
      titleHint='请点选「群控已选·本机」安装本机 USB。';
    }
  }catch(e){n=0;}
  el.textContent='当前将安装: '+n+' 台'+extra;
  el.title=titleHint||'';
  el.style.color=n?'var(--accent)':'var(--text-muted)';
  if(mode==='cluster_selected') void updateScrApkCapabilityBanner();
  else{
    const cap=document.getElementById('scr-apk-cap-hint');
    if(cap){cap.style.display='none'; cap.innerHTML='';}
  }
}
async function _onScrApkFilePicked(){
  const inp=document.getElementById('scr-apk-file');
  const f=inp&&inp.files&&inp.files[0];
  if(f&&f.size>96*1024*1024&&!(await ocDialog({title:'APK 较大',message:`APK 约 ${(f.size/1024/1024).toFixed(0)} MB，上传可能较慢。`,type:'warning',confirmText:'继续',cancelText:'取消'}))){
    inp.value='';
    return;
  }
  _refreshScrApkPreview();
}
function _updateScrApkRetryUI(r,file){
  _screenApkLastFile=file||_screenApkLastFile;
  _screenApkFailedIds=Object.entries(r.results||{}).filter(([,v])=>!v.success).map(([id])=>id);
  const w=document.getElementById('scr-apk-retry-wrap');
  if(!w) return;
  if(_screenApkFailedIds.length&&_screenApkLastFile){
    w.innerHTML=`<button type="button" class="sb-btn2" onclick="retryScreenApkFailed()" style="margin-top:6px;font-size:10px;color:#f59e0b;border-color:#f59e0b">↻ 仅重试失败 (${_screenApkFailedIds.length} 台)</button>`;
  }else w.innerHTML='';
}
async function retryScreenApkFailed(){
  if(!_screenApkLastFile||!_screenApkFailedIds.length){showToast('无可重试的失败记录或缺少 APK 缓存','warn');return;}
  if(!(await ocDialog({title:'重试安装',message:`对 ${_screenApkFailedIds.length} 台重试安装「${_screenApkLastFile.name}」？`,type:'info',confirmText:'重试',cancelText:'取消'}))) return;
  const prog=document.getElementById('scr-apk-progress');
  if(prog) prog.innerHTML='<div style="color:var(--accent);font-size:11px">重试中…</div>';
  try{
    const b64=await _apkFileToBase64(_screenApkLastFile);
    const body={apk_data:b64,filename:_screenApkLastFile.name,device_ids:_screenApkFailedIds};
    const r=_screenApkUseClusterForward
      ?await postClusterApkInstall(body,_OC_APK_POST_TIMEOUT_MS)
      :await api('POST','/batch/install-apk',body,_OC_APK_POST_TIMEOUT_MS);
    if(prog) _renderApkResultsToContainer(r,prog);
    _updateScrApkRetryUI(r,_screenApkLastFile);
    showToast(`重试完成: ${r.success}/${r.total} 成功`);
    try{renderScreens();}catch(e){}
  }catch(e){
    if(prog) prog.innerHTML=`<div style="color:var(--red);font-size:11px">${e.message||e}</div>`;
  }
}
async function installApkToModalDevice(){
  const inp=document.getElementById('modal-apk-file');
  const st=document.getElementById('modal-apk-status');
  const f=inp&&inp.files&&inp.files[0];
  if(typeof modalDeviceId==='undefined'||!modalDeviceId){showToast('无当前远控设备','warn');return;}
  if(!f||!f.name.toLowerCase().endsWith('.apk')){showToast('请先点「选APK」选择文件','warn');return;}
  const d=typeof allDevices!=='undefined'&&allDevices.find?allDevices.find(x=>x.device_id===modalDeviceId):null;
  const on=d&&(d.status==='connected'||d.status==='online');
  if(!on){showToast('当前设备非在线，无法安装','warn');return;}
  if(f.size>96*1024*1024&&!(await ocDialog({title:'APK 较大',message:`APK 约 ${(f.size/1024/1024).toFixed(0)} MB，可能耗时较长。`,type:'warning',confirmText:'继续',cancelText:'取消'}))) return;
  if(!(await ocDialog({title:'安装 APK',message:`安装「${f.name}」到当前设备？`,type:'info',confirmText:'安装',cancelText:'取消'}))) return;
  if(st) st.textContent='安装中…';
  try{
    const b64=await _apkFileToBase64(f);
    const body={apk_data:b64,filename:f.name,device_ids:[modalDeviceId]};
    const r=d&&(d._isCluster||(d.host_id&&d.host_id!=='__local__'))
      ?await postClusterApkInstall(body,_OC_APK_POST_TIMEOUT_MS)
      :await api('POST','/batch/install-apk',body,_OC_APK_POST_TIMEOUT_MS);
    const row=(r.results||{})[modalDeviceId];
    if(st) st.textContent=row&&row.success?'✓ 安装成功':('✗ '+(row&&row.message||'失败').slice(0,80));
    showToast(row&&row.success?'安装成功':'安装失败',row&&row.success?'':'warn');
  }catch(e){
    if(st) st.textContent='错误: '+(e.message||e).slice(0,80);
    showToast('安装请求失败','warn');
  }
}
function toggleScrApkPanel(){
  const p=document.getElementById('scr-apk-panel');
  if(!p) return;
  const hidden=p.style.display!=='block';
  p.style.display=hidden?'block':'none';
  if(hidden){
    _refreshScrApkPreview();
    const m=document.querySelector('input[name="scr-apk-mode"]:checked');
    if(m&&m.value==='cluster_selected') void updateScrApkCapabilityBanner();
  }
}
async function runScreenInstallApk(){
  const inp=document.getElementById('scr-apk-file');
  const f=inp&&inp.files&&inp.files[0];
  if(!f||!f.name.toLowerCase().endsWith('.apk')){showToast('请选择 .apk 文件','warn');return;}
  if(f.size>96*1024*1024&&!(await ocDialog({title:'APK 较大',message:`APK 约 ${(f.size/1024/1024).toFixed(0)} MB，上传可能较慢。`,type:'warning',confirmText:'继续',cancelText:'取消'}))) return;
  if(typeof ensureClusterApkModeFromSelection==='function') ensureClusterApkModeFromSelection();
  let modeEl=document.querySelector('input[name="scr-apk-mode"]:checked');
  let mode=modeEl?modeEl.value:'page';
  let ids=typeof getScreenApkTargetIds==='function'?getScreenApkTargetIds(mode):[];
  /* 群控已选·本机 但勾选的全是集群机：单选框可能未切成功，直接按集群列表安装 */
  if(mode==='selected'&&!ids.length&&typeof getScreenApkTargetIds==='function'){
    const clusterIds=getScreenApkTargetIds('cluster_selected');
    if(clusterIds.length){
      ids=clusterIds;
      mode='cluster_selected';
      const r=document.querySelector('input[name="scr-apk-mode"][value="cluster_selected"]');
      if(r) r.checked=true;
    }
  }
  if(mode==='selected'&&!ids.length){
    const raw=typeof getScrGroupSelectedCount==='function'?getScrGroupSelectedCount():0;
    if(raw===0){
      showToast('请先开启「群控模式」并勾选要安装的本机设备（仅本机 USB ADB 直连）','warn');
    }else{
      showToast('群控已勾选 '+raw+' 台，但其中没有本机 USB 目标，也没有可转发的在线集群目标。请确认「显示集群设备」与 Worker 在线。','warn');
    }
    return;
  }
  if(mode==='cluster_selected'&&!ids.length){
    const raw=typeof getScrGroupSelectedCount==='function'?getScrGroupSelectedCount():0;
    if(raw===0){
      showToast('请先开启「群控模式」并勾选集群 Worker 上的设备','warn');
    }else{
      showToast('所勾选中没有在线的集群设备（请确认已勾选「显示集群设备」且 Worker 在线；本机 USB 请用「群控已选·本机」）','warn');
    }
    return;
  }
  if(!ids.length){
    const nCl=typeof getScreenApkTargetIds==='function'?getScreenApkTargetIds('cluster_selected').length:0;
    const onPageCl=typeof getScreenApkClusterOnlineOnPageCount==='function'?getScreenApkClusterOnlineOnPageCount():0;
    const labelPage=mode==='page'?'当前页在线（只装本机 USB）':mode==='all'?'全部在线（只装本机 USB）':'此模式';
    if((mode==='page'||mode==='all')&&nCl>0){
      showToast('你勾选了 '+nCl+' 台集群机，但安装范围仍是「'+labelPage+'」。请点选「群控已选→集群」后再开始安装。','warn');
    }else if((mode==='page'||mode==='all')&&onPageCl>0&&nCl===0){
      showToast('本页没有本机在线设备；要装 '+onPageCl+' 台集群机请先开「群控模式」勾选它们，再选「群控已选→集群」。','warn');
    }else if(mode==='page'||mode==='all'){
      showToast('当前范围内没有在线的本机 USB 设备。集群手机请用「群控已选→集群」并勾选目标。','warn');
    }else{
      showToast('没有可安装的目标设备，请检查模式、群控勾选与设备在线状态。','warn');
    }
    return;
  }
  const label={selected:'群控已选·本机',cluster_selected:'群控已选→集群',page:'当前页',all:'全部在线'}[mode]||mode;
  const clusterHint=mode==='cluster_selected'
    ?'\n\n主控将把 APK 转发到各 Worker 执行 adb install（需各 Worker 在线且 OPENCLAW_API_KEY 一致）。'
    :'\n\n仅本机 ADB 直连；集群机请用「群控已选→集群」。';
  if(!(await ocDialog({title:'批量安装 APK',message:`将安装「${f.name}」到 <b>${ids.length}</b> 台设备（${label}）`+(mode==='cluster_selected'?'<br><br>主控将把 APK 转发到各 Worker 执行。':''),type:'info',confirmText:'安装',cancelText:'取消'}))) return;
  const prog=document.getElementById('scr-apk-progress');
  const rw=document.getElementById('scr-apk-retry-wrap');
  if(rw) rw.innerHTML='';
  _screenApkLastFile=null;
  _screenApkFailedIds=[];
  _screenApkUseClusterForward=(mode==='cluster_selected');
  if(prog) prog.innerHTML='<div style="color:var(--accent);font-size:11px">上传并安装中…</div>';
  try{
    const b64=await _apkFileToBase64(f);
    const body={apk_data:b64,filename:f.name,device_ids:ids};
    const r=mode==='cluster_selected'
      ?await postClusterApkInstall(body,_OC_APK_POST_TIMEOUT_MS)
      :await api('POST','/batch/install-apk',body,_OC_APK_POST_TIMEOUT_MS);
    if(prog) _renderApkResultsToContainer(r,prog);
    _updateScrApkRetryUI(r,f);
    showToast(`安装完成: ${r.success}/${r.total} 成功`);
    try{renderScreens();}catch(e){}
  }catch(e){
    const msg=(e&&e.message)||String(e);
    if(prog) prog.innerHTML='<div style="color:var(--red);font-size:11px;word-break:break-word">'+msg+'</div>';
    showToast(msg.length>160?msg.slice(0,160)+'…':msg,'warn');
  }
}
window._refreshScrApkPreview=_refreshScrApkPreview;
window._onScrApkFilePicked=_onScrApkFilePicked;

/* ── 批量文字输入 ── */
function loadBatchTextPage(){
  const rows=document.getElementById('batch-text-rows');
  rows.innerHTML='';
  allDevices.filter(d=>d.is_online).forEach(d=>{
    const alias=ALIAS[d.device_id]||d.display_name||d.device_id.substring(0,8);
    rows.innerHTML+=`<div style="display:flex;gap:8px;align-items:center" data-did="${d.device_id}">
      <span style="font-size:12px;min-width:70px;font-weight:600">${alias}</span>
      <input class="batch-text-val" placeholder="输入要发送的文字..." style="flex:1;padding:6px 10px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
    </div>`;
  });
}
function addBatchTextRow(){
  const rows=document.getElementById('batch-text-rows');
  rows.innerHTML+=`<div style="display:flex;gap:8px;align-items:center">
    <input placeholder="设备ID" style="min-width:100px;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px" class="batch-text-did"/>
    <input class="batch-text-val" placeholder="输入文字..." style="flex:1;padding:6px 10px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
  </div>`;
}
async function executeBatchText(mode){
  if(mode==='broadcast'){
    const txt=await ocPrompt('广播文字','',{message:'输入要发送到所有设备的文字',inputPlaceholder:'输入文字...'});
    if(!txt) return;
    try{
      const r=await api('POST','/batch/text-input',{mode:'broadcast',broadcast_text:txt});
      showToast(`已发送到 ${r.total} 台设备`);
    }catch(e){showToast('发送失败','warn');}
    return;
  }
  const rows=document.querySelectorAll('#batch-text-rows > div');
  const entries=[];
  rows.forEach(row=>{
    const did=row.dataset?.did||row.querySelector('.batch-text-did')?.value;
    const text=row.querySelector('.batch-text-val')?.value;
    if(did&&text) entries.push({device_id:did,text});
  });
  if(!entries.length){showToast('没有可发送的内容','warn');return;}
  try{
    const r=await api('POST','/batch/text-input',{entries,mode});
    showToast(`已${mode==='clipboard'?'复制':'输入'}到 ${r.total} 台设备`);
  }catch(e){showToast('发送失败','warn');}
}

/* ── 应用管理器 ── */
let _appList=[];
function loadAppManagerPage(){
  const sel=document.getElementById('app-mgr-device');
  sel.innerHTML='';
  allDevices.filter(d=>d.is_online).forEach(d=>{
    const alias=ALIAS[d.device_id]||d.display_name||d.device_id.substring(0,8);
    sel.innerHTML+=`<option value="${d.device_id}">${alias}</option>`;
  });
  document.getElementById('app-list').innerHTML='<div style="color:var(--text-dim);font-size:12px">选择设备后点击"扫描应用"</div>';
}
async function loadAppList(){
  const did=document.getElementById('app-mgr-device').value;
  if(!did){showToast('请选择设备','warn');return;}
  try{
    const r=await api('GET',`/devices/${did}/apps`);
    _appList=r.packages||[];
    renderAppList();
  }catch(e){showToast('扫描失败','warn');}
}
function renderAppList(){
  const q=(document.getElementById('app-search')?.value||'').toLowerCase();
  const list=_appList.filter(p=>!q||p.toLowerCase().includes(q));
  const did=document.getElementById('app-mgr-device').value;
  document.getElementById('app-list').innerHTML=list.map(p=>`
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:10px;display:flex;justify-content:space-between;align-items:center">
      <div>
        <input type="checkbox" class="app-chk" value="${p}" style="margin-right:6px"/>
        <span style="font-size:11px;font-family:monospace">${p}</span>
      </div>
      <div style="display:flex;gap:4px">
        <button class="sb-btn2" style="font-size:10px;padding:2px 6px" onclick="appAction('${did}','start','${p}')">启动</button>
        <button class="sb-btn2" style="font-size:10px;padding:2px 6px" onclick="appAction('${did}','stop','${p}')">停止</button>
        <button class="sb-btn2" style="font-size:10px;padding:2px 6px" onclick="appAction('${did}','clear','${p}')">清除</button>
        <button class="sb-btn2" style="font-size:10px;padding:2px 6px;color:var(--red)" onclick="appAction('${did}','uninstall','${p}')">卸载</button>
      </div>
    </div>`).join('')||'<div style="color:var(--text-dim);font-size:12px">无应用</div>';
}
function filterApps(){renderAppList();}
async function appAction(did,action,pkg){
  if(action==='uninstall'&&!(await ocDialog({title:'卸载应用',message:`确认卸载 <code>${pkg}</code> ？`,type:'danger',confirmText:'卸载',cancelText:'取消'}))) return;
  try{
    const r=await api('POST',`/devices/${did}/apps/action`,{action,package:pkg});
    showToast(`${action}: ${r.ok?'成功':'失败'} ${r.output||''}`);
    if(action==='uninstall') setTimeout(loadAppList,500);
  }catch(e){showToast('操作失败','warn');}
}
async function appBatchAction(action){
  const checked=[...document.querySelectorAll('.app-chk:checked')].map(c=>c.value);
  if(!checked.length){showToast('请先勾选应用','warn');return;}
  if(action==='uninstall'&&!(await ocDialog({title:'批量卸载',message:`确认卸载 ${checked.length} 个应用？`,type:'danger',confirmText:'卸载',cancelText:'取消'}))) return;
  const did=document.getElementById('app-mgr-device').value;
  for(const pkg of checked){
    try{await api('POST',`/devices/${did}/apps/action`,{action,package:pkg});}catch(e){}
  }
  showToast(`已对 ${checked.length} 个应用执行 ${action}`);
  setTimeout(loadAppList,800);
}


/* ── 设备资产管理 ── */
async function loadDeviceAssetsPage(){
  try{
    const [assets,aliases]=await Promise.all([api('GET','/device-assets'),api('GET','/devices/aliases')]);
    const grid=document.getElementById('assets-grid');
    let html='';
    allDevices.forEach(d=>{
      const did=d.device_id;
      const alias=(aliases[did]||{}).alias||d.display_name||did.substring(0,8);
      const a=assets[did]||{};
      html+=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-size:13px;font-weight:600">${alias}</span>
          <div style="display:flex;gap:4px">
            <button class="sb-btn2" style="font-size:10px;padding:2px 6px" onclick="autoDetectAsset('${did}')">自动检测</button>
            <button class="sb-btn2" style="font-size:10px;padding:2px 6px" onclick="editAsset('${did}')">编辑</button>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:4px;font-size:11px;color:var(--text-dim)">
          <div>IMEI: <span style="color:var(--text-main)">${a.imei||'-'}</span></div>
          <div>IP: <span style="color:var(--text-main)">${a.ip||'-'}</span></div>
          <div>SIM: <span style="color:var(--text-main)">${a.sim_number||'-'}</span></div>
          <div>运营商: <span style="color:var(--text-main)">${a.carrier||'-'}</span></div>
          <div>购入: <span style="color:var(--text-main)">${a.purchase_date||'-'}</span></div>
          <div>位置: <span style="color:var(--text-main)">${a.location||'-'}</span></div>
        </div>
        ${a.notes?`<div style="font-size:10px;color:var(--text-muted);margin-top:4px;border-top:1px solid var(--border);padding-top:4px">${a.notes}</div>`:''}
      </div>`;
    });
    grid.innerHTML=html||'<div style="color:var(--text-dim)">暂无设备</div>';
  }catch(e){showToast('加载失败','warn');}
}
async function autoDetectAsset(did){
  showToast('正在检测...');
  try{
    await api('POST',`/device-assets/${did}/auto-detect`);
    showToast('检测完成');
    loadDeviceAssetsPage();
  }catch(e){showToast('检测失败','warn');}
}
async function autoDetectAllAssets(){
  showToast('正在检测所有设备...');
  for(const d of allDevices.filter(d=>d.is_online)){
    try{await api('POST',`/device-assets/${d.device_id}/auto-detect`);}catch(e){}
  }
  showToast('全部检测完成');
  loadDeviceAssetsPage();
}
async function editAsset(did){
  const alias=ALIAS[did]||did.substring(0,8);
  const fields=['imei','sim_number','ip','purchase_date','location','notes','owner'];
  let current={};
  try{current=await api('GET',`/device-assets/${did}`);}catch(e){}
  const data={};
  for(const f of fields){
    const val=await ocPrompt(alias+' - '+f,current[f]||'',{inputPlaceholder:f});
    if(val!==null) data[f]=val;
  }
  if(Object.keys(data).length){
    try{await api('POST',`/device-assets/${did}`,data);showToast('已保存');loadDeviceAssetsPage();}catch(e){showToast('保存失败','warn');}
  }
}

