# -*- coding: utf-8 -*-
"""
OpenClaw Web Dashboard — 一站式控制面板。

访问 http://localhost:<端口>/dashboard（默认端口见 src/openclaw_env.py）
"""

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

router = APIRouter()

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
<meta name="theme-color" content="#0b1120">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<link rel="manifest" href="/manifest.json">
<link rel="icon" href="/icon-192.svg" type="image/svg+xml">
<title>OpenClaw 控制中心</title>
<link rel="stylesheet" href="/static/css/dashboard.css?v=20260418k">
</head>
<body>

<aside class="sidebar">
  <div class="sidebar-logo"><h1>OpenClaw</h1><small>智能群控中心</small></div>
  <nav class="sidebar-nav">
    <div class="nav-search"><input id="nav-search" placeholder="搜索功能..." oninput="_filterNav(this.value)"/></div>

    <!-- 客服中心置顶 (PR-6.5+) — 确保用户第一屏就看到, 不被滚动条吞掉 -->
    <div class="nav-section" onclick="_toggleSection(this)" data-cs-section="1" style="background:linear-gradient(135deg,rgba(168,85,247,.18),rgba(96,165,250,.18));border-left:3px solid #a855f7">
      &#129309; 客服中心
      <span id="cs-pending-badge" style="display:none;margin-left:6px;font-size:10px;padding:1px 6px;background:#ef4444;color:#fff;border-radius:8px;font-weight:600">0</span>
      <span class="sec-arrow">&#9660;</span>
    </div>
    <div class="nav-group" data-cs-group="1">
      <div class="nav-item" onclick="if(window.lmOpenMyDesk)lmOpenMyDesk()" style="font-weight:600;background:rgba(168,85,247,.08)"><span class="icon">&#128100;</span><span>我的工作台</span></div>
      <div class="nav-item" onclick="if(window.lmOpenHandoffInbox)lmOpenHandoffInbox('')" style="font-weight:500"><span class="icon">&#128229;</span><span>待接管队列</span></div>
      <div class="nav-item" onclick="if(window.lmOpenLeadSearch)lmOpenLeadSearch()"><span class="icon">&#128270;</span><span>客户搜索</span></div>
      <div class="nav-item" onclick="if(window.lmOpenCommandCenter)lmOpenCommandCenter()"><span class="icon">&#128290;</span><span>命令中心</span></div>
      <div class="nav-item" onclick="window.open('/static/l2-dashboard.html','_blank')"><span class="icon">&#128202;</span><span>L2 客户漏斗看板</span></div>
    </div>

    <div class="nav-section" onclick="_toggleSection(this)">核心 <span class="sec-arrow">&#9660;</span></div>
    <div class="nav-group">
      <div class="nav-item active" data-page="overview"><span class="icon">&#9632;</span><span data-i18n="overview">总览</span></div>
      <div class="nav-item" data-page="devices"><span class="icon">&#9783;</span><span data-i18n="devices">设备管理</span></div>
      <div class="nav-item" data-page="chat"><span class="icon">&#9993;</span><span>AI 指令</span></div>
      <div class="nav-item" data-page="tasks"><span class="icon">&#9881;</span><span data-i18n="tasks">任务中心</span></div>
      <div class="nav-item" data-page="vpn-manage"><span class="icon">&#128274;</span><span>VPN 管理</span></div>
      <div class="nav-item" data-page="router-manage"><span class="icon">&#128225;</span><span>代理中心</span></div>
      <div class="nav-item" data-page="screens"><span class="icon">&#9707;</span><span data-i18n="screen-monitor">屏幕监控</span></div>
      <div class="nav-item" data-page="multi-screen"><span class="icon">&#128187;</span><span>多屏操控</span></div>
      <div class="nav-item" data-page="groups"><span class="icon">&#127991;</span><span>设备分组</span></div>
    </div>

    <div class="nav-section" onclick="_toggleSection(this)">平台 <span class="sec-arrow">&#9660;</span></div>
    <div class="nav-group">
      <div class="nav-item" data-page="plat-tiktok"><span class="icon">&#127916;</span><span>TikTok</span></div>
      <div class="nav-item" data-page="plat-telegram"><span class="icon">&#9992;</span><span>Telegram</span></div>
      <div class="nav-item" data-page="plat-whatsapp"><span class="icon">&#128172;</span><span>WhatsApp</span></div>
      <div class="nav-item" data-page="plat-facebook"><span class="icon">&#128101;</span><span>Facebook</span></div>
      <div class="nav-item" data-page="plat-linkedin"><span class="icon">&#128188;</span><span>LinkedIn</span></div>
      <div class="nav-item" data-page="plat-instagram"><span class="icon">&#128247;</span><span>Instagram</span></div>
      <div class="nav-item" data-page="plat-twitter"><span class="icon">&#120143;</span><span>X (Twitter)</span></div>
    </div>

    <div class="nav-section collapsed" onclick="_toggleSection(this)">自动化 <span class="sec-arrow">&#9660;</span></div>
    <div class="nav-group collapsed">
      <div class="nav-item" data-page="workflows"><span class="icon">&#9881;</span><span>工作流</span></div>
      <div class="nav-item" data-page="visual-workflow"><span class="icon">&#128736;</span><span>可视化工作流</span></div>
      <div class="nav-item" data-page="script-engine"><span class="icon">&#128221;</span><span>脚本模板</span></div>
      <div class="nav-item" data-page="ai-script"><span class="icon">&#129302;</span><span>AI脚本生成</span></div>
      <div class="nav-item" data-page="scheduled-jobs"><span class="icon">&#9200;</span><span>定时任务</span></div>
      <div class="nav-item" data-page="quick-actions"><span class="icon">&#9889;</span><span>批量快捷操作</span></div>
      <div class="nav-item" data-page="sync-mirror"><span class="icon">&#128260;</span><span>同步镜像</span></div>
      <div class="nav-item" data-page="op-replay"><span class="icon">&#9654;</span><span>操作回放</span></div>
    </div>

    <div class="nav-section collapsed" onclick="_toggleSection(this)">工具 <span class="sec-arrow">&#9660;</span></div>
    <div class="nav-group collapsed">
      <div class="nav-item" data-page="batch-apk"><span class="icon">&#128230;</span><span>批量安装APK</span></div>
      <div class="nav-item" data-page="batch-text"><span class="icon">&#9997;</span><span>批量文字输入</span></div>
      <div class="nav-item" data-page="batch-upload"><span class="icon">&#128228;</span><span>批量文件上传</span></div>
      <div class="nav-item" data-page="app-manager"><span class="icon">&#128187;</span><span>应用管理器</span></div>
      <!-- phrases page removed: duplicate of 获客/messages, no backend implementation -->
      <div class="nav-item" data-page="screen-record"><span class="icon">&#127909;</span><span>录屏管理</span></div>
    </div>


    <div class="nav-section collapsed" onclick="_toggleSection(this)">数据 <span class="sec-arrow">&#9660;</span></div>
    <div class="nav-group collapsed">
      <div class="nav-item" data-page="analytics"><span class="icon">&#128202;</span><span>数据分析</span></div>
      <div class="nav-item" data-page="roi"><span class="icon">&#128176;</span><span>ROI 面板</span></div>
      <div class="nav-item" data-page="funnel"><span class="icon">&#128200;</span><span>转化漏斗</span></div>
      <div class="nav-item" data-page="data-export"><span class="icon">&#128229;</span><span>数据导出</span></div>
      <div class="nav-item" data-page="op-timeline"><span class="icon">&#128336;</span><span>操作时间线</span></div>
      <div class="nav-item" data-page="audit"><span class="icon">&#128221;</span><span>审计日志</span></div>
    </div>

    <div class="nav-section collapsed" onclick="_toggleSection(this)">监控 <span class="sec-arrow">&#9660;</span></div>
    <div class="nav-group collapsed">
      <div class="nav-item" data-page="health"><span class="icon">&#9829;</span><span>设备健康</span></div>
      <div class="nav-item" data-page="health-report"><span class="icon">&#128203;</span><span>健康报告</span></div>
      <div class="nav-item" data-page="perf-monitor"><span class="icon">&#128200;</span><span>性能监控</span></div>
      <div class="nav-item" data-page="logs"><span class="icon">&#128220;</span><span>系统日志</span></div>
      <div class="nav-item" data-page="notifications"><span class="icon">&#128276;</span><span>消息通知</span></div>
      <div class="nav-item" data-page="alert-rules"><span class="icon">&#9888;</span><span>告警规则</span></div>
      <div class="nav-item" data-page="device-assets"><span class="icon">&#128179;</span><span>设备资产</span></div>
    </div>

    <div class="nav-section" onclick="_toggleSection(this)">&#127912; 内容工作室 <span class="sec-arrow">&#9660;</span></div>
    <div class="nav-group">
      <div class="nav-item" data-page="studio"><span class="icon">&#127775;</span><span>工作台</span></div>
    </div>

    <div class="nav-section collapsed" onclick="_toggleSection(this)" data-admin-only="1">系统 <span class="sec-arrow">&#9660;</span></div>
    <div class="nav-group collapsed" data-admin-only="1">
      <div class="nav-item" data-page="cluster"><span class="icon">&#9741;</span><span>集群管理</span></div>
      <div class="nav-item" data-page="notify-center"><span class="icon">&#128276;</span><span>通知中心</span></div>
      <div class="nav-item" data-page="backup"><span class="icon">&#128190;</span><span>备份恢复</span></div>
      <div class="nav-item" data-page="plugins"><span class="icon">&#128268;</span><span>插件管理</span></div>
      <div class="nav-item" data-page="tpl-market"><span class="icon">&#127970;</span><span>模板市场</span></div>
      <div class="nav-item" data-page="user-mgmt"><span class="icon">&#128101;</span><span>用户管理</span></div>
      <div class="nav-item" onclick="window.open('/docs','_blank')"><span class="icon">&#128196;</span><span>API 文档</span></div>
    </div>

    <div class="nav-group">
      <div class="nav-item" onclick="doLogout()"><span class="icon">&#128682;</span><span>退出登录</span></div>
    </div>

    <script>
    /* 待接管 badge 自动更新 — 30 秒一次 */
    async function _updateCsBadge() {
      try {
        const r = await fetch('/lead-mesh/handoffs?state=pending&limit=1', {
          headers: {'Authorization': 'Bearer ' + (localStorage.getItem('oc_token') || '')}
        });
        if (!r.ok) return;
        const d = await r.json();
        const list = (d && d.handoffs) || [];
        // 拉准数: 再调一次拿全 count (limit=200 估足够)
        const r2 = await fetch('/lead-mesh/handoffs?state=pending&limit=200', {
          headers: {'Authorization': 'Bearer ' + (localStorage.getItem('oc_token') || '')}
        });
        const d2 = await r2.json();
        const total = ((d2 && d2.handoffs) || []).length;
        const badges = [
          document.getElementById('cs-pending-badge'),
          document.getElementById('ov-cs-pending-badge'),
        ];
        const display = total > 0 ? '' : 'none';
        const text = total > 99 ? '99+' : String(total);
        badges.forEach(function(b) {
          if (b) { b.style.display = display; b.textContent = text; }
        });
      } catch (e) {}
    }
    setInterval(_updateCsBadge, 30000);
    setTimeout(_updateCsBadge, 1500);  // 启动 1.5s 后第一次拉

    /* Phase-2: SSE 实时事件订阅 + 桌面通知 */
    (function _subscribeEvents() {
      try {
        if (typeof EventSource === 'undefined') return;
        const es = new EventSource('/lead-mesh/events/stream');
        es.addEventListener('hello', function (e) {
          console.log('[SSE] connected', e.data);
        });
        es.addEventListener('handoff_assigned', function (e) {
          try {
            const d = JSON.parse(e.data);
            const p = d.payload || {};
            _toast('🙋 ' + (p.by || '?') + ' 接走客户 ' + (p.peer_name || p.handoff_id.substring(0,8)), '#a855f7');
            _updateCsBadge();
            _maybeDesktopNotify('客户被接管', (p.by || '') + ' → ' + (p.peer_name || ''));
          } catch (err) {}
        });
        es.addEventListener('handoff_outcome', function (e) {
          try {
            const d = JSON.parse(e.data);
            const p = d.payload || {};
            const emoji = p.outcome === 'converted' ? '✅' : p.outcome === 'lost' ? '❌' : '⏳';
            _toast(emoji + ' ' + (p.by || '?') + ' 标 ' + (p.peer_name || '?') + ' 为 ' + (p.outcome || '?'), '#22c55e');
            _updateCsBadge();
          } catch (err) {}
        });
        /* Phase-3: 客户回了消息 → 检查是否我接管中, 是则桌面通知 */
        es.addEventListener('chat_inbound', async function (e) {
          try {
            const d = JSON.parse(e.data);
            const p = d.payload || {};
            const me = localStorage.getItem('oc_user') || localStorage.getItem('oc_cs_id') || '';
            if (!me) return;
            // 异步查我是不是接管中此客户
            const r = await fetch('/lead-mesh/handoffs/assigned/' + encodeURIComponent(me),
              { headers: { 'Authorization': 'Bearer ' + (localStorage.getItem('oc_token') || '') } });
            if (!r.ok) return;
            const data = await r.json();
            const isMine = (data.handoffs || []).some(h => h.canonical_id === p.customer_id);
            if (isMine) {
              _toast('💬 客户 ' + (p.peer_name || '?') + ' 回了一条 ' + p.content_lang + ' 消息 (' + p.content_len + '字)', '#06b6d4');
              _maybeDesktopNotify('客户回消息了', (p.peer_name || '客户') + ' · ' + p.channel);
            }
          } catch (err) { console.warn('chat_inbound:', err); }
        });
        es.onerror = function () {
          // EventSource 自动重连, 不需要手动处理
        };
      } catch (e) { console.warn('[SSE] init failed:', e); }
    })();

    function _toast(msg, color) {
      const t = document.createElement('div');
      t.style.cssText = 'position:fixed;bottom:20px;right:20px;'
        + 'background:rgba(15,23,42,.95);border:1px solid ' + (color || '#475569')
        + ';color:' + (color || '#e2e8f0') + ';padding:12px 20px;border-radius:10px;'
        + 'font-size:13px;z-index:99999;box-shadow:0 8px 30px rgba(0,0,0,.5);'
        + 'animation:slideInRight .3s ease-out';
      t.textContent = msg;
      document.body.appendChild(t);
      setTimeout(function () { t.remove(); }, 4000);
    }

    function _maybeDesktopNotify(title, body) {
      try {
        if (typeof Notification === 'undefined') return;
        if (Notification.permission === 'granted') {
          new Notification(title, { body: body, icon: '/icon-192.svg' });
        } else if (Notification.permission === 'default') {
          Notification.requestPermission();
        }
      } catch (e) {}
    }

    /* Phase-5: 注册 service worker (PWA offline 缓存) */
    if ('serviceWorker' in navigator) {
      window.addEventListener('load', function () {
        navigator.serviceWorker.register('/sw.js', {scope: '/'})
          .then(function (reg) { console.log('[PWA] sw registered, scope:', reg.scope); })
          .catch(function (err) { console.warn('[PWA] sw register failed:', err); });
      });
    }
    </script>
    <script>
    /* PR-6.5: role-based 菜单显隐. customer_service 只看客服中心 + 总览 */
    (function applyRoleMenu(){
      try {
        const role = (localStorage.getItem('oc_role') || '').toLowerCase();
        if (role === 'customer_service') {
          // 隐藏所有 admin-only sections (核心/平台/自动化/数据/监控/系统 等)
          // 但保留 客服中心 (data-cs-section/data-cs-group) + 总览
          document.querySelectorAll('.nav-section').forEach(function(s) {
            const isCs = s.hasAttribute('data-cs-section');
            if (!isCs) s.style.display = 'none';
          });
          document.querySelectorAll('.nav-group').forEach(function(g) {
            const isCs = g.hasAttribute('data-cs-group');
            // 保留客服中心 + 退出登录所在的 group
            const hasLogout = g.querySelector('[onclick*="doLogout"]');
            if (!isCs && !hasLogout) g.style.display = 'none';
          });
          // 保留 总览 nav item (单独, 不隐藏其所在 group 时已隐藏, 单独 reveal 它)
          const ov = document.querySelector('[data-page="overview"]');
          if (ov) {
            ov.style.display = '';
            const ovGroup = ov.closest('.nav-group');
            if (ovGroup) {
              ovGroup.style.display = '';
              // 隐藏同 group 的其它 item
              ovGroup.querySelectorAll('.nav-item').forEach(function(it) {
                if (it !== ov) it.style.display = 'none';
              });
            }
          }
        }
      } catch (e) { console.warn('[role menu] apply failed:', e); }
    })();
    </script>
  </nav>
  <div class="sidebar-footer"><span>OpenClaw v1.2.0</span></div>
</aside>

<div class="toast-container" id="toast-container"></div>
<div class="main">
  <header class="topbar">
    <div class="topbar-left">
      <button id="mob-menu-btn" onclick="document.querySelector('.sidebar').classList.toggle('mob-open')" style="display:none;background:none;border:none;color:var(--text-main);font-size:20px;cursor:pointer;padding:0 8px 0 0">&#9776;</button>
      <h2 id="page-title">总览</h2>
      <div class="status-pill"><span class="status-dot ok" id="h-dot"></span><span id="h-status">连接中...</span></div>
      <div id="node-role-badge" style="display:none;margin-left:6px;font-size:10px;padding:2px 8px;border-radius:4px;font-weight:500"></div>
      <div id="gate-policy-pill" style="display:none;margin-left:6px;font-size:10px;padding:2px 8px;border-radius:10px;font-weight:500;cursor:pointer;border:1px solid var(--border);white-space:nowrap" onclick="_showGatePolicyDetail()" title="任务门禁策略实时状态（点击查看详情并可热加载）"></div>
      <span id="branch-chip" style="display:none;margin-left:6px;font-size:10px;padding:2px 8px;border-radius:10px;font-weight:500;border:1px solid transparent;white-space:nowrap;cursor:help" title="service 当前 git 分支"></span>
      <span class="kbd" style="margin-left:8px" title="搜索导航">Ctrl+K</span>
    </div>
    <div class="topbar-right">
      <span id="h-uptime" style="font-size:11px"></span>
      <span id="h-version" style="font-size:11px;color:var(--text-dim)"></span>
      <span style="width:1px;height:18px;background:var(--border)"></span>
      <span id="user-info" style="font-size:11px;padding:3px 10px;background:var(--bg-card);border:1px solid var(--border);border-radius:6px;cursor:pointer;height:28px;display:inline-flex;align-items:center" onclick="showUserMenu()"></span>
      <button id="theme-toggle" onclick="toggleTheme()" style="background:none;border:1px solid var(--border);border-radius:6px;padding:0 6px;cursor:pointer;font-size:14px;color:var(--text);height:28px" title="切换主题">&#127769;</button>
      <button id="lang-toggle" onclick="toggleLang()" style="background:none;border:1px solid var(--border);border-radius:6px;padding:0 6px;cursor:pointer;font-size:11px;color:var(--text);font-weight:600;height:28px" title="中/EN">中</button>
      <div style="position:relative;cursor:pointer;height:28px;display:flex;align-items:center;padding:0 4px" onclick="toggleAlertPanel()">
        <span style="font-size:16px">&#128276;</span>
        <span id="alert-badge" style="display:none;position:absolute;top:0;right:-2px;background:#ef4444;color:#fff;font-size:8px;font-weight:700;border-radius:50%;width:14px;height:14px;line-height:14px;text-align:center">0</span>
      </div>
      <span id="clock" style="font-size:11px;font-family:monospace;min-width:60px"></span>
    </div>
  </header>
  <script>
  // P3 L3 sibling 协同护栏 — 顶栏 git branch chip
  (function _initBranchChip(){
    fetch('/system/git-branch', {cache: 'no-store'})
      .then(r => r.json())
      .then(d => {
        var el = document.getElementById('branch-chip');
        if (!el || !d || !d.branch) return;
        var isMain = !!d.is_main;
        var ahead = parseInt(d.ahead_of_main || 0, 10);
        var label = d.branch;
        if (!isMain && ahead > 0) label += ' (+' + ahead + ')';
        el.textContent = label;
        el.style.display = 'inline-block';
        el.style.background = isMain ? '#10b981' : '#f59e0b';
        el.style.color = '#fff';
        el.title = 'Git branch: ' + d.branch + (ahead > 0 ? ' — ' + ahead + ' commits ahead of main' : '');
      })
      .catch(function(){});
  })();
  </script>
  <!-- 告警通知面板 -->
  <div id="alert-panel" style="display:none;position:absolute;top:50px;right:20px;width:360px;max-height:480px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;box-shadow:0 12px 40px rgba(0,0,0,.5);z-index:300;overflow:hidden">
    <div style="padding:10px 14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
      <span style="font-weight:600;font-size:13px">通知中心</span>
      <div style="display:flex;gap:6px">
        <button class="sb-btn2" onclick="configureAlerts()" style="font-size:9px">&#9881; 配置</button>
        <button class="sb-btn2" onclick="clearAlerts()" style="font-size:9px">清除</button>
      </div>
    </div>
    <div id="alert-list" style="overflow-y:auto;max-height:400px;padding:4px 0"></div>
    <div id="alert-empty" style="padding:20px;text-align:center;color:var(--text-muted);font-size:12px">暂无通知</div>
  </div>

  <!-- ═══ 总览 ═══ -->
  <div class="page active" id="page-overview">
    <div id="ov-device-alerts" style="display:none;margin-bottom:12px;padding:12px 14px;border-radius:10px;border:1px solid rgba(239,68,68,.45);background:linear-gradient(135deg,rgba(239,68,68,.12),rgba(245,158,11,.08));font-size:12px;line-height:1.5"></div>
    <!-- 统计卡片行 - 核心指标 -->
    <div class="stats-row" style="grid-template-columns:repeat(4,1fr)">
      <div class="stat-card blue" style="cursor:pointer" onclick="navigateToPage('devices')"><div class="stat-num" id="s-online">-</div><div class="stat-label">在线设备</div><div style="font-size:10px;color:var(--text-muted);margin-top:4px">/ <span id="s-total">-</span> 总计</div><div id="s-total-breakdown" style="font-size:9px;color:var(--text-dim);margin-top:3px;line-height:1.25" title="本机=主控ADB/配置行；集群=Worker合并"></div><div class="stat-hint">点击查看设备管理 &rarr;</div></div>
      <div class="stat-card green" style="cursor:pointer" onclick="navigateToPage('tasks')"><div class="stat-num" id="s-tasks">-</div><div class="stat-label">任务总数</div><div style="font-size:10px;margin-top:4px"><span style="color:#4ade80" id="s-success">-</span> 成功 &middot; <span style="color:#f87171" id="s-failed">-</span> 失败</div><div class="stat-hint">点击查看任务管理 &rarr;</div></div>
      <div class="stat-card purple" style="cursor:pointer" onclick="navigateToPage('health')"><div class="stat-num" id="s-avg-bat" style="color:#a78bfa">-</div><div class="stat-label">平均电量</div><div style="font-size:10px;color:var(--text-muted);margin-top:4px"><span style="color:#fb923c" id="s-low-bat">-</span> 低电量</div><div class="stat-hint">点击查看健康监控 &rarr;</div></div>
      <div class="stat-card orange" style="cursor:pointer" onclick="navigateToPage('perf-monitor')"><div class="stat-num" id="s-running" style="color:#22d3ee">-</div><div class="stat-label">运行中任务</div><div style="font-size:10px;color:var(--text-muted);margin-top:4px">内存 <span id="s-avg-mem">-</span> / 高 <span style="color:#f472b6" id="s-high-mem">-</span></div><div class="stat-hint">点击查看性能监控 &rarr;</div></div>
    </div>
    <!-- 集群探针（与 TikTok 页命令条「探针」同源，总览直达） -->
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:14px">
      <div style="font-size:12px;color:var(--text-muted);line-height:1.45">集群节点 OpenAPI 是否与主控一致（<code style="font-size:11px">contacts/enriched</code>）。Worker 需与主控同版代码后此处为 ✓。</div>
      <button type="button" class="qa-btn" onclick="typeof _ttShowContactsEnrichedProbe==='function'&&_ttShowContactsEnrichedProbe()" style="padding:6px 14px;font-size:12px;white-space:nowrap">&#128300; 集群探针</button>
    </div>
    <!-- 今日简报 -->
    <div id="ov-daily-briefing" style="background:linear-gradient(135deg,#1e40af22,#7c3aed18);border:1px solid #3b82f644;border-radius:12px;padding:14px 18px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center">
        <div style="display:flex;align-items:center;gap:10px">
          <span style="font-size:20px">&#9728;</span>
          <div>
            <div style="font-size:13px;font-weight:600">今日简报</div>
            <div style="font-size:12px;color:var(--text-muted);margin-top:2px" id="ov-briefing-text">加载中...</div>
          </div>
        </div>
        <button class="qa-btn" onclick="_loadOverviewBriefing()" style="padding:4px 10px;font-size:11px">&#128260; 刷新</button>
      </div>
      <div id="ops-status-bar" style="font-size:12px;margin-top:6px;display:flex;gap:12px;flex-wrap:wrap"></div>
      <div id="ov-exec-policy" style="font-size:11px;color:var(--text-dim);margin-top:8px;line-height:1.45;display:none;padding:8px 10px;border-radius:8px;background:rgba(99,102,241,.07);border:1px solid rgba(99,102,241,.22)"></div>
    </div>
    <!-- 2026-05-09 P3-2: 自愈 + 链式任务运维状态 -->
    <div id="ov-heal-chain-bar" style="display:none;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px 18px;margin-bottom:16px">
      <div style="font-size:13px;font-weight:600;margin-bottom:10px">&#9889; 自动化运维</div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px" id="ov-heal-grid">
        <div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:18px;font-weight:700;color:#22c55e" id="heal-total">0</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">自愈触发</div>
        </div>
        <div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:18px;font-weight:700;color:#4ade80" id="heal-success">0</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">成功修复</div>
        </div>
        <div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:18px;font-weight:700;color:#60a5fa" id="heal-rate">0%</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">自愈率</div>
        </div>
        <div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:18px;font-weight:700;color:#a78bfa" id="heal-chains">0</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">链运行</div>
        </div>
      </div>
      <div style="margin-top:8px;font-size:11px;color:var(--text-muted);display:flex;gap:14px;flex-wrap:wrap" id="heal-detail-row">
        <span>换IP: <b id="heal-rotate">0</b></span>
        <span>重连: <b id="heal-reconnect">0</b></span>
        <span>重试: <b id="heal-retry">0</b></span>
        <span>近1h: <b id="heal-1h">0</b></span>
        <span>升级隔离: <b id="heal-escalated" style="color:#ef4444">0</b></span>
      </div>
      <div style="margin-top:6px;font-size:11px;color:var(--text-muted);display:none;gap:14px;flex-wrap:wrap" id="heal-effect-row"></div>
      <div id="chain-trend-chart" style="display:none;margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">
        <div style="font-size:11px;font-weight:600;margin-bottom:6px;color:var(--text-muted)">7天链执行趋势</div>
        <div id="chain-trend-bars" style="display:flex;align-items:flex-end;gap:4px;height:50px"></div>
      </div>
      <div id="heal-config-panel" style="display:none;margin-top:10px;padding-top:8px;border-top:1px solid var(--border)">
        <div style="font-size:11px;font-weight:600;margin-bottom:6px;color:var(--text-muted)">自愈策略配置 <span style="font-weight:400;font-size:10px">(热生效)</span></div>
        <div style="display:flex;gap:10px;flex-wrap:wrap;align-items:center;font-size:11px">
          <label>每小时上限 <input id="hc-max-heals" type="number" min="1" max="20" value="3" style="width:48px;padding:3px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg-main);color:var(--text);font-size:11px"></label>
          <label>最低恢复率% <input id="hc-min-rate" type="number" min="5" max="90" value="30" style="width:48px;padding:3px 6px;border:1px solid var(--border);border-radius:4px;background:var(--bg-main);color:var(--text);font-size:11px"></label>
          <label style="display:flex;align-items:center;gap:4px"><input id="hc-enabled" type="checkbox" checked style="accent-color:#8b5cf6"> 启用</label>
          <button class="qa-btn" onclick="_saveHealConfig()" style="padding:3px 10px;font-size:10px;background:#8b5cf6;color:#fff;border-color:#8b5cf6">保存</button>
        </div>
      </div>
    </div>
    <!-- Z2: Lead 摘要卡片 -->
    <div id="ov-lead-summary" style="display:none;background:linear-gradient(135deg,rgba(14,165,233,.08),rgba(139,92,246,.06));border:1px solid rgba(14,165,233,.25);border-radius:12px;padding:14px 18px;margin-bottom:16px;cursor:pointer" onclick="typeof lmOpenIdentityKPI==='function'&&lmOpenIdentityKPI()">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div style="font-size:13px;font-weight:600">&#127760; Lead Mesh 摘要</div>
        <span style="font-size:10px;color:var(--text-dim)">点击查看详情 &rarr;</span>
      </div>
      <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:8px" id="ov-lead-cards"></div>
      <div id="ov-lead-trend" style="margin-top:8px;font-size:10px;color:var(--text-muted)"></div>
    </div>
    <!-- BB5: FB 引流健康度卡片 -->
    <div id="ov-fb-health" style="display:none;background:linear-gradient(135deg,rgba(24,119,242,.08),rgba(59,130,246,.04));border:1px solid rgba(24,119,242,.2);border-radius:12px;padding:14px 18px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div style="font-size:13px;font-weight:600">&#128266; Facebook 引流健康度</div>
        <div style="display:flex;align-items:center;gap:8px">
          <span id="ov-fb-persona-tag" style="font-size:10px;color:var(--text-dim)"></span>
          <span id="ov-fb-score-badge" style="font-size:11px;font-weight:700;padding:2px 8px;border-radius:10px;background:rgba(100,116,139,.15);color:#94a3b8">--</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:10px" id="ov-fb-config-cards"></div>
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:6px" id="ov-fb-funnel-cards"></div>
      <div id="ov-fb-health-actions" style="margin-top:8px;display:flex;gap:8px;justify-content:flex-end"></div>
    </div>
    <!-- AI 状态面板 -->
    <div id="ai-status-panel" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px 18px;margin-bottom:16px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <div style="font-size:13px;font-weight:600">AI 智能引擎</div>
        <div style="display:flex;align-items:center;gap:8px">
          <span class="status-dot" id="ai-status-dot" style="width:8px;height:8px;border-radius:50%;background:#94a3b8;display:inline-block"></span>
          <span id="ai-status-text" style="font-size:11px;color:var(--text-muted)">检测中...</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:8px" id="ai-stats-grid">
        <div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:18px;font-weight:700;color:#a78bfa" id="ai-total-calls">-</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">LLM 调用总次数</div>
        </div>
        <div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:18px;font-weight:700;color:#4ade80" id="ai-cache-rate">-</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">缓存命中率</div>
        </div>
        <div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:18px;font-weight:700;color:#60a5fa" id="ai-rewrites">-</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">AI改写条数</div>
        </div>
        <div style="background:var(--bg-main);border-radius:8px;padding:10px;text-align:center">
          <div style="font-size:18px;font-weight:700;color:#fb923c" id="ai-auto-replies">-</div>
          <div style="font-size:10px;color:var(--text-muted);margin-top:2px">自动回复条数</div>
        </div>
      </div>
      <div style="margin-top:8px;font-size:11px;color:var(--text-muted);display:flex;gap:16px;flex-wrap:wrap" id="ai-detail-row">
        <span>提供商: <span id="ai-provider" style="color:var(--text)">-</span></span>
        <span>今日Token: <span id="ai-tokens" style="color:var(--text)">-</span></span>
        <span>预估费用: <span id="ai-cost" style="color:var(--text)">-</span></span>
        <span>错误率: <span id="ai-error-rate" style="color:var(--text)">-</span></span>
      </div>
    </div>
    <!-- 一键操作中心 (精简版：仅保留真实有效的批量操作) -->
    <h3 style="font-size:14px;margin-bottom:10px;color:var(--text-dim)">一键操作</h3>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(155px,1fr));gap:10px;margin-bottom:16px">
      <!-- 客服中心快捷入口 (高优先级 - 紫色边框突出) -->
      <div class="action-card" onclick="if(window.lmOpenHandoffInbox)lmOpenHandoffInbox('')" style="border:2px solid #a855f7;background:linear-gradient(135deg,rgba(168,85,247,.1),rgba(96,165,250,.05))">
        <div class="action-icon" style="background:linear-gradient(135deg,#a855f7,#6366f1);position:relative">
          &#128229;
          <span id="ov-cs-pending-badge" style="display:none;position:absolute;top:-6px;right:-6px;font-size:10px;padding:2px 6px;background:#ef4444;color:#fff;border-radius:8px;font-weight:600">0</span>
        </div>
        <div class="action-label" style="color:#a855f7;font-weight:600">客服接管队列</div>
        <div class="action-desc">真人接客户 &middot; 标成交</div>
      </div>
      <div class="action-card" onclick="window.open('/static/l2-dashboard.html','_blank')" style="border:1px solid rgba(96,165,250,.3)">
        <div class="action-icon" style="background:linear-gradient(135deg,#3b82f6,#06b6d4)">&#128202;</div>
        <div class="action-label">L2 客户漏斗</div>
        <div class="action-desc">运营看板 &middot; 实时刷</div>
      </div>
      <div class="action-card" onclick="batchTask('tiktok_check_inbox',{auto_reply:true,max_conversations:20})">
        <div class="action-icon" style="background:linear-gradient(135deg,#06b6d4,#3b82f6)">&#128172;</div>
        <div class="action-label">立即收件箱</div>
        <div class="action-desc">所有设备 &middot; AI自动回复</div>
      </div>
      <div class="action-card" onclick="batchTask('tiktok_follow',{target_country:'italy',max_follows:30})">
        <div class="action-icon" style="background:linear-gradient(135deg,#3b82f6,#2dd4bf)">&#128101;</div>
        <div class="action-label">立即关注</div>
        <div class="action-desc">意大利目标 &middot; 每设备30人</div>
      </div>
      <div class="action-card" onclick="batchTask('tiktok_check_and_chat_followbacks',{max_chats:10})">
        <div class="action-icon" style="background:linear-gradient(135deg,#22c55e,#16a34a)">&#128640;</div>
        <div class="action-label">跟进回关</div>
        <div class="action-desc">检测回关 &middot; AI发送DM</div>
      </div>
      <div class="action-card" onclick="batchTask('tiktok_warmup',{watch_seconds:30,do_like:true})" style="border-color:rgba(168,85,247,.3)">
        <div class="action-icon" style="background:linear-gradient(135deg,#8b5cf6,#6366f1)">&#127916;</div>
        <div class="action-label">账号预热</div>
        <div class="action-desc">刷视频+点赞 &middot; 养号</div>
      </div>
      <div class="action-card" onclick="fixAllOffline()" style="border-color:rgba(234,179,8,.3)">
        <div class="action-icon" style="background:linear-gradient(135deg,#eab308,#f59e0b)">&#128295;</div>
        <div class="action-label">修复离线</div>
        <div class="action-desc">重连所有离线设备</div>
      </div>
      <div class="action-card" onclick="cancelAllTasks()" style="border-color:rgba(239,68,68,.3)">
        <div class="action-icon" style="background:linear-gradient(135deg,#6b7280,#4b5563)">&#9209;</div>
        <div class="action-label">取消全部任务</div>
        <div class="action-desc">停止所有运行中任务</div>
      </div>
    </div>
    <!-- 人工升级提醒横幅 -->
    <div id="ov-escalation-banner" style="display:none;background:linear-gradient(135deg,#ef444422,#f9731622);border:1px solid #ef444466;border-radius:10px;padding:10px 16px;margin-bottom:14px;display:none;align-items:center;justify-content:space-between">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:18px">&#128226;</span>
        <div>
          <div style="font-size:13px;font-weight:600;color:#ef4444">有对话需要人工处理</div>
          <div style="font-size:12px;color:var(--text-muted)" id="ov-esc-detail">-</div>
        </div>
      </div>
      <button class="qa-btn" onclick="navigateToPage('conversations')" style="border-color:#ef4444;color:#ef4444;white-space:nowrap">查看队列 &rarr;</button>
    </div>

    <!-- 今日 TikTok 运营成果 -->
    <div style="display:grid;grid-template-columns:repeat(6,1fr);gap:10px;margin-bottom:16px">
      <div class="stat-card" style="border-left:3px solid #8b5cf6"><div class="stat-num" id="today-watched" style="color:#8b5cf6">0</div><div class="stat-label">今日刷视频</div></div>
      <div class="stat-card" style="border-left:3px solid #3b82f6"><div class="stat-num" id="today-followed" style="color:#3b82f6">0</div><div class="stat-label">今日关注</div></div>
      <div class="stat-card" style="border-left:3px solid #22c55e"><div class="stat-num" id="today-dms" style="color:#22c55e">0</div><div class="stat-label">今日私信</div></div>
      <div class="stat-card" style="border-left:3px solid #06b6d4"><div class="stat-num" id="today-autoreplied" style="color:#06b6d4">0</div><div class="stat-label">AI自动回复</div></div>
      <div class="stat-card" style="border-left:3px solid #f59e0b"><div class="stat-num" id="today-leads" style="color:#f59e0b">0</div><div class="stat-label">今日新线索</div></div>
      <div class="stat-card" style="border-left:3px solid #22c55e;cursor:pointer" onclick="navigateToPage('conversations')"><div class="stat-num" id="today-converts" style="color:#22c55e">0</div><div class="stat-label">已转化 →</div></div>
    </div>
    <!-- P10-A: 自动化任务快速开关 -->
    <div class="card" id="sched-quick-panel" style="margin-bottom:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <span style="font-size:13px;font-weight:600">⚡ 自动化任务开关</span>
        <div style="display:flex;gap:6px">
          <button class="dev-btn" onclick="_schedQuickAll(true)" style="font-size:10px;padding:3px 8px;color:var(--green);border-color:var(--green)">全开</button>
          <button class="dev-btn" onclick="_schedQuickAll(false)" style="font-size:10px;padding:3px 8px;color:var(--red);border-color:var(--red)">全关</button>
          <button class="dev-btn" onclick="_loadScheduledJobsQuick()" style="font-size:10px;padding:3px 8px">刷新</button>
        </div>
      </div>
      <div id="sched-quick-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px">
        <div style="color:var(--text-dim);font-size:12px;padding:8px">加载中...</div>
      </div>
    </div>
    <!-- 设备效能排行 -->
    <div class="card" style="margin-top:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div style="font-size:13px;font-weight:600;color:var(--text)">📊 设备效能排行</div>
        <button class="dev-btn" onclick="_loadDeviceRanking()" style="font-size:11px">刷新</button>
      </div>
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="border-bottom:1px solid var(--border)">
            <th style="padding:6px 12px;text-align:left;font-size:11px;color:var(--text-dim);font-weight:500">设备</th>
            <th style="padding:6px 12px;text-align:left;font-size:11px;color:var(--text-dim);font-weight:500">阶段</th>
            <th style="padding:6px 12px;text-align:center;font-size:11px;color:var(--text-dim);font-weight:500">关注</th>
            <th style="padding:6px 12px;text-align:center;font-size:11px;color:var(--text-dim);font-weight:500">私信</th>
            <th style="padding:6px 12px;text-align:center;font-size:11px;color:var(--text-dim);font-weight:500">DM率</th>
            <th style="padding:6px 12px;text-align:center;font-size:11px;color:var(--text-dim);font-weight:500">今日</th>
          </tr>
        </thead>
        <tbody id="device-ranking-body">
          <tr><td colspan="6" style="padding:20px;text-align:center;color:var(--text-muted)">加载中...</td></tr>
        </tbody>
      </table>
      <div id="device-ranking-empty" style="display:none;text-align:center;padding:20px;color:var(--text-muted);font-size:12px">暂无设备数据</div>
    </div>
    <!-- 设备健康概览 -->
    <div id="ov-health-bar" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:14px"></div>
    <!-- P10-D: 7天活跃趋势 -->
    <div class="card" style="margin-bottom:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
        <span style="font-size:13px;font-weight:600;color:var(--text)">📈 近7天活跃趋势</span>
        <button class="dev-btn" onclick="_loadActivityTrend()" style="font-size:11px">刷新</button>
      </div>
      <canvas id="chart-activity-trend" height="100"></canvas>
    </div>
    <!-- 图表看板 -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:16px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-size:12px;font-weight:600">设备在线趋势</span>
          <select id="chart-trend-range" onchange="loadTrendChart()" style="font-size:10px;padding:2px 6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:4px">
            <option value="1h">1小时</option><option value="6h">6小时</option><option value="24h" selected>24小时</option><option value="7d">7天</option>
          </select>
        </div>
        <canvas id="chart-device-trend" height="160"></canvas>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-size:12px;font-weight:600">任务完成趋势</span>
          <select id="chart-task-range" onchange="loadTaskChart()" style="font-size:10px;padding:2px 6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:4px">
            <option value="1h">1小时</option><option value="6h">6小时</option><option value="24h" selected>24小时</option><option value="7d">7天</option>
          </select>
        </div>
        <canvas id="chart-task-trend" height="160"></canvas>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:14px;margin-bottom:16px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <span style="font-size:12px;font-weight:600;display:block;margin-bottom:8px">设备状态分布</span>
        <canvas id="chart-device-pie" height="160"></canvas>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <span style="font-size:12px;font-weight:600;display:block;margin-bottom:8px">任务类型分布</span>
        <canvas id="chart-task-pie" height="160"></canvas>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <span style="font-size:12px;font-weight:600;display:block;margin-bottom:8px">电量分布</span>
        <canvas id="chart-battery-bar" height="160"></canvas>
      </div>
    </div>
    <!-- P10-B: 今日运营日报 -->
    <div class="card" id="daily-report-card" style="margin-bottom:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <span style="font-size:13px;font-weight:600">📊 今日运营日报</span>
        <div style="display:flex;gap:6px">
          <button class="dev-btn" onclick="_loadDailyReport()" style="font-size:10px;padding:3px 8px">刷新</button>
          <button class="dev-btn" onclick="_printDailyReport()" style="font-size:10px;padding:3px 8px">🖨️ 打印</button>
        </div>
      </div>
      <div id="daily-report-body">
        <!-- filled by _loadDailyReport() -->
        <div style="color:var(--text-dim);font-size:12px;text-align:center;padding:16px">点击刷新加载今日数据</div>
      </div>
    </div>
    <!-- 两栏: 设备 + 任务 -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div>
        <h3 style="font-size:14px;margin-bottom:10px;color:var(--text-dim)">设备状态</h3>
        <div class="device-grid" id="ov-device-grid" style="grid-template-columns:repeat(auto-fill,minmax(200px,1fr))">加载中...</div>
      </div>
      <div>
        <h3 style="font-size:14px;margin-bottom:10px;color:var(--text-dim)">最近任务</h3>
        <div id="ov-task-list" style="background:var(--bg-card);border-radius:12px;border:1px solid var(--border);overflow:hidden">
          <table class="task-table" style="width:100%"><thead><tr><th style="width:36px"></th><th>任务</th><th>设备</th><th>状态</th><th>时间</th><th>操作</th></tr></thead><tbody id="ov-tasks-body"></tbody></table>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ 设备管理 ═══ -->
  <div class="page" id="page-devices">
    <div id="dev-page-alerts" style="display:none;margin-bottom:10px;padding:10px 12px;border-radius:10px;border:1px solid rgba(239,68,68,.4);background:linear-gradient(135deg,rgba(239,68,68,.1),rgba(245,158,11,.06));font-size:11px;line-height:1.45"></div>
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <div class="stats-row" style="margin-bottom:0;flex:1">
        <div class="stat-card blue"><div class="stat-num" id="d-online">-</div><div class="stat-label">在线</div></div>
        <div class="stat-card green"><div class="stat-num" id="d-total">-</div><div class="stat-label">总计</div><div id="d-total-breakdown" style="font-size:9px;color:var(--text-dim);margin-top:4px;line-height:1.25" title="本机·集群分解"></div></div>
      </div>
      <div style="display:flex;gap:6px;align-items:center;flex-wrap:wrap">
        <span style="font-size:10px;color:var(--text-dim);max-width:220px;line-height:1.3" title="本机=当前主机 ADB/配置；开启集群后「总计」含 Worker 合并行，与 GET /devices/meta 中本机计数可能不同">本机/集群计数说明见各卡片 title</span>
        <button class="qa-btn" onclick="_cleanupGhosts()" style="padding:6px 12px;font-size:11px;color:#ef4444;border-color:#ef4444">&#128465; 清理离线幽灵</button>
        <button class="qa-btn" onclick="autoNumberDevices(false)" style="padding:6px 12px;font-size:11px">自动编号</button>
        <button class="qa-btn" onclick="autoNumberDevices(true)" style="padding:6px 12px;font-size:11px">编号+壁纸</button>
        <button class="qa-btn" onclick="deployAllWallpapers()" style="padding:6px 12px;font-size:11px">&#127912; 壁纸</button>
      </div>
    </div>
    <div id="dev-today-tips" style="background:linear-gradient(135deg,rgba(59,130,246,.08),rgba(139,92,246,.08));border:1px solid rgba(59,130,246,.2);border-radius:10px;padding:10px 14px;margin-bottom:10px;display:flex;align-items:center;gap:12px;font-size:12px">
      <span style="font-size:18px">&#128161;</span>
      <div style="flex:1">
        <b>今日推荐:</b>
        <span id="dev-tip-text" style="color:var(--text-dim)">加载中...</span>
      </div>
      <button id="dev-tip-btn" class="qa-btn" style="padding:6px 14px;font-size:11px;white-space:nowrap" onclick="_execTodayTip()">执行</button>
    </div>
    <div id="dev-filter-bar" style="margin-bottom:10px;display:flex;gap:8px;align-items:center;flex-wrap:wrap">
      <input id="dev-search" placeholder="搜索设备…" data-i18n-placeholder="search-device" oninput="filterDeviceGrid()" style="flex:1;min-width:160px;padding:6px 10px;background:var(--bg-card);color:var(--text-main);border:1px solid var(--border);border-radius:8px;font-size:12px"/>
      <select id="dev-status-filter" onchange="filterDeviceGrid()" style="padding:6px;background:var(--bg-card);color:var(--text-main);border:1px solid var(--border);border-radius:8px;font-size:12px">
        <option value="all">全部状态</option><option value="online">在线</option><option value="offline">离线</option><option value="busy">执行中</option>
      </select>
      <select id="dev-group-filter" onchange="filterDeviceGrid()" style="padding:6px;background:var(--bg-card);color:var(--text-main);border:1px solid var(--border);border-radius:8px;font-size:12px">
        <option value="">全部分组</option>
      </select>
      <span id="dev-count-info" style="font-size:11px;color:var(--text-muted)"></span>
      <div style="display:flex;gap:4px">
        <button class="sb-btn2" onclick="setDevGridSize('small')" style="font-size:10px;padding:3px 8px">紧凑</button>
        <button class="sb-btn2" onclick="setDevGridSize('normal')" style="font-size:10px;padding:3px 8px">标准</button>
        <button class="sb-btn2" onclick="setDevGridSize('large')" style="font-size:10px;padding:3px 8px">大卡片</button>
      </div>
    </div>
    <div id="worker-tab-bar"></div>
    <div id="cluster-node-bar" style="display:none"></div>
    <div class="device-grid" id="dev-grid" style="max-height:calc(100vh - 200px);overflow-y:auto">加载中...</div>
    <div id="dev-pager" style="display:flex;justify-content:center;gap:8px;margin-top:10px"></div>
  </div>

  <!-- ═══ 指令控制 ═══ -->
  <div class="page" id="page-chat">
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:10px">&#9889; 快速指令</div>
      <div style="display:flex;gap:8px;margin-bottom:10px">
        <input id="ai-quick-input" type="text" placeholder="输入指令，如：所有手机养号30分钟"
          style="flex:1;padding:10px 14px;background:var(--bg-input);border:1px solid var(--border);border-radius:8px;color:var(--text);font-size:13px;outline:none"
          onkeydown="if(event.key==='Enter')_aiQuickExec()">
        <button onclick="_aiQuickExec()" style="background:linear-gradient(135deg,#3b82f6,#8b5cf6);color:#fff;border:none;border-radius:8px;padding:10px 20px;font-size:13px;font-weight:600;cursor:pointer;white-space:nowrap">执行</button>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px">
        <button class="qa-btn" onclick="_aiQuickSet('所有手机养号30分钟')">&#127793; 养号</button>
        <button class="qa-btn" onclick="_aiQuickSet('所有手机关注50人')">&#128101; 关注50人</button>
        <button class="qa-btn" onclick="_aiQuickSet('全流程获客意大利')">&#128640; 全流程</button>
        <button class="qa-btn" onclick="_aiQuickSet('引流500人到Telegram')">&#128172; 引流500人</button>
        <button class="qa-btn" onclick="_aiQuickSet('查看日报')">&#128202; 日报</button>
        <button class="qa-btn" onclick="_aiQuickSet('VPN重连')">&#128274; VPN</button>
        <button class="qa-btn" onclick="_aiQuickSet('创建意大利引流战役')">&#128640; 战役</button>
        <button class="qa-btn" onclick="_aiQuickSet('停止所有任务')" style="border-color:var(--red);color:var(--red)">&#9209; 停止</button>
      </div>
      <div id="ai-quick-result" style="margin-top:10px;font-size:12px;display:none"></div>
    </div>
    <div class="chat-layout">
      <div class="chat-main">
        <div class="chat-header"><span>AI 指令控制台 (DeepSeek)</span><span style="font-size:11px;color:var(--text-muted)">自然语言 · 中文输入</span></div>
        <div class="chat-messages" id="chat-messages">
          <div class="chat-msg bot"><div class="chat-bubble">你好！我是 OpenClaw AI 助手。<br><br>
🇵🇭 <b>当前目标：菲律宾女性 20-25岁</b><br>
<span style="font-size:11px;color:#a5b4fc">点击下方快捷指令一键启动 ↓</span><br><br>
<b>· 菲律宾女性获客剧本</b> — 养号+直播+评论+关注全流程<br>
<b>· 菲律宾直播评论互动</b> — 进直播间发评论获曝光<br>
<b>· 菲律宾评论区关注女粉</b> — 搜热门视频评论区关注<br><br>
输入 <b>"帮助"</b> 查看全部指令。</div><div class="chat-meta">系统消息</div></div>
        </div>
        <!-- ★ P2-6: 菲律宾快捷指令卡片 -->
        <div id="chat-quick-chips" style="display:flex;flex-wrap:wrap;gap:6px;padding:8px 12px;border-top:1px solid var(--border);background:var(--bg-card)">
          <button class="chat-chip-btn ph-chip" onclick="fillAndSend('菲律宾女性20-25岁获客剧本：养号+直播互动+评论区关注+私信引流')" title="完整获客剧本">🇵🇭 获客剧本</button>
          <button class="chat-chip-btn ph-chip" onclick="fillAndSend('菲律宾直播间评论互动，关注活跃女性观众，20-25岁')" title="进入直播间评论">📡 直播评论</button>
          <button class="chat-chip-btn ph-chip" onclick="fillAndSend('菲律宾评论区互动，关注20-25岁女性评论者')" title="评论区关注女粉">💬 评论区关注</button>
          <button class="chat-chip-btn ph-chip" onclick="fillAndSend('菲律宾关注女性用户50人，年龄20-25岁')" title="批量关注">👥 批量关注</button>
          <button class="chat-chip-btn" onclick="fillAndSend('查看今日日报')" title="查看数据">📊 日报</button>
          <button class="chat-chip-btn" onclick="fillAndSend('停止所有任务')" title="紧急停止" style="color:#f87171;border-color:rgba(248,113,113,.4)">⛔ 停止</button>
        </div>
        <div class="chat-input-area">
          <input type="text" id="chat-input" placeholder="输入中文指令，如：菲律宾女性20-25岁获客剧本 ..." autocomplete="off" onkeydown="if(event.key==='Enter'&&!event.shiftKey){event.preventDefault();sendChat();}">
          <button id="chat-btn" onclick="sendChat()">发送</button>
        </div>
      </div>
      <div class="chat-hints">
        <!-- ★ P3-1: 今日涨粉实时仪表盘卡片 -->
        <div id="growth-dashboard" style="padding:8px 10px 6px;border-bottom:1px solid var(--border);background:var(--bg-card)">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:5px">
            <span style="font-size:10px;font-weight:700;color:var(--text)">📈 今日涨粉进度</span>
            <span id="growth-last-update" style="font-size:9px;color:var(--text-muted)">加载中...</span>
          </div>
          <div id="growth-summary" style="display:flex;gap:8px;margin-bottom:6px">
            <div class="growth-kpi">
              <div id="gk-followed" class="growth-kpi-val">-</div>
              <div class="growth-kpi-label">今日关注</div>
            </div>
            <div class="growth-kpi">
              <div id="gk-fans" class="growth-kpi-val" style="color:#22c55e">-</div>
              <div class="growth-kpi-label">预估回粉</div>
            </div>
            <div class="growth-kpi">
              <div id="gk-quota" class="growth-kpi-val" style="color:#f59e0b">-</div>
              <div class="growth-kpi-label">配额剩余</div>
            </div>
          </div>
          <div id="growth-device-list" style="font-size:9px;color:var(--text-muted);line-height:1.7;max-height:80px;overflow-y:auto"></div>
        </div>
        <div class="chat-hints-header">常用指令</div>
        <div class="hint-list">
          <div style="font-size:10px;color:#fb923c;font-weight:600;margin-bottom:4px">🇵🇭 菲律宾获客</div>
          <div class="hint-item" onclick="fillCmd('菲律宾女性20-25岁获客剧本：养号+直播互动+评论区关注+私信引流')"><span class="hint-cmd">🎬 完整获客剧本</span><span class="hint-desc">— 全流程自动化</span></div>
          <div class="hint-item" onclick="fillCmd('菲律宾直播间评论互动，关注活跃女性观众，20-25岁')"><span class="hint-cmd">📡 直播评论互动</span><span class="hint-desc">— 曝光+关注主播</span></div>
          <div class="hint-item" onclick="fillCmd('菲律宾评论区互动，关注20-25岁女性评论者')"><span class="hint-cmd">💬 评论区关注</span><span class="hint-desc">— 高意向用户池</span></div>
          <div class="hint-item" onclick="fillCmd('菲律宾关注女性用户50人，年龄20-25岁')"><span class="hint-cmd">👥 批量关注</span><span class="hint-desc">— 精准人群过滤</span></div>
          <div class="hint-item" onclick="fillCmd('我想涨粉30万，目标菲律宾女性20-25岁')"><span class="hint-cmd">🎯 30万粉计划</span><span class="hint-desc">— AI智能规划</span></div>
          <div style="font-size:10px;color:var(--accent);font-weight:600;margin:8px 0 4px">运营指令</div>
          <div class="hint-item" onclick="fillCmd('所有手机养号30分钟，关注20人，发10条引流消息')"><span class="hint-cmd">养号+关注+引流</span><span class="hint-desc">— 组合任务</span></div>
          <div class="hint-item" onclick="fillCmd('查看今日日报')"><span class="hint-cmd">今日日报</span><span class="hint-desc">— 运营数据</span></div>
          <div class="hint-item" onclick="fillCmd('查看线索')"><span class="hint-cmd">查看线索</span><span class="hint-desc">— CRM数据</span></div>
          <div style="font-size:10px;color:var(--accent);font-weight:600;margin:8px 0 4px">设备控制</div>
          <div class="hint-item" onclick="fillCmd('所有手机养号30分钟')"><span class="hint-cmd">养号30分钟</span><span class="hint-desc">— 全部设备</span></div>
          <div class="hint-item" onclick="fillCmd('01-05号手机关注50人')"><span class="hint-cmd">01-05号关注50人</span><span class="hint-desc">— 范围指定</span></div>
          <div class="hint-item" onclick="fillCmd('设备状态')"><span class="hint-cmd">设备状态</span><span class="hint-desc">— 在线/离线</span></div>
          <div class="hint-item" onclick="fillCmd('VPN重连')"><span class="hint-cmd">VPN重连</span><span class="hint-desc">— 静默重连</span></div>
          <div style="font-size:10px;color:var(--accent);font-weight:600;margin:8px 0 4px">配置管理</div>
          <div class="hint-item" onclick="fillCmd('设置引流 telegram @dthb3')"><span class="hint-cmd">设置引流账号</span><span class="hint-desc">— Telegram/WhatsApp</span></div>
          <div class="hint-item" onclick="fillCmd('切换菲律宾')"><span class="hint-cmd">切换菲律宾</span><span class="hint-desc">— VPN+目标切换</span></div>
          <div class="hint-item" onclick="fillCmd('停止所有任务')"><span class="hint-cmd">紧急停止</span><span class="hint-desc">— 终止全部</span></div>
          <div class="hint-item" onclick="fillCmd('帮助')"><span class="hint-cmd">帮助</span><span class="hint-desc">— 全部指令</span></div>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ 任务中心 ═══ -->
  <div class="page" id="page-tasks">
    <div class="oc-task-toolbar">
      <div class="oc-task-toolbar__row1">
        <div class="oc-task-toolbar__tabs">
          <button class="qa-btn" onclick="filterTasks('all')" id="tf-all" style="border-color:var(--accent)">全部</button>
          <button class="qa-btn" onclick="filterTasks('running')" id="tf-running">运行中</button>
          <button class="qa-btn" onclick="filterTasks('pending')" id="tf-pending">等待中</button>
          <button class="qa-btn" onclick="filterTasks('completed')" id="tf-completed">已完成</button>
          <button class="qa-btn" onclick="filterTasks('failed')" id="tf-failed">失败</button>
          <button class="qa-btn" onclick="filterTasks('trash')" id="tf-trash" title="软删除的任务">回收站<span id="tf-trash-badge" style="font-size:10px;font-weight:600;margin-left:2px;opacity:0.9"></span></button>
        </div>
        <span class="oc-task-toolbar__count-wrap">
          <span id="task-count-info" class="oc-task-toolbar__count"></span>
          <button type="button" id="task-count-help-btn" class="oc-task-count-help" title="" aria-label="条数说明" onclick="var i=document.getElementById('task-count-info');if(i&&i.title&&typeof showToast==='function')showToast(i.title,'info',9000,'task-count-hint')">?</button>
        </span>
        <div class="task-health-chips" id="task-health-chips" aria-label="任务中心健康状态">
          <span class="task-health-chip gray" id="chip-vpn" title="加载中..."><span class="dot"></span>VPN —</span>
          <span class="task-health-chip gray" id="chip-success" title="加载中..."><span class="dot"></span>今日 —</span>
        </div>
      </div>
      <div class="oc-task-toolbar__row2">
        <p class="oc-task-toolbar__hint">批量操作：勾选列表后使用「移入所选」；<strong>失败</strong>下「清空失败」由服务端一次性批量移入回收站（本机 SQL + 通知各 Worker）。</p>
        <div class="oc-task-toolbar__actions">
          <button type="button" class="dev-btn" onclick="purgeTasks(7)" style="font-size:11px;color:var(--text-muted)" title="删除较早的已完成/失败记录">清理7天前</button>
          <span class="oc-task-toolbar__sep" aria-hidden="true"></span>
          <button type="button" id="task-bulk-delete-btn" class="qa-btn oc-task-btn-warn" disabled onclick="deleteSelectedTasksBulk()" style="font-size:11px" title="将勾选的任务移入回收站">移入所选</button>
          <button type="button" id="task-failed-clear-all-btn" class="qa-btn oc-task-btn-warn" onclick="moveAllFailedDeletableToTrash()" style="display:none;font-size:11px" title="将当前失败列表全部移入回收站（每批最多100条，可多批）">清空失败</button>
          <button type="button" id="task-bulk-restore-btn" class="qa-btn" disabled onclick="restoreSelectedTasksBulk()" style="display:none;font-size:11px;border-color:#22c55e;color:#22c55e">恢复所选</button>
          <button type="button" id="task-bulk-erase-btn" class="qa-btn oc-task-btn-warn" disabled onclick="eraseSelectedTasksBulk()" style="display:none;font-size:11px">永久删除所选</button>
        </div>
        <button type="button" class="qa-btn oc-task-toolbar__cancel" onclick="cancelAllTasks()" style="border-color:var(--red);color:var(--red);font-size:11px" title="取消所有运行中/等待中的任务">&#9209; 取消全部运行</button>
      </div>
    </div>
    <!-- P9: 错误分析面板 -->
    <div id="error-analysis-panel" style="display:none;margin-bottom:12px;border-radius:12px;border:1px solid var(--border);overflow:hidden">
      <div id="eap-header" onclick="_toggleEap()" style="display:flex;align-items:center;gap:10px;padding:10px 14px;background:var(--bg-card);cursor:pointer;user-select:none">
        <span id="eap-icon" style="font-size:16px">⚠️</span>
        <span id="eap-title" style="font-size:13px;font-weight:600;color:var(--text)">错误分析</span>
        <span id="eap-badge-count" style="background:var(--red);color:#fff;font-size:10px;padding:1px 6px;border-radius:10px;display:none"></span>
        <span id="eap-summary-text" style="font-size:12px;color:var(--text-dim);flex:1"></span>
        <span id="eap-rate" style="font-size:12px;font-weight:600"></span>
        <span id="eap-chevron" style="font-size:11px;color:var(--text-dim);transition:transform .2s">▼</span>
      </div>
      <div id="eap-detail" style="display:none;padding:14px;background:var(--bg-main);border-top:1px solid var(--border)">
        <!-- 告警横幅 -->
        <div id="eap-alerts" style="margin-bottom:10px"></div>
        <!-- 两列：错误类型条 + 修复建议 -->
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
          <div>
            <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:8px">错误类型分布</div>
            <div id="eap-cats"></div>
          </div>
          <div>
            <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:8px">修复建议</div>
            <div id="eap-suggestions"></div>
          </div>
        </div>
        <!-- Unknown错误样本 -->
        <div id="eap-unknown-samples-wrap" style="display:none;margin-top:10px">
          <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:4px">未识别错误样本</div>
          <div id="eap-unknown-samples"></div>
        </div>
        <!-- 24小时趋势 -->
        <div style="margin-top:12px">
          <div style="font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:6px">24小时失败趋势</div>
          <div id="eap-trend" style="display:flex;align-items:flex-end;gap:2px;height:40px"></div>
        </div>
        <div style="margin-top:8px;text-align:right">
          <button class="qa-btn" onclick="_loadErrorAnalysis()" style="font-size:10px;padding:3px 8px">刷新分析</button>
        </div>
      </div>
    </div>
    <div id="oc-task-list-card" class="oc-task-list-card">
      <table class="task-table"><thead><tr><th style="width:40px;text-align:center" title="仅可选中可删除的终态任务"><input type="checkbox" id="task-select-all" title="全选本页可删" onclick="taskBulkToggleAll(this.checked)" style="accent-color:var(--accent)" /></th><th>任务</th><th>设备</th><th>状态</th><th>时间</th><th>操作</th></tr></thead><tbody id="task-tbody"></tbody></table>
      <button id="task-load-more" onclick="loadMoreTasks()" class="dev-btn" style="display:none;margin-top:12px;width:100%">加载更多任务</button>
      <div id="task-empty" style="display:none;text-align:center;padding:40px;color:var(--text-muted)">
        <div style="font-size:28px;margin-bottom:10px">&#128203;</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:6px">暂无任务</div>
        <div class="task-empty-hint" style="font-size:12px;color:var(--text-dim);margin-bottom:14px">从总览页或 TikTok 面板创建你的第一个任务</div>
        <div style="display:flex;gap:8px;justify-content:center">
          <button class="qa-btn" onclick="navigateToPage('overview')" style="padding:6px 14px;font-size:12px">&#9776; 去总览</button>
          <button class="qa-btn" onclick="navigateToPage('plat-tiktok')" style="padding:6px 14px;font-size:12px">&#127916; TikTok</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ 转化漏斗 ═══ -->
  <div class="page" id="page-funnel">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:18px">
      <h3 style="font-size:15px;font-weight:600;color:var(--text)">TikTok 引流转化漏斗</h3>
      <button class="qa-btn" onclick="loadFunnel()" style="padding:6px 14px;font-size:12px">刷新数据</button>
    </div>
    <div class="funnel-container" id="funnel-chart">加载中...</div>
    <div class="funnel-stats" id="funnel-stats"></div>
    <div id="funnel-aggregate-stats" style="margin-bottom:16px"></div>
    <div style="margin-top:24px">
      <h3 style="font-size:14px;margin-bottom:12px;color:var(--text-dim)">近7天趋势</h3>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;overflow-x:auto">
        <table class="task-table" style="width:100%"><thead><tr><th>日期</th><th>发现</th><th>关注</th><th>回关</th><th>聊天</th><th>回复</th><th>转化</th></tr></thead><tbody id="funnel-daily"></tbody></table>
      </div>
    </div>
  </div>

  <!-- ═══ 屏幕监控 ═══ -->
  <div class="page" id="page-screens">
    <!-- 集群实时概况 -->
    <div id="scr-stats-bar" style="display:flex;align-items:center;gap:14px;padding:8px 14px;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;margin-bottom:10px;font-size:12px;flex-wrap:wrap">
      <span style="font-weight:600">📊</span>
      <span id="ss-online" style="color:var(--green)">-在线</span>
      <span style="color:var(--border)">|</span>
      <span id="ss-tasks" style="color:var(--yellow)">-任务</span>
      <span id="ss-idle" style="color:var(--text-muted)">-空闲</span>
      <span style="color:var(--border)">|</span>
      <span id="ss-vpn">🔒 VPN -/-</span>
      <span style="color:var(--border)">|</span>
      <span id="ss-health">⚡ 健康 -</span>
      <span style="flex:1"></span>
      <span id="ss-today" style="color:var(--text-dim);font-size:11px"></span>
    </div>
    <div style="margin-bottom:10px;display:flex;flex-direction:column;gap:8px">
      <!-- 第一行: 筛选 + 视图 + 分页 + 计数 -->
      <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
        <div style="display:flex;gap:2px;background:var(--bg-input);border-radius:6px;padding:2px">
          <button class="sz-btn active" id="filter-all" onclick="setDeviceFilter('all')" style="border:none;border-radius:4px;padding:4px 10px;font-size:11px">全部</button>
          <button class="sz-btn" id="filter-online" onclick="setDeviceFilter('online')" style="border:none;border-radius:4px;padding:4px 10px;font-size:11px">在线</button>
          <button class="sz-btn" id="filter-offline" onclick="setDeviceFilter('offline')" style="border:none;border-radius:4px;padding:4px 10px;font-size:11px">离线</button>
        </div>
        <span style="width:1px;height:18px;background:var(--border)"></span>
        <div style="display:flex;gap:2px;background:var(--bg-input);border-radius:6px;padding:2px" title="卡片大小">
          <button class="sz-btn" onclick="setGridSize('sm')" style="border:none;border-radius:4px;padding:4px 8px;font-size:11px">小</button>
          <button class="sz-btn active" onclick="setGridSize('md')" style="border:none;border-radius:4px;padding:4px 8px;font-size:11px">中</button>
          <button class="sz-btn" onclick="setGridSize('lg')" style="border:none;border-radius:4px;padding:4px 8px;font-size:11px">大</button>
        </div>
        <select id="scr-pagesize" onchange="setPageSize(+this.value)" style="background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:11px;height:28px">
          <option value="20">20/页</option>
          <option value="50">50/页</option>
          <option value="100">100/页</option>
          <option value="0">全部</option>
        </select>
        <div id="scr-pager" style="display:flex;gap:2px;align-items:center;font-size:11px"></div>
        <span style="flex:1"></span>
        <span id="scr-device-count" style="font-size:12px;color:var(--text-muted);font-weight:500"></span>
        <span id="scr-device-breakdown" style="font-size:10px;color:var(--text-dim);max-width:200px" title="本机=主控列表；集群=合并的Worker设备"></span>
        <button class="qa-btn" onclick="refreshAllScreens()" style="padding:4px 12px;font-size:11px;height:28px">刷新</button>
      </div>
      <!-- 第二行: 功能按钮 -->
      <div style="display:flex;align-items:center;gap:6px;flex-wrap:wrap">
        <button class="qa-btn" id="group-mode-btn" onclick="toggleGroupMode()" style="padding:4px 12px;font-size:11px;height:28px">群控模式</button>
        <button class="qa-btn" onclick="openMultiStream()" style="padding:4px 12px;font-size:11px;height:28px">多宫格</button>
        <button class="qa-btn" onclick="checkAllAnomalies()" style="padding:4px 12px;font-size:11px;height:28px">异常检测</button>
        <button class="qa-btn" onclick="setupDualChannelAll()" style="padding:4px 12px;font-size:11px;height:28px" title="对本机所有 USB 设备执行 adb tcpip 5555 并连接 Wi‑Fi（与电脑同局域网）">📡 双通道</button>
        <button type="button" class="qa-btn" onclick="toggleScrApkPanel()" title="向本机 ADB 直连设备推送 APK（接口与「批量安装APK」相同）" style="padding:4px 12px;font-size:11px;height:28px">📦 安装APK</button>
        <span style="width:1px;height:18px;background:var(--border)"></span>
        <label style="font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:3px;cursor:pointer">
          <input type="checkbox" id="auto-refresh-all" onchange="toggleAutoRefreshAll()" style="accent-color:var(--accent);width:13px;height:13px"> 自动刷新
        </label>
        <label style="font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:3px;cursor:pointer">
          <input type="checkbox" id="show-cluster-devices" checked onchange="toggleClusterDevices()" style="accent-color:#8b5cf6;width:13px;height:13px"> 集群设备
        </label>
        <label style="font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:3px;cursor:pointer" title="关闭时VPN仅在任务运行期间自动重连">
          <input type="checkbox" id="vpn-auto-reconnect" onchange="toggleVpnAutoReconnect()" style="accent-color:#22c55e;width:13px;height:13px"> VPN自动重连
        </label>
        <select id="scr-host-filter" onchange="filterByHost(this.value)" style="display:none;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:11px;height:28px">
          <option value="">全部主机</option>
        </select>
        <button class="qa-btn conflict-btn" id="conflict-fix-btn" onclick="fixAllConflicts()" style="display:none;padding:4px 12px;font-size:11px;height:28px">⚠ 重复编号</button>
        <button class="qa-btn" id="auto-assign-btn" onclick="_quickAutoAssign()" style="display:none;padding:4px 12px;font-size:11px;height:28px;color:#22c55e;border-color:#22c55e">⚡ 自动编号</button>
        <span id="active-tasks-badge"></span>
        <button class="qa-btn" id="swap-mode-btn" onclick="toggleSwapMode()" style="padding:4px 12px;font-size:11px;height:28px">⇄ 互换编号</button>
        <button class="qa-btn" onclick="showNumMgr()" style="padding:4px 12px;font-size:11px;height:28px;color:#a78bfa;border-color:#a78bfa">📋 编号管理</button>
        <span style="flex:1"></span>
        <div style="position:relative;display:inline-flex" id="wp-dropdown-wrap">
          <button id="wp-outdated-btn" class="qa-btn" onclick="deployOutdatedWallpapers()" style="padding:4px 12px;font-size:11px;height:28px;border-radius:6px 0 0 6px">🖼 补缺壁纸 <span id="wp-outdated-badge" style="display:none;background:#f59e0b;color:#fff;font-size:9px;font-weight:700;padding:0 5px;border-radius:3px;margin-left:4px"></span><span id="unset-count-badge" style="display:none;background:#60a5fa;color:#fff;font-size:9px;font-weight:700;padding:0 5px;border-radius:3px;margin-left:2px"></span></button><button class="qa-btn" onclick="toggleWpMenu(event)" style="padding:4px 6px;font-size:10px;height:28px;border-radius:0 6px 6px 0;border-left:1px solid rgba(255,255,255,.12)">&#9660;</button>
          <div id="wp-dropdown-menu" style="display:none;position:absolute;top:100%;right:0;margin-top:4px;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;min-width:200px;z-index:999;box-shadow:0 8px 30px rgba(0,0,0,.4);overflow:hidden">
            <div onclick="deployOutdatedWallpapers();toggleWpMenu()" style="padding:10px 14px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)" onmouseenter="this.style.background='var(--bg-hover)'" onmouseleave="this.style.background=''" title="仅部署壁纸已过期的设备">🖼 补缺壁纸（推荐）</div>
            <div onclick="quickDeployAllWallpapers();toggleWpMenu()" style="padding:10px 14px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)" onmouseenter="this.style.background='var(--bg-hover)'" onmouseleave="this.style.background=''">全部设备一键部署</div>
            <div onclick="showWallpaperDialog();toggleWpMenu()" style="padding:10px 14px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)" onmouseenter="this.style.background='var(--bg-hover)'" onmouseleave="this.style.background=''">自定义编号部署</div>
            <div onclick="quickDeploySelectedWallpapers();toggleWpMenu()" style="padding:10px 14px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)" onmouseenter="this.style.background='var(--bg-hover)'" onmouseleave="this.style.background=''">仅部署已选设备</div>
            <div onclick="renumberAllDevices();toggleWpMenu()" style="padding:10px 14px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)" onmouseenter="this.style.background='var(--bg-hover)'" onmouseleave="this.style.background=''">重新顺序编号</div>
            <div onclick="rescanAllDevices();toggleWpMenu()" style="padding:10px 14px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)" onmouseenter="this.style.background='var(--bg-hover)'" onmouseleave="this.style.background=''">重新扫描设备</div>
            <div onclick="showDeviceRegistryInfo();toggleWpMenu()" style="padding:10px 14px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:8px;border-bottom:1px solid var(--border)" onmouseenter="this.style.background='var(--bg-hover)'" onmouseleave="this.style.background=''">查看设备指纹</div>
            <div onclick="runUsbDiagnostics();toggleWpMenu()" style="padding:10px 14px;font-size:12px;cursor:pointer;display:flex;align-items:center;gap:8px" onmouseenter="this.style.background='var(--bg-hover)'" onmouseleave="this.style.background=''">USB 诊断扫描</div>
          </div>
        </div>
      </div>
      <div id="scr-apk-panel" style="display:none;background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:10px 12px;font-size:11px">
        <div style="display:flex;flex-wrap:wrap;align-items:center;gap:10px;margin-bottom:6px">
          <span style="font-weight:600;color:var(--text-dim)">📦 安装到</span>
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer"><input type="radio" name="scr-apk-mode" value="selected" onchange="_refreshScrApkPreview()"> 群控已选·本机</label>
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer" title="勾选集群卡片，由主控转发 APK 到对应 Worker 执行 adb install"><input type="radio" name="scr-apk-mode" value="cluster_selected" onchange="_refreshScrApkPreview()"> 群控已选→集群</label>
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer"><input type="radio" name="scr-apk-mode" value="page" checked onchange="_refreshScrApkPreview()"> 当前页在线</label>
          <label style="display:flex;align-items:center;gap:4px;cursor:pointer"><input type="radio" name="scr-apk-mode" value="all" onchange="_refreshScrApkPreview()"> 全部在线</label>
          <span id="scr-apk-preview-count" style="font-size:11px;font-weight:600;color:var(--accent)">当前将安装: —</span>
        </div>
        <div style="color:var(--text-muted);font-size:10px;margin-bottom:8px">集群手机：选「群控已选→集群」由主控转发到 Worker；本机 USB：选「群控已选·本机」或当前页/全部在线。</div>
        <div id="scr-apk-cap-hint" style="display:none;margin-bottom:8px;padding:8px 10px;border-radius:8px;border:1px solid var(--border);background:rgba(239,68,68,.08);font-size:10px;line-height:1.45"></div>
        <details style="margin-bottom:8px;font-size:10px;color:var(--text-muted);max-width:720px">
          <summary style="cursor:pointer;color:var(--accent);font-weight:600">排障与说明（HTTP 404 / 反代 / 版本）</summary>
          <ul style="margin:6px 0 0 18px;line-height:1.5">
            <li>主控与 Worker 需部署含集群 APK 转发的版本并已重启；反代须放行 <code style="font-size:9px">POST /batch/install-apk-cluster</code> 与 <code style="font-size:9px">POST /cluster/batch/install-apk</code>。</li>
            <li>浏览器控制台或 <code style="font-size:9px">GET /health</code> 查看 <code style="font-size:9px">capabilities</code> 与 <code style="font-size:9px">build_id</code>（部署脚本可设环境变量 <code style="font-size:9px">OPENCLAW_BUILD_ID</code>）。</li>
            <li>完整文档见仓库 <code style="font-size:9px">docs/抖音/集群APK安装与排障.md</code>。</li>
          </ul>
        </details>
        <div style="display:flex;flex-wrap:wrap;align-items:center;gap:8px">
          <input type="file" id="scr-apk-file" accept=".apk" style="font-size:11px;max-width:240px" onchange="_onScrApkFilePicked()"/>
          <button type="button" class="qa-btn" onclick="runScreenInstallApk()" style="padding:4px 14px;font-size:11px;background:var(--green);color:#111">开始安装</button>
          <button type="button" class="sb-btn2" onclick="navigateToPage('batch-apk')" style="font-size:10px">完整批量页 →</button>
        </div>
        <div id="scr-apk-progress" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;margin-top:10px"></div>
        <div id="scr-apk-retry-wrap"></div>
      </div>
    </div>
    <div class="anomaly-bar" id="anomaly-bar"></div>
    <div class="group-toolbar" id="group-toolbar">
      <span class="grp-count" id="grp-count">已选: 0</span>
      <span id="grp-ws-status" style="font-size:11px;color:var(--text-sub);min-width:80px"></span>
      <span id="grp-latency" style="font-size:11px;color:#22c55e;font-weight:600;min-width:70px"></span>
      <button class="grp-btn" onclick="groupSelectAll()">全选</button>
      <button class="grp-btn" onclick="groupDeselectAll()">取消全选</button>
      <span style="width:1px;height:20px;background:var(--border)"></span>
      <button class="grp-btn" onclick="groupAction('home')">&#127968; Home</button>
      <button class="grp-btn" onclick="groupAction('back')">&#9664; Back</button>
      <button class="grp-btn" onclick="groupAction('recent')">&#9634; Recent</button>
      <span style="width:1px;height:20px;background:var(--border)"></span>
      <button class="grp-btn" onclick="groupAction('swipe_up')">&#8679; 上滑</button>
      <button class="grp-btn" onclick="groupAction('swipe_down')">&#8681; 下滑</button>
      <button class="grp-btn" onclick="groupAction('swipe_left')">&#8678; 左滑</button>
      <button class="grp-btn" onclick="groupAction('swipe_right')">&#8680; 右滑</button>
      <span style="width:1px;height:20px;background:var(--border)"></span>
      <button class="grp-btn" onclick="groupAction('tap_center')">&#128433; 点击中心</button>
      <button class="grp-btn" onclick="groupCustomTap()">&#128433; 自定义点击</button>
      <button class="grp-btn" onclick="groupInputText()">&#9000; 输入文字</button>
      <span style="width:1px;height:20px;background:var(--border)"></span>
      <select id="grp-task-type" style="background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:12px">
        <option value="">-- 批量任务 --</option>
        <option value="tiktok_warmup">养号</option>
        <option value="tiktok_watch">刷视频</option>
        <option value="tiktok_follow">关注</option>
        <option value="tiktok_acquisition">全流程</option>
      </select>
      <button class="grp-btn" onclick="groupLaunchTask()">&#9654; 启动</button>
      <span style="width:1px;height:20px;background:var(--border)"></span>
      <select id="grp-macro-sel" style="background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:12px">
        <option value="">-- 选择宏 --</option>
      </select>
      <button class="grp-btn" onclick="groupPlayMacro()">&#9654; 播放宏</button>
      <button class="grp-btn" onclick="loadMacroList()">&#8635;</button>
      <button class="grp-btn" onclick="exportMacroJSON()" title="导出宏">&#8681; 导出</button>
      <button class="grp-btn" onclick="importMacroJSON()" title="导入宏">&#8679; 导入</button>
      <span style="width:1px;height:20px;background:var(--border)"></span>
      <button class="grp-btn" onclick="quickBatchAddToGroup()" style="color:var(--accent)">&#128193; 批量编组</button>
      <button class="grp-btn" onclick="batchReconnectSelected()" style="color:#22c55e">&#128268; 批量重连</button>
      <button class="grp-btn" onclick="batchDeleteSelected()" style="color:#ef4444">&#128465; 批量删除</button>
      <span style="width:1px;height:20px;background:var(--border)"></span>
      <label class="grp-btn" style="color:#8b5cf6;cursor:pointer;display:inline-flex;align-items:center;gap:4px">
        &#128274; VPN
        <input type="file" accept="image/*" style="display:none" onchange="_vpnGlobalUpload(this)">
      </label>
      <button class="grp-btn" onclick="_batchQuickTask('tiktok_warmup')" style="color:#22c55e">&#127793; 养号</button>
      <button class="grp-btn" onclick="_batchQuickTask('tiktok_follow')" style="color:#3b82f6">&#128101; 关注</button>
      <button class="grp-btn" onclick="_batchQuickTask('tiktok_inbox')" style="color:#06b6d4">&#128229; 收件箱</button>
    </div>
    <div class="screen-grid" id="screen-grid">加载中...</div>
  </div>

  <!-- ═══ 系统日志 ═══ -->
  <div class="page" id="page-logs">
    <div style="margin-bottom:14px;display:flex;align-items:center;justify-content:space-between">
      <div style="display:flex;gap:6px">
        <button class="qa-btn log-filter active" data-lv="" onclick="setLogFilter(this,'')">全部</button>
        <button class="qa-btn log-filter" data-lv="INFO" onclick="setLogFilter(this,'INFO')">INFO</button>
        <button class="qa-btn log-filter" data-lv="WARNING" onclick="setLogFilter(this,'WARNING')">WARNING</button>
        <button class="qa-btn log-filter" data-lv="ERROR" onclick="setLogFilter(this,'ERROR')">ERROR</button>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <label style="font-size:12px;color:var(--text-muted);display:flex;align-items:center;gap:4px">
          <input type="checkbox" id="log-auto" checked onchange="toggleLogAuto()" style="accent-color:var(--accent)"> 自动刷新
        </label>
        <button class="qa-btn" onclick="loadLogs()" style="padding:6px 14px;font-size:12px">刷新</button>
      </div>
    </div>
    <div class="log-panel" id="log-panel">加载中...</div>
  </div>

  <!-- ═══ 设备健康 ═══ -->
  <div class="page" id="page-health">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">设备健康监控</h3>
      <div style="display:flex;gap:8px">
        <button class="qa-btn" onclick="loadHealthPage()" style="padding:6px 14px;font-size:12px">&#8635; 刷新</button>
      </div>
    </div>
    <div class="stats-row" id="health-stats-row">加载中...</div>
    <div id="recovery-stats-panel" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:14px;margin-top:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <div style="font-size:14px;font-weight:600">&#128737; 设备恢复统计</div>
        <button class="sb-btn2" onclick="_loadRecoveryStats()" style="font-size:10px">刷新</button>
      </div>
      <div id="recovery-stats-content" style="font-size:12px;color:var(--text-muted)">加载中...</div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-top:14px">
      <div>
        <h4 style="font-size:13px;margin-bottom:10px;color:var(--text-dim)">健康评分排名</h4>
        <div id="health-rank-panel" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;overflow:hidden">
          <table class="task-table"><thead><tr><th>设备</th><th>总分</th><th>稳定性</th><th>响应</th><th>任务</th><th>状态</th><th>操作</th></tr></thead><tbody id="health-rank-body"></tbody></table>
        </div>
      </div>
      <div>
        <h4 style="font-size:13px;margin-bottom:10px;color:var(--text-dim)">恢复事件时间线</h4>
        <div id="recovery-timeline" class="log-panel" style="max-height:400px">加载中...</div>
      </div>
    </div>
    <div style="margin-top:16px">
      <h4 style="font-size:13px;margin-bottom:10px;color:var(--text-dim)">健康评分趋势 (24h)</h4>
      <div id="health-trend-panel" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;min-height:120px">加载中...</div>
    </div>
  </div>

  <!-- ═══ 工作流编排 ═══ -->
  <div class="page" id="page-workflows">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">工作流编排</h3>
      <div style="display:flex;gap:8px">
        <button class="dev-btn" onclick="loadWorkflowsPage()">刷新</button>
        <button class="dev-btn" onclick="showCreateWorkflow()" style="background:var(--green);color:#111">+ 新建</button>
      </div>
    </div>
    <div id="wf-stats-row" class="stats-row" style="margin-bottom:14px"></div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div>
        <h4 style="font-size:13px;margin-bottom:8px;color:var(--text-dim)">工作流列表</h4>
        <div id="wf-list" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:12px;min-height:200px">加载中...</div>
      </div>
      <div>
        <h4 style="font-size:13px;margin-bottom:8px;color:var(--text-dim)">执行记录</h4>
        <div id="wf-runs" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:12px;min-height:200px">加载中...</div>
      </div>
    </div>
    <div style="margin-top:14px">
      <h4 style="font-size:13px;margin-bottom:8px;color:var(--text-dim)">活动运行</h4>
      <div id="wf-active-runs" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:12px;min-height:80px">无活动运行</div>
    </div>
    <div id="wf-editor-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:1000;align-items:center;justify-content:center">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;width:700px;max-height:80vh;overflow-y:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
          <h4 id="wf-editor-title" style="font-size:14px">编辑工作流</h4>
          <button class="dev-btn" onclick="closeWfEditor()" style="font-size:11px;padding:2px 10px">✕</button>
        </div>
        <input id="wf-editor-name" placeholder="工作流名称" style="width:100%;padding:8px;margin-bottom:10px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;color:var(--text-main);font-size:13px"/>
        <textarea id="wf-editor-yaml" style="width:100%;height:350px;padding:10px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;color:var(--text-main);font-family:monospace;font-size:12px;resize:vertical"></textarea>
        <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end">
          <button class="dev-btn" onclick="saveWorkflow()">保存</button>
          <button class="dev-btn" onclick="closeWfEditor()">取消</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ 集群管理 ═══ -->
  <div class="page" id="page-cluster">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">集群管理</h3>
      <div style="display:flex;gap:8px">
        <button class="dev-btn" onclick="loadClusterPage()">刷新</button>
        <button class="dev-btn" onclick="_otaPushAll()" style="background:linear-gradient(135deg,#22c55e,#16a34a);color:#fff;border:none">&#128228; 一键更新 Worker</button>
        <button class="dev-btn" onclick="showJoinCluster()" style="background:var(--accent);color:#111">加入集群</button>
      </div>
    </div>
    <div id="cl-stats-row" class="stats-row" style="margin-bottom:14px"></div>
    <div style="margin-bottom:14px">
      <h4 style="font-size:13px;margin-bottom:8px;color:var(--text-dim)">主机拓扑</h4>
      <div id="cl-hosts" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div>
        <h4 style="font-size:13px;margin-bottom:8px;color:var(--text-dim)">跨主机设备列表</h4>
        <div id="cl-devices" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:12px;max-height:300px;overflow-y:auto">加载中...</div>
      </div>
      <div>
        <h4 style="font-size:13px;margin-bottom:8px;color:var(--text-dim)">跨主机任务提交</h4>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
          <select id="cl-task-type" style="width:100%;padding:8px;margin-bottom:8px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;color:var(--text-main);font-size:12px">
            <optgroup label="TikTok">
              <option value="tiktok_warmup">养号</option>
              <option value="tiktok_watch">刷视频</option>
              <option value="tiktok_follow">关注</option>
              <option value="tiktok_acquisition">全流程</option>
            </optgroup>
            <optgroup label="Telegram">
              <option value="telegram_auto_reply">自动回复</option>
              <option value="telegram_join_group">加群</option>
            </optgroup>
            <optgroup label="WhatsApp">
              <option value="whatsapp_auto_reply">自动回复</option>
            </optgroup>
            <optgroup label="Facebook">
              <option value="facebook_browse_feed">浏览动态</option>
              <option value="facebook_browse_feed_by_interest">兴趣刷帖</option>
              <option value="facebook_add_friend">加好友</option>
            </optgroup>
          </select>
          <select id="cl-device-select" style="width:100%;padding:8px;margin-bottom:8px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;color:var(--text-main);font-size:12px">
            <option value="">自动选择最优设备</option>
          </select>
          <div style="display:flex;gap:8px;margin-bottom:8px">
            <button class="dev-btn" onclick="clusterDispatch()" style="flex:1;background:var(--green);color:#111;padding:8px">单设备提交</button>
            <button class="dev-btn" onclick="clusterBatchAll()" style="flex:1;background:var(--accent);color:#fff;padding:8px">全集群批量</button>
          </div>
        </div>
      </div>
    </div>
    <div style="margin-top:14px">
      <h4 style="font-size:13px;margin-bottom:8px;color:var(--text-dim)">跨集群脚本执行</h4>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:14px">
        <div style="display:grid;grid-template-columns:1fr 1fr auto;gap:8px;align-items:flex-end">
          <div><label style="font-size:10px;color:var(--text-muted)">脚本</label>
            <select id="cl-script-sel" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;margin-top:2px"></select></div>
          <div><label style="font-size:10px;color:var(--text-muted)">目标</label>
            <select id="cl-script-target" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;margin-top:2px">
              <option value="all">全部主机</option>
            </select></div>
          <button class="dev-btn" onclick="clusterExecScript()" style="background:var(--green);color:#111;padding:6px 16px;font-size:12px;height:34px">执行</button>
        </div>
        <div id="cl-script-results" style="margin-top:8px;font-size:10px;color:var(--text-muted);max-height:200px;overflow-y:auto"></div>
      </div>
    </div>
    <div style="margin-top:14px">
      <h4 style="font-size:13px;margin-bottom:8px;color:var(--text-dim)">编号段分配</h4>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:14px">
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:10px">为每台 Worker 分配编号范围，防止跨机重复。新设备接入时自动从所属 Worker 的范围内取下一可用编号。</div>
        <div id="cl-ranges-rows" style="margin-bottom:10px"></div>
        <button class="dev-btn" onclick="saveClusterRanges()" style="background:var(--accent);color:#111;padding:6px 16px;font-size:12px">保存编号段</button>
      </div>
    </div>
    <div style="margin-top:14px">
      <h4 style="font-size:13px;margin-bottom:8px;color:var(--text-dim)">集群配置</h4>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px" id="cl-config-panel">
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px">
          <div>
            <label style="font-size:10px;color:var(--text-muted);display:block;margin-bottom:3px">角色</label>
            <select id="cl-cfg-role" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px">
              <option value="standalone">单机 (standalone)</option>
              <option value="coordinator">主控 (coordinator)</option>
              <option value="worker">工作节点 (worker)</option>
            </select>
          </div>
          <div>
            <label style="font-size:10px;color:var(--text-muted);display:block;margin-bottom:3px">主控地址 (Worker模式)</label>
            <input id="cl-cfg-url" placeholder="http://192.168.1.100:@@OC_PORT@@" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
          </div>
          <div>
            <label style="font-size:10px;color:var(--text-muted);display:block;margin-bottom:3px">通信密钥</label>
            <input id="cl-cfg-secret" type="password" placeholder="留空则不验证" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px">
          <div>
            <label style="font-size:10px;color:var(--text-muted);display:block;margin-bottom:3px">本机端口</label>
            <input id="cl-cfg-port" type="number" value="@@OC_PORT@@" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
          </div>
          <div>
            <label style="font-size:10px;color:var(--text-muted);display:block;margin-bottom:3px">主机名</label>
            <input id="cl-cfg-name" placeholder="自动检测" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
          </div>
          <div style="display:flex;align-items:flex-end">
            <button class="dev-btn" onclick="saveClusterConfig()" style="width:100%;background:var(--accent);color:#111;padding:6px">保存并应用</button>
          </div>
        </div>
      </div>
    </div>
    <div id="cl-join-modal" style="display:none;position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,0.6);z-index:1000;align-items:center;justify-content:center">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:14px;padding:20px;width:420px">
        <h4 style="font-size:14px;margin-bottom:12px">加入集群</h4>
        <input id="cl-coord-url" placeholder="中心节点地址 (如 http://192.168.1.100:@@OC_PORT@@)" style="width:100%;padding:8px;margin-bottom:10px;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;color:var(--text-main);font-size:13px"/>
        <div style="display:flex;gap:8px;justify-content:flex-end">
          <button class="dev-btn" onclick="joinCluster()">确认加入</button>
          <button class="dev-btn" onclick="document.getElementById('cl-join-modal').style.display='none'">取消</button>
        </div>
      </div>
    </div>
  </div>

  <!-- ═══ 数据分析 ═══ -->
  <div class="page" id="page-analytics">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">数据分析</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <select id="ana-days" onchange="loadAnalytics()" style="background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:4px 8px;font-size:12px">
          <option value="7">最近 7 天</option><option value="14">最近 14 天</option><option value="30" selected>最近 30 天</option>
        </select>
        <button class="qa-btn" onclick="exportCSV()" style="padding:5px 12px;font-size:11px">&#128229; 导出 CSV</button>
      </div>
    </div>
    <!-- 汇总统计 -->
    <div class="stats-row" id="ana-summary"></div>
    <!-- 图表区 -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">每日任务趋势</div>
        <canvas id="chart-tasks" height="200"></canvas>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">转化漏斗趋势</div>
        <canvas id="chart-funnel" height="200"></canvas>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">设备效率排名</div>
        <div id="ana-device-rank" style="max-height:260px;overflow-y:auto"></div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">任务类型分布</div>
        <canvas id="chart-types" height="200"></canvas>
      </div>
    </div>
  </div>

  <!-- ═══ 审计日志 ═══ -->
  <div class="page" id="page-audit">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">审计日志</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <input id="audit-search" type="text" placeholder="搜索操作..." oninput="filterAuditLogs()" style="background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:5px 10px;font-size:12px;width:180px">
        <button class="sb-btn2" onclick="loadAuditLogs()">刷新</button>
      </div>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;overflow:hidden">
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead><tr style="border-bottom:1px solid var(--border);text-align:left">
          <th style="padding:10px 14px;color:var(--text-muted);font-weight:600">时间</th>
          <th style="padding:10px 14px;color:var(--text-muted);font-weight:600">操作</th>
          <th style="padding:10px 14px;color:var(--text-muted);font-weight:600">目标</th>
          <th style="padding:10px 14px;color:var(--text-muted);font-weight:600">详情</th>
          <th style="padding:10px 14px;color:var(--text-muted);font-weight:600">来源</th>
        </tr></thead>
        <tbody id="audit-tbody"></tbody>
      </table>
    </div>
  </div>

  <!-- ═══ 告警设置 ═══ -->
  <div class="page" id="page-alert-rules">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">告警规则管理</h3>
      <div style="display:flex;gap:8px">
        <button class="sb-btn2" onclick="showAddRuleForm()">+ 新建规则</button>
        <button class="sb-btn2" onclick="evalAlertRules()">手动评估</button>
        <button class="sb-btn2" onclick="loadAlertRulesPage()">刷新</button>
      </div>
    </div>
    <div id="add-rule-form" style="display:none;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:10px">新建告警规则</div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px">
        <div><label style="font-size:10px;color:var(--text-muted)">规则名称</label><input id="rule-name" placeholder="my_rule" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;margin-top:4px"></div>
        <div><label style="font-size:10px;color:var(--text-muted)">监控指标</label><select id="rule-metric" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;margin-top:4px">
          <option value="devices_offline">设备掉线数</option>
          <option value="error_rate">错误率 (%)</option>
          <option value="tasks_failed">失败任务数</option>
          <option value="tasks_pending">待执行任务数</option>
          <option value="health_score_min">最低健康分</option>
          <option value="vpn_down_count">VPN异常数</option>
        </select></div>
        <div><label style="font-size:10px;color:var(--text-muted)">比较运算</label><select id="rule-op" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;margin-top:4px">
          <option value=">">&gt; 大于</option>
          <option value=">=">&ge; 大于等于</option>
          <option value="<">&lt; 小于</option>
          <option value="<=">&le; 小于等于</option>
          <option value="==">== 等于</option>
        </select></div>
        <div><label style="font-size:10px;color:var(--text-muted)">阈值</label><input id="rule-threshold" type="number" value="3" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;margin-top:4px"></div>
        <div><label style="font-size:10px;color:var(--text-muted)">级别</label><select id="rule-severity" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;margin-top:4px">
          <option value="info">Info</option>
          <option value="warning" selected>Warning</option>
          <option value="critical">Critical</option>
        </select></div>
        <div><label style="font-size:10px;color:var(--text-muted)">冷却时间(秒)</label><input id="rule-cooldown" type="number" value="300" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;margin-top:4px"></div>
      </div>
      <div style="display:flex;gap:8px;margin-top:12px;justify-content:flex-end">
        <button class="sb-btn2" onclick="hideAddRuleForm()">取消</button>
        <button class="qa-btn" onclick="submitNewRule()" style="padding:6px 16px;font-size:12px">创建</button>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">当前规则</div>
        <div id="rules-list" style="max-height:340px;overflow-y:auto"></div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">触发历史</div>
        <div id="alert-history-list" style="max-height:340px;overflow-y:auto"></div>
      </div>
    </div>
    <!-- Telegram 推送配置卡片 -->
    <div style="margin-top:14px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div style="font-size:13px;font-weight:600">📱 Telegram 告警推送</div>
        <div style="display:flex;align-items:center;gap:8px">
          <span id="tg-status-dot" style="width:8px;height:8px;border-radius:50%;background:#6b7280;display:inline-block"></span>
          <span id="tg-status-label" style="font-size:11px;color:var(--text-muted)">未配置</span>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px">
        <div><label style="font-size:10px;color:var(--text-muted)">Bot Token</label>
          <input id="tg-bot-token" type="password" placeholder="123456789:ABCdef..." style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:11px;margin-top:4px;box-sizing:border-box"></div>
        <div><label style="font-size:10px;color:var(--text-muted)">Chat ID (群组/频道)</label>
          <input id="tg-chat-id" placeholder="-1001234567890" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:11px;margin-top:4px;box-sizing:border-box"></div>
        <div><label style="font-size:10px;color:var(--text-muted)">最低推送级别</label>
          <select id="tg-min-level" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:11px;margin-top:4px">
            <option value="info">Info（全部）</option>
            <option value="warning" selected>Warning（推荐）</option>
            <option value="error">Error</option>
            <option value="critical">Critical（仅最严重）</option>
          </select></div>
      </div>
      <div style="margin-bottom:10px">
        <label style="font-size:10px;color:var(--text-muted)">额外接收方（每行一个：数字用户ID、@公开用户名、超级群/频道 ID -100…）</label>
        <textarea id="tg-recipients" rows="4" placeholder="6107037825&#10;@mychannel&#10;-1001234567890" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:8px;font-size:11px;margin-top:4px;box-sizing:border-box;font-family:ui-monospace,monospace;resize:vertical"></textarea>
        <div style="font-size:9px;color:var(--text-muted);margin-top:4px;line-height:1.4">与上方「主 Chat ID」一并投递；机器人须已在对应私聊/群内。<b>群邀请链接</b>（<code style="font-size:9px">t.me/+…</code>）不能作为 API 的 chat_id，请拉机器人入群后在下方备忘或群内用 <code style="font-size:9px">@userinfobot</code> 等取得 <code style="font-size:9px">-100…</code> 再填入。</div>
      </div>
      <div style="margin-bottom:10px">
        <label style="font-size:10px;color:var(--text-muted)">群链接备忘（仅记录，不会作为发送目标）</label>
        <input id="tg-invite-notes" type="text" placeholder="https://t.me/+xxxx 等" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:11px;margin-top:4px;box-sizing:border-box">
      </div>
      <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
        <label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer">
          <input type="checkbox" id="tg-enabled" onchange="_tgToggleEnabled(this.checked)"> 启用 Telegram 推送
        </label>
        <div style="display:flex;gap:6px;margin-left:auto">
          <button class="sb-btn2" onclick="_saveTelegramConfig()">保存配置</button>
          <button class="sb-btn2" onclick="_testTelegramMsg()">发送测试</button>
        </div>
      </div>
      <div style="margin-top:10px;font-size:10px;color:var(--text-muted)">
        触发场景: 设备掉线 · 每日日报 · VPN失败（critical）· 连续任务失败 · 可观测性规则触发
      </div>
      <div id="notif-config-display" style="font-size:11px;color:var(--text-dim);margin-top:8px"></div>
    </div>
  </div>

  <!-- ═══ 设备分组 ═══ -->
  <div class="page" id="page-groups">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">设备分组管理</h3>
      <div style="display:flex;gap:8px">
        <button class="sb-btn2" onclick="showCreateGroupForm()">+ 新建分组</button>
        <button class="sb-btn2" onclick="loadGroupsPage()">刷新</button>
      </div>
    </div>
    <div id="create-group-form" style="display:none;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;margin-bottom:14px">
      <div style="display:flex;gap:10px;align-items:flex-end">
        <div style="flex:1"><label style="font-size:10px;color:var(--text-muted)">分组名称</label><input id="group-name" placeholder="意大利组" style="width:100%;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:12px;margin-top:4px"></div>
        <div style="flex:1"><label style="font-size:10px;color:var(--text-muted)">颜色标签</label><input id="group-color" type="color" value="#60a5fa" style="height:34px;border:1px solid var(--border);border-radius:6px;margin-top:4px"></div>
        <button class="qa-btn" onclick="createGroup()" style="padding:6px 16px;font-size:12px;height:34px">创建</button>
        <button class="sb-btn2" onclick="document.getElementById('create-group-form').style.display='none'" style="height:34px">取消</button>
      </div>
    </div>
    <div id="groups-container" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:14px">
      <div id="groups-empty-hint" style="grid-column:1/-1;text-align:center;padding:40px;color:var(--text-muted)">
        <div style="font-size:28px;margin-bottom:8px">&#128193;</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:4px">创建设备分组</div>
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:12px">按业务场景分组管理设备，如"TikTok组"、"Telegram组"</div>
        <button class="sb-btn2" onclick="showCreateGroupForm()" style="padding:6px 16px;font-size:12px">+ 新建第一个分组</button>
      </div>
    </div>
  </div>

  <!-- ═══ VPN 管理 ═══ -->
  <div class="page" id="page-vpn-manage">
    <!-- 状态大盘卡片 -->
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
      <div class="vpn-stat-card" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:var(--text-main)" id="vpn-stat-total">-</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">总设备</div>
      </div>
      <div class="vpn-stat-card" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:#22c55e" id="vpn-stat-connected">-</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">VPN 已连接</div>
      </div>
      <div class="vpn-stat-card" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:#ef4444" id="vpn-stat-disconnected">-</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">VPN 断开</div>
      </div>
      <div class="vpn-stat-card" style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:#eab308" id="vpn-stat-failed">-</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">异常设备</div>
      </div>
    </div>

    <!-- 快速操作栏 -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center">
      <button class="qa-btn" onclick="_vpnMgrRefresh()" style="padding:6px 14px;font-size:12px">&#128260; 刷新状态</button>
      <button class="qa-btn" onclick="_vpnMgrStartAll()" style="padding:6px 14px;font-size:12px;color:#22c55e">&#9654; 全部启动</button>
      <button class="qa-btn" onclick="_vpnMgrStopAll()" style="padding:6px 14px;font-size:12px;color:#ef4444">&#9632; 全部停止</button>
      <button class="qa-btn" onclick="_vpnMgrApplyPool()" style="padding:6px 14px;font-size:12px;color:#8b5cf6">&#127793; 按分配启动</button>
      <button class="qa-btn" onclick="_vpnMgrGeoVerify()" style="padding:6px 14px;font-size:12px;color:#06b6d4">&#127760; Geo-IP 验证</button>
      <button class="qa-btn" onclick="_vpnMgrInstallV2RayNG()" style="padding:6px 14px;font-size:12px;color:#f59e0b" title="批量安装 V2RayNG APK（需提前将 APK 放入 apk_repo/）">&#128241; 安装 V2RayNG</button>
      <button class="qa-btn" onclick="_vpnMgrExport()" style="padding:6px 14px;font-size:12px;color:var(--text-muted)">&#128229; 导出</button>
      <div style="margin-left:auto;display:flex;align-items:center;gap:6px">
        <label style="font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:4px">
          <input type="checkbox" id="vpn-mgr-auto-refresh" onchange="_vpnMgrToggleAutoRefresh()" checked> 自动刷新
        </label>
        <label style="font-size:11px;color:var(--text-muted);display:flex;align-items:center;gap:4px">
          <input type="checkbox" id="vpn-mgr-auto-reconnect" onchange="_vpnMgrToggleAutoReconnect()"> 自动重连
        </label>
      </div>
    </div>

    <!-- 两栏布局：配置池 + 设备状态 -->
    <div style="display:grid;grid-template-columns:340px 1fr;gap:14px">
      <!-- 左侧：配置池 -->
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <span style="font-size:13px;font-weight:600">&#128274; 配置池</span>
          <div style="display:flex;gap:4px">
            <button class="sb-btn2" onclick="_vpnMgrShowAddConfig()" style="font-size:10px">+ 添加</button>
            <button class="sb-btn2" onclick="_vpnMgrShowImportSub()" style="font-size:10px;color:#8b5cf6">&#128279; 导入订阅</button>
          </div>
        </div>
        <!-- 添加配置表单 -->
        <div id="vpn-pool-add-form" style="display:none;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px;margin-bottom:10px">
          <div style="margin-bottom:6px">
            <input id="vpn-pool-uri" placeholder="粘贴 VPN 链接 (vless://...)" style="width:100%;padding:6px 8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;font-family:monospace">
          </div>
          <div style="display:flex;gap:6px;margin-bottom:6px">
            <input id="vpn-pool-country" placeholder="国家 (如 italy)" style="flex:1;padding:6px 8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px">
            <input id="vpn-pool-label" placeholder="标签 (可选)" style="flex:1;padding:6px 8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px">
          </div>
          <div style="display:flex;gap:6px">
            <button class="sb-btn2" onclick="_vpnMgrAddConfig()" style="background:var(--accent);color:#111;font-size:10px;padding:4px 12px">确认添加</button>
            <button class="sb-btn2" onclick="document.getElementById('vpn-pool-add-form').style.display='none'" style="font-size:10px">取消</button>
          </div>
        </div>
        <!-- 订阅导入表单 -->
        <div id="vpn-sub-import-form" style="display:none;background:var(--bg-main);border:1px solid #7c3aed44;border-radius:8px;padding:10px;margin-bottom:10px">
          <div style="font-size:10px;color:#a78bfa;margin-bottom:6px;font-weight:600">&#128279; 导入订阅 / 批量链接</div>
          <div style="margin-bottom:6px">
            <input id="vpn-sub-url" placeholder="订阅链接 (https://sub.example.com/...)" style="width:100%;padding:6px 8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;font-family:monospace">
          </div>
          <div style="font-size:9px;color:var(--text-dim);margin-bottom:4px">或粘贴多行 URI（每行一个 vless:// vmess:// 等）:</div>
          <div style="margin-bottom:6px">
            <textarea id="vpn-sub-text" rows="3" placeholder="vless://...&#10;vmess://...&#10;trojan://..." style="width:100%;padding:6px 8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:10px;font-family:monospace;resize:vertical"></textarea>
          </div>
          <div style="display:flex;gap:6px;align-items:center;margin-bottom:6px">
            <input id="vpn-sub-country" placeholder="默认国家 (可选)" style="flex:1;padding:6px 8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px">
            <label style="font-size:10px;color:var(--text-muted);display:flex;align-items:center;gap:3px;white-space:nowrap">
              <input type="checkbox" id="vpn-sub-replace"> 清空已有
            </label>
          </div>
          <div style="display:flex;gap:6px">
            <button class="sb-btn2" onclick="_vpnMgrImportSub()" style="background:#8b5cf6;color:#fff;font-size:10px;padding:4px 12px">&#128279; 导入</button>
            <button class="sb-btn2" onclick="document.getElementById('vpn-sub-import-form').style.display='none'" style="font-size:10px">取消</button>
          </div>
          <div id="vpn-sub-result" style="font-size:10px;margin-top:4px;color:var(--text-dim)"></div>
        </div>
        <!-- 配置列表 -->
        <div id="vpn-pool-list" style="max-height:calc(100vh - 380px);overflow-y:auto">
          <div style="text-align:center;color:var(--text-muted);font-size:11px;padding:20px 0">加载中...</div>
        </div>
      </div>

      <!-- 右侧：设备状态表 -->
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
          <span style="font-size:13px;font-weight:600">&#128241; 设备 VPN 状态</span>
          <div style="display:flex;gap:6px;align-items:center">
            <select id="vpn-dev-filter" onchange="_vpnMgrFilterDevices()" style="padding:4px 8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:10px">
              <option value="all">全部</option>
              <option value="connected">已连接</option>
              <option value="disconnected">断开</option>
              <option value="failed">异常</option>
            </select>
          </div>
        </div>
        <div id="vpn-devices-table" style="max-height:calc(100vh - 380px);overflow-y:auto">
          <div style="text-align:center;color:var(--text-muted);font-size:11px;padding:20px 0">加载中...</div>
        </div>
      </div>
    </div>

    <!-- 轮换设置 + 连接趋势图 + 测速 -->
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:14px;align-items:start">
      <!-- 左：轮换引擎 -->
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">&#128260; 自动轮换</div>
        <div style="display:flex;gap:10px;align-items:center;margin-bottom:8px">
          <label style="font-size:11px;display:flex;align-items:center;gap:4px">
            <input type="checkbox" id="vpn-rotation-enabled" onchange="_vpnMgrSaveRotation()"> 启用自动轮换
          </label>
          <select id="vpn-rotation-interval" onchange="_vpnMgrSaveRotation()" style="padding:4px 8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:10px">
            <option value="30">每 30 分钟</option>
            <option value="60">每 1 小时</option>
            <option value="120" selected>每 2 小时</option>
            <option value="360">每 6 小时</option>
            <option value="720">每 12 小时</option>
            <option value="1440">每 24 小时</option>
          </select>
          <select id="vpn-rotation-strategy" onchange="_vpnMgrSaveRotation()" style="padding:4px 8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:10px">
            <option value="round-robin">轮询</option>
            <option value="random">随机</option>
            <option value="country-balanced">国家均衡</option>
          </select>
        </div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">
          <button class="qa-btn" onclick="_vpnMgrRotateNow(false)" style="padding:4px 12px;font-size:11px">&#128260; 立即轮换(仅分配)</button>
          <button class="qa-btn" onclick="_vpnMgrRotateNow(true)" style="padding:4px 12px;font-size:11px;color:#22c55e">&#128260; 轮换并应用</button>
          <button class="qa-btn" onclick="_vpnMgrCreateScheduledRotation()" style="padding:4px 12px;font-size:11px;color:#8b5cf6" title="创建定时任务自动轮换">&#9200; 创建定时任务</button>
        </div>
        <div id="vpn-rotation-status" style="font-size:10px;color:var(--text-dim)">上次轮换: -</div>
        <!-- 测速 -->
        <div style="border-top:1px solid var(--border);margin-top:10px;padding-top:10px">
          <div style="font-size:13px;font-weight:600;margin-bottom:8px">&#128225; 连接质量测试</div>
          <button class="qa-btn" onclick="_vpnMgrSpeedTest()" style="padding:4px 12px;font-size:11px" id="vpn-speed-btn">&#128225; 开始测速</button>
          <div id="vpn-speed-result" style="margin-top:6px;font-size:10px;color:var(--text-muted)"></div>
        </div>
      </div>

      <!-- 右：连接趋势图 -->
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;height:300px;overflow:hidden">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-size:13px;font-weight:600">&#128200; VPN 连接趋势 (24h)</span>
          <button class="sb-btn2" onclick="_vpnMgrLoadChart()" style="font-size:9px">刷新</button>
        </div>
        <div style="position:relative;width:100%;height:240px;overflow:hidden">
          <canvas id="vpn-history-chart" style="max-height:240px"></canvas>
        </div>
      </div>
    </div>

    <!-- 批量配置进度 -->
    <div id="vpn-mgr-progress" style="display:none;margin-top:14px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span id="vpn-mgr-prog-label" style="font-size:12px;color:var(--accent)">配置中...</span>
        <span id="vpn-mgr-prog-count" style="font-size:11px;color:var(--text-muted)"></span>
      </div>
      <div style="height:6px;background:var(--bg-input);border-radius:3px;overflow:hidden">
        <div id="vpn-mgr-prog-bar" style="height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);width:0%;transition:width .3s"></div>
      </div>
      <div id="vpn-mgr-prog-details" style="margin-top:6px;max-height:120px;overflow-y:auto;font-size:10px;color:var(--text-muted)"></div>
    </div>
  </div>

  <!-- ═══ 代理中心 ═══ -->
  <div class="page" id="page-router-manage">
    <!-- 统计卡片 -->
    <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:16px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:var(--text-main)" id="rm-stat-routers">-</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">路由器总数</div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:#22c55e" id="rm-stat-online">-</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">路由器在线</div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:#8b5cf6" id="rm-stat-proxies">-</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">代理账号</div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:16px;text-align:center">
        <div style="font-size:28px;font-weight:700;color:#06b6d4" id="rm-stat-devices">-</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">管理手机</div>
      </div>
    </div>

    <!-- 健康监控横幅 -->
    <div id="rm-health-banner" style="background:var(--bg-card);border:1px solid var(--border);border-radius:10px;padding:10px 14px;margin-bottom:12px;min-height:36px">
      <div style="font-size:10px;color:var(--text-dim)">&#128260; 加载健康监控数据...</div>
    </div>

    <!-- 操作栏 -->
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px;align-items:center">
      <button class="qa-btn" onclick="loadRouterManagePage()" style="font-size:12px">&#128260; 刷新</button>
      <button class="qa-btn" onclick="_rmShowAddRouter()" style="font-size:12px;color:#22c55e">&#43; 添加路由器</button>
      <button class="qa-btn" onclick="_rmShowAddProxy()" style="font-size:12px;color:#8b5cf6">&#128273; 添加代理账号</button>
      <button class="qa-btn" onclick="_rmShowBatchProxy()" style="font-size:12px;color:#06b6d4">&#128203; 批量导入代理</button>
      <button class="qa-btn" onclick="_rmDeployAll()" style="font-size:12px;color:#f59e0b">&#128640; 一键部署全部路由器</button>
      <button class="qa-btn" onclick="_rmRefreshStatus()" style="font-size:12px;color:var(--text-muted)">&#127760; 检测所有出口IP</button>
      <button class="qa-btn" onclick="_rmShowHealthPanel()" style="font-size:12px;color:#ef4444">&#128737; 健康监控</button>
    </div>

    <!-- 三栏布局 -->
    <div style="display:grid;grid-template-columns:1fr 280px;gap:14px">
      <!-- 左：路由器卡片网格 -->
      <div>
        <div style="font-size:13px;font-weight:600;margin-bottom:10px;color:var(--text-main)">
          &#128225; 路由器状态
          <span style="font-size:10px;color:var(--text-dim);font-weight:400;margin-left:8px">每台路由器对应一组手机 · 出口IP实时监控</span>
        </div>
        <div id="rm-router-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:12px">
          <div style="text-align:center;padding:40px;color:var(--text-muted)">&#128260; 加载中...</div>
        </div>
      </div>

      <!-- 右：代理账号列表 -->
      <div>
        <div style="font-size:13px;font-weight:600;margin-bottom:10px;color:var(--text-main)">
          &#128273; 代理账号池
          <span style="font-size:10px;color:var(--text-dim);font-weight:400">SOCKS5 / HTTP</span>
        </div>
        <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:12px;max-height:500px;overflow-y:auto">
          <div id="rm-proxy-list">
            <div style="text-align:center;padding:20px;color:var(--text-muted)">&#128260; 加载中...</div>
          </div>
        </div>
      </div>
    </div>

    <!-- 设备-路由器映射表 -->
    <div style="margin-top:16px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:10px;color:var(--text-main)">
        &#128241; 手机-路由器-出口IP 映射
        <span style="font-size:10px;color:var(--text-dim);font-weight:400;margin-left:8px">每台手机的完整代理路径</span>
      </div>
      <div id="rm-device-map">
        <div style="text-align:center;padding:20px;color:var(--text-muted)">&#128260; 加载中...</div>
      </div>
    </div>

    <!-- 使用指南 -->
    <div style="margin-top:14px;background:rgba(139,92,246,.06);border:1px solid rgba(139,92,246,.2);border-radius:12px;padding:14px;font-size:11px;color:var(--text-muted)">
      <div style="font-weight:600;margin-bottom:8px;color:var(--text-main)">&#128218; 快速上手指南</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:10px">
        <div><strong>1. 添加路由器</strong><br>填写 GL.iNet 路由器的IP和管理密码</div>
        <div><strong>2. 添加代理账号</strong><br>推荐 922S5（美国）/ Proxy-Seller（意大利）</div>
        <div><strong>3. 分配代理→路由器</strong><br>每台路由器分配对应国家的代理账号</div>
        <div><strong>4. 一键部署</strong><br>系统自动生成 Clash 配置并推送到路由器</div>
        <div><strong>5. 手机连 WiFi</strong><br>手机连接路由器 WiFi，自动走目标国家IP</div>
      </div>
    </div>
  </div>

  <!-- ═══ 批量安装APK ═══ -->
  <div class="page" id="page-batch-apk">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">批量安装APK</h3>
      <button class="sb-btn2" onclick="loadBatchApkPage()">刷新设备</button>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:14px">
      <div id="apk-drop-zone" style="border:2px dashed var(--border);border-radius:10px;padding:30px;text-align:center;cursor:pointer;transition:border-color .2s"
           ondragover="event.preventDefault();this.style.borderColor='var(--accent)'"
           ondragleave="this.style.borderColor='var(--border)'"
           ondrop="event.preventDefault();this.style.borderColor='var(--border)';handleApkDrop(event)"
           onclick="document.getElementById('apk-file-input').click()">
        <div style="font-size:32px;margin-bottom:8px">&#128230;</div>
        <div style="font-size:13px;color:var(--text-main)">拖拽APK文件到此处，或点击选择</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">支持 .apk 文件</div>
        <input type="file" id="apk-file-input" accept=".apk" style="display:none" onchange="handleApkSelect(this)"/>
      </div>
      <div id="apk-selected-info" style="display:none;margin-top:10px;padding:10px;background:var(--bg-main);border-radius:8px">
        <span id="apk-file-name" style="font-size:12px;font-weight:600"></span>
        <span id="apk-file-size" style="font-size:11px;color:var(--text-dim);margin-left:8px"></span>
      </div>
    </div>
    <div style="display:flex;align-items:center;gap:10px;margin-bottom:14px">
      <label style="font-size:12px;color:var(--text-dim)">目标设备:</label>
      <select id="apk-target" style="padding:6px;min-width:200px;max-width:min(420px,92vw);background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px">
        <option value="all">全部在线（本机 ADB）</option>
      </select>
      <button class="dev-btn" onclick="batchInstallApk()" style="background:var(--green);color:#111;padding:6px 18px">开始安装</button>
    </div>
    <div id="apk-progress" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px"></div>
  </div>

  <!-- ═══ 批量文字输入 ═══ -->
  <div class="page" id="page-batch-text">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">批量文字输入</h3>
      <div style="display:flex;gap:8px">
        <button class="sb-btn2" onclick="addBatchTextRow()">+ 新增行</button>
        <button class="sb-btn2" onclick="loadBatchTextPage()">刷新</button>
      </div>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:14px">
      <div style="font-size:12px;color:var(--text-dim);margin-bottom:10px">为每台设备设置不同的文字内容，执行后自动输入到当前焦点位置</div>
      <div id="batch-text-rows" style="display:grid;gap:8px"></div>
      <div style="display:flex;gap:8px;margin-top:12px">
        <button class="dev-btn" onclick="executeBatchText('type')" style="background:var(--green);color:#111;padding:6px 18px">键入文字</button>
        <button class="dev-btn" onclick="executeBatchText('clipboard')" style="background:var(--accent);color:#111;padding:6px 18px">复制到剪贴板</button>
        <button class="dev-btn" onclick="executeBatchText('broadcast')" style="padding:6px 18px">广播同一文字</button>
      </div>
    </div>
  </div>

  <!-- ═══ 应用管理器 ═══ -->
  <div class="page" id="page-app-manager">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">应用管理器</h3>
      <div style="display:flex;gap:8px">
        <select id="app-mgr-device" style="padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"></select>
        <button class="sb-btn2" onclick="loadAppList()">扫描应用</button>
      </div>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:14px;flex-wrap:wrap">
      <button class="dev-btn" onclick="appBatchAction('start')" style="background:var(--green);color:#111;padding:6px 14px">批量启动</button>
      <button class="dev-btn" onclick="appBatchAction('stop')" style="padding:6px 14px">批量停止</button>
      <button class="dev-btn" onclick="appBatchAction('clear')" style="padding:6px 14px">批量清缓存</button>
      <button class="dev-btn" onclick="appBatchAction('uninstall')" style="background:var(--red);color:#fff;padding:6px 14px">批量卸载</button>
      <input id="app-search" placeholder="搜索应用..." oninput="filterApps()" style="padding:6px 10px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px;width:200px"/>
    </div>
    <div id="app-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px"></div>
  </div>

  <!-- ═══ 话术管理 ═══ -->
  <div class="page" id="page-phrases">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">话术管理</h3>
      <div style="display:flex;gap:8px">
        <button class="sb-btn2" onclick="showAddPhraseGroup()">+ 新建分组</button>
        <button class="sb-btn2" onclick="loadPhrasesPage()">刷新</button>
      </div>
    </div>
    <div id="phrase-groups-container" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:14px"></div>
  </div>

  <!-- ═══ 消息通知 ═══ -->
  <div class="page" id="page-notifications">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">手机消息通知</h3>
      <div style="display:flex;gap:8px">
        <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--text-dim)"><input type="checkbox" id="notif-sound-enabled" checked onchange="toggleNotifSound()"/>提示音</label>
        <button class="sb-btn2" onclick="clearNotifications()">清空</button>
      </div>
    </div>
    <div style="font-size:12px;color:var(--text-dim);margin-bottom:10px">实时转发手机端通知消息到此面板 (需设备支持通知监听)</div>
    <div id="notif-list" style="display:grid;gap:8px;max-height:600px;overflow-y:auto"></div>
  </div>

  <!-- ═══ 性能监控 ═══ -->
  <div class="page" id="page-perf-monitor">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">设备性能监控</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--text-dim)"><input type="checkbox" id="perf-auto-refresh" onchange="togglePerfAutoRefresh()"/>自动刷新</label>
        <button class="sb-btn2" onclick="loadPerfMonitor()">刷新</button>
      </div>
    </div>
    <div id="perf-overview" style="display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-bottom:14px">
      <div class="stat-card blue"><div class="stat-num" id="perf-avg-cpu">-</div><div class="stat-label">平均CPU</div></div>
      <div class="stat-card green"><div class="stat-num" id="perf-avg-mem">-</div><div class="stat-label">平均内存</div></div>
      <div class="stat-card"><div class="stat-num" id="perf-avg-bat">-</div><div class="stat-label">平均电量</div></div>
      <div class="stat-card"><div class="stat-num" id="perf-avg-storage">-</div><div class="stat-label">平均存储</div></div>
    </div>
    <div id="perf-cards" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:12px"></div>
  </div>

  <!-- ═══ 录屏管理 ═══ -->
  <div class="page" id="page-screen-record">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">录屏管理</h3>
      <button class="sb-btn2" onclick="loadScreenRecordPage()">刷新</button>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:14px">
      <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
        <select id="rec-device" style="padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"></select>
        <label style="font-size:11px;color:var(--text-dim)">时长(秒):</label>
        <input id="rec-duration" type="number" value="30" min="5" max="180" style="width:60px;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
        <button class="dev-btn" onclick="startRecording()" style="background:var(--red);color:#fff;padding:6px 14px">&#128308; 开始录屏</button>
        <button class="dev-btn" onclick="stopRecording()" style="padding:6px 14px">&#9724; 停止并保存</button>
      </div>
    </div>
    <h4 style="font-size:13px;color:var(--text-dim);margin-bottom:8px">已保存的录屏</h4>
    <div id="rec-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px"></div>
  </div>

  <!-- ═══ 操作回放 ═══ -->
  <div class="page" id="page-op-replay">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">操作录制与回放</h3>
      <button class="sb-btn2" onclick="loadOpReplayPage()">刷新</button>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">开始录制</div>
        <div style="display:flex;gap:8px;align-items:flex-end;margin-bottom:8px">
          <div style="flex:1"><label style="font-size:10px;color:var(--text-muted)">选择设备</label>
            <select id="oprec-device" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;margin-top:2px"></select></div>
          <div style="flex:1"><label style="font-size:10px;color:var(--text-muted)">录制名称</label>
            <input id="oprec-name" placeholder="录制名称" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;margin-top:2px"/></div>
        </div>
        <div style="display:flex;gap:6px">
          <button class="dev-btn" id="oprec-start-btn" onclick="startOpRecording()" style="background:var(--red);color:#fff;padding:6px 16px;font-size:12px">&#9679; 开始录制</button>
          <button class="dev-btn" id="oprec-stop-btn" onclick="stopOpRecording()" style="padding:6px 16px;font-size:12px;display:none">&#9632; 停止录制</button>
        </div>
        <div id="oprec-status" style="margin-top:6px;font-size:11px;color:var(--text-muted)"></div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">回放设置</div>
        <div style="display:flex;gap:8px;align-items:flex-end;margin-bottom:8px">
          <div style="flex:1"><label style="font-size:10px;color:var(--text-muted)">目标设备 (可选)</label>
            <select id="oprep-device" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;margin-top:2px">
              <option value="">使用原设备</option>
            </select></div>
          <div><label style="font-size:10px;color:var(--text-muted)">速度</label>
            <select id="oprep-speed" style="padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;margin-top:2px">
              <option value="0.5">0.5x</option><option value="1" selected>1x</option><option value="2">2x</option><option value="5">5x</option>
            </select></div>
        </div>
      </div>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:8px">录制文件</div>
      <div id="oprec-list" style="display:grid;gap:8px;max-height:400px;overflow-y:auto"></div>
    </div>
  </div>

  <!-- ═══ 脚本执行器 ═══ -->
  <div class="page" id="page-script-engine">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">脚本模板引擎</h3>
      <div style="display:flex;gap:6px">
        <button class="sb-btn2" onclick="loadScriptTemplates()">模板库</button>
        <button class="sb-btn2" onclick="loadScriptEnginePage()">刷新</button>
      </div>
    </div>
    <!-- 模板库面板 -->
    <div id="tpl-panel" style="display:none;background:var(--bg-card);border:1px solid var(--accent);border-radius:12px;padding:14px;margin-bottom:14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:8px">内置脚本模板 <span style="font-size:10px;color:var(--text-muted)">— 点击使用模板</span></div>
      <div id="tpl-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px">编写脚本</div>
        <input id="script-name" placeholder="脚本名称 (如: setup.sh)" style="width:100%;padding:6px;margin-bottom:8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
        <textarea id="script-content" placeholder="# 支持变量模板: {{device_id}} {{device_alias}} {{device_index}} {{group_name}} {{timestamp}}&#10;echo 'Device {{device_alias}} #{{device_index}}'&#10;pm list packages -3" style="width:100%;height:200px;padding:8px;background:var(--bg-main);color:var(--text-main);border:1px solid var(--border);border-radius:8px;font-family:monospace;font-size:12px;resize:vertical"></textarea>
        <div style="margin:8px 0;padding:6px;background:rgba(96,165,250,.08);border-radius:6px;font-size:10px;color:var(--text-muted)">
          可用变量: <code>{{device_id}}</code> <code>{{device_alias}}</code> <code>{{device_index}}</code> <code>{{group_name}}</code> <code>{{timestamp}}</code> <code>{{date}}</code>
        </div>
        <div style="display:flex;gap:6px;margin-bottom:8px;flex-wrap:wrap;align-items:center">
          <select id="script-type" style="padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px">
            <option value="adb">逐行ADB</option><option value="shell">Shell</option>
          </select>
          <select id="script-target-group" style="padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px">
            <option value="">全部在线设备</option>
          </select>
          <input id="script-custom-vars" placeholder="自定义变量 (如 apk_name=test.apk)" style="flex:1;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px"/>
        </div>
        <div style="display:flex;gap:8px">
          <button class="dev-btn" onclick="saveScript()" style="background:var(--accent);color:#111;padding:6px 14px;font-size:12px">保存</button>
          <button class="dev-btn" onclick="executeSelectedScript()" style="background:var(--green);color:#111;padding:6px 14px;font-size:12px">执行脚本</button>
        </div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px">已保存脚本</div>
        <div id="script-list" style="display:grid;gap:8px;max-height:300px;overflow-y:auto"></div>
        <div style="font-size:13px;font-weight:600;margin:14px 0 8px">执行结果</div>
        <div id="script-results" style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:8px;max-height:300px;overflow-y:auto;font-family:monospace;font-size:11px;color:var(--text-dim)">等待执行...</div>
      </div>
    </div>
  </div>

  <!-- ═══ 批量快捷操作 ═══ -->
  <div class="page" id="page-quick-actions">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">批量快捷操作</h3>
      <button class="sb-btn2" onclick="loadQuickActionsPage()">刷新设备</button>
    </div>
    <div style="font-size:12px;color:var(--text-dim);margin-bottom:14px">一键对全部在线设备执行快捷操作</div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(160px,1fr));gap:10px;margin-bottom:14px" id="qa-buttons">
      <button class="dev-btn" onclick="batchQuickAction('volume_up')" style="padding:12px;font-size:13px">&#128266; 音量+</button>
      <button class="dev-btn" onclick="batchQuickAction('volume_down')" style="padding:12px;font-size:13px">&#128264; 音量-</button>
      <button class="dev-btn" onclick="batchQuickAction('lock')" style="padding:12px;font-size:13px">&#128274; 锁屏</button>
      <button class="dev-btn" onclick="batchQuickAction('unlock')" style="padding:12px;font-size:13px">&#128275; 解锁</button>
      <button class="dev-btn" onclick="batchQuickAction('wifi_on')" style="padding:12px;font-size:13px">&#128246; WiFi开</button>
      <button class="dev-btn" onclick="batchQuickAction('wifi_off')" style="padding:12px;font-size:13px">&#128246; WiFi关</button>
      <button class="dev-btn" onclick="batchQuickAction('airplane_on')" style="padding:12px;font-size:13px">&#9992; 飞行模式开</button>
      <button class="dev-btn" onclick="batchQuickAction('airplane_off')" style="padding:12px;font-size:13px">&#9992; 飞行模式关</button>
      <button class="dev-btn" onclick="batchQuickAction('screenshot')" style="padding:12px;font-size:13px">&#128247; 批量截图</button>
      <button class="dev-btn" onclick="batchBrightness()" style="padding:12px;font-size:13px">&#9728; 调亮度</button>
      <button class="dev-btn" onclick="batchQuickAction('reboot')" style="padding:12px;font-size:13px;background:var(--red);color:#fff">&#128260; 批量重启</button>
    </div>
    <div style="font-size:13px;font-weight:600;margin-bottom:8px">操作结果</div>
    <div id="qa-results" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px"></div>
  </div>

  <!-- ═══ 批量文件上传 ═══ -->
  <div class="page" id="page-batch-upload">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">批量文件上传</h3>
      <button class="sb-btn2" onclick="loadBatchUploadPage()">刷新</button>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;margin-bottom:14px">
      <div id="file-drop-zone" style="border:2px dashed var(--border);border-radius:10px;padding:30px;text-align:center;cursor:pointer;transition:border-color .2s"
           ondragover="event.preventDefault();this.style.borderColor='var(--accent)'"
           ondragleave="this.style.borderColor='var(--border)'"
           ondrop="event.preventDefault();this.style.borderColor='var(--border)';handleFileDrop(event)"
           onclick="document.getElementById('batch-file-input').click()">
        <div style="font-size:32px;margin-bottom:8px">&#128228;</div>
        <div style="font-size:13px;color:var(--text-main)">拖拽文件到此处，或点击选择</div>
        <div style="font-size:11px;color:var(--text-muted);margin-top:4px">支持任意文件（图片、视频、文档等）</div>
        <input type="file" id="batch-file-input" multiple style="display:none" onchange="handleBatchFileSelect(this)"/>
      </div>
      <div id="upload-file-info" style="display:none;margin-top:10px;padding:10px;background:var(--bg-main);border-radius:8px;font-size:12px"></div>
    </div>
    <div style="display:flex;gap:10px;align-items:center;margin-bottom:14px">
      <label style="font-size:12px;color:var(--text-dim)">目标路径:</label>
      <input id="upload-dest" value="/sdcard/Download/" style="flex:1;max-width:300px;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
      <button class="dev-btn" onclick="executeBatchUpload()" style="background:var(--green);color:#111;padding:6px 18px">上传到全部设备</button>
    </div>
    <div id="upload-progress" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:10px"></div>
  </div>

  <!-- ═══ 定时任务 ═══ -->
  <div class="page" id="page-scheduled-jobs">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">定时任务管理</h3>
      <div style="display:flex;gap:8px">
        <button class="sb-btn2" onclick="showAddJobForm()">+ 新建定时任务</button>
        <button class="sb-btn2" onclick="loadScheduledJobsPage()">刷新</button>
      </div>
    </div>
    <div id="add-job-form" style="display:none;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:14px">
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;margin-bottom:10px">
        <div><label style="font-size:10px;color:var(--text-muted);display:block;margin-bottom:3px">任务名称</label>
          <input id="job-name" placeholder="如: 每日养号" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
        <div><label style="font-size:10px;color:var(--text-muted);display:block;margin-bottom:3px">Cron表达式</label>
          <input id="job-cron" placeholder="0 9 * * * (每天9:00)" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
        <div><label style="font-size:10px;color:var(--text-muted);display:block;margin-bottom:3px">操作类型</label>
          <select id="job-action" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px">
            <option value="tiktok_warmup">TikTok 养号</option>
            <option value="tiktok_watch">TikTok 刷视频</option>
            <option value="tiktok_acquisition">TikTok 全流程</option>
            <option value="batch_quick_action">批量快捷操作</option>
            <option value="execute_script">执行脚本</option>
            <option value="deploy_wallpapers">部署壁纸</option>
            <option value="telegram_auto_reply">Telegram自动回复</option>
            <option value="whatsapp_auto_reply">WhatsApp自动回复</option>
          </select></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
        <div><label style="font-size:10px;color:var(--text-muted);display:block;margin-bottom:3px">参数 (JSON，可选，建议先用保守值)</label>
          <input id="job-params" placeholder='{"duration":30}' style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px;font-family:monospace"/></div>
        <div style="display:flex;flex-direction:column;justify-content:flex-end">
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:3px">Cron快捷:</div>
          <div style="display:flex;gap:4px;flex-wrap:wrap">
            <button class="sb-btn2" onclick="document.getElementById('job-cron').value='0 9 * * *';_workflowRenderJobSummary();" style="font-size:9px">每天9:00</button>
            <button class="sb-btn2" onclick="document.getElementById('job-cron').value='*/30 * * * *';_workflowRenderJobSummary();" style="font-size:9px">每30分钟</button>
            <button class="sb-btn2" onclick="document.getElementById('job-cron').value='0 */2 * * *';_workflowRenderJobSummary();" style="font-size:9px">每2小时</button>
            <button class="sb-btn2" onclick="document.getElementById('job-cron').value='0 9,18 * * *';_workflowRenderJobSummary();" style="font-size:9px">早晚各一次</button>
          </div>
        </div>
      </div>
      <div id="job-execution-summary" style="background:var(--bg-main);border:1px dashed var(--border);border-radius:8px;padding:10px;margin-bottom:8px"></div>
      <div style="font-size:10px;color:var(--text-muted);margin-bottom:6px">Cron格式: 分 时 日 月 周 | 示例: <code>*/30 * * * *</code> 每30分钟, <code>0 9,18 * * *</code> 每天9点和18点。建议先以低频启动后逐步提高。</div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="sb-btn2" onclick="document.getElementById('add-job-form').style.display='none'">取消</button>
        <button class="dev-btn" onclick="createScheduledJob()" style="background:var(--accent);color:#111;padding:6px 14px">创建</button>
      </div>
    </div>
    <div id="jobs-list" style="display:grid;gap:10px"></div>
  </div>

  <!-- ═══ 数据导出 ═══ -->
  <div class="page" id="page-data-export">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">数据导出</h3>
    </div>
    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:14px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;text-align:center">
        <div style="font-size:28px;margin-bottom:8px">&#128202;</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:4px">任务数据</div>
        <div style="font-size:11px;color:var(--text-dim);margin-bottom:12px">导出任务执行记录</div>
        <div style="display:flex;gap:6px;justify-content:center">
          <button class="dev-btn" onclick="exportData('tasks',7)" style="padding:6px 12px">近7天</button>
          <button class="dev-btn" onclick="exportData('tasks',30)" style="padding:6px 12px">近30天</button>
          <button class="dev-btn" onclick="exportData('tasks',90)" style="padding:6px 12px">近90天</button>
        </div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;text-align:center">
        <div style="font-size:28px;margin-bottom:8px">&#128241;</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:4px">设备列表</div>
        <div style="font-size:11px;color:var(--text-dim);margin-bottom:12px">导出设备信息和别名</div>
        <button class="dev-btn" onclick="exportData('devices')" style="padding:6px 18px">导出CSV</button>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px;text-align:center">
        <div style="font-size:28px;margin-bottom:8px">&#128200;</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:4px">性能数据</div>
        <div style="font-size:11px;color:var(--text-dim);margin-bottom:12px">导出当前设备性能快照</div>
        <button class="dev-btn" onclick="exportData('performance')" style="padding:6px 18px">导出CSV</button>
      </div>
    </div>

    <!-- 数据导出 -->
    <div style="display:flex;gap:8px;margin-top:16px;flex-wrap:wrap">
      <a href="/analytics/export/data?type=devices&format=csv" download class="dev-btn" style="font-size:12px;text-decoration:none">&#128229; 导出设备数据 CSV</a>
      <a href="/analytics/export/data?type=tasks&format=csv" download class="dev-btn" style="font-size:12px;text-decoration:none">&#128229; 导出任务记录 CSV</a>
      <a href="/analytics/export/data?type=leads&format=csv" download class="dev-btn" style="font-size:12px;text-decoration:none">&#128229; 导出线索数据 CSV</a>
    </div>
  </div>

  <!-- ═══ 设备资产 ═══ -->
  <div class="page" id="page-device-assets">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">设备资产管理</h3>
      <div style="display:flex;gap:8px">
        <button class="sb-btn2" onclick="autoDetectAllAssets()">自动检测全部</button>
        <button class="sb-btn2" onclick="loadDeviceAssetsPage()">刷新</button>
      </div>
    </div>
    <div id="assets-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:12px"></div>
  </div>

  <!-- ═══ AI脚本生成 ═══ -->
  <div class="page" id="page-ai-script">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">AI 脚本生成器</h3>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px">描述你的需求</div>
        <textarea id="ai-script-desc" placeholder="用自然语言描述你想要的操作，例如：&#10;&#10;• 清理所有应用缓存&#10;• 查看手机电池和存储信息&#10;• 打开TikTok并等待3秒&#10;• 关闭所有后台应用" style="width:100%;height:120px;padding:10px;background:var(--bg-main);color:var(--text-main);border:1px solid var(--border);border-radius:8px;font-size:13px;resize:vertical"></textarea>
        <div style="display:flex;gap:8px;margin-top:10px;flex-wrap:wrap">
          <button class="dev-btn" onclick="generateAiScript()" style="background:var(--accent);color:#111;padding:8px 18px;font-size:13px">&#129302; AI生成脚本</button>
          <button class="dev-btn" onclick="saveAiScript()" style="padding:8px 14px">保存脚本</button>
          <button class="dev-btn" onclick="executeAiScript()" style="background:var(--green);color:#111;padding:8px 14px">执行到全部设备</button>
        </div>
        <div style="margin-top:10px">
          <div style="font-size:11px;color:var(--text-dim);margin-bottom:6px">快捷模板:</div>
          <div style="display:flex;gap:4px;flex-wrap:wrap">
            <button class="sb-btn2" onclick="aiQuickDesc('查看手机电池状态和温度')" style="font-size:10px">电池状态</button>
            <button class="sb-btn2" onclick="aiQuickDesc('清理所有第三方应用缓存')" style="font-size:10px">清理缓存</button>
            <button class="sb-btn2" onclick="aiQuickDesc('查看手机存储空间使用情况')" style="font-size:10px">存储信息</button>
            <button class="sb-btn2" onclick="aiQuickDesc('查看当前网络连接和IP地址')" style="font-size:10px">网络状态</button>
            <button class="sb-btn2" onclick="aiQuickDesc('关闭所有后台运行的应用')" style="font-size:10px">关闭后台</button>
            <button class="sb-btn2" onclick="aiQuickDesc('设置屏幕亮度为最大并关闭自动旋转')" style="font-size:10px">屏幕设置</button>
          </div>
        </div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px">生成的脚本</div>
        <textarea id="ai-script-output" placeholder="AI生成的脚本将在此显示..." style="width:100%;height:180px;padding:10px;background:var(--bg-main);color:var(--text-main);border:1px solid var(--border);border-radius:8px;font-family:monospace;font-size:12px;resize:vertical"></textarea>
        <div style="font-size:13px;font-weight:600;margin:10px 0 6px">执行结果</div>
        <div id="ai-script-results" style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:8px;max-height:150px;overflow-y:auto;font-family:monospace;font-size:11px;color:var(--text-dim)">等待执行...</div>
      </div>
    </div>
  </div>

  <!-- ═══ 操作时间线 ═══ -->
  <div class="page" id="page-op-timeline">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">操作时间线</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <div style="display:flex;gap:2px;border:1px solid var(--border);border-radius:6px;overflow:hidden">
          <button class="tl-tab" data-mode="device" onclick="switchTlMode('device')" style="padding:4px 10px;font-size:10px;background:var(--accent);color:#111;border:none;cursor:pointer">设备操作</button>
          <button class="tl-tab" data-mode="user" onclick="switchTlMode('user')" style="padding:4px 10px;font-size:10px;background:transparent;color:var(--text-main);border:none;cursor:pointer">用户审计</button>
        </div>
        <select id="tl-device-filter" onchange="loadOpTimelinePage()" style="padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px">
          <option value="">全部设备</option>
        </select>
        <button class="sb-btn2" onclick="loadOpTimelinePage()">刷新</button>
      </div>
    </div>
    <div id="timeline-container" style="position:relative;padding-left:20px"></div>
  </div>

  <!-- ═══ 同步镜像操作 ═══ -->
  <div class="page" id="page-sync-mirror">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">同步镜像操作</h3>
      <div style="display:flex;gap:8px;align-items:center">
        <label style="font-size:11px;color:var(--text-dim)">在线设备: <span id="sync-count">0</span></label>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">触摸操作 (按百分比坐标)</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
          <div><label style="font-size:10px;color:var(--text-muted)">X% (0-100)</label><input id="sync-x" type="number" value="50" min="0" max="100" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
          <div><label style="font-size:10px;color:var(--text-muted)">Y% (0-100)</label><input id="sync-y" type="number" value="50" min="0" max="100" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:12px">
          <button class="dev-btn" onclick="syncTouch('tap')" style="padding:8px 16px;background:var(--accent);color:#fff">&#128070; 同步点击</button>
          <button class="dev-btn" onclick="syncTouch('long_press')" style="padding:8px 16px">&#9203; 长按</button>
        </div>
        <div style="font-size:12px;font-weight:600;margin-bottom:6px">滑动操作</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">
          <div><label style="font-size:10px;color:var(--text-muted)">终点X%</label><input id="sync-ex" type="number" value="50" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
          <div><label style="font-size:10px;color:var(--text-muted)">终点Y%</label><input id="sync-ey" type="number" value="20" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
        </div>
        <button class="dev-btn" onclick="syncTouch('swipe')" style="padding:8px 16px">&#128070; 同步滑动</button>
        <div style="margin-top:14px;display:flex;gap:6px;flex-wrap:wrap">
          <div style="font-size:11px;color:var(--text-dim);width:100%;margin-bottom:4px">快捷预设:</div>
          <button class="sb-btn2" onclick="syncPreset('scroll_down')" style="font-size:10px">下滑</button>
          <button class="sb-btn2" onclick="syncPreset('scroll_up')" style="font-size:10px">上滑</button>
          <button class="sb-btn2" onclick="syncPreset('swipe_left')" style="font-size:10px">左滑</button>
          <button class="sb-btn2" onclick="syncPreset('swipe_right')" style="font-size:10px">右滑</button>
          <button class="sb-btn2" onclick="syncPreset('center_tap')" style="font-size:10px">中心点击</button>
        </div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">按键 & 文字同步</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:12px">
          <button class="dev-btn" onclick="syncKey(3)" style="padding:8px 14px">Home</button>
          <button class="dev-btn" onclick="syncKey(4)" style="padding:8px 14px">Back</button>
          <button class="dev-btn" onclick="syncKey(187)" style="padding:8px 14px">Recent</button>
          <button class="dev-btn" onclick="syncKey(26)" style="padding:8px 14px">Power</button>
          <button class="dev-btn" onclick="syncKey(24)" style="padding:8px 14px">Vol+</button>
          <button class="dev-btn" onclick="syncKey(25)" style="padding:8px 14px">Vol-</button>
        </div>
        <div style="margin-bottom:12px">
          <label style="font-size:10px;color:var(--text-muted)">同步输入文字</label>
          <div style="display:flex;gap:8px;margin-top:4px">
            <input id="sync-text-input" placeholder="输入文字..." style="flex:1;padding:8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/>
            <button class="dev-btn" onclick="syncTextInput()" style="padding:8px 14px;background:var(--green);color:#111">发送</button>
          </div>
        </div>
        <div style="font-size:13px;font-weight:600;margin:12px 0 8px">操作结果</div>
        <div id="sync-results" style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:8px;max-height:160px;overflow-y:auto;font-size:11px;color:var(--text-dim)">等待操作...</div>
      </div>
    </div>
  </div>

  <!-- ═══ 健康报告 ═══ -->
  <div class="page" id="page-health-report">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">设备健康报告</h3>
      <div style="display:flex;gap:8px">
        <button class="sb-btn2" onclick="loadHealthReport()">重新生成</button>
        <button class="sb-btn2" onclick="exportHealthReport()">导出报告</button>
      </div>
    </div>
    <div id="health-report-content" style="display:grid;gap:14px"></div>
  </div>

  <!-- ═══ 模板市场 ═══ -->
  <div class="page" id="page-tpl-market">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">操作模板市场</h3>
      <div style="display:flex;gap:8px">
        <button class="sb-btn2" onclick="showCreateTplForm()">+ 分享模板</button>
        <button class="sb-btn2" onclick="loadTplMarketPage()">刷新</button>
      </div>
    </div>
    <div id="tpl-create-form" style="display:none;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:14px">
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:10px">
        <div><label style="font-size:10px;color:var(--text-muted)">模板名称</label><input id="tpl-name" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
        <div><label style="font-size:10px;color:var(--text-muted)">类型</label><select id="tpl-type" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"><option value="script">脚本</option><option value="workflow">工作流</option><option value="macro">宏</option></select></div>
      </div>
      <div style="margin-bottom:8px"><label style="font-size:10px;color:var(--text-muted)">描述</label><input id="tpl-desc" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
      <div style="margin-bottom:8px"><label style="font-size:10px;color:var(--text-muted)">内容</label><textarea id="tpl-content" style="width:100%;height:100px;padding:8px;background:var(--bg-main);color:var(--text-main);border:1px solid var(--border);border-radius:8px;font-family:monospace;font-size:12px"></textarea></div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="sb-btn2" onclick="document.getElementById('tpl-create-form').style.display='none'">取消</button>
        <button class="dev-btn" onclick="createTemplate()" style="background:var(--accent);color:#111;padding:6px 14px">发布模板</button>
      </div>
    </div>
    <div id="tpl-list" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px"></div>
  </div>

  <!-- ═══ 多屏并行操控 ═══ -->
  <div class="page" id="page-multi-screen">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <h3 style="font-size:15px;font-weight:600">多屏并行操控</h3>
      <div style="display:flex;gap:6px;align-items:center">
        <span style="font-size:11px;color:var(--text-muted)">布局:</span>
        <button class="sb-btn2" onclick="setMsLayout(2)" style="font-size:10px">2列</button>
        <button class="sb-btn2" onclick="setMsLayout(3)" style="font-size:10px">3列</button>
        <button class="sb-btn2" onclick="setMsLayout(4)" style="font-size:10px">4列</button>
        <button class="sb-btn2" onclick="setMsLayout(6)" style="font-size:10px">6列</button>
        <button class="dev-btn" onclick="addMsPanel()" style="font-size:10px;padding:4px 10px">+ 添加屏幕</button>
        <button class="sb-btn2" onclick="toggleMsStreamMode()" style="font-size:10px" title="切换MJPEG流/轮询模式">流模式</button>
        <button class="sb-btn2" onclick="refreshAllMsScreens()" style="font-size:10px">刷新全部</button>
      </div>
    </div>
    <div id="ms-panels" style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;height:calc(100vh - 160px);overflow-y:auto">
      <div id="ms-empty-hint" style="grid-column:1/-1;text-align:center;padding:60px 20px;color:var(--text-muted)">
        <div style="font-size:32px;margin-bottom:10px">&#128421;</div>
        <div style="font-size:15px;font-weight:600;margin-bottom:6px">多屏并行操控</div>
        <div style="font-size:12px;color:var(--text-dim);margin-bottom:16px">同时查看和控制多台手机，支持实时画面流</div>
        <button class="dev-btn" onclick="addMsPanel()" style="padding:8px 20px;font-size:13px">+ 添加第一块屏幕</button>
      </div>
    </div>
  </div>

  <!-- ═══ 可视化工作流编辑器 ═══ -->
  <div class="page" id="page-visual-workflow">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:10px">
      <h3 style="font-size:15px;font-weight:600">可视化工作流编辑器</h3>
      <div style="display:flex;gap:6px">
        <input id="wf-name" placeholder="工作流名称" style="padding:5px 10px;background:var(--bg-card);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px;width:140px"/>
        <button class="sb-btn2" onclick="saveVisualWorkflow()">保存</button>
        <button class="sb-btn2" onclick="loadSavedWorkflows()">加载</button>
        <button class="dev-btn" onclick="executeVisualWorkflow()" style="background:var(--accent);color:#111;padding:5px 12px">执行</button>
        <button class="sb-btn2" onclick="clearWorkflowCanvas()" style="color:#f87171">清空</button>
      </div>
    </div>
    <div style="display:flex;gap:10px;height:calc(100vh - 180px)">
      <div id="wf-palette" style="width:180px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:10px;overflow-y:auto;flex-shrink:0">
        <div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:8px">拖拽节点到画布</div>
        <div class="wf-node-tpl" draggable="true" data-type="adb_cmd" style="background:#1e3a5f;border:1px solid #3b82f6;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#9881; ADB 命令</span></div>
        <div class="wf-node-tpl" draggable="true" data-type="tap" style="background:#1e3a5f;border:1px solid #3b82f6;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#128073; 点击坐标</span></div>
        <div class="wf-node-tpl" draggable="true" data-type="swipe" style="background:#1e3a5f;border:1px solid #3b82f6;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#128070; 滑动</span></div>
        <div class="wf-node-tpl" draggable="true" data-type="text_input" style="background:#1e3a5f;border:1px solid #3b82f6;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#9997; 输入文本</span></div>
        <div class="wf-node-tpl" draggable="true" data-type="delay" style="background:#3a2e1e;border:1px solid #eab308;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#9203; 等待延迟</span></div>
        <div class="wf-node-tpl" draggable="true" data-type="key" style="background:#1e3a5f;border:1px solid #3b82f6;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#127929; 按键</span></div>
        <div class="wf-node-tpl" draggable="true" data-type="app_launch" style="background:#1e3b2e;border:1px solid #22c55e;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#128187; 启动App</span></div>
        <div class="wf-node-tpl" draggable="true" data-type="screenshot" style="background:#1e3b2e;border:1px solid #22c55e;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#128247; 截图</span></div>
        <div class="wf-node-tpl" draggable="true" data-type="condition" style="background:#3a1e3a;border:1px solid #a855f7;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#128268; 条件判断</span></div>
        <div class="wf-node-tpl" draggable="true" data-type="loop" style="background:#3a1e3a;border:1px solid #a855f7;border-radius:8px;padding:8px;margin-bottom:6px;cursor:grab;font-size:11px">
          <span>&#128257; 循环</span></div>
      </div>
      <div style="flex:1;position:relative;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;overflow:hidden">
        <canvas id="wf-canvas" style="position:absolute;top:0;left:0;width:100%;height:100%;pointer-events:none;z-index:1"></canvas>
        <div id="wf-board" style="position:absolute;top:0;left:0;width:100%;height:100%;overflow:auto;z-index:2"></div>
      </div>
      <div id="wf-props" style="width:220px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:10px;overflow-y:auto;flex-shrink:0">
        <div style="font-size:11px;font-weight:600;color:var(--text-muted);margin-bottom:8px">节点属性</div>
        <div id="wf-prop-content" style="font-size:11px;color:var(--text-muted)">选择一个节点查看属性</div>
      </div>
    </div>
    <div id="wf-saved-list" style="display:none;margin-top:10px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:12px"></div>
  </div>

  <!-- ═══ 通知中心 ═══ -->
  <div class="page" id="page-notify-center">
    <h3 style="font-size:15px;font-weight:600;margin-bottom:14px">通知推送中心</h3>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">Webhook 配置</div>
        <div style="margin-bottom:8px"><label style="font-size:10px;color:var(--text-muted)">Webhook URL</label>
          <input id="nc-webhook-url" placeholder="https://..." style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;margin-top:2px"/></div>
        <div style="margin-bottom:8px"><label style="font-size:10px;color:var(--text-muted)">类型</label>
          <select id="nc-webhook-type" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px;margin-top:2px">
            <option value="generic">通用 JSON</option><option value="dingtalk">钉钉</option><option value="feishu">飞书</option><option value="slack">Slack</option>
          </select></div>
        <div style="margin-bottom:8px"><label style="font-size:10px;color:var(--text-muted)">静默时段</label>
          <div style="display:flex;gap:6px;margin-top:2px">
            <input id="nc-quiet-start" type="time" value="23:00" style="flex:1;padding:4px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px"/>
            <span style="font-size:11px;color:var(--text-muted);line-height:30px">至</span>
            <input id="nc-quiet-end" type="time" value="07:00" style="flex:1;padding:4px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:11px"/>
          </div></div>
        <div style="display:flex;gap:6px">
          <button class="dev-btn" onclick="saveNotifyConfig()" style="padding:6px 14px;font-size:11px">保存配置</button>
          <button class="sb-btn2" onclick="testNotification()" style="font-size:11px">发送测试</button>
        </div>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">启用的事件</div>
        <div id="nc-events" style="display:grid;gap:4px"></div>
      </div>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:10px">
        <span style="font-size:13px;font-weight:600">推送历史</span>
        <button class="sb-btn2" onclick="loadNotifyCenter()" style="font-size:10px">刷新</button>
      </div>
      <div id="nc-history" style="max-height:300px;overflow-y:auto"></div>
    </div>
  </div>

  <!-- ═══ 备份恢复 ═══ -->
  <div class="page" id="page-backup">
    <h3 style="font-size:15px;font-weight:600;margin-bottom:14px">系统备份与恢复</h3>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-bottom:14px">
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px">导出备份</div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:12px">将所有配置文件、脚本、模板打包为 ZIP 下载</div>
        <button class="dev-btn" onclick="exportBackup()" style="background:var(--accent);color:#111;padding:8px 20px">下载备份 ZIP</button>
      </div>
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:18px">
        <div style="font-size:13px;font-weight:600;margin-bottom:8px">导入恢复</div>
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:12px">上传之前导出的 ZIP 文件恢复配置</div>
        <input type="file" id="backup-file" accept=".zip" style="display:none" onchange="importBackup()"/>
        <button class="dev-btn" onclick="document.getElementById('backup-file').click()" style="padding:8px 20px">选择 ZIP 文件</button>
        <div id="backup-result" style="margin-top:8px;font-size:11px"></div>
      </div>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:8px">配置文件列表</div>
      <div id="config-file-list" style="font-size:11px"></div>
    </div>
  </div>

  <!-- ═══ 插件管理 ═══ -->
  <div class="page" id="page-plugins">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">插件管理</h3>
      <button class="sb-btn2" onclick="reloadAllPlugins()" style="font-size:10px">全部热重载</button>
      <button class="sb-btn2" onclick="loadPluginsPage()">刷新</button>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:14px;font-size:11px;color:var(--text-muted)">
      插件目录: <code>plugins/</code> — 将 Python 文件放入此目录即可被自动发现。每个插件需要一个 <code>plugin_info()</code> 函数和可选的生命周期钩子。
    </div>
    <div id="plugins-list" style="display:grid;gap:10px"></div>
  </div>

  <!-- ═══ 用户管理 ═══ -->
  <div class="page" id="page-user-mgmt">
    <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:14px">
      <h3 style="font-size:15px;font-weight:600">用户管理</h3>
      <button class="sb-btn2" onclick="showAddUserForm()">+ 新建用户</button>
    </div>
    <div id="add-user-form" style="display:none;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-bottom:14px">
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr 1fr;gap:10px;margin-bottom:10px">
        <div><label style="font-size:10px;color:var(--text-muted)">用户名</label><input id="new-user-name" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
        <div><label style="font-size:10px;color:var(--text-muted)">密码</label><input id="new-user-pass" type="password" value="" placeholder="设置强密码" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
        <div><label style="font-size:10px;color:var(--text-muted)">角色</label>
          <select id="new-user-role" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px">
            <option value="admin">管理员</option><option value="operator" selected>操作员</option><option value="viewer">只读</option>
          </select></div>
        <div><label style="font-size:10px;color:var(--text-muted)">显示名</label><input id="new-user-display" style="width:100%;padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
      </div>
      <div style="display:flex;gap:8px;justify-content:flex-end">
        <button class="sb-btn2" onclick="document.getElementById('add-user-form').style.display='none'">取消</button>
        <button class="dev-btn" onclick="createUser()" style="background:var(--accent);color:#111;padding:6px 14px">创建</button>
      </div>
    </div>
    <div id="users-list" style="display:grid;gap:10px"></div>
    <div style="margin-top:20px;background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:8px">修改当前密码</div>
      <div style="display:flex;gap:10px;align-items:flex-end">
        <div><label style="font-size:10px;color:var(--text-muted)">新密码</label><input id="change-pass" type="password" style="padding:6px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;font-size:12px"/></div>
        <button class="dev-btn" onclick="changeMyPassword()" style="padding:6px 14px">修改密码</button>
      </div>
    </div>
  </div>

  <!-- ═══ ROI 面板 ═══ -->
  <div class="page" id="page-roi"><div id="roi-content" style="padding:8px">加载中...</div></div>

  <!-- ═══ 对话监控 ═══ -->
  <div class="page" id="page-conversations">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div>
        <h3 style="margin:0;font-size:16px">&#128172; 对话监控</h3>
        <p style="margin:4px 0 0;font-size:12px;color:var(--text-muted)">实时查看 TikTok AI 对话状态与人工处理队列</p>
      </div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">
        <div style="display:flex;flex-direction:column;align-items:center;gap:2px">
          <button id="auto-monitor-toggle" class="qa-btn" onclick="Conv.toggleMonitor()"
                  data-enabled="0"
                  style="background:var(--bg-main);color:var(--text-muted);border:1px solid var(--border);min-width:160px">
            自动监控: 关闭
          </button>
          <span id="auto-monitor-status" style="font-size:10px;color:var(--text-muted)">点击开启自动回复</span>
        </div>
        <button class="qa-btn" onclick="Conv.startDailyCampaign()" style="background:var(--accent);color:#fff;border-color:var(--accent)">&#9654; 一键启动今日运营</button>
        <button id="batch-reply-btn" class="qa-btn" onclick="Conv.batchAutoReply()" style="background:rgba(99,102,241,.15);color:#818cf8;border-color:rgba(99,102,241,.4)">&#129302; 批量AI回复</button>
        <button class="qa-btn" onclick="Conv.refresh()">&#8635; 刷新</button>
        <button class="qa-btn" onclick="Conv.clearEscalations()" style="border-color:#ef4444;color:#ef4444">清空升级队列</button>
      </div>
    </div>

    <!-- 设备健康状态 -->
    <div id="conv-device-health" style="display:flex;gap:8px;overflow-x:auto;margin-bottom:14px;padding-bottom:4px"></div>

    <!-- 今日统计卡片 -->
    <div id="conv-stats-row" style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;margin-bottom:16px">
      <div class="metric-card"><div class="metric-label">今日关注</div><div class="metric-value" id="cs-follows">-</div></div>
      <div class="metric-card"><div class="metric-label">今日回关</div><div class="metric-value" id="cs-followbacks">-</div></div>
      <div class="metric-card"><div class="metric-label">今日私信</div><div class="metric-value" id="cs-dms">-</div></div>
      <div class="metric-card"><div class="metric-label">AI自动回复</div><div class="metric-value" id="cs-autoreplied">-</div></div>
      <div class="metric-card"><div class="metric-label">待人工处理</div><div class="metric-value" id="cs-escalated" style="color:#ef4444">-</div></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <!-- 人工处理队列 -->
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px;display:flex;justify-content:space-between">
          <span>&#128226; 升级队列 (需人工处理)</span>
          <span id="esc-count" style="background:#ef4444;color:#fff;border-radius:10px;padding:0 7px;font-size:11px">0</span>
        </div>
        <div id="escalation-list" style="display:flex;flex-direction:column;gap:8px;max-height:400px;overflow-y:auto">
          <div style="text-align:center;color:var(--text-muted);padding:20px;font-size:12px">暂无需处理的对话 &#128522;</div>
        </div>
      </div>

      <!-- 最近对话列表 -->
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">&#128172; 最近对话</div>
        <div id="conv-list" style="display:flex;flex-direction:column;gap:6px;max-height:400px;overflow-y:auto">
          <div style="text-align:center;color:var(--text-muted);padding:20px;font-size:12px">加载中...</div>
        </div>
      </div>
    </div>
  </div>

  <!-- 对话详情滑入面板 -->
  <div id="conv-detail-panel" style="display:none;position:fixed;right:0;top:0;bottom:0;width:380px;background:var(--bg-card);border-left:1px solid var(--border);z-index:400;box-shadow:-8px 0 30px rgba(0,0,0,.4);overflow:hidden;flex-direction:column">
    <div style="padding:14px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
      <div>
        <div style="font-weight:600;font-size:14px" id="detail-contact-name">联系人</div>
        <div style="font-size:11px;color:var(--text-muted)" id="detail-contact-meta"></div>
      </div>
      <button onclick="Conv.closeDetail()" style="background:none;border:none;color:var(--text-muted);cursor:pointer;font-size:20px;padding:0">&#10005;</button>
    </div>
    <div id="detail-messages" style="flex:1;overflow-y:auto;padding:12px;display:flex;flex-direction:column;gap:8px"></div>
    <div style="padding:12px;border-top:1px solid var(--border)">
      <div style="display:flex;gap:8px">
        <input id="detail-reply-input" type="text" placeholder="输入回复消息..." style="flex:1;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;padding:8px 10px;color:var(--text-main);font-size:12px" onkeydown="if(event.key==='Enter')Conv.sendReply()">
        <button class="qa-btn" onclick="Conv.sendReply()" style="background:var(--accent);color:#fff;border-color:var(--accent)">发送</button>
      </div>
      <div style="font-size:10px;color:var(--text-muted);margin-top:4px">设备: <span id="detail-device-id"></span></div>
    </div>
  </div>

  <!-- ═══ 客户线索 ═══ -->
  <div class="page" id="page-leads">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div>
        <h3 style="margin:0;font-size:16px">&#128100; 客户线索</h3>
        <p style="margin:4px 0 0;font-size:12px;color:var(--text-muted)">已入库的潜在客户与互动记录</p>
      </div>
      <button class="qa-btn" onclick="Leads.refresh()">&#8635; 刷新</button>
    </div>
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
      <table style="width:100%;border-collapse:collapse;font-size:12px">
        <thead>
          <tr style="border-bottom:1px solid var(--border);color:var(--text-muted)">
            <th style="text-align:left;padding:6px 8px">用户名</th>
            <th style="text-align:left;padding:6px 8px">平台</th>
            <th style="text-align:left;padding:6px 8px">意向</th>
            <th style="text-align:left;padding:6px 8px">状态</th>
            <th style="text-align:left;padding:6px 8px">最后互动</th>
          </tr>
        </thead>
        <tbody id="leads-tbody">
          <tr><td colspan="5" style="text-align:center;color:var(--text-muted);padding:20px">加载中...</td></tr>
        </tbody>
      </table>
    </div>
  </div>

  <!-- ═══ 话术管理 ═══ -->
  <div class="page" id="page-messages">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div>
        <h3 style="margin:0;font-size:16px">&#9997; 话术管理</h3>
        <p style="margin:4px 0 0;font-size:12px;color:var(--text-muted)">配置 TikTok 自动化消息模板（意大利语）</p>
      </div>
      <div style="display:flex;gap:8px">
        <button class="qa-btn" onclick="Msg.save()" style="background:var(--accent);color:#fff;border-color:var(--accent)">&#128190; 保存所有</button>
        <button class="qa-btn" onclick="Msg.refresh()">&#8635; 刷新</button>
      </div>
    </div>

    <!-- 统计卡片 -->
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-bottom:16px">
      <div class="metric-card"><div class="metric-label">已发私信总数</div><div class="metric-value" id="msg-total-sent">-</div></div>
      <div class="metric-card"><div class="metric-label">AI自动回复总数</div><div class="metric-value" id="msg-total-replied">-</div></div>
      <div class="metric-card"><div class="metric-label">目标国家</div><div class="metric-value" id="msg-country" style="font-size:14px">-</div></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
      <!-- 问候消息 -->
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">&#128075; 回关问候语 <span style="font-size:10px;color:var(--text-muted);font-weight:normal">({name} = 用户名)</span></div>
        <div id="msg-greeting-list" style="display:flex;flex-direction:column;gap:8px;margin-bottom:10px"></div>
        <button class="qa-btn" onclick="Msg.addGreeting()" style="width:100%;justify-content:center">+ 添加问候语</button>
      </div>

      <!-- 引流模板 -->
      <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px">
        <div style="font-size:13px;font-weight:600;margin-bottom:10px">&#128640; TG 引流话术 <span style="font-size:10px;color:var(--text-muted);font-weight:normal">({telegram} = TG账号)</span></div>
        <div id="msg-tg-list" style="display:flex;flex-direction:column;gap:8px;margin-bottom:10px"></div>
        <button class="qa-btn" onclick="Msg.addTg()" style="width:100%;justify-content:center">+ 添加引流语</button>
        <div style="font-size:13px;font-weight:600;margin:12px 0 8px">&#128172; WA 引流话术 <span style="font-size:10px;color:var(--text-muted);font-weight:normal">({whatsapp})</span></div>
        <div id="msg-wa-list" style="display:flex;flex-direction:column;gap:8px;margin-bottom:10px"></div>
        <button class="qa-btn" onclick="Msg.addWa()" style="width:100%;justify-content:center">+ 添加WA引流语</button>
      </div>
    </div>

    <!-- 设备引流账号配置 -->
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-top:14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:10px">&#128241; 设备引流账号配置</div>
      <div id="msg-device-refs" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:10px"></div>
    </div>

    <!-- 消息预览 -->
    <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:12px;padding:14px;margin-top:14px">
      <div style="font-size:13px;font-weight:600;margin-bottom:10px">&#128065; 消息预览</div>
      <div style="display:flex;gap:8px;margin-bottom:8px">
        <input id="preview-template" type="text" style="flex:1;background:var(--bg-main);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text-main);font-size:12px" placeholder="输入消息模板，包含 {name}/{telegram}/{whatsapp}">
        <select id="preview-device" style="background:var(--bg-main);border:1px solid var(--border);border-radius:6px;padding:6px 10px;color:var(--text-main);font-size:12px"></select>
        <button class="qa-btn" onclick="Msg.preview()">预览</button>
      </div>
      <div id="preview-result" style="background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:10px;font-size:12px;color:var(--text-muted);min-height:40px">输入模板后点击预览...</div>
    </div>

    <!-- A/B 测试统计 -->
    <div class="card" style="margin-top:16px" id="ab-stats-card">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
        <div style="font-size:13px;font-weight:600">&#129514; 话术 A/B 测试统计</div>
        <button class="dev-btn" onclick="_loadAbStats()" style="font-size:11px">刷新</button>
      </div>
      <div id="ab-stats-body">
        <div style="text-align:center;color:var(--text-muted);padding:20px;font-size:12px">暂无数据（发送话术后显示）</div>
      </div>
    </div>
  </div>

  <!-- ═══ 引流活动 ═══ -->
  <div class="page hidden" id="page-campaigns">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div>
        <h3 style="margin:0;font-size:16px">引流活动管理</h3>
        <div style="font-size:12px;color:var(--text-muted);margin-top:2px">创建并管理自动化引流活动，AI驱动全流程</div>
      </div>
      <button class="sb-btn" onclick="showCreateCampaignModal()">+ 新建活动</button>
    </div>
    <div id="campaign-list"></div>
  </div>

  <!-- 账号养号页面 -->
  <div class="page" id="page-account-farming"><div style="padding:8px;color:var(--text-muted)">加载中...</div></div>

  <!-- ═══ 社交平台控制面板 (通用模板，4个平台共用) ═══ -->
  <div class="page" id="page-plat-tiktok">
  <div id="tt-tabbar" style="display:none"></div>
  <div id="tt-tab-ops">
    <!-- 指挥台：统计 + 全局操作 -->
    <div id="tt-cmd-bar" class="tt-cmd-bar">
      <div class="tt-cmd-stats">
        <span class="tt-cmd-pill">&#128241; 在线 <b id="tt-gs-online">-</b></span>
        <span class="tt-cmd-pill">&#11088; 线索 <b id="tt-gs-leads">-</b></span>
        <span class="tt-cmd-pill">&#128279; 引流配置 <b id="tt-gs-cfg">-</b></span>
        <span class="tt-cmd-pill" title="自动刷新状态">&#128338; <span id="tt-refresh-label" style="font-size:10px;color:var(--text-muted)">自动刷新</span><span id="tt-refresh-dot"></span></span>
        <span class="tt-cmd-pill" title="Worker-03 事件桥接状态" id="tt-w03-pill" style="cursor:default">&#127760; W03 <span id="tt-w03-indicator" style="display:inline-block;width:7px;height:7px;border-radius:50%;background:#475569;margin-left:3px;vertical-align:middle;transition:background .3s" title="等待 W03 事件..."></span></span>
      </div>
  <div class="tt-cmd-actions tt-cmd-tier-primary">
    <button id="tt-pitch-btn" class="tt-cmd-btn primary" onclick="ttPitchHotLeads(this)" title="对高意向线索批量发送转化话术">&#128176; 发话术</button>
    <button id="tt-inbox-btn" class="tt-cmd-btn primary" onclick="ttCheckInbox()" title="为所有在线设备提交「检查收件箱」任务" style="background:rgba(99,102,241,.9);border-color:rgba(99,102,241,.95)">&#128172; 批量收件箱</button>
    <button class="tt-cmd-btn" onclick="_loadTtDeviceGrid()" title="刷新设备列表与统计">&#8635; 刷新</button>
    <button class="tt-cmd-btn" onclick="_ttToggleMultiSelect()" id="tt-multi-sel-btn" title="多选设备后批量操作">&#9745; 多选</button>
    <details class="tt-cmd-dd" id="tt-cmd-dd-ops">
      <summary class="tt-cmd-btn tt-cmd-dd-summary">&#128203; 运营与数据</summary>
      <div class="tt-cmd-dd-menu">
        <button type="button" id="tt-launch-btn" class="tt-cmd-dd-item" onclick="ttLaunchCampaign(this); document.getElementById('tt-cmd-dd-ops').removeAttribute('open')" title="为在线设备提交今日全流程任务">&#128203; 工作计划（今日流程）</button>
        <button type="button" class="tt-cmd-dd-item" onclick="ttBatchFlowConfig(); document.getElementById('tt-cmd-dd-ops').removeAttribute('open')" title="为每台在线设备打开执行流程配置">&#9881; 批量配置流程</button>
        <button type="button" class="tt-cmd-dd-item" onclick="_ttShowReportPanel(); document.getElementById('tt-cmd-dd-ops').removeAttribute('open')">&#128202; 运营日报</button>
        <button type="button" class="tt-cmd-dd-item" onclick="_ttShowConversionFunnel(); document.getElementById('tt-cmd-dd-ops').removeAttribute('open')">&#127987; 转化漏斗</button>
        <button type="button" class="tt-cmd-dd-item" onclick="_ttShowABPanel(); document.getElementById('tt-cmd-dd-ops').removeAttribute('open')">&#129514; A/B 实验</button>
      </div>
    </details>
    <details class="tt-cmd-dd" id="tt-cmd-dd-sys">
      <summary class="tt-cmd-btn tt-cmd-dd-summary">&#128295; 系统与集群</summary>
      <div class="tt-cmd-dd-menu">
        <button type="button" id="tt-monitor-btn" class="tt-cmd-dd-item" onclick="Conv&&Conv.toggleMonitor(); document.getElementById('tt-cmd-dd-sys').removeAttribute('open')" title="开启/关闭全机自动检查收件箱（定时）">&#129302; 对话自动监控</button>
        <button type="button" class="tt-cmd-dd-item" onclick="_ttSyncW03Leads(this); document.getElementById('tt-cmd-dd-sys').removeAttribute('open')" title="将远端 Worker 合格线索合并到主控数据库">&#127760; 同步远端线索</button>
        <button type="button" class="tt-cmd-dd-item" onclick="_ttShowWorkerLoad(); document.getElementById('tt-cmd-dd-sys').removeAttribute('open')">&#9881; Worker 负载</button>
        <button type="button" class="tt-cmd-dd-item" onclick="_ttShowContactsEnrichedProbe(); document.getElementById('tt-cmd-dd-sys').removeAttribute('open')" title="检查各节点 API 是否含增强通讯录">&#128300; 版本探针</button>
        <button type="button" class="tt-cmd-dd-item" onclick="_ttShowBatchHistory(); document.getElementById('tt-cmd-dd-sys').removeAttribute('open')">&#128203; 批量操作历史</button>
      </div>
    </details>
  </div>
    </div>
    <div id="tt-ia-hint" style="display:none;margin:0 0 10px;padding:9px 14px;font-size:11px;line-height:1.45;color:var(--text-muted);background:rgba(99,102,241,.09);border:1px solid rgba(99,102,241,.22);border-radius:10px;align-items:flex-start;justify-content:space-between;gap:10px;flex-wrap:wrap">
      <span><b style="color:#a5b4fc">提示</b>：<b>批量收件箱</b>经确认后为<strong>所有就绪设备</strong>创建任务；卡片上的 <b>本机收件</b> / 侧栏 <b>单机快捷</b> 仅针对<strong>当前手机</strong>。</span>
      <button type="button" class="tt-cmd-btn" style="padding:4px 12px;font-size:10px;flex-shrink:0" onclick="window._ttDismissIaHint&&window._ttDismissIaHint()">知道了</button>
    </div>
    <!-- 设备就绪状态栏 -->
    <div id="tt-readiness-bar" style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;background:var(--bg-main);border:1px solid var(--border);border-radius:8px;padding:7px 12px;margin:8px 0 6px">
      <span style="font-size:11px;color:var(--text-muted);flex-shrink:0">&#128202; 设备就绪</span>
      <div id="tt-readiness-summary" style="font-size:12px;color:var(--text-dim);flex:1">— 点击「全检」查看网络/VPN状态</div>
      <button onclick="_ttCheckAllReadiness()" style="font-size:11px;padding:3px 12px;border:1px solid var(--accent);border-radius:6px;background:none;color:var(--accent);cursor:pointer;flex-shrink:0">&#128269; 全检</button>
    </div>
    <div id="tt-ops-audience-prefs" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:11px;margin:0 0 8px;padding:8px 12px;background:rgba(255,255,255,.03);border-radius:8px;border:1px solid var(--border)">
      <span style="color:var(--text-muted);flex-shrink:0" title="快捷按钮与工作计划使用的默认人群预设，存于浏览器 localStorage">&#128101; 默认人群预设</span>
      <button type="button" id="tt-ops-audience-refresh" onclick="ttOpsRefreshAudiencePresets()" title="服务端重载 YAML 并刷新下拉" style="font-size:11px;padding:4px 10px;border-radius:6px;border:1px solid var(--border);background:rgba(255,255,255,.06);color:var(--text-dim);cursor:pointer;flex-shrink:0">↻</button>
      <label style="display:flex;align-items:center;gap:6px;color:var(--text-dim)">获客/养号/关注
        <select id="tt-ops-preset-default" style="max-width:200px;padding:4px 8px;font-size:11px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text)"></select>
      </label>
      <label style="display:flex;align-items:center;gap:6px;color:var(--text-dim)">收件箱
        <select id="tt-ops-preset-inbox" style="max-width:200px;padding:4px 8px;font-size:11px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;color:var(--text)"></select>
      </label>
    </div>
    <!-- 设备卡片网格 -->
    <div id="tt-device-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(175px,1fr));gap:10px;margin-top:4px">
      <div style="grid-column:1/-1;text-align:center;color:var(--text-muted);padding:20px;font-size:12px">加载中...</div>
    </div>
    <!-- 滑入详情面板 -->
    <div id="tt-dev-panel">
      <div id="tt-dev-panel-content"></div>
    </div>
    <div id="tt-dev-overlay" onclick="_ttCloseDevicePanel()"></div>
    <!-- 兼容旧代码的隐藏元素 -->
    <span id="escalation-badge" style="display:none">0</span>
    <span id="tt-rev-hot" style="display:none">-</span>
    <span id="tt-hot-leads-list" style="display:none"></span>
  </div>
  <div id="tt-tab-farming" style="display:none"></div>
  <div id="tt-tab-conv" style="display:none"></div>
  <div id="tt-tab-leads" style="display:none"></div>
  <div id="tt-tab-msg" style="display:none"></div>
</div>
  <div class="page" id="page-plat-telegram"><div class="plat-page"></div></div>
  <div class="page" id="page-plat-whatsapp"><div class="plat-page"></div></div>
  <div class="page" id="page-plat-facebook"><div class="plat-page"></div></div>
  <div class="page" id="page-plat-linkedin"><div class="plat-page"></div></div>
  <div class="page" id="page-plat-instagram"><div class="plat-page"></div></div>
  <div class="page" id="page-plat-twitter"><div class="plat-page"></div></div>

  <!-- ═══ 内容工作室 ═══ -->
  <div class="page" id="page-studio"></div>

<!-- ═══ 一键养号面板 ═══ -->
<div class="modal-overlay" id="campaign-modal" onclick="if(event.target===this)_closeCampaign()" style="display:none">
  <div style="background:var(--bg-card);border:1px solid var(--border);border-radius:16px;padding:24px;max-width:560px;width:90vw;max-height:85vh;overflow-y:auto;position:relative">
    <button onclick="_closeCampaign()" style="position:absolute;right:14px;top:10px;background:none;border:none;color:var(--text-muted);font-size:20px;cursor:pointer">&times;</button>
    <div style="font-size:18px;font-weight:700;margin-bottom:16px">&#127793; 一键养号</div>

    <!-- 国家选择 -->
    <div style="font-size:11px;color:var(--text-muted);margin-bottom:6px">选择目标国家</div>
    <div id="campaign-countries" style="display:flex;flex-wrap:wrap;gap:8px;margin-bottom:14px">
      <button class="campaign-country selected" data-country="italy" onclick="_selectCampaignCountry(this)" style="padding:8px 16px;border-radius:8px;border:2px solid var(--accent);background:rgba(59,130,246,.1);cursor:pointer;font-size:14px;color:var(--text-main)">&#127470;&#127481; 意大利</button>
      <button class="campaign-country" data-country="us" onclick="_selectCampaignCountry(this)" style="padding:8px 16px;border-radius:8px;border:2px solid var(--border);background:transparent;cursor:pointer;font-size:14px;color:var(--text-main)">&#127482;&#127480; 美国</button>
      <button class="campaign-country" data-country="germany" onclick="_selectCampaignCountry(this)" style="padding:8px 16px;border-radius:8px;border:2px solid var(--border);background:transparent;cursor:pointer;font-size:14px;color:var(--text-main)">&#127465;&#127466; 德国</button>
      <button class="campaign-country" data-country="france" onclick="_selectCampaignCountry(this)" style="padding:8px 16px;border-radius:8px;border:2px solid var(--border);background:transparent;cursor:pointer;font-size:14px;color:var(--text-main)">&#127467;&#127479; 法国</button>
      <button class="campaign-country" data-country="uk" onclick="_selectCampaignCountry(this)" style="padding:8px 16px;border-radius:8px;border:2px solid var(--border);background:transparent;cursor:pointer;font-size:14px;color:var(--text-main)">&#127468;&#127463; 英国</button>
      <button class="campaign-country" data-country="japan" onclick="_selectCampaignCountry(this)" style="padding:8px 16px;border-radius:8px;border:2px solid var(--border);background:transparent;cursor:pointer;font-size:14px;color:var(--text-main)">&#127471;&#127477; 日本</button>
    </div>

    <!-- 时长 + 设备范围 -->
    <div style="display:flex;gap:12px;margin-bottom:14px">
      <div style="flex:1">
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">&#9201; 时长</div>
        <select id="campaign-duration" style="width:100%;padding:8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:8px;font-size:13px">
          <option value="15">15 分钟</option>
          <option value="30" selected>30 分钟</option>
          <option value="45">45 分钟</option>
          <option value="60">60 分钟</option>
        </select>
      </div>
      <div style="flex:1">
        <div style="font-size:11px;color:var(--text-muted);margin-bottom:4px">&#128241; 设备</div>
        <select id="campaign-scope" style="width:100%;padding:8px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:8px;font-size:13px">
          <option value="all">全部在线设备</option>
          <option value="ready">仅就绪设备</option>
        </select>
      </div>
    </div>

    <!-- VPN 选项 -->
    <div style="display:flex;gap:12px;margin-bottom:14px">
      <label style="font-size:12px;display:flex;align-items:center;gap:4px;color:var(--text-main)">
        <input type="checkbox" id="campaign-auto-vpn" checked> &#128274; 自动部署 VPN
      </label>
    </div>

    <!-- 操作按钮 -->
    <div style="display:flex;gap:10px">
      <button id="campaign-launch-btn" onclick="_launchCampaign()" style="flex:1;padding:12px;background:linear-gradient(135deg,#22c55e,#16a34a);color:#111;border:none;border-radius:10px;font-size:15px;font-weight:700;cursor:pointer;transition:transform .15s" onmouseenter="this.style.transform='scale(1.02)'" onmouseleave="this.style.transform='scale(1)'">&#128640; 启动养号</button>
      <button onclick="_createCampaignSchedule()" style="padding:12px 18px;background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:10px;font-size:13px;cursor:pointer">&#9200; 创建定时计划</button>
    </div>

    <!-- 进度区 -->
    <div id="campaign-progress" style="display:none;margin-top:14px">
      <div style="display:flex;align-items:center;gap:8px;margin-bottom:6px">
        <span id="campaign-prog-label" style="font-size:13px;font-weight:600;color:var(--accent)"></span>
        <span id="campaign-prog-count" style="font-size:11px;color:var(--text-muted)"></span>
      </div>
      <div style="height:6px;background:var(--bg-input);border-radius:3px;overflow:hidden;margin-bottom:6px">
        <div id="campaign-prog-bar" style="height:100%;background:linear-gradient(90deg,#22c55e,#3b82f6);width:0%;transition:width .5s"></div>
      </div>
      <div id="campaign-prog-details" style="max-height:150px;overflow-y:auto;font-size:10px;color:var(--text-muted)"></div>
    </div>
  </div>
</div>

<!-- ═══ 交互控制弹窗 ═══ -->
<div class="modal-overlay" id="screen-modal" onclick="if(event.target===this)closeModal()">
  <div class="modal-box" style="max-width:95vw;width:auto;min-width:600px">
    <div class="modal-header" style="padding:6px 14px;flex-direction:column;gap:2px">
      <div style="display:flex;align-items:center;justify-content:space-between;width:100%">
        <h3 id="modal-title" style="font-size:14px;margin:0">设备控制</h3>
        <button class="modal-close" onclick="closeModal()">&times;</button>
      </div>
      <div style="display:flex;align-items:center;gap:10px;width:100%;font-size:10px;flex-wrap:wrap">
        <span id="ctrl-device-info" style="color:var(--text-muted)">-</span>
        <span class="coord" id="ctrl-coord" style="color:var(--accent);font-family:monospace">-</span>
        <span id="ctrl-zoom-level" style="color:var(--text-muted);font-family:monospace">100%</span>
        <span id="ctrl-status" style="color:#22c55e"></span>
        <span id="stream-stats" style="color:var(--text-muted);font-family:monospace"></span>
      </div>
    </div>
    <div style="display:flex;gap:0;flex:1;min-height:0;overflow:hidden">
      <!-- 左侧按钮栏 -->
      <div class="ctrl-sidebar" style="display:flex;flex-direction:column;gap:0;padding:4px;background:var(--bg-main);border-right:1px solid var(--border);min-width:42px;overflow-y:auto">
        <div class="sb-group" data-open="true">
          <div class="sb-group-title" onclick="this.parentElement.dataset.open=this.parentElement.dataset.open==='true'?'false':'true'">导航 <span class="sb-arrow">&#9662;</span></div>
          <div class="sb-group-body">
            <button class="sb-btn" onclick="sendKey(3)" title="Home">&#127968;</button>
            <button class="sb-btn" onclick="sendKey(4)" title="Back">&#9194;</button>
            <button class="sb-btn" onclick="sendKey(187)" title="Recent">&#9744;</button>
            <button class="sb-btn" onclick="wakeAndUnlock()" title="亮屏解锁" style="background:linear-gradient(135deg,#22c55e,#059669);color:#fff">&#128275;</button>
          </div>
        </div>
        <div class="sb-group" data-open="false">
          <div class="sb-group-title" onclick="this.parentElement.dataset.open=this.parentElement.dataset.open==='true'?'false':'true'">音量 <span class="sb-arrow">&#9662;</span></div>
          <div class="sb-group-body">
            <button class="sb-btn" onclick="sendKey(24)" title="音量+">&#128266;+</button>
            <button class="sb-btn" onclick="sendKey(25)" title="音量-">&#128264;-</button>
            <button class="sb-btn" onclick="sendKey(164)" title="静音">&#128263;</button>
          </div>
        </div>
        <div class="sb-group" data-open="true">
          <div class="sb-group-title" onclick="this.parentElement.dataset.open=this.parentElement.dataset.open==='true'?'false':'true'">屏幕 <span class="sb-arrow">&#9662;</span></div>
          <div class="sb-group-body">
            <button class="sb-btn" onclick="sendKey(26)" title="电源/亮屏" style="font-size:16px">&#9211;</button>
            <button class="sb-btn" onclick="rotateScreen()" title="旋转屏幕" id="rotate-btn">&#128260;</button>
            <button class="sb-btn" onclick="pullNotification()" title="下拉通知栏">&#128227;</button>
            <button class="sb-btn" onclick="openQuickSettings()" title="快捷设置">&#9881;</button>
          </div>
        </div>
        <div class="sb-group" data-open="false">
          <div class="sb-group-title" onclick="this.parentElement.dataset.open=this.parentElement.dataset.open==='true'?'false':'true'">缩放 <span class="sb-arrow">&#9662;</span></div>
          <div class="sb-group-body">
            <button class="sb-btn" onclick="zoomIn()" title="放大">&#128269;+</button>
            <button class="sb-btn" onclick="zoomOut()" title="缩小">&#128269;-</button>
            <button class="sb-btn" onclick="zoomReset()" title="重置">1:1</button>
          </div>
        </div>
        <div class="sb-group" data-open="true">
          <div class="sb-group-title" onclick="this.parentElement.dataset.open=this.parentElement.dataset.open==='true'?'false':'true'">操作 <span class="sb-arrow">&#9662;</span></div>
          <div class="sb-group-body">
            <button class="sb-btn" onclick="captureModalScreen()" title="截屏">&#128247;</button>
            <button class="sb-btn" onclick="saveScreenshot()" title="保存截图">&#128190;</button>
            <button class="sb-btn" onclick="sendKey(82)" title="菜单键">&#9776;</button>
            <button class="sb-btn" onclick="sendKey(61)" title="Tab">&#8677;</button>
          </div>
        </div>
        <div class="sb-group" data-open="false">
          <div class="sb-group-title" onclick="this.parentElement.dataset.open=this.parentElement.dataset.open==='true'?'false':'true'">显示 <span class="sb-arrow">&#9662;</span></div>
          <div class="sb-group-body">
            <button class="sb-btn" id="hud-toggle-btn" onclick="_toggleHud()" title="HUD 信息 (F2)">&#128202;</button>
            <button class="sb-btn" onclick="_toggleFullscreen()" title="全屏 (F11)">&#9974;</button>
          </div>
        </div>
      </div>
      <!-- 中间屏幕区域 (可缩放) -->
      <div id="screen-viewport" style="flex:1;overflow:auto;display:flex;align-items:center;justify-content:center;background:#111;position:relative;min-height:400px">
        <div id="modal-body" style="transform-origin:center center;transition:transform 0.15s ease;position:relative">
          <div class="loading-text">加载中...</div>
        </div>
      </div>
      <!-- 右侧面板 -->
      <div style="display:flex;flex-direction:column;width:280px;border-left:1px solid var(--border);background:var(--bg-main)">
        <!-- 标签栏 -->
        <div style="display:flex;border-bottom:1px solid var(--border);background:var(--bg-card)">
          <button class="rpanel-tab active" data-tab="tools" onclick="switchRightTab('tools')">&#9881; 工具</button>
          <button class="rpanel-tab" data-tab="files" onclick="switchRightTab('files')">&#128193; 文件</button>
          <button class="rpanel-tab" data-tab="clipboard" onclick="switchRightTab('clipboard')">&#128203; 剪贴板</button>
          <button class="rpanel-tab" data-tab="terminal" onclick="switchRightTab('terminal')">&#9000; 终端</button>
          <button class="rpanel-tab" data-tab="ai-assist" onclick="switchRightTab('ai-assist')">&#129302; AI</button>
        </div>
        <!-- 工具面板 -->
        <div class="rpanel-content active" id="rpanel-tools" style="padding:8px;overflow-y:auto">
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">流媒体 & 录制</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:2px">
            <button class="sb-btn2" id="stream-toggle" onclick="toggleStreaming()" title="实时流" style="background:var(--bg-input)">&#9654; 实时流</button>
            <select id="quality-sel" onchange="changeStreamQuality()" title="切换画质" style="background:var(--bg-input);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:3px 6px;font-size:10px;display:none">
              <option value="ultra">超高(6M/60fps)</option>
              <option value="high">高(3M/30fps)</option>
              <option value="medium" selected>中(2M/30fps)</option>
              <option value="low">低(1M/24fps)</option>
              <option value="minimal">极低(500K/15fps)</option>
            </select>
            <button class="sb-btn2" id="record-toggle" onclick="toggleRecording()" title="录屏" style="background:var(--bg-input)">&#9679; 录屏</button>
          </div>
          <!-- 画质切换状态行 -->
          <div id="quality-status-bar" style="display:none;font-size:10px;margin-bottom:6px;padding:3px 6px;border-radius:4px;background:rgba(239,68,68,.15);color:#ef4444;border:1px solid rgba(239,68,68,.3)">
            ⚠️ <span id="quality-status-msg"></span>
            <span style="float:right;cursor:pointer;opacity:.6" onclick="document.getElementById('quality-status-bar').style.display='none'">✕</span>
          </div>
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">宏 & 检测</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
            <button class="sb-btn2" id="macro-rec-btn" onclick="toggleMacroRec()" title="录制宏">&#9899; 录宏</button>
            <button class="sb-btn2" onclick="addSmartMacroStep()" title="智能步骤">&#9889; 智能</button>
            <button class="sb-btn2" onclick="singleDevicePlayMacro()" title="对当前设备播放宏">&#9654; 播放宏</button>
            <button class="sb-btn2" onclick="checkDeviceAnomaly(modalDeviceId)" title="异常检测">&#128270; 检测</button>
          </div>
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">快捷操作</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:6px">
            <button class="sb-btn2" onclick="openAppPicker()" title="打开应用">&#128241; 应用</button>
            <button class="sb-btn2" onclick="getBatteryInfo()" title="电池信息">&#128267; 电池</button>
            <button class="sb-btn2" onclick="inputTextPrompt()" title="输入文字">&#9000; 文字</button>
          </div>
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">安装 APK（本机 ADB）</div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px;align-items:center">
            <input type="file" id="modal-apk-file" accept=".apk" style="display:none" onchange="var el=document.getElementById('modal-apk-fname');if(el)el.textContent=(this.files[0]&&this.files[0].name)||''"/>
            <button class="sb-btn2" onclick="document.getElementById('modal-apk-file').click()" title="选择 APK 文件">&#128230; 选APK</button>
            <span id="modal-apk-fname" style="font-size:9px;color:var(--text-dim);max-width:100px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"></span>
            <button class="sb-btn2" onclick="installApkToModalDevice()" style="color:#22c55e" title="仅当前远控设备（需本机直连）">安装</button>
          </div>
          <div id="modal-apk-status" style="font-size:9px;color:var(--text-dim);margin-bottom:6px;min-height:14px"></div>
          <!-- Android 键盘快捷键面板 -->
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">Android 按键</div>
          <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:3px;margin-bottom:4px">
            <button class="sb-btn2" onclick="sendKey(3)" title="Home (keycode 3)" style="padding:4px 2px;font-size:11px;text-align:center">🏠</button>
            <button class="sb-btn2" onclick="sendKey(4)" title="返回 (keycode 4)" style="padding:4px 2px;font-size:11px;text-align:center">⬅</button>
            <button class="sb-btn2" onclick="sendKey(187)" title="多任务 (keycode 187)" style="padding:4px 2px;font-size:11px;text-align:center">⬛</button>
            <button class="sb-btn2" onclick="wakeAndUnlock()" title="亮屏解锁" style="padding:4px 2px;font-size:10px;text-align:center">🔓</button>
            <button class="sb-btn2" onclick="sendKey(24)" title="音量+ (keycode 24)" style="padding:4px 2px;font-size:11px;text-align:center">🔊</button>
            <button class="sb-btn2" onclick="sendKey(25)" title="音量- (keycode 25)" style="padding:4px 2px;font-size:11px;text-align:center">🔉</button>
            <button class="sb-btn2" onclick="sendKey(164)" title="静音 (keycode 164)" style="padding:4px 2px;font-size:11px;text-align:center">🔇</button>
            <button class="sb-btn2" onclick="_takeScreenshot()" title="截图(Power+VolDown)" style="padding:4px 2px;font-size:11px;text-align:center">📷</button>
            <button class="sb-btn2" onclick="expandNotifications()" title="展开通知栏" style="padding:4px 2px;font-size:10px;text-align:center;grid-column:span 2">🔔 通知栏</button>
            <button class="sb-btn2" onclick="sendKey(66)" title="回车/确认 (keycode 66)" style="padding:4px 2px;font-size:11px;text-align:center">↵</button>
            <button class="sb-btn2" onclick="sendKey(67)" title="退格删除 (keycode 67)" style="padding:4px 2px;font-size:11px;text-align:center">⌫</button>
          </div>
          <div style="font-size:9px;color:var(--text-dim);margin-bottom:6px">键盘快捷键: Esc=返回 Home=主页 F5=截屏 F11=全屏 Ctrl±=缩放</div>
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">VPN · 当前设备 <span id="vpn-scope-device" style="color:var(--accent);font-weight:600"></span></div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px">
            <button class="sb-btn2" onclick="_vpnCheckCurrent()" title="检查当前设备VPN状态">&#128270; 检查</button>
            <button class="sb-btn2" onclick="_vpnStartCurrent()" title="启动当前设备VPN" style="color:#22c55e">&#9654; 启动</button>
            <button class="sb-btn2" onclick="_vpnStopCurrent()" title="停止当前设备VPN" style="color:#ef4444">&#9632; 停止</button>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px">
            <label class="sb-btn2" style="cursor:pointer;display:inline-flex;align-items:center" title="上传二维码配置本机VPN">
              &#128247; 上传QR
              <input type="file" accept="image/*,.txt" style="display:none" onchange="_vpnToolUpload(this,'single')">
            </label>
            <button class="sb-btn2" onclick="_vpnPasteUri('single')" title="粘贴URI配置本机VPN">&#128203; 粘贴URI</button>
          </div>
          <div id="vpn-tool-status" style="font-size:10px;color:var(--text-dim);min-height:16px;margin-bottom:4px"></div>
          <div style="height:1px;background:var(--border);margin:4px 0"></div>
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:4px">VPN · 全部设备 <span id="vpn-scope-all-count" style="color:#a78bfa"></span></div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px">
            <button class="sb-btn2" onclick="_vpnRefreshAll()" title="刷新全部设备VPN状态" style="border-color:#7c3aed44">&#128260; 刷新状态</button>
            <button class="sb-btn2" onclick="_vpnStartAll()" title="启动全部设备VPN" style="color:#22c55e;border-color:#7c3aed44">&#9654; 全部启动</button>
            <button class="sb-btn2" onclick="_vpnStopAll()" title="停止全部设备VPN" style="color:#ef4444;border-color:#7c3aed44">&#9632; 全部停止</button>
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:4px">
            <label class="sb-btn2" style="cursor:pointer;display:inline-flex;align-items:center;border-color:#7c3aed44" title="上传二维码配置全部设备VPN">
              &#128247; 上传QR(全部)
              <input type="file" accept="image/*,.txt" style="display:none" onchange="_vpnToolUpload(this,'all')">
            </label>
            <button class="sb-btn2" onclick="_vpnPasteUri('all')" title="粘贴URI配置全部设备VPN" style="border-color:#7c3aed44">&#128203; 粘贴URI(全部)</button>
          </div>
          <div id="vpn-batch-progress" style="font-size:10px;min-height:16px;margin-bottom:6px;display:none">
            <div style="display:flex;align-items:center;gap:6px;margin-bottom:2px">
              <span id="vpn-prog-label" style="color:var(--accent)">配置中...</span>
              <span id="vpn-prog-count" style="color:var(--text-muted)"></span>
            </div>
            <div style="height:4px;background:var(--bg-input);border-radius:2px;overflow:hidden">
              <div id="vpn-prog-bar" style="height:100%;background:linear-gradient(90deg,#3b82f6,#22c55e);width:0%;transition:width .3s"></div>
            </div>
            <div id="vpn-prog-details" style="margin-top:3px;max-height:80px;overflow-y:auto;font-size:9px;color:var(--text-muted)"></div>
          </div>
          <div style="display:flex;align-items:center;gap:4px">
            <label class="auto-label" style="font-size:10px"><input type="checkbox" id="modal-auto" checked onchange="toggleModalAuto()"> 自动刷新</label>
          </div>
          <div style="font-size:10px;color:var(--text-muted);margin:8px 0 4px 0">自定义按钮</div>
          <div id="custom-buttons-area" style="display:flex;flex-wrap:wrap;gap:4px"></div>
          <button class="sb-btn2" onclick="addCustomButton()" style="margin-top:4px;font-size:9px">+ 添加按钮</button>
        </div>
        <!-- 文件管理面板 -->
        <div class="rpanel-content" id="rpanel-files" style="display:none;flex-direction:column;overflow:hidden">
          <div style="display:flex;align-items:center;gap:4px;padding:6px;border-bottom:1px solid var(--border)">
            <button class="sb-btn2" onclick="fileBrowserUp()" title="上级目录">&#11014;</button>
            <input id="fb-path" type="text" value="/sdcard" onkeydown="if(event.key==='Enter')loadFileList()" style="flex:1;padding:4px 6px;background:var(--bg-input);border:1px solid var(--border);border-radius:4px;color:var(--text-main);font-size:10px;font-family:monospace"/>
            <button class="sb-btn2" onclick="loadFileList()" title="刷新">&#8635;</button>
            <button class="sb-btn2" onclick="fileMkdir()" title="新建文件夹">&#128193;+</button>
          </div>
          <div id="fb-list" style="flex:1;overflow-y:auto;font-size:11px;min-height:200px"></div>
          <div style="padding:4px 6px;border-top:1px solid var(--border);font-size:9px;color:var(--text-muted)" id="fb-status">就绪</div>
        </div>
        <!-- 剪贴板面板 -->
        <div class="rpanel-content" id="rpanel-clipboard" style="display:none;flex-direction:column;padding:8px">
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:6px">PC → 设备</div>
          <textarea id="clip-send-text" placeholder="输入要发送到设备的文字..." style="width:100%;height:80px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;padding:6px;color:var(--text-main);font-size:11px;resize:vertical"></textarea>
          <button class="sb-btn2" onclick="sendClipboard()" style="margin-top:4px;align-self:flex-start">&#128203; 发送到设备</button>
          <div style="height:1px;background:var(--border);margin:10px 0"></div>
          <div style="font-size:10px;color:var(--text-muted);margin-bottom:6px">设备 → PC</div>
          <div id="clip-device-text" style="min-height:60px;background:var(--bg-input);border:1px solid var(--border);border-radius:6px;padding:6px;font-size:11px;color:var(--text-main);word-break:break-all">-</div>
          <div style="display:flex;gap:4px;margin-top:4px">
            <button class="sb-btn2" onclick="getDeviceClipboard()">&#128203; 获取设备剪贴板</button>
            <button class="sb-btn2" onclick="copyDeviceClipToPC()">&#128203; 复制到PC</button>
          </div>
        </div>
        <!-- ADB 终端面板 -->
        <div class="rpanel-content" id="rpanel-terminal" style="display:none;flex-direction:column">
          <div style="padding:4px 8px;font-size:10px;color:var(--text-muted);background:var(--bg-card);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
            <span>ADB 终端</span>
            <button class="sb-btn2" onclick="clearAdbTerminal()" style="font-size:9px;padding:1px 6px">清空</button>
          </div>
          <div id="adb-output" style="flex:1;overflow-y:auto;padding:6px;font-family:monospace;font-size:11px;color:#4ade80;background:#0a0a0a;white-space:pre-wrap;word-break:break-all;min-height:200px"></div>
          <div style="display:flex;border-top:1px solid var(--border)">
            <span style="padding:4px 6px;color:var(--accent);font-family:monospace;font-size:12px">$</span>
            <input id="adb-input" type="text" placeholder="输入 ADB shell 命令..." onkeydown="if(event.key==='Enter')runAdbCmd()" style="flex:1;padding:6px;background:#0a0a0a;border:none;color:#4ade80;font-family:monospace;font-size:12px;outline:none"/>
            <button onclick="runAdbCmd()" style="padding:4px 10px;background:var(--accent);color:#111;border:none;cursor:pointer;font-size:11px">&#9654;</button>
          </div>
        </div>
        <!-- AI 助手面板 -->
        <div class="rpanel-content" id="rpanel-ai-assist" style="display:none;flex-direction:column">
          <div style="padding:4px 8px;font-size:10px;color:var(--text-muted);background:var(--bg-card);border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center">
            <span>AI 操控助手</span>
            <button class="sb-btn2" onclick="clearAiChat()" style="font-size:9px;padding:1px 6px">清空</button>
          </div>
          <div id="ai-chat-output" style="flex:1;overflow-y:auto;padding:8px;font-size:12px;color:var(--text-main);background:#0a0a0a;min-height:200px"></div>
          <div style="padding:4px 8px;font-size:10px;color:var(--text-muted);display:flex;gap:4px;flex-wrap:wrap">
            <button class="sb-btn2" onclick="aiQuickCmd('打开TikTok')" style="font-size:9px">打开TikTok</button>
            <button class="sb-btn2" onclick="aiQuickCmd('打开Telegram')" style="font-size:9px">打开Telegram</button>
            <button class="sb-btn2" onclick="aiQuickCmd('截屏')" style="font-size:9px">截屏</button>
            <button class="sb-btn2" onclick="aiQuickCmd('返回桌面')" style="font-size:9px">返回桌面</button>
            <button class="sb-btn2" onclick="aiQuickCmd('清除应用缓存')" style="font-size:9px">清缓存</button>
          </div>
          <div style="display:flex;border-top:1px solid var(--border)">
            <input id="ai-input" type="text" placeholder="输入自然语言指令，如: 打开TikTok搜索cooking..." onkeydown="if(event.key==='Enter')sendAiCmd()" style="flex:1;padding:6px;background:#0a0a0a;border:none;color:var(--text-main);font-size:12px;outline:none"/>
            <button onclick="sendAiCmd()" style="padding:4px 10px;background:#8b5cf6;color:#fff;border:none;cursor:pointer;font-size:11px">&#9654;</button>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>

<script src="/static/js/jmuxer.min.js" onerror="window._jmuxerFailed=true"></script>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js" onerror="this.onerror=null;this.src='https://unpkg.com/chart.js@4.4.1/dist/chart.umd.min.js'"></script>
<script src="/static/js/core.js?v=20260501a"></script>
<script src="/static/js/devices.js?v=20260413a"></script>
<script src="/static/js/overview.js?v=20260417q"></script>
<script src="/static/js/video-stream.js?v=20260516a"></script>
<script src="/static/js/grid-control.js?v=20260419c"></script>
<script src="/static/js/macros.js?v=20260330g"></script>
<script src="/static/js/batch-ops.js?v=20260417r"></script>
<script src="/static/js/tasks-chat.js?v=20260501a"></script>
<script src="/static/js/analytics.js?v=20260408b"></script>
<script src="/static/js/cluster-ops.js?v=20260417r"></script>
<script src="/static/js/alerts-notify.js?v=20260408d"></script>
<script src="/static/js/platforms.js?v=20260417h"></script>
<script src="/static/js/device-mgmt.js?v=20260420d"></script>
<script src="/static/js/scripts-templates.js?v=20260330g"></script>
<script src="/static/js/workflows.js?v=20260408z"></script>
<script src="/static/js/system.js?v=20260330g"></script>
<script src="/static/js/vpn-manage.js?v=20260330h"></script>
<script src="/static/js/router-manage.js?v=20260411b"></script>
<script src="/static/js/conversations.js?v=20260404d"></script>
<script src="/static/js/messages.js?v=20260404a"></script>
<script src="/static/js/account-farming.js?v=20260404a"></script>
<script src="/static/js/campaigns.js"></script>
<script src="/static/js/platform-shell.js?v=20260420c"></script>
<script src="/static/js/tiktok-ops.js?v=20260417i"></script>
<script src="/static/js/facebook-ops.js?v=20260430a"></script>
<script src="/static/js/lead-mesh-ui.js?v=20260426pr66c"></script>
<script src="/static/js/platform-grid.js?v=20260417a"></script>
<script src="/static/js/studio.js?v=20260411f"></script>
<script>
/* 后备加载：确保屏幕监控页面始终能加载设备 */
setTimeout(async function(){
  try{
    const grid=document.getElementById('screen-grid');
    if(!grid||grid.textContent.trim()!=='加载中...') return;
    console.log('[fallback] screen-grid stuck, forcing load...');
    /* 强制加载设备 */
    try{await loadDevices();}catch(e){console.error('[fallback] loadDevices:',e);}
    /* 如果本地无设备,启用集群 */
    if(!allDevices.length||allDevices.every(d=>d._isCluster)){
      const cb=document.getElementById('show-cluster-devices');
      if(cb&&!cb.checked){cb.checked=true;_showCluster=true;}
      try{
        const resp=await fetch('/cluster/devices').then(r=>r.json());
        const cd=resp.devices||resp||[];
        if(Array.isArray(cd)&&cd.length){
          _clusterDevices=cd.map(d=>{d._isCluster=true;d.host_name=d.host_name||'remote';return d;});
          const localIds=new Set(allDevices.map(d=>d.device_id));
          cd.forEach(d=>{if(!localIds.has(d.device_id)){d._isCluster=true;allDevices.push(d);}});
        }
      }catch(e){console.error('[fallback] cluster:',e);}
    }
    console.log('[fallback] allDevices='+allDevices.length+', _clusterDevices='+_clusterDevices.length);
    if(allDevices.length||_clusterDevices.length){
      try{renderScreens();}catch(e){
        console.error('[fallback] renderScreens:',e);
        grid.innerHTML='<div style="color:#f87171;padding:20px">渲染失败: '+e.message+'<br><a href="#" onclick="location.reload()" style="color:#60a5fa">刷新页面</a></div>';
      }
    }else{
      grid.innerHTML='<div style="color:var(--text-muted);padding:20px">未发现设备。请确认手机已连接并刷新页面。<br><a href="#" onclick="location.reload()" style="color:#60a5fa">刷新</a></div>';
    }
  }catch(e){console.error('[fallback] error:',e);}
},3000);
</script>
</body>
</html>"""


import hashlib as _hashlib

@router.get("/dashboard", response_class=HTMLResponse, operation_id="get_dashboard_spa")
def dashboard(request: Request):
    from fastapi.responses import Response

    from src.openclaw_env import openclaw_port

    _p = str(openclaw_port())
    html = DASHBOARD_HTML.replace("@@OC_PORT@@", _p)
    etag = _hashlib.md5(html.encode("utf-8")).hexdigest()[:16]
    client_etag = request.headers.get("if-none-match", "")
    if client_etag and client_etag.strip('"') == etag:
        return Response(status_code=304)
    return HTMLResponse(
        content=html,
        headers={
            "ETag": f'"{etag}"',
            "Cache-Control": "no-cache",
        },
    )
