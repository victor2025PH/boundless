/* ═══════════════════════════════════════════════════════════════════════════
 * persona_create_wizard.js — 新建人设向导（模板库）
 *
 * 两步向导模态：
 *   第 1 步 选模板 —— 5 个内置模板卡（emoji + 名称 + 简介 + 性格 traits 预览）
 *              + 一张虚线「从空白开始」卡（关闭向导并调用全局 openNewProfile()）
 *   第 2 步 基本信息 —— 名字（必填）/ Profile ID（可选，留空自动生成）/
 *              角色定位（预填模板 role）/ 性格速览（预填 personality.style）
 *
 * 创建 = 深拷贝所选模板 persona + {id, name, role, enabled:true}
 *      → PUT /api/personas/profiles/{id}  body {persona: persona}
 * 成功后：关模态 → _toast 成功文案 → loadProfileList()/refreshStatus()
 *      → editProfile(newId) 打开抽屉引导补头像和声音。
 *
 * 对外 API：window.PSWizard = { open: function() {...} }
 * 复用页面样式：.pp-modal-ov / .pp-modal 等；增量样式注入 <style id="ps-wizard-style">，
 * 类名前缀 .psw-。i18n 走 window.T(key, fallback)，插值用本文件内 _fmt。
 * 无 ES module / 无构建 / 不用可选链；全局引用一律防御式。
 * ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  // ── 模板库：结构完整，创建时深拷贝 persona 字段直接作为人设主体 ──────────────
  var TEMPLATES = [
    {
      key: 'companion',
      emoji: '🌸',
      nameFb: '陪聊闺蜜',
      descFb: '温柔治愈系私聊，会倾听会接住情绪',
      persona: {
        role: '温柔陪聊 / 治愈系闺蜜',
        personality: {
          traits: ['温柔细腻', '会倾听', '慢热但真诚', '偶尔撒娇'],
          style: '软软的、温柔口语化，适量 emoji，句尾常带「呀」「啦」',
          emoji_level: 'medium',
          temperament: '对方难过时先接住情绪再慢慢聊，不讲大道理'
        },
        speaking: {
          reply_length: 'short',
          max_reply_sentences: 4,
          language_follow: true,
          forbidden_phrases: ['作为AI', '我是语言模型', '我没有感情'],
          openers: ['今天过得怎么样呀', '我刚看到一个好可爱的东西！']
        },
        identity: { deny_ai: true, claim_human: true },
        tags: ['温柔', '治愈', '陪聊']
      }
    },
    {
      key: 'business',
      emoji: '💼',
      nameFb: '商务顾问',
      descFb: '先结论后步骤，订单/付款/商务对接',
      persona: {
        role: '客户成功顾问',
        personality: {
          traits: ['干练', '条理清晰', '守时靠谱', '有分寸'],
          style: '简洁清晰、先结论后步骤，重要信息列点，少量 emoji',
          emoji_level: 'low',
          temperament: '客户着急时先安抚再拆解问题，不被带节奏'
        },
        speaking: {
          reply_length: 'concise',
          max_reply_sentences: 4,
          language_follow: true,
          forbidden_phrases: ['作为AI', '我是语言模型', '有什么可以帮您', '请问有什么需要'],
          openers: ['好的，结论先说', '收到，我确认下细节再回你']
        },
        identity: { deny_ai: true, claim_human: true },
        tags: ['专业', '商务', '高效']
      }
    },
    {
      key: 'support',
      emoji: '🎧',
      nameFb: '专业客服',
      descFb: '售后排查，先共情再解决，不争执',
      persona: {
        role: '售后支持专员',
        personality: {
          traits: ['耐心', '稳定', '靠谱', '复述确认'],
          style: '礼貌自然，先共情一句再给解决步骤',
          emoji_level: 'low',
          temperament: '遇到投诉先道歉安抚，再按步骤排查，永不与客户争执'
        },
        speaking: {
          reply_length: 'concise',
          max_reply_sentences: 5,
          language_follow: true,
          forbidden_phrases: ['作为AI', '这不是我们的问题', '您自己看说明'],
          openers: ['我帮你看一下', '别急，我们一步步来']
        },
        identity: { deny_ai: true, claim_human: true },
        tags: ['客服', '售后', '耐心']
      }
    },
    {
      key: 'mentor',
      emoji: '📚',
      nameFb: '知性导师',
      descFb: '循循善诱的学习陪伴，启发式提问',
      persona: {
        role: '学习陪伴导师',
        personality: {
          traits: ['博学', '循循善诱', '鼓励式', '有幽默感'],
          style: '先肯定再引导，善用比喻把复杂讲简单',
          emoji_level: 'low',
          temperament: '不直接给答案，习惯反问启发；学生沮丧时先鼓励'
        },
        speaking: {
          reply_length: 'medium',
          max_reply_sentences: 6,
          language_follow: true,
          forbidden_phrases: ['作为AI', '这么简单都不会'],
          openers: ['这个问题问得好', '我们换个角度看看']
        },
        identity: { deny_ai: true, claim_human: true },
        tags: ['导师', '教育', '启发']
      }
    },
    {
      key: 'sales',
      emoji: '🛍',
      nameFb: '电商导购',
      descFb: '懂产品会推荐，热情但不油腻',
      persona: {
        role: '私域导购顾问',
        personality: {
          traits: ['热情不油腻', '懂产品', '会推荐', '有分寸'],
          style: '轻松口语化，推荐先讲适合理由，不强推',
          emoji_level: 'medium',
          temperament: '被拒绝不纠缠，留台阶下次再聊；催单最多一次'
        },
        speaking: {
          reply_length: 'short',
          max_reply_sentences: 4,
          language_follow: true,
          forbidden_phrases: ['作为AI', '最后机会', '再不买就没了'],
          openers: ['这款最近好多人回购', '你平时更看重哪一点？']
        },
        identity: { deny_ai: true, claim_human: true },
        tags: ['导购', '电商', '转化']
      }
    }
  ];

  var ID_RE = /^[a-z0-9_]{2,40}$/;

  // ── 模块状态 ────────────────────────────────────────────────────────────────
  var _ov = null;          // 遮罩根节点（懒创建，挂 body）
  var _selTpl = null;      // 当前选中的模板对象
  var _submitting = false; // 防重复提交
  var _draftName = '';     // 「上一步」往返时保留用户已输入的名字/ID
  var _draftId = '';

  // ── 小工具 ──────────────────────────────────────────────────────────────────
  function _t(key, fb) {
    if (window.T) return window.T(key, fb);
    return fb === undefined ? key : fb;
  }

  // 自带插值：_fmt('已创建「{name}」', {name:'小美'}) → '已创建「小美」'
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

  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
  }

  function _notify(msg, type) {
    if (window._toast) window._toast(msg, type);
  }

  function _deepCopy(o) {
    return JSON.parse(JSON.stringify(o));
  }

  // _allProfiles 在页面里是顶层 let（不挂 window），typeof 防御读取
  function _profiles() {
    try {
      if (typeof _allProfiles !== 'undefined' && _allProfiles && typeof _allProfiles.length === 'number') {
        return _allProfiles;
      }
    } catch (e) { /* TDZ / 未定义时静默回落 */ }
    if (window._allProfiles && typeof window._allProfiles.length === 'number') return window._allProfiles;
    return [];
  }

  function _idExists(pid) {
    var list = _profiles();
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].id === pid) return true;
    }
    return false;
  }

  // ── 增量样式（一次注入）────────────────────────────────────────────────────
  function _injectStyle() {
    if (document.getElementById('ps-wizard-style')) return;
    var css = '' +
      '.psw-ov{z-index:9100}' +
      '.psw-modal{width:min(640px,94vw)}' +
      '.psw-body{flex:1;overflow-y:auto;padding:.4rem 1.1rem 1rem}' +
      '.psw-grid{display:grid;grid-template-columns:1fr 1fr;gap:.6rem}' +
      '@media (max-width:560px){.psw-grid{grid-template-columns:1fr}}' +
      '.psw-card{border:1px solid var(--bd);border-radius:11px;padding:.7rem .75rem;cursor:pointer;' +
        'background:var(--card,var(--bg2,transparent));transition:border-color .15s,box-shadow .15s,transform .12s}' +
      '.psw-card:hover{border-color:var(--accent,#5b7cf6);box-shadow:0 4px 14px rgba(91,124,246,.15);transform:translateY(-1px)}' +
      '.psw-card:focus{outline:2px solid var(--accent,#5b7cf6);outline-offset:1px}' +
      '.psw-card-top{display:flex;align-items:center;gap:.45rem;margin-bottom:.25rem}' +
      '.psw-emoji{font-size:1.25rem;line-height:1}' +
      '.psw-card-name{font-size:.86rem;font-weight:700;color:var(--t1)}' +
      '.psw-card-desc{font-size:.7rem;color:var(--t2);line-height:1.45;min-height:2em}' +
      '.psw-traits{display:flex;flex-wrap:wrap;gap:.25rem;margin-top:.45rem}' +
      '.psw-trait{font-size:.6rem;padding:.08rem .38rem;border-radius:999px;background:rgba(91,124,246,.1);' +
        'color:var(--accent,#5b7cf6);border:1px solid rgba(91,124,246,.25);line-height:1.5}' +
      '.psw-card-blank{border-style:dashed;display:flex;flex-direction:column;align-items:center;justify-content:center;text-align:center}' +
      '.psw-blank-plus{font-size:1.3rem;color:var(--t3);margin-bottom:.2rem;line-height:1}' +
      '.psw-badge{display:flex;align-items:center;gap:.5rem;padding:.5rem .65rem;border:1px solid var(--bd);' +
        'border-radius:10px;background:var(--bg2,transparent);margin-bottom:.85rem;cursor:pointer;transition:border-color .15s}' +
      '.psw-badge:hover{border-color:var(--accent,#5b7cf6)}' +
      '.psw-badge-name{font-weight:700;font-size:.84rem;color:var(--t1)}' +
      '.psw-badge-chg{margin-left:auto;font-size:.68rem;color:var(--accent,#5b7cf6);white-space:nowrap}' +
      '.psw-field{margin-bottom:.7rem}' +
      '.psw-label{display:block;font-size:.76rem;font-weight:600;color:var(--t1);margin-bottom:.28rem}' +
      '.psw-input,.psw-textarea{width:100%;box-sizing:border-box;padding:.5rem .65rem;border:1px solid var(--bd);' +
        'border-radius:8px;background:var(--input,var(--bg));color:var(--t1);font-size:.84rem;outline:none;transition:border-color .15s}' +
      '.psw-input:focus,.psw-textarea:focus{border-color:var(--accent,#5b7cf6)}' +
      '.psw-input.err{border-color:#ef4444}' +
      '.psw-textarea{min-height:74px;resize:vertical;line-height:1.5;font-family:inherit}' +
      '.psw-err{display:none;font-size:.68rem;color:#ef4444;margin-top:.25rem}' +
      '.psw-err.show{display:block}';
    var st = document.createElement('style');
    st.id = 'ps-wizard-style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  // ── 模态骨架（复用 .pp-modal-ov / .pp-modal，懒创建一次）────────────────────
  function _ensureModal() {
    if (_ov) return;
    _injectStyle();
    _ov = document.createElement('div');
    _ov.id = 'psw-ov';
    _ov.className = 'pp-modal-ov psw-ov';
    _ov.innerHTML = '' +
      '<div class="pp-modal psw-modal">' +
        '<div class="pp-modal-hd">' +
          '<div>' +
            '<div class="pp-modal-ttl" id="psw-title"></div>' +
            '<div class="pp-modal-sub" id="psw-sub"></div>' +
          '</div>' +
          '<button type="button" class="pp-modal-x" id="psw-x" title="' + _esc(_t('psn_close', '关闭')) + '">✕</button>' +
        '</div>' +
        '<div class="psw-body" id="psw-body"></div>' +
        '<div class="pp-modal-foot psw-foot" id="psw-foot" style="display:none"></div>' +
      '</div>';
    document.body.appendChild(_ov);
    _ov.addEventListener('click', function (ev) { if (ev.target === _ov) _close(); });
    var x = document.getElementById('psw-x');
    if (x) x.addEventListener('click', _close);
  }

  function _onKeydown(ev) {
    if ((ev.key === 'Escape' || ev.key === 'Esc') && _ov && _ov.classList.contains('open')) _close();
  }

  // ── 第 1 步：选模板 ────────────────────────────────────────────────────────
  function _renderStep1() {
    var sub = document.getElementById('psw-sub');
    var body = document.getElementById('psw-body');
    var foot = document.getElementById('psw-foot');
    if (sub) sub.textContent = _t('psn_wiz_step1', '第 1 步 · 选择模板');
    if (foot) { foot.style.display = 'none'; foot.innerHTML = ''; }
    if (!body) return;

    var h = '<div class="psw-grid">';
    for (var i = 0; i < TEMPLATES.length; i++) {
      var tpl = TEMPLATES[i];
      var traits = (tpl.persona.personality && tpl.persona.personality.traits) || [];
      var chips = '';
      for (var j = 0; j < traits.length; j++) chips += '<span class="psw-trait">' + _esc(traits[j]) + '</span>';
      h += '<div class="psw-card" data-key="' + _esc(tpl.key) + '" tabindex="0">' +
             '<div class="psw-card-top">' +
               '<span class="psw-emoji">' + tpl.emoji + '</span>' +
               '<span class="psw-card-name">' + _esc(_t('psn_wiz_tpl_' + tpl.key, tpl.nameFb)) + '</span>' +
             '</div>' +
             '<div class="psw-card-desc">' + _esc(_t('psn_wiz_tpl_' + tpl.key + '_d', tpl.descFb)) + '</div>' +
             '<div class="psw-traits">' + chips + '</div>' +
           '</div>';
    }
    h += '<div class="psw-card psw-card-blank" id="psw-blank" tabindex="0">' +
           '<div class="psw-blank-plus">＋</div>' +
           '<div class="psw-card-name">' + _esc(_t('psn_wiz_blank', '从空白开始')) + '</div>' +
           '<div class="psw-card-desc">' + _esc(_t('psn_wiz_blank_desc', '不用模板，直接打开完整编辑抽屉')) + '</div>' +
         '</div>';
    h += '</div>';
    body.innerHTML = h;

    var cards = body.querySelectorAll('.psw-card[data-key]');
    for (var k = 0; k < cards.length; k++) {
      (function (card) {
        var pick = function () { _pickTemplate(card.getAttribute('data-key')); };
        card.addEventListener('click', pick);
        card.addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); pick(); }
        });
      })(cards[k]);
    }
    var blank = document.getElementById('psw-blank');
    if (blank) {
      blank.addEventListener('click', _pickBlank);
      blank.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); _pickBlank(); }
      });
    }
  }

  function _pickTemplate(key) {
    for (var i = 0; i < TEMPLATES.length; i++) {
      if (TEMPLATES[i].key === key) { _selTpl = TEMPLATES[i]; _renderStep2(); return; }
    }
  }

  // 「从空白开始」：关向导，交给页面原有的完整编辑抽屉
  function _pickBlank() {
    _close();
    if (window.openNewProfile) window.openNewProfile();
  }

  // ── 第 2 步：基本信息 ──────────────────────────────────────────────────────
  function _renderStep2() {
    if (!_selTpl) { _renderStep1(); return; }
    var tpl = _selTpl;
    var sub = document.getElementById('psw-sub');
    var body = document.getElementById('psw-body');
    var foot = document.getElementById('psw-foot');
    if (sub) sub.textContent = _t('psn_wiz_step2', '第 2 步 · 基本信息');
    if (!body) return;

    var backTxt = _t('psn_wiz_back', '上一步');
    body.innerHTML = '' +
      '<div class="psw-badge" id="psw-badge" title="' + _esc(backTxt) + '">' +
        '<span class="psw-emoji">' + tpl.emoji + '</span>' +
        '<span class="psw-badge-name">' + _esc(_t('psn_wiz_tpl_' + tpl.key, tpl.nameFb)) + '</span>' +
        '<span class="psw-badge-chg">' + _esc(backTxt) + ' ›</span>' +
      '</div>' +
      '<div class="psw-field">' +
        '<label class="psw-label" for="psw-name">' + _esc(_t('psn_wiz_name', '名字')) + '</label>' +
        '<input class="psw-input" id="psw-name" type="text" autocomplete="off" placeholder="' +
          _esc(_t('psn_wiz_name_ph', '给 TA 起个名字（必填）')) + '">' +
        '<div class="psw-err" id="psw-name-err"></div>' +
      '</div>' +
      '<div class="psw-field">' +
        '<label class="psw-label" for="psw-id">' + _esc(_t('psn_wiz_id', 'Profile ID')) + '</label>' +
        '<input class="psw-input" id="psw-id" type="text" autocomplete="off" spellcheck="false" placeholder="' +
          _esc(_t('psn_wiz_id_ph', '留空自动生成；小写字母/数字/下划线')) + '">' +
        '<div class="psw-err" id="psw-id-err"></div>' +
      '</div>' +
      '<div class="psw-field">' +
        '<label class="psw-label" for="psw-role">' + _esc(_t('psn_wiz_role', '角色定位')) + '</label>' +
        '<input class="psw-input" id="psw-role" type="text" autocomplete="off">' +
      '</div>' +
      '<div class="psw-field">' +
        '<label class="psw-label" for="psw-style">' + _esc(_t('psn_wiz_persona_style', '性格速览（创建后可在抽屉里细调）')) + '</label>' +
        '<textarea class="psw-textarea" id="psw-style"></textarea>' +
      '</div>';

    // 值一律走属性赋值（不进 innerHTML），名字/ID 保留往返草稿
    var nameEl = document.getElementById('psw-name');
    var idEl = document.getElementById('psw-id');
    var roleEl = document.getElementById('psw-role');
    var styleEl = document.getElementById('psw-style');
    if (nameEl) nameEl.value = _draftName;
    if (idEl) idEl.value = _draftId;
    if (roleEl) roleEl.value = tpl.persona.role || '';
    if (styleEl) styleEl.value = (tpl.persona.personality && tpl.persona.personality.style) || '';

    if (foot) {
      foot.style.display = 'flex';
      foot.innerHTML = '' +
        '<button type="button" class="btn btn-sm" id="psw-back">' + _esc(backTxt) + '</button>' +
        '<button type="button" class="btn btn-sm btn-primary" id="psw-create">' +
          _esc(_t('psn_wiz_create', '创建人设')) + '</button>';
      var backBtn = document.getElementById('psw-back');
      var createBtn = document.getElementById('psw-create');
      if (backBtn) backBtn.addEventListener('click', _goBack);
      if (createBtn) createBtn.addEventListener('click', _create);
    }

    var badge = document.getElementById('psw-badge');
    if (badge) badge.addEventListener('click', _goBack);
    if (idEl) idEl.addEventListener('input', _onIdInput);
    if (nameEl) nameEl.addEventListener('input', function () { _setFieldErr('psw-name', ''); });
    var enterSubmit = function (ev) { if (ev.key === 'Enter') { ev.preventDefault(); _create(); } };
    if (nameEl) nameEl.addEventListener('keydown', enterSubmit);
    if (idEl) idEl.addEventListener('keydown', enterSubmit);
    if (roleEl) roleEl.addEventListener('keydown', enterSubmit);

    if (_draftId) _onIdInput();
    if (nameEl) nameEl.focus();
  }

  function _goBack() {
    var nameEl = document.getElementById('psw-name');
    var idEl = document.getElementById('psw-id');
    if (nameEl) _draftName = nameEl.value;
    if (idEl) _draftId = idEl.value;
    _renderStep1();
  }

  // ── 校验 ────────────────────────────────────────────────────────────────────
  function _setFieldErr(inputId, msg) {
    var input = document.getElementById(inputId);
    var err = document.getElementById(inputId + '-err');
    if (err) {
      err.textContent = msg || '';
      if (msg) err.classList.add('show'); else err.classList.remove('show');
    }
    if (input) {
      if (msg) input.classList.add('err'); else input.classList.remove('err');
    }
  }

  // ID 输入即时校验：格式不合法 / 与现有 profile 重复 → 红字
  function _onIdInput() {
    var idEl = document.getElementById('psw-id');
    if (!idEl) return true;
    var v = idEl.value.trim();
    var msg = '';
    if (v !== '') {
      if (!ID_RE.test(v)) msg = _t('psn_wiz_id_invalid', 'ID 只能是 2-40 位小写字母、数字或下划线');
      else if (_idExists(v)) msg = _t('psn_wiz_id_exists', '该 ID 已存在，请换一个');
    }
    _setFieldErr('psw-id', msg);
    return msg === '';
  }

  // ── 创建 ────────────────────────────────────────────────────────────────────
  async function _create() {
    if (_submitting || !_selTpl) return;
    var nameEl = document.getElementById('psw-name');
    var idEl = document.getElementById('psw-id');
    var roleEl = document.getElementById('psw-role');
    var styleEl = document.getElementById('psw-style');

    var name = nameEl ? nameEl.value.trim() : '';
    if (!name) {
      _setFieldErr('psw-name', _t('psn_wiz_name_required', '名字不能为空'));
      if (nameEl) nameEl.focus();
      return;
    }

    var pid = idEl ? idEl.value.trim() : '';
    if (pid !== '' && !ID_RE.test(pid)) {
      _setFieldErr('psw-id', _t('psn_wiz_id_invalid', 'ID 只能是 2-40 位小写字母、数字或下划线'));
      if (idEl) idEl.focus();
      return;
    }
    // 留空自动生成：模板key_ + 时间戳 base36 后 6 位
    if (pid === '') pid = _selTpl.key + '_' + Date.now().toString(36).slice(-6);
    if (_idExists(pid)) {
      _setFieldErr('psw-id', _t('psn_wiz_id_exists', '该 ID 已存在，请换一个'));
      if (idEl) idEl.focus();
      return;
    }

    // persona = 深拷贝模板 + {id, name, role, enabled:true}；性格速览写回 personality.style
    var persona = _deepCopy(_selTpl.persona);
    persona.id = pid;
    persona.name = name;
    persona.role = roleEl ? roleEl.value.trim() : (persona.role || '');
    if (!persona.personality || typeof persona.personality !== 'object') persona.personality = {};
    persona.personality.style = styleEl ? styleEl.value.trim() : (persona.personality.style || '');
    persona.enabled = true;

    _submitting = true;
    var btn = document.getElementById('psw-create');
    if (btn) { btn.disabled = true; btn.textContent = _t('psn_wiz_creating', '创建中…'); }
    function _restoreBtn() {
      _submitting = false;
      var b = document.getElementById('psw-create');
      if (b) { b.disabled = false; b.textContent = _t('psn_wiz_create', '创建人设'); }
    }

    try {
      var r = await fetch('/api/personas/profiles/' + encodeURIComponent(pid), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ persona: persona })
      });
      var d = null;
      try { d = await r.json(); } catch (e2) { d = null; }
      if (d && d.ok) {
        _submitting = false;
        _close();
        _notify(_fmt(_t('psn_wiz_created', '已创建「{name}」，接下来补上头像和声音吧'), { name: name }), 'ok');
        if (window.loadProfileList) window.loadProfileList();
        if (window.refreshStatus) window.refreshStatus();
        if (window.editProfile) window.editProfile(pid);
      } else {
        var detail = (d && (d.detail || d.error)) || ('HTTP ' + r.status);
        _notify(_t('psn_js_064', '保存失败: ') + detail, 'err');
        _restoreBtn();
      }
    } catch (e) {
      _notify(_t('psn_js_031', '请求失败: ') + e, 'err');
      _restoreBtn();
    }
  }

  // ── 开关 ────────────────────────────────────────────────────────────────────
  function _open() {
    _ensureModal();
    _selTpl = null;
    _draftName = '';
    _draftId = '';
    var ttl = document.getElementById('psw-title');
    if (ttl) ttl.textContent = _t('psn_wiz_title', '新建人设');
    _renderStep1();
    _ov.classList.add('open');
    document.addEventListener('keydown', _onKeydown);
  }

  function _close() {
    if (_ov) _ov.classList.remove('open');
    document.removeEventListener('keydown', _onKeydown);
  }

  window.PSWizard = { open: _open };
})();
