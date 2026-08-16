# -*- coding: utf-8 -*-
"""控制台侧栏（P4-6 第一刀拆出；2026-08-14 第二刀：菜单单一真相 NAV_SPEC + 页面族）。

本文件是**菜单结构的单一真相**：
  * ``NAV_SPEC`` —— 分组 / 条目 / 页面族（family）/ 角色可见性 全部在这一张表里；
  * 侧栏 HTML 与 ``window.__NAV``（页面族页签、标题、搜索索引数据）都由它派生；
  * 改菜单只改 NAV_SPEC，不要手写 HTML。

页面族（family）说明：56 项菜单收敛到 31 项的主要手法——把同一功能域的既有页面
收编为「一个菜单项 + 页内页签条」。页面 div / loader / hash 一律不动：
``#batch-apk`` 这类老链接照常直达（页签条自动点亮所属家族），物理合并推迟到各页大改时再做。
家族子页会生成隐藏的 ``.fam-child`` 搜索项，Ctrl+K 搜「录屏」仍能直达。

注意：品牌替换锚点 <h1>OpenClaw</h1> / OpenClaw v1.2.0 必须原样保留——
dashboard.py 服务期 .replace() 依赖这些源串换成智拓 ReachX；改显示名去 src/host/brand.py。
"""
import json

# ── 菜单单一真相 ──────────────────────────────────────────────────
# section 字段：
#   key       data-sec（折叠记忆键；沿用旧键保住用户已存的折叠偏好）
#   cs        客服角色可见（data-cs-section/group）；cs_keep=客服保留的无题组
#   admin     仅管理员（data-admin-only，CSS html[data-role] 首屏前隐藏）
#   collapsed 默认折叠；accent=线索与转化的品牌渐变条；badge=角标 span 的 id
# item 字段：
#   page      页面 id（对应 dashboard.py 的 page-<id>）或 onclick=动作（弹窗/外链）
#   family    [(page, 页签名), …] 首项为菜单默认落点；页面 div/loader 不动
#   factions  [(动作key, 页签名), …] 家族页签条上的动作钮（注册表在 overview.js _FAM_ACTIONS）
#   beacon    动作类条目的埋点名（页面类自动用 page id）
#   i18n      沿用词典键（词典在 core.js _i18n，键值须与 label 对齐）
#   emph      strong/medium=客服主战场强调样式
NAV_SPEC = [
    {"key": None, "cs_keep": True, "items": [
        {"page": "overview", "label": "总览", "icon": "&#9632;", "i18n": "overview", "active": True},
    ]},
    # P2（2026-08-14 下午）：客服四件套页面化——lead-mesh 弹窗经 overview.js 的
    # 页面宿主垫片渲染进 page-cs-* 容器（lead-mesh-ui.js 零改动），URL 可寻址、刷新不丢。
    {"key": "leads", "label": "线索与转化", "i18n": "sec-leads", "cs": True, "accent": True,
     "badge": "cs-pending-badge", "items": [
        {"page": "cs-desk", "label": "我的工作台", "icon": "&#128100;", "emph": "strong"},
        {"page": "cs-inbox", "label": "待接管队列", "icon": "&#128229;", "emph": "medium"},
        {"page": "cs-search", "label": "客户档案", "icon": "&#128270;"},
        {"page": "funnel", "label": "转化分析", "icon": "&#128200;",
         "family": [("funnel", "转化漏斗"), ("roi", "ROI 面板"), ("cs-command", "指挥台")],
         "factions": [("l2", "投屏大屏")]},
    ]},
    {"key": "acquire", "label": "获客作业", "i18n": "sec-acquire", "items": [
        {"page": "plat-tiktok", "label": "TikTok", "icon": "&#127916;"},
        {"page": "plat-telegram", "label": "Telegram", "icon": "&#9992;"},
        {"page": "plat-whatsapp", "label": "WhatsApp", "icon": "&#128172;"},
        {"page": "plat-facebook", "label": "Facebook", "icon": "&#128101;"},
        {"page": "studio", "label": "内容工作室", "icon": "&#127775;"},
        {"page": "tasks", "label": "任务中心", "icon": "&#9881;", "i18n": "tasks"},
        # 「AI 指令」出栏（P2）：页面保留（#chat 直达），入口收编进 Ctrl+K 命令面板
    ]},
    # 2026-08-16：弱平台收编。LinkedIn/Instagram/X 目前只有设备网格壳、无真实执行流程
    # （platform-grid.js:551 明确 toast「该平台暂未实现」），从主菜单「获客作业」移到折叠的
    # 「更多平台」，主菜单只留有真实获客能力的 TikTok/Telegram/WhatsApp/Facebook。
    # page id / loader 完全不变——旧 #plat-linkedin 等深链、Ctrl+K 搜索照常直达。
    {"key": "acquire-more", "label": "更多平台", "i18n": "sec-acquire-more", "collapsed": True, "items": [
        {"page": "plat-linkedin", "label": "LinkedIn", "icon": "&#128188;"},
        {"page": "plat-instagram", "label": "Instagram", "icon": "&#128247;"},
        {"page": "plat-twitter", "label": "X (Twitter)", "icon": "&#120143;"},
    ]},
    {"key": "devices", "label": "设备与网络", "i18n": "sec-devices", "items": [
        {"page": "devices", "label": "设备管理", "icon": "&#9783;", "i18n": "devices",
         "family": [("devices", "设备列表"), ("groups", "设备分组"), ("device-assets", "设备资产")]},
        {"page": "screens", "label": "屏幕墙", "icon": "&#9707;", "i18n": "screen-monitor",
         "family": [("screens", "屏幕监控"), ("multi-screen", "多屏操控"), ("screen-record", "录屏管理")]},
        {"page": "health", "label": "设备健康", "icon": "&#9829;",
         "family": [("health", "健康监控"), ("perf-monitor", "性能监控"), ("health-report", "体检报告")]},
        {"page": "vpn-manage", "label": "VPN 管理", "icon": "&#128274;"},
        {"page": "router-manage", "label": "代理中心", "icon": "&#128225;"},
    ]},
    {"key": "auto", "label": "自动化", "i18n": "sec-auto", "collapsed": True, "items": [
        {"page": "workflows", "label": "编排中心", "icon": "&#9881;",
         "family": [("workflows", "工作流列表"), ("visual-workflow", "可视化画布")]},
        {"page": "script-engine", "label": "脚本工房", "icon": "&#128221;",
         "family": [("script-engine", "脚本模板"), ("ai-script", "AI 生成"),
                    ("op-replay", "操作回放"), ("tpl-market", "模板库")]},
        {"page": "scheduled-jobs", "label": "定时任务", "icon": "&#9200;"},
        # 2026-08-17 P4.5：AI 操盘手（感知→决策→护栏→执行→审计）状态与审计面。
        # 端点 admin/operator 双角色可用，故放非 admin-only 的自动化组。
        {"page": "operator", "label": "AI 操盘手", "icon": "&#129302;"},
        {"page": "quick-actions", "label": "批量群控", "icon": "&#9889;",
         "family": [("quick-actions", "快捷操作"), ("batch-apk", "安装应用"),
                    ("batch-text", "文字输入"), ("batch-upload", "文件上传"),
                    ("app-manager", "应用管理"), ("sync-mirror", "同步输入")]},
    ]},
    {"key": "data", "label": "数据与消息", "i18n": "sec-data", "collapsed": True, "items": [
        {"page": "analytics", "label": "数据分析", "icon": "&#128202;",
         "family": [("analytics", "分析总览"), ("data-export", "数据导出")]},
        {"page": "logs", "label": "日志与审计", "icon": "&#128220;",
         "family": [("logs", "系统日志"), ("audit", "审计日志"), ("op-timeline", "操作时间线")]},
        {"page": "notifications", "label": "消息与告警", "icon": "&#128276;",
         "family": [("notifications", "手机消息"), ("notify-center", "推送渠道"),
                    ("alert-rules", "告警规则")]},
    ]},
    {"key": "system", "label": "系统", "i18n": "sec-system", "collapsed": True, "admin": True, "items": [
        {"page": "cluster", "label": "集群管理", "icon": "&#9741;"},
        {"page": "backup", "label": "备份恢复", "icon": "&#128190;"},
        {"page": "plugins", "label": "插件管理", "icon": "&#128268;"},
        {"page": "user-mgmt", "label": "用户与权限", "icon": "&#128101;"},
        {"onclick": "window.open('/docs','_blank')", "beacon": "api-docs",
         "label": "API 文档", "icon": "&#128196;"},
    ]},
]

_EMPH_STYLE = {
    "strong": ' style="font-weight:600;background:rgba(168,85,247,.08)"',
    "medium": ' style="font-weight:500"',
}


def _item_html(it: dict) -> str:
    icon = it.get("icon", "&#9632;")
    span_attr = f' data-i18n="{it["i18n"]}"' if it.get("i18n") else ""
    style = _EMPH_STYLE.get(it.get("emph", ""), "")
    cls = "nav-item active" if it.get("active") else "nav-item"
    inner = f'<span class="icon">{icon}</span><span{span_attr}>{it["label"]}</span>'
    if "page" in it:
        return f'      <div class="{cls}" data-page="{it["page"]}"{style}>{inner}</div>'
    onclick = f"window._navBeacon&&_navBeacon('{it.get('beacon', 'action')}','action');{it['onclick']}"
    return f'      <div class="{cls}" onclick="{onclick}"{style}>{inner}</div>'


def _fam_child_html(it: dict) -> list[str]:
    """家族子页的隐藏搜索项（CSS 默认 display:none，_filterNav 搜索命中时以 sr-show 显形）。"""
    rows = []
    for pg, lab in it.get("family", []):
        rows.append(
            f'      <div class="nav-item fam-child" data-page="{pg}">'
            f'<span class="icon">&#183;</span><span>{it["label"]} · {lab}</span></div>'
        )
    return rows


def _section_html(sec: dict) -> str:
    parts: list[str] = []
    collapsed = " collapsed" if sec.get("collapsed") else ""
    if sec.get("label"):
        attrs = f' onclick="_toggleSection(this)" data-sec="{sec["key"]}"'
        if sec.get("cs"):
            attrs += ' data-cs-section="1"'
        if sec.get("admin"):
            attrs += ' data-admin-only="1"'
        if sec.get("accent"):
            attrs += (' style="background:linear-gradient(135deg,rgba(168,85,247,.18),'
                      'rgba(96,165,250,.18));border-left:3px solid var(--accent-2)"')
        badge = ""
        if sec.get("badge"):
            badge = (f'\n      <span id="{sec["badge"]}" style="display:none;margin-left:6px;'
                     'font-size:10px;padding:1px 6px;background:var(--red-strong);color:#fff;'
                     'border-radius:8px;font-weight:600">0</span>')
        i18n = sec.get("i18n", "")
        label_span = (f'<span data-i18n="{i18n}">{sec["label"]}</span>' if i18n
                      else f'<span>{sec["label"]}</span>')
        parts.append(f'    <div class="nav-section{collapsed}"{attrs}>\n'
                     f'      {label_span}{badge}\n'
                     '      <span class="sec-arrow">&#9660;</span>\n    </div>')
    grp_attrs = ""
    if sec.get("cs"):
        grp_attrs += ' data-cs-group="1"'
    if sec.get("cs_keep"):
        grp_attrs += ' data-cs-keep="1"'
    if sec.get("admin"):
        grp_attrs += ' data-admin-only="1"'
    rows: list[str] = []
    for it in sec["items"]:
        rows.append(_item_html(it))
        rows.extend(_fam_child_html(it))
    parts.append(f'    <div class="nav-group{collapsed}"{grp_attrs}>\n'
                 + "\n".join(rows) + "\n    </div>")
    return "\n".join(parts)


def _nav_json() -> str:
    """window.__NAV：页面族定义 + 子页→家族映射 + 页签名（overview.js 渲染页签条/标题用）。"""
    families: dict = {}
    page_family: dict = {}
    tab_label: dict = {}
    for sec in NAV_SPEC:
        for it in sec["items"]:
            fam = it.get("family")
            if not fam:
                continue
            root = it["page"]
            families[root] = {
                "label": it["label"],
                "tabs": [{"page": p, "label": lab} for p, lab in fam],
                "actions": [{"act": a, "label": lab} for a, lab in it.get("factions", [])],
            }
            for p, lab in fam:
                page_family[p] = root
                tab_label[p] = lab
    return json.dumps(
        {"families": families, "pageFamily": page_family, "tabLabel": tab_label},
        ensure_ascii=False,
    )


_SIDEBAR_SCRIPTS = r"""
    <script>
    /* 待接管 badge：/handoffs/count 轻量计数（P2 替代旧「拉 200 条数长度」）+ SSE 即时推 */
    function _setCsBadge(total) {
      const badges = [
        document.getElementById('cs-pending-badge'),
        document.getElementById('ov-cs-pending-badge'),
      ];
      const display = total > 0 ? '' : 'none';
      const text = total > 99 ? '99+' : String(total);
      badges.forEach(function(b) {
        if (b) { b.style.display = display; b.textContent = text; }
      });
    }
    async function _updateCsBadge() {
      try {
        const r = await fetch('/lead-mesh/handoffs/count?state=pending', {
          headers: {'Authorization': 'Bearer ' + (localStorage.getItem('oc_token') || '')}
        });
        if (!r.ok) return;
        const d = await r.json();
        _setCsBadge((d && d.count) || 0);
      } catch (e) {}
    }
    setInterval(_updateCsBadge, 30000);   // 兜底轮询（计数查询极轻）
    setTimeout(_updateCsBadge, 1500);     // 启动 1.5s 后第一次拉

    /* P3: 客服页在场时，SSE 事件直接触发当前页重渲染（防抖 800ms；
       lmOpen* 渲染幂等——同一份内容重画进页面宿主，比等 30s 轮询快一个数量级） */
    var _lmLiveT = null;
    function _lmLiveRefresh() {
      if (_lmLiveT) return;
      _lmLiveT = setTimeout(function () {
        _lmLiveT = null;
        try {
          var pg = (typeof _currentPage !== 'undefined') ? _currentPage : '';
          var hasUser = !!(localStorage.getItem('oc_user') || localStorage.getItem('oc_cs_id'));
          if (pg === 'cs-inbox' && window.lmOpenHandoffInbox) lmOpenHandoffInbox('');
          else if (pg === 'cs-desk' && hasUser && window.lmOpenMyDesk) lmOpenMyDesk();
        } catch (e) {}
      }, 800);
    }

    /* Phase-2: SSE 实时事件订阅 + 桌面通知 */
    (function _subscribeEvents() {
      try {
        if (typeof EventSource === 'undefined') return;
        const es = new EventSource('/lead-mesh/events/stream');
        es.addEventListener('hello', function (e) {
          console.log('[SSE] connected', e.data);
        });
        /* P2: 新交接单/状态迁移 → 角标即时更新（count 随事件带来，零请求）+ 客服页在场重渲染 */
        es.addEventListener('handoff_pending_changed', function (e) {
          try {
            const d = JSON.parse(e.data);
            const p = d.payload || {};
            if (typeof p.count === 'number') _setCsBadge(p.count);
            else _updateCsBadge();
            _lmLiveRefresh();
          } catch (err) {}
        });
        es.addEventListener('handoff_assigned', function (e) {
          try {
            const d = JSON.parse(e.data);
            const p = d.payload || {};
            _toast('🙋 ' + (p.by || '?') + ' 接走客户 ' + (p.peer_name || p.handoff_id.substring(0,8)), 'var(--accent-2)');
            _updateCsBadge();
            _lmLiveRefresh();
            _maybeDesktopNotify('客户被接管', (p.by || '') + ' → ' + (p.peer_name || ''));
          } catch (err) {}
        });
        es.addEventListener('handoff_outcome', function (e) {
          try {
            const d = JSON.parse(e.data);
            const p = d.payload || {};
            const emoji = p.outcome === 'converted' ? '✅' : p.outcome === 'lost' ? '❌' : '⏳';
            _toast(emoji + ' ' + (p.by || '?') + ' 标 ' + (p.peer_name || '?') + ' 为 ' + (p.outcome || '?'), 'var(--green-strong)');
            _updateCsBadge();
            _lmLiveRefresh();
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
              _toast('💬 客户 ' + (p.peer_name || '?') + ' 回了一条 ' + p.content_lang + ' 消息 (' + p.content_len + '字)', 'var(--cyan)');
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
        + 'background:rgba(15,23,42,.95);border:1px solid ' + (color || 'var(--border)')
        + ';color:' + (color || 'var(--text-main)') + ';padding:12px 20px;border-radius:10px;'
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
    /* 角色菜单：显隐由 CSS html[data-role] 在首屏前完成（data-admin-only / data-cs-*）。
       这里只再声明一次 role，防止无头脚本晚于 body 的极端时序。hash 越权由 overview.js _navAllowed 拦。 */
    (function applyRoleMenu(){
      try {
        var role = (localStorage.getItem('oc_role') || 'operator').toLowerCase();
        document.documentElement.setAttribute('data-role', role);
      } catch (e) { console.warn('[role menu] apply failed:', e); }
    })();
    </script>
"""


def _build_sidebar() -> str:
    body = "\n\n".join(_section_html(sec) for sec in NAV_SPEC)
    return (
        '<aside class="sidebar">\n'
        '  <div class="sidebar-logo"><h1>OpenClaw</h1><small>智能群控中心</small></div>\n'
        '  <nav class="sidebar-nav">\n'
        '    <div class="nav-search"><input id="nav-search" placeholder="搜索功能..." '
        'oninput="_filterNav(this.value)"/></div>\n\n'
        + body + "\n\n"
        + "    <script>window.__NAV=" + _nav_json() + ";</script>\n"
        + _SIDEBAR_SCRIPTS
        + "  </nav>\n"
        '  <div class="sidebar-footer"><span>OpenClaw v1.2.0</span></div>\n'
        "</aside>"
    )


SIDEBAR_HTML = _build_sidebar()
