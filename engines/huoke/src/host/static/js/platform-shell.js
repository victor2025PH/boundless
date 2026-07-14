/* platform-shell.js — 公共平台组件库(Sprint 2 P1)
 *
 * 目标:抽出 facebook-ops.js / tiktok-ops.js 共有的通用 UI 模式,
 *      避免每加一个平台(LinkedIn/Twitter/Instagram)就重复同样的 200 行。
 *
 * 暴露(全部挂在 window.PlatShell 命名空间下):
 *   PlatShell.modal.open(id, html, opts)        创建/复用 overlay 模态
 *   PlatShell.modal.close(id)                   关闭模态
 *   PlatShell.cmdBar.render(opts)               渲染指挥栏(返回 DOM)
 *   PlatShell.geo.commonRegions()               GEO 列表(11 国)
 *   PlatShell.geo.renderSelector(opts)          渲染下拉/按钮组
 *   PlatShell.preset.renderCards(presets, opts) 渲染预设卡片网格
 *   PlatShell.referral.modal(platform, opts)    通用引流账号配置弹窗
 *   PlatShell.api.get/post                      薄封装,自动加 API_KEY、错误吐司
 *
 * 设计原则:
 *   - 0 个外部依赖,只用浏览器原生 DOM API
 *   - 与现有 api()/showToast() 全局函数兼容
 *   - 不强制 facebook-ops.js / tiktok-ops.js 立即迁移,可渐进采用
 */

(function (global) {
  'use strict';

  if (global.PlatShell) {
    return;
  }

  // ──────────────────────────────────────────────
  // 1. modal
  // ──────────────────────────────────────────────
  function _ensureModalRoot() {
    let root = document.getElementById('plat-shell-modal-root');
    if (!root) {
      root = document.createElement('div');
      root.id = 'plat-shell-modal-root';
      document.body.appendChild(root);
    }
    return root;
  }

  function modalOpen(id, html, opts) {
    opts = opts || {};
    const root = _ensureModalRoot();
    let overlay = document.getElementById(id);
    if (!overlay) {
      overlay = document.createElement('div');
      overlay.id = id;
      overlay.style.cssText =
        'position:fixed;inset:0;background:rgba(0,0,0,.55);z-index:9999;'
        + 'display:flex;align-items:center;justify-content:center;padding:24px';
      overlay.addEventListener('click', function (e) {
        if (e.target === overlay && opts.closeOnBackdrop !== false) {
          modalClose(id);
        }
      });
      root.appendChild(overlay);
    }
    const maxW = opts.maxWidth || '760px';
    overlay.innerHTML =
      '<div style="background:var(--bg-card);border:1px solid var(--border);'
      + 'border-radius:12px;padding:18px 22px;width:100%;max-width:' + maxW + ';'
      + 'max-height:88vh;overflow:auto;color:var(--text)">'
      + html + '</div>';
    overlay.style.display = 'flex';
    return overlay;
  }

  function modalClose(id) {
    const overlay = document.getElementById(id);
    if (overlay) overlay.style.display = 'none';
  }

  // ──────────────────────────────────────────────
  // 2. cmdBar
  // ──────────────────────────────────────────────
  function cmdBarRender(opts) {
    opts = opts || {};
    const containerId = opts.containerId || 'plat-cmd-bar';
    let parent = opts.parent;
    if (!parent && opts.parentSelector) {
      parent = document.querySelector(opts.parentSelector);
    }
    if (!parent) return null;

    let bar = document.getElementById(containerId);
    if (!bar) {
      bar = document.createElement('div');
      bar.id = containerId;
      bar.style.cssText =
        'background:var(--bg-card);border:1px solid var(--border);'
        + 'border-radius:10px;padding:10px 14px;margin-bottom:12px;'
        + 'display:flex;flex-wrap:wrap;align-items:center;gap:10px';
      parent.insertBefore(bar, parent.firstChild);
    }

    const buttons = (opts.actions || []).map(function (a) {
      const style = a.primary
        ? 'background:linear-gradient(135deg,' + (a.color || '#1877f2')
            + ',#0d6efd);color:#fff;border:none;font-weight:600;padding:6px 14px;font-size:12px'
        : 'padding:6px 12px;font-size:12px';
      return '<button class="qa-btn" onclick="' + a.onclick
        + '" style="' + style + '">' + (a.icon || '') + ' ' + a.label + '</button>';
    }).join('');

    bar.innerHTML =
      '<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">'
      + '<span style="font-size:11px;padding:4px 10px;background:'
      + (opts.badgeBg || 'rgba(24,119,242,.15)') + ';color:'
      + (opts.badgeColor || '#60a5fa')
      + ';border-radius:6px;font-weight:600">'
      + (opts.badge || '') + '</span>'
      + (opts.subtitle ? '<span style="font-size:10px;color:var(--text-muted)">'
        + opts.subtitle + '</span>' : '')
      + '</div>'
      + '<div style="margin-left:auto;display:flex;gap:8px;flex-wrap:wrap">'
      + buttons + '</div>';
    return bar;
  }

  // ──────────────────────────────────────────────
  // 3. GEO
  // ──────────────────────────────────────────────
  const _COMMON_GEO = [
    { code: '',  flag: '🌐', zh: '全球(默认)', lang: 'en' },
    { code: 'IT', flag: '🇮🇹', zh: '意大利', lang: 'it' },
    { code: 'US', flag: '🇺🇸', zh: '美国',  lang: 'en' },
    { code: 'GB', flag: '🇬🇧', zh: '英国',  lang: 'en' },
    { code: 'DE', flag: '🇩🇪', zh: '德国',  lang: 'de' },
    { code: 'FR', flag: '🇫🇷', zh: '法国',  lang: 'fr' },
    { code: 'ES', flag: '🇪🇸', zh: '西班牙', lang: 'es' },
    { code: 'BR', flag: '🇧🇷', zh: '巴西',  lang: 'pt' },
    { code: 'MX', flag: '🇲🇽', zh: '墨西哥', lang: 'es' },
    { code: 'AE', flag: '🇦🇪', zh: '阿联酋', lang: 'ar' },
    { code: 'JP', flag: '🇯🇵', zh: '日本',  lang: 'ja' },
    { code: 'KR', flag: '🇰🇷', zh: '韩国',  lang: 'ko' },
    { code: 'TR', flag: '🇹🇷', zh: '土耳其', lang: 'tr' },
  ];

  function geoCommonRegions() {
    return _COMMON_GEO.slice();
  }

  function geoRenderSelector(opts) {
    opts = opts || {};
    const id = opts.id || 'plat-geo-select';
    const initial = opts.initial || '';
    const regions = opts.regions || _COMMON_GEO;
    const html = '<select id="' + id
      + '" style="background:var(--bg-main);color:var(--text);border:1px solid var(--border);'
      + 'border-radius:6px;padding:6px 10px;font-size:12px">'
      + regions.map(function (r) {
        const sel = (r.code === initial) ? ' selected' : '';
        return '<option value="' + r.code + '"' + sel + '>'
          + r.flag + ' ' + r.zh + '</option>';
      }).join('')
      + '</select>';
    return html;
  }

  function geoGet(id) {
    const el = document.getElementById(id);
    if (!el) return { code: '', lang: '' };
    const code = el.value;
    const r = _COMMON_GEO.find(function (x) { return x.code === code; });
    return { code: code, lang: r ? r.lang : '' };
  }

  // ──────────────────────────────────────────────
  // 4. Preset cards
  // ──────────────────────────────────────────────
  function presetRenderCards(presets, opts) {
    opts = opts || {};
    const onClickFn = opts.onClickFn || 'console.log';
    const ctx = opts.ctxArgs || '';
    return (presets || []).map(function (p) {
      const stepsTxt = (p.steps || []).map(function (s) {
        return (s.type || '').replace(/^[a-z_]+_/, '');
      }).join(' → ');
      return ''
        + '<div onclick="' + onClickFn + "('" + p.key + "'" + (ctx ? ', ' + ctx : '') + ')"'
        + ' style="background:var(--bg-main);border:1px solid var(--border);'
        + 'border-left:4px solid ' + (p.color || '#1877f2')
        + ';border-radius:10px;padding:14px;cursor:pointer;transition:transform .15s"'
        + ' onmouseover="this.style.transform=\'translateY(-2px)\'"'
        + ' onmouseout="this.style.transform=\'\'">'
        + '<div style="font-size:15px;font-weight:700;margin-bottom:4px">'
        + (p.name || p.key) + '</div>'
        + '<div style="font-size:11px;color:var(--text-muted);margin-bottom:8px">'
        + (p.desc || '') + '</div>'
        + '<div style="font-size:10px;color:var(--text-dim);margin-bottom:8px">'
        + (p.detail || '') + '</div>'
        + '<div style="display:flex;justify-content:space-between;font-size:10px">'
        + '<span style="color:var(--text-dim)">≈ '
        + (p.estimated_minutes || '?') + ' 分钟</span>'
        + '<span style="color:' + (p.color || '#1877f2')
        + ';font-weight:600">' + (p.estimated_output || '') + '</span>'
        + '</div>'
        + '<div style="margin-top:8px;font-size:9px;color:var(--text-dim);font-family:monospace">'
        + stepsTxt + '</div>'
        + '<button style="margin-top:10px;width:100%;padding:6px;background:'
        + (p.color || '#1877f2')
        + ';color:#fff;border:none;border-radius:6px;font-size:11px;font-weight:600;cursor:pointer">'
        + '▶ 一键启动</button>'
        + '</div>';
    }).join('');
  }

  // ──────────────────────────────────────────────
  // 5. Lead list 渲染(Sprint 3 P1 — 高分线索表格,带分页/筛选/操作)
  // ──────────────────────────────────────────────
  function _scoreColor(s) {
    if (s >= 80) return '#10b981'; // S 绿
    if (s >= 65) return '#3b82f6'; // A 蓝
    if (s >= 45) return '#f59e0b'; // B 橙
    if (s >= 25) return '#ef4444'; // C 红
    return '#6b7280';              // D 灰
  }
  function _tierBadge(t) {
    const colors = { S: '#10b981', A: '#3b82f6', B: '#f59e0b',
                     C: '#ef4444', D: '#6b7280' };
    const c = colors[t] || '#6b7280';
    return '<span style="display:inline-block;min-width:22px;text-align:center;'
      + 'padding:2px 6px;border-radius:4px;background:' + c
      + ';color:#fff;font-size:10px;font-weight:700">' + (t || '?') + '</span>';
  }

  /** 渲染高分线索表
   * opts:
   *   leads: [{name, score, tier, tags[], reasons[], lead_id}]
   *   onAction(action, lead): 回调("action_request"/"action_open"/"action_skip"...)
   *   showTagsCol: 是否显示 tags 列 (default true)
   *   showReasonsCol: default true
   *   actions: 自定义操作按钮 [{key, label, color}]
   */
  function leadListRender(opts) {
    opts = opts || {};
    const leads = opts.leads || [];
    if (!leads.length) {
      return '<div style="padding:40px;text-align:center;color:var(--text-muted)">'
        + '暂无高分线索 — 跑一次"群成员打招呼"即可。</div>';
    }
    const showTags = opts.showTagsCol !== false;
    const showReasons = opts.showReasonsCol !== false;
    const acts = opts.actions || [
      { key: 'request', label: '加好友', color: '#1877f2' },
      { key: 'open',    label: '查看',  color: '#6b7280' },
    ];
    const cbName = opts.callbackName || '_platShellLeadAction';

    const head = ''
      + '<thead><tr style="background:var(--bg-elev);position:sticky;top:0">'
      + '<th style="padding:8px;text-align:left;font-size:11px">名字</th>'
      + '<th style="padding:8px;width:60px;text-align:center;font-size:11px">档位</th>'
      + '<th style="padding:8px;width:80px;text-align:right;font-size:11px">分数</th>'
      + (showTags    ? '<th style="padding:8px;text-align:left;font-size:11px">来源</th>' : '')
      + (showReasons ? '<th style="padding:8px;text-align:left;font-size:11px">原因</th>' : '')
      + '<th style="padding:8px;width:140px;text-align:center;font-size:11px">操作</th>'
      + '</tr></thead>';

    const rows = leads.map(function (l, idx) {
      const score = l.score || l.final_score || 0;
      const tier = l.tier || l.final_tier || 'D';
      const tags = (l.tags || []).slice(0, 2)
        .map(function (t) { return String(t).slice(0, 18); }).join(', ');
      const reasons = (l.reasons || l.score_reasons || [])
        .slice(0, 2).join('; ').slice(0, 80);
      const sc = _scoreColor(score);
      const actsHtml = acts.map(function (a) {
        return '<button onclick="' + cbName + '(\'' + a.key + '\', ' + idx + ')"'
          + ' style="padding:4px 8px;background:' + (a.color || '#1877f2')
          + ';color:#fff;border:none;border-radius:4px;font-size:10px;'
          + 'cursor:pointer;margin-right:4px">' + a.label + '</button>';
      }).join('');
      return ''
        + '<tr style="border-bottom:1px solid var(--border)">'
        + '<td style="padding:8px;font-size:12px;font-weight:600">'
        + (l.name || '?') + '</td>'
        + '<td style="padding:8px;text-align:center">' + _tierBadge(tier) + '</td>'
        + '<td style="padding:8px;text-align:right;color:' + sc
        + ';font-weight:700;font-size:13px">' + score + '</td>'
        + (showTags    ? '<td style="padding:8px;font-size:10px;color:var(--text-dim)">'
                       + tags + '</td>' : '')
        + (showReasons ? '<td style="padding:8px;font-size:10px;color:var(--text-muted)" title="'
                       + (l.reasons || l.score_reasons || []).join('; ') + '">' + reasons
                       + '</td>' : '')
        + '<td style="padding:6px;text-align:center">' + actsHtml + '</td>'
        + '</tr>';
    }).join('');

    // 全局回调注入
    if (typeof opts.onAction === 'function') {
      global[cbName] = function (action, idx) {
        try { opts.onAction(action, leads[idx]); }
        catch (e) { console.error('[leadList] action error', e); }
      };
    }

    return ''
      + '<div style="max-height:520px;overflow:auto;border:1px solid var(--border);border-radius:8px">'
      + '<table style="width:100%;border-collapse:collapse">'
      + head
      + '<tbody>' + rows + '</tbody>'
      + '</table>'
      + '</div>'
      + '<div style="margin-top:6px;font-size:10px;color:var(--text-dim);text-align:right">'
      + '共 ' + leads.length + ' 条 · 排序按分数高→低'
      + '</div>';
  }

  // ──────────────────────────────────────────────
  // 6. API thin wrapper(沿用全局 api())
  // ──────────────────────────────────────────────
  async function apiGet(path) {
    if (typeof api === 'function') return api('GET', path);
    const r = await fetch(path);
    return r.json();
  }
  async function apiPost(path, body) {
    if (typeof api === 'function') return api('POST', path, body);
    const r = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body || {}),
    });
    return r.json();
  }

  // ──────────────────────────────────────────────
  // Export
  // ──────────────────────────────────────────────
  global.PlatShell = {
    modal: { open: modalOpen, close: modalClose },
    cmdBar: { render: cmdBarRender },
    geo: {
      commonRegions: geoCommonRegions,
      renderSelector: geoRenderSelector,
      getValue: geoGet,
    },
    preset: { renderCards: presetRenderCards },
    leadList: { render: leadListRender },
    api: { get: apiGet, post: apiPost },
    version: '0.2.0',
  };

  console.log('[PlatShell] v' + global.PlatShell.version + ' loaded');
})(window);
