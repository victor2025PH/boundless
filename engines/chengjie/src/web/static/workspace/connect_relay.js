/* Messenger 应用内登录（表单中继 Form-Relay）前端视图模型。
 *
 * 后端三块（读 relay-step / 写 relay-submit / 边车无窗口 offscreen）已就绪。本文件是前端的
 * **纯视图模型**：吃一份 /relay-step 响应 + 本地提交状态 → 吐「此刻该渲染什么」的规格
 * （表单字段 / 验证中 / 卡住升级截图 / 截图回落 / 成功 / 准备中 / 过期），供 connect-qr-view
 * 的 DOM 渲染器消费。刻意做成纯函数 + 零 DOM 依赖：
 *   - 可 node --test 直测（本目录无 package.json → CJS；浏览器按经典 <script> 挂 window.ConnectRelay）；
 *   - 文案只给 i18n 键（window.T 在渲染器侧取），本模块不含任何中文字面量（过 CJK 门禁）。
 *
 * 设计约束（与 login_relay.js 的后端契约对齐，别让两侧漂移）：
 *   step: credentials → 账密表单；twofactor → 6 位码；e2ee_pin → PIN；
 *         checkpoint → 截图回落（不可预测的安全验证，应用内驱不动，交回落/运维）；
 *         wait/booting → 准备中（可显示截图安抚）；done/authorized → 成功；expired → 过期。
 *
 * B64 续（2026-08-23 23:55 实录「点登录没反应」，本轮重构的三个不变量）：
 *   ① 提交绝不 fire-and-forget：POST 结果必须回到视图（成功→verifying、网络失败→
 *      submit_failed 就地重试、字段缺失→missing 就地报错）——静默吞掉=用户对着
 *      纹丝不动的表单怀疑人生。
 *   ② verifying 有超时升级：提交成功后 step 长时间不前进（FB 卡检查点/慢校验）→
 *      升级为截图视图 + 官方式出路指引，绝不永远转圈。
 *   ③ 每一步都说「现在在干什么 + 接下来会发生什么」（对齐官方登录的叙事节奏），
 *      步骤推进经 onPhase 回调同步给宿主（步骤条/状态行）。
 *
 * B64 三期（2026-08-24 凌晨，「手机确认后卡死」余波收口）：
 *   ④ 「继续」点击回执分道：continuing/continued/continue_miss/continue_fail 本地相位
 *      ——点了没点到必须让用户知道；忙态由相位携带，在非表单态每 2.5s 的整块重渲间存活。
 *   ⑤ device_confirm 子味（relay-step code）→ 手机动作为主视觉 + 三步清单，截图折叠让位
 *      （开合状态挂容器跨重渲存活）；泛检查点仍以截图为主（窗体 chrome + LIVE 徽标包装）。
 *   ⑥ 等待生命感：已等待时长 / 截图更新于 n 秒前 / 会话剩余 <5min 倒计时（spec 由
 *      driveTick 后置补全，视图模型保持纯函数）；解卡后 verifying/preparing 闪现
 *      「验证已通过」正反馈（刻意不进 form——闪现条会逼重渲清掉用户输入）。
 *   ⑦ 「继续」钮收窄为 checkpoint 专属：verify_stuck 时服务端 step 仍在可填步，
 *      relay-submit 的 step 守卫必拒 checkpoint 提交——按钮=必失败的假出口，只留「重试」。
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

  // B65（实施64 P2-c，`_325`）：占位/检查点文案按平台分键——基键此前写死
  // 「登录 Facebook 的邮箱或手机号」，LINE/IG/Zalo 弹窗原样露出。仅白名单平台
  // 换后缀键（i18n 双语齐备才入表；未知平台走中性基键，绝不吐裸键名）。
  var PH_PLATFORMS = { messenger: 1, line: 1, instagram: 1, zalo: 1, whatsapp: 1 };

  function specFor(name, platform) {
    var s = FIELD_SPECS[name];
    // 返回副本，渲染器改不脏内部表；未知字段给一个安全的文本输入兜底（不崩）。
    if (!s) return { name: name, type: 'text', inputmode: 'text', autocomplete: 'off', labelKey: '', phKey: '' };
    var out = { name: s.name, type: s.type, inputmode: s.inputmode, autocomplete: s.autocomplete,
                labelKey: s.labelKey, phKey: s.phKey };
    var p = String(platform || '').toLowerCase();
    if (name === 'email' && PH_PLATFORMS[p]) out.phKey = 'inbox.connect.relay_email_ph_' + p;
    return out;
  }

  // 每步的「标题 / 引导 / 接下来会发生什么」i18n 键（官方式分步叙事，B64 续①③）。
  var STEP_COPY = {
    credentials: {
      headKey: 'inbox.connect.relay_head_credentials',
      hintKey: 'inbox.connect.relay_hint_credentials',
      nextKey: 'inbox.connect.relay_next_credentials',
    },
    twofactor: {
      headKey: 'inbox.connect.relay_head_twofactor',
      hintKey: 'inbox.connect.relay_hint_twofactor',
      nextKey: 'inbox.connect.relay_next_twofactor',
    },
    e2ee_pin: {
      headKey: 'inbox.connect.relay_head_pin',
      hintKey: 'inbox.connect.relay_hint_pin',
      nextKey: '',
    },
  };

  // 提交后 step 仍不前进多久算「卡住」→ 升级截图视图（B64 续②）。
  var VERIFY_ESCALATE_MS = 10000;
  // B64 三期：「继续」点击回执 continued 驻留窗——期间显示「已替你点击、正在跟进」并保持
  // 按钮忙态（非表单态每轮整块重渲，忙态必须由相位携带才能在重渲间存活）；超窗自动回常态
  // 可再点。真推进（step 变化）由 driveTick 清相位，不等这个窗。
  var CONTINUE_WAIT_MS = 60000;
  // 解卡正反馈闪现窗：checkpoint 经「继续」推进成功后，下一视图顶部短暂出现绿色
  // 「验证已通过」条——让「我点了继续→有效」有明确回响（纯按时间窗判，零定时器）。
  var UNSTUCK_FLASH_MS = 4000;

  /**
   * 视图模型：/relay-step 响应 + 本地提交状态 → 渲染规格。纯函数、无副作用、脏输入不抛。
   * @param {object} resp {status, booting, step, fields, error, escalate, code, qr_image}
   * @param {object} opts {platform, local:{phase, ts, detail, step}, qrImage, now}
   *   local.phase: '' | 'submitting' | 'verifying' | 'submit_failed'
   *   qrImage: 宿主从 /status 拿到的登录页实时截图（verify_stuck 视图用）
   * @returns {{mode, fields, submitStep, headKey, hintKey, nextKey, errorKey, noticeCode,
   *            showScreenshot, qrImage, titleKey, submitKey, submitBusy, prefill}}
   */
  function relayFormView(resp, opts) {
    var r = (resp && typeof resp === 'object') ? resp : {};
    var o = opts || {};
    var platform = String(o.platform || '').toLowerCase();
    var local = (o.local && typeof o.local === 'object') ? o.local : {};
    var now = (typeof o.now === 'number') ? o.now : Date.now();
    var status = String(r.status || '');
    var step = String(r.step || '');
    var booting = !!r.booting;
    var qrImage = String(r.qr_image || o.qrImage || '');

    // 终态优先：已授权 / 过期（本地态一律让位——服务端事实赢）。
    if (status === 'authorized' || step === 'done') {
      return _mk('success', { titleKey: 'inbox.connect.relay_success' });
    }
    if (status === 'expired' || step === 'expired') {
      return _mk('expired', { titleKey: 'inbox.connect.relay_expired' });
    }
    // 检查点：不可预测的安全验证，offscreen/headless 无窗口可交互 → 截图回落 + 官方式出路。
    // P1：account_locked（临时锁定/尝试过多）单列文案——出路是「等冷却/去申诉」，
    // 与泛检查点的「当场过验证」完全不同，混谈=给错药方。
    // B64 三期：① device_confirm 子味（边车 checkpointFlavor 判「等手机确认」页）→ 手机
    //   动作为主视觉 + 三步清单，截图降级为折叠区（主角是用户的手，不是那张白截图）；
    // ② 「继续」点击回执分道：continuing（在途）/continued（点到了，跟进中）/
    //   continue_miss（页面上没找到可点按钮）/continue_fail（网络没送达）——旧实现
    //   fire-and-forget，点没点到用户无从得知；忙态由相位携带，在每轮重渲间存活。
    if (step === 'checkpoint' || r.escalate) {
      var code0 = String(r.code || '');
      var locked = code0 === 'account_locked';
      var devconf = !locked && code0 === 'device_confirm';
      var contPhase = (!locked && String(local.step || '') === 'checkpoint')
        ? String(local.phase || '') : '';
      if (contPhase.indexOf('continu') !== 0) contPhase = '';   // 非 continue 族相位不进本视图
      if (contPhase === 'continued'
          && (now - (Number(local.ts) || now)) >= CONTINUE_WAIT_MS) contPhase = '';
      var ckHint;
      if (locked) ckHint = 'inbox.connect.relay_locked_hint';
      else if (contPhase === 'continued') ckHint = 'inbox.connect.relay_continued_hint';
      else if (devconf) ckHint = 'inbox.connect.relay_devconf_hint';
      else ckHint = (PH_PLATFORMS[platform]
        ? 'inbox.connect.relay_checkpoint_' + platform
        : 'inbox.connect.relay_checkpoint');
      return _mk('screenshot', {
        headKey: locked ? 'inbox.connect.relay_locked_title'
          : (devconf ? 'inbox.connect.relay_devconf_title' : 'inbox.connect.relay_stuck_title'),
        hintKey: ckHint,
        nextKey: locked ? 'inbox.connect.relay_locked_ways'
          : (devconf ? '' : 'inbox.connect.relay_stuck_ways'),
        noticeCode: locked ? 'account_locked' : (devconf ? 'device_confirm' : 'checkpoint'),
        devConfirm: devconf,
        contPhase: contPhase,
        contNoticeKey: contPhase === 'continue_miss' ? 'inbox.connect.relay_continue_miss'
          : (contPhase === 'continue_fail' ? 'inbox.connect.relay_continue_fail' : ''),
        showScreenshot: !!qrImage, qrImage: qrImage });
    }
    // 本地提交状态：服务端 step 还停在可填步骤时，本地相位决定「验证中 / 卡住 / 提交失败」。
    var phase = String(local.phase || '');
    var sameStep = String(local.step || '') === step;
    if (phase === 'verifying' && sameStep && _fillable(step)) {
      var waited = now - (Number(local.ts) || now);
      if (waited >= VERIFY_ESCALATE_MS) {
        // 卡住升级：给登录页实时画面 + 出路（手机 App 确认 / 换网络重试），绝不无限转圈。
        return _mk('verify_stuck', {
          headKey: 'inbox.connect.relay_stuck_title',
          hintKey: 'inbox.connect.relay_stuck_hint',
          nextKey: 'inbox.connect.relay_stuck_ways',
          showScreenshot: !!qrImage, qrImage: qrImage,
          submitKey: 'inbox.connect.relay_retry',
        });
      }
      return _mk('verifying', {
        headKey: 'inbox.connect.relay_verifying_title',
        hintKey: 'inbox.connect.relay_verifying_hint',
      });
    }
    // 准备中：冷启 / 认不出的过渡页 → 显示截图安抚（有的话），不渲表单。
    if (booting || step === 'wait' || step === '') {
      return _mk('preparing', { hintKey: 'inbox.connect.relay_preparing',
                                showScreenshot: !!qrImage, qrImage: qrImage });
    }
    // 可填步骤：字段以后端下发的 fields 为准（与 login_relay 契约一致），空则按 step 兜底。
    var names = Array.isArray(r.fields) && r.fields.length ? r.fields.slice() : _defaultFields(step);
    if (!names.length) {
      // 认不出的可填步骤：稳妥回落准备中，别渲空表单。
      return _mk('preparing', { hintKey: 'inbox.connect.relay_preparing',
                                showScreenshot: !!qrImage, qrImage: qrImage });
    }
    var copy = STEP_COPY[step] || {};
    var errorKey = '';
    if (phase === 'submit_failed' && sameStep) {
      errorKey = 'inbox.connect.relay_err_network';        // 提交没送达：就地重试，输入还在
    } else if (phase === 'submit_missing' && sameStep) {
      errorKey = 'inbox.connect.relay_err_missing';        // 字段没填全（本地即拦或服务端判）
    } else if (r.error) {
      // password_error → 账密步带错误横幅（就地重填，不另起一步）。
      errorKey = 'inbox.connect.relay_err_' + (step === 'credentials' ? 'password' : 'generic');
    }
    return _mk('form', {
      submitStep: step,
      fields: names.map(function (n) { return specFor(n, platform); }),
      headKey: copy.headKey || '',
      hintKey: copy.hintKey || '',
      nextKey: copy.nextKey || '',
      // 首步的「准备好三件事」前置卡（P1；只在干净的账密步出——报错重填时不再占版面）
      prepKey: (step === 'credentials' && !errorKey && !r.error)
        ? 'inbox.connect.relay_prep_credentials' : '',
      errorKey: errorKey,
      noticeCode: String(r.code || ''),
      submitKey: 'inbox.connect.relay_submit',
      submitBusy: phase === 'submitting' && sameStep,
      prefill: (local.prefill && typeof local.prefill === 'object') ? local.prefill : null,
    });
  }

  function _fillable(step) {
    return step === 'credentials' || step === 'twofactor' || step === 'e2ee_pin';
  }

  function _defaultFields(step) {
    if (step === 'credentials') return ['email', 'password'];
    if (step === 'twofactor') return ['code'];
    if (step === 'e2ee_pin') return ['pin'];
    return [];
  }

  function _mk(mode, extra) {
    var o = {
      mode: mode, fields: [], submitStep: '', headKey: '', hintKey: '', nextKey: '',
      prepKey: '', errorKey: '', noticeCode: '', showScreenshot: false, qrImage: '',
      titleKey: '', submitKey: '', submitBusy: false, prefill: null,
      // B64 三期：手机确认叙事 / 继续回执 / 等待生命感（-1 或 0 = 不渲染对应行；
      // waitedSec/shotAgeSec/ttlSec/shotOpen/unstuck 由 driveTick 按会话状态后置补全，
      // 视图模型保持纯函数——单测可直接在 spec 上摆值验证渲染）。
      devConfirm: false, contPhase: '', contNoticeKey: '',
      waitedSec: -1, shotAgeSec: -1, ttlSec: 0, shotOpen: false, unstuck: false,
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

  // m:ss 时长（纯 ASCII 免 i18n；不满 1 分钟只出秒，如 "42s" / "3:07"）
  function _fmtDur(sec) {
    var s0 = Math.max(0, Math.floor(Number(sec) || 0));
    var m = Math.floor(s0 / 60);
    var s2 = s0 % 60;
    return m > 0 ? (m + ':' + ('0' + s2).slice(-2)) : (s2 + 's');
  }

  // 手机确认主视觉（内联 SVG：手机 + 通知点 + 对勾；呼吸/描画动效在 CSS
  // .crh-dot/.crh-check）。stroke 用 currentColor，颜色由容器 CSS 令牌决定。
  function _phoneHero() {
    return '<div class="connect-relay-hero" aria-hidden="true">'
      + '<svg viewBox="0 0 64 64" class="crh-phone">'
      + '<rect x="19" y="6" width="26" height="52" rx="6"/>'
      + '<line x1="28" y1="51" x2="36" y2="51"/>'
      + '<circle class="crh-dot" cx="45" cy="15" r="8"/>'
      + '<path class="crh-check" d="M41.5 15l2.5 2.5 5-5"/>'
      + '</svg></div>';
  }

  // 解卡正反馈：checkpoint 经「继续」推进成功后，下一视图顶部闪现绿色确认条
  // （spec.unstuck 由 driveTick 按 UNSTUCK_FLASH_MS 时间窗后置补全）。
  function _unstuckLine(s, t) {
    if (!s.unstuck) return '';
    return '<div class="connect-relay-unstuck" role="status">'
      + _esc(t('inbox.connect.relay_unstuck')) + '</div>';
  }

  /**
   * 视图规格 → HTML 字符串（纯函数，可 node --test）。文案经注入的 t(i18nKey) 取；无 t 则回落
   * 键名（测试/降级）。所有插值走 _esc（qr_image 是 data URI，仍消毒防属性逃逸）。字段带
   * data-relay-field=<name>，提交钮带 data-relay-submit——render() 据此收值/绑定，不靠脆弱选择器。
   */
  function buildFormHtml(spec, opts) {
    var o = opts || {};
    var t = (typeof o.t === 'function') ? o.t : function (k) { return k; };
    // 带参词条（{sec}/{t} 插值）：宿主注入 window.Tf；缺席时用 t() 结果做朴素替换兜底。
    var tf = (typeof o.tf === 'function') ? o.tf : function (k, p) {
      var out = String(t(k));
      p = p || {};
      for (var kk in p) {
        if (Object.prototype.hasOwnProperty.call(p, kk)) {
          out = out.split('{' + kk + '}').join(String(p[kk]));
        }
      }
      return out;
    };
    var s = (spec && typeof spec === 'object') ? spec : { mode: 'preparing' };
    if (s.mode === 'success') {
      // P2：成功画勾动效（与全局 connect-qr-okov 同视觉语言；stroke 动画在 CSS）
      return '<div class="connect-relay-msg ok" data-relay-mode="success">'
        + '<svg class="connect-relay-check" viewBox="0 0 52 52" aria-hidden="true">'
        + '<circle cx="26" cy="26" r="24"/><path d="M15 27l7 7 15-16"/></svg>'
        + '<div>' + _esc(t(s.titleKey || 'inbox.connect.relay_success')) + '</div></div>';
    }
    if (s.mode === 'expired') {
      return '<div class="connect-relay-msg expired" data-relay-mode="expired">'
        + _esc(t(s.titleKey || 'inbox.connect.relay_expired')) + '</div>';
    }
    if (s.mode === 'preparing') {
      var pshot = (s.showScreenshot && s.qrImage)
        ? '<img class="connect-relay-shot" alt="" src="' + _esc(s.qrImage) + '">' : '';
      return '<div class="connect-relay-prep" data-relay-mode="preparing">'
        + _unstuckLine(s, t)
        + '<span class="connect-relay-spin" aria-hidden="true"></span>'
        + '<span class="connect-relay-hint">' + _esc(t(s.hintKey || 'inbox.connect.relay_preparing')) + '</span>'
        + pshot + '</div>';
    }
    if (s.mode === 'verifying') {
      // 提交已送达：明确说「正在验证 + 接下来会发生什么」，官方式叙事而非静默转圈。
      return '<div class="connect-relay-prep" data-relay-mode="verifying">'
        + _unstuckLine(s, t)
        + '<span class="connect-relay-spin" aria-hidden="true"></span>'
        + '<div class="connect-relay-stage">'
        + '<div class="connect-relay-head">' + _esc(t(s.headKey || 'inbox.connect.relay_verifying_title')) + '</div>'
        + '<div class="connect-relay-hint">' + _esc(t(s.hintKey || 'inbox.connect.relay_verifying_hint')) + '</div>'
        + '</div></div>';
    }
    if (s.mode === 'verify_stuck' || s.mode === 'screenshot') {
      // 卡住/检查点（B64 三期重排）：
      //   · device_confirm 子味 → 手机动作为主视觉（hero + 三步清单），截图折叠让位——
      //     主角是「你去手机上点一下」，不是那张缩小的白截图；
      //   · 其余检查点/卡住 → 截图为主（用户必须看着画面过验证），带窗体 chrome + LIVE 徽标；
      //   · 「继续」钮 **checkpoint 专属**：verify_stuck 时服务端 step 仍停在可填步，
      //     relay-submit 的 step 守卫必拒 checkpoint 提交——给按钮=必失败的假出口，
      //     verify_stuck 只留「重试」（回表单重新提交）。account_locked 同理无「继续」。
      //   · 回执 notice（miss/fail）、已等待、环境保留倒计时随 spec 渲染。
      var cparts = ['<div class="connect-relay-checkpoint" data-relay-mode="' + _esc(s.mode) + '"'
        + (s.devConfirm ? ' data-relay-flavor="device_confirm"' : '') + '>'];
      if (s.headKey) cparts.push('<div class="connect-relay-head warn">' + _esc(t(s.headKey)) + '</div>');
      if (s.devConfirm) cparts.push(_phoneHero());
      cparts.push('<div class="connect-relay-hint">' + _esc(t(s.hintKey || 'inbox.connect.relay_checkpoint')) + '</div>');
      if (s.devConfirm) {
        cparts.push('<ol class="connect-relay-steps">'
          + '<li>' + _esc(t('inbox.connect.relay_devconf_s1')) + '</li>'
          + '<li>' + _esc(t('inbox.connect.relay_devconf_s2')) + '</li>'
          + '<li>' + _esc(t('inbox.connect.relay_devconf_s3')) + '</li></ol>');
      }
      if (s.qrImage) {
        var tsL = (typeof s.shotAgeSec === 'number' && s.shotAgeSec >= 0)
          ? '<div class="connect-relay-shot-ts">' + _esc(tf('inbox.connect.relay_shot_ts', { sec: Math.floor(s.shotAgeSec) })) + '</div>' : '';
        var frame = '<div class="connect-relay-shotwrap">'
          + '<span class="crs-chrome" aria-hidden="true"><i></i><i></i><i></i></span>'
          + '<span class="crs-live">LIVE</span>'
          + '<img class="connect-relay-shot zoomable" data-relay-zoom alt="" src="' + _esc(s.qrImage) + '">'
          + '</div>' + tsL;
        if (s.devConfirm) {
          cparts.push('<details class="connect-relay-shotbox" data-relay-shotbox' + (s.shotOpen ? ' open' : '') + '>'
            + '<summary>' + _esc(t('inbox.connect.relay_shot_toggle')) + '</summary>' + frame + '</details>');
        } else {
          cparts.push(frame);
        }
      }
      if (s.nextKey) cparts.push('<div class="connect-relay-next">' + _esc(t(s.nextKey)) + '</div>');
      if (s.contNoticeKey) {
        cparts.push('<div class="connect-relay-notice ' + (s.contPhase === 'continue_fail' ? 'err' : 'warn')
          + '" role="alert">' + _esc(t(s.contNoticeKey)) + '</div>');
      }
      if (s.mode === 'screenshot' && s.noticeCode !== 'account_locked') {
        var contBusy = (s.contPhase === 'continuing' || s.contPhase === 'continued');
        cparts.push('<button type="button" class="connect-relay-submit" data-relay-continue'
          + (contBusy ? ' disabled' : '') + '>'
          + _esc(t(contBusy ? 'inbox.connect.relay_continuing' : 'inbox.connect.relay_continue')) + '</button>');
      }
      if (s.mode === 'verify_stuck' && s.submitKey) {
        cparts.push('<button type="button" class="connect-relay-submit ghost" data-relay-retry>'
          + _esc(t(s.submitKey)) + '</button>');
      }
      var meta = [];
      if (typeof s.waitedSec === 'number' && s.waitedSec >= 5) {
        meta.push(tf('inbox.connect.relay_waited', { t: _fmtDur(s.waitedSec) }));
      }
      if (typeof s.ttlSec === 'number' && s.ttlSec > 0 && s.ttlSec < 300) {
        meta.push(tf('inbox.connect.relay_ttl_left', { t: _fmtDur(s.ttlSec) }));
      }
      if (meta.length) cparts.push('<div class="connect-relay-meta">' + _esc(meta.join(' · ')) + '</div>');
      cparts.push('</div>');
      return cparts.join('');
    }
    // form
    var parts = ['<div class="connect-relay-form" data-relay-mode="form" data-relay-step="'
      + _esc(s.submitStep || '') + '">'];
    if (s.headKey) parts.push('<div class="connect-relay-head">' + _esc(t(s.headKey)) + '</div>');
    if (s.hintKey) parts.push('<div class="connect-relay-hint">' + _esc(t(s.hintKey)) + '</div>');
    if (s.prepKey) parts.push('<div class="connect-relay-next prep">' + _esc(t(s.prepKey)) + '</div>');
    if (s.errorKey) parts.push('<div class="connect-relay-error" role="alert">' + _esc(t(s.errorKey)) + '</div>');
    var fields = s.fields || [];
    for (var i = 0; i < fields.length; i++) {
      var f = fields[i];
      var label = f.labelKey
        ? '<label class="connect-relay-label" for="relay-f-' + _esc(f.name) + '">' + _esc(t(f.labelKey)) + '</label>' : '';
      var pre = (s.prefill && s.prefill[f.name] && f.type !== 'password')
        ? ' value="' + _esc(s.prefill[f.name]) + '"' : '';
      // 密码族字段带「显示/隐藏」眼睛（P1；render 侧接线切换 input.type）
      var eye = (f.type === 'password')
        ? '<button type="button" class="connect-relay-eye" data-relay-eye="' + _esc(f.name) + '">'
          + _esc(t('inbox.connect.relay_eye_show')) + '</button>' : '';
      parts.push('<div class="connect-relay-field' + (eye ? ' has-eye' : '') + '">' + label
        + '<div class="connect-relay-inwrap">'
        + '<input class="connect-relay-input" id="relay-f-' + _esc(f.name) + '"'
        + ' data-relay-field="' + _esc(f.name) + '"'
        + ' type="' + _esc(f.type || 'text') + '"'
        + ' inputmode="' + _esc(f.inputmode || 'text') + '"'
        + ' autocomplete="' + _esc(f.autocomplete || 'off') + '"'
        + pre
        + ' placeholder="' + _esc(f.phKey ? t(f.phKey) : '') + '">' + eye + '</div></div>');
    }
    parts.push('<button type="button" class="connect-relay-submit" data-relay-submit'
      + (s.submitBusy ? ' disabled' : '') + '>'
      + _esc(t(s.submitBusy ? 'inbox.connect.relay_submitting' : (s.submitKey || 'inbox.connect.relay_submit')))
      + '</button>');
    if (s.nextKey) parts.push('<div class="connect-relay-next">' + _esc(t(s.nextKey)) + '</div>');
    parts.push('</div>');
    return parts.join('');
  }

  /**
   * 把视图规格渲进 container（浏览器侧薄壳；node 测试只测 buildFormHtml，不调本函数）。
   * **关键不变量：form 态同步骤不整块重渲**——否则每 2.5s 一轮 poll 会清掉用户正在输入的
   * 账密/验证码（cp-goal 草稿幸存事故同类）。故按 mode:step:error:busy 指纹去重；非 form 态
   * （preparing/verifying/screenshot 无输入可丢）每轮刷新以更新截图/计时。提交经
   * data-relay-field 收值 → onSubmit(step, values)。Enter 键提交与按钮同路径。
   */
  function render(container, spec, handlers) {
    if (!container) return;
    var s = (spec && typeof spec === 'object') ? spec : { mode: 'preparing' };
    var h = handlers || {};
    var key = s.mode + ':' + (s.submitStep || '') + ':' + (s.errorKey || '') + ':' + (s.submitBusy ? 1 : 0);
    if (s.mode === 'form' && container.__relayKey === key) return; // 同步骤不重渲，保住输入
    container.__relayKey = key;
    container.innerHTML = buildFormHtml(s, { t: h.t, tf: h.tf });
    var doSubmit = function () {
      var values = {};
      var inputs = container.querySelectorAll('[data-relay-field]');
      for (var i = 0; i < inputs.length; i++) {
        values[inputs[i].getAttribute('data-relay-field')] = inputs[i].value;
      }
      if (typeof h.onSubmit === 'function') h.onSubmit(s.submitStep, values);
    };
    var btn = container.querySelector('[data-relay-submit]');
    if (btn) {
      btn.addEventListener('click', doSubmit);
      // Enter 提交（与按钮同路径；shift/组合键不拦）
      var inputs2 = container.querySelectorAll('[data-relay-field]');
      for (var j = 0; j < inputs2.length; j++) {
        inputs2[j].addEventListener('keydown', function (ev) {
          if (ev.key === 'Enter' && !ev.shiftKey && !ev.ctrlKey && !ev.altKey) {
            ev.preventDefault(); doSubmit();
          }
        });
      }
    }
    var retryBtn = container.querySelector('[data-relay-retry]');
    if (retryBtn && typeof h.onRetry === 'function') {
      retryBtn.addEventListener('click', function () { h.onRetry(); });
    }
    // B64 二期：检查点「继续」——替用户点离屏登录页的推进按钮。点后进 busy 防连点。
    var contBtn = container.querySelector('[data-relay-continue]');
    if (contBtn && typeof h.onContinue === 'function') {
      contBtn.addEventListener('click', function () {
        contBtn.disabled = true;
        var tt = (h && typeof h.t === 'function') ? h.t : function (k) { return k; };
        contBtn.textContent = tt('inbox.connect.relay_continuing');
        h.onContinue();
      });
    }
    // 密码显隐眼睛（P1）：切 input.type + 换词。不进重渲 key——切换是纯本地视觉。
    var eyes = container.querySelectorAll('[data-relay-eye]');
    for (var e = 0; e < eyes.length; e++) {
      eyes[e].addEventListener('click', function (ev) {
        var b = ev.currentTarget;
        var inp = container.querySelector('[data-relay-field="' + b.getAttribute('data-relay-eye') + '"]');
        if (!inp) return;
        var show = inp.type === 'password';
        inp.type = show ? 'text' : 'password';
        var tt = (h && typeof h.t === 'function') ? h.t : function (k) { return k; };
        b.textContent = tt(show ? 'inbox.connect.relay_eye_hide' : 'inbox.connect.relay_eye_show');
      });
    }
    // 截图点击放大（lightbox 由宿主提供；无宿主回调时退化为新开原图）。
    var shot = container.querySelector('[data-relay-zoom]');
    if (shot) {
      shot.addEventListener('click', function () {
        if (typeof h.onZoom === 'function') h.onZoom(shot.getAttribute('src') || '');
      });
    }
    // 折叠截图开合状态跨重渲存活：非表单态每轮整块重渲，<details> 会被打回默认收起——
    // 用户开着看画面时每 2.5s 被合上一次＝没法用。开合记在容器上，重渲时由 spec.shotOpen 回填。
    var sbox = container.querySelector('[data-relay-shotbox]');
    if (sbox) {
      sbox.addEventListener('toggle', function () { container.__relayShotOpen = sbox.open; });
    }
  }

  /**
   * 一轮交互登录 tick（供 unified_inbox.html 的 pollConnect 在 interactive 时调用，把内联
   * 足迹压到一行）：拉 relay-step → 合成本地提交状态 → 渲染 → 提交经 relay-submit 回填。
   * fetchJson 由宿主注入（带鉴权的 apiFetch→json），本模块不假设任何全局 fetch。
   *
   * B64 续①：提交不再 fire-and-forget——await POST 结果：
   *   成功(submitted/accepted) → local.phase='verifying'（步进叙事 + 超时升级计时起点）；
   *   字段缺失(missing 非空)   → local.phase='submit_missing'（就地红字）；
   *   网络/服务异常            → local.phase='submit_failed'（就地红字 + 输入保留可重试）。
   * 本地状态挂 container.__relayLocal（会话内存活；换会话宿主整块重建容器自然清零）。
   *
   * @param {object} o {container, stepUrl, submitUrl, fetchJson, t, tf, platform, qrImage,
   *                    expiresIn, onPhase, onZoom}
   *   tf=带参词条（window.Tf）；expiresIn=/status 的会话剩余秒（checkpoint 视图 <5min 出倒计时行）
   * @returns {Promise<object|null>} 本轮 relay 响应（宿主可据 status 决定是否已终态）
   */
  function driveTick(o) {
    var opt = o || {};
    if (!opt.container || typeof opt.fetchJson !== 'function') return Promise.resolve(null);
    var container = opt.container;
    var submitUrl = opt.submitUrl;
    var fetchJson = opt.fetchJson;
    if (!container.__relayLocal) container.__relayLocal = { phase: '', ts: 0, step: '', prefill: null };
    var local = container.__relayLocal;

    var onSubmit = function (step, values) {
      // P1 本地即拦：必填字段为空不打网络（服务端 sanitize 也会判，本地拦=零往返反馈）。
      var required = submitPayload(step, values).values;
      for (var rk in required) {
        if (Object.prototype.hasOwnProperty.call(required, rk) && !String(required[rk] || '').trim()) {
          local.phase = 'submit_missing'; local.ts = Date.now(); local.step = step;
          _paint(null);
          return;
        }
      }
      // 立即进入 submitting（按钮禁用 + 「正在提交…」），点击永远有回响。
      local.phase = 'submitting'; local.ts = Date.now(); local.step = step;
      // email 类非敏感字段留作回填（密码/验证码绝不留）。
      var pre = {};
      if (values && values.email) pre.email = String(values.email);
      local.prefill = pre;
      _paint(null);
      Promise.resolve()
        .then(function () {
          return fetchJson(submitUrl, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(submitPayload(step, values)),
          });
        })
        .then(function (res) {
          var r = res || {};
          if (Array.isArray(r.missing) && r.missing.length) {
            local.phase = 'submit_missing'; local.ts = Date.now();
          } else if (r.ok === false) {
            local.phase = 'submit_failed'; local.ts = Date.now();
          } else {
            local.phase = 'verifying'; local.ts = Date.now();
          }
          _paint(null);
        })
        .catch(function () {
          local.phase = 'submit_failed'; local.ts = Date.now();
          _paint(null);
        });
    };
    var onRetry = function () {
      // 卡住视图的「重试」＝回到表单重新提交（本地相位清零；服务端 step 仍是权威）。
      local.phase = ''; local.ts = 0;
      _paint(null);
    };
    var onContinue = function () {
      // 检查点「继续」＝对边车发一次 step=checkpoint 的 relay-submit（边车替点登录页
      // 「继续/This was me」）。B64 三期回执化（旧 fire-and-forget：点没点到用户无从
      // 得知，busy 态 2.5s 后被整块重渲静默冲掉）：
      //   clicked=true  → continued（已替点，跟进页面变化，CONTINUE_WAIT_MS 内保持忙态）
      //   clicked=false → continue_miss（页面上没找到可点按钮 → 指路先去手机确认）
      //   step_changed  → 页面其实已前进：清相位交下一轮 relay-step 重同步（不是错误）
      //   网络/异常     → continue_fail（就地提示再点一次）
      if (local.phase === 'continuing') return;   // 在途防连点
      local.phase = 'continuing'; local.ts = Date.now(); local.step = 'checkpoint';
      _paint(null);
      Promise.resolve()
        .then(function () {
          return fetchJson(submitUrl, {
            method: 'POST', headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(submitPayload('checkpoint', {})),
          });
        })
        .then(function (res) {
          var r = res || {};
          // clicked=点到没点到；Python 路由老版本不透传该键 → 回落 submitted
          //（边车 checkpoint 分支里 submitted===clicked，语义相同；热更先于重启的
          // 中间态必须自洽——JS 立刻上线，.py 白名单补丁要等下个重启窗）。
          var hit = (r.clicked != null) ? !!r.clicked : !!r.submitted;
          if (String(r.reason_code || '') === 'step_changed') { local.phase = ''; local.ts = 0; }
          else if (r.ok && hit) { local.phase = 'continued'; local.ts = Date.now(); }
          else if (r.ok) { local.phase = 'continue_miss'; local.ts = Date.now(); }
          else { local.phase = 'continue_fail'; local.ts = Date.now(); }
          _paint(null);
        })
        .catch(function () {
          local.phase = 'continue_fail'; local.ts = Date.now();
          _paint(null);
        });
    };

    function _paint(resp) {
      var nowTs = Date.now();
      // step 前进（如 credentials→twofactor / checkpoint→done）＝服务端已消费本次提交/推进：
      // 本地相位使命完成，清零防「新步骤还挂着旧 verifying」。continue 族相位被步进清掉
      // ＝「继续」真起效了 → 记 unstuckTs，下一视图闪现「验证已通过」正反馈（B64 三期）。
      if (resp && String(resp.step || '') && String(resp.step) !== String(local.step || '')
          && local.phase) {
        if (local.phase.indexOf('continu') === 0 && String(resp.step) !== 'expired') {
          local.unstuckTs = nowTs;
        }
        local.phase = ''; local.ts = 0;
      }
      var spec = relayFormView(resp || container.__relayResp || {}, {
        platform: opt.platform, local: local, qrImage: opt.qrImage,
      });
      // ── 会话级观感状态后置补全（视图模型保持纯函数，单测可直接摆 spec 值）──
      if (spec.mode !== local.lastMode) { local.lastMode = spec.mode; local.modeSince = nowTs; }
      if (spec.mode === 'screenshot' || spec.mode === 'verify_stuck') {
        // 已等待时长 + 会话剩余（<5min 才渲染，builder 内判）+ 截图新鲜度 + 折叠开合
        spec.waitedSec = Math.floor((nowTs - (local.modeSince || nowTs)) / 1000);
        spec.ttlSec = Math.max(0, Math.floor(Number(opt.expiresIn) || 0));
        spec.shotOpen = !!container.__relayShotOpen;
        var fp = String(opt.qrImage || '');
        fp = fp ? (fp.length + ':' + fp.slice(-48)) : '';
        if (fp && fp !== local.shotFp) { local.shotFp = fp; local.shotTs = nowTs; }
        if (local.shotTs) spec.shotAgeSec = Math.max(0, Math.floor((nowTs - local.shotTs) / 1000));
      }
      // 解卡闪现只进无输入视图（verifying/preparing）：form 有渲染去重键保输入，
      // 闪现条挤进去会逼着重渲、把用户敲了一半的验证码清掉——绝不值得。
      spec.unstuck = !!(local.unstuckTs && (nowTs - local.unstuckTs) < UNSTUCK_FLASH_MS
        && (spec.mode === 'verifying' || spec.mode === 'preparing'));
      render(container, spec, { t: opt.t, tf: opt.tf, onSubmit: onSubmit, onRetry: onRetry,
        onContinue: onContinue, onZoom: opt.onZoom });
      if (typeof opt.onPhase === 'function') {
        try { opt.onPhase(spec.mode, spec); } catch (_e) { /* 宿主回调异常不伤本模块 */ }
      }
      return spec;
    }

    return Promise.resolve()
      .then(function () { return opt.fetchJson(opt.stepUrl); })
      .then(function (resp) {
        var r = resp || {};
        container.__relayResp = r;
        _paint(r);
        return r;
      })
      .catch(function () { return null; });
  }

  var api = { relayFormView: relayFormView, submitPayload: submitPayload, specFor: specFor,
              buildFormHtml: buildFormHtml, render: render, driveTick: driveTick,
              FIELD_SPECS: FIELD_SPECS, VERIFY_ESCALATE_MS: VERIFY_ESCALATE_MS,
              CONTINUE_WAIT_MS: CONTINUE_WAIT_MS, UNSTUCK_FLASH_MS: UNSTUCK_FLASH_MS };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;   // node --test
  else root.ConnectRelay = api;                                               // 浏览器经典 <script>
})(typeof globalThis !== 'undefined' ? globalThis : this);
