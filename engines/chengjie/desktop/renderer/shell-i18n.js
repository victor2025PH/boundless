/* 桌面壳 · 渲染进程文案 i18n（i18n P0 批 1，2026-08-19）
   ───────────────────────────────────────────────────────────────────────────
   背景：主进程菜单/关于框已由 main.js::SHELL_STR + SS() 收口，但**渲染进程**
   （index.html 静态文案 + renderer.js 动态文案）此前 100% 硬编码中文——英文坐席
   开壳看到的是「回复台 / 客户关系 / 工具箱 / 正在启动后台服务…」。本模块即渲染
   进程侧的对等单源。

   语言解析（与 main.js::shellLang 同语义：**显式 en 才切英文**，其余一律中文，
   存量中文坐席机零行为变化）：
     ① URL ?lang=  —— 权威源。main.js 用 loadFile(..., {query:{lang: shellLang()}})
        把壳配置 unified_inbox.lang 直接钉在页面 URL 上：**首帧即正确**，无需等
        异步 IPC，因此不存在「先闪中文再跳英文」的 FOUC，也不需要整页 reload 兜。
        同一个 ?lang= 还被 shared/copilot/i18n/cp-i18n.js 的 _resolveLang() 直接
        消费 —— 整条 cp-* 组件链（草稿/关系阶段/协作/账号/语音）零接线自动英文化。
     ② localStorage 缓存 —— 旧版 main.js 未带 query 时（壳未随包升级、pip 窗等）
        的回落，由 ① 命中时写入。
     ③ <html lang> —— 最后回落（模板里是 zh）。
   ⚠ 本模块必须在 <head> 内同步加载：既为在首帧前改掉 title/lang，也为让 body
   末尾的 cp-i18n.js 读到已就位的 window.CP_LANG。CSP 是 script-src 'self'，
   故只能是外链文件，不能内联。

   消费方式：
     · index.html 静态文案 → data-sh-i18n / -txt / -ph / -title / -aria 属性
     · renderer.js 等动态文案 → window.SH('key') （同 main.js SS() 的心智模型）
   零回归底线：dict 缺键 → 回落 zh → 回落 key 本身，绝不返回 undefined。 */
(function (root) {
  'use strict';

  var DICT = {
    zh: {
      'app.title': '智聊 · 桌面工作台',
      'notice.dismiss': '暂不',
      'rail.aria': '工作台标签栏',
      'brand.workbench': '智聊 · 坐席工作台',
      'brand.mark': '智',
      'brand.product': '智聊',
      'console.manual': '人工操作台',
      'rail.inbox.title': '人工操作台（统一收件箱 · AI+人工协作）',
      'rail.inbox.chip': 'AI+人工',
      // platform-caps.js 的诚实提示（assist-only 内嵌页）：只回 key，文案在此
      // 壳级通知条（update-notify.pickNotice 只回 key+vars）
      'notice.forced_ready': '当前版本已停止支持 · 新版本已就绪，请立即重启更新（约 30 秒，不影响账号与聊天记录）',
      'notice.forced_downloading': '当前版本已停止支持 · 正在下载新版本 {pct}%，完成后请立即重启更新',
      'notice.forced_fetching': '当前版本已停止支持 · 正在获取新版本，请保持网络连接稍候',
      'notice.update_ready': '新版本 {v}已就绪 · 点击重启即完成更新（约 30 秒，不影响账号与聊天记录）',
      'notice.update_downloading': '新版本 {v}后台下载中 {pct}% · 完成后一键重启生效，现在可继续正常使用',
      'caps.tag_manual': '人工',
      'caps.menu_manual': '（人工）',
      'caps.banner_messenger': '此标签=官方网页（人工聊天/翻译）。全自动请用「人工操作台」（统一收件箱）——需服务器完整登录（含加密 PIN），与本页登录无关。',
      'caps.banner_generic': '此标签=官方网页（人工聊天/翻译/标签）。全自动收发请到「人工操作台」（统一收件箱）。',
      'rail.inbox.chip_title': 'AI 拟稿/自动回复 + 人工审核协作',
      'conn.connecting': '正在连接后台…',
      'conn.retry': '重试连接',
      'conn.spawning': '正在启动后台服务，请稍候…（首次启动较慢）',
      'conn.starting_secs': '正在启动后台服务…（已 {secs}s）',
      'conn.first_boot_init': '首次启动初始化中（约 1–2 分钟，仅此一次）…已 {secs}s',
      'conn.loading_ws': '正在载入工作台…',
      'conn.logging_in': '正在登录后台…',
      'conn.logging_in_alt': '首选凭据失败，尝试备用凭据…',
      'conn.login_manual': '自动登录失败，请在页面手动登录',
      'conn.err_unknown': '未知错误',
      'conn.err_code': '错误 {code}',
      'conn.spawn_failed': '后台启动失败：{err}\n详见 用户数据/logs/backend.log；将持续重试…',
      'conn.spawn_failed_short': '后台启动失败：{err}\n详见 用户数据/logs/backend.log。',
      'conn.waiting_secs': '后台还没起来（已等待 {secs}s，首次启动较慢）。',
      'conn.offline_shell': '后台还没起来（当前是离线页）。\n正在等待后台启动并自动重连…\n后端地址：{base}',
      'conn.fail_load': '无法连接后台服务（{why}）。\n正在等待后台启动并自动重连…\n后端地址：{base}',
      'conn.auto_retry': '{why}\n正在自动重试…\n后端地址：{base}',
      'conn.port_conflict': '后端端口被占用：{who}\n请停掉占用该端口的程序，或改 config.json 的 backend.base_url 端口后重启本应用。\n后端地址：{base}',
      'stage.no_accounts': 'config.json 里没有启用任何账号',
      'tab.assist_title': '{label}（官方网页·仅人工；全自动请用「人工操作台」）',
      'tab.plain_title': '{label}（{platform}:{id}）',
      'tab.remove': '移除该内嵌标签',
      'tab.assist_chip_title': '仅人工聊天/翻译；全自动在「人工操作台」',
      'tab.dot_idle': '等待注入…',
      'tab.via_inbox_title': '{label}（{platform}）无官方网页版聊天，请在「人工操作台」中使用',
      'tab.via_workbench': '↪工作台',
      'tab.via_inbox_flash': '{label} 无官方网页版，已切到人工操作台',
      'tab.add_title': '新增内嵌账号标签（Telegram / WhatsApp 网页版，在标签内扫码登录）',
      'tab.add_label': '新增',
      'account.auto_label': '{base} 账号{n}',
      'add.no_web': '该平台无可内嵌网页版',
      'add.no_url': '无法新增：缺少该平台网页地址',
      'add.failed': '新增失败',
      'add.done': '已新增内嵌标签：{label}（请在标签内扫码登录）',
      'tab.removed': '已移除内嵌标签',
      'web.disabled': '内嵌官方网页版未启用（config.embedded_official_pages）',
      'web.no_embeddable': '该平台无可内嵌的官方网页版',
      'web.switched': '已切到网页版标签：{label}',
      'web.group_assist': '官方平台 · 人工',
      'web.group_assist_title': '官方网页版账号：人工聊天/翻译辅助；全自动请用「人工操作台」',
      'cp.resize': '拖动调节宽度（双击恢复默认）',
      'cp.title': '业务助手',
      'cp.open_inbox': '在人工操作台（统一收件箱）打开该会话',
      'cp.accounts': '账号管理（多账号扫码登录）',
      'cp.health': '自动化健康：全账号注入命中 + 受控出站队列概览',
      'cp.mode': '切换统一 App（实验，与网页同源同款）',
      'cp.collapse': '折叠/展开',
      'tab.reply': '回复台',
      'tab.customer': '客户关系',
      'tab.tools': '工具箱',
      'cp.empty': '打开一个会话即可显示智能草拟、AI 分析、知识库与客户档案',
      'card.draft': '回复工坊',
      'card.voice': '语音克隆 / 发送',
      'card.kb': '知识库',
      'card.tpl': '快捷回复',
      'card.profile': '客户信息',
      'card.relstage': '关系阶段',
      'card.chain': '跟进 SOP（工作链）',
      'card.collab': '协作上下文',
      'card.analysis': 'AI 对话分析',
      'voice.note': '与 📥 人工操作台同源 API；完整横向工具栏请用「人工操作台」标签。',
      'kb.ph': '搜索话术/知识条目…',
      'kb.btn': '搜',
      'analysis.btn': '分析当前对话',
      'pill.guard_high': '高风险',
      'pill.guard_medium': '中风险',
      'pill.draft_ready': '已生成',
      'pill.chain_failed': '{n} 失败',
      'pill.chain_running': '{n} 运行中',
      'pill.snoozed': '搁置中',
      'cp.showcase_ft': '打开一个会话即可开始',
      'exec.done': '已执行 ✓',
      'exec.failed': '执行失败，请稍后重试',
      'inbox.disabled': '人工操作台（统一收件箱）未启用',
      'inbox.switched_assist': '已切到人工操作台 — Messenger 全自动需在此完成服务器登录',
      'inbox.switched': '已切到人工操作台',
      'inbox.opened_assist': '已在人工操作台打开 — 全自动请确认账号已服务器登录',
      'inbox.opened': '已在人工操作台打开 📥',
      'inbox.not_ready': '收件箱尚未就绪，请稍后重试',
      'iframe.loading': '正在加载业务面板…',
      'iframe.timeout': '统一业务面板加载超时，已回退经典面板（后台就绪后自动恢复）',
      'iframe.restored': '统一业务面板已恢复 ✓',
      'send.filled_sent': '已填入并发送 ✓',
      'send.filled': '已填入 ✓',
      'send.peer_prefix': '会话对象：',
      'send.confirm_high': '⚠ 高风险内容，命中敏感词：{terms}\n（支付/密码/账号安全类）\n\n确认仍要直接发送给客户吗？',
      'send.confirm_medium': '提醒：命中需谨慎词：{terms}\n（优惠/投诉/法律类）\n\n确认发送？',
      'send.confirm_robotic': '提醒：回复像 AI 口吻（含「{robo}」），可能露馅。\n\n仍要发送吗？',
      'act.copied': '已复制 ✓',
      'act.copy_failed': '复制失败',
      'act.fill': '填入',
      'act.fill_send': '填入并发送',
      'act.copy': '复制',
      'sep.enum': '、',
      'tab.shortcut_hint': '快捷键 Ctrl+{n}',
      'msg.file_prefix': '[文件] ',
      'an.intent': '意图：',
      'an.sentiment': '情绪：',
      'an.lang': '语种：',
      'an.failed': '分析失败',
      'an.running': '分析中…',
      'profile.load_failed': '档案加载失败',
      'profile.loading': '加载中…',
      'profile.empty': '暂无档案（会话刚同步，稍后重试）',
      /* 档案 tag 的**标签**（值由后端给）。冒号带进词条：英文用 ": "、中文用全角「：」，
         拼接处不再各自补冒号（那是「英文里蹦出全角标点」的经典来源）。 */
      'profile.stage': '阶段：',
      'profile.lang': '语言：',
      'profile.intimacy': '亲密度：',
      'profile.msgs': '消息：',
      'kb.search_failed': '搜索失败',
      'kb.no_match': '无匹配条目',
      'kb.entry': '条目',
      'kb.hint_input': '输入关键词搜索知识库',
      'kb.searching': '搜索中…',
      'tpl.load_failed': '模板加载失败',
      'tpl.empty': '暂无快捷回复模板',
      'notice.update_restart': '立即重启更新',
      'notice.open_detail': '查看详情',
      'notice.restarting': '正在重启…',
      /* 注入诊断（键名＝inject-status.js 的 code＝后端 classify_inject_health 状态字，
         三处同名；`.d` 是 tooltip 详解，{bubbles}/{tried} 由纯函数的 vars 插值）。 */
      'inject.wait': '等待注入…',
      'inject.wait.d': '注入脚本尚未上报状态',
      'inject.unsupported': '无注入档案',
      'inject.unsupported.d': '该平台无选择器档案，功能不可用',
      'inject.no_chat': '未登录/未进入会话',
      'inject.no_chat.d': '未检测到会话或输入框：请扫码登录并打开一个对话',
      'inject.mismatch_composer': '选择器失配（输入框）',
      'inject.mismatch_composer.d': '找不到输入框，注入可能因官方改版失效（需校准 PROFILES.composer）',
      'inject.mismatch_bubble': '选择器失配（消息）',
      'inject.mismatch_bubble.d': '会话已打开但抓不到消息气泡（需校准 PROFILES.bubble/text）',
      'inject.mismatch_text': '选择器失配（正文提取）',
      'inject.mismatch_text.d': '抓到 {bubbles} 条气泡但一条都提不出正文/媒体（需校准 PROFILES.bubbleText）',
      'inject.mismatch_ingest': '选择器失配（消息标识）',
      'inject.mismatch_ingest.d': '{tried} 条待回流消息取不到 msg_id/会话 id，同步已静默中断（需校准 mid/peerId）',
      'inject.ok': '注入正常',
      'inject.ok.d': '输入框 ✓　消息气泡 ×{bubbles}',
      /* 账号综合健康三维（webmulti.js 只出键，取词在此） */
      'health.session_online': '在线',
      'health.session_offline': '掉线',
      'health.unknown': '未知',
      'health.inject_unsupported': '无注入档案',
      'health.inject_ok': '注入正常',
      'health.inject_warn': '注入失配',
      'health.inject_bad': '注入异常',
      'health.inject_wait': '等待注入',
      'health.translate_ok': '翻译正常',
      'health.translate_down': '翻译不可达',
      'health.all_ok': '正常',
      'health.waiting': '等待',
      /* 🩺 自动化健康看板（health-panel.js）。注入状态文案**刻意复用上面的 inject.***
         ——同一件事在状态条与看板里必须同一说法，各写一份迟早分叉。 */
      'hp.title': '🩺 自动化健康',
      'hp.refresh': '刷新',
      'hp.sec_inject': '注入命中',
      'hp.sec_outbound': '受控出站',
      'hp.colon': '：',
      'hp.inj_persistent': '{n} 账号持续失配',
      'hp.inj_persistent_h': '疑似官方网页改版，可热修 selector-profiles(D1)',
      'hp.inj_mismatch': '{n} 账号选择器失配',
      'hp.inj_mismatch_h': '短暂失配可自愈；持续则需校准',
      'hp.inj_none': '暂无注入账号',
      'hp.inj_none_h': '打开内嵌官方页并登录后开始上报',
      'hp.inj_ok': '全部正常（{n}）',
      'hp.ob_failed': '{n} 条发送失败',
      'hp.ob_failed_h': 'DOM 发送未成功（已记 failed，非误判已送达）',
      'hp.ob_held': '{n} 条待人工审核',
      'hp.ob_held_h': 'review_mode：放行后才会自动发送',
      'hp.ob_active': '活动中：待发 {pending} · 发送中 {claimed}',
      'hp.ob_idle': '队列空闲',
      'hp.st_pending': '待发',
      'hp.st_claimed': '发送中',
      'hp.st_sent': '已发送',
      'hp.st_failed': '失败',
      'hp.st_held': '待审核',
      'hp.st_cancelled': '已拦截',
      'hp.st_unreported': '未上报',
      'hp.act_cancel': '拦截',
      'hp.act_hold': '暂停',
      'hp.act_edit': '改写',
      'hp.act_release': '放行',
      'hp.act_retry': '重试',
      'hp.dur_sec': '{n} 秒',
      'hp.dur_min': '{m} 分',
      'hp.dur_min_sec': '{m} 分 {s} 秒',
      'hp.dur_hour': '{h} 时',
      'hp.dur_hour_min': '{h} 时 {m} 分',
      'hp.for_duration': ' · 已持续 {d}',
      'hp.stale': '数据陈旧',
      'hp.dot_mismatch': '{n} 个账号注入持续失配（疑似官方网页改版，可热修 selector-profiles）',
      'hp.dot_default': '自动化健康：全账号注入命中 + 受控出站队列概览',
      'hp.ai_filled': '已填入 ✓',
      'hp.dot_sla_warn': '{n} 条待审超时未处理（review_mode），客户可能久等——点开 🩺 尽快放行/改写',
      'hp.dot_sla_urgent': '🔴 严重：{n} 条待审超时未处理（review_mode），客户可能已流失——请立即放行/改写',
      'hp.val_failed': '校验失败：{err}',
      'hp.val_no_backend': '后端不可达',
      'hp.val_absent': '无覆写文件（注入用内置档）',
      'hp.val_invalid': 'JSON 无效：{err}',
      'hp.val_parse_failed': '解析失败',
      'hp.val_ok': '有效 ✓ {n} 个平台覆写',
      'hp.val_dropped': ' · 已忽略 {n} 项（{list}）',
      'hp.sel_composer': '输入框',
      'hp.sel_sendBtn': '发送按钮',
      'hp.sel_bubble': '消息气泡',
      'hp.sel_peerTitle': '对话标题',
      'hp.diag': '失配定位：{list} → 优先校准这些 selector key',
      'hp.diag_title': '跨失配账号统计各 selector key 抓空次数，定位官方改版到底改了哪个元素',
      'hp.alerts_hdr': '⚠ 注入持续失配（{n} 个账号）',
      'hp.alerts_ft': '疑似官方网页改版 → 点「热修选择器」打开 desktop_selector_profiles.json，保存后注入下次拉取即生效',
      'hp.fix_btn': '热修选择器',
      'hp.fix_title': '打开覆写文件，按平台填正确选择器即可热修（无需重发桌面包）',
      'hp.rate_nosample': '拦截率 —（样本不足）',
      'hp.rate': '近 7 日拦截率 {pct}（{n} 条已审）',
      'hp.corr': '纠正样本 {total} 条（改写 {edit}{ai}）',
      'hp.corr_ai': ' · AI 协同 {n}',
      'hp.corr_title': '人审改写/拦截沉淀的 AI 失误样本，供离线调优',
      'hp.export_btn': '导出样本',
      'hp.export_title': '导出 JSONL 偏好对（rejected/chosen），喂 DPO/eval',
      'hp.rsn_prompt': '拦截原因：',
      'hp.rsn_off_topic': '答非所问',
      'hp.rsn_tone': '语气不当',
      'hp.rsn_factual': '事实错误',
      'hp.rsn_over_boundary': '越界违规',
      'hp.rsn_redundant': '冗余',
      'hp.rsn_other': '其他',
      'hp.cluster': '失误聚类：{list}',
      'hp.cluster_title': '人审拦截按原因聚类，看 AI 最常错在哪类',
      'hp.review_hdr': '🔎 待审 {n} 条（先进先出）',
      'hp.bulk_release': '全部放行',
      'hp.bulk_cancel': '全部拦截',
      'hp.confirm_bulk_cancel': '确认拦截全部 {n} 条待审命令？此操作不可撤销。',
      'hp.edit_ph': '输入改写后的内容…',
      'hp.ai_btn': 'AI 重写',
      'hp.ai_title': '按客户会话上下文生成更好的候选，可再编辑后保存',
      'hp.save': '保存',
      'hp.cancel': '取消',
      'hp.no_inject': '暂无注入上报。',
      'hp.no_outbound': '暂无出站命令（未开 desktop_bridge / 无 desktop 账号全自动回复）。',
      'hp.validate_btn': '校验覆写',
      'hp.validate_title': '校验 desktop_selector_profiles.json：JSON 是否合法、有无被忽略字段',
      'hp.reload_btn': '重载注入',
      'hp.reload_title': '重载内嵌官方页 → 注入重拉选择器（热修保存后点此即时生效，无需重启）',
      'hp.footer': '每 10 秒自动刷新 · 持续失配可改 config/desktop_selector_profiles.json 热修',
      'hp.opening': '打开中…',
      'hp.opened': '已打开 ✓',
      'hp.open_failed': '打开失败',
      /* 活动海报判定行（P0 2026-08-22 可诊断化）：回答「为什么没弹」——
         原因人话与 campaign-model.cmEligible 的 reason 一一对应。 */
      'hp.camp': '活动海报',
      'hp.camp_shown': '已展示（{id}）',
      'hp.camp_preview': '预览模式（--poster-preview）',
      'hp.camp_skip': '未弹：{why}',
      'hp.camp_no_query': '本会话尚未查询',
      'hp.camp_feed': 'feed {n} 条',
      'hp.camp_r_no_feed': '未拉到活动 feed（离线/首启竞态，下次启动自愈）',
      'hp.camp_r_not_managed': '非托管版（开发态不弹，属预期）',
      'hp.camp_r_no_onboarding': '首启向导未完成（新用户海报以向导完成为锚）',
      'hp.camp_r_window_passed': '新用户 72h 窗已过（本机是老安装，属预期）',
      'hp.camp_r_max_shows': '展示次数已用完',
      'hp.camp_r_interval': '距上次展示未满间隔（12h 冷却）',
      'hp.camp_r_opted_out': '本机点过「不再提醒」',
      'hp.camp_r_not_started': '活动未开始',
      'hp.camp_r_ended': '活动已结束',
      'hp.camp_hint': '验收强制弹出：带 --poster-preview 启动（跳资格/频控，不污染读数）',
      'hp.validating': '校验中…',
      'hp.val_fail_short': '校验失败',
      'hp.exporting': '导出中…',
      'hp.exported': '已导出 {n} 条 ✓',
      'hp.export_failed': '导出失败',
      'hp.reloaded': '已重载 {n} 个内嵌页（注入将重拉选择器）',
      'hp.reload_none': '无内嵌官方页（当前为人工操作台模式）',
      'hp.read_failed': '读取健康数据失败：{err}',
      'hp.generating': '生成中…',
      'hp.no_context': '无上下文',
      'hp.failed_short': '失败',
      'hp.sla_urgent_title': '🔴 待审严重超时',
      'hp.sla_urgent_body': '{n} 条 AI 回复待审已超 {mins} 分钟，客户极可能流失，请立即处理',
      'hp.sla_warn_title': '待审超时',
      'hp.sla_warn_body': '{n} 条 AI 回复待人工审核已超 {mins} 分钟，请尽快放行/改写',
      'boot.title': '正在启动后台服务…',
      'boot.sub': '首次启动较慢（约 1 分钟），就绪后自动进入业务面板',
      'boot.connecting': '正在连接后台服务…（就绪后自动进入业务面板）',
      'boot.failed': '后台服务启动失败：请重启应用；反复出现请联系运维（日志：logs/backend.log）',
      'boot.port_conflict': '端口被其他程序占用，后台服务无法启动——请联系运维处理',
      'appframe.title': '统一业务面板',
      /* ── 开机动画（splash P0 2026-08-22）────────────────────────────────
         双轨文案制：splash.amb.*＝氛围层（赛博朋克叙事，可以浪漫）；
         splash.stage.* / splash.real*＝真实层（阶段+秒数，永远诚实）。
         两层同屏，氛围行按当前真实阶段从对应池轮换（splash-model 确定性取索引，
         池大小契约在 splashModel.AMBIENT_COUNTS，改这边键数必须同步那边）。 */
      'splash.brand.co': '无界科技',
      'splash.tagline': '多平台智能会话 · 人机协同工作台',
      'splash.skip': '跳过动画',
      'splash.real': '[阶段 {n}/6] {stage} · 已 {secs}s',
      'splash.real_eta': ' · 预计还需约 {secs}s',
      'splash.real_slow': ' · 首次启动较慢（解包初始化，仅此一次）',
      'splash.err.title': '启动序列中断',
      'splash.err.retry': '重试连接',
      'splash.err.copy': '复制诊断信息',
      'splash.err.copied': '已复制 ✓',
      'splash.stage.shell': '初始化桌面环境',
      'splash.stage.probe': '检测本地服务',
      'splash.stage.engine': '启动智能引擎',
      'splash.stage.link': '建立加密会话',
      'splash.stage.load': '载入工作台',
      'splash.stage.done': '就绪',
      'splash.term.shell': '✓ 桌面环境初始化完成',
      'splash.term.probe': '✓ 本地服务探测完成',
      'splash.term.engine': '✓ 智能引擎在线',
      'splash.term.link': '✓ 加密会话已建立',
      'splash.term.load': '✓ 工作台界面就绪',
      'splash.amb.shell.0': '系统内核加载完成 · 欢迎回到无界',
      'splash.amb.probe.0': '正在扫描本地算力节点…',
      'splash.amb.probe.1': '正在绘制网络拓扑地图…',
      'splash.amb.engine.0': '正在唤醒量子计算机群…',
      'splash.amb.engine.1': '正在进行深度神经网络数据搭建…',
      'splash.amb.engine.2': '正在点亮神经元矩阵 · 1,024 个智能体就位',
      'splash.amb.engine.3': '正在装载人设人格核心…',
      'splash.amb.engine.4': '正在预热语音声纹反应堆…',
      'splash.amb.engine.5': '正在注入情感计算模块…',
      'splash.amb.engine.6': '正在给 AI 倒一杯电子咖啡 ☕',
      'splash.amb.link.0': '正在建立高强度加密安全隧道…',
      'splash.amb.link.1': '量子密钥交换完成 · 信道强度 256-bit',
      'splash.amb.link.2': '防御矩阵上线 · 风控护盾 100%',
      'splash.amb.load.0': '正在展开全息工作界面…',
      'splash.amb.load.1': '正在同步全球会话时区…',
      'splash.amb.load.2': '多语言翻译引擎在线 · 17 种语言待命',
      'splash.amb.load.3': '正在编织跨平台通信网络…',
      'splash.amb.done.0': '无界网络 · 已连接',
      'splash.amb.done.1': '星际链路握手完成 · 欢迎回来，指挥官',
      // 融合标题栏细条（titlebar merge P2 2026-08-22）：⋯应急菜单（白屏保命通道）
      'tb.more': '关于 / 上传诊断 / 检查更新',
      'tb.about': '关于',
      'tb.diag': '上传诊断给客服',
      'tb.update': '检查更新',
    },
    en: {
      'app.title': 'ChatX · Desktop Workbench',
      'notice.dismiss': 'Not now',
      'rail.aria': 'Workbench tabs',
      'brand.workbench': 'ChatX · Agent Workbench',
      'brand.mark': 'C',
      'brand.product': 'ChatX',
      'console.manual': 'Manual Console',
      'rail.inbox.title': 'Manual Console (Unified Inbox · AI + human collaboration)',
      'rail.inbox.chip': 'AI+human',
      'notice.forced_ready': 'This version is no longer supported · A new version is ready — please restart now (about 30 seconds; accounts and chat history are kept)',
      'notice.forced_downloading': 'This version is no longer supported · Downloading the new version ({pct}%) — please restart as soon as it finishes',
      'notice.forced_fetching': 'This version is no longer supported · Fetching the new version — please stay connected',
      'notice.update_ready': 'Version {v}is ready · Click restart to finish updating (about 30 seconds; accounts and chat history are kept)',
      'notice.update_downloading': 'Version {v}downloading in the background ({pct}%) · Restart once to apply; you can keep working for now',
      'caps.tag_manual': 'Manual',
      'caps.menu_manual': ' (manual)',
      'caps.banner_messenger': 'This tab is the official web app (manual chat / translation). Full automation runs in the Manual Console (Unified Inbox) and needs a complete server-side login including the encryption PIN — unrelated to the login on this page.',
      'caps.banner_generic': 'This tab is the official web app (manual chat / translation / tagging). For automated send & receive, use the Manual Console (Unified Inbox).',
      'rail.inbox.chip_title': 'AI drafting / auto-reply + human review',
      'conn.connecting': 'Connecting to the backend…',
      'conn.retry': 'Retry',
      'conn.spawning': 'Starting the backend service, please wait… (the first launch is slower)',
      'conn.starting_secs': 'Starting the backend service… ({secs}s elapsed)',
      'conn.first_boot_init': 'First-run initialization (about 1–2 minutes, one time only)… {secs}s elapsed',
      'conn.loading_ws': 'Loading the workbench…',
      'conn.logging_in': 'Signing in to the backend…',
      'conn.logging_in_alt': 'Primary credentials failed, trying the backup set…',
      'conn.login_manual': 'Automatic sign-in failed — please sign in on the page',
      'conn.err_unknown': 'unknown error',
      'conn.err_code': 'error {code}',
      'conn.spawn_failed': 'Backend service failed to start: {err}\nSee User Data/logs/backend.log; retrying…',
      'conn.spawn_failed_short': 'Backend service failed to start: {err}\nSee User Data/logs/backend.log.',
      'conn.waiting_secs': 'The backend is not up yet (waited {secs}s; the first launch is slower).',
      'conn.offline_shell': 'The backend is not up yet (this is the offline page).\nWaiting for it to start, reconnecting automatically…\nBackend: {base}',
      'conn.fail_load': 'Cannot reach the backend service ({why}).\nWaiting for it to start, reconnecting automatically…\nBackend: {base}',
      'conn.auto_retry': '{why}\nRetrying automatically…\nBackend: {base}',
      'conn.port_conflict': 'The backend port is already in use: {who}\nStop the program holding that port, or change backend.base_url in config.json and restart this app.\nBackend: {base}',
      'stage.no_accounts': 'No accounts are enabled in config.json',
      'tab.assist_title': '{label} (official web · human-only; use the Manual Console for full automation)',
      'tab.plain_title': '{label} ({platform}:{id})',
      'tab.remove': 'Remove this embedded tab',
      'tab.assist_chip_title': 'Human chat / translation only; full automation lives in the Manual Console',
      'tab.dot_idle': 'Waiting for injection…',
      'tab.via_inbox_title': '{label} ({platform}) has no official web chat — use it from the Manual Console',
      'tab.via_workbench': '↪Workbench',
      'tab.via_inbox_flash': '{label} has no official web version — switched to the Manual Console',
      'tab.add_title': 'Add an embedded account tab (Telegram / WhatsApp Web — scan the QR inside the tab)',
      'tab.add_label': 'Add',
      'account.auto_label': '{base} Account {n}',
      'add.no_web': 'This platform has no embeddable web app',
      'add.no_url': 'Cannot add: the web address for this platform is missing',
      'add.failed': 'Could not add the tab',
      'add.done': 'Embedded tab added: {label} (scan the QR code inside the tab)',
      'tab.removed': 'Embedded tab removed',
      'web.disabled': 'Embedded official web pages are disabled (config.embedded_official_pages)',
      'web.no_embeddable': 'This platform has no embeddable official web app',
      'web.switched': 'Switched to the web tab: {label}',
      'web.group_assist': 'Official platforms · human',
      'web.group_assist_title': 'Official web accounts: human chat / translation assist; use the Manual Console for full automation',
      'cp.resize': 'Drag to resize (double-click to reset)',
      'cp.title': 'Copilot',
      'cp.open_inbox': 'Open this conversation in the Manual Console (Unified Inbox)',
      'cp.accounts': 'Accounts (multi-account QR login)',
      'cp.health': 'Automation health: injection coverage across accounts + outbound queue overview',
      'cp.mode': 'Switch to the unified App (experimental, same build as the web workbench)',
      'cp.collapse': 'Collapse / expand',
      'tab.reply': 'Replies',
      'tab.customer': 'Customer',
      'tab.tools': 'Tools',
      'cp.empty': 'Open a conversation to see AI drafts, analysis, knowledge base and the customer profile',
      'card.draft': 'Reply Studio',
      'card.voice': 'Voice clone / send',
      'card.kb': 'Knowledge base',
      'card.tpl': 'Quick replies',
      'card.profile': 'Customer info',
      'card.relstage': 'Relationship stage',
      'card.chain': 'Follow-up SOP (work chain)',
      'card.collab': 'Collaboration context',
      'card.analysis': 'AI conversation analysis',
      'voice.note': 'Same API as the 📥 Manual Console; use the Manual Console tab for the full toolbar.',
      'kb.ph': 'Search replies / knowledge entries…',
      'kb.btn': 'Go',
      'analysis.btn': 'Analyze this conversation',
      'pill.guard_high': 'High risk',
      'pill.guard_medium': 'Medium risk',
      'pill.draft_ready': 'Draft ready',
      'pill.chain_failed': '{n} failed',
      'pill.chain_running': '{n} running',
      'pill.snoozed': 'Snoozed',
      'cp.showcase_ft': 'Open a conversation to get started',
      'exec.done': 'Done ✓',
      'exec.failed': 'Action failed, please try again',
      'inbox.disabled': 'The Manual Console (Unified Inbox) is disabled',
      'inbox.switched_assist': 'Switched to the Manual Console — Messenger automation needs a server-side login here',
      'inbox.switched': 'Switched to the Manual Console',
      'inbox.opened_assist': 'Opened in the Manual Console — for automation make sure the account is logged in server-side',
      'inbox.opened': 'Opened in the Manual Console 📥',
      'inbox.not_ready': 'The inbox is not ready yet, please try again',
      'iframe.loading': 'Loading the business panel…',
      'iframe.timeout': 'The unified business panel timed out; fell back to the classic panel (it restores automatically once ready)',
      'iframe.restored': 'Unified business panel restored ✓',
      'send.filled_sent': 'Filled in and sent ✓',
      'send.filled': 'Filled in ✓',
      'send.peer_prefix': 'Conversation with: ',
      'send.confirm_high': '⚠ High-risk content, matched sensitive terms: {terms}\n(payment / password / account security)\n\nSend this to the customer anyway?',
      'send.confirm_medium': 'Heads-up: matched sensitive terms: {terms}\n(promotion / complaint / legal)\n\nSend it?',
      'send.confirm_robotic': 'Heads-up: this reply sounds AI-like (contains "{robo}") and may break character.\n\nSend it anyway?',
      'act.copied': 'Copied ✓',
      'act.copy_failed': 'Copy failed',
      'act.fill': 'Fill in',
      'act.fill_send': 'Fill in & send',
      'act.copy': 'Copy',
      'sep.enum': ', ',
      'tab.shortcut_hint': 'Shortcut: Ctrl+{n}',
      'msg.file_prefix': '[File] ',
      'an.intent': 'Intent: ',
      'an.sentiment': 'Sentiment: ',
      'an.lang': 'Language: ',
      'an.failed': 'Analysis failed',
      'an.running': 'Analyzing…',
      'profile.load_failed': 'Failed to load the profile',
      'profile.loading': 'Loading…',
      'profile.empty': 'No profile yet (this chat just synced — try again shortly)',
      'profile.stage': 'Stage: ',
      'profile.lang': 'Language: ',
      'profile.intimacy': 'Intimacy: ',
      'profile.msgs': 'Messages: ',
      'kb.search_failed': 'Search failed',
      'kb.no_match': 'No matching entries',
      'kb.entry': 'Entry',
      'kb.hint_input': 'Type a keyword to search the knowledge base',
      'kb.searching': 'Searching…',
      'tpl.load_failed': 'Failed to load templates',
      'tpl.empty': 'No quick-reply templates yet',
      'notice.update_restart': 'Restart to update',
      'notice.open_detail': 'View details',
      'notice.restarting': 'Restarting…',
      'inject.wait': 'Waiting for injection…',
      'inject.wait.d': 'The injection script has not reported any status yet',
      'inject.unsupported': 'No injection profile',
      'inject.unsupported.d': 'This platform has no selector profile, so the feature is unavailable',
      'inject.no_chat': 'Not signed in / no conversation open',
      'inject.no_chat.d': 'No conversation or composer detected: scan the QR to sign in, then open a chat',
      'inject.mismatch_composer': 'Selector mismatch (composer)',
      'inject.mismatch_composer.d': 'The composer was not found — injection may be broken by a vendor UI change (recalibrate PROFILES.composer)',
      'inject.mismatch_bubble': 'Selector mismatch (messages)',
      'inject.mismatch_bubble.d': 'The chat is open but no message bubbles can be found (recalibrate PROFILES.bubble/text)',
      'inject.mismatch_text': 'Selector mismatch (message text)',
      'inject.mismatch_text.d': 'Found {bubbles} bubbles but could not extract text/media from any of them (recalibrate PROFILES.bubbleText)',
      'inject.mismatch_ingest': 'Selector mismatch (message ids)',
      'inject.mismatch_ingest.d': '{tried} messages pending sync have no msg_id / chat id — syncing stopped silently (recalibrate mid/peerId)',
      'inject.ok': 'Injection healthy',
      'inject.ok.d': 'Composer ✓　message bubbles ×{bubbles}',
      'health.session_online': 'Online',
      'health.session_offline': 'Offline',
      'health.unknown': 'Unknown',
      'health.inject_unsupported': 'No injection profile',
      'health.inject_ok': 'Injection healthy',
      'health.inject_warn': 'Injection mismatch',
      'health.inject_bad': 'Injection error',
      'health.inject_wait': 'Waiting for injection',
      'health.translate_ok': 'Translation healthy',
      'health.translate_down': 'Translation unreachable',
      'health.all_ok': 'Healthy',
      'health.waiting': 'Waiting',
      'hp.title': '🩺 Automation health',
      'hp.refresh': 'Refresh',
      'hp.sec_inject': 'Injection coverage',
      'hp.sec_outbound': 'Controlled outbound',
      'hp.colon': ': ',
      'hp.inj_persistent': '{n} account(s) persistently mismatched',
      'hp.inj_persistent_h': 'Likely a vendor UI change — hot-fix via selector-profiles (D1)',
      'hp.inj_mismatch': '{n} account(s) with selector mismatch',
      'hp.inj_mismatch_h': 'Brief mismatches self-heal; persistent ones need recalibration',
      'hp.inj_none': 'No injected accounts yet',
      'hp.inj_none_h': 'Reporting starts once you open an embedded official page and sign in',
      'hp.inj_ok': 'All healthy ({n})',
      'hp.ob_failed': '{n} message(s) failed to send',
      'hp.ob_failed_h': 'DOM send did not succeed (recorded as failed — not a false "delivered")',
      'hp.ob_held': '{n} message(s) awaiting review',
      'hp.ob_held_h': 'review_mode: messages are sent only after you release them',
      'hp.ob_active': 'Active: {pending} queued · {claimed} sending',
      'hp.ob_idle': 'Queue idle',
      'hp.st_pending': 'Queued',
      'hp.st_claimed': 'Sending',
      'hp.st_sent': 'Sent',
      'hp.st_failed': 'Failed',
      'hp.st_held': 'In review',
      'hp.st_cancelled': 'Blocked',
      'hp.st_unreported': 'Not reported',
      'hp.act_cancel': 'Block',
      'hp.act_hold': 'Hold',
      'hp.act_edit': 'Rewrite',
      'hp.act_release': 'Release',
      'hp.act_retry': 'Retry',
      'hp.dur_sec': '{n}s',
      'hp.dur_min': '{m}m',
      'hp.dur_min_sec': '{m}m {s}s',
      'hp.dur_hour': '{h}h',
      'hp.dur_hour_min': '{h}h {m}m',
      'hp.for_duration': ' · for {d}',
      'hp.stale': 'stale data',
      'hp.dot_mismatch': '{n} account(s) persistently mismatched (likely a vendor UI change — hot-fix selector-profiles)',
      'hp.dot_default': 'Automation health: injection coverage across accounts + controlled outbound queue',
      'hp.ai_filled': 'Filled in ✓',
      'hp.dot_sla_warn': '{n} message(s) past the review SLA (review_mode) — the customer may be waiting; open 🩺 and release/rewrite',
      'hp.dot_sla_urgent': '🔴 Critical: {n} message(s) past the review SLA (review_mode) — the customer may already be lost; release/rewrite now',
      'hp.val_failed': 'Validation failed: {err}',
      'hp.val_no_backend': 'backend unreachable',
      'hp.val_absent': 'No override file (injection uses the built-in profiles)',
      'hp.val_invalid': 'Invalid JSON: {err}',
      'hp.val_parse_failed': 'parse error',
      'hp.val_ok': 'Valid ✓ {n} platform override(s)',
      'hp.val_dropped': ' · ignored {n} item(s) ({list})',
      'hp.sel_composer': 'composer',
      'hp.sel_sendBtn': 'send button',
      'hp.sel_bubble': 'message bubble',
      'hp.sel_peerTitle': 'chat title',
      'hp.diag': 'Mismatch located: {list} → recalibrate these selector keys first',
      'hp.diag_title': 'Counts how often each selector key came up empty across mismatched accounts, pinpointing what the vendor changed',
      'hp.alerts_hdr': '⚠ Persistent injection mismatch ({n} account(s))',
      'hp.alerts_ft': 'Likely a vendor UI change → click "Hot-fix selectors" to open desktop_selector_profiles.json; it takes effect on the next injection fetch after you save',
      'hp.fix_btn': 'Hot-fix selectors',
      'hp.fix_title': 'Open the override file and fill in the correct selectors per platform — no new desktop build needed',
      'hp.rate_nosample': 'Block rate —(not enough samples)',
      'hp.rate': '7-day block rate {pct} ({n} reviewed)',
      'hp.corr': '{total} correction sample(s) ({edit} rewritten{ai})',
      'hp.corr_ai': ' · {n} AI-assisted',
      'hp.corr_title': 'AI-mistake samples captured from human rewrites / blocks, for offline tuning',
      'hp.export_btn': 'Export samples',
      'hp.export_title': 'Export JSONL preference pairs (rejected/chosen) for DPO / eval',
      'hp.rsn_prompt': 'Reason: ',
      'hp.rsn_off_topic': 'Off topic',
      'hp.rsn_tone': 'Wrong tone',
      'hp.rsn_factual': 'Factually wrong',
      'hp.rsn_over_boundary': 'Out of bounds',
      'hp.rsn_redundant': 'Redundant',
      'hp.rsn_other': 'Other',
      'hp.cluster': 'Mistake clusters: {list}',
      'hp.cluster_title': 'Blocked replies clustered by reason — see what the AI gets wrong most',
      'hp.review_hdr': '🔎 {n} awaiting review (first in, first out)',
      'hp.bulk_release': 'Release all',
      'hp.bulk_cancel': 'Block all',
      'hp.confirm_bulk_cancel': 'Block all {n} messages awaiting review? This cannot be undone.',
      'hp.edit_ph': 'Type the rewritten message…',
      'hp.ai_btn': 'AI rewrite',
      'hp.ai_title': 'Generate a better candidate from the conversation context; you can still edit before saving',
      'hp.save': 'Save',
      'hp.cancel': 'Cancel',
      'hp.no_inject': 'No injection reports yet.',
      'hp.no_outbound': 'No outbound commands (desktop_bridge is off, or no desktop account has auto-reply enabled).',
      'hp.validate_btn': 'Validate overrides',
      'hp.validate_title': 'Validate desktop_selector_profiles.json: is the JSON valid, are any fields ignored',
      'hp.reload_btn': 'Reload injection',
      'hp.reload_title': 'Reload the embedded official pages → injection re-fetches selectors (takes effect right after a hot-fix, no restart)',
      'hp.footer': 'Auto-refreshes every 10s · persistent mismatches can be hot-fixed in config/desktop_selector_profiles.json',
      'hp.opening': 'Opening…',
      'hp.opened': 'Opened ✓',
      'hp.open_failed': 'Could not open',
      'hp.camp': 'Campaign poster',
      'hp.camp_shown': 'Shown ({id})',
      'hp.camp_preview': 'Preview mode (--poster-preview)',
      'hp.camp_skip': 'Not shown: {why}',
      'hp.camp_no_query': 'Not queried this session yet',
      'hp.camp_feed': 'feed: {n}',
      'hp.camp_r_no_feed': 'campaign feed not fetched (offline/first-boot race; self-heals next launch)',
      'hp.camp_r_not_managed': 'not a managed build (dev mode never shows — expected)',
      'hp.camp_r_no_onboarding': 'first-run wizard not completed (new-user poster anchors on it)',
      'hp.camp_r_window_passed': 'new-user 72h window passed (old install — expected)',
      'hp.camp_r_max_shows': 'show quota used up',
      'hp.camp_r_interval': 'still inside the 12h show cooldown',
      'hp.camp_r_opted_out': '"Don\'t show again" was clicked on this machine',
      'hp.camp_r_not_started': 'campaign not started yet',
      'hp.camp_r_ended': 'campaign ended',
      'hp.camp_hint': 'To force it for acceptance: launch with --poster-preview (skips eligibility/frequency, never pollutes stats)',
      'hp.validating': 'Validating…',
      'hp.val_fail_short': 'Validation failed',
      'hp.exporting': 'Exporting…',
      'hp.exported': 'Exported {n} ✓',
      'hp.export_failed': 'Export failed',
      'hp.reloaded': 'Reloaded {n} embedded page(s) (injection will re-fetch selectors)',
      'hp.reload_none': 'No embedded official pages (currently in Manual Console mode)',
      'hp.read_failed': 'Failed to read health data: {err}',
      'hp.generating': 'Generating…',
      'hp.no_context': 'No context',
      'hp.failed_short': 'Failed',
      'hp.sla_urgent_title': '🔴 Review critically overdue',
      'hp.sla_urgent_body': '{n} AI replies have been awaiting review for over {mins} minutes — the customer is very likely lost, act now',
      'hp.sla_warn_title': 'Review overdue',
      'hp.sla_warn_body': '{n} AI replies have been awaiting human review for over {mins} minutes — please release or rewrite soon',
      'boot.title': 'Starting the backend service…',
      'boot.sub': 'The first launch takes about a minute; the business panel opens automatically once ready',
      'boot.connecting': 'Connecting to the backend service… (the business panel opens automatically once ready)',
      'boot.failed': 'Backend service failed to start. Restart the app; if it keeps happening contact ops (log: logs/backend.log)',
      'boot.port_conflict': 'The port is taken by another program, so the backend service cannot start — please contact ops',
      'appframe.title': 'Unified business panel',
      'splash.brand.co': 'BOUNDLESS TECH',
      'splash.tagline': 'Multi-platform AI conversations · human-AI workbench',
      'splash.skip': 'Skip intro',
      'splash.real': '[STAGE {n}/6] {stage} · {secs}s',
      'splash.real_eta': ' · ~{secs}s left',
      'splash.real_slow': ' · first launch is slower (one-time unpack & init)',
      'splash.err.title': 'BOOT SEQUENCE INTERRUPTED',
      'splash.err.retry': 'Retry',
      'splash.err.copy': 'Copy diagnostics',
      'splash.err.copied': 'Copied ✓',
      'splash.stage.shell': 'Initializing desktop environment',
      'splash.stage.probe': 'Probing local services',
      'splash.stage.engine': 'Starting the AI engine',
      'splash.stage.link': 'Establishing encrypted session',
      'splash.stage.load': 'Loading the workbench',
      'splash.stage.done': 'Ready',
      'splash.term.shell': '✓ Desktop environment initialized',
      'splash.term.probe': '✓ Local services probed',
      'splash.term.engine': '✓ AI engine online',
      'splash.term.link': '✓ Encrypted session established',
      'splash.term.load': '✓ Workbench interface ready',
      'splash.amb.shell.0': 'Kernel loaded · welcome back to BOUNDLESS',
      'splash.amb.probe.0': 'Scanning local compute nodes…',
      'splash.amb.probe.1': 'Mapping the network topology…',
      'splash.amb.engine.0': 'Waking the quantum compute cluster…',
      'splash.amb.engine.1': 'Assembling deep neural network layers…',
      'splash.amb.engine.2': 'Igniting the neuron matrix · 1,024 agents standing by',
      'splash.amb.engine.3': 'Loading persona cores…',
      'splash.amb.engine.4': 'Preheating the voiceprint reactor…',
      'splash.amb.engine.5': 'Injecting affective computing modules…',
      'splash.amb.engine.6': 'Pouring the AI a cup of electron espresso ☕',
      'splash.amb.link.0': 'Establishing a hardened encrypted tunnel…',
      'splash.amb.link.1': 'Quantum key exchange complete · 256-bit channel',
      'splash.amb.link.2': 'Defense matrix online · risk shield at 100%',
      'splash.amb.load.0': 'Unfolding the holographic console…',
      'splash.amb.load.1': 'Syncing conversation time zones worldwide…',
      'splash.amb.load.2': 'Translation engines online · 17 languages standing by',
      'splash.amb.load.3': 'Weaving the cross-platform comm grid…',
      'splash.amb.done.0': 'BOUNDLESS NETWORK · LINKED',
      'splash.amb.done.1': 'Interstellar handshake complete · welcome back, Commander',
      // Merged titlebar strip (titlebar merge P2 2026-08-22): ⋯ emergency menu
      'tb.more': 'About / Send Diagnostics / Check for Updates',
      'tb.about': 'About',
      'tb.diag': 'Send Diagnostics to Support',
      'tb.update': 'Check for Updates',
    },
  };

  var LS_KEY = 'aitr.shell.lang';

  /* 与 main.js::shellLang 同一口径：显式 en / en-* → en；显式扩展语（后端
     i18n_packs.UI_LANGS：vi/th/id/zh_hant）原样保留——DICT 无该语时取词按表定底
     回落（vi/th/id→en，zh_hant→zh）；其余（空/未知）全部 zh。 */
  var EXT_LANGS = { vi: 'en', th: 'en', id: 'en', zh_hant: 'zh' }; /* 码→词典回落底 */
  function _norm(raw) {
    return (raw == null ? '' : String(raw)).trim().toLowerCase().replace(/-/g, '_');
  }
  function normalizeLang(raw) {
    var s = _norm(raw);
    if (s === 'en' || s.indexOf('en_') === 0) return 'en';
    if (s === 'zh_hant' || s === 'zh_tw' || s === 'zh_hk') return 'zh_hant';
    var b = s.split('_')[0];
    if (EXT_LANGS[b]) return b;
    return 'zh';
  }

  function isExplicit(raw) {
    var s = _norm(raw);
    return s === 'en' || s === 'zh' || s.indexOf('en_') === 0 || s.indexOf('zh_') === 0 ||
      !!EXT_LANGS[s.split('_')[0]];
  }

  function _urlParam(n) {
    try { return new URLSearchParams(location.search).get(n) || ''; } catch (e) { return ''; }
  }
  function _lsGet() {
    try { return localStorage.getItem(LS_KEY) || ''; } catch (e) { return ''; }
  }
  function _lsSet(v) {
    try { localStorage.setItem(LS_KEY, v); } catch (e) { /* 隐私模式/配额，忽略 */ }
  }

  /* ①?lang= → ②localStorage → ③<html lang>；每级都要求「显式」才采信，
     否则继续往下找（避免空串被 normalize 成 zh 就地截断解析链）。 */
  function resolveLang() {
    var q = _urlParam('lang');
    if (isExplicit(q)) { var lq = normalizeLang(q); _lsSet(lq); return lq; }
    var c = _lsGet();
    if (isExplicit(c)) return normalizeLang(c);
    var h = '';
    try { h = document.documentElement.getAttribute('lang') || ''; } catch (e) { h = ''; }
    return normalizeLang(h);
  }

  var LANG = (typeof document !== 'undefined' && typeof location !== 'undefined') ? resolveLang() : 'zh';

  /* tIn = 显式指定语言取词（门禁可在 Node 下断言两语的回落链；t() 是它的绑定壳）。
     词典回落：扩展语按 EXT_LANGS 表定底（vi/th/id→en，zh_hant→zh）——词典整体缺
     （ext 未装）与单键缺都先走底语言，再落 zh，最后回键名。 */
  function tIn(lang, key, vars) {
    if (key == null) return '';
    var d = DICT[lang] || DICT[EXT_LANGS[lang]] || DICT.zh;
    var b = DICT[EXT_LANGS[lang]] || DICT.zh;
    var s = (d[key] != null) ? d[key]
      : (b[key] != null) ? b[key]
        : (DICT.zh[key] != null ? DICT.zh[key] : null);
    if (s == null) return String(key);
    if (vars) {
      for (var k in vars) {
        if (Object.prototype.hasOwnProperty.call(vars, k)) s = s.split('{' + k + '}').join(String(vars[k]));
      }
    }
    return s;
  }
  function t(key, vars) { return tIn(LANG, key, vars); }

  /* 只替换元素**自有文本节点**，保留子元素——卡头是
     `<span class="cp-card-ttl"><span class="cp-card-ic"></span>回复工坊</span>`，
     用 textContent 会把图标 span 一起抹掉。 */
  function setOwnText(el, txt) {
    var found = false, i, n;
    for (i = 0; i < el.childNodes.length; i++) {
      n = el.childNodes[i];
      if (n.nodeType === 3) {
        if (!found && String(n.nodeValue).trim() !== '') { n.nodeValue = txt; found = true; }
        else if (found) { n.nodeValue = ''; }
      }
    }
    if (!found) el.appendChild(document.createTextNode(txt));
  }

  var SPECS = [
    ['data-sh-i18n', function (el, s) { el.textContent = s; }],
    ['data-sh-i18n-txt', setOwnText],
    ['data-sh-i18n-ph', function (el, s) { el.setAttribute('placeholder', s); }],
    ['data-sh-i18n-title', function (el, s) { el.setAttribute('title', s); }],
    ['data-sh-i18n-aria', function (el, s) { el.setAttribute('aria-label', s); }],
  ];

  function applyI18n(rootEl) {
    rootEl = rootEl || document;
    for (var s = 0; s < SPECS.length; s++) {
      var attr = SPECS[s][0], set = SPECS[s][1];
      var ns = rootEl.querySelectorAll('[' + attr + ']');
      for (var i = 0; i < ns.length; i++) {
        var k = ns[i].getAttribute(attr);
        if (k) { try { set(ns[i], t(k)); } catch (e) { /* 单条失败不拖垮整轮 */ } }
      }
    }
  }

  /* 扩展语词典后装（shell-i18n-ext.js 生成文件调用，紧跟本文件的同步 <script>）。
     LANG 本就解析成扩展语码（normalizeLang 认 zh_hant 等），装载即生效；装载前
     取词由 tIn 的底语言链兜着（zh_hant→zh），不闪键名。 */
  function registerExt(lang, dict) {
    var lg = normalizeLang(lang);
    if (!dict || !EXT_LANGS[lg]) return;
    var d = DICT[lg] || (DICT[lg] = {});
    for (var k in dict) { if (Object.prototype.hasOwnProperty.call(dict, k)) d[k] = dict[k]; }
  }

  root.shellI18n = {
    lang: LANG,
    t: t,
    tIn: tIn,
    applyI18n: applyI18n,
    normalizeLang: normalizeLang,
    isExplicit: isExplicit,
    registerExt: registerExt,
    _dict: DICT,
    _lsKey: LS_KEY,
  };
  root.SH = t;

  /* 浏览器侧同步副作用：<html lang> + document.title + CP_LANG 必须在 body
     解析与 cp-i18n.js 加载之前就位（本文件在 <head> 内加载即满足）。
     <html lang> 用规范 BCP-47 值（zh_hant→zh-Hant，字体/断行/读屏都认标准标签）。 */
  var HTML_LANGS = { zh_hant: 'zh-Hant' };
  if (typeof document !== 'undefined') {
    try { document.documentElement.setAttribute('lang', HTML_LANGS[LANG] || LANG); } catch (e) { /* ignore */ }
    try { document.title = t('app.title'); } catch (e) { /* ignore */ }
    if (root.CP_LANG !== 'zh' && root.CP_LANG !== 'en') root.CP_LANG = LANG;
    if (document.readyState === 'loading') {
      document.addEventListener('DOMContentLoaded', function () { applyI18n(document); });
    } else {
      applyI18n(document);
    }
  }
})(typeof window !== 'undefined' ? window : globalThis);

/* Node 侧（desktop/test/*.test.js 门禁）以 CommonJS 取用：本文件无 DOM 依赖的
   纯函数部分（dict / normalizeLang / t）可直接断言。 */
if (typeof module !== 'undefined' && module.exports) {
  module.exports = (typeof window !== 'undefined' ? window : globalThis).shellI18n;
}
