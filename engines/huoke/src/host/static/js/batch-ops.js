/* batch-ops.js — 批量操作: 一键批量、批量APK安装入口、批量文字输入、批量快捷操作 */

/* ── 批量任务进度追踪面板 (P7) ── */
let _batchPollTimer=null;
function _startBatchTracking(batchId,taskName,total){
  if(_batchPollTimer) clearInterval(_batchPollTimer);
  let panel=document.getElementById('batch-progress-panel');
  if(!panel){panel=document.createElement('div');panel.id='batch-progress-panel';panel.className='batch-progress-panel';document.body.appendChild(panel);}
  panel.innerHTML=`<div class="wpp-header"><span class="wpp-title">🚀 ${taskName} ·批次</span><button class="wpp-close" onclick="clearInterval(_batchPollTimer);this.closest('#batch-progress-panel').remove()">×</button></div>
    <div class="wpp-bar-wrap"><div class="bpp-bar" id="bpp-bar"></div></div>
    <div class="bpp-stats" id="bpp-stats">等待执行…</div>`;
  _batchPollTimer=setInterval(async()=>{
    try{
      const r=await api('GET','/tasks/batch/'+batchId,null,8000);
      const pct=r.progress||0;
      const bar=document.getElementById('bpp-bar');
      const stats=document.getElementById('bpp-stats');
      if(bar) bar.style.width=pct+'%';
      if(stats){
        const parts=[];
        if(r.running) parts.push(`▶ ${r.running}执行`);
        if(r.pending) parts.push(`⏳ ${r.pending}排队`);
        if(r.completed) parts.push(`✓ ${r.completed}完成`);
        if(r.failed) parts.push(`✗ ${r.failed}失败`);
        stats.textContent=parts.join('  ·  ')+(pct?` (${pct}%)`:'');
      }
      if(!r.running&&!r.pending){
        clearInterval(_batchPollTimer);
        const allFail=r.completed===0&&r.failed>0;
        if(bar){bar.style.width='100%';bar.style.background=allFail?'#ef4444':'#22c55e';}
        if(stats) stats.textContent=`完成 ✓  ${r.completed}成功${r.failed?' · '+r.failed+'失败':''}`;
        setTimeout(()=>{const p=document.getElementById('batch-progress-panel');if(p)p.remove();},4000);
      }
    }catch(e){}
  },3000);
}

/* ── 一键批量操作（P7: 8并发调度 + batch_id追踪）── */
async function batchTask(taskType, params){
  const online=allDevices.filter(d=>d.status==='connected'||d.status==='online');
  if(!online.length){showToast('没有在线设备','warn');return;}
  const count=online.length;
  const taskName=TASK_NAMES[taskType]||taskType;
  // 生成批次ID（8位，用于progress追踪）
  const batchId=Math.random().toString(36).slice(2,10);

  // Show inline progress modal
  const overlay=document.createElement('div');
  overlay.style.cssText='position:fixed;inset:0;z-index:9998;background:rgba(0,0,0,.6);display:flex;align-items:center;justify-content:center;padding:20px';
  overlay.innerHTML=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:24px;min-width:340px;max-width:480px;width:100%">
    <div style="font-size:15px;font-weight:700;margin-bottom:14px">&#128640; 批量创建任务</div>
    <div style="font-size:12px;color:var(--text-muted);margin-bottom:10px" id="_bp-label">正在为 ${count} 台设备创建 <b>${taskName}</b> 任务...</div>
    <div style="height:8px;background:var(--border);border-radius:4px;margin-bottom:10px;overflow:hidden">
      <div id="_bp-bar" style="height:100%;width:0%;background:var(--accent);border-radius:4px;transition:width .2s"></div>
    </div>
    <div id="_bp-count" style="font-size:11px;color:var(--text-dim);text-align:right;margin-bottom:10px">0 / ${count}</div>
    <div id="_bp-details" style="max-height:160px;overflow-y:auto;display:grid;gap:4px"></div>
  </div>`;
  document.body.appendChild(overlay);

  const bar=overlay.querySelector('#_bp-bar');
  const countEl=overlay.querySelector('#_bp-count');
  const details=overlay.querySelector('#_bp-details');
  const label=overlay.querySelector('#_bp-label');

  // 立即在设备卡上标记 pending 状态（P6: 零延迟视觉反馈）
  if(typeof _taskMap!=='undefined'){
    for(const d of online) _taskMap[d.device_id]={status:'pending',type:taskType,progress:0};
    if(typeof _updateTaskStrips==='function') _updateTaskStrips();
  }

  let ok=0,fail=0,done=0;

  // P7: 8并发调度（替代串行for-await，6x提速）
  const CHUNK=8;
  for(let i=0;i<online.length;i+=CHUNK){
    const chunk=online.slice(i,i+CHUNK);
    await Promise.allSettled(chunk.map(async d=>{
      const alias=ALIAS[d.device_id]||d.device_id.substring(0,8);
      try{
        await api('POST','/tasks',{type:taskType,device_id:d.device_id,params:params||{},batch_id:batchId});
        ok++;done++;
        details.insertAdjacentHTML('beforeend',`<div style="font-size:11px;color:#22c55e">&#10003; ${alias}</div>`);
      }catch(e){
        fail++;done++;
        details.insertAdjacentHTML('beforeend',`<div style="font-size:11px;color:#ef4444">&#10007; ${alias}: ${(e.message||e+'').substring(0,40)}</div>`);
      }
      bar.style.width=`${Math.round(done/count*100)}%`;
      countEl.textContent=`${done} / ${count}`;
      details.scrollTop=details.scrollHeight;
    }));
  }

  label.innerHTML=ok
    ?`<span style="color:#22c55e">&#10003; 已创建 ${ok} 个 <b>${taskName}</b> 任务${fail?'，<span style="color:#ef4444">'+fail+' 个失败</span>':''}</span>`
    :`<span style="color:#ef4444">&#10007; 全部失败 (${fail} 台)</span>`;

  const closeBtn=document.createElement('button');
  closeBtn.className='dev-btn';
  closeBtn.style.cssText='margin-top:12px;width:100%';
  closeBtn.textContent='关闭';
  closeBtn.onclick=()=>overlay.remove();
  overlay.querySelector('div').appendChild(closeBtn);
  if(!fail) setTimeout(()=>overlay.remove(),2000);

  setTimeout(loadTasks,500);
  setTimeout(loadHealth,1000);
  // P7: 启动批次进度追踪面板
  if(ok>0) _startBatchTracking(batchId,taskName,count);
}

async function toggleVpnAutoReconnect(){
  const on=document.getElementById('vpn-auto-reconnect')?.checked||false;
  try{
    await api('POST','/vpn/auto-reconnect',{enabled:on});
    showToast(on?'VPN自动重连已开启':'VPN自动重连已关闭 (仅任务期间重连)');
  }catch(e){showToast('设置失败','warn');}
}
async function _loadVpnAutoReconnectState(){
  try{
    const r=await api('GET','/vpn/auto-reconnect');
    const cb=document.getElementById('vpn-auto-reconnect');
    if(cb)cb.checked=!!r.enabled;
  }catch(e){}
}
_loadVpnAutoReconnectState();

async function checkAllVPN(){
  showToast('正在检查所有设备VPN...');
  try{
    const r=await api('GET','/vpn/health');
    const data=r.devices||r;
    let healthy=0,unhealthy=0,details=[];
    for(const [did,info] of Object.entries(data)){
      if(info.healthy||info.status==='healthy') healthy++;
      else{unhealthy++;details.push(ALIAS[did]||did.substring(0,8));}
    }
    if(unhealthy>0){
      showToast(`VPN异常: ${details.join(', ')} (${unhealthy}台)，正在修复...`,'warn');
      for(const did of Object.keys(data)){
        const info=data[did];
        if(!info.healthy&&info.status!=='healthy'){
          try{await api('POST',`/vpn/reconnect/${did}`);}catch(e){}
        }
      }
      setTimeout(()=>showToast('VPN重连已触发，请稍后检查'),3000);
    }else{
      showToast(`所有设备VPN正常 (${healthy}台)`);
    }
  }catch(e){showToast('VPN检查失败: '+e.message,'warn');}
}

async function fixAllOffline(){
  const offline=allDevices.filter(d=>d.status!=='connected'&&d.status!=='online');
  if(!offline.length){showToast('没有离线设备');return;}
  showToast(`正在修复 ${offline.length} 台离线设备...`);
  let fixed=0;
  for(const d of offline){
    try{
      await api('POST',`/devices/${d.device_id}/reconnect`);
      fixed++;
    }catch(e){}
  }
  showToast(`已触发 ${fixed} 台设备重连，请等待30秒后查看结果`);
  setTimeout(()=>{loadDevices();loadHealth();},15000);
}

async function runUsbDiagnostics(){
  showToast('正在扫描USB/ADB设备状态...');
  try{
    const r=await api('GET','/devices/usb-diagnostics');
    const m=document.createElement('div');
    m.style.cssText='position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.7);display:flex;align-items:center;justify-content:center;padding:20px';
    m.onclick=e=>{if(e.target===m)m.remove();};
    let h=`<div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;max-width:720px;width:100%;max-height:85vh;overflow-y:auto;padding:24px">`;
    h+=`<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">`;
    h+=`<h3 style="margin:0;font-size:16px">&#128268; USB / ADB 诊断报告</h3>`;
    h+=`<button onclick="this.closest('div[style*=fixed]').remove()" style="background:none;border:none;color:var(--text-muted);font-size:20px;cursor:pointer">&times;</button></div>`;
    h+=`<div style="font-size:11px;color:var(--text-muted);margin-bottom:12px">${r.adb_version||'ADB版本未知'}</div>`;

    h+=`<div style="font-weight:600;color:#22c55e;margin:12px 0 6px">&#9989; 正常连接 (${r.connected?.length||0})</div>`;
    if(r.connected?.length){
      h+=`<table style="width:100%;font-size:12px;border-collapse:collapse">`;
      h+=`<tr style="color:var(--text-muted);text-align:left"><th style="padding:4px 8px">序列号</th><th>编号</th><th>型号</th><th>电量</th><th>transport</th></tr>`;
      r.connected.forEach(d=>{
        h+=`<tr style="border-top:1px solid var(--border)"><td style="padding:4px 8px;font-family:monospace">${d.device_id.substring(0,12)}</td><td>${d.alias||'-'}</td><td>${d.model||'-'}</td><td>${d.battery!==undefined?d.battery+'%':'-'}</td><td>${d.transport_id||'-'}</td></tr>`;
      });
      h+=`</table>`;
    }

    h+=`<div style="font-weight:600;color:#f59e0b;margin:16px 0 6px">&#9888; USB异常设备 (${r.problem?.length||0})</div>`;
    if(r.problem?.length){
      r.problem.forEach(d=>{
        h+=`<div style="background:rgba(245,158,11,.1);border:1px solid rgba(245,158,11,.3);border-radius:8px;padding:10px 12px;margin-bottom:8px">`;
        h+=`<div style="font-weight:600;font-size:13px">${d.device_id.substring(0,12)} — <span style="color:#f59e0b">${d.status}</span></div>`;
        h+=`<div style="font-size:12px;color:var(--text-muted);margin-top:4px">&#128161; ${d.diagnosis}</div>`;
        h+=`</div>`;
      });
    }else{
      h+=`<div style="color:var(--text-muted);font-size:12px">没有USB异常设备</div>`;
    }

    h+=`<div style="font-weight:600;color:#ef4444;margin:16px 0 6px">&#10060; 配置但未检测到 (${r.configured_missing?.length||0})</div>`;
    if(r.configured_missing?.length){
      r.configured_missing.forEach(d=>{
        h+=`<div style="background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:8px;padding:10px 12px;margin-bottom:8px">`;
        h+=`<div style="font-weight:600;font-size:13px">${d.display_name} (${d.device_id.substring(0,12)})</div>`;
        h+=`<div style="font-size:12px;color:var(--text-muted);margin-top:4px">&#128161; ${d.diagnosis}</div>`;
        h+=`</div>`;
      });
    }else{
      h+=`<div style="color:var(--text-muted);font-size:12px">所有配置设备均已检测到</div>`;
    }

    if(r.usb_tree?.length){
      h+=`<div style="font-weight:600;color:#06b6d4;margin:16px 0 6px">&#128268; USB 端口映射 (${r.usb_tree.length} 端口)</div>`;
      r.usb_tree.forEach(port=>{
        h+=`<div style="background:rgba(6,182,212,.08);border:1px solid rgba(6,182,212,.25);border-radius:8px;padding:8px 12px;margin-bottom:6px;display:flex;align-items:center;gap:10px">`;
        h+=`<span style="font-size:16px">&#128268;</span>`;
        h+=`<span style="font-size:11px;color:var(--text-muted);min-width:50px">端口 ${port.port}</span>`;
        port.devices.forEach(d=>{
          const color=d.status==='device'?'#22c55e':'#f59e0b';
          h+=`<span style="background:${color}20;border:1px solid ${color}50;padding:2px 8px;border-radius:4px;font-size:11px;color:${color}">${d.alias||d.device_id.substring(0,8)} (${d.model||d.status})</span>`;
        });
        h+=`</div>`;
      });
    }

    if(r.summary){
      const s=r.summary;
      h+=`<div style="margin-top:16px;padding:12px;background:var(--bg-input);border-radius:8px;font-size:12px;display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px;text-align:center">`;
      h+=`<div><div style="font-size:20px;font-weight:700;color:var(--text-main)">${s.total_configured}</div><div style="color:var(--text-muted)">已配置</div></div>`;
      h+=`<div><div style="font-size:20px;font-weight:700;color:#22c55e">${s.connected}</div><div style="color:var(--text-muted)">已连接</div></div>`;
      h+=`<div><div style="font-size:20px;font-weight:700;color:#f59e0b">${s.problem}</div><div style="color:var(--text-muted)">异常</div></div>`;
      h+=`<div><div style="font-size:20px;font-weight:700;color:#ef4444">${s.missing}</div><div style="color:var(--text-muted)">缺失</div></div>`;
      h+=`<div><div style="font-size:20px;font-weight:700;color:#06b6d4">${s.usb_ports_active}</div><div style="color:var(--text-muted)">USB端口</div></div>`;
      h+=`</div>`;
    }

    h+=`<div style="margin-top:16px;text-align:right"><button class="qa-btn" onclick="this.closest('div[style*=fixed]').remove()" style="padding:8px 20px">关闭</button></div>`;
    h+=`</div>`;
    m.innerHTML=h;
    document.body.appendChild(m);
    showToast(`诊断完成: ${r.connected?.length||0} 正常, ${r.problem?.length||0} 异常, ${r.configured_missing?.length||0} 未检测到`);
  }catch(e){showToast('USB诊断失败: '+e.message,'warn');}
}

function _renderHealthBar(){
  const bar=document.getElementById('ov-health-bar');
  if(!bar||!allDevices.length) return;
  bar.innerHTML=allDevices.map(d=>{
    const isOn=d.status==='connected'||d.status==='online';
    const alias=ALIAS[d.device_id]||d.device_id.substring(0,4);
    const score=_healthTotalNum(d.device_id);
    let color='#4b5563';
    if(isOn&&score!=null){
      if(score>=80) color='#22c55e';
      else if(score>=50) color='#eab308';
      else color='#ef4444';
    }
    return `<div style="display:flex;flex-direction:column;align-items:center;gap:2px" title="${alias}: ${isOn?'在线':'离线'}${score!=null?' 健康:'+score:''}">
      <div style="width:28px;height:28px;border-radius:8px;background:${color};display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;font-weight:700">${alias.replace(/[^0-9]/g,'')}</div>
      <span style="font-size:8px;color:var(--text-muted)">${score!=null?score+'%':''}</span>
    </div>`;
  }).join('');
}

/* ── 意图回显 确认卡 (★ P1 升级: 更醒目，失败时显示警告) ── */
function _intentChipsHtml(r){
  const chips=[];
  // 意图名 chip
  if(r.intent_display){
    chips.push(`<span class="chat-chip intent">${esc(r.intent_display)}</span>`);
  }
  // 国家 chip
  const countryFlag={'italy':'🇮🇹','philippines':'🇵🇭','usa':'🇺🇸','germany':'🇩🇪','france':'🇫🇷',
    'spain':'🇪🇸','brazil':'🇧🇷','japan':'🇯🇵','thailand':'🇹🇭','vietnam':'🇻🇳','uk':'🇬🇧',
    'indonesia':'🇮🇩','malaysia':'🇲🇾','brazil':'🇧🇷','vietnam':'🇻🇳','singapore':'🇸🇬'};
  if(r.country_desc){
    const flag=countryFlag[r.country_desc.toLowerCase()]||'🌍';
    chips.push(`<span class="chat-chip country">${flag} ${esc(r.country_desc)}</span>`);
  }
  // 人群 chip
  if(r.targeting_desc){
    chips.push(`<span class="chat-chip targeting">🎯 ${esc(r.targeting_desc)}</span>`);
  }
  if(!chips.length) return '';

  // ★ P1: 解析确认卡 — 当有 targeting 时显示绿色确认条
  const hasTargeting = !!(r.country_desc || r.targeting_desc);
  const confirmBar = hasTargeting
    ? `<div style="margin-top:5px;padding:4px 8px;border-radius:6px;background:rgba(34,197,94,.08);border:1px solid rgba(34,197,94,.2);font-size:10px;color:#4ade80">
        ✅ AI 已理解目标人群，将按以上条件过滤
       </div>`
    : '';
  return `<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">${chips.join('')}</div>${confirmBar}`;
}

/* ── 设备健康警告 HTML ── */
function _deviceWarningsHtml(r){
  const warns=r&&r.device_warnings;
  if(!warns||!warns.length)return '';
  return '<div style="margin-top:6px;padding:6px 8px;background:rgba(251,191,36,.1);border:1px solid rgba(251,191,36,.3);border-radius:6px">'
    +warns.slice(0,4).map(w=>`<div style="font-size:10px;color:#fbbf24">${esc(w)}</div>`).join('')
    +'</div>';
}

/* ── 任务状态自动追踪 ── */
let _chatTaskMonitors={};
function _monitorChatTasks(bubbleId,taskIds){
  if(!taskIds||!taskIds.length)return;
  let attempts=0;
  const maxAttempts=8;
  const check=async()=>{
    attempts++;
    if(attempts>maxAttempts)return;
    try{
      const results=await Promise.all(taskIds.slice(0,6).map(tid=>
        api('GET','/tasks/'+encodeURIComponent(tid)).catch(()=>null)
      ));
      const bubble=document.getElementById(bubbleId);
      if(!bubble)return;
      const statusLine=bubble.querySelector('.chat-task-status');
      const done=results.filter(t=>t&&['completed','failed','cancelled'].includes(t.status));
      const failed=results.filter(t=>t&&t.status==='failed');
      const running=results.filter(t=>t&&t.status==='running');
      if(statusLine){
        if(done.length===taskIds.slice(0,6).length){
          const failCount=failed.length;
          if(failCount===0){
            // 全部成功 — 显示摘要统计
            const stats=[];
            results.forEach(t=>{
              const r=t&&t.result;
              if(!r) return;
              if(r.followed||r.viewers_followed) stats.push(`关注${(r.followed||0)+(r.viewers_followed||0)+(r.hosts_followed||0)}人`);
              if(r.comments_sent) stats.push(`评论${r.comments_sent}条`);
              if(r.rooms_visited) stats.push(`进${r.rooms_visited}个直播间`);
              if(r.videos_visited) stats.push(`看${r.videos_visited}个视频`);
            });
            const statStr=stats.length?` · ${stats.join(' · ')}:`:'';
            statusLine.innerHTML=`<span style="color:#22c55e">✓ 全部 ${done.length} 个任务已完成${statStr}</span>`;
          } else {
            // ★ P3-4: 内联展示每个失败任务的错误原因
            const errLines=failed.map(t=>{
              const res=t&&t.result;
              const err=(res&&(res.error||res.message||res.err))||'未知错误';
              const dev=(t.device_id||'').substring(0,16);
              return `<div style="font-size:9px;color:#fca5a5;margin-top:2px">✗ ${esc(dev)}: ${esc(String(err).substring(0,80))}</div>`;
            }).join('');
            statusLine.innerHTML=`<span style="color:#f87171">⚠ ${failCount}/${done.length} 个任务失败</span>${errLines}`;
          }
          return;
        } else if(running.length>0){
          const pct=results.filter(t=>t).map(t=>t.progress||0).reduce((a,b)=>a+b,0)/Math.max(results.length,1);
          const pctStr=pct>0?` ${Math.round(pct)}%`:'';
          statusLine.innerHTML=`<span style="color:var(--accent)">▶ ${running.length} 个任务运行中${pctStr}...</span>`;
        }
      }
      if(attempts<maxAttempts){
        _chatTaskMonitors[bubbleId]=setTimeout(check,30000);
      }
    }catch(e){/* silent */}
  };
  _chatTaskMonitors[bubbleId]=setTimeout(check,20000);
}

async function sendChat(){
  const input=document.getElementById('chat-input');const msg=input.value.trim();if(!msg)return;
  input.value='';const btn=document.getElementById('chat-btn');btn.disabled=true;btn.textContent='发送中...';
  const box=document.getElementById('chat-messages');
  const now=new Date().toLocaleTimeString('zh-CN',{hour:'2-digit',minute:'2-digit'});
  box.innerHTML+=`<div class="chat-msg user"><div class="chat-bubble">${esc(msg)}</div><div class="chat-meta">${now}</div></div>`;
  box.scrollTop=box.scrollHeight;
  try{
    const t0=Date.now();const r=await api('POST','/chat',{message:msg});const elapsed=Date.now()-t0;
    const reply=(r.reply||'操作完成').replace(/\n/g,'<br>');const tc=r.task_ids?.length||0;

    // 意图回显 chip
    const chipsHtml=_intentChipsHtml(r);
    // 设备警告
    const warningsHtml=_deviceWarningsHtml(r);
    // 任务快捷入口
    const hintBlock=formatChatTaskHintsHtml(r);

    let extra=chipsHtml+warningsHtml;
    if(tc)extra+=`<br><span style="font-size:11px;color:var(--text-muted)">✓ 已创建 ${tc} 个任务</span>`;
    if(hintBlock) extra+=hintBlock;
    if(r.target_count>1)extra+=`<br><span style="font-size:10px;color:var(--accent)">➤ 目标: ${r.target_count} 台设备</span>`;
    if(r.commands&&r.commands.length){
      const cmdSummary=r.commands.slice(0,5).map(c=>{
        const dev=c.device?ALIAS[c.device]||c.device.substring(0,8):'';
        const icon=c.success!==false?'✓':'✗';
        const out=(c.output||c.command||'').substring(0,60);
        return `<div style="font-size:9px;color:var(--text-muted)">${icon} ${dev?dev+': ':''}${esc(out)}</div>`;
      }).join('');
      extra+=cmdSummary;
      if(r.commands.length>5)extra+=`<div style="font-size:9px;color:var(--text-muted)">...还有${r.commands.length-5}条</div>`;
    }
    const dr=r.dry_run?' · 预览':'';const pc=r.pending_confirmation?' · 待确认':'';const cf=r.confirmed?' · 已确认':'';

    // ★ P1: meta 行改为中文意图名，隐藏 session_id（减少干扰）
    const intentLabel=r.intent_display||(r.intent?r.intent:'')||'';
    const metaText=intentLabel?`${esc(intentLabel)} · ${elapsed}ms`:`${elapsed}ms`;

    // 任务状态跟踪行（30s后自动刷新）
    const bubbleId='chat-bubble-'+Date.now();
    let statusLine='';
    if(tc){
      statusLine=`<div class="chat-task-status" style="margin-top:5px;font-size:10px;color:var(--text-muted)">⏳ 等待任务启动（约20秒后检查状态）</div>`;
    }

    box.innerHTML+=`<div class="chat-msg bot" id="${bubbleId}"><div class="chat-bubble">${reply}${extra}${statusLine}</div><div class="chat-meta">${metaText}${dr}${pc}${cf}</div></div>`;
    box.scrollTop=box.scrollHeight;

    // 启动任务状态追踪
    if(tc&&r.task_ids&&r.task_ids.length){
      _monitorChatTasks(bubbleId,r.task_ids);
    }

    setTimeout(function(){if(typeof loadTasks==='function')loadTasks();},tc?800:400);
    setTimeout(()=>{loadHealth();loadDevices();},2000);
  }catch(e){
    box.innerHTML+=`<div class="chat-msg bot"><div class="chat-bubble" style="border-color:var(--red)">请求失败: ${esc(e.message)}</div><div class="chat-meta">错误</div></div>`;
  }
  box.scrollTop=box.scrollHeight;btn.disabled=false;btn.textContent='发送';input.focus();
}

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}

/** POST /chat 返回的 task_hints：可点击打开任务详情（依赖 tasks-chat.js 中的 showTaskDetail）；超过 2 条链接时用 details 折叠。 */
function formatChatTaskHintsHtml(r){
  const hints=r&&r.task_hints;
  if(!hints||!hints.length) return '';
  const escH=(x)=>{const d=document.createElement('div');d.textContent=x==null?'':String(x);return d.innerHTML;};
  const openDetail=(tid)=>{
    const id=String(tid);
    // 直接打开 modal，不跳转页面（避免离开对话台）
    return 'event.preventDefault();if(typeof showTaskDetail===\'function\')showTaskDetail('+JSON.stringify(id)+');';
  };
  const errParts=[];
  const okParts=[];
  let okCount=0;
  for(const h of hints){
    if(h.error){
      errParts.push('<div style="font-size:10px;color:#f87171;margin-top:4px">'+escH(h.action||'?')+': '+escH(h.error)+'</div>');
      continue;
    }
    if(Array.isArray(h.tasks)&&h.tasks.length){
      okCount+=Math.min(h.tasks.length,20);
      const rows=h.tasks.slice(0,20).map(t=>{
        const tid=t.task_id||'';
        const lab=escH(t.device_label||t.device_serial||'');
        const short=escH(t.task_id_short||(String(tid).length>8?String(tid).slice(0,8)+'…':String(tid)));
        return '<div style="font-size:10px;margin-top:3px"><a href="#" style="color:var(--accent);text-decoration:underline" onclick="'+openDetail(tid)+'">'+short+'</a> <span style="color:var(--text-muted)">'+lab+'</span></div>';
      }).join('');
      const more=h.tasks.length>20?'<div style="font-size:9px;color:var(--text-muted)">…共 '+h.tasks.length+' 条</div>':'';
      okParts.push('<div style="margin-top:4px">'+rows+more+'</div>');
      continue;
    }
    if(h.task_id){
      okCount++;
      const tid=String(h.task_id);
      const lab=escH(h.device_label||'');
      const short=escH(h.task_id_short||(tid.length>8?tid.slice(0,8)+'…':tid));
      okParts.push('<div style="font-size:10px;margin-top:4px"><a href="#" style="color:var(--accent);text-decoration:underline" onclick="'+openDetail(tid)+'">'+short+'</a> <span style="color:var(--text-muted)">'+lab+'</span></div>');
    }
  }
  const errHtml=errParts.join('');
  const okInner=okParts.join('');
  if(!okInner) return errHtml;
  const fold=okCount>2;
  const okWrapped=fold
    ? '<details class="chat-task-hints" style="margin-top:8px;padding-top:6px;border-top:1px solid var(--border)"><summary style="cursor:pointer;font-size:11px;color:var(--accent);list-style:none">任务快捷入口 <span style="color:var(--text-muted)">('+okCount+')</span></summary><div style="margin-top:8px">'+okInner+'</div></details>'
    : '<div style="margin-top:8px;padding-top:6px;border-top:1px solid var(--border)"><div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">任务快捷入口</div>'+okInner+'</div>';
  return errHtml+okWrapped;
}

/* ═══════════════════════════════════════════
   一键养号面板
   ═══════════════════════════════════════════ */

let _campaignCountry='italy';

function _showLaunchCampaign(){
  document.getElementById('campaign-modal').style.display='flex';
  _loadCampaignReadiness();
}

function _closeCampaign(){
  document.getElementById('campaign-modal').style.display='none';
}

function _selectCampaignCountry(btn){
  document.querySelectorAll('.campaign-country').forEach(b=>{
    b.style.borderColor='var(--border)';b.style.background='transparent';
    b.classList.remove('selected');
  });
  btn.style.borderColor='var(--accent)';btn.style.background='rgba(59,130,246,.1)';
  btn.classList.add('selected');
  _campaignCountry=btn.dataset.country;
  _loadCampaignReadiness();
}

async function _loadCampaignReadiness(){
  const el=document.getElementById('campaign-readiness');
  if(!el) return;
  el.innerHTML='<span style="color:var(--accent)">\u23F3 检测中...</span>';
  try{
    const d=await api('GET','/tiktok/readiness');
    const s=d.summary;
    const devs=d.devices||[];

    // 统计阶段分布
    const phases={cold_start:0,interest_building:0,active:0,unknown:0};
    devs.forEach(v=>{phases[v.phase]=(phases[v.phase]||0)+1;});

    // 意大利时区
    const now=new Date();
    const itHour=(now.getUTCHours()+1)%24;
    const inGolden=itHour>=9&&itHour<=22;
    const tzInfo=inGolden
      ?`<span style="color:#22c55e">\u{1F7E2} \u610F\u5927\u5229\u73B0\u5728 ${itHour}:00 \u6D3B\u8DC3\u65F6\u6BB5</span>`
      :`<span style="color:#eab308">\u{1F7E1} \u610F\u5927\u5229\u73B0\u5728 ${itHour}:00 \u975E\u6D3B\u8DC3\u65F6\u6BB5</span>`;

    // VPN 配置池国家匹配
    const hasCountry=d.available_countries?.includes(_campaignCountry);
    const vpnNote=hasCountry
      ?'<span style="color:#22c55e">\u2705 \u914D\u7F6E\u6C60\u6709 '+_campaignCountry+' \u8282\u70B9</span>'
      :'<span style="color:#eab308">\u26A0 \u914D\u7F6E\u6C60\u65E0 '+_campaignCountry+' \u8282\u70B9\uFF0C\u5C06\u7528\u5F53\u524D VPN</span>';

    let html=`<div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:8px">
      <div>\u{1F4F1} <b>${s.online}</b>/${s.total} \u5728\u7EBF</div>
      <div>\u2705 <b>${s.ready}</b> \u5C31\u7EEA</div>
      <div>\u{1F512} <b>${s.vpn_connected}</b> VPN</div>
      ${s.recovering?`<div>\u26A0 <b>${s.recovering}</b> \u6062\u590D\u4E2D</div>`:''}
    </div>`;
    html+=`<div style="display:flex;gap:12px;font-size:11px;margin-bottom:6px">
      <span>\u{1F331} \u51B7\u542F\u52A8 ${phases.cold_start}</span>
      <span>\u{1F33F} \u5174\u8DA3\u5EFA\u7ACB ${phases.interest_building}</span>
      <span>\u{1F525} \u6D3B\u8DC3\u671F ${phases.active}</span>
    </div>`;
    html+=`<div style="font-size:11px">${tzInfo}</div>`;
    html+=`<div style="font-size:11px;margin-top:2px">${vpnNote}</div>`;

    el.innerHTML=html;
  }catch(e){
    el.innerHTML='<span style="color:#ef4444">\u274C \u68C0\u6D4B\u5931\u8D25: '+e.message+'</span>';
  }
}

async function _launchCampaign(){
  const country=_campaignCountry;
  const duration=parseInt(document.getElementById('campaign-duration').value)||30;
  const autoVpn=document.getElementById('campaign-auto-vpn').checked;
  const scope=document.getElementById('campaign-scope').value;

  const btn=document.getElementById('campaign-launch-btn');
  const prog=document.getElementById('campaign-progress');
  const progLabel=document.getElementById('campaign-prog-label');
  const progCount=document.getElementById('campaign-prog-count');
  const progBar=document.getElementById('campaign-prog-bar');
  const progDetails=document.getElementById('campaign-prog-details');

  btn.disabled=true;btn.textContent='\u23F3 \u542F\u52A8\u4E2D...';
  if(prog) prog.style.display='block';
  if(progLabel) progLabel.textContent='\u{1F680} \u6B63\u5728\u542F\u52A8\u517B\u53F7\u6218\u5F79...';
  if(progBar) progBar.style.width='10%';
  if(progDetails) progDetails.innerHTML='';
  if(progCount) progCount.textContent='';

  try{
    // 如果选 ready 模式，先获取就绪设备
    let deviceIds=undefined;
    if(scope==='ready'){
      const rd=await api('GET','/tiktok/readiness');
      deviceIds=(rd.devices||[]).filter(d=>d.ready).map(d=>d.device_id);
      if(!deviceIds.length){
        showToast('\u6CA1\u6709\u5C31\u7EEA\u8BBE\u5907','warn');
        btn.disabled=false;btn.textContent='\u{1F680} \u542F\u52A8\u517B\u53F7';
        return;
      }
    }

    if(progBar) progBar.style.width='30%';
    if(progLabel) progLabel.textContent=autoVpn?'\u{1F512} \u90E8\u7F72 VPN \u4E2D...':'\u{1F4CB} \u521B\u5EFA\u4EFB\u52A1\u4E2D...';

    const body={country, duration_minutes:duration, auto_vpn:autoVpn};
    if(deviceIds) body.device_ids=deviceIds;

    const d=await api('POST','/tiktok/launch-campaign',body);

    if(progBar) progBar.style.width='100%';

    // VPN 阶段结果
    let vpnHtml='';
    if(d.results?.vpn_phase){
      const vp=d.results.vpn_phase;
      const vpnOk=Object.values(vp).filter(v=>v.ok).length;
      vpnHtml+=`<div style="font-size:11px;font-weight:600;margin-bottom:2px">\u{1F512} VPN: ${vpnOk}/${Object.keys(vp).length} \u6210\u529F</div>`;
      for(const [short,r] of Object.entries(vp)){
        const icon=r.ok?'\u2705':'\u274C';
        const color=r.ok?'#22c55e':'#ef4444';
        vpnHtml+=`<div style="color:${color};font-size:10px">${icon} ${short} ${r.action||''} ${r.error||''}</div>`;
      }
    }

    // 任务阶段结果
    let taskHtml='';
    const tp=d.results?.task_phase||{};
    const taskOk=Object.values(tp).filter(v=>v.ok).length;
    taskHtml+=`<div style="font-size:11px;font-weight:600;margin-top:4px;margin-bottom:2px">\u{1F33F} \u517B\u53F7\u4EFB\u52A1: ${taskOk}/${Object.keys(tp).length} \u5DF2\u521B\u5EFA</div>`;

    if(progLabel) progLabel.innerHTML=d.ok
      ?`<span style="color:#22c55e">\u2705 \u517B\u53F7\u6218\u5F79\u5DF2\u542F\u52A8</span>`
      :`<span style="color:#ef4444">\u274C \u542F\u52A8\u5931\u8D25</span>`;
    if(progCount) progCount.textContent=`${d.tasks_created} \u53F0\u8BBE\u5907 \u00B7 ${country} \u00B7 ${duration}\u5206\u949F`;
    if(progDetails) progDetails.innerHTML=vpnHtml+taskHtml;

    const msg=`\u{1F680} \u517B\u53F7\u6218\u5F79\u5DF2\u542F\u52A8: ${d.tasks_created} \u53F0 \u00B7 ${country} \u00B7 ${duration}\u5206\u949F`;
    showToast(msg,'success');

    // 刷新任务列表
    setTimeout(()=>{try{loadTasks();}catch(e){}},1000);

  }catch(e){
    if(progLabel) progLabel.innerHTML=`<span style="color:#ef4444">\u274C ${e.message}</span>`;
    showToast('\u542F\u52A8\u5931\u8D25: '+e.message,'warn');
  }
  btn.disabled=false;btn.textContent='\u{1F680} \u542F\u52A8\u517B\u53F7';
}

async function _createCampaignSchedule(){
  const country=_campaignCountry;
  const duration=parseInt(document.getElementById('campaign-duration').value)||30;
  const choice=await ocPrompt('\u9009\u62e9\u517b\u53f7\u8ba1\u5212','',{message:'1. \u6bcf\u5929 3 \u6b21 (9:00/13:00/19:00)<br>2. \u6bcf\u5929 2 \u6b21 (10:00/18:00)<br>3. \u6bcf\u5929 1 \u6b21 (10:00)<br>4. \u6bcf 2 \u5c0f\u65f6 1 \u6b21',inputPlaceholder:'\u8f93\u5165 1-4'});
  if(!choice) return;

  const plans={
    '1':[{cron:'0 9 * * *',name:'\u65E9\u95F4'},{cron:'0 13 * * *',name:'\u5348\u95F4'},{cron:'0 19 * * *',name:'\u665A\u95F4'}],
    '2':[{cron:'0 10 * * *',name:'\u4E0A\u5348'},{cron:'0 18 * * *',name:'\u4E0B\u5348'}],
    '3':[{cron:'0 10 * * *',name:'\u6BCF\u65E5'}],
    '4':[{cron:'0 */2 * * *',name:'\u6BCF2h'}],
  };
  const selected=plans[choice.trim()];
  if(!selected){showToast('\u65E0\u6548\u9009\u62E9','warn');return;}

  let created=0;
  for(const p of selected){
    try{
      await api('POST','/scheduled-jobs',{
        name:`TikTok \u517B\u53F7 ${p.name} (${country}, ${duration}min)`,
        cron:p.cron,
        action:'tiktok_warmup',
        params:{duration_minutes:duration,target_country:country}
      });
      created++;
    }catch(e){}
  }
  showToast(`\u5DF2\u521B\u5EFA ${created} \u4E2A\u5B9A\u65F6\u517B\u53F7\u4EFB\u52A1`,'success');
}

