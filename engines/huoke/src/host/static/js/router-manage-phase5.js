// Phase 5 新增功能 — 代理轮换 / 备份历史 / 健康评分
// 此文件内容会被追加到 router-manage.js

// ══ 代理自动轮换 ══
async function _rmRotateProxy(routerId, btn){
  const r=_rmData.routers.find(x=>x.router_id===routerId);
  if(!(await ocDialog({title:'代理轮换',message:`将自动为路由器「${r?.name||routerId}」选取备用代理并重新部署。<br>速率限制: 30分钟/次，约60秒完成。`,type:'info',confirmText:'轮换',cancelText:'取消'})))return;
  if(btn){btn.disabled=true;btn.textContent='轮换中...';}
  showToast('正在自动轮换代理，请稍候（约60秒）...','info');
  try{
    const res=await api('POST',`/routers/${routerId}/rotate-proxy`,{reason:'手动触发轮换'});
    if(res.skipped){
      showToast('\u23F3 '+res.skipped,'warn');
    } else if(res.ok){
      showToast(`\u2705 代理轮换成功！新出口IP: ${res.exit_ip||'验证中'}${res.geo_match===false?' \u26A0\uFE0F 地理不匹配':''}`, 'ok');
    } else {
      showToast('\u274C 轮换失败: '+(res.error||'未知原因'),'warn');
    }
    await loadRouterManagePage();
  }catch(e){showToast('轮换失败: '+e.message,'warn');}
  finally{if(btn){btn.disabled=false;btn.textContent='\uD83D\uDD04轮换代理';}}
}

// ══ 备份历史 + 轮换历史面板 ══
async function _rmShowBackups(routerId){
  const r=_rmData.routers.find(x=>x.router_id===routerId);
  try{
    const [backups, history] = await Promise.all([
      api('GET',`/routers/${routerId}/backups`),
      api('GET',`/routers/${routerId}/rotation-history`).catch(()=>({history:[],blacklist:[]})),
    ]);
    const bList=backups.backups||[];
    const hList=history.history||[];
    const blList=history.blacklist||[];

    const overlay=document.createElement('div');
    overlay.className='rm-overlay';
    overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center';

    const backupRows=bList.length
      ?bList.map(b=>`<div style="display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:var(--bg-main);border-radius:6px;margin-bottom:4px;font-size:10px">
          <div><div style="font-family:monospace;color:var(--text-main)">${_rmEsc(b.filename)}</div>
          <div style="color:var(--text-dim)">${_rmEsc(b.created_at_str)} &middot; ${(b.size_bytes/1024).toFixed(1)}KB</div></div>
          <button class="sb-btn2" style="font-size:9px;padding:2px 8px;color:#f59e0b" onclick="_rmRestoreBackup('${routerId}','${_rmEsc(b.filename)}',this)">回滚</button>
        </div>`).join('')
      :'<div style="color:var(--text-dim);font-size:10px;text-align:center;padding:10px">暂无备份</div>';

    const histRows=hList.length
      ?hList.map(h=>`<div style="padding:8px 10px;background:var(--bg-main);border-radius:6px;margin-bottom:4px;font-size:10px">
          <div style="display:flex;justify-content:space-between">
            <span style="color:${h.success?'#22c55e':'#ef4444'}">${h.success?'\u2705 成功':'\u274C 失败'}</span>
            <span style="color:var(--text-dim)">${_rmEsc(h.ts_str||'')}</span>
          </div>
          <div style="color:var(--text-dim);margin-top:2px">${_rmEsc(h.reason||'')} &middot; IP: ${_rmEsc(h.exit_ip||'-')}</div>
          <div style="font-family:monospace;font-size:9px;color:var(--text-dim)">${_rmEsc((h.from||[]).join(','))} &rarr; ${_rmEsc((h.to||[]).join(','))}</div>
        </div>`).join('')
      :'<div style="color:var(--text-dim);font-size:10px;text-align:center;padding:10px">暂无轮换历史</div>';

    overlay.innerHTML=`<div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(640px,92vw);max-height:85vh;display:flex;flex-direction:column;gap:12px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:600">\uD83D\uDCC2 备份历史 &mdash; ${_rmEsc(r?.name||routerId)}</span>
        <button onclick="this.closest('.rm-overlay').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px">&times;</button>
      </div>
      <div style="overflow-y:auto;max-height:60vh;display:flex;flex-direction:column;gap:14px">
        <div>
          <div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:6px">Clash 配置备份 (${bList.length}个)</div>
          ${backupRows}
        </div>
        <div>
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:6px">
            <span style="font-size:11px;font-weight:600;color:var(--text-muted)">代理轮换历史 (${hList.length}条)</span>
            ${blList.length?`<button class="sb-btn2" style="font-size:9px;padding:2px 8px;color:#ef4444" onclick="_rmClearBlacklist('${routerId}',this)">清除黑名单(${blList.length})</button>`:''}
          </div>
          ${histRows}
        </div>
      </div>
      <button class="qa-btn" onclick="this.closest('.rm-overlay').remove()">关闭</button>
    </div>`;
    document.body.appendChild(overlay);
  }catch(e){showToast('加载备份失败: '+e.message,'warn');}
}

async function _rmRestoreBackup(routerId, filename, btn){
  if(!(await ocDialog({title:'回滚配置',message:`确认从备份恢复配置？<br><code>${filename}</code><br><br>Clash 会重启，约 30 秒后生效。`,type:'warning',confirmText:'回滚',cancelText:'取消'})))return;
  if(btn){btn.disabled=true;btn.textContent='回滚中...';}
  try{
    const r=await api('POST',`/routers/${routerId}/restore`,{filename});
    showToast(r.ok?'\u2705 配置已回滚，Clash重启中...':'\u274C 回滚失败','ok');
  }catch(e){showToast('回滚失败: '+e.message,'warn');}
  finally{if(btn){btn.disabled=false;btn.textContent='回滚';}}
}

async function _rmClearBlacklist(routerId, btn){
  if(btn){btn.disabled=true;}
  try{
    await api('POST',`/routers/${routerId}/clear-blacklist`);
    showToast('黑名单已清除','ok');
    document.querySelector('.rm-overlay')?.remove();
  }catch(e){showToast('清除失败: '+e.message,'warn');}
  finally{if(btn){btn.disabled=false;}}
}

// ══ 代理账号健康评分面板 ══
async function _rmShowProxyScores(){
  try{
    const res=await api('GET','/proxy/scores');
    const scores=res.scores||[];
    const overlay=document.createElement('div');
    overlay.className='rm-overlay';
    overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center';

    const rows=scores.length
      ?scores.map(s=>{
          const pct=Math.round((s.score||0)*100);
          const color=pct>=80?'#22c55e':pct>=60?'#eab308':'#ef4444';
          return `<tr style="border-bottom:1px solid rgba(255,255,255,.04)">
            <td style="padding:6px"><div style="font-weight:600">${_rmEsc(s.label||s.proxy_id)}</div>
              <div style="color:var(--text-dim);font-family:monospace;font-size:9px">${_rmEsc(s.server||'')}</div></td>
            <td style="text-align:center;padding:6px"><span style="font-weight:700;color:${color}">${pct}%</span>
              <div style="width:40px;height:4px;background:rgba(255,255,255,.1);border-radius:2px;margin:2px auto 0">
                <div style="width:${pct}%;height:100%;background:${color};border-radius:2px"></div></div></td>
            <td style="text-align:center;padding:6px;color:var(--text-muted)">${s.success_rate!==null&&s.success_rate!==undefined?Math.round(s.success_rate*100)+'%':'N/A'}</td>
            <td style="text-align:center;padding:6px;color:var(--text-muted)">${s.success||0}\u2705 ${s.fail||0}\u274C</td>
            <td style="text-align:right;padding:6px;font-family:monospace">${s.avg_latency_ms?s.avg_latency_ms+'ms':'-'}</td>
            <td style="text-align:right;padding:6px;color:var(--text-dim)">${_rmEsc(s.last_test_str||'')}</td>
          </tr>`;
        }).join('')
      :'<tr><td colspan="6" style="text-align:center;padding:30px;color:var(--text-dim)">暂无评分数据（部署时自动记录）</td></tr>';

    overlay.innerHTML=`<div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(700px,92vw);max-height:85vh;display:flex;flex-direction:column;gap:12px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <span style="font-weight:600">\uD83D\uDCCA 代理账号健康评分</span>
        <button onclick="this.closest('.rm-overlay').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px">&times;</button>
      </div>
      <div style="font-size:10px;color:var(--text-dim)">评分 = 历史成功率 &times; 时效衰减系数 &middot; 未知账号默认75% &middot; 轮换时优先高分账号</div>
      <div style="overflow-y:auto;max-height:55vh">
        <table style="width:100%;border-collapse:collapse;font-size:10px">
          <thead><tr style="color:var(--text-muted);border-bottom:1px solid var(--border)">
            <th style="text-align:left;padding:6px">代理账号</th>
            <th style="text-align:center;padding:6px">综合评分</th>
            <th style="text-align:center;padding:6px">成功率</th>
            <th style="text-align:center;padding:6px">测试次数</th>
            <th style="text-align:right;padding:6px">平均延迟</th>
            <th style="text-align:right;padding:6px">最后测试</th>
          </tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      <button class="qa-btn" onclick="this.closest('.rm-overlay').remove()">关闭</button>
    </div>`;
    document.body.appendChild(overlay);
  }catch(e){showToast('加载评分失败: '+e.message,'warn');}
}
