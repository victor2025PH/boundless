/*
 * 人设「试聊」抽屉 — persona_chat_test.js
 *
 * 供人设工作室（personas.html）调用的独立组件，IIFE 封装，无 ES module / 无构建依赖。
 * 对外 API：
 *   window.PSChatTest.open(pid)   打开指定人设的试聊抽屉
 *
 * 功能概述：
 *   - 右侧滑入抽屉（自建 DOM + 自注入 <style id="ps-chattest-style">，类名前缀 .psc-）
 *   - POST /api/chat/test 全链路自测：展示意图 / 知识库命中 / 耗时 / 可折叠链路 trace
 *   - session_id 多轮会话：同 pid 重开抽屉保留会话，切换 pid 重建；「清空会话」重置
 *   - 响应缺 persona_used 字段时，消息区顶部显示一次性黄条降级提示（后端未重启、
 *     persona_id 注入未生效），不阻塞聊天
 *   - 每条成功的 AI 回复气泡可「存为草稿」：弹层选统一收件箱会话（GET
 *     /api/unified-inbox/chats）→ POST /api/drafts/persona-test 入草稿队列，
 *     不直接发送，经工作台人工审核后才发出
 *
 * 安全 / 约定：
 *   - 文本一律 textContent 注入；对 innerHTML 型外部出口（页面 _toast）先 _esc 转义
 *   - 全局引用（window.T / _toast / _allProfiles / _faceRefs / _profileColor）防御式访问
 *   - 颜色全部使用页面 CSS 变量（含变量级回退），深浅色主题自适应；不使用可选链
 */
(function () {
  'use strict';

  var STYLE_ID = 'ps-chattest-style';
  var MAX_INPUT_H = 92; // textarea 约 3 行的上限高度（px）

  // ── 小工具 ────────────────────────────────────────────────────────────────

  // i18n：优先页面全局 window.T(key, fallback)，缺失时用中文兜底
  function t(key, fb) { return window.T ? window.T(key, fb) : fb; }

  // 简易插值：_fmt('失败：{err}', {err:'x'}) → '失败：x'
  function _fmt(s, vars) {
    s = String(s == null ? '' : s);
    if (vars) {
      for (var k in vars) {
        if (Object.prototype.hasOwnProperty.call(vars, k)) {
          s = s.split('{' + k + '}').join(String(vars[k]));
        }
      }
    }
    return s;
  }

  // HTML 转义：用于必须经 innerHTML 渲染的外部出口（本页 _toast）
  function _esc(s) {
    s = String(s == null ? '' : s);
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function _notify(msg, type) {
    if (window._toast) window._toast(_esc(msg), type);
  }

  function _profileOf(pid) {
    var list = window._allProfiles || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].id === pid) return list[i];
    }
    return null;
  }

  function _colorOf(pid) {
    return window._profileColor ? window._profileColor(pid) : '';
  }

  // ── 状态 ──────────────────────────────────────────────────────────────────

  var _state = {
    pid: null,          // 当前试聊的人设 id
    sessionId: '',      // 上次响应的 session_id（'' = 下次发送开新会话）
    sending: false,     // 防重复提交
    personaWarned: false, // persona_used 缺失黄条只提示一次
    visible: false,
    gen: 0              // 会话代号：重建/清空后自增，丢弃在途的过期响应
  };
  var _els = null;

  // ── 样式注入 ──────────────────────────────────────────────────────────────

  function _injectStyle() {
    if (document.getElementById(STYLE_ID)) return;
    var css = [
      '.psc-mask{position:fixed;inset:0;background:var(--nav,var(--t));opacity:0;transition:opacity .25s ease;z-index:1099}',
      '.psc-mask.psc-open{opacity:.45}',
      '.psc-drawer{position:fixed;top:0;right:0;height:100vh;width:min(430px,92vw);background:var(--card);border-left:1px solid var(--bd);box-shadow:var(--s2,none);z-index:1100;display:flex;flex-direction:column;transform:translateX(102%);transition:transform .25s ease}',
      '.psc-drawer.psc-open{transform:translateX(0)}',
      '.psc-off{display:none}',
      /* 头部 */
      '.psc-hd{display:flex;align-items:center;gap:.65rem;padding:.8rem 1rem;border-bottom:1px solid var(--bd);background:var(--bg2,var(--bg));flex-shrink:0}',
      '.psc-ava{width:38px;height:38px;border-radius:10px;flex-shrink:0;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:1rem;color:var(--card);background:var(--p);overflow:hidden}',
      '.psc-ava img{width:100%;height:100%;object-fit:cover;display:block}',
      '.psc-hd-txt{flex:1;min-width:0}',
      '.psc-name{font-size:.9rem;font-weight:700;color:var(--t1,var(--t));display:flex;align-items:center;gap:.4rem;min-width:0}',
      '.psc-name-txt{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.psc-badge{flex-shrink:0;font-size:.62rem;font-weight:600;padding:.08rem .38rem;border-radius:5px;background:var(--ps,var(--bg2,var(--bg)));color:var(--p)}',
      '.psc-role{font-size:.7rem;font-weight:500;color:var(--t2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;flex-shrink:1}',
      '.psc-sub{font-size:.67rem;color:var(--t2);margin-top:.12rem}',
      '.psc-x{margin-left:auto;flex-shrink:0;width:28px;height:28px;border:1px solid var(--bd);border-radius:8px;background:transparent;color:var(--t2);font-size:1rem;line-height:1;cursor:pointer;display:flex;align-items:center;justify-content:center;transition:all .15s}',
      '.psc-x:hover{color:var(--t1,var(--t));border-color:var(--bd2,var(--bd))}',
      /* 消息区 */
      '.psc-body{flex:1;overflow-y:auto;padding:.9rem 1rem;display:flex;flex-direction:column;gap:.7rem;background:var(--bg)}',
      '.psc-empty{flex:1;display:flex;align-items:center;justify-content:center;text-align:center;color:var(--t2);font-size:.8rem;padding:1rem;line-height:1.7}',
      '.psc-warn{background:var(--as,transparent);border:1px solid var(--amber,var(--bd));color:var(--amber,var(--t2));font-size:.72rem;line-height:1.55;padding:.5rem .7rem;border-radius:8px;flex-shrink:0}',
      '.psc-msg{display:flex;flex-direction:column;max-width:86%}',
      '.psc-msg.psc-user{align-self:flex-end;align-items:flex-end}',
      '.psc-msg.psc-ai{align-self:flex-start;align-items:flex-start}',
      '.psc-bubble{padding:.5rem .75rem;border-radius:12px;font-size:.84rem;line-height:1.55;white-space:pre-wrap;word-break:break-word}',
      '.psc-user .psc-bubble{background:var(--p);color:var(--card);border-bottom-right-radius:4px}',
      '.psc-ai .psc-bubble{background:var(--card);border:1px solid var(--bd);color:var(--t1,var(--t));border-bottom-left-radius:4px}',
      '.psc-bubble.psc-pending{color:var(--t2);font-style:italic}',
      '.psc-bubble.psc-errb{color:var(--red);border-color:var(--red)}',
      '.psc-meta{font-size:.66rem;color:var(--t2);margin-top:.25rem;display:flex;flex-wrap:wrap;gap:.2rem .6rem}',
      /* trace 折叠区 */
      '.psc-trace{margin-top:.3rem;font-size:.66rem;color:var(--t2);max-width:100%;align-self:stretch}',
      '.psc-trace summary{cursor:pointer;user-select:none;outline:none}',
      '.psc-trace summary:hover{color:var(--t1,var(--t))}',
      '.psc-trace-list{margin-top:.3rem;padding:.45rem .55rem;background:var(--bg2,var(--bg));border:1px solid var(--bd);border-radius:8px;font-family:ui-monospace,SFMono-Regular,Consolas,"Courier New",monospace;font-size:.62rem;line-height:1.55;white-space:pre-wrap;word-break:break-all;max-height:180px;overflow-y:auto}',
      '.psc-trace-step{margin-bottom:.28rem}',
      '.psc-trace-step:last-child{margin-bottom:0}',
      '.psc-trace-step b{color:var(--t1,var(--t));font-weight:600}',
      /* 底部输入区 */
      '.psc-ft{border-top:1px solid var(--bd);padding:.65rem .85rem .55rem;background:var(--card);flex-shrink:0}',
      '.psc-ft-row{display:flex;gap:.5rem;align-items:flex-end}',
      '.psc-input{flex:1;resize:none;border:1px solid var(--bd);border-radius:10px;background:var(--bg2,var(--bg));color:var(--t1,var(--t));font-size:.84rem;font-family:inherit;line-height:1.45;padding:.48rem .65rem;min-height:36px;max-height:' + MAX_INPUT_H + 'px;overflow-y:auto;outline:none;box-sizing:border-box;transition:border-color .15s}',
      '.psc-input:focus{border-color:var(--p)}',
      '.psc-input:disabled{opacity:.55}',
      '.psc-send{flex-shrink:0;height:36px;border:none;border-radius:10px;background:var(--p);color:var(--card);font-size:.82rem;font-weight:600;padding:0 .95rem;cursor:pointer;transition:opacity .15s}',
      '.psc-send:hover{opacity:.88}',
      '.psc-send:disabled{opacity:.5;cursor:default}',
      '.psc-ft-tools{display:flex;justify-content:flex-end;margin-top:.35rem}',
      '.psc-clear{border:none;background:transparent;color:var(--t2);font-size:.7rem;cursor:pointer;padding:.1rem .2rem;transition:color .15s}',
      '.psc-clear:hover{color:var(--red)}',
      '.psc-clear:disabled{opacity:.5;cursor:default}',
      /* 存为草稿：气泡元信息行内按钮 + 已存态旁的「去工作台审核」小链接 */
      '.psc-draft-btn{border:1px solid var(--bd);background:transparent;color:var(--t2);font-size:.64rem;line-height:1;padding:.16rem .42rem;border-radius:6px;cursor:pointer;font-family:inherit;transition:all .15s}',
      '.psc-draft-btn:hover{color:var(--p);border-color:var(--p)}',
      '.psc-draft-btn:disabled{opacity:.6;cursor:default;color:var(--t2);border-color:var(--bd)}',
      '.psc-draft-link{border:none;background:transparent;color:var(--p);font-size:.64rem;line-height:1;padding:.16rem .1rem;cursor:pointer;font-family:inherit;text-decoration:underline}',
      /* 会话选择弹层（z-index 高于抽屉 1100/遮罩 1099） */
      '.psc-pk-mask{position:fixed;inset:0;background:var(--nav,var(--t));opacity:.45;z-index:1200}',
      '.psc-pk{position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);width:min(360px,92vw);max-height:min(480px,84vh);background:var(--card);border:1px solid var(--bd);border-radius:14px;box-shadow:var(--s2,none);z-index:1201;display:flex;flex-direction:column;overflow:hidden}',
      '.psc-pk-hd{padding:.7rem .85rem .5rem;border-bottom:1px solid var(--bd);flex-shrink:0}',
      '.psc-pk-title{display:flex;align-items:center;gap:.5rem;font-size:.86rem;font-weight:700;color:var(--t1,var(--t))}',
      '.psc-pk-hint{font-size:.66rem;color:var(--t2);line-height:1.55;margin-top:.25rem}',
      '.psc-pk-search{margin:.55rem .85rem 0;flex-shrink:0;border:1px solid var(--bd);border-radius:8px;background:var(--bg2,var(--bg));color:var(--t1,var(--t));font-size:.78rem;font-family:inherit;padding:.4rem .6rem;outline:none;box-sizing:border-box;transition:border-color .15s}',
      '.psc-pk-search:focus{border-color:var(--p)}',
      '.psc-pk-search:disabled{opacity:.55}',
      '.psc-pk-list{flex:1;overflow-y:auto;padding:.45rem .5rem .6rem;min-height:110px}',
      '.psc-pk-row{display:flex;align-items:center;gap:.5rem;width:100%;border:none;background:transparent;text-align:left;padding:.45rem;border-radius:8px;cursor:pointer;font-family:inherit;transition:background .15s}',
      '.psc-pk-row:hover{background:var(--bg2,var(--bg))}',
      '.psc-pk-row:disabled{opacity:.55;cursor:default}',
      '.psc-pk-plat{flex-shrink:0;font-size:.6rem;font-weight:600;padding:.1rem .35rem;border-radius:5px;background:var(--ps,var(--bg2,var(--bg)));color:var(--p)}',
      '.psc-pk-name{flex:1;min-width:0;font-size:.8rem;color:var(--t1,var(--t));overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.psc-pk-acc{flex-shrink:0;max-width:96px;font-size:.64rem;color:var(--t2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}',
      '.psc-pk-empty{padding:1.1rem .6rem;text-align:center;color:var(--t2);font-size:.74rem;line-height:1.7;word-break:break-word}'
    ].join('\n');
    var style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = css;
    document.head.appendChild(style);
  }

  // ── DOM 构建（仅一次）─────────────────────────────────────────────────────

  function _build() {
    if (_els) return;
    _injectStyle();

    var mask = document.createElement('div');
    mask.className = 'psc-mask psc-off';
    mask.addEventListener('click', _close);

    var drawer = document.createElement('div');
    drawer.className = 'psc-drawer psc-off';
    drawer.setAttribute('role', 'dialog');
    drawer.setAttribute('aria-label', t('psn_ct_title', '试聊'));

    // 头部：头像 + 名字/角色 + 副标题 + 关闭
    var hd = document.createElement('div');
    hd.className = 'psc-hd';
    var ava = document.createElement('div');
    ava.className = 'psc-ava';
    var txt = document.createElement('div');
    txt.className = 'psc-hd-txt';
    var nameLine = document.createElement('div');
    nameLine.className = 'psc-name';
    var badge = document.createElement('span');
    badge.className = 'psc-badge';
    badge.textContent = t('psn_ct_title', '试聊');
    var nameEl = document.createElement('span');
    nameEl.className = 'psc-name-txt';
    var roleEl = document.createElement('span');
    roleEl.className = 'psc-role';
    nameLine.appendChild(badge);
    nameLine.appendChild(nameEl);
    nameLine.appendChild(roleEl);
    var subEl = document.createElement('div');
    subEl.className = 'psc-sub';
    subEl.textContent = t('psn_ct_sub', '对话仅用于预览人设效果，不影响生产数据');
    txt.appendChild(nameLine);
    txt.appendChild(subEl);
    var xBtn = document.createElement('button');
    xBtn.type = 'button';
    xBtn.className = 'psc-x';
    xBtn.textContent = '×';
    xBtn.addEventListener('click', _close);
    hd.appendChild(ava);
    hd.appendChild(txt);
    hd.appendChild(xBtn);

    // 消息区 + 空态
    var body = document.createElement('div');
    body.className = 'psc-body';
    var empty = document.createElement('div');
    empty.className = 'psc-empty';
    empty.textContent = t('psn_ct_empty', '发条消息，感受一下 TA 的语气吧');
    body.appendChild(empty);

    // 底部：textarea + 发送 + 清空会话
    var ft = document.createElement('div');
    ft.className = 'psc-ft';
    var row = document.createElement('div');
    row.className = 'psc-ft-row';
    var input = document.createElement('textarea');
    input.className = 'psc-input';
    input.rows = 1;
    input.placeholder = t('psn_ct_input_ph', '输入消息，Enter 发送，Shift+Enter 换行');
    input.addEventListener('input', _autoResize);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        if (e.isComposing || e.keyCode === 229) return; // 中文输入法组词中的回车不发送
        e.preventDefault();
        _send();
      }
    });
    var sendBtn = document.createElement('button');
    sendBtn.type = 'button';
    sendBtn.className = 'psc-send';
    sendBtn.textContent = t('psn_ct_send', '发送');
    sendBtn.addEventListener('click', _send);
    row.appendChild(input);
    row.appendChild(sendBtn);
    var tools = document.createElement('div');
    tools.className = 'psc-ft-tools';
    var clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'psc-clear';
    clearBtn.textContent = t('psn_ct_clear', '清空会话');
    clearBtn.addEventListener('click', _clearSession);
    tools.appendChild(clearBtn);
    ft.appendChild(row);
    ft.appendChild(tools);

    drawer.appendChild(hd);
    drawer.appendChild(body);
    drawer.appendChild(ft);
    document.body.appendChild(mask);
    document.body.appendChild(drawer);

    document.addEventListener('keydown', function (e) {
      if (e.key !== 'Escape' && e.key !== 'Esc') return;
      if (_pkState.visible) { _closePicker(); return; } // 弹层开着时 Esc 只关弹层
      if (_state.visible) _close();
    });

    _els = {
      mask: mask, drawer: drawer, ava: ava, nameEl: nameEl, roleEl: roleEl,
      body: body, empty: empty, input: input, sendBtn: sendBtn, clearBtn: clearBtn
    };
  }

  // ── 头部信息 ──────────────────────────────────────────────────────────────

  function _avaInitial(ava, name, color) {
    while (ava.firstChild) ava.removeChild(ava.firstChild);
    ava.style.background = color || '';
    ava.textContent = (String(name || '?').trim().charAt(0) || '?').toUpperCase();
  }

  function _setHeader(pid) {
    var prof = _profileOf(pid) || {};
    var name = prof.name || pid;
    _els.nameEl.textContent = name;
    _els.roleEl.textContent = prof.role || '';
    var ava = _els.ava;
    while (ava.firstChild) ava.removeChild(ava.firstChild);
    ava.textContent = '';
    var refs = window._faceRefs || {};
    var ref = refs[pid];
    var color = _colorOf(pid);
    if (ref && ref.url) {
      ava.style.background = '';
      var img = document.createElement('img');
      img.alt = name;
      img.onerror = function () {
        if (img.parentNode === ava) ava.removeChild(img);
        _avaInitial(ava, name, color);
      };
      img.src = ref.url;
      ava.appendChild(img);
    } else {
      _avaInitial(ava, name, color);
    }
  }

  // ── 消息区渲染 ────────────────────────────────────────────────────────────

  function _scrollBottom() {
    _els.body.scrollTop = _els.body.scrollHeight;
  }

  function _updateEmpty() {
    var has = _els.body.querySelector('.psc-msg');
    _els.empty.style.display = has ? 'none' : '';
  }

  function _addUserMsg(text) {
    var wrap = document.createElement('div');
    wrap.className = 'psc-msg psc-user';
    var b = document.createElement('div');
    b.className = 'psc-bubble';
    b.textContent = text;
    wrap.appendChild(b);
    _els.body.appendChild(wrap);
    _updateEmpty();
    _scrollBottom();
  }

  // 「正在输入…」占位气泡，响应到达后原位替换
  function _addAiPending() {
    var wrap = document.createElement('div');
    wrap.className = 'psc-msg psc-ai';
    var b = document.createElement('div');
    b.className = 'psc-bubble psc-pending';
    b.textContent = t('psn_ct_thinking', '正在输入…');
    wrap.appendChild(b);
    _els.body.appendChild(wrap);
    _updateEmpty();
    _scrollBottom();
    return { wrap: wrap, bubble: b };
  }

  function _fillAiMsg(ph, data, userText) {
    ph.bubble.classList.remove('psc-pending');
    var replyText = String(data.reply == null ? '' : data.reply);
    ph.bubble.textContent = replyText;

    // 元信息行：意图 / 知识库命中 / 耗时 / 实际使用人设（persona_used 有才显示）
    var meta = document.createElement('div');
    meta.className = 'psc-meta';
    function part(label, val) {
      var s = document.createElement('span');
      s.textContent = label + ' ' + val;
      meta.appendChild(s);
    }
    if (data.intent) part(t('psn_ct_meta_intent', '意图'), data.intent);
    part(t('psn_ct_meta_kb', '知识库'), data.kb_hit ? '✓' : '✗');
    if (data.total_ms != null) part(t('psn_ct_meta_ms', '耗时'), data.total_ms + 'ms');
    var pu = data.persona_used;
    if (pu && pu.name) part(t('psn_ct_meta_persona', '人设'), pu.name);
    meta.appendChild(_draftBtn(replyText, userText));
    ph.wrap.appendChild(meta);

    // 可折叠链路 trace：step 名 + detail JSON 摘要（截断 300 字符）
    var steps = data.trace && data.trace.steps;
    if (steps && steps.length) {
      var det = document.createElement('details');
      det.className = 'psc-trace';
      var sum = document.createElement('summary');
      sum.textContent = t('psn_ct_trace', '链路 trace');
      det.appendChild(sum);
      var list = document.createElement('div');
      list.className = 'psc-trace-list';
      for (var i = 0; i < steps.length; i++) {
        var st = steps[i] || {};
        var rowEl = document.createElement('div');
        rowEl.className = 'psc-trace-step';
        var bName = document.createElement('b');
        bName.textContent = String(st.step || '?');
        rowEl.appendChild(bName);
        var dStr = '';
        try { dStr = JSON.stringify(st.detail); } catch (e) { dStr = String(st.detail); }
        dStr = String(dStr == null ? '' : dStr).slice(0, 300);
        rowEl.appendChild(document.createTextNode(' ' + dStr));
        list.appendChild(rowEl);
      }
      det.appendChild(list);
      ph.wrap.appendChild(det);
    }
  }

  function _fillAiError(ph, msg) {
    ph.bubble.classList.remove('psc-pending');
    ph.bubble.classList.add('psc-errb');
    ph.bubble.textContent = msg;
  }

  // persona_used 缺失（后端未重启、persona_id 未生效）→ 一次性黄条，不阻塞聊天
  function _showPersonaBanner() {
    if (_state.personaWarned) return;
    _state.personaWarned = true;
    var bn = document.createElement('div');
    bn.className = 'psc-warn';
    bn.textContent = t('psn_ct_persona_missing',
      '当前后端尚未加载人设注入（需重启生效），回复将使用全局默认人设');
    _els.body.insertBefore(bn, _els.body.firstChild);
  }

  // ── 存为草稿（AI 气泡 → 统一收件箱会话的草稿队列，人工审核后才发出）───────

  var _pk = null;   // 会话选择弹层 DOM（懒建一次）
  var _pkState = {
    visible: false,
    saving: false,  // POST /api/drafts/persona-test 在途 → 防重复提交
    ctx: null,      // 当前待存气泡 { reply, peer, pid, btn }
    chats: [],
    loadGen: 0      // 加载代号：重开弹层自增，丢弃在途的过期 chats 响应
  };

  // AI 气泡元信息行内的「存为草稿」小按钮；回复文本 + 触发它的用户消息存在闭包
  function _draftBtn(replyText, userText) {
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'psc-draft-btn';
    btn.textContent = t('psn_ct_savedraft', '存为草稿');
    var ctx = { reply: replyText, peer: userText, pid: _state.pid, btn: btn };
    btn.addEventListener('click', function () {
      if (btn.disabled) return;
      _openPicker(ctx);
    });
    return btn;
  }

  // 存草稿成功 → 气泡按钮置「已存」终态；响应带 review_url 时旁挂审核入口
  function _markDraftSaved(ctx, reviewUrl) {
    var btn = ctx.btn;
    btn.disabled = true;
    btn.textContent = t('psn_ct_draft_done', '已存草稿');
    if (reviewUrl && btn.parentNode) {
      var link = document.createElement('button');
      link.type = 'button';
      link.className = 'psc-draft-link';
      link.textContent = t('psn_ct_go_review', '去工作台审核');
      link.addEventListener('click', function () {
        window.open(reviewUrl, '_blank');
      });
      if (btn.nextSibling) btn.parentNode.insertBefore(link, btn.nextSibling);
      else btn.parentNode.appendChild(link);
    }
  }

  function _buildPicker() {
    if (_pk) return;
    var mask = document.createElement('div');
    mask.className = 'psc-pk-mask psc-off';
    mask.addEventListener('click', _closePicker);

    var box = document.createElement('div');
    box.className = 'psc-pk psc-off';
    box.setAttribute('role', 'dialog');
    box.setAttribute('aria-label', t('psn_ct_pick_conv', '选择要发送到的会话'));

    var hd = document.createElement('div');
    hd.className = 'psc-pk-hd';
    var title = document.createElement('div');
    title.className = 'psc-pk-title';
    title.textContent = t('psn_ct_pick_conv', '选择要发送到的会话');
    var hint = document.createElement('div');
    hint.className = 'psc-pk-hint';
    hint.textContent = t('psn_ct_draft_hint', '不会直接发送；经工作台人工审核后才会发出。');
    hd.appendChild(title);
    hd.appendChild(hint);

    var search = document.createElement('input');
    search.type = 'text';
    search.className = 'psc-pk-search';
    search.placeholder = t('psn_ct_conv_search', '搜索会话…');
    search.addEventListener('input', function () { _renderPkList(); });

    var list = document.createElement('div');
    list.className = 'psc-pk-list';

    box.appendChild(hd);
    box.appendChild(search);
    box.appendChild(list);
    document.body.appendChild(mask);
    document.body.appendChild(box);
    _pk = { mask: mask, box: box, search: search, list: list };
  }

  function _pkEmpty(msg) {
    var list = _pk.list;
    while (list.firstChild) list.removeChild(list.firstChild);
    var d = document.createElement('div');
    d.className = 'psc-pk-empty';
    d.textContent = msg;
    list.appendChild(d);
  }

  // 行内缺 conversation_id 时按 normalizer.conv_id 规范拼（三段都非空才拼）
  function _convIdOf(c) {
    var cid = String(c.conversation_id == null ? '' : c.conversation_id);
    if (cid) return cid;
    var plat = String(c.platform == null ? '' : c.platform);
    var acc = String(c.account_id == null ? '' : c.account_id);
    var key = String(c.chat_key == null ? '' : c.chat_key);
    if (!plat || !acc || !key) return '';
    return plat + ':' + acc + ':' + key;
  }

  function _pkRow(c) {
    var row = document.createElement('button');
    row.type = 'button';
    row.className = 'psc-pk-row';
    row.disabled = _pkState.saving;
    var plat = document.createElement('span');
    plat.className = 'psc-pk-plat';
    plat.textContent = String(c.platform_name || c.platform || '?');
    var name = document.createElement('span');
    name.className = 'psc-pk-name';
    name.textContent = String(c.name || c.chat_key || '');
    var acc = document.createElement('span');
    acc.className = 'psc-pk-acc';
    acc.textContent = String(c.account_label || c.account_id || '');
    row.appendChild(plat);
    row.appendChild(name);
    row.appendChild(acc);
    row.addEventListener('click', function () { _saveDraft(c); });
    return row;
  }

  // 按搜索词（会话名 / chat_key，大小写不敏感）过滤重绘列表
  function _renderPkList() {
    var list = _pk.list;
    var q = String(_pk.search.value == null ? '' : _pk.search.value).trim().toLowerCase();
    var rows = [];
    for (var i = 0; i < _pkState.chats.length; i++) {
      var c = _pkState.chats[i] || {};
      if (!_convIdOf(c)) continue;
      if (q) {
        var nm = String(c.name == null ? '' : c.name).toLowerCase();
        var ck = String(c.chat_key == null ? '' : c.chat_key).toLowerCase();
        if (nm.indexOf(q) < 0 && ck.indexOf(q) < 0) continue;
      }
      rows.push(c);
    }
    while (list.firstChild) list.removeChild(list.firstChild);
    if (!rows.length) {
      _pkEmpty(t('psn_ct_no_convs', '暂无会话（先在统一收件箱接入账号）'));
      return;
    }
    for (var j = 0; j < rows.length; j++) {
      list.appendChild(_pkRow(rows[j]));
    }
  }

  function _loadPkChats() {
    var gen = ++_pkState.loadGen;
    _pkState.chats = [];
    _pkEmpty('…');
    fetch('/api/unified-inbox/chats?limit=50').then(function (res) {
      return res.json().then(
        function (body) { return { httpOk: res.ok, status: res.status, body: body }; },
        function () { return { httpOk: res.ok, status: res.status, body: null }; }
      );
    }).then(function (r) {
      if (gen !== _pkState.loadGen || !_pkState.visible) return;
      var body = r.body;
      if (!r.httpOk || !body || body.ok === false) {
        var err = '';
        if (body && body.error) err = String(body.error);
        else if (body && body.detail) {
          err = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
        }
        if (!err) err = 'HTTP ' + r.status;
        _pkEmpty(err);
        return;
      }
      _pkState.chats = body.chats || [];
      _renderPkList();
    }).catch(function (e) {
      if (gen !== _pkState.loadGen || !_pkState.visible) return;
      _pkEmpty(e && e.message ? e.message : String(e));
    });
  }

  function _setPickerBusy(on) {
    if (!_pk) return;
    _pk.search.disabled = on;
    var rows = _pk.list.querySelectorAll('.psc-pk-row');
    for (var i = 0; i < rows.length; i++) rows[i].disabled = on;
  }

  function _saveDraft(c) {
    if (_pkState.saving) return;
    var ctx = _pkState.ctx;
    if (!ctx) return;
    var cid = _convIdOf(c);
    if (!cid) return;
    _pkState.saving = true;
    _setPickerBusy(true);
    fetch('/api/drafts/persona-test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        conversation_id: cid,
        text: ctx.reply,
        persona_id: ctx.pid,
        peer_text: ctx.peer
      })
    }).then(function (res) {
      return res.json().then(
        function (body) { return { httpOk: res.ok, status: res.status, body: body }; },
        function () { return { httpOk: res.ok, status: res.status, body: null }; }
      );
    }).then(function (r) {
      _pkState.saving = false;
      _setPickerBusy(false);
      var body = r.body;
      if (r.httpOk && body && body.ok) {
        _closePicker();
        _markDraftSaved(ctx, body.review_url ? String(body.review_url) : '');
        _notify(t('psn_ct_draft_saved', '已存入草稿队列，待坐席在工作台审核发送'), 'ok');
        return;
      }
      // 失败：error → detail（404 的 detail 文案原样透传）→ HTTP 状态兜底
      var err = '';
      if (body && body.error) err = String(body.error);
      else if (body && body.detail) {
        err = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
      }
      if (!err) err = 'HTTP ' + r.status;
      _notify(_fmt(t('psn_ct_draft_fail', '存草稿失败：{err}'), { err: err }), 'err');
    }).catch(function (e) {
      _pkState.saving = false;
      _setPickerBusy(false);
      _notify(_fmt(t('psn_ct_draft_fail', '存草稿失败：{err}'),
        { err: e && e.message ? e.message : String(e) }), 'err');
    });
  }

  function _openPicker(ctx) {
    _buildPicker();
    _pkState.ctx = ctx;
    _pkState.visible = true;
    _pk.search.value = '';
    _pk.mask.classList.remove('psc-off');
    _pk.box.classList.remove('psc-off');
    _loadPkChats();
    try { _pk.search.focus(); } catch (e) { /* 忽略不可聚焦场景 */ }
  }

  function _closePicker() {
    if (!_pk || !_pkState.visible) return;
    _pkState.visible = false;
    _pkState.ctx = null;
    _pkState.loadGen++;
    _pk.mask.classList.add('psc-off');
    _pk.box.classList.add('psc-off');
  }

  // ── 输入区 ────────────────────────────────────────────────────────────────

  function _autoResize() {
    var el = _els.input;
    el.style.height = 'auto';
    var h = el.scrollHeight;
    if (h > MAX_INPUT_H) h = MAX_INPUT_H;
    el.style.height = h + 'px';
  }

  function _focusInput() {
    if (_els && _els.input && !_els.input.disabled) {
      try { _els.input.focus(); } catch (e) { /* 忽略不可聚焦场景 */ }
    }
  }

  function _setSending(on) {
    _state.sending = on;
    _els.input.disabled = on;
    _els.sendBtn.disabled = on;
    _els.clearBtn.disabled = on;
  }

  // ── 发送 / 会话 ───────────────────────────────────────────────────────────

  function _failReply(ph, err) {
    var msg = _fmt(t('psn_ct_fail', '发送失败：{err}'), { err: err });
    _fillAiError(ph, msg);
    _notify(msg, 'err');
    _scrollBottom();
  }

  function _send() {
    if (_state.sending || !_els) return;
    var msg = String(_els.input.value == null ? '' : _els.input.value).trim();
    if (!msg) return;
    _els.input.value = '';
    _autoResize();
    _addUserMsg(msg);
    _setSending(true);
    var ph = _addAiPending();
    var gen = _state.gen;
    var payload = {
      message: msg,
      session_id: _state.sessionId || '',
      persona_id: _state.pid,
      user_id: '__persona_test__' + _state.pid
    };
    fetch('/api/chat/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    }).then(function (res) {
      return res.json().then(
        function (body) { return { httpOk: res.ok, status: res.status, body: body }; },
        function () { return { httpOk: res.ok, status: res.status, body: null }; }
      );
    }).then(function (r) {
      if (gen !== _state.gen) return; // 期间已清空/切换人设，丢弃过期响应
      _setSending(false);
      var body = r.body;
      if (!r.httpOk || !body || body.ok === false) {
        var err = '';
        if (body && body.error) err = String(body.error);
        else if (body && body.detail) {
          err = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail);
        }
        if (!err) err = 'HTTP ' + r.status;
        _failReply(ph, err);
        _focusInput();
        return;
      }
      if (body.session_id) _state.sessionId = body.session_id;
      _fillAiMsg(ph, body, msg);
      if (!body.persona_used) _showPersonaBanner();
      _scrollBottom();
      _focusInput();
    }).catch(function (e) {
      if (gen !== _state.gen) return;
      _setSending(false);
      _failReply(ph, e && e.message ? e.message : String(e));
      _focusInput();
    });
  }

  function _clearSession() {
    if (!_els || _state.sending) return;
    _state.sessionId = '';
    _state.personaWarned = false;
    _state.gen++;
    var body = _els.body;
    while (body.firstChild) body.removeChild(body.firstChild);
    _els.empty.style.display = '';
    body.appendChild(_els.empty);
    _notify(t('psn_ct_cleared', '已清空，下一条消息将开启新会话'), 'ok');
    _focusInput();
  }

  // 切换到新 pid：清空消息与会话状态，刷新头部
  function _resetConversation(pid) {
    _state.pid = pid;
    _state.sessionId = '';
    _state.personaWarned = false;
    _state.gen++;
    var body = _els.body;
    while (body.firstChild) body.removeChild(body.firstChild);
    _els.empty.style.display = '';
    body.appendChild(_els.empty);
    _setSending(false);
    _setHeader(pid);
  }

  // ── 打开 / 关闭 ───────────────────────────────────────────────────────────

  function _open(pid) {
    pid = String(pid == null ? '' : pid);
    if (!pid) return;
    _build();
    if (_state.pid !== pid) {
      _resetConversation(pid);
    } else {
      _setHeader(pid); // 同 pid 保留会话，仅刷新头像/名字（资料可能已编辑）
    }
    if (_state.visible) { _focusInput(); return; }
    _state.visible = true;
    _els.mask.classList.remove('psc-off');
    _els.drawer.classList.remove('psc-off');
    void _els.drawer.offsetWidth; // 强制 reflow，确保滑入过渡生效
    _els.mask.classList.add('psc-open');
    _els.drawer.classList.add('psc-open');
    _scrollBottom();
    setTimeout(_focusInput, 260);
  }

  function _close() {
    if (!_els || !_state.visible) return;
    _closePicker(); // 抽屉关闭连带收起会话选择弹层
    _state.visible = false;
    _els.mask.classList.remove('psc-open');
    _els.drawer.classList.remove('psc-open');
    setTimeout(function () {
      if (_state.visible) return; // 动画期间被重新打开
      _els.mask.classList.add('psc-off');
      _els.drawer.classList.add('psc-off');
    }, 260);
  }

  // ── 对外 API ──────────────────────────────────────────────────────────────

  window.PSChatTest = { open: _open };
})();
