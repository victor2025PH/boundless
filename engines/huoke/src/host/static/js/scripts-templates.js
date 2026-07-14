/* scripts-templates.js — 脚本与模板: 话术管理、脚本模板引擎、批量快捷操作、批量文件上传、AI脚本生成、操作时间线 */
/* ── 话术管理 ── */
let _phrasesData=[];
async function loadPhrasesPage(){
  try{
    _phrasesData=await api('GET','/phrases');
    renderPhrases();
  }catch(e){showToast('加载失败','warn');}
}
function renderPhrases(){
  const c=document.getElementById('phrase-groups-container');
  c.innerHTML=_phrasesData.map(g=>{
    const items=(g.items||[]).map((t,i)=>`<div style="display:flex;gap:6px;align-items:center;padding:4px 0;border-bottom:1px solid var(--border)">
      <span style="font-size:12px;flex:1">${t}</span>
      <button class="sb-btn2" style="font-size:10px;padding:1px 6px" onclick="copyPhrase('${t.replace(/'/g,"\\'")}')">复制</button>
      <button class="sb-btn2" style="font-size:10px;padding:1px 6px;color:var(--red)" onclick="removePhraseItem('${g.id}',${i})">删</button>
    </div>`).join('');
    return `<div style="background:var(--bg-card);border:1px solid var(--border);border-left:4px solid ${g.color};border-radius:10px;padding:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:13px;font-weight:600">${g.name}</span>
        <div style="display:flex;gap:4px">
          <button class="sb-btn2" style="font-size:10px;padding:2px 6px" onclick="addPhraseItem('${g.id}')">+话术</button>
          <button class="sb-btn2" style="font-size:10px;padding:2px 6px;color:var(--red)" onclick="deletePhraseGroup('${g.id}')">删除分组</button>
        </div>
      </div>
      <div>${items||'<div style="font-size:11px;color:var(--text-dim)">暂无话术</div>'}</div>
    </div>`;
  }).join('')||'<div style="color:var(--text-dim)">暂无话术分组</div>';
}
async function showAddPhraseGroup(){
  const name=await ocPrompt('新建分组','',{inputPlaceholder:'分组名称'});
  if(!name) return;
  api('POST','/phrases',{name,items:[]}).then(()=>loadPhrasesPage()).catch(e=>showToast('创建失败','warn'));
}
async function addPhraseItem(gid){
  const text=await ocPrompt('添加话术','',{inputPlaceholder:'话术内容'});
  if(!text) return;
  const g=_phrasesData.find(x=>x.id===gid);
  if(!g) return;
  g.items.push(text);
  try{
    await api('PUT',`/phrases/${gid}`,{items:g.items});
    loadPhrasesPage();
  }catch(e){showToast('添加失败','warn');}
}
async function removePhraseItem(gid,idx){
  const g=_phrasesData.find(x=>x.id===gid);
  if(!g) return;
  g.items.splice(idx,1);
  try{
    await api('PUT',`/phrases/${gid}`,{items:g.items});
    loadPhrasesPage();
  }catch(e){showToast('删除失败','warn');}
}
async function deletePhraseGroup(gid){
  if(!(await ocDialog({title:'删除分组',message:'确认删除此分组？',type:'danger',confirmText:'删除',dangerous:true}))) return;
  try{
    await api('DELETE',`/phrases/${gid}`);
    loadPhrasesPage();
  }catch(e){showToast('删除失败','warn');}
}
function copyPhrase(text){
  navigator.clipboard.writeText(text).then(()=>showToast('已复制'));
}


/* ── 脚本模板引擎 ── */
async function loadScriptEnginePage(){
  try{
    const r=await api('GET','/scripts');
    document.getElementById('script-list').innerHTML=(r.scripts||[]).map(s=>
      `<div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border)">
        <span style="font-size:12px;font-family:monospace;cursor:pointer" onclick="loadScript('${s.name}')">${s.name} <span style="color:var(--text-muted)">(${(s.size/1024).toFixed(1)}K)</span></span>
        <div style="display:flex;gap:4px">
          <button class="sb-btn2" style="font-size:10px;padding:2px 6px" onclick="runScript('${s.name}')">执行</button>
          <button class="sb-btn2" style="font-size:10px;padding:2px 6px;color:var(--red)" onclick="deleteScript('${s.name}')">删除</button>
        </div>
      </div>`
    ).join('')||'<div style="color:var(--text-dim);font-size:12px">暂无脚本</div>';
  }catch(e){}
  _populateScriptGroupSel();
}
function _populateScriptGroupSel(){
  const sel=document.getElementById('script-target-group');
  if(!sel)return;
  sel.innerHTML='<option value="">全部在线设备</option>'+
    _groupsData.map(g=>`<option value="${g.id}">${g.name} (${(g.devices||[]).length})</option>`).join('');
}
function _parseCustomVars(){
  const raw=document.getElementById('script-custom-vars')?.value||'';
  const vars={};
  raw.split(/[,;]/).forEach(pair=>{
    const [k,v]=pair.split('=').map(s=>s.trim());
    if(k&&v)vars[k]=v;
  });
  return vars;
}
async function loadScriptTemplates(){
  const panel=document.getElementById('tpl-panel');
  if(panel.style.display!=='none'){panel.style.display='none';return;}
  panel.style.display='block';
  try{
    const r=await api('GET','/scripts/templates');
    document.getElementById('tpl-list').innerHTML=(r.templates||[]).map(t=>
      `<div onclick="useTemplate(${JSON.stringify(t.content).replace(/"/g,'&quot;')},'${t.filename}')" style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px;cursor:pointer;transition:border-color .2s" onmouseenter="this.style.borderColor='var(--accent)'" onmouseleave="this.style.borderColor='var(--border)'">
        <div style="font-size:12px;font-weight:600">${t.name}</div>
        <div style="font-size:9px;color:var(--text-muted);margin-top:4px">变量: ${t.vars.map(v=>'{{'+v+'}}').join(' ')}</div>
      </div>`
    ).join('');
  }catch(e){}
}
function useTemplate(content,filename){
  document.getElementById('script-content').value=content;
  document.getElementById('script-name').value=filename;
  document.getElementById('tpl-panel').style.display='none';
  showToast('已加载模板');
}
async function saveScript(){
  const name=document.getElementById('script-name').value.trim();
  const content=document.getElementById('script-content').value;
  if(!name||!content){showToast('请输入脚本名和内容','warn');return;}
  try{
    await api('POST','/scripts/upload',{filename:name,content});
    showToast('脚本已保存');
    loadScriptEnginePage();
  }catch(e){showToast('保存失败','warn');}
}
async function loadScript(name){
  document.getElementById('script-name').value=name;
  showToast('已加载 '+name);
}
async function deleteScript(name){
  if(!(await ocDialog({title:'删除脚本',message:'删除脚本 '+name+' ？',type:'danger',confirmText:'删除',dangerous:true}))) return;
  await api('DELETE','/scripts/'+name);
  loadScriptEnginePage();
}
async function runScript(name){
  const type=document.getElementById('script-type').value;
  const groupId=document.getElementById('script-target-group')?.value||'';
  const vars=_parseCustomVars();
  document.getElementById('script-results').textContent='正在执行...';
  try{
    const payload={filename:name,type,device_ids:[],variables:vars};
    if(groupId)payload.group_id=groupId;
    const r=await api('POST','/scripts/execute',payload);
    let txt='';
    for(const[did,info] of Object.entries(r.results||{})){
      const alias=ALIAS[did]||did.substring(0,8);
      const icon=info.success?'✓':'✗';
      txt+=`${icon} === ${alias} ===\n${info.output}\n\n`;
    }
    document.getElementById('script-results').textContent=txt||'无结果';
    showToast(`执行完成: ${r.total} 台设备`);
  }catch(e){document.getElementById('script-results').textContent='执行失败: '+e.message;}
}
async function executeSelectedScript(){
  const name=document.getElementById('script-name').value.trim();
  const content=document.getElementById('script-content').value;
  if(!content){showToast('请输入脚本内容','warn');return;}
  const fname=name||'_temp.sh';
  await api('POST','/scripts/upload',{filename:fname,content});
  runScript(fname);
}

/* ── 批量快捷操作 ── */
function loadQuickActionsPage(){
  document.getElementById('qa-results').innerHTML='<div style="color:var(--text-dim);font-size:12px">点击上方按钮执行操作</div>';
}
async function batchQuickAction(action){
  if(action==='reboot'&&!(await ocDialog({title:'重启设备',message:'确认重启所有在线设备？',type:'warning',confirmText:'重启',cancelText:'取消'}))) return;
  try{
    const r=await api('POST','/batch/quick-action',{action});
    let html='';
    for(const[did,info] of Object.entries(r.results||{})){
      const alias=ALIAS[did]||did.substring(0,8);
      const color=info.success?'var(--green)':'var(--red)';
      html+=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:8px;display:flex;justify-content:space-between;align-items:center">
        <span style="font-size:12px;font-weight:600">${alias}</span>
        <span style="color:${color};font-size:11px">${info.success?'成功':'失败'}</span>
      </div>`;
    }
    document.getElementById('qa-results').innerHTML=html;
    showToast(`${action} 已执行: ${r.total} 台设备`);
  }catch(e){showToast('操作失败','warn');}
}
async function batchBrightness(){
  const v=await ocPrompt('设置亮度','128',{message:'范围 0-255',inputPlaceholder:'0-255'});
  if(!v) return;
  api('POST','/batch/quick-action',{action:'brightness',value:v}).then(r=>{
    showToast(`亮度已设置: ${r.total} 台设备`);
  }).catch(()=>showToast('设置失败','warn'));
}

/* ── 批量文件上传 ── */
let _uploadFiles=[];
function loadBatchUploadPage(){
  _uploadFiles=[];
  document.getElementById('upload-file-info').style.display='none';
  document.getElementById('upload-progress').innerHTML='';
}
function handleFileDrop(e){
  _uploadFiles=Array.from(e.dataTransfer.files);
  _showUploadFiles();
}
function handleBatchFileSelect(input){
  _uploadFiles=Array.from(input.files);
  _showUploadFiles();
}
function _showUploadFiles(){
  if(!_uploadFiles.length) return;
  const info=document.getElementById('upload-file-info');
  info.style.display='block';
  info.innerHTML=_uploadFiles.map(f=>`<div>${f.name} (${(f.size/1024/1024).toFixed(2)} MB)</div>`).join('');
}
async function executeBatchUpload(){
  if(!_uploadFiles.length){showToast('请先选择文件','warn');return;}
  const dest=document.getElementById('upload-dest').value||'/sdcard/Download/';
  const prog=document.getElementById('upload-progress');
  prog.innerHTML='<div style="color:var(--accent)">正在上传...</div>';
  for(const f of _uploadFiles){
    try{
      const buf=await f.arrayBuffer();
      const b64=btoa(new Uint8Array(buf).reduce((s,b)=>s+String.fromCharCode(b),''));
      const r=await api('POST','/batch/upload-file',{data_b64:b64,filename:f.name,dest_dir:dest});
      let html=`<div style="font-size:12px;font-weight:600;margin-bottom:4px">${f.name}</div>`;
      for(const[did,info] of Object.entries(r.results||{})){
        const alias=ALIAS[did]||did.substring(0,8);
        const color=info.success?'var(--green)':'var(--red)';
        html+=`<div style="display:flex;justify-content:space-between;font-size:11px;padding:2px 0">
          <span>${alias}</span><span style="color:${color}">${info.success?'成功':'失败'}</span>
        </div>`;
      }
      prog.innerHTML+=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:10px">${html}</div>`;
      showToast(`${f.name}: ${r.success}/${r.total} 成功`);
    }catch(e){
      prog.innerHTML+=`<div style="color:var(--red)">${f.name}: 上传失败</div>`;
    }
  }
}


/* ── AI脚本生成 ── */
function loadAiScriptPage(){
  document.getElementById('ai-script-desc').value='';
  document.getElementById('ai-script-output').value='';
  document.getElementById('ai-script-results').textContent='等待执行...';
}
function aiQuickDesc(text){document.getElementById('ai-script-desc').value=text;}
async function generateAiScript(){
  const desc=document.getElementById('ai-script-desc').value.trim();
  if(!desc){showToast('请描述你的需求','warn');return;}
  document.getElementById('ai-script-output').value='正在生成...';
  try{
    const r=await api('POST','/ai/generate-script',{description:desc});
    document.getElementById('ai-script-output').value=r.script||'';
    showToast(r.fallback?'使用内置模板':'AI脚本已生成');
  }catch(e){
    document.getElementById('ai-script-output').value='# 生成失败: '+e.message;
    showToast('生成失败','warn');
  }
}
async function saveAiScript(){
  const content=document.getElementById('ai-script-output').value;
  if(!content){showToast('没有脚本内容','warn');return;}
  const name=await ocPrompt('脚本文件名','ai_generated.sh',{inputPlaceholder:'文件名'});
  if(!name) return;
  try{
    await api('POST','/scripts/upload',{filename:name,content});
    showToast('脚本已保存到: '+name);
  }catch(e){showToast('保存失败','warn');}
}
async function executeAiScript(){
  const content=document.getElementById('ai-script-output').value.trim();
  if(!content){showToast('没有脚本内容','warn');return;}
  const out=document.getElementById('ai-script-results');
  out.textContent='正在执行...';
  try{
    await api('POST','/scripts/upload',{filename:'_ai_temp.sh',content});
    const r=await api('POST','/scripts/execute',{filename:'_ai_temp.sh',type:'adb',device_ids:[]});
    let txt='';
    for(const[did,info] of Object.entries(r.results||{})){
      const alias=ALIAS[did]||did.substring(0,8);
      txt+=`=== ${alias} (${info.success?'OK':'FAIL'}) ===\n${info.output}\n\n`;
    }
    out.textContent=txt||'无结果';
    showToast(`执行完成: ${r.total} 台设备`);
  }catch(e){out.textContent='执行失败: '+e.message;}
}

/* ── 操作时间线 ── */
let _tlMode='device';
async function switchTlMode(mode){
  _tlMode=mode;
  document.querySelectorAll('.tl-tab').forEach(b=>b.style.background=b.dataset.mode===mode?'var(--accent)':'transparent');
  if(mode==='user')loadUserAuditTimeline();
  else loadOpTimelinePage();
}
async function loadUserAuditTimeline(){
  const container=document.getElementById('timeline-container');
  container.innerHTML='<div style="color:var(--text-muted)">加载中...</div>';
  try{
    const logs=await api('GET','/audit/user-logs?limit=80');
    if(!logs.length){container.innerHTML='<div style="color:var(--text-dim)">暂无审计记录</div>';return;}
    let html='<div style="position:absolute;left:8px;top:0;bottom:0;width:2px;background:var(--border)"></div>';
    logs.forEach(l=>{
      const color=l.status>=400?'#ef4444':l.action==='DELETE'?'#f97316':'#3b82f6';
      html+=`<div style="position:relative;margin-bottom:10px;padding-left:20px">
        <div style="position:absolute;left:3px;top:4px;width:12px;height:12px;border-radius:50%;background:${color};border:2px solid var(--bg-main)"></div>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:8px 10px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="font-size:12px"><strong>${l.user||'?'}</strong> <span style="color:${color}">${l.action}</span> <code style="font-size:10px">${l.path}</code></span>
            <span style="font-size:10px;color:var(--text-muted)">${l.ts}</span>
          </div>
          <div style="font-size:9px;color:var(--text-muted);margin-top:2px">IP: ${l.ip||'?'} | 状态: ${l.status}</div>
        </div>
      </div>`;
    });
    container.innerHTML=html;
  }catch(e){container.innerHTML='<div style="color:#f87171">加载失败</div>';}
}
async function loadOpTimelinePage(){
  const filter=document.getElementById('tl-device-filter');
  if(filter.options.length<=1){
    allDevices.forEach(d=>{
      const alias=ALIAS[d.device_id]||d.display_name||d.device_id.substring(0,8);
      filter.innerHTML+=`<option value="${d.device_id}">${alias}</option>`;
    });
  }
  const did=filter.value;
  try{
    const r=did?await api('GET',`/devices/${did}/timeline?limit=80`):await api('GET','/timeline/all?limit=80');
    const events=r.events||[];
    const container=document.getElementById('timeline-container');
    if(!events.length){container.innerHTML='<div style="color:var(--text-dim);font-size:12px">暂无操作记录</div>';return;}
    let html='<div style="position:absolute;left:8px;top:0;bottom:0;width:2px;background:var(--border)"></div>';
    events.forEach(ev=>{
      const action=ev.action||ev.operation||'unknown';
      const ts=ev.timestamp||ev.time||'';
      const extra=ev.extra||'';
      let extraStr='';
      try{
        const obj=typeof extra==='string'?JSON.parse(extra):extra;
        extraStr=Object.entries(obj).slice(0,3).map(([k,v])=>`${k}:${JSON.stringify(v).substring(0,40)}`).join(' | ');
      }catch(e){extraStr=String(extra).substring(0,100);}
      const color=action.includes('fail')||action.includes('error')?'var(--red)':action.includes('create')||action.includes('start')?'var(--green)':'var(--accent)';
      html+=`<div style="position:relative;margin-bottom:12px;padding-left:20px">
        <div style="position:absolute;left:3px;top:4px;width:12px;height:12px;border-radius:50%;background:${color};border:2px solid var(--bg-main)"></div>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:10px">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <span style="font-size:12px;font-weight:600">${action}</span>
            <span style="font-size:10px;color:var(--text-muted)">${ts}</span>
          </div>
          ${extraStr?`<div style="font-size:10px;color:var(--text-dim);margin-top:4px;word-break:break-all">${extraStr}</div>`:''}
        </div>
      </div>`;
    });
    container.innerHTML=html;
  }catch(e){document.getElementById('timeline-container').innerHTML='<div style="color:var(--text-dim)">加载失败</div>';}
}

