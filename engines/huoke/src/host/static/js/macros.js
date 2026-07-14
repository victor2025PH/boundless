/* macros.js — 宏录制回放: 宏录制、宏回放 */
/* ── Macro Recording ── */
let _macroRecording=false;
let _macroSteps=[];
let _macroLastTime=0;

function toggleMacroRec(){
  if(_macroRecording) stopMacroRec();
  else startMacroRec();
}

function startMacroRec(){
  _macroRecording=true;
  _macroSteps=[];
  _macroLastTime=Date.now();
  const btn=document.getElementById('macro-rec-btn');
  btn.style.background='#ef4444';btn.style.color='#fff';
  btn.textContent='\u25A0 停止录制';
  showToast('宏录制已开始 — 在手机上操作，所有动作将被记录');
}

async function stopMacroRec(){
  _macroRecording=false;
  const btn=document.getElementById('macro-rec-btn');
  btn.style.background='var(--bg-input)';btn.style.color='';
  btn.textContent='\u26AB 录宏';
  if(_macroSteps.length===0){showToast('未录制到任何操作');return;}
  const name=await ocPrompt('保存宏','macro_'+Date.now(),{message:`录制了 ${_macroSteps.length} 步，输入名称`,inputPlaceholder:'宏名称'});
  if(!name) return;
  const macro={name:name,screen_width:modalScreenSize?modalScreenSize.w:1080,screen_height:modalScreenSize?modalScreenSize.h:2400,steps:_macroSteps};
  api('POST','/macros',macro).then(r=>{
    showToast(`宏已保存: ${name} (${_macroSteps.length} 步)`);
    _macroSteps=[];
    loadMacroList();
  }).catch(e=>showToast('宏保存失败','warn'));
}

async function addSmartMacroStep(){
  if(!_macroRecording){showToast('请先开始录宏','warn');return;}
  const type=await ocPrompt('智能步骤类型','',{message:'1. wait_for<br>2. wait_gone<br>3. tap_element<br>4. screenshot_check<br>5. loop_start<br>6. loop_end<br>7. delay',inputPlaceholder:'输入数字 1-7'});
  if(!type) return;
  const types={1:'wait_for',2:'wait_gone',3:'tap_element',4:'screenshot_check',5:'loop_start',6:'loop_end',7:'delay'};
  const stype=types[type];
  if(!stype){showToast('无效的类型','warn');return;}
  if(stype==='screenshot_check'){
    _recordMacroStep({type:'screenshot_check'});
    showToast('已添加: 异常检测步骤');return;
  }
  if(stype==='loop_start'){
    const count=parseInt(await ocPrompt('循环次数','3'))||3;
    _recordMacroStep({type:'loop_start',count:count});
    showToast(`已添加: 循环开始 (${count}次)`);return;
  }
  if(stype==='loop_end'){
    _recordMacroStep({type:'loop_end'});
    showToast('已添加: 循环结束');return;
  }
  if(stype==='delay'){
    const ms=parseInt(await ocPrompt('延迟时间','1000',{message:'单位: 毫秒',inputPlaceholder:'毫秒'}))||1000;
    _recordMacroStep({type:'delay',delay_ms:ms});
    showToast(`已添加: 延迟 ${ms}ms`);return;
  }
  const target=await ocPrompt('目标文字','',{message:'按钮文字或描述',inputPlaceholder:'要查找的文字'});
  if(!target) return;
  const timeout=parseInt(await ocPrompt('超时时间','10000',{message:'单位: 毫秒',inputPlaceholder:'毫秒'}))||10000;
  _recordMacroStep({type:stype,target:target,timeout_ms:timeout});
  showToast(`已添加: ${stype} → "${target}" (${timeout/1000}s超时)`);
}

function _recordMacroStep(step){
  if(!_macroRecording) return;
  const now=Date.now();
  if(step.delay_ms===undefined) step.delay_ms=now-_macroLastTime;
  _macroLastTime=now;
  _macroSteps.push(step);
  _updateMacroStepCount();
}

function _updateMacroStepCount(){
  const btn=document.getElementById('macro-rec-btn');
  if(btn&&_macroRecording) btn.textContent=`\u25A0 停止 (${_macroSteps.length}步)`;
}

function exportMacroJSON(){
  const sel=document.getElementById('grp-macro-sel');
  if(!sel||!sel.value){showToast('请先选择要导出的宏','warn');return;}
  api('GET',`/macros/${sel.value}`).then(data=>{
    const blob=new Blob([JSON.stringify(data,null,2)],{type:'application/json'});
    const a=document.createElement('a');
    a.href=URL.createObjectURL(blob);
    a.download=sel.value.replace('.json','')+'.macro.json';
    a.click();URL.revokeObjectURL(a.href);
    showToast('宏已导出');
  }).catch(e=>showToast('导出失败','warn'));
}

function importMacroJSON(){
  const input=document.createElement('input');
  input.type='file';input.accept='.json';
  input.onchange=async e=>{
    const file=e.target.files[0];
    if(!file) return;
    try{
      const text=await file.text();
      const macro=JSON.parse(text);
      if(!macro.name) macro.name=file.name.replace('.macro.json','').replace('.json','');
      if(!macro.steps||!macro.steps.length){showToast('无效的宏文件','warn');return;}
      await api('POST','/macros',macro);
      showToast(`宏已导入: ${macro.name} (${macro.steps.length}步)`);
      loadMacroList();
    }catch(err){showToast('导入失败: '+err.message,'warn');}
  };
  input.click();
}

async function loadMacroList(){
  try{
    const r=await api('GET','/macros');
    const sel=document.getElementById('grp-macro-sel');
    if(!sel) return;
    sel.innerHTML='<option value="">-- 选择宏 --</option>'+
      (r.macros||[]).map(m=>`<option value="${m.filename}">${m.name} (${m.steps}步)</option>`).join('');
  }catch(e){}
}

async function groupPlayMacro(){
  const ids=[..._selectedDevices];
  if(!ids.length){showToast('请先选择设备','warn');return;}
  const sel=document.getElementById('grp-macro-sel');
  if(!sel||!sel.value){showToast('请选择要播放的宏','warn');return;}
  const speed=parseFloat(await ocPrompt('播放速度','1.0',{message:'1.0=正常, 2.0=双倍速'}))||1.0;
  const repeat=parseInt(await ocPrompt('重复次数','1'))||1;
  try{
    await api('POST',`/macros/${sel.value}/play`,{device_ids:ids,speed:speed,repeat:repeat});
    showToast(`宏播放已启动: ${sel.options[sel.selectedIndex].text} → ${ids.length} 台设备 (${repeat}次, ${speed}x速)`);
    _startMacroProgressPolling();
  }catch(e){showToast('宏播放失败','warn');}
}

async function singleDevicePlayMacro(){
  if(!modalDeviceId){showToast('请先打开设备','warn');return;}
  const sel=document.getElementById('grp-macro-sel');
  if(!sel||!sel.value){showToast('请选择要播放的宏','warn');return;}
  const speed=parseFloat(await ocPrompt('播放速度','1.0',{message:'1.0=正常'}))||1.0;
  const repeat=parseInt(await ocPrompt('重复次数','1'))||1;
  try{
    await api('POST',`/macros/${sel.value}/play`,{device_ids:[modalDeviceId],speed:speed,repeat:repeat});
    showToast(`宏播放已启动 → ${ALIAS[modalDeviceId]||modalDeviceId.substring(0,8)} (${repeat}次)`);
    _startMacroProgressPolling();
  }catch(e){showToast('宏播放失败','warn');}
}

let _macroProgressTimer=null;
function _startMacroProgressPolling(){
  _stopMacroProgressPolling();
  _macroProgressTimer=setInterval(async()=>{
    try{
      const r=await api('GET','/macros/progress');
      const bar=document.getElementById('anomaly-bar');
      if(!r.progress||Object.keys(r.progress).length===0){
        _stopMacroProgressPolling();
        if(bar){bar.classList.remove('active');bar.innerHTML='';}
        showToast('所有宏播放已完成');
        return;
      }
      if(bar){
        bar.classList.add('active');
        bar.style.borderColor='rgba(59,130,246,.3)';bar.style.background='rgba(59,130,246,.05)';
        bar.innerHTML='<span style="font-size:12px;font-weight:600;color:#3b82f6">\u25B6 宏播放中:</span>'+
          Object.entries(r.progress).map(([did,p])=>{
            const alias=ALIAS[did]||did.substring(0,8);
            const pct=p.percent||0;
            return `<span class="anomaly-item info" style="flex-direction:column;align-items:start;gap:2px">
              <span>${alias}: ${p.current_detail||''} (${p.step}/${p.total})${p.paused?' ⏸':''}</span>
              <div style="width:100%;height:3px;background:rgba(59,130,246,.2);border-radius:2px"><div style="width:${pct}%;height:100%;background:#3b82f6;border-radius:2px"></div></div>
            </span>`;
          }).join('');
      }
    }catch(e){}
  },1500);
}
function _stopMacroProgressPolling(){
  if(_macroProgressTimer){clearInterval(_macroProgressTimer);_macroProgressTimer=null;}
}

let _deviceHealthScores={};
let _deviceRecoveryState={};

let _recoveryStatus={};
async function _fetchRecoveryStatus(){
  try{
    const r=await api('GET','/devices/recovery-status');
    _recoveryStatus=r.devices||{};
  }catch(e){_recoveryStatus={};}
}
async function _fetchHealthData(){
  try{
    const [scores,recon]=await Promise.all([
      api('GET','/devices/health-scores'),
      api('GET','/devices/reconnection-status'),
      _fetchRecoveryStatus(),
    ]);
    if(scores&&scores.scores) _deviceHealthScores=scores.scores;
    if(recon&&recon.recovery_state) _deviceRecoveryState=recon.recovery_state;
  }catch(e){}
}

function _healthBadge(did,isOn){
  const s=_deviceHealthScores[did];
  if(!isOn) return '<span class="scr-health offline">--</span>';
  if(!s) return '';
  const t=s.total;
  const cls=t>=75?'good':t>=50?'warn':'bad';
  return `<span class="scr-health ${cls}" title="稳定:${s.stability} 响应:${s.responsiveness} 任务:${s.task_success} u2:${s.u2_health}">${t}</span>`;
}

function _recoveryBanner(did){
  const r=_deviceRecoveryState[did];
  if(!r) return '';
  const names={reconnect:'L1 重连',reset_transport:'L2 重置',kill_server:'L3 ADB重启',usb_power_cycle:'L4 USB电源',exhausted:'已用尽'};
  return `<div class="scr-recovering">恢复中: ${names[r.level_name]||r.level_name} (${r.offline_sec}s)</div>`;
}

