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
 * 视觉体系 v2「智连光带」（2026-09-05）：一条青→智连蓝→紫的光带只在小智活动
 * 时出现于四处——球的立体光环（前后遮挡两半）/ 面板边缘泛光（听·想·说）/
 * 生成中的 AI 气泡描边（pending 光雾 → live 光标 → settle 落定，同一节点换形态）
 * / 语音波形三条彩色正弦。令牌 --xz-rb-*、玻璃/高光/阴影见 injectCss 顶部注释；
 * 图标全部为内置线性 SVG（ICONS + ic()），三个 assistant-*.js 各留子集。
 * 动效三级降级：prefers-reduced-motion → 安静档 lv0/1（asb-lv* 同时挂球与面板）
 * → 运行期 EMA 第二档 .asb-lite；空闲态面板内零动效。
 * v2.1（同日）：状态色走 @property --xz-core 色变量（hue-rotate 退场，画布粒子
 * 与之同源 orbStateRgb）；光谱两端由品牌色 OKLCH 相对推导（白标换主色整条光带
 * 跟转，@supports 回落令牌）；registerMode 收 iconName；默认摆放避让宿主浮球。
 * v3「内光核」（2026-09-06 实施95 P0）：待机也看得出活着——硬体球面与两条常驻
 * 刚性光环退场（46px 上读成「带环的行星图标」），改成软边半透明核 + 球内三团
 * 光谱色沿互质周期漂移 + 轮廓微呼吸（全部 transform/opacity，待机 0 JS 帧）；
 * 光环只在活跃态从核里「聚成环」。待机三档 calm/breathe/flow（默认 flow）由
 * 偏好页段控切换；球内图标改为悬停标签片；新增 connecting/error 两态；安静档与
 * reduced-motion 保留状态脉冲（去运动不去信息）。改球体前先读「Orb 引擎」段。
 * P1/P2「语音对话」（同日）：长按球进入免手动回合——PCM 环形缓冲采集（worklet）
 * → VAD 判停 → 只传说话段（WAV）转写 → 问答 → 首分句提前开口、按句 TTS →
 * 说话即打断（开口位置带入下一轮）→ 再听；时延分桶埋点 asb_voice_ttfs_… 与
 * asb_voice_asr_…。详见文内「语音对话」段。
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

  var VER = '20260907a';
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
      fb_up: '有帮助', fb_down: '没帮助',
      fb_thanks: '已记录，谢谢反馈', retry: '重试',
      stop_t: '停止生成', stopped_gen: '已停止',
      copy_t: '复制回答',
      to_report: '这像是故障？一键转报障',
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
      shot_page: '截当前页', shot_busy: '截取中…',
      an_title: '圈出问题 · 涂掉敏感信息', an_rect: '圈选',
      an_mosaic: '马赛克', an_undo: '撤销', an_ok: '用这张',
      an_cancel: '取消',
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
      /* 动效三档段控（v3 2026-09-06）：一个控件同时管待机样式与画质档——
         安静=lv1+calm、呼吸=lv2+breathe、流动=lv2+flow。诊断行说清「现在是
         哪一档、为什么」：远程桌面会话常关掉系统动画 → reduced-motion → 球全静，
         用户以为坏了；此前这个原因在界面上无处可查。 */
      fx_t: '动效', fx_calm: '安静', fx_breathe: '呼吸', fx_flow: '流动',
      fx_now: '当前', fx_r_default: '默认', fx_r_user: '你的选择',
      fx_r_admin: '管理员默认',
      fx_r_rm: '系统已开启「减少动态效果」，动效自动关闭',
      fx_r_deg1: '本机性能自动降档（粒子减半）',
      fx_r_deg2: '本机性能不足，本次会话已自动转安静',
      /* 球的悬停标签片（v3）：图标退出球身后，可发现性由它接住 */
      tip_ask: '问小智', tip_ask_voice: '问小智 · 长按开始语音对话',
      /* 语音对话（P1 2026-09-06）：免手动回合——说完自动答、答完自动念、念完接着听 */
      v_interrupt: '打断', v_end: '结束语音',
      v_autosay_on: '自动播报：开（点击关闭）', v_autosay_off: '自动播报：关（点击开启）',
      v_idle_end: '一直没听到声音，语音对话已结束',
      v_started: '语音对话已开始：直接说，说完我自动回答；我在说时你开口就能打断',
      v_barge_hint: '说话即可打断',
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
      err_report: '报障',
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
      /* 标题栏状态短句（v2 2026-09-05）：与球的状态机一一对应，球是余光信号，
         这一行是明确信号——「小智在干什么」终于有字可读。 */
      hd_idle: '随时可问', hd_hint: '有新进展', hd_listening: '在听…',
      hd_thinking: '思考中', hd_live: '回答中', hd_speaking: '播报中',
      hd_teach: '教学中', hd_agent: '执行中', hd_alert: '页面有异常',
      hd_connecting: '正在连接麦克风…', hd_error: '出错了，可以重试',
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
      fb_up: 'Helpful', fb_down: 'Not helpful',
      fb_thanks: 'Recorded, thanks', retry: 'Retry',
      stop_t: 'Stop generating', stopped_gen: 'Stopped',
      copy_t: 'Copy answer',
      to_report: 'Looks like a bug? File a report',
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
      shot_page: 'Capture this page', shot_busy: 'Capturing…',
      an_title: 'Box the issue · mosaic sensitive info', an_rect: 'Box',
      an_mosaic: 'Mosaic', an_undo: 'Undo', an_ok: 'Use it',
      an_cancel: 'Cancel',
      mine_empty: 'No reports yet',
      mine_refresh: 'Refresh', mine_fixed_note: 'Developer note',
      st_new: 'Open', st_confirmed: 'Confirmed', st_in_progress: 'In progress',
      st_fixed: 'Fixed', st_verified: 'Verified', st_closed: 'Closed',
      tools_tips_on: 'Term tips: on', tools_tips_off: 'Term tips: off',
      tools_tour: 'Tour', tools_keys: 'Shortcuts', tools_cmd: 'Cmd palette',
      tools_support: 'Upload diagnostics',
      fx_t: 'Motion', fx_calm: 'Calm', fx_breathe: 'Breathe', fx_flow: 'Flow',
      fx_now: 'Now', fx_r_default: 'default', fx_r_user: 'your choice',
      fx_r_admin: 'admin default',
      fx_r_rm: 'System "reduce motion" is on — motion disabled automatically',
      fx_r_deg1: 'Auto-reduced for performance (half particles)',
      fx_r_deg2: 'This machine struggled — switched to Calm for this session',
      tip_ask: 'Ask the assistant', tip_ask_voice: 'Ask · hold to start voice chat',
      v_interrupt: 'Interrupt', v_end: 'End voice',
      v_autosay_on: 'Auto read-aloud: on (click to turn off)',
      v_autosay_off: 'Auto read-aloud: off (click to turn on)',
      v_idle_end: 'Heard nothing for a while — voice chat ended',
      v_started: 'Voice chat started: just talk, I answer when you pause; speak over me to interrupt',
      v_barge_hint: 'speak to interrupt',
      say_t: 'Read this answer aloud (system voice)',
      say_fail: 'Read-aloud failed, please retry',
      hello: 'Three things I can do: **answer how-to**, **walk you through**, '
        + '**do it for you**. Just say what you need.',
      err_net: 'Network error, please retry',
      rate_hint: 'Too fast, try again later',
      err_nostream: 'I could not deliver an answer (server cut the stream). '
        + 'Retry, or file a report.',
      err_report: 'Report',
      thinking_long: 'Asking the AI (usually 5-30s)',
      suggest_t: 'These I can answer — try one:',
      suggest_near_t: 'Did you mean:',
      basis_product: 'From the product description',
      basis_general: 'General knowledge · not product docs',
      resize_aria: 'Resize panel (arrow keys, Shift to speed up, Esc to reset)',
      move_hint: 'Drag to move · double-click to reset',
      reset_geo: 'Restore default size and position',
      hd_idle: 'Ready', hd_hint: 'Something new', hd_listening: 'Listening…',
      hd_thinking: 'Thinking', hd_live: 'Answering', hd_speaking: 'Speaking',
      hd_teach: 'Teaching', hd_agent: 'Working', hd_alert: 'Page issue detected',
      hd_connecting: 'Connecting microphone…', hd_error: 'Something went wrong — retry',
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
/* ── 「智连光带」令牌（2026-09-05 视觉体系 v2）────────────────────────────
   光带 = 小智在活动。一条从品牌蓝推导的三色光带（青→智连蓝→紫，全部取
   宿主 --tk-cyan/--tk-violet，admin 壳无此二令牌时落到同值字面量），只在
   球环 / 面板边缘 / 生成中的气泡 / 语音波形四处出现，且每处都映射一个真实
   状态（空闲时面板内零动效）。--xz-a 走 @property 才能让 conic 角度被
   animation 补间；不支持的宿主自然退化为静态渐变（仍然好看，只是不流动）。
   注意 :root 上只放**色停** --xz-rb-stops，不放整条 conic——自定义属性里的
   var() 在声明处即解析，若把 `from var(--xz-a)` 写进 :root 令牌，各使用点再
   动画自己的 --xz-a 也换不动角度（首版即踩）。使用点一律写
   `conic-gradient(from var(--xz-a),var(--xz-rb-stops))`。
   玻璃/高光/阴影/着色投影是「立体化」的四层纵深：L0 环境光（面板外沿泛光、
   球的带色接触阴影）→ L1 玻璃壳 → L2 气泡（带色投影 + 顶部高光）→ L3 光带。
   亮暗差异只走 .asb-dark 一个类（由 ORB 亮度探测同步），不写两壳成对暗色
   选择器（frontend-theme.mdc 纪律）。 */
'@property --xz-a{syntax:"<angle>";inherits:false;initial-value:0deg}' +
/* 状态核心色（v2.1 2026-09-05）：球面/标题核/光晕/接触阴影全部吃 --xz-core，
   状态类只改这一个变量。注册成 <color> 是为了能被 transition 补间——v1/v2.0 用
   filter:hue-rotate 换色，矩阵近似把品牌蓝转成青时会偏绿、转琥珀时偏橄榄，
   且白标换了主色角度全错；现在每个状态直接指到光谱令牌或语义色。 */
'@property --xz-core{syntax:"<color>";inherits:true;initial-value:#4f6ef7}' +
':root{' +
'--xz-rb-1:var(--tk-cyan,#22d3ee);' +
'--xz-rb-2:var(--xz-accent);' +
'--xz-rb-3:var(--tk-violet,#8b5cf6);' +
/* 对称色停（青→蓝→紫→蓝→青）：conic 0°/360° 接缝处颜色相同，环上没有硬边 */
'--xz-rb-stops:var(--xz-rb-1),var(--xz-rb-2),var(--xz-rb-3),var(--xz-rb-2),var(--xz-rb-1);' +
'--xz-ribbon-l:linear-gradient(90deg,var(--xz-rb-1),var(--xz-rb-2),var(--xz-rb-3));' +
'--xz-glass:color-mix(in srgb,var(--xz-bg) 86%,transparent);' +
'--xz-hl:rgba(255,255,255,.6);' +
'--xz-sh-key:0 24px 60px -18px rgba(15,27,45,.32);' +
'--xz-sh-amb:0 2px 8px rgba(15,27,45,.08);' +
'--xz-tint:color-mix(in srgb,var(--xz-accent) 55%,transparent);' +
'--xz-ease:cubic-bezier(.22,1,.36,1)}' +
/* 白标推导（v2.1）：光谱两端不再取固定的 --tk-cyan/--tk-violet，而是从品牌色
   在 OKLCH 里相对推导——青 = 品牌色 L+.23、C×.77、h−42°；紫 = L+.04、C×1.26、
   h+39°（两组系数按智连蓝 #0d76d9 → #22d3ee / #8b5cf6 反算，默认品牌下与原色
   肉眼无差；白标换主色后整条光带随之转向，不再出现「绿色品牌配紫色光带」）。
   L 用 min() 封顶防止暗壳 400 级 accent 推出近白的青。相对色语法 Chromium 119+ /
   Safari 16.4+，老浏览器落回上面的令牌/字面量。 */
'@supports (color:oklch(from red l c h)){:root{' +
'--xz-rb-1:oklch(from var(--xz-accent) min(.92,calc(l + .23)) calc(c * .77) calc(h - 42));' +
'--xz-rb-3:oklch(from var(--xz-accent) min(.86,calc(l + .04)) calc(c * 1.26) calc(h + 39))}}' +
'.asb-dark{--xz-hl:rgba(255,255,255,.09);' +
'--xz-glass:color-mix(in srgb,var(--xz-bg) 80%,transparent);' +
'--xz-sh-key:0 24px 60px -18px rgba(0,0,0,.65);--xz-sh-amb:0 2px 8px rgba(0,0,0,.35)}' +
'.asb-wrap{position:fixed;z-index:9998;font-family:inherit}' +
'.asb-ball{width:46px;height:46px;border-radius:50%;border:none;cursor:pointer;' +
'display:flex;align-items:center;justify-content:center;background:transparent;color:#fff;' +
'transition:transform .18s;position:relative;padding:0;z-index:2;' +
'--xz-core:var(--xz-accent,#4f6ef7)}' +
'.asb-ball:hover{transform:scale(1.07)}' +
'.asb-ball svg{width:20px;height:20px;pointer-events:none}' +
/* 状态 → 核心色（球与面板同一张表；面板上的 .asb-hd-core / 状态短句跟着走）：
   听=青(rb-1)、想=紫(rb-3)、说=靛(蓝紫各半)、做=蓝紫(偏蓝)、教=青蓝、
   警=琥珀(语义警示色)、成=翠绿(语义成功色)、连接中=灰蓝(压饱和：还没在录，
   不能长得像在听)、出错=玫红(语义错误色，与「页面异常」的琥珀分开)。 */
'.asb-ball.st-connecting,.asb-panel.st-connecting{' +
'--xz-core:color-mix(in srgb,var(--xz-accent,#4f6ef7) 55%,#94a3b8)}' +
'.asb-ball.st-error,.asb-panel.st-error{--xz-core:var(--tk-rose,#ef4444)}' +
'.asb-ball.st-listening,.asb-panel.st-listening{--xz-core:var(--xz-rb-1,#22d3ee)}' +
'.asb-ball.st-thinking,.asb-panel.st-thinking{--xz-core:var(--xz-rb-3,#8b5cf6)}' +
'.asb-ball.st-speaking,.asb-panel.st-speaking{' +
'--xz-core:color-mix(in srgb,var(--xz-rb-2,#4f6ef7) 45%,var(--xz-rb-3,#8b5cf6))}' +
'.asb-ball.st-agent,.asb-panel.st-agent{' +
'--xz-core:color-mix(in srgb,var(--xz-rb-2,#4f6ef7) 65%,var(--xz-rb-3,#8b5cf6))}' +
'.asb-ball.st-teach,.asb-panel.st-teach{' +
'--xz-core:color-mix(in srgb,var(--xz-rb-1,#22d3ee) 60%,var(--xz-rb-2,#4f6ef7))}' +
'.asb-ball.st-alert,.asb-panel.st-alert{--xz-core:var(--tk-amber,#f59e0b)}' +
'.asb-ball.okflash{--xz-core:var(--tk-emerald,#10b981)}' +
/* 活体能量核 v3「内光核」（2026-09-06 实施95 P0）：待机也看得出活着。
   v2 的硬体球面（inset 明暗交界 + 镜面高光）与两条常驻刚性光环在 46px 上读成
   「带环的行星图标」——刚体慢转不是生命信号；极光层被 blur+裁切在 46px 上退化
   成一层几乎看不见的色调漂移（为看不见的动效持续付费）。v3 = 软边半透明核 +
   球内三团光谱色沿互质周期漂移（7.3s/11s/13.7s，整体永不重复）+ 轮廓微呼吸；
   全部只动 transform/opacity（合成器，待机 0 JS 帧）。人眼对「内部在流动、
   轮廓在变」远比对「对称环慢转」敏感，这是 ChatGPT/AI Pin 那类软体球的核心手法。
   radial/mask 一律 closest-side：默认 farthest-corner 半径 = 23√2，76% 处早在
   盒外，边缘根本不会变软（首版即踩）。背景仍留在 .asb-orb 本体上——代办线的
   流星头取样 getComputedStyle(.asb-orb).backgroundImage（DOM 协作契约）。 */
'.asb-orb{position:absolute;inset:0;border-radius:50%;overflow:hidden;isolation:isolate;' +
'background:radial-gradient(circle closest-side at 50% 50%,' +
'color-mix(in srgb,var(--xz-core) 58%,#fff) 0%,var(--xz-core) 46%,' +
'color-mix(in srgb,var(--xz-core) 84%,#fff) 80%,' +
'color-mix(in srgb,var(--xz-core) 72%,transparent) 100%);' +
'-webkit-mask-image:radial-gradient(circle closest-side,#000 0 87%,transparent 100%);' +
'mask-image:radial-gradient(circle closest-side,#000 0 87%,transparent 100%);' +
'transform:perspective(90px) rotateX(var(--xz-tx,0deg)) rotateY(var(--xz-ty,0deg));' +
'transition:--xz-core .5s ease,transform .18s ease-out}' +
/* 内光三团：青(rb-1)/紫(rb-3)/白。白团＝会移动的高光，液体感的来源（固定
   高光=固体弹珠）。各自一层只动 transform；亮底普通叠加，暗底 screen 发光。 */
'.asb-lc{position:absolute;width:70%;height:70%;border-radius:50%;pointer-events:none;' +
'background:radial-gradient(circle closest-side,var(--lc) 0%,' +
'color-mix(in srgb,var(--lc) 55%,transparent) 42%,transparent 100%);' +
'filter:blur(2px);opacity:.85;will-change:transform;transition:opacity .5s ease}' +
'.asb-lc.lc1{--lc:var(--xz-rb-1,#22d3ee);left:-16%;top:-10%;' +
'animation:asbDrift1 7.3s ease-in-out infinite}' +
'.asb-lc.lc2{--lc:var(--xz-rb-3,#8b5cf6);left:44%;top:30%;' +
'animation:asbDrift2 11s ease-in-out infinite}' +
'.asb-lc.lc3{--lc:#fff;width:46%;height:46%;left:18%;top:4%;opacity:.55;' +
'animation:asbDrift3 13.7s ease-in-out infinite}' +
'.asb-dark .asb-lc{mix-blend-mode:screen}' +
'.asb-dark .asb-lc.lc3{opacity:.4}' +
'@keyframes asbDrift1{0%,100%{transform:translate(0,0) scale(1)}' +
'30%{transform:translate(28%,20%) scale(1.14)}60%{transform:translate(6%,42%) scale(.9)}}' +
'@keyframes asbDrift2{0%,100%{transform:translate(0,0) scale(1)}' +
'35%{transform:translate(-32%,-16%) scale(1.1)}70%{transform:translate(-12%,20%) scale(.92)}}' +
'@keyframes asbDrift3{0%,100%{transform:translate(0,0) scale(1)}' +
'40%{transform:translate(30%,34%) scale(1.18)}75%{transform:translate(-8%,48%) scale(.86)}}' +
/* 待机三档（2026-09-06 拍板）：calm=只留辉光慢呼吸；breathe=内光漂移+呼吸；
   flow（默认）=再加轮廓微呼吸——椭圆挤压 ±3.5% 随慢转换向（不用 border-radius：
   角部形变全在 mask 渐隐区里，肉眼看不到）。悬停微倾走 --xz-tx/--xz-ty 变量
   写进同一条 transform，动画与悬停互不覆盖。只作用于待机/提示两态。 */
'.asb-idle-flow .asb-ball.st-idle .asb-orb,.asb-idle-flow .asb-ball.st-hint .asb-orb{' +
'animation:asbMorph 9.7s ease-in-out infinite}' +
'@keyframes asbMorph{0%,100%{transform:perspective(90px) rotateX(var(--xz-tx,0deg)) ' +
'rotateY(var(--xz-ty,0deg)) rotate(0deg) scale(1.035,.965)}' +
'33%{transform:perspective(90px) rotateX(var(--xz-tx,0deg)) rotateY(var(--xz-ty,0deg)) ' +
'rotate(120deg) scale(.972,1.028)}' +
'66%{transform:perspective(90px) rotateX(var(--xz-tx,0deg)) rotateY(var(--xz-ty,0deg)) ' +
'rotate(240deg) scale(1.02,.98)}}' +
'.asb-idle-calm .asb-lc{animation:none}' +
'.asb-idle-calm .asb-glow{animation-duration:9s}' +
/* 琥珀/玫红/翠绿是语义色，青紫内光叠上去会混成橄榄——这几态把内光压到几乎不见；
   连接中（还没在录）内光半隐，与「在听」拉开距离 */
'.asb-ball.st-alert .asb-lc,.asb-ball.st-error .asb-lc,.asb-ball.okflash .asb-lc{opacity:.12}' +
'.asb-ball.st-connecting .asb-lc{opacity:.3}' +
'@keyframes asbSpin{to{transform:rotate(360deg)}}' +
/* 光环（Siri 线圈的 3D 版）：一条倾斜的光带环绕球体，拆成前后两半——上半在
   球后、下半在球前（rotateX 正角把下缘转向观者），DOM 顺序 + clip-path 各取
   一半即成真实遮挡；环带在环平面内自转（transform，合成器）。v3 起**不再常驻**：
   待机 opacity 0 + scale .8 + 转动 paused（零合成开销），活跃态从核里聚出来
   （淡入 + 放大到位）再开始转——「状态 = 形态变化」的签名。第二条更细、反向
   倾斜的环只在 lv2 出现。 */
'.asb-ring{position:absolute;inset:-7px;border-radius:50%;pointer-events:none;' +
'transform:rotateX(68deg) rotateZ(-22deg) scale(.8);opacity:0;' +
'transition:opacity .5s ease,transform .5s var(--xz-ease,ease),inset .4s ease}' +
'.asb-ring.back{clip-path:inset(0 0 50% 0)}' +
'.asb-ring.front{clip-path:inset(50% 0 0 0);z-index:3}' +
'.asb-ring::before{content:"";position:absolute;inset:0;border-radius:50%;padding:2.4px;' +
'background:conic-gradient(from 0deg,var(--xz-rb-stops));' +
'-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);' +
'-webkit-mask-composite:xor;mask-composite:exclude;' +
'animation:asbSpin 12s linear infinite paused}' +
'.asb-ring.r2{transform:rotateX(64deg) rotateZ(38deg) scale(.8);inset:-4px;display:none}' +
'.asb-ring.r2::before{padding:1.6px;animation-duration:17s;animation-direction:reverse}' +
'.asb-lv2 .asb-ring.r2{display:block}' +
'.asb-ball.st-listening .asb-ring,.asb-ball.st-thinking .asb-ring,.asb-ball.st-speaking .asb-ring,' +
'.asb-ball.st-agent .asb-ring,.asb-ball.st-teach .asb-ring{opacity:1;' +
'transform:rotateX(68deg) rotateZ(-22deg) scale(1)}' +
'.asb-ball.st-listening .asb-ring.r2,.asb-ball.st-thinking .asb-ring.r2,' +
'.asb-ball.st-speaking .asb-ring.r2,.asb-ball.st-agent .asb-ring.r2,' +
'.asb-ball.st-teach .asb-ring.r2{opacity:.6;transform:rotateX(64deg) rotateZ(38deg) scale(1)}' +
'.asb-ball.st-listening .asb-ring::before,.asb-ball.st-thinking .asb-ring::before,' +
'.asb-ball.st-speaking .asb-ring::before,.asb-ball.st-agent .asb-ring::before,' +
'.asb-ball.st-teach .asb-ring::before{animation-play-state:running}' +
'.asb-ball.st-listening .asb-ring{inset:-9px}' +
'.asb-ball.st-listening .asb-ring::before{padding:3.2px;animation-duration:4.5s}' +
'.asb-ball.st-thinking .asb-ring::before,.asb-ball.st-agent .asb-ring::before{animation-duration:2.6s}' +
'.asb-ball.st-speaking .asb-ring::before,.asb-ball.st-teach .asb-ring::before{animation-duration:4s}' +
/* 辉光 = 环境光 + 带色接触阴影，呼吸 6.5s ±5%/透明度 .45→.9（亮度变化比缩放
   可感知得多）；connecting 快而浅（在等授权）、error 不动（静态 + 文字解释失败） */
'.asb-glow{position:absolute;inset:-8px;border-radius:50%;pointer-events:none;' +
'background:radial-gradient(circle closest-side,color-mix(in srgb,var(--xz-core) 60%,transparent) 0%,' +
'color-mix(in srgb,var(--xz-core) 30%,transparent) 55%,transparent 100%);' +
'filter:blur(4px);animation:asbBreath 6.5s ease-in-out infinite;transition:--xz-core .5s ease}' +
'@keyframes asbBreath{0%,100%{transform:scale(.98);opacity:.45}50%{transform:scale(1.08);opacity:.9}}' +
'.asb-ball.st-connecting .asb-glow{animation:asbBreath 1.4s ease-in-out infinite;opacity:.5}' +
'.asb-ball.st-error .asb-glow{animation:none;opacity:.7}' +
/* 球内图标退场（v3）：白色 glyph 把心智钉在「按钮」上；悬停/聚焦/首次提示时
   才浮现，常态下球身是空的（Siri/ChatGPT 同款）。可发现性由悬停标签片接住。 */
'.asb-ic{position:relative;z-index:3;display:flex;align-items:center;justify-content:center;' +
'opacity:0;transform:scale(.8);transition:opacity .25s ease,transform .25s var(--xz-ease,ease);' +
'filter:drop-shadow(0 1px 3px rgba(0,0,0,.35))}' +
'.asb-ball:hover .asb-ic,.asb-ball:focus-visible .asb-ic,.asb-wrap.asb-tipon .asb-ic{' +
'opacity:1;transform:none}' +
/* 悬停标签片：贴球的一侧（球吸左边→片在右，吸右边→片在左），延迟 .25s 出现
   防扫过闪烁；面板开着时不出（面板本身就是答案）。首次启动展示 4s 一次。 */
'.asb-tip{position:absolute;top:50%;--tip-dx:6px;' +
'transform:translateY(-50%) translateX(var(--tip-dx));white-space:nowrap;' +
'font-size:.76rem;font-weight:600;line-height:1;padding:.44rem .66rem;border-radius:999px;' +
'color:var(--xz-txt,#111);background:var(--xz-glass,var(--xz-bg,#fff));' +
'border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'box-shadow:var(--xz-sh-amb,0 2px 8px rgba(15,27,45,.08));pointer-events:none;opacity:0;' +
'-webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);z-index:1;' +
'transition:opacity .2s ease .25s,transform .25s var(--xz-ease,ease) .25s}' +
'.asb-wrap.asb-side-r .asb-tip{right:calc(100% + 10px)}' +
'.asb-wrap.asb-side-l .asb-tip{left:calc(100% + 10px);--tip-dx:-6px}' +
'.asb-wrap:hover .asb-tip,.asb-wrap.asb-tipon .asb-tip,.asb-wrap.asb-holding .asb-tip{' +
'opacity:1;transform:translateY(-50%) translateX(0);transition-delay:0s}' +
'.asb-wrap.asb-open .asb-tip{display:none}' +
'.asb-ping{position:absolute;inset:-3px;border-radius:50%;border:2px solid var(--xz-accent,#4f6ef7);' +
'opacity:0;pointer-events:none}' +
'.asb-ball.st-hint .asb-ping{animation:asbPing 30s cubic-bezier(0,0,.2,1) 3}' +
'@keyframes asbPing{0%{transform:scale(.85);opacity:.75}7%{transform:scale(1.9);opacity:0}100%{opacity:0}}' +
'.asb-alertfx{position:absolute;inset:0;border-radius:50%;pointer-events:none;opacity:0;' +
'background:radial-gradient(circle,rgba(251,191,36,.28) 20%,rgba(245,158,11,.62) 100%)}' +
'.asb-ball.st-alert .asb-alertfx{animation:asbAmber 2.1s ease-in-out infinite}' +
'@keyframes asbAmber{0%,100%{opacity:0}18%,42%{opacity:1}30%{opacity:.35}}' +
/* 各状态的动效节奏（颜色已由上面的 --xz-core 表接管；活跃态的能量由画布粒子承担） */
'.asb-ball.st-listening .asb-glow{animation:asbBreath 3.2s ease-in-out infinite}' +
'.asb-ball.st-thinking .asb-glow{animation:asbBreath 1.7s ease-in-out infinite}' +
'.asb-ball.st-speaking .asb-glow{animation:asbBreath 2.2s ease-in-out infinite}' +
'.asb-ball.okflash .asb-orb{filter:brightness(1.12) saturate(1.2)}' +
'.asb-sweep{position:absolute;inset:-5px;border-radius:50%;pointer-events:none;opacity:0;' +
'background:conic-gradient(from 0deg,transparent 0deg,color-mix(in srgb,var(--xz-rb-1,#22d3ee) 55%,transparent) 40deg,' +
'transparent 80deg);filter:blur(1px)}' +
'.asb-ball.st-teach .asb-sweep{opacity:1;animation:asbSpin 3s linear infinite}' +
'.asb-fx{position:absolute;left:50%;top:50%;width:168px;height:168px;' +
'margin:-84px 0 0 -84px;pointer-events:none;z-index:1;display:none}' +
/* 画质档/减动效（v3 修正）：lv0 与系统 reduced-motion 同语义——**去运动、留状态
   脉冲**（Apple 同款状态灯降级）。v2 只给 thinking 留了脉冲，听/说/做在 lv0 下
   完全无反馈，等于把状态信息连同装饰一起删了。 */
'.asb-lv0 .asb-lc,.asb-lv0 .asb-orb,.asb-lv0 .asb-glow,' +
'.asb-lv0 .asb-sweep,.asb-lv0 .asb-ring::before{animation:none}' +
'.asb-lv0 .asb-ball.st-hint .asb-ping{animation:none}' +
'.asb-lv0 .asb-ball.st-alert .asb-alertfx{animation:asbFade 2.2s ease-in-out infinite}' +
'.asb-lv0 .asb-ball.st-thinking .asb-orb{animation:asbFade 1.8s ease-in-out infinite}' +
'.asb-lv0 .asb-ball.st-listening .asb-orb{animation:asbFade 1.1s ease-in-out infinite}' +
'.asb-lv0 .asb-ball.st-speaking .asb-orb{animation:asbFade 1.5s ease-in-out infinite}' +
'.asb-lv0 .asb-ball.st-connecting .asb-orb{animation:asbFade 1.4s ease-in-out infinite}' +
'.asb-lv0 .asb-ball.st-agent .asb-orb,.asb-lv0 .asb-ball.st-teach .asb-orb{' +
'animation:asbFade 2.4s ease-in-out infinite}' +
'@keyframes asbFade{0%,100%{opacity:1}50%{opacity:.62}}' +
'@media (prefers-reduced-motion:reduce){' +
'.asb-lc,.asb-orb,.asb-glow,.asb-ping,.asb-sweep,.asb-ring::before{animation:none!important}' +
'.asb-ball.st-thinking .asb-orb{animation:asbFade 1.8s ease-in-out infinite!important}' +
'.asb-ball.st-listening .asb-orb{animation:asbFade 1.1s ease-in-out infinite!important}' +
'.asb-ball.st-speaking .asb-orb{animation:asbFade 1.5s ease-in-out infinite!important}' +
'.asb-ball.st-connecting .asb-orb{animation:asbFade 1.4s ease-in-out infinite!important}' +
'}' +
'.asb-hero{display:none;padding:.25rem .8rem 0;flex-shrink:0}' +
'.asb-hero.on{display:block}' +
'.asb-hero canvas{width:100%;height:44px;display:block}' +
/* ── 语音对话块（P1 2026-09-06）：取代输入区——「语音替代打字」。大球是同一状态
   机的大尺寸渲染（Canvas 2D，只在本块存在时跑 rAF）；对话内容仍在上方消息流里
   流动（识别文本=用户气泡、回答=AI 气泡，即实时字幕），不另开屏——ChatGPT 自
   2025-11 也把语音并回对话线程。点大球=打断（与「打断」按钮同义）。 */
'.asb-voice{display:flex;flex-direction:column;align-items:center;gap:.3rem;' +
'padding:.3rem .8rem .7rem;border-top:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 70%,transparent);' +
'flex-shrink:0;position:relative}' +
'.asb-vorb{display:block;width:168px;height:168px;border-radius:50%;cursor:pointer}' +
'.asb-vorb:focus-visible{outline:2px solid var(--xz-accent,#4f6ef7);outline-offset:4px}' +
'.asb-v-st{font-size:.8rem;font-weight:500;color:var(--xz-muted,#888);min-height:1.2em;' +
'text-align:center;transition:color .3s ease}' +
'.asb-panel.st-listening .asb-v-st,.asb-panel.st-thinking .asb-v-st,' +
'.asb-panel.st-speaking .asb-v-st{color:color-mix(in srgb,var(--xz-core) 72%,var(--xz-txt,#111))}' +
'.asb-v-acts{display:flex;gap:.45rem;align-items:center;margin-top:.15rem}' +
'.asb-v-b{border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);background:none;' +
'color:var(--xz-txt,#111);cursor:pointer;display:inline-flex;align-items:center;gap:.3em;' +
'font-size:.78rem;font-family:inherit;padding:.36rem .7rem;border-radius:999px;min-height:30px;' +
'transition:border-color .15s ease,background .15s ease,color .15s ease}' +
'.asb-v-b:hover{background:var(--xz-hover,rgba(0,0,0,.04));border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-v-b.primary{background:var(--xz-accent,#4f6ef7);color:#fff;border-color:transparent}' +
'.asb-v-b.primary:hover{filter:brightness(1.06)}' +
'.asb-v-b[aria-pressed="true"]{color:var(--xz-muted,#888)}' +
'.asb-v-b svg{width:15px;height:15px}' +
'@media (max-width:480px){.asb-vorb{width:132px;height:132px}}' +
'.asb-fbq{display:flex;gap:.35rem;align-items:center}' +
'.asb-fb .asb-say.on{background:var(--xz-accent,#4f6ef7);color:#fff;' +
'border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-dot{position:absolute;top:2px;right:2px;width:10px;height:10px;border-radius:50%;' +
'background:#ef4444;border:2px solid #fff;display:none;z-index:4}' +
'.asb-ball.hasdot .asb-dot{display:block}' +
/* box-sizing 是拖拽的**正确性前提**，不是风格偏好：rect.width 含 1px 边框而
   style.width 默认写 content-box，两者差 2px。存 rect / 写 style 形成闭环后
   每拖一次就胖 2px（实测 382→384→386…），几十次后面板自己长满屏。 */
/* ── 面板壳 v2（2026-09-05）：玻璃 + 分层阴影 + 边缘泛光 ─────────────────
   根节点自身透明，底/边/高光/backdrop 模糊全放在 ::after（z-1），泛光放在
   ::before（z-2）——固定定位的面板本身就是层叠上下文，负 z 子层会画在根背景
   **之上**，只有把「背景」也做成子层，泛光才能真的躲到玻璃后面（隔着 14%
   的玻璃透进来一点，正是想要的「光从边缘渗进来」）。overflow 由 hidden 改
   visible：泛光与八向手柄都长在盒外；直属子级没有会越过圆角的背景，滚动由
   .asb-body 自己的 overflow-y 承担。泛光只在听/想/说三态亮，steps(120) 把
   模糊层重画压到 20fps——它是软光，不需要 60fps。 */
'.asb-panel{position:fixed;z-index:9999;box-sizing:border-box;width:392px;' +
'max-width:calc(100vw - 24px);--xz-core:var(--xz-accent,#4f6ef7);' +
'height:min(600px,78vh);display:none;flex-direction:column;overflow:visible;' +
'background:transparent;color:var(--xz-txt,#111);border:none;' +
'border-radius:20px;box-shadow:var(--xz-sh-key,0 12px 40px rgba(0,0,0,.24)),' +
'var(--xz-sh-amb,0 2px 8px rgba(15,27,45,.08))}' +
/* 入场动画挂一次性 .asb-in（开面板加、结束即摘）而不是常驻在 .asb-panel 上：
   常驻时每次拖拽的 .asb-moving{animation:none} 一撤，动画就从头重播；且带
   scale 的入场期内 getBoundingClientRect 读到的是缩放中的尺寸，若此时开始
   拖动会把 378.6×557.9 固化成自定义几何（探针 G2b 实锤）。 */
/* 入场类叫 asb-enter（2026-09-06 修）：此前叫 asb-in，与输入框同名——面板在
   300ms 入场期间吃到 textarea 的 max-height:132px/圆角/底色，先矮后跳。 */
'.asb-panel.asb-enter{animation:asbUp .26s var(--xz-ease,ease)}' +
'.asb-panel::after{content:"";position:absolute;inset:0;border-radius:inherit;z-index:-1;' +
'pointer-events:none;box-sizing:border-box;background:var(--xz-glass,var(--xz-bg,#fff));' +
'border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'box-shadow:inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6));' +
'-webkit-backdrop-filter:blur(18px) saturate(1.3);backdrop-filter:blur(18px) saturate(1.3)}' +
'.asb-panel::before{content:"";position:absolute;inset:-2px;border-radius:22px;z-index:-2;' +
'pointer-events:none;background:conic-gradient(from var(--xz-a),var(--xz-rb-stops));' +
'filter:blur(14px);opacity:0;transition:opacity .5s ease}' +
/* 动画只挂在亮着的三态上：opacity:0 的层若也在补间角度，每步都会白白重画 */
'.asb-panel.st-listening::before,.asb-panel.st-thinking::before,.asb-panel.st-speaking::before{' +
'opacity:.5;animation:asbRibbon 6s steps(120) infinite}' +
'.asb-panel.asb-dark.st-listening::before,.asb-panel.asb-dark.st-thinking::before,' +
'.asb-panel.asb-dark.st-speaking::before{opacity:.85}' +
'.asb-panel.asb-lite::before,.asb-panel.asb-lv0::before,.asb-panel.asb-lv1::before{display:none}' +
'.asb-panel.open,.asb-panel.closing{display:flex}' +
'.asb-panel.closing{animation:asbDown .16s ease forwards;pointer-events:none}' +
'@keyframes asbDown{to{opacity:0;transform:translateY(6px) scale(.98)}}' +
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
'.asb-rs.se::after{content:"";position:absolute;right:7px;bottom:7px;width:7px;' +
'height:7px;border-right:2px solid var(--xz-muted,#bbb);' +
'border-bottom:2px solid var(--xz-muted,#bbb);border-radius:0 0 3px 0;opacity:.6}' +
'.asb-rs:focus-visible{outline:2px solid var(--xz-accent,#4f6ef7);outline-offset:1px}' +
/* 吸附提示：贴边命中时描边一下，让「吸住了」有反馈 */
'.asb-panel.asb-snapped{box-shadow:var(--xz-sh-key,0 12px 40px rgba(0,0,0,.24)),' +
'0 0 0 2px var(--xz-accent,#4f6ef7)}' +
'.asb-hd-rst{border:none;background:none;color:var(--xz-muted,#888);' +
'cursor:pointer;font-size:.95rem;width:30px;height:30px;padding:0;border-radius:50%;' +
'line-height:1;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center}' +
'.asb-hd-rst:hover{background:var(--xz-hover,rgba(0,0,0,.05));' +
'color:var(--xz-txt,#111)}' +
'.asb-hd-rst[hidden]{display:none}' +
'@keyframes asbUp{from{opacity:0;transform:translateY(10px) scale(.96)}to{opacity:1;transform:none}}' +
'.asb-hd{display:flex;align-items:center;gap:.5rem;padding:.6rem .8rem;' +
'border-bottom:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 70%,transparent);flex-shrink:0}' +
/* 标题栏迷你光核（v2）：与球同一套球面 shading + 一条细光环，状态色相镜像球
   （同一隐喻，球↔面板视觉连续）；名字下方一行状态短句由 syncOrb 写入——
   「小智在干什么」第一次有了明确的文字信号，不再只靠球的色差。 */
'.asb-hd-ic{position:relative;width:26px;height:26px;border-radius:50%;flex-shrink:0;' +
'display:flex;align-items:center;justify-content:center;color:#fff;isolation:isolate}' +
'.asb-hd-core{position:absolute;inset:0;border-radius:50%;z-index:-1;' +
'background:radial-gradient(circle closest-side at 50% 50%,' +
'color-mix(in srgb,var(--xz-core) 58%,#fff) 0%,var(--xz-core) 46%,' +
'color-mix(in srgb,var(--xz-core) 84%,#fff) 80%,' +
'color-mix(in srgb,var(--xz-core) 72%,transparent) 100%);' +
'-webkit-mask-image:radial-gradient(circle closest-side,#000 0 74%,transparent 100%);' +
'mask-image:radial-gradient(circle closest-side,#000 0 74%,transparent 100%);' +
'box-shadow:0 4px 10px -3px color-mix(in srgb,var(--xz-core) 55%,transparent);' +
'transition:--xz-core .5s ease}' +
/* 迷你光环与球同律：待机隐没、活跃聚成环并转动 */
'.asb-hd-ring{position:absolute;inset:-4px;border-radius:50%;pointer-events:none;' +
'transform:rotateX(68deg) rotateZ(-22deg) scale(.8);opacity:0;z-index:1;' +
'transition:opacity .5s ease,transform .5s var(--xz-ease,ease)}' +
'.asb-hd-ring::before{content:"";position:absolute;inset:0;border-radius:50%;padding:1.5px;' +
'background:conic-gradient(from 0deg,var(--xz-rb-stops));' +
'-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);' +
'-webkit-mask-composite:xor;mask-composite:exclude;animation:asbSpin 12s linear infinite paused}' +
'.asb-panel.st-listening .asb-hd-ring,.asb-panel.st-thinking .asb-hd-ring,' +
'.asb-panel.st-speaking .asb-hd-ring,.asb-panel.st-agent .asb-hd-ring,' +
'.asb-panel.st-teach .asb-hd-ring{opacity:.85;transform:rotateX(68deg) rotateZ(-22deg) scale(1)}' +
'.asb-panel.st-listening .asb-hd-ring::before,.asb-panel.st-thinking .asb-hd-ring::before,' +
'.asb-panel.st-speaking .asb-hd-ring::before,.asb-panel.st-agent .asb-hd-ring::before,' +
'.asb-panel.st-teach .asb-hd-ring::before{animation-play-state:running}' +
'.asb-hd-ic svg{width:14px;height:14px;position:relative;z-index:2;' +
'filter:drop-shadow(0 1px 2px rgba(0,0,0,.35))}' +
'.asb-panel.st-listening .asb-hd-ring::before{animation-duration:4.5s}' +
'.asb-panel.st-thinking .asb-hd-ring::before,.asb-panel.st-agent .asb-hd-ring::before{' +
'animation-duration:2.6s}' +
'.asb-panel.st-speaking .asb-hd-ring::before,.asb-panel.st-teach .asb-hd-ring::before{' +
'animation-duration:4s}' +
'.asb-panel.asb-lv0 .asb-hd-ring::before{animation:none}' +
'.asb-hd-name{flex:1;min-width:0;display:flex;flex-direction:column;line-height:1.2}' +
'.asb-hd-name b{font-weight:700;font-size:.86rem;overflow:hidden;text-overflow:ellipsis;' +
'white-space:nowrap}' +
'.asb-hd-st{font-style:normal;font-size:.75rem;font-weight:500;color:var(--xz-muted,#888);' +
'overflow:hidden;text-overflow:ellipsis;white-space:nowrap;transition:color .3s ease}' +
/* 状态句取当前核心色（压 28% 文字色保对比度；琥珀警示态用 ink 级别的深琥珀） */
'.asb-panel.st-listening .asb-hd-st,.asb-panel.st-thinking .asb-hd-st,' +
'.asb-panel.st-speaking .asb-hd-st,.asb-panel.st-agent .asb-hd-st,' +
'.asb-panel.st-teach .asb-hd-st{color:color-mix(in srgb,var(--xz-core) 72%,var(--xz-txt,#111))}' +
'.asb-panel.st-alert .asb-hd-st{color:var(--tk-warn-ink,#b45309)}' +
'.asb-panel.st-error .asb-hd-st{color:var(--tk-rose,#dc2626)}' +
'.asb-x{border:none;background:none;color:var(--xz-muted,#888);cursor:pointer;' +
'font-size:1.05rem;width:30px;height:30px;padding:0;border-radius:50%;line-height:1;' +
'display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;' +
'transition:background .15s ease,color .15s ease}' +
'.asb-x:hover{background:var(--xz-hover,rgba(0,0,0,.05));color:var(--xz-txt,#111)}' +
'.asb-x:focus-visible,.asb-hd-pair:focus-visible,.asb-hd-pc:focus-visible,' +
'.asb-hd-rst:focus-visible{outline:2px solid var(--xz-accent,#4f6ef7);outline-offset:1px}' +
/* 图标态：说明用 */
'.asb-i-ok{color:#059669}' +
'.asb-spin{animation:asbSpin 1s linear infinite}' +
'.asb-msg--sys{font-size:.8rem;color:var(--xz-muted,#888);display:inline-flex;' +
'align-items:center;gap:.35em}' +
/* ── 模式条（实施73 P1-1）：三大功能升格为一级导航 ──────────────────────
   段控 + 滑块指示器；只有一个模式在册（兄弟组件缺席）时整条隐藏 `.solo`，
   退化成旧的「纯问答面板」而不是留一个点不动的空段控。 */
'.asb-modes{position:relative;display:flex;gap:.2rem;flex-shrink:0;' +
'margin:.55rem .85rem .2rem;padding:.22rem;border-radius:14px;' +
'background:color-mix(in srgb,var(--xz-input,#f3f4f6) 80%,transparent)}' +
'.asb-modes.solo{display:none}' +
/* 滑块带当前模式的色调投影（问答=蓝、教学=青、替我做=紫）——三大能力第一次
   有了颜色编码，与球在各态的色相偏移同一套语言（data-cur 由 syncModes 写） */
'.asb-modes-ind{position:absolute;top:.22rem;bottom:.22rem;left:0;border-radius:11px;' +
'background:var(--xz-bg,#fff);box-shadow:0 1px 4px rgba(0,0,0,.14),inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6));' +
'z-index:0;pointer-events:none;transition:transform .22s var(--xz-ease,ease),width .22s var(--xz-ease,ease),' +
'box-shadow .3s ease}' +
'.asb-modes[data-cur="chat"] .asb-modes-ind{box-shadow:0 2px 8px -2px color-mix(in srgb,var(--xz-rb-2,#4f6ef7) 45%,transparent),' +
'inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6))}' +
'.asb-modes[data-cur="teach"] .asb-modes-ind{box-shadow:0 2px 8px -2px color-mix(in srgb,var(--xz-rb-1,#22d3ee) 55%,transparent),' +
'inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6))}' +
'.asb-modes[data-cur="agent"] .asb-modes-ind{box-shadow:0 2px 8px -2px color-mix(in srgb,var(--xz-rb-3,#8b5cf6) 50%,transparent),' +
'inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6))}' +
'.asb-mode{position:relative;z-index:1;flex:1;border:none;background:none;' +
'cursor:pointer;font-family:inherit;font-size:.8rem;font-weight:600;' +
'color:var(--xz-muted,#888);padding:.48rem .3rem;border-radius:11px;display:flex;' +
'align-items:center;justify-content:center;gap:.32rem;white-space:nowrap;' +
'transition:color .2s ease}' +
'.asb-mode i{font-style:normal;font-size:1rem;line-height:1;display:inline-flex}' +
'.asb-mode[aria-checked="true"]{color:var(--xz-txt,#111)}' +
'.asb-modes[data-cur="chat"] .asb-mode[aria-checked="true"] i{color:var(--xz-rb-2,#4f6ef7)}' +
'.asb-modes[data-cur="teach"] .asb-mode[aria-checked="true"] i{' +
'color:color-mix(in srgb,var(--xz-rb-1,#22d3ee) 70%,var(--xz-txt,#111))}' +
'.asb-modes[data-cur="agent"] .asb-mode[aria-checked="true"] i{color:var(--xz-rb-3,#8b5cf6)}' +
'.asb-mode:focus-visible{outline:2px solid var(--xz-accent,#4f6ef7);outline-offset:1px}' +
'@media (prefers-reduced-motion:reduce){.asb-modes-ind{transition:none}}' +
/* ── 次级页签行（P1-5）：报障 · 我的 · 常问 · ⚙ ───────────────────────── */
'.asb-subtabs{display:flex;gap:.15rem;align-items:center;flex-shrink:0;' +
'padding:.35rem .6rem .45rem;border-top:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 70%,transparent)}' +
'.asb-sub{border:none;background:none;cursor:pointer;font-family:inherit;' +
'font-size:.76rem;color:var(--xz-muted,#888);padding:.3rem .62rem;border-radius:999px;' +
'transition:background .15s ease,color .15s ease}' +
'.asb-sub:hover{background:var(--xz-hover,rgba(0,0,0,.05));color:var(--xz-txt,#111)}' +
'.asb-sub.cur{background:color-mix(in srgb,var(--xz-accent,#4f6ef7) 12%,transparent);' +
'color:var(--xz-accent,#4f6ef7);font-weight:600}' +
'.asb-sub--ic{width:30px;height:30px;padding:0;display:inline-flex;align-items:center;' +
'justify-content:center;font-size:1rem}' +
'.asb-sub-sp{flex:1}' +
/* ── 标题栏手机操控（P1-2）：通道不是第四种能力，故不占模式条 ─────────── */
'.asb-hd-pair{position:relative;border:none;background:none;cursor:pointer;' +
'font-size:1rem;width:30px;height:30px;padding:0;border-radius:50%;line-height:1;' +
'display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;' +
'color:var(--xz-muted,#888);transition:background .15s ease,color .15s ease}' +
'.asb-hd-pair:hover{background:var(--xz-hover,rgba(0,0,0,.05));color:var(--xz-txt,#111)}' +
'.asb-hd-pair i{position:absolute;right:3px;bottom:3px;width:8px;height:8px;' +
'border-radius:50%;background:var(--xz-muted,#bbb);border:1.5px solid var(--xz-bg,#fff)}' +
'.asb-hd-pair.on{color:var(--xz-txt,#111)}' +
'.asb-hd-pair.on i{background:#22c55e;box-shadow:0 0 6px rgba(34,197,94,.7)}' +
/* ── 标题栏电脑操控（实施91 P1-1）：与手机键同族的通道式入口，enabled 才显 ── */
'.asb-hd-pc{position:relative;border:none;background:none;cursor:pointer;' +
'font-size:1rem;width:30px;height:30px;padding:0;border-radius:50%;line-height:1;' +
'display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;' +
'color:var(--xz-muted,#888);transition:background .15s ease,color .15s ease}' +
'.asb-hd-pc[hidden]{display:none}' +
'.asb-hd-pc:hover{background:var(--xz-hover,rgba(0,0,0,.05));color:var(--xz-txt,#111)}' +
'.asb-hd-pc i{position:absolute;right:3px;bottom:3px;width:8px;height:8px;' +
'border-radius:50%;background:var(--xz-muted,#bbb);border:1.5px solid var(--xz-bg,#fff)}' +
'.asb-hd-pc.on{color:var(--xz-txt,#111)}' +
'.asb-hd-pc.on i{background:#22c55e;box-shadow:0 0 6px rgba(34,197,94,.7)}' +
/* ── 模式内容通用卡（球提供，教学/替我做挂载时直接复用＝三个模式一套度量） */
'.asb-md{display:flex;flex-direction:column;gap:.6rem}' +
'.asb-md-hero{position:relative;border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 80%,transparent);' +
'border-radius:14px;background:color-mix(in srgb,var(--xz-input,#f7f7f9) 70%,var(--xz-bg,#fff));' +
'padding:.75rem .8rem;display:flex;flex-direction:column;gap:.5rem;' +
'box-shadow:inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6))}' +
'.asb-md-t{font-size:.86rem;font-weight:700;display:flex;align-items:center;gap:.4rem}' +
'.asb-md-t .asb-i{color:var(--xz-accent,#4f6ef7);font-size:1.05rem}' +
'.asb-md-d{font-size:.78rem;color:var(--xz-muted,#888);line-height:1.6}' +
/* 主按钮：background 声明保留 accent 实色（门禁钉），再叠一层蓝→紫渐变 */
'.asb-md-go{border:none;border-radius:12px;background:var(--xz-accent,#4f6ef7);' +
'background-image:linear-gradient(145deg,var(--xz-rb-2,#4f6ef7),var(--xz-rb-3,#8b5cf6));' +
'color:#fff;cursor:pointer;font-family:inherit;font-size:.84rem;font-weight:600;' +
'padding:.6rem 1rem;display:inline-flex;align-items:center;justify-content:center;gap:.4em;' +
'box-shadow:0 8px 18px -10px var(--xz-tint,rgba(79,110,247,.5));' +
'transition:transform .15s ease,filter .15s ease}' +
'.asb-md-go:hover{filter:brightness(1.06)}' +
'.asb-md-go:active{transform:scale(.98)}' +
'.asb-md-go.off{background:#ef4444;background-image:none}' +
'.asb-md-row{display:flex;gap:.4rem;flex-wrap:wrap}' +
'.asb-md-b{border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 90%,transparent);' +
'background:var(--xz-bg,#fff);color:var(--xz-txt,#333);border-radius:10px;cursor:pointer;' +
'font-family:inherit;font-size:.78rem;padding:.4rem .7rem;display:inline-flex;' +
'align-items:center;gap:.35em;transition:border-color .15s ease,color .15s ease}' +
'.asb-md-b:hover{border-color:var(--xz-accent,#4f6ef7);color:var(--xz-accent,#4f6ef7)}' +
'.asb-md-safe{font-size:.75rem;color:var(--xz-muted,#888);line-height:1.6;display:flex;' +
'align-items:flex-start;gap:.35em}' +
'.asb-md-safe .asb-i{flex-shrink:0;margin-top:.18em;color:#059669}' +
/* ── 主次分层（P1-2，2026-08-28）──────────────────────────────────────────
   此前「替我做」两张卡共用 .asb-md-hero＝同边框同底色同 padding，于是核心
   能力与手机通道视觉权重相同，整屏零焦点（老板实录：版面太碎没有重点）。
   现在：主卡＝accent 左轴 + 提亮标题 + 唯一实心主按钮（.asb-md-go，样式表
   里早已定义却从没被调用过）；次要入口降级为一行 .asb-md-sub。 */
/* 主卡：白玻璃 + 三色渐变左轴（伪元素，卡自身圆角靠 overflow 裁） */
'.asb-md-hero--pri{border-color:color-mix(in srgb,var(--xz-bd,#ddd) 60%,transparent);' +
'background:var(--xz-bg,#fff);overflow:hidden;' +
'box-shadow:var(--xz-sh-amb,0 2px 8px rgba(15,27,45,.08)),inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6))}' +
'.asb-md-hero--pri::before{content:"";position:absolute;left:0;top:0;bottom:0;width:3px;' +
'background:linear-gradient(180deg,var(--xz-rb-1,#22d3ee),var(--xz-rb-2,#4f6ef7),var(--xz-rb-3,#8b5cf6))}' +
'.asb-md-hero--pri .asb-md-t{font-size:.94rem}' +
'.asb-md-hero--pri .asb-md-d{font-size:.8rem;color:var(--xz-txt,#333)}' +
'.asb-md-hd{display:flex;align-items:center;gap:.4rem}' +
'.asb-md-hd .asb-md-t{flex:1;min-width:0}' +
'.asb-md-lnk{border:none;background:none;cursor:pointer;font-family:inherit;' +
'font-size:.76rem;color:var(--xz-accent,#4f6ef7);padding:.15rem .3rem;border-radius:6px}' +
'.asb-md-lnk:hover{text-decoration:underline}' +
'.asb-md-sub{display:flex;align-items:center;gap:.55rem;width:100%;' +
'text-align:left;box-sizing:border-box;border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'border-radius:12px;background:none;cursor:pointer;font-family:inherit;' +
'padding:.55rem .65rem;color:var(--xz-txt,#333);transition:border-color .15s ease,background .15s ease}' +
'.asb-md-sub:hover{border-color:var(--xz-accent,#4f6ef7);background:var(--xz-hover,rgba(0,0,0,.03))}' +
'.asb-md-sub-i{font-size:1.15rem;line-height:1;flex-shrink:0;display:inline-flex;' +
'color:var(--xz-accent,#4f6ef7)}' +
'.asb-md-sub-x{flex:1;min-width:0;display:flex;flex-direction:column;gap:.12rem}' +
'.asb-md-sub-x b{font-size:.82rem;font-weight:600}' +
'.asb-md-sub-x i{font-style:normal;font-size:.75rem;color:var(--xz-muted,#888)}' +
'.asb-md-sub-go{color:var(--xz-muted,#bbb);flex-shrink:0;font-size:1rem;' +
'line-height:1;display:inline-flex}' +
/* 示例任务（P1-3）：空态是新用户最需要引导的位置，不该只说「还没有记录」。
   点一条＝填进输入框并聚焦（不自动提交——替我做会改设置，得让人先看清）。 */
'.asb-md-ex{display:flex;flex-direction:column;gap:.35rem}' +
'.asb-md-ex-t{font-size:.75rem;color:var(--xz-muted,#888)}' +
/* 模式状态行（实施73 P2-1）：一句「本页 N 处可讲解」就是进入模式的理由。
   0 处/词典未就绪时同一行改说实话，所以做成中性底色而非成绩单绿。 */
'.asb-md-stat{font-size:.78rem;line-height:1.5;color:var(--xz-txt,#333);' +
'background:var(--xz-bg,#fff);border:1px solid color-mix(in srgb,var(--xz-bd,#e5e7eb) 85%,transparent);' +
'border-radius:10px;padding:.4rem .6rem}' +
'.asb-md-stat b{color:var(--xz-accent,#4f6ef7);font-size:.92rem;' +
'padding:0 .12rem}' +
'.asb-md-stat[data-n="0"],.asb-md-stat[data-n="-1"],' +
'.asb-md-stat[data-n="-2"]{color:var(--xz-muted,#888)}' +
'.asb-md-list{display:flex;flex-direction:column}' +
'.asb-md-li{display:flex;align-items:center;gap:.45rem;font-size:.78rem;' +
'border-top:1px solid color-mix(in srgb,var(--xz-bd,#eee) 80%,transparent);padding:.42rem 0}' +
'.asb-md-li .m{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;' +
'white-space:nowrap}' +
'.asb-md-li .w{color:var(--xz-muted,#888);font-size:.75rem;flex-shrink:0;' +
'font-variant-numeric:tabular-nums}' +
/* ── 常问面板（P1-6） ─────────────────────────────────────────────────── */
'.asb-faq-s{display:flex;gap:.35rem}' +
'.asb-faq-s input{flex:1;border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'border-radius:12px;background:var(--xz-input,#f7f7f9);color:var(--xz-txt,#111);font-family:inherit;' +
'font-size:.82rem;padding:.5rem .75rem;outline:none;transition:box-shadow .2s ease}' +
'.asb-faq-s input:focus{border-color:var(--xz-accent,#4f6ef7);' +
'box-shadow:0 0 0 3px color-mix(in srgb,var(--xz-accent,#4f6ef7) 18%,transparent)}' +
'.asb-faq-g{font-size:.75rem;color:var(--xz-muted,#888);margin:.35rem 0 .1rem;font-weight:600}' +
'.asb-faq-i{display:flex;align-items:center;gap:.45rem;width:100%;text-align:left;' +
'border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);background:var(--xz-bg,#fff);' +
'color:var(--xz-txt,#333);border-radius:10px;cursor:pointer;font-family:inherit;' +
'font-size:.8rem;padding:.45rem .65rem;box-sizing:border-box;transition:border-color .15s ease}' +
'.asb-faq-i:hover{border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-faq-i .q{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;' +
'white-space:nowrap}' +
'.asb-faq-i .n{font-size:.75rem;color:var(--xz-muted,#888);flex-shrink:0}' +
'.asb-body{flex:1;overflow-y:auto;padding:.75rem .85rem;display:flex;' +
'flex-direction:column;gap:.6rem;scrollbar-width:thin}' +
'.asb-chips{display:flex;flex-wrap:wrap;gap:.4rem}' +
'.asb-chips-t{font-size:.75rem;color:var(--xz-muted,#888);width:100%}' +
'.asb-chip{border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'background:color-mix(in srgb,var(--xz-input,#f7f7f9) 70%,var(--xz-bg,#fff));' +
'color:var(--xz-txt,#333);border-radius:999px;padding:.3rem .75rem;font-size:.78rem;' +
'cursor:pointer;font-family:inherit;max-width:100%;overflow:hidden;' +
'text-overflow:ellipsis;white-space:nowrap;transition:border-color .15s ease,color .15s ease,background .15s ease}' +
'.asb-chip:hover{border-color:var(--xz-accent,#4f6ef7);color:var(--xz-accent,#4f6ef7);' +
'background:color-mix(in srgb,var(--xz-accent,#4f6ef7) 8%,var(--xz-bg,#fff))}' +
/* ── 气泡系统 v2（2026-09-05）────────────────────────────────────────────
   形态：16px 圆角 + 6px 尾角；AI 侧玻璃底 + 顶部 1px 高光 + 环境阴影；用户侧
   品牌渐变 + 带色投影 + 对角高光。用户底比 accent 略深（88%→66% 混 #0b1020）
   不是审美偏好，是对比度：workspace 的 --tk-brand #1e8cf2 直接铺底时白字只有
   3.45:1（v1 一直如此），压深后主体区 ≥4.5:1；暗壳 accent 是 400 级更浅，
   .asb-dark 再压到 66%→48%。
   状态是同一节点的形态变化（pending → live → settle → 无），不再删节点重建：
   pending = 光雾 + 细光带流动 2.4s；live = 光带 3.6s + 渐变光标；settle = 光带
   减速凝成静态细线再淡出（2.2s），完成信号就是这一下「落定」。
   运行态类只加在 DOM 上，chatHistory 里的字符串永远干净（restore 后静态）。
   光带只动 --xz-a 与 opacity；blur 层的 filter 值恒定，旋转由角度补间承担，
   合成器即可完成；外发光仅 lv2 且非 .asb-lite。 */
'.asb-msg{position:relative;isolation:isolate;max-width:88%;padding:.6rem .85rem;' +
'border-radius:16px;font-size:.86rem;line-height:1.6;word-break:break-word;' +
'transition:border-color .6s ease}' +
'.asb-new{animation:asbBubbleIn .28s var(--xz-ease,ease) both}' +
'@keyframes asbBubbleIn{from{opacity:0;transform:translateY(8px) scale(.97)}' +
'to{opacity:1;transform:none}}' +
'.asb-msg.user{align-self:flex-end;color:#fff;border-bottom-right-radius:6px;' +
'transform-origin:100% 100%;' +
'background:linear-gradient(160deg,color-mix(in srgb,var(--xz-accent,#4f6ef7) 88%,#0b1020),' +
'color-mix(in srgb,var(--xz-accent,#4f6ef7) 66%,#0b1020));' +
'box-shadow:0 10px 22px -12px var(--xz-tint,rgba(79,110,247,.5)),' +
'inset 0 1px 0 rgba(255,255,255,.28)}' +
'.asb-dark .asb-msg.user{background:linear-gradient(160deg,' +
'color-mix(in srgb,var(--xz-accent,#4f6ef7) 66%,#0b1020),' +
'color-mix(in srgb,var(--xz-accent,#4f6ef7) 48%,#0b1020))}' +
'.asb-msg.user::after{content:"";position:absolute;inset:0;border-radius:inherit;' +
'pointer-events:none;z-index:-1;background:linear-gradient(115deg,rgba(255,255,255,0) 42%,' +
'rgba(255,255,255,.13) 50%,rgba(255,255,255,0) 58%)}' +
'.asb-msg.ai{align-self:flex-start;transform-origin:0 100%;' +
'background:color-mix(in srgb,var(--xz-input,#f3f4f6) 70%,var(--xz-bg,#fff));' +
'color:var(--xz-txt,#111);border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 75%,transparent);' +
'border-bottom-left-radius:6px;' +
'box-shadow:var(--xz-sh-amb,0 2px 8px rgba(15,27,45,.08)),inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6))}' +
'.asb-msg.ai.pending,.asb-msg.ai.live{border-color:transparent}' +
/* 光带描边：伪元素铺 conic，mask 挖掉内容盒只留 1.5px 环＝一条贴边流动的光带 */
'.asb-msg.ai.pending::before,.asb-msg.ai.live::before,.asb-msg.ai.settle::before,' +
'.asb-msg.ai.asb-greet::before{content:"";' +
'position:absolute;inset:0;border-radius:inherit;padding:1.5px;pointer-events:none;z-index:-1;' +
'background:conic-gradient(from var(--xz-a),var(--xz-rb-stops));' +
'-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);' +
'-webkit-mask-composite:xor;mask-composite:exclude;' +
'animation:asbRibbon 3.6s linear infinite}' +
'.asb-msg.ai.pending::before{animation-duration:2.4s}' +
'.asb-msg.ai.settle::before{animation:asbSettle 2.2s var(--xz-ease,ease) forwards}' +
/* 签名时刻：首次开面板（无历史）光带绕问候气泡扫一圈即熄——一次性、1.1s，
   安静档/减动效不播 */
'.asb-msg.ai.asb-greet::before{animation:asbGreet 1.1s var(--xz-ease,ease) forwards}' +
'@keyframes asbRibbon{to{--xz-a:360deg}}' +
'@keyframes asbSettle{0%{--xz-a:0deg;opacity:1}45%{--xz-a:250deg;opacity:.9}' +
'100%{--xz-a:290deg;opacity:0}}' +
'@keyframes asbGreet{0%{--xz-a:0deg;opacity:0}20%{opacity:1}100%{--xz-a:400deg;opacity:0}}' +
/* 外发光（lv2）：同一条 conic 再铺一层，mask 只留外圈 8px 带并模糊——光只在
   气泡外侧呼吸，正文区域始终干净 */
'.asb-lv2 .asb-msg.ai.pending::after,.asb-lv2 .asb-msg.ai.live::after{content:"";' +
'position:absolute;inset:-8px;border-radius:22px;padding:8px;pointer-events:none;z-index:-2;' +
'background:conic-gradient(from var(--xz-a),var(--xz-rb-stops));filter:blur(6px);opacity:.28;' +
'-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);' +
'-webkit-mask-composite:xor;mask-composite:exclude;' +
'animation:asbRibbon 3.6s linear infinite}' +
'.asb-dark .asb-msg.ai.pending::after,.asb-dark .asb-msg.ai.live::after{opacity:.42}' +
'.asb-lite .asb-msg.ai::after{display:none}' +
/* 思考光雾：三团着色模糊圆斑只做 transform 补间，裁在气泡内 */
'.asb-mesh{position:absolute;inset:0;border-radius:inherit;overflow:hidden;' +
'pointer-events:none;z-index:-1;opacity:.45}' +
'.asb-dark .asb-mesh{opacity:.32}' +
'.asb-mesh i{position:absolute;width:64%;padding-top:64%;border-radius:50%;filter:blur(14px);' +
'left:-22%;top:-45%;background:var(--xz-rb-1,#22d3ee);' +
'animation:asbBlob 5s ease-in-out infinite alternate}' +
'.asb-mesh i:nth-child(2){left:38%;top:-35%;background:var(--xz-rb-2,#4f6ef7);' +
'animation-duration:6.4s;animation-delay:-2s}' +
'.asb-mesh i:nth-child(3){left:8%;top:15%;background:var(--xz-rb-3,#8b5cf6);' +
'animation-duration:7.2s;animation-delay:-4s}' +
'@keyframes asbBlob{from{transform:translate(0,0) scale(1)}to{transform:translate(38%,28%) scale(1.25)}}' +
'.asb-lv0 .asb-mesh,.asb-lv1 .asb-mesh,.asb-lite .asb-mesh{display:none}' +
'.asb-tick{display:inline-block;margin-left:.45em;font-size:.75rem;color:var(--xz-muted,#888);' +
'font-variant-numeric:tabular-nums}' +
/* 流式光标：渐变小块随 token 到达闪动，回答落定即撤 */
'.asb-msg.ai.live .asb-txt::after{content:"";display:inline-block;width:.42em;height:1em;' +
'margin-left:.12em;vertical-align:-.14em;border-radius:2px;' +
'background:var(--xz-ribbon-l,var(--xz-accent,#4f6ef7));animation:asbCaret .9s ease-in-out infinite}' +
'@keyframes asbCaret{50%{opacity:.15}}' +
/* 安静档/减动效：光带静止成渐变细线，光雾与光标撤掉，入场无位移 */
'.asb-lv0 .asb-msg.ai.pending::before,.asb-lv0 .asb-msg.ai.live::before,' +
'.asb-lv0 .asb-msg.ai.settle::before,.asb-lv0 .asb-new,.asb-lv0 .asb-txt::after{animation:none}' +
'.asb-lv0 .asb-msg.ai.asb-greet::before{display:none}' +
'@media (prefers-reduced-motion:reduce){' +
'.asb-msg.ai.pending::before,.asb-msg.ai.live::before,.asb-msg.ai.settle::before,' +
'.asb-msg.ai::after,.asb-mesh i,.asb-txt::after,.asb-new,.asb-panel,.asb-panel::before,' +
'.asb-panel.closing{animation:none!important}' +
'.asb-mesh,.asb-msg.ai.asb-greet::before{display:none}}' +
/* 错误气泡视觉降级（P2）：原满饱和红描边 + #dc2626 文字在暗色壳里既刺眼
   又对比度不足；改左色条保语义、正文回 --xz-txt 保可读。 */
'.asb-msg.err{align-self:flex-start;background:rgba(239,68,68,.09);' +
'color:var(--xz-txt,#111);border:none;border-left:3px solid #ef4444;' +
'border-radius:6px 16px 16px 6px}' +
'.asb-msg code{background:rgba(127,127,127,.16);padding:.05rem .3rem;' +
'border-radius:4px;font-size:.78rem}' +
'.asb-sref{color:var(--xz-accent,#4f6ef7);font-weight:600;font-size:.75rem}' +
'.asb-srcs{align-self:flex-start;display:flex;flex-direction:column;gap:.35rem;' +
'max-width:92%}' +
'.asb-srcs-t{font-size:.75rem;color:var(--xz-muted,#888)}' +
/* 依据徽标（2026-08-29）：产品事实卡＝中性；通用知识＝琥珀提示色，因为那句
   话不是产品承诺，视觉上必须与「有文档依据」区分得开。用 color-mix 取宿主
   accent/warn，不引入新色值（品牌令牌纪律）。 */
'.asb-basis{align-self:flex-start;font-size:.75rem;color:var(--xz-muted,#888);' +
'border-left:2px solid var(--xz-bd,#ddd);padding:.08rem .5rem;line-height:1.5}' +
'.asb-basis.gen{color:#b45309;border-left-color:#f59e0b;' +
'background:rgba(245,158,11,.08);border-radius:0 8px 8px 0}' +
/* 来源卡：索引徽标依次取光谱三色（S1 青 / S2 蓝 / S3 紫），与正文 [S1] 引用同色系 */
'.asb-src{display:flex;align-items:center;gap:.45rem;' +
'border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'border-radius:10px;padding:.38rem .6rem;font-size:.78rem;color:var(--xz-txt,#333);' +
'background:var(--xz-bg,#fff);box-shadow:inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6))}' +
'.asb-src .n{--src-c:var(--xz-rb-2,#4f6ef7);display:inline-flex;align-items:center;' +
'justify-content:center;height:18px;padding:0 .45em;border-radius:999px;font-size:.68rem;' +
'font-weight:700;flex-shrink:0;color:color-mix(in srgb,var(--src-c) 70%,var(--xz-txt,#111));' +
'background:color-mix(in srgb,var(--src-c) 16%,transparent)}' +
'.asb-src:nth-of-type(1) .n{--src-c:var(--xz-rb-1,#22d3ee)}' +
'.asb-src:nth-of-type(3) .n{--src-c:var(--xz-rb-3,#8b5cf6)}' +
'.asb-src .tt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}' +
'.asb-src a{color:var(--xz-accent,#4f6ef7);text-decoration:none;flex-shrink:0;' +
'font-size:.75rem;cursor:pointer;font-weight:600}' +
/* 反馈行：28px 圆形图标按钮（v1 是 .75rem 的 emoji 文本按钮，命中面积不到 24px） */
'.asb-fb{align-self:flex-start;display:flex;gap:.4rem;align-items:center;' +
'font-size:.75rem;color:var(--xz-muted,#888)}' +
'.asb-fb button{border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'background:none;border-radius:50%;width:28px;height:28px;padding:0;' +
'cursor:pointer;font-size:.9rem;font-family:inherit;color:var(--xz-muted,#888);' +
'display:inline-flex;align-items:center;justify-content:center;' +
'transition:border-color .15s ease,color .15s ease,background .15s ease}' +
'.asb-fb button:hover{border-color:var(--xz-accent,#4f6ef7);color:var(--xz-accent,#4f6ef7);' +
'background:color-mix(in srgb,var(--xz-accent,#4f6ef7) 10%,transparent)}' +
'.asb-fb button:disabled{opacity:.45;cursor:default}' +
'.asb-fb button.ok{color:#059669;border-color:#059669}' +
'.asb-fb button:focus-visible{outline:2px solid var(--xz-accent,#4f6ef7);outline-offset:1px}' +
/* 出路按钮：v1 是虚线胶囊（设计系统里虚线＝占位/未完成语义），改实线 */
'.asb-act{align-self:flex-start;border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 90%,transparent);' +
'background:var(--xz-bg,#fff);border-radius:10px;cursor:pointer;padding:.38rem .7rem;' +
'font-size:.78rem;color:var(--xz-txt,#333);font-family:inherit;display:inline-flex;' +
'align-items:center;gap:.35em;transition:border-color .15s ease,color .15s ease}' +
'.asb-act:hover{border-color:var(--xz-accent,#4f6ef7);color:var(--xz-accent,#4f6ef7)}' +
/* 错误动作成组（P0-4）：.asb-act 自带 align-self:flex-start，两个按钮直接
   并列会各占一行；套一层 flex 让「重试 / 报障」读起来是一组出路。 */
'.asb-err-acts{align-self:flex-start;display:flex;gap:.35rem;flex-wrap:wrap}' +
'.asb-err-acts .asb-act{align-self:auto}' +
/* 线性图标通用度量：随字号、随文字色 */
'.asb-i{width:1em;height:1em;flex-shrink:0;display:inline-block;vertical-align:-.16em}' +
'button>.asb-i+span,button>.asb-i+b{margin-left:.3em}' +
/* ── 输入区 v2 ────────────────────────────────────────────────────────────
   聚焦时描边换成光谱渐变细线（padding-box/border-box 双层背景），外加 3px 品牌
   色光环；发送键改 40px 圆形渐变按钮（箭头图标），生成期变停止键并绕一圈与
   气泡同源的光带环；麦克风录音时红点保住「正在录音」的强约定，电平环走光谱。 */
'.asb-ft{display:flex;gap:.5rem;padding:.6rem .8rem;' +
'border-top:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 70%,transparent);' +
'flex-shrink:0;align-items:flex-end}' +
'.asb-in{flex:1;resize:none;border:1px solid transparent;border-radius:14px;' +
'background:linear-gradient(var(--xz-input,#f7f7f9),var(--xz-input,#f7f7f9)) padding-box,' +
'linear-gradient(color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent),' +
'color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent)) border-box;' +
'color:var(--xz-txt,#111);font-family:inherit;' +
'font-size:.86rem;line-height:1.5;padding:.55rem .8rem;max-height:132px;min-height:40px;' +
'overflow-y:auto;outline:none;box-sizing:border-box;transition:box-shadow .2s ease}' +
'.asb-in:focus{background:linear-gradient(var(--xz-input,#f7f7f9),var(--xz-input,#f7f7f9)) padding-box,' +
'var(--xz-ribbon-l,var(--xz-accent,#4f6ef7)) border-box;' +
'box-shadow:0 0 0 3px color-mix(in srgb,var(--xz-accent,#4f6ef7) 18%,transparent)}' +
'.asb-in::placeholder{color:var(--xz-muted,#888)}' +
'.asb-send{border:none;border-radius:12px;background:var(--xz-accent,#4f6ef7);color:#fff;' +
'cursor:pointer;font-family:inherit;font-size:.82rem;font-weight:600;' +
'padding:.55rem 1rem;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center;' +
'gap:.35em;box-shadow:0 6px 14px -8px var(--xz-tint,rgba(79,110,247,.5));' +
'transition:transform .15s ease,filter .15s ease}' +
'.asb-send:hover{filter:brightness(1.06)}' +
'.asb-send:active{transform:scale(.96)}' +
'.asb-send:disabled{opacity:.5;cursor:default;transform:none;filter:none}' +
'.asb-send:focus-visible{outline:2px solid var(--xz-accent,#4f6ef7);outline-offset:2px}' +
/* 对话输入区的圆形发送键：蓝→紫渐变，箭头图标 */
'.asb-send.asb-send--ic{position:relative;width:40px;height:40px;padding:0;border-radius:50%;' +
'background:linear-gradient(145deg,var(--xz-rb-2,#4f6ef7),var(--xz-rb-3,#8b5cf6));' +
'font-size:1.05rem}' +
'.asb-send.asb-send--ic .asb-i{width:1.15em;height:1.15em}' +
/* 生成期：停止键 + 绕一圈流动光带（与气泡光带同源） */
'.asb-send.asb-send--ic.busy{background:color-mix(in srgb,var(--xz-txt,#111) 72%,var(--xz-bg,#fff))}' +
'.asb-send.asb-send--ic.busy::before{content:"";position:absolute;inset:-3px;border-radius:50%;' +
'padding:2px;background:conic-gradient(from 0deg,var(--xz-rb-stops));' +
'-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);' +
'-webkit-mask-composite:xor;mask-composite:exclude;animation:asbSpin 2.4s linear infinite}' +
'.asb-lv0 .asb-send.busy::before{animation:none}' +
'.asb-mic{position:relative;width:40px;height:40px;border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'border-radius:50%;background:var(--xz-input,#f7f7f9);color:var(--xz-txt,#333);' +
'cursor:pointer;font-size:1.05rem;padding:0;flex-shrink:0;font-family:inherit;' +
'display:inline-flex;align-items:center;justify-content:center;transition:border-color .2s ease}' +
'.asb-mic:hover{border-color:var(--xz-accent,#4f6ef7);color:var(--xz-accent,#4f6ef7)}' +
'.asb-mic.rec{color:#ef4444;border-color:transparent}' +
'.asb-mic.rec::after{content:"";position:absolute;right:6px;top:6px;width:8px;height:8px;' +
'border-radius:50%;background:#ef4444;animation:asbCaret 1s ease-in-out infinite}' +
'.asb-mic.rec::before{content:"";position:absolute;inset:-3px;border-radius:50%;padding:2px;' +
'background:conic-gradient(from 0deg,var(--xz-rb-stops));' +
'-webkit-mask:linear-gradient(#000 0 0) content-box,linear-gradient(#000 0 0);' +
'-webkit-mask-composite:xor;mask-composite:exclude;animation:asbSpin 3s linear infinite}' +
'.asb-lv0 .asb-mic.rec::before{animation:none}' +
/* 报障页等处的 .asb-mic 复用为普通胶囊按钮（截当前页）：不是圆形 */
'.asb-mic.asb-mic--pill{width:auto;height:auto;border-radius:10px;padding:.4rem .7rem;' +
'font-size:.8rem;gap:.35em}' +
'.asb-an{position:fixed;inset:0;z-index:10001;background:rgba(0,0,0,.72);display:flex;' +
'flex-direction:column;align-items:center;justify-content:center;gap:.6rem}' +
'.asb-an-bar{display:flex;gap:.4rem;align-items:center;flex-wrap:wrap;justify-content:center}' +
'.asb-an-bar span{color:#fff;font-size:.78rem;margin-right:.4rem}' +
'.asb-an-bar button{border:1px solid rgba(255,255,255,.4);background:rgba(255,255,255,.12);' +
'color:#fff;border-radius:10px;cursor:pointer;font-family:inherit;font-size:.8rem;' +
'padding:.38rem .8rem;display:inline-flex;align-items:center;gap:.35em}' +
'.asb-an-bar button.on{background:var(--xz-accent,#4f6ef7);border-color:var(--xz-accent,#4f6ef7)}' +
'.asb-an-bar button.pri{background:#059669;border-color:#059669}' +
'.asb-an canvas{max-width:94vw;max-height:78vh;border-radius:8px;box-shadow:0 8px 40px rgba(0,0,0,.5);' +
'cursor:crosshair;background:#fff}' +
'.asb-tools{display:flex;gap:.3rem;flex-wrap:wrap;padding:.4rem .8rem .55rem;' +
'border-top:1px solid var(--xz-bd,#ddd);flex-shrink:0}' +
/* ⚙ 面板里复用同一批工具键，但那里它是内容不是底栏，去掉分隔线与内缩 */
'.asb-body .asb-tools{border-top:none;padding:.1rem 0;gap:.4rem}' +
'.asb-tool{border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);background:none;' +
'color:var(--xz-muted,#888);cursor:pointer;display:inline-flex;align-items:center;gap:.3em;' +
'font-size:.76rem;font-family:inherit;padding:.3rem .6rem;border-radius:999px;' +
'transition:border-color .15s ease,color .15s ease,background .15s ease}' +
'.asb-tool:hover{background:var(--xz-hover,rgba(0,0,0,.04));color:var(--xz-txt,#111);' +
'border-color:var(--xz-accent,#4f6ef7)}' +
/* 动效三档段控（偏好页整行独占）+ 诊断行 */
'.asb-fxrow{flex-basis:100%;display:flex;align-items:center;gap:.6rem;margin-top:.35rem}' +
'.asb-fx-t{font-size:.76rem;color:var(--xz-muted,#888);flex-shrink:0}' +
'.asb-seg{display:inline-flex;padding:2px;border-radius:999px;' +
'background:var(--xz-input,rgba(0,0,0,.05));' +
'border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent)}' +
'.asb-seg-b{border:none;background:none;color:var(--xz-muted,#888);cursor:pointer;' +
'font-size:.76rem;font-family:inherit;padding:.28rem .7rem;border-radius:999px;' +
'transition:background .15s ease,color .15s ease}' +
'.asb-seg-b:hover{color:var(--xz-txt,#111)}' +
'.asb-seg-b.on{background:var(--xz-bg,#fff);color:var(--xz-txt,#111);font-weight:600;' +
'box-shadow:var(--xz-sh-amb,0 1px 4px rgba(15,27,45,.12))}' +
'.asb-seg-b:focus-visible{outline:2px solid var(--xz-accent,#4f6ef7);outline-offset:1px}' +
'.asb-fxdiag{flex-basis:100%;font-size:.72rem;color:var(--xz-muted,#888);margin-top:-.1rem}' +
'.asb-rp{display:flex;flex-direction:column;gap:.6rem}' +
'.asb-rp textarea{width:100%;min-height:88px;resize:vertical;' +
'border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'border-radius:12px;background:var(--xz-input,#f7f7f9);color:var(--xz-txt,#111);' +
'font-family:inherit;font-size:.86rem;line-height:1.5;padding:.55rem .75rem;outline:none;' +
'box-sizing:border-box;transition:box-shadow .2s ease}' +
'.asb-rp textarea:focus{border-color:var(--xz-accent,#4f6ef7);' +
'box-shadow:0 0 0 3px color-mix(in srgb,var(--xz-accent,#4f6ef7) 18%,transparent)}' +
/* 提交成功后的预期管理行：比正文淡、比正文小，但必须可读（不用 opacity 调灰） */
'.asb-rp-next{display:inline-block;margin:.25rem 0;font-size:.76rem;' +
'line-height:1.5;color:var(--xz-muted,#888)}' +
'.asb-rp-shot{font-size:.78rem;color:var(--xz-muted,#888);line-height:1.8}' +
'.asb-rp-shot a{color:var(--xz-accent,#4f6ef7);cursor:pointer;text-decoration:underline}' +
'.asb-rp-thumb{position:relative;width:120px}' +
'.asb-rp-thumb img{width:120px;border-radius:10px;border:1px solid var(--xz-bd,#ddd);display:block}' +
'.asb-rp-thumb button{position:absolute;top:-8px;right:-8px;width:22px;height:22px;' +
'border-radius:50%;border:none;background:#ef4444;color:#fff;font-size:.75rem;' +
'cursor:pointer;line-height:1;display:inline-flex;align-items:center;justify-content:center;' +
'box-shadow:0 2px 6px rgba(0,0,0,.25)}' +
'.asb-env{border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);' +
'border-radius:10px;font-size:.75rem}' +
'.asb-env summary{padding:.4rem .6rem;cursor:pointer;color:var(--xz-muted,#888)}' +
'.asb-env div{padding:0 .6rem .5rem;color:var(--xz-muted,#888);line-height:1.55}' +
'.asb-tk{border:1px solid color-mix(in srgb,var(--xz-bd,#ddd) 85%,transparent);border-radius:12px;' +
'padding:.55rem .65rem;font-size:.8rem;display:flex;flex-direction:column;gap:.25rem;' +
'background:var(--xz-bg,#fff);box-shadow:inset 0 1px 0 var(--xz-hl,rgba(255,255,255,.6))}' +
'.asb-tk-hd{display:flex;align-items:center;gap:.45rem}' +
'.asb-tk-hd .id{color:var(--xz-muted,#888);font-size:.75rem;flex-shrink:0;' +
'font-variant-numeric:tabular-nums}' +
'.asb-tk-hd .tt{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;' +
'font-weight:600}' +
'.asb-tk-meta{font-size:.75rem;color:var(--xz-muted,#888)}' +
'.asb-st{flex-shrink:0;font-size:.72rem;border-radius:999px;padding:.12rem .5rem;' +
'font-weight:600;background:var(--xz-input,#f3f4f6);color:var(--xz-muted,#666)}' +
'.asb-st.fixed{background:rgba(16,185,129,.15);color:#059669}' +
'.asb-st.verified{background:rgba(99,102,241,.14);color:#6366f1}' +
'.asb-tk-note{font-size:.76rem;color:#059669}' +
'.asb-mine-bar{display:flex;justify-content:flex-end}' +
/* 空态 padding 1.2rem→.7rem（P2）：380x560 面板里固定件已吃掉约 29%，
   一句「还没有记录」独占 62px 会把真正该被看见的引导挤下去。 */
'.asb-empty{color:var(--xz-muted,#888);font-size:.8rem;text-align:center;padding:.7rem 0}' +
'.asb-live .tip-toggle{display:none!important}' +
'.asb-spot{position:fixed;z-index:10000;pointer-events:none;border:3px solid var(--xz-accent,#4f6ef7);' +
'border-radius:12px;box-shadow:0 0 0 100vmax rgba(15,23,42,.38),0 0 22px rgba(99,102,241,.65);' +
'animation:asbSpotIn .3s ease}' +
'@keyframes asbSpotIn{from{opacity:0;transform:scale(1.12)}to{opacity:1;transform:none}}' +
'.asb-nudge{position:fixed;z-index:9999;display:flex;align-items:center;gap:.5rem;max-width:300px;' +
'padding:.6rem .75rem;background:var(--xz-bg,#fff);color:var(--xz-txt,#111);' +
'border:1px solid var(--xz-bd,#ddd);border-left:3px solid #ef4444;border-radius:14px;' +
'box-shadow:var(--xz-sh-key,0 8px 28px rgba(0,0,0,.22));font-size:.8rem;line-height:1.45;' +
'animation:asbUp .25s ease}' +
'.asb-nudge button{border:1px solid var(--xz-bd,#ddd);background:var(--xz-input,#f7f7f9);' +
'color:var(--xz-txt,#333);border-radius:9px;cursor:pointer;font-family:inherit;font-size:.76rem;' +
'padding:.3rem .6rem;flex-shrink:0;display:inline-flex;align-items:center;justify-content:center}' +
'.asb-nudge [data-n="go"]{background:var(--xz-accent,#4f6ef7);border-color:var(--xz-accent,#4f6ef7);color:#fff}' +
'@media(max-width:480px){' +
'.asb-panel{left:8px!important;right:8px!important;width:auto;bottom:72px!important;' +
'top:auto!important;height:min(600px,72vh)}' +
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

  /* ── 单色线性图标集（v2 2026-09-05）────────────────────────────────────────
     24 网格、1.8 描边、圆头圆角、currentColor——替掉全部 emoji 图标。emoji 由
     操作系统字体渲染（Windows 是 Segoe 彩色 emoji），粗细/基线/配色都不受主题
     控制，同一排按钮里 🔊📋👍 各长各的；换成线性 SVG 后随文字色、随主题、随
     hover 一起变。三个 assistant-*.js 各留一份所需子集（独立 IIFE，复制是被迫的）。
     注意 SVG 属性名不得含 `on***=` 形态（零内联 handler 门禁按正则扫全文）。 */
  var ICONS = {
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    check: '<path d="M20 6 9 17l-5-5"/>',
    mic: '<path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3Z"/>' +
      '<path d="M19 10v2a7 7 0 0 1-14 0v-2"/><path d="M12 19v3"/>',
    send: '<path d="M12 19V5"/><path d="m5 12 7-7 7 7"/>',
    stop: '<rect x="6" y="6" width="12" height="12" rx="2"/>',
    speaker: '<path d="M11 5 6 9H2v6h4l5 4V5Z"/><path d="M15.54 8.46a5 5 0 0 1 0 7.07"/>' +
      '<path d="M19.07 4.93a10 10 0 0 1 0 14.14"/>',
    speakerOff: '<path d="M11 5 6 9H2v6h4l5 4V5Z"/><path d="m23 9-6 6"/><path d="m17 9 6 6"/>',
    copy: '<rect width="14" height="14" x="8" y="8" rx="2" ry="2"/>' +
      '<path d="M4 16c-1.1 0-2-.9-2-2V4c0-1.1.9-2 2-2h10c1.1 0 2 .9 2 2"/>',
    up: '<path d="M7 10v12"/><path d="M15 5.88 14 10h5.83a2 2 0 0 1 1.92 2.56l-2.33 8' +
      'A2 2 0 0 1 17.5 22H4a2 2 0 0 1-2-2v-8a2 2 0 0 1 2-2h2.76a2 2 0 0 0 1.79-1.11L12 2' +
      'a3.13 3.13 0 0 1 3 3.88Z"/>',
    down: '<path d="M17 14V2"/><path d="M9 18.12 10 14H4.17a2 2 0 0 1-1.92-2.56l2.33-8' +
      'A2 2 0 0 1 6.5 2H20a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2h-2.76a2 2 0 0 0-1.79 1.11L12 22' +
      'a3.13 3.13 0 0 1-3-3.88Z"/>',
    bug: '<path d="m8 2 1.88 1.88"/><path d="M14.12 3.88 16 2"/>' +
      '<path d="M9 7.13v-1a3.003 3.003 0 1 1 6 0v1"/>' +
      '<path d="M12 20c-3.3 0-6-2.7-6-6v-3a4 4 0 0 1 4-4h4a4 4 0 0 1 4 4v3c0 3.3-2.7 6-6 6"/>' +
      '<path d="M12 20v-9"/><path d="M6.53 9C4.6 8.8 3 7.1 3 5"/><path d="M6 13H2"/>' +
      '<path d="M3 21c0-2.1 1.7-3.9 3.8-4"/><path d="M20.97 5c0 2.1-1.6 3.8-3.5 4"/>' +
      '<path d="M22 13h-4"/><path d="M17.2 17c2.1.1 3.8 1.9 3.8 4"/>',
    camera: '<path d="M14.5 4h-5L7 7H4a2 2 0 0 0-2 2v9a2 2 0 0 0 2 2h16a2 2 0 0 0 2-2V9' +
      'a2 2 0 0 0-2-2h-3l-2.5-3z"/><circle cx="12" cy="13" r="3"/>',
    refresh: '<path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>' +
      '<path d="M21 3v5h-5"/><path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>' +
      '<path d="M8 16H3v5"/>',
    sliders: '<path d="M21 4h-3"/><path d="M11 4H3"/><path d="M21 12h-9"/><path d="M5 12H3"/>' +
      '<path d="M21 20h-5"/><path d="M9 20H3"/><path d="M14 2v4"/><path d="M8 10v4"/>' +
      '<path d="M16 18v4"/>',
    phone: '<rect width="14" height="20" x="5" y="2" rx="2" ry="2"/><path d="M12 18h.01"/>',
    monitor: '<rect width="20" height="14" x="2" y="3" rx="2"/><path d="M8 21h8"/>' +
      '<path d="M12 17v4"/>',
    expand: '<path d="M15 3h6v6"/><path d="M9 21H3v-6"/><path d="m21 3-7 7"/><path d="m3 21 7-7"/>',
    chat: '<path d="M7.9 20A9 9 0 1 0 4 16.1L2 22Z"/>',
    cap: '<path d="M21.42 10.922a1 1 0 0 0-.019-1.838L12.83 5.18a2 2 0 0 0-1.66 0L2.6 9.08' +
      'a1 1 0 0 0 0 1.832l8.57 3.908a2 2 0 0 0 1.66 0z"/><path d="M22 10v6"/>' +
      '<path d="M6 12.5V16a6 3 0 0 0 12 0v-3.5"/>',
    bolt: '<path d="M4 14a1 1 0 0 1-.78-1.63l9.9-10.2a.5.5 0 0 1 .86.46l-1.92 6.02' +
      'A1 1 0 0 0 13 10h7a1 1 0 0 1 .78 1.63l-9.9 10.2a.5.5 0 0 1-.86-.46l1.92-6.02' +
      'A1 1 0 0 0 11 14z"/>',
    undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 5.5 5.5a5.5 5.5 0 0 1' +
      '-5.5 5.5H11"/>',
    square: '<rect x="3" y="3" width="18" height="18" rx="2"/>',
    grid: '<rect width="18" height="18" x="3" y="3" rx="2"/><path d="M3 9h18"/>' +
      '<path d="M3 15h18"/><path d="M9 3v18"/><path d="M15 3v18"/>',
    play: '<path d="M6 3l14 9-14 9z"/>',
    keyboard: '<rect width="20" height="16" x="2" y="4" rx="2"/><path d="M6 8h.01"/>' +
      '<path d="M10 8h.01"/><path d="M14 8h.01"/><path d="M18 8h.01"/><path d="M8 12h.01"/>' +
      '<path d="M12 12h.01"/><path d="M16 12h.01"/><path d="M7 16h10"/>',
    command: '<path d="M15 6v12a3 3 0 1 0 3-3H6a3 3 0 1 0 3 3V6a3 3 0 1 0-3 3h12' +
      'a3 3 0 1 0-3-3"/>',
    wrench: '<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77' +
      'a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94' +
      'l-3.76 3.76z"/>',
    bulb: '<path d="M15 14c.2-1 .7-1.7 1.5-2.5 1-.9 1.5-2.2 1.5-3.5A6 6 0 0 0 6 8c0 1 .2 2.2' +
      ' 1.5 3.5.7.7 1.3 1.5 1.5 2.5"/><path d="M9 18h6"/><path d="M10 22h4"/>',
    ok: '<circle cx="12" cy="12" r="10"/><path d="m9 12 2 2 4-4"/>',
    arrow: '<path d="M5 12h14"/><path d="m12 5 7 7-7 7"/>',
  };
  function ic(name, cls) {
    return '<svg class="asb-i' + (cls ? ' ' + cls : '') +
      '" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8"' +
      ' stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
      (ICONS[name] || '') + '</svg>';
  }

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
    /* v3（2026-09-06）：connecting=麦克风授权/预热中（还没在录，不能长得像在听）；
       errorUntil=「我的请求失败了」瞬态（玫红，静态），与 alert「页面有异常」
       （琥珀）分开——两者是不同的人该做不同的事。idle=待机样式 calm|breathe|flow。 */
    connecting: false, errorUntil: 0, idle: 'flow',
  };
  var ORB_SIZE = 168, ORB_N = 128;
  var ORB_STATES = ['idle', 'hint', 'listening', 'speaking', 'thinking',
    'agent', 'teach', 'alert', 'connecting', 'error'];
  var ORB_IDLE_STYLES = ['calm', 'breathe', 'flow'];

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

  /* 待机样式解析（v3）：URL ?orbidle=（只读不落盘，供并排截图比稿）→ 本地偏好
     asb_orb_idle → boot.orb_idle（后端旋钮预留）→ 默认 flow。安静档/减动效
     （lvl<2）一律 calm：「动效：安静」这个词必须真的安静。 */
  function orbIdleStyle() {
    var v = 'flow';
    try {
      var b = S.boot || {};
      if (ORB_IDLE_STYLES.indexOf(String(b.orb_idle || '')) >= 0) { v = b.orb_idle; }
      var saved = localStorage.getItem('asb_orb_idle');
      if (saved && ORB_IDLE_STYLES.indexOf(saved) >= 0) { v = saved; }
      var m = String(location.search || '').match(/[?&]orbidle=(calm|breathe|flow)\b/);
      if (m) { v = m[1]; }
    } catch (e) { /* ignore */ }
    if (ORB.lvl < 2) { v = 'calm'; }
    return v;
  }

  /* 为什么是这一档（偏好页诊断行的数据源）：reduced-motion > 调速器降档 >
     本地偏好 > 管理员旋钮 > 默认。 */
  function orbLevelReason() {
    try {
      if (window.matchMedia &&
          window.matchMedia('(prefers-reduced-motion: reduce)').matches) {
        return 'rm';
      }
    } catch (e) { /* ignore */ }
    if (ORB.degStep > 1) { return 'deg2'; }
    if (ORB.degStep === 1) { return 'deg1'; }
    try {
      var s = localStorage.getItem('asb_orb_lvl');
      if (s === '0' || s === '1' || s === '2') { return 'user'; }
      if (localStorage.getItem('asb_orb_idle')) { return 'user'; }
    } catch (e2) { /* ignore */ }
    var b = S.boot || {};
    if (typeof b.orb_level === 'number' || b.orb_idle) { return 'admin'; }
    return 'default';
  }

  function orbCompute() {
    if (ORB.force) { return ORB.force; }
    if (REC) { return 'listening'; }
    if (ORB.connecting) { return 'connecting'; }
    if (ORB.speaking) { return 'speaking'; }
    if (S.busy) { return 'thinking'; }
    if (ORB.errorUntil > Date.now()) { return 'error'; }
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

  /* 标题栏状态短句：状态机每次跃迁写一次；流式首 token 到达时由 readNdjson
     直接改成「回答中」（busy 未变、状态机不跃迁，所以不会被这里覆写）。 */
  function setHdStatus(key) {
    if (!$panel) { return; }
    var txt = t(key);
    var el = $panel.querySelector('.asb-hd-st');
    if (el && el.textContent !== txt) { el.textContent = txt; }
    /* 语音对话块的状态行是同一句话的大字版（P1）；播报期附「说话即可打断」（P2） */
    var vs = $panel.querySelector('.asb-v-st');
    if (vs) {
      var vt = (key === 'hd_speaking' && VOICE && VOICE.on && !VOICE.demo && VOICE.bargeOn)
        ? txt + ' · ' + t('v_barge_hint') : txt;
      if (vs.textContent !== vt) { vs.textContent = vt; }
    }
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
      setHdStatus('hd_' + st);
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
    /* 主题亮暗决定合成模式：暗底加色发光(lighter)/亮底普通叠加防洗白 */
    syncDark();
    ORB.cv.style.display = 'block';
    ORB.on = true;
    ORB.last = 0;
    ORB.ema = 16.7;
    ORB.degT0 = (window.performance || Date).now();
    ORB.raf = requestAnimationFrame(orbFrame);
  }

  /* 亮暗探测（v2 起球/面板/画布共用）：读 --xz-bg 落到 rgb 的真实值，而不是
     面板自身背景——面板底自 v2 起是 color-mix 玻璃，computed 会序列化成
     color(srgb …)，老正则会漏配成「恒暗」，亮色壳的画布就会用 lighter 洗白。
     探针挂在球容器（恒 display），面板收起时也算得准。 */
  var _probe = null;
  function probeDark() {
    try {
      if (!_probe) {
        _probe = document.createElement('span');
        _probe.setAttribute('aria-hidden', 'true');
        _probe.style.cssText = 'position:absolute;width:0;height:0;' +
          'overflow:hidden;pointer-events:none;background:var(--xz-bg,#fff)';
      }
      ($wrap || document.body).appendChild(_probe);
      var bg = getComputedStyle(_probe).backgroundColor || '';
      _probe.remove();
      var m = bg.match(/(\d+)[,\s]+(\d+)[,\s]+(\d+)(?:[,\s/]+([\d.]+))?/);
      if (m && !(m[4] != null && parseFloat(m[4]) === 0)) {
        return ((+m[1] * 299 + +m[2] * 587 + +m[3] * 114) / 1000) < 140;
      }
    } catch (e) { /* ignore */ }
    return false;
  }
  function syncDark() {
    _rbCache = null;  /* 主题/令牌可能变了：画布光谱色重取 */
    ORB.dark = probeDark();
    if ($wrap) { $wrap.classList.toggle('asb-dark', ORB.dark); }
    if ($panel) { $panel.classList.toggle('asb-dark', ORB.dark); }
  }

  /* 动效档类同时挂球容器与面板：面板是 body 直属兄弟节点，只挂 $wrap 的话
     `.asb-lv0 .asb-msg…` 这类面板侧降级选择器永远命不中——安静档就只安静了
     球，气泡与边缘光照旧在动（v2 之前面板没有动效，所以没暴露）。
     .asb-lite 只在调速器第二档（降回纯 CSS）才挂：第一档是「砍半粒子」的温和
     降级，若它也关掉光雾/外发光/边缘光，一次 1.6s 的页面卡顿（切标签、大页加载）
     就会让面板效果整个会话消失——无头截图实测就是这么触发的。 */
  function applyLevelClass() {
    ORB.idle = orbIdleStyle();
    var els = [$wrap, $panel];
    for (var i = 0; i < els.length; i++) {
      if (!els[i]) { continue; }
      els[i].classList.remove('asb-lv0', 'asb-lv1', 'asb-lv2',
        'asb-idle-calm', 'asb-idle-breathe', 'asb-idle-flow');
      els[i].classList.add('asb-lv' + ORB.lvl, 'asb-idle-' + ORB.idle);
      els[i].classList.toggle('asb-lite', ORB.degStep > 1);
    }
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
  function orbAudioStart(stream, quiet) {
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
    /* 聆听入场：粒子先聚成话筒再散入声波环。语音对话的后续回合静默（quiet）——
       每 10 秒一次的入场秀会把签名变成噪音 */
    if (!quiet) { orbBurst('mic'); }
  }
  function orbAudioStop() {
    try { if (ORB.src) { ORB.src.disconnect(); } } catch (e) { /* ignore */ }
    ORB.src = null;
    ORB.an = null;
    if (ORB.micEl) {
      try { ORB.micEl.style.boxShadow = ''; } catch (e2) { /* ignore */ }
    }
    ORB.micEl = null;
    /* 语音对话会话期（PCM 采集 worklet 挂在同一 AudioContext 上）不许挂起：
       挂了缓冲就停，打断判定与开口预卷一起失明 */
    try {
      if (ORB.actx && ORB.actx.state === 'running' && !(VOICE && VOICE.cap)) {
        ORB.actx.suspend();
      }
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
      /* 录音按钮实时电平圈：「看得见被听见」——v2 走光谱蓝（红点已承担
         「正在录音」语义，电平环不必再红） */
      ORB.micEl.style.boxShadow = '0 0 0 ' +
        (2 + ORB.level * 9).toFixed(1) + 'px rgba(' + orbRibbonRgb()[1] + ',' +
        (0.14 + ORB.level * 0.26).toFixed(2) + ')';
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
      if (ORB.actx && ORB.actx.state === 'running' && !REC && !(VOICE && VOICE.cap)) {
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
     拖拽（e.buttons）/安静档/reduced-motion（lvl<1）一律不动。
     v3 起写 --xz-tx/--xz-ty 变量而不是 inline transform：轮廓呼吸动画（asbMorph）
     占着 transform 属性，inline 值会被动画整个覆盖；变量写进关键帧里则两者共存。 */
  function orbWireTilt() {
    $ball.addEventListener('pointermove', function (e) {
      if (ORB.lvl < 1 || e.buttons) { return; }
      var r = $ball.getBoundingClientRect();
      if (!r.width) { return; }
      var dx = (e.clientX - r.left) / r.width - 0.5;
      var dy = (e.clientY - r.top) / r.height - 0.5;
      var orb = $ball.querySelector('.asb-orb');
      if (orb) {
        orb.style.setProperty('--xz-tx', (-dy * 7).toFixed(1) + 'deg');
        orb.style.setProperty('--xz-ty', (dx * 7).toFixed(1) + 'deg');
      }
    });
    $ball.addEventListener('pointerleave', function () {
      var orb = $ball.querySelector('.asb-orb');
      if (orb) {
        orb.style.removeProperty('--xz-tx');
        orb.style.removeProperty('--xz-ty');
      }
    });
  }

  /* 悬停标签片文案：常态「问小智（· 长按说话）」，长按录音期「在听…松开结束」 */
  function setTip(key) {
    var el = $wrap ? $wrap.querySelector('.asb-tip') : null;
    if (!el) { return; }
    var k = key || (voiceOn() ? 'tip_ask_voice' : 'tip_ask');
    var txt = t(k);
    if (el.textContent !== txt) { el.textContent = txt; }
  }

  /* 首次启动提示（每用户一次）：图标退出球身后，第一次见到它的人需要一个
     「这是什么」的答案——1.5s 后标签片 + 图标浮现 4s，随后交给悬停。 */
  function firstRunTip() {
    try {
      if (localStorage.getItem('asb_tip_seen')) { return; }
    } catch (e) { return; }
    setTimeout(function () {
      if (!$wrap || S.open) { return; }
      $wrap.classList.add('asb-tipon');
      try { localStorage.setItem('asb_tip_seen', '1'); } catch (e2) { /* ignore */ }
      setTimeout(function () { $wrap.classList.remove('asb-tipon'); }, 4000);
    }, 1500);
  }

  function orbFlash(ms) {
    ORB.alertUntil = Date.now() + (ms || 2600);
    syncOrb();
    setTimeout(syncOrb, (ms || 2600) + 80);
  }
  /* 「我的请求失败了」瞬态（问答/转写/播报失败）：玫红静态 2.6s。与 orbFlash
     的琥珀「页面有异常」分开——前者用户该重试，后者该报障。 */
  function orbError(ms) {
    ORB.errorUntil = Date.now() + (ms || 2600);
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
        applyLevelClass();  /* 面板同步进 .asb-lite：关外发光/光雾 */
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
    /* 语音对话块在场时大球就是主视觉，44px 波形条退场（两个都动=抢注意力） */
    var want = S.open && !VOICE.on && (st === 'listening' || st === 'speaking');
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
    var bw = Math.round(cssW * k), bh = Math.round(44 * k);
    if (cv.width !== bw) { cv.width = bw; }
    if (cv.height !== bh) { cv.height = bh; }
    var c = cv.getContext('2d');
    if (!c) { return; }
    c.clearRect(0, 0, bw, bh);
    /* v2：三条交织的多彩正弦光带（Siri 波形语法）。每条吃一个频带（低/中/高）
       定振幅，两端用 sin^1.4 包络收细，沿 x 铺光谱渐变（三条各自错相），暗底
       lighter 叠色发光、亮底普通叠加。无分析器（能力缺失/夹具）走合成波保住
       「在听/在说」的反馈语义。 */
    var rb = orbRibbonRgb();
    var mid = bh / 2;
    var t = ((window.performance || Date).now()) / 1000;
    var lv = Math.max(0, Math.min(1, ORB.level || 0));
    var bands = ORB.an
      ? [ORB.bass, ORB.mid, ORB.treb]
      : [0.32 + 0.22 * Math.sin(t * 1.9), 0.3 + 0.2 * Math.sin(t * 2.6 + 1.1),
         0.26 + 0.18 * Math.sin(t * 3.3 + 2.3)];
    var waves = [
      { f: 1.5, sp: 2.1, w: 2.2, a: 0.95, amp: 1.0, ord: [0, 1, 2] },
      { f: 2.2, sp: -1.6, w: 1.6, a: 0.7, amp: 0.78, ord: [2, 0, 1] },
      { f: 3.0, sp: 2.8, w: 1.2, a: 0.5, amp: 0.58, ord: [1, 2, 0] },
    ];
    c.globalCompositeOperation = ORB.dark ? 'lighter' : 'source-over';
    c.lineCap = 'round';
    c.lineJoin = 'round';
    var stepX = Math.max(2, 3 * k);
    for (var w = 0; w < waves.length; w++) {
      var wv = waves[w];
      var band = Math.max(0, Math.min(1, bands[w] * 1.4 + lv * 0.35));
      var A = (mid - 3 * k) * (0.12 + 0.88 * band) * wv.amp;
      var g = c.createLinearGradient(0, 0, bw, 0);
      g.addColorStop(0, 'rgb(' + rb[wv.ord[0]] + ')');
      g.addColorStop(0.5, 'rgb(' + rb[wv.ord[1]] + ')');
      g.addColorStop(1, 'rgb(' + rb[wv.ord[2]] + ')');
      c.strokeStyle = g;
      c.globalAlpha = wv.a;
      c.lineWidth = wv.w * k;
      c.beginPath();
      for (var x = 0; x <= bw; x += stepX) {
        var u = x / bw;
        var env = Math.pow(Math.sin(Math.PI * u), 1.4);
        var y = mid + Math.sin(u * Math.PI * 2 * wv.f + t * wv.sp) * A * env;
        if (x === 0) { c.moveTo(x, y); } else { c.lineTo(x, y); }
      }
      c.stroke();
    }
    c.globalAlpha = 1;
    c.globalCompositeOperation = 'source-over';
  }

  /* 状态粒子色与 CSS 的 --xz-core 表同源：听=rb-1、做=rb-2、想/说=rb-3。
     亮底压暗 28%（亮色粒子铺白底看不见），暗底提亮 18%（lighter 合成下更透亮）。 */
  function orbStateRgb(st) {
    var rb = orbRibbonRgb();
    var base = st === 'listening' ? rb[0] : (st === 'agent' ? rb[1] : rb[2]);
    var p = base.split(',');
    var k = ORB.dark ? 1.18 : 0.72;
    var out = [];
    for (var i = 0; i < 3; i++) {
      out.push(Math.max(0, Math.min(255, Math.round(+p[i] * k))));
    }
    return out.join(',');
  }

  /* 光谱三色的 rgb 分量（画布用）：从 --xz-rb-* 经探针元素落到 computed color，
     宿主令牌换色/切主题后由 syncDark 清缓存。取不到时回落三个字面量。 */
  var _rbCache = null, _cvt = null;
  /* 任意 CSS 颜色 → "r,g,b"：经 1×1 画布落成像素字节。白标推导后 computed color
     是 oklch(…) 字串（Chromium 对非 sRGB 色不转 rgb 序列化），正则解析会漏配；
     画布的颜色解析器和 CSS 同一套，oklch/color-mix 都吃得下。非法色让 fillStyle
     保持初始黑（#000000），据此判失败回落。 */
  function cssToRgb(str) {
    try {
      if (!_cvt) {
        _cvt = document.createElement('canvas');
        _cvt.width = 1;
        _cvt.height = 1;
      }
      var c = _cvt.getContext('2d', { willReadFrequently: true });
      if (!c) { return null; }
      c.fillStyle = '#000000';
      c.fillStyle = str;
      if (c.fillStyle === '#000000') { return null; }
      c.clearRect(0, 0, 1, 1);
      c.fillRect(0, 0, 1, 1);
      var d = c.getImageData(0, 0, 1, 1).data;
      return d[0] + ',' + d[1] + ',' + d[2];
    } catch (e) { return null; }
  }
  function orbRibbonRgb() {
    if (_rbCache) { return _rbCache; }
    var out = ['34,211,238', '79,110,247', '139,92,246'];
    try {
      var host = $panel || $wrap || document.body;
      var p = document.createElement('span');
      p.setAttribute('aria-hidden', 'true');
      p.style.cssText = 'position:absolute;width:0;height:0;overflow:hidden;pointer-events:none';
      host.appendChild(p);
      for (var i = 1; i <= 3; i++) {
        p.style.color = 'var(--xz-rb-' + i + ')';
        var rgb = cssToRgb(getComputedStyle(p).color || '');
        if (rgb) { out[i - 1] = rgb; }
      }
      p.remove();
    } catch (e) { /* 回落字面量 */ }
    _rbCache = out;
    return out;
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
      var colL = orbStateRgb(st);
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
      var colS = orbStateRgb(st);
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
      var colA = orbStateRgb(st);
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
    var colT = orbStateRgb(st);
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
    applyLevelClass();
    syncDark();
    /* 两壳都在 <html> 上切 data-theme / data-cp-theme：主题一换，玻璃/高光/
       画布合成模式立即跟随，不必等下一次开面板 */
    try {
      if (window.MutationObserver) {
        new MutationObserver(function () { syncDark(); }).observe(
          document.documentElement,
          { attributes: true,
            attributeFilter: ['data-theme', 'data-cp-theme', 'class'] });
      }
    } catch (e0) { /* ignore */ }
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
    /* 标签片长在离屏幕边缘远的那一侧 */
    var right = x + w / 2 > window.innerWidth / 2;
    $wrap.classList.toggle('asb-side-r', right);
    $wrap.classList.toggle('asb-side-l', !right);
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
    /* 先掐掉入场动画再量：动画期内 rect 带着 scale/translate，量出来是假几何 */
    if (_inT) { clearTimeout(_inT); _inT = 0; }
    $panel.classList.remove('asb-enter');
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
    var pw = Math.min(392, window.innerWidth - 24);
    var ph = Math.min(600, window.innerHeight * 0.78);
    var left = r.left + r.width / 2 < window.innerWidth / 2
      ? r.left : r.right - pw;
    left = Math.min(Math.max(left, 12), window.innerWidth - pw - 12);
    var top = r.top + r.height / 2 < window.innerHeight / 2
      ? r.bottom + 10 : r.top - ph - 10;
    top = Math.min(Math.max(top, 12), window.innerHeight - ph - 12);
    left = avoidHostFabs(left, top, pw, ph);
    $panel.style.left = left + 'px';
    $panel.style.top = top + 'px';
  }

  /* 让开宿主的悬浮小圆钮（v2.1）：老板截图里面板右缘压着一颗宿主浮球。只在
     默认摆放时生效（用户自己拖过的位置一律尊重）；判据刻意收窄——body 直属、
     position:fixed、24–80px 的可点击小件、不是小智自家的（.asb-/.xza-/.xzt-）、
     且与拟放位置相交——命中则把面板整体左移到它左侧 8px；左移会顶到 12px 边界
     时放弃避让（宁可重叠也不把面板挤出屏）。 */
  function avoidHostFabs(left, top, pw, ph) {
    try {
      var kids = document.body.children;
      for (var i = 0; i < kids.length; i++) {
        var el = kids[i];
        if (!el || el === $wrap || el === $panel || el.nodeType !== 1) { continue; }
        var cls = String(el.className || '');
        if (/(^|\s)(asb|xza|xzt)-/.test(cls)) { continue; }
        var cs = getComputedStyle(el);
        if (cs.position !== 'fixed' || cs.pointerEvents === 'none' ||
            cs.visibility === 'hidden' || cs.display === 'none') { continue; }
        var b = el.getBoundingClientRect();
        if (b.width < 24 || b.width > 80 || b.height < 24 || b.height > 80) { continue; }
        var hit = b.left < left + pw && b.right > left && b.top < top + ph &&
          b.bottom > top;
        if (!hit) { continue; }
        var nl = b.left - 8 - pw;
        if (nl >= 12) { left = nl; }
      }
    } catch (e) { /* 避让只是礼貌，失败就按原位放 */ }
    return left;
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
    /* 层序即遮挡：辉光 → 后半环 → 核（内含三团内光）→ 状态层 → 前半环（z3）→
       图标（z3，悬停才现）→ 角标。r2 是 lv2 才显示的第二条细环。 */
    $ball.innerHTML = '<span class="asb-glow"></span>' +
      '<span class="asb-ring back" aria-hidden="true"></span>' +
      '<span class="asb-ring back r2" aria-hidden="true"></span>' +
      '<span class="asb-orb"><i class="asb-lc lc1"></i><i class="asb-lc lc2"></i>' +
      '<i class="asb-lc lc3"></i></span><span class="asb-alertfx"></span>' +
      '<span class="asb-sweep"></span><span class="asb-ping"></span>' +
      '<span class="asb-ring front" aria-hidden="true"></span>' +
      '<span class="asb-ring front r2" aria-hidden="true"></span>' +
      '<span class="asb-ic">' + ICON_SPARK + '</span>' +
      '<span class="asb-dot"></span>';
    $wrap.appendChild($ball);
    /* 悬停标签片挂在容器上（不进按钮：按钮 overflow 与 aria 都不该被它污染） */
    var tip = document.createElement('span');
    tip.className = 'asb-tip';
    tip.setAttribute('aria-hidden', 'true');
    $wrap.appendChild(tip);
    document.body.appendChild($wrap);
    setTip();

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

  /* 球的三个手势：单击=开合面板、拖动(>6px)=搬位置、长按(≥420ms 不动)=开始
     语音对话（P1；仅麦克风可用时，否则长按等同单击）。长按触发后的那次 pointerup
     不再切面板。 */
  var HOLD_MS = 420;
  function wireDrag() {
    var sx = 0, sy = 0, ox = 0, oy = 0, moved = false, dragging = false;
    var holdT = 0, held = false;
    function clearHold() {
      if (holdT) { clearTimeout(holdT); holdT = 0; }
      $wrap.classList.remove('asb-holding');
    }
    $ball.addEventListener('pointerdown', function (e) {
      dragging = true; moved = false; held = false;
      sx = e.clientX; sy = e.clientY;
      var r = $wrap.getBoundingClientRect();
      ox = r.left; oy = r.top;
      $ball.setPointerCapture(e.pointerId);
      clearHold();
      if (voiceOn()) {
        $wrap.classList.add('asb-holding');
        holdT = setTimeout(function () {
          holdT = 0;
          if (!dragging || moved) { return; }
          held = true;
          dragging = false;
          $wrap.classList.remove('asb-holding');
          try { $ball.releasePointerCapture(e.pointerId); } catch (e2) { /* ignore */ }
          beacon('asb_hold_voice');
          voiceStart();
        }, HOLD_MS);
      }
    });
    $ball.addEventListener('pointermove', function (e) {
      if (!dragging) { return; }
      var dx = e.clientX - sx, dy = e.clientY - sy;
      if (Math.abs(dx) + Math.abs(dy) > 6) { moved = true; clearHold(); }
      if (moved) { applyPos({ x: ox + dx, y: oy + dy }); }
    });
    $ball.addEventListener('pointercancel', function () {
      dragging = false;
      clearHold();
    });
    $ball.addEventListener('pointerup', function () {
      clearHold();
      if (held) { held = false; return; }
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
  var _closeT = 0, _inT = 0;
  function togglePanel(force, src) {
    var wasOpen = S.open;
    S.open = force != null ? !!force : !S.open;
    if (!S.open && VOICE.on) { voiceStop(); }
    if (!S.open && REC) { stopRec(true); }
    /* open 类同步切（门禁与外部都按它判「面板是否开着」）；关闭的 160ms 淡出
       由 .closing 单独承担：它只负责把 display 撑住到动画结束，安静档跳过。 */
    if (_closeT) {
      clearTimeout(_closeT);
      _closeT = 0;
      $panel.classList.remove('closing');
    }
    $panel.classList.toggle('open', S.open);
    if (_inT) { clearTimeout(_inT); _inT = 0; $panel.classList.remove('asb-enter'); }
    if (S.open && !wasOpen && ORB.lvl > 0) {
      $panel.classList.add('asb-enter');
      _inT = setTimeout(function () {
        _inT = 0;
        $panel.classList.remove('asb-enter');
      }, 300);
    }
    if (!S.open && wasOpen && ORB.lvl > 0) {
      $panel.classList.add('closing');
      _closeT = setTimeout(function () {
        _closeT = 0;
        $panel.classList.remove('closing');
      }, 170);
    }
    $ball.setAttribute('aria-expanded', S.open ? 'true' : 'false');
    $wrap.classList.toggle('asb-open', S.open);
    if (S.open && !wasOpen) {
      beacon(src === 'ext' ? 'asb_open_ext' : 'asb_open');
    }
    if (S.open) {
      placePanel();
      /* 面板隐藏时 offsetWidth 恒 0 → 滑块只能在开的这一刻定位 */
      syncModes();
      if (!wasOpen) { greetSweep(); }
      loadPair();
      loadPc();
      if (S.tab === 'mine') { loadTickets(); }
      var inp = $panel.querySelector('.asb-in');
      if (inp && window.innerWidth > 480) {
        setTimeout(function () { inp.focus(); }, 60);
      }
    }
  }

  /* 签名时刻：面板从关到开、还没有对话历史时，光带绕问候气泡扫一圈。只在
     饱满档播（lv2），一次 1.1s；有历史的重开不播——它是「你好」不是常驻装饰。 */
  function greetSweep() {
    if (ORB.lvl < 2 || S.tab !== 'chat' || chatHistory.length) { return; }
    var hello = $panel.querySelector('.asb-body .asb-msg.ai');
    if (!hello || hello.classList.contains('pending')) { return; }
    hello.classList.add('asb-greet');
    setTimeout(function () { hello.classList.remove('asb-greet'); }, 1250);
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
  var MODE_ICONS = { chat: 'chat', teach: 'cap', agent: 'bolt' };

  function defineMode(id, def) {
    if (!MODES[id]) { MODE_ORDER.push(id); }
    MODES[id] = {
      id: id,
      icon: String(def.icon || ''),
      iconName: String(def.iconName || ''),
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
      defineMode('chat', { order: 10, icon: 'chat', labelKey: 'mode_chat',
                           mount: mountChat });
    }
    $panel.innerHTML = '' +
      '<div class="asb-hd" tabindex="0" title="' + esc(t('move_hint')) + '">' +
      '<span class="asb-hd-ic" aria-hidden="true"><i class="asb-hd-core"></i>' +
      '<i class="asb-hd-ring"></i>' + ICON_SPARK + '</span>' +
      '<span class="asb-hd-name"><b>' + esc(dispName()) + '</b>' +
      '<em class="asb-hd-st">' + esc(t('hd_idle')) + '</em></span>' +
      /* 复位入口做成**看得见的按钮**而不是只靠双击：双击这类手势发现性极差
         （没人知道可以双击），且在指针捕获场景下浏览器未必合成 dblclick。
         只在几何被改过时出现——没自定义过就没有「复位」可言，常驻只是噪音。 */
      '<button type="button" class="asb-hd-rst" data-act="rstgeo" hidden' +
      ' title="' + esc(t('reset_geo')) + '" aria-label="' +
      esc(t('reset_geo')) + '">' + ic('expand') + '</button>' +
      '<button type="button" class="asb-hd-pair" data-act="pair" title="' +
      esc(t('pair_off')) + '" aria-label="' + esc(t('pair_t')) +
      '">' + ic('phone') + '<i aria-hidden="true"></i></button>' +
      '<button type="button" class="asb-hd-pc" data-act="pc" hidden title="' +
      esc(t('pc_off')) + '" aria-label="' + esc(t('pc_t')) +
      '">' + ic('monitor') + '<i aria-hidden="true"></i></button>' +
      '<button type="button" class="asb-x" data-act="close" aria-label="' +
      esc(t('close')) + '">' + ic('x') + '</button></div>' +
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
      /* 大球可聚焦（role=button）：回车/空格=打断 */
      if ((e.key === 'Enter' || e.key === ' ') && e.target &&
          e.target.classList.contains('asb-vorb')) {
        e.preventDefault();
        voiceInterrupt();
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
      /* 图标解析顺序（v2.1）：iconName（姊妹组件指名的内置线性图标）→ icon 本身
         恰是图标名（球自带模式）→ 按模式 id 查表（旧缓存的姊妹组件只传 emoji）
         → 最后才把 icon 当 emoji 文本渲染 */
      var icon = ICONS[m.iconName] ? ic(m.iconName)
        : (ICONS[m.icon] ? ic(m.icon)
          : (MODE_ICONS[m.id] ? ic(MODE_ICONS[m.id]) : (m.icon ? esc(m.icon) : '')));
      html += '<button type="button" class="asb-mode" role="radio" ' +
        'aria-checked="false" tabindex="-1" data-mode="' + esc(m.id) + '">' +
        (icon ? '<i aria-hidden="true">' + icon + '</i>' : '') +
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
    /* 当前模式写到容器上：滑块投影与图标按模式取色（问答蓝/教学青/替我做紫） */
    box.setAttribute('data-cur', cur ? S.tab : '');
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
      '<button type="button" class="asb-sub asb-sub--ic" data-tab="set" title="' +
      esc(t('set_t')) + '" aria-label="' + esc(t('set_t')) + '">' + ic('sliders') +
      '</button>';
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

  /* 动效三档段控 + 诊断行（v3 2026-09-06）。段控值 = 待机样式；安静档同时把
     画质压到 lv1（无粒子）。诊断行回答「现在是哪档、为什么」——远程桌面/系统
     减动效把球压成全静时，此前用户无从知道原因。 */
  function fxHtml() {
    var cur = ORB.lvl < 2 ? 'calm' : ORB.idle;
    var html = '<div class="asb-fxrow"><span class="asb-fx-t">' + esc(t('fx_t')) +
      '</span><div class="asb-seg" role="radiogroup" aria-label="' + esc(t('fx_t')) + '">';
    var opts = ['calm', 'breathe', 'flow'];
    for (var i = 0; i < opts.length; i++) {
      var on = opts[i] === cur;
      html += '<button type="button" class="asb-seg-b' + (on ? ' on' : '') +
        '" role="radio" aria-checked="' + (on ? 'true' : 'false') +
        '" data-act="orb-fx" data-fx="' + opts[i] + '">' +
        esc(t('fx_' + opts[i])) + '</button>';
    }
    html += '</div></div><div class="asb-fxdiag">' + esc(t('fx_now')) + '：' +
      esc(t('fx_' + cur)) + ' · ' + esc(t('fx_r_' + orbLevelReason())) + '</div>';
    return html;
  }

  function renderTools() {
    var box = $panel.querySelector('.asb-tools');
    if (!box) { return; }
    if (S.shell !== 'admin') {
      /* workspace 壳没有 admin 全局工具，但「动效」档是坐席自己的注意力权：
         8 小时班盯屏的人必须能就地调安静（P2 起不再整行隐藏） */
      box.innerHTML = fxHtml();
      return;
    }
    var html = '';
    /* 2026-08-21 帮助球退役：术语提示开关首选全局函数 window.toggleTermTips
       （base.html 提供）；旧 .tip-toggle 点击仅作过渡兼容。 */
    if (typeof window.toggleTermTips === 'function' ||
        document.querySelector('.tip-toggle')) {
      var on = localStorage.getItem('tipOn') !== '0';
      html += '<button type="button" class="asb-tool" data-act="tips">' + ic('bulb') +
        '<span>' + esc(on ? t('tools_tips_on') : t('tools_tips_off')) + '</span></button>';
    }
    if (typeof window.startTour === 'function') {
      html += '<button type="button" class="asb-tool" data-act="tour">' + ic('play') +
        '<span>' + esc(t('tools_tour')) + '</span></button>';
    }
    if (typeof window.openShortcuts === 'function') {
      html += '<button type="button" class="asb-tool" data-act="keys">' + ic('keyboard') +
        '<span>' + esc(t('tools_keys')) + '</span></button>';
    }
    if (typeof window.openCmdPalette === 'function') {
      html += '<button type="button" class="asb-tool" data-act="cmd">' + ic('command') +
        '<span>' + esc(t('tools_cmd')) + ' (Ctrl+K)</span></button>';
    }
    /* 「上传诊断给客服」（实施49 P1-9 的独立诊断上传面板）——帮助球退役后
       由助手面板接住这个入口，防止孤儿化。 */
    if (typeof window.openSupportPanel === 'function') {
      html += '<button type="button" class="asb-tool" data-act="support">' + ic('wrench') +
        '<span>' + esc(t('tools_support')) + '</span></button>';
    }
    /* orb 动效三档（写 localStorage；reduced-motion 恒压到 lv0，诊断行会说明） */
    html += fxHtml();
    box.innerHTML = html;
  }

  /* 动效档落盘 + 立即生效：calm → lv1（无粒子）+ 待机 calm；breathe/flow → lv2。
     调速器降档状态一并清零（用户主动选择应覆盖本会话的自动降档）。 */
  function setOrbFx(fx) {
    if (ORB_IDLE_STYLES.indexOf(fx) < 0) { return; }
    try {
      localStorage.setItem('asb_orb_lvl', fx === 'calm' ? '1' : '2');
      localStorage.setItem('asb_orb_idle', fx);
    } catch (e) { /* ignore */ }
    ORB.degStep = 0;
    ORB.lvl = orbLevel();
    applyLevelClass();
    orbBeacon(fx === 'calm' ? 'asb_orb_calm_on' : 'asb_orb_calm_off');
    orbBeacon('asb_orb_idle_' + fx);
    syncOrb();
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
    if (VOICE.on) { voiceStop(); }
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
      '<button type="button" class="asb-tool" data-act="mine-refresh">' + ic('refresh') +
      '<span>' + esc(t('mine_refresh')) + '</span></button></div>' +
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
    /* 语音对话进行中：大球块取代输入区（语音替代打字） */
    if (isChat && VOICE.on) { voiceRender(ft); return; }
    var ph = isChat ? t('input_ph') : composerPh(mode);
    /* 2026-08-21 老板拍板：不设字数限制（maxlength 移除） */
    ft.innerHTML = '<div class="asb-ft">' +
      '<textarea class="asb-in" rows="1" placeholder="' + esc(ph) +
      '"></textarea>' +
      ((isChat && voiceOn())
        ? '<button type="button" class="asb-mic" data-act="mic" ' +
          'title="' + esc(t('mic_title')) + '" aria-label="' +
          esc(t('mic_title')) + '">' + ic('mic') + '</button>'
        : '') +
      '<button type="button" class="asb-send asb-send--ic" data-act="send" title="' +
      esc(t('send')) + '" aria-label="' + esc(t('send')) + '">' + ic('send') +
      '</button></div>';
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
    /* 入场动画类只加在刚插入的 DOM 节点上（字符串保持干净）：restoreChat
       重绘整段历史时不会整屏重播一遍入场 */
    pushChatDom(html);
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
    /* 只认对话输入区的圆形发送键：报障页的提交键也叫 .asb-send，v1 在用户
       生成期切去报障页时会把「提交报障」改成停止符 */
    var btn = $panel.querySelector('.asb-send.asb-send--ic');
    if (!btn) { return; }
    if (v && S.abortCtl) {
      /* 生成期发送键变「停止」（问答链可中断；转写等短请求维持旧禁用态） */
      btn.disabled = false;
      btn.innerHTML = ic('stop');
      btn.classList.add('busy');
      btn.title = t('stop_t');
      btn.setAttribute('aria-label', t('stop_t'));
      btn.setAttribute('data-act', 'stop-gen');
    } else {
      btn.disabled = v;
      btn.innerHTML = ic('send');
      btn.classList.remove('busy');
      btn.title = t('send');
      btn.setAttribute('aria-label', t('send'));
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
    /* 思考占位（v2）：光雾 + 文案 + 计秒三件分开放。计秒 aria-hidden——
       .asb-body 是 aria-live 区域，v1 每秒改一次整段文字，读屏用户每秒听一遍
       「思考中 N 秒」。文案只在 3s/8s 两个节点变化且同值不重写。 */
    pushChat('<div class="asb-msg ai pending" id="' + thinkId + '" aria-busy="true">' +
      '<span class="asb-mesh" aria-hidden="true"><i></i><i></i><i></i></span>' +
      '<span class="asb-txt">' + esc(t('searching')) + '</span>' +
      '<span class="asb-tick" aria-hidden="true"></span></div>');
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
        var tx = el.querySelector('.asb-txt');
        var tk = el.querySelector('.asb-tick');
        var phrase = s >= 8 ? t('thinking_long') : t('thinking');
        if (tx && tx.textContent !== phrase) { tx.textContent = phrase; }
        if (tk) { tk.textContent = s + 's'; }
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
        var tx0 = el0 ? (el0.querySelector('.asb-txt') || el0) : null;
        var live = (el0 && el0.getAttribute('data-live') === '1')
          ? String(tx0.textContent || '') : '';
        replaceThinking(thinkId,
          (live ? '<div class="asb-msg ai">' + mdLite(live) + '</div>' : '') +
          '<div class="asb-msg ai asb-msg--sys">' + ic('stop') + ' ' +
          esc(t('stopped_gen')) + '</div>');
        return;
      }
      replaceThinking(thinkId,
        '<div class="asb-msg err">' + esc(e && e.message || t('err_net')) +
        '</div>' + errActionsHtml(q));
      orbError();
      if (VOICE.on) { voiceAnswerDone(false); }
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
      '<button type="button" class="asb-act" data-act="retry">' + ic('refresh') +
      '<span>' + esc(t('retry')) + '</span></button>' +
      '<button type="button" class="asb-act" data-act="to-report" data-q="' +
      esc(String(q || S.lastQ || '')) + '">' + ic('bug') + '<span>' +
      esc(t('err_report')) + '</span></button></div>';
  }

  /* 就地落定（v2）：思考占位不再「删节点再 push」——那会闪一下、滚动跳一下、
     动画状态全丢。现在同一节点换形态：结果 HTML 的第一个 .asb-msg 写进占位
     节点（AI 回答进入 settle：光带减速凝成细线再淡出，这一下就是完成信号），
     其余节点（依据标注/来源卡/反馈行/出路）依次插在它后面并各自入场。
     chatHistory 同索引替换字符串，与 DOM 顺序一致；字符串里没有运行态类。 */
  function replaceThinking(thinkId, html) {
    var el = document.getElementById(thinkId);
    var found = false;
    for (var i = chatHistory.length - 1; i >= 0; i--) {
      if (chatHistory[i].indexOf(thinkId) !== -1) {
        chatHistory[i] = html;
        found = true;
        break;
      }
    }
    if (!found) { chatHistory.push(html); }
    saveChatStore();
    if (!el) {
      /* 占位不在 DOM（用户切去了别的页签）：字符串已换，回到问答页 restore
         即是答案；当前就在问答页却找不到节点属异常，直接追加兜底 */
      var body0 = $panel.querySelector('.asb-body');
      if (S.tab === 'chat' && body0) {
        body0.insertAdjacentHTML('beforeend', html);
        body0.scrollTop = body0.scrollHeight;
      }
      return;
    }
    var tmp = document.createElement('div');
    tmp.innerHTML = html;
    var first = tmp.firstElementChild;
    if (!first || !first.classList.contains('asb-msg')) {
      el.remove();
      pushChatDom(html);
      return;
    }
    var isAi = first.classList.contains('ai');
    el.className = first.className + (isAi ? ' settle' : '');
    el.removeAttribute('id');
    el.removeAttribute('aria-busy');
    el.removeAttribute('data-live');
    el.innerHTML = first.innerHTML;
    var anchor = el;
    var rest = tmp.children;
    while (rest.length > 1) {
      var n = rest[1];
      n.classList.add('asb-new');
      anchor.insertAdjacentElement('afterend', n);
      anchor = n;
    }
    var body = el.closest('.asb-body');
    if (body) { body.scrollTop = body.scrollHeight; }
    if (isAi) {
      setTimeout(function () { el.classList.remove('settle'); }, 2300);
    }
  }

  /* 只往 DOM 追加（不入历史）——replaceThinking 的兜底分支用 */
  function pushChatDom(html) {
    var body = $panel.querySelector('.asb-body');
    if (S.tab !== 'chat' || !body) { return; }
    var before = body.lastElementChild;
    body.insertAdjacentHTML('beforeend', html);
    var n = before ? before.nextElementSibling : body.firstElementChild;
    while (n) { n.classList.add('asb-new'); n = n.nextElementSibling; }
    body.scrollTop = body.scrollHeight;
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
      orbError();
      if (VOICE.on) { voiceAnswerDone(false); }
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
        html += '<div class="asb-src"><span class="n">S' + (j + 1) +
          '</span><span class="tt">' + esc(s.title || '') + '</span>' +
          (linkable ? '<a data-goto="' + esc(path) + '" data-anchor="' +
            esc(String(s.anchor || '')) + '">' +
            esc(t('goto')) + ' →</a>' : '') + '</div>';
      }
      html += '</div>';
    }
    var sayBtn = answer
      ? '<button type="button" class="asb-say" data-act="say" data-say="' +
        esc(sayPrep(answer)) + '" title="' + esc(t('say_t')) +
        '" aria-label="' + esc(t('say_t')) + '">' + ic('speaker') + '</button>'
      : '';
    var copyBtn = answer
      ? '<button type="button" class="asb-say" data-act="copy" title="' +
        esc(t('copy_t')) + '" aria-label="' + esc(t('copy_t')) +
        '">' + ic('copy') + '</button>'
      : '';
    if (done && done.answered && done.qa_id) {
      html += '<div class="asb-fb" data-qa="' + esc(String(done.qa_id)) +
        '">' + sayBtn + copyBtn +
        '<span class="asb-fbq"><span>' + esc(t('helpful')) + '</span>' +
        '<button type="button" data-fb="up" title="' + esc(t('fb_up')) +
        '" aria-label="' + esc(t('fb_up')) + '">' + ic('up') + '</button>' +
        '<button type="button" data-fb="down" title="' + esc(t('fb_down')) +
        '" aria-label="' + esc(t('fb_down')) + '">' + ic('down') +
        '</button></span></div>';
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
        'data-q="' + esc(q) + '">' + ic('bug') + '<span>' + esc(t('to_report')) +
        '</span></button>';
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
    /* 语音对话：余下半句一并送合成（拒答文案也念——那是诚实的回答），随后回到听 */
    if (VOICE.on) {
      voiceFeed(answer, true);
      voiceAnswerDone(true);
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
    if (th) {
      var thx = th.querySelector('.asb-txt');
      if (thx) { thx.textContent = t('thinking'); } else { th.textContent = t('thinking'); }
    }
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
        /* 语音对话：句子一完整就送去合成——首句在余下内容还在生成时已经开口 */
        if (VOICE.on) { voiceFeed(live, false); }
        var el = document.getElementById(thinkId);
        if (el) {
          if (el.getAttribute('data-live') !== '1') {
            /* 首个 token：pending → live——光雾与计秒撤下，光带放慢、光标亮起 */
            el.setAttribute('data-live', '1');  /* 通知计秒 ticker 让位 */
            el.classList.remove('pending');
            el.classList.add('live');
            var mesh = el.querySelector('.asb-mesh');
            if (mesh) { mesh.remove(); }
            var tk = el.querySelector('.asb-tick');
            if (tk) { tk.remove(); }
            setHdStatus('hd_live');
          }
          var tx = el.querySelector('.asb-txt');
          /* 流式也走 mdLite（先转义再加粗）：v1 直写 textContent，生成期满屏裸
             星号，落定那一刻才突然变粗——现在边生成边有格式 */
          if (tx) { tx.innerHTML = mdLite(live); } else { el.textContent = live; }
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
    orbError();
  }
  function stopRec(cancel) {
    if (!REC) { return; }
    /* 语音对话的一轮（无 MediaRecorder，音频在 PCM 环形缓冲）走回合链 */
    if (REC.voice) { voiceTurnStop(!!cancel); return; }
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
    if (ORB.connecting) { return; }
    sayStop();  /* 录音前停播报：防扬声器进麦（回声）+ 状态灯语义唯一 */
    /* connecting（v3）：首次用麦会弹授权框、慢机器上 getUserMedia 也要几百毫秒，
       这段时间球必须诚实地说「在连」而不是装作「在听」 */
    ORB.connecting = true;
    syncOrb();
    navigator.mediaDevices.getUserMedia({ audio: true }).then(function (st) {
      ORB.connecting = false;
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
    }).catch(function () {
      ORB.connecting = false;
      micErr(t('mic_denied'));
    });
  }
  /* 音频块 → 文本（Promise）：手动麦克风键与语音对话回合共用同一条转写链 */
  function transcribeBlob(blob) {
    return new Promise(function (res, rej) {
      var rd = new FileReader();
      rd.onerror = function () { rej(new Error(t('mic_fail'))); };
      rd.onload = function () {
        fetch('/api/assistant/transcribe', {
          method: 'POST',
          headers: csrfHeaders({ 'Content-Type': 'application/json' }),
          body: JSON.stringify({ audio_b64: String(rd.result || '') }),
        }).then(function (r) {
          return r.json().catch(function () { return {}; }).then(function (j) {
            if (!r.ok || !j.ok) {
              var err = new Error(String(j.detail || t('mic_fail')));
              err.status = r.status;   /* 429 让语音回合退避而不是硬撞 */
              throw err;
            }
            res(String(j.text || ''));
          });
        }).catch(rej);
      };
      rd.readAsDataURL(blob);
    });
  }
  function uploadRec(blob) {
    setBusy(true);
    var inp0 = $panel.querySelector('.asb-in');
    if (inp0) { inp0.placeholder = t('mic_busy'); }
    transcribeBlob(blob).then(function (text) {
      var inp = $panel.querySelector('.asb-in');
      if (inp) {
        inp.value = text;
        inp.focus();
      }
    }).catch(function (e) {
      micErr(e && e.message || t('mic_fail'));
    }).then(function () {
      setBusy(false);
      syncMicUi(false);
    });
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
      btn.innerHTML = ic('check');
      btn.classList.add('ok');
      btn.setAttribute('data-ok', '1');
      setTimeout(function () {
        btn.innerHTML = ic('copy');
        btn.classList.remove('ok');
        btn.removeAttribute('data-ok');
      }, 1200);
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
  function sayPrep(s, max) {
    var x = String(s || '');
    x = x.replace(/\[S\d\]/g, ' ');
    x = x.replace(/[*`#>_~]/g, '');
    x = x.replace(/\s+/g, ' ').trim();
    return x.slice(0, max || 360);
  }
  /* 文本 → 可播放 URL（Promise）：手动播报键与语音对话的句级队列共用 */
  function ttsFetch(text) {
    return fetch('/api/voice/tts-test', {
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
        return url;
      });
    });
  }
  function sayReset() {
    var b = $panel ? $panel.querySelector('[data-act="say"].on') : null;
    if (b) { b.classList.remove('on'); b.innerHTML = ic('speaker'); }
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
    btn.innerHTML = ic('refresh', 'asb-spin');
    ttsFetch(text).then(function (url) {
      if (!SAY.el) {
        SAY.el = new Audio();
        SAY.el.addEventListener('ended', sayStop);
        SAY.el.addEventListener('error', function () { sayStop(); });
      }
      SAY.el.src = url;
      orbSpeakStart(SAY.el);
      btn.classList.add('on');
      btn.innerHTML = ic('stop');
      orbBeacon('asb_orb_say');
      return SAY.el.play();
    }).catch(function (e) {
      sayStop();
      orbError();
      pushChat('<div class="asb-msg err">' +
        esc((e && e.message) || t('say_fail')) + '</div>');
    }).then(function () {
      SAY.busy = false;
      btn.disabled = false;
      if (!btn.classList.contains('on')) { btn.innerHTML = ic('speaker'); }
    });
  }

  /* ─────────────────────── 语音对话（P1 2026-09-06 实施95）：免手动回合 ──────
     ChatGPT 语音模式让人觉得「活」的不是那颗球，而是球背后的回合循环：
       听（VAD 判停）→ 想（转写 + 检索 + 生成）→ 说（按句自动播报，首句先响）→ 再听。
     此前小智的语音是「点录音→再点停→转写填输入框→再点发送→再点播报」五步，最有
     表现力的听/说两态几乎没机会出现。本模块**全部复用既有端点**（transcribe /
     query / tts-test），零新后端；大球是同一状态机（ORB.state）的大尺寸 Canvas 2D
     渲染，只在语音块存在时跑 rAF。对话内容仍在消息流里流动（识别文本=用户气泡、
     回答=AI 气泡，即实时字幕）。
     P2（同日）：采集改 PCM 环形缓冲（AudioWorklet 音频线程重采样到 16k，失败回落
     ScriptProcessor）——① 只上传「说话段」（前留 300ms 预卷，掐掉开口前的静默：
     whisper 对长静默会幻觉出字、且时长即时延）；② 缓冲全程在录，**语音打断**
     （说话即停播报/掐生成）触发时开口那几百毫秒已经在缓冲里，不丢字；③ VAD 直接
     在 PCM 帧上算，与可视化用的分析器解耦（安静档不跑画布时 VAD 照常）。
     铁律沿用：每个动效映射真实状态；电平低通平滑；隐藏即停。 */
  var VOICE = {
    on: false, demo: false, demoT: 0, stream: null, turns: 0,
    floor: 0.01, speechSeen: false, speechT0: 0, quietT0: 0, silentT0: 0,
    spStart: -1, carry: null, bargeOn: true, bargeT0: 0, bargeStart: 0, lastLoud: 0,
    loudT0: 0,
    cap: null, ring: null, w: 0, rate: 16000,
    autoSay: true, q: [], playing: false, au: null, spoken: 0, answerDone: true,
    ttsFail: 0, tEnd: 0, tPlay: 0, eager: false,
    cv: null, ctx: null, dpr: 1, raf: 0, last: 0, lv: 0, prevLv: 0,
    ph2: 0, ph3: 1.3, ph5: 2.1, rings: [], lastRip: 0,
  };
  var VOICE_SIL_MS = 800;          /* 说完后静默多久判「这一轮说完了」 */
  var VOICE_MIN_SPEECH_MS = 220;   /* 短于此的响动不算说话（咳嗽/键盘） */
  var VOICE_BARGE_MS = 350;        /* 播报/生成期打断门：更长、更响才算（AEC 残余回声很短） */
  var VOICE_MAX_TURN_MS = 15000;   /* 单轮硬上限 */
  var VOICE_IDLE_END_MS = 25000;   /* 开着但一直没人说话 → 自动结束会话 */
  var VOICE_PREROLL = 4800;        /* 300ms @16k：开口前的预卷，防首字被吃 */
  var VOICE_RING_SEC = 40;

  function voiceStart(opts) {
    opts = opts || {};
    if (VOICE.on) { return true; }
    if (!opts.demo && !voiceOn()) { return false; }
    sayStop();
    if (REC) { stopRec(true); }
    if (!S.open) { togglePanel(true, opts.demo ? 'ext' : undefined); }
    if (S.tab !== 'chat') { switchTab('chat', true); }
    VOICE.on = true;
    VOICE.demo = !!opts.demo;
    VOICE.autoSay = true;
    VOICE.q = []; VOICE.spoken = 0; VOICE.answerDone = true; VOICE.playing = false;
    VOICE.turns = 0; VOICE.ttsFail = 0; VOICE.rings = []; VOICE.lv = 0;
    VOICE.silentT0 = 0; VOICE.floor = 0.01; VOICE.carry = null; VOICE.spStart = -1;
    VOICE.tEnd = 0; VOICE.tPlay = 0; VOICE.eager = false; VOICE.bargeT0 = 0;
    beacon(VOICE.demo ? 'asb_voice_demo' : 'asb_voice_start');
    var ft = $panel.querySelector('.asb-ftwrap');
    if (ft) { voiceRender(ft); }
    voiceLoopStart();
    /* 会话提示只进 DOM 不进历史：切页签/刷新后不该留下一排「语音对话已开始」 */
    pushChatDom('<div class="asb-msg ai asb-msg--sys">' + ic('mic') + ' ' +
      esc(t('v_started')) + '</div>');
    if (VOICE.demo) { voiceDemoStep(0); return true; }
    ORB.connecting = true;
    syncOrb();
    navigator.mediaDevices.getUserMedia({ audio: {
      echoCancellation: true, noiseSuppression: true, autoGainControl: true } })
      .then(function (st) {
        if (!VOICE.on) {
          try { st.getTracks().forEach(function (tk) { tk.stop(); }); } catch (e) { /* */ }
          return;
        }
        VOICE.stream = st;
        return voiceCapStart(st).then(function () {
          if (!VOICE.on) { return; }
          ORB.connecting = false;
          voiceListen();
        });
      }).catch(function () {
        ORB.connecting = false;
        micErr(t('mic_denied'));
        voiceStop();
      });
    return true;
  }

  /* ── PCM 采集（会话级常开）────────────────────────────────────────────────
     Worklet 源码以 Blob URL 装载（单文件纪律，不另起静态文件）：音频线程里线性
     插值重采样到 16k、量化 Int16，每 320 采样（20ms）postMessage 一帧。主线程只
     写环形缓冲 + 算一次 RMS 喂 VAD。addModule 失败/无 AudioWorklet → 同一套
     重采样在 ScriptProcessor 里跑（主线程，抗卡顿差一点但语义相同）。 */
  var VOICE_WORKLET_SRC =
    'class XzCap extends AudioWorkletProcessor{' +
    'constructor(){super();this.r=sampleRate/16000;this.p=0;this.tail=0;' +
    'this.buf=new Int16Array(320);this.n=0}' +
    'process(inputs){var x=inputs[0]&&inputs[0][0];if(!x||!x.length)return true;' +
    'var p=this.p,r=this.r,b=this.buf,n=this.n,L=x.length,s,i,f;' +
    'while(p+1<L){i=Math.floor(p);f=p-i;' +
    's=i<0?this.tail+(x[0]-this.tail)*(p+1):x[i]+(x[i+1]-x[i])*f;' +
    's=s<-1?-1:(s>1?1:s);b[n++]=(s*32767)|0;' +
    'if(n===320){this.port.postMessage(b.buffer,[b.buffer]);b=this.buf=new Int16Array(320);n=0}' +
    'p+=r}' +
    'this.p=p-L;this.tail=x[L-1];this.n=n;return true}}' +
    'registerProcessor("xz-cap",XzCap);';

  function voiceCapStart(stream) {
    var AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) { return Promise.reject(new Error('no-audiocontext')); }
    if (!ORB.actx) { ORB.actx = new AC(); }
    var ctx = ORB.actx;
    try { if (ctx.state === 'suspended') { ctx.resume(); } } catch (e0) { /* ignore */ }
    VOICE.ring = new Int16Array(VOICE.rate * VOICE_RING_SEC);
    VOICE.w = 0;
    var src = ctx.createMediaStreamSource(stream);
    var cap = { src: src, node: null, worklet: false, url: '' };
    VOICE.cap = cap;
    function wireScriptProcessor() {
      var sp = ctx.createScriptProcessor(2048, 1, 1);
      var st = { p: 0, tail: 0, r: ctx.sampleRate / VOICE.rate };
      sp.onaudioprocess = function (ev) {
        var x = ev.inputBuffer.getChannelData(0);
        voicePcmPush(voiceResample(x, st));
      };
      src.connect(sp);
      /* ScriptProcessor 不接 destination 在部分浏览器不会触发回调；接一个
         零增益节点保活，不外放 */
      var g = ctx.createGain();
      g.gain.value = 0;
      sp.connect(g);
      g.connect(ctx.destination);
      cap.node = sp;
      cap.gain = g;
      cap.worklet = false;
    }
    if (!ctx.audioWorklet || typeof URL === 'undefined' || !window.Blob) {
      wireScriptProcessor();
      return Promise.resolve();
    }
    var url = '';
    try {
      url = URL.createObjectURL(new Blob([VOICE_WORKLET_SRC], { type: 'text/javascript' }));
    } catch (e1) { wireScriptProcessor(); return Promise.resolve(); }
    cap.url = url;
    return ctx.audioWorklet.addModule(url).then(function () {
      if (VOICE.cap !== cap) { return; }
      var node = new AudioWorkletNode(ctx, 'xz-cap', {
        numberOfInputs: 1, numberOfOutputs: 0, channelCount: 1 });
      node.port.onmessage = function (ev) { voicePcmPush(new Int16Array(ev.data)); };
      src.connect(node);
      cap.node = node;
      cap.worklet = true;
    }).catch(function () {
      if (VOICE.cap === cap) { wireScriptProcessor(); }
    });
  }
  function voiceCapStop() {
    var cap = VOICE.cap;
    VOICE.cap = null;
    if (!cap) { return; }
    try { if (cap.node && cap.node.port) { cap.node.port.onmessage = null; } } catch (e) { /* */ }
    try { if (cap.node) { cap.node.disconnect(); } } catch (e1) { /* ignore */ }
    try { if (cap.gain) { cap.gain.disconnect(); } } catch (e2) { /* ignore */ }
    try { if (cap.src) { cap.src.disconnect(); } } catch (e3) { /* ignore */ }
    try { if (cap.url) { URL.revokeObjectURL(cap.url); } } catch (e4) { /* ignore */ }
    VOICE.ring = null;
  }
  /* 主线程重采样（ScriptProcessor 回落用；与 worklet 同一算法） */
  function voiceResample(x, st) {
    var out = [];
    var p = st.p, r = st.r, L = x.length, s, i, f;
    while (p + 1 < L) {
      i = Math.floor(p);
      f = p - i;
      s = i < 0 ? st.tail + (x[0] - st.tail) * (p + 1) : x[i] + (x[i + 1] - x[i]) * f;
      s = s < -1 ? -1 : (s > 1 ? 1 : s);
      out.push((s * 32767) | 0);
      p += r;
    }
    st.p = p - L;
    st.tail = x[L - 1];
    return Int16Array.from(out);
  }
  /* 写环形缓冲 + 帧 RMS → VAD。w 是自会话起的绝对采样序号（单调），切片按它算。 */
  function voicePcmPush(pcm) {
    if (!VOICE.on || !VOICE.ring || !pcm || !pcm.length) { return; }
    var ring = VOICE.ring, N = ring.length, w = VOICE.w;
    var s = 0;
    for (var i = 0; i < pcm.length; i++) {
      ring[(w + i) % N] = pcm[i];
      var v = pcm[i] / 32768;
      s += v * v;
    }
    VOICE.w = w + pcm.length;
    voiceVad(Math.sqrt(s / pcm.length), VOICE.w);
  }
  /* 取 [a, b) 的绝对采样区间成 Int16Array（超出缓冲长度的部分已被覆盖，按可得截取） */
  function voicePcmSlice(a, b) {
    var ring = VOICE.ring;
    if (!ring) { return new Int16Array(0); }
    var N = ring.length;
    a = Math.max(a, b - N, 0);
    var out = new Int16Array(Math.max(0, b - a));
    for (var i = 0; i < out.length; i++) { out[i] = ring[(a + i) % N]; }
    return out;
  }
  /* PCM16 → WAV Blob（纯函数，门禁可测：44 字节头 + 数据） */
  function pcmToWav(pcm, rate) {
    var n = pcm.length;
    var buf = new ArrayBuffer(44 + n * 2);
    var v = new DataView(buf);
    function w4(o, str) { for (var i = 0; i < 4; i++) { v.setUint8(o + i, str.charCodeAt(i)); } }
    w4(0, 'RIFF'); v.setUint32(4, 36 + n * 2, true); w4(8, 'WAVE'); w4(12, 'fmt ');
    v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
    v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true);
    v.setUint16(34, 16, true); w4(36, 'data'); v.setUint32(40, n * 2, true);
    var o = 44;
    for (var i = 0; i < n; i++, o += 2) { v.setInt16(o, pcm[i], true); }
    return new Blob([buf], { type: 'audio/wav' });
  }

  /* 一轮「听」：只是打开 VAD 的「听」语义 + 视觉分析器；音频本身一直在环形缓冲里。
     REC 标记与手动录音同形（orbCompute 据 REC 判 listening；stopRec 能收它），
     voice=true 让 stopRec 走回合链。carry＝语音打断带过来的开口位置。 */
  function voiceListen() {
    if (!VOICE.on || VOICE.demo || !VOICE.stream || !VOICE.cap || REC) { return; }
    if (S.busy) { voiceResume(200); return; }
    var carry = VOICE.carry;
    VOICE.carry = null;
    VOICE.speechSeen = !!carry;
    VOICE.speechT0 = carry ? carry.speechT0 : 0;
    VOICE.spStart = carry ? carry.spStart : -1;
    VOICE.quietT0 = 0;
    VOICE.loudT0 = 0;
    VOICE.lastLoud = carry ? Date.now() : 0;
    /* 「一直没人说话」计时跨轮累计：单轮 15s 硬上限到点后接着听时不重置，
       否则 25s 会话超时永远等不到（每 15s 被归零一次） */
    if (!VOICE.silentT0 && !carry) { VOICE.silentT0 = Date.now(); }
    REC = { stream: VOICE.stream, cancelled: false, voice: true,
            cap: setTimeout(voiceEndTurn, VOICE_MAX_TURN_MS) };
    orbAudioStart(VOICE.stream, VOICE.turns > 0);
    VOICE.turns++;
    syncOrb();
  }

  /* 收一轮（cancel=true 为收场：不转写）。REC 无 mr——stopRec 对 voice 标记走这里。 */
  function voiceTurnStop(cancel) {
    var rec = REC;
    REC = null;
    if (rec && rec.cap) { clearTimeout(rec.cap); }
    orbAudioStop();
    syncOrb();
    if (cancel || !rec || !VOICE.on) { return; }
    if (!VOICE.speechSeen || VOICE.spStart < 0) {
      voiceListen();   /* 这一轮其实没人说话（硬上限到点/误触发）：接着听 */
      return;
    }
    VOICE.tEnd = Date.now();
    var pcm = voicePcmSlice(VOICE.spStart, VOICE.w);
    VOICE.spStart = -1;
    if (pcm.length < VOICE.rate * 0.3) { voiceListen(); return; }
    voiceTranscribe(pcmToWav(pcm, VOICE.rate));
  }
  function voiceEndTurn() {
    if (REC && REC.voice) { voiceTurnStop(false); }
  }

  /* ── VAD（能量门，20ms 帧 RMS + 自适应底噪）。两种门：
       听态：≥220ms 连续过门＝开口，随后静默 800ms＝说完；
       说/想态（语音打断）：门更高（×1.6 + 随播报电平上浮）且要 ≥350ms——AEC 之后
       残余回声短而弱，这两条足以不被自己的播报打断。打断触发时 spStart 已指在
       开口前 300ms，随后的新一轮直接带着它继续听。 */
  function voiceVad(lv, w) {
    if (!VOICE.on || VOICE.demo) { return; }
    var now = Date.now();
    if (lv < VOICE.floor + 0.01) { VOICE.floor = VOICE.floor * 0.95 + lv * 0.05; }
    var listening = !!(REC && REC.voice);
    if (listening) {
      var th = Math.max(0.035, VOICE.floor * 2.2 + 0.02);
      if (lv > th) {
        if (!VOICE.speechT0) {
          VOICE.speechT0 = now;
          VOICE.spStart = Math.max(0, w - 320 - VOICE_PREROLL);
        } else if (!VOICE.speechSeen && now - VOICE.speechT0 > VOICE_MIN_SPEECH_MS) {
          VOICE.speechSeen = true;
          VOICE.silentT0 = 0;   /* 有人说话了：会话超时计时归零，下轮重新起算 */
          beacon('asb_voice_turn');
        }
        VOICE.lastLoud = now;
        /* 说完后的 800ms 静默窗只被**持续 ≥120ms** 的响动打断（人接着说），
           键盘/关门这类 1-2 帧的脉冲不该把「说完了」往后拖 */
        if (!VOICE.loudT0) { VOICE.loudT0 = now; }
        if (now - VOICE.loudT0 >= 120) { VOICE.quietT0 = 0; }
      } else if (!VOICE.speechSeen) {
        VOICE.loudT0 = 0;
        /* 开口候选要连续 220ms 过门，但单帧（20ms）跌门不算断——辅音/两源交错
           的帧都会短暂低于门；120ms 以上的安静才放弃候选 */
        if (VOICE.speechT0 && now - VOICE.lastLoud > 120) {
          VOICE.speechT0 = 0;
          VOICE.spStart = -1;
        }
      } else {
        VOICE.loudT0 = 0;
        if (!VOICE.quietT0) {
          VOICE.quietT0 = now;
        } else if (now - VOICE.quietT0 > VOICE_SIL_MS) {
          voiceEndTurn();
          return;
        }
      }
      if (!VOICE.speechSeen && VOICE.silentT0 && now - VOICE.silentT0 > VOICE_IDLE_END_MS) {
        pushChatDom('<div class="asb-msg ai asb-msg--sys">' + esc(t('v_idle_end')) + '</div>');
        voiceStop();
      }
      return;
    }
    /* 语音打断：只在「在说」或「在生成」（可掐）时看；转写期不看（没法掐，且只
       有零点几秒） */
    var canBarge = VOICE.bargeOn &&
      (VOICE.playing || ORB.speaking || (S.busy && S.abortCtl && !VOICE.answerDone));
    if (!canBarge) { VOICE.bargeT0 = 0; return; }
    var out = (VOICE.playing || ORB.speaking) ? Math.max(0, Math.min(1, ORB.level || 0)) : 0;
    var thB = Math.max(0.06, VOICE.floor * 3 + 0.03) + out * 0.05;
    if (lv > thB) {
      if (!VOICE.bargeT0) {
        VOICE.bargeT0 = now;
        VOICE.bargeStart = Math.max(0, w - 320 - VOICE_PREROLL);
      } else if (now - VOICE.bargeT0 > VOICE_BARGE_MS) {
        /* 确认是人在说话：carry 只在此刻成立（短噪音到不了这里，不会污染下一轮） */
        VOICE.carry = { speechT0: VOICE.bargeT0, spStart: VOICE.bargeStart };
        VOICE.bargeT0 = 0;
        beacon('asb_voice_barge');
        voiceInterrupt(true);
      }
      VOICE.lastLoud = now;
    } else if (VOICE.bargeT0 && now - VOICE.lastLoud > 120) {
      VOICE.bargeT0 = 0;
    }
  }

  /* 时延分桶埋点（数据裁决用）：asr=说完→转写回来；ttfs=说完→第一声 */
  function voiceLatBeacon(kind, ms) {
    var b;
    if (kind === 'asr') {
      b = ms < 1000 ? 'lt1' : (ms < 2000 ? '1_2' : (ms < 4000 ? '2_4' : 'gt4'));
    } else {
      b = ms < 2000 ? 'lt2' : (ms < 4000 ? '2_4' : (ms < 7000 ? '4_7' : 'gt7'));
    }
    beacon('asb_voice_' + kind + '_' + b);
  }

  function voiceTranscribe(blob) {
    setBusy(true);   /* 转写期球=思考（对用户而言「在处理我说的话」） */
    setHdStatus('mic_busy');
    var t0 = Date.now();
    transcribeBlob(blob).then(function (text) {
      setBusy(false);
      if (!VOICE.on) { return; }
      voiceLatBeacon('asr', Date.now() - t0);
      text = String(text || '').trim();
      if (!text) { voiceListen(); return; }
      voiceAsk(text);
    }).catch(function (e) {
      setBusy(false);
      micErr((e && e.message) || t('mic_fail'));
      /* 429（限频）别硬撞：停 20s 再听；其余错误 0.9s 后接着听 */
      voiceResume(e && e.status === 429 ? 20000 : 900);
    });
  }

  /* 转写文本直接成为一问：用户气泡 + 问答链（SSE 的 delta 经 voiceFeed 逐句送合成） */
  function voiceAsk(text) {
    VOICE.spoken = 0;
    VOICE.q = [];
    VOICE.answerDone = false;
    VOICE.eager = false;
    VOICE.tPlay = 0;
    sendQuery(text);
    if (!S.busy) { VOICE.answerDone = true; voiceResume(300); }
  }

  /* 句级切分（纯函数，门禁可测）：从 spoken 之后取**完整句**（中英句末标点/换行），
     短于 6 字的句并入下一句（TTS 逐句请求，太碎会一顿一顿）；final=true 时余下
     全部作为最后一句。返回 {sentences, spoken}。 */
  function voiceSplit(text, spoken, final) {
    var rest = String(text || '').slice(spoken);
    var out = [];
    var buf = '';
    var consumed = 0;
    var re = /[^。！？!?；;\n]+[。！？!?；;\n]+|[^。！？!?；;\n]+$/g;
    var m;
    while ((m = re.exec(rest)) !== null) {
      var seg = m[0];
      var complete = /[。！？!?；;\n]$/.test(seg);
      if (!complete && !final) { break; }
      buf += seg;
      consumed += seg.length;
      if (sayPrep(buf).length >= 6) { out.push(buf); buf = ''; }
    }
    if (buf) {
      if (final) { out.push(buf); }
      else { consumed -= buf.length; }   /* 短句留到下一轮与后文合并 */
    }
    return { sentences: out, spoken: spoken + consumed };
  }

  /* 首句提前开口（P2）：整句还没生成完、但已到一个分句（，、：）且 ≥8 字 → 先把这
     一分句送合成。首声时延 = LLM 把首句写完的时间 + 首句 TTS 时长，两项都随首段
     长度涨；只对答案的**第一段**这么做（后续按整句，韵律更顺）。纯函数 voiceEager。 */
  function voiceEager(text) {
    var m = /^([^。！？!?；;\n]{8,60}?[，、：,])/.exec(String(text || ''));
    return m ? m[1] : '';
  }
  function voiceFeed(text, final) {
    if (!VOICE.on || !VOICE.autoSay) { return; }
    if (!final && VOICE.spoken === 0 && !VOICE.eager) {
      var head = voiceEager(text);
      if (head && !/[。！？!?；;\n]/.test(String(text || ''))) {
        VOICE.eager = true;
        VOICE.spoken = head.length;
        var s0 = sayPrep(head, 240);
        if (s0) { voiceEnqueue(s0); }
        return;
      }
    }
    var r = voiceSplit(text, VOICE.spoken, !!final);
    VOICE.spoken = r.spoken;
    for (var i = 0; i < r.sentences.length; i++) {
      var s = sayPrep(r.sentences[i], 240);
      if (s) { voiceEnqueue(s); }
    }
  }
  /* 入队即预取合成（短句并发，播放按序）：第 k 句在播时第 k+1 句已在合成，
     句间不留空白 */
  function voiceEnqueue(text) {
    var item = { text: text, url: '', err: false, p: null };
    item.p = ttsFetch(text).then(function (url) { item.url = url; },
                                 function () { item.err = true; });
    VOICE.q.push(item);
    voicePump();
  }
  function voicePump() {
    if (!VOICE.on || VOICE.playing) { return; }
    var item = VOICE.q.shift();
    if (!item) {
      if (VOICE.answerDone) { voiceSpeakEnd(); }
      return;
    }
    VOICE.playing = true;
    item.p.then(function () {
      if (!VOICE.on) { VOICE.playing = false; return; }
      if (item.err || !item.url) {
        /* 合成失败：第一次诚实说一声，之后本会话静默跳过（文字回答还在） */
        VOICE.ttsFail++;
        if (VOICE.ttsFail === 1) {
          pushChat('<div class="asb-msg err">' + esc(t('say_fail')) + '</div>');
          orbError();
        }
        VOICE.playing = false;
        voicePump();
        return;
      }
      if (!VOICE.au) {
        VOICE.au = new Audio();
        VOICE.au.addEventListener('ended', voiceNext);
        VOICE.au.addEventListener('error', voiceNext);
      }
      VOICE.au.src = item.url;
      if (!ORB.speaking) { orbSpeakStart(VOICE.au); }
      syncOrb();
      if (!VOICE.tPlay && VOICE.tEnd) {
        VOICE.tPlay = Date.now();
        voiceLatBeacon('ttfs', VOICE.tPlay - VOICE.tEnd);   /* 说完→第一声 */
      }
      var pr = VOICE.au.play();
      if (pr && pr.catch) { pr.catch(function () { voiceNext(); }); }
    });
  }
  function voiceNext() {
    VOICE.playing = false;
    if (!VOICE.on) { return; }
    if (VOICE.q.length) { voicePump(); return; }
    if (VOICE.answerDone) { voiceSpeakEnd(); }
    /* 否则：流还在生成，下一句到了 voiceEnqueue 会接着 pump；球保持「说」 */
  }
  function voiceSpeakEnd() {
    orbSpeakStop();
    voiceResume(250);
  }
  function voiceAnswerDone(ok) {
    if (!VOICE.on) { return; }
    VOICE.answerDone = true;
    if (!VOICE.autoSay || !ok) {
      voiceResume(ok ? 300 : 1200);   /* 失败先让玫红态停一下再回到听 */
      return;
    }
    if (!VOICE.playing && !VOICE.q.length) { voiceSpeakEnd(); }
  }
  /* 回到听：延迟一拍等 setBusy(false) 落地；仍忙则再等 */
  function voiceResume(delay) {
    setTimeout(function () {
      if (!VOICE.on || VOICE.demo || REC || VOICE.playing) { return; }
      if (S.busy) { voiceResume(200); return; }
      voiceListen();
    }, delay || 0);
  }

  /* 打断（点大球 /「打断」键 / 语音打断）：停播报、清队列、掐生成，回到听。
     fromVoice=true 时 VOICE.carry 已带着开口位置，新一轮立刻接着听不等 250ms。 */
  function voiceInterrupt(fromVoice) {
    if (!VOICE.on) { return; }
    if (!fromVoice) { beacon('asb_voice_interrupt'); VOICE.carry = null; }
    VOICE.q = [];
    VOICE.answerDone = true;
    if (VOICE.au) { try { VOICE.au.pause(); } catch (e) { /* ignore */ } }
    VOICE.playing = false;
    orbSpeakStop();
    if (S.busy && S.abortCtl) {
      S.aborted = true;
      try { S.abortCtl.abort(); } catch (e2) { /* ignore */ }
    }
    if (VOICE.demo) { return; }
    voiceResume(fromVoice ? 0 : 250);
  }
  function voiceToggleSay() {
    VOICE.autoSay = !VOICE.autoSay;
    var b = $panel ? $panel.querySelector('[data-act="v-mute"]') : null;
    if (b) {
      b.setAttribute('aria-pressed', VOICE.autoSay ? 'false' : 'true');
      b.title = t(VOICE.autoSay ? 'v_autosay_on' : 'v_autosay_off');
      b.setAttribute('aria-label', b.title);
      b.innerHTML = ic(VOICE.autoSay ? 'speaker' : 'speakerOff');
    }
    if (!VOICE.autoSay && (VOICE.playing || VOICE.q.length)) {
      VOICE.q = [];
      if (VOICE.au) { try { VOICE.au.pause(); } catch (e) { /* ignore */ } }
      VOICE.playing = false;
      orbSpeakStop();
      if (VOICE.answerDone) { voiceResume(250); }
    }
  }

  function voiceStop() {
    if (!VOICE.on) { return; }
    VOICE.on = false;
    if (VOICE.demoT) { clearTimeout(VOICE.demoT); VOICE.demoT = 0; }
    if (VOICE.demo) { ORB.force = ''; }
    VOICE.demo = false;
    if (REC && REC.voice) {
      if (REC.cap) { clearTimeout(REC.cap); }
      REC = null;
    }
    VOICE.carry = null;
    VOICE.spStart = -1;
    VOICE.q = [];
    if (VOICE.au) { try { VOICE.au.pause(); } catch (e1) { /* ignore */ } }
    VOICE.playing = false;
    orbSpeakStop();
    orbAudioStop();
    voiceCapStop();
    /* 会话结束才让 AudioContext 休眠（会话期两处 stop 都被 cap 守卫放行了） */
    try {
      if (ORB.actx && ORB.actx.state === 'running' && !REC) { ORB.actx.suspend(); }
    } catch (e0) { /* ignore */ }
    if (VOICE.stream) {
      try { VOICE.stream.getTracks().forEach(function (tk) { tk.stop(); }); }
      catch (e2) { /* ignore */ }
    }
    VOICE.stream = null;
    ORB.connecting = false;
    voiceLoopStop();
    var ft = $panel ? $panel.querySelector('.asb-ftwrap') : null;
    if (ft && MODES[S.tab]) { renderComposer(ft, MODES[S.tab]); }
    syncOrb();
    beacon('asb_voice_end');
  }

  /* 语音块 DOM（取代输入区） */
  function voiceRender(ft) {
    ft.innerHTML = '<div class="asb-voice" data-demo="' + (VOICE.demo ? '1' : '0') + '">' +
      '<canvas class="asb-vorb" data-act="v-interrupt" tabindex="0" role="button" ' +
      'aria-label="' + esc(t('v_interrupt')) + '"></canvas>' +
      '<div class="asb-v-st" aria-live="polite">' + esc(t('hd_' + ORB.state)) + '</div>' +
      '<div class="asb-v-acts">' +
      '<button type="button" class="asb-v-b" data-act="v-interrupt">' + ic('stop') +
      '<span>' + esc(t('v_interrupt')) + '</span></button>' +
      '<button type="button" class="asb-v-b" data-act="v-mute" aria-pressed="' +
      (VOICE.autoSay ? 'false' : 'true') + '" title="' +
      esc(t(VOICE.autoSay ? 'v_autosay_on' : 'v_autosay_off')) + '" aria-label="' +
      esc(t(VOICE.autoSay ? 'v_autosay_on' : 'v_autosay_off')) + '">' +
      ic(VOICE.autoSay ? 'speaker' : 'speakerOff') + '</button>' +
      '<button type="button" class="asb-v-b primary" data-act="v-end">' + ic('x') +
      '<span>' + esc(t('v_end')) + '</span></button>' +
      '</div></div>';
    var cv = ft.querySelector('.asb-vorb');
    VOICE.dpr = Math.min(window.devicePixelRatio || 1, 2);
    var css = window.innerWidth <= 480 ? 132 : 168;
    cv.width = Math.round(css * VOICE.dpr);
    cv.height = cv.width;
    VOICE.cv = cv;
    VOICE.ctx = cv.getContext('2d');
  }

  /* ── 大球渲染（Canvas 2D）：软体 = 基圆 + 2/3/5 次谐波的轮廓调制，振幅随电平
     （听=麦克风、说=TTS、想=流式 token 到达）；内部三团光谱色沿李萨如轨迹漂移
     并裁进球体；外圈辉光随电平；听时攻击音起涟漪。与角标球同一光谱/同一状态色。 */
  function voiceLoopStart() {
    if (VOICE.raf) { return; }
    VOICE.last = 0;
    VOICE.raf = requestAnimationFrame(voiceFrame);
  }
  function voiceLoopStop() {
    if (VOICE.raf) { cancelAnimationFrame(VOICE.raf); VOICE.raf = 0; }
    if (VOICE.ctx && VOICE.cv) {
      try { VOICE.ctx.clearRect(0, 0, VOICE.cv.width, VOICE.cv.height); } catch (e) { /* */ }
    }
    VOICE.cv = null;
    VOICE.ctx = null;
  }
  function rgbMix(a, b, tt) {
    var pa = a.split(','), pb = b.split(',');
    var o = [];
    for (var i = 0; i < 3; i++) {
      o.push(Math.round(+pa[i] + (+pb[i] - +pa[i]) * tt));
    }
    return o.join(',');
  }
  function voiceColor(st) {
    var rb = orbRibbonRgb();
    if (st === 'listening') { return rb[0]; }
    if (st === 'thinking') { return rb[2]; }
    if (st === 'speaking') { return rgbMix(rb[1], rb[2], 0.55); }
    if (st === 'connecting') { return rgbMix(rb[1], '148,163,184', 0.5); }
    if (st === 'error') { return '239,68,68'; }
    if (st === 'alert') { return '245,158,11'; }
    return rb[1];
  }
  function voiceFrame(ts) {
    if (!VOICE.on) { VOICE.raf = 0; return; }
    VOICE.raf = requestAnimationFrame(voiceFrame);
    var ctx = VOICE.ctx, cv = VOICE.cv;
    if (!ctx || !cv || !cv.isConnected) { return; }
    var dt = VOICE.last ? (ts - VOICE.last) : 16.7;
    VOICE.last = ts;
    if (dt <= 0 || dt > 200) { dt = 16.7; }
    var d = dt / 16.7;
    var st = ORB.state;
    var live = st === 'listening' || st === 'speaking';
    /* 角标球的画布在 lv2 活跃态自己读电平、衰减 token 脉冲；它不跑时（安静档/
       reduced-motion）这里代做，电平与脉冲仍然新鲜 */
    if (!ORB.on) {
      if (live) { orbAudioRead(ts); }
      ORB.pulse *= Math.pow(0.94, d);
    }
    var target = live ? Math.max(0, Math.min(1, ORB.level || 0)) : 0;
    VOICE.prevLv = VOICE.lv;
    VOICE.lv += (target - VOICE.lv) * Math.min(1, 0.22 * d);
    var W = cv.width, k = VOICE.dpr, cx = W / 2, cy = W / 2;
    var R = W * 0.32;
    var still = ORB.lvl === 0;
    var spd = still ? 0 : 1;
    VOICE.ph2 += (0.006 + (st === 'thinking' ? 0.018 : 0) + ORB.pulse * 0.02) * d * spd;
    VOICE.ph3 -= (0.009 + VOICE.lv * 0.03) * d * spd;
    VOICE.ph5 += (0.013 + VOICE.lv * 0.05) * d * spd;
    var amp = still ? 0.012
      : ((st === 'thinking' ? 0.045 : 0.022) + VOICE.lv * 0.11 + ORB.pulse * 0.03);
    var br = 1 + (still ? 0 : 0.022 * Math.sin(ts / 1400)) + VOICE.lv * 0.06;
    var col = voiceColor(st);
    var rb = orbRibbonRgb();
    ctx.clearRect(0, 0, W, W);
    /* 辉光 */
    var g = ctx.createRadialGradient(cx, cy, R * 0.55, cx, cy, R * 1.75);
    g.addColorStop(0, 'rgba(' + col + ',' + (0.22 + VOICE.lv * 0.3).toFixed(3) + ')');
    g.addColorStop(1, 'rgba(' + col + ',0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, W, W);
    /* 涟漪（听：攻击音起一圈） */
    if (st === 'listening' && !still && VOICE.lv > 0.3 &&
        VOICE.lv - VOICE.prevLv > 0.06 && ts - VOICE.lastRip > 260) {
      VOICE.lastRip = ts;
      if (VOICE.rings.length < 5) { VOICE.rings.push({ r: R * 1.02, a: 0.35 }); }
    }
    for (var ri = VOICE.rings.length - 1; ri >= 0; ri--) {
      var rg = VOICE.rings[ri];
      rg.r += 1.4 * k * d;
      rg.a *= Math.pow(0.955, d);
      if (rg.a < 0.01) { VOICE.rings.splice(ri, 1); continue; }
      ctx.strokeStyle = 'rgba(' + col + ',' + rg.a.toFixed(3) + ')';
      ctx.lineWidth = 1.5 * k;
      ctx.beginPath();
      ctx.arc(cx, cy, rg.r, 0, 6.2832);
      ctx.stroke();
    }
    /* 软体轮廓 */
    var N = 72;
    ctx.beginPath();
    for (var i = 0; i <= N; i++) {
      var th = (i / N) * 6.2832;
      var r = R * br * (1 + amp * (0.55 * Math.sin(2 * th + VOICE.ph2) +
        0.3 * Math.sin(3 * th + VOICE.ph3) + 0.2 * Math.sin(5 * th + VOICE.ph5)));
      var x = cx + Math.cos(th) * r, y = cy + Math.sin(th) * r;
      if (i === 0) { ctx.moveTo(x, y); } else { ctx.lineTo(x, y); }
    }
    ctx.closePath();
    var fg = ctx.createRadialGradient(cx - R * 0.12, cy - R * 0.16, R * 0.05, cx, cy, R * 1.06);
    fg.addColorStop(0, 'rgb(' + rgbMix(col, '255,255,255', 0.5) + ')');
    fg.addColorStop(0.5, 'rgb(' + col + ')');
    fg.addColorStop(0.86, 'rgb(' + rgbMix(col, '255,255,255', 0.14) + ')');
    fg.addColorStop(1, 'rgba(' + col + ',0.6)');
    ctx.fillStyle = fg;
    ctx.shadowColor = 'rgba(' + col + ',.5)';
    ctx.shadowBlur = 16 * k;
    ctx.fill();
    ctx.shadowBlur = 0;
    /* 内光三团（裁进球体） */
    ctx.save();
    ctx.clip();
    ctx.globalCompositeOperation = ORB.dark ? 'lighter' : 'source-over';
    var cells = [
      { c: rb[0], a: 0.55, f1: 0.00031, f2: 0.00023, p: 0.0, s: 0.62 },
      { c: rb[2], a: 0.5, f1: 0.00027, f2: 0.00037, p: 2.1, s: 0.58 },
      { c: '255,255,255', a: ORB.dark ? 0.28 : 0.42, f1: 0.00041, f2: 0.00029, p: 4.2, s: 0.42 },
    ];
    var tms = still ? 0 : ts;
    for (var ci = 0; ci < cells.length; ci++) {
      var c = cells[ci];
      var px = cx + Math.sin(tms * c.f1 + c.p) * R * 0.42;
      var py = cy + Math.cos(tms * c.f2 + c.p * 1.7) * R * 0.42;
      var cr = R * c.s * (1 + VOICE.lv * 0.25);
      var cg = ctx.createRadialGradient(px, py, 0, px, py, cr);
      cg.addColorStop(0, 'rgba(' + c.c + ',' + c.a.toFixed(3) + ')');
      cg.addColorStop(1, 'rgba(' + c.c + ',0)');
      ctx.fillStyle = cg;
      ctx.fillRect(0, 0, W, W);
    }
    ctx.restore();
    ctx.globalCompositeOperation = 'source-over';
  }

  /* 演示循环（不依赖麦克风）：听 → 想 → 说 各几秒，电平走合成波（无分析器分支）。
     供录演示、官网、老板验收。 */
  function voiceDemoStep(i) {
    if (!VOICE.on || !VOICE.demo) { return; }
    var seq = ['listening', 'thinking', 'speaking'];
    ORB.force = seq[i % 3];
    syncOrb();
    VOICE.demoT = setTimeout(function () { voiceDemoStep(i + 1); },
      seq[i % 3] === 'thinking' ? 2400 : 3400);
  }
  function demo(mode) {
    if (mode === false || mode === 'off') {
      if (VOICE.demo) { voiceStop(); } else { ORB.force = ''; syncOrb(); }
      return true;
    }
    if (mode == null || mode === true || mode === 'loop') {
      return voiceStart({ demo: true });
    }
    if (ORB_STATES.indexOf(String(mode)) >= 0) {
      ORB.force = String(mode);
      syncOrb();
      return true;
    }
    return false;
  }

  /* ────────────────────────────────────────────── 报障 tab */
  function renderReport(body) {
    body.innerHTML = '<div class="asb-rp">' +
      '<textarea maxlength="1000" class="asb-rp-desc" placeholder="' +
      esc(t('rp_ph')) + '"></textarea>' +
      '<div class="asb-rp-shot">' +
      '<button type="button" class="asb-mic asb-mic--pill" data-act="shot-page">' +
      ic('camera') + '<span>' + esc(t('shot_page')) + '</span></button>　' +
      esc(t('rp_shot_hint')) + ' ' +
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
        esc(t('rp_shot_del')) + '">' + ic('x') + '</button></div>'
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
    var btnLabel = function (txt) {
      if (!btn) { return; }
      var sp = btn.querySelector('span');
      if (sp) { sp.textContent = txt; } else { btn.textContent = txt; }
    };
    if (btn) { btn.disabled = true; btnLabel(t('shot_busy')); }
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
      if (btn) { btn.disabled = false; btnLabel(t('shot_page')); }
    });
  }
  function openAnnotate(src) {
    var ov = document.createElement('div');
    ov.className = 'asb-an';
    var bar = document.createElement('div');
    bar.className = 'asb-an-bar';
    bar.innerHTML = '<span>' + esc(t('an_title')) + '</span>' +
      '<button type="button" data-tool="rect" class="on">' + ic('square') +
      '<span>' + esc(t('an_rect')) + '</span></button>' +
      '<button type="button" data-tool="mosaic">' + ic('grid') + '<span>' +
      esc(t('an_mosaic')) + '</span></button>' +
      '<button type="button" data-tool="undo">' + ic('undo') + '<span>' +
      esc(t('an_undo')) + '</span></button>' +
      '<button type="button" data-tool="ok" class="pri">' + ic('check') + '<span>' +
      esc(t('an_ok')) + '</span></button>' +
      '<button type="button" data-tool="cancel">' + ic('x') + '<span>' +
      esc(t('an_cancel')) + '</span></button>';
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
        out.innerHTML = '<div class="asb-msg ai">' + ic('ok', 'asb-i-ok') + ' ' +
          esc(t('rp_ok')) +
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
          '<div class="asb-tk-meta">' +
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
    /* 语音对话块（P1）：点大球=打断 */
    if (act === 'v-interrupt') { voiceInterrupt(); return; }
    if (act === 'v-mute') { voiceToggleSay(); return; }
    if (act === 'v-end') { voiceStop(); return; }
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
      /* 无 data-fx 的旧按钮（缓存的老面板）退化为 安静↔流动 切换 */
      var fx = el.getAttribute('data-fx') || (ORB.lvl >= 2 ? 'calm' : 'flow');
      setOrbFx(fx);
      renderTools();
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
      '">' + ic('x') + '</button>';
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
    firstRunTip();
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
       挂成一级模式；def = {order, icon, iconName?, labelKey|label, mount(el, api),
       unmount?, composer?:{ph, submit(text)}}。iconName（v2.1）指名球内置线性
       图标；icon 保留 emoji 作旧球回退。缺席即该模式不出现。 */
    registerMode: registerMode,
    /* 开面板 + 投问（教学模式「详细讲讲」）：勿对 .asb-ball 调 click() */
    open: function () { togglePanel(true); },
    ask: askFromOutside,
    /* orb 调试钩子：真浏览器门禁/演示页驱动状态（force 空串=还原真实信号） */
    _orbForce: function (st) { ORB.force = st || ''; syncOrb(); },
    _orbState: function () { return ORB.state; },
    _orbLevel: function () { return ORB.lvl; },
    /* v3：待机样式读/写（偏好页段控同一条通路；门禁/演示页用） */
    _orbIdle: function () { return ORB.idle; },
    orbFx: setOrbFx,
    /* P1：语音对话（免手动回合）。voice.start() 需要麦克风可用（boot.voice +
       安全上下文），否则返回 false；demo('loop') 不依赖麦克风，走合成电平循环
       听→想→说，供录演示/官网/验收；demo(false) 收场。 */
    voice: { start: function () { return voiceStart(); }, stop: voiceStop,
             interrupt: voiceInterrupt, on: function () { return VOICE.on; } },
    demo: demo,
    /* 门禁钩子：句级切分纯函数；_voiceSim(text) 跳过麦克风把一句「听到的话」
       直接送进回合链（转写→问答→按句播报→回到听），验证回合状态机。 */
    _voiceSplit: voiceSplit,
    _voiceEager: voiceEager,
    _voiceSim: function (text) {
      if (!VOICE.on) { return false; }
      /* 与真实链同序：一轮「听」先收（真实流程里转写发生在收轮之后） */
      if (REC && REC.voice) { voiceTurnStop(true); }
      VOICE.tEnd = Date.now();
      voiceAsk(String(text || ''));
      return true;
    },
    /* 门禁钩子（P2）：按真实节拍（20ms 一帧）注入给定 RMS 电平的合成 PCM，驱动
       VAD/打断判定；返回 Promise。不经麦克风，测试可完全确定。 */
    _voiceInjectPcm: function (level, ms) {
      return new Promise(function (res) {
        var frames = Math.max(1, Math.round((Number(ms) || 0) / 20));
        var amp = Math.max(0, Math.min(1, Number(level) || 0)) * 32767 * 1.41421;
        var k = 0;
        var tm = setInterval(function () {
          var f = new Int16Array(320);
          for (var i = 0; i < 320; i++) { f[i] = Math.round(Math.sin(i * 0.3) * amp); }
          voicePcmPush(f);
          if (++k >= frames) { clearInterval(tm); res(true); }
        }, 20);
      });
    },
    _voiceWavBytes: function (n) { return pcmToWav(new Int16Array(n), 16000).size; },
    _voiceState: function () {
      return { on: VOICE.on, demo: VOICE.demo, playing: VOICE.playing,
               queued: VOICE.q.length, turns: VOICE.turns, autoSay: VOICE.autoSay,
               answerDone: VOICE.answerDone, rec: !!(REC && REC.voice),
               speechSeen: VOICE.speechSeen, spStart: VOICE.spStart,
               carry: !!VOICE.carry, cap: !!VOICE.cap,
               worklet: !!(VOICE.cap && VOICE.cap.worklet), w: VOICE.w,
               eager: VOICE.eager, tEnd: VOICE.tEnd, tPlay: VOICE.tPlay };
    },
    _orbBurst: orbBurst,
    /* 光谱三色的 rgb（画布同源）：白标推导是否真的到了画布，门禁/演示页读这里 */
    _ribbonRgb: orbRibbonRgb };
})();
