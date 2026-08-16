/* core.js — 基础设施: API代理、i18n、Auth Guard、User Management、Sidebar、Toast、Theme */
/* ── 反代 / 分离端口：localhost 上常见前端端口 3000 / 5173 / 4173 会自动把 API/WS 指到同主机 :8000。
   其它环境可手动: localStorage.setItem('oc_api_origin','http://IP:8000'); 或 oc_ws_root=ws://IP:8000 */
function _apiOrigin(){
  try{
    const s=(localStorage.getItem('oc_api_origin')||'').trim().replace(/\/$/,'');
    if(s) return s;
  }catch(e){}
  const h=location.hostname,p=location.port;
  const devFront=['3000','5173','4173'];
  if((h==='localhost'||h==='127.0.0.1')&&devFront.indexOf(p)>=0){
    return 'http://'+h+':8000';
  }
  return '';
}
function _apiUrl(path){
  if(!path)return path;
  if(path.indexOf('http://')===0||path.indexOf('https://')===0)return path;
  const b=_apiOrigin();
  return b?b+path:path;
}
function _wsUrl(path){
  if(!path||path.charAt(0)!=='/')path='/'+(path||'');
  let wr='';
  try{wr=(localStorage.getItem('oc_ws_root')||'').trim();}catch(e){}
  if(wr){const r=wr.replace(/\/$/,'');return r+path;}
  const b=_apiOrigin();
  if(b){try{const u=new URL(b);const pr=u.protocol==='https:'?'wss:':'ws:';return pr+'//'+u.host+path;}catch(e){}}
  const proto=location.protocol==='https:'?'wss:':'ws:';
  return proto+'//'+location.host+path;
}

/* ── P0-1 连接状态机（2026-08-17）──
   后端开发期高频重启（28h 内 16 次实锤），此前每次重启：WS 掉线只改头部小字、
   每个失败的 fetch 各弹一条英文红 toast（5s 轮询=5s 一条）。这里把「与主控的连接」
   收敛成一个状态机：连续 2 次网络级失败（或 WS 已断 + 1 次失败）→ offline，
   页顶出常驻横幅（重连次数 + 数据快照时间 + 立即重试），并以 5s /health 探针自愈；
   恢复 → 绿横幅 3s + 自动刷新当前页数据。网络级失败的 toast 由横幅接管
   （api() 抛 isNetwork 标记，调用方据此免弹；toast 全部走 'conn' 组去重）。 */
window.OCConn=(function(){
  let state='online';          // online | offline
  let failStreak=0;            // 连续网络级失败次数（也用作横幅里的重连计数）
  let wsIsDown=false;
  let lastOkAt=Date.now();     // 最后一次拿到后端响应（任何状态码都算活着）
  let probeTimer=null;
  let banner=null,bannerText=null,recoverHideTimer=null;

  function _fmt(ts){const d=new Date(ts);const p=n=>String(n).padStart(2,'0');return p(d.getHours())+':'+p(d.getMinutes());}
  function _ensureBanner(){
    if(banner||!document.body)return;
    banner=document.createElement('div');
    banner.id='oc-conn-banner';
    banner.setAttribute('role','status');
    banner.setAttribute('aria-live','polite');
    banner.style.cssText='position:fixed;top:10px;left:50%;transform:translateX(-50%);z-index:99999;display:none;align-items:center;gap:10px;padding:8px 16px;border-radius:10px;font-size:12px;line-height:1.5;max-width:min(92vw,700px);border:1px solid var(--amber,#f59e0b);background:var(--bg-card,#151c29);color:var(--amber,#f59e0b)';
    bannerText=document.createElement('span');
    const retry=document.createElement('button');
    retry.textContent='立即重试';
    retry.style.cssText='border:1px solid currentColor;background:transparent;color:inherit;border-radius:6px;padding:2px 10px;font-size:11px;cursor:pointer;flex:none';
    retry.onclick=function(){probe();};
    banner.appendChild(bannerText);banner.appendChild(retry);
    document.body.appendChild(banner);
  }
  function _render(){
    if(state!=='offline')return;
    _ensureBanner();if(!banner)return;
    banner.style.display='flex';
    banner.style.borderColor='var(--amber,#f59e0b)';
    banner.style.color='var(--amber,#f59e0b)';
    bannerText.textContent='与主控失联，正在自动重连（第 '+failStreak+' 次）· 页面数据为 '+_fmt(lastOkAt)+' 快照';
  }
  function _setOffline(){
    const first=state!=='offline';
    state='offline';
    _render();
    if(first){
      if(typeof showToast==='function')showToast('无法连接主控服务，已启动自动重连','warn',6000,'conn');
      if(!probeTimer)probeTimer=setInterval(probe,5000);
    }
  }
  function _setOnline(){
    failStreak=0;lastOkAt=Date.now();
    if(state!=='offline')return;
    state='online';
    if(probeTimer){clearInterval(probeTimer);probeTimer=null;}
    _ensureBanner();
    if(banner){
      banner.style.display='flex';
      banner.style.borderColor='var(--green,#34d399)';
      banner.style.color='var(--green,#34d399)';
      bannerText.textContent='已恢复与主控的连接 · 正在刷新数据…';
      if(recoverHideTimer)clearTimeout(recoverHideTimer);
      recoverHideTimer=setTimeout(function(){if(state==='online'&&banner)banner.style.display='none';},3000);
    }
    if(typeof showToast==='function')showToast('已恢复与主控的连接','success',3000,'conn');
    try{window.dispatchEvent(new CustomEvent('oc-conn-recovered'));}catch(e){}
    // 恢复后自动刷新当前页主数据（存在才调，页面各自认领）
    ['loadTasks','loadDevices'].forEach(function(fn){
      try{if(typeof window[fn]==='function')window[fn]();}catch(e){}
    });
  }
  async function probe(){
    try{
      const ctrl=new AbortController();
      const to=setTimeout(function(){ctrl.abort();},4000);
      const r=await fetch(_apiUrl('/health'),{cache:'no-store',signal:ctrl.signal});
      clearTimeout(to);
      if(r)_setOnline();
    }catch(e){failStreak++;_render();}
  }
  return {
    apiOk:function(){_setOnline();},
    apiFail:function(){failStreak++;if(failStreak>=2||(wsIsDown&&failStreak>=1))_setOffline();else _render();},
    wsDown:function(){wsIsDown=true;if(failStreak>=1)_setOffline();},
    wsUp:function(){wsIsDown=false;if(state==='offline')probe();},
    probe:probe,
    isOffline:function(){return state==='offline';},
    lastOkAt:function(){return lastOkAt;}
  };
})();

/* ── i18n 国际化 ── */
const _i18n={
  zh:{
    'overview':'总览','devices':'设备管理','tasks':'任务中心','screen-monitor':'屏幕墙',
    'batch-ops':'批量操作','ai-assistant':'AI 助手','cluster':'集群管理','platforms':'平台控制',
    'perf-monitor':'性能监控','screen-record':'录屏管理','script-engine':'脚本执行器',
    'quick-actions':'批量快捷操作','batch-upload':'批量文件上传','scheduled-jobs':'定时任务',
    'data-export':'数据导出','device-assets':'设备资产','ai-script':'AI脚本生成',
    'op-timeline':'操作时间线','sync-mirror':'同步镜像操作','health-report':'健康报告',
    'tpl-market':'模板库','visual-workflow':'可视化画布','multi-screen':'多屏操控',
    'user-mgmt':'用户与权限','logout':'退出登录','api-docs':'API 文档',
    'system-status':'系统状态','running':'运行正常','online-devices':'在线设备',
    'total-devices':'总设备','total-tasks':'总任务','success':'成功','failed':'失败',
    'running-tasks':'执行中','search-device':'搜索设备…','all-status':'全部状态',
    'online':'在线','offline':'离线','busy':'执行中','compact':'紧凑','standard':'标准',
    'large-card':'大卡片','save':'保存','load':'加载','execute':'执行','clear':'清空',
    'cancel':'取消','confirm':'确认','delete':'删除','create':'创建','refresh':'刷新',
    'sec-leads':'线索与转化','sec-acquire':'获客作业','sec-acquire-more':'更多平台','sec-devices':'设备与网络',
    'sec-auto':'自动化','sec-data':'数据与消息','sec-system':'系统',
  },
  en:{
    'overview':'Overview','devices':'Devices','tasks':'Tasks','screen-monitor':'Screen Wall',
    'batch-ops':'Batch Ops','ai-assistant':'AI Assistant','cluster':'Cluster','platforms':'Platforms',
    'perf-monitor':'Performance','screen-record':'Recordings','script-engine':'Script Engine',
    'quick-actions':'Quick Actions','batch-upload':'File Upload','scheduled-jobs':'Scheduled Jobs',
    'data-export':'Data Export','device-assets':'Device Assets','ai-script':'AI Script Gen',
    'op-timeline':'Timeline','sync-mirror':'Sync Mirror','health-report':'Health Report',
    'tpl-market':'Templates','visual-workflow':'Visual Workflow','multi-screen':'Multi-Screen',
    'user-mgmt':'Users','logout':'Logout','api-docs':'API Docs',
    'system-status':'System Status','running':'Running','online-devices':'Online','total-devices':'Total',
    'total-tasks':'Tasks','success':'Success','failed':'Failed','running-tasks':'Running',
    'search-device':'Search devices...','all-status':'All Status',
    'online':'Online','offline':'Offline','busy':'Busy','compact':'Compact','standard':'Standard',
    'large-card':'Large','save':'Save','load':'Load','execute':'Execute','clear':'Clear',
    'cancel':'Cancel','confirm':'Confirm','delete':'Delete','create':'Create','refresh':'Refresh',
    'sec-leads':'Leads & Conversion','sec-acquire':'Acquisition','sec-acquire-more':'More Platforms','sec-devices':'Devices & Network',
    'sec-auto':'Automation','sec-data':'Data & Messages','sec-system':'System',
  }
};
let _curLang=localStorage.getItem('oc-lang')||'zh';
function t(key){return (_i18n[_curLang]&&_i18n[_curLang][key])||(_i18n.zh[key])||key;}
function toggleLang(){
  _curLang=_curLang==='zh'?'en':'zh';
  localStorage.setItem('oc-lang',_curLang);
  document.getElementById('lang-toggle').textContent=_curLang==='zh'?'中':'EN';
  _applyI18n();
  if(typeof _i18nWalkApply==='function')_i18nWalkApply();
}
function _applyI18n(){
  document.querySelectorAll('[data-i18n]').forEach(el=>{
    el.textContent=t(el.dataset.i18n);
  });
  document.querySelectorAll('[data-i18n-placeholder]').forEach(el=>{
    el.placeholder=t(el.dataset.i18nPlaceholder);
  });
}
/* 开页即应用持久化语言（此前只有手动切换才生效，en 用户开页恒中文） */
if(_curLang!=='zh'){
  try{
    _applyI18n();
    const _lb=document.getElementById('lang-toggle');
    if(_lb)_lb.textContent='EN';
    setTimeout(function(){if(typeof _i18nWalkApply==='function')_i18nWalkApply();},0);
  }catch(e){}
}
(function _initLang(){
  const btn=document.getElementById('lang-toggle');
  if(btn)btn.textContent=_curLang==='zh'?'中':'EN';
})();

/* ── 2026-05-08 P1-1: 任务错误信息中文化 + 可操作建议 ── */
const _ERROR_LOCALES = [
  {pattern: 'all_attempts_rejected', zh: '好友请求全部被忽略', hint: '建议: 更换目标群组，或等待 24 小时后重试'},
  {pattern: 'quota_or_pool_exhausted', zh: '今日好友请求已达上限', hint: '系统将在明日自动恢复配额'},
  {pattern: 'quota_exceeded', zh: '操作频率超限', hint: '该账号触发风控，建议暂停 2-4 小时'},
  {pattern: '任务执行超时', zh: '任务执行超时', hint: '设备响应过慢，可尝试重启设备后重试'},
  {pattern: '任务孤儿', zh: '任务因服务重启中断', hint: '幂等任务已自动重新排队，非幂等任务需手动重试'},
  {pattern: '无可用设备', zh: '设备未连接', hint: '请检查 USB 线连接和 ADB 授权'},
  {pattern: 'keyword 必填', zh: '缺少搜索关键词', hint: '请在参数中填写 keyword 字段'},
  {pattern: 'extract_zero_after_disc', zh: '群成员提取为空', hint: '该群组可能无活跃成员，尝试更换群组'},
  {pattern: 'fb.home_tab_not_found', zh: '无法进入 Facebook 首页', hint: '请检查 Facebook 是否正常安装，或重启设备'},
  {pattern: 'account.*disabled', zh: '账号已被封禁', hint: '该账号已被 Facebook 停用，需更换账号'},
  {pattern: 'geo_mismatch', zh: '地理位置不匹配', hint: '设备 VPN 区域与账号注册地不一致'},
  {pattern: 'feed 浏览已达时间上限', zh: 'Feed 浏览已完成（达到时间上限）', hint: '这是正常结束，养号效果已达到'},
  {pattern: 'device_offline', zh: '设备离线', hint: '检查 USB 连接，尝试 adb reconnect'},
  {pattern: 'Suspicious login', zh: '账号触发登录验证', hint: '需要手动完成安全验证后重试'},
  {pattern: 'temporarily blocked', zh: '账号被临时封锁', hint: '暂停该账号所有操作 24-48 小时'},
];

function localizeTaskError(rawError) {
  if (!rawError) return {text: '', hint: ''};
  const s = String(rawError);
  for (const e of _ERROR_LOCALES) {
    if (s.includes(e.pattern) || new RegExp(e.pattern, 'i').test(s)) {
      return {text: e.zh, hint: e.hint, original: s};
    }
  }
  return {text: s.length > 80 ? s.substring(0, 77) + '...' : s, hint: '', original: s};
}

/* ── Worker Color Registry ─────────────────────────────────────────── */
// Palette excludes green/red to avoid confusion with online/offline status indicators
const _WCP=['#2196F3','#9C27B0','#FF9800','#00BCD4','#FF5722','#795548','#E91E63','#607D8B'];
window._workerColorMap={};
window._deviceWorkerMap={}; // device_id → workerName, populated after loadDevices

function _getWorkerColor(name){
  if(!name||name==='本机'||name==='主控')return '#607D8B';
  if(!_workerColorMap[name]){
    // Stable hash: same name always gets same color regardless of load order
    let h=0;for(let i=0;i<name.length;i++)h=(h*31+name.charCodeAt(i))&0xffff;
    _workerColorMap[name]=_WCP[h%_WCP.length];
  }
  return _workerColorMap[name];
}
function _hexToRgba(hex,a){
  try{const r=parseInt(hex.slice(1,3),16),g=parseInt(hex.slice(3,5),16),b=parseInt(hex.slice(5,7),16);return `rgba(${r},${g},${b},${a})`;}
  catch(e){return `rgba(96,165,250,${a})`;}
}
function _getDeviceWorker(device_id){return window._deviceWorkerMap[device_id]||'本机';}
function _workerBadge(workerName){
  if(!workerName||workerName==='本机'||workerName==='主控')return '';
  const c=_getWorkerColor(workerName);
  return `<span class="worker-badge" style="background:${c}">${workerName}</span>`;
}
function _workerBadgeById(device_id){
  const wn=_getDeviceWorker(device_id);
  return wn!=='本机'?_workerBadge(wn):'';
}
function _buildDeviceWorkerMap(devices){
  (devices||[]).forEach(d=>{
    window._deviceWorkerMap[d.device_id]=(d._isCluster&&d.host_name)?d.host_name:'本机';
  });
}

/* ── Auth Guard ── */
const _OC_TOKEN=localStorage.getItem('oc_token')||'';
const _OC_USER=localStorage.getItem('oc_user')||'';
const _OC_ROLE=localStorage.getItem('oc_role')||'';
function _authHeaders(){return _OC_TOKEN?{'Authorization':'Bearer '+_OC_TOKEN}:{}}
(function _authGuard(){
  if(!_OC_TOKEN){window.location.href='/login';return;}
  const ui=document.getElementById('user-info');
  if(ui)ui.textContent=(_OC_USER||'user')+' ('+_roleLabel(_OC_ROLE)+')';
})();
function doLogout(){
  fetch(_apiUrl('/auth/logout'),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({token:_OC_TOKEN})});
  localStorage.removeItem('oc_token');localStorage.removeItem('oc_user');localStorage.removeItem('oc_role');
  document.cookie='oc_token=;path=/;max-age=0';window.location.href='/login';
}

/* ── User Management ── */
async function loadUserMgmtPage(){
  const list=document.getElementById('users-list');
  if(!list)return; list.innerHTML='<div style="color:var(--text-muted)">加载中…</div>';
  try{
    const r=await fetch(_apiUrl('/auth/users'),{headers:_authHeaders()});
    const users=await r.json();
    list.innerHTML=users.map(u=>`
      <div style="display:flex;align-items:center;justify-content:space-between;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:12px 16px">
        <div><span style="font-weight:600;font-size:13px">${u.username}</span>
        <span style="margin-left:8px;font-size:10px;padding:2px 8px;border-radius:6px;background:${u.role==='admin'?'var(--blue-strong)':u.role==='operator'?'var(--green-strong)':u.role==='customer_service'?'var(--violet)':'var(--text-muted)'};color:#fff">${_roleLabel(u.role)}</span>
        <span style="margin-left:8px;font-size:11px;color:var(--text-muted)">${u.display||''}</span></div>
        <div style="display:flex;gap:6px">
          <button class="sb-btn2" onclick="editUserRole('${u.username}')" style="font-size:10px">改角色</button>
          <button class="sb-btn2" onclick="resetUserPass('${u.username}')" style="font-size:10px">重置密码</button>
          <button class="sb-btn2" onclick="deleteUser('${u.username}')" style="font-size:10px;color:var(--red)">删除</button>
        </div>
      </div>`).join('');
  }catch(e){list.innerHTML='<div style="color:var(--red)">加载失败:'+e.message+'</div>';}
}
function showAddUserForm(){document.getElementById('add-user-form').style.display='block';}
async function createUser(){
  const name=document.getElementById('new-user-name').value.trim();
  const pass=document.getElementById('new-user-pass').value;
  const role=document.getElementById('new-user-role').value;
  const disp=document.getElementById('new-user-display').value.trim();
  if(!name){showToast('\u8bf7\u8f93\u5165\u7528\u6237\u540d','warn');return;}
  await fetch(_apiUrl('/auth/users'),{method:'POST',headers:{...{'Content-Type':'application/json'},..._authHeaders()},
    body:JSON.stringify({username:name,password:pass,role:role,display:disp||name})});
  document.getElementById('add-user-form').style.display='none';
  loadUserMgmtPage();
}
async function editUserRole(username){
  const role=await ocPrompt('新角色','operator',{message:'admin / operator / viewer',inputPlaceholder:'角色'});
  if(!role)return;
  await fetch(_apiUrl('/auth/users/'+username),{method:'PUT',headers:{...{'Content-Type':'application/json'},..._authHeaders()},
    body:JSON.stringify({role:role})});
  loadUserMgmtPage();
}
async function resetUserPass(username){
  const pass=await ocPrompt('新密码','123456',{inputPlaceholder:'密码'});
  if(!pass)return;
  await fetch(_apiUrl('/auth/users/'+username),{method:'PUT',headers:{...{'Content-Type':'application/json'},..._authHeaders()},
    body:JSON.stringify({password:pass})});
  showToast('密码已重置','success');
}
async function deleteUser(username){
  if(!(await ocDialog({title:'删除用户',message:'确认删除用户 '+username+' ？',type:'danger',confirmText:'删除',dangerous:true})))return;
  await fetch(_apiUrl('/auth/users/'+username),{method:'DELETE',headers:_authHeaders()});
  loadUserMgmtPage();
}
async function changeMyPassword(){
  const pass=document.getElementById('change-pass').value;
  if(!pass){showToast('请输入新密码','warn');return;}
  await fetch(_apiUrl('/auth/users/'+_OC_USER),{method:'PUT',headers:{...{'Content-Type':'application/json'},..._authHeaders()},
    body:JSON.stringify({password:pass})});
  showToast('密码修改成功，下次登录生效','success');
}
function showUserMenu(){
  var old=document.getElementById('oc-user-menu');
  if(old){old.remove();return;}
  var m=document.createElement('div');
  m.id='oc-user-menu';
  m.style.cssText='position:fixed;top:52px;right:16px;z-index:5000;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;box-shadow:0 12px 40px rgba(0,0,0,.45);min-width:190px;padding:6px;font-size:12px';
  var items=[];
  items.push('<div style="padding:8px 10px;color:var(--text-muted);border-bottom:1px solid var(--border);margin-bottom:4px"><b style="color:var(--text-main)">'+(_OC_USER||'user')+'</b> · '+_roleLabel(_OC_ROLE)+'</div>');
  if(_isAdminRole()) items.push('<div class="oc-um-item" onclick="document.getElementById(\'oc-user-menu\').remove();if(typeof navigateToPage===\'function\')navigateToPage(\'user-mgmt\')">&#128101; 用户与权限</div>');
  items.push('<div class="oc-um-item" style="color:var(--red)" onclick="doLogout()">&#128682; 退出登录</div>');
  m.innerHTML=items.join('');
  document.body.appendChild(m);
  setTimeout(function(){
    document.addEventListener('click',function h(e){
      var ui=document.getElementById('user-info');
      if(!m.contains(e.target)&&e.target!==ui&&!(ui&&ui.contains(e.target))){
        m.remove();document.removeEventListener('click',h);
      }
    });
  },0);
}

/* ── Sidebar Section Toggle & Search ── */
function _toggleSection(el){
  el.classList.toggle('collapsed');
  const grp=el.nextElementSibling;
  if(grp&&grp.classList.contains('nav-group')){grp.classList.toggle('collapsed');}
  const k=el.dataset.sec;
  if(k){
    try{
      const st=JSON.parse(localStorage.getItem('oc_nav_collapsed')||'{}');
      st[k]=el.classList.contains('collapsed');
      localStorage.setItem('oc_nav_collapsed',JSON.stringify(st));
    }catch(e){}
  }
}
/* 折叠状态跨会话记忆（core.js 在 body 尾加载，侧栏 DOM 已就位） */
(function _restoreNavCollapse(){
  let st={};
  try{st=JSON.parse(localStorage.getItem('oc_nav_collapsed')||'{}');}catch(e){}
  document.querySelectorAll('.nav-section[data-sec]').forEach(s=>{
    const k=s.dataset.sec;
    if(!(k in st))return;
    const grp=s.nextElementSibling;
    s.classList.toggle('collapsed',!!st[k]);
    if(grp&&grp.classList.contains('nav-group'))grp.classList.toggle('collapsed',!!st[k]);
  });
})();
function _roleLabel(role){
  role=(role||'').toLowerCase();
  if(role==='admin') return '管理员';
  if(role==='operator') return '操作员';
  if(role==='customer_service') return '客服';
  if(role==='viewer') return '只读';
  return role||'用户';
}
function _isAdminRole(){
  return (document.documentElement.getAttribute('data-role')||_OC_ROLE||'').toLowerCase()==='admin';
}
function _filterNav(q){
  q=q.toLowerCase().trim();
  const admin=_isAdminRole();
  document.querySelectorAll('.nav-group .nav-item').forEach(it=>{
    if(!admin && it.closest('[data-admin-only]')) return;
    const txt=(it.textContent||'').toLowerCase();
    const pg=(it.dataset.page||'').toLowerCase();
    const match=!q||txt.includes(q)||pg.includes(q);
    it.classList.toggle('hidden',!match);
    // 家族子页（fam-child）平时隐藏，只在搜索命中时显形——Ctrl+K 搜「录屏」仍能直达
    if(it.classList.contains('fam-child')) it.classList.toggle('sr-show',!!q&&match);
  });
  if(q){
    document.querySelectorAll('.nav-group').forEach(g=>{
      if(!admin && g.hasAttribute('data-admin-only')) return;
      g.classList.remove('collapsed');
    });
    document.querySelectorAll('.nav-section').forEach(s=>{
      if(!admin && s.hasAttribute('data-admin-only')) return;
      s.classList.remove('collapsed');
    });
  }
}
/* ── Ctrl+K 命令面板（P2）：页面 + 动作 + 客户 三域搜索；「AI 指令」入口收编于此 ── */
const _CP_ACTIONS=[
  {label:'AI 指令 · 自然语言控制台',hint:'页面',run:function(){if(typeof navigateToPage==='function')navigateToPage('chat');}},
  {label:'修复离线设备',hint:'动作',run:function(){if(window.fixAllOffline)fixAllOffline();}},
  {label:'取消全部运行任务',hint:'动作',run:function(){if(window.cancelAllTasks)cancelAllTasks();}},
  {label:'投屏大屏 · L2 看板',hint:'新窗口',run:function(){window.open('/static/l2-dashboard.html','_blank');}},
  {label:'API 文档',hint:'新窗口',run:function(){window.open('/docs','_blank');}},
  {label:'演示模式 · 示例数据开/关',hint:'动作',run:function(){if(window.toggleDemoMode)toggleDemoMode();}},
];
let _cpSel=0,_cpRows=[],_cpLeadTimer=null,_cpLeadRows=[];
function _cpPages(){
  const admin=_isAdminRole();
  const seen=new Set();const out=[];
  document.querySelectorAll('.nav-item[data-page]').forEach(it=>{
    if(!admin&&it.closest('[data-admin-only]'))return;
    const pg=it.dataset.page;
    if(seen.has(pg))return;
    seen.add(pg);
    out.push({label:(it.textContent||'').replace(/^[\u00b7\s]+/,'').trim(),hint:'页面',run:(function(p){return function(){if(typeof navigateToPage==='function')navigateToPage(p);};})(pg)});
  });
  return out;
}
function _cpRender(q){
  const list=document.getElementById('oc-cp-list');
  if(!list)return;
  q=(q||'').toLowerCase().trim();
  const rows=[];
  _cpPages().forEach(r=>{if(!q||r.label.toLowerCase().includes(q))rows.push(r);});
  _CP_ACTIONS.forEach(r=>{if(!q||r.label.toLowerCase().includes(q))rows.push(r);});
  (_cpLeadRows||[]).forEach(r=>rows.push(r));
  _cpRows=rows.slice(0,14);
  _cpSel=Math.min(_cpSel,Math.max(0,_cpRows.length-1));
  list.innerHTML=_cpRows.map((r,i)=>'<div class="oc-cp-item'+(i===_cpSel?' sel':'')+'" data-i="'+i+'" onmousedown="event.preventDefault();_cpRun('+i+')">'
    +'<span>'+r.label+'</span><span class="oc-cp-hint">'+(r.hint||'')+'</span></div>').join('')
    ||'<div style="padding:14px;color:var(--text-muted);font-size:12px">没有匹配项</div>';
}
function _cpRun(i){
  const r=_cpRows[i];
  _cpClose();
  if(r&&r.run){
    try{if(window._navBeacon)_navBeacon('palette','action');}catch(e){}
    r.run();
  }
}
function _cpQueryLeads(q){
  if(_cpLeadTimer)clearTimeout(_cpLeadTimer);
  if(!q||q.length<2){_cpLeadRows=[];_cpRender(q);return;}
  _cpLeadTimer=setTimeout(async function(){
    try{
      const d=await api('GET','/lead-mesh/leads/search?name_like='+encodeURIComponent(q)+'&limit=6');
      const rows=((d&&d.results)||[]).map(function(r){
        const name=r.display_name||r.name||r.peer_name||r.canonical_id||'?';
        const plat=r.platform?(' · '+r.platform):'';
        return {label:'客户 · '+name+plat,hint:'档案',run:(function(id){return function(){if(window.lmOpenLeadDossier)lmOpenLeadDossier(id);};})(r.canonical_id)};
      });
      const inp=document.getElementById('oc-cp-input');
      if(inp&&inp.value.trim().toLowerCase()===q.toLowerCase()){_cpLeadRows=rows;_cpRender(q);}
    }catch(e){}
  },250);
}
function _cpClose(){const o=document.getElementById('oc-cmdpal');if(o)o.remove();}
function _cpOpen(){
  if(document.getElementById('oc-cmdpal')){_cpClose();return;}
  const o=document.createElement('div');
  o.id='oc-cmdpal';
  o.innerHTML='<div class="oc-cp-box">'
    +'<input id="oc-cp-input" placeholder="搜页面 / 动作 / 客户…（Esc 关闭）" autocomplete="off"/>'
    +'<div id="oc-cp-list"></div>'
    +'<div class="oc-cp-foot">↑↓ 选择 · Enter 执行 · 客户搜索输入 2 字起</div></div>';
  o.addEventListener('mousedown',function(e){if(e.target===o)_cpClose();});
  document.body.appendChild(o);
  const inp=document.getElementById('oc-cp-input');
  inp.addEventListener('input',function(){_cpSel=0;_cpLeadRows=[];const q=inp.value;_cpRender(q);_cpQueryLeads(q.trim());});
  inp.addEventListener('keydown',function(e){
    if(e.key==='Escape'){e.preventDefault();_cpClose();}
    else if(e.key==='ArrowDown'){e.preventDefault();_cpSel=Math.min(_cpSel+1,_cpRows.length-1);_cpRender(inp.value);}
    else if(e.key==='ArrowUp'){e.preventDefault();_cpSel=Math.max(_cpSel-1,0);_cpRender(inp.value);}
    else if(e.key==='Enter'){e.preventDefault();_cpRun(_cpSel);}
  });
  _cpLeadRows=[];
  _cpRender('');
  inp.focus();
}
document.addEventListener('keydown',e=>{
  if((e.ctrlKey||e.metaKey)&&e.key==='k'){e.preventDefault();_cpOpen();}
  if(e.key==='Escape'&&document.getElementById('oc-cmdpal'))_cpClose();
});

/* ── Toast Notification System ── */
function showToast(msg,type='info',duration=4000,group=''){
  const icons={success:'\u2705',error:'\u274C',warn:'\u26A0\uFE0F',info:'\u2139\uFE0F'};
  const c=document.getElementById('toast-container');
  // 同组 toast 互斥：新消息自动清除旧消息
  if(group){
    c.querySelectorAll('[data-toast-group="'+group+'"]').forEach(el=>el.remove());
  }
  const t=document.createElement('div');
  t.className='toast '+(type||'info');
  if(group) t.setAttribute('data-toast-group',group);
  t.innerHTML='<span class="t-icon">'+(icons[type]||icons.info)+'</span><span class="t-msg">'+msg+'</span><span class="t-close" onclick="this.parentElement.remove()">&times;</span>';
  c.appendChild(t);
  setTimeout(()=>{if(t.parentElement)t.remove();},duration);
}

/* ── ocDialog — 统一页面内弹窗 (替代 confirm/prompt/alert) ── */
/**
 * ocDialog(opts) → Promise<boolean|string|null>
 *
 * opts.mode      : 'confirm'(默认) | 'prompt' | 'alert'
 * opts.title     : 标题文字
 * opts.message   : 描述文字 (支持 HTML)
 * opts.type      : 'danger' | 'warning' | 'info'(默认) | 'success'
 * opts.confirmText: 确认按钮文字 (默认根据 type 自动)
 * opts.cancelText : 取消按钮文字 (默认 '取消')
 * opts.inputDefault    : prompt 模式默认值
 * opts.inputPlaceholder: prompt 模式 placeholder
 * opts.dangerous : danger模式下蒙版不可点击关闭
 *
 * 返回: confirm → true/false, prompt → string/null, alert → true
 */
function ocDialog(opts){
  if(!opts) opts={};
  var mode=opts.mode||'confirm';
  var type=opts.type||'info';
  var icons={danger:'🗑',warning:'⚠️',info:'ℹ️',success:'✅'};
  var defConfirm={danger:'删除',warning:'继续',info:'确定',success:'确定'};
  var confirmText=opts.confirmText||defConfirm[type]||'确定';
  var cancelText=opts.cancelText||'取消';

  return new Promise(function(resolve){
    // 清除已有对话
    var old=document.querySelector('.oc-dialog-overlay');
    if(old) old.remove();

    var overlay=document.createElement('div');
    overlay.className='oc-dialog-overlay';

    var box=document.createElement('div');
    box.className='oc-dialog-box';

    // header
    var header=document.createElement('div');
    header.className='oc-dlg-header';
    var iconEl=document.createElement('div');
    iconEl.className='oc-dlg-icon '+type;
    iconEl.textContent=icons[type]||icons.info;
    var textWrap=document.createElement('div');
    textWrap.style.cssText='flex:1;min-width:0';
    var titleEl=document.createElement('div');
    titleEl.className='oc-dlg-title';
    titleEl.textContent=opts.title||'提示';
    textWrap.appendChild(titleEl);
    if(opts.message){
      var msgEl=document.createElement('div');
      msgEl.className='oc-dlg-message';
      msgEl.innerHTML=opts.message;
      textWrap.appendChild(msgEl);
    }
    header.appendChild(iconEl);
    header.appendChild(textWrap);
    box.appendChild(header);

    // input (prompt mode)
    var inputEl=null;
    if(mode==='prompt'){
      inputEl=document.createElement('input');
      inputEl.className='oc-dlg-input';
      inputEl.type='text';
      inputEl.value=opts.inputDefault||'';
      inputEl.placeholder=opts.inputPlaceholder||'';
      box.appendChild(inputEl);
    }

    // actions
    var actions=document.createElement('div');
    actions.className='oc-dlg-actions';
    var resolved=false;

    function close(val){
      if(resolved) return;
      resolved=true;
      overlay.classList.add('oc-dlg-closing');
      setTimeout(function(){ overlay.remove(); },140);
      resolve(val);
    }

    if(mode!=='alert'){
      var cancelBtn=document.createElement('button');
      cancelBtn.className='oc-dlg-btn cancel';
      cancelBtn.textContent=cancelText;
      cancelBtn.onclick=function(){close(mode==='prompt'?null:false);};
      actions.appendChild(cancelBtn);
    }

    var confirmBtn=document.createElement('button');
    confirmBtn.className='oc-dlg-btn primary '+type;
    confirmBtn.textContent=confirmText;
    confirmBtn.onclick=function(){
      close(mode==='prompt'?(inputEl?inputEl.value:''):true);
    };
    actions.appendChild(confirmBtn);
    box.appendChild(actions);
    overlay.appendChild(box);

    // 蒙版点击关闭（danger 模式除外）
    overlay.addEventListener('click',function(e){
      if(e.target===overlay && !opts.dangerous){
        close(mode==='prompt'?null:mode==='alert'?true:false);
      }
    });

    // 键盘事件
    function onKey(e){
      if(resolved) return;
      if(e.key==='Escape'){
        e.preventDefault();
        close(mode==='prompt'?null:mode==='alert'?true:false);
      }
      if(e.key==='Enter'&&mode!=='prompt'){
        e.preventDefault();
        close(true);
      }
      if(e.key==='Enter'&&mode==='prompt'&&inputEl){
        e.preventDefault();
        close(inputEl.value);
      }
    }
    document.addEventListener('keydown',onKey);
    // 弹窗关闭后移除监听
    var origClose=close;
    close=function(val){
      document.removeEventListener('keydown',onKey);
      origClose(val);
    };

    document.body.appendChild(overlay);

    // 自动聚焦
    if(inputEl){inputEl.focus();inputEl.select();}
    else{confirmBtn.focus();}
  });
}

/* 快捷方法 — 替代原生 confirm() */
function ocConfirm(message,opts){
  if(typeof message==='object') return ocDialog(message);
  return ocDialog(Object.assign({mode:'confirm',title:'操作确认',message:message},opts||{}));
}

/* 快捷方法 — 替代原生 prompt() */
function ocPrompt(title,defaultVal,opts){
  return ocDialog(Object.assign({mode:'prompt',title:title||'输入',inputDefault:defaultVal||''},opts||{}));
}

/* 快捷方法 — 替代原生 alert() */
function ocAlert(message,opts){
  return ocDialog(Object.assign({mode:'alert',title:'提示',message:message},opts||{}));
}

/* ── Theme ── */
function _initTheme(){
  const saved=localStorage.getItem('oc-theme');
  if(saved){document.documentElement.setAttribute('data-theme',saved);}
  else if(window.matchMedia('(prefers-color-scheme:light)').matches){document.documentElement.setAttribute('data-theme','light');}
  _updateThemeIcon();
}
function toggleTheme(){
  const cur=document.documentElement.getAttribute('data-theme')||'dark';
  const next=cur==='dark'?'light':'dark';
  document.documentElement.setAttribute('data-theme',next);
  localStorage.setItem('oc-theme',next);
  _updateThemeIcon();
}
function _updateThemeIcon(){
  const btn=document.getElementById('theme-toggle');
  if(!btn) return;
  const isDark=(document.documentElement.getAttribute('data-theme')||'dark')==='dark';
  btn.textContent=isDark?'\u{1F319}':'\u{2600}\u{FE0F}';
}
_initTheme();

/* ── P4 EN 走译层（AvatarHub 同款架构·2026-08-15）──────────────────
   词典键=中文原文：zh 态零词典零开销；缺译原样回退永不破版；
   TreeWalker 全文走译 + MutationObserver 盯动态渲染（页面族页签/页面标题/loader 产出），
   切回中文用 WeakMap 还原原文。品牌名（智拓 ReachX/BOUNDLESS）不入词典=天然不译。 */
const _EN_DICT={
  /* 分组（data-i18n 之外的兜底）与导航项 */
  '总览':'Overview','我的工作台':'My Desk','待接管队列':'Handoff Queue','客户档案':'Customers',
  '转化分析':'Conversion','内容工作室':'Content Studio','任务中心':'Tasks','AI 指令':'AI Console',
  '设备管理':'Devices','屏幕墙':'Screen Wall','设备健康':'Device Health','VPN 管理':'VPN',
  '代理中心':'Proxies','编排中心':'Orchestration','脚本工房':'Script Studio','定时任务':'Scheduled Jobs',
  '批量群控':'Batch Control','数据分析':'Analytics','日志与审计':'Logs & Audit','消息与告警':'Alerts & Inbox',
  '集群管理':'Cluster','备份恢复':'Backup','插件管理':'Plugins','用户与权限':'Users & Roles','API 文档':'API Docs',
  '退出登录':'Sign out','搜索功能...':'Search...',
  /* 页面族页签 */
  '转化漏斗':'Funnel','ROI 面板':'ROI','指挥台':'Command Center','投屏大屏':'Big Screen',
  '设备列表':'Device List','设备分组':'Groups','设备资产':'Assets',
  '屏幕监控':'Monitor','多屏操控':'Multi-Screen','录屏管理':'Recordings',
  '健康监控':'Health','性能监控':'Performance','体检报告':'Report',
  '工作流列表':'Workflows','可视化画布':'Visual Canvas',
  '脚本模板':'Templates','AI 生成':'AI Generate','操作回放':'Replay','模板库':'Library',
  '快捷操作':'Quick Actions','安装应用':'Install APK','文字输入':'Text Input','文件上传':'Upload',
  '应用管理':'App Manager','同步输入':'Sync Input',
  '分析总览':'Overview','数据导出':'Export',
  '系统日志':'System Log','审计日志':'Audit Log','操作时间线':'Timeline',
  '手机消息':'Phone Inbox','推送渠道':'Push Channels','告警规则':'Alert Rules',
  /* 页面标题（家族 · 页签 组合，navigateToPage 动态设置） */
  '转化分析 · 转化漏斗':'Conversion · Funnel','转化分析 · ROI 面板':'Conversion · ROI',
  '转化分析 · 指挥台':'Conversion · Command Center',
  '设备管理 · 设备列表':'Devices · List','设备管理 · 设备分组':'Devices · Groups',
  '设备管理 · 设备资产':'Devices · Assets',
  '屏幕墙 · 屏幕监控':'Screen Wall · Monitor','屏幕墙 · 多屏操控':'Screen Wall · Multi-Screen',
  '屏幕墙 · 录屏管理':'Screen Wall · Recordings',
  '设备健康 · 健康监控':'Device Health · Health','设备健康 · 性能监控':'Device Health · Performance',
  '设备健康 · 体检报告':'Device Health · Report',
  '编排中心 · 工作流列表':'Orchestration · Workflows','编排中心 · 可视化画布':'Orchestration · Canvas',
  '脚本工房 · 脚本模板':'Script Studio · Templates','脚本工房 · AI 生成':'Script Studio · AI Generate',
  '脚本工房 · 操作回放':'Script Studio · Replay','脚本工房 · 模板库':'Script Studio · Library',
  '批量群控 · 快捷操作':'Batch · Quick Actions','批量群控 · 安装应用':'Batch · Install APK',
  '批量群控 · 文字输入':'Batch · Text Input','批量群控 · 文件上传':'Batch · Upload',
  '批量群控 · 应用管理':'Batch · App Manager','批量群控 · 同步输入':'Batch · Sync Input',
  '数据分析 · 分析总览':'Analytics · Overview','数据分析 · 数据导出':'Analytics · Export',
  '日志与审计 · 系统日志':'Logs · System','日志与审计 · 审计日志':'Logs · Audit',
  '日志与审计 · 操作时间线':'Logs · Timeline',
  '消息与告警 · 手机消息':'Alerts · Phone Inbox','消息与告警 · 推送渠道':'Alerts · Push Channels',
  '消息与告警 · 告警规则':'Alerts · Rules',
  /* 顶栏 / 用户菜单 */
  '运行正常':'Running','连接中...':'Connecting...','管理员':'Admin','操作员':'Operator',
  '客服':'Support','只读':'Viewer',
  /* 总览三区 */
  '一键操作':'Quick Actions','今日简报':'Daily Brief','更多面板 · 运营洞察与明细':'More Panels · Insights & Details',
  '在线设备':'Online Devices','任务总数':'Total Tasks','平均电量':'Avg Battery','运行中任务':'Running Tasks',
  '客服接管队列':'Handoff Queue','真人接客户 · 标成交':'Human takeover · Mark deals',
  'L2 客户漏斗':'L2 Funnel','投屏看板 · 实时刷':'Big screen · Live',
  '立即收件箱':'Scan Inbox Now','所有设备 · AI自动回复':'All devices · AI auto-reply',
  '关注拓客':'Follow & Acquire','进 TikTok · 选国家与数量':'TikTok · Pick geo & volume',
  '跟进回关':'Follow-backs','检测回关 · AI发送DM':'Detect follow-backs · AI DM',
  '账号预热':'Account Warmup','刷视频+点赞 · 养号':'Watch + like · Nurture',
  '修复离线':'Fix Offline','重连所有离线设备':'Reconnect offline devices',
  '取消全部任务':'Cancel All Tasks','停止所有运行中任务':'Stop all running tasks',
  '今日刷视频 →':'Watched today →','今日关注 →':'Followed today →','今日私信 →':'DMs today →',
  'AI自动回复 →':'AI replies →','今日新线索 →':'New leads →','已转化 →':'Converted →',
  'AI 智能引擎':'AI Engine','LLM 调用总次数':'LLM Calls','缓存命中率':'Cache Hit','AI改写条数':'AI Rewrites',
  '自动回复条数':'Auto Replies','设备状态':'Device Status','最近任务':'Recent Tasks',
  '设备效能排行':'Device Ranking','今日运营日报':'Daily Ops Report','近7天活跃趋势':'7-Day Activity',
  /* 命令面板 / 演示模式 */
  '搜页面 / 动作 / 客户…（Esc 关闭）':'Search pages / actions / customers… (Esc to close)',
  '↑↓ 选择 · Enter 执行 · 客户搜索输入 2 字起':'↑↓ select · Enter run · type 2+ chars for customers',
  '页面':'Page','动作':'Action','档案':'Dossier','新窗口':'New Window','没有匹配项':'No matches',
  'AI 指令 · 自然语言控制台':'AI Console · natural language',
  '修复离线设备':'Fix offline devices','取消全部运行任务':'Cancel all running tasks',
  '投屏大屏 · L2 看板':'Big Screen · L2 board','演示模式 · 示例数据开/关':'Demo mode · sample data on/off',
  '演示模式 · 页面数字为示例数据':'Demo mode · numbers are samples','退出':'Exit',
  /* 客服页高频 */
  '待处理':'Pending','已认领':'Claimed','已完成':'Done','已驳回':'Rejected','刷新':'Refresh',
  /* 漏斗页 */
  '引流转化漏斗':'Acquisition Funnel','刷新数据':'Refresh','近7天趋势':'7-Day Trend',
  '好友通过率':'Accept Rate','请求→DM率':'Request→DM','DM→引流率':'DM→Referral','打招呼发送':'Greetings Sent',
};
const _I18N_ORIG=new WeakMap();
function _walkI18n(root,toEn){
  try{
    const base=(root&&root.nodeType===1)?root:document.body;
    const w=document.createTreeWalker(base,NodeFilter.SHOW_TEXT,null);
    let n;
    while((n=w.nextNode())){
      const p=n.parentElement;
      if(!p)continue;
      const tag=p.tagName;
      if(tag==='SCRIPT'||tag==='STYLE')continue;
      if(p.closest('[data-no-i18n]'))continue;
      if(toEn){
        const t=(n.nodeValue||'').trim();
        if(!t)continue;
        const en=_EN_DICT[t];
        if(en){
          if(!_I18N_ORIG.has(n))_I18N_ORIG.set(n,n.nodeValue);
          n.nodeValue=n.nodeValue.replace(t,en);
        }
      }else if(_I18N_ORIG.has(n)){
        n.nodeValue=_I18N_ORIG.get(n);
      }
    }
    base.querySelectorAll('[placeholder],[title]').forEach(el=>{
      if(el.closest('[data-no-i18n]'))return;
      ['placeholder','title'].forEach(a=>{
        const v=el.getAttribute(a);
        if(toEn){
          if(!v)return;
          const en=_EN_DICT[v.trim()];
          if(en){
            if(!el.getAttribute('data-i18n-orig-'+a))el.setAttribute('data-i18n-orig-'+a,v);
            el.setAttribute(a,en);
          }
        }else{
          const o=el.getAttribute('data-i18n-orig-'+a);
          if(o){el.setAttribute(a,o);el.removeAttribute('data-i18n-orig-'+a);}
        }
      });
    });
  }catch(e){}
}
let _i18nMo=null;
function _i18nWalkApply(){
  const toEn=_curLang==='en';
  _walkI18n(document.body,toEn);
  if(toEn&&!_i18nMo){
    _i18nMo=new MutationObserver(function(muts){
      if(_curLang!=='en')return;
      muts.forEach(function(m){
        m.addedNodes&&m.addedNodes.forEach(function(nd){
          if(nd.nodeType===1)_walkI18n(nd,true);
          else if(nd.nodeType===3&&nd.parentElement)_walkI18n(nd.parentElement,true);
        });
      });
    });
    _i18nMo.observe(document.body,{childList:true,subtree:true});
  }
}
window._i18nWalkApply=_i18nWalkApply;
/* P5 词典取材工具：en 态跑一圈，收集当前 DOM 里仍是中文且不在词典的文本（下批词条的证据端）。
   控制台执行 __i18nMissing() 即得「频次\t原文」清单；数据串（角色名/文件名）自行甄别不译。 */
window.__i18nMissing=function(){
  const seen=new Map();
  try{
    const w=document.createTreeWalker(document.body,NodeFilter.SHOW_TEXT,null);
    let n;
    while((n=w.nextNode())){
      const p=n.parentElement;
      if(!p||p.tagName==='SCRIPT'||p.tagName==='STYLE')continue;
      if(p.closest('[data-no-i18n]'))continue;
      const t=(n.nodeValue||'').trim();
      if(!t||t.length>60)continue;
      if(!/[\u4e00-\u9fff]/.test(t))continue;
      if(_EN_DICT[t])continue;
      seen.set(t,(seen.get(t)||0)+1);
    }
  }catch(e){}
  return Array.from(seen.entries()).sort((a,b)=>b[1]-a[1]).map(([t,c])=>c+'\t'+t);
};

/* ── P5 图表取色助手：canvas 不认 var()，画图前把令牌解析成实色（跟随当前主题） ── */
function themeColor(token,fallback){
  try{
    const v=getComputedStyle(document.documentElement).getPropertyValue(token).trim();
    return v||fallback||'#4f7aff';
  }catch(e){return fallback||'#4f7aff';}
}
window.themeColor=themeColor;

let ALIAS={};
window.WP_NUM = {};       // {device_id: wallpaper_number} 壁纸状态追踪
window._globalRanges = {}; // {host_id: {start,end}} 全局编号段配置

async function loadAliases(){
  try{
    const data=await api('GET','/devices/aliases');
    ALIAS={};
    window.WP_NUM={};
    for(const[did,info] of Object.entries(data)){
      ALIAS[did]=info.alias||`${(info.number||0).toString().padStart(2,'0')}号`;
      if(info.wallpaper_number) window.WP_NUM[did]=info.wallpaper_number;
    }
  }catch(e){ALIAS={}; window.WP_NUM={};}
  // 预加载全局编号段配置（供 _updateUnsetCount 和 devCard 使用）
  try{
    window._globalRanges = await api('GET','/cluster/number-ranges');
  }catch(e){ window._globalRanges={}; }
  // 刷新未编号计数徽章
  if(typeof _updateUnsetCount==='function') _updateUnsetCount();
}
// TASK_NAMES 是前端当前展示字典：后端可能返回历史任务创建时的旧 type_label_zh，
// 因此前端展示优先使用 TASK_NAMES，再回退到 type_label_zh。
// 本字典在启动时会被 refreshTaskLabels() 覆盖并保持 Object 引用不变（Object.assign）。
let TASK_NAMES={tiktok_warmup:'养号',tiktok_watch:'刷视频',tiktok_browse_feed:'刷视频',tiktok_follow:'关注',tiktok_test_follow:'测试关注',tiktok_send_dm:'发私信',tiktok_check_inbox:'查收件箱',tiktok_acquisition:'全流程获客',tiktok_auto:'全流程获客',tiktok_check_and_chat_followbacks:'回关私信',vpn_setup:'配置VPN',vpn_status:'VPN状态',telegram_send_message:'Telegram 发消息',telegram_read_messages:'Telegram 读消息',telegram_send_file:'Telegram 发文件',telegram_workflow:'Telegram 工作流',telegram_auto_reply:'Telegram 自动回复',telegram_join_group:'Telegram 加群',telegram_send_group:'Telegram 群消息',telegram_monitor_chat:'Telegram 监控',whatsapp_send_message:'WhatsApp 发消息',whatsapp_read_messages:'WhatsApp 读消息',whatsapp_auto_reply:'WhatsApp 自动回复',whatsapp_send_media:'WhatsApp 发媒体',whatsapp_list_chats:'WhatsApp 聊天列表',facebook_send_message:'Facebook 发私信',facebook_add_friend:'Facebook 加好友(安全)',facebook_browse_feed:'Facebook 浏览动态',facebook_browse_feed_by_interest:'Facebook 兴趣刷帖',facebook_search_leads:'Facebook 搜索潜客',facebook_join_group:'Facebook 加入群组',facebook_browse_groups:'Facebook 浏览我的群组',facebook_group_engage:'Facebook 群组互动',facebook_extract_members:'Facebook 群成员候选采集',facebook_group_member_greet:'Facebook 群成员好友打招呼',facebook_check_inbox:'Facebook Messenger 收件箱',facebook_check_message_requests:'Facebook 陌生人收件箱',facebook_check_friend_requests:'Facebook 好友请求处理',facebook_campaign_run:'Facebook 剧本任务',linkedin_send_message:'LinkedIn 发消息',linkedin_read_messages:'LinkedIn 读消息',linkedin_post_update:'LinkedIn 发动态',linkedin_search_profile:'LinkedIn 搜人脉',linkedin_send_connection:'LinkedIn 发邀请',linkedin_accept_connections:'LinkedIn 接受邀请',linkedin_like_post:'LinkedIn 点赞',linkedin_comment_post:'LinkedIn 评论',instagram_browse_feed:'Instagram 浏览首页',instagram_browse_hashtag:'Instagram 浏览标签',instagram_search_leads:'Instagram 搜用户',instagram_send_dm:'Instagram 发私信',twitter_browse_timeline:'X 浏览时间线',twitter_search_leads:'X 搜用户',twitter_search_and_engage:'X 关键词互动',twitter_send_dm:'X 发私信'};
window.TASK_NAMES = TASK_NAMES;
function applyBusinessSafeTaskNames(){
  TASK_NAMES.facebook_extract_members = 'Facebook 群成员候选采集';
  TASK_NAMES.facebook_group_member_greet = 'Facebook 群成员好友打招呼';
  TASK_NAMES.facebook_campaign_run = 'Facebook 剧本任务';
}
// 从后端拉取统一的 task_type -> 中文 字典，覆盖本地兜底。启动时调一次即可。
async function refreshTaskLabels(){
  try{
    const r = await api('GET','/tasks/meta/labels');
    const labels = (r && r.labels) || {};
    if(labels && typeof labels === 'object'){
      Object.assign(TASK_NAMES, labels);
      applyBusinessSafeTaskNames();
    }
  }catch(e){ /* 后端不可用时保持兜底字典即可 */ }
  applyBusinessSafeTaskNames();
}
applyBusinessSafeTaskNames();
window.businessSafeText = function(text){
  return String(text == null ? '' : text)
    .replace(/FB 提取群成员/g, 'FB 群成员候选采集')
    .replace(/Facebook 提取群成员/g, 'Facebook 群成员候选采集')
    .replace(/提取群成员/g, '群成员候选采集')
    .replace(/群成员提取/g, '群成员候选采集')
    .replace(/成员采集/g, '成员整理')
    .replace(/圈层拓客/g, '好友打招呼')
    .replace(/好友拓展/g, '好友打招呼')
    .replace(/全链路获客/g, '全链路客服拓展');
};
// 统一入口：任务对象 -> 展示名（当前前端字典优先，历史 type_label_zh 只作回退）
window.taskDisplayName = function(task){
  if(!task) return '';
  const t = (task.type && TASK_NAMES[task.type]) || task.type_label_zh || task.type || '';
  return businessSafeText(t);
};
refreshTaskLabels();
let allDevices=[], allTasks=[], currentFilter='all';
let modalDeviceId=null, modalTimer=null, allRefreshTimer=null;
let _wsConnected=false;
let _devicePerfCache={};

/* 2026-08-16 P3 收编：_escHtml 曾在 overview/device-mgmt/studio 三处各自定义（后加载
   覆盖前者）。统一收进 core.js（最先加载，全局可用），取 null 安全语义。 */
function _escHtml(s){
  if(s==null||s==='') return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}
let _loadDevicesDebounce=null;
function _scheduleLoadDevices(){
  if(_loadDevicesDebounce) clearTimeout(_loadDevicesDebounce);
  _loadDevicesDebounce=setTimeout(()=>{loadDevices().catch(()=>{});_loadDevicesDebounce=null;},450);
}
async function api(method,path,body,timeoutMs){
  const headers={..._authHeaders()};
  if(body) headers['Content-Type']='application/json';
  if(_OC_TOKEN) headers['Authorization']='Bearer '+_OC_TOKEN;
  const apiKey=localStorage.getItem('oc_api_key');
  if(apiKey) headers['X-API-Key']=apiKey;
  const o={method,headers,credentials:'include'};
  if(body) o.body=JSON.stringify(body);
  const ms=timeoutMs!=null?timeoutMs:(path.indexOf('/devices')===0||path==='/devices'?120000:90000);
  const ctrl=new AbortController();
  const to=setTimeout(()=>ctrl.abort(),ms);
  try{
    const r=await fetch(_apiUrl(path),{...o,signal:ctrl.signal});
    clearTimeout(to);
    if(window.OCConn){try{OCConn.apiOk();}catch(_){}}  // 任何状态码=后端活着
    if(r.status===401&&!localStorage.getItem('oc_api_key')){
      /* 2026-08-16 P3：session 失效全局拦截。此前服务重启（内存 session 清空）后页面
         还能看、API 全部静默 401——用户只见「数据都空了」，发布/保存悄悄失败（浏览器
         冒烟实锤误导排查一小时）。节流跳登录页并带回跳；X-API-Key 机器身份不拦。 */
      if(!window.__auth401At||Date.now()-window.__auth401At>8000){
        window.__auth401At=Date.now();
        try{showToast('登录已过期，正在跳转登录页…','warn');}catch(_){}
        setTimeout(()=>{location.href='/login?next='+encodeURIComponent(location.pathname+location.hash);},600);
      }
      throw new Error('401 登录已过期');
    }
    if(!r.ok){
      const txt=await r.text().catch(()=>'');
      let detail=txt.substring(0,400);
      let detailObj=null;     // 结构化 detail，给需要逐字段处理的调用方用（如 P1 fb-launch dialog）
      try{
        if(txt && txt.trim().charAt(0)==='{'){
          const j=JSON.parse(txt);
          const d=j&&j.detail;
          if(d&&typeof d==='object'){
            detailObj=d;
            const msg=(d.message||d.msg||d.error||'').trim();
            const hint=(d.hint||'').trim();
            const code=(d.code||'').trim();
            if(msg) detail=msg+(hint?(' — '+hint):'')+(code?(' ['+code+']'):'');
          }else if(typeof d==='string') detail=d;
        }
      }catch(_){}
      let errLine=`${r.status} ${r.statusText}: ${detail}`;
      if(r.status===404&&path&&(path.indexOf('install-apk')>=0||path.indexOf('install-apk-cluster')>=0)){
        errLine+=' [提示: 主控需已部署含集群 APK 转发的版本并已重启；反代须放行 /batch/install-apk-cluster 与 /cluster/batch/install-apk；可 GET /health 查看 capabilities]';
      }
      const err=new Error(errLine);
      // 增强属性 — 不影响既有 e.message 用法，让新代码能区分 422 等业务错误
      err.status=r.status;
      err.statusText=r.statusText;
      if(detailObj) err.detail=detailObj;
      throw err;
    }
    return await r.json();
  }catch(e){
    clearTimeout(to);
    if(e.name==='AbortError') throw new Error('请求超时，请检查服务是否卡住或网络');
    if(e instanceof TypeError){
      /* 网络级失败（后端重启/断网）：交给连接状态机横幅统一表达。
         调用方看 err.isNetwork 决定免弹自己的红 toast（Failed to fetch 英文原文不再直出）。 */
      if(window.OCConn){try{OCConn.apiFail();}catch(_){}}
      const ne=new Error('无法连接主控服务');
      ne.isNetwork=true;
      throw ne;
    }
    throw e;
  }
}
loadAliases();
async function _refreshPerfCache(){
  try{
    const r=await api('GET','/devices/performance/all');
    _devicePerfCache=r.devices||{};
  }catch(e){}
}
_refreshPerfCache();
setInterval(_refreshPerfCache,120000);
// 每2分钟自动刷新设备列表，感知新接入设备
setInterval(()=>{ if(typeof loadDevices==='function') loadDevices().catch(()=>{}); }, 120000);

/* VPN 管理已迁移到工具面板 → devices.js */

