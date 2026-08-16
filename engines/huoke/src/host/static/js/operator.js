/* AI 操盘手控制台卡片（2026-08-17 P4.5）。
 * 只读状态 + 决策时间线 + 一键演习。真执行的双闸在 operator_goals.yaml，
 * 本页刻意不提供「扳实弹」按钮——避免误点把真机投放，实弹是运维显式动作。 */
function _opEsc(s){
  if(typeof _escHtml==='function') return _escHtml(s==null?'':String(s));
  return String(s==null?'':s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
}
const _OP_ACTION=({
  execute:{t:'执行',c:'var(--green)'},
  hold:{t:'暂停',c:'var(--text-muted)'},
  skip:{t:'跳过',c:'var(--text-dim)'},
  blocked:{t:'待批准',c:'var(--amber,#f59e0b)'},
});
function _opBadge(on,label,color){
  const c=on?(color||'var(--green)'):'var(--text-muted)';
  return `<span style="font-size:11px;padding:3px 9px;border-radius:6px;border:1px solid ${c};color:${c}">${label}</span>`;
}
async function loadOperatorPage(){
  const el=document.getElementById('page-operator');
  if(!el) return;
  el.innerHTML='<div style="color:var(--text-dim);font-size:12px;padding:20px">加载中…</div>';
  let st;
  try{ st=await api('GET','/operator/status'); }
  catch(e){ el.innerHTML='<div style="color:var(--red);padding:20px">加载操盘手状态失败（需 admin/operator 权限）</div>'; return; }
  const armed=st.enabled && !st.dry_run;
  const intervalMin=Math.round((st.tick_interval_s||1800)/60);
  const goals=(st.goals||[]).map(g=>`
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:8px;padding:10px 12px;font-size:12px">
      <b>${_opEsc(g.id)}</b> · ${_opEsc(g.platform)}
      <span style="color:var(--text-dim)"> · ${g.devices} 台设备 · 日额度 ${g.daily_cap}/台 · 高危链${g.allow_high_risk?'<span style="color:var(--amber,#f59e0b)">允许自动</span>':'需人工批准'}</span>
    </div>`).join('')||'<div style="color:var(--text-dim);font-size:12px">未配置目标（config/operator_goals.yaml）</div>';
  el.innerHTML=`
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px;flex-wrap:wrap;gap:10px">
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        ${_opBadge(st.enabled,st.enabled?'总开关 开':'总开关 关',st.enabled?'var(--green)':'var(--text-muted)')}
        ${_opBadge(!st.dry_run,st.dry_run?'演习模式':'实弹模式',st.dry_run?'var(--amber,#f59e0b)':'var(--green)')}
        ${_opBadge(st.clock_alive,st.clock_alive?`时钟运转 · ${intervalMin}分`:'时钟停',st.clock_alive?'var(--green)':'var(--red)')}
      </div>
      <div style="display:flex;gap:6px">
        <button class="sb-btn2" style="font-size:11px;padding:5px 12px" onclick="operatorDryRun()">&#9654; 演习一次</button>
        <button class="sb-btn2" style="font-size:11px;padding:5px 12px" onclick="loadOperatorPage()">&#8635; 刷新</button>
      </div>
    </div>
    ${armed?'':'<div style="font-size:11px;color:var(--text-dim);margin-bottom:10px">当前非实弹状态——决策会照常产出并入审计，但不会真的下发任务链。实弹开关在 config/operator_goals.yaml（enabled+dry_run），刻意不放到网页避免误触。</div>'}
    <div style="font-size:12px;font-weight:600;margin:6px 0">目标</div>
    <div style="display:grid;gap:6px;margin-bottom:16px">${goals}</div>
    <div style="font-size:12px;font-weight:600;margin:6px 0">决策时间线</div>
    <div id="op-decisions" style="display:grid;gap:5px"></div>`;
  _opRenderDecisions();
}
async function _opRenderDecisions(){
  const box=document.getElementById('op-decisions');
  if(!box) return;
  let rows=[];
  try{ const r=await api('GET','/operator/decisions?limit=40'); rows=r.decisions||[]; }
  catch(e){ box.innerHTML='<div style="color:var(--red);font-size:12px">决策加载失败</div>'; return; }
  if(!rows.length){ box.innerHTML='<div style="color:var(--text-dim);font-size:12px">暂无决策记录（跑一次演习看看）</div>'; return; }
  box.innerHTML=rows.map(d=>{
    const a=_OP_ACTION[d.action]||{t:d.action,c:'var(--text-dim)'};
    const warn=(d.rule_id==='R2b'||d.action==='blocked');
    const dry=d.dry_run?'<span style="font-size:9px;color:var(--text-dim);border:1px solid var(--border);border-radius:3px;padding:0 3px;margin-left:4px">演习</span>':'';
    return `<div style="background:var(--bg-card);border:1px solid ${warn?'var(--amber,#f59e0b)':'var(--border)'};border-radius:8px;padding:8px 11px;display:flex;align-items:center;gap:10px;font-size:11px">
      <span style="color:var(--text-dim);white-space:nowrap">${_opEsc((d.ts||'').replace('T',' ').replace('Z',''))}</span>
      <span style="font-family:monospace;color:var(--text-dim)">${_opEsc((d.device_id||'').slice(0,10))}</span>
      <span style="padding:1px 7px;border-radius:4px;border:1px solid ${a.c};color:${a.c};white-space:nowrap">${a.t}</span>
      <span style="color:var(--text-dim)">${_opEsc(d.rule_id)}</span>
      ${d.chain_id?`<span style="font-family:monospace">${_opEsc(d.chain_id)}</span>`:''}
      <span style="flex:1;color:var(--text)">${_opEsc(d.reason)}${dry}</span>
    </div>`;
  }).join('');
}
async function operatorDryRun(){
  try{
    const r=await api('POST','/operator/tick',{dry_run:true});
    if(r.enabled===false){ showToast(r.note||'操盘手总开关未开','warn'); return; }
    const decs=r.decisions||[];
    const ex=decs.filter(d=>d.action==='execute').length;
    showToast(`演习完成：${decs.length} 条决策（拟执行 ${ex}），未下发`);
    _opRenderDecisions();
  }catch(e){ showToast('演习失败（需权限或总开关未开）','warn'); }
}
