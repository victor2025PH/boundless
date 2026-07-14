/* workflows.js — 工作流: 工作流配置、定时任务管理、数据导出、同步镜像、健康报告、模板市场、可视化工作流编辑器 */
/* ── Workflows ── */
async function loadWorkflowsPage(){
  try{
    const [wfList, active, history, schedules]=await Promise.all([
      api('GET','/workflows'),
      api('GET','/workflows/runs/active'),
      api('GET','/workflows/runs/history?limit=15'),
      api('GET','/schedules'),
    ]);
    const wfSchedules=(Array.isArray(schedules)?schedules:[]).filter(s=>s.task_type==='workflow');
    const wfs=Array.isArray(wfList)?wfList:[];
    document.getElementById('wf-stats-row').innerHTML=`
      <div class="stat-card"><div class="stat-value">${wfs.length}</div><div class="stat-label">工作流</div></div>
      <div class="stat-card"><div class="stat-value">${(active.runs||[]).length}</div><div class="stat-label">运行中</div></div>
      <div class="stat-card"><div class="stat-value">${(history.runs||[]).filter(r=>r.status==='success').length}</div><div class="stat-label">近期成功</div></div>
      <div class="stat-card"><div class="stat-value">${(history.runs||[]).filter(r=>r.status==='failed').length}</div><div class="stat-label">近期失败</div></div>
      <div class="stat-card"><div class="stat-value">${wfSchedules.length}</div><div class="stat-label">定时计划</div></div>`;

    if(wfs.length){
      document.getElementById('wf-list').innerHTML=wfs.map(w=>{
        const vars=(w.variables||[]).join(', ');
        const wn=w.name||w.file.replace('.yaml','');
        const sched=wfSchedules.find(s=>(s.params||{}).workflow===wn);
        const schedInfo=sched?`<span style="font-size:10px;color:var(--accent)"> ⏰ ${sched.cron_expr}${sched.enabled?'':' (暂停)'}</span>`:'';
        return `<div style="padding:10px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
          <div>
            <b style="font-size:13px">${w.name||w.file}</b>
            <div style="font-size:11px;color:var(--text-muted);margin-top:2px">${w.steps||0} 步骤${vars?' · 变量: '+vars:''}${schedInfo}</div>
          </div>
          <div style="display:flex;gap:4px">
            <button class="dev-btn" style="font-size:10px;padding:2px 8px" onclick="editWorkflow('${w.name||w.file.replace('.yaml','')}')" title="编辑">编辑</button>
            <button class="dev-btn" style="font-size:10px;padding:2px 8px;color:var(--accent)" onclick="scheduleWorkflow('${w.name||w.file.replace('.yaml','')}')" title="定时">⏰</button>
            <button class="dev-btn" style="font-size:10px;padding:2px 8px;color:var(--green)" onclick="runWorkflow('${w.name||w.file.replace('.yaml','')}')" title="安全启动">安全启动</button>
          </div>
        </div>`;
      }).join('');
    }else{
      document.getElementById('wf-list').innerHTML='<div style="text-align:center;padding:30px;color:var(--text-muted)">暂无工作流</div>';
    }

    const runs=history.runs||[];
    if(runs.length){
      document.getElementById('wf-runs').innerHTML=runs.map(r=>{
        const cls=r.status==='success'?'badge-completed':r.status==='running'?'badge-running':'badge-failed';
        const elapsed=r.elapsed_sec?r.elapsed_sec.toFixed(1)+'s':'—';
        return `<div style="padding:8px;border-bottom:1px solid var(--border);font-size:12px">
          <div style="display:flex;justify-content:space-between">
            <b>${r.workflow_name}</b>
            <span class="badge ${cls}">${r.status}</span>
          </div>
          <div style="color:var(--text-muted);font-size:11px;margin-top:2px">ID: ${r.run_id} · ${elapsed}</div>
        </div>`;
      }).join('');
    }else{
      document.getElementById('wf-runs').innerHTML='<div style="text-align:center;padding:30px;color:var(--text-muted)">暂无执行记录</div>';
    }

    const activeRuns=active.runs||[];
    if(activeRuns.length){
      document.getElementById('wf-active-runs').innerHTML=activeRuns.map(r=>{
        const pct=r.total_steps?Math.round(r.completed_steps/r.total_steps*100):0;
        return `<div style="padding:10px;border-bottom:1px solid var(--border)">
          <div style="display:flex;justify-content:space-between;align-items:center">
            <b style="font-size:13px">${r.workflow_name}</b>
            <span class="badge badge-running">${r.completed_steps}/${r.total_steps}</span>
          </div>
          <div style="background:var(--bg-main);border-radius:4px;height:6px;margin-top:6px;overflow:hidden">
            <div style="background:var(--green);height:100%;width:${pct}%;transition:width .3s"></div>
          </div>
          <div style="font-size:11px;color:var(--text-muted);margin-top:4px">当前: ${r.current_step||'—'}</div>
        </div>`;
      }).join('');
    }else{
      document.getElementById('wf-active-runs').innerHTML='<div style="text-align:center;padding:20px;color:var(--text-muted)">无活动运行</div>';
    }
  }catch(e){
    console.error('loadWorkflowsPage error:', e);
  }
}
function showCreateWorkflow(){
  document.getElementById('wf-editor-title').textContent='新建工作流';
  document.getElementById('wf-editor-name').value='';
  document.getElementById('wf-editor-yaml').value='name: new_workflow\ndescription: \"\"\nvariables:\n  target_country: italy\nsteps:\n  - id: step1\n    action: tiktok.warmup_session\n    params:\n      duration_minutes: 20\n';
  document.getElementById('wf-editor-modal').style.display='flex';
}
async function editWorkflow(name){
  try{
    const data=await api('GET','/workflows/'+encodeURIComponent(name));
    document.getElementById('wf-editor-title').textContent='编辑: '+name;
    document.getElementById('wf-editor-name').value=name;
    document.getElementById('wf-editor-yaml').value=data.content||'';
    document.getElementById('wf-editor-modal').style.display='flex';
  }catch(e){showToast('加载失败: '+e.message);}
}
async function saveWorkflow(){
  const name=document.getElementById('wf-editor-name').value.trim();
  const yaml=document.getElementById('wf-editor-yaml').value;
  if(!name){showToast('请输入名称');return;}
  try{
    await api('PUT','/workflows/'+encodeURIComponent(name),{content:yaml});
    showToast('工作流已保存');
    closeWfEditor();
    loadWorkflowsPage();
  }catch(e){showToast('保存失败: '+e.message);}
}
function closeWfEditor(){document.getElementById('wf-editor-modal').style.display='none';}
async function runWorkflow(name){
  try{
    const res=await api('POST','/workflows/run',{workflow:name});
    showToast('工作流已启动: '+res.run_id);
    setTimeout(loadWorkflowsPage,1000);
  }catch(e){showToast('启动失败: '+e.message);}
}
async function scheduleWorkflow(name){
  const cron=await ocPrompt('Cron 表达式','0 9 * * *',{message:'如 "0 9 * * *" 表示每天9点',inputPlaceholder:'Cron 表达式'});
  if(!cron) return;
  try{
    await api('POST','/schedules/workflow',{workflow:name,cron_expr:cron,name:'定时-'+name});
    showToast('定时调度已创建');
  }catch(e){showToast('创建失败: '+e.message);}
}


/* ── 定时任务管理 ── */
async function loadScheduledJobsPage(){
  try{
    const jobs=await api('GET','/scheduled-jobs');
    document.getElementById('jobs-list').innerHTML=(Array.isArray(jobs)?jobs:[]).map(j=>{
      const statusColor=j.enabled?'var(--green)':'var(--text-muted)';
      return `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px;display:flex;justify-content:space-between;align-items:center">
        <div style="flex:1">
          <div style="display:flex;align-items:center;gap:8px;margin-bottom:4px">
            <span style="font-size:13px;font-weight:600">${j.name||'未命名'}</span>
            <span style="font-size:10px;padding:2px 6px;border-radius:4px;background:${statusColor};color:#111">${j.enabled?'启用':'禁用'}</span>
          </div>
          <div style="font-size:11px;color:var(--text-dim)">
            <span>&#9200; ${j.cron||'-'}</span>
            <span style="margin-left:10px">&#9881; ${j.action||'-'}</span>
            ${j.last_run?`<span style="margin-left:10px">上次: ${j.last_run}</span>`:''}
          </div>
        </div>
        <div style="display:flex;gap:4px">
          <button class="sb-btn2" style="font-size:10px;padding:3px 8px" onclick="runJobNow('${j.id}')">安全启动</button>
          <button class="sb-btn2" style="font-size:10px;padding:3px 8px" onclick="toggleJobEnabled('${j.id}',${!j.enabled})">${j.enabled?'禁用':'启用'}</button>
          <button class="sb-btn2" style="font-size:10px;padding:3px 8px;color:var(--red)" onclick="deleteJob('${j.id}')">删除</button>
        </div>
      </div>`;
    }).join('')||'<div style="color:var(--text-dim);font-size:12px">暂无定时任务</div>';
  }catch(e){showToast('加载失败','warn');}
}
function _workflowRiskLevelByAction(action, params){
  const p=params||{};
  if(action==='tiktok_acquisition') return '中';
  if(action==='tiktok_follow' && (Number(p.max_follows||0)>60)) return '高';
  if(action==='batch_quick_action') return '中';
  return '低';
}

function _workflowRenderJobSummary(){
  const name=document.getElementById('job-name')?.value?.trim()||'未命名任务';
  const cron=document.getElementById('job-cron')?.value?.trim()||'未设置';
  const action=document.getElementById('job-action')?.value||'未设置';
  const raw=document.getElementById('job-params')?.value?.trim()||'';
  let params={};
  let paramsErr='';
  if(raw){
    try{params=JSON.parse(raw);}catch(e){paramsErr='参数JSON格式错误';}
  }
  const risk=paramsErr?'高':_workflowRiskLevelByAction(action, params);
  const riskColor=risk==='高'?'#ef4444':risk==='中'?'#eab308':'#22c55e';
  const nextHint=cron==='未设置'?'请填写 Cron':(cron.includes('*/')?'按周期执行':(cron.includes('0 9')?'每天上午执行':'按计划执行'));
  const c=document.getElementById('job-execution-summary');
  if(!c)return;
  c.innerHTML=`
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
      <span style="font-size:12px;font-weight:600">执行前摘要</span>
      <span style="font-size:10px;padding:2px 8px;border-radius:999px;background:${riskColor}22;color:${riskColor};border:1px solid ${riskColor}66">风险: ${risk}</span>
    </div>
    <div style="font-size:11px;color:var(--text-dim);line-height:1.6">
      <div>任务: <span style="color:var(--text-main)">${name}</span></div>
      <div>计划: <span style="color:var(--text-main)">${cron}</span>（${nextHint}）</div>
      <div>动作: <span style="color:var(--text-main)">${action}</span></div>
      <div>可随时禁用/删除，支持手动安全启动验证。</div>
      ${paramsErr?`<div style="color:#ef4444">${paramsErr}</div>`:''}
    </div>`;
}

function showAddJobForm(){
  document.getElementById('add-job-form').style.display='block';
  ['job-name','job-cron','job-action','job-params'].forEach(id=>{
    const el=document.getElementById(id);
    if(el && !el.dataset.boundSummary){
      el.addEventListener('input',_workflowRenderJobSummary);
      el.dataset.boundSummary='1';
    }
  });
  _workflowRenderJobSummary();
}
async function createScheduledJob(){
  const name=document.getElementById('job-name').value.trim();
  const cron=document.getElementById('job-cron').value.trim();
  const action=document.getElementById('job-action').value;
  let params={};
  try{const raw=document.getElementById('job-params').value.trim();if(raw)params=JSON.parse(raw);}catch(e){showToast('参数JSON格式错误，请修正后再安全启动','warn');return;}
  if(!name||!cron){showToast('请填写名称和Cron表达式','warn');return;}
  if(name.length<2){showToast('任务名称至少2个字符','warn');return;}
  try{
    await api('POST','/scheduled-jobs',{name,cron,action,params});
    showToast('定时任务已创建（已启用安全启动策略）');
    document.getElementById('add-job-form').style.display='none';
    loadScheduledJobsPage();
  }catch(e){showToast('创建失败','warn');}
}
async function runJobNow(id){
  try{
    const r=await api('POST',`/scheduled-jobs/${id}/run-now`);
    showToast('任务已执行');
    loadScheduledJobsPage();
  }catch(e){showToast('执行失败','warn');}
}
async function toggleJobEnabled(id,enabled){
  try{
    await api('PUT',`/scheduled-jobs/${id}`,{enabled});
    loadScheduledJobsPage();
  }catch(e){showToast('更新失败','warn');}
}
async function deleteJob(id){
  if(!(await ocDialog({title:'删除定时任务',message:'确认删除此定时任务？',type:'danger',confirmText:'删除',dangerous:true}))) return;
  try{await api('DELETE',`/scheduled-jobs/${id}`);loadScheduledJobsPage();}catch(e){}
}

/* ── 数据导出 ── */
function loadDataExportPage(){}
function exportData(type,days){
  const url=days?`/export/${type}?fmt=csv&days=${days}`:`/export/${type}?fmt=csv`;
  window.open(url,'_blank');
  showToast('正在导出...');
}


/* ── 同步镜像操作 ── */
function loadSyncMirrorPage(){
  document.getElementById('sync-count').textContent=allDevices.filter(d=>d.is_online).length;
  document.getElementById('sync-results').textContent='等待操作...';
}
async function syncTouch(action){
  const body={
    x_pct:parseFloat(document.getElementById('sync-x').value),
    y_pct:parseFloat(document.getElementById('sync-y').value),
    action,
  };
  if(action==='swipe'){
    body.end_x_pct=parseFloat(document.getElementById('sync-ex').value);
    body.end_y_pct=parseFloat(document.getElementById('sync-ey').value);
  }
  if(action==='long_press') body.duration=800;
  try{
    const r=await api('POST','/sync/touch',body);
    document.getElementById('sync-results').textContent=`${action}: ${r.success}/${r.total} 成功`;
    showToast(`同步${action}: ${r.success}/${r.total}`);
  }catch(e){document.getElementById('sync-results').textContent='失败: '+e.message;}
}
async function syncKey(code){
  try{
    const r=await api('POST','/sync/key',{keycode:code});
    document.getElementById('sync-results').textContent=`按键 ${code}: ${r.success}/${r.total} 成功`;
  }catch(e){document.getElementById('sync-results').textContent='失败: '+e.message;}
}
async function syncTextInput(){
  const text=document.getElementById('sync-text-input').value;
  if(!text){showToast('请输入文字','warn');return;}
  try{
    const r=await api('POST','/sync/text',{text});
    document.getElementById('sync-results').textContent=`文字同步: ${r.success}/${r.total} 成功`;
    showToast(`文字已同步到 ${r.success} 台设备`);
  }catch(e){document.getElementById('sync-results').textContent='失败: '+e.message;}
}
function syncPreset(preset){
  const m={
    scroll_down:{x:50,y:70,ex:50,ey:30},
    scroll_up:{x:50,y:30,ex:50,ey:70},
    swipe_left:{x:80,y:50,ex:20,ey:50},
    swipe_right:{x:20,y:50,ex:80,ey:50},
    center_tap:{x:50,y:50,ex:50,ey:50},
  };
  const p=m[preset]||m.center_tap;
  document.getElementById('sync-x').value=p.x;
  document.getElementById('sync-y').value=p.y;
  document.getElementById('sync-ex').value=p.ex;
  document.getElementById('sync-ey').value=p.ey;
  if(preset==='center_tap') syncTouch('tap');
  else syncTouch('swipe');
}

/* ── 健康报告 ── */
async function loadHealthReport(){
  const c=document.getElementById('health-report-content');
  c.innerHTML='<div style="color:var(--accent)">正在生成报告...</div>';
  try{
    const r=await api('GET','/health-report');
    let html='';
    html+=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px">
      <div style="font-size:14px;font-weight:700;margin-bottom:10px">&#128203; 报告摘要</div>
      <div style="font-size:11px;color:var(--text-dim);margin-bottom:8px">生成时间: ${r.generated_at}</div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:10px">
        <div class="stat-card blue"><div class="stat-num">${r.summary.total_devices}</div><div class="stat-label">总设备</div></div>
        <div class="stat-card green"><div class="stat-num">${r.summary.online}</div><div class="stat-label">在线</div></div>
        <div class="stat-card" style="border-top:3px solid #ef4444"><div class="stat-num" style="color:#f87171">${r.summary.offline}</div><div class="stat-label">离线</div></div>
        <div class="stat-card" style="border-top:3px solid #eab308"><div class="stat-num" style="color:#eab308">${(r.alerts||[]).length}</div><div class="stat-label">告警</div></div>
      </div>
      ${r.summary.offline_devices?.length?`<div style="margin-top:8px;font-size:11px;color:#f87171">离线设备: ${r.summary.offline_devices.join(', ')}</div>`:''}
    </div>`;
    if((r.alerts||[]).length){
      html+=`<div style="background:var(--bg-card);border:1px solid var(--border);border-left:4px solid #ef4444;border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px;color:#f87171">&#9888; 告警项目</div>
        ${r.alerts.map(a=>`<div style="font-size:12px;padding:4px 0;color:var(--text-dim)">&bull; ${a}</div>`).join('')}
      </div>`;
    }
    const perfEntries=Object.entries(r.performance||{});
    if(perfEntries.length){
      html+=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">&#128200; 设备性能详情</div>
        <table style="width:100%;font-size:12px;border-collapse:collapse">
          <thead><tr style="border-bottom:1px solid var(--border);color:var(--text-muted)"><th style="text-align:left;padding:6px">设备</th><th>电量</th><th>温度</th><th>内存</th><th>存储</th></tr></thead>
          <tbody>${perfEntries.map(([name,d])=>{
            const batC=d.battery<20?'#ef4444':d.battery<50?'#eab308':'#22c55e';
            return `<tr style="border-bottom:1px solid rgba(51,65,85,.3)"><td style="padding:6px;font-weight:600">${name}</td>
              <td style="text-align:center;color:${batC}">${d.battery??'-'}%</td>
              <td style="text-align:center">${d.temp??'-'}°C</td>
              <td style="text-align:center">${d.mem_pct??'-'}%</td>
              <td style="text-align:center">${d.storage_pct??'-'}%</td></tr>`;
          }).join('')}</tbody>
        </table>
      </div>`;
    }
    c.innerHTML=html;
  }catch(e){c.innerHTML=`<div style="color:var(--red)">生成失败: ${e.message}</div>`;}
}
function exportHealthReport(){window.open('/health-report','_blank');showToast('报告已生成');}

/* ── 模板市场 ── */
async function loadTplMarketPage(){
  try{
    const r=await api('GET','/templates');
    const list=r.templates||[];
    document.getElementById('tpl-list').innerHTML=list.map(t=>{
      const typeColors={script:'var(--accent)',workflow:'var(--green)',macro:'#a78bfa'};
      return `<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <span style="font-size:13px;font-weight:600">${t.name}</span>
          <span style="font-size:9px;padding:2px 8px;border-radius:4px;background:${typeColors[t.type]||'var(--accent)'};color:#fff">${t.type}</span>
        </div>
        <div style="font-size:11px;color:var(--text-dim);margin-bottom:8px">${t.description||'无描述'}</div>
        ${t.author?`<div style="font-size:10px;color:var(--text-muted)">作者: ${t.author}</div>`:''}
        <div style="display:flex;gap:6px;margin-top:8px">
          <button class="sb-btn2" onclick="importTemplate('${t.filename}')" style="font-size:10px">导入使用</button>
          <button class="sb-btn2" onclick="viewTemplate('${t.filename}')" style="font-size:10px">查看</button>
          <button class="sb-btn2" onclick="deleteTemplate('${t.filename}')" style="font-size:10px;color:var(--red)">删除</button>
        </div>
      </div>`;
    }).join('')||'<div style="color:var(--text-dim);font-size:12px">暂无模板，点击"分享模板"创建第一个</div>';
  }catch(e){showToast('加载失败','warn');}
}
function showCreateTplForm(){document.getElementById('tpl-create-form').style.display='block';}
async function createTemplate(){
  const body={
    name:document.getElementById('tpl-name').value,
    type:document.getElementById('tpl-type').value,
    description:document.getElementById('tpl-desc').value,
    content:document.getElementById('tpl-content').value,
  };
  if(!body.name||!body.content){showToast('请填写名称和内容','warn');return;}
  try{
    await api('POST','/templates',body);
    showToast('模板已发布');
    document.getElementById('tpl-create-form').style.display='none';
    loadTplMarketPage();
  }catch(e){showToast('发布失败','warn');}
}
async function importTemplate(filename){
  try{
    const r=await api('POST',`/templates/${filename}/import`);
    showToast(`已导入为 ${r.imported_as||r.type}`);
  }catch(e){showToast('导入失败','warn');}
}
async function viewTemplate(filename){
  try{
    const t=await api('GET',`/templates/${filename}`);
    await ocAlert(`=== ${t.name} ===\n\n${t.content}`);
  }catch(e){showToast('查看失败','warn');}
}
async function deleteTemplate(filename){
  if(!(await ocDialog({title:'删除模板',message:'确认删除此模板？',type:'danger',confirmText:'删除',dangerous:true}))) return;
  try{await api('DELETE',`/templates/${filename}`);loadTplMarketPage();}catch(e){}
}


/* ── Visual Workflow Editor ── */
let _wfNodes=[];let _wfEdges=[];let _wfNextId=1;let _wfSelectedNode=null;let _wfConnecting=null;
const _wfNodeTypes={
  adb_cmd:{label:'ADB 命令',color:'#3b82f6',fields:[{key:'command',label:'命令',type:'text',placeholder:'input tap 500 500'}]},
  tap:{label:'点击',color:'#3b82f6',fields:[{key:'x',label:'X坐标',type:'number'},{key:'y',label:'Y坐标',type:'number'}]},
  swipe:{label:'滑动',color:'#3b82f6',fields:[{key:'x1',label:'起X',type:'number'},{key:'y1',label:'起Y',type:'number'},{key:'x2',label:'终X',type:'number'},{key:'y2',label:'终Y',type:'number'},{key:'duration',label:'时长ms',type:'number'}]},
  text_input:{label:'输入文本',color:'#3b82f6',fields:[{key:'text',label:'文本',type:'text'}]},
  delay:{label:'等待',color:'#eab308',fields:[{key:'seconds',label:'秒数',type:'number'}]},
  key:{label:'按键',color:'#3b82f6',fields:[{key:'keycode',label:'键码',type:'text',placeholder:'KEYCODE_HOME'}]},
  app_launch:{label:'启动App',color:'#22c55e',fields:[{key:'package',label:'包名',type:'text'}]},
  screenshot:{label:'截图',color:'#22c55e',fields:[]},
  condition:{label:'条件',color:'#a855f7',fields:[{key:'check',label:'检查命令',type:'text'},{key:'expect',label:'期望结果',type:'text'}]},
  loop:{label:'循环',color:'#a855f7',fields:[{key:'count',label:'次数',type:'number'}]},
};

function loadVisualWorkflow(){
  const board=document.getElementById('wf-board');
  if(!board)return;
  document.querySelectorAll('.wf-node-tpl').forEach(tpl=>{
    tpl.ondragstart=e=>{e.dataTransfer.setData('node-type',tpl.dataset.type);};
  });
  board.ondragover=e=>e.preventDefault();
  board.ondrop=e=>{
    e.preventDefault();
    const type=e.dataTransfer.getData('node-type');
    if(!type)return;
    const rect=board.getBoundingClientRect();
    _addWfNode(type,e.clientX-rect.left-60,e.clientY-rect.top-20);
  };
  _drawWfEdges();
}

function _addWfNode(type,x,y){
  const id=_wfNextId++;
  const info=_wfNodeTypes[type]||{label:type,color:'#64748b',fields:[]};
  const node={id,type,x:Math.max(0,x),y:Math.max(0,y),params:{},label:info.label};
  _wfNodes.push(node);
  _renderWfNode(node);
  _selectWfNode(node);
}

function _renderWfNode(node){
  const board=document.getElementById('wf-board');
  const info=_wfNodeTypes[node.type]||{color:'#64748b'};
  const el=document.createElement('div');
  el.id='wf-n-'+node.id;
  el.className='wf-node';
  el.style.cssText=`position:absolute;left:${node.x}px;top:${node.y}px;width:130px;background:var(--bg-main,#0f172a);border:2px solid ${info.color};border-radius:10px;padding:8px;cursor:move;z-index:10;font-size:11px;user-select:none`;
  el.innerHTML=`<div style="font-weight:600;color:${info.color};margin-bottom:4px;display:flex;justify-content:space-between;align-items:center">
    <span>${node.label}</span>
    <span style="cursor:pointer;font-size:9px;color:#f87171" onclick="event.stopPropagation();_removeWfNode(${node.id})">&times;</span>
  </div>
  <div style="font-size:9px;color:var(--text-muted)">#${node.id}</div>
  <div style="display:flex;justify-content:space-between;margin-top:6px">
    <div class="wf-port wf-port-in" data-node="${node.id}" style="width:10px;height:10px;background:#22c55e;border-radius:50%;cursor:crosshair" title="输入"></div>
    <div class="wf-port wf-port-out" data-node="${node.id}" style="width:10px;height:10px;background:#ef4444;border-radius:50%;cursor:crosshair" title="输出"></div>
  </div>`;

  let isDragging=false,offX=0,offY=0;
  el.onmousedown=e=>{
    if(e.target.classList.contains('wf-port')){
      _startWfConnect(node.id,e.target.classList.contains('wf-port-out'));
      return;
    }
    isDragging=true;offX=e.offsetX;offY=e.offsetY;
    _selectWfNode(node);
    const onMove=ev=>{
      if(!isDragging)return;
      const rect=board.getBoundingClientRect();
      node.x=Math.max(0,ev.clientX-rect.left-offX);
      node.y=Math.max(0,ev.clientY-rect.top-offY);
      el.style.left=node.x+'px';el.style.top=node.y+'px';
      _drawWfEdges();
    };
    const onUp=()=>{isDragging=false;document.removeEventListener('mousemove',onMove);document.removeEventListener('mouseup',onUp);};
    document.addEventListener('mousemove',onMove);document.addEventListener('mouseup',onUp);
  };
  board.appendChild(el);
}

function _selectWfNode(node){
  _wfSelectedNode=node;
  document.querySelectorAll('.wf-node').forEach(el=>el.style.boxShadow='none');
  const el=document.getElementById('wf-n-'+node.id);
  if(el)el.style.boxShadow='0 0 0 2px var(--accent)';
  _renderWfProps(node);
}

function _renderWfProps(node){
  const info=_wfNodeTypes[node.type]||{fields:[]};
  const c=document.getElementById('wf-prop-content');
  let h=`<div style="font-weight:600;margin-bottom:8px">${node.label} #${node.id}</div>`;
  info.fields.forEach(f=>{
    const val=node.params[f.key]||'';
    h+=`<div style="margin-bottom:6px"><label style="font-size:10px;color:var(--text-muted)">${f.label}</label>
    <input value="${val}" onchange="_updateWfParam(${node.id},'${f.key}',this.value)" placeholder="${f.placeholder||''}"
    style="width:100%;padding:4px 6px;background:var(--bg-input,#0f172a);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px"/></div>`;
  });
  h+=`<div style="margin-top:8px;display:flex;gap:4px">
    <button class="sb-btn2" onclick="_startWfConnectUI(${node.id})" style="font-size:10px;flex:1">连接到...</button>
    <button class="sb-btn2" onclick="_removeWfNode(${node.id})" style="font-size:10px;color:#f87171">删除</button>
  </div>`;
  const edges=_wfEdges.filter(e=>e.from===node.id||e.to===node.id);
  if(edges.length){
    h+=`<div style="margin-top:8px;font-size:10px;color:var(--text-muted)">连接:</div>`;
    edges.forEach(e=>{
      const other=e.from===node.id?e.to:e.from;
      const otherN=_wfNodes.find(n=>n.id===other);
      h+=`<div style="display:flex;justify-content:space-between;align-items:center;margin-top:2px">
        <span style="font-size:10px">${e.from===node.id?'→':'←'} #${other} ${otherN?otherN.label:''}</span>
        <span style="cursor:pointer;color:#f87171;font-size:10px" onclick="_removeWfEdge(${e.from},${e.to})">&times;</span></div>`;
    });
  }
  c.innerHTML=h;
}

function _updateWfParam(nodeId,key,val){
  const n=_wfNodes.find(n=>n.id===nodeId);
  if(n)n.params[key]=val;
}

function _startWfConnect(nodeId,isOutput){
  if(!_wfConnecting){
    _wfConnecting={node:nodeId,isOutput};
    return;
  }
  if(_wfConnecting.node===nodeId){_wfConnecting=null;return;}
  const from=_wfConnecting.isOutput?_wfConnecting.node:nodeId;
  const to=_wfConnecting.isOutput?nodeId:_wfConnecting.node;
  if(!_wfEdges.find(e=>e.from===from&&e.to===to)){
    _wfEdges.push({from,to});
    _drawWfEdges();
  }
  _wfConnecting=null;
  if(_wfSelectedNode)_renderWfProps(_wfSelectedNode);
}

async function _startWfConnectUI(nodeId){
  const target=await ocPrompt('目标节点','',{message:'输入目标节点 ID',inputPlaceholder:'节点 ID'});
  if(!target)return;
  const tid=parseInt(target);
  if(!_wfNodes.find(n=>n.id===tid)){showToast('节点不存在','warn');return;}
  if(!_wfEdges.find(e=>e.from===nodeId&&e.to===tid)){
    _wfEdges.push({from:nodeId,to:tid});
    _drawWfEdges();
  }
  if(_wfSelectedNode)_renderWfProps(_wfSelectedNode);
}

function _removeWfEdge(from,to){
  _wfEdges=_wfEdges.filter(e=>!(e.from===from&&e.to===to));
  _drawWfEdges();
  if(_wfSelectedNode)_renderWfProps(_wfSelectedNode);
}

function _removeWfNode(id){
  _wfNodes=_wfNodes.filter(n=>n.id!==id);
  _wfEdges=_wfEdges.filter(e=>e.from!==id&&e.to!==id);
  const el=document.getElementById('wf-n-'+id);
  if(el)el.remove();
  _drawWfEdges();
  document.getElementById('wf-prop-content').innerHTML='选择一个节点查看属性';
  _wfSelectedNode=null;
}

function _drawWfEdges(){
  const canvas=document.getElementById('wf-canvas');
  if(!canvas)return;
  const board=document.getElementById('wf-board');
  canvas.width=board.offsetWidth;canvas.height=board.offsetHeight;
  const ctx=canvas.getContext('2d');
  ctx.clearRect(0,0,canvas.width,canvas.height);
  ctx.strokeStyle='#3b82f6';ctx.lineWidth=2;
  _wfEdges.forEach(e=>{
    const fromN=_wfNodes.find(n=>n.id===e.from);
    const toN=_wfNodes.find(n=>n.id===e.to);
    if(!fromN||!toN)return;
    const x1=fromN.x+120,y1=fromN.y+40;
    const x2=toN.x+10,y2=toN.y+40;
    ctx.beginPath();ctx.moveTo(x1,y1);
    const cpx=(x1+x2)/2;
    ctx.bezierCurveTo(cpx,y1,cpx,y2,x2,y2);
    ctx.stroke();
    ctx.fillStyle='#3b82f6';
    ctx.beginPath();ctx.moveTo(x2,y2);ctx.lineTo(x2-6,y2-4);ctx.lineTo(x2-6,y2+4);ctx.closePath();ctx.fill();
  });
}

async function clearWorkflowCanvas(){
  if(!(await ocDialog({title:'清空画布',message:'确认清空所有节点？',type:'warning',confirmText:'清空',cancelText:'取消'})))return;
  _wfNodes=[];_wfEdges=[];_wfNextId=1;_wfSelectedNode=null;
  document.getElementById('wf-board').innerHTML='';
  _drawWfEdges();
  document.getElementById('wf-prop-content').innerHTML='选择一个节点查看属性';
}

function _buildWfSteps(){
  if(!_wfNodes.length)return[];
  const visited=new Set();const steps=[];
  const roots=_wfNodes.filter(n=>!_wfEdges.find(e=>e.to===n.id));
  if(!roots.length)roots.push(_wfNodes[0]);
  function walk(node){
    if(visited.has(node.id))return;
    visited.add(node.id);
    const step={type:node.type,...node.params};
    if(node.type==='tap')step.type='adb_cmd',step.command=`input tap ${node.params.x||0} ${node.params.y||0}`;
    if(node.type==='swipe')step.type='adb_cmd',step.command=`input swipe ${node.params.x1||0} ${node.params.y1||0} ${node.params.x2||0} ${node.params.y2||0} ${node.params.duration||300}`;
    if(node.type==='text_input')step.type='adb_cmd',step.command=`input text "${node.params.text||''}"`;
    if(node.type==='key')step.type='adb_cmd',step.command=`input keyevent ${node.params.keycode||'KEYCODE_HOME'}`;
    if(node.type==='app_launch')step.type='adb_cmd',step.command=`monkey -p ${node.params.package||''} 1`;
    if(node.type==='screenshot')step.type='adb_cmd',step.command='screencap -p /sdcard/wf_screenshot.png';
    if(node.type==='delay')step.type='delay',step.seconds=parseFloat(node.params.seconds)||1;
    if(node.type==='loop'){
      const children=_wfEdges.filter(e=>e.from===node.id).map(e=>_wfNodes.find(n=>n.id===e.to)).filter(Boolean);
      step.type='loop';step.count=parseInt(node.params.count)||1;step.steps=[];
      children.forEach(c=>{if(!visited.has(c.id)){visited.add(c.id);const sub={type:c.type,...c.params};step.steps.push(sub);}});
    }
    steps.push(step);
    const nexts=_wfEdges.filter(e=>e.from===node.id).map(e=>_wfNodes.find(n=>n.id===e.to)).filter(Boolean);
    nexts.forEach(n=>walk(n));
  }
  roots.forEach(r=>walk(r));
  return steps;
}

async function saveVisualWorkflow(){
  const name=document.getElementById('wf-name').value.trim()||'未命名工作流';
  const data={name,nodes:_wfNodes,edges:_wfEdges,steps:_buildWfSteps()};
  try{
    await api('POST','/visual-workflows',data);
    showToast('工作流已保存: '+name,'success');
  }catch(e){showToast('保存失败: '+e.message,'error');}
}

async function loadSavedWorkflows(){
  const panel=document.getElementById('wf-saved-list');
  panel.style.display='block';
  try{
    const list=await api('GET','/visual-workflows');
    panel.innerHTML=`<div style="font-size:12px;font-weight:600;margin-bottom:8px">已保存的工作流</div>`+
      (list.length?list.map(w=>`<div style="display:flex;align-items:center;justify-content:space-between;padding:6px 0;border-bottom:1px solid var(--border)">
        <span style="font-size:12px">${w.name}</span>
        <div style="display:flex;gap:4px">
          <button class="sb-btn2" onclick="loadWorkflowById('${w.id}')" style="font-size:10px">加载</button>
          <button class="sb-btn2" onclick="deleteVisualWorkflow('${w.id}')" style="font-size:10px;color:#f87171">删除</button>
        </div></div>`).join(''):'<div style="font-size:11px;color:var(--text-muted)">暂无保存的工作流</div>');
  }catch(e){panel.innerHTML='<div style="color:#f87171">加载失败</div>';}
}

async function loadWorkflowById(id){
  try{
    const data=await api('GET',`/visual-workflows/${id}`);
    clearWorkflowCanvas();
    document.getElementById('wf-name').value=data.name||'';
    _wfNodes=data.nodes||[];_wfEdges=data.edges||[];
    _wfNextId=Math.max(..._wfNodes.map(n=>n.id),0)+1;
    _wfNodes.forEach(n=>_renderWfNode(n));
    _drawWfEdges();
    document.getElementById('wf-saved-list').style.display='none';
  }catch(e){showToast('加载失败: '+e.message,'error');}
}

async function deleteVisualWorkflow(id){
  if(!(await ocDialog({title:'删除工作流',message:'确认删除此工作流？',type:'danger',confirmText:'删除',dangerous:true})))return;
  try{await api('DELETE',`/visual-workflows/${id}`);loadSavedWorkflows();}catch(e){}
}

async function executeVisualWorkflow(){
  const steps=_buildWfSteps();
  if(!steps.length){showToast('请先添加节点','warn');return;}
  const targets=allDevices.filter(d=>d.status==='connected'||d.status==='online').map(d=>d.device_id);
  if(!targets.length){showToast('没有在线设备','warn');return;}
  const sel=await ocPrompt('目标设备','all',{message:'all=全部, 或设备ID逗号分隔',inputPlaceholder:'设备'});
  if(!sel)return;
  const deviceIds=sel==='all'?targets:sel.split(',').map(s=>s.trim());
  try{
    const r=await api('POST','/visual-workflows/execute',{steps,device_ids:deviceIds});
    showToast('工作流已提交执行，任务数: '+(r.task_count||0),'success');
  }catch(e){showToast('执行失败: '+e.message,'error');}
}

