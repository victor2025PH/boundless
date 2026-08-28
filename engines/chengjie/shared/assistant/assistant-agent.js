/* 小智「替我做」智能体 + 流星执行秀（实施58 P2，2026-08-23）。
 *
 * 与 assistant-ball.js（形象线）零符号依赖，只做 DOM 协作（同 teach 模块）：
 *   - 入口：面板注入「🤖 替我做」行（MutationObserver 自愈）；
 *   - 链路：目标一句话 → POST /api/assistant/agent/plan（零副作用计划）→
 *     任务卡逐步执行——每步转投 POST /api/assistant/act（P1 的确认/撤销/
 *     审计三板斧在服务端，本文件不开第二条写通道）；
 *   - L2 步必出**确认卡**（diff 现值→新值 + 应用/跳过），应用后带**撤销**；
 *   - **流星（融合契约落地）**：起飞时取样球的当前视觉（computedStyle
 *     backgroundImage——形象线改球主题流星自动继承），球隐身化作彗头，
 *     贝塞尔飞行 + 拖尾粒子，到达目标涟漪；任务收尾飞回球位还原。
 *     prefers-reduced-motion / 球缺席 → 自动退化为零动画直执行；
 *   - 跨页续跑：goto 步经 sessionStorage(xza_task_v1) 在新页恢复任务；
 *   - 后端未装载（路由 404/403）→ 入口如实显示「待装载」，绝不装死。
 *
 * 门禁：tests/test_assistant_agent_wireup.py（版本戳同步/零内联 handler/
 * 词典 zh-en 齐平/零球符号依赖）。
 */
(function () {
  'use strict';
  if (window.XZAgent) { return; }

  var VER = '20260828a';

  var I18N = {
    zh: {
      row_btn: '🤖 替我做 · 一句话交给小智',
      row_hint: '规划后逐步执行，改设置前必先确认',
      mode_t: '一句话交给小智',
      mode_d: '在下面说一件要我做的事，我会先出计划再逐步执行。',
      recent_t: '最近做过',
      pair_card_t: '换个说法：用手机指挥',
      pair_card_d: '手机扫码连上这台电脑，躺着用语音/打字派活；'
        + '连接状态见标题栏的 📱。',
      p_title: '替我做',
      p_ph: '例如：把回复速度调快一点 / 为什么不自动回复了',
      p_run: '开始',
      p_close: '关闭',
      p_na: '智能体后端待装载或未启用（重启窗后自动可用）',
      planning: '小智规划中…',
      plan_empty: '这个目标我还做不了：',
      plan_empty2: '想知道我能做什么？点下面任意一件试试：',
      sa_online: '已连接 · 电脑在线',
      sa_off: '连接中断 · 看看电脑开着没',
      hist_btn: '📜 做过什么',
      hist_title: '小智做过什么',
      hist_empty: '还没有操作记录',
      op_apply: '改了',
      op_undo: '↩ 撤销了',
      ask_ph: '回答小智的问题…',
      ask_send: '发送',
      say_fb: '按下面步骤执行：',
      st_run: '执行中…',
      st_wait: '等你确认',
      st_ok: '完成',
      st_fail: '失败',
      st_skip: '已跳过',
      st_nav: '带你去',
      cf_apply: '✅ 确认应用',
      cf_skip: '跳过这步',
      cf_hint: '改动可撤销；不确认不会写入',
      undo: '↩ 撤销',
      undone: '已撤销',
      hot_worker: '已热更生效',
      hot_restart: '写入成功（重启窗后全量生效）',
      diag_line: '结论：',
      diag_warn: '，需关注 ',
      diag_warn2: ' 项',
      stop: '⏹ 停止',
      stopped: '已停止，剩余步骤未执行',
      done_line: '全部完成',
      done_part: '执行结束（部分步骤未成功）',
      replay: '🔁 再看一遍',
      resume: '继续上次任务…',
      net_err: '网络异常，请重试',
      mic_title: '语音说目标（点击开始，再点结束）',
      mic_rec: '录音中…再点一次结束',
      mic_busy: '转写中…',
      mic_fail: '语音转写失败，请改用文字',
      mic_denied: '麦克风不可用（桌面版可用）',
      tts_on: '🔊 播报：开',
      tts_off: '🔇 播报：关',
      say_confirm: '这一步需要你确认',
      nav_on_pc: '该页请在电脑上看（手机端已跳过）',
      pair_btn: '📱 手机操控',
      pair_title: '手机扫码操控',
      pair_hint: '同 WiFi 下用手机相机扫码即用；二维码 2 分钟内有效、单次核销',
      pair_regen: '↻ 重新生成',
      pair_sessions: '已连接的手机',
      pair_none: '暂无',
      pair_kick: '踢下线',
      pair_fail: '生成失败，请重试',
      pair_url: '或手机浏览器打开：',
      pair_lan_down: '局域网入口未就绪：手机现在打不开这个地址。请点「重新生成」；仍不行就把电脑与手机连同一 WiFi（不要用流量）。',
      fl_start: '✅ 开始',
      fl_no: '取消',
      fl_step_confirm: '确认要做的事',
      fl_step_preflight: '连通预检',
      fl_step_qr: '生成二维码',
      fl_step_wait: '扫码与确认',
      fl_step_done: '完成',
      fl_waiting: '等待扫码…',
      fl_pwd_ph: '两步验证云密码…',
      fl_pwd_send: '提交',
      fl_pwd_bad: '密码不对，可重试',
      fl_canceled: '已取消登录',
      fl_fail: '登录失败',
      fl_timeout: '等待超时，已取消本次登录',
      fl_next_t: '下一步：',
      fl_next_persona: '🎭 去绑人设',
      fl_next_reply: '⚙️ 设回复模式',
    },
    en: {
      row_btn: '🤖 Do it for me',
      row_hint: 'Plans first; settings need your confirm',
      mode_t: 'Tell me in one sentence',
      mode_d: 'Say what you need below — I draft a plan, then run it step by step.',
      recent_t: 'Recently done',
      pair_card_t: 'Another way: drive it from your phone',
      pair_card_d: 'Scan to pair your phone with this desktop and dictate tasks; '
        + 'connection status lives on the 📱 in the title bar.',
      p_title: 'Do it for me',
      p_ph: 'e.g. speed up replies a bit / why is auto-reply off',
      p_run: 'Go',
      p_close: 'Close',
      p_na: 'Agent backend not loaded/enabled yet (auto after restart window)',
      planning: 'Planning…',
      plan_empty: 'I cannot do this yet: ',
      plan_empty2: 'Want to see what I can do? Tap one:',
      sa_online: 'Connected · PC online',
      sa_off: 'Disconnected · is the PC on?',
      hist_btn: '📜 History',
      hist_title: 'What the assistant did',
      hist_empty: 'No operations yet',
      op_apply: 'changed',
      op_undo: '↩ undid',
      ask_ph: 'Answer the question…',
      ask_send: 'Send',
      say_fb: 'Executing these steps:',
      st_run: 'Running…',
      st_wait: 'Waiting for confirm',
      st_ok: 'Done',
      st_fail: 'Failed',
      st_skip: 'Skipped',
      st_nav: 'Navigating',
      cf_apply: '✅ Apply',
      cf_skip: 'Skip',
      cf_hint: 'Reversible; nothing is written until you confirm',
      undo: '↩ Undo',
      undone: 'Undone',
      hot_worker: 'Hot-applied',
      hot_restart: 'Written (fully effective after restart window)',
      diag_line: 'Verdict: ',
      diag_warn: ', attention items: ',
      diag_warn2: '',
      stop: '⏹ Stop',
      stopped: 'Stopped; remaining steps not executed',
      done_line: 'All done',
      done_part: 'Finished (some steps failed)',
      replay: '🔁 Replay',
      resume: 'Resuming previous task…',
      net_err: 'Network error, please retry',
      mic_title: 'Speak the goal (click to start/stop)',
      mic_rec: 'Recording… click again to stop',
      mic_busy: 'Transcribing…',
      mic_fail: 'Transcription failed, please type',
      mic_denied: 'Microphone unavailable (works in desktop app)',
      tts_on: '🔊 Voice: on',
      tts_off: '🔇 Voice: off',
      say_confirm: 'This step needs your confirmation',
      nav_on_pc: 'View that page on the PC (skipped on phone)',
      pair_btn: '📱 Phone control',
      pair_title: 'Scan to control from phone',
      pair_hint: 'Scan with the phone camera on the same WiFi; QR valid 2 min, single use',
      pair_regen: '↻ Regenerate',
      pair_sessions: 'Connected phones',
      pair_none: 'None',
      pair_kick: 'Kick',
      pair_fail: 'Failed, please retry',
      pair_url: 'Or open in the phone browser: ',
      pair_lan_down: 'LAN entrance is down — the phone cannot open this address. Tap Regenerate; keep phone and PC on the same WiFi (not cellular).',
      fl_start: '✅ Start',
      fl_no: 'Cancel',
      fl_step_confirm: 'Confirm the goal',
      fl_step_preflight: 'Preflight',
      fl_step_qr: 'Generate QR',
      fl_step_wait: 'Scan & verify',
      fl_step_done: 'Done',
      fl_waiting: 'Waiting for scan…',
      fl_pwd_ph: 'Cloud password…',
      fl_pwd_send: 'Submit',
      fl_pwd_bad: 'Wrong password, try again',
      fl_canceled: 'Login canceled',
      fl_fail: 'Login failed',
      fl_timeout: 'Timed out, login canceled',
      fl_next_t: 'Next: ',
      fl_next_persona: '🎭 Bind a persona',
      fl_next_reply: '⚙️ Reply settings',
    },
  };

  var S = {
    shell: 'admin', lang: 'zh', api: null, claimed: false,
    panel: null, card: null, task: null, running: false, stopFlag: false,
    head: null, ballWasHidden: false, confirmResolve: null, probeOk: null,
    voiceOk: null, rec: null, audio: null, standalone: false, pair: null,
    hist: null,
  };
  function ttsOn() {
    try { return localStorage.getItem('xza_tts_on') !== '0'; }
    catch (e) { return true; }
  }
  function setTts(v) {
    try { localStorage.setItem('xza_tts_on', v ? '1' : '0'); }
    catch (e) { /* */ }
  }

  function t(k) {
    var d = I18N[S.lang] || I18N.zh;
    return d[k] || I18N.zh[k] || k;
  }
  function esc(s) {
    return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;',
               "'": '&#39;' }[c];
    });
  }
  function beacon(action) {
    try {
      navigator.sendBeacon('/api/telemetry/ui-event', new Blob(
        [JSON.stringify({ page: (location && location.pathname) || '',
                          action: String(action) })],
        { type: 'application/json' }));
    } catch (e) { /* best-effort */ }
  }
  function csrfHeaders(extra) {
    var h = extra || {};
    var m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/);
    if (m) { h['X-CSRF-Token'] = m[1]; }
    return h;
  }
  function post(url, body) {
    return fetch(url, {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(body || {}),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        j.__status = r.status;
        return j;
      });
    });
  }
  function reducedMotion() {
    try {
      return window.matchMedia &&
        window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    } catch (e) { return false; }
  }

  /* ── 样式 ── */
  function injectCss() {
    if (document.getElementById('xza-style')) { return; }
    var css = '' +
/* P0-3 2026-08-27（与 assistant-teach.js 同批同理）：虚线→实线+柔和底
   （「没做完」观感），hint 独占整行不再截断。两文件规格必须保持一致，
   否则同一面板里两行入口长得不一样。 */
'.xza-panel{position:fixed;z-index:10006;width:320px;max-width:calc(100vw - 20px);' +
'background:var(--xz-bg,#fff);color:var(--xz-txt,#111);border:1px solid var(--xz-bd,#ddd);' +
'border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.26);padding:.7rem .8rem;' +
'right:14px;bottom:80px;font-size:.8rem;animation:xzaUp .18s ease}' +
'@keyframes xzaUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}' +
'.xza-p-t{font-weight:700;margin-bottom:.4rem;display:flex;align-items:center;gap:.4rem}' +
'.xza-p-t .x{margin-left:auto;border:none;background:none;cursor:pointer;' +
'color:var(--xz-muted,#888);font-size:.9rem;padding:.1rem .3rem}' +
'.xza-panel textarea{width:100%;box-sizing:border-box;resize:none;min-height:54px;' +
'border:1px solid var(--xz-bd,#ddd);border-radius:9px;background:var(--xz-input,#f6f7f9);' +
'color:var(--xz-txt,#111);font-family:inherit;font-size:.8rem;padding:.45rem .55rem;outline:none}' +
'.xza-panel textarea:focus{border-color:var(--xz-accent,#4f6ef7)}' +
'.xza-p-row{display:flex;gap:.4rem;margin-top:.5rem;align-items:center}' +
'.xza-p-row .na{font-size:.68rem;color:#d97706}' +
'.xza-run{border:none;border-radius:9px;background:var(--xz-accent,#4f6ef7);color:#fff;' +
'cursor:pointer;font-family:inherit;font-size:.78rem;font-weight:600;padding:.4rem .9rem}' +
'.xza-run:disabled{opacity:.5;cursor:default}' +
'.xza-mic{border:1px solid var(--xz-bd,#ddd);border-radius:9px;background:var(--xz-input,#f6f7f9);' +
'cursor:pointer;font-size:.9rem;padding:.34rem .5rem;font-family:inherit}' +
'.xza-mic.rec{background:#ef4444;border-color:#ef4444;animation:xzaRec 1.2s ease-in-out infinite}' +
'@keyframes xzaRec{0%,100%{box-shadow:0 0 0 0 rgba(239,68,68,.4)}' +
'50%{box-shadow:0 0 0 7px rgba(239,68,68,0)}}' +
'.xza-snd{border:none;background:none;cursor:pointer;color:var(--xz-muted,#888);' +
'font-size:.68rem;font-family:inherit;padding:.1rem .35rem;border-radius:6px}' +
'.xza-snd:hover{background:var(--xz-input,#f3f4f6)}' +
'.xza-card{position:fixed;z-index:10004;width:330px;max-width:calc(100vw - 20px);' +
'background:var(--xz-bg,#fff);color:var(--xz-txt,#111);border:1px solid var(--xz-bd,#ddd);' +
'border-radius:14px;box-shadow:0 12px 40px rgba(0,0,0,.28);right:14px;bottom:80px;' +
'font-size:.78rem;display:flex;flex-direction:column;max-height:70vh;animation:xzaUp .18s ease}' +
'.xza-c-hd{display:flex;align-items:center;gap:.4rem;padding:.55rem .7rem;' +
'border-bottom:1px solid var(--xz-bd,#ddd);font-weight:700}' +
'.xza-c-hd .stop{margin-left:auto;border:1px solid #ef4444;background:rgba(239,68,68,.08);' +
'color:#ef4444;border-radius:8px;cursor:pointer;font-family:inherit;font-size:.7rem;' +
'font-weight:700;padding:.2rem .55rem}' +
'.xza-c-hd .x{border:none;background:none;cursor:pointer;color:var(--xz-muted,#888);' +
'font-size:.9rem;padding:.1rem .3rem}' +
'.xza-c-bd{padding:.5rem .7rem;overflow-y:auto;display:flex;flex-direction:column;gap:.4rem}' +
'.xza-say{color:var(--xz-muted,#667);font-size:.74rem}' +
'.xza-step{border:1px solid var(--xz-bd,#e5e7eb);border-radius:10px;padding:.4rem .55rem}' +
'.xza-st-hd{display:flex;align-items:center;gap:.4rem}' +
'.xza-st-hd .ic{flex-shrink:0;width:1.1em;text-align:center}' +
'.xza-st-hd .lb{flex:1;min-width:0;font-weight:600;overflow:hidden;' +
'text-overflow:ellipsis;white-space:nowrap}' +
'.xza-st-hd .lv{flex-shrink:0;font-size:.62rem;border-radius:999px;padding:.06rem .4rem;' +
'background:var(--xz-input,#eef);color:var(--xz-muted,#667)}' +
'.xza-st-note{font-size:.7rem;color:var(--xz-muted,#667);margin-top:.2rem;word-break:break-word}' +
'.xza-st.cur{border-color:var(--xz-accent,#4f6ef7);box-shadow:0 0 0 2px rgba(79,110,247,.15)}' +
'.xza-st.fail{border-color:rgba(239,68,68,.5)}' +
'.xza-cf{margin-top:.35rem;border-top:1px dashed var(--xz-bd,#ddd);padding-top:.35rem}' +
'.xza-cf-d{font-size:.72rem;margin:.12rem 0;display:flex;gap:.35rem;align-items:baseline}' +
'.xza-cf-d code{background:rgba(127,127,127,.14);border-radius:4px;padding:.02rem .3rem;' +
'font-size:.68rem}' +
'.xza-cf-d .arr{color:var(--xz-accent,#4f6ef7);font-weight:700}' +
'.xza-cf-row{display:flex;gap:.4rem;margin-top:.35rem;align-items:center}' +
'.xza-cf-row button{border:1px solid var(--xz-bd,#ddd);border-radius:8px;cursor:pointer;' +
'font-family:inherit;font-size:.72rem;padding:.26rem .6rem;background:var(--xz-input,#f5f6f8);' +
'color:var(--xz-txt,#333)}' +
'.xza-cf-row button.pri{background:var(--xz-accent,#4f6ef7);border-color:var(--xz-accent,#4f6ef7);' +
'color:#fff;font-weight:700}' +
'.xza-cf-hint{font-size:.64rem;color:var(--xz-muted,#999);margin-top:.25rem}' +
'.xza-undo{border:1px dashed var(--xz-bd,#ccc);background:none;border-radius:7px;' +
'cursor:pointer;font-family:inherit;font-size:.68rem;padding:.14rem .45rem;' +
'color:var(--xz-muted,#667);margin-top:.25rem}' +
'.xza-undo:hover{border-color:var(--xz-accent,#4f6ef7);color:var(--xz-accent,#4f6ef7)}' +
'.xza-ft{padding:.45rem .7rem;border-top:1px solid var(--xz-bd,#ddd);display:flex;' +
'gap:.4rem;align-items:center;font-size:.72rem}' +
'.xza-ft .rep{border:1px solid var(--xz-bd,#ddd);background:none;border-radius:8px;' +
'cursor:pointer;font-family:inherit;font-size:.7rem;padding:.2rem .5rem;' +
'color:var(--xz-txt,#333)}' +
'.xza-head{position:fixed;z-index:10007;width:20px;height:20px;border-radius:50%;' +
'pointer-events:none;background:linear-gradient(135deg,#4f6ef7,#8b5cf6);' +
'box-shadow:0 0 14px 4px rgba(99,102,241,.65),0 0 30px 8px rgba(139,92,246,.35)}' +
'.xza-trail{position:fixed;z-index:10005;width:7px;height:7px;border-radius:50%;' +
'pointer-events:none;background:linear-gradient(135deg,#4f6ef7,#8b5cf6);opacity:.75;' +
'transition:opacity .5s ease,transform .5s ease}' +
'.xza-pulse{animation:xzaPulse .7s ease}' +
'@keyframes xzaPulse{0%{box-shadow:0 0 0 0 rgba(99,102,241,.55)}' +
'100%{box-shadow:0 0 0 16px rgba(99,102,241,0)}}' +
'.xza-qr{text-align:center;padding:.3rem 0}' +
'.xza-qr img{width:190px;height:190px;border-radius:10px;background:#fff;' +
'border:1px solid var(--xz-bd,#ddd)}' +
'.xza-qr .qh{font-size:.68rem;color:var(--xz-muted,#888);margin-top:.3rem}' +
'.xza-pin{font-size:1.5rem;font-weight:800;text-align:center;' +
'letter-spacing:.22em;color:var(--xz-accent,#4f6ef7);padding:.2rem 0}' +
'.xza-pwd-row{display:flex;gap:.4rem;margin:.4rem 0 .1rem}' +
'.xza-pwd-row input{flex:1;min-width:0;border:1px solid var(--xz-bd,#ddd);' +
'border-radius:9px;background:var(--xz-input,#f6f7f9);color:var(--xz-txt,#111);' +
'font-family:inherit;font-size:.82rem;padding:.4rem .55rem;outline:none}' +
'.xza-pwd-row input:focus{border-color:var(--xz-accent,#4f6ef7)}' +
'.xza-chips{display:flex;flex-wrap:wrap;gap:.35rem;margin:.25rem 0}' +
'.xza-chips button{border:1px solid var(--xz-accent,#4f6ef7);border-radius:999px;' +
'background:none;color:var(--xz-accent,#4f6ef7);cursor:pointer;font-family:inherit;' +
'font-size:.7rem;padding:.22rem .6rem;max-width:100%;overflow:hidden;' +
'text-overflow:ellipsis;white-space:nowrap}' +
'.xza-chips button:hover{background:rgba(79,110,247,.1)}' +
'.xza-sa-dot{display:flex;align-items:center;gap:6px;font-size:.68rem;color:#9aa3c0}' +
'.xza-sa-dot i{width:8px;height:8px;border-radius:50%;background:#22c55e;' +
'box-shadow:0 0 8px rgba(34,197,94,.8);flex-shrink:0}' +
'.xza-sa-dot.off i{background:#ef4444;box-shadow:0 0 8px rgba(239,68,68,.8)}' +
'.xza-sa-dot.off em{color:#fca5a5}' +
'.xza-sa-dot em{font-style:normal}' +
'.xza-hist{display:flex;flex-direction:column;max-height:72vh}' +
'.xza-hist .hist-list{overflow-y:auto;margin-top:.35rem;min-height:60px}' +
'.hist-row{border-top:1px dashed var(--xz-bd,#ddd);padding:.42rem 0;font-size:.74rem}' +
'.hist-row.ud{opacity:.72}' +
'.hist-row .hr-meta{font-size:.62rem;color:var(--xz-muted,#999);margin-top:.15rem}' +
'.hist-row .hr-main{word-break:break-word}' +
/* standalone（手机操控页）：输入面板钉底、任务卡占上方，全宽拇指优先 */
'.xza-sa .xza-panel{left:8px;right:8px;bottom:calc(30px + env(safe-area-inset-bottom));' +
'width:auto;max-width:none}' +
'.xza-sa .xza-panel textarea{min-height:64px;font-size:.9rem}' +
'.xza-sa .xza-run{font-size:.9rem;padding:.5rem 1.1rem}' +
'.xza-sa .xza-mic{font-size:1.05rem;padding:.44rem .62rem}' +
'.xza-sa .xza-card{left:8px;right:8px;bottom:auto;top:96px;width:auto;' +
'max-width:none;max-height:52vh}' +
'.xza-pair{position:fixed;z-index:10008;left:50%;top:50%;transform:translate(-50%,-50%);' +
'width:320px;max-width:calc(100vw - 24px);background:var(--xz-bg,#fff);' +
'color:var(--xz-txt,#111);border:1px solid var(--xz-bd,#ddd);border-radius:14px;' +
'box-shadow:0 16px 48px rgba(0,0,0,.35);padding:.8rem .9rem;font-size:.78rem;' +
'animation:xzaUp .18s ease}' +
'.xza-pair img{display:block;margin:.5rem auto;width:200px;height:200px;' +
'border-radius:8px;background:#fff}' +
'.xza-pair .url{font-size:.64rem;color:var(--xz-muted,#888);word-break:break-all;' +
'margin:.3rem 0}' +
'.xza-pair .ses{border-top:1px dashed var(--xz-bd,#ddd);margin-top:.5rem;' +
'padding-top:.45rem}' +
'.xza-pair .ses-row{display:flex;align-items:center;gap:.4rem;font-size:.7rem;' +
'margin:.2rem 0}' +
'.xza-pair .ses-row .ua{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;' +
'white-space:nowrap;color:var(--xz-muted,#777)}' +
'.xza-pair .ses-row button{border:1px solid #ef4444;color:#ef4444;background:none;' +
'border-radius:7px;cursor:pointer;font-family:inherit;font-size:.66rem;' +
'padding:.1rem .4rem}' +
'@media(prefers-reduced-motion:reduce){.xza-panel,.xza-card,.xza-pair{animation:none}}';
    var st = document.createElement('style');
    st.id = 'xza-style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  /* 画布令牌 --xz-*：与 assistant-ball.js / assistant-teach.js 的同名函数
     逐字等值（P0-4 三套前缀合一）。别再改回私有前缀。 */
  function themeVars() {
    return S.shell === 'workspace'
      ? { '--xz-bg': 'var(--tk-surface,#fff)',
          '--xz-bd': 'var(--tk-border,#e2e8f0)',
          '--xz-txt': 'var(--tk-text,#0f172a)',
          '--xz-muted': 'var(--tk-text-muted,#64748b)',
          '--xz-accent': 'var(--tk-brand,#4f6ef7)',
          '--xz-input': 'var(--tk-bg,#f1f5f9)' }
      : { '--xz-bg': 'var(--card,#fff)',
          '--xz-bd': 'var(--bd,#e5e7eb)',
          '--xz-txt': 'var(--t,#111827)',
          '--xz-muted': 'var(--t3,#6b7280)',
          '--xz-accent': 'var(--p,#4f6ef7)',
          '--xz-input': 'var(--input,#f3f4f6)' };
  }
  function applyVars(el) {
    var vars = themeVars();
    for (var k in vars) {
      if (Object.prototype.hasOwnProperty.call(vars, k)) {
        el.style.setProperty(k, vars[k]);
      }
    }
  }

  /* ── 面板「替我做」模式（实施73 P1-1，2026-08-28）──
     此前是面板底部一行三枚虚线胶囊（替我做 / 手机操控 / 做过什么）。现在：
     替我做升格为模式条第三格；**手机操控搬去球的标题栏**（它是本模式的远程
     输入端，不是第四种能力——实施73 §3.1）并在本模式内再给一张说明卡（两处
     曝光，语义诚实）；**「做过什么」并入本模式**成常驻「最近做过」3 条 +
     就地撤销（P1-7：撤销必须在手边，不能藏在第二层弹层里）。 */
  function mountMode(el, api) {
    S.api = api;
    el.innerHTML = '' +
      '<div class="asb-md-hero">' +
      '<div class="asb-md-t">⚡ <span>' + esc(t('mode_t')) + '</span></div>' +
      '<div class="asb-md-d">' + esc(t('mode_d')) + '</div>' +
      '<div class="asb-md-safe">🛡 ' + esc(t('row_hint')) + '</div></div>' +
      '<div class="asb-md-hero">' +
      '<div class="asb-md-t">📱 <span>' + esc(t('pair_card_t')) + '</span></div>' +
      '<div class="asb-md-d">' + esc(t('pair_card_d')) + '</div>' +
      '<div class="asb-md-row">' +
      '<button type="button" class="asb-md-b" data-xza="mode-pair">' +
      esc(t('pair_btn')) + '</button></div></div>' +
      '<div class="asb-md-t">📜 <span>' + esc(t('recent_t')) + '</span></div>' +
      '<div class="asb-md-list xza-recent"><div class="asb-empty">…</div></div>' +
      '<div class="asb-md-row">' +
      '<button type="button" class="asb-md-b" data-xza="mode-hist">' +
      esc(t('hist_btn')) + '</button></div>';
    applyVars(el);
    el.addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-xza]');
      if (!b) { return; }
      var a = b.getAttribute('data-xza');
      if (a === 'mode-pair') { beacon('asb_pair_open'); openPairModal(); return; }
      if (a === 'mode-hist') { beacon('asb_hist_open'); openHistory(); return; }
      if (a === 'recent-undo') {
        beacon('asb_hist_undo');
        b.disabled = true;
        post('/api/assistant/act/undo', { undo_id: b.getAttribute('data-undo') })
          .then(function (j) {
            if (j.__status === 200 && j.ok) {
              loadRecent(el.querySelector('.xza-recent'));
              return;
            }
            b.disabled = false;
            b.textContent = String(j.detail || t('net_err')).slice(0, 24);
          });
      }
    });
    loadRecent(el.querySelector('.xza-recent'));
  }

  /* 「最近做过」3 条：与历史弹层同一条 /api/assistant/act/history，只是把
     最该被看见的三条 + 撤销钮提到模式首屏。取数失败静默——它是补充信息，
     不该让整个模式卡变成错误页。 */
  function loadRecent(list) {
    if (!list) { return; }
    fetch('/api/assistant/act/history?lang=' + S.lang)
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        if (!list.isConnected) { return; }
        var items = (j && j.ok && j.items) ? j.items.slice(0, 3) : [];
        if (!items.length) {
          list.innerHTML = '<div class="asb-empty">' + esc(t('hist_empty')) +
            '</div>';
          return;
        }
        var h = '';
        for (var i = 0; i < items.length; i++) {
          var it = items[i] || {};
          var d = new Date((Number(it.ts) || 0) * 1000);
          var when = (d.getMonth() + 1) + '-' + d.getDate() + ' ' +
            ('0' + d.getHours()).slice(-2) + ':' +
            ('0' + d.getMinutes()).slice(-2);
          h += '<div class="asb-md-li"><span class="m">' +
            esc(it.label || it.action_label || '') + '</span>' +
            '<span class="w">' + esc(when) + '</span>' +
            (it.undoable
              ? '<button type="button" class="xza-undo" data-xza="recent-undo"' +
                ' data-undo="' + esc(it.undo_id) + '">' + esc(t('undo')) +
                '</button>'
              : '') + '</div>';
        }
        list.innerHTML = h;
      })
      .catch(function () { /* 静默 */ });
  }

  /* ── 输入小面板 ── */
  function probeBackend() {
    if (S.probeOk !== null) { return Promise.resolve(S.probeOk); }
    return fetch('/api/assistant/actions').then(function (r) {
      S.probeOk = r.ok;
      return S.probeOk;
    }).catch(function () { S.probeOk = false; return false; });
  }
  function probeVoice() {
    /* mic 三前提：assistant.voice 开 + 安全上下文 mediaDevices + MediaRecorder */
    if (S.voiceOk !== null) { return Promise.resolve(S.voiceOk); }
    if (!(navigator.mediaDevices && navigator.mediaDevices.getUserMedia &&
          window.MediaRecorder)) {
      S.voiceOk = false;
      return Promise.resolve(false);
    }
    return fetch('/api/assistant/bootstrap', { cache: 'no-store' })
      .then(function (r) { return r.ok ? r.json() : {}; })
      .then(function (b) {
        S.voiceOk = !!(b && b.enabled && b.voice);
        return S.voiceOk;
      }).catch(function () { S.voiceOk = false; return false; });
  }

  /* ── 语音说目标（复用 /api/assistant/transcribe，与球同链） ── */
  function stopRec(cancel) {
    if (!S.rec) { return; }
    S.rec.cancelled = !!cancel;
    try { S.rec.mr.stop(); } catch (e) {
      try {
        S.rec.stream.getTracks().forEach(function (tk) { tk.stop(); });
      } catch (e2) { /* */ }
      S.rec = null;
    }
  }
  function toggleRec(micBtn, ta) {
    if (S.rec) { stopRec(false); return; }
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (st) {
      var mime = '';
      try {
        if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
          mime = 'audio/webm;codecs=opus';
        }
      } catch (e) { /* */ }
      var mr;
      try {
        mr = mime ? new MediaRecorder(st, { mimeType: mime })
          : new MediaRecorder(st);
      } catch (e) {
        st.getTracks().forEach(function (tk) { tk.stop(); });
        ta.placeholder = t('mic_fail');
        return;
      }
      var chunks = [];
      mr.ondataavailable = function (ev) {
        if (ev.data && ev.data.size) { chunks.push(ev.data); }
      };
      mr.onstop = function () {
        var cancelled = S.rec && S.rec.cancelled;
        if (S.rec && S.rec.cap) { clearTimeout(S.rec.cap); }
        st.getTracks().forEach(function (tk) { tk.stop(); });
        S.rec = null;
        micBtn.classList.remove('rec');
        var blob = new Blob(chunks, { type: mr.mimeType || 'audio/webm' });
        if (cancelled || blob.size < 1200) {
          ta.placeholder = t('p_ph');
          return;
        }
        ta.placeholder = t('mic_busy');
        var rd = new FileReader();
        rd.onload = function () {
          post('/api/assistant/transcribe',
               { audio_b64: String(rd.result || '') })
            .then(function (j) {
              ta.placeholder = t('p_ph');
              if (j.__status === 200 && j.ok && j.text) {
                ta.value = String(j.text);
                try { ta.focus(); } catch (e) { /* */ }
                beacon('asb_agent_voice_goal');
              } else {
                ta.placeholder = String(j.detail || t('mic_fail'));
              }
            });
        };
        rd.readAsDataURL(blob);
      };
      S.rec = { mr: mr, stream: st, cancelled: false,
                cap: setTimeout(function () { stopRec(false); }, 45000) };
      mr.start();
      micBtn.classList.add('rec');
      ta.placeholder = t('mic_rec');
    }).catch(function () { ta.placeholder = t('mic_denied'); });
  }

  /* ── 小智播报（自有 TTS 快档：常热 edge 秒级出声；best-effort 绝不阻塞） ── */
  function speak(text) {
    if (!ttsOn()) { return; }
    text = String(text || '').trim().slice(0, 180);
    if (!text) { return; }
    post('/api/voice/tts-test', { text: text, fast: true, format: 'mp3' })
      .then(function (j) {
        if (j.__status !== 200 || !j.ok || !j.url) { return; }
        try {
          if (S.audio) { try { S.audio.pause(); } catch (e) { /* */ } }
          S.audio = new Audio(j.url);
          S.audio.play().catch(function () { /* 自动播放策略拒绝=静默 */ });
        } catch (e) { /* */ }
      });
  }
  function closePanel() {
    if (S.panel) { try { S.panel.remove(); } catch (e) { /* */ } }
    S.panel = null;
  }
  function openPanel() {
    closePanel();
    var ballPanel = document.querySelector('.asb-panel');
    if (ballPanel && ballPanel.classList.contains('open')) {
      var x = ballPanel.querySelector('.asb-x');
      if (x) { try { x.click(); } catch (e) { /* */ } }
    }
    var p = document.createElement('div');
    p.className = 'xza-panel';
    applyVars(p);
    p.innerHTML = '' +
      '<div class="xza-p-t">🤖 <span>' + esc(t('p_title')) + '</span>' +
      '<button type="button" class="x" data-xza="hist" title="' +
      esc(t('hist_title')) + '" aria-label="' + esc(t('hist_title')) +
      '">🕘</button>' +
      '<button type="button" class="x" data-xza="close">✕</button></div>' +
      '<textarea placeholder="' + esc(t('p_ph')) + '"></textarea>' +
      '<div class="xza-p-row"><button type="button" class="xza-run" ' +
      'data-xza="run">' + esc(t('p_run')) + '</button>' +
      '<span class="na" style="display:none">' + esc(t('p_na')) +
      '</span></div>';
    document.body.appendChild(p);
    S.panel = p;
    var ta = p.querySelector('textarea');
    setTimeout(function () { try { ta.focus(); } catch (e) { /* */ } }, 60);
    probeBackend().then(function (ok) {
      if (!ok && S.panel === p) {
        p.querySelector('.na').style.display = '';
        p.querySelector('.xza-run').disabled = true;
      }
    });
    probeVoice().then(function (ok) {
      if (!ok || S.panel !== p) { return; }
      var row = p.querySelector('.xza-p-row');
      var mic = document.createElement('button');
      mic.type = 'button';
      mic.className = 'xza-mic';
      mic.setAttribute('data-xza', 'mic');
      mic.title = t('mic_title');
      mic.textContent = '🎤';
      row.insertBefore(mic, row.firstChild.nextSibling);
    });
    p.addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-xza]');
      if (!b) { return; }
      if (b.getAttribute('data-xza') === 'close') {
        if (S.rec) { stopRec(true); }
        closePanel();
        return;
      }
      if (b.getAttribute('data-xza') === 'mic') { toggleRec(b, ta); return; }
      if (b.getAttribute('data-xza') === 'hist') {
        beacon('asb_hist_open');
        openHistory();
        return;
      }
      if (b.getAttribute('data-xza') === 'chip') {
        var g3 = String(b.getAttribute('data-goal') || '');
        if (g3) {
          beacon('asb_agent_chip');
          if (!S.standalone) { closePanel(); }
          runGoal(g3);
        }
        return;
      }
      if (b.getAttribute('data-xza') === 'run') {
        var goal = String(ta.value || '').trim();
        if (!goal) { ta.focus(); return; }
        if (S.rec) { stopRec(true); }
        if (S.standalone) { ta.value = ''; } else { closePanel(); }
        runGoal(goal);
      }
    });
    p.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter' && !ev.shiftKey) {
        /* 中文输入法选字回车 ≠ 提交（isComposing/229 守卫，2026-08-23） */
        if (ev.isComposing || ev.keyCode === 229) { return; }
        ev.preventDefault();
        var goal = String(ta.value || '').trim();
        if (!goal) { return; }
        if (S.standalone) { ta.value = ''; } else { closePanel(); }
        runGoal(goal);
      }
    });
    if (S.standalone) { saChips(p, ta); }
  }

  /* ── 快捷指令 chips（动作池目录即菜单；「做不了」死路变菜单同源） ── */
  function abilityLabels(cb) {
    var actP = fetch('/api/assistant/actions?lang=' + S.lang)
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
    var flowP = fetch('/api/assistant/flows?lang=' + S.lang)
      .then(function (r) { return r.ok ? r.json() : null; })
      .catch(function () { return null; });
    Promise.all([actP, flowP]).then(function (rs) {
      var labels = [];
      var fl = (rs[1] && rs[1].flows) || [];
      for (var i = 0; i < fl.length && labels.length < 2; i++) {
        if (fl[i] && fl[i].label) { labels.push(String(fl[i].label)); }
      }
      var ac = (rs[0] && rs[0].actions) || [];
      for (var k = 0; k < ac.length && labels.length < 7; k++) {
        var a = ac[k] || {};
        if (a.id === 'goto_page') { continue; }
        if (a.label) { labels.push(String(a.label)); }
      }
      cb(labels);
    });
  }
  function chipsHtml(labels) {
    var h = '<div class="xza-chips">';
    for (var i = 0; i < labels.length; i++) {
      h += '<button type="button" data-xza="chip" data-goal="' +
        esc(labels[i]) + '">' + esc(labels[i]) + '</button>';
    }
    return h + '</div>';
  }
  function saChips(p, ta) {
    abilityLabels(function (labels) {
      if (!labels.length || S.panel !== p || !p.isConnected) { return; }
      var strip = document.createElement('div');
      strip.innerHTML = chipsHtml(labels);
      p.insertBefore(strip.firstChild, ta);
    });
  }

  /* ── 任务历史 + 撤销中心（P1）：审计尾部 N 条，桌面/手机同一端点 ── */
  function closeHist() {
    if (S.hist) { try { S.hist.remove(); } catch (e) { /* */ } }
    S.hist = null;
  }
  function openHistory() {
    closeHist();
    closePair();
    var m = document.createElement('div');
    m.className = 'xza-pair xza-hist';
    applyVars(m);
    m.innerHTML = '<div class="xza-p-t">📜 <span>' + esc(t('hist_title')) +
      '</span><button type="button" class="x" data-xza="hist-close">✕' +
      '</button></div>' +
      '<div class="hist-list"><div class="xza-say">…</div></div>';
    document.body.appendChild(m);
    S.hist = m;
    m.addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-xza]');
      if (!b) { return; }
      var a = b.getAttribute('data-xza');
      if (a === 'hist-close') { closeHist(); return; }
      if (a === 'hist-undo') {
        beacon('asb_hist_undo');
        b.disabled = true;
        post('/api/assistant/act/undo',
             { undo_id: b.getAttribute('data-undo') })
          .then(function (j) {
            if (j.__status === 200 && j.ok) { loadHistory(); return; }
            b.disabled = false;
            b.textContent = String(j.detail || t('net_err')).slice(0, 24);
          });
      }
    });
    loadHistory();
  }
  function loadHistory() {
    var list = S.hist && S.hist.querySelector('.hist-list');
    if (!list) { return; }
    fetch('/api/assistant/act/history?lang=' + S.lang)
      .then(function (r) {
        if (r.status === 404) { return { __pending: true }; }
        return r.ok ? r.json() : null;
      })
      .then(function (j) {
        if (!S.hist || !list.isConnected) { return; }
        if (j && j.__pending) {
          /* 旧后端特性探测：端点未装载=如实待装，不装死不报网络错 */
          list.innerHTML = '<div class="xza-say">' + esc(t('p_na')) + '</div>';
          return;
        }
        if (!j || !j.ok) {
          list.innerHTML = '<div class="xza-say" style="color:#dc2626">' +
            esc(t('net_err')) + '</div>';
          return;
        }
        var items = j.items || [];
        if (!items.length) {
          list.innerHTML = '<div class="xza-say">' + esc(t('hist_empty')) +
            '</div>';
          return;
        }
        var h = '';
        for (var i = 0; i < items.length; i++) {
          var it = items[i] || {};
          var d = new Date((Number(it.ts) || 0) * 1000);
          var when = (d.getMonth() + 1) + '-' + d.getDate() + ' ' +
            ('0' + d.getHours()).slice(-2) + ':' +
            ('0' + d.getMinutes()).slice(-2);
          var line = it.op === 'undo'
            ? esc(t('op_undo')) + ' ' + esc(it.label || it.action_label || '') +
              (it.new_h ? ' → ' + esc(it.new_h) : '')
            : esc(it.label || it.action_label || '') + '：' + esc(it.old_h) +
              ' → <b>' + esc(it.new_h) + '</b>';
          h += '<div class="hist-row' + (it.op === 'undo' ? ' ud' : '') + '">' +
            '<div class="hr-main">' + line + '</div>' +
            '<div class="hr-meta">' + esc(when) + ' · ' +
            esc(it.actor || '') + '</div>' +
            (it.undoable
              ? '<button type="button" class="xza-undo" data-xza="hist-undo"' +
                ' data-undo="' + esc(it.undo_id) + '">' + esc(t('undo')) +
                '</button>'
              : '') +
            '</div>';
        }
        list.innerHTML = h;
      })
      .catch(function () {
        if (list.isConnected) {
          list.innerHTML = '<div class="xza-say" style="color:#dc2626">' +
            esc(t('net_err')) + '</div>';
        }
      });
  }

  /* ── 手机配对弹层（桌面端） ── */
  function closePair() {
    if (S.pair) { try { S.pair.remove(); } catch (e) { /* */ } }
    S.pair = null;
  }
  function openPairModal() {
    closePair();
    var m = document.createElement('div');
    m.className = 'xza-pair';
    applyVars(m);
    m.innerHTML = '<div class="xza-p-t">📱 <span>' + esc(t('pair_title')) +
      '</span><button type="button" class="x" data-xza="pair-close">✕' +
      '</button></div>' +
      '<div style="font-size:.7rem;color:var(--xz-muted,#888)">' +
      esc(t('pair_hint')) + '</div>' +
      '<div class="qrbox"><div class="xza-say" style="text-align:center;' +
      'padding:1.2rem 0">…</div></div>' +
      '<div class="xza-p-row">' +
      '<button type="button" class="xza-run" data-xza="pair-regen">' +
      esc(t('pair_regen')) + '</button></div>' +
      '<div class="ses"><b style="font-size:.72rem">' +
      esc(t('pair_sessions')) + '</b><div class="ses-list"></div></div>';
    document.body.appendChild(m);
    S.pair = m;
    m.addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-xza]');
      if (!b) { return; }
      var a = b.getAttribute('data-xza');
      if (a === 'pair-close') { closePair(); return; }
      if (a === 'pair-regen') { loadPairQr(); return; }
      if (a === 'pair-kick') {
        beacon('asb_pair_kick');
        post('/api/assistant/pair/revoke',
             { msid: b.getAttribute('data-msid') })
          .then(function () { loadPairSessions(); });
      }
    });
    loadPairQr();
    loadPairSessions();
  }
  function loadPairQr() {
    var box = S.pair && S.pair.querySelector('.qrbox');
    if (!box) { return; }
    post('/api/assistant/pair', {}).then(function (j) {
      if (!S.pair || !box.isConnected) { return; }
      if (j.__status !== 200 || !j.ok) {
        box.innerHTML = '<div class="xza-say" style="color:#dc2626">' +
          esc(String(j.detail || t('pair_fail'))) + '</div>';
        return;
      }
      box.innerHTML = (j.lan_ok === false
        ? '<div class="xza-say" style="color:#dc2626;margin-bottom:.5rem">' +
          esc(t('pair_lan_down')) + '</div>'
        : '') +
        (j.qr_b64
        ? '<img alt="QR" src="' + esc(j.qr_b64) + '">'
        : '') +
        '<div class="url">' + esc(t('pair_url')) + esc(String(j.url || '')) +
        '</div>';
    });
  }
  function loadPairSessions() {
    var list = S.pair && S.pair.querySelector('.ses-list');
    if (!list) { return; }
    fetch('/api/assistant/pair/sessions').then(function (r) {
      return r.ok ? r.json() : { sessions: [] };
    }).then(function (j) {
      if (!S.pair || !list.isConnected) { return; }
      var rows = (j && j.sessions) || [];
      if (!rows.length) {
        list.innerHTML = '<div class="xza-say">' + esc(t('pair_none')) +
          '</div>';
        return;
      }
      var html = '';
      for (var i = 0; i < rows.length; i++) {
        var r2 = rows[i];
        var d = new Date((Number(r2.ts) || 0) * 1000);
        html += '<div class="ses-row"><span class="ua">' +
          esc(String(r2.uname || '')) + ' · ' +
          esc(String(r2.ua || '').slice(0, 42)) + ' · ' +
          esc(d.toLocaleTimeString()) + '</span>' +
          '<button type="button" data-xza="pair-kick" data-msid="' +
          esc(String(r2.msid || '')) + '">' + esc(t('pair_kick')) +
          '</button></div>';
      }
      list.innerHTML = html;
    }).catch(function () { /* best-effort */ });
  }

  /* ── 任务卡 ── */
  function closeCard() {
    if (S.card) { try { S.card.remove(); } catch (e) { /* */ } }
    S.card = null;
  }
  function stIcon(status) {
    return { pending: '○', cur: '➤', ok: '✓', fail: '✗', skip: '▷',
             wait: '⏸' }[status] || '○';
  }
  function lvHuman(level) {
    /* 老板令 2026-08-23：界面不出现 L0/L2 黑话，等级说人话 */
    var zh = S.lang !== 'en';
    return { L0: zh ? '查询' : 'read',
             L1: zh ? '带路' : 'nav',
             L2: zh ? '改设置·可撤销' : 'setting · undoable',
             L3: zh ? '登录流程' : 'login flow' }[level] || String(level || '');
  }
  function renderCard() {
    var task = S.task;
    if (!task) { return; }
    if (!S.card) {
      var c = document.createElement('div');
      c.className = 'xza-card';
      applyVars(c);
      document.body.appendChild(c);
      S.card = c;
      c.addEventListener('click', onCardClick);
      c.addEventListener('keydown', function (ev) {
        if (ev.key !== 'Enter' || ev.isComposing || ev.keyCode === 229) {
          return;
        }
        var box = ev.target && ev.target.closest &&
          ev.target.closest('.xza-ask');
        if (!box) { return; }
        ev.preventDefault();
        var btn = box.querySelector('[data-xza="ask-send"]');
        if (btn) { btn.click(); }
      });
    }
    /* flow 密码框在整卡重渲染间保值（绝不入 task/存储，只回填 DOM） */
    var pwdKeep = '';
    var pwdOld = S.card.querySelector('.xza-pwd-row input');
    if (pwdOld) { pwdKeep = String(pwdOld.value || ''); }
    var html = '<div class="xza-c-hd">🤖 <span>' + esc(t('p_title')) +
      '</span>' +
      '<button type="button" class="xza-snd" data-xza="tts" ' +
      'style="margin-left:auto">' + esc(t(ttsOn() ? 'tts_on' : 'tts_off')) +
      '</button>' +
      (S.running ? '<button type="button" class="stop" data-xza="stop">' +
        esc(t('stop')) + '</button>'
        : '<button type="button" class="x" data-xza="close-card">✕</button>') +
      '</div><div class="xza-c-bd">';
    html += '<div class="xza-say">' + esc(task.say || t('say_fb')) + '</div>';
    if (task.ask && !task.flow) {
      /* clarify 追问（P1）：目标含糊时规划器只回一句反问，就地补答重规划 */
      html += '<div class="xza-cf xza-ask"><div class="xza-cf-d"><b>' +
        esc(task.ask) + '</b></div>' +
        '<div class="xza-pwd-row">' +
        '<input type="text" autocomplete="off" placeholder="' +
        esc(t('ask_ph')) + '">' +
        '<button type="button" class="xza-run" data-xza="ask-send">' +
        esc(t('ask_send')) + '</button></div></div>';
    }
    if (task.finished && task.chips && task.chips.length) {
      html += chipsHtml(task.chips);
    }
    if (task.flow) {
      var fv = task.fvars || {};
      if (fv.qr_img) {
        html += '<div class="xza-qr"><img alt="QR" src="' + esc(fv.qr_img) +
          '">' + (fv.pin ? '<div class="xza-pin">' + esc(fv.pin) + '</div>'
            : '') +
          '<div class="qh">' + esc(fv.qr_hint || '') + '</div></div>';
      }
      if (task.fneed === 'confirm') {
        html += '<div class="xza-cf"><div class="xza-cf-d"><b>' +
          esc(fv.confirm_text || '') + '</b></div>' +
          '<div class="xza-cf-row">' +
          '<button type="button" class="pri" data-xza="fl-yes">' +
          esc(t('fl_start')) + '</button>' +
          '<button type="button" data-xza="fl-no">' + esc(t('fl_no')) +
          '</button></div></div>';
      }
      if (task.fneed === 'pwd') {
        html += '<div class="xza-pwd-row">' +
          '<input type="password" autocomplete="off" placeholder="' +
          esc(t('fl_pwd_ph')) + '">' +
          '<button type="button" class="xza-run" data-xza="fl-pwd-send">' +
          esc(t('fl_pwd_send')) + '</button></div>';
      }
      /* 完成后的下一步引导（老板原话「完成后还可以继续下一步的操作引导」）：
         手机 standalone 隐藏——目标页是桌面后台页 */
      if (task.finished && task.allOk && !S.standalone) {
        html += '<div class="xza-cf-row"><span style="font-size:.7rem;' +
          'color:var(--xz-muted,#888)">' + esc(t('fl_next_t')) + '</span>' +
          '<button type="button" data-xza="fl-next" data-goto="/personas">' +
          esc(t('fl_next_persona')) + '</button>' +
          '<button type="button" data-xza="fl-next" data-goto="/reply-settings">' +
          esc(t('fl_next_reply')) + '</button></div>';
      }
    }
    for (var i = 0; i < task.steps.length; i++) {
      var s = task.steps[i];
      var cls = 'xza-step' + (s.status === 'cur' || s.status === 'wait'
        ? ' cur' : '') + (s.status === 'fail' ? ' fail' : '');
      html += '<div class="' + cls + '" data-idx="' + i + '">' +
        '<div class="xza-st-hd"><span class="ic">' + stIcon(s.status) +
        '</span><span class="lb">' + esc(s.label || s.action) + '</span>' +
        '<span class="lv">' + esc(lvHuman(s.level)) + '</span></div>';
      if (s.note) {
        html += '<div class="xza-st-note">' + esc(s.note) + '</div>';
      }
      if (s.status === 'wait' && s.confirm) {
        html += '<div class="xza-cf">';
        var diff = s.confirm.diff || [];
        for (var j = 0; j < diff.length; j++) {
          var d = diff[j];
          /* 人话优先：后端 label/old_h/new_h（2026-08-23）；旧后端回落路径尾 */
          var name = d.label ||
            String(d.path || '').split('.').slice(-2).join('.');
          var oldS = (d.old_h != null && d.old_h !== '')
            ? d.old_h : String(d.old);
          var newS = (d.new_h != null && d.new_h !== '')
            ? d.new_h : String(d.new);
          html += '<div class="xza-cf-d"><code>' + esc(name) + '</code>' +
            '<span>' + esc(oldS) + '</span>' +
            '<span class="arr">→</span><b>' + esc(newS) +
            '</b></div>';
        }
        html += '<div class="xza-cf-row">' +
          '<button type="button" class="pri" data-xza="cf-apply">' +
          esc(t('cf_apply')) + '</button>' +
          '<button type="button" data-xza="cf-skip">' + esc(t('cf_skip')) +
          '</button></div>' +
          '<div class="xza-cf-hint">' + esc(t('cf_hint')) + '</div></div>';
      }
      if (s.undo_id && !s.undone) {
        html += '<button type="button" class="xza-undo" data-xza="undo" ' +
          'data-undo="' + esc(s.undo_id) + '" data-idx="' + i + '">' +
          esc(t('undo')) + '</button>';
      }
      html += '</div>';
    }
    html += '</div>';
    if (!S.running && task.finished) {
      html += '<div class="xza-ft"><span>' +
        esc(task.allOk ? t('done_line') : t('done_part')) + '</span>' +
        (task.flow ? ''
          : '<button type="button" class="rep" data-xza="replay" ' +
            'style="margin-left:auto">' + esc(t('replay')) + '</button>') +
        '</div>';
    }
    S.card.innerHTML = html;
    if (pwdKeep) {
      var pwdNew = S.card.querySelector('.xza-pwd-row input');
      if (pwdNew) { pwdNew.value = pwdKeep; }
    }
  }
  function onCardClick(ev) {
    var b = ev.target.closest('[data-xza]');
    if (!b) { return; }
    var act2 = b.getAttribute('data-xza');
    if (act2 === 'chip') {
      var g4 = String(b.getAttribute('data-goal') || '');
      if (g4) { beacon('asb_agent_chip'); runGoal(g4); }
      return;
    }
    if (act2 === 'ask-send') {
      var inp2 = S.card.querySelector('.xza-ask input');
      var ans = inp2 ? String(inp2.value || '').trim() : '';
      if (!ans) { if (inp2) { inp2.focus(); } return; }
      beacon('asb_agent_clarify');
      var g0 = String((S.task && S.task.goal0) || (S.task && S.task.goal) ||
                      '');
      runGoal(g0 + (S.lang === 'en' ? ' (clarified: ' : '（补充：') + ans +
              (S.lang === 'en' ? ')' : '）'), true);
      return;
    }
    if (act2 === 'tts') {
      setTts(!ttsOn());
      if (!ttsOn() && S.audio) { try { S.audio.pause(); } catch (e) { /* */ } }
      renderCard();
      return;
    }
    if (act2 === 'stop') {
      S.stopFlag = true;
      beacon('asb_agent_stop');
      if (S.task && S.task.flow && !S.task.finished) {
        flowCancel(true);
        return;
      }
      if (S.confirmResolve) { S.confirmResolve('skip'); }
      return;
    }
    if (act2 === 'close-card') { closeCard(); clearTask(); return; }
    if (act2 === 'cf-apply' && S.confirmResolve) {
      beacon('asb_agent_confirm');
      S.confirmResolve('apply');
      return;
    }
    if (act2 === 'cf-skip' && S.confirmResolve) {
      beacon('asb_agent_skip');
      S.confirmResolve('skip');
      return;
    }
    if (act2 === 'undo') {
      var undoId = b.getAttribute('data-undo');
      var idx = Number(b.getAttribute('data-idx'));
      beacon('asb_agent_undo');
      post('/api/assistant/act/undo', { undo_id: undoId })
        .then(function (j) {
          var s = S.task && S.task.steps[idx];
          if (s) {
            s.undone = true;
            s.note = j.ok ? t('undone')
              : (j.detail || t('net_err'));
            renderCard();
          }
        });
      return;
    }
    if (act2 === 'replay') {
      beacon('asb_agent_replay');
      replayMeteor();
      return;
    }
    if (act2 === 'fl-yes') {
      beacon('asb_flow_start');
      flowAfterConfirm();
      return;
    }
    if (act2 === 'fl-no') { flowCancel(true); return; }
    if (act2 === 'fl-next') {
      var g = String(b.getAttribute('data-goto') || '');
      if (g.charAt(0) === '/') {
        beacon('asb_flow_next');
        location.href = g;
      }
      return;
    }
    if (act2 === 'fl-pwd-send') {
      var inp = S.card.querySelector('.xza-pwd-row input');
      var val = inp ? String(inp.value || '') : '';
      if (!val) { if (inp) { inp.focus(); } return; }
      if (inp) { inp.value = ''; }
      flowSubmitPassword(val);
    }
  }

  /* ── 任务持久化（跨页续跑）。流程任务刻意不持久：交互态含敏感环节，
     重开重扫比恢复更安全（密码值本就只活在输入框里）。 ── */
  function saveTask() {
    if (S.task && S.task.flow) { return; }
    try {
      sessionStorage.setItem('xza_task_v1',
        JSON.stringify({ task: S.task, ts: Date.now() }));
    } catch (e) { /* */ }
  }
  function clearTask() {
    try { sessionStorage.removeItem('xza_task_v1'); } catch (e) { /* */ }
  }
  function tryResume() {
    var raw = null;
    try { raw = sessionStorage.getItem('xza_task_v1'); } catch (e) { return; }
    if (!raw) { return; }
    var ent = null;
    try { ent = JSON.parse(raw); } catch (e) { clearTask(); return; }
    if (!ent || !ent.task || Date.now() - (ent.ts || 0) > 600000) {
      clearTask();
      return;
    }
    S.task = ent.task;
    /* 刚经 goto 到达新页：把当前 nav 步记完成 */
    var idx = S.task.idx || 0;
    var st = S.task.steps[idx];
    if (st && st.kind === 'nav' && st.status === 'cur') {
      st.status = 'ok';
      st.note = t('st_ok');
      S.task.idx = idx + 1;
    }
    beacon('asb_agent_resume');
    S.running = true;
    renderCard();
    setTimeout(runLoop, 600);
  }

  /* ── 流星引擎（融合契约：视觉取样自球本体） ──
     2026-08-23 形象线 0157 落地「活体能量核」后升级：核心渐变在 .asb-orb
     子元素、状态换色走 filter:hue-rotate——取样顺序 .asb-orb → .asb-ball，
     且把 filter 一并带走＝流星带着球此刻的「心情色」起飞。 */
  function ballEl() { return document.querySelector('.asb-ball'); }
  function sampleBallVisual() {
    var src = document.querySelector('.asb-orb') || ballEl();
    if (!src) { return { bg: '', filter: '' }; }
    try {
      var cs = getComputedStyle(src);
      var bg = cs.backgroundImage;
      var flt = cs.filter;
      return { bg: bg && bg !== 'none' ? bg : '',
               filter: flt && flt !== 'none' ? flt : '' };
    } catch (e) { return { bg: '', filter: '' }; }
  }
  function centerOf(el) {
    var r = el.getBoundingClientRect();
    return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
  }
  function ensureHead() {
    if (S.head) { return S.head; }
    var b = ballEl();
    var h = document.createElement('div');
    h.className = 'xza-head';
    var vis = sampleBallVisual();
    if (vis.bg) { h.style.background = vis.bg; }
    if (vis.filter) { h.style.filter = vis.filter; }
    var p = b ? centerOf(b) : { x: window.innerWidth - 40,
                                y: window.innerHeight - 40 };
    h.style.left = (p.x - 10) + 'px';
    h.style.top = (p.y - 10) + 'px';
    document.body.appendChild(h);
    S.head = h;
    if (b && !S.ballWasHidden) {
      try { b.style.visibility = 'hidden'; } catch (e) { /* */ }
      S.ballWasHidden = true;
    }
    return h;
  }
  function meteorRemove() {
    if (S.head) { try { S.head.remove(); } catch (e) { /* */ } }
    S.head = null;
    if (S.ballWasHidden) {
      var b = ballEl();
      if (b) { try { b.style.visibility = ''; } catch (e) { /* */ } }
      S.ballWasHidden = false;
    }
  }
  function flyTo(target, cb) {
    if (reducedMotion() || !ballEl()) { cb(); return; }
    var head = ensureHead();
    var from = centerOf(head);
    var to = target ? centerOf(target) : centerOf(S.card || document.body);
    /* 贝塞尔控制点：中点向上拱 */
    var cx = (from.x + to.x) / 2;
    var cy = Math.min(from.y, to.y) - Math.max(60,
      Math.abs(from.x - to.x) * 0.18);
    var t0 = performance.now();
    var dur = 550;
    var lastTrail = 0;
    function frame(now) {
      var k = Math.min(1, (now - t0) / dur);
      var u = 1 - k;
      var x = u * u * from.x + 2 * u * k * cx + k * k * to.x;
      var y = u * u * from.y + 2 * u * k * cy + k * k * to.y;
      head.style.left = (x - 10) + 'px';
      head.style.top = (y - 10) + 'px';
      if (now - lastTrail > 30) {
        lastTrail = now;
        spawnTrail(x, y);
      }
      if (k < 1) { requestAnimationFrame(frame); return; }
      if (target) {
        target.classList.add('xza-pulse');
        setTimeout(function () {
          try { target.classList.remove('xza-pulse'); } catch (e) { /* */ }
        }, 750);
      }
      cb();
    }
    requestAnimationFrame(frame);
  }
  function spawnTrail(x, y) {
    var d = document.createElement('div');
    d.className = 'xza-trail';
    var vis = S.head && S.head.style.background;
    if (vis) { d.style.background = vis; }
    var flt = S.head && S.head.style.filter;
    if (flt) { d.style.filter = flt; }
    d.style.left = (x - 3.5) + 'px';
    d.style.top = (y - 3.5) + 'px';
    document.body.appendChild(d);
    requestAnimationFrame(function () {
      d.style.opacity = '0';
      d.style.transform = 'scale(.35)';
    });
    setTimeout(function () { try { d.remove(); } catch (e) { /* */ } }, 560);
  }
  function meteorHome(cb) {
    var b = ballEl();
    if (!S.head || !b) { meteorRemove(); if (cb) { cb(); } return; }
    flyTo(b, function () { meteorRemove(); if (cb) { cb(); } });
  }
  function targetFor(step) {
    if (step.kind === 'nav' && step.goto) {
      var a = document.querySelector('a[href="' + step.goto + '"]');
      if (a && a.offsetParent) { return a; }
    }
    return S.card;
  }
  function replayMeteor() {
    var task = S.task;
    if (!task || S.running) { return; }
    var i = 0;
    (function next() {
      if (i >= task.steps.length) { meteorHome(); return; }
      var el = targetFor(task.steps[i]);
      i += 1;
      flyTo(el, next);
    })();
  }

  /* ── 登录流程模式运行器（实施58 P5 旗舰「登陆飞机」；LINE 同族） ──
     动作真身在服务端登录状态机（start/status/password/cancel 四路由 +
     账号落库都在那边），本运行器只做编排+可视化+语音陪跑。密码值只活在
     输入框里：不进 task、不进存储、不进朗读。 */
  var FLOW = null; /* {spec,T,login_id,requery,errs,deadline,lastState,timer} */

  function fstep(name) {
    var map = { confirm: t('fl_step_confirm'),
                preflight: t('fl_step_preflight'), qr: t('fl_step_qr'),
                wait: t('fl_step_wait'), done: t('fl_step_done') };
    var lb = map[name];
    var steps = (S.task && S.task.steps) || [];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].label === lb) { return steps[i]; }
    }
    return null;
  }
  function furl(tpl) {
    return String(tpl || '').replace(
      '{login_id}', encodeURIComponent((FLOW && FLOW.login_id) || ''));
  }
  function startLoginFlow(resp) {
    var spec = resp.flow || {};
    var T = spec.texts || {};
    S.stopFlag = false;
    if (FLOW && FLOW.timer) { clearTimeout(FLOW.timer); }
    FLOW = { spec: spec, T: T, login_id: '', requery: 0, errs: 0,
             deadline: 0, lastState: '', timer: null };
    var steps = [{ label: t('fl_step_confirm'), status: 'cur',
                   kind: 'flow', level: spec.level || 'L3', note: '' }];
    if (spec.preflight_url) {
      steps.push({ label: t('fl_step_preflight'), status: 'pending',
                   kind: 'flow', level: 'L0', note: '' });
    }
    steps.push({ label: t('fl_step_qr'), status: 'pending', kind: 'flow',
                 level: 'L3', note: '' });
    steps.push({ label: t('fl_step_wait'), status: 'pending', kind: 'flow',
                 level: 'L3', note: '' });
    steps.push({ label: t('fl_step_done'), status: 'pending', kind: 'flow',
                 level: 'L3', note: '' });
    S.task = { goal: resp.goal, say: resp.say || T.say || '', steps: steps,
               idx: 0, finished: false, allOk: true, flow: true,
               fvars: { confirm_text: T.confirm || '' }, fneed: 'confirm' };
    S.running = true;
    renderCard();
    speak(S.task.say);
  }
  function flowAfterConfirm() {
    var c = fstep('confirm');
    if (c) { c.status = 'ok'; c.note = ''; }
    S.task.fneed = '';
    renderCard();
    if (FLOW.spec.preflight_url) { flowPreflight(); } else { flowStart(); }
  }
  function flowPreflight() {
    var p = fstep('preflight');
    if (p) { p.status = 'cur'; }
    renderCard();
    fetch(FLOW.spec.preflight_url)
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .catch(function () { return {}; })
      .then(function (j) {
        if (!S.task || !S.task.flow) { return; }
        if (p) {
          p.status = 'ok';
          /* 预检是顾问不是闸门：只有显式 reachable=false 才提示配代理 */
          if (j && j.ok && j.reachable === false) {
            p.note = FLOW.T.preflight_warn || '';
          }
        }
        flowStart();
      });
  }
  function flowStart() {
    var q = fstep('qr');
    if (q) { q.status = 'cur'; }
    renderCard();
    flyTo(S.card, function () {
      post(FLOW.spec.start_url, {}).then(function (j) {
        if (!S.task || !S.task.flow) { return; }
        if (j.__status !== 200 || !j.ok) {
          return flowFail(String(j.detail || t('fl_fail')));
        }
        FLOW.login_id = String(j.login_id || '');
        S.task.fvars.qr_img = String(j.qr_image || '');
        S.task.fvars.qr_hint = FLOW.T.qr_hint || '';
        S.task.fvars.pin = String(j.pin || '');
        if (q) { q.status = 'ok'; }
        var w = fstep('wait');
        if (w) { w.status = 'cur'; w.note = t('fl_waiting'); }
        renderCard();
        speak(FLOW.T.qr_hint);
        FLOW.deadline = Date.now() + (Number(FLOW.spec.timeout_ms) || 180000);
        flowSchedule();
      });
    });
  }
  function flowSchedule() {
    if (FLOW.timer) { clearTimeout(FLOW.timer); }
    FLOW.timer = setTimeout(flowPoll,
                            Number(FLOW.spec.poll_ms) || 2000);
  }
  function flowPoll() {
    if (!S.task || !S.task.flow || S.task.finished) { return; }
    if (S.stopFlag) { return flowCancel(false); }
    if (Date.now() > FLOW.deadline) {
      post(furl(FLOW.spec.cancel_url), {});
      return flowFail(t('fl_timeout'));
    }
    fetch(furl(FLOW.spec.status_url))
      .then(function (r) { return r.json().catch(function () { return {}; }); })
      .then(function (j) {
        if (!S.task || !S.task.flow || S.task.finished) { return; }
        FLOW.errs = 0;
        var st = String(j.status || '');
        var fv = S.task.fvars;
        var dirty = false;
        if (j.qr_image && j.qr_image !== fv.qr_img) {
          fv.qr_img = String(j.qr_image);   /* DC 迁移/换码：新码即时上屏 */
          dirty = true;
        }
        if (j.pin && j.pin !== fv.pin) { fv.pin = String(j.pin); dirty = true; }
        var w = fstep('wait');
        if (st === 'authorized') { return flowDone(); }
        if (st === 'failed') {
          return flowFail(String(j.detail || j.reason_code || t('fl_fail')));
        }
        if (st === 'expired') {
          if (FLOW.requery < (Number(FLOW.spec.requery_max) || 3)) {
            FLOW.requery += 1;
            fv.pin = '';
            if (w) { w.note = FLOW.T.requery || ''; }
            renderCard();
            speak(FLOW.T.requery);
            return flowStart();
          }
          return flowFail(t('fl_fail'));
        }
        if (st === 'scanned' && FLOW.lastState !== 'scanned') {
          if (w) { w.note = FLOW.T.scanned || ''; }
          dirty = true;
          speak(FLOW.T.scanned);
        }
        if (st === 'password_needed' && S.task.fneed !== 'pwd') {
          S.task.fneed = 'pwd';
          if (w) { w.note = FLOW.T.pwd || ''; }
          dirty = true;
          speak(FLOW.T.pwd);
        }
        if (st && st !== 'password_needed' && S.task.fneed === 'pwd') {
          S.task.fneed = '';
          dirty = true;
        }
        FLOW.lastState = st;
        if (dirty) { renderCard(); }
        flowSchedule();
      })
      .catch(function () {
        if (!S.task || !S.task.flow || S.task.finished) { return; }
        FLOW.errs += 1;
        if (FLOW.errs > 10) { return flowFail(t('net_err')); }
        flowSchedule();
      });
  }
  function flowSubmitPassword(val) {
    post(furl(FLOW.spec.password_url), { password: val }).then(function (j) {
      if (!S.task || !S.task.flow || S.task.finished) { return; }
      if (j.ok && j.status === 'authorized') { return flowDone(); }
      var w = fstep('wait');
      if (w) { w.note = String(j.detail || t('fl_pwd_bad')); }
      renderCard();
    });
  }
  function flowDone() {
    if (FLOW && FLOW.timer) { clearTimeout(FLOW.timer); }
    S.task.fneed = '';
    var w = fstep('wait');
    if (w) { w.status = 'ok'; w.note = ''; }
    var d = fstep('done');
    if (d) { d.status = 'ok'; d.note = FLOW.T.ok || ''; }
    S.task.finished = true;
    S.task.allOk = true;
    S.running = false;
    beacon('asb_flow_done');
    speak(FLOW.T.ok);
    vibrateDone();
    meteorHome(function () { renderCard(); });
  }
  function flowFail(msg) {
    if (FLOW && FLOW.timer) { clearTimeout(FLOW.timer); }
    if (FLOW && FLOW.login_id) { post(furl(FLOW.spec.cancel_url), {}); }
    S.task.fneed = '';
    var steps = S.task.steps || [];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].status === 'cur' || steps[i].status === 'wait') {
        steps[i].status = 'fail';
        steps[i].note = msg;
        msg = '';
      }
    }
    S.task.finished = true;
    S.task.allOk = false;
    S.running = false;
    beacon('asb_flow_fail');
    meteorHome(function () { renderCard(); });
  }
  function flowCancel(userClick) {
    if (FLOW && FLOW.timer) { clearTimeout(FLOW.timer); }
    beacon('asb_flow_cancel');
    if (FLOW && FLOW.login_id) { post(furl(FLOW.spec.cancel_url), {}); }
    S.task.fneed = '';
    var steps = S.task.steps || [];
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].status === 'cur' || steps[i].status === 'pending') {
        steps[i].status = 'skip';
      }
    }
    S.task.say = t('fl_canceled');
    S.task.finished = true;
    S.task.allOk = false;
    S.running = false;
    meteorHome(function () { renderCard(); });
  }

  /* ── 执行循环 ── */
  function runGoal(goal, clarified) {
    beacon('asb_agent_plan');
    S.stopFlag = false;
    S.task = { goal: goal, goal0: goal, say: t('planning'), steps: [], idx: 0,
               finished: false, allOk: true };
    S.running = true;
    renderCard();
    post('/api/assistant/agent/plan', {
      goal: goal,
      lang: S.lang,
      page: (location && location.pathname) || '',
    }).then(function (j) {
      if (j.__status !== 200 || !j.ok) {
        S.task.say = String(j.detail || t('net_err'));
        S.task.finished = true;
        S.task.allOk = false;
        S.running = false;
        renderCard();
        return;
      }
      if (j.flow) { startLoginFlow(j); return; }
      if (!j.plan_ok || !(j.steps || []).length) {
        S.task.finished = true;
        S.task.allOk = false;
        S.running = false;
        /* clarify 优先（P1，仅一轮防拉扯）：规划器给了追问就就地补答 */
        if (j.ask && !clarified) {
          S.task.say = String(j.say || '');
          S.task.ask = String(j.ask);
          renderCard();
          speak(S.task.ask);
          var inp3 = S.card && S.card.querySelector('.xza-ask input');
          if (inp3) { setTimeout(function () { inp3.focus(); }, 80); }
          return;
        }
        S.task.say = t('plan_empty') + String(j.say || '') + ' ' +
          t('plan_empty2');
        renderCard();
        /* 死路变菜单（2026-08-23）：列出真能做的事，点了即重新规划 */
        abilityLabels(function (labels) {
          if (!S.task || !S.task.finished || !labels.length) { return; }
          S.task.chips = labels;
          renderCard();
        });
        return;
      }
      S.task.say = String(j.say || '');
      S.task.steps = (j.steps || []).map(function (s) {
        s.status = 'pending';
        s.note = '';
        return s;
      });
      renderCard();
      saveTask();
      speak(S.task.say);
      runLoop();
    }).catch(function () {
      S.task.say = t('net_err');
      S.task.finished = true;
      S.running = false;
      renderCard();
    });
  }

  function runLoop() {
    var task = S.task;
    if (!task) { return; }
    if (S.stopFlag) { return finishTask(t('stopped')); }
    var idx = task.idx || 0;
    if (idx >= task.steps.length) { return finishTask(''); }
    var step = task.steps[idx];
    step.status = 'cur';
    step.note = t('st_run');
    renderCard();
    saveTask();
    flyTo(targetFor(step), function () { execStep(step); });
  }

  function execStep(step) {
    var task = S.task;
    if (!task) { return; }
    if (S.stopFlag) { return finishTask(t('stopped')); }

    if (step.kind === 'nav') {
      if (S.standalone) {
        /* 手机端不替桌面翻页：动作真身在服务端 API，导航步如实跳过 */
        return stepDone(step, 'ok', t('nav_on_pc'));
      }
      step.note = t('st_nav') + ' ' + String(step.goto || '');
      renderCard();
      saveTask(); /* status=cur 落盘：新页 tryResume 判到达并续跑 */
      setTimeout(function () { location.href = step.goto; }, 350);
      return;
    }

    post('/api/assistant/act',
         { action: step.action, params: step.params, lang: S.lang })
      .then(function (j) {
        if (j.__status !== 200 || !j.ok) {
          return stepDone(step, 'fail', String(j.detail || t('net_err')));
        }
        if (step.kind === 'query') {
          var note = t('st_ok');
          try {
            var res = j.result || {};
            var sum = res.summary || {};
            var verdict = sum.verdict || res.verdict || '';
            var warn = 0;
            var fs = res.factors || [];
            for (var i = 0; i < fs.length; i++) {
              var stt = String(fs[i].status || '');
              if (stt && stt !== 'ok' && stt !== 'pass') { warn += 1; }
            }
            if (verdict) {
              note = t('diag_line') + verdict +
                (warn ? t('diag_warn') + warn + t('diag_warn2') : '');
            }
          } catch (e) { /* keep default */ }
          return stepDone(step, 'ok', note);
        }
        if (j.need_confirm) {
          step.status = 'wait';
          step.note = t('st_wait');
          step.confirm = { token: j.token, diff: j.diff || [] };
          renderCard();
          speak(t('say_confirm'));
          return waitConfirm().then(function (choice) {
            step.confirm_done = true;
            if (choice !== 'apply') {
              return stepDone(step, 'skip', t('st_skip'));
            }
            return post('/api/assistant/act',
                        { confirm_token: step.confirm.token })
              .then(function (j2) {
                if (j2.__status !== 200 || !j2.ok) {
                  return stepDone(step, 'fail',
                                  String(j2.detail || t('net_err')));
                }
                step.undo_id = j2.undo_id || '';
                /* 生效口径只认服务端 hot_applied（诚实：没热更就说重启窗后生效） */
                var hotNote = j2.hot_applied ? t('hot_worker')
                  : t('hot_restart');
                return stepDone(step, 'ok', hotNote);
              });
          });
        }
        return stepDone(step, 'ok', t('st_ok'));
      }).catch(function () {
        stepDone(step, 'fail', t('net_err'));
      });
  }

  function waitConfirm() {
    return new Promise(function (res) {
      S.confirmResolve = function (choice) {
        S.confirmResolve = null;
        res(choice);
      };
    });
  }

  function stepDone(step, status, note) {
    step.status = status;
    step.note = note;
    step.confirm = null;
    if (status === 'fail') { S.task.allOk = false; }
    if (status === 'skip') { S.task.allOk = false; }
    beacon('asb_agent_step_' + status);
    S.task.idx = (S.task.idx || 0) + 1;
    renderCard();
    saveTask();
    setTimeout(runLoop, 250);
  }

  function vibrateDone() {
    /* 手机端完成触觉反馈（2026-08-23 体验包）；桌面/不支持=静默 */
    if (!S.standalone || !navigator.vibrate) { return; }
    try { navigator.vibrate([35, 60, 35]); } catch (e) { /* */ }
  }

  function finishTask(sayOverride) {
    var task = S.task;
    if (!task) { return; }
    task.finished = true;
    if (sayOverride) { task.say = sayOverride; }
    S.running = false;
    clearTask();
    beacon('asb_agent_done');
    speak(sayOverride || t(task.allOk ? 'done_line' : 'done_part'));
    vibrateDone();
    meteorHome(function () { renderCard(); });
  }

  /* ── 手机页装饰（2026-08-23 体验包）：连接状态灯 + 心跳 + A2HS 提示
     只出现一次。全部 JS 注入＝热更新即达，不等服务端页面重启。 ── */
  function saDecorate() {
    try {
      var a2 = document.querySelector('.xzm-a2hs');
      if (a2) {
        if (localStorage.getItem('xz_seen')) { a2.style.display = 'none'; }
        else { localStorage.setItem('xz_seen', '1'); }
      }
    } catch (e) { /* */ }
    var hd = document.querySelector('.xzm-hd') || document.body;
    var dot = document.createElement('span');
    dot.className = 'xza-sa-dot';
    dot.innerHTML = '<i></i><em>' + esc(t('sa_online')) + '</em>';
    hd.appendChild(dot);
    function beat() {
      fetch('/api/assistant/bootstrap', { cache: 'no-store' })
        .then(function (r) {
          if (r.status === 401) {
            /* 会话被踢/过期：/xz 会渲染图文排查卡（服务端 401 页） */
            location.replace('/xz');
            return;
          }
          dot.classList.toggle('off', !r.ok);
          var em = dot.querySelector('em');
          if (em) { em.textContent = t(r.ok ? 'sa_online' : 'sa_off'); }
        })
        .catch(function () {
          dot.classList.add('off');
          var em = dot.querySelector('em');
          if (em) { em.textContent = t('sa_off'); }
        });
    }
    beat();
    setInterval(beat, 45000);
  }

  /* ── init ── */
  function init(opts) {
    opts = opts || {};
    S.shell = opts.shell === 'workspace' ? 'workspace' : 'admin';
    S.lang = String(opts.lang || '').toLowerCase().indexOf('en') === 0
      ? 'en' : 'zh';
    S.standalone = !!opts.standalone;
    injectCss();
    if (S.standalone) {
      /* 手机操控页：无球无面板行，直接常驻输入面板（xza-sa 布局接管） */
      try { document.body.classList.add('xza-sa'); } catch (e) { /* */ }
      saDecorate();
      openPanel();
      setTimeout(tryResume, 500);
      return;
    }
    var tries = 0;
    (function poll() {
      if (window.AssistantBall &&
          typeof window.AssistantBall.registerMode === 'function') {
        S.claimed = window.AssistantBall.registerMode('agent', {
          order: 30, icon: '⚡', labelKey: 'mode_agent', mount: mountMode,
          composer: {
            ph: function () { return t('p_ph'); },
            submit: function (goal) {
              beacon('asb_agent_open');
              /* 收面板再跑：流星执行秀在页面上演，面板挡着就看不见了 */
              if (S.api && typeof S.api.close === 'function') {
                try { S.api.close(); } catch (e) { /* */ }
              }
              runGoal(goal);
            },
          },
        });
      }
      if (!S.claimed && ++tries < 20) { setTimeout(poll, 500); }
    })();
    setTimeout(tryResume, 800);
  }

  window.XZAgent = { init: init, run: runGoal,
    /* 球的标题栏手机键调这里（配对链仍全部归本模块） */
    pair: openPairModal, _ver: VER };
})();
