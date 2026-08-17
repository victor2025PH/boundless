/* Messenger 应用内登录（表单中继 Form-Relay）前端视图模型。
 *
 * 后端三块（读 relay-step / 写 relay-submit / 边车无窗口 offscreen）已就绪。本文件是前端的
 * **纯视图模型**：吃一份 /relay-step 响应 → 吐「此刻该渲染什么」的规格（表单字段 / 截图回落 /
 * 成功 / 准备中 / 过期），供 connect-qr-view 的 DOM 渲染器消费。刻意做成纯函数 + 零 DOM 依赖：
 *   - 可 node --test 直测（本目录无 package.json → CJS；浏览器按经典 <script> 挂 window.ConnectRelay）；
 *   - 文案只给 i18n 键（window.T 在渲染器侧取），本模块不含任何中文字面量（过 CJK 门禁）。
 *
 * 设计约束（与 login_relay.js 的后端契约对齐，别让两侧漂移）：
 *   step: credentials → 账密表单；twofactor → 6 位码；e2ee_pin → PIN；
 *         checkpoint → 截图回落（不可预测的安全验证，应用内驱不动，交回落/运维）；
 *         wait/booting → 准备中（可显示截图安抚）；done/authorized → 成功；expired → 过期。
 */
(function (root) {
  'use strict';

  // 每个字段的输入属性 + i18n 键（渲染器据此建 <input>，值按 name 收集回 relay-submit）。
  var FIELD_SPECS = {
    email: {
      name: 'email', type: 'text', inputmode: 'email', autocomplete: 'username',
      labelKey: 'inbox.connect.relay_email_label', phKey: 'inbox.connect.relay_email_ph',
    },
    password: {
      name: 'password', type: 'password', inputmode: 'text', autocomplete: 'current-password',
      labelKey: 'inbox.connect.relay_password_label', phKey: 'inbox.connect.relay_password_ph',
    },
    code: {
      name: 'code', type: 'text', inputmode: 'numeric', autocomplete: 'one-time-code',
      labelKey: 'inbox.connect.relay_code_label', phKey: 'inbox.connect.relay_code_ph',
    },
    pin: {
      name: 'pin', type: 'password', inputmode: 'numeric', autocomplete: 'one-time-code',
      labelKey: 'inbox.connect.relay_pin_label', phKey: 'inbox.connect.relay_pin_ph',
    },
  };

  function specFor(name) {
    var s = FIELD_SPECS[name];
    // 返回副本，渲染器改不脏内部表；未知字段给一个安全的文本输入兜底（不崩）。
    if (!s) return { name: name, type: 'text', inputmode: 'text', autocomplete: 'off', labelKey: '', phKey: '' };
    return { name: s.name, type: s.type, inputmode: s.inputmode, autocomplete: s.autocomplete,
             labelKey: s.labelKey, phKey: s.phKey };
  }

  // 每步的引导语 / 错误 i18n 键。
  var STEP_HINT = {
    credentials: 'inbox.connect.relay_hint_credentials',
    twofactor: 'inbox.connect.relay_hint_twofactor',
    e2ee_pin: 'inbox.connect.relay_hint_pin',
  };

  /**
   * 视图模型：/relay-step 响应 → 渲染规格。纯函数、无副作用、脏输入不抛。
   * @param {object} resp {status, booting, step, fields, error, escalate, code, qr_image}
   * @returns {{mode, fields, submitStep, hintKey, errorKey, noticeCode, showScreenshot, qrImage, titleKey, submitKey}}
   */
  function relayFormView(resp) {
    var r = (resp && typeof resp === 'object') ? resp : {};
    var status = String(r.status || '');
    var step = String(r.step || '');
    var booting = !!r.booting;
    var qrImage = String(r.qr_image || '');

    // 终态优先：已授权 / 过期。
    if (status === 'authorized' || step === 'done') {
      return _mk('success', { titleKey: 'inbox.connect.relay_success' });
    }
    if (status === 'expired' || step === 'expired') {
      return _mk('expired', { titleKey: 'inbox.connect.relay_expired' });
    }
    // 准备中：冷启 / 认不出的过渡页 → 显示截图安抚（有的话），不渲表单。
    if (booting || step === 'wait' || step === '') {
      return _mk('preparing', { hintKey: 'inbox.connect.relay_preparing',
                                showScreenshot: !!qrImage, qrImage: qrImage });
    }
    // 检查点：不可预测的安全验证，offscreen/headless 无窗口可交互 → 截图回落 + 转交提示。
    if (step === 'checkpoint' || r.escalate) {
      return _mk('screenshot', { hintKey: 'inbox.connect.relay_checkpoint',
                                 noticeCode: 'checkpoint',
                                 showScreenshot: !!qrImage, qrImage: qrImage });
    }
    // 可填步骤：字段以后端下发的 fields 为准（与 login_relay 契约一致），空则按 step 兜底。
    var names = Array.isArray(r.fields) && r.fields.length ? r.fields.slice() : _defaultFields(step);
    if (!names.length) {
      // 认不出的可填步骤：稳妥回落准备中，别渲空表单。
      return _mk('preparing', { hintKey: 'inbox.connect.relay_preparing',
                                showScreenshot: !!qrImage, qrImage: qrImage });
    }
    return _mk('form', {
      submitStep: step,
      fields: names.map(specFor),
      hintKey: STEP_HINT[step] || '',
      // password_error → 账密步带错误横幅（就地重填，不另起一步）。
      errorKey: r.error ? 'inbox.connect.relay_err_' + (step === 'credentials' ? 'password' : 'generic') : '',
      noticeCode: String(r.code || ''),
      submitKey: 'inbox.connect.relay_submit',
    });
  }

  function _defaultFields(step) {
    if (step === 'credentials') return ['email', 'password'];
    if (step === 'twofactor') return ['code'];
    if (step === 'e2ee_pin') return ['pin'];
    return [];
  }

  function _mk(mode, extra) {
    var o = {
      mode: mode, fields: [], submitStep: '', hintKey: '', errorKey: '',
      noticeCode: '', showScreenshot: false, qrImage: '', titleKey: '', submitKey: '',
    };
    if (extra) for (var k in extra) { if (Object.prototype.hasOwnProperty.call(extra, k)) o[k] = extra[k]; }
    return o;
  }

  /**
   * 收集表单值 → relay-submit 请求体。只挑该步骤的合法字段（与视图模型 fields 对齐），
   * 缺字段留空由后端 sanitizeSubmit 统一判 missing。
   * @param {string} step
   * @param {object} values  {fieldName: value}
   */
  function submitPayload(step, values) {
    var v = (values && typeof values === 'object') ? values : {};
    var names = _defaultFields(step);
    var out = {};
    for (var i = 0; i < names.length; i++) {
      var n = names[i];
      out[n] = (v[n] == null) ? '' : String(v[n]);
    }
    return { step: step, values: out };
  }

  function _esc(s) {
    return String(s == null ? '' : s)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /**
   * 视图规格 → HTML 字符串（纯函数，可 node --test）。文案经注入的 t(i18nKey) 取；无 t 则回落
   * 键名（测试/降级）。所有插值走 _esc（qr_image 是 data URI，仍消毒防属性逃逸）。字段带
   * data-relay-field=<name>，提交钮带 data-relay-submit——render() 据此收值/绑定，不靠脆弱选择器。
   */
  function buildFormHtml(spec, opts) {
    var o = opts || {};
    var t = (typeof o.t === 'function') ? o.t : function (k) { return k; };
    var s = (spec && typeof spec === 'object') ? spec : { mode: 'preparing' };
    if (s.mode === 'success') {
      return '<div class="connect-relay-msg ok" data-relay-mode="success">'
        + _esc(t(s.titleKey || 'inbox.connect.relay_success')) + '</div>';
    }
    if (s.mode === 'expired') {
      return '<div class="connect-relay-msg expired" data-relay-mode="expired">'
        + _esc(t(s.titleKey || 'inbox.connect.relay_expired')) + '</div>';
    }
    if (s.mode === 'preparing') {
      var pshot = (s.showScreenshot && s.qrImage)
        ? '<img class="connect-relay-shot" alt="" src="' + _esc(s.qrImage) + '">' : '';
      return '<div class="connect-relay-prep" data-relay-mode="preparing">'
        + '<span class="connect-relay-spin" aria-hidden="true"></span>'
        + '<span class="connect-relay-hint">' + _esc(t(s.hintKey || 'inbox.connect.relay_preparing')) + '</span>'
        + pshot + '</div>';
    }
    if (s.mode === 'screenshot') {
      var cshot = s.qrImage ? '<img class="connect-relay-shot" alt="" src="' + _esc(s.qrImage) + '">' : '';
      return '<div class="connect-relay-checkpoint" data-relay-mode="screenshot">' + cshot
        + '<div class="connect-relay-hint">' + _esc(t(s.hintKey || 'inbox.connect.relay_checkpoint')) + '</div></div>';
    }
    // form
    var parts = ['<div class="connect-relay-form" data-relay-mode="form" data-relay-step="'
      + _esc(s.submitStep || '') + '">'];
    if (s.hintKey) parts.push('<div class="connect-relay-hint">' + _esc(t(s.hintKey)) + '</div>');
    if (s.errorKey) parts.push('<div class="connect-relay-error" role="alert">' + _esc(t(s.errorKey)) + '</div>');
    var fields = s.fields || [];
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      var label = f.labelKey
        ? '<label class="connect-relay-label" for="relay-f-' + _esc(f.name) + '">' + _esc(t(f.labelKey)) + '</label>' : '';
      parts.push('<div class="connect-relay-field">' + label
        + '<input class="connect-relay-input" id="relay-f-' + _esc(f.name) + '"'
        + ' data-relay-field="' + _esc(f.name) + '"'
        + ' type="' + _esc(f.type || 'text') + '"'
        + ' inputmode="' + _esc(f.inputmode || 'text') + '"'
        + ' autocomplete="' + _esc(f.autocomplete || 'off') + '"'
        + ' placeholder="' + _esc(f.phKey ? t(f.phKey) : '') + '"></div>');
    }
    parts.push('<button type="button" class="connect-relay-submit" data-relay-submit>'
      + _esc(t(s.submitKey || 'inbox.connect.relay_submit')) + '</button>');
    parts.push('</div>');
    return parts.join('');
  }

  /**
   * 把视图规格渲进 container（浏览器侧薄壳；node 测试只测 buildFormHtml，不调本函数）。
   * **关键不变量：form 态同步骤不整块重渲**——否则每 2.5s 一轮 poll 会清掉用户正在输入的
   * 账密/验证码（cp-goal 草稿幸存事故同类）。故按 mode:step:error 指纹去重；非 form 态
   * （preparing/screenshot 无输入可丢）每轮刷新以更新截图。提交经 data-relay-field 收值 →
   * onSubmit(step, values)。
   */
  function render(container, spec, handlers) {
    if (!container) return;
    var s = (spec && typeof spec === 'object') ? spec : { mode: 'preparing' };
    var h = handlers || {};
    var key = s.mode + ':' + (s.submitStep || '') + ':' + (s.errorKey || '');
    if (s.mode === 'form' && container.__relayKey === key) return; // 同步骤不重渲，保住输入
    container.__relayKey = key;
    container.innerHTML = buildFormHtml(s, { t: h.t });
    var btn = container.querySelector('[data-relay-submit]');
    if (btn && typeof h.onSubmit === 'function') {
      btn.addEventListener('click', function () {
        var values = {};
        var inputs = container.querySelectorAll('[data-relay-field]');
        for (var i = 0; i < inputs.length; i++) {
          values[inputs[i].getAttribute('data-relay-field')] = inputs[i].value;
        }
        h.onSubmit(s.submitStep, values);
      });
    }
  }

  /**
   * 一轮交互登录 tick（供 unified_inbox.html 的 pollConnect 在 interactive 时调用，把内联
   * 足迹压到一行）：拉 relay-step → 渲染原生表单 → 提交经 relay-submit 回填。fetchJson 由
   * 宿主注入（带鉴权的 apiFetch→json），本模块不假设任何全局 fetch。提交 fire-and-forget：
   * 结果（→2FA / 密码错 / 授权）由下一轮 tick 的 relay-step 重渲观测（render 同步骤不清屏）。
   * 返回本轮 relay 响应（宿主可据 status 决定是否已终态）。任何异常软回落 null，绝不抛。
   *
   * @param {object} o {container, stepUrl, submitUrl, fetchJson, t}
   * @returns {Promise<object|null>}
   */
  function driveTick(o) {
    var opt = o || {};
    if (!opt.container || typeof opt.fetchJson !== 'function') return Promise.resolve(null);
    var submitUrl = opt.submitUrl;
    var fetchJson = opt.fetchJson;
    var onSubmit = function (step, values) {
      try {
        fetchJson(submitUrl, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(submitPayload(step, values)),
        });
      } catch (_e) { /* 提交异常不阻断：下一轮 tick 仍会重渲当前步 */ }
    };
    return Promise.resolve()
      .then(function () { return opt.fetchJson(opt.stepUrl); })
      .then(function (resp) {
        var r = resp || {};
        render(opt.container, relayFormView(r), { t: opt.t, onSubmit: onSubmit });
        return r;
      })
      .catch(function () { return null; });
  }

  var api = { relayFormView: relayFormView, submitPayload: submitPayload, specFor: specFor,
              buildFormHtml: buildFormHtml, render: render, driveTick: driveTick,
              FIELD_SPECS: FIELD_SPECS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;   // node --test
  else root.ConnectRelay = api;                                               // 浏览器经典 <script>
})(typeof globalThis !== 'undefined' ? globalThis : this);
