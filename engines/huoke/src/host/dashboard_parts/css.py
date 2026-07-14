# -*- coding: utf-8 -*-
"""Dashboard CSS 样式。"""

def css_block() -> str:
    return r"""
:root, [data-theme="dark"] {
  --bg-body: #0b1120;
  --bg-sidebar: #111827;
  --bg-card: #1e293b;
  --bg-card-hover: #263348;
  --bg-input: #0f172a;
  --bg-main: #0f172a;
  --border: #334155;
  --text: #e2e8f0;
  --text-main: #e2e8f0;
  --text-dim: #94a3b8;
  --text-muted: #64748b;
  --accent: #3b82f6;
  --accent-glow: rgba(59,130,246,.25);
  --green: #22c55e;
  --yellow: #eab308;
  --red: #ef4444;
  --sidebar-w: 220px;
  --header-h: 56px;
}
[data-theme="light"] {
  --bg-body: #f1f5f9;
  --bg-sidebar: #ffffff;
  --bg-card: #ffffff;
  --bg-card-hover: #f8fafc;
  --bg-input: #f1f5f9;
  --bg-main: #f1f5f9;
  --border: #e2e8f0;
  --text: #1e293b;
  --text-main: #1e293b;
  --text-dim: #475569;
  --text-muted: #94a3b8;
  --accent: #2563eb;
  --accent-glow: rgba(37,99,235,.18);
  --green: #16a34a;
  --yellow: #ca8a04;
  --red: #dc2626;
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,'Segoe UI','Microsoft YaHei',sans-serif;background:var(--bg-body);color:var(--text);min-height:100vh;overflow-x:hidden}

.sidebar{position:fixed;left:0;top:0;bottom:0;width:var(--sidebar-w);background:var(--bg-sidebar);border-right:1px solid var(--border);display:flex;flex-direction:column;z-index:100}
.sidebar-logo{padding:18px 20px 14px;border-bottom:1px solid var(--border)}
.sidebar-logo h1{font-size:18px;font-weight:700;background:linear-gradient(135deg,#3b82f6,#8b5cf6);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sidebar-logo small{font-size:11px;color:var(--text-muted)}
.sidebar-nav{flex:1;padding:8px 10px;overflow-y:auto}
.nav-search{padding:0 6px 8px}
.nav-search input{width:100%;padding:6px 10px 6px 28px;background:var(--bg-input);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:11px;outline:none;transition:border .2s}
.nav-search input:focus{border-color:var(--accent)}
.nav-search{position:relative}
.nav-search::before{content:'\26B2';position:absolute;left:14px;top:5px;font-size:12px;color:var(--text-muted);pointer-events:none}
.nav-section{font-size:10px;text-transform:uppercase;letter-spacing:1px;color:var(--text-muted);padding:10px 10px 4px;display:flex;justify-content:space-between;align-items:center;cursor:pointer;user-select:none}
.nav-section:hover{color:var(--text-dim)}
.nav-section .sec-arrow{font-size:8px;transition:transform .2s}
.nav-section.collapsed .sec-arrow{transform:rotate(-90deg)}
.nav-group{overflow:hidden;transition:max-height .25s ease;max-height:1000px}
.nav-group.collapsed{max-height:0}
.nav-item{display:flex;align-items:center;gap:10px;padding:7px 12px;border-radius:8px;font-size:12.5px;cursor:pointer;transition:all .15s;color:var(--text-dim);margin:1px 0}
.nav-item:hover{background:var(--bg-card);color:var(--text)}
.nav-item.active{background:var(--accent);color:#fff;box-shadow:0 2px 10px var(--accent-glow)}
.nav-item .icon{font-size:15px;width:20px;text-align:center}
.nav-item.hidden{display:none}
.sidebar-footer{padding:10px 16px;border-top:1px solid var(--border);font-size:10px;color:var(--text-muted)}

.main{margin-left:var(--sidebar-w);min-height:100vh}
.topbar{height:var(--header-h);background:var(--bg-sidebar);border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 20px;position:sticky;top:0;z-index:50}
.topbar-left{display:flex;align-items:center;gap:12px}
.topbar-left h2{font-size:15px;font-weight:600;margin:0}
.status-pill{display:inline-flex;align-items:center;gap:5px;padding:3px 10px;border-radius:20px;font-size:11px;background:var(--bg-card);border:1px solid var(--border)}
.status-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.status-dot.ok{background:var(--green);box-shadow:0 0 6px var(--green)}
.status-dot.warn{background:var(--yellow);box-shadow:0 0 6px var(--yellow)}
.status-dot.err{background:var(--red);box-shadow:0 0 6px var(--red)}
.topbar-right{display:flex;align-items:center;gap:8px;font-size:11px;color:var(--text-muted)}
.topbar-right button{height:28px;min-width:28px}

.page{display:none;padding:20px 24px}
.page.active{display:block}

.stats-row{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:14px;margin-bottom:20px}
.stat-card{background:var(--bg-card);border-radius:12px;padding:18px 20px;border:1px solid var(--border);position:relative;overflow:hidden}
.stat-card::before{content:'';position:absolute;top:0;left:0;right:0;height:3px}
.stat-card.blue::before{background:linear-gradient(90deg,#3b82f6,#60a5fa)}
.stat-card.green::before{background:linear-gradient(90deg,#22c55e,#4ade80)}
.stat-card.purple::before{background:linear-gradient(90deg,#8b5cf6,#a78bfa)}
.stat-card.orange::before{background:linear-gradient(90deg,#f97316,#fb923c)}
.stat-card .stat-num{font-size:28px;font-weight:700;margin-bottom:4px}
.stat-card.blue .stat-num{color:#60a5fa}
.stat-card.green .stat-num{color:#4ade80}
.stat-card.purple .stat-num{color:#a78bfa}
.stat-card.orange .stat-num{color:#fb923c}
.stat-card .stat-label{font-size:12px;color:var(--text-muted)}
.stat-card[onclick]{transition:transform .15s,box-shadow .15s}
.stat-card[onclick]:hover{transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.3)}
.stat-card .stat-hint{font-size:10px;color:var(--text-muted);margin-top:6px;opacity:.6;transition:opacity .15s}
.stat-card[onclick]:hover .stat-hint{opacity:1}
.action-card{background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;cursor:pointer;transition:all .2s;display:flex;flex-direction:column;align-items:center;text-align:center;gap:6px}
.action-card:hover{border-color:var(--accent);transform:translateY(-3px);box-shadow:0 6px 20px rgba(0,0,0,.3)}
.action-card:active{transform:scale(.97)}
.action-icon{width:42px;height:42px;border-radius:12px;display:flex;align-items:center;justify-content:center;font-size:20px;color:#fff}
.action-label{font-size:13px;font-weight:600;color:var(--text-main)}
.action-desc{font-size:10px;color:var(--text-muted)}

.device-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:14px}
.dev-card{background:var(--bg-card);border-radius:12px;border:1px solid var(--border);overflow:hidden;transition:all .2s;cursor:pointer}
.dev-card:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.3)}
.dev-card-top{padding:14px 16px;display:flex;align-items:center;gap:12px}
.dev-avatar{width:42px;height:42px;border-radius:10px;display:flex;align-items:center;justify-content:center;font-size:16px;font-weight:700;color:#fff;flex-shrink:0}
.dev-avatar.on{background:linear-gradient(135deg,#22c55e,#16a34a)}
.dev-avatar.off{background:#475569}
.dev-avatar.busy{background:linear-gradient(135deg,#eab308,#ca8a04)}
.dev-info{flex:1;min-width:0}
.dev-info .dev-name{font-size:14px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.dev-info .dev-serial{font-size:10px;color:var(--text-muted);font-family:'Courier New',monospace}
.dev-card-bottom{padding:8px 16px 12px;display:flex;align-items:center;justify-content:space-between;border-top:1px solid var(--border)}
.dev-status{font-size:12px;display:flex;align-items:center;gap:5px}
.dev-actions{display:flex;gap:4px}
.dev-btn{padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:transparent;color:var(--text-dim);font-size:11px;cursor:pointer;transition:all .15s}
.dev-btn:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.dev-btn.screen{background:var(--accent);border-color:var(--accent);color:#fff}
.dev-btn.screen:hover{background:#2563eb}

.quick-actions{display:flex;flex-wrap:wrap;gap:8px;margin-bottom:20px}
.qa-btn{padding:4px 12px;border-radius:6px;border:1px solid var(--border);background:var(--bg-card);color:var(--text-main);font-size:11px;cursor:pointer;transition:all .15s;display:inline-flex;align-items:center;gap:4px;height:28px;white-space:nowrap}
.qa-btn:hover{border-color:var(--accent);background:var(--bg-card-hover)}

.chat-layout{display:grid;grid-template-columns:1fr 360px;gap:16px;height:calc(100vh - var(--header-h) - 40px)}
.chat-main{display:flex;flex-direction:column;background:var(--bg-card);border-radius:12px;border:1px solid var(--border);overflow:hidden}
.chat-header{padding:14px 18px;border-bottom:1px solid var(--border);font-size:14px;font-weight:600;display:flex;align-items:center;justify-content:space-between}
.chat-messages{flex:1;overflow-y:auto;padding:16px 18px}
.chat-msg{margin-bottom:12px;max-width:85%;animation:msgIn .25s ease}
@keyframes msgIn{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}
.chat-msg.user{margin-left:auto}
.chat-bubble{padding:10px 14px;border-radius:12px;font-size:13px;line-height:1.6;word-break:break-word}
.chat-msg.user .chat-bubble{background:var(--accent);color:#fff;border-bottom-right-radius:4px}
.chat-msg.bot .chat-bubble{background:var(--bg-input);border:1px solid var(--border);border-bottom-left-radius:4px}
.chat-meta{font-size:10px;color:var(--text-muted);margin-top:4px}
.chat-msg.user .chat-meta{text-align:right}
.chat-input-area{padding:12px 16px;border-top:1px solid var(--border);display:flex;gap:8px}
.chat-input-area input{flex:1;background:var(--bg-input);border:1px solid var(--border);border-radius:10px;padding:10px 14px;color:var(--text);font-size:13px;outline:none;transition:border .2s}
.chat-input-area input:focus{border-color:var(--accent)}
.chat-input-area button{background:var(--accent);color:#fff;border:none;border-radius:10px;padding:10px 20px;cursor:pointer;font-size:13px;font-weight:600;transition:all .15s}
.chat-input-area button:hover{background:#2563eb}
.chat-input-area button:disabled{background:#475569;cursor:wait}
.chat-hints{background:var(--bg-card);border-radius:12px;border:1px solid var(--border);overflow-y:auto}
.chat-hints-header{padding:12px 16px;border-bottom:1px solid var(--border);font-size:13px;font-weight:600}
.hint-list{padding:8px}
.hint-item{padding:8px 12px;border-radius:8px;font-size:12px;color:var(--text-dim);cursor:pointer;transition:all .1s;margin-bottom:2px}
.hint-item:hover{background:var(--bg-card-hover);color:var(--text)}
.hint-item .hint-cmd{color:var(--accent);font-weight:500}
.hint-item .hint-desc{color:var(--text-muted);margin-left:6px}

.task-table{width:100%;border-collapse:collapse}
.task-table th{text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px;color:var(--text-muted);padding:10px 14px;border-bottom:1px solid var(--border)}
.task-table td{padding:10px 14px;border-bottom:1px solid rgba(51,65,85,.5);font-size:13px;vertical-align:middle}
.task-table tr:hover{background:var(--bg-card-hover)}
.badge{padding:3px 10px;border-radius:20px;font-size:11px;font-weight:600}
.badge-running{background:rgba(59,130,246,.15);color:#60a5fa}
.badge-completed{background:rgba(34,197,94,.12);color:#4ade80}
.badge-failed{background:rgba(239,68,68,.12);color:#f87171}
.badge-pending{background:rgba(148,163,184,.1);color:#94a3b8}
.badge-cancelled{background:rgba(148,163,184,.1);color:#64748b}

/* ── Screen Mirror ── */
.screen-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(220px,1fr));gap:14px}
.screen-grid.sz-sm{grid-template-columns:repeat(auto-fill,minmax(140px,1fr));gap:8px}
.screen-grid.sz-sm .scr-footer{padding:4px 6px}
.screen-grid.sz-sm .scr-label{font-size:10px}
.screen-grid.sz-sm .scr-badge{font-size:11px;padding:2px 6px}
.screen-grid.sz-lg{grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px}
.sz-btn{background:transparent;color:var(--text-muted);border:none;border-radius:4px;padding:4px 10px;font-size:11px;cursor:pointer;transition:all .15s;height:24px;line-height:16px}
.sz-btn:hover{color:var(--text-main);background:rgba(255,255,255,.08)}
.sz-btn.active{background:var(--accent);color:#fff}
.multi-stream-grid{display:grid;gap:4px;background:#000;border-radius:8px;overflow:hidden}
.multi-stream-grid.g2x2{grid-template-columns:1fr 1fr}
.multi-stream-grid.g3x3{grid-template-columns:1fr 1fr 1fr}
.multi-stream-grid.g4x4{grid-template-columns:1fr 1fr 1fr 1fr}
.ms-cell{position:relative;aspect-ratio:9/16;background:#111;overflow:hidden}
.ms-cell video,.ms-cell img{width:100%;height:100%;object-fit:contain}
.ms-cell .ms-label{position:absolute;bottom:4px;left:4px;background:rgba(0,0,0,.7);color:#fff;font-size:10px;padding:2px 6px;border-radius:4px}
.scr-card{background:var(--bg-card);border-radius:12px;border:1px solid var(--border);overflow:hidden;cursor:pointer;transition:all .2s;position:relative}
.scr-card:hover{border-color:var(--accent);transform:translateY(-2px);box-shadow:0 4px 16px rgba(0,0,0,.3)}
.scr-card .scr-img{width:100%;aspect-ratio:9/16;object-fit:cover;background:#000;display:block;transition:opacity .3s ease}
.scr-card .scr-placeholder{width:100%;aspect-ratio:9/16;display:flex;align-items:center;justify-content:center;background:#000;color:var(--text-muted);font-size:13px}
.scr-card .scr-footer{padding:8px 12px;display:flex;align-items:center;justify-content:space-between}
.scr-card .scr-label{font-size:13px;font-weight:600;display:flex;align-items:center;gap:6px}
.scr-card .scr-st{font-size:11px;color:var(--text-muted)}
.scr-badge{position:absolute;top:8px;left:8px;background:rgba(59,130,246,.85);color:#fff;font-weight:800;font-size:16px;padding:4px 10px;border-radius:8px;z-index:5;text-shadow:0 1px 2px rgba(0,0,0,.4);min-width:30px;text-align:center}
.scr-health{position:absolute;top:8px;right:8px;z-index:5;font-size:11px;font-weight:700;padding:3px 7px;border-radius:6px;min-width:28px;text-align:center}
.scr-health.good{background:rgba(34,197,94,.85);color:#fff}
.scr-health.warn{background:rgba(234,179,8,.85);color:#000}
.scr-health.bad{background:rgba(239,68,68,.85);color:#fff}
.scr-health.offline{background:rgba(100,116,139,.7);color:#fff}
.scr-recovering{position:absolute;bottom:40px;left:0;right:0;text-align:center;z-index:5;padding:4px;font-size:10px;font-weight:600;color:#fbbf24;background:rgba(0,0,0,.7)}
.scr-card .scr-check{position:absolute;top:8px;right:8px;z-index:6;width:22px;height:22px;accent-color:var(--accent);cursor:pointer;display:none}
.scr-card.group-mode .scr-check{display:block}
.scr-card.selected{border-color:#3b82f6;box-shadow:0 0 0 2px rgba(59,130,246,.5)}
.group-toolbar{display:none;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:8px 14px;margin-bottom:10px;gap:6px;align-items:center;flex-wrap:wrap}
.group-toolbar.active{display:flex}
.group-toolbar .grp-btn{background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:4px 10px;font-size:11px;cursor:pointer;transition:all .15s;height:28px;white-space:nowrap}
.group-toolbar .grp-btn:hover{border-color:var(--accent);background:rgba(59,130,246,.1)}
.group-toolbar .grp-btn.danger{color:#ef4444;border-color:rgba(239,68,68,.3)}
.group-toolbar .grp-btn.danger:hover{background:rgba(239,68,68,.1)}
.group-toolbar .grp-count{font-size:11px;color:var(--accent);font-weight:600;min-width:60px}
.anomaly-bar{display:none;background:rgba(239,68,68,.1);border:1px solid rgba(239,68,68,.3);border-radius:10px;padding:10px 16px;margin-bottom:14px;gap:8px;flex-wrap:wrap;align-items:center}
.anomaly-bar.active{display:flex}
.anomaly-bar .anomaly-item{background:rgba(239,68,68,.15);border-radius:6px;padding:4px 10px;font-size:11px;display:flex;align-items:center;gap:4px}
.anomaly-bar .anomaly-item.critical{background:rgba(239,68,68,.25);color:#ef4444;font-weight:600}
.anomaly-bar .anomaly-item.warning{background:rgba(234,179,8,.2);color:#eab308}
.anomaly-bar .anomaly-item.info{background:rgba(59,130,246,.15);color:#3b82f6}

/* ── Modal (Interactive Control) ── */
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.85);z-index:200;align-items:center;justify-content:center}
.modal-overlay.open{display:flex}
.modal-box{background:var(--bg-card);border-radius:16px;border:1px solid var(--border);max-width:95vw;width:auto;min-width:700px;max-height:95vh;display:flex;flex-direction:column;overflow:hidden;box-shadow:0 20px 60px rgba(0,0,0,.5);animation:modalIn .2s ease}
@keyframes modalIn{from{opacity:0;transform:scale(.95)}to{opacity:1;transform:none}}
.modal-header{padding:14px 18px;display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--border)}
.modal-header h3{font-size:15px;font-weight:600}
.modal-close{background:none;border:none;color:var(--text-dim);font-size:22px;cursor:pointer;padding:0 4px;line-height:1}
.modal-close:hover{color:var(--text)}
.modal-body{flex:1;overflow:auto;display:flex;flex-direction:column;align-items:center;padding:0;background:#000;position:relative;user-select:none;-webkit-user-select:none}
.modal-body img{max-width:100%;max-height:74vh;border-radius:0;cursor:crosshair;touch-action:none}
.modal-body .loading-text{color:var(--text-muted);padding:40px;font-size:13px}
.modal-footer{padding:8px 14px;border-top:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:6px}
.modal-footer .auto-label{font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:6px}
.modal-footer .auto-label input{accent-color:var(--accent)}
.ctrl-btns{display:flex;gap:4px;flex-wrap:wrap}
.ctrl-btn{background:var(--bg-input);border:1px solid var(--border);color:var(--text);border-radius:6px;padding:4px 10px;font-size:12px;cursor:pointer;transition:all .15s;min-width:34px;text-align:center}
.ctrl-btn:hover{background:var(--accent);border-color:var(--accent);color:#fff}
.ctrl-btn:active{transform:scale(.92)}
.sb-btn{background:var(--bg-card);border:1px solid var(--border);color:var(--text-main);border-radius:6px;padding:5px 2px;font-size:13px;cursor:pointer;transition:all .12s;width:38px;height:32px;display:flex;align-items:center;justify-content:center}
.sb-btn:hover{background:var(--accent);border-color:var(--accent);color:#111;transform:scale(1.08)}
.sb-btn:active{transform:scale(.9)}
.sb-btn2{background:var(--bg-card);border:1px solid var(--border);color:var(--text-main);border-radius:6px;padding:3px 8px;font-size:10px;cursor:pointer;transition:all .12s}
.sb-btn2:hover{background:var(--accent);border-color:var(--accent);color:#111}
.rpanel-tab{flex:1;padding:5px 4px;font-size:10px;background:none;border:none;border-bottom:2px solid transparent;color:var(--text-muted);cursor:pointer;transition:all .15s;text-align:center}
.rpanel-tab:hover{color:var(--text-main)}
.rpanel-tab.active{color:var(--accent);border-bottom-color:var(--accent);font-weight:600}
.rpanel-content{flex:1;overflow:hidden}
.rpanel-content.active{display:flex !important;flex-direction:column}
.fb-item{display:flex;align-items:center;gap:6px;padding:4px 8px;cursor:pointer;border-bottom:1px solid rgba(255,255,255,.04);transition:background .1s}
.fb-item:hover{background:rgba(255,255,255,.06)}
.fb-item .fb-icon{font-size:14px;min-width:18px;text-align:center}
.fb-item .fb-name{flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px}
.fb-item .fb-size{font-size:9px;color:var(--text-muted);font-family:monospace;min-width:50px;text-align:right}
.fb-item .fb-actions{display:none;gap:2px}
.fb-item:hover .fb-actions{display:flex}
.tap-ripple{position:absolute;width:24px;height:24px;border-radius:50%;border:2px solid #3b82f6;background:rgba(59,130,246,.25);pointer-events:none;animation:rippleOut .5s ease forwards}
@keyframes rippleOut{0%{transform:translate(-50%,-50%) scale(.5);opacity:1}100%{transform:translate(-50%,-50%) scale(2.5);opacity:0}}
.swipe-line{position:absolute;pointer-events:none;z-index:10}
.ctrl-hint{font-size:10px;color:var(--text-muted);text-align:center;padding:3px 0;background:rgba(0,0,0,.7);position:absolute;bottom:0;left:0;right:0}
.ctrl-status{position:absolute;top:6px;right:6px;display:flex;gap:4px;z-index:10}
.ctrl-status-dot{width:8px;height:8px;border-radius:50%;background:#22c55e;animation:statusPulse 1.5s infinite}
@keyframes statusPulse{0%,100%{opacity:1}50%{opacity:.4}}
@keyframes spin{from{transform:rotate(0deg)}to{transform:rotate(360deg)}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.swipe-trail{position:absolute;pointer-events:none;z-index:10;border-radius:50%;background:rgba(59,130,246,.3);width:10px;height:10px;animation:trailFade .4s ease forwards}
@keyframes trailFade{to{opacity:0;transform:scale(0.3)}}
.ctrl-info-bar{padding:4px 14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;font-size:11px;color:var(--text-muted);background:var(--bg-card)}
.ctrl-info-bar .coord{font-family:monospace;color:var(--text-dim)}

/* ── 移动端自适应 ── */
@media(max-width:768px){
  .sidebar{position:fixed;z-index:100;transform:translateX(-100%);transition:transform .3s;height:100vh}
  .sidebar.mob-open{transform:translateX(0);box-shadow:4px 0 20px rgba(0,0,0,.5)}
  .main{margin-left:0!important}
  #mob-menu-btn{display:block!important}
  .topbar{padding:8px 10px}
  .device-grid{grid-template-columns:repeat(auto-fill,minmax(160px,1fr))!important;gap:8px!important}
  .stat-cards,.overview-quick-actions{grid-template-columns:repeat(2,1fr)!important}
  .stat-grid{grid-template-columns:repeat(2,1fr)!important}
  .task-table th:nth-child(4),.task-table td:nth-child(4){display:none}
  .ctrl-modal-body{flex-direction:column!important}
  .ctrl-modal-body .ctrl-left-sidebar,.ctrl-modal-body .ctrl-right-panel{width:100%!important;max-height:160px;flex-direction:row!important;overflow-x:auto;overflow-y:hidden}
  .ctrl-modal-body .screen-viewport{min-height:300px}
  #alert-panel{right:4px;left:4px;width:auto}
  .page-header{flex-direction:column;gap:8px}
  .topbar-left h2{font-size:15px}
}
@media(max-width:480px){
  .device-grid{grid-template-columns:1fr!important}
  .stat-cards,.overview-quick-actions{grid-template-columns:1fr!important}
  .action-card{padding:10px}
  .stats-row{grid-template-columns:repeat(2,1fr)!important;gap:6px!important}
  .nav-item span:last-child{display:none}
  .sidebar{width:50px!important}
  .main{margin-left:50px!important}
  .topbar-right{gap:4px!important}
  #user-info{display:none}
  #lang-toggle,#theme-toggle{padding:2px 4px!important;font-size:10px!important}
}
@media(max-width:360px){
  .topbar{padding:6px!important}
  .stat-card{padding:8px!important}
  .stat-num{font-size:16px!important}
}
@media(display-mode:standalone){
  body{padding-top:env(safe-area-inset-top)}
}

/* ── Funnel ── */
.funnel-container{display:flex;flex-direction:column;gap:4px;max-width:700px;margin:0 auto}
.funnel-step{display:flex;align-items:center;gap:14px;padding:12px 18px;border-radius:10px;background:var(--bg-card);border:1px solid var(--border);transition:all .2s}
.funnel-step:hover{border-color:var(--accent);transform:translateX(4px)}
.funnel-bar-wrap{flex:1;height:28px;background:var(--bg-input);border-radius:6px;overflow:hidden;position:relative}
.funnel-bar{height:100%;border-radius:6px;transition:width .8s ease;min-width:2px}
.funnel-bar-label{position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:11px;color:var(--text);font-weight:600}
.funnel-step-label{min-width:80px;font-size:13px;font-weight:600;text-align:right}
.funnel-step-count{min-width:50px;font-size:15px;font-weight:700;text-align:right}
.funnel-rate{min-width:55px;font-size:12px;color:var(--text-muted);text-align:right}
.funnel-colors-1{background:linear-gradient(90deg,#3b82f6,#60a5fa)}
.funnel-colors-2{background:linear-gradient(90deg,#8b5cf6,#a78bfa)}
.funnel-colors-3{background:linear-gradient(90deg,#06b6d4,#22d3ee)}
.funnel-colors-4{background:linear-gradient(90deg,#f59e0b,#fbbf24)}
.funnel-colors-5{background:linear-gradient(90deg,#10b981,#34d399)}
.funnel-colors-6{background:linear-gradient(90deg,#ec4899,#f472b6)}
.funnel-colors-7{background:linear-gradient(90deg,#22c55e,#4ade80)}

.funnel-stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-top:20px}
.f-stat{background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:16px;text-align:center}
.f-stat-num{font-size:24px;font-weight:700;margin-bottom:4px}
.f-stat-label{font-size:11px;color:var(--text-muted)}

/* ── Log Panel ── */
.log-panel{background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:4px 0;max-height:calc(100vh - 180px);overflow-y:auto;font-family:'Cascadia Code','Fira Code','Courier New',monospace;font-size:12px}
.log-entry{padding:4px 14px;border-bottom:1px solid rgba(51,65,85,.3);display:flex;gap:10px;line-height:1.5}
.log-entry:hover{background:var(--bg-card-hover)}
.log-ts{color:var(--text-muted);white-space:nowrap;min-width:70px}
.log-lv{font-weight:700;min-width:60px}
.log-lv.INFO{color:#60a5fa}.log-lv.WARNING{color:#fbbf24}.log-lv.ERROR{color:#f87171}.log-lv.DEBUG{color:#94a3b8}
.log-src{color:var(--text-muted);min-width:120px;max-width:180px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.log-msg{color:var(--text);flex:1;word-break:break-word}
.log-ctx{color:var(--accent);font-size:11px;white-space:nowrap}
.log-filter.active{border-color:var(--accent)!important;color:var(--accent)}

/* ── Progress Bar ── */
.progress-bar{width:100%;height:6px;background:var(--bg-input);border-radius:3px;overflow:hidden;margin-top:4px}
.progress-fill{height:100%;background:linear-gradient(90deg,#3b82f6,#60a5fa);border-radius:3px;transition:width .5s ease}
.progress-text{font-size:10px;color:var(--text-muted);margin-top:2px}

/* ── Toast Notification ── */
.toast-container{position:fixed;top:68px;right:24px;z-index:9999;display:flex;flex-direction:column;gap:8px;pointer-events:none}
.toast{pointer-events:auto;padding:10px 18px 10px 14px;border-radius:10px;font-size:12.5px;color:#fff;box-shadow:0 4px 20px rgba(0,0,0,.4);display:flex;align-items:center;gap:10px;animation:toastIn .3s ease,toastOut .4s ease 3.6s forwards;min-width:240px;max-width:400px}
.toast.success{background:linear-gradient(135deg,#059669,#10b981)}
.toast.error{background:linear-gradient(135deg,#dc2626,#ef4444)}
.toast.warn{background:linear-gradient(135deg,#d97706,#f59e0b)}
.toast.info{background:linear-gradient(135deg,#2563eb,#3b82f6)}
.toast .t-icon{font-size:18px;flex-shrink:0}
.toast .t-msg{flex:1;line-height:1.4}
.toast .t-close{cursor:pointer;font-size:16px;opacity:.7}
.toast .t-close:hover{opacity:1}
@keyframes toastIn{from{opacity:0;transform:translateX(40px)}to{opacity:1;transform:none}}
@keyframes toastOut{to{opacity:0;transform:translateX(40px)}}
/* ── Kbd hint ── */
.kbd{display:inline-block;padding:1px 5px;font-size:10px;font-family:monospace;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text-muted);line-height:1.4}
::-webkit-scrollbar{width:6px}
::-webkit-scrollbar-track{background:transparent}
::-webkit-scrollbar-thumb{background:var(--border);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:#475569}

@media(max-width:900px){
  .sidebar{width:60px}.sidebar-logo small,.nav-item span,.sidebar-footer span,.nav-search,.nav-section,.sec-arrow{display:none}
  .sidebar-logo h1{font-size:14px}.main{margin-left:60px}
  .chat-layout{grid-template-columns:1fr}.chat-hints{display:none}
  .stats-row{grid-template-columns:repeat(2,1fr)}
  .kbd{display:none}
  .toast-container{right:12px;left:12px}
  .toast{min-width:auto}
}
"""
