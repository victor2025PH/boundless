/* ═══════════════════════════════════════════════════
   代理中心 — 路由器管理 + 代理账号 + 设备映射
   ═══════════════════════════════════════════════════ */

const _rmFlags={us:'🇺🇸',usa:'🇺🇸',italy:'🇮🇹',uk:'🇬🇧',germany:'🇩🇪',france:'🇫🇷',
  japan:'🇯🇵',korea:'🇰🇷',canada:'🇨🇦',australia:'🇦🇺',singapore:'🇸🇬',
  netherlands:'🇳🇱',spain:'🇪🇸',brazil:'🇧🇷',mexico:'🇲🇽',philippines:'🇵🇭'};
function _rmFlag(c){return _rmFlags[(c||'').toLowerCase()]||'🌐';}
function _rmEsc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}

let _rmData={routers:[],proxies:[]};
let _rmRefreshTimer=null;

// ══ 主入口 ══
async function loadRouterManagePage(){
  try{
    const [rData,pData,hData]=await Promise.all([
      api('GET','/routers'),
      api('GET','/vpn/pool'),
      api('GET','/proxy/health/summary').catch(()=>null),
    ]);
    _rmData={
      routers: rData.routers||[],
      proxies: (pData.configs||[]).filter(c=>['socks5','http','https'].includes(c.protocol)||c.proxy_mode==='router'),
      allProxies: pData.configs||[],
      health: hData||{total:0,ok:0,circuit_open:0,fail:0},
    };
    _rmRenderStats();
    _rmRenderRouters();
    _rmRenderProxies();
    _rmRenderDeviceMap();
    _rmRenderHealthBanner();
    if(_rmRefreshTimer) clearInterval(_rmRefreshTimer);
    _rmRefreshTimer=setInterval(_rmRefreshStatus,30000);
  }catch(e){
    showToast('加载代理中心失败: '+e.message,'warn');
  }
}

// ══ 健康状态横幅（统计卡下方）══
function _rmRenderHealthBanner(){
  const el=document.getElementById('rm-health-banner');
  if(!el)return;
  const h=_rmData.health||{};
  const total=h.total||0;
  if(!total){
    el.innerHTML='<div style="font-size:10px;color:var(--text-dim)">暂无健康监控数据（设备完成首次检测后显示）</div>';
    return;
  }
  const healthRate=h.health_rate||Math.round((h.ok||0)/total*100);
  const color=healthRate>=90?'#22c55e':healthRate>=70?'#eab308':'#ef4444';
  el.innerHTML=`<div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <span style="font-size:11px;color:var(--text-muted)">🛡️ 代理健康:</span>
    <span style="font-size:13px;font-weight:700;color:${color}">${healthRate}%</span>
    <span style="font-size:10px;color:#22c55e">✅ 正常: ${h.ok||0}</span>
    ${h.circuit_open?`<span style="font-size:10px;color:#ef4444">🔴 熔断: ${h.circuit_open}</span>`:''}
    ${h.fail?`<span style="font-size:10px;color:#eab308">⚠️ 失败: ${h.fail}</span>`:''}
    <button class="sb-btn2" style="font-size:9px;padding:2px 8px;margin-left:auto" onclick="_rmShowHealthPanel()">查看详情</button>
    <button class="sb-btn2" style="font-size:9px;padding:2px 8px;color:#8b5cf6" onclick="_rmGeoConfigAll(this)">批量地理配置</button>
    <button class="sb-btn2" style="font-size:9px;padding:2px 8px;color:#f59e0b" onclick="_rmShowProxyScores()">代理评分</button>
    <button class="sb-btn2" style="font-size:9px;padding:2px 8px;color:#06b6d4" onclick="_rmShowProxyPool()">代理池</button>
  </div>`;
}

// ══ 统计卡片 ══
function _rmRenderStats(){
  const rs=_rmData.routers;
  const ps=_rmData.proxies||[];
  const online=rs.filter(r=>r.online).length;
  const totalDevices=rs.reduce((s,r)=>s+(r.device_count||0),0);
  document.getElementById('rm-stat-routers').textContent=rs.length;
  document.getElementById('rm-stat-online').textContent=online;
  document.getElementById('rm-stat-proxies').textContent=ps.length;
  document.getElementById('rm-stat-devices').textContent=totalDevices;
}

// ══ 路由器卡片 ══
function _rmRenderRouters(){
  const el=document.getElementById('rm-router-grid');
  if(!el)return;
  const rs=_rmData.routers;
  if(!rs.length){
    el.innerHTML=`<div style="text-align:center;padding:40px;color:var(--text-muted)">
      <div style="font-size:32px">📡</div>
      <div style="margin-top:8px;font-size:12px">暂无路由器</div>
      <div style="font-size:10px;margin-top:4px;color:var(--text-dim)">点击右上角"添加路由器"开始配置</div>
    </div>`;
    return;
  }
  el.innerHTML=rs.map(r=>{
    const statusDot=r.online
      ?'<span style="width:8px;height:8px;border-radius:50%;background:#22c55e;display:inline-block"></span>'
      :'<span style="width:8px;height:8px;border-radius:50%;background:#ef4444;display:inline-block"></span>';
    const flag=_rmFlag(r.country);
    const exitIp=r.current_exit_ip?`<div style="font-size:10px;color:#22c55e;font-family:monospace;margin-top:2px">🌐 ${_rmEsc(r.current_exit_ip)}</div>`:'';
    const proxySummary=r.proxy_count?`${r.proxy_count}个代理`:'<span style="color:#ef4444">未分配代理</span>';
    const devSummary=r.device_count?`${r.device_count}台手机`:'未分配手机';
    return `<div style="background:var(--bg-card);border:1px solid ${r.online?'rgba(34,197,94,0.3)':'var(--border)'};border-radius:12px;padding:14px">
      <div style="display:flex;justify-content:space-between;align-items:flex-start">
        <div>
          <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
            ${statusDot}<span style="font-weight:600;font-size:13px">${_rmEsc(r.name)}</span>
            <span style="font-size:16px">${flag}</span>
          </div>
          <div style="font-size:10px;color:var(--text-dim);font-family:monospace">${_rmEsc(r.ip)}</div>
          ${exitIp}
        </div>
        <div style="display:flex;gap:4px">
          <button class="sb-btn2" style="font-size:9px;padding:2px 6px;color:#8b5cf6" onclick="_rmDeployRouter('${r.router_id}')" title="推送Clash配置">🚀</button>
          <button class="sb-btn2" style="font-size:9px;padding:2px 6px;color:#06b6d4" onclick="_rmCheckRouter('${r.router_id}')" title="检测状态">🔍</button>
          <button class="sb-btn2" style="font-size:9px;padding:2px 6px" onclick="_rmEditRouter('${r.router_id}')" title="编辑">✏️</button>
          <button class="sb-btn2" style="font-size:9px;padding:2px 6px;color:#ef4444" onclick="_rmDeleteRouter('${r.router_id}')" title="删除">×</button>
        </div>
      </div>
      <div style="display:flex;gap:10px;margin-top:10px;font-size:10px;color:var(--text-muted)">
        <span>📦 ${proxySummary}</span>
        <span>📱 ${devSummary}</span>
        <span>🌍 ${_rmEsc(r.city||r.country||'未设国家')}</span>
      </div>
      <div style="display:flex;gap:4px;margin-top:8px;flex-wrap:wrap">
        <button class="sb-btn2" style="font-size:9px;padding:2px 8px" onclick="_rmShowAssignProxy('${r.router_id}')">分配代理</button>
        <button class="sb-btn2" style="font-size:9px;padding:2px 8px" onclick="_rmShowAssignDevice('${r.router_id}')">分配手机</button>
        <button class="sb-btn2" style="font-size:9px;padding:2px 8px" onclick="_rmPreviewClash('${r.router_id}')">预览Clash</button>
        <button class="sb-btn2" style="font-size:9px;padding:2px 8px;color:#8b5cf6" onclick="_rmGeoConfigRouter('${r.router_id}')">🌍地理配置</button>
        <button class="sb-btn2" style="font-size:9px;padding:2px 8px;color:#f59e0b" onclick="_rmRotateProxy('${r.router_id}',this)" title="自动轮换到备用代理">🔄轮换代理</button>
        <button class="sb-btn2" style="font-size:9px;padding:2px 8px;color:#06b6d4" onclick="_rmShowBackups('${r.router_id}')" title="查看配置备份历史">📂备份</button>
      </div>
    </div>`;
  }).join('');
}

// ══ 代理账号列表 ══
function _rmRenderProxies(){
  const el=document.getElementById('rm-proxy-list');
  if(!el)return;
  const ps=_rmData.proxies||[];
  if(!ps.length){
    el.innerHTML=`<div style="text-align:center;padding:30px;color:var(--text-muted)">
      <div style="font-size:24px">🔑</div>
      <div style="font-size:11px;margin-top:6px">暂无代理账号</div>
      <div style="font-size:10px;color:var(--text-dim);margin-top:4px">点击"添加代理账号"录入<br>支持 922S5 / Proxy-Seller 等服务商</div>
    </div>`;
    return;
  }
  // 统计每个代理被哪个路由器使用
  const proxyUsage={};
  for(const r of (_rmData.routers||[])){
    for(const pid of (r.proxy_ids||[])){
      if(!proxyUsage[pid]) proxyUsage[pid]=[];
      proxyUsage[pid].push(r.name);
    }
  }
  el.innerHTML=ps.map(p=>{
    const flag=_rmFlag(p.country);
    const usage=proxyUsage[p.id];
    const usageBadge=usage
      ?`<span style="font-size:9px;background:rgba(34,197,94,.15);color:#22c55e;padding:1px 5px;border-radius:3px">已分配:${usage.join(',')}</span>`
      :`<span style="font-size:9px;background:rgba(234,179,8,.15);color:#eab308;padding:1px 5px;border-radius:3px">未分配</span>`;
    return `<div style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:6px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="display:flex;align-items:center;gap:6px">
          <span style="font-size:14px">${flag}</span>
          <span style="font-size:11px;font-weight:600">${_rmEsc(p.label||p.remark)}</span>
          ${usageBadge}
        </div>
        <button onclick="_rmDeleteProxy('${p.id}')" style="background:none;border:none;color:#ef4444;cursor:pointer;font-size:13px">×</button>
      </div>
      <div style="font-size:9px;color:var(--text-dim);font-family:monospace;margin-top:4px">
        ${_rmEsc(p.protocol)} → ${_rmEsc(p.server)}:${p.port}
        ${p.username?'· 用户:'+_rmEsc(p.username.slice(0,8))+'…':''}
        ${p.city?'· '+_rmEsc(p.city):''}
      </div>
    </div>`;
  }).join('');
}

// ══ 设备-路由器映射表 ══
function _rmRenderDeviceMap(){
  const el=document.getElementById('rm-device-map');
  if(!el)return;
  const rs=_rmData.routers||[];
  if(!rs.length){
    el.innerHTML='<div style="color:var(--text-dim);font-size:11px;text-align:center;padding:20px">请先添加路由器</div>';
    return;
  }
  let rows='';
  for(const r of rs){
    const devs=r.device_ids||[];
    const flag=_rmFlag(r.country);
    const statusColor=r.online?'#22c55e':'#ef4444';
    if(!devs.length){
      rows+=`<tr><td>${flag} ${_rmEsc(r.name)}</td><td style="color:var(--text-dim)">未分配</td>
        <td style="color:${statusColor}">${r.online?'在线':'离线'}</td>
        <td style="font-family:monospace;font-size:10px">${_rmEsc(r.current_exit_ip||'-')}</td></tr>`;
    } else {
      rows+=devs.map((did,i)=>`<tr>
        ${i===0?`<td rowspan="${devs.length}" style="border-right:1px solid var(--border)">${flag} ${_rmEsc(r.name)}</td>`:''}
        <td style="font-size:10px;font-family:monospace">${_rmEsc(did)}</td>
        ${i===0?`<td rowspan="${devs.length}" style="color:${statusColor}">${r.online?'在线':'离线'}</td>`:''}
        ${i===0?`<td rowspan="${devs.length}" style="font-family:monospace;font-size:10px">${_rmEsc(r.current_exit_ip||'-')}</td>`:''}
      </tr>`).join('');
    }
  }
  el.innerHTML=`<table style="width:100%;border-collapse:collapse;font-size:11px">
    <thead><tr style="color:var(--text-muted);border-bottom:1px solid var(--border)">
      <th style="text-align:left;padding:6px">路由器</th>
      <th style="text-align:left;padding:6px">手机ID</th>
      <th style="text-align:left;padding:6px">状态</th>
      <th style="text-align:left;padding:6px">出口IP</th>
    </tr></thead>
    <tbody style="color:var(--text-main)">${rows}</tbody>
  </table>`;
}

// ══ 操作函数 ══
async function _rmRefreshStatus(){
  try{
    const r=await api('GET','/routers/status-all');
    const statusMap={};
    for(const s of (r.routers||[])) statusMap[s.router_id]=s;
    for(let i=0;i<_rmData.routers.length;i++){
      const rid=_rmData.routers[i].router_id;
      if(statusMap[rid]){
        _rmData.routers[i].online=statusMap[rid].online;
        _rmData.routers[i].current_exit_ip=statusMap[rid].exit_ip||'';
      }
    }
    _rmRenderStats();
    _rmRenderRouters();
  }catch(e){ /* 静默失败 */ }
}

async function _rmCheckRouter(routerId){
  showToast('正在检测路由器状态...','info');
  try{
    const r=await api('GET',`/routers/${routerId}/status`);
    const msg=r.online
      ?`✅ ${r.name} 在线\n出口IP: ${r.exit_ip||'获取中'}`
      :`❌ ${r.name} 离线`;
    showToast(msg, r.online?'ok':'warn');
    await loadRouterManagePage();
  }catch(e){showToast('检测失败: '+e.message,'warn');}
}

async function _rmDeployRouter(routerId){
  showToast('正在推送 Clash 配置...','info');
  try{
    const r=await api('POST',`/routers/${routerId}/deploy`);
    showToast(r.pushed?`✅ 配置已推送到路由器 (${r.proxy_count}个代理)`:`⚠️ 配置生成成功，但推送失败（请手动上传）`,'ok');
    await loadRouterManagePage();
  }catch(e){showToast('部署失败: '+e.message,'warn');}
}

async function _rmDeployAll(){
  showToast('正在批量部署所有路由器...','info');
  try{
    const r=await api('POST','/routers/deploy-all');
    showToast(`部署完成：${r.success}/${r.total} 台路由器成功`,'ok');
    await loadRouterManagePage();
  }catch(e){showToast('批量部署失败: '+e.message,'warn');}
}

async function _rmPreviewClash(routerId){
  try{
    const r=await api('GET',`/routers/${routerId}/clash-config`);
    _rmShowClashModal(r.clash_yaml, routerId);
  }catch(e){showToast('获取配置失败: '+e.message,'warn');}
}

function _rmShowClashModal(yaml, routerId){
  const overlay=document.createElement('div');
  overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center';
  overlay.innerHTML=`<div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(700px,90vw);max-height:80vh;display:flex;flex-direction:column;gap:12px">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span style="font-weight:600">Clash 配置预览 — ${routerId}</span>
      <button onclick="this.closest('.rm-overlay').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px">×</button>
    </div>
    <textarea readonly style="flex:1;min-height:400px;font-family:monospace;font-size:11px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;padding:10px;color:var(--text-main);resize:vertical">${_rmEsc(yaml)}</textarea>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button class="qa-btn" onclick="navigator.clipboard.writeText(this.closest('.rm-overlay').querySelector('textarea').value);showToast('已复制','ok')">复制</button>
      <button class="qa-btn" onclick="this.closest('.rm-overlay').remove()">关闭</button>
    </div>
  </div>`;
  overlay.className='rm-overlay';
  overlay.querySelector('button:last-of-type').addEventListener('click',()=>overlay.remove());
  document.body.appendChild(overlay);
}

async function _rmDeleteRouter(routerId){
  if(!(await ocDialog({title:'删除路由器',message:'确认删除该路由器？',type:'danger',confirmText:'删除',dangerous:true}))) return;
  await api('DELETE',`/routers/${routerId}`);
  showToast('已删除','ok');
  await loadRouterManagePage();
}

async function _rmDeleteProxy(proxyId){
  if(!(await ocDialog({title:'删除代理',message:'确认删除该代理账号？',type:'danger',confirmText:'删除',dangerous:true}))) return;
  await api('DELETE',`/vpn/pool/${proxyId}`);
  showToast('已删除','ok');
  await loadRouterManagePage();
}

// ══ 添加路由器对话框 ══
function _rmShowAddRouter(){
  const html=`<div class="rm-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center">
    <div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(480px,90vw);display:flex;flex-direction:column;gap:12px">
      <div style="font-weight:600;font-size:14px">➕ 添加 GL.iNet 路由器</div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:12px">
        <div><label>路由器名称 *</label><input id="rm-add-name" class="form-input" placeholder="美国组A" style="width:100%;margin-top:4px"></div>
        <div><label>路由器 IP *</label><input id="rm-add-ip" class="form-input" placeholder="192.168.0.201" style="width:100%;margin-top:4px"></div>
        <div><label>管理密码</label><input id="rm-add-pass" class="form-input" type="password" placeholder="GL.iNet 登录密码" style="width:100%;margin-top:4px"></div>
        <div><label>管理端口</label><input id="rm-add-port" class="form-input" placeholder="80" value="80" style="width:100%;margin-top:4px"></div>
        <div><label>目标国家</label><select id="rm-add-country" class="form-input" style="width:100%;margin-top:4px">
          <option value="">选择国家...</option>
          <option value="us">🇺🇸 美国</option>
          <option value="italy">🇮🇹 意大利</option>
          <option value="uk">🇬🇧 英国</option>
          <option value="germany">🇩🇪 德国</option>
          <option value="france">🇫🇷 法国</option>
          <option value="japan">🇯🇵 日本</option>
          <option value="canada">🇨🇦 加拿大</option>
        </select></div>
        <div><label>目标城市</label><input id="rm-add-city" class="form-input" placeholder="New York" style="width:100%;margin-top:4px"></div>
      </div>
      <div style="font-size:12px"><label>备注 / 说明</label>
        <input id="rm-add-notes" class="form-input" placeholder="如：01-20号手机，美国IP，纽约节点" style="width:100%;margin-top:4px">
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="qa-btn" onclick="this.closest('.rm-overlay').remove()">取消</button>
        <button class="qa-btn" style="color:#22c55e" onclick="_rmSubmitAddRouter()">确认添加</button>
      </div>
    </div>
  </div>`;
  const el=document.createElement('div');
  el.innerHTML=html;
  document.body.appendChild(el.firstElementChild);
}

async function _rmSubmitAddRouter(){
  const body={
    name: document.getElementById('rm-add-name').value.trim(),
    ip:   document.getElementById('rm-add-ip').value.trim(),
    password: document.getElementById('rm-add-pass').value,
    port: parseInt(document.getElementById('rm-add-port').value)||80,
    country: document.getElementById('rm-add-country').value,
    city: document.getElementById('rm-add-city').value.trim(),
    notes: document.getElementById('rm-add-notes').value.trim(),
  };
  if(!body.name||!body.ip){showToast('请填写名称和IP','warn');return;}
  try{
    await api('POST','/routers',body);
    document.querySelector('.rm-overlay')?.remove();
    showToast('路由器添加成功','ok');
    await loadRouterManagePage();
  }catch(e){showToast('添加失败: '+e.message,'warn');}
}

// ══ 添加代理账号对话框 ══
function _rmShowAddProxy(){
  const html=`<div class="rm-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center">
    <div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(500px,90vw);display:flex;flex-direction:column;gap:12px">
      <div style="font-weight:600;font-size:14px">🔑 添加代理账号</div>
      <div style="font-size:10px;color:var(--text-dim);background:rgba(34,197,94,.08);border-radius:6px;padding:8px">
        💡 推荐服务商：<strong>922S5</strong>（922proxy.com）美国 · <strong>Proxy-Seller</strong>（proxy-seller.com）美国+意大利<br>
        选择"静态住宅ISP代理"，TikTok识别率最低，支持SOCKS5协议
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;font-size:12px">
        <div><label>代理类型</label><select id="rm-px-type" class="form-input" style="width:100%;margin-top:4px">
          <option value="socks5">SOCKS5（推荐）</option>
          <option value="http">HTTP</option>
        </select></div>
        <div><label>服务器地址 *</label><input id="rm-px-host" class="form-input" placeholder="us.proxy.922s5.com" style="width:100%;margin-top:4px"></div>
        <div><label>端口 *</label><input id="rm-px-port" class="form-input" placeholder="10001" style="width:100%;margin-top:4px"></div>
        <div><label>用户名</label><input id="rm-px-user" class="form-input" placeholder="用户名" style="width:100%;margin-top:4px"></div>
        <div><label>密码</label><input id="rm-px-pass" class="form-input" type="password" placeholder="密码" style="width:100%;margin-top:4px"></div>
        <div><label>目标国家</label><select id="rm-px-country" class="form-input" style="width:100%;margin-top:4px">
          <option value="us">🇺🇸 美国</option>
          <option value="italy">🇮🇹 意大利</option>
          <option value="uk">🇬🇧 英国</option>
          <option value="germany">🇩🇪 德国</option>
          <option value="france">🇫🇷 法国</option>
        </select></div>
        <div><label>城市</label><input id="rm-px-city" class="form-input" placeholder="New York" style="width:100%;margin-top:4px"></div>
      </div>
      <div style="font-size:12px"><label>备注标签</label>
        <input id="rm-px-label" class="form-input" placeholder="美国-纽约-01" style="width:100%;margin-top:4px">
      </div>
      <div style="font-size:10px;color:var(--text-dim)">
        💡 批量添加：点击"批量导入"可一次粘贴多行 host:port:user:pass
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="qa-btn" onclick="this.closest('.rm-overlay').remove()">取消</button>
        <button class="qa-btn" style="color:#8b5cf6" onclick="_rmShowBatchProxy()">批量导入</button>
        <button class="qa-btn" style="color:#22c55e" onclick="_rmSubmitAddProxy()">确认添加</button>
      </div>
    </div>
  </div>`;
  const el=document.createElement('div');
  el.innerHTML=html;
  document.body.appendChild(el.firstElementChild);
}

async function _rmSubmitAddProxy(){
  const body={
    type: document.getElementById('rm-px-type').value,
    host: document.getElementById('rm-px-host').value.trim(),
    port: parseInt(document.getElementById('rm-px-port').value)||1080,
    username: document.getElementById('rm-px-user').value.trim(),
    password: document.getElementById('rm-px-pass').value,
    country: document.getElementById('rm-px-country').value,
    city: document.getElementById('rm-px-city').value.trim(),
    label: document.getElementById('rm-px-label').value.trim(),
  };
  if(!body.host||!body.port){showToast('请填写服务器地址和端口','warn');return;}
  try{
    await api('POST','/vpn/pool/add-proxy',body);
    document.querySelector('.rm-overlay')?.remove();
    showToast('代理账号已添加','ok');
    await loadRouterManagePage();
  }catch(e){showToast('添加失败: '+e.message,'warn');}
}

// ══ 批量导入代理 ══
function _rmShowBatchProxy(){
  document.querySelector('.rm-overlay')?.remove();
  const html=`<div class="rm-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center">
    <div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(560px,90vw);display:flex;flex-direction:column;gap:12px">
      <div style="font-weight:600;font-size:14px">📋 批量导入代理账号</div>
      <div style="font-size:11px;color:var(--text-dim)">
        每行一个代理，格式：<code>host:port:username:password</code><br>
        例：<code>us.proxy.922s5.com:10001:user123:pass456</code>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;font-size:12px">
        <div><label>代理类型</label><select id="rm-batch-type" class="form-input" style="width:100%;margin-top:4px">
          <option value="socks5">SOCKS5</option><option value="http">HTTP</option>
        </select></div>
        <div><label>目标国家</label><select id="rm-batch-country" class="form-input" style="width:100%;margin-top:4px">
          <option value="us">🇺🇸 美国</option><option value="italy">🇮🇹 意大利</option>
          <option value="uk">🇬🇧 英国</option><option value="germany">🇩🇪 德国</option>
        </select></div>
        <div><label>标签前缀</label><input id="rm-batch-prefix" class="form-input" placeholder="美国-" style="width:100%;margin-top:4px"></div>
      </div>
      <textarea id="rm-batch-text" style="height:200px;font-family:monospace;font-size:11px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;padding:10px;color:var(--text-main);resize:vertical" placeholder="粘贴代理列表，每行一个&#10;us.proxy.922s5.com:10001:user1:pass1&#10;us.proxy.922s5.com:10002:user2:pass2"></textarea>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="qa-btn" onclick="this.closest('.rm-overlay').remove()">取消</button>
        <button class="qa-btn" style="color:#22c55e" onclick="_rmSubmitBatchProxy()">批量导入</button>
      </div>
    </div>
  </div>`;
  const el=document.createElement('div');
  el.innerHTML=html;
  document.body.appendChild(el.firstElementChild);
}

async function _rmSubmitBatchProxy(){
  const text=document.getElementById('rm-batch-text').value.trim();
  const type=document.getElementById('rm-batch-type').value;
  const country=document.getElementById('rm-batch-country').value;
  const prefix=document.getElementById('rm-batch-prefix').value.trim();
  if(!text){showToast('请输入代理列表','warn');return;}
  const lines=text.split('\n').map(l=>l.trim()).filter(Boolean);
  let ok=0, fail=0;
  for(let i=0;i<lines.length;i++){
    const parts=lines[i].split(':');
    if(parts.length<2) {fail++;continue;}
    const [host,port,...rest]=parts;
    const username=rest[0]||'';
    const password=rest.slice(1).join(':')||'';
    try{
      await api('POST','/vpn/pool/add-proxy',{
        type,host,port:parseInt(port),username,password,country,
        label:`${prefix}${i+1}`.trim()||`${host}:${port}`,
      });
      ok++;
    }catch(e){fail++;}
  }
  document.querySelector('.rm-overlay')?.remove();
  showToast(`批量导入完成：${ok} 成功，${fail} 失败`,'ok');
  await loadRouterManagePage();
}

// ══ 分配代理到路由器 ══
function _rmShowAssignProxy(routerId){
  const router=_rmData.routers.find(r=>r.router_id===routerId);
  const proxies=_rmData.proxies||[];
  if(!proxies.length){showToast('请先添加代理账号','warn');return;}
  const currentIds=new Set(router?.proxy_ids||[]);
  const html=`<div class="rm-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center">
    <div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(440px,90vw);display:flex;flex-direction:column;gap:12px">
      <div style="font-weight:600;font-size:14px">📦 分配代理账号 → ${_rmEsc(router?.name||routerId)}</div>
      <div style="max-height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:6px">
        ${proxies.map(p=>`<label style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:8px;background:var(--bg-main);border-radius:6px;font-size:11px">
          <input type="checkbox" value="${p.id}" ${currentIds.has(p.id)?'checked':''}>
          <span>${_rmFlag(p.country)} ${_rmEsc(p.label||p.remark)}</span>
          <span style="color:var(--text-dim);font-family:monospace;font-size:9px">${_rmEsc(p.server)}:${p.port}</span>
        </label>`).join('')}
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="qa-btn" onclick="this.closest('.rm-overlay').remove()">取消</button>
        <button class="qa-btn" style="color:#22c55e" onclick="_rmSubmitAssignProxy('${routerId}')">确认分配</button>
      </div>
    </div>
  </div>`;
  const el=document.createElement('div');
  el.innerHTML=html;
  document.body.appendChild(el.firstElementChild);
}

async function _rmSubmitAssignProxy(routerId){
  const checks=document.querySelectorAll('.rm-overlay input[type=checkbox]:checked');
  const ids=Array.from(checks).map(c=>c.value);
  await api('POST',`/routers/${routerId}/assign-proxy`,{proxy_ids:ids});
  document.querySelector('.rm-overlay')?.remove();
  showToast(`已分配 ${ids.length} 个代理账号`,'ok');
  await loadRouterManagePage();
}

// ══ 分配设备到路由器 ══
async function _rmShowAssignDevice(routerId){
  const router=_rmData.routers.find(r=>r.router_id===routerId);
  let devices=[];
  try{const d=await api('GET','/devices');devices=d.devices||d||[];}catch(e){}
  const currentIds=new Set(router?.device_ids||[]);
  const html=`<div class="rm-overlay" style="position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center">
    <div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(440px,90vw);display:flex;flex-direction:column;gap:12px">
      <div style="font-weight:600;font-size:14px">📱 分配手机 → ${_rmEsc(router?.name||routerId)}</div>
      <div style="max-height:300px;overflow-y:auto;display:flex;flex-direction:column;gap:4px">
        ${devices.length?devices.map(d=>{
          const did=d.device_id||d.id||'';
          const alias=d.alias||d.name||did;
          return `<label style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:6px 8px;background:var(--bg-main);border-radius:6px;font-size:11px">
            <input type="checkbox" value="${did}" ${currentIds.has(did)?'checked':''}>
            <span>${_rmEsc(alias)}</span>
            <span style="color:var(--text-dim);font-size:9px">${_rmEsc(did.slice(0,12))}</span>
          </label>`;
        }).join(''):'<div style="color:var(--text-dim);text-align:center;padding:20px">暂无设备数据</div>'}
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="qa-btn" onclick="this.closest('.rm-overlay').remove()">取消</button>
        <button class="qa-btn" style="color:#22c55e" onclick="_rmSubmitAssignDevice('${routerId}')">确认分配</button>
      </div>
    </div>
  </div>`;
  const el=document.createElement('div');
  el.innerHTML=html;
  document.body.appendChild(el.firstElementChild);
}

async function _rmSubmitAssignDevice(routerId){
  const checks=document.querySelectorAll('.rm-overlay input[type=checkbox]:checked');
  const ids=Array.from(checks).map(c=>c.value);
  await api('POST',`/routers/${routerId}/assign-device`,{device_ids:ids});
  document.querySelector('.rm-overlay')?.remove();
  showToast(`已分配 ${ids.length} 台手机`,'ok');
  await loadRouterManagePage();
}

// ══ 代理健康监控面板 ══
async function _rmShowHealthPanel(){
  showToast('正在获取健康数据...','info');
  let data;
  try{data=await api('GET','/proxy/health');}catch(e){showToast('获取失败: '+e.message,'warn');return;}

  const devices=data.devices||[];
  const rows=devices.map(d=>{
    const matchIcon=d.ip_match?'✅':'❌';
    const cbIcon=d.circuit_open?'🔴熔断':'🟢正常';
    const failBadge=d.consecutive_fails>0?`<span style="font-size:9px;color:#ef4444">(失败${d.consecutive_fails}次)</span>`:'';
    return `<tr style="border-bottom:1px solid var(--border)">
      <td style="padding:6px;font-family:monospace;font-size:10px">${_rmEsc(d.device_id.slice(0,12))}</td>
      <td style="padding:6px;font-family:monospace;font-size:10px">${_rmEsc(d.expected_ip||'-')}</td>
      <td style="padding:6px;font-family:monospace;font-size:10px">${_rmEsc(d.actual_ip||'-')}</td>
      <td style="padding:6px;font-size:11px">${matchIcon}</td>
      <td style="padding:6px;font-size:11px">${cbIcon} ${failBadge}</td>
      <td style="padding:6px">
        <button class="sb-btn2" style="font-size:9px;padding:2px 6px" onclick="_rmCheckDeviceHealth('${d.device_id}',this)">检测</button>
        ${d.circuit_open?`<button class="sb-btn2" style="font-size:9px;padding:2px 6px;color:#22c55e" onclick="_rmResetCircuit('${d.device_id}',this)">重置熔断</button>`:''}
      </td>
    </tr>`;
  }).join('');

  const summary=`正常: ${data.ok} / 熔断: ${data.circuit_open} / 失败: ${data.fail} / 总计: ${data.total}`;
  const overlay=document.createElement('div');
  overlay.className='rm-overlay';
  overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:9000;display:flex;align-items:center;justify-content:center';
  overlay.innerHTML=`<div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(800px,95vw);max-height:85vh;display:flex;flex-direction:column;gap:12px">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <div>
        <span style="font-weight:600;font-size:14px">🛡️ 代理健康监控</span>
        <span style="font-size:10px;color:var(--text-dim);margin-left:10px">${summary}</span>
      </div>
      <div style="display:flex;gap:6px">
        <button class="qa-btn" style="font-size:11px" onclick="_rmCheckAllHealth(this)">批量检测</button>
        <button class="qa-btn" style="font-size:11px;color:#8b5cf6" onclick="_rmGeoConfigAll(this)">批量地理配置</button>
        <button onclick="this.closest('.rm-overlay').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px">×</button>
      </div>
    </div>
    ${devices.length?`
    <div style="overflow-y:auto;flex:1">
      <table style="width:100%;border-collapse:collapse;font-size:11px">
        <thead><tr style="color:var(--text-muted);border-bottom:2px solid var(--border)">
          <th style="text-align:left;padding:6px">设备ID</th>
          <th style="text-align:left;padding:6px">期望IP</th>
          <th style="text-align:left;padding:6px">实际IP</th>
          <th style="text-align:left;padding:6px">匹配</th>
          <th style="text-align:left;padding:6px">熔断状态</th>
          <th style="text-align:left;padding:6px">操作</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>`:`<div style="text-align:center;padding:40px;color:var(--text-dim)">
      <div style="font-size:32px">📡</div>
      <div style="margin-top:8px">暂无监控数据</div>
      <div style="font-size:10px;margin-top:4px">请先分配手机到路由器，系统将自动开始监控</div>
    </div>`}
    <div style="font-size:10px;color:var(--text-dim);border-top:1px solid var(--border);padding-top:8px">
      💡 监控间隔: 5分钟 · 连续3次IP不匹配 → 触发熔断 → 自动停止该设备TikTok任务 + Telegram告警
    </div>
  </div>`;
  document.body.appendChild(overlay);
}

async function _rmCheckDeviceHealth(deviceId, btn){
  if(btn){btn.disabled=true;btn.textContent='检测中...';}
  try{
    const r=await api('POST',`/proxy/health/${deviceId}/check`);
    if(btn){
      btn.disabled=false;btn.textContent='检测';
      const row=btn.closest('tr');
      if(row){
        row.cells[2].textContent=r.actual_ip||'-';
        row.cells[3].textContent=r.ip_match?'✅':'❌';
        row.cells[4].textContent=r.circuit_open?'🔴熔断':'🟢正常';
      }
    }
    showToast(r.ok?`✅ ${deviceId.slice(0,8)} IP正常: ${r.actual_ip}`:`⚠️ IP异常: 期望 ${r.expected_ip} 实际 ${r.actual_ip||'无法获取'}`
      , r.ok?'ok':'warn');
  }catch(e){
    if(btn){btn.disabled=false;btn.textContent='检测';}
    showToast('检测失败: '+e.message,'warn');
  }
}

async function _rmCheckAllHealth(btn){
  if(btn){btn.disabled=true;btn.textContent='检测中...';}
  try{
    const r=await api('POST','/proxy/health/check-all');
    showToast(`已启动批量检测: ${r.device_count}台设备，结果将在5-30秒后更新`,'ok');
    setTimeout(()=>_rmShowHealthPanel(),15000);
  }catch(e){showToast('批量检测失败: '+e.message,'warn');}
  finally{if(btn){btn.disabled=false;btn.textContent='批量检测';}}
}

async function _rmResetCircuit(deviceId, btn){
  if(btn){btn.disabled=true;}
  try{
    await api('POST',`/proxy/circuit/${deviceId}/reset`);
    showToast(`✅ ${deviceId.slice(0,8)} 熔断已重置`,'ok');
    if(btn){
      const row=btn.closest('tr');
      if(row) row.cells[4].textContent='🟢正常';
      btn.remove();
    }
  }catch(e){
    if(btn){btn.disabled=false;}
    showToast('重置失败: '+e.message,'warn');
  }
}

// ══ 批量地理配置（GPS/时区/语言） ══
async function _rmGeoConfigAll(btn){
  if(!(await ocDialog({title:'批量地理配置',message:'将根据每台手机绑定路由器的目标国家，自动配置所有手机的 GPS 位置、时区和语言。',type:'info',confirmText:'执行',cancelText:'取消'})))return;
  if(btn){btn.disabled=true;btn.textContent='配置中...';}
  try{
    const r=await api('POST','/proxy/geo-all',{});
    showToast(`地理配置完成：${r.success}/${r.total} 台成功`+(r.failed>0?` (${r.failed}台失败)`:''),(r.failed>0?'warn':'ok'));
  }catch(e){showToast('批量地理配置失败: '+e.message,'warn');}
  finally{if(btn){btn.disabled=false;btn.textContent='批量地理配置';}}
}

async function _rmGeoConfigRouter(routerId){
  const r=_rmData.routers.find(x=>x.router_id===routerId);
  if(!r){showToast('路由器不存在','warn');return;}
  if(!r.country){showToast('该路由器未设置国家，请先编辑路由器','warn');return;}
  const devs=r.device_ids||[];
  if(!devs.length){showToast('该路由器未分配手机','warn');return;}
  if(!(await ocDialog({title:'地理配置',message:`将为路由器「${r.name}」下 <b>${devs.length}</b> 台手机配置 ${r.country.toUpperCase()} 地理信息<br>（GPS位置、时区、系统语言）`,type:'info',confirmText:'执行',cancelText:'取消'})))return;
  showToast(`正在配置 ${devs.length} 台手机地理信息...`,'info');
  try{
    const res=await api('POST','/proxy/geo-all',{device_ids:devs});
    showToast(`配置完成：${res.success}/${res.total} 台成功`,'ok');
  }catch(e){showToast('地理配置失败: '+e.message,'warn');}
}

// ══ 编辑路由器（简化版） ══
function _rmEditRouter(routerId){
  const r=_rmData.routers.find(x=>x.router_id===routerId);
  if(!r)return;
  // 复用添加对话框逻辑，预填数据
  _rmShowAddRouter();
  setTimeout(()=>{
    document.getElementById('rm-add-name').value=r.name;
    document.getElementById('rm-add-ip').value=r.ip;
    document.getElementById('rm-add-port').value=r.port;
    document.getElementById('rm-add-country').value=r.country||'';
    document.getElementById('rm-add-city').value=r.city||'';
    document.getElementById('rm-add-notes').value=r.notes||'';
    // 改提交函数为更新
    const btn=document.querySelector('.rm-overlay .qa-btn:last-child');
    if(btn){
      btn.textContent='确认更新';
      btn.onclick=async()=>{
        const body={
          name:document.getElementById('rm-add-name').value.trim(),
          ip:document.getElementById('rm-add-ip').value.trim(),
          password:document.getElementById('rm-add-pass').value,
          port:parseInt(document.getElementById('rm-add-port').value)||80,
          country:document.getElementById('rm-add-country').value,
          city:document.getElementById('rm-add-city').value.trim(),
          notes:document.getElementById('rm-add-notes').value.trim(),
        };
        await api('PUT',`/routers/${routerId}`,body);
        document.querySelector('.rm-overlay')?.remove();
        showToast('已更新','ok');
        await loadRouterManagePage();
      };
    }
  },50);
}
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

// ══ 代理池管理面板 ══
async function _rmShowProxyPool(){
  // 移除已有弹窗
  document.querySelectorAll('.rm-overlay').forEach(e=>e.remove());

  // 创建遮罩
  const overlay=document.createElement('div');
  overlay.className='rm-overlay';
  overlay.style.cssText='position:fixed;inset:0;background:rgba(0,0,0,.6);z-index:9999;display:flex;align-items:center;justify-content:center';
  overlay.innerHTML=`<div style="background:var(--bg-card);border-radius:12px;padding:20px;width:min(780px,94vw);max-height:88vh;display:flex;flex-direction:column;gap:12px">
    <div style="display:flex;justify-content:space-between;align-items:center">
      <span style="font-weight:600;font-size:14px">🌐 代理池管理</span>
      <button onclick="this.closest('.rm-overlay').remove()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:18px">&times;</button>
    </div>
    <div id="_rmpp-body" style="overflow-y:auto;flex:1;display:flex;flex-direction:column;gap:10px">
      <div style="text-align:center;padding:30px;color:var(--text-dim);font-size:12px">加载中...</div>
    </div>
  </div>`;
  document.body.appendChild(overlay);

  const body=overlay.querySelector('#_rmpp-body');

  // 渲染列表视图
  async function renderList(){
    body.innerHTML='<div style="text-align:center;padding:20px;color:var(--text-dim);font-size:12px">加载列表中...</div>';
    try{
      const r=await api('GET','/proxy/pool/list');
      const proxies=r.proxies||[];
      const flag=c=>(c||'').toUpperCase().replace(/./g,ch=>String.fromCodePoint(ch.charCodeAt(0)+127397));
      const rows=proxies.length
        ?proxies.map(p=>{
          const expColor=p.active?'#22c55e':'#ef4444';
          const expTxt=p.expires_at?new Date(p.expires_at).toLocaleString('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}):'永久';
          const srcBadge=p.source==='922s5'
            ?'<span style="background:#0e7490;color:#fff;border-radius:3px;padding:1px 5px;font-size:9px">922S5</span>'
            :'<span style="background:#4b5563;color:#fff;border-radius:3px;padding:1px 5px;font-size:9px">手动</span>';
          return `<tr style="border-bottom:1px solid var(--border)">
            <td style="padding:5px 6px;font-family:monospace;font-size:10px;color:var(--text-dim)">${p.id||'-'}</td>
            <td style="padding:5px 6px;font-family:monospace;font-size:10px">${_rmEsc(p.server||'')}:${p.port||''}</td>
            <td style="padding:5px 6px;text-align:center;font-size:13px">${flag(p.country)} <span style="font-size:9px;color:var(--text-muted)">${(p.country||'??').toUpperCase()}</span></td>
            <td style="padding:5px 6px;text-align:center">${srcBadge}</td>
            <td style="padding:5px 6px;text-align:center;font-size:10px;color:${expColor}">${p.active?'✅ 活跃':'❌ 停用'}</td>
            <td style="padding:5px 6px;font-size:9px;color:var(--text-dim)">${expTxt}</td>
          </tr>`;
        }).join('')
        :'<tr><td colspan="6" style="text-align:center;padding:24px;color:var(--text-dim)">代理池暂无数据，请先同步922S5</td></tr>';
      body.innerHTML=`
        <div style="display:flex;justify-content:space-between;align-items:center">
          <span style="font-size:11px;color:var(--text-muted)">共 ${r.total||0} 条 &nbsp;·&nbsp; 活跃 <span style="color:#22c55e">${r.ok||0}</span></span>
          <button class="sb-btn2" style="font-size:9px;padding:2px 8px" onclick="_rmShowProxyPool()">← 返回概览</button>
        </div>
        <div style="overflow-y:auto;max-height:55vh">
          <table style="width:100%;border-collapse:collapse;font-size:10px">
            <thead><tr style="color:var(--text-muted);border-bottom:1px solid var(--border)">
              <th style="text-align:left;padding:5px 6px">ID</th>
              <th style="text-align:left;padding:5px 6px">服务器:端口</th>
              <th style="text-align:center;padding:5px 6px">国家</th>
              <th style="text-align:center;padding:5px 6px">来源</th>
              <th style="text-align:center;padding:5px 6px">状态</th>
              <th style="text-align:left;padding:5px 6px">到期时间</th>
            </tr></thead>
            <tbody>${rows}</tbody>
          </table>
        </div>`;
    }catch(e){
      body.innerHTML=`<div style="color:#ef4444;font-size:11px;padding:10px">加载列表失败: ${_rmEsc(e.message)}</div>`;
    }
  }

  // 渲染概览
  try{
    const [stats, s5]=await Promise.all([
      api('GET','/proxy/pool/stats'),
      api('GET','/proxy/922s5/status').catch(()=>null)
    ]);

    const byCountry=stats.by_country||{};
    const countryColors=['#06b6d4','#8b5cf6','#f59e0b','#22c55e','#ef4444','#ec4899','#14b8a6','#f97316'];
    const flag=c=>(c||'').toUpperCase().replace(/./g,ch=>String.fromCodePoint(ch.charCodeAt(0)+127397));
    const countryBadges=Object.entries(byCountry).sort((a,b)=>b[1]-a[1]).map(([c,n],i)=>
      `<span style="display:inline-flex;align-items:center;gap:3px;background:${countryColors[i%countryColors.length]}22;border:1px solid ${countryColors[i%countryColors.length]}55;border-radius:12px;padding:2px 8px;font-size:10px">
        ${flag(c)} ${c.toUpperCase()} <b>${n}</b>
      </span>`
    ).join(' ');

    const s5Html=s5&&s5.configured
      ?`<span style="font-size:10px;color:#06b6d4">💳 922S5 余额: <b>${s5.balance!=null?s5.balance:'--'}</b></span>
         <span style="font-size:10px;color:var(--text-dim)">· 池: ${s5.pool_count!=null?s5.pool_count:'--'}</span>`
      :'<span style="font-size:10px;color:var(--text-dim)">922S5 未配置</span>';

    const needsAttn=stats.needs_attention||0;

    body.innerHTML=`
      <!-- 统计行 -->
      <div style="display:flex;align-items:center;gap:16px;flex-wrap:wrap;padding:10px 12px;background:var(--bg-main,#111);border-radius:8px;border:1px solid var(--border)">
        <div style="display:flex;flex-direction:column;align-items:center;gap:2px">
          <span style="font-size:18px;font-weight:700;color:#06b6d4">${stats.total||0}</span>
          <span style="font-size:9px;color:var(--text-dim)">总代理</span>
        </div>
        <div style="display:flex;flex-direction:column;align-items:center;gap:2px">
          <span style="font-size:18px;font-weight:700;color:#22c55e">${stats.active||0}</span>
          <span style="font-size:9px;color:var(--text-dim)">活跃</span>
        </div>
        <div style="display:flex;flex-direction:column;align-items:center;gap:2px">
          <span style="font-size:18px;font-weight:700;color:#ef4444">${stats.expired||0}</span>
          <span style="font-size:9px;color:var(--text-dim)">已过期</span>
        </div>
        ${needsAttn?`<div style="display:flex;flex-direction:column;align-items:center;gap:2px">
          <span style="font-size:18px;font-weight:700;color:#f59e0b">${needsAttn}</span>
          <span style="font-size:9px;color:var(--text-dim)">需关注</span>
        </div>`:''}
        <div style="margin-left:auto;display:flex;flex-direction:column;gap:2px;align-items:flex-end">
          ${s5Html}
        </div>
      </div>

      <!-- 国家分布 -->
      ${Object.keys(byCountry).length?`
      <div style="padding:8px 12px;background:var(--bg-main,#111);border-radius:8px;border:1px solid var(--border)">
        <div style="font-size:9px;color:var(--text-dim);margin-bottom:6px">国家分布</div>
        <div style="display:flex;flex-wrap:wrap;gap:6px">${countryBadges||'<span style="color:var(--text-dim);font-size:10px">暂无数据</span>'}</div>
      </div>`:''}

      <!-- 操作按钮 -->
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="sb-btn2" id="_rmpp-sync" style="font-size:10px;padding:4px 14px;color:#06b6d4" onclick="
          const btn=this;btn.disabled=true;btn.textContent='同步中...';
          api('POST','/proxy/pool/sync').then(r=>{
            showToast((r&&r.added!=null?'同步完成，新增 '+r.added+' 条':'同步已触发'),'ok');
            btn.closest('.rm-overlay').remove();_rmShowProxyPool();
          }).catch(e=>{showToast('同步失败: '+e.message,'warn');btn.disabled=false;btn.textContent='同步922S5';})
        ">同步922S5</button>
        <button class="sb-btn2" id="_rmpp-cleanup" style="font-size:10px;padding:4px 14px;color:#f59e0b" onclick="
          const btn=this;btn.disabled=true;btn.textContent='清理中...';
          api('POST','/proxy/pool/cleanup').then(r=>{
            showToast((r&&r.removed!=null?'清理完成，移除 '+r.removed+' 条':'清理完成'),'ok');
            btn.closest('.rm-overlay').remove();_rmShowProxyPool();
          }).catch(e=>{showToast('清理失败: '+e.message,'warn');btn.disabled=false;btn.textContent='清理过期';})
        ">清理过期</button>
        <button class="sb-btn2" style="font-size:10px;padding:4px 14px;color:#8b5cf6" id="_rmpp-list-btn">查看列表</button>
      </div>

      <!-- 手动添加表单 -->
      <div style="padding:10px 12px;background:var(--bg-main,#111);border-radius:8px;border:1px solid var(--border)">
        <div style="font-size:10px;color:var(--text-muted);margin-bottom:8px;font-weight:600">手动添加代理</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(120px,1fr));gap:6px" id="_rmpp-add-form">
          <input id="_rmpp-server" placeholder="服务器地址" style="background:var(--bg-main,#111);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;color:var(--text-main,#eee);width:100%;box-sizing:border-box">
          <input id="_rmpp-port" placeholder="端口" type="number" style="background:var(--bg-main,#111);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;color:var(--text-main,#eee);width:100%;box-sizing:border-box">
          <input id="_rmpp-user" placeholder="用户名" style="background:var(--bg-main,#111);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;color:var(--text-main,#eee);width:100%;box-sizing:border-box">
          <input id="_rmpp-pass" placeholder="密码" type="password" style="background:var(--bg-main,#111);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;color:var(--text-main,#eee);width:100%;box-sizing:border-box">
          <input id="_rmpp-country" placeholder="国家代码 (如 US)" maxlength="2" style="background:var(--bg-main,#111);border:1px solid var(--border);border-radius:4px;padding:4px 8px;font-size:10px;color:var(--text-main,#eee);width:100%;box-sizing:border-box;text-transform:uppercase">
        </div>
        <div style="margin-top:8px;display:flex;align-items:center;gap:8px">
          <button class="sb-btn2" style="font-size:10px;padding:4px 14px;color:#22c55e" id="_rmpp-add-btn">添加代理</button>
          <span id="_rmpp-add-msg" style="font-size:10px;color:var(--text-dim)"></span>
        </div>
      </div>`;

    // 绑定查看列表按钮
    overlay.querySelector('#_rmpp-list-btn').addEventListener('click', renderList);

    // 绑定手动添加按钮
    overlay.querySelector('#_rmpp-add-btn').addEventListener('click', async()=>{
      const server=(overlay.querySelector('#_rmpp-server').value||'').trim();
      const port=parseInt(overlay.querySelector('#_rmpp-port').value)||0;
      const username=(overlay.querySelector('#_rmpp-user').value||'').trim();
      const password=(overlay.querySelector('#_rmpp-pass').value||'').trim();
      const country=(overlay.querySelector('#_rmpp-country').value||'').trim().toUpperCase();
      const msg=overlay.querySelector('#_rmpp-add-msg');
      if(!server||!port){msg.style.color='#ef4444';msg.textContent='服务器和端口必填';return;}
      const btn=overlay.querySelector('#_rmpp-add-btn');
      btn.disabled=true;btn.textContent='添加中...';msg.textContent='';
      try{
        await api('POST','/proxy/pool/add',{server,port,username,password,country,source:'manual'});
        msg.style.color='#22c55e';msg.textContent='添加成功';
        ['#_rmpp-server','#_rmpp-port','#_rmpp-user','#_rmpp-pass','#_rmpp-country'].forEach(s=>{overlay.querySelector(s).value='';});
        setTimeout(()=>{overlay.remove();_rmShowProxyPool();},800);
      }catch(e){
        if(e.message&&(e.message.includes('404')||e.message.includes('not found')||e.message.includes('Not Found'))){
          msg.style.color='#f59e0b';msg.textContent='请通过922S5同步添加';
        }else{
          msg.style.color='#ef4444';msg.textContent='添加失败: '+e.message;
        }
        btn.disabled=false;btn.textContent='添加代理';
      }
    });

  }catch(e){
    body.innerHTML=`<div style="color:#ef4444;font-size:11px;padding:10px">加载代理池失败: ${_rmEsc(e.message)}</div>`;
  }
}
