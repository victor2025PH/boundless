/* AI 助手悬浮球「小智」（2026-08-20 P0）。
 *
 * 自包含单文件：样式注入 + 中英词典内置（cp-i18n 模式，模板零键）+
 * 零内联 on* handler（全部 addEventListener + data 属性事件委托——
 * 哑按钮/孤儿引用/动态点属性三门禁天然免疫）。
 *
 * 挂载协议（两壳模板各一段内联 loader，先探 /api/assistant/bootstrap，
 * 404/未启用即静默——后端未重启/灰度未开时全站零痕迹）：
 *   AssistantBall.init({ shell:'admin'|'workspace', lang:'zh'|'en', boot:{...} })
 *
 * 主题：内部只用 --xz-* 局部变量，init 按壳映射到各自 token 体系
 * （admin: --card/--bd/--t/--p；workspace: --tk-*），暗亮四态零成对覆盖。
 *
 * Orb 状态灯（2026-08-23 实施59「活体能量核」）：球=状态灯，状态机
 * idle/hint/listening/thinking/alert + success 爆闪；三档画质与运行期
 * 调速详见文内「Orb 引擎」段注释——改球体视觉/动效前先读那段。
 *
 * 线协议（与 assistant_routes.py 钉死）：
 *   POST /api/assistant/query  → ndjson: {ev:meta,sources,report_hint} →
 *                                 {ev:delta,text} → {ev:done,qa_id,answered}
 *                                 | {ev:err,key,text}
 *   POST /api/assistant/report → {ok,ticket_id,dup}
 *   GET  /api/assistant/tickets → {ok,tickets[]}
 *   POST /api/assistant/feedback → {ok}
 */
(function () {
  'use strict';
  if (window.AssistantBall) { return; }

  var VER = '20260829c';
  var I18N = {
    zh: {
      name: '小智 · AI 助手', open_aria: '打开 AI 助手', close: '关闭',
      tab_chat: '对话', tab_report: '报障', tab_mine: '我的',
      /* 模式条（实施73 P1-1）：三大功能升格为一级导航 */
      modes_aria: '小智模式', mode_chat: '问答', mode_teach: '教学',
      mode_agent: '替我做',
      /* 次级页签行（P1-5）：常问排在「我的」之后＝老板拍板的顺序 */
      tab_faq: '常问', tab_set: '偏好', set_t: '偏好与工具',
      /* 手机操控进标题栏（P1-2）：图标 + 连接状态点，不占模式条第四格 */
      pair_t: '手机操控', pair_on: '手机已连', pair_off: '未连手机',
      /* 电脑操控进标题栏（实施91 P1-1）：同手机走通道式入口，enabled 才显 */
      pc_t: '电脑操控', pc_on: '电脑在线', pc_off: '电脑未就绪',
      faq_ph: '搜索帮助库…', faq_page: '本页常问', faq_global: '全站常问',
      faq_kb: '帮助库条目', faq_seed: '新手先问这些',
      faq_empty: '还没有人问过。先在「问答」里问一句，这里会长出来。',
      faq_none: '没找到相关条目——直接问小智，答不上会记下来补语料',
      faq_na: '「常问」的后端待装载（下次重启后自动可用）',
      chips_title: '本页常问', chips_more: '更多 →',
      input_ph: '输入问题，回车发送…', send: '发送',
      thinking: '思考中…', searching: '检索帮助库…',
      src_title: '来源', goto: '带我去', helpful: '有帮助吗',
      fb_thanks: '已记录，谢谢反馈', retry: '重试',
      stop_t: '停止生成', stopped_gen: '已停止',
      copy_t: '复制回答',
      to_report: '🐞 这像是故障？一键转报障',
      rp_ph: '一句话描述遇到的问题（必填，≥5 字）…',
      rp_shot_hint: '截图：在此面板直接 Ctrl+V 粘贴，或',
      rp_shot_pick: '选择图片', rp_shot_del: '移除截图',
      rp_env_title: '将随工单自动提交的信息',
      rp_env_note: '当前页面路径、界面版本、浏览器标识、近期前端错误指纹。' +
        '不包含聊天内容与任何密钥。',
      rp_submit: '提交报障', rp_submitting: '提交中…',
      rp_ok: '已提交，工单号 #', rp_dup: '（已合并到你近期的同类工单）',
      rp_view: '在「我的」查看进度', rp_fail: '提交失败，请稍后重试',
      /* 预期管理（2026-08-27）：原成功提示只给工单号就没了——用户不知道
         「接下来谁会看、要等多久、我该做什么」，于是要么反复提交要么放弃。 */
      rp_next: '客服群已收到通知，我们会尽快跟进；修复后这条工单会变成'
        + '「已修复」，你也会在这里看到开发者留言。',
      mic_title: '语音提问（点击开始，再点结束）',
      mic_rec: '录音中…再点一次结束', mic_busy: '转写中…',
      mic_denied: '麦克风不可用：浏览器未授权或非安全上下文（桌面版可用）',
      mic_fail: '语音转写失败，请改用文字输入',
      nudge_text: '页面好像出了点问题，需要我帮你报障吗？',
      nudge_go: '帮我报障', nudge_prefill: '页面脚本错误：',
      shot_page: '📷 截当前页', shot_busy: '截取中…',
      an_title: '圈出问题 · 涂掉敏感信息', an_rect: '🟥 圈选',
      an_mosaic: '▦ 马赛克', an_undo: '↩ 撤销', an_ok: '✔ 用这张',
      an_cancel: '✕ 取消',
      mine_empty: '还没有提交过报障',
      mine_refresh: '刷新', mine_fixed_note: '开发者留言',
      /* 状态键与后端 bug_intake.VALID_STATUSES 一一对应（门禁钉住）。
         2026-08-27 修：原有 collecting/wontfix 后端根本不存在，而真实存在的
         in_progress/closed 没有映射 → 用户看到的是英文原文「in_progress」。 */
      st_new: '待处理', st_confirmed: '已确认', st_in_progress: '处理中',
      st_fixed: '已修复', st_verified: '已验证', st_closed: '已关闭',
      tools_tips_on: '术语提示：开', tools_tips_off: '术语提示：关',
      tools_tour: '重看引导', tools_keys: '快捷键', tools_cmd: '命令面板',
      tools_support: '上传诊断',
      orb_fx_full: '动效：饱满', orb_fx_calm: '动效：安静',
      say_t: '播报这条回答（系统音色）', say_fail: '播报失败，请稍后重试',
      /* 首屏第一句（P1-1）：一句话讲清三个模式各能干什么，让用户不必先给
         自己分类。**此前这个键根本不存在** → t() 缺键回落裸键名，面板首屏
         五天来一直显示 "hello"（2026-08-28 修，门禁
         tests/test_assistant_i18n_keys.py 防复发）。 */
      hello: '我能帮你三件事：**查用法**、**带你操作**、**替你动手**。' +
        '直接把想问的、想做的说出来就行。',
      err_net: '网络异常，请重试', rate_hint: '操作太频繁，稍后再试',
      /* 错误文案分级（P0-4）：收到过 meta＝请求已抵达服务端并开始处理，
         此时说「网络异常」是在冤枉用户的网络（他会去重启路由器），必须
         如实说是我们没出话。 */
      err_nostream: '小智没能把回答送出来（服务端中断）。可以重试，或直接报障。',
      err_report: '🐞 报障',
      thinking_long: '正在问 AI（通常 5-30 秒）',
      /* 拒答不该是死路（2026-08-29 老板实录「回复的内容没一点帮助」）：
         答不上来时给几条**确定答得上**的问法，比只留一句「不知道」有用。 */
      suggest_t: '这些我答得上，试试：',
      suggest_near_t: '你是不是想问：',
      /* 依据标注（2026-08-29）：**答什么**与**凭什么答**必须分开说。
         没有 basis=general 这条标注，模型的通用回答会被当成产品承诺。 */
      basis_product: '依据产品说明',
      basis_general: '通用回答 · 非产品文档',
      resize_aria: '调整面板大小（方向键，Shift 加速，Esc 复位）',
      move_hint: '拖动移动 · 双击复位',
      reset_geo: '恢复默认大小和位置',
    },
    en: {
      name: 'AI Assistant', open_aria: 'Open AI assistant', close: 'Close',
      tab_chat: 'Chat', tab_report: 'Report', tab_mine: 'Mine',
      modes_aria: 'Assistant modes', mode_chat: 'Ask', mode_teach: 'Learn',
      mode_agent: 'Do it',
      tab_faq: 'FAQ', tab_set: 'Prefs', set_t: 'Preferences & tools',
      pair_t: 'Phone control', pair_on: 'Phone connected',
      pair_off: 'No phone connected',
      pc_t: 'PC control', pc_on: 'PC online', pc_off: 'PC not ready',
      faq_ph: 'Search help…', faq_page: 'Popular on this page',
      faq_global: 'Popular everywhere',
      faq_kb: 'Help entries', faq_seed: 'Good first questions',
      faq_empty: 'Nothing asked yet. Ask something in Chat and it shows up here.',
      faq_none: 'No entry matched — ask directly; misses are logged for the corpus',
      faq_na: 'The FAQ backend is not loaded yet (available after next restart)',
      chips_title: 'Popular here', chips_more: 'More →',
      input_ph: 'Type a question…',
      send: 'Send', thinking: 'Thinking…', searching: 'Searching help…',
      src_title: 'Sources', goto: 'Take me there', helpful: 'Helpful?',
      fb_thanks: 'Recorded, thanks', retry: 'Retry',
      stop_t: 'Stop generating', stopped_gen: 'Stopped',
      copy_t: 'Copy answer',
      to_report: '🐞 Looks like a bug? File a report',
      rp_ph: 'Describe the issue in one sentence (min 5 chars)…',
      rp_shot_hint: 'Screenshot: paste (Ctrl+V) into this panel, or',
      rp_shot_pick: 'choose image', rp_shot_del: 'Remove screenshot',
      rp_env_title: 'Info attached automatically',
      rp_env_note: 'Current page path, UI build, browser UA, recent frontend ' +
        'error fingerprints. Never chat content or secrets.',
      rp_submit: 'Submit report', rp_submitting: 'Submitting…',
      rp_ok: 'Submitted, ticket #', rp_dup: '(merged into your recent ticket)',
      rp_view: 'Track it in the Mine tab', rp_fail: 'Failed, please retry',
      rp_next: 'Our support channel has been notified and will follow up. '
        + 'Once fixed, this ticket turns to "Fixed" and any developer note '
        + 'shows up right here.',
      mic_title: 'Voice input (click to start/stop)',
      mic_rec: 'Recording… click again to stop', mic_busy: 'Transcribing…',
      mic_denied: 'Microphone unavailable: not granted or insecure context',
      mic_fail: 'Transcription failed, please type instead',
      nudge_text: 'This page seems to have a problem — want me to file it?',
      nudge_go: 'File a report', nudge_prefill: 'Page script error: ',
      shot_page: '📷 Capture this page', shot_busy: 'Capturing…',
      an_title: 'Box the issue · mosaic sensitive info', an_rect: '🟥 Box',
      an_mosaic: '▦ Mosaic', an_undo: '↩ Undo', an_ok: '✔ Use it',
      an_cancel: '✕ Cancel',
      mine_empty: 'No reports yet',
      mine_refresh: 'Refresh', mine_fixed_note: 'Developer note',
      st_new: 'Open', st_confirmed: 'Confirmed', st_in_progress: 'In progress',
      st_fixed: 'Fixed', st_verified: 'Verified', st_closed: 'Closed',
      tools_tips_on: 'Term tips: on', tools_tips_off: 'Term tips: off',
      tools_tour: 'Tour', tools_keys: 'Shortcuts', tools_cmd: 'Cmd palette',
      tools_support: 'Upload diagnostics',
      orb_fx_full: 'Motion: rich', orb_fx_calm: 'Motion: calm',
      say_t: 'Read this answer aloud (system voice)',
      say_fail: 'Read-aloud failed, please retry',
      hello: 'Three things I can do: **answer how-to**, **walk you through**, '
        + '**do it for you**. Just say what you need.',
      err_net: 'Network error, please retry',
      rate_hint: 'Too fast, try again later',
      err_nostream: 'I could not deliver an answer (server cut the stream). '
        + 'Retry, or file a report.',
      err_report: '🐞 Report',
      thinking_long: 'Asking the AI (usually 5-30s)',
      suggest_t: 'These I can answer — try one:',
      suggest_near_t: 'Did you mean:',
      basis_product: 'From the product description',
      basis_general: 'General knowledge · not product docs',
      resize_aria: 'Resize panel (arrow keys, Shift to speed up, Esc to reset)',
      move_hint: 'Drag to move · double-click to reset',
      reset_geo: 'Restore default size and position',
    },
  };

  var S = {
    shell: 'admin', lang: 'zh', boot: null, open: false, busy: false,
    tab: 'chat', shotB64: '', lastQ: '', seen: {}, dot: false,
    pairN: 0, pairBusy: false,
  };
  var $wrap = null, $ball = null, $panel = null;

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
  function csrfHeaders(extra) {
    var h = extra || {};
    var m = document.cookie.match(/(?:^|;\s*)csrf_token=([^;]*)/);
    if (m) { h['X-CSRF-Token'] = m[1]; }
    return h;
  }
  function pagePath() {
    try { return location.pathname.slice(0, 120); } catch (e) { return ''; }
  }
  function uiBuild() {
    return String(window.__uiBuild || '').slice(0, 40);
  }

  /* ── 极简安全 markdown：转义先行，仅 **粗体** / `code` / 换行 / [S1] 标 ── */
  function mdLite(s) {
    var x = esc(s);
    x = x.replace(/\*\*([^*\n]{1,80})\*\*/g, '<b>$1</b>');
    x = x.replace(/`([^`\n]{1,80})`/g, '<code>$1</code>');
    x = x.replace(/\[S(\d)\]/g,
      '<span class="asb-sref" data-sref="$1">[S$1]</span>');
    return x.replace(/\n/g, '<br>');
  }

  /* ────────────────────────────────────────────── 样式（--xz-* 局部令牌） */
  function injectCss() {
    if (document.getElementById('asb-style')) { return; }
    var css = '' +
/* ── 主题桥（P2 2026-08-29）────────────────────────────────────────────────
   `--xz-*` 全站**从来没有被定义过**：三个 assistant-*.js 里到处写
   `var(--xz-accent,#4f6ef7)`，但没有任何一处定义它，所以生效的永远是回落值。
   后果不是「代码不整洁」，是**小智根本没接进主题系统**——白标客户换了品牌色、
   或壳切到暗色，整个面板还是那身长春花紫，与它嵌在里面的产品对不上。
   这里按宿主逐级桥接：admin 壳有 --p/--card/--t，工作台壳有 --tk-*，
   两者都没有才落到品牌 SSOT --bl-growth，最后才是原来的字面色。
   挂 :root 而不是挂 .asb-* ——教学模式的 .xzt-banner/.xzt-bubble、代理模式的
   卡片都是 position:fixed **直接挂 body** 的，套在小智容器里就漏掉它们（首版
   实测只桥好了一半）。`--xz-` 前缀本身就是隔离：全站只有 assistant-*.js 认它，
   宿主不会因为多了几个没人读的变量而变样。 */
':root{' +
'--xz-accent:var(--p,var(--tk-brand,var(--bl-growth-600,#4f6ef7)));' +
'--xz-bg:var(--card,var(--tk-surface,#fff));' +
'--xz-txt:var(--t,var(--tk-text,#111));' +
'--xz-muted:var(--t3,var(--tk-text-muted,#888));' +
'--xz-bd:var(--bd,var(--tk-border,#ddd));' +
'--xz-hover:var(--sb-hover,rgba(17,24,39,.05))}' +
'.asb-wrap{position:fixed;z-index:9998;font-family:inherit}' +
'.asb-ball{width:46px;height:46px;border-radius:50%;border:none;cursor:pointer;' +
'display:flex;align-items:center;justify-content:center;background:transparent;color:#fff;' +
'transition:transform .18s;position:relative;padding:0;z-index:2}' +
'.asb-ball:hover{transform:scale(1.07)}' +
'.asb-ball svg{width:24px;height:24px;pointer-events:none}' +
/* 活体能量核：径向底盘(品牌色随 --xz-accent)+两层旋转极光(伪元素,纯合成器动画)。
   状态换色统一走 filter:hue-rotate（可过渡动画、不换渐变；白标 accent 自动跟随）。 */
'.asb-orb{position:absolute;inset:0;border-radius:50%;overflow:hidden;' +
'background:radial-gradient(circle at 32% 28%,#93a7ff 0%,var(--xz-accent,#4f6ef7) 46%,#181a3d 100%);' +
'box-shadow:0 4px 16px rgba(0,0,0,.26),inset 0 0 8px rgba(255,255,255,.14);' +
'transition:filter .5s ease,box-shadow .4s ease,transform .18s ease-out}' +
'.asb-orb::before{content:"";position:absolute;inset:-38%;border-radius:50%;' +
'background:conic-gradient(from 10deg,rgba(139,92,246,0) 0deg,rgba(139,92,246,.9) 78deg,' +
'rgba(34,211,238,.8) 150deg,rgba(139,92,246,0) 222deg,rgba(236,72,153,.6) 300deg,' +
'rgba(139,92,246,0) 360deg);filter:blur(7px);animation:asbSpin 9s linear infinite}' +
'.asb-orb::after{content:"";position:absolute;inset:-22%;border-radius:50%;' +
'background:conic-gradient(from 200deg,rgba(255,255,255,0) 0deg,rgba(255,255,255,.55) 42deg,' +
'rgba(255,255,255,0) 92deg);filter:blur(5px);animation:asbSpin 5.5s linear infinite reverse}' +
'@keyframes asbSpin{to{transform:rotate(360deg)}}' +
'.asb-glow{position:absolute;inset:-7px;border-radius:50%;pointer-events:none;' +
'background:radial-gradient(circle,rgba(99,102,241,.42) 0%,rgba(99,102,241,0) 70%);' +
'filter:blur(4px);animation:asbBreath 7s ease-in-out infinite}' +
'@keyframes asbBreath{0%,100%{transform:scale(1);opacity:.55}50%{transform:scale(1.05);opacity:.95}}' +
'.asb-ic{position:relative;z-index:3;display:flex;align-items:center;justify-content:center;' +
'text-shadow:0 1px 4px rgba(0,0,0,.35)}' +
'.asb-ping{position:absolute;inset:-3px;border-radius:50%;border:2px solid var(--xz-accent,#4f6ef7);' +
'opacity:0;pointer-events:none}' +
'.asb-ball.st-hint .asb-ping{animation:asbPing 30s cubic-bezier(0,0,.2,1) 3}' +
'@keyframes asbPing{0%{transform:scale(.85);opacity:.75}7%{transform:scale(1.9);opacity:0}100%{opacity:0}}' +
'.asb-alertfx{position:absolute;inset:0;border-radius:50%;pointer-events:none;opacity:0;' +
'background:radial-gradient(circle,rgba(251,191,36,.28) 20%,rgba(245,158,11,.62) 100%)}' +
'.asb-ball.st-alert .asb-alertfx{animation:asbAmber 2.1s ease-in-out infinite}' +
'@keyframes asbAmber{0%,100%{opacity:0}18%,42%{opacity:1}30%{opacity:.35}}' +
'.asb-ball.st-alert .asb-orb{filter:hue-rotate(96deg) saturate(1.1)}' +
'.asb-ball.st-listening .asb-orb{filter:hue-rotate(-54deg) saturate(1.3) brightness(1.07);' +
'box-shadow:0 4px 20px rgba(34,211,238,.4)}' +
'.asb-ball.st-listening .asb-orb::before{animation-duration:3.6s}' +
'.asb-ball.st-thinking .asb-orb{filter:hue-rotate(26deg) saturate(1.25)}' +
'.asb-ball.st-thinking .asb-orb::before{animation-duration:1.7s}' +
'.asb-ball.st-thinking .asb-glow{animation:asbBreath 1.7s ease-in-out infinite}' +
'.asb-ball.st-speaking .asb-orb{filter:hue-rotate(-18deg) saturate(1.25) brightness(1.06)}' +
'.asb-ball.st-speaking .asb-glow{animation:asbBreath 2.2s ease-in-out infinite}' +
'.asb-ball.st-agent .asb-orb{filter:hue-rotate(-32deg) saturate(1.2)}' +
'.asb-sweep{position:absolute;inset:-5px;border-radius:50%;pointer-events:none;opacity:0;' +
'background:conic-gradient(from 0deg,rgba(34,211,238,0) 0deg,rgba(34,211,238,.55) 40deg,' +
'rgba(34,211,238,0) 80deg);filter:blur(1px)}' +
'.asb-ball.st-teach .asb-sweep{opacity:1;animation:asbSpin 3s linear infinite}' +
'.asb-ball.st-teach .asb-orb{filter:hue-rotate(-60deg) saturate(1.15)}' +
'.asb-ball.okflash .asb-orb{filter:hue-rotate(-120deg) saturate(1.5) brightness(1.12)}' +
'@keyframes asbPulse{0%,100%{box-shadow:0 4px 16px rgba(0,0,0,.22)}' +
'50%{box-shadow:0 4px 26px rgba(99,102,241,.55)}}' +
'.asb-fx{position:absolute;left:50%;top:50%;width:168px;height:168px;' +
'margin:-84px 0 0 -84px;pointer-events:none;z-index:1;display:none}' +
/* 画质档/减动效：lv0=静态+透明度脉冲；系统 reduced-motion 同语义（旋转必杀，
   opacity 脉冲保留=Apple 同款状态灯降级） */
'.asb-lv0 .asb-orb::before,.asb-lv0 .asb-orb::after,.asb-lv0 .asb-glow,' +
'.asb-lv0 .asb-sweep{animation:none}' +
'.asb-lv0 .asb-ball.st-hint .asb-ping{animation:none}' +
'.asb-lv0 .asb-ball.st-alert .asb-alertfx{animation:asbFade 2.2s ease-in-out infinite}' +
'.asb-lv0 .asb-ball.st-thinking .asb-orb{animation:asbFade 1.8s ease-in-out infinite}' +
'@keyframes asbFade{0%,100%{opacity:1}50%{opacity:.62}}' +
'@media (prefers-reduced-motion:reduce){' +
'.asb-orb::before,.asb-orb::after,.asb-glow,.asb-ping,.asb-sweep{animation:none!important}' +
'.asb-ball.st-thinking .asb-orb{animation:asbFade 1.8s ease-in-out infinite}' +
'}' +
'.asb-hero{display:none;padding:.25rem .8rem 0;flex-shrink:0}' +
'.asb-hero.on{display:block}' +
'.asb-hero canvas{width:100%;height:38px;display:block}' +
'.asb-fbq{display:flex;gap:.35rem;align-items:center}' +
'.asb-fb .asb-say.on{background:var(--xz-accent,#4f6ef7);color:#fff;' +
'border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-dot{position:absolute;top:2px;right:2px;width:10px;height:10px;border-radius:50%;' +
'background:#ef4444;border:2px solid #fff;display:none;z-index:4}' +
'.asb-ball.hasdot .asb-dot{display:block}' +
/* box-sizing 是拖拽的**正确性前提**，不是风格偏好：rect.width 含 1px 边框而
   style.width 默认写 content-box，两者差 2px。存 rect / 写 style 形成闭环后
   每拖一次就胖 2px（实测 382→384→386…），几十次后面板自己长满屏。 */
'.asb-panel{position:fixed;z-index:9999;box-sizing:border-box;width:380px;' +
'max-width:calc(100vw - 24px);' +
'height:min(560px,78vh);display:none;flex-direction:column;overflow:hidden;' +
'background:var(--xz-bg,#fff);color:var(--xz-txt,#111);border:1px solid var(--xz-bd,#ddd);' +
'border-radius:16px;box-shadow:0 12px 40px rgba(0,0,0,.24);animation:asbUp .18s ease}' +
'.asb-panel.open{display:flex}' +
/* ── 自由拖拽/缩放（P1 2026-08-29，老板：「要能随意拖拉位置和放大放小」）──
   面板原本死锚在球的对角象限，读长回答只能在 380x560 的小窗里滚。
   拖动期禁掉过渡与动画：否则每帧都在补间，跟手感会糊。 */
'.asb-panel.asb-moving{animation:none;transition:none;user-select:none}' +
'.asb-panel.asb-moving .asb-body{pointer-events:none}' +
'.asb-hd{cursor:grab;touch-action:none}' +
'.asb-panel.asb-moving .asb-hd{cursor:grabbing}' +
/* 八向手柄：四边 6px 命中带 + 四角 14px 方块（角优先，故 z 更高）。
   窄屏整组隐藏——那里 CSS 强制全宽贴底，拖拽没有意义且会挡内容。 */
'.asb-rs{position:absolute;z-index:6;touch-action:none}' +
'.asb-rs.n{top:-3px;left:14px;right:14px;height:8px;cursor:ns-resize}' +
'.asb-rs.s{bottom:-3px;left:14px;right:14px;height:8px;cursor:ns-resize}' +
'.asb-rs.w{left:-3px;top:14px;bottom:14px;width:8px;cursor:ew-resize}' +
'.asb-rs.e{right:-3px;top:14px;bottom:14px;width:8px;cursor:ew-resize}' +
'.asb-rs.nw{top:-3px;left:-3px;width:16px;height:16px;cursor:nwse-resize;z-index:7}' +
'.asb-rs.ne{top:-3px;right:-3px;width:16px;height:16px;cursor:nesw-resize;z-index:7}' +
'.asb-rs.sw{bottom:-3px;left:-3px;width:16px;height:16px;cursor:nesw-resize;z-index:7}' +
'.asb-rs.se{bottom:-3px;right:-3px;width:16px;height:16px;cursor:nwse-resize;z-index:7}' +
/* 右下角给一个看得见的抓手暗示——不画的话没人知道可以缩放 */
'.asb-rs.se::after{content:"";position:absolute;right:3px;bottom:3px;width:8px;' +
'height:8px;border-right:2px solid var(--xz-bd,#ddd);' +
'border-bottom:2px solid var(--xz-bd,#ddd);border-radius:0 0 3px 0}' +
'.asb-rs:focus-visible{outline:2px solid var(--xz-accent,#4f6ef7);outline-offset:1px}' +
/* 吸附提示：贴边命中时描边一下，让「吸住了」有反馈 */
'.asb-panel.asb-snapped{box-shadow:0 12px 40px rgba(0,0,0,.24),' +
'0 0 0 2px var(--xz-accent,#4f6ef7)}' +
'.asb-hd-rst{border:none;background:none;color:var(--xz-muted,#888);' +
'cursor:pointer;font-size:.85rem;padding:.2rem .4rem;border-radius:8px;' +
'line-height:1;flex-shrink:0}' +
'.asb-hd-rst:hover{background:var(--xz-hover,rgba(0,0,0,.05));' +
'color:var(--xz-txt,#111)}' +
'.asb-hd-rst[hidden]{display:none}' +
'@keyframes asbUp{from{opacity:0;transform:translateY(8px)}to{opacity:1;transform:none}}' +
'.asb-hd{display:flex;align-items:center;gap:.5rem;padding:.6rem .8rem;' +
'border-bottom:1px solid var(--xz-bd,#ddd);flex-shrink:0}' +
'.asb-hd-ic{width:26px;height:26px;border-radius:50%;flex-shrink:0;' +
'background:linear-gradient(135deg,var(--xz-accent,#4f6ef7),#8b5cf6);display:flex;' +
'align-items:center;justify-content:center;color:#fff;transition:filter .5s ease}' +
'.asb-hd-ic svg{width:15px;height:15px}' +
/* 面板头像迷你核：镜像球的状态色（同一隐喻，球↔面板视觉连续） */
'.asb-panel.st-listening .asb-hd-ic{filter:hue-rotate(-54deg) saturate(1.3)}' +
'.asb-panel.st-thinking .asb-hd-ic{filter:hue-rotate(26deg) saturate(1.25)}' +
'.asb-panel.st-alert .asb-hd-ic{filter:hue-rotate(96deg) saturate(1.15)}' +
'.asb-hd-name{font-weight:700;font-size:.88rem;flex:1;min-width:0;overflow:hidden;' +
'text-overflow:ellipsis;white-space:nowrap}' +
'.asb-x{border:none;background:none;color:var(--xz-muted,#888);cursor:pointer;' +
'font-size:1rem;padding:.2rem .45rem;border-radius:8px;line-height:1}' +
'.asb-x:hover{background:var(--xz-input,#f3f4f6);color:var(--xz-txt,#111)}' +
/* ── 模式条（实施73 P1-1）：三大功能升格为一级导航 ──────────────────────
   段控 + 滑块指示器；只有一个模式在册（兄弟组件缺席）时整条隐藏 `.solo`，
   退化成旧的「纯问答面板」而不是留一个点不动的空段控。 */
'.asb-modes{position:relative;display:flex;gap:.2rem;flex-shrink:0;' +
'margin:.5rem .8rem .15rem;padding:.2rem;border-radius:12px;' +
'background:var(--xz-input,#f3f4f6)}' +
'.asb-modes.solo{display:none}' +
'.asb-modes-ind{position:absolute;top:.2rem;bottom:.2rem;left:0;border-radius:10px;' +
'background:var(--xz-bg,#fff);box-shadow:0 1px 4px rgba(0,0,0,.16);z-index:0;' +
'pointer-events:none;transition:transform .18s ease,width .18s ease}' +
'.asb-mode{position:relative;z-index:1;flex:1;border:none;background:none;' +
'cursor:pointer;font-family:inherit;font-size:.78rem;font-weight:600;' +
'color:var(--xz-muted,#888);padding:.42rem .3rem;border-radius:10px;display:flex;' +
'align-items:center;justify-content:center;gap:.28rem;white-space:nowrap}' +
'.asb-mode i{font-style:normal;font-size:.92rem;line-height:1}' +
'.asb-mode[aria-checked="true"]{color:var(--xz-txt,#111)}' +
'.asb-mode:focus-visible{outline:2px solid var(--xz-accent,#4f6ef7);outline-offset:1px}' +
'@media (prefers-reduced-motion:reduce){.asb-modes-ind{transition:none}}' +
/* ── 次级页签行（P1-5）：报障 · 我的 · 常问 · ⚙ ───────────────────────── */
'.asb-subtabs{display:flex;gap:.08rem;align-items:center;flex-shrink:0;' +
'padding:.28rem .55rem .38rem;border-top:1px solid var(--xz-bd,#ddd)}' +
'.asb-sub{border:none;background:none;cursor:pointer;font-family:inherit;' +
'font-size:.7rem;color:var(--xz-muted,#888);padding:.24rem .5rem;border-radius:7px}' +
'.asb-sub:hover{background:var(--xz-input,#f3f4f6);color:var(--xz-txt,#111)}' +
'.asb-sub.cur{background:var(--xz-input,#f3f4f6);color:var(--xz-accent,#4f6ef7);' +
'font-weight:600}' +
'.asb-sub-sp{flex:1}' +
/* ── 标题栏手机操控（P1-2）：通道不是第四种能力，故不占模式条 ─────────── */
'.asb-hd-pair{position:relative;border:none;background:none;cursor:pointer;' +
'font-size:.95rem;padding:.2rem .4rem;border-radius:8px;line-height:1;' +
'color:var(--xz-muted,#888)}' +
'.asb-hd-pair:hover{background:var(--xz-input,#f3f4f6)}' +
'.asb-hd-pair i{position:absolute;right:2px;bottom:2px;width:7px;height:7px;' +
'border-radius:50%;background:var(--xz-muted,#bbb);border:1.5px solid var(--xz-bg,#fff)}' +
'.asb-hd-pair.on{color:var(--xz-txt,#111)}' +
'.asb-hd-pair.on i{background:#22c55e}' +
/* ── 标题栏电脑操控（实施91 P1-1）：与手机键同族的通道式入口，enabled 才显 ── */
'.asb-hd-pc{position:relative;border:none;background:none;cursor:pointer;' +
'font-size:.95rem;padding:.2rem .4rem;border-radius:8px;line-height:1;' +
'color:var(--xz-muted,#888)}' +
'.asb-hd-pc[hidden]{display:none}' +
'.asb-hd-pc:hover{background:var(--xz-input,#f3f4f6)}' +
'.asb-hd-pc i{position:absolute;right:2px;bottom:2px;width:7px;height:7px;' +
'border-radius:50%;background:var(--xz-muted,#bbb);border:1.5px solid var(--xz-bg,#fff)}' +
'.asb-hd-pc.on{color:var(--xz-txt,#111)}' +
'.asb-hd-pc.on i{background:#22c55e}' +
/* ── 模式内容通用卡（球提供，教学/替我做挂载时直接复用＝三个模式一套度量） */
'.asb-md{display:flex;flex-direction:column;gap:.5rem}' +
'.asb-md-hero{border:1px solid var(--xz-bd,#ddd);border-radius:12px;' +
'background:var(--xz-input,#f7f7f9);padding:.65rem .7rem;display:flex;' +
'flex-direction:column;gap:.42rem}' +
'.asb-md-t{font-size:.8rem;font-weight:700;display:flex;align-items:center;gap:.35rem}' +
'.asb-md-d{font-size:.7rem;color:var(--xz-muted,#888);line-height:1.55}' +
'.asb-md-go{border:none;border-radius:10px;background:var(--xz-accent,#4f6ef7);' +
'color:#fff;cursor:pointer;font-family:inherit;font-size:.82rem;font-weight:600;' +
'padding:.55rem .9rem}' +
'.asb-md-go.off{background:#ef4444}' +
'.asb-md-row{display:flex;gap:.35rem;flex-wrap:wrap}' +
'.asb-md-b{border:1px solid var(--xz-bd,#ddd);background:var(--xz-bg,#fff);' +
'color:var(--xz-txt,#333);border-radius:9px;cursor:pointer;font-family:inherit;' +
'font-size:.74rem;padding:.32rem .6rem}' +
'.asb-md-b:hover{border-color:var(--xz-accent,#4f6ef7);color:var(--xz-accent,#4f6ef7)}' +
'.asb-md-safe{font-size:.68rem;color:var(--xz-muted,#888);line-height:1.55}' +
/* ── 主次分层（P1-2，2026-08-28）──────────────────────────────────────────
   此前「替我做」两张卡共用 .asb-md-hero＝同边框同底色同 padding，于是核心
   能力与手机通道视觉权重相同，整屏零焦点（老板实录：版面太碎没有重点）。
   现在：主卡＝accent 左轴 + 提亮标题 + 唯一实心主按钮（.asb-md-go，样式表
   里早已定义却从没被调用过）；次要入口降级为一行 .asb-md-sub。 */
'.asb-md-hero--pri{border-color:transparent;' +
'border-left:3px solid var(--xz-accent,#4f6ef7);background:var(--xz-bg,#fff);' +
'box-shadow:0 1px 6px rgba(0,0,0,.10)}' +
'.asb-md-hero--pri .asb-md-t{font-size:.9rem}' +
'.asb-md-hero--pri .asb-md-d{font-size:.74rem;color:var(--xz-txt,#333)}' +
'.asb-md-hd{display:flex;align-items:center;gap:.4rem}' +
'.asb-md-hd .asb-md-t{flex:1;min-width:0}' +
'.asb-md-lnk{border:none;background:none;cursor:pointer;font-family:inherit;' +
'font-size:.7rem;color:var(--xz-accent,#4f6ef7);padding:.1rem .2rem}' +
'.asb-md-lnk:hover{text-decoration:underline}' +
'.asb-md-sub{display:flex;align-items:center;gap:.5rem;width:100%;' +
'text-align:left;box-sizing:border-box;border:1px solid var(--xz-bd,#ddd);' +
'border-radius:10px;background:none;cursor:pointer;font-family:inherit;' +
'padding:.45rem .55rem;color:var(--xz-txt,#333)}' +
'.asb-md-sub:hover{border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-md-sub-i{font-size:1rem;line-height:1;flex-shrink:0}' +
'.asb-md-sub-x{flex:1;min-width:0;display:flex;flex-direction:column;gap:.1rem}' +
'.asb-md-sub-x b{font-size:.78rem;font-weight:600}' +
'.asb-md-sub-x i{font-style:normal;font-size:.68rem;color:var(--xz-muted,#888)}' +
'.asb-md-sub-go{color:var(--xz-muted,#bbb);flex-shrink:0;font-size:1rem;' +
'line-height:1}' +
/* 示例任务（P1-3）：空态是新用户最需要引导的位置，不该只说「还没有记录」。
   点一条＝填进输入框并聚焦（不自动提交——替我做会改设置，得让人先看清）。 */
'.asb-md-ex{display:flex;flex-direction:column;gap:.3rem}' +
'.asb-md-ex-t{font-size:.68rem;color:var(--xz-muted,#888)}' +
/* 模式状态行（实施73 P2-1）：一句「本页 N 处可讲解」就是进入模式的理由。
   0 处/词典未就绪时同一行改说实话，所以做成中性底色而非成绩单绿。 */
'.asb-md-stat{font-size:.72rem;line-height:1.5;color:var(--xz-txt,#333);' +
'background:var(--xz-bg,#fff);border:1px solid var(--xz-bd,#e5e7eb);' +
'border-radius:9px;padding:.34rem .55rem}' +
'.asb-md-stat b{color:var(--xz-accent,#4f6ef7);font-size:.86rem;' +
'padding:0 .12rem}' +
'.asb-md-stat[data-n="0"],.asb-md-stat[data-n="-1"],' +
'.asb-md-stat[data-n="-2"]{color:var(--xz-muted,#888)}' +
'.asb-md-list{display:flex;flex-direction:column}' +
'.asb-md-li{display:flex;align-items:center;gap:.4rem;font-size:.72rem;' +
'border-top:1px solid var(--xz-bd,#eee);padding:.34rem 0}' +
'.asb-md-li .m{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;' +
'white-space:nowrap}' +
'.asb-md-li .w{color:var(--xz-muted,#888);font-size:.66rem;flex-shrink:0}' +
/* ── 常问面板（P1-6） ─────────────────────────────────────────────────── */
'.asb-faq-s{display:flex;gap:.35rem}' +
'.asb-faq-s input{flex:1;border:1px solid var(--xz-bd,#ddd);border-radius:10px;' +
'background:var(--xz-input,#f7f7f9);color:var(--xz-txt,#111);font-family:inherit;' +
'font-size:.78rem;padding:.4rem .6rem;outline:none}' +
'.asb-faq-s input:focus{border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-faq-g{font-size:.66rem;color:var(--xz-muted,#888);margin:.3rem 0 .05rem}' +
'.asb-faq-i{display:flex;align-items:center;gap:.4rem;width:100%;text-align:left;' +
'border:1px solid var(--xz-bd,#ddd);background:var(--xz-bg,#fff);' +
'color:var(--xz-txt,#333);border-radius:9px;cursor:pointer;font-family:inherit;' +
'font-size:.75rem;padding:.36rem .55rem;box-sizing:border-box}' +
'.asb-faq-i:hover{border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-faq-i .q{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;' +
'white-space:nowrap}' +
'.asb-faq-i .n{font-size:.64rem;color:var(--xz-muted,#888);flex-shrink:0}' +
'.asb-body{flex:1;overflow-y:auto;padding:.7rem .8rem;display:flex;' +
'flex-direction:column;gap:.55rem}' +
'.asb-chips{display:flex;flex-wrap:wrap;gap:.35rem}' +
'.asb-chips-t{font-size:.68rem;color:var(--xz-muted,#888);width:100%}' +
'.asb-chip{border:1px solid var(--xz-bd,#ddd);background:var(--xz-input,#f7f7f9);' +
'color:var(--xz-txt,#333);border-radius:999px;padding:.22rem .6rem;font-size:.74rem;' +
'cursor:pointer;font-family:inherit;max-width:100%;overflow:hidden;' +
'text-overflow:ellipsis;white-space:nowrap}' +
'.asb-chip:hover{border-color:var(--xz-accent,#4f6ef7);color:var(--xz-accent,#4f6ef7)}' +
'.asb-msg{max-width:92%;padding:.5rem .7rem;border-radius:12px;font-size:.82rem;' +
'line-height:1.55;word-break:break-word}' +
'.asb-msg.user{align-self:flex-end;background:var(--xz-accent,#4f6ef7);color:#fff;' +
'border-bottom-right-radius:4px}' +
'.asb-msg.ai{align-self:flex-start;background:var(--xz-input,#f3f4f6);' +
'color:var(--xz-txt,#111);border-bottom-left-radius:4px}' +
/* 错误气泡视觉降级（P2）：原满饱和红描边 + #dc2626 文字在暗色壳里既刺眼
   又对比度不足；改左色条保语义、正文回 --xz-txt 保可读。 */
'.asb-msg.err{align-self:flex-start;background:rgba(239,68,68,.09);' +
'color:var(--xz-txt,#111);border:none;border-left:3px solid #ef4444;' +
'border-radius:4px 12px 12px 4px}' +
'.asb-msg code{background:rgba(127,127,127,.16);padding:.05rem .3rem;' +
'border-radius:4px;font-size:.76rem}' +
'.asb-sref{color:var(--xz-accent,#4f6ef7);font-weight:600;font-size:.72rem}' +
'.asb-srcs{align-self:flex-start;display:flex;flex-direction:column;gap:.3rem;' +
'max-width:92%}' +
'.asb-srcs-t{font-size:.66rem;color:var(--xz-muted,#888)}' +
/* 依据徽标（2026-08-29）：产品事实卡＝中性；通用知识＝琥珀提示色，因为那句
   话不是产品承诺，视觉上必须与「有文档依据」区分得开。用 color-mix 取宿主
   accent/warn，不引入新色值（品牌令牌纪律）。 */
'.asb-basis{align-self:flex-start;font-size:.66rem;color:var(--xz-muted,#888);' +
'border-left:2px solid var(--xz-bd,#ddd);padding:.05rem .45rem;line-height:1.5}' +
'.asb-basis.gen{color:#b45309;border-left-color:#f59e0b;' +
'background:rgba(245,158,11,.08);border-radius:0 6px 6px 0}' +
'.asb-src{display:flex;align-items:center;gap:.4rem;border:1px solid var(--xz-bd,#ddd);' +
'border-radius:9px;padding:.3rem .55rem;font-size:.72rem;color:var(--xz-txt,#333);' +
'background:var(--xz-bg,#fff)}' +
'.asb-src .n{color:var(--xz-accent,#4f6ef7);font-weight:700;flex-shrink:0}' +
'.asb-src .tt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
'.asb-src a{color:var(--xz-accent,#4f6ef7);text-decoration:none;flex-shrink:0;' +
'font-size:.7rem;cursor:pointer}' +
'.asb-fb{align-self:flex-start;display:flex;gap:.35rem;align-items:center;' +
'font-size:.7rem;color:var(--xz-muted,#888)}' +
'.asb-fb button{border:1px solid var(--xz-bd,#ddd);background:none;border-radius:7px;' +
'cursor:pointer;padding:.12rem .45rem;font-size:.75rem;font-family:inherit}' +
'.asb-fb button:hover{border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-fb button:disabled{opacity:.45;cursor:default}' +
'.asb-act{align-self:flex-start;border:1px dashed var(--xz-bd,#ddd);background:none;' +
'border-radius:9px;cursor:pointer;padding:.3rem .6rem;font-size:.74rem;' +
'color:var(--xz-txt,#333);font-family:inherit}' +
'.asb-act:hover{border-color:var(--xz-accent,#4f6ef7);color:var(--xz-accent,#4f6ef7)}' +
/* 错误动作成组（P0-4）：.asb-act 自带 align-self:flex-start，两个按钮直接
   并列会各占一行；套一层 flex 让「重试 / 报障」读起来是一组出路。 */
'.asb-err-acts{align-self:flex-start;display:flex;gap:.35rem;flex-wrap:wrap}' +
'.asb-err-acts .asb-act{align-self:auto}' +
'.asb-ft{display:flex;gap:.45rem;padding:.55rem .8rem;border-top:1px solid var(--xz-bd,#ddd);' +
'flex-shrink:0;align-items:flex-end}' +
'.asb-in{flex:1;resize:none;border:1px solid var(--xz-bd,#ddd);border-radius:10px;' +
'background:var(--xz-input,#f7f7f9);color:var(--xz-txt,#111);font-family:inherit;' +
'font-size:.82rem;padding:.45rem .6rem;max-height:132px;min-height:36px;' +
'overflow-y:auto;outline:none}' +
'.asb-in:focus{border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-send{border:none;border-radius:10px;background:var(--xz-accent,#4f6ef7);color:#fff;' +
'cursor:pointer;font-family:inherit;font-size:.8rem;font-weight:600;' +
'padding:.5rem .85rem;flex-shrink:0}' +
'.asb-send:disabled{opacity:.5;cursor:default}' +
'.asb-mic{border:1px solid var(--xz-bd,#ddd);border-radius:10px;background:var(--xz-input,#f7f7f9);' +
'cursor:pointer;font-size:.95rem;padding:.42rem .55rem;flex-shrink:0;font-family:inherit}' +
'.asb-mic.rec{background:#ef4444;border-color:#ef4444;animation:asbPulse 1.2s ease-in-out infinite}' +
'.asb-an{position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,.72);display:flex;' +
'flex-direction:column;align-items:center;justify-content:center;gap:.6rem}' +
'.asb-an-bar{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;justify-content:center}' +
'.asb-an-bar span{color:#fff;font-size:.78rem;margin-right:.4rem}' +
'.asb-an-bar button{border:1px solid rgba(255,255,255,.4);background:rgba(255,255,255,.12);' +
'color:#fff;border-radius:8px;cursor:pointer;font-family:inherit;font-size:.78rem;' +
'padding:.32rem .7rem}' +
'.asb-an-bar button.on{background:var(--xz-accent,#4f6ef7);border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-an canvas{max-width:94vw;max-height:78vh;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.5);' +
'cursor:crosshair;background:#fff}' +
'.asb-tools{display:flex;gap:.3rem;flex-wrap:wrap;padding:.4rem .8rem .55rem;' +
'border-top:1px solid var(--xz-bd,#ddd);flex-shrink:0}' +
/* ⚙ 面板里复用同一批工具键，但那里它是内容不是底栏，去掉分隔线与内缩 */
'.asb-body .asb-tools{border-top:none;padding:.1rem 0;gap:.35rem}' +
'.asb-tool{border:none;background:none;color:var(--xz-muted,#888);cursor:pointer;' +
'font-size:.68rem;font-family:inherit;padding:.18rem .4rem;border-radius:6px}' +
'.asb-tool:hover{background:var(--xz-input,#f3f4f6);color:var(--xz-txt,#111)}' +
'.asb-rp{display:flex;flex-direction:column;gap:.55rem}' +
'.asb-rp textarea{width:100%;min-height:84px;resize:vertical;border:1px solid var(--xz-bd,#ddd);' +
'border-radius:10px;background:var(--xz-input,#f7f7f9);color:var(--xz-txt,#111);' +
'font-family:inherit;font-size:.82rem;padding:.5rem .6rem;outline:none;box-sizing:border-box}' +
'.asb-rp textarea:focus{border-color:var(--xz-accent,#4f6ef7)}' +
/* 提交成功后的预期管理行：比正文淡、比正文小，但必须可读（不用 opacity 调灰） */
'.asb-rp-next{display:inline-block;margin:.25rem 0;font-size:.72rem;' +
'line-height:1.5;color:var(--xz-muted,#888)}' +
'.asb-rp-shot{font-size:.74rem;color:var(--xz-muted,#888)}' +
'.asb-rp-shot a{color:var(--xz-accent,#4f6ef7);cursor:pointer;text-decoration:underline}' +
'.asb-rp-thumb{position:relative;width:120px}' +
'.asb-rp-thumb img{width:120px;border-radius:8px;border:1px solid var(--xz-bd,#ddd);display:block}' +
'.asb-rp-thumb button{position:absolute;top:-7px;right:-7px;width:20px;height:20px;' +
'border-radius:50%;border:none;background:#ef4444;color:#fff;font-size:.7rem;' +
'cursor:pointer;line-height:1}' +
'.asb-env{border:1px solid var(--xz-bd,#ddd);border-radius:9px;font-size:.7rem}' +
'.asb-env summary{padding:.35rem .55rem;cursor:pointer;color:var(--xz-muted,#888)}' +
'.asb-env div{padding:0 .55rem .45rem;color:var(--xz-muted,#888);line-height:1.5}' +
'.asb-tk{border:1px solid var(--xz-bd,#ddd);border-radius:10px;padding:.5rem .6rem;' +
'font-size:.76rem;display:flex;flex-direction:column;gap:.2rem}' +
'.asb-tk-hd{display:flex;align-items:center;gap:.4rem}' +
'.asb-tk-hd .id{color:var(--xz-muted,#888);font-size:.68rem;flex-shrink:0}' +
'.asb-tk-hd .tt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' +
'font-weight:600}' +
'.asb-st{flex-shrink:0;font-size:.64rem;border-radius:999px;padding:.1rem .45rem;' +
'font-weight:600;background:var(--xz-input,#f3f4f6);color:var(--xz-muted,#666)}' +
'.asb-st.fixed{background:rgba(16,185,129,.15);color:#059669}' +
'.asb-st.verified{background:rgba(99,102,241,.14);color:#6366f1}' +
'.asb-tk-note{font-size:.7rem;color:#059669}' +
'.asb-mine-bar{display:flex;justify-content:flex-end}' +
/* 空态 padding 1.2rem→.7rem（P2）：380x560 面板里固定件已吃掉约 29%，
   一句「还没有记录」独占 62px 会把真正该被看见的引导挤下去。 */
'.asb-empty{color:var(--xz-muted,#888);font-size:.78rem;text-align:center;padding:.7rem 0}' +
'.asb-live .tip-toggle{display:none!important}' +
'.asb-spot{position:fixed;z-index:10000;pointer-events:none;border:3px solid var(--xz-accent,#4f6ef7);' +
'border-radius:12px;box-shadow:0 0 0 100vmax rgba(15,23,42,.38),0 0 22px rgba(99,102,241,.65);' +
'animation:asbSpotIn .3s ease}' +
'@keyframes asbSpotIn{from{opacity:0;transform:scale(1.12)}to{opacity:1;transform:none}}' +
'.asb-nudge{position:fixed;z-index:9999;display:flex;align-items:center;gap:.5rem;max-width:300px;' +
'padding:.55rem .7rem;background:var(--xz-bg,#fff);color:var(--xz-txt,#111);' +
'border:1px solid var(--xz-bd,#ddd);border-left:3px solid #ef4444;border-radius:12px;' +
'box-shadow:0 8px 28px rgba(0,0,0,.22);font-size:.78rem;line-height:1.45;animation:asbUp .25s ease}' +
'.asb-nudge button{border:1px solid var(--xz-bd,#ddd);background:var(--xz-input,#f7f7f9);' +
'color:var(--xz-txt,#333);border-radius:8px;cursor:pointer;font-family:inherit;font-size:.74rem;' +
'padding:.25rem .55rem;flex-shrink:0}' +
'.asb-nudge [data-n="go"]{background:var(--xz-accent,#4f6ef7);border-color:var(--xz-accent,#4f6ef7);color:#fff}' +
'@media(max-width:480px){' +
'.asb-panel{left:8px!important;right:8px!important;width:auto;bottom:72px!important;' +
'top:auto!important;height:min(560px,72vh)}' +
'}';
    var st = document.createElement('style');
    st.id = 'asb-style';
    st.textContent = css;
    document.head.appendChild(st);
  }

  var ICON_SPARK =
    '<svg viewBox="0 0 24 24" fill="none" aria-hidden="true">' +
    '<path d="M12 3l1.7 4.6L18 9.3l-4.3 1.7L12 15.6l-1.7-4.6L6 9.3l4.3-1.7L12 3z"' +
    ' fill="currentColor"/>' +
    '<path d="M18.5 14l.9 2.3 2.1.8-2.1.8-.9 2.3-.9-2.3-2.1-.8 2.1-.8.9-2.3z"' +
    ' fill="currentColor" opacity=".85"/></svg>';

  /* ────────────────────────────────── Orb 引擎（2026-08-23 实施59 未来感形态）
   * 「活体能量核」：球=状态灯。状态机 idle/hint/listening/thinking/alert +
   * 一次性 success 爆闪（粒子重组对勾）。三档画质：
   *   lv0 = 静态渐变+透明度脉冲（prefers-reduced-motion 强制此档）
   *   lv1 = 纯 CSS 极光层（合成器动画，0 JS 帧）
   *   lv2 = lv1 + Canvas 粒子/声波涟漪/大 bloom（仅活跃态跑 rAF，待机零开销）
   * 档位解析：localStorage asb_orb_lvl（工具行「动效」档）→ boot.orb_level
   * （后端旋钮预留，bootstrap 暂不下发）→ 默认 2。运行期调速器只降不升：
   * 帧时长 EMA>26ms 先砍半粒子、再降 CSS 档，事件打 asb_orb_* 进
   * /api/telemetry/ui-event（file:// 夹具下自动跳过）。
   * 麦克风电平复用 REC.stream（AudioContext 在用户手势内创建，停录 suspend
   * 复用）；thinking 无音频，用流式 delta 到达节奏当「思考的声音」。
   * 铁律：每个动效必须映射真实系统状态——信号消失动画即熄。 */
  var ORB = {
    lvl: 2, state: 'idle', force: '',
    cv: null, ctx: null, dpr: 1, raf: 0, on: false,
    last: 0, ema: 16.7, degT0: 0, degStep: 0,
    bloom: 0, bloomT: 0, pulse: 0,
    pa: null, pr: null, ps: null, pz: null,
    rings: null, burst: null, glyphPts: null,
    actx: null, an: null, src: null, au: null,
    level: 0, prevLv: 0, bass: 0, mid: 0, treb: 0,
    floor: 0.05, peak: 0.25, lastRip: 0,
    nudgeOn: false, alertUntil: 0, dark: true, micEl: null,
    listenSeen: false,
    /* P2（2026-08-23）：speaking=小智播报（TTS 元素电平驱动径向均衡条）；
       modes.teach/agent=实施58 教学/代办线经公共 API AssistantBall.orbMode
       点亮的黏性模式（本组件零依赖它们的内部实现）。 */
    modes: { teach: false, agent: false }, agentP: -1,
    speaking: false, speakEl: null,
  };
  var ORB_SIZE = 168, ORB_N = 128;
  var ORB_STATES = ['idle', 'hint', 'listening', 'speaking', 'thinking',
    'agent', 'teach', 'alert'];

  function orbBeacon(action) {
    try {
      if (String(location.protocol).indexOf('http') !== 0) { return; }
      if (!navigator.sendBeacon) { return; }
      navigator.sendBeacon('/api/telemetry/ui-event', new Blob(
        [JSON.stringify({ page: pagePath(), action: String(action) })],
        { type: 'application/json' }));
    } catch (e) { /* ignore */ }
  }

  /* 意图埋点（P0-1 2026-08-27）——补的是**漏斗的分母**。
     此前球只发 asb_orb_*（boot/listen/degrade/say/calm），全是球自身的生命
     周期事件，boot 更是「页面加载即发」而非用户意图；于是「有多少人真的点开
     了小智」「点开后去了哪」线上无从得知，任何改版都无法验收。2026-08-01..27
     实测：boot 101 次，而全部功能类事件集中在 08-23 单日（开发自测），
     此后 7 次 boot 零功能事件——没有分母时，这个「7 次触达」曾被误读成真实用量。
     协议与 orbBeacon 完全一致（同通道、同守卫），只是语义分层：
     orbBeacon = 球在做什么，beacon = 人在做什么。 */
  function beacon(action) { orbBeacon(action); }

  function orbLevel() {
    var lv = 2;
    try {
      var b = S.boot || {};
      if (typeof b.orb_level === 'number') { lv = b.orb_level; }
      var saved = localStorage.getItem('asb_orb_lvl');
      if (saved === '0' || saved === '1' || saved === '2') { lv = +saved; }
    } catch (e) { /* ignore */ }
    try {
      if (window.matchMedia &&
          window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        lv = 0;
      }
    } catch (e2) { /* ignore */ }
    return Math.max(0, Math.min(2, lv));
  }

  function orbCompute() {
    if (ORB.force) { return ORB.force; }
    if (REC) { return 'listening'; }
    if (ORB.speaking) { return 'speaking'; }
    if (S.busy) { return 'thinking'; }
    if (ORB.modes.agent) { return 'agent'; }
    if (ORB.modes.teach) { return 'teach'; }
    if (ORB.nudgeOn || ORB.alertUntil > Date.now()) { return 'alert'; }
    if (S.dot) { return 'hint'; }
    return 'idle';
  }

  /* 公共 API：教学/代办线点亮黏性模式（agent 可带 0..1 进度画弧，缺省不画）。
     与 _orbForce 的区别：force 是测试钩子整个覆写判定；mode 参与正常优先级
     （listening/speaking/thinking 仍压过它——真实信号永远赢）。 */
  function orbMode(name, on, progress) {
    if (!ORB.modes || !(name in ORB.modes)) { return; }
    ORB.modes[name] = !!on;
    if (name === 'agent') {
      ORB.agentP = (on && typeof progress === 'number')
        ? Math.max(0, Math.min(1, progress)) : -1;
    }
    syncOrb();
  }

  function syncOrb() {
    if (!$ball) { return; }
    var st = orbCompute();
    if (st !== ORB.state) {
      ORB.state = st;
      for (var i = 0; i < ORB_STATES.length; i++) {
        var c = 'st-' + ORB_STATES[i];
        $ball.classList.toggle(c, ORB_STATES[i] === st);
        if ($panel) { $panel.classList.toggle(c, ORB_STATES[i] === st); }
      }
      if (st === 'listening' && !ORB.listenSeen) {
        ORB.listenSeen = true;
        orbBeacon('asb_orb_listen');
      }
    }
    orbSyncCanvas();
  }

  function orbNeedsCanvas() {
    if (ORB.lvl < 2 || ORB.degStep > 1) { return false; }
    return !!ORB.burst || ORB.state === 'listening' ||
      ORB.state === 'thinking' || ORB.state === 'speaking' ||
      ORB.state === 'agent';
  }
  function orbSyncCanvas() {
    if (orbNeedsCanvas()) { orbStart(); } else { orbStop(); }
  }

  function orbAlloc() {
    if (ORB.pa) { return; }
    ORB.pa = new Float32Array(ORB_N);
    ORB.pr = new Float32Array(ORB_N);
    ORB.ps = new Float32Array(ORB_N);
    ORB.pz = new Float32Array(ORB_N);
    for (var i = 0; i < ORB_N; i++) {
      ORB.pa[i] = Math.random() * 6.28318;
      ORB.pr[i] = Math.random();
      ORB.ps[i] = 0.5 + Math.random();
      ORB.pz[i] = 0.8 + Math.random() * 1.5;
    }
    ORB.rings = [];
    for (var j = 0; j < 4; j++) { ORB.rings.push({ r: 0, a: 0 }); }
  }

  function orbStart() {
    if (ORB.on) { return; }
    orbAlloc();
    if (!ORB.cv) {
      ORB.dpr = Math.min(window.devicePixelRatio || 1, 2);
      ORB.cv = document.createElement('canvas');
      ORB.cv.className = 'asb-fx';
      ORB.cv.width = Math.round(ORB_SIZE * ORB.dpr);
      ORB.cv.height = Math.round(ORB_SIZE * ORB.dpr);
      ORB.cv.setAttribute('aria-hidden', 'true');
      $wrap.insertBefore(ORB.cv, $ball);
      ORB.ctx = ORB.cv.getContext('2d');
    }
    if (!ORB.ctx) { return; }
    try {
      /* 主题亮暗决定合成模式：暗底加色发光(lighter)/亮底普通叠加防洗白 */
      var bg = getComputedStyle($panel || document.body).backgroundColor || '';
      var m = bg.match(/(\d+)[,\s]+(\d+)[,\s]+(\d+)/);
      ORB.dark = m
        ? ((+m[1] * 299 + +m[2] * 587 + +m[3] * 114) / 1000) < 140
        : true;
    } catch (e) { ORB.dark = true; }
    ORB.cv.style.display = 'block';
    ORB.on = true;
    ORB.last = 0;
    ORB.ema = 16.7;
    ORB.degT0 = (window.performance || Date).now();
    ORB.raf = requestAnimationFrame(orbFrame);
  }

  function orbStop() {
    if (!ORB.on) { return; }
    ORB.on = false;
    if (ORB.raf) { cancelAnimationFrame(ORB.raf); ORB.raf = 0; }
    if (ORB.cv && ORB.ctx) {
      ORB.ctx.clearRect(0, 0, ORB.cv.width, ORB.cv.height);
      ORB.cv.style.display = 'none';
      ORB.cv.style.zIndex = '1';
    }
    ORB.bloom = 0;
    var hb = $panel ? $panel.querySelector('.asb-hero') : null;
    if (hb) { hb.classList.remove('on'); }
  }

  /* ── 音频电平（仅 listening 期挂载；停录即拆，AudioContext suspend 复用） */
  function orbAudioStart(stream) {
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (!AC) { return; }
      if (!ORB.actx) { ORB.actx = new AC(); }
      if (ORB.actx.state === 'suspended') { ORB.actx.resume(); }
      ORB.an = ORB.actx.createAnalyser();
      ORB.an.fftSize = 256;
      ORB.an.smoothingTimeConstant = 0.72;
      ORB.src = ORB.actx.createMediaStreamSource(stream);
      ORB.src.connect(ORB.an);
      if (!ORB.au) { ORB.au = new Uint8Array(ORB.an.frequencyBinCount); }
      ORB.floor = 0.05; ORB.peak = 0.25; ORB.level = 0;
      ORB.micEl = $panel ? $panel.querySelector('[data-act="mic"]') : null;
    } catch (e) { ORB.an = null; ORB.src = null; }
    orbBurst('mic');  /* 聆听入场：粒子先聚成话筒再散入声波环 */
  }
  function orbAudioStop() {
    try { if (ORB.src) { ORB.src.disconnect(); } } catch (e) { /* ignore */ }
    ORB.src = null;
    ORB.an = null;
    if (ORB.micEl) {
      try { ORB.micEl.style.boxShadow = ''; } catch (e2) { /* ignore */ }
    }
    ORB.micEl = null;
    try {
      if (ORB.actx && ORB.actx.state === 'running') { ORB.actx.suspend(); }
    } catch (e3) { /* ignore */ }
    ORB.level = 0; ORB.bass = 0; ORB.mid = 0; ORB.treb = 0;
  }
  function orbBand(from, to) {
    var s = 0;
    for (var i = from; i < to; i++) { s += ORB.au[i]; }
    return s / ((to - from) * 255);
  }
  function orbAudioRead(now) {
    ORB.prevLv = ORB.level;
    if (!ORB.an) {
      /* 无分析器（能力缺失/夹具强制态）：柔和合成波保住「在听」反馈语义 */
      var t = now / 1000;
      var syn = 0.3 + 0.22 * Math.sin(t * 1.9) + 0.1 * Math.sin(t * 4.7);
      ORB.level += (Math.max(0, syn) - ORB.level) * 0.12;
      ORB.bass = ORB.level * 0.8;
      ORB.mid = ORB.level;
      ORB.treb = ORB.level * 0.6;
    } else {
      ORB.an.getByteFrequencyData(ORB.au);
      var raw = orbBand(2, 40);      /* 人声主频带 */
      if (raw > ORB.peak) { ORB.peak = ORB.peak * 0.7 + raw * 0.3; }
      else { ORB.peak *= 0.999; }
      if (ORB.peak < 0.12) { ORB.peak = 0.12; }
      if (raw < ORB.floor) { ORB.floor = ORB.floor * 0.9 + raw * 0.1; }
      var lv = (raw - ORB.floor) / Math.max(0.06, ORB.peak - ORB.floor);
      lv = Math.max(0, Math.min(1, lv));
      ORB.level += (lv - ORB.level) * 0.35;
      ORB.bass = orbBand(2, 12);
      ORB.mid = orbBand(12, 40);
      ORB.treb = orbBand(40, 96);
    }
    if (ORB.level > 0.3 && ORB.level - ORB.prevLv > 0.1 &&
        now - ORB.lastRip > 240) {
      orbRipple(now);
    }
    if (ORB.micEl) {
      /* 录音按钮实时电平圈：「看得见被听见」 */
      ORB.micEl.style.boxShadow = '0 0 0 ' +
        (2 + ORB.level * 9).toFixed(1) + 'px rgba(239,68,68,' +
        (0.16 + ORB.level * 0.24).toFixed(2) + ')';
    }
  }
  function orbRipple(now) {
    ORB.lastRip = now;
    for (var i = 0; i < ORB.rings.length; i++) {
      if (ORB.rings[i].a <= 0.01) {
        ORB.rings[i].r = 26;
        ORB.rings[i].a = 0.34 + ORB.level * 0.3;
        return;
      }
    }
  }

  /* ── speaking：TTS <audio> 元素接同一 AudioContext（小智开口，球随声舞）。
     createMediaElementSource 会把元素音频整路接进 context——分析器必须再接
     destination 否则静音；每元素只能建一次 source（缓存在元素上复用）。 */
  function orbSpeakStart(el) {
    ORB.speaking = true;
    try {
      var AC = window.AudioContext || window.webkitAudioContext;
      if (AC) {
        if (!ORB.actx) { ORB.actx = new AC(); }
        if (ORB.actx.state === 'suspended') { ORB.actx.resume(); }
        if (!el.__asbSrc) {
          el.__asbSrc = ORB.actx.createMediaElementSource(el);
        }
        ORB.an = ORB.actx.createAnalyser();
        ORB.an.fftSize = 256;
        ORB.an.smoothingTimeConstant = 0.7;
        el.__asbSrc.connect(ORB.an);
        ORB.an.connect(ORB.actx.destination);
        if (!ORB.au) { ORB.au = new Uint8Array(ORB.an.frequencyBinCount); }
        ORB.speakEl = el;
      }
    } catch (e) {
      /* 分析器链失败也不能哑巴：source 直连 destination 保出声，视觉走合成波 */
      ORB.an = null;
      try {
        if (el.__asbSrc && ORB.actx) {
          el.__asbSrc.connect(ORB.actx.destination);
        }
      } catch (e2) { /* ignore */ }
    }
    syncOrb();
  }
  function orbSpeakStop() {
    if (!ORB.speaking && !ORB.speakEl) { return; }
    ORB.speaking = false;
    try {
      if (ORB.speakEl && ORB.speakEl.__asbSrc) {
        ORB.speakEl.__asbSrc.disconnect();
      }
    } catch (e) { /* ignore */ }
    try { if (ORB.an) { ORB.an.disconnect(); } } catch (e2) { /* ignore */ }
    ORB.an = null;
    ORB.speakEl = null;
    try {
      if (ORB.actx && ORB.actx.state === 'running' && !REC) {
        ORB.actx.suspend();
      }
    } catch (e3) { /* ignore */ }
    syncOrb();
  }

  /* ── 一次性爆闪字形库（P3 泛化）：粒子飞散重组成字形（目标点=离屏字形像素
     采样，每字形缓存一次）。check=办成(报障提交/代办完成)、bang=警示入场
     (页面报错 nudge)、mic=聆听入场(开始录音)——「状态切换=能量重组成形」
     是本 orb 的视觉签名。 */
  var ORB_GLYPHS = {
    check: { dur: 950, col: ['52,211,153', '5,150,105'],
      draw: function (c) {
        c.lineWidth = 13;
        c.beginPath();
        c.moveTo(20, 52);
        c.lineTo(41, 71);
        c.lineTo(76, 28);
        c.stroke();
      } },
    bang: { dur: 900, col: ['251,191,36', '180,83,9'],
      draw: function (c) {
        c.lineWidth = 14;
        c.beginPath();
        c.moveTo(48, 18);
        c.lineTo(48, 54);
        c.stroke();
        c.beginPath();
        c.arc(48, 73, 7, 0, 6.2832);
        c.fill();
      } },
    mic: { dur: 800, col: ['103,232,249', '8,145,178'],
      draw: function (c) {
        c.lineWidth = 18;
        c.beginPath();
        c.moveTo(48, 26);
        c.lineTo(48, 44);
        c.stroke();
        c.lineWidth = 6;
        c.beginPath();
        c.arc(48, 44, 16, 0.25, 2.89);
        c.stroke();
        c.beginPath();
        c.moveTo(48, 60);
        c.lineTo(48, 71);
        c.stroke();
        c.beginPath();
        c.moveTo(37, 76);
        c.lineTo(59, 76);
        c.stroke();
      } },
  };
  function orbBurstTargets(kind) {
    if (!ORB.glyphPts) { ORB.glyphPts = {}; }
    if (ORB.glyphPts[kind]) { return ORB.glyphPts[kind]; }
    var off = document.createElement('canvas');
    off.width = 96; off.height = 96;
    var c = off.getContext('2d');
    c.strokeStyle = '#fff';
    c.fillStyle = '#fff';
    c.lineCap = 'round';
    c.lineJoin = 'round';
    ORB_GLYPHS[kind].draw(c);
    var img = c.getImageData(0, 0, 96, 96).data;
    var pts = [];
    for (var y = 0; y < 96; y += 3) {
      for (var x = 0; x < 96; x += 3) {
        if (img[(y * 96 + x) * 4 + 3] > 120) {
          pts.push([(x - 48) / 48, (y - 48) / 48]);
        }
      }
    }
    ORB.glyphPts[kind] = pts;
    return pts;
  }
  function orbBurst(kind) {
    kind = ORB_GLYPHS[kind] ? kind : 'check';
    if (!$ball) { return; }
    if (ORB.lvl < 2 || ORB.degStep > 1) {
      /* 低档回退：仅 check 给核心绿闪（纯 CSS），入场字形直接省略 */
      if (kind === 'check') {
        $ball.classList.add('okflash');
        setTimeout(function () { $ball.classList.remove('okflash'); }, 950);
      }
      return;
    }
    try { orbBurstTargets(kind); } catch (e) { return; }
    ORB.burst = { t0: (window.performance || Date).now(), kind: kind };
    syncOrb();
    /* 爆闪期画布临时压到球面之上：字形要「盖在核上」才可读；结束即还原 */
    if (ORB.cv) { ORB.cv.style.zIndex = '3'; }
  }

  /* ── 兄弟组件 DOM 观察（实施58 教学/代办线协奏，P3）——
     他们的门禁钉死「对球零符号依赖」（test_assistant_teach_wireup），协奏因此
     走对称的「DOM 协作」：他们点我们的 .asb-x 收面板，我们观察他们挂在 body
     的公开标记——.xzt-banner=教学态、.xza-card+[data-xza="stop"]=代办执行中
     （步骤进度=数 .xza-step 里 .ic 的 ✓✗▷）。软失败：标记缺席=模式不亮。
     词汇表由 tests/test_orb_sibling_dom_contract.py 双向钉住，改类名先红那里。 */
  var SIB = { mo: null, timer: 0 };
  function sibScan() {
    var teachOn = !!document.querySelector('.xzt-banner');
    if (teachOn !== ORB.modes.teach) { orbMode('teach', teachOn); }
    var card = document.querySelector('.xza-card');
    var agentOn = !!(card && card.querySelector('[data-xza="stop"]'));
    if (agentOn) {
      var steps = card.querySelectorAll('.xza-step');
      var done = 0;
      for (var i = 0; i < steps.length; i++) {
        var ic = steps[i].querySelector('.ic');
        var ch = ic ? String(ic.textContent || '') : '';
        if (ch === '✓' || ch === '✗' || ch === '▷') { done++; }
      }
      orbMode('agent', true,
        steps.length ? (done / steps.length) : undefined);
    } else if (ORB.modes.agent) {
      orbMode('agent', false);
    }
    /* 步骤打勾发生在卡内层（body childList 看不见）→ 仅执行期 1s 慢轮询 */
    if (agentOn && !SIB.timer) {
      SIB.timer = setInterval(sibScan, 1000);
    } else if (!agentOn && SIB.timer) {
      clearInterval(SIB.timer);
      SIB.timer = 0;
    }
  }
  function orbWatchSiblings() {
    if (!window.MutationObserver) { return; }
    try {
      SIB.mo = new MutationObserver(function () { sibScan(); });
      /* 只观察 body 直属子级（两个兄弟标记都直接挂 body）：零热区噪声 */
      SIB.mo.observe(document.body, { childList: true });
    } catch (e) { /* ignore */ }
    sibScan();
  }

  /* ── hover 磁性微倾（活物质感）：指针在球面上时核轻微朝向指针。
     拖拽（e.buttons）/安静档/reduced-motion（lvl<1）一律不动。 */
  function orbWireTilt() {
    $ball.addEventListener('pointermove', function (e) {
      if (ORB.lvl < 1 || e.buttons) { return; }
      var r = $ball.getBoundingClientRect();
      if (!r.width) { return; }
      var dx = (e.clientX - r.left) / r.width - 0.5;
      var dy = (e.clientY - r.top) / r.height - 0.5;
      var orb = $ball.querySelector('.asb-orb');
      if (orb) {
        orb.style.transform = 'perspective(90px) rotateX(' +
          (-dy * 7).toFixed(1) + 'deg) rotateY(' +
          (dx * 7).toFixed(1) + 'deg)';
      }
    });
    $ball.addEventListener('pointerleave', function () {
      var orb = $ball.querySelector('.asb-orb');
      if (orb) { orb.style.transform = ''; }
    });
  }

  function orbFlash(ms) {
    ORB.alertUntil = Date.now() + (ms || 2600);
    syncOrb();
    setTimeout(syncOrb, (ms || 2600) + 80);
  }

  function orbFrame(ts) {
    if (!ORB.on) { return; }
    ORB.raf = requestAnimationFrame(orbFrame);
    var dt = ORB.last ? (ts - ORB.last) : 16.7;
    ORB.last = ts;
    if (dt > 0 && dt < 130) {
      /* 调速器：EMA 持续超 26ms → 砍半粒子 → 再超 → 本会话降回纯 CSS 档 */
      ORB.ema = ORB.ema * 0.92 + dt * 0.08;
      if (ORB.ema > 26 && (ts - ORB.degT0) > 1600) {
        ORB.degT0 = ts;
        ORB.degStep++;
        ORB.ema = 16.7;
        if (ORB.degStep > 1) {
          orbBeacon('asb_orb_degrade_css');
          orbStop();
          return;
        }
        orbBeacon('asb_orb_degrade_half');
      }
    }
    var d = dt / 16.7;
    var st = ORB.state;
    ORB.bloomT = ORB.burst ? 0.8
      : (st === 'listening' ? 1
        : (st === 'speaking' ? 0.85
          : (st === 'thinking' ? 0.7
            : (st === 'agent' ? 0.6 : 0))));
    ORB.bloom += (ORB.bloomT - ORB.bloom) * Math.min(0.5, 0.09 * d);
    if (st === 'listening' || st === 'speaking') { orbAudioRead(ts); }
    ORB.pulse *= Math.pow(0.94, d);
    orbDraw(ts, d, st);
    orbHero(st);
  }

  /* ── 面板 voice-hero 波形条：聆听/播报期在输入区上方长出实时频谱（对称条）。
     与球共用同一 rAF 与同一分析器读数，零独立循环；面板没开/状态不符即收。 */
  function orbHero(st) {
    var box = $panel ? $panel.querySelector('.asb-hero') : null;
    if (!box) { return; }
    var want = S.open && (st === 'listening' || st === 'speaking');
    if (!want) {
      if (box.classList.contains('on')) { box.classList.remove('on'); }
      return;
    }
    if (!box.classList.contains('on')) { box.classList.add('on'); }
    var cv = box.firstElementChild;
    if (!cv) { return; }
    var k = ORB.dpr || 1;
    var cssW = box.clientWidth - 18;
    if (cssW < 60) { return; }
    var bw = Math.round(cssW * k), bh = Math.round(38 * k);
    if (cv.width !== bw) { cv.width = bw; }
    if (cv.height !== bh) { cv.height = bh; }
    var c = cv.getContext('2d');
    if (!c) { return; }
    c.clearRect(0, 0, bw, bh);
    var col = st === 'listening'
      ? (ORB.dark ? '103,232,249' : '8,145,178')
      : (ORB.dark ? '167,139,250' : '109,40,217');
    var NB = 44;
    var step = bw / NB;
    var mid = bh / 2;
    var now = (window.performance || Date).now();
    for (var i = 0; i < NB; i++) {
      var v = ORB.an
        ? (ORB.au[2 + ((i * 94 / NB) | 0)] / 255)
        : (0.28 + 0.26 * Math.sin(now / 150 + i * 0.9));
      v = Math.min(1, v * 1.3);
      var h = Math.max(1.6 * k, v * (mid - 2 * k));
      c.fillStyle = 'rgba(' + col + ',' + (0.3 + 0.6 * v).toFixed(3) + ')';
      c.fillRect(i * step + step * 0.22, mid - h, step * 0.56, h * 2);
    }
  }

  function orbDraw(ts, d, st) {
    var ctx = ORB.ctx;
    var W = ORB.cv.width;
    var cx = W / 2;
    var k = ORB.dpr;
    ctx.clearRect(0, 0, W, W);
    if (ORB.bloom < 0.02 && !ORB.burst) { return; }
    var mul = (0.35 + 0.65 * ORB.bloom) * k;
    ctx.globalCompositeOperation = ORB.dark ? 'lighter' : 'source-over';
    var n = ORB.degStep > 0 ? (ORB_N >> 1) : ORB_N;
    var i, a, r, x, y, al;

    if (ORB.burst) {
      var g = ORB_GLYPHS[ORB.burst.kind] || ORB_GLYPHS.check;
      var bt = ((window.performance || Date).now() - ORB.burst.t0) / g.dur;
      if (bt >= 1) {
        ORB.burst = null;
        ORB.cv.style.zIndex = '1';
        syncOrb();
        return;
      }
      var pts = (ORB.glyphPts || {})[ORB.burst.kind] || [];
      if (!pts.length) { ORB.burst = null; syncOrb(); return; }
      var colB = ORB.dark ? g.col[0] : g.col[1];
      var R = 34 * k;
      var ease = bt < 0.4 ? (1 - Math.pow(1 - bt / 0.4, 3)) : 1;
      var fade = bt > 0.72 ? 1 - (bt - 0.72) / 0.28 : 1;
      for (i = 0; i < n; i++) {
        var p = pts[i % pts.length];
        a = ORB.pa[i];
        var r0 = 30 * k * (0.6 + ORB.pr[i] * 0.8);
        var x0 = Math.cos(a) * r0, y0 = Math.sin(a) * r0;
        x = cx + x0 + (p[0] * R - x0) * ease;
        y = cx + y0 + (p[1] * R - y0) * ease;
        al = (0.25 + 0.75 * ease) * fade;
        ctx.fillStyle = 'rgba(' + colB + ',' + al.toFixed(3) + ')';
        ctx.beginPath();
        ctx.arc(x, y, ORB.pz[i] * k * (bt > 0.72 ? 1.6 : 1), 0, 6.2832);
        ctx.fill();
      }
      return;  /* 爆闪期独占画面：成功时刻要干净 */
    }

    if (st === 'listening') {
      var colL = ORB.dark ? '103,232,249' : '8,145,178';
      for (i = 0; i < ORB.rings.length; i++) {
        var rg = ORB.rings[i];
        if (rg.a > 0.01) {
          rg.r += (1.5 + ORB.level * 2.4) * d * k;
          rg.a *= Math.pow(0.955, d);
          ctx.strokeStyle = 'rgba(' + colL + ',' + rg.a.toFixed(3) + ')';
          ctx.lineWidth = 1.6 * k;
          ctx.beginPath();
          ctx.arc(cx, cx, rg.r * (0.5 + 0.5 * ORB.bloom), 0, 6.2832);
          ctx.stroke();
        }
      }
      for (i = 0; i < n; i++) {
        ORB.pa[i] += (0.010 + 0.017 * ORB.ps[i] *
          (0.4 + ORB.level * 1.6)) * d;
        a = ORB.pa[i];
        var band = i % 3 === 0 ? ORB.bass
          : (i % 3 === 1 ? ORB.mid : ORB.treb);
        r = (30 + ORB.pr[i] * 9 + band * 16) * mul;
        x = cx + Math.cos(a) * r;
        y = cx + Math.sin(a) * r * 0.96;
        al = Math.min(0.85, (0.18 + 0.5 * band + 0.2 * ORB.level) * ORB.bloom);
        ctx.fillStyle = 'rgba(' + colL + ',' + al.toFixed(3) + ')';
        ctx.beginPath();
        ctx.arc(x, y, ORB.pz[i] * k * 0.9, 0, 6.2832);
        ctx.fill();
      }
      return;
    }

    if (st === 'speaking') {
      /* 播报：径向均衡条——声音的形状直接长在核缘（TTS 频谱驱动） */
      var colS = ORB.dark ? '167,139,250' : '109,40,217';
      var NB = 24;
      ctx.lineCap = 'round';
      ctx.lineWidth = 2 * k;
      for (i = 0; i < NB; i++) {
        a = (i / NB) * 6.2832 + ts / 2400;
        var vb = ORB.an
          ? (ORB.au[2 + ((i * 94 / NB) | 0)] / 255)
          : (0.3 + 0.3 * Math.sin(ts / 130 + i * 1.7));
        vb = Math.min(1, vb * 1.35);
        var r0s = 27 * k * (0.6 + 0.4 * ORB.bloom);
        var len = (3 + vb * 15) * k * ORB.bloom;
        ctx.strokeStyle = 'rgba(' + colS + ',' +
          (0.25 + 0.55 * vb).toFixed(3) + ')';
        ctx.beginPath();
        ctx.moveTo(cx + Math.cos(a) * r0s, cx + Math.sin(a) * r0s);
        ctx.lineTo(cx + Math.cos(a) * (r0s + len),
          cx + Math.sin(a) * (r0s + len));
        ctx.stroke();
      }
      return;
    }

    if (st === 'agent') {
      /* 代办进行时：3 颗彗星顺行 + 进度弧（orbMode('agent',true,p) 才画弧） */
      var colA = ORB.dark ? '147,197,253' : '29,78,216';
      var rA = 31 * k * (0.5 + 0.5 * ORB.bloom);
      if (ORB.agentP >= 0) {
        ctx.strokeStyle = 'rgba(' + colA + ',.5)';
        ctx.lineWidth = 2.2 * k;
        ctx.beginPath();
        ctx.arc(cx, cx, rA, -1.5708, -1.5708 + ORB.agentP * 6.2832);
        ctx.stroke();
      }
      for (i = 0; i < 3; i++) {
        var hd = ts / 1000 * 1.9 + i * 2.0944;
        for (var tl = 0; tl < 7; tl++) {
          a = hd - tl * 0.07;
          al = (0.55 - tl * 0.07) * ORB.bloom;
          ctx.fillStyle = 'rgba(' + colA + ',' + al.toFixed(3) + ')';
          ctx.beginPath();
          ctx.arc(cx + Math.cos(a) * rA, cx + Math.sin(a) * rA,
            (2.2 - tl * 0.22) * k, 0, 6.2832);
          ctx.fill();
        }
      }
      return;
    }

    /* thinking：紫色星系旋涡（椭圆轨道），流式 delta 到达即加速 */
    var colT = ORB.dark ? '196,181,253' : '124,58,237';
    var spd = 1 + ORB.pulse * 2.2;
    for (i = 0; i < n; i++) {
      ORB.pa[i] += (0.018 + 0.02 * ORB.ps[i]) * spd * d;
      a = ORB.pa[i];
      /* 轨道 30-42px：球半径 23px，旋涡必须环出球缘才看得见（画布在球后层） */
      r = (30 + 12 * (0.5 + 0.5 * Math.sin(ts / 900 + ORB.pr[i] * 6.283)) +
        ORB.pulse * 6) * mul;
      x = cx + Math.cos(a) * r;
      y = cx + Math.sin(a) * r * 0.82;
      al = (0.14 + 0.4 * ORB.pr[i]) * ORB.bloom;
      ctx.fillStyle = 'rgba(' + colT + ',' + al.toFixed(3) + ')';
      ctx.beginPath();
      ctx.arc(x, y, ORB.pz[i] * k * 0.8, 0, 6.2832);
      ctx.fill();
    }
  }

  function orbInit() {
    ORB.lvl = orbLevel();
    $wrap.classList.add('asb-lv' + ORB.lvl);
    document.addEventListener('visibilitychange', function () {
      if (document.hidden) { orbStop(); } else { syncOrb(); }
    });
    orbWatchSiblings();
    orbWireTilt();
    syncOrb();
    orbBeacon('asb_orb_boot_lv' + ORB.lvl);
  }

  /* ────────────────────────────────────────────── 位置（拖拽 + 记忆） */
  function loadPos() {
    try {
      var raw = localStorage.getItem('asb_pos_v1');
      if (raw) { return JSON.parse(raw); }
    } catch (e) { /* ignore */ }
    return null;
  }
  function savePos(p) {
    try { localStorage.setItem('asb_pos_v1', JSON.stringify(p)); }
    catch (e) { /* ignore */ }
  }
  function applyPos(p) {
    var w = 46, m = 12;
    var x = Math.min(Math.max(p.x, m), window.innerWidth - w - m);
    var y = Math.min(Math.max(p.y, m), window.innerHeight - w - m);
    $wrap.style.left = x + 'px';
    $wrap.style.top = y + 'px';
    $wrap.style.right = 'auto';
    $wrap.style.bottom = 'auto';
  }
  function defaultPos() {
    /* 默认右下角；工作台窄屏让开 56px 底部平台条 */
    var y = window.innerHeight - 46 - (window.innerWidth <= 480 ? 76 : 20);
    return { x: window.innerWidth - 46 - 18, y: y };
  }
  /* ── 面板几何：自由拖拽 + 八向缩放（P1 2026-08-29）──────────────────────
     存了自定义位置就用自定义的，没存才回落「锚在球的对角象限」的老行为。
     这个回落是刻意的：多数人从不拖窗，默认体验一个字都不该变。 */
  var PANEL_KEY = 'asb_panel_v1';
  var PANEL_MIN_W = 300, PANEL_MIN_H = 260;
  var SNAP_PX = 18;          /* 贴边吸附命中半径 */

  function panelNarrow() {
    /* 与 CSS 断点同值（那里强制全宽贴底）——窄屏整套拖拽都不接管 */
    return window.innerWidth <= 480;
  }
  function loadPanelBox() {
    try {
      var b = JSON.parse(localStorage.getItem(PANEL_KEY) || 'null');
      if (b && isFinite(b.x) && isFinite(b.y) && isFinite(b.w) && isFinite(b.h)) {
        return b;
      }
    } catch (e) { /* ignore */ }
    return null;
  }
  function savePanelBox(b) {
    try { localStorage.setItem(PANEL_KEY, JSON.stringify(b)); }
    catch (e) { /* ignore */ }
  }
  function clearPanelBox() {
    try { localStorage.removeItem(PANEL_KEY); } catch (e) { /* ignore */ }
  }
  /* 夹回视口：窗口变小/换显示器后，存的坐标可能把面板整个推到屏幕外——
     那会变成「点了没反应」的幽灵故障，所以每次应用都夹一遍。 */
  function clampBox(b) {
    var maxW = window.innerWidth - 16, maxH = window.innerHeight - 16;
    var w = Math.max(PANEL_MIN_W, Math.min(b.w, maxW));
    var h = Math.max(PANEL_MIN_H, Math.min(b.h, maxH));
    var x = Math.max(8, Math.min(b.x, window.innerWidth - w - 8));
    var y = Math.max(8, Math.min(b.y, window.innerHeight - h - 8));
    return { x: x, y: y, w: w, h: h };
  }
  function applyPanelBox(b) {
    var c = clampBox(b);
    $panel.style.left = c.x + 'px';
    $panel.style.top = c.y + 'px';
    $panel.style.width = c.w + 'px';
    $panel.style.height = c.h + 'px';
    return c;
  }
  function currentBox() {
    var r = $panel.getBoundingClientRect();
    return { x: r.left, y: r.top, w: r.width, h: r.height };
  }
  /* 贴边吸附：只吸「移动」不吸「缩放」——缩放时吸边会让人抓不住想要的尺寸。 */
  function snapBox(b) {
    var vw = window.innerWidth, vh = window.innerHeight, hit = false;
    if (Math.abs(b.x - 8) <= SNAP_PX) { b.x = 8; hit = true; }
    if (Math.abs(vw - (b.x + b.w) - 8) <= SNAP_PX) { b.x = vw - b.w - 8; hit = true; }
    if (Math.abs(b.y - 8) <= SNAP_PX) { b.y = 8; hit = true; }
    if (Math.abs(vh - (b.y + b.h) - 8) <= SNAP_PX) { b.y = vh - b.h - 8; hit = true; }
    return hit;
  }

  function placePanel() {
    if (!panelNarrow()) {
      var saved = loadPanelBox();
      if (saved) { applyPanelBox(saved); return; }
      /* 没有自定义几何：清掉可能残留的行内尺寸，交还 CSS 默认 */
      $panel.style.width = '';
      $panel.style.height = '';
    }
    /* 面板锚在球的对角象限，贴边自适应 */
    var r = $ball.getBoundingClientRect();
    var pw = Math.min(380, window.innerWidth - 24);
    var ph = Math.min(560, window.innerHeight * 0.78);
    var left = r.left + r.width / 2 < window.innerWidth / 2
      ? r.left : r.right - pw;
    left = Math.min(Math.max(left, 12), window.innerWidth - pw - 12);
    var top = r.top + r.height / 2 < window.innerHeight / 2
      ? r.bottom + 10 : r.top - ph - 10;
    top = Math.min(Math.max(top, 12), window.innerHeight - ph - 12);
    $panel.style.left = left + 'px';
    $panel.style.top = top + 'px';
  }

  /* ── 拖拽/缩放引擎 ─────────────────────────────────────────────────────
     照抄 sidebar-chrome 的成熟形态（指针捕获 + 全屏遮罩 + rAF），三处都不是
     装饰：① 指针捕获保证手指/鼠标滑出面板甚至滑出窗口仍收得到事件；
     ② 遮罩挡住底下的 iframe/webview——工作台里嵌着 webview，没有遮罩时指针
     一旦滑进去，事件就被它吃掉、面板卡在半路；③ rAF 合帧，避免一次移动里
     写几十遍布局。遮罩自带自愈：万一 onUp 没走到而残留，下次点击自清，
     绝不把整个界面锁死。 */
  var _rsShield = null, _rsRaf = 0, _rsPend = null;

  function rsShield(on, cur) {
    if (on) {
      if (!_rsShield) {
        _rsShield = document.createElement('div');
        _rsShield.setAttribute('aria-hidden', 'true');
        _rsShield.addEventListener('pointerdown', function () {
          if (!_rsShield.__busy) { rsShield(false); }
        });
      }
      _rsShield.style.cssText = 'position:fixed;inset:0;z-index:2147483000;' +
        'background:transparent;touch-action:none;cursor:' + (cur || 'move');
      _rsShield.__busy = true;
      if (!_rsShield.parentNode) { document.body.appendChild(_rsShield); }
    } else if (_rsShield) {
      _rsShield.__busy = false;
      if (_rsShield.parentNode) { _rsShield.parentNode.removeChild(_rsShield); }
    }
  }
  function rsFlush() {
    _rsRaf = 0;
    if (!_rsPend) { return; }
    var b = _rsPend; _rsPend = null;
    applyPanelBox(b);
  }
  function rsQueue(b) {
    _rsPend = b;
    if (!_rsRaf) { _rsRaf = requestAnimationFrame(rsFlush); }
  }

  /* dir='' 表示整窗移动；否则是 n/s/e/w 的任意组合（八向）。 */
  function startPanelGesture(e, dir, node) {
    if (panelNarrow()) { return; }
    if (e.button != null && e.button !== 0) { return; }
    var start = currentBox();
    var sx = e.clientX, sy = e.clientY;
    var moving = !dir;
    var moved = false;   /* 见 onUp：没真移动过就不落盘 */
    var cur = moving ? 'grabbing'
      : (node && node.style ? window.getComputedStyle(node).cursor : 'move');
    $panel.classList.add('asb-moving');
    rsShield(true, cur);
    try { node.setPointerCapture(e.pointerId); } catch (e1) { /* ignore */ }

    function onMove(ev) {
      var dx = ev.clientX - sx, dy = ev.clientY - sy;
      var b;
      /* 3px 死区：手抖不算拖动，否则「点一下标题栏」也会被当成自定义位置 */
      if (!moved && (Math.abs(dx) > 3 || Math.abs(dy) > 3)) { moved = true; }
      if (moving) {
        b = { x: start.x + dx, y: start.y + dy, w: start.w, h: start.h };
        var hit = snapBox(b);
        $panel.classList.toggle('asb-snapped', hit);
      } else {
        b = { x: start.x, y: start.y, w: start.w, h: start.h };
        /* 西/北边拉伸要同时改原点：只改宽高会让对边跟着跑，手感完全不对 */
        /* 东/南方向就地封顶：交给 clampBox 去夹的话，它为了不出界会**上推
           原点**，表现成「拉右下角结果整个窗往上跳」（G4b 实测 y 跳了 102px）。
           缩放的心理契约是「按住的那个角跟手、对角钉死」，宁可拉不动也不能跳。 */
        if (dir.indexOf('e') > -1) {
          b.w = Math.min(start.w + dx, window.innerWidth - start.x - 8);
        }
        if (dir.indexOf('s') > -1) {
          b.h = Math.min(start.h + dy, window.innerHeight - start.y - 8);
        }
        if (dir.indexOf('w') > -1) {
          b.w = start.w - dx;
          if (b.w < PANEL_MIN_W) { b.w = PANEL_MIN_W; }
          b.x = start.x + (start.w - b.w);
        }
        if (dir.indexOf('n') > -1) {
          b.h = start.h - dy;
          if (b.h < PANEL_MIN_H) { b.h = PANEL_MIN_H; }
          b.y = start.y + (start.h - b.h);
        }
      }
      rsQueue(b);
      if (ev.cancelable) { ev.preventDefault(); }
    }
    function onUp() {
      node.removeEventListener('pointermove', onMove);
      node.removeEventListener('pointerup', onUp);
      node.removeEventListener('pointercancel', onUp);
      try { node.releasePointerCapture(e.pointerId); } catch (e2) { /* ignore */ }
      if (_rsRaf) { cancelAnimationFrame(_rsRaf); _rsRaf = 0; rsFlush(); }
      $panel.classList.remove('asb-moving', 'asb-snapped');
      rsShield(false);
      /* **没真的动过就不落盘**，两个理由都不是洁癖：
         ① 单击标题栏（很常见）会把当时的位置固化成「自定义几何」，面板从此
            不再跟随球，用户完全不知道自己做了什么；
         ② 双击复位时，两次 pointerup 各存一遍，其中一次落在 dblclick 之后，
            把刚清掉的存储又写回去——复位看起来「点了没反应」（G7 实测）。 */
      if (!moved) { return; }
      /* 存**落地后的真实几何**（clamp/min 都已生效），别存计算中间值 */
      savePanelBox(currentBox());
      syncGeoReset();
      beacon(moving ? 'asb_panel_move' : 'asb_panel_resize');
    }
    node.addEventListener('pointermove', onMove);
    node.addEventListener('pointerup', onUp);
    node.addEventListener('pointercancel', onUp);
    /* 刻意**不**在 pointerdown 上 preventDefault：那会阻断浏览器合成 click /
       dblclick，双击标题栏复位就永远收不到（G7 实测）。文本选中由
       `.asb-moving{user-select:none}` 压住，移动中再 preventDefault 即可。 */
  }

  /* 键盘可达（无障碍不是可选项：手柄能 Tab 到就必须能用键盘操作）。
     方向键 16px 一档，Shift 加速到 48px，Esc 复位。 */
  function onHandleKey(e, dir) {
    var step = e.shiftKey ? 48 : 16;
    var b = currentBox(), used = true;
    var dx = e.key === 'ArrowRight' ? step : (e.key === 'ArrowLeft' ? -step : 0);
    var dy = e.key === 'ArrowDown' ? step : (e.key === 'ArrowUp' ? -step : 0);
    if (e.key === 'Escape') { resetPanelBox(); return true; }
    if (!dx && !dy) { return false; }
    if (!dir) { b.x += dx; b.y += dy; }
    else {
      if (dir.indexOf('e') > -1) { b.w += dx; }
      if (dir.indexOf('s') > -1) { b.h += dy; }
      if (dir.indexOf('w') > -1) { b.w -= dx; b.x += dx; }
      if (dir.indexOf('n') > -1) { b.h -= dy; b.y += dy; }
    }
    savePanelBox(applyPanelBox(b));
    if (used && e.preventDefault) { e.preventDefault(); }
    return true;
  }

  function resetPanelBox() {
    clearPanelBox();
    $panel.style.width = '';
    $panel.style.height = '';
    placePanel();
    syncGeoReset();
    beacon('asb_panel_reset');
  }

  /* 复位按钮只在「几何被改过」时露出（没改过就没有可复位的东西）。
     每次拖拽落地与每次渲染都同步一次，别让它与真实状态脱节。 */
  function syncGeoReset() {
    try {
      var b = $panel.querySelector('.asb-hd-rst');
      if (b) { b.hidden = !loadPanelBox(); }
    } catch (e) { /* ignore */ }
  }

  /* 自己认双击，不等浏览器合成 dblclick。两个实测理由：
     ① 拖拽用了 setPointerCapture，两次点击的 target 会被重定向到捕获元素，
        Chromium 判定「不是同一个目标」就**根本不合成 dblclick**——真实鼠标
        序列下复位永远不触发（合成事件却能过，所以静态/合成测试看不出来）；
     ② 触摸屏对 dblclick 的支持本来就参差，自己数更稳。 */
  var _tapT = 0, _tapX = 0, _tapY = 0;
  function isDoubleTap(e) {
    var now = Date.now();
    var dbl = (now - _tapT) < 400 &&
      Math.abs(e.clientX - _tapX) < 10 && Math.abs(e.clientY - _tapY) < 10;
    _tapT = dbl ? 0 : now;   /* 认定后清零，防三击被当成第二次双击 */
    _tapX = e.clientX; _tapY = e.clientY;
    return dbl;
  }

  function wirePanelGestures() {
    /* 事件委托：renderPanel() 每次重写 innerHTML，直接绑元素会在重渲染后失效 */
    $panel.addEventListener('pointerdown', function (e) {
      var h = e.target && e.target.closest ? e.target.closest('.asb-rs') : null;
      if (h) { startPanelGesture(e, h.getAttribute('data-rs') || 'se', h);
               return; }
      var hd = e.target && e.target.closest ? e.target.closest('.asb-hd') : null;
      /* 标题栏里的按钮（关闭/配对）不能变成拖拽把手 */
      if (hd && !(e.target.closest && e.target.closest('button'))) {
        if (isDoubleTap(e)) { resetPanelBox(); return; }
        startPanelGesture(e, '', hd);
      }
    });
    $panel.addEventListener('keydown', function (e) {
      var h = e.target && e.target.closest ? e.target.closest('.asb-rs') : null;
      if (h) { onHandleKey(e, h.getAttribute('data-rs') || 'se'); return; }
      if (e.target && e.target.classList &&
          e.target.classList.contains('asb-hd')) { onHandleKey(e, ''); }
    });
    /* 双击标题栏＝复位（与侧栏宽度手柄同一手势语义，不必另学） */
    $panel.addEventListener('dblclick', function (e) {
      if (e.target && e.target.closest && e.target.closest('.asb-hd') &&
          !e.target.closest('button')) { resetPanelBox(); }
    });
  }

  /* ────────────────────────────────────────────── DOM 构建 */
  function buildDom() {
    $wrap = document.createElement('div');
    $wrap.className = 'asb-wrap';
    $ball = document.createElement('button');
    $ball.type = 'button';
    $ball.className = 'asb-ball';
    $ball.setAttribute('aria-label', t('open_aria'));
    $ball.setAttribute('aria-haspopup', 'dialog');
    $ball.setAttribute('aria-expanded', 'false');
    $ball.innerHTML = '<span class="asb-glow"></span>' +
      '<span class="asb-orb"></span><span class="asb-alertfx"></span>' +
      '<span class="asb-sweep"></span><span class="asb-ping"></span>' +
      '<span class="asb-ic">' + ICON_SPARK + '</span>' +
      '<span class="asb-dot"></span>';
    $wrap.appendChild($ball);
    document.body.appendChild($wrap);

    $panel = document.createElement('div');
    $panel.className = 'asb-panel';
    $panel.setAttribute('role', 'dialog');
    $panel.setAttribute('aria-label', dispName());
    document.body.appendChild($panel);

    applyPos(loadPos() || defaultPos());
    window.addEventListener('resize', function () {
      applyPos(loadPos() || defaultPos());
      if (S.open) { placePanel(); }
    });
    wireDrag();
    wirePanelGestures();
    wireGlobal();
    document.addEventListener('asb-ask', function (e) {
      var q = e && e.detail ? e.detail.q : '';
      askFromOutside(q);
    });
    renderPanel();
  }

  function dispName() {
    var b = S.boot || {};
    return String(b.name || '').trim() ||
      (String(b.brand || '').trim() ? b.brand + ' · ' + t('name') : t('name'));
  }

  function wireDrag() {
    var sx = 0, sy = 0, ox = 0, oy = 0, moved = false, dragging = false;
    $ball.addEventListener('pointerdown', function (e) {
      dragging = true; moved = false;
      sx = e.clientX; sy = e.clientY;
      var r = $wrap.getBoundingClientRect();
      ox = r.left; oy = r.top;
      $ball.setPointerCapture(e.pointerId);
    });
    $ball.addEventListener('pointermove', function (e) {
      if (!dragging) { return; }
      var dx = e.clientX - sx, dy = e.clientY - sy;
      if (Math.abs(dx) + Math.abs(dy) > 6) { moved = true; }
      if (moved) { applyPos({ x: ox + dx, y: oy + dy }); }
    });
    $ball.addEventListener('pointerup', function () {
      if (!dragging) { return; }
      dragging = false;
      if (moved) {
        var r = $wrap.getBoundingClientRect();
        /* 吸边：贴近的水平边 */
        var x = r.left + r.width / 2 < window.innerWidth / 2
          ? 12 : window.innerWidth - r.width - 12;
        var p = { x: x, y: r.top };
        applyPos(p); savePos(p);
        if (S.open) { placePanel(); }
      } else {
        togglePanel();
      }
    });
  }

  /* src='ext' = 程序化开面板（教学/代办线投问），不计入「人点开了球」的
     分母——混进去会让打开率虚高，而打开率正是本批要建立的基线。 */
  function togglePanel(force, src) {
    var wasOpen = S.open;
    S.open = force != null ? !!force : !S.open;
    if (!S.open && REC) { stopRec(true); }
    $panel.classList.toggle('open', S.open);
    $ball.setAttribute('aria-expanded', S.open ? 'true' : 'false');
    if (S.open && !wasOpen) {
      beacon(src === 'ext' ? 'asb_open_ext' : 'asb_open');
    }
    if (S.open) {
      placePanel();
      /* 面板隐藏时 offsetWidth 恒 0 → 滑块只能在开的这一刻定位 */
      syncModes();
      loadPair();
      loadPc();
      if (S.tab === 'mine') { loadTickets(); }
      var inp = $panel.querySelector('.asb-in');
      if (inp && window.innerWidth > 480) {
        setTimeout(function () { inp.focus(); }, 60);
      }
    }
  }

  /* 教学/代办线投问：球开合走 pointerup 不是 click，HTMLElement.click()
     打不开面板（2026-08-23「让小智详细讲讲」空点事故）。本入口同步开面板、
     切对话 tab、走 sendQuery——DOM 事件 asb-ask 与 AssistantBall.ask 同源。 */
  function askFromOutside(q) {
    q = String(q == null ? '' : q).trim();
    if (!q) { return false; }
    togglePanel(true, 'ext');
    if (S.tab !== 'chat') { switchTab('chat', true); }
    sendQuery(q);
    return true;
  }

  function wireGlobal() {
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && S.open) { togglePanel(false); }
    });
    /* 报障 tab 激活时接收粘贴截图 */
    document.addEventListener('paste', function (e) {
      if (!S.open || S.tab !== 'report') { return; }
      var items = (e.clipboardData || {}).items || [];
      for (var i = 0; i < items.length; i++) {
        if (items[i].type && items[i].type.indexOf('image/') === 0) {
          var f = items[i].getAsFile();
          if (f) { readShot(f); e.preventDefault(); break; }
        }
      }
    });
  }

  /* ─────────────────────────────── 模式注册表（实施73 P1-1）
     「小智怎么帮我」升格为一级导航：问答由球自带，教学 / 替我做由姊妹组件
     在自己 init 时经 `AssistantBall.registerMode` 认领。组件缺席＝该模式
     **不出现**（而不是留一个点不动的死格），单模式时整条模式条隐藏，退化
     成旧的纯问答面板。与 orbMode/ask 同属公共 API 层——姊妹线仍不触碰球的
     内部符号，只是协作面从「往面板里插一行」升级成「认领一个模式」。 */
  var MODES = {};
  var MODE_ORDER = [];
  var SUBTABS = ['report', 'mine', 'faq', 'set'];

  function defineMode(id, def) {
    if (!MODES[id]) { MODE_ORDER.push(id); }
    MODES[id] = {
      id: id,
      icon: String(def.icon || ''),
      label: typeof def.label === 'function' ? def.label : null,
      labelKey: String(def.labelKey || ''),
      order: Number(def.order) || 50,
      mount: def.mount,
      unmount: typeof def.unmount === 'function' ? def.unmount : null,
      composer: def.composer && typeof def.composer.submit === 'function'
        ? def.composer : null,
    };
    MODE_ORDER.sort(function (a, b) { return MODES[a].order - MODES[b].order; });
  }

  function registerMode(id, def) {
    id = String(id || '').trim();
    if (!id || !def || typeof def.mount !== 'function') { return false; }
    defineMode(id, def);
    if ($panel) {
      renderModes();
      /* 姊妹组件晚于球 init 注册：若当前正停在该模式（重开面板/深链），
         补挂一次内容——否则用户看到一个空模式直到手动再点一下。 */
      if (S.tab === id) { switchTab(id, true); }
    }
    return true;
  }

  function modeLabel(m) {
    if (m.label) {
      try { return String(m.label() || ''); } catch (e) { return m.id; }
    }
    return m.labelKey ? t(m.labelKey) : m.id;
  }

  /* ────────────────────────────────────────────── 面板渲染 */
  /* 八向手柄。只有右下角进 Tab 序（tabindex=0）：八个都进会让键盘用户每次
     穿过面板都要按八下 Tab，而缩放用一个角就够——其余留给指针操作。 */
  function rsHandlesHtml() {
    var dirs = ['n', 's', 'w', 'e', 'nw', 'ne', 'sw', 'se'];
    var out = '';
    for (var i = 0; i < dirs.length; i++) {
      var d = dirs[i];
      var kb = d === 'se';
      out += '<div class="asb-rs ' + d + '" data-rs="' + d + '"' +
        (kb ? ' tabindex="0" role="separator" aria-orientation="vertical"' +
              ' aria-label="' + esc(t('resize_aria')) + '"'
            : ' aria-hidden="true"') + '></div>';
    }
    return out;
  }

  function renderPanel() {
    if (!MODES.chat) {
      defineMode('chat', { order: 10, icon: '💬', labelKey: 'mode_chat',
                           mount: mountChat });
    }
    $panel.innerHTML = '' +
      '<div class="asb-hd" tabindex="0" title="' + esc(t('move_hint')) + '">' +
      '<span class="asb-hd-ic">' + ICON_SPARK + '</span>' +
      '<span class="asb-hd-name">' + esc(dispName()) + '</span>' +
      /* 复位入口做成**看得见的按钮**而不是只靠双击：双击这类手势发现性极差
         （没人知道可以双击），且在指针捕获场景下浏览器未必合成 dblclick。
         只在几何被改过时出现——没自定义过就没有「复位」可言，常驻只是噪音。 */
      '<button type="button" class="asb-hd-rst" data-act="rstgeo" hidden' +
      ' title="' + esc(t('reset_geo')) + '" aria-label="' +
      esc(t('reset_geo')) + '">⤢</button>' +
      '<button type="button" class="asb-hd-pair" data-act="pair" title="' +
      esc(t('pair_off')) + '" aria-label="' + esc(t('pair_t')) +
      '">📱<i aria-hidden="true"></i></button>' +
      '<button type="button" class="asb-hd-pc" data-act="pc" hidden title="' +
      esc(t('pc_off')) + '" aria-label="' + esc(t('pc_t')) +
      '">💻<i aria-hidden="true"></i></button>' +
      '<button type="button" class="asb-x" data-act="close" aria-label="' +
      esc(t('close')) + '">✕</button></div>' +
      '<div class="asb-modes" role="radiogroup" aria-label="' +
      esc(t('modes_aria')) + '"></div>' +
      '<div class="asb-body" aria-live="polite"></div>' +
      '<div class="asb-hero"><canvas aria-hidden="true"></canvas></div>' +
      '<div class="asb-ftwrap"></div>' +
      '<div class="asb-subtabs"></div>' +
      rsHandlesHtml();
    syncGeoReset();
    $panel.addEventListener('click', onPanelClick);
    $panel.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey &&
          e.target && e.target.classList.contains('asb-in')) {
        /* 中文输入法选字回车 ≠ 发送（isComposing/229 守卫，2026-08-23——
           没这行，拼音候选一确认就把半截话发出去） */
        if (e.isComposing || e.keyCode === 229) { return; }
        e.preventDefault();
        composerSubmit();
        return;
      }
      if (e.target && e.target.classList.contains('asb-mode')) {
        onModeKey(e);
      }
    });
    $panel.addEventListener('input', function (e) {
      if (e.target && e.target.classList.contains('asb-in')) {
        autoGrow(e.target);
      }
    });
    renderModes();
    renderSubtabs();
    switchTab('chat', true);
    syncPair();
    syncPc();
  }

  function reportOn() {
    return !S.boot || S.boot.report_enabled !== false;
  }

  /* ── 模式条 ── */
  function renderModes() {
    var box = $panel && $panel.querySelector('.asb-modes');
    if (!box) { return; }
    var html = '<span class="asb-modes-ind" aria-hidden="true"></span>';
    for (var i = 0; i < MODE_ORDER.length; i++) {
      var m = MODES[MODE_ORDER[i]];
      html += '<button type="button" class="asb-mode" role="radio" ' +
        'aria-checked="false" tabindex="-1" data-mode="' + esc(m.id) + '">' +
        (m.icon ? '<i aria-hidden="true">' + esc(m.icon) + '</i>' : '') +
        '<span>' + esc(modeLabel(m)) + '</span></button>';
    }
    box.innerHTML = html;
    /* 单模式＝没得切，段控只会显得像个坏掉的开关 */
    box.classList.toggle('solo', MODE_ORDER.length < 2);
    syncModes();
  }

  function syncModes() {
    var box = $panel && $panel.querySelector('.asb-modes');
    if (!box) { return; }
    var btns = box.querySelectorAll('.asb-mode');
    var cur = null;
    for (var i = 0; i < btns.length; i++) {
      var on = btns[i].getAttribute('data-mode') === S.tab;
      btns[i].setAttribute('aria-checked', on ? 'true' : 'false');
      btns[i].tabIndex = on ? 0 : -1;
      if (on) { cur = btns[i]; }
    }
    var ind = box.querySelector('.asb-modes-ind');
    if (!ind) { return; }
    /* 面板未开时 offsetWidth 恒 0——滑块位置在 togglePanel 打开时补算 */
    if (!cur || !cur.offsetWidth) { ind.style.opacity = '0'; return; }
    ind.style.opacity = '1';
    ind.style.width = cur.offsetWidth + 'px';
    ind.style.transform = 'translateX(' + cur.offsetLeft + 'px)';
  }

  /* 段控无障碍：方向键在模式间移动并即时切换（radiogroup 标准行为） */
  function onModeKey(e) {
    var d = 0;
    if (e.key === 'ArrowRight' || e.key === 'ArrowDown') { d = 1; }
    if (e.key === 'ArrowLeft' || e.key === 'ArrowUp') { d = -1; }
    if (!d) { return; }
    var i = MODE_ORDER.indexOf(S.tab);
    if (i < 0) { i = 0; }
    var next = MODE_ORDER[(i + d + MODE_ORDER.length) % MODE_ORDER.length];
    e.preventDefault();
    switchTab(next);
    var btn = $panel.querySelector('.asb-mode[data-mode="' + next + '"]');
    if (btn) { btn.focus(); }
  }

  /* ── 次级页签行：报障 · 我的 · 常问 · ⚙（老板拍板的顺序） ── */
  function renderSubtabs() {
    var box = $panel && $panel.querySelector('.asb-subtabs');
    if (!box) { return; }
    var html = '';
    if (reportOn()) {
      html += '<button type="button" class="asb-sub" data-tab="report">' +
        esc(t('tab_report')) + '</button>' +
        '<button type="button" class="asb-sub" data-tab="mine">' +
        esc(t('tab_mine')) + '</button>';
    }
    html += '<button type="button" class="asb-sub" data-tab="faq">' +
      esc(t('tab_faq')) + '</button>' +
      '<span class="asb-sub-sp"></span>' +
      '<button type="button" class="asb-sub" data-tab="set" title="' +
      esc(t('set_t')) + '" aria-label="' + esc(t('set_t')) + '">⚙</button>';
    box.innerHTML = html;
    syncSubtabs();
  }

  function syncSubtabs() {
    var box = $panel && $panel.querySelector('.asb-subtabs');
    if (!box) { return; }
    var b = box.querySelectorAll('.asb-sub');
    for (var i = 0; i < b.length; i++) {
      b[i].classList.toggle('cur', b[i].getAttribute('data-tab') === S.tab);
    }
  }

  /* ── 标题栏手机操控状态点（P1-2）──
     手机是「替我做」的远程输入端，不是第四种能力（实施73 §3.1 已拍板）。
     配对态读既有 /api/assistant/pair/sessions；端点未装载/无权限=静默灰点。 */
  function syncPair() {
    var btn = $panel && $panel.querySelector('.asb-hd-pair');
    if (!btn) { return; }
    var on = S.pairN > 0;
    btn.classList.toggle('on', on);
    btn.title = on ? t('pair_on') : t('pair_off');
  }

  function loadPair() {
    if (S.pairBusy) { return; }
    S.pairBusy = true;
    fetch('/api/assistant/pair/sessions')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        S.pairN = (j && j.ok && j.sessions) ? j.sessions.length : 0;
        syncPair();
      })
      .catch(function () { /* 静默：状态点保持灰色 */ })
      .then(function () { S.pairBusy = false; });
  }

  /* ── 标题栏电脑操控状态点（实施91 P1-1）──
     电脑操控是一种能力，但同手机走「通道式」头栏入口（不占模式条第四格）。
     读 /api/assistant/pc/machines：enabled=false / 端点未装载 → 键保持隐藏；
     有在线受控机 → 绿点。绝不做死按钮：读不到就静默藏起来。 */
  function syncPc() {
    var btn = $panel && $panel.querySelector('.asb-hd-pc');
    if (!btn) { return; }
    if (!S.pcEnabled) { btn.hidden = true; return; }
    btn.hidden = false;
    var on = (S.pcOnlineN || 0) > 0;
    btn.classList.toggle('on', on);
    btn.title = on ? t('pc_on') : t('pc_off');
  }

  function loadPc() {
    if (S.pcBusy) { return; }
    S.pcBusy = true;
    fetch('/api/assistant/pc/machines')
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (j) {
        S.pcEnabled = !!(j && j.ok && j.enabled);
        var ms = (j && j.machines) || [];
        var n = 0;
        for (var i = 0; i < ms.length; i++) {
          if (ms[i] && !ms[i].revoked && ms[i].online) { n += 1; }
        }
        S.pcOnlineN = n;
        syncPc();
      })
      .catch(function () { /* 静默：键保持隐藏 */ })
      .then(function () { S.pcBusy = false; });
  }

  function modeApi(id) {
    return {
      lang: S.lang,
      shell: S.shell,
      ask: askFromOutside,
      close: function () { togglePanel(false); },
      /* 姊妹组件状态变了（如开/退教学）→ 让球重挂当前模式内容 */
      refresh: function () { if (S.tab === id) { switchTab(id, true); } },
      /* 把文本填进本模式的输入框并聚焦，**刻意不提交**（P1-3）：示例任务是
         引导，用户得先看清自己要说什么再按发送——尤其「替我做」会改设置。
         与 ask 同属公共 API 层，姊妹组件不必去摸球的 DOM。 */
      fill: function (text, focus) {
        return fillComposer(text, focus);
      },
    };
  }

  function fillComposer(text, focus) {
    var inp = $panel && $panel.querySelector('.asb-in');
    if (!inp) { return false; }
    inp.value = String(text == null ? '' : text);
    autoGrow(inp);
    if (focus !== false) { inp.focus(); }
    return true;
  }

  function renderTools() {
    var box = $panel.querySelector('.asb-tools');
    if (!box) { return; }
    if (S.shell !== 'admin') {
      /* workspace 壳没有 admin 全局工具，但「动效」档是坐席自己的注意力权：
         8 小时班盯屏的人必须能就地调安静（P2 起不再整行隐藏） */
      box.innerHTML =
        '<button type="button" class="asb-tool" data-act="orb-fx">' +
        esc(ORB.lvl < 2 ? t('orb_fx_calm') : t('orb_fx_full')) + '</button>';
      return;
    }
    var html = '';
    /* 2026-08-21 帮助球退役：术语提示开关首选全局函数 window.toggleTermTips
       （base.html 提供）；旧 .tip-toggle 点击仅作过渡兼容。 */
    if (typeof window.toggleTermTips === 'function' ||
        document.querySelector('.tip-toggle')) {
      var on = localStorage.getItem('tipOn') !== '0';
      html += '<button type="button" class="asb-tool" data-act="tips">' +
        esc(on ? t('tools_tips_on') : t('tools_tips_off')) + '</button>';
    }
    if (typeof window.startTour === 'function') {
      html += '<button type="button" class="asb-tool" data-act="tour">▶ ' +
        esc(t('tools_tour')) + '</button>';
    }
    if (typeof window.openShortcuts === 'function') {
      html += '<button type="button" class="asb-tool" data-act="keys">⌨ ' +
        esc(t('tools_keys')) + '</button>';
    }
    if (typeof window.openCmdPalette === 'function') {
      html += '<button type="button" class="asb-tool" data-act="cmd">' +
        esc(t('tools_cmd')) + ' (Ctrl+K)</button>';
    }
    /* 「上传诊断给客服」（实施49 P1-9 的独立诊断上传面板）——帮助球退役后
       由助手面板接住这个入口，防止孤儿化。 */
    if (typeof window.openSupportPanel === 'function') {
      html += '<button type="button" class="asb-tool" data-act="support">🧰 ' +
        esc(t('tools_support')) + '</button>';
    }
    /* orb 动效档切换（安静模式）：写 localStorage，reduced-motion 恒压到 lv0 */
    html += '<button type="button" class="asb-tool" data-act="orb-fx">' +
      esc(ORB.lvl < 2 ? t('orb_fx_calm') : t('orb_fx_full')) + '</button>';
    box.innerHTML = html;
  }

  /* silent=true＝程序化切换（renderPanel 初始化 / 外部投问），不计埋点：
     初始化每次开面板必切一次 chat，混进去会让 asb_tab_chat 恒等于开面板数，
     「坐席主动去了哪个页签」的信号就被稀释没了。
     P1 起视图分两类：模式（问答/教学/替我做，计 asb_mode_*）与次级页签
     （报障/我的/常问/⚙，计 asb_tab_*）——两类的分布要分开读。 */
  function switchTab(tab, silent) {
    if (!MODES[tab] && SUBTABS.indexOf(tab) < 0) { tab = 'chat'; }
    if (!silent) { beacon((MODES[tab] ? 'asb_mode_' : 'asb_tab_') + tab); }
    var prev = S.tab;
    if (prev && prev !== tab && MODES[prev] && MODES[prev].unmount) {
      try { MODES[prev].unmount(); } catch (e0) { /* 姊妹组件异常不拖累球 */ }
    }
    S.tab = tab;
    syncModes();
    syncSubtabs();
    var body = $panel.querySelector('.asb-body');
    var ft = $panel.querySelector('.asb-ftwrap');
    if (REC) { stopRec(true); }
    ft.innerHTML = '';
    body.innerHTML = '';
    if (MODES[tab]) {
      var host = body;
      if (tab !== 'chat') {
        host = document.createElement('div');
        host.className = 'asb-md';
        body.appendChild(host);
      }
      try {
        MODES[tab].mount(host, modeApi(tab));
      } catch (e1) {
        host.innerHTML = '<div class="asb-empty">' + esc(t('err_net')) + '</div>';
      }
      renderComposer(ft, MODES[tab]);
      return;
    }
    if (tab === 'report') { renderReport(body); return; }
    if (tab === 'faq') { renderFaq(body); return; }
    if (tab === 'set') { renderSettings(body); return; }
    body.innerHTML = '<div class="asb-mine-bar">' +
      '<button type="button" class="asb-tool" data-act="mine-refresh">↻ ' +
      esc(t('mine_refresh')) + '</button></div>' +
      '<div class="asb-mine-list"><div class="asb-empty">…</div></div>';
    loadTickets();
  }

  /* ── 问答模式内容（球自带的那一格） ── */
  function mountChat(body) {
    if (!chatHistory.length) { renderChatIntro(body); }
    restoreChat(body);
  }

  /* 输入区随模式变（P1-4）：问答=提问框走 sendQuery；带 composer 的模式
     （替我做=说一件要我做的事）走该模式自己的 submit；教学模式无输入区。 */
  function renderComposer(ft, mode) {
    var isChat = mode.id === 'chat';
    if (!isChat && !mode.composer) { return; }
    var ph = isChat ? t('input_ph') : composerPh(mode);
    /* 2026-08-21 老板拍板：不设字数限制（maxlength 移除） */
    ft.innerHTML = '<div class="asb-ft">' +
      '<textarea class="asb-in" rows="1" placeholder="' + esc(ph) +
      '"></textarea>' +
      ((isChat && voiceOn())
        ? '<button type="button" class="asb-mic" data-act="mic" ' +
          'title="' + esc(t('mic_title')) + '" aria-label="' +
          esc(t('mic_title')) + '">🎤</button>'
        : '') +
      '<button type="button" class="asb-send" data-act="send">' +
      esc(t('send')) + '</button></div>';
  }

  function composerPh(mode) {
    var c = mode.composer || {};
    if (typeof c.ph === 'function') {
      try { return String(c.ph() || ''); } catch (e) { return t('input_ph'); }
    }
    return String(c.ph || t('input_ph'));
  }

  /* 回车/发送键的单一出口：按当前模式分流（问答=问答链，其余=模式自己的
     提交）。别在别处再直调 sendQuery——那会让替我做模式的回车问到问答链去。 */
  function composerSubmit() {
    var mode = MODES[S.tab];
    if (!mode || mode.id === 'chat') { sendQuery(); return; }
    if (!mode.composer) { return; }
    var inp = $panel.querySelector('.asb-in');
    var v = inp ? String(inp.value || '').trim() : '';
    if (!v) { return; }
    if (inp) { inp.value = ''; inp.style.height = ''; }
    try { mode.composer.submit(v); } catch (e) { /* 姊妹组件异常不拖累球 */ }
  }

  /* ────────────────────────────────────────────── 对话 tab */
  var chatHistory = [];  /* [{cls,html}] 会话内重绘用（切 tab 保留） */
  var turns = [];        /* [{q,a}] 多轮上下文（随问答链送最近 3 轮） */

  /* 对话持久化（2026-08-23）：sessionStorage 24h——刷新/跳页不再丢对话。
     thinking 占位（asb-th-*）不入存储；quota/隐私模式一律静默。 */
  function saveChatStore() {
    try {
      var items = [];
      for (var i = 0; i < chatHistory.length; i++) {
        if (chatHistory[i].indexOf('asb-th-') === -1) {
          items.push(chatHistory[i]);
        }
      }
      sessionStorage.setItem('asb_chat_v1', JSON.stringify(
        { ts: Date.now(), items: items.slice(-40), turns: turns.slice(-6) }));
    } catch (e) { /* best-effort */ }
  }
  function loadChatStore() {
    try {
      var raw = sessionStorage.getItem('asb_chat_v1');
      if (!raw) { return; }
      var ent = JSON.parse(raw);
      if (!ent || Date.now() - (ent.ts || 0) > 24 * 3600 * 1000) {
        sessionStorage.removeItem('asb_chat_v1');
        return;
      }
      if (ent.items && ent.items.length) { chatHistory = ent.items; }
      if (ent.turns && ent.turns.length) { turns = ent.turns; }
    } catch (e) { /* ignore */ }
  }

  /* 问答模式首屏（P1-3）：欢迎三卡已删除——问功能=输入框、报障=次级页签、
     学操作=模式条第二格，三张卡是同一批入口的第三次重复，只是在挤首屏。
     chips 由 5 条减为 **3 条本页相关**，「全站高频」搬去「常问」面板。 */
  /* 「确定答得上」的问题池——首屏引导与拒答建议**共用这一个源**，两处各写
     一份必然漂移（一处推荐的问法另一处答不上，比不推荐更糟）。
     boot.chips 来自 qa_log 里 answered=1 且没被 👎 的高频问句，所以点进去
     必有结果；无历史时回落到三条**实测过分数**的问法（2026-08-28 用生产
     语料量过：中文 133/121/266、英文 38/55/49，全部过 min_score=35）。
     exclude＝刚问过的那句，避免「答不上来」之后又推荐同一句。 */
  function answerableChips(exclude) {
    var raw = (S.boot && S.boot.chips && S.boot.chips.length)
      ? S.boot.chips
      : (S.lang === 'en'
        ? ['How to send a voice message', 'How to report a bug',
           'How to clear inbox filters']
        : ['怎么发语音', '在哪提交 bug', '收件箱筛选怎么清空']);
    var ex = String(exclude == null ? '' : exclude).trim();
    var out = [];
    for (var i = 0; i < raw.length && out.length < 3; i++) {
      var c = String(raw[i] == null ? '' : raw[i]).trim();
      if (c && c !== ex) { out.push(c); }
    }
    return out;
  }

  /* 拒答时的出路（P1 2026-08-29）：与首屏 chip 同源、同点击链（data-q →
     sendQuery），所以零新 handler、零新样式。池子空就什么都不加。 */
  /* 依据徽标：doc 走既有「来源」块（引用得到具体条目），product/general 只能
     给一句出处说明——但**必须给**，否则用户无从判断这句话有多少分量。 */
  function basisHtml(basis) {
    var key = basis === 'general' ? 'basis_general'
      : (basis === 'product' ? 'basis_product' : '');
    if (!key) { return ''; }
    return '<div class="asb-basis' + (basis === 'general' ? ' gen' : '') +
      '">' + esc(t(key)) + '</div>';
  }

  /* 拒答建议（2026-08-29 P0-3）：优先用**本次真检索到的条目**（meta.sources），
     它们是离这个问题最近的东西，服务端本来就送过来了、前端此前直接丢掉；
     按页面热度推「怎么发语音」那种毫不相干的建议正是老板说的「没区别」。
     检索空了才回落到「确定答得上」的高频池。 */
  function suggestHtml(q, meta) {
    var near = [];
    try {
      var ss = (meta && meta.sources) || [];
      for (var k = 0; k < ss.length && near.length < 3; k++) {
        var ti = String((ss[k] && ss[k].title) || '').trim();
        if (ti) { near.push(ti); }
      }
    } catch (e0) { near = []; }
    if (near.length) {
      var hn = '<div class="asb-chips asb-sugg"><span class="asb-chips-t">' +
        esc(t('suggest_near_t')) + '</span>';
      for (var m = 0; m < near.length; m++) {
        hn += '<button type="button" class="asb-chip" data-sugg="1" data-q="' +
          esc(near[m]) + '">' + esc(near[m]) + '</button>';
      }
      return hn + '</div>';
    }
    var chips = answerableChips(q);
    if (!chips.length) { return ''; }
    /* data-sugg 有两个作用：埋点与首屏 chip 分流（保住 asb_chip「本页常问
       点击」的语义纯净），以及给门禁一个精确选择器——首屏也有 .asb-chips，
       只按 class 断言分不清是哪一组。 */
    var h = '<div class="asb-chips asb-sugg"><span class="asb-chips-t">' +
      esc(t('suggest_t')) + '</span>';
    for (var i = 0; i < chips.length; i++) {
      h += '<button type="button" class="asb-chip" data-sugg="1" data-q="' +
        esc(chips[i]) + '">' + esc(chips[i]) + '</button>';
    }
    return h + '</div>';
  }

  function renderChatIntro(body) {
    var chips = answerableChips('');
    var html = '<div class="asb-msg ai">' + mdLite(t('hello')) + '</div>' +
      '<div class="asb-chips"><span class="asb-chips-t">' +
      esc(t('chips_title')) + '</span>';
    for (var i = 0; i < chips.length; i++) {
      html += '<button type="button" class="asb-chip" data-q="' +
        esc(chips[i]) + '">' + esc(chips[i]) + '</button>';
    }
    html += '<button type="button" class="asb-chip" data-tab="faq">' +
      esc(t('chips_more')) + '</button></div>';
    body.insertAdjacentHTML('beforeend', html);
  }

  /* ────────────────────────────────────────────── 常问面板（P1-6）
     单一数据源＝真实问答日志（`qa_log`）+ 帮助库 BM25 搜索，刻意**不做**
     人工维护的 FAQ 库——人工表必然与实际漂移（实施73 §5 已拍板）。 */
  function renderFaq(body) {
    body.innerHTML = '<div class="asb-faq-s">' +
      '<input class="asb-faq-q" type="search" placeholder="' +
      esc(t('faq_ph')) + '" aria-label="' + esc(t('faq_ph')) + '"></div>' +
      '<div class="asb-faq-out"><div class="asb-empty">…</div></div>';
    var inp = body.querySelector('.asb-faq-q');
    if (inp) {
      var timer = 0;
      inp.addEventListener('input', function () {
        clearTimeout(timer);
        timer = setTimeout(function () { loadFaq(inp.value); }, 260);
      });
      if (window.innerWidth > 480) { setTimeout(function () { inp.focus(); }, 60); }
    }
    loadFaq('');
  }

  function loadFaq(q) {
    var out = $panel && $panel.querySelector('.asb-faq-out');
    if (!out) { return; }
    q = String(q || '').trim();
    if (q) { beacon('asb_faq_search'); }
    var url = '/api/assistant/faq?lang=' + encodeURIComponent(S.lang) +
      '&page=' + encodeURIComponent(pagePath()) +
      (q ? '&q=' + encodeURIComponent(q.slice(0, 120)) : '');
    fetch(url)
      .then(function (r) {
        /* 特性探测：前端热更新先于 .py 装载（共享树常态），此刻端点还是
           404。那是「后端待装载」不是「没人问过」——两句话该做的事完全
           不同，混成一句会让人以为功能坏了。 */
        if (r.status === 404) { return { __na: true }; }
        return r.ok ? r.json() : null;
      })
      .then(function (j) {
        if (!out.isConnected) { return; }
        if (j && j.__na) {
          out.innerHTML = '<div class="asb-empty">' + esc(t('faq_na')) +
            '</div>';
          return;
        }
        if (!j || !j.ok) {
          out.innerHTML = '<div class="asb-empty">' + esc(t('faq_empty')) +
            '</div>';
          return;
        }
        out.innerHTML = q ? faqSearchHtml(j) : faqTopHtml(j);
      })
      .catch(function () {
        if (out.isConnected) {
          out.innerHTML = '<div class="asb-empty">' + esc(t('err_net')) +
            '</div>';
        }
      });
  }

  function faqRow(q, n) {
    return '<button type="button" class="asb-faq-i" data-faq="' + esc(q) +
      '"><span class="q">' + esc(q) + '</span>' +
      (n > 1 ? '<span class="n">×' + n + '</span>' : '') + '</button>';
  }

  function faqTopHtml(j) {
    var pageItems = j.page_items || [];
    var globalItems = j.global_items || [];
    var seed = j.seed_items || [];
    var html = '';
    var i;
    if (pageItems.length) {
      html += '<div class="asb-faq-g">' + esc(t('faq_page')) + '</div>';
      for (i = 0; i < pageItems.length; i++) {
        html += faqRow(pageItems[i].q, pageItems[i].n);
      }
    }
    if (globalItems.length) {
      html += '<div class="asb-faq-g">' + esc(t('faq_global')) + '</div>';
      for (i = 0; i < globalItems.length; i++) {
        html += faqRow(globalItems[i].q, globalItems[i].n);
      }
    }
    if (!html && seed.length) {
      /* 空态 seed：新装机没有任何问答记录，给一组入门问题而不是空面板 */
      html += '<div class="asb-faq-g">' + esc(t('faq_seed')) + '</div>';
      for (i = 0; i < seed.length; i++) { html += faqRow(seed[i].q, 0); }
    }
    if (!html) {
      html = '<div class="asb-empty">' + esc(t('faq_empty')) + '</div>';
    }
    return html;
  }

  function faqSearchHtml(j) {
    var items = j.items || [];
    if (!items.length) {
      return '<div class="asb-empty">' + esc(t('faq_none')) + '</div>';
    }
    var html = '<div class="asb-faq-g">' + esc(t('faq_kb')) + '</div>';
    for (var i = 0; i < items.length; i++) {
      html += faqRow(items[i].title, 0);
    }
    return html;
  }

  /* ────────────────────────────────────────────── ⚙ 偏好与工具（P1-5）
     动效档从功能区移入此处——它是偏好不是功能，摆在主视觉里只会分散注意。 */
  function renderSettings(body) {
    body.innerHTML = '<div class="asb-faq-g">' + esc(t('set_t')) + '</div>' +
      '<div class="asb-tools"></div>';
    renderTools();
  }
  function restoreChat(body) {
    for (var i = 0; i < chatHistory.length; i++) {
      var el = document.createElement('div');
      el.innerHTML = chatHistory[i];
      while (el.firstChild) { body.appendChild(el.firstChild); }
    }
    body.scrollTop = body.scrollHeight;
  }
  function pushChat(html) {
    chatHistory.push(html);
    if (chatHistory.length > 60) { chatHistory.shift(); }
    var body = $panel.querySelector('.asb-body');
    if (S.tab === 'chat' && body) {
      body.insertAdjacentHTML('beforeend', html);
      body.scrollTop = body.scrollHeight;
    }
    saveChatStore();
  }

  function autoGrow(el) {
    /* 输入框随内容长高（1→6 行；上限内滚），发送后复位 */
    el.style.height = 'auto';
    el.style.height = Math.min(el.scrollHeight, 132) + 'px';
  }

  function setBusy(v) {
    S.busy = v;
    $ball.classList.toggle('busy', v);
    syncOrb();
    var btn = $panel.querySelector('.asb-send');
    if (!btn) { return; }
    if (v && S.abortCtl) {
      /* 生成期发送键变「停止」（问答链可中断；转写等短请求维持旧禁用态） */
      btn.disabled = false;
      btn.textContent = '⏹';
      btn.title = t('stop_t');
      btn.setAttribute('data-act', 'stop-gen');
    } else {
      btn.disabled = v;
      btn.textContent = t('send');
      btn.title = '';
      btn.setAttribute('data-act', 'send');
    }
  }

  function sendQuery(qArg) {
    if (S.busy) { return; }
    var inp = $panel.querySelector('.asb-in');
    var q = String(qArg != null ? qArg : (inp ? inp.value : '')).trim();
    if (!q) { return; }
    /* 问答主链的唯一计数点（chip / 输入框 / 外部投问都汇到这里）。
       来源归因由各入口自己的 asb_chip / asb_h3_* 承担，故此处不分流。 */
    beacon('asb_ask');
    if (inp && qArg == null) { inp.value = ''; inp.style.height = ''; }
    S.lastQ = q;
    S.aborted = false;
    S.abortCtl = null;
    try {
      if (window.AbortController) { S.abortCtl = new AbortController(); }
    } catch (e0) { S.abortCtl = null; }
    pushChat('<div class="asb-msg user">' + esc(q) + '</div>');
    var thinkId = 'asb-th-' + Date.now();
    pushChat('<div class="asb-msg ai" id="' + thinkId + '">' +
      esc(t('searching')) + '</div>');
    setBusy(true);

    /* 2026-08-21 老板拍板：不设秒数限制（50s abort 移除）；改为思考气泡
       每秒显示已等待时长——长答案时用户看得到进度，而不是死转圈。 */
    var tick0 = Date.now();
    var tick = setInterval(function () {
      var el = document.getElementById(thinkId);
      if (!el) { clearInterval(tick); return; }
      /* 首个 delta 到达后打字机接管气泡，计秒立即让位（否则互相覆写） */
      if (el.getAttribute('data-live') === '1') { clearInterval(tick); return; }
      var s = Math.round((Date.now() - tick0) / 1000);
      /* 8 秒后换成带预期的文案（P1-4）：qa_log 里存过 latency_ms=37638 的
         真实案例，长静默期用户需要知道「这是正常的」，而不是猜是否卡死。 */
      if (s >= 3) {
        el.textContent = (s >= 8 ? t('thinking_long') : t('thinking')) +
          ' ' + s + 's';
      }
    }, 1000);
    fetch('/api/assistant/query', {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ q: q, page: pagePath(), lang: S.lang,
                             ui_build: uiBuild(),
                             history: turns.slice(-3) }),
      signal: S.abortCtl ? S.abortCtl.signal : undefined,
    }).then(function (resp) {
      if (!resp.ok) {
        return resp.json().catch(function () { return {}; })
          .then(function (j) {
            throw new Error(String((j && j.detail) ||
              (resp.status === 429 ? t('rate_hint') : t('err_net'))));
          });
      }
      /* P0 服务端=JSON {ok,events:[...]}（流式外壳被压缩中间件掐断，已弃用）；
         P1 真流式换 SSE/ndjson 时本分支自动走流读——双模前端零重构。 */
      var ct = String(resp.headers.get('content-type') || '');
      if (ct.indexOf('json') >= 0 && ct.indexOf('ndjson') < 0) {
        return resp.json().then(function (j) {
          renderEvents((j && j.events) || [], thinkId, q);
        });
      }
      return readNdjson(resp, thinkId, q);
    }).catch(function (e) {
      if (S.aborted || (e && e.name === 'AbortError')) {
        /* 用户主动停止：已吐的内容保留成正式气泡，不算错误 */
        var el0 = document.getElementById(thinkId);
        var live = (el0 && el0.getAttribute('data-live') === '1')
          ? String(el0.textContent || '') : '';
        replaceThinking(thinkId,
          (live ? '<div class="asb-msg ai">' + mdLite(live) + '</div>' : '') +
          '<div class="asb-msg ai">⏹ ' + esc(t('stopped_gen')) + '</div>');
        return;
      }
      replaceThinking(thinkId,
        '<div class="asb-msg err">' + esc(e && e.message || t('err_net')) +
        '</div>' + errActionsHtml(q));
      orbFlash();
    }).then(function () {
      clearInterval(tick);
      S.abortCtl = null;
      setBusy(false);
    });
  }

  /* 错误态动作（P0-4）：只给「重试」是把用户关在死路里——机制性故障重试
     多少次都一样（2026-08-28 实录：坐席连点两次重试，两次同一个错）。报障
     复用既有 to-report 链（切报障页 + 预填问题原文），零新后端零新键位。 */
  function errActionsHtml(q) {
    return '<div class="asb-err-acts">' +
      '<button type="button" class="asb-act" data-act="retry">↻ ' +
      esc(t('retry')) + '</button>' +
      '<button type="button" class="asb-act" data-act="to-report" data-q="' +
      esc(String(q || S.lastQ || '')) + '">' + esc(t('err_report')) +
      '</button></div>';
  }

  function replaceThinking(thinkId, html) {
    var el = document.getElementById(thinkId);
    /* 同步 chatHistory：thinking 占位是最后一条 */
    for (var i = chatHistory.length - 1; i >= 0; i--) {
      if (chatHistory[i].indexOf(thinkId) !== -1) {
        chatHistory.splice(i, 1); break;
      }
    }
    if (el) { el.remove(); }
    pushChat(html);
  }

  /* 统一渲染：事件数组（JSON 一次到达或 ndjson 逐行收齐后）→ 消息气泡 */
  function renderEvents(evs, thinkId, q) {
    var meta = null, answer = '', done = null, errEv = null;
    for (var i = 0; i < evs.length; i++) {
      var ev = evs[i] || {};
      if (ev.ev === 'meta') { meta = ev; }
      else if (ev.ev === 'delta') { answer += String(ev.text || ''); }
      else if (ev.ev === 'done') { done = ev; }
      else if (ev.ev === 'err') { errEv = ev; }
    }
    if (errEv || (!answer && !done)) {
      /* meta 已到＝服务端收下并开始处理了，之后才断的（最常见是流在等 LLM
         首 token 的静默窗里被中间层掐断——见 assistant_routes 的 keepalive
         段）。这种情况说「网络异常」是在冤枉用户的网络，必须如实分开。 */
      var emsg = (errEv && errEv.text) ||
        (meta ? t('err_nostream') : t('err_net'));
      replaceThinking(thinkId,
        '<div class="asb-msg err">' + esc(emsg) + '</div>' +
        errActionsHtml(q));
      orbFlash();
      return;
    }
    /* 拒答（零命中 或 NO_BASIS 哨兵）判据——**必须在渲染来源之前算**：
       服务端的 meta 先于答案发出，所以哨兵路径下 sources 早就在线上了，
       但那些条目**恰恰是被判定为回答不了这个问题的**。照旧列出来会读成
       「它找到了却不肯说」——老板 2026-08-28 实录截图正是这一幕：一句
       「没有找到可靠依据」下面挂着「为什么提示『在此使用/保持待机』」。 */
    var noBasis = !!(done && done.answered === false);
    var html = '<div class="asb-msg ai">' + mdLite(answer) + '</div>';
    /* 无文档依据但答了（产品事实卡 / 通用知识）→ 标一行出处。doc 依据走下面
       的「来源」块，不重复标。 */
    if (!noBasis && done) { html += basisHtml(done.basis); }
    if (!noBasis && meta && meta.sources && meta.sources.length) {
      html += '<div class="asb-srcs"><span class="asb-srcs-t">' +
        esc(t('src_title')) + '</span>';
      for (var j = 0; j < meta.sources.length; j++) {
        var s = meta.sources[j];
        var path = String(s.path || '');
        var linkable = path &&
          (S.shell === 'admin' || path.indexOf('/workspace') === 0);
        html += '<div class="asb-src"><span class="n">[S' + (j + 1) +
          ']</span><span class="tt">' + esc(s.title || '') + '</span>' +
          (linkable ? '<a data-goto="' + esc(path) + '" data-anchor="' +
            esc(String(s.anchor || '')) + '">' +
            esc(t('goto')) + ' →</a>' : '') + '</div>';
      }
      html += '</div>';
    }
    var sayBtn = answer
      ? '<button type="button" class="asb-say" data-act="say" data-say="' +
        esc(sayPrep(answer)) + '" title="' + esc(t('say_t')) +
        '" aria-label="' + esc(t('say_t')) + '">🔊</button>'
      : '';
    var copyBtn = answer
      ? '<button type="button" class="asb-say" data-act="copy" title="' +
        esc(t('copy_t')) + '" aria-label="' + esc(t('copy_t')) +
        '">📋</button>'
      : '';
    if (done && done.answered && done.qa_id) {
      html += '<div class="asb-fb" data-qa="' + esc(String(done.qa_id)) +
        '">' + sayBtn + copyBtn +
        '<span class="asb-fbq"><span>' + esc(t('helpful')) + '</span>' +
        '<button type="button" data-fb="up">👍</button>' +
        '<button type="button" data-fb="down">👎</button></span></div>';
    } else if (sayBtn) {
      html += '<div class="asb-fb">' + sayBtn + copyBtn + '</div>';
    }
    /* 报障入口的两个来源（2026-08-27 补第二个）：
       ① meta.report_hint —— 问句里有故障词（报错/闪退…），答前就知道；
       ② done.answered === false —— 服务端判定「没依据」，**答完才知道**：
          零命中，或 LLM 自认参考条目回答不了（NO_BASIS 哨兵）。
       只看 ① 会漏掉「答不上来」这个最该给出路的时刻。
       （noBasis 已在上面来源渲染前算好。） */
    if (reportOn() && ((meta && meta.report_hint) || noBasis)) {
      html += '<button type="button" class="asb-act" data-act="to-report" ' +
        'data-q="' + esc(q) + '">' + esc(t('to_report')) + '</button>';
    }
    /* 报障是「这可能是故障」的出路；下面这排是「你现在就能得到答案」的出路。
       两者并存：答不上来的原因多半是语料没覆盖，而不是坏了。 */
    if (noBasis) { html += suggestHtml(q, meta); }
    replaceThinking(thinkId, html);
    if (done && done.answered && answer) {
      /* 多轮上下文素材（2026-08-23）：只记答成的轮次，随下问送最近 3 轮 */
      turns.push({ q: q, a: answer.slice(0, 600) });
      if (turns.length > 6) { turns.shift(); }
      saveChatStore();
    }
  }

  /* 流读取器（SSE `data: {...}` 帧 / 裸 ndjson 行双兼容，2026-08-21 P2）：
     delta 逐段实时渲染成打字机（textContent 赋值天然转义），流结束后再用
     renderEvents 的格式化版本（mdLite+来源卡+反馈行）原位替换。 */
  function readNdjson(resp, thinkId, q) {
    var reader = resp.body.getReader();
    var dec = new TextDecoder('utf-8');
    var buf = '';
    var evs = [];
    var live = '';
    var th = document.getElementById(thinkId);
    if (th) { th.textContent = t('thinking'); }
    function handleLine(line) {
      line = line.trim();
      if (!line) { return; }
      if (line.indexOf('data:') === 0) { line = line.slice(5).trim(); }
      if (!line) { return; }
      var ev;
      try { ev = JSON.parse(line); } catch (e) { return; }
      evs.push(ev);
      if (ev.ev === 'delta' && ev.text) {
        live += String(ev.text);
        ORB.pulse = 1;  /* 思考旋涡随 token 到达加速：把流本身可视化 */
        var el = document.getElementById(thinkId);
        if (el) {
          el.setAttribute('data-live', '1');  /* 通知计秒 ticker 让位 */
          el.textContent = live;
          var body = el.closest('.asb-body');
          if (body) { body.scrollTop = body.scrollHeight; }
        }
      }
    }
    function pump() {
      return reader.read().then(function (r) {
        if (r.value) {
          buf += dec.decode(r.value, { stream: true });
          var idx;
          while ((idx = buf.indexOf('\n')) >= 0) {
            handleLine(buf.slice(0, idx));
            buf = buf.slice(idx + 1);
          }
        }
        if (!r.done) { return pump(); }
        handleLine(buf);
        renderEvents(evs, thinkId, q);
        return null;
      });
    }
    return pump();
  }

  /* ─────────────────────────────────────── 语音提问（P1，能力探测自动隐藏） */
  var REC = null; /* {mr, stream, cancelled, cap} */
  function voiceOn() {
    return !!(S.boot && S.boot.voice && navigator.mediaDevices &&
      navigator.mediaDevices.getUserMedia && window.MediaRecorder);
  }
  function syncMicUi(on) {
    var b = $panel.querySelector('[data-act="mic"]');
    if (b) {
      b.classList.toggle('rec', on);
      b.title = on ? t('mic_rec') : t('mic_title');
    }
    var inp = $panel.querySelector('.asb-in');
    if (inp) { inp.placeholder = on ? t('mic_rec') : t('input_ph'); }
  }
  function micErr(msg) {
    pushChat('<div class="asb-msg err">' + esc(msg) + '</div>');
  }
  function stopRec(cancel) {
    if (!REC) { return; }
    REC.cancelled = !!cancel;
    try { REC.mr.stop(); } catch (e) {
      try {
        REC.stream.getTracks().forEach(function (tk) { tk.stop(); });
      } catch (e2) { /* ignore */ }
      REC = null;
      syncMicUi(false);
      orbAudioStop();
      syncOrb();
    }
  }
  function toggleRec() {
    if (REC) { stopRec(false); return; }
    sayStop();  /* 录音前停播报：防扬声器进麦（回声）+ 状态灯语义唯一 */
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (st) {
      var mime = '';
      try {
        if (MediaRecorder.isTypeSupported('audio/webm;codecs=opus')) {
          mime = 'audio/webm;codecs=opus';
        }
      } catch (e) { /* ignore */ }
      var mr;
      try {
        mr = mime ? new MediaRecorder(st, { mimeType: mime })
          : new MediaRecorder(st);
      } catch (e) {
        st.getTracks().forEach(function (tk) { tk.stop(); });
        micErr(t('mic_fail'));
        return;
      }
      var chunks = [];
      mr.ondataavailable = function (ev) {
        if (ev.data && ev.data.size) { chunks.push(ev.data); }
      };
      mr.onstop = function () {
        var cancelled = REC && REC.cancelled;
        if (REC && REC.cap) { clearTimeout(REC.cap); }
        st.getTracks().forEach(function (tk) { tk.stop(); });
        REC = null;
        syncMicUi(false);
        orbAudioStop();
        syncOrb();
        var blob = new Blob(chunks, { type: mr.mimeType || 'audio/webm' });
        if (cancelled || blob.size < 1200) { return; } /* 过短≈误触 */
        uploadRec(blob);
      };
      REC = { mr: mr, stream: st, cancelled: false,
              cap: setTimeout(function () { stopRec(false); }, 45000) };
      mr.start();
      syncMicUi(true);
      orbAudioStart(st);
      syncOrb();
    }).catch(function () { micErr(t('mic_denied')); });
  }
  function uploadRec(blob) {
    var rd = new FileReader();
    rd.onload = function () {
      setBusy(true);
      var inp0 = $panel.querySelector('.asb-in');
      if (inp0) { inp0.placeholder = t('mic_busy'); }
      fetch('/api/assistant/transcribe', {
        method: 'POST',
        headers: csrfHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ audio_b64: String(rd.result || '') }),
      }).then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (j) {
          if (!r.ok || !j.ok) {
            throw new Error(String(j.detail || t('mic_fail')));
          }
          var inp = $panel.querySelector('.asb-in');
          if (inp) {
            inp.value = String(j.text || '');
            inp.focus();
          }
        });
      }).catch(function (e) {
        micErr(e && e.message || t('mic_fail'));
      }).then(function () {
        setBusy(false);
        syncMicUi(false);
      });
    };
    rd.readAsDataURL(blob);
  }

  function copyAnswer(btn) {
    /* 复制最近的 AI 回答（2026-08-23）。LAN http 无 clipboard API →
       textarea+execCommand 兜底（这才是坐席环境的主路径）。 */
    var box = btn.closest('.asb-fb');
    var node = box ? box.previousElementSibling : null;
    while (node && !(node.classList && node.classList.contains('asb-msg') &&
                     node.classList.contains('ai'))) {
      node = node.previousElementSibling;
    }
    var txt = node ? String(node.innerText || '').trim() : '';
    if (!txt) { return; }
    var flash = function () {
      btn.textContent = '✓';
      setTimeout(function () { btn.textContent = '📋'; }, 1200);
    };
    var legacy = function () {
      try {
        var ta = document.createElement('textarea');
        ta.value = txt;
        ta.style.cssText = 'position:fixed;left:-9999px;opacity:0';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        ta.remove();
        flash();
      } catch (e) { /* ignore */ }
    };
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(txt).then(flash, legacy);
    } else { legacy(); }
  }

  function sendFeedback(qaId, verdict, box) {
    fetch('/api/assistant/feedback', {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ qa_id: Number(qaId), verdict: verdict }),
    }).catch(function () { /* best-effort */ });
    /* 只替换评价半区，保住同排的 🔊 播报按钮（P2 起 fb 行是复合行） */
    var q = box.querySelector('.asb-fbq');
    (q || box).innerHTML = '<span>' + esc(t('fb_thanks')) + '</span>';
  }

  /* ─────────────────────── 小智播报（P2）：既有 /api/voice/tts-test 直用，
     persona_id='__system__'（服务端哨兵=系统通用音色，绕过克隆栈——身份边界：
     小智是产品助手不是人设，绝不该用人设克隆声说话）+ fast=true（常热 edge
     引擎，零 GPU 占用）。逐条点击 opt-in，绝无自动播报。 */
  var SAY = { el: null, busy: false };
  function sayPrep(s) {
    var x = String(s || '');
    x = x.replace(/\[S\d\]/g, ' ');
    x = x.replace(/[*`#>_~]/g, '');
    x = x.replace(/\s+/g, ' ').trim();
    return x.slice(0, 360);
  }
  function sayReset() {
    var b = $panel ? $panel.querySelector('[data-act="say"].on') : null;
    if (b) { b.classList.remove('on'); b.textContent = '🔊'; }
  }
  function sayStop() {
    if (SAY.el) {
      try { SAY.el.pause(); } catch (e) { /* ignore */ }
    }
    orbSpeakStop();
    sayReset();
  }
  function sayText(btn, text) {
    if (btn.classList.contains('on')) { sayStop(); return; }
    if (SAY.busy || !text) { return; }
    sayStop();
    SAY.busy = true;
    btn.disabled = true;
    btn.textContent = '…';
    fetch('/api/voice/tts-test', {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ text: text, persona_id: '__system__',
                             fast: true, format: 'mp3' }),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || !j.ok) {
          throw new Error(String(j.message || j.error || t('say_fail')));
        }
        var url = String(j.audio_url || j.url || '');
        if (!url && j.filename) {
          url = '/api/voice/tts-test/' + encodeURIComponent(j.filename);
        }
        if (!url) { throw new Error(t('say_fail')); }
        if (!SAY.el) {
          SAY.el = new Audio();
          SAY.el.addEventListener('ended', sayStop);
          SAY.el.addEventListener('error', function () { sayStop(); });
        }
        SAY.el.src = url;
        orbSpeakStart(SAY.el);
        btn.classList.add('on');
        btn.textContent = '⏹';
        orbBeacon('asb_orb_say');
        return SAY.el.play();
      });
    }).catch(function (e) {
      sayStop();
      pushChat('<div class="asb-msg err">' +
        esc((e && e.message) || t('say_fail')) + '</div>');
    }).then(function () {
      SAY.busy = false;
      btn.disabled = false;
      if (!btn.classList.contains('on')) { btn.textContent = '🔊'; }
    });
  }

  /* ────────────────────────────────────────────── 报障 tab */
  function renderReport(body) {
    body.innerHTML = '<div class="asb-rp">' +
      '<textarea maxlength="1000" class="asb-rp-desc" placeholder="' +
      esc(t('rp_ph')) + '"></textarea>' +
      '<div class="asb-rp-shot">' +
      '<button type="button" class="asb-mic" data-act="shot-page">' +
      esc(t('shot_page')) + '</button>　' + esc(t('rp_shot_hint')) + ' ' +
      '<a data-act="pick-shot">' + esc(t('rp_shot_pick')) + '</a>' +
      '<input type="file" accept="image/png,image/jpeg" class="asb-rp-file" ' +
      'style="display:none"></div>' +
      '<div class="asb-rp-prev"></div>' +
      '<details class="asb-env"><summary>' + esc(t('rp_env_title')) +
      '</summary><div>' + esc(t('rp_env_note')) + '<br>· ' +
      esc(pagePath() || '/') +
      (uiBuild() ? ' · build ' + esc(uiBuild()) : '') + '</div></details>' +
      '<button type="button" class="asb-send" data-act="rp-submit">' +
      esc(t('rp_submit')) + '</button>' +
      '<div class="asb-rp-out"></div></div>';
    var file = body.querySelector('.asb-rp-file');
    file.addEventListener('change', function () {
      if (file.files && file.files[0]) { readShot(file.files[0]); }
      file.value = '';
    });
    renderShot();
  }

  function readShot(f) {
    if (!f || f.size > 4 * 1024 * 1024) { return; }
    var rd = new FileReader();
    rd.onload = function () {
      S.shotB64 = String(rd.result || '');
      renderShot();
    };
    rd.readAsDataURL(f);
  }
  function renderShot() {
    var box = $panel.querySelector('.asb-rp-prev');
    if (!box) { return; }
    box.innerHTML = S.shotB64
      ? '<div class="asb-rp-thumb"><img alt="screenshot" src="' + S.shotB64 +
        '"><button type="button" data-act="del-shot" aria-label="' +
        esc(t('rp_shot_del')) + '">✕</button></div>'
      : '';
  }

  /* ───────────────────────────── DOM 一键截图 + 标注（P1，纯 JS 免权限） */
  var _h2cP = null;
  function loadVendor() {
    if (window.html2canvas) { return Promise.resolve(window.html2canvas); }
    if (_h2cP) { return _h2cP; }
    _h2cP = new Promise(function (res, rej) {
      var s = document.createElement('script');
      s.src = '/assistant-shared/vendor-html2canvas.min.js?v=' + VER;
      s.onload = function () {
        if (window.html2canvas) { res(window.html2canvas); }
        else { rej(new Error('h2c missing')); }
      };
      s.onerror = function () { _h2cP = null; rej(new Error('h2c load')); };
      document.head.appendChild(s);
    });
    return _h2cP;
  }
  function rpErr(msg) {
    var out = $panel.querySelector('.asb-rp-out');
    if (out) {
      out.innerHTML = '<div class="asb-msg err">' + esc(msg) + '</div>';
    }
  }
  function capturePage() {
    var btn = $panel.querySelector('[data-act="shot-page"]');
    if (btn && btn.disabled) { return; }
    if (btn) { btn.disabled = true; btn.textContent = t('shot_busy'); }
    loadVendor().then(function (h2c) {
      /* 隐藏助手自身（不重渲染面板，保住已输入的描述文字） */
      $panel.style.visibility = 'hidden';
      $wrap.style.visibility = 'hidden';
      return new Promise(function (res) { setTimeout(res, 140); })
        .then(function () {
          return h2c(document.body, {
            useCORS: true,
            logging: false,
            x: window.scrollX,
            y: window.scrollY,
            width: document.documentElement.clientWidth,
            height: document.documentElement.clientHeight,
            windowWidth: document.documentElement.clientWidth,
            windowHeight: document.documentElement.clientHeight,
            ignoreElements: function (el) {
              return el === $wrap || el === $panel;
            },
          });
        });
    }).then(function (cv) {
      openAnnotate(cv);
    }).catch(function () {
      rpErr(t('rp_fail'));
    }).then(function () {
      $panel.style.visibility = '';
      $wrap.style.visibility = '';
      if (btn) { btn.disabled = false; btn.textContent = t('shot_page'); }
    });
  }
  function openAnnotate(src) {
    var ov = document.createElement('div');
    ov.className = 'asb-an';
    var bar = document.createElement('div');
    bar.className = 'asb-an-bar';
    bar.innerHTML = '<span>' + esc(t('an_title')) + '</span>' +
      '<button type="button" data-tool="rect" class="on">' +
      esc(t('an_rect')) + '</button>' +
      '<button type="button" data-tool="mosaic">' + esc(t('an_mosaic')) +
      '</button>' +
      '<button type="button" data-tool="undo">' + esc(t('an_undo')) +
      '</button>' +
      '<button type="button" data-tool="ok">' + esc(t('an_ok')) + '</button>' +
      '<button type="button" data-tool="cancel">' + esc(t('an_cancel')) +
      '</button>';
    var cv = document.createElement('canvas');
    cv.width = src.width;
    cv.height = src.height;
    var ctx = cv.getContext('2d');
    ctx.drawImage(src, 0, 0);
    ov.appendChild(bar);
    ov.appendChild(cv);
    document.body.appendChild(ov);
    var tool = 'rect', hist = [], drag = null;
    function snap() {
      if (hist.length >= 8) { hist.shift(); }
      hist.push(ctx.getImageData(0, 0, cv.width, cv.height));
    }
    function xy(ev) {
      var r = cv.getBoundingClientRect();
      return { x: (ev.clientX - r.left) * cv.width / r.width,
               y: (ev.clientY - r.top) * cv.height / r.height };
    }
    function mosaic(x, y, w, h) {
      if (w < 4 || h < 4) { return; }
      var s = Math.max(8, Math.round(Math.max(w, h) / 18));
      var tmp = document.createElement('canvas');
      tmp.width = Math.max(1, Math.round(w / s));
      tmp.height = Math.max(1, Math.round(h / s));
      var tc = tmp.getContext('2d');
      tc.imageSmoothingEnabled = false;
      tc.drawImage(cv, x, y, w, h, 0, 0, tmp.width, tmp.height);
      ctx.imageSmoothingEnabled = false;
      ctx.drawImage(tmp, 0, 0, tmp.width, tmp.height, x, y, w, h);
      ctx.imageSmoothingEnabled = true;
    }
    cv.addEventListener('pointerdown', function (ev) {
      snap();
      drag = xy(ev);
      cv.setPointerCapture(ev.pointerId);
    });
    cv.addEventListener('pointermove', function (ev) {
      if (!drag) { return; }
      var p = xy(ev);
      ctx.putImageData(hist[hist.length - 1], 0, 0);
      var x = Math.min(drag.x, p.x), y = Math.min(drag.y, p.y);
      var w = Math.abs(p.x - drag.x), h = Math.abs(p.y - drag.y);
      if (tool === 'rect') {
        ctx.strokeStyle = '#ef4444';
        ctx.lineWidth = Math.max(3, Math.round(cv.width / 400));
        ctx.strokeRect(x, y, w, h);
      } else {
        mosaic(x, y, w, h);
      }
    });
    cv.addEventListener('pointerup', function () { drag = null; });
    bar.addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-tool]');
      if (!b) { return; }
      var tl = b.getAttribute('data-tool');
      if (tl === 'rect' || tl === 'mosaic') {
        tool = tl;
        var bs = bar.querySelectorAll('[data-tool]');
        for (var i = 0; i < bs.length; i++) {
          bs[i].classList.toggle('on', bs[i] === b);
        }
        return;
      }
      if (tl === 'undo') {
        if (hist.length) { ctx.putImageData(hist.pop(), 0, 0); }
        return;
      }
      if (tl === 'cancel') { ov.remove(); return; }
      if (tl === 'ok') {
        S.shotB64 = cv.toDataURL('image/jpeg', 0.85);
        ov.remove();
        renderShot();
      }
    });
  }

  function submitReport() {
    var ta = $panel.querySelector('.asb-rp-desc');
    var out = $panel.querySelector('.asb-rp-out');
    var btn = $panel.querySelector('[data-act="rp-submit"]');
    var desc = String(ta && ta.value || '').trim();
    if (desc.length < 5 || !btn || btn.disabled) {
      if (ta) { ta.focus(); }
      return;
    }
    btn.disabled = true;
    btn.textContent = t('rp_submitting');
    /* 提交与成功分开记：两者的差＝提交失败率（网络/校验），
       混成一个数就看不出「报障提交不上去」这类静默故障。 */
    beacon('asb_report_submit');
    fetch('/api/assistant/report', {
      method: 'POST',
      headers: csrfHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({
        desc: desc, page: pagePath(), shot_b64: S.shotB64 || '',
        include_env: true, ui_build: uiBuild(),
      }),
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (j) {
        if (!r.ok || !j.ok) {
          throw new Error(String(j.detail || t('rp_fail')));
        }
        ta.value = '';
        S.shotB64 = '';
        renderShot();
        out.innerHTML = '<div class="asb-msg ai">✅ ' + esc(t('rp_ok')) +
          esc(String(j.ticket_id)) +
          (j.dup ? ' ' + esc(t('rp_dup')) : '') + '<br>' +
          '<span class="asb-rp-next">' + esc(t('rp_next')) + '</span><br>' +
          '<a href="#" data-act="go-mine" style="color:var(--xz-accent,#4f6ef7)">' +
          esc(t('rp_view')) + ' →</a></div>';
        beacon('asb_report_ok');
        orbBurst();
      });
    }).catch(function (e) {
      out.innerHTML = '<div class="asb-msg err">' +
        esc(e && e.message || t('rp_fail')) + '</div>';
    }).then(function () {
      btn.disabled = false;
      btn.textContent = t('rp_submit');
    });
  }

  /* ────────────────────────────────────────────── 我的 tab */
  /* 六态与 src/ops/bug_intake.py::VALID_STATUSES 逐字对齐——多一个少一个都会
     让用户看到英文原文状态（认不出「我的单进展到哪了」）。门禁：
     tests/test_bug_report_loop.py::test_status_labels_match_backend_states */
  function stLabel(st) {
    var map = { 'new': 'st_new', confirmed: 'st_confirmed',
      in_progress: 'st_in_progress', fixed: 'st_fixed',
      verified: 'st_verified', closed: 'st_closed' };
    return map[st] ? t(map[st]) : st;
  }
  function loadSeen() {
    try { return JSON.parse(localStorage.getItem('asb_seen_v1') || '{}'); }
    catch (e) { return {}; }
  }
  function saveSeen(m) {
    try { localStorage.setItem('asb_seen_v1', JSON.stringify(m)); }
    catch (e) { /* ignore */ }
  }
  function loadTickets() {
    var list = $panel.querySelector('.asb-mine-list');
    fetch('/api/assistant/tickets').then(function (r) {
      return r.ok ? r.json() : { tickets: [] };
    }).then(function (j) {
      var rows = (j && j.tickets) || [];
      if (S.tab !== 'mine' || !list) {
        syncDot(rows);
        return;
      }
      if (!rows.length) {
        list.innerHTML = '<div class="asb-empty">' + esc(t('mine_empty')) +
          '</div>';
        syncDot(rows);
        return;
      }
      var seen = loadSeen();
      var html = '';
      for (var i = 0; i < rows.length; i++) {
        var tk = rows[i];
        var st = String(tk.status || 'new');
        var d = new Date((Number(tk.updated_ts || tk.created_ts) || 0) * 1000);
        html += '<div class="asb-tk"><div class="asb-tk-hd">' +
          '<span class="id">#' + esc(String(tk.id)) + '</span>' +
          '<span class="tt">' + esc(String(tk.title || '')) + '</span>' +
          '<span class="asb-st ' + esc(st) + '">' + esc(stLabel(st)) +
          '</span></div>' +
          '<div style="font-size:.64rem;color:var(--xz-muted,#888)">' +
          esc(d.toLocaleString()) +
          (Number(tk.report_count || 1) > 1
            ? ' · ×' + esc(String(tk.report_count)) : '') + '</div>' +
          (st === 'fixed' && tk.notify_note && tk.notify_note !== 'assistant-panel'
            ? '<div class="asb-tk-note">' + esc(t('mine_fixed_note')) + ': ' +
              esc(String(tk.notify_note)) + '</div>' : '') +
          '</div>';
        if (st === 'fixed' || st === 'verified') { seen[tk.id] = 1; }
      }
      list.innerHTML = html;
      saveSeen(seen);
      syncDot(rows);
    }).catch(function () {
      if (list) {
        list.innerHTML = '<div class="asb-empty">' + esc(t('err_net')) +
          '</div>';
      }
    });
  }
  function syncDot(rows) {
    var seen = loadSeen();
    var dot = false;
    for (var i = 0; i < rows.length; i++) {
      if (String(rows[i].status) === 'fixed' && !seen[rows[i].id]) {
        dot = true; break;
      }
    }
    S.dot = dot;
    $ball.classList.toggle('hasdot', dot);
    syncOrb();
  }

  /* ────────────────────────────────────────────── 事件委托 */
  function onPanelClick(e) {
    var el = e.target.closest(
      '[data-act],[data-tab],[data-mode],[data-faq],[data-q],[data-fb],[data-goto]');
    if (!el) { return; }
    var mode = el.getAttribute('data-mode');
    if (mode) { switchTab(mode); return; }
    var faqQ = el.getAttribute('data-faq');
    if (faqQ) {
      /* 常问面板点条目＝直接切问答并发问（这就是它存在的理由：把「不知道
         能问什么」变成一次点击），来源归因单独一枚。 */
      beacon('asb_faq_pick');
      switchTab('chat', true);
      sendQuery(faqQ);
      return;
    }
    var tab = el.getAttribute('data-tab');
    if (tab) { switchTab(tab); return; }
    var gotoPath = el.getAttribute('data-goto');
    if (gotoPath) {
      /* P3 聚光灯交接（2026-08-21）：目标页组件 init 后读取并高亮 anchor */
      var anc = el.getAttribute('data-anchor') || '';
      if (anc) {
        try {
          sessionStorage.setItem('asb_goto',
            JSON.stringify({ sel: anc, ts: Date.now() }));
        } catch (e4) { /* ignore */ }
      }
      location.href = gotoPath;
      return;
    }
    var fb = el.getAttribute('data-fb');
    if (fb) {
      var box = el.closest('.asb-fb');
      if (box) { sendFeedback(box.getAttribute('data-qa'), fb, box); }
      return;
    }
    var act = el.getAttribute('data-act');
    /* 快捷 chip：只有 data-q 没有 data-act（to-report 两者都有，act 优先） */
    if (!act && el.getAttribute('data-q')) {
      /* 「大家常问」用量——P1 要把它搬去独立面板，搬之前得知道它有没有人点。
         拒答建议单独记 asb_sugg_pick：它是「答不上来之后还能不能救回来」的
         唯一读数，混进 asb_chip 会同时污染两个指标。 */
      beacon(el.getAttribute('data-sugg') ? 'asb_sugg_pick' : 'asb_chip');
      sendQuery(el.getAttribute('data-q'));
      return;
    }
    if (act === 'close') { togglePanel(false); return; }
    if (act === 'rstgeo') { resetPanelBox(); return; }
    /* 标题栏手机操控：配对弹层归 XZAgent（同一条 /api/assistant/pair 链），
       组件缺席则静默——不做死按钮。 */
    if (act === 'pair') {
      beacon('asb_pair_open');
      if (window.XZAgent && typeof window.XZAgent.pair === 'function') {
        try { window.XZAgent.pair(); } catch (e6) { /* ignore */ }
      }
      return;
    }
    /* 标题栏电脑操控：受控机弹层归 XZAgent（同一条 /api/assistant/pc 链），
       组件缺席则静默——不做死按钮。 */
    if (act === 'pc') {
      beacon('asb_pc_open');
      if (window.XZAgent && typeof window.XZAgent.pc === 'function') {
        try { window.XZAgent.pc(); } catch (e7) { /* ignore */ }
      }
      return;
    }
    if (act === 'say') { sayText(el, el.getAttribute('data-say') || ''); return; }
    if (act === 'send') { composerSubmit(); return; }
    if (act === 'stop-gen') {
      S.aborted = true;
      if (S.abortCtl) { try { S.abortCtl.abort(); } catch (e5) { /* */ } }
      return;
    }
    if (act === 'copy') { copyAnswer(el); return; }
    if (act === 'retry') { el.remove(); sendQuery(S.lastQ); return; }
    if (act === 'to-report') {
      var q = el.getAttribute('data-q') || '';
      switchTab('report');
      var ta = $panel.querySelector('.asb-rp-desc');
      if (ta && !ta.value) { ta.value = q; }
      return;
    }
    if (act === 'pick-shot') {
      var f = $panel.querySelector('.asb-rp-file');
      if (f) { f.click(); }
      return;
    }
    if (act === 'del-shot') { S.shotB64 = ''; renderShot(); return; }
    if (act === 'rp-submit') { submitReport(); return; }
    if (act === 'go-mine') { e.preventDefault(); switchTab('mine'); return; }
    if (act === 'mine-refresh') { loadTickets(); return; }
    if (act === 'mic') { toggleRec(); return; }
    if (act === 'shot-page') { capturePage(); return; }
    if (act === 'tips') {
      if (typeof window.toggleTermTips === 'function') {
        window.toggleTermTips();
      } else {
        var old = document.querySelector('.tip-toggle');
        if (old) { old.click(); }
      }
      renderTools();
      return;
    }
    if (act === 'support') {
      togglePanel(false);
      if (typeof window.openSupportPanel === 'function') {
        try { window.openSupportPanel(''); } catch (e3) { /* ignore */ }
      }
      return;
    }
    if (act === 'tour') {
      try { localStorage.removeItem('tour_done'); } catch (e2) { /* */ }
      togglePanel(false);
      if (typeof window.startTour === 'function') { window.startTour(); }
      return;
    }
    if (act === 'keys') {
      togglePanel(false);
      if (typeof window.openShortcuts === 'function') { window.openShortcuts(); }
      return;
    }
    if (act === 'cmd') {
      togglePanel(false);
      if (typeof window.openCmdPalette === 'function') { window.openCmdPalette(); }
      return;
    }
    if (act === 'orb-fx') {
      var calmNext = ORB.lvl >= 2;
      try {
        localStorage.setItem('asb_orb_lvl', calmNext ? '1' : '2');
      } catch (e5) { /* ignore */ }
      $wrap.classList.remove('asb-lv0');
      $wrap.classList.remove('asb-lv1');
      $wrap.classList.remove('asb-lv2');
      ORB.degStep = 0;
      ORB.lvl = orbLevel();
      $wrap.classList.add('asb-lv' + ORB.lvl);
      orbBeacon(ORB.lvl < 2 ? 'asb_orb_calm_on' : 'asb_orb_calm_off');
      renderTools();
      syncOrb();
      return;
    }
  }

  /* ─────────────────────────── P3：带我去聚光灯 + 主动援助（2026-08-21） */
  function trySpotlight() {
    var raw = null;
    try { raw = sessionStorage.getItem('asb_goto'); } catch (e) { return; }
    if (!raw) { return; }
    try { sessionStorage.removeItem('asb_goto'); } catch (e) { /* ignore */ }
    var h;
    try { h = JSON.parse(raw); } catch (e) { return; }
    if (!h || !h.sel || (Date.now() - (Number(h.ts) || 0)) > 90000) { return; }
    var el;
    try { el = document.querySelector(String(h.sel)); } catch (e) { return; }
    /* 不存在/不可见=优雅跳过（如 feat 探测隐藏的卡）——宁可不聚光不指空气 */
    if (!el || !el.offsetParent) { return; }
    try {
      el.scrollIntoView({ block: 'center', behavior: 'smooth' });
    } catch (e) { el.scrollIntoView(); }
    setTimeout(function () { spotOn(el); }, 380);
  }
  function spotOn(el) {
    var r = el.getBoundingClientRect();
    var pad = 6;
    var d = document.createElement('div');
    d.className = 'asb-spot';
    d.style.left = (r.left - pad) + 'px';
    d.style.top = (r.top - pad) + 'px';
    d.style.width = (r.width + pad * 2) + 'px';
    d.style.height = (r.height + pad * 2) + 'px';
    document.body.appendChild(d);
    function off() {
      try { d.remove(); } catch (e) { /* ignore */ }
      document.removeEventListener('pointerdown', off, true);
      document.removeEventListener('keydown', off, true);
    }
    setTimeout(off, 8000);
    setTimeout(function () {
      document.addEventListener('pointerdown', off, true);
      document.addEventListener('keydown', off, true);
    }, 400);
  }

  /* 主动援助：同页 30s 内 ≥2 个脚本错误 → 球旁冒泡「需要帮忙吗」（每页一次，
     面板开着不打扰，自家组件报错不自责）。点「帮我报障」= 开报障 tab 预填指纹。 */
  var _errT = [];
  var _nudged = false;
  function wireErrWatch() {
    window.addEventListener('error', function (ev) {
      if (_nudged || S.open) { return; }
      var src = String((ev && ev.filename) || '');
      if (src.indexOf('assistant-ball') >= 0) { return; }
      var now = Date.now();
      _errT.push(now);
      while (_errT.length && now - _errT[0] > 30000) { _errT.shift(); }
      if (_errT.length >= 2) {
        _nudged = true;
        showNudge(String((ev && ev.message) || 'script error'));
      }
    });
  }
  function showNudge(msg) {
    ORB.nudgeOn = true;
    syncOrb();
    orbBurst('bang');  /* 警示入场：粒子重组「!」——页面出事的瞬间可读 */
    var n = document.createElement('div');
    n.className = 'asb-nudge';
    n.innerHTML = '<span>' + esc(t('nudge_text')) + '</span>' +
      '<button type="button" data-n="go">' + esc(t('nudge_go')) + '</button>' +
      '<button type="button" data-n="x" aria-label="' + esc(t('close')) +
      '">✕</button>';
    document.body.appendChild(n);
    var r = $wrap.getBoundingClientRect();
    n.style.right = Math.max(10, window.innerWidth - r.right) + 'px';
    n.style.bottom = Math.max(10, window.innerHeight - r.top + 10) + 'px';
    var kill = setTimeout(function () {
      try { n.remove(); } catch (e) { /* ignore */ }
      ORB.nudgeOn = false;
      syncOrb();
    }, 30000);
    n.addEventListener('click', function (ev2) {
      var b = ev2.target.closest('[data-n]');
      if (!b) { return; }
      clearTimeout(kill);
      n.remove();
      ORB.nudgeOn = false;
      syncOrb();
      if (b.getAttribute('data-n') === 'go') {
        togglePanel(true);
        if (reportOn()) {
          switchTab('report');
          var ta = $panel.querySelector('.asb-rp-desc');
          if (ta && !ta.value) {
            ta.value = t('nudge_prefill') + String(msg).slice(0, 120);
          }
        }
      }
    });
  }

  /* ────────────────────────────────────────────── init */
  /* 画布令牌 --xz-*（P0-4 2026-08-27 三套合一）：原先 ball/teach/agent 各用
     --as-/--xzt-/--xza- 三套前缀，三份 themeVars 却逐字节相同——同一批宿主
     token 被抄了三遍，任何一处漂移都会让三个模块在同一面板里长得不一样。
     现统一为 --xz-*，三文件各留一份等值副本（三个独立 IIFE 无模块系统，
     复制是被迫的）。**改这里必须同步 teach/agent 的同名函数**；
     门禁 tests/test_assistant_beacons.py::test_canonical_theme_tokens。 */
  function themeVars(shell) {
    return shell === 'workspace'
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

  function init(opts) {
    opts = opts || {};
    if ($wrap) { return; }
    S.shell = opts.shell === 'workspace' ? 'workspace' : 'admin';
    S.lang = String(opts.lang || '').toLowerCase().indexOf('en') === 0
      ? 'en' : 'zh';
    S.boot = opts.boot || null;
    loadChatStore();  /* 对话持久化：刷新/跳页恢复（24h TTL） */
    injectCss();
    buildDom();
    var vars = themeVars(S.shell);
    for (var k in vars) {
      if (Object.prototype.hasOwnProperty.call(vars, k)) {
        $wrap.style.setProperty(k, vars[k]);
        $panel.style.setProperty(k, vars[k]);
      }
    }
    if (S.shell === 'admin') { document.body.classList.add('asb-live'); }
    orbInit();
    /* P3：带我去聚光灯交接 + 主动援助错误监听（2026-08-21） */
    setTimeout(trySpotlight, 600);
    wireErrWatch();
    /* 启动即拉一次工单同步角标（静默失败） */
    if (reportOn()) {
      setTimeout(function () {
        fetch('/api/assistant/tickets').then(function (r) {
          return r.ok ? r.json() : { tickets: [] };
        }).then(function (j) { syncDot((j && j.tickets) || []); })
          .catch(function () { /* ignore */ });
      }, 2500);
    }
  }

  window.AssistantBall = { init: init, _ver: VER,
    /* 公共 API（实施58 教学/代办线消费）：orbMode('teach'|'agent', on[, 0..1 进度])
       ——黏性模式参与正常优先级；真实信号（听/说/想）永远压过它。 */
    orbMode: orbMode,
    /* 模式认领（实施73 P1-1）：姊妹组件在自己 init 时把「教学 / 替我做」
       挂成一级模式；def = {order, icon, labelKey|label, mount(el, api),
       unmount?, composer?:{ph, submit(text)}}。缺席即该模式不出现。 */
    registerMode: registerMode,
    /* 开面板 + 投问（教学模式「详细讲讲」）：勿对 .asb-ball 调 click() */
    open: function () { togglePanel(true); },
    ask: askFromOutside,
    /* orb 调试钩子：真浏览器门禁/演示页驱动状态（force 空串=还原真实信号） */
    _orbForce: function (st) { ORB.force = st || ''; syncOrb(); },
    _orbState: function () { return ORB.state; },
    _orbLevel: function () { return ORB.lvl; },
    _orbBurst: orbBurst };
})();
